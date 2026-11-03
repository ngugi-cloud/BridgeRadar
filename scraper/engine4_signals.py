"""ENGINE 4 - Predictive hiring-intent signals.   SIGNAL-BASED PRIORITIZATION.

This is a prioritisation aid, not an oracle. Detection is keyword heuristics over
news headlines; expect false positives. Every row links to its source and is
labelled "signal-based prioritization". Nothing here claims a company WILL hire.

Pipeline: feeds -> items -> (advisory? / signal type / company match / amounts)
          -> cluster same event across outlets -> score -> outputs.

Privacy / strategy: the full signal file (with outreach angles) is written to the
PRIVATE folder. The public repo gets only aggregates and company-level event notes.
Article text is never stored: headline, our own summary, and the link only.

Run:  python -m scraper.engine4_signals [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import email.utils
import hashlib
import json
import pathlib
import re
from collections import Counter, defaultdict
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET

from .common.classify import Classifier
from .common.config import CACHE_DIR, CONFIG_DIR, DATA_DIR, gate, load_yaml
from .common.http import PoliteClient, RobotsDisallowed, SourceBlocked
from .common.store import CHANGE_FIELDS, LOG_FIELDS, append_csv, log_row, read_csv, write_csv
from .common.textutil import clean, normalize_company, parse_date
from .engine3_contacts import private_dir

ENGINE = "engine4_signals"
LABEL = "signal-based prioritization"

SIGNAL_FIELDS = [
    "signal_id", "company_id", "company", "company_matched", "sector", "icp_status", "signal_type",
    "signal_date", "headline", "summary", "amount_text", "jobs_stated", "source_url", "source_outlet",
    "outlets_count", "also_reported_by", "reliability_class", "reliability_multiplier", "base_points",
    "decay", "signal_score", "trigger_strength", "predicted_hiring_probability",
    "suggested_outreach_angle", "first_seen", "last_seen", "label",
]
ADVISORY_FIELDS = ["advisory_id", "company_id", "company", "company_matched", "signal_date", "headline",
                   "source_url", "source_outlet", "suggested_angle", "last_seen", "label"]
SUMMARY_FIELDS = ["signal_type", "sector", "probability", "count"]
MODIFIER_FIELDS = ["sector", "type", "event_date", "points", "active", "summary", "source_url"]
CANDIDATE_FIELDS = ["candidate_name", "signal_type", "signal_date", "headline", "source_url", "last_seen"]
EXCLUDED_ICP = {"excluded_public", "excluded_ngo", "competitor_agency"}


# =============================================================================
# Feeds
# =============================================================================
def _txt(el) -> str:
    return clean(el.text) if el is not None and el.text else ""


def parse_feed(xml_text: str, default_outlet: str = "") -> list[dict]:
    """RSS 2.0 or Atom -> [{headline, link, date, outlet, text}]. Unsafe XML is rejected."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except Exception:
        return []
    items = []
    for it in root.iter("item"):                                  # RSS
        src = it.find("source")
        outlet = _txt(src) or default_outlet
        title = _txt(it.find("title"))
        if outlet and title.endswith(" - " + outlet):
            title = title[: -len(outlet) - 3].strip()
        items.append({"headline": title, "link": _txt(it.find("link")),
                      "date": _feed_date(_txt(it.find("pubDate"))), "outlet": outlet,
                      "text": "" if src is not None else BeautifulSoup(_txt(it.find("description")), "html.parser").get_text(" ")[:500]})
    ns = "{http://www.w3.org/2005/Atom}"
    for en in root.iter(ns + "entry"):                            # Atom
        link = en.find(ns + "link")
        items.append({"headline": _txt(en.find(ns + "title")), "link": (link.get("href") if link is not None else ""),
                      "date": _feed_date(_txt(en.find(ns + "published")) or _txt(en.find(ns + "updated"))),
                      "outlet": default_outlet,
                      "text": BeautifulSoup(_txt(en.find(ns + "summary")), "html.parser").get_text(" ")[:500]})
    return [i for i in items if i["headline"] and i["link"]]


def _feed_date(s: str) -> dt.date | None:
    if not s:
        return None
    try:
        return email.utils.parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return parse_date(s)


def google_news_urls(cfg: dict) -> list[str]:
    g = cfg["google_news"]
    return [f"{g['base']}?q={quote_plus(q + ' ' + g['window'])}&{g['params']}" for q in g["queries"]]


