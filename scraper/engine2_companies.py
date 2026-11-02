"""ENGINE 2 - Kenya companies master list (private SMEs, 10-250 staff target).

Inputs (all gated):
  * engine1_employers   employer names from Engine 1's active jobs (internal)
  * career_pages_seed   config/career_pages.csv (internal, maintained by you)
  * csv imports         public lists you download after reviewing their terms
                        (config/imports.yaml -> data/imports/*.csv)
  * html_table sources  simple public table pages, config-driven (sources.yaml)
Outputs:
  data/kenya_companies_master.csv   data/competitor_agencies.csv
  data/needs_review.csv             data/changes.csv, data/scrape_log.csv

Nothing is invented: size is stored only with a size_source; every row carries
its source URL; blanks stay blank.

Run:  python -m scraper.engine2_companies [--dry-run]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .common.classify import Classifier
from .common.config import CACHE_DIR, CONFIG_DIR, DATA_DIR, ROOT, gate, load_yaml
from .common.entities import FIELDS, EntityStore, domain_of, usable_domain
from .common.http import PoliteClient, RobotsDisallowed, SourceBlocked
from .common.store import CHANGE_FIELDS, LOG_FIELDS, append_csv, log_row, read_csv, write_csv
from .common.textutil import clean, looks_like_agency, normalize_company

ENGINE = "engine2_companies"
RANK = {"regulator": 3, "import": 3, "table": 3, "seed": 2, "employer": 1}
AGENCY_FIELDS = ["name", "norm", "website", "email", "licence_number", "licence_status",
                 "source_id", "source_url", "last_seen"]
REVIEW_FIELDS = ["company_a", "company_b", "ratio", "reason"]
PUBLIC_OWNERS = {"public", "state_corporation", "government", "county"}
LARGE_OWNERS = {"multinational", "listed"}


# =============================================================================
# ICP classification
# =============================================================================
def parse_size(text: str) -> tuple[int, int] | None:
    nums = [int(n) for n in re.findall(r"\d[\d,]*", str(text or "").replace(",", ""))]
    if not nums:
        return None
    s = str(text)
    if "+" in s and len(nums) == 1:
        return nums[0], 10 ** 7
    return (min(nums), max(nums))


def icp_status(e: dict, icp: dict, agency_norms: set) -> tuple[str, str]:
    """Return (status, note). Statuses: in_band, unknown_size, large_outside_band,
    excluded_public, excluded_ngo, excluded_facility, excluded_micro, competitor_agency."""
    name = e.get("display_name", "")
    if e["_norms"] & agency_norms:
        return "competitor_agency", "matches NEA/agency list"
    if looks_like_agency(name):
        return "competitor_agency", "name suggests a recruitment agency: verify"
    excl = set(icp.get("exclude", []))
    owner = e.get("ownership_type", "")
    if owner in PUBLIC_OWNERS and "public_sector" in excl:
        return "excluded_public", f"ownership: {owner}"
    if e.get("sector") in excl:
        return "excluded_ngo", "outside private-sector ICP"
    if e.get("sector") == "healthcare":
        hc = icp.get("healthcare", {})
        blob = f"{name} {e.get('sub_industry', '')}".lower()
        grouped = bool(e.get("parent_group"))
        lvl = re.sub(r"\D", "", e.get("facility_level", ""))
        if not grouped and lvl and int(lvl) < int(hc.get("min_facility_level", 3)):
            return "excluded_facility", f"facility level {lvl} below minimum"
        if not grouped and any(t in blob for t in hc.get("exclude_terms", [])):
            return "excluded_facility", "single-practitioner / dispensary"
    rng = parse_size(e.get("size_band", ""))
    lo, hi = icp.get("employees_min", 10), icp.get("employees_max", 250)
    if rng and e.get("size_source"):
        a, b = rng
        if a >= lo and b <= hi:
            return "in_band", ""
        if a > hi:
            return "large_outside_band", "size above SME band"
        if b < lo:
            return "excluded_micro", "size below SME band"
        return "unknown_size", "reported size band straddles the SME band"
    if owner in LARGE_OWNERS:
        return "large_outside_band", f"ownership ({owner}) suggests large; size unverified"
    return "unknown_size", "size unverified"


# =============================================================================
# Adapters: each returns a list of incoming records (see EntityStore.add_record)
# =============================================================================
def _with_sector(rec: dict, clf: Classifier, hint: str | None) -> dict:
    if hint and hint in clf.sectors:
        rec.update(sector=hint, vertical=clf.sectors[hint]["vertical"],
                   sector_confidence="high", sector_is_hint=True)
    else:
        s, v, c = clf.sector("", rec["name"], rec.get("sub_industry", ""))
        rec.update(sector=s, vertical=v, sector_confidence=c, sector_is_hint=False)
    return rec


def adapter_employers(clf: Classifier, data_dir) -> tuple[list[dict], list[dict]]:
    """Employer names from Engine 1. Agencies go to the competitor list;
    confidential postings are skipped (no company to attribute)."""
    jobs = read_csv(data_dir / "active_jobs.csv")
    by_co = defaultdict(list)
    for j in jobs:
        by_co[j["company"]].append(j)
    recs, agencies = [], []
    for company, rows in by_co.items():
        posted_by = Counter(r.get("posted_by") for r in rows).most_common(1)[0][0]
        if posted_by == "confidential":
            continue
        if posted_by == "agency":
            agencies.append({"name": company, "source_id": "engine1_jobs",
                             "source_url": rows[0].get("source_url", "")})
            continue
        sectors = Counter(r["sector"] for r in rows if r.get("sector") and r["sector"] != "other")
        sector = sectors.most_common(1)[0][0] if sectors else "other"
        recs.append({"name": company, "sector": sector,
                     "vertical": clf.sectors.get(sector, {}).get("vertical", "Other"),
                     "sector_confidence": "low", "sector_is_hint": False,
                     "source_id": "engine1_employers", "source_url": rows[0].get("source_url", ""),
                     "rank": RANK["employer"]})
    return recs, agencies


def adapter_career_seeds(clf: Classifier, config_dir=None) -> list[dict]:
    recs = []
    career_config_dir = config_dir or CONFIG_DIR
    for r in read_csv(career_config_dir / "career_pages.csv"):
        name, url = clean(r.get("company")), clean(r.get("careers_url"))
        if not name or not url:
            continue
        host = domain_of(url)
        rec = {"name": name, "careers_page_url": url, "ats_type": clean(r.get("ats_type")),
               "ats_token": clean(r.get("ats_token")),
               "website": f"https://{host}" if usable_domain(host) else "",
               "source_id": "career_pages_seed", "source_url": url, "rank": RANK["seed"]}
        recs.append(_with_sector(rec, clf, None))
    return recs


_COLS = ["name", "sub_industry", "hq_location", "website", "licence_number", "licence_status",
         "ownership_type", "parent_group", "size", "size_source", "facility_level", "email"]


def _map_row(row: dict, columns: dict) -> dict:
    out = {}
    for field in _COLS:
        col = columns.get(field)
        out[field] = clean(row.get(col)) if col else ""
    return out


def adapter_csv_import(imp: dict, clf: Classifier, root=ROOT) -> tuple[list[dict], list[dict], list[str]]:
    """Returns (master_records, agency_records, problems)."""
    problems = []
    src_url = clean(imp.get("source_url"))
    if not src_url or "PAGE-WHERE" in src_url:
        return [], [], [f"{imp.get('id')}: source_url is required"]
    path = (root / imp["file"]) if not str(imp["file"]).startswith("/") else imp["file"]
    rows = read_csv(path)
    if not rows:
        return [], [], [f"{imp.get('id')}: file empty or missing: {imp['file']}"]
    recs, agencies = [], []
    for row in rows:
        m = _map_row(row, imp.get("columns", {}))
        if not m["name"]:
            continue
        if imp.get("output") == "competitor_agencies":
            agencies.append({**m, "source_id": imp.get("source_ref") or imp["id"], "source_url": src_url})
            continue
        if m["size"] and not (m["size_source"] or imp.get("size_source")):
            m["size"] = ""                      # size without a source is ignored
        m["size_source"] = m["size_source"] or imp.get("size_source", "")
        m["ownership_type"] = m["ownership_type"] or clean(imp.get("ownership_type"))
        m.update(source_id=imp.get("source_ref") or imp["id"], source_url=src_url, rank=RANK["import"])
        recs.append(_with_sector(m, clf, imp.get("sector")))
    return recs, agencies, problems


def adapter_html_table(client: PoliteClient, src: dict, clf: Classifier) -> list[dict]:
    """Config-driven reader for a simple public HTML table.

    sources.yaml entry needs:  table: {url, index: 0, columns: {name: "Header text", ...}}
    Columns are matched by header text (case-insensitive). JavaScript-rendered
    tables are not supported.
    """
    cfg = src["table"]
    html = client.get(cfg["url"])
    if not html:
        return []
    tables = BeautifulSoup(html, "html.parser").find_all("table")
    idx = int(cfg.get("index", 0))
    if idx >= len(tables):
        return []
    trs = tables[idx].find_all("tr")
    if not trs:
        return []
    headers = [clean(c.get_text(" ")).lower() for c in trs[0].find_all(["th", "td"])]
    colmap = {f: headers.index(clean(h).lower()) for f, h in cfg["columns"].items()
              if clean(h).lower() in headers}
    recs = []
    for tr in trs[1:]:
        cells = [clean(c.get_text(" ")) for c in tr.find_all(["td", "th"])]
        row = {f: (cells[i] if i < len(cells) else "") for f, i in colmap.items()}
        if not row.get("name"):
            continue
        rec = {f: row.get(f, "") for f in _COLS}
        rec.update(source_id=src["id"], source_url=cfg["url"], rank=RANK["table"])
        recs.append(_with_sector(rec, clf, src.get("sector")))
    return recs


# =============================================================================
# Runner
# =============================================================================
def _agency_row(a: dict, today: dt.date) -> dict:
    return {"name": clean(a.get("name")), "norm": normalize_company(a.get("name", "")),
            "website": clean(a.get("website")), "email": clean(a.get("email")),
            "licence_number": clean(a.get("licence_number")),
            "licence_status": clean(a.get("licence_status")),
            "source_id": clean(a.get("source_id")), "source_url": clean(a.get("source_url")),
            "last_seen": today.isoformat()}


def run(dry_run: bool = False, today: dt.date | None = None, client: PoliteClient | None = None,
        data_dir=None, config_dir=None, root=None) -> dict:
    today = today or dt.date.today()
    data_dir = data_dir or DATA_DIR
    root = root or ROOT
    sources_cfg = load_yaml("sources.yaml", config_dir)
    settings = sources_cfg["settings"]
    sector_cfg = load_yaml("sector_map.yaml", config_dir)
    clf, icp = Classifier(sector_cfg), sector_cfg.get("icp", {})
    log, records, agencies = [], [], []

    # previous master -> store (so IDs stay stable and history is kept)
    store = EntityStore()
    prev = read_csv(data_dir / "kenya_companies_master.csv")
    for r in prev:
        store.add_existing(r)
    baseline = len(prev) == 0

    for src in sources_cfg.get("engine2_companies", []):
        ok, why = gate(src, settings)
        if not ok:
            log.append(log_row(ENGINE, src["id"], f"skipped:{why}", today=today))
            continue
        try:
            if src["id"] == "engine1_employers":
                recs, ags = adapter_employers(clf, data_dir)
                records += recs
                agencies += ags
                log.append(log_row(ENGINE, src["id"], "ok" if recs else "zero_results", len(recs), today=today))
            elif src["id"] == "career_pages_seed":
                recs = adapter_career_seeds(clf, config_dir)
                records += recs
                log.append(log_row(ENGINE, src["id"], "ok" if recs else "zero_results", len(recs), today=today))
            elif src.get("table"):
                client = client or PoliteClient(settings, CACHE_DIR)
                client.begin_source(src.get("max_requests"))
                recs = adapter_html_table(client, src, clf)
                records += recs
                log.append(log_row(ENGINE, src["id"], "ok" if recs else "zero_results", len(recs), today=today))
            else:
                log.append(log_row(ENGINE, src["id"], "skipped:no_adapter_or_table_config", today=today))
        except (RobotsDisallowed, SourceBlocked) as e:
            log.append(log_row(ENGINE, src["id"], "blocked", detail=str(e), today=today))
        except Exception as e:
            log.append(log_row(ENGINE, src["id"], "error", detail=f"{type(e).__name__}: {e}"[:300], today=today))

    for imp in (load_yaml("imports.yaml", config_dir).get("imports") or []):
        if not (imp.get("reviewed_by") and imp.get("reviewed_on")):
            log.append(log_row(ENGINE, f"import:{imp.get('id')}", "skipped:not_reviewed", today=today))
            continue
        recs, ags, problems = adapter_csv_import(imp, clf, root)
        records += recs
        agencies += ags
        status = "error" if problems and not (recs or ags) else ("ok" if (recs or ags) else "zero_results")
        log.append(log_row(ENGINE, f"import:{imp['id']}", status, len(recs) + len(ags), "; ".join(problems), today))

    # resolve
    changes, created_n, skipped = [], 0, 0
    for rec in records:
        try:
            e, created = store.add_record(rec, today, baseline)
        except ValueError:
            skipped += 1
            continue
        if created:
            created_n += 1
            if not baseline:
                changes.append({"date": today.isoformat(), "change_type": "new_company",
                                "entity_type": "company", "entity_id": e["company_id"],
                                "company": e["display_name"], "summary": f"new in {e['sector'] or 'unclassified'}",
                                "source_url": rec.get("source_url", "")})

    # competitor agencies (previous file + this run), deduped by norm
    ag_by_norm = {r["norm"]: r for r in read_csv(data_dir / "competitor_agencies.csv") if r.get("norm")}
    for a in agencies:
        row = _agency_row(a, today)
        if row["norm"]:
            ag_by_norm[row["norm"]] = {**ag_by_norm.get(row["norm"], {}), **{k: v for k, v in row.items() if v}}
    agency_norms = set(ag_by_norm)

    # ICP + low-confidence review queue
    review = list({(r["company_a"], r["company_b"]): r for r in store.review}.values())
    for e in store.entities:
        e["icp_status"], e["icp_note"] = icp_status(e, icp, agency_norms)
        if e["sector"] in ("", "other") or e["classification_confidence"] == "low":
            review.append({"company_a": e["display_name"], "company_b": "",
                           "ratio": "", "reason": "sector uncertain: confirm or add override"})

    summary = {"companies": len(store.entities), "created": created_n, "agencies": len(ag_by_norm),
               "review": len(review), "baseline": baseline, "skipped_unnamed": skipped,
               "icp": dict(Counter(e["icp_status"] for e in store.entities))}
    if dry_run:
        return summary
    write_csv(data_dir / "kenya_companies_master.csv", store.rows(), FIELDS)
    write_csv(data_dir / "competitor_agencies.csv", sorted(ag_by_norm.values(), key=lambda r: r["name"].lower()), AGENCY_FIELDS)
    write_csv(data_dir / "needs_review.csv", review, REVIEW_FIELDS)
    append_csv(data_dir / "changes.csv", changes, CHANGE_FIELDS)
    append_csv(data_dir / "scrape_log.csv", log, LOG_FIELDS)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Engine 2: Kenya companies master list")
    ap.add_argument("--dry-run", action="store_true")
    print(json.dumps(run(dry_run=ap.parse_args().dry_run), indent=2))
