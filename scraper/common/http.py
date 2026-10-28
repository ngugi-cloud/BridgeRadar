"""Polite HTTP client shared by every engine.

Guarantees:
  * robots.txt is fetched and obeyed for every origin (fails CLOSED: if robots.txt
    cannot be read cleanly, the origin is treated as disallowed).
  * Crawl-delay is honoured; otherwise a minimum gap per domain is enforced.
  * Honest User-Agent from config (includes a contact email).
  * Responses are cached on disk (TTL from config).
  * 403/429 are never worked around. Repeated blocks stop that domain
    (SourceBlocked) and the caller flags the source as manual.
  * No login, no CAPTCHA handling, no proxy rotation.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time
import urllib.robotparser
from collections import defaultdict
from urllib.parse import urlencode, urlparse

import requests

PRODUCT_TOKEN = "BridgeLeadRadar"


class SourceBlocked(Exception):
    """The site is refusing us (403/429 repeatedly) or our request budget is spent."""


class RobotsDisallowed(Exception):
    """robots.txt does not allow this URL (or could not be read cleanly)."""


class PoliteClient:
    def __init__(self, settings: dict, cache_dir, session=None,
                 sleep=time.sleep, clock=time.monotonic, wall=time.time):
        self.ua = settings["user_agent"]
        self.min_gap = float(settings.get("min_seconds_between_requests", 2))
        self.default_budget = int(settings.get("max_requests_per_source_per_run", 200))
        self.cache_ttl = float(settings.get("cache_ttl_hours", 20)) * 3600
        self.timeout = float(settings.get("request_timeout_seconds", 20))
        self.retries = int(settings.get("retry_count", 2))
        self.max_errors = int(settings.get("stop_source_after_consecutive_errors", 3))
        self.cache_dir = pathlib.Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
        })
        self._sleep, self._clock, self._wall = sleep, clock, wall
        self._robots: dict[str, object] = {}
        self._last: dict[str, float] = {}
        self._errors: dict[str, int] = defaultdict(int)
        self.budget = self.default_budget
        self.requests_this_source = 0
        self.stats = {"network_requests": 0, "cache_hits": 0, "robots_denied": 0}

    # ---- per-source bookkeeping -------------------------------------------
    def begin_source(self, budget: int | None = None) -> None:
        self.budget = int(budget or self.default_budget)
        self.requests_this_source = 0
        self._errors.clear()

    # ---- robots.txt --------------------------------------------------------
    @staticmethod
    def _origin(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def _load_robots(self, origin: str):
        if origin in self._robots:
            return self._robots[origin]
        domain = urlparse(origin).netloc
        self._wait(domain)
        try:
            r = self.session.get(origin + "/robots.txt", timeout=self.timeout)
            if r.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                self._robots[origin] = rp
            elif r.status_code in (404, 410):
                self._robots[origin] = "allow_all"
            else:  # 401/403/5xx: cannot read cleanly -> be conservative
                self._robots[origin] = "deny_all"
        except requests.RequestException:
            self._robots[origin] = "deny_all"
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        rp = self._load_robots(self._origin(url))
        if rp == "allow_all":
            return True
        if rp == "deny_all":
            return False
        return rp.can_fetch(PRODUCT_TOKEN, url)

    def _crawl_delay(self, url: str) -> float:
        rp = self._robots.get(self._origin(url))
        if isinstance(rp, urllib.robotparser.RobotFileParser):
            d = rp.crawl_delay(PRODUCT_TOKEN)
            return float(d) if d else 0.0
        return 0.0

    # ---- rate limit --------------------------------------------------------
    def _wait(self, domain: str, extra: float = 0.0) -> None:
        gap = max(self.min_gap, extra)
        last = self._last.get(domain)
        now = self._clock()
        if last is not None and now - last < gap:
            self._sleep(gap - (now - last))
            now = self._clock()
        self._last[domain] = now

    # ---- cache -------------------------------------------------------------
    def _cache_path(self, key: str) -> pathlib.Path:
        return self.cache_dir / f"{key}.txt"

    def _cache_get(self, key: str):
        p = self._cache_path(key)
        if p.exists() and (self._wall() - p.stat().st_mtime) < self.cache_ttl:
            return p.read_text(encoding="utf-8")
        return None

    def _cache_put(self, key: str, text: str) -> None:
        self._cache_path(key).write_text(text, encoding="utf-8")

    # ---- main entry --------------------------------------------------------
    def get(self, url: str, params: dict | None = None, json_body: dict | None = None):
        """Return response text, or None (404 / soft failure).

        Raises RobotsDisallowed or SourceBlocked. Callers must catch both and
        flag the source/domain rather than retry.
        """
        method = "POST" if json_body is not None else "GET"
        if self.requests_this_source >= self.budget:
            raise SourceBlocked("request budget exhausted")
        if not self.allowed(url):
            self.stats["robots_denied"] += 1
            raise RobotsDisallowed(url)

        full = url + ("?" + urlencode(params, doseq=True) if params else "")
        key = hashlib.sha1(
            f"{method}|{full}|{json.dumps(json_body, sort_keys=True)}".encode()
        ).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached

        domain = urlparse(url).netloc
        if self._errors[domain] >= self.max_errors:
            raise SourceBlocked(f"too many errors from {domain}")

        for attempt in range(self.retries + 1):
            self._wait(domain, self._crawl_delay(url))
            self.requests_this_source += 1
            self.stats["network_requests"] += 1
            try:
                r = self.session.request(method, url, params=params, json=json_body,
                                         timeout=self.timeout)
            except requests.RequestException:
                if attempt == self.retries:
                    self._errors[domain] += 1
                    return None
                self._sleep(2 ** attempt)
                continue
            sc = r.status_code
            if sc == 200:
                self._errors[domain] = 0
                self._cache_put(key, r.text)
                return r.text
            if sc in (403, 429):
                self._errors[domain] += 1
                if sc == 429 or self._errors[domain] >= self.max_errors:
                    raise SourceBlocked(f"HTTP {sc} from {domain}")
                return None
            if 500 <= sc < 600:
                if attempt < self.retries:
                    self._sleep(2 ** attempt)
                    continue
                self._errors[domain] += 1
            return None
        return None