# =============================================================================
# Detection (pure)
# =============================================================================
class Detector:
    def __init__(self, sig_cfg: dict, scoring: dict):
        self.cfg = sig_cfg
        self.points = scoring["signals"]["points"]
        self.patterns = {t: [re.compile(p, re.I) for p in pats] for t, pats in sig_cfg["patterns"].items()}
        self.advisory = re.compile(sig_cfg["advisory_pattern"], re.I)
        self.markers = [re.compile(r"\b" + re.escape(m) + r"\b", re.I) for m in sig_cfg["kenya_markers"]]
        self.generic = {w.lower() for w in sig_cfg["generic_candidate_words"]}
        self.outlet_classes = {k: [v.lower() for v in vs] for k, vs in sig_cfg["outlet_classes"].items()}

    def is_advisory(self, text: str) -> bool:
        return bool(self.advisory.search(text))

    def signal_type(self, text: str) -> str | None:
        hits = [t for t, pats in self.patterns.items() if any(p.search(text) for p in pats)]
        if "international_market_entry" in hits:
            return "international_market_entry"
        return max(hits, key=lambda t: self.points.get(t, 0)) if hits else None   # one article -> one signal

    def has_kenya_marker(self, text: str) -> bool:
        return any(m.search(text) for m in self.markers)

    def outlet_class(self, outlet: str, default: str = "single_unverified") -> str:
        o = (outlet or "").lower()
        for cls, names in self.outlet_classes.items():
            if any(n in o for n in names):
                return cls
        return default

    _VERBS = (r"raises?|secures?|closes|lands|bags|wins|launche[sd]|opens?|expands?|unveils?|acquires?|"
              r"appoints?|names|announces?|to (?:build|open|expand|acquire|invest|set up)|inks?|signs?")
    _CAND = re.compile(r"^((?:[A-Z][\w&'’.\-]*)(?:\s+(?:[A-Z][\w&'’.\-]*|&|and|of)){0,4}?)\s+(?:" + _VERBS + r")\b")
    _PREFIX = re.compile(r"^(?:(?:Kenyan|Nairobi|Mombasa|Kenya)(?:-based)?|Kenya[’']s)\s+")

    def candidate_company(self, headline: str) -> str:
        """Best-effort name from a headline like 'Acme Foods raises $8M'. Often blank."""
        h = self._PREFIX.sub("", clean(headline))
        m = self._CAND.match(h)
        if not m:
            return ""
        name = m.group(1).strip(" -&")
        words = [w for w in re.split(r"\s+", name) if w]
        if not words or all(w.lower() in self.generic for w in words) or len(name) < 3:
            return ""
        return name


_AMOUNT = re.compile(r"(?:US\$|\$|USD|KES|KSh|Ksh|Sh|Shs)\s?\d[\d,.]*\s?(?:million|billion|trillion|m|bn|b|k|thousand)?\b", re.I)
_JOBS = re.compile(r"(\d[\d,]*)\s*(?:direct\s+|indirect\s+|new\s+|permanent\s+)?jobs", re.I)
_JOBS2 = re.compile(r"creat\w+\s+(?:over\s+|about\s+|up to\s+)?(\d[\d,]*)", re.I)


def extract_amount(text: str) -> str:
    m = _AMOUNT.search(text or "")
    return clean(m.group(0)) if m else ""


def extract_jobs(text: str) -> int:
    m = _JOBS.search(text or "") or (re.search(r"jobs?", text or "", re.I) and _JOBS2.search(text or ""))
    return int(m.group(1).replace(",", "")) if m else 0


class CompanyIndex:
    """Match headlines to KNOWN companies (precise) from the master list."""

    def __init__(self, master: list[dict]):
        self.entries = []
        for m in master:
            names = {m.get("display_name", ""), m.get("legal_name", ""), m.get("trading_name", "")}
            names |= {a for a in (m.get("aliases") or "").split("|") if a}
            for n in names:
                norm = normalize_company(n)
                if n and len(norm) >= 5:
                    self.entries.append((norm, n, m))
        self.entries.sort(key=lambda e: -len(e[0]))

    def match(self, headline: str) -> dict | None:
        h = " " + re.sub(r"[^a-z0-9 ]+", " ", clean(headline).lower().replace("&", " and ")) + " "
        h = re.sub(r"\s+", " ", h)
        found = {}
        for norm, raw, m in self.entries:
            if f" {norm} " not in h:
                continue
            if " " not in norm and not re.search(r"\b" + re.escape(raw.split()[0]) + r"\b", headline):
                continue          # single-word names must appear with the original capitalisation
            found.setdefault(m["company_id"], (len(norm), m))
        if not found:
            return None
        ranked = sorted(found.values(), key=lambda x: -x[0])
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            return None           # ambiguous: do not guess
        return ranked[0][1]


