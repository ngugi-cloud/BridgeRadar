"""ENGINE 1 - Active job leads (Kenya only).

Pipeline:  gate -> adapter -> normalise -> scope/expiry filter -> classify ->
           dedupe (company+title+location, newest wins) -> merge with history ->
           write active_jobs.csv, closed_jobs.csv, changes.csv, scrape_log.csv,
           manual_integration_required.csv

Stores structured fields and links only. Ad text is read transiently for
classification and is never saved (copyright).

Run:  python -m scraper.engine1_jobs            (live)
      python -m scraper.engine1_jobs --dry-run  (no files written)
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
from collections import Counter, defaultdict
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .common.classify import Classifier
from .common.config import CACHE_DIR, DATA_DIR, CONFIG_DIR, gate, load_yaml
from .common.http import PoliteClient, RobotsDisallowed, SourceBlocked
from .common.store import (CHANGE_FIELDS, LOG_FIELDS, MANUAL_FIELDS, append_csv,
                           log_row, read_csv, write_csv)
from .common.textutil import (clean, is_confidential, looks_like_agency, make_job_id,
                              normalize_company, out_of_scope, parse_date)

ENGINE = "engine1_jobs"
CLOSE_AFTER_MISSING_RUNS = 3      # not seen for 3 successful source runs -> closed
STALE_DAYS = 30                   # not seen at all for 30 days -> closed
CLOSED_RETENTION_DAYS = 60        # keep closed rows for time-to-fill stats

FIELDS = [
    "job_id", "title", "company", "location", "posted_date", "expiry_date",
    "source_id", "source_url", "category", "vertical", "sector",
    "classification_confidence", "seniority", "role_family", "employment_type",
    "posted_by", "urgency_flag", "also_seen_on", "source_count", "first_seen",
    "last_seen", "days_open", "repost_count", "status", "missing_runs",
    "closed_date", "first_seen_baseline",
]


# =============================================================================
# Adapters. Each returns a list of RAW dicts:
#   title, company, location, posted_date, expiry_date, url, category,
#   employment_type, text (transient, classification only)
# =============================================================================
def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        if "@graph" in node:
            yield from _walk_jsonld(node["@graph"])


def _loc_from_jobposting(node) -> str:
    locs = node.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs]
    parts = []
    for loc in locs:
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict):
            country = addr.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts += [addr.get("addressLocality"), addr.get("addressRegion"), country]
        elif isinstance(addr, str):
            parts.append(addr)
    return ", ".join(clean(p) for p in parts if clean(p))


def extract_jobposting_jsonld(html: str, page_url: str) -> list[dict]:
    """Parse schema.org JobPosting markup (public, structured, designed for crawlers)."""
    out = []
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        for node in _walk_jsonld(data):
            t = node.get("@type")
            if "JobPosting" not in (t if isinstance(t, list) else [t]):
                continue
            org = node.get("hiringOrganization")
            company = org.get("name") if isinstance(org, dict) else org
            emp = node.get("employmentType")
            emp = ", ".join(emp) if isinstance(emp, list) else emp
            desc = BeautifulSoup(str(node.get("description") or ""), "html.parser").get_text(" ")
            out.append({
                "title": node.get("title"), "company": company,
                "location": _loc_from_jobposting(node),
                "posted_date": node.get("datePosted"), "expiry_date": node.get("validThrough"),
                "url": node.get("url") or page_url,
                "category": node.get("occupationalCategory") or node.get("industry") or "",
                "employment_type": emp, "text": desc[:600],
            })
    return out


_CAREER_LINK = re.compile(r"(job|career|vacanc|position|opening|opportunit)", re.I)


def scan_careers_page(client: PoliteClient, target: dict, max_links: int = 15) -> list[dict]:
    """Company career page: JSON-LD on the page, then on same-domain job links.

    Limitation: pages rendered purely by JavaScript expose no markup to us.
    """
    url = target["careers_url"]
    html = client.get(url)
    if not html:
        return []
    raws = extract_jobposting_jsonld(html, url)
    base_host = urlparse(url).netloc
    seen, links = set(), []
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if urlparse(href).netloc != base_host or href in seen or href == url:
            continue
        if _CAREER_LINK.search(href) or _CAREER_LINK.search(a.get_text(" ")):
            seen.add(href)
            links.append(href)
    for href in links[:max_links]:
        page = client.get(href)
        if page:
            raws += extract_jobposting_jsonld(page, href)
    for r in raws:
        r["company"] = r.get("company") or target["company"]
    return raws


def parse_greenhouse(text: str, target: dict) -> list[dict]:
    try:
        jobs = json.loads(text).get("jobs", [])
    except (ValueError, AttributeError):
        return []
    return [{"title": j.get("title"), "company": target["company"],
             "location": (j.get("location") or {}).get("name", ""),
             "posted_date": j.get("first_published"),   # blank if not provided: never use updated_at
             "expiry_date": None, "url": j.get("absolute_url"), "category": "",
             "employment_type": "", "text": ""} for j in jobs]


def parse_lever(text: str, target: dict) -> list[dict]:
    try:
        jobs = json.loads(text)
    except ValueError:
        return []
    out = []
    for j in jobs if isinstance(jobs, list) else []:
        cat = j.get("categories") or {}
        out.append({"title": j.get("text"), "company": target["company"],
                    "location": cat.get("location", ""), "posted_date": j.get("createdAt"),
                    "expiry_date": None, "url": j.get("hostedUrl"),
                    "category": cat.get("team", ""), "employment_type": cat.get("commitment", ""),
                    "text": ""})
    return out


def load_career_targets() -> list[dict]:
    """Seed list (config/career_pages.csv) + master list columns when Engine 2 has run."""
    rows = read_csv(CONFIG_DIR / "career_pages.csv")
    for r in read_csv(DATA_DIR / "kenya_companies_master.csv"):
        if r.get("careers_page_url"):
            rows.append({"company": r.get("trading_name") or r.get("legal_name"),
                         "careers_url": r["careers_page_url"],
                         "ats_type": r.get("ats_type", ""), "ats_token": r.get("ats_token", "")})
    seen, out = set(), []
    for r in rows:
        key = (clean(r.get("company")).lower(), clean(r.get("careers_url")))
        if r.get("company") and r.get("careers_url") and key not in seen:
            seen.add(key)
            out.append({k: clean(r.get(k)) for k in ("company", "careers_url", "ats_type", "ats_token")})
    return out


def adapter_company_ats(client: PoliteClient, src: dict, ctx: dict) -> list[dict]:
    raws = []
    for t in ctx["career_targets"]:
        try:
            kind, token = t.get("ats_type", "").lower(), t.get("ats_token", "")
            if kind == "greenhouse" and token:
                text = client.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
                raws += parse_greenhouse(text or "", t)
            elif kind == "lever" and token:
                text = client.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
                raws += parse_lever(text or "", t)
            else:
                raws += scan_careers_page(client, t)
        except RobotsDisallowed:
            ctx["blocked"].append((f"{src['id']}:{urlparse(t['careers_url']).netloc}", "robots_disallowed", t["careers_url"]))
        except SourceBlocked as e:
            ctx["blocked"].append((f"{src['id']}:{urlparse(t['careers_url']).netloc}", f"blocked:{e}", t["careers_url"]))
    return raws


def adapter_reliefweb(client: PoliteClient, src: dict, ctx: dict) -> list[dict]:
    """ReliefWeb v2 API (NGO jobs; outside the private-sector ICP, off by default).

    VERIFY on first live run: field names below follow my reading of the v2 docs.
    """
    appname = os.environ[src["secret"]]
    url = f"https://api.reliefweb.int/v2/jobs?appname={appname}"
    body = {"limit": 100, "filter": {"field": "country.iso3", "value": "KEN"},
            "fields": {"include": ["title", "source.name", "date.created", "date.closing",
                                   "url_alias", "country.name", "city.name",
                                   "career_categories.name", "type.name"]}}
    text = client.get(url, json_body=body)
    if not text:
        return []
    out = []
    for item in json.loads(text).get("data", []):
        f = item.get("fields", {})
        first = lambda k: (f.get(k) or [{}])[0].get("name", "") if isinstance(f.get(k), list) else ""
        out.append({"title": f.get("title"), "company": first("source"),
                    "location": ", ".join(x for x in (first("city"), first("country")) if x),
                    "posted_date": (f.get("date") or {}).get("created"),
                    "expiry_date": (f.get("date") or {}).get("closing"),
                    "url": f.get("url_alias") or f.get("url"),
                    "category": first("career_categories"), "employment_type": first("type"),
                    "text": ""})
    return out


# Job-board adapters (MyJobMag, Talent.com, KenyaMoja ...) are intentionally NOT
# built yet. Each needs a human-confirmed ToS + robots review first; we add an
# adapter only after you approve that source.
ADAPTERS = {"company_ats": adapter_company_ats, "reliefweb": adapter_reliefweb}


# =============================================================================
# Pure pipeline functions (unit-tested offline)
# =============================================================================
def to_candidate(raw: dict, source_id: str, clf: Classifier, agency_norms: set,
                 today: dt.date) -> tuple[dict | None, str]:
    title, company = clean(raw.get("title")), clean(raw.get("company"))
    if not title or not company:
        return None, "missing_title_or_company"
    location = clean(raw.get("location"))
    if out_of_scope(location):
        return None, "out_of_scope"
    posted = parse_date(raw.get("posted_date"), today)
    expiry = parse_date(raw.get("expiry_date"), today)
    if expiry and expiry < today:
        return None, "expired"
    sector, vertical, conf = clf.sector(title, company, f"{raw.get('category', '')} {raw.get('text', '')}")
    if is_confidential(company):
        posted_by = "confidential"
    elif normalize_company(company) in agency_norms or looks_like_agency(company):
        posted_by = "agency"
    else:
        posted_by = "unknown"          # never assume "employer"
    return {
        "job_id": make_job_id(company, title, location), "title": title, "company": company,
        "location": location, "posted_date": posted.isoformat() if posted else "",
        "expiry_date": expiry.isoformat() if expiry else "", "source_id": source_id,
        "source_url": clean(raw.get("url")), "category": clean(raw.get("category")),
        "vertical": vertical, "sector": sector, "classification_confidence": conf,
        "seniority": clf.seniority(title), "role_family": clf.role_family(title),
        "employment_type": clean(raw.get("employment_type")), "posted_by": posted_by,
        "urgency_flag": "1" if clf.is_urgent(title, raw.get("text")) else "",
    }, "ok"


def merge(cands: list[dict], prev_rows: list[dict], ran_ok: set, today: dt.date,
          baseline: bool) -> tuple[list[dict], list[dict], list[dict]]:
    """Dedupe + merge with history. Returns (open_rows, closed_rows, changes)."""
    t = today.isoformat()
    prev = {r["job_id"]: r for r in prev_rows}
    best, srcs = {}, defaultdict(set)
    for c in cands:
        srcs[c["job_id"]].add(c["source_id"])
        cur = best.get(c["job_id"])
        if cur is None or (c["posted_date"] or "") > (cur["posted_date"] or ""):
            best[c["job_id"]] = c          # newest posting date wins

    open_rows, closed_rows, changes = [], [], []

    def change(kind, row, summary):
        changes.append({"date": t, "change_type": kind, "entity_type": "job",
                        "entity_id": row["job_id"], "company": row["company"],
                        "summary": summary, "source_url": row.get("source_url", "")})

    for jid, c in best.items():
        p, row = prev.get(jid), dict(c)
        row.update(also_seen_on=";".join(sorted(srcs[jid] - {c["source_id"]})),
                   source_count=len(srcs[jid]), last_seen=t, missing_runs=0,
                   status="open", closed_date="")
        if p:
            row["first_seen"] = p["first_seen"]
            row["first_seen_baseline"] = p.get("first_seen_baseline", "")
            rc = int(p.get("repost_count") or 0)
            if p.get("status") == "closed":
                rc += 1
                change("job_reopened", row, f"{row['title']} reappeared")
            elif c["posted_date"] and p.get("posted_date") and c["posted_date"] > p["posted_date"]:
                rc += 1
            row["repost_count"] = rc
        else:
            row.update(first_seen=t, repost_count=0, first_seen_baseline="1" if baseline else "")
            if not baseline:
                change("new_job", row, f"{row['title']} ({row['location'] or 'location n/a'})")
        open_rows.append(row)

    for jid, p in prev.items():
        if jid in best:
            continue
        p = dict(p)
        if p.get("status") == "closed":
            cd = parse_date(p.get("closed_date"), today)
            if cd and (today - cd).days <= CLOSED_RETENTION_DAYS:
                closed_rows.append(p)
            continue
        exp = parse_date(p.get("expiry_date"), today)
        last = parse_date(p.get("last_seen"), today) or today
        missing = int(p.get("missing_runs") or 0)
        if p.get("source_id") in ran_ok:
            missing += 1
        p["missing_runs"] = missing
        if (exp and exp < today) or missing >= CLOSE_AFTER_MISSING_RUNS or (today - last).days > STALE_DAYS:
            p.update(status="closed", closed_date=t)
            change("job_closed", p, f"{p.get('title', '')} no longer listed")
            closed_rows.append(p)
        else:
            open_rows.append(p)

    for r in open_rows:
        fs = parse_date(r.get("first_seen"), today) or today
        r["days_open"] = (today - fs).days
    return open_rows, closed_rows, changes


# =============================================================================
# Runner
# =============================================================================
def run(dry_run: bool = False, today: dt.date | None = None, client: PoliteClient | None = None,
        data_dir=None, config_dir=None) -> dict:
    today = today or dt.date.today()
    data_dir = data_dir or DATA_DIR
    sources_cfg = load_yaml("sources.yaml", config_dir)
    settings = sources_cfg["settings"]
    clf = Classifier(load_yaml("sector_map.yaml", config_dir))
    client = client or PoliteClient(settings, CACHE_DIR)
    agency_norms = {normalize_company(r.get("name", "")) for r in read_csv(data_dir / "competitor_agencies.csv")}
    ctx = {"career_targets": load_career_targets(), "blocked": []}

    log, manual, cands, ran_ok = [], [], [], set()
    reasons = Counter()
    for src in sources_cfg.get("engine1_jobs", []):
        ok, why = gate(src, settings)
        if not ok:
            log.append(log_row(ENGINE, src["id"], f"skipped:{why}", today=today))
            if why == "manual_required":
                manual.append({"source_id": src["id"], "name": src.get("name", ""),
                               "reason": src.get("finding", "")[:300],
                               "helper_url": src.get("manual_helper", ""), "date": today.isoformat()})
            continue
        adapter = ADAPTERS.get(src["id"])
        if not adapter:
            log.append(log_row(ENGINE, src["id"], "skipped:no_adapter_built", today=today))
            continue
        client.begin_source(src.get("max_requests"))
        try:
            raws = adapter(client, src, ctx)
        except (RobotsDisallowed, SourceBlocked) as e:
            log.append(log_row(ENGINE, src["id"], "blocked", detail=str(e), today=today))
            manual.append({"source_id": src["id"], "name": src.get("name", ""),
                           "reason": f"blocked at run time: {e}", "helper_url": src.get("manual_helper", ""),
                           "date": today.isoformat()})
            continue
        except Exception as e:                       # one broken source must not stop the run
            log.append(log_row(ENGINE, src["id"], "error", detail=f"{type(e).__name__}: {e}"[:300], today=today))
            continue
        n_before = len(cands)
        for raw in raws:
            cand, reason = to_candidate(raw, src["id"], clf, agency_norms, today)
            reasons[reason] += 1
            if cand:
                cands.append(cand)
        kept = len(cands) - n_before
        status = "ok" if kept else "zero_results"    # zero results = suspicious, not counted as a clean run
        if kept:
            ran_ok.add(src["id"])
        log.append(log_row(ENGINE, src["id"], status, kept, f"raw={len(raws)} dropped={dict(reasons)}", today))

    for name, reason, helper in ctx["blocked"]:      # per-company blocks inside company_ats
        manual.append({"source_id": name, "name": name, "reason": reason, "helper_url": helper,
                       "date": today.isoformat()})

    prev = read_csv(data_dir / "active_jobs.csv") + read_csv(data_dir / "closed_jobs.csv")
    baseline = len(prev) == 0
    open_rows, closed_rows, changes = merge(cands, prev, ran_ok, today, baseline)
    if baseline and open_rows:
        log.append(log_row(ENGINE, "_run", "baseline_run", len(open_rows),
                           "first run: new-today and velocity are unreliable for ~30 days", today))

    summary = {"open": len(open_rows), "closed": len(closed_rows), "changes": len(changes),
               "manual": len(manual), "baseline": baseline, "dropped": dict(reasons)}
    if dry_run:
        return summary
    write_csv(data_dir / "active_jobs.csv", open_rows, FIELDS)
    write_csv(data_dir / "closed_jobs.csv", closed_rows, FIELDS)
    append_csv(data_dir / "changes.csv", changes, CHANGE_FIELDS)
    append_csv(data_dir / "scrape_log.csv", log, LOG_FIELDS)
    write_csv(data_dir / "manual_integration_required.csv", manual, MANUAL_FIELDS)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Engine 1: active job leads")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(run(dry_run=args.dry_run), indent=2))
