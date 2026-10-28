"""LEAD SCORING - builds the ranked priority queue.   SIGNAL-BASED PRIORITIZATION.

Temperature applies to LEADS, not companies. A lead is one actionable trigger:
  job_lead            one open posting from Engine 1            Lead_ID  J-<job_id>
  signal_lead         one clustered news event from Engine 4    Lead_ID  S-<signal_id>
  contact_change_lead a newly detected HR/TA head (Engine 3)    Lead_ID  C-<contact_id>

Score (0-100) = trigger (<=40) + momentum (<=20) + account fit (<=20) + reachability (<=20).
Every number comes from config/scoring.yaml. Why_This_Score shows each component.

Inputs:   data/active_jobs.csv, data/kenya_companies_master.csv, data/competitor_agencies.csv,
          data/sector_modifiers.csv, PRIVATE hiring_signals.csv, hr_contacts.csv,
          suppression.csv, clients.csv, priority_queue.csv (previous run),
          lead_status.csv (optional: Sheet edits written back by Apps Script).
Outputs:  PRIVATE priority_queue.csv (names, angles, ranking: NEVER public)
          data/leads_summary.csv (public: counts by sector x temperature only)

Human columns (Owner, Status, Last_contacted, Notes, Outcome) are never overwritten
by the pipeline: they are carried forward from the previous queue, or taken from
lead_status.csv when the Sheet has sent them back.

Run:  python -m scraper.scoring [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
from collections import Counter, defaultdict

from .common.config import DATA_DIR, load_yaml
from .common.entities import domain_of
from .common.store import LOG_FIELDS, append_csv, log_row, read_csv, write_csv
from .common.textutil import clean, normalize_company, parse_date
from .engine3_contacts import private_dir

ENGINE = "scoring"
LABEL = "signal-based prioritization"

QUEUE_FIELDS = [
    # the columns agreed with the owner
    "Lead_ID", "Lead_Type", "Company", "Temperature", "Score", "Why_This_Score", "Suggested_Angle",
    "Primary_Lead", "Owner", "Status", "Last_contacted", "Notes", "Outcome", "Trigger_Type",
    # supporting columns (source URL is mandatory on every row; the rest drive the Sheet/lifecycle)
    "Action", "Flags", "Sector", "Company_ID", "Trigger_Date", "Source_URL",
    "Hot_Since", "First_Seen", "Last_Seen", "Label",
]
HUMAN = ["Owner", "Status", "Last_contacted", "Notes", "Outcome"]
SUMMARY_FIELDS = ["sector", "temperature", "count"]
TIER = ["Cold", "Warm", "Hot"]
SORT = {"Hot": 0, "Warm": 1, "Cold": 2, "Expired": 3, "Closed": 4}


# =============================================================================
# Small helpers
# =============================================================================
def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _truthy(x) -> bool:
    return clean(x).lower() in ("1", "yes", "true", "y")


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


def _days(d: dt.date | None, today: dt.date) -> int | None:
    return (today - d).days if d else None


class Companies:
    """Looks up master rows by company_id or by any of their normalised names."""

    def __init__(self, master: list[dict]):
        self.by_id = {m["company_id"]: m for m in master if m.get("company_id")}
        self.by_norm = {}
        for m in master:
            names = [m.get("display_name"), m.get("legal_name"), m.get("trading_name")]
            names += (m.get("aliases") or "").split("|")
            for n in names:
                k = normalize_company(n or "")
                if k:
                    self.by_norm.setdefault(k, m)

    def find(self, company_id: str = "", name: str = "") -> dict | None:
        if company_id and company_id in self.by_id:
            return self.by_id[company_id]
        return self.by_norm.get(normalize_company(name or "")) if name else None


def company_key(company_id: str, name: str) -> str:
    return company_id or "name:" + normalize_company(name)


# =============================================================================
# Exclusions
# =============================================================================
class Exclusions:
    def __init__(self, scoring: dict, sector_map: dict, suppression: list[dict], clients: list[dict],
                 agencies: list[dict]):
        ex = scoring["exclusions"]
        self.icp = {"competitor_agency"} | set(ex.get("also_exclude_icp") or [])
        self.sectors = set()
        if ex.get("public_sector"):
            self.icp.add("excluded_public")
            self.sectors.add("public_sector")
        if ex.get("ngo"):
            self.icp.add("excluded_ngo")
            self.sectors.add("ngo_development")
        self.posted_by = set(ex.get("job_posted_by") or [])
        self.sup = {"company_id": set(), "name": set(), "domain": set(), "email": set()}
        for r in suppression:
            t, v = clean(r.get("type")).lower(), clean(r.get("value")).lower()
            if t in ("company", "do_not_contact"):
                t = "name"
            if t in self.sup and v:
                self.sup[t].add(normalize_company(v) if t == "name" else v)
        self.client_ids = {clean(r.get("company_id")) for r in clients if clean(r.get("company_id"))}
        self.client_norms = {normalize_company(r.get("name", "")) for r in clients if clean(r.get("name"))}
        self.agency_norms = {clean(r.get("norm")) or normalize_company(r.get("name", "")) for r in agencies}
        self.agency_norms.discard("")

    def contact_suppressed(self, c: dict) -> bool:
        """A suppressed email/domain is never a contact route (even on days Engine 3 does not run)."""
        email = clean(c.get("email")).lower()
        return bool(email) and (email in self.sup["email"] or email.split("@", 1)[-1] in self.sup["domain"])

    def reason(self, company_id: str, name: str, master: dict | None, sector: str) -> str:
        """Blank = may be queued; otherwise the exclusion reason (for the run summary)."""
        norms = {normalize_company(name)}
        if master:
            norms |= {normalize_company(master.get(k, "")) for k in ("display_name", "legal_name", "trading_name")}
            norms |= {normalize_company(a) for a in (master.get("aliases") or "").split("|")}
        norms.discard("")
        site_domain = domain_of(master.get("website", "")) if master else ""
        if company_id in self.sup["company_id"] or norms & self.sup["name"] or (site_domain and site_domain in self.sup["domain"]):
            return "suppressed"
        if company_id in self.client_ids or norms & self.client_norms:
            return "existing_client"
        if norms & self.agency_norms:
            return "competitor_agency"
        icp = master.get("icp_status", "") if master else ""
        if icp in self.icp:
            return icp
        if sector in self.sectors:
            return f"sector:{sector}"
        return ""


# =============================================================================
# Component scores (pure)
# =============================================================================
def job_trigger_date(job: dict, today: dt.date) -> tuple[dt.date | None, str]:
    """Posted date if known; else first_seen, unless that was the baseline run (age unknown)."""
    posted = parse_date(job.get("posted_date"), today)
    if posted:
        return posted, "posted"
    if clean(job.get("first_seen_baseline")) == "1":
        return None, "age unknown (seen on first run, no posted date)"
    fs = parse_date(job.get("first_seen"), today)
    return (fs, "first seen") if fs else (None, "age unknown")


def job_trigger(job: dict, today: dt.date, cfg: dict) -> tuple[float, list[str], dt.date | None]:
    c = cfg["lead_score"]["trigger"]["job_lead"]
    pts, why = c["base_active_posting"], [f"open posting {c['base_active_posting']}"]
    d, how = job_trigger_date(job, today)
    age = _days(d, today)
    if age is None:
        why.append(how)
    else:
        bonus = next((b["points"] for b in c["freshness_bonus"] if age <= b["max_days"]), 0)
        if bonus:
            pts += bonus
        why.append(f"{how} {age}d ago +{bonus}")
    urgent = []
    if clean(job.get("urgency_flag")) == "1":
        urgent.append("urgent wording")
    if int(_f(job.get("repost_count"))) > 0:
        urgent.append("reposted")
    if int(_f(job.get("days_open"))) > 30:
        urgent.append("open >30d")
    if urgent:
        pts += c["urgency_bonus"]
        why.append(f"{'/'.join(urgent)} +{c['urgency_bonus']}")
    if job.get("role_family") == "hr":
        pts += c["hiring_own_hr_bonus"]
        why.append(f"hiring own HR +{c['hiring_own_hr_bonus']}")
    if pts > c["cap"]:
        why.append(f"capped {c['cap']}")
    return min(pts, c["cap"]), why, d


def signal_trigger(sig: dict, cfg: dict) -> tuple[float, list[str]]:
    c = cfg["lead_score"]["trigger"]["signal_lead"]
    t = min(_f(sig.get("trigger_strength")), c["cap"])
    outlets = int(_f(sig.get("outlets_count"), 1)) or 1
    why = [f"{sig.get('signal_type', '')}: {_fmt(_f(sig.get('base_points')))} pts x decay {sig.get('decay')} "
           f"x reliability {sig.get('reliability_multiplier')} ({outlets} outlet{'s' if outlets > 1 else ''}) "
           f"x {c['multiplier']}" + (f" + jobs bonus ({sig.get('jobs_stated')} jobs stated)" if clean(sig.get("jobs_stated")) else "")]
    return round(t, 1), why


def account_fit(sector: str, icp: str, cfg: dict, sector_map: dict) -> tuple[float, list[str], list[str]]:
    c = cfg["lead_score"]["account_fit"]
    v = sector_map.get("verticals") or {}
    if sector in (v.get("target") or []):
        s, tag = c["sector"]["target"], "target sector"
    elif sector in (v.get("adjacent") or []):
        s, tag = c["sector"]["adjacent"], "adjacent sector"
    else:
        s, tag = c["sector"]["other"], "other sector"
    flags = []
    size = c["size_icp"]
    if icp == "in_band":
        z, ztag = size["verified_in_band"], "10-250 staff (sourced)"
    elif icp == "large_outside_band":
        z, ztag = size["outside_band_large"], "large company"
        flags.append("large company")
    else:
        z, ztag = size["unknown_size"], "size unverified"
        flags.append("size unverified")
    total = min(s + z, c["max"])
    return total, [f"{tag} {sector or 'other'} {s}", f"{ztag} {z}"], flags


def reachability(contacts: list[dict], today: dt.date, cfg: dict) -> tuple[float, str]:
    """Best contact route at the company. Opted-out or bounced contacts never count."""
    c = cfg["lead_score"]["reachability"]
    best, why = 0.0, "no contact route found 0"
    for r in contacts:
        if _truthy(r.get("opt_out")) or _truthy(r.get("bounce_flag")):
            continue
        conf = clean(r.get("confidence")).lower() or "low"
        if r.get("contact_type") == "named_person" and r.get("role_category") == "hr":
            pts, tag = c["named_hr_or_ta"].get(conf, c["named_hr_or_ta"]["low"]), f"named HR/TA contact ({conf})"
        elif r.get("contact_type") == "named_person":
            pts, tag = (c["other_decision_maker"].get(conf, c["other_decision_maker"]["low"]),
                        f"named {r.get('role_category') or 'other'} decision-maker ({conf})")
        elif r.get("email") or r.get("phone"):
            pts, tag = c["generic_inbox_or_form"], ("HR inbox" if r.get("role_category") == "hr" else
                                                     "general inbox/switchboard")
        else:
            continue
        verified = parse_date(r.get("last_verified_date"), today)
        if not verified or (today - verified).days > c["halve_if_verified_older_than_days"]:
            pts, tag = pts / 2, tag + ", not re-verified in 180d: halved"
        if pts > best:
            best, why = pts, f"{tag} {_fmt(pts)}"
    return min(best, c["max"]), why


def temperature(score: float, fresh: bool, reach: float, cfg: dict) -> tuple[str, list[str]]:
    t = cfg["temperature"]
    flags = []
    if score >= t["hot"]["min_score"]:
        if not fresh:
            return "Warm", ["no fresh trigger"]
        if reach < t["hot"]["min_reachability"]:
            return "Warm", [t["warm"]["no_contact_flag"]]
        return "Hot", flags
    if score >= t["warm"]["min_score"]:
        return "Warm", flags
    return "Cold", flags


def drop_tier(temp: str) -> str:
    return TIER[max(0, TIER.index(temp) - 1)] if temp in TIER else temp


def cap_tier(temp: str, cap: str) -> str:
    return cap if temp in TIER and TIER.index(temp) > TIER.index(cap) else temp


# =============================================================================
# Build leads (pure: everything is passed in)
# =============================================================================
def build_candidates(jobs, signals, contacts, companies: Companies, excl: Exclusions, cfg, sector_map, today):
    """Return (leads, excluded_counter). Each lead is a dict with private keys starting '_'."""
    tcfg = cfg["temperature"]
    fresh_cfg = tcfg["hot"]["fresh_trigger"]
    life = tcfg["lifecycle"]
    q = cfg.get("queue", {})
    angles = q.get("angles", {})
    leads, excluded = [], Counter()

    # ---- job leads
    for j in jobs:
        if clean(j.get("status") or "open") != "open":
            continue
        if j.get("posted_by") in excl.posted_by:
            excluded[f"job_posted_by_{j.get('posted_by')}"] += 1
            continue
        m = companies.find(name=j.get("company", ""))
        cid = m["company_id"] if m else ""
        sector = (m.get("sector") if m else "") or j.get("sector") or "other"
        why_x = excl.reason(cid, j.get("company", ""), m, sector)
        if why_x:
            excluded[why_x] += 1
            continue
        trig, why_t, tdate = job_trigger(j, today, cfg)
        age = _days(tdate, today)
        title = clean(j.get("title"))
        angle = (angles.get("job_lead_hr_role") if j.get("role_family") == "hr" else angles.get("job_lead", ""))
        leads.append({
            "Lead_ID": f"J-{j['job_id']}", "Lead_Type": "job_lead",
            "Company": (m.get("display_name") if m else "") or j.get("company", ""),
            "Company_ID": cid, "Sector": sector, "Trigger_Type": f"job:{j.get('role_family') or 'other'}",
            "Trigger_Date": tdate.isoformat() if tdate else "", "Source_URL": j.get("source_url", ""),
            "Suggested_Angle": (angle or "").format(title=title),
            "_icp": m.get("icp_status", "") if m else "", "_trigger": trig, "_why_trigger": why_t,
            "_fresh": age is not None and age <= fresh_cfg["job_lead_max_days"],
            "_drop_tier": age is not None and age > life["job_lead_drop_tier_after_days_without_new_trigger"],
            "_cap": "", "_flags": [], "_title": title,
        })

    # ---- signal leads
    unmatched_policy = q.get("unmatched_signals", "include_capped_warm")
    for s in signals:
        d = parse_date(s.get("signal_date"), today)
        if not d or (today - d).days > life["signal_lead_expires_after_days"] or _f(s.get("decay")) <= 0:
            excluded["signal_expired"] += 1
            continue
        matched = clean(s.get("company_matched")) == "1"
        if not matched and unmatched_policy == "exclude":
            excluded["signal_unmatched_company"] += 1
            continue
        m = companies.find(company_id=s.get("company_id", ""), name=s.get("company", "") if matched else "")
        cid = m["company_id"] if m else ""
        sector = (m.get("sector") if m else "") or s.get("sector") or "other"
        icp = (m.get("icp_status") if m else "") or ("" if not matched else s.get("icp_status", ""))
        why_x = excl.reason(cid, s.get("company", ""), m, sector) or (icp if icp in excl.icp else "")
        if why_x:
            excluded[why_x] += 1
            continue
        trig, why_t = signal_trigger(s, cfg)
        flags = [] if matched else ["verify company (not in master list)"]
        leads.append({
            "Lead_ID": f"S-{s['signal_id']}", "Lead_Type": "signal_lead",
            "Company": (m.get("display_name") if m else "") or s.get("company", ""),
            "Company_ID": cid, "Sector": sector, "Trigger_Type": f"signal:{s.get('signal_type', '')}",
            "Trigger_Date": d.isoformat(), "Source_URL": s.get("source_url", ""),
            "Suggested_Angle": s.get("suggested_outreach_angle", ""),
            "_icp": icp, "_trigger": trig, "_why_trigger": why_t,
            "_fresh": (today - d).days <= fresh_cfg["signal_lead_max_days"], "_drop_tier": False,
            "_cap": "Warm" if not matched else "", "_flags": flags,
            "_outlets": int(_f(s.get("outlets_count"), 1)),
        })

    # ---- contact-change leads
    cc = cfg["lead_score"]["trigger"]["contact_change_lead"]
    shelf = min(cc["shelf_life_days"], life.get("contact_change_lead_expires_after_days", cc["shelf_life_days"]))
    for c in contacts:
        if clean(c.get("change_detected")) != "new_hr_head" or _truthy(c.get("opt_out")):
            continue
        d = parse_date(c.get("change_date"), today)
        if not d or (today - d).days > shelf:
            excluded["contact_change_expired"] += 1
            continue
        m = companies.find(company_id=c.get("company_id", ""), name=c.get("company", ""))
        cid = (m["company_id"] if m else "") or c.get("company_id", "")
        sector = (m.get("sector") if m else "") or c.get("sector") or "other"
        why_x = excl.reason(cid, c.get("company", ""), m, sector)
        if why_x:
            excluded[why_x] += 1
            continue
        title = clean(c.get("contact_title")) or "HR/TA"
        leads.append({
            "Lead_ID": f"C-{c['contact_id']}", "Lead_Type": "contact_change_lead",
            "Company": (m.get("display_name") if m else "") or c.get("company", ""),
            "Company_ID": cid, "Sector": sector, "Trigger_Type": "contact:new_hr_head",
            "Trigger_Date": d.isoformat(), "Source_URL": c.get("source_url", ""),
            "Suggested_Angle": angles.get("contact_change_lead", "").format(title=title),
            "_icp": m.get("icp_status", "") if m else "", "_trigger": cc["fixed"],
            "_why_trigger": [f"new HR/TA head detected {(today - d).days}d ago {cc['fixed']}"],
            "_fresh": (today - d).days <= fresh_cfg.get("contact_change_lead_max_days", 30),
            "_drop_tier": False, "_cap": "", "_flags": [],
        })
    return leads, excluded


def score_leads(leads, contacts, modifiers, cfg, sector_map, today):
    """Add momentum, fit, reachability, score, temperature and Why_This_Score in place."""
    mcfg = cfg["lead_score"]["momentum"]
    angles = cfg.get("queue", {}).get("angles", {})
    # company-level facts
    by_co = defaultdict(list)
    for L in leads:
        by_co[company_key(L["Company_ID"], L["Company"])].append(L)
    open_jobs = defaultdict(int)
    recent, prior = defaultdict(int), defaultdict(int)
    for L in leads:
        if L["Lead_Type"] != "job_lead":
            continue
        k = company_key(L["Company_ID"], L["Company"])
        open_jobs[k] += 1
        d = parse_date(L["Trigger_Date"], today)
        if d is not None:
            age = (today - d).days
            if 0 <= age < 7:
                recent[k] += 1
            elif 7 <= age < 14:
                prior[k] += 1
    contacts_by_co = defaultdict(list)
    for c in contacts:
        contacts_by_co[c.get("company_id", "")].append(c)
    sector_mod = defaultdict(dict)
    for r in modifiers:
        if clean(r.get("active")) == "1" and clean(r.get("sector")):
            sector_mod[r["sector"]][r.get("type", "")] = _f(r.get("points"))   # each type at most once

    for L in leads:
        k = company_key(L["Company_ID"], L["Company"])
        # momentum
        mom, why_m = 0.0, []
        types_here = {x["Lead_Type"] for x in by_co[k]}
        if types_here - {L["Lead_Type"]}:
            mom += mcfg["also_has_live_lead_of_other_type"]
            why_m.append(f"also {'/'.join(sorted(t.replace('_lead', '') for t in types_here - {L['Lead_Type']}))} lead "
                         f"+{mcfg['also_has_live_lead_of_other_type']}")
        if recent[k] > prior[k] and recent[k] >= 2:     # a trend needs 2+ new postings
            mom += mcfg["hiring_velocity_up"]
            why_m.append(f"postings up ({prior[k]}->{recent[k]} per week) +{mcfg['hiring_velocity_up']}")
        corroborated = any(x.get("_outlets", 1) >= 2 for x in by_co[k])
        if open_jobs[k] >= 3 or corroborated:
            mom += mcfg["volume_or_corroboration"]
            why_m.append(("%d open postings" % open_jobs[k] if open_jobs[k] >= 3 else "signal in 2+ outlets")
                         + f" +{mcfg['volume_or_corroboration']}")
        for typ, pts in sector_mod.get(L["Sector"], {}).items():
            mom += pts
            why_m.append(f"sector {typ.replace('_', ' ')} +{_fmt(pts)}")
        mom = min(mom, mcfg["max"])
        # bulk hiring angle for job leads
        if L["Lead_Type"] == "job_lead" and open_jobs[k] >= 3 and L.get("Trigger_Type") != "job:hr" and angles.get("job_lead_bulk"):
            L["Suggested_Angle"] = angles["job_lead_bulk"]
        # fit + reach
        fit, why_f, fit_flags = account_fit(L["Sector"], L["_icp"], cfg, sector_map)
        reach, why_r = reachability(contacts_by_co.get(L["Company_ID"], []) if L["Company_ID"] else [], today, cfg)
        trig = L["_trigger"]
        score = round(trig + mom + fit + reach, 1)
        temp, t_flags = temperature(score, L["_fresh"], reach, cfg)
        notes = []
        if L["_drop_tier"]:
            new = drop_tier(temp)
            if new != temp:
                notes.append(f"dropped {temp}->{new}: no new trigger for 30+ days")
            temp = new
        if L["_cap"]:
            new = cap_tier(temp, L["_cap"])
            if new != temp:
                notes.append(f"capped at {new}: company unverified")
            temp = new
        flags = L["_flags"] + fit_flags + t_flags
        if reach == 0 and "find contact" not in flags:
            flags.append("find contact")
        L.update({
            "Score": _fmt(score), "Temperature": temp, "_score": score, "_reach": reach,
            "Why_This_Score": " | ".join([
                f"Trigger {_fmt(trig)}/40 ({'; '.join(L['_why_trigger'])})",
                f"Momentum {_fmt(mom)}/20 ({'; '.join(why_m) or 'none'})",
                f"Fit {_fmt(fit)}/20 ({'; '.join(why_f)})",
                f"Reach {_fmt(reach)}/20 ({why_r})",
            ]) + f" = {_fmt(score)}" + (f" [{'; '.join(notes)}]" if notes else "") + f" — {LABEL}",
            "Flags": "; ".join(dict.fromkeys(flags)),
            "Action": cfg["temperature"][temp.lower()]["action"],
        })
    return leads


def group_hot(leads: list[dict]) -> None:
    """One Primary_Lead per company among its Hot leads (highest score wins)."""
    hot = defaultdict(list)
    for L in leads:
        L["Primary_Lead"] = ""
        if L["Temperature"] == "Hot":
            hot[company_key(L["Company_ID"], L["Company"])].append(L)
    for group in hot.values():
        group.sort(key=lambda x: (-x["_score"], x["Lead_ID"]))
        for L in group:
            L["Primary_Lead"] = group[0]["Lead_ID"]
        if len(group) > 1:
            group[0]["Flags"] = "; ".join(x for x in [group[0]["Flags"], f"{len(group)} Hot leads at this company"] if x)


def apply_history(leads, prev_rows, overrides, cfg, today) -> list[dict]:
    """Carry human columns forward, freeze Won/Lost, keep touched-but-expired leads, add lifecycle flags."""
    q = cfg.get("queue", {})
    frozen = set(cfg["exclusions"].get("statuses") or [])
    t = today.isoformat()
    prev = {r["Lead_ID"]: r for r in prev_rows if r.get("Lead_ID")}
    over = {r["Lead_ID"]: r for r in overrides if r.get("Lead_ID")}

    def human(lid: str) -> dict:
        src = over.get(lid) or prev.get(lid) or {}
        h = {k: clean(src.get(k)) for k in HUMAN}
        h["Owner"] = h["Owner"] or q.get("default_owner", "")
        h["Status"] = h["Status"] or "New"
        return h

    out, seen = [], set()
    won_companies = set()
    for lid in set(prev) | set(over):
        if human(lid)["Status"] == "Won":
            r = prev.get(lid) or over.get(lid)
            won_companies.add(company_key(clean(r.get("Company_ID")), clean(r.get("Company"))))

    for L in leads:
        lid = L["Lead_ID"]
        seen.add(lid)
        h = human(lid)
        p = prev.get(lid, {})
        if h["Status"] in frozen:
            row = {**{f: "" for f in QUEUE_FIELDS}, **p, **{k: L.get(k, "") for k in QUEUE_FIELDS if not p.get(k)}}
            row.update(h, Temperature="Closed", Action="", Primary_Lead="", Hot_Since="", Last_Seen=t, Label=LABEL)
            out.append(row)
            continue
        row = {**{f: "" for f in QUEUE_FIELDS}, **{k: v for k, v in L.items() if not k.startswith("_")}, **h}
        row["First_Seen"] = p.get("First_Seen") or t
        row["Last_Seen"] = t
        row["Label"] = LABEL
        flags = [x for x in row["Flags"].split("; ") if x]
        if row["Temperature"] == "Hot":
            row["Hot_Since"] = p.get("Hot_Since") if p.get("Temperature") == "Hot" and p.get("Hot_Since") else t
            hs = parse_date(row["Hot_Since"], today)
            alert = cfg["temperature"]["lifecycle"]["untouched_hot_alert_days"]
            if h["Status"] == "New" and hs and (today - hs).days >= alert:
                flags.append(f"Hot and untouched {alert}+ days")
        else:
            row["Hot_Since"] = ""
        if company_key(row["Company_ID"], row["Company"]) in won_companies:
            flags.append("company already Won: add to clients.csv?")
        row["Flags"] = "; ".join(dict.fromkeys(flags))
        out.append(row)

    keep_days = int(q.get("keep_touched_expired_days", 180))
    for lid in set(prev) | set(over):
        if lid in seen:
            continue
        p = prev.get(lid) or {**{f: "" for f in QUEUE_FIELDS}, "Lead_ID": lid}
        h = human(lid)
        last = parse_date(p.get("Last_Seen"), today) or today
        touched = h["Status"] != "New" or any(h[k] for k in ("Notes", "Last_contacted", "Outcome"))
        if h["Status"] in frozen:
            out.append({**{f: "" for f in QUEUE_FIELDS}, **p, **h, "Temperature": "Closed", "Action": "",
                        "Primary_Lead": "", "Hot_Since": ""})
        elif touched and (today - last).days <= keep_days and p.get("Company"):
            out.append({**{f: "" for f in QUEUE_FIELDS}, **p, **h, "Temperature": "Expired",
                        "Action": "Trigger no longer live (job closed / signal expired); kept for your notes",
                        "Primary_Lead": "", "Hot_Since": ""})
    out.sort(key=lambda r: (SORT.get(r["Temperature"], 9), -_f(r["Score"]), r["Company"].lower(), r["Lead_ID"]))
    return out


def leads_summary(rows: list[dict]) -> list[dict]:
    """PUBLIC aggregate: counts only, no company names, no angles."""
    n = Counter((r["Sector"] or "other", r["Temperature"]) for r in rows if r["Temperature"] in TIER)
    return [{"sector": s, "temperature": t, "count": c} for (s, t), c in sorted(n.items())]


# =============================================================================
# Runner
# =============================================================================
def run(dry_run: bool = False, today: dt.date | None = None, data_dir=None, pdir=None, config_dir=None) -> dict:
    today = today or dt.date.today()
    data_dir, pdir = pathlib.Path(data_dir or DATA_DIR), pathlib.Path(pdir or private_dir())
    cfg, sector_map = load_yaml("scoring.yaml", config_dir), load_yaml("sector_map.yaml", config_dir)

    master = read_csv(data_dir / "kenya_companies_master.csv")
    companies = Companies(master)
    excl = Exclusions(cfg, sector_map, read_csv(pdir / "suppression.csv"), read_csv(pdir / "clients.csv"),
                      read_csv(data_dir / "competitor_agencies.csv"))
    contacts = [c for c in read_csv(pdir / "hr_contacts.csv") if not excl.contact_suppressed(c)]
    jobs = read_csv(data_dir / "active_jobs.csv")
    signals = read_csv(pdir / "hiring_signals.csv")

    leads, excluded = build_candidates(jobs, signals, contacts, companies, excl, cfg, sector_map, today)
    score_leads(leads, contacts, read_csv(data_dir / "sector_modifiers.csv"), cfg, sector_map, today)
    group_hot(leads)
    rows = apply_history(leads, read_csv(pdir / "priority_queue.csv"), read_csv(pdir / "lead_status.csv"), cfg, today)

    temps = Counter(r["Temperature"] for r in rows)
    summary = {"leads": len(rows), "temperatures": dict(temps),
               "by_type": dict(Counter(r["Lead_Type"] for r in rows if r["Temperature"] in TIER)),
               "excluded": dict(excluded)}
    if dry_run:
        return summary
    write_csv(pdir / "priority_queue.csv", rows, QUEUE_FIELDS)                 # PRIVATE
    write_csv(data_dir / "leads_summary.csv", leads_summary(rows), SUMMARY_FIELDS)   # public aggregates
    append_csv(data_dir / "scrape_log.csv",
               [log_row(ENGINE, "_run", "ok" if rows else "zero_results", len(rows),
                        json.dumps({k: v for k, v in temps.items()}), today)], LOG_FIELDS)
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Lead scoring: ranked priority queue (signal-based prioritization)")
    ap.add_argument("--dry-run", action="store_true")
    print(json.dumps(run(dry_run=ap.parse_args().dry_run), indent=2))