# =============================================================================
# Scoring (pure) - numbers come from scoring.yaml
# =============================================================================
def decay_for(age_days: int, scoring: dict) -> float:
    for b in scoring["signals"]["age_decay"]:
        if "max_days" in b and age_days <= b["max_days"]:
            return b["multiplier"]
    return 0.0


def reliability_for(outlet_classes: list[str], n_outlets: int, scoring: dict) -> tuple[str, float]:
    rel = scoring["signals"]["reliability"]
    best = max(outlet_classes, key=lambda c: rel.get(c, 0.7))
    mult = rel.get(best, 0.7)
    if n_outlets >= 2:
        mult = min(mult + rel["corroboration_bonus"], rel["corroboration_cap"])
    return best, round(mult, 3)


def score_signal(sig_type: str, signal_date: dt.date, jobs: int, cls_list, n_outlets, scoring, today):
    pts = scoring["signals"]["points"][sig_type]
    decay = decay_for((today - signal_date).days, scoring)
    cls, mult = reliability_for(cls_list, n_outlets, scoring)
    score = pts * decay * mult
    sl = scoring["lead_score"]["trigger"]["signal_lead"]
    bonus = next((b["points"] for b in sl["job_numbers_bonus"] if jobs >= b["min_jobs"]), 0)
    trigger = min(sl["cap"], score * sl["multiplier"] + (bonus if decay > 0 else 0))
    pl = scoring["signals"]["probability_label"]
    prob = "High" if score >= pl["high_at_or_above"] else "Medium" if score >= pl["medium_at_or_above"] else "Low"
    return {"base_points": pts, "decay": decay, "reliability_class": cls, "reliability_multiplier": mult,
            "signal_score": round(score, 2), "trigger_strength": round(trigger, 1),
            "predicted_hiring_probability": prob}


def make_angle(sig_type: str, amount: str, jobs: int, cfg: dict) -> str:
    tmpl = cfg["angles"].get(sig_type, "")
    return tmpl.format(amount=amount or "funding", jobs=f" (~{jobs:,} jobs stated)" if jobs else "")


# =============================================================================
# Items -> clusters -> rows
# =============================================================================
def enrich_items(raw: list[dict], det: Detector, index: CompanyIndex, clf: Classifier,
                 today: dt.date, lookback: int, require_marker: bool, default_class: str = "single_unverified"):
    """Return (signal_items, advisory_items, stats)."""
    sigs, advs, stats = [], [], Counter()
    seen_links = set()
    for r in raw:
        if r["link"] in seen_links:
            stats["duplicate_link"] += 1
            continue
        seen_links.add(r["link"])
        d = r.get("date")
        if not d or (today - d).days > lookback or d > today + dt.timedelta(days=1):
            stats["no_or_old_date"] += 1          # never invent a date
            continue
        head = r["headline"]
        blob = head + " " + r.get("text", "")
        adv = det.is_advisory(head)
        stype = None if adv else det.signal_type(head)
        if not adv and not stype:
            stats["no_signal"] += 1
            continue
        co = index.match(head)
        cand = "" if co else det.candidate_company(head)
        if not co and (not cand or (require_marker and not det.has_kenya_marker(blob))):
            stats["unattributed"] += 1
            continue
        sector, vertical, conf = (co.get("sector"), "", "") if co else clf.sector(head, cand)
        item = {"headline": head, "link": r["link"], "date": d, "outlet": r.get("outlet") or "",
                "outlet_class": det.outlet_class(r.get("outlet", ""), default_class),
                "company_id": co["company_id"] if co else "", "company": co["display_name"] if co else cand,
                "matched": bool(co), "sector": sector or "other", "icp": co.get("icp_status", "") if co else "",
                "amount": extract_amount(head), "jobs": extract_jobs(blob), "type": stype}
        (advs if adv else sigs).append(item)
    return sigs, advs, stats


def _ckey(it: dict) -> str:
    return it["company_id"] or "cand:" + normalize_company(it["company"])


