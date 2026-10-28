"""ENGINE 3 - HR / decision-maker contacts (Phase 1: company's own website only).

Privacy design:
  * Personal-data files are written ONLY to the private folder
    (env PRIVATE_DATA_DIR, default <project>/private_data, git-ignored).
  * The public repo receives only company-level facts: contact_status
    (found / not_found), counts, and "contact changed" events without names.
  * No LinkedIn, no logins, no guessed emails, no SMTP probing, no mobiles.
  * Suppression and client lists (private) are honoured before anything is stored.

Run:  python -m scraper.engine3_contacts [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
from collections import Counter, defaultdict
from urllib.parse import quote_plus

from .common.config import CACHE_DIR, DATA_DIR, ROOT, gate, load_yaml
from .common.contacts_extract import (Rules, extract_emails, extract_landlines, extract_people,
                                      inbox_kind, pick_pages)
from .common.entities import FIELDS as MASTER_FIELDS
from .common.entities import domain_of, usable_domain
from .common.http import PoliteClient, RobotsDisallowed, SourceBlocked
from .common.store import CHANGE_FIELDS, LOG_FIELDS, append_csv, log_row, read_csv, write_csv
from .common.textutil import clean, normalize_company

ENGINE = "engine3_contacts"

CONTACT_FIELDS = [
    "contact_id", "company_id", "company", "sector", "role_category", "buys", "contact_type",
    "contact_name", "contact_title", "email", "phone", "email_domain_matches_site", "mx_valid",
    "source_url", "source_type", "confidence", "first_seen", "last_verified_date",
    "next_reverify_date", "reconfirmed", "change_detected", "change_date",
    "bounce_flag", "opt_out", "notes",
]
STATE_FIELDS = ["company_id", "company", "website", "last_crawled", "outcome", "pages_fetched", "next_crawl_date"]
MANUAL_FIELDS = ["company_id", "company", "website", "reason", "linkedin_search_url"]
_STEP = {"High": "Medium", "Medium": "Low", "Low": "Low"}


def private_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PRIVATE_DATA_DIR") or ROOT / "private_data")


# =============================================================================
# Crawl one company (network)
# =============================================================================
def crawl_company(client: PoliteClient, company: dict, rules: Rules, cfg: dict, check_mx=None) -> dict:
    """Returns {outcome, pages, found:[contact dicts]}. outcome in
    found | none | robots_disallowed | blocked | no_response | error."""
    s = cfg["settings"]
    site = clean(company.get("website"))
    if site and not site.startswith("http"):
        site = "https://" + site
    domain = domain_of(site)
    client.begin_source(s["max_pages_per_company"] + 2)
    try:
        home = client.get(site)
    except RobotsDisallowed:
        return {"outcome": "robots_disallowed", "pages": 0, "found": []}
    except SourceBlocked:
        return {"outcome": "blocked", "pages": 0, "found": []}
    if not home:
        return {"outcome": "no_response", "pages": 0, "found": []}

    pages = {site: home}
    for url in pick_pages(home, site, rules, s["max_pages_per_company"] - 1):
        try:
            html = client.get(url)
        except (RobotsDisallowed, SourceBlocked):
            continue                       # skip that page, keep what we have
        if html:
            pages[url] = html

    people, inboxes, phone_src = [], {}, {}
    for url, html in pages.items():
        for p in extract_people(html, domain, rules):
            p["source_url"] = url
            people.append(p)
        for e in extract_emails(html, domain, rules):
            kind = inbox_kind(e, rules)
            if kind and e not in inboxes:
                inboxes[e] = (kind, url)
        for ph in extract_landlines(html, s["max_phones_per_company"]):
            phone_src.setdefault(ph, url)          # remember the page each number came from

    found, seen = [], set()
    for p in people:
        key = (p["name"].lower(), p["role_category"])
        if key in seen:
            continue
        seen.add(key)
        found.append({"contact_type": "named_person", "contact_name": p["name"],
                      "contact_title": p["title"], "role_category": p["role_category"],
                      "email": p["email"], "phone": "", "source_url": p["source_url"]})
    found = found[: s["max_contacts_per_company"]]
    n_in = 0
    for e, (kind, url) in inboxes.items():
        if n_in >= s["max_role_inboxes_per_company"]:
            break
        n_in += 1
        found.append({"contact_type": "role_inbox", "contact_name": "", "contact_title": "",
                      "role_category": "hr" if kind == "hr" else "generic", "email": e,
                      "phone": "", "source_url": url})
    for ph, url in list(phone_src.items())[: s["max_phones_per_company"]]:
        found.append({"contact_type": "switchboard", "contact_name": "", "contact_title": "",
                      "role_category": "generic", "email": "", "phone": ph, "source_url": url})
    for c in found:
        c["email_domain_matches_site"] = "1" if c["email"] else ""
        c["mx_valid"] = ""
        if c["email"] and check_mx:
            c["mx_valid"] = check_mx(c["email"].split("@", 1)[1])
    return {"outcome": "found" if found else "none", "pages": len(pages), "found": found}


def make_mx_checker(enabled: bool):
    """DNS MX lookup only. Returns '1'/'0'/'' (unchecked)."""
    if not enabled:
        return None
    try:
        import dns.resolver
    except ImportError:
        return None
    cache: dict[str, str] = {}

    def check(domain: str) -> str:
        if domain not in cache:
            try:
                cache[domain] = "1" if len(dns.resolver.resolve(domain, "MX", lifetime=5)) else "0"
            except Exception:
                cache[domain] = "0"
        return cache[domain]
    return check


# =============================================================================
# Pure logic (unit-tested offline)
# =============================================================================
def contact_key(c: dict) -> str:
    if c["contact_type"] == "named_person":
        return normalize_company(c["contact_name"]) + "|" + c["role_category"]
    if c["contact_type"] == "role_inbox":
        return c["email"].lower()
    return re.sub(r"\D", "", c["phone"])


def make_contact_id(company_id: str, c: dict) -> str:
    return hashlib.sha1(f"{company_id}|{c['contact_type']}|{contact_key(c)}".encode()).hexdigest()[:12]


def confidence_for(row: dict, today: dt.date, stale_days: int) -> str:
    base = "High" if row["contact_type"] == "named_person" else "Low"
    try:
        age = (today - dt.date.fromisoformat(row["last_verified_date"])).days
    except (ValueError, TypeError):
        age = 10 ** 6
    if row.get("reconfirmed") == "no" or age > stale_days:
        base = _STEP[base]
    return base


def load_suppression(pdir: pathlib.Path) -> dict:
    sup = {"email": set(), "domain": set(), "company_id": set(), "name": set()}
    for r in read_csv(pdir / "suppression.csv"):
        t, v = clean(r.get("type")).lower(), clean(r.get("value")).lower()
        if t in sup and v:
            sup[t].add(normalize_company(v) if t == "name" else v)
    return sup


def is_suppressed(c: dict, company_id: str, sup: dict) -> bool:
    if company_id in sup["company_id"]:
        return True
    if c.get("email") and (c["email"].lower() in sup["email"] or c["email"].split("@", 1)[1].lower() in sup["domain"]):
        return True
    return bool(c.get("contact_name")) and normalize_company(c["contact_name"]) in sup["name"]


def merge_contacts(prev: list[dict], results: dict, companies: dict, today: dt.date, cfg: dict, sup: dict):
    """results: company_id -> crawl result (only companies crawled this run).
    Returns (rows, changes)."""
    s = cfg["settings"]
    t = today.isoformat()
    nxt = (today + dt.timedelta(days=s["reverify_days"]["found"])).isoformat()
    by_co = defaultdict(list)
    for r in prev:
        by_co[r["company_id"]].append(dict(r))
    rows, changes = [], []
    for cid in set(by_co) | set(results):
        co = companies.get(cid, {})
        old = by_co.get(cid, [])
        res = results.get(cid)
        if res is None or res["outcome"] in ("robots_disallowed", "blocked", "no_response", "error"):
            rows += old                      # nothing new learned: keep as is
            continue
        old_by_id = {r["contact_id"]: r for r in old}
        new_ids, new_rows = set(), []
        prev_hr_names = {r["contact_name"].lower() for r in old
                         if r["contact_type"] == "named_person" and r["role_category"] == "hr"}
        for c in res["found"]:
            if is_suppressed(c, cid, sup):
                continue
            c = dict(c)
            cidx = make_contact_id(cid, c)
            new_ids.add(cidx)
            p = old_by_id.get(cidx, {})
            row = {**{f: "" for f in CONTACT_FIELDS}, **p}
            row.update(c, contact_id=cidx, company_id=cid, company=co.get("display_name", row.get("company", "")),
                       sector=co.get("sector", ""), buys=cfg["buys"].get(c["role_category"], ""),
                       source_type="company_site", last_verified_date=t, next_reverify_date=nxt,
                       reconfirmed="yes", first_seen=p.get("first_seen") or t)
            if (c["contact_type"] == "named_person" and c["role_category"] == "hr"
                    and prev_hr_names and c["contact_name"].lower() not in prev_hr_names):
                row.update(change_detected="new_hr_head", change_date=t)
                changes.append({"date": t, "change_type": "contact_change", "entity_type": "company",
                                "entity_id": cid, "company": row["company"],
                                "summary": "new HR/TA contact detected (see private data)", "source_url": ""})
            new_rows.append(row)
        for r in old:
            if r["contact_id"] not in new_ids:
                r["reconfirmed"] = "no"
                rows.append(r)
        rows += new_rows
    # retention + suppression + confidence
    out = []
    for r in rows:
        try:
            age = (today - dt.date.fromisoformat(r["last_verified_date"])).days
        except (ValueError, TypeError):
            age = 10 ** 6
        if age > s["retention_days"] or r.get("opt_out", "").lower() in ("1", "yes", "true"):
            continue
        if is_suppressed(r, r["company_id"], sup):
            continue
        r["confidence"] = confidence_for(r, today, s["confidence_stale_after_days"])
        out.append(r)
    out.sort(key=lambda r: (r["company"].lower(), r["contact_type"], r["contact_name"].lower(), r["email"]))
    return out, changes


def select_companies(master: list[dict], state: dict, active_norms: set, skip_ids: set, skip_norms: set,
                     cfg: dict, today: dt.date) -> list[dict]:
    s = cfg["settings"]
    eligible = []
    for m in master:
        cid = m["company_id"]
        if (m.get("icp_status") not in s["eligible_icp"] or cid in skip_ids
                or normalize_company(m["display_name"]) in skip_norms):
            continue
        site = clean(m.get("website"))
        if not usable_domain(domain_of(site)):
            continue
        st = state.get(cid)
        due = dt.date.fromisoformat(st["next_crawl_date"]) if st and st.get("next_crawl_date") else None
        if due and due > today:
            continue
        eligible.append((m, st is None))
    def rank(item):
        m, never = item
        return (normalize_company(m["display_name"]) not in active_norms,      # companies hiring now first
                m.get("icp_status") == "large_outside_band",                    # large firms last
                not never, m["display_name"].lower())
    eligible.sort(key=rank)
    return [m for m, _ in eligible[: s["crawl_budget_companies_per_run"]]]


# =============================================================================
# Runner
# =============================================================================
def run(dry_run: bool = False, today: dt.date | None = None, client: PoliteClient | None = None,
        data_dir=None, pdir=None, config_dir=None, check_mx=None) -> dict:
    """check_mx: None = follow config, False = off, or a callable(domain) -> '1'/'0'."""
    today = today or dt.date.today()
    data_dir = pathlib.Path(data_dir or DATA_DIR)
    pdir = pathlib.Path(pdir or private_dir())
    sources_cfg = load_yaml("sources.yaml", config_dir)
    settings = sources_cfg["settings"]
    cfg = load_yaml("contacts.yaml", config_dir)
    rules = Rules(cfg)
    log = []
    src = next((s for s in sources_cfg["engine3_contacts"]["sources"] if s["id"] == "company_site"), {})
    ok, why = gate(src, settings)
    if not ok:
        log.append(log_row(ENGINE, "company_site", f"skipped:{why}", today=today))
        if not dry_run:
            append_csv(data_dir / "scrape_log.csv", log, LOG_FIELDS)
        return {"ran": False, "reason": why}

    master = read_csv(data_dir / "kenya_companies_master.csv")
    companies = {m["company_id"]: m for m in master}
    state = {r["company_id"]: r for r in read_csv(pdir / "crawl_state.csv")}
    sup = load_suppression(pdir)
    client_norms = {normalize_company(r.get("name", "")) for r in read_csv(pdir / "clients.csv")}
    active_norms = {normalize_company(r["company"]) for r in read_csv(data_dir / "active_jobs.csv")}
    batch = select_companies(master, state, active_norms, sup["company_id"], client_norms, cfg, today)
    client = client or PoliteClient(settings, CACHE_DIR)
    if callable(check_mx):
        mx = check_mx
    elif check_mx is False:
        mx = None
    else:
        mx = make_mx_checker(bool(cfg["settings"]["check_mx"]))

    results = {}
    for m in batch:
        try:
            res = crawl_company(client, m, rules, cfg, mx)
        except Exception as e:                    # one bad site must not stop the run
            res = {"outcome": "error", "pages": 0, "found": [], "detail": f"{type(e).__name__}"}
        results[m["company_id"]] = res
        d = cfg["settings"]["reverify_days"]
        gap = d["found"] if res["outcome"] == "found" else d["none"] if res["outcome"] == "none" else \
            d["error"] if res["outcome"] in ("error", "no_response") else d["blocked"]
        state[m["company_id"]] = {"company_id": m["company_id"], "company": m["display_name"],
                                  "website": m.get("website", ""), "last_crawled": today.isoformat(),
                                  "outcome": res["outcome"], "pages_fetched": res["pages"],
                                  "next_crawl_date": (today + dt.timedelta(days=gap)).isoformat()}

    prev = read_csv(pdir / "hr_contacts.csv")
    rows, changes = merge_contacts(prev, results, companies, today, cfg, sup)
    have = {r["company_id"] for r in rows}
    for m in master:
        m["contact_status"] = "found" if m["company_id"] in have else "not_found"
    manual = [{"company_id": s["company_id"], "company": s["company"], "website": s["website"],
               "reason": s["outcome"],
               "linkedin_search_url": "https://www.linkedin.com/search/results/people/?keywords="
                                      + quote_plus(f"HR manager {s['company']}")}
              for s in state.values() if s["outcome"] in ("robots_disallowed", "blocked")]

    outcomes = Counter(r["outcome"] for r in results.values())
    summary = {"ran": True, "crawled": len(results), "outcomes": dict(outcomes), "contacts": len(rows),
               "companies_with_contacts": len(have), "changes": len(changes), "manual_checks": len(manual)}
    log.append(log_row(ENGINE, "company_site", "ok" if results else "zero_results", len(results),
                       json.dumps(dict(outcomes)), today))
    if dry_run:
        return summary
    write_csv(pdir / "hr_contacts.csv", rows, CONTACT_FIELDS)
    write_csv(pdir / "crawl_state.csv", list(state.values()), STATE_FIELDS)
    write_csv(pdir / "manual_contact_check.csv", manual, MANUAL_FIELDS)
    write_csv(data_dir / "kenya_companies_master.csv", master, MASTER_FIELDS)   # only contact_status changed
    append_csv(data_dir / "changes.csv", changes, CHANGE_FIELDS)
    append_csv(data_dir / "scrape_log.csv", log, LOG_FIELDS)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Engine 3: HR / decision-maker contacts")
    ap.add_argument("--dry-run", action="store_true")
    print(json.dumps(run(dry_run=ap.parse_args().dry_run), indent=2))
