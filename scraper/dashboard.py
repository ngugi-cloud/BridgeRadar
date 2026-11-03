"""PUBLIC dashboard data: docs/data/dashboard.json (served by GitHub Pages).

AGGREGATES ONLY. Reads nothing from the private folder. Never contains contact
names/emails/phones, ranked leads, lead scores, Lead_IDs, outreach angles, or
per-company temperatures. The company table shows contact STATUS only
(found / not found / not checked). A test enforces this.

Run:  python -m scraper.dashboard           (normally called by run_all.py)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
from collections import Counter, defaultdict

from .common.config import DATA_DIR, ROOT, load_yaml
from .common.store import read_csv
from .common.textutil import clean, normalize_company, parse_date

DOCS_DIR = ROOT / "docs"
LABEL = "signal-based prioritization"
TOP_N = 10
TREND_DAYS = 30
TEMPS = ["Hot", "Warm", "Cold"]

_SECTOR_LABELS = {"tech_ecommerce": "Tech & e-commerce", "retail_fmcg": "Retail & FMCG",
                  "ngo_development": "NGO & development", "financial_services": "Financial services",
                  "professional_services": "Professional services", "public_sector": "Public sector",
                  "other": "Other / unclassified", "": "Other / unclassified"}
ICP_LABELS = {"in_band": "SME 10–250 staff (sourced)", "unknown_size": "Size unverified",
              "large_outside_band": "Large company", "excluded_public": "Public sector (excluded)",
              "excluded_ngo": "NGO (excluded)", "excluded_facility": "Below Level 3 facility (excluded)",
              "excluded_micro": "Under 10 staff (excluded)", "competitor_agency": "Recruitment agency (excluded)",
              "": "Not assessed"}
CONTACT_LABELS = {"found": "Found", "not_found": "Not found", "": "Not checked"}
SIGNAL_LABELS = {"funding_raised": "Funding raised", "expansion_announced": "Expansion announced",
                 "government_investment_deal": "Investment / PPP deal", "mna_or_ipo": "M&A / listing",
                 "licence_or_approval": "Licence / approval", "contract_or_tender_won": "Contract / tender won",
                 "leadership_hire": "Leadership hire", "product_launch": "Product launch",
                 "workforce_development": "Workforce development", "youth_or_job_access": "Youth / job access",
                 "hr_system_or_transformation": "HR / payroll transformation",
                 "overseas_remote_hiring": "Overseas / remote hiring",
                 "international_market_entry": "International company enters Kenya"}
# Every key that may appear in a company row. Anything else is a bug (see tests).
COMPANY_KEYS = ["name", "sector", "location", "fit", "open_jobs", "contact", "website"]


def sector_label(key: str) -> str:
    key = clean(key)
    return _SECTOR_LABELS.get(key) or key.replace("_", " ").capitalize()


def _status_group(status: str) -> str:
    s = clean(status)
    if s in ("ok", "baseline_run"):
        return "ok"
    if s == "zero_results":
        return "zero results"
    if s.startswith("skipped:manual"):
        return "manual only"
    if s.startswith("skipped"):
        return "off / not reviewed"
    if s.startswith("blocked"):
        return "blocked"
    return "error" if s == "error" else s or "unknown"


def build(data_dir=None, today: dt.date | None = None, meta: dict | None = None, sector_map: dict | None = None) -> dict:
    today = today or dt.date.today()
    data_dir = pathlib.Path(data_dir or DATA_DIR)
    meta = meta or {}
    sector_map = sector_map if sector_map is not None else load_yaml("sector_map.yaml")
    targets = set((sector_map.get("verticals") or {}).get("target") or [])

    jobs = [j for j in read_csv(data_dir / "active_jobs.csv") if clean(j.get("status") or "open") == "open"]
    remote_jobs = [j for j in jobs if clean(j.get("work_arrangement")) == "remote"]
    kenya_remote_jobs = [
        j for j in remote_jobs
        if clean(j.get("talent_market")) == "Kenya / Africa eligible"
        or re.search(r"\b(kenya|kenyan|africa|african)\b", " ".join((
            j.get("location", ""), j.get("title", "")
        )), re.I)
    ]
    closed = read_csv(data_dir / "closed_jobs.csv")
    master = read_csv(data_dir / "kenya_companies_master.csv")
    leads = read_csv(data_dir / "leads_summary.csv")
    signals = read_csv(data_dir / "signals_summary.csv")
    log = read_csv(data_dir / "scrape_log.csv")

    # ---- baseline period (first ~30 days: "new" counts are unreliable)
    base_dates = [parse_date(j.get("first_seen"), today) for j in jobs + closed if clean(j.get("first_seen_baseline")) == "1"]
    base_dates = [d for d in base_dates if d]
    baseline_since = min(base_dates) if base_dates else None
    in_baseline = bool(baseline_since and (today - baseline_since).days < 30)

    # ---- KPIs
    t = today.isoformat()
    new_today = sum(1 for j in jobs if j.get("first_seen") == t and clean(j.get("first_seen_baseline")) != "1")
    employers = [j for j in jobs if j.get("posted_by") not in ("agency", "confidential")]
    closed_jobs_30d = sum(
        1 for j in closed
        if parse_date(j.get("closed_date"), today)
        and 0 <= (today - parse_date(j.get("closed_date"), today)).days <= 30
    )
    by_company = Counter(clean(j.get("company")) for j in employers if clean(j.get("company")))
    hiring_norms = defaultdict(int)
    for name, n in by_company.items():
        hiring_norms[normalize_company(name)] += n

    # ---- charts
    by_sector = Counter(clean(j.get("sector")) or "other" for j in jobs)
    master_sector = {normalize_company(m.get("display_name", "")): m.get("sector", "") for m in master}
    top = sorted(by_company.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:TOP_N]
    remote_by_company = Counter(
        clean(j.get("company")) for j in remote_jobs if clean(j.get("company"))
    )
    top_remote = sorted(remote_by_company.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:TOP_N]
    trend = []
    first_seen = Counter(j.get("first_seen") for j in jobs + closed if clean(j.get("first_seen_baseline")) != "1")
    for i in range(TREND_DAYS - 1, -1, -1):
        d = (today - dt.timedelta(days=i)).isoformat()
        trend.append({"date": d, "count": first_seen.get(d, 0)})

    lead_temp = Counter()
    lead_matrix = defaultdict(Counter)
    for r in leads:
        temp, n = clean(r.get("temperature")), int(float(r.get("count") or 0))
        if temp in TEMPS and n > 0:
            lead_temp[temp] += n
            lead_matrix[clean(r.get("sector")) or "other"][temp] += n
    sig_types = Counter()
    for r in signals:
        sig_types[clean(r.get("signal_type"))] += int(float(r.get("count") or 0))

    # ---- data sources: latest status per source
    latest = {}
    for r in log:
        k = (clean(r.get("engine")), clean(r.get("source_id")))
        if k[1] == "_run":
            continue
        if k not in latest or clean(r.get("run_date")) >= clean(latest[k].get("run_date")):
            latest[k] = r
    sources = sorted(({"engine": e, "source": s, "status": _status_group(r.get("status")), "date": clean(r.get("run_date"))}
                      for (e, s), r in latest.items()), key=lambda x: (x["engine"], x["source"]))

    # ---- company table (contact STATUS only)
    companies = []
    for m in master:
        name = clean(m.get("display_name")) or clean(m.get("legal_name"))
        if not name:
            continue
        site = clean(m.get("website"))
        companies.append({
            "name": name, "sector": sector_label(m.get("sector", "")),
            "location": clean(m.get("hq_location")),
            "fit": ICP_LABELS.get(clean(m.get("icp_status")), clean(m.get("icp_status"))),
            "open_jobs": hiring_norms.get(normalize_company(name), 0),
            "contact": CONTACT_LABELS.get(clean(m.get("contact_status")), "Not checked"),
            "website": site if site.startswith(("http://", "https://")) else "",
        })
    companies.sort(key=lambda c: (-c["open_jobs"], c["name"].lower()))

    steps = meta.get("steps") or {}
    pipeline_ok = bool(meta.get("ok", True))
    source_states = [s["status"] for s in sources]
    external_states = [s["status"] for s in sources if s["engine"] in ("engine1_jobs", "engine4_signals")]
    if external_states and not any(s == "ok" for s in external_states):
        pipeline_ok = False
    return {
        "generated_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "run_date": t,
        "last_refreshed_utc": meta.get("last_refreshed_utc", ""),
        "pipeline_ok": pipeline_ok,
        "failed_steps": [str(s) for s in meta.get("failed_steps") or []],
        "baseline": {"active": in_baseline, "since": baseline_since.isoformat() if baseline_since else ""},
        "kpis": {"active_jobs": len(jobs), "new_today": new_today, "companies_hiring": len(hiring_norms),
                 "companies_tracked": len(companies),
                 "remote_jobs": len(remote_jobs),
                 "kenya_eligible_remote_jobs": len(kenya_remote_jobs),
                 "closed_jobs_30d": closed_jobs_30d,
                 "companies_with_contact": sum(1 for c in companies if c["contact"] == "Found"),
                 "live_signals": sum(sig_types.values()), "leads": sum(lead_temp.values())},
        "jobs_by_sector": [{"label": sector_label(k), "target": k in targets, "count": n}
                           for k, n in sorted(by_sector.items(), key=lambda kv: (-kv[1], kv[0]))],
        "top_hiring": [{"label": name, "sector": sector_label(master_sector.get(normalize_company(name), "")), "count": n}
                       for name, n in top],
        "remote_jobs_by_company": [{"label": name, "count": n} for name, n in top_remote],
        "new_jobs_trend": trend,
        "leads_by_temperature": {k: lead_temp.get(k, 0) for k in TEMPS},
        "leads_by_sector": [{"label": sector_label(s), **{k: c.get(k, 0) for k in TEMPS}}
                            for s, c in sorted(lead_matrix.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))],
        "signals_by_type": [{"label": SIGNAL_LABELS.get(k, k.replace("_", " ")), "count": n}
                            for k, n in sorted(sig_types.items(), key=lambda kv: (-kv[1], kv[0])) if n],
        "sources": sources,
        "companies": companies,
        "label": LABEL,
        "steps_ok": {k: (v.get("status") == "ok") for k, v in steps.items()},
    }


def write(payload: dict, docs_dir=None) -> pathlib.Path:
    out = pathlib.Path(docs_dir or DOCS_DIR) / "data" / "dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the public dashboard data (aggregates only)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    meta_path = DATA_DIR / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    p = build(meta=meta)
    if args.dry_run:
        print(json.dumps(p["kpis"], indent=2))
    else:
        print(write(p))