def cluster(items: list[dict], window_days: int) -> list[dict]:
    """Same company + same type within window_days = one event, however many outlets."""
    groups = defaultdict(list)
    for it in sorted(items, key=lambda x: x["date"]):
        groups[(_ckey(it), it["type"])].append(it)
    out = []
    for (ck, typ), its in groups.items():
        cur = None
        for it in its:
            if cur and (it["date"] - cur["first_date"]).days <= window_days:
                cur["items"].append(it)
            else:
                cur = {"ckey": ck, "type": typ, "first_date": it["date"], "items": [it]}
                out.append(cur)
    return out


def build_signals(items: list[dict], prev_rows: list[dict], det: Detector, scoring: dict, sig_cfg: dict,
                  today: dt.date, baseline: bool):
    """Cluster, merge with history, score. Returns (rows, new_ids)."""
    s = sig_cfg["settings"]
    clusters = cluster(items, s["cluster_window_days"])
    # fold in previous rows so repeated coverage extends the same event
    prev = {}
    for r in prev_rows:
        try:
            d = dt.date.fromisoformat(r["signal_date"])
        except ValueError:
            continue
        ck = r["company_id"] or "cand:" + normalize_company(r["company"])
        prev[(ck, r["signal_type"], r["signal_id"])] = (d, r)
    rows, new_ids, used_prev = [], set(), set()
    for c in clusters:
        match = next((k for k, (d, _) in prev.items()
                      if k[0] == c["ckey"] and k[1] == c["type"] and abs((c["first_date"] - d).days) <= s["cluster_window_days"]),
                     None)
        its = c["items"]
        outlets = {i["outlet"].lower(): i["outlet"] for i in its if i["outlet"]}
        classes = [i["outlet_class"] for i in its]
        first = min(its, key=lambda i: (-_rel_rank(i["outlet_class"], scoring), i["date"]))
        jobs = max(i["jobs"] for i in its)
        amount = next((i["amount"] for i in its if i["amount"]), "")
        sdate, first_seen, headline, link, outlet_main = c["first_date"], today.isoformat(), first["headline"], first["link"], first["outlet"]
        sid = hashlib.sha1(f"{c['ckey']}|{c['type']}|{sdate}".encode()).hexdigest()[:12]
        if match:
            used_prev.add(match)
            d0, p = prev[match]
            sid, first_seen, sdate = p["signal_id"], p["first_seen"] or first_seen, min(sdate, d0)
            for o in [p["source_outlet"]] + [x for x in (p["also_reported_by"] or "").split("; ") if x]:
                if o:
                    outlets.setdefault(o.lower(), o)
            classes.append(p["reliability_class"] or "single_unverified")
            jobs, amount = max(jobs, int(p["jobs_stated"] or 0)), amount or p["amount_text"]
            if p.get("source_url"):
                headline, link, outlet_main = p["headline"], p["source_url"], p["source_outlet"]
        sc = score_signal(c["type"], sdate, jobs, classes, len(outlets), scoring, today)
        it0 = its[0]
        also = [o for o in outlets.values() if o != outlet_main]
        label = {"funding_raised": "Funding", "expansion_announced": "Expansion", "government_investment_deal": "Investment/PPP",
                 "mna_or_ipo": "M&A/IPO", "licence_or_approval": "Licence/approval", "contract_or_tender_won": "Contract win",
                 "leadership_hire": "Leadership hire", "product_launch": "Product launch",
                 "international_market_entry": "International market entry",
                 "workforce_development": "Workforce development", "youth_or_job_access": "Youth / job access",
                 "hr_system_or_transformation": "HR / payroll transformation",
                 "overseas_remote_hiring": "Overseas / remote hiring"}[c["type"]]
        rows.append({
            "signal_id": sid, "company_id": it0["company_id"], "company": it0["company"],
            "company_matched": "1" if it0["matched"] else "0", "sector": it0["sector"], "icp_status": it0["icp"],
            "signal_type": c["type"], "signal_date": sdate.isoformat(), "headline": headline,
            "summary": f"{label} signal for {it0['company']}" + (f" ({amount})" if amount else "")
                       + (f", ~{jobs:,} jobs stated" if jobs else "") + f" [{len(outlets) or 1} outlet(s)]",
            "amount_text": amount, "jobs_stated": jobs or "", "source_url": link, "source_outlet": outlet_main,
            "outlets_count": len(outlets) or 1, "also_reported_by": "; ".join(sorted(also)),
            **sc, "suggested_outreach_angle": make_angle(c["type"], amount, jobs, sig_cfg),
            "first_seen": first_seen, "last_seen": today.isoformat(), "label": LABEL})
        if not match and not baseline:
            new_ids.add(sid)
    # keep previous signals that were not refreshed (re-score so decay stays current)
    for key, (d, p) in prev.items():
        if key in used_prev or (today - d).days > s["retention_days"]:
            continue
        outlets = [o for o in [p["source_outlet"]] + (p["also_reported_by"] or "").split("; ") if o]
        sc = score_signal(p["signal_type"], d, int(p["jobs_stated"] or 0), [p["reliability_class"] or "single_unverified"],
                          max(1, len(outlets)), scoring, today)
        rows.append({**p, **sc, "label": LABEL})
    rows.sort(key=lambda r: (-float(r["trigger_strength"]), r["company"].lower()))
    return rows, new_ids


def _rel_rank(cls: str, scoring: dict) -> float:
    return scoring["signals"]["reliability"].get(cls, 0.7)


def build_advisories(items: list[dict], prev: list[dict], today: dt.date, cfg: dict, retention: int) -> list[dict]:
    by_id = {r["advisory_id"]: r for r in prev}
    for it in items:
        aid = hashlib.sha1(f"{_ckey(it)}|{it['date']}|{normalize_company(it['headline'])[:60]}".encode()).hexdigest()[:12]
        by_id[aid] = {"advisory_id": aid, "company_id": it["company_id"], "company": it["company"],
                      "company_matched": "1" if it["matched"] else "0", "signal_date": it["date"].isoformat(),
                      "headline": it["headline"], "source_url": it["link"], "source_outlet": it["outlet"],
                      "suggested_angle": cfg["advisory_angle"], "last_seen": today.isoformat(), "label": LABEL}
    keep = [r for r in by_id.values() if (today - dt.date.fromisoformat(r["signal_date"])).days <= retention]
    return sorted(keep, key=lambda r: r["signal_date"], reverse=True)


def load_sector_modifiers(path, scoring: dict, today: dt.date) -> list[dict]:
    """Manual sector-wide events (surveys, regulatory changes). Modifier only - never a lead."""
    pts = scoring["signals"]["sector_modifier"]
    out = []
    for r in read_csv(path):
        typ, d, src = clean(r.get("type")), parse_date(r.get("event_date"), today), clean(r.get("source_url"))
        if typ not in pts or not d or not src or not clean(r.get("sector")):
            continue                      # source URL is required; never invent
        days = int(clean(r.get("expires_days")) or 90)
        out.append({"sector": clean(r["sector"]), "type": typ, "event_date": d.isoformat(), "points": pts[typ],
                    "active": "1" if (today - d).days <= days else "0", "summary": clean(r.get("summary")),
                    "source_url": src})
    return out


# =============================================================================
# Runner
# =============================================================================
def fetch_all(client: PoliteClient, sources_cfg: dict, sig_cfg: dict, today: dt.date, log: list) -> list[dict]:
    raw = []
    settings = sources_cfg["settings"]
    cap = sig_cfg["settings"]["max_items_per_feed"]
    for src in sources_cfg["engine4_signals"]["sources"]:
        ok, why = gate(src, settings)
        if not ok:
            log.append(log_row(ENGINE, src["id"], f"skipped:{why}", today=today))
            continue
        if src["id"] == "gnews_rss":
            urls, default_outlet = google_news_urls(sig_cfg), ""
        elif src["id"] == "google_alerts":
            urls, default_outlet = list(sig_cfg.get("alert_feeds") or []), "Google Alerts"
        else:
            u = sig_cfg.get("feeds", {}).get(src["id"], "")
            urls, default_outlet = ([u] if u else []), src.get("name", "")
        if not urls:
            log.append(log_row(ENGINE, src["id"], "skipped:no_feed_url", today=today))
            continue
        client.begin_source(max(len(urls) + 2, 10))
        got, blocked = 0, ""
        for u in urls:
            try:
                text = client.get(u)
            except (RobotsDisallowed, SourceBlocked) as e:
                blocked = str(e)[:120]
                continue
            for it in parse_feed(text or "", default_outlet)[:cap]:
                # aggregators say nothing about the outlet's quality; classify by outlet name instead
                it["default_class"] = ("single_unverified" if src["id"] in ("gnews_rss", "google_alerts")
                                       else src.get("reliability", "single_unverified"))
                raw.append(it)
                got += 1
        status = "blocked" if blocked and not got else "ok" if got else "zero_results"
        log.append(log_row(ENGINE, src["id"], status, got, blocked, today))
    return raw


def run(dry_run=False, today=None, client=None, data_dir=None, pdir=None, config_dir=None, raw_items=None) -> dict:
    today = today or dt.date.today()
    data_dir, pdir = pathlib.Path(data_dir or DATA_DIR), pathlib.Path(pdir or private_dir())
    sources_cfg = load_yaml("sources.yaml", config_dir)
    sig_cfg, scoring = load_yaml("signals.yaml", config_dir), load_yaml("scoring.yaml", config_dir)
    clf = Classifier(load_yaml("sector_map.yaml", config_dir))
    det, log = Detector(sig_cfg, scoring), []
    master = read_csv(data_dir / "kenya_companies_master.csv")
    index = CompanyIndex(master)

    if raw_items is None:
        client = client or PoliteClient(sources_cfg["settings"], CACHE_DIR)
        raw_items = fetch_all(client, sources_cfg, sig_cfg, today, log)
    s = sig_cfg["settings"]
    sig_items, adv_items, stats = [], [], Counter()
    # per-source default reliability class travels with each raw item
    for r in raw_items:
        a, b, st = enrich_items([r], det, index, clf, today, s["lookback_days"],
                                s["require_kenya_marker_for_unmatched"], r.get("default_class", "single_unverified"))
        sig_items += a
        adv_items += b
        stats.update(st)

    prev = read_csv(pdir / "hiring_signals.csv")
    baseline = len(prev) == 0
    rows, new_ids = build_signals(sig_items, prev, det, scoring, sig_cfg, today, baseline)
    advisories = build_advisories(adv_items, read_csv(pdir / "advisory_flags.csv"), today, sig_cfg, s["retention_days"])
    modifiers = load_sector_modifiers(CONFIG_DIR / "sector_events.csv" if config_dir is None else pathlib.Path(config_dir) / "sector_events.csv", scoring, today)

    live = [r for r in rows if r["decay"] not in (0, 0.0, "0", "0.0") and r["icp_status"] not in EXCLUDED_ICP]
    summary_rows = [{"signal_type": t, "sector": sec, "probability": p, "count": n}
                    for (t, sec, p), n in sorted(Counter((r["signal_type"], r["sector"], r["predicted_hiring_probability"]) for r in live).items())]
    candidates = [{"candidate_name": r["company"], "signal_type": r["signal_type"], "signal_date": r["signal_date"],
                   "headline": r["headline"], "source_url": r["source_url"], "last_seen": today.isoformat()}
                  for r in rows if r["company_matched"] == "0"]
    changes = [{"date": today.isoformat(), "change_type": "new_signal", "entity_type": "signal", "entity_id": r["signal_id"],
                "company": r["company"], "summary": f"{r['signal_type']} ({r['predicted_hiring_probability']})",
                "source_url": r["source_url"]} for r in rows if r["signal_id"] in new_ids]
    result = {"signals": len(rows), "live": len(live), "advisories": len(advisories), "candidates": len(candidates),
              "baseline": baseline, "dropped": dict(stats), "sector_modifiers": len(modifiers)}
    log.append(log_row(ENGINE, "_run", "baseline_run" if baseline and rows else "ok", len(rows), json.dumps(dict(stats)), today))
    if dry_run:
        return result
    write_csv(pdir / "hiring_signals.csv", rows, SIGNAL_FIELDS)               # PRIVATE (has outreach angles)
    write_csv(pdir / "advisory_flags.csv", advisories, ADVISORY_FIELDS)       # PRIVATE
    write_csv(data_dir / "signals_summary.csv", summary_rows, SUMMARY_FIELDS)       # public aggregates
    write_csv(data_dir / "sector_modifiers.csv", modifiers, MODIFIER_FIELDS)        # public
    write_csv(data_dir / "signal_company_candidates.csv", candidates, CANDIDATE_FIELDS)  # for human review
    append_csv(data_dir / "changes.csv", changes, CHANGE_FIELDS)
    append_csv(data_dir / "scrape_log.csv", log, LOG_FIELDS)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Engine 4: hiring-intent signals (signal-based prioritization)")
    ap.add_argument("--dry-run", action="store_true")
    print(json.dumps(run(dry_run=ap.parse_args().dry_run), indent=2))
