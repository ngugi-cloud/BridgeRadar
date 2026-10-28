"""Offline tests for lead scoring and run_all.  Run:  python -m unittest discover -s tests -t . -v"""
import contextlib
import datetime as dt
import io
import json
import pathlib
import re
import tempfile
import unittest

from scraper import run_all
from scraper import scoring as sc
from scraper.common.config import load_yaml
from scraper.common.store import read_csv, write_csv

TODAY = dt.date(2026, 10, 23)
CFG, SMAP = load_yaml("scoring.yaml"), load_yaml("sector_map.yaml")


def d(days_ago: int) -> str:
    return (TODAY - dt.timedelta(days=days_ago)).isoformat()


MASTER = [
    {"company_id": "c1", "display_name": "Karen Hospital Ltd", "sector": "healthcare", "icp_status": "in_band",
     "website": "https://karen.co.ke"},
    {"company_id": "c2", "display_name": "Twiga Foods Ltd", "sector": "retail_fmcg", "icp_status": "unknown_size"},
    {"company_id": "c3", "display_name": "State Hospital", "sector": "healthcare", "icp_status": "excluded_public"},
    {"company_id": "c4", "display_name": "Help Kenya Trust", "sector": "ngo_development", "icp_status": "excluded_ngo"},
    {"company_id": "c5", "display_name": "Client Co Ltd", "sector": "manufacturing", "icp_status": "in_band"},
    {"company_id": "c6", "display_name": "Quiet Mills Ltd", "sector": "manufacturing", "icp_status": "in_band",
     "website": "https://quietmills.co.ke"},
    {"company_id": "c7", "display_name": "Big Telco PLC", "sector": "telecoms", "icp_status": "large_outside_band"},
]
MASTER_FIELDS = ["company_id", "display_name", "legal_name", "trading_name", "aliases", "sector", "icp_status", "website"]


def job(jid, company, title="Registered Nurse", posted=2, role="clinical", **kw):
    return {"job_id": jid, "company": company, "title": title, "posted_date": d(posted) if posted is not None else "",
            "first_seen": kw.pop("first_seen", d(posted or 0)), "first_seen_baseline": kw.pop("baseline", ""),
            "role_family": role, "urgency_flag": kw.pop("urgent", ""), "repost_count": kw.pop("reposts", 0),
            "days_open": kw.pop("days_open", posted or 0), "posted_by": kw.pop("posted_by", "unknown"),
            "status": "open", "sector": kw.pop("sector", ""), "source_url": f"https://jobs/{jid}", **kw}


def contact(cid, company_id, ctype="named_person", role="hr", conf="High", verified=5, **kw):
    return {"contact_id": cid, "company_id": company_id, "company": kw.pop("company", ""), "contact_type": ctype,
            "role_category": role, "confidence": conf, "last_verified_date": d(verified),
            "contact_name": kw.pop("name", "Jane Wanjiru" if ctype == "named_person" else ""),
            "contact_title": kw.pop("title", "HR Manager"), "email": kw.pop("email", "jane@karen.co.ke"),
            "phone": "", "opt_out": kw.pop("opt_out", ""), "bounce_flag": "", "source_url": "https://karen.co.ke/team",
            "change_detected": kw.pop("change", ""), "change_date": kw.pop("change_date", ""), **kw}


def signal(sid, company_id, company, days=5, trigger=37.5, matched="1", outlets=1, **kw):
    return {"signal_id": sid, "company_id": company_id, "company": company, "company_matched": matched,
            "sector": kw.pop("sector", "healthcare"), "icp_status": kw.pop("icp", ""),
            "signal_type": kw.pop("stype", "expansion_announced"), "signal_date": d(days),
            "base_points": 15, "decay": kw.pop("decay", 1.0), "reliability_multiplier": 1.0,
            "trigger_strength": trigger, "outlets_count": outlets, "jobs_stated": "",
            "suggested_outreach_angle": "Expansion announced — pitch recruitment for the new site",
            "source_url": f"https://news/{sid}", **kw}


def excl(**kw):
    return sc.Exclusions(CFG, SMAP, kw.get("sup", []), kw.get("clients", []), kw.get("agencies", []))


def build(jobs=(), signals=(), contacts=(), master=MASTER, modifiers=(), **kw):
    leads, ex = sc.build_candidates(list(jobs), list(signals), list(contacts), sc.Companies(master),
                                    kw.get("excl") or excl(), CFG, SMAP, TODAY)
    sc.score_leads(leads, list(contacts), list(modifiers), CFG, SMAP, TODAY)
    sc.group_hot(leads)
    return {L["Lead_ID"]: L for L in leads}, ex


def components(why: str) -> list[float]:
    return [float(x) for x in re.findall(r"(?:Trigger|Momentum|Fit|Reach) ([\d.]+)/", why)]


class ComponentTests(unittest.TestCase):
    def test_job_trigger_points_and_cap(self):
        t, why, _ = sc.job_trigger(job("j", "X", posted=2, role="hr", urgent="1"), TODAY, CFG)
        self.assertEqual(t, 40)                                    # 15 + 10 + 10 + 5 = 40 (cap)
        t, _, _ = sc.job_trigger(job("j", "X", posted=10), TODAY, CFG)
        self.assertEqual(t, 19)                                    # 15 + 4
        t, _, _ = sc.job_trigger(job("j", "X", posted=45, days_open=0), TODAY, CFG)
        self.assertEqual(t, 15)                                    # too old for a freshness bonus

    def test_baseline_job_without_posted_date_has_unknown_age(self):
        j = job("j", "X", posted=None, first_seen=d(0), baseline="1")
        t, why, tdate = sc.job_trigger(j, TODAY, CFG)
        self.assertEqual((t, tdate), (15, None))                   # never guess the age
        self.assertIn("age unknown", " ".join(why))

    def test_account_fit(self):
        self.assertEqual(sc.account_fit("healthcare", "in_band", CFG, SMAP)[0], 20)
        self.assertEqual(sc.account_fit("energy", "unknown_size", CFG, SMAP)[0], 8)
        fit, _, flags = sc.account_fit("telecoms", "large_outside_band", CFG, SMAP)
        self.assertEqual((fit, flags), (6, ["large company"]))
        self.assertEqual(sc.account_fit("other", "", CFG, SMAP)[0], 5)

    def test_reachability(self):
        self.assertEqual(sc.reachability([contact("a", "c1")], TODAY, CFG)[0], 20)
        self.assertEqual(sc.reachability([contact("a", "c1", conf="Medium")], TODAY, CFG)[0], 14)
        self.assertEqual(sc.reachability([contact("a", "c1", role="finance")], TODAY, CFG)[0], 12)
        self.assertEqual(sc.reachability([contact("a", "c1", ctype="role_inbox", role="generic")], TODAY, CFG)[0], 5)
        self.assertEqual(sc.reachability([contact("a", "c1", verified=200)], TODAY, CFG)[0], 10)   # halved
        self.assertEqual(sc.reachability([contact("a", "c1", opt_out="yes")], TODAY, CFG)[0], 0)
        self.assertEqual(sc.reachability([], TODAY, CFG), (0.0, "no contact route found 0"))

    def test_temperature_rules(self):
        self.assertEqual(sc.temperature(70, True, 14, CFG), ("Hot", []))
        self.assertEqual(sc.temperature(70, True, 0, CFG), ("Warm", ["find contact"]))
        self.assertEqual(sc.temperature(70, False, 20, CFG), ("Warm", ["no fresh trigger"]))
        self.assertEqual(sc.temperature(40, True, 20, CFG)[0], "Warm")
        self.assertEqual(sc.temperature(39.9, True, 20, CFG)[0], "Cold")
        self.assertEqual((sc.drop_tier("Hot"), sc.drop_tier("Cold")), ("Warm", "Cold"))


class LeadTests(unittest.TestCase):
    def test_hot_job_lead_with_full_explanation(self):
        leads, _ = build(jobs=[job("j1", "Karen Hospital Ltd", posted=2)], contacts=[contact("k1", "c1")])
        L = leads["J-j1"]
        # trigger 25 + momentum 0 + fit 20 + reach 20 = 65 -> Hot
        self.assertEqual((L["Score"], L["Temperature"], L["Company_ID"]), ("65", "Hot", "c1"))
        self.assertEqual(sum(components(L["Why_This_Score"])), 65)
        self.assertEqual(L["Primary_Lead"], "J-j1")
        self.assertIn("signal-based prioritization", L["Why_This_Score"])
        self.assertTrue(L["Source_URL"])

    def test_would_be_hot_without_contact_is_warm_and_flagged(self):
        leads, _ = build(jobs=[job("j1", "Karen Hospital Ltd", posted=1, role="hr", urgent="1")])
        L = leads["J-j1"]
        self.assertEqual((L["Temperature"], float(L["Score"])), ("Warm", 60.0))
        leads, _ = build(jobs=[job("j1", "Karen Hospital Ltd", posted=1, role="hr", urgent="1")],
                         modifiers=[{"sector": "healthcare", "type": "hiring_intent_survey", "points": 4, "active": "1"},
                                    {"sector": "healthcare", "type": "regulatory_trigger", "points": 3, "active": "1"}])
        L = leads["J-j1"]
        self.assertEqual((L["Temperature"], float(L["Score"])), ("Warm", 67.0))   # >=65 but no contact route
        self.assertIn("find contact", L["Flags"])

    def test_exclusions(self):
        jobs = [job("a", "State Hospital"), job("b", "Help Kenya Trust"), job("c", "Client Co Ltd"),
                job("d", "Quiet Mills Ltd"), job("e", "Acme Recruiters"), job("f", "Confidential", posted_by="confidential"),
                job("g", "Some Staffing", posted_by="agency"), job("h", "Ministry Of Things", sector="public_sector"),
                job("i", "Twiga Foods Ltd")]
        ex = excl(sup=[{"type": "domain", "value": "quietmills.co.ke"}], clients=[{"name": "Client Co Limited"}],
                  agencies=[{"name": "Acme Recruiters", "norm": "acme recruiters"}])
        leads, why = build(jobs=jobs, excl=ex)
        self.assertEqual(list(leads), ["J-i"])
        for reason in ("excluded_public", "excluded_ngo", "existing_client", "suppressed", "competitor_agency",
                       "job_posted_by_confidential", "job_posted_by_agency", "sector:public_sector"):
            self.assertIn(reason, why)

    def test_signal_leads(self):
        sigs = [signal("s1", "c1", "Karen Hospital Ltd", days=5),
                signal("s2", "c1", "Karen Hospital Ltd", days=95, decay=0),                 # expired
                signal("s3", "", "Zuri Labs", days=3, trigger=40, matched="0", sector="tech_ecommerce"),
                signal("s4", "c3", "State Hospital", icp="unknown_size")]                   # stale icp on the signal row
        leads, why = build(signals=sigs, contacts=[contact("k1", "c1")])
        self.assertEqual(set(leads), {"S-s1", "S-s3"})
        self.assertEqual(why["signal_expired"], 1)
        self.assertEqual(why["excluded_public"], 1)                # master's current status wins
        self.assertEqual(leads["S-s1"]["Temperature"], "Hot")       # 37.5 + 0 + 20 + 20
        z = leads["S-s3"]
        self.assertEqual((float(z["Score"]), z["Temperature"]), (52.0, "Warm"))   # 40 + 0 + (8+4) + 0
        # unmatched company is flagged for checking (and can never be Hot: see the cap test)
        self.assertIn("verify company", z["Flags"])

    def test_unmatched_signal_capped_warm(self):
        L = {"_cap": "Warm"}
        self.assertEqual(sc.cap_tier("Hot", L["_cap"]), "Warm")
        self.assertEqual(sc.cap_tier("Cold", "Warm"), "Cold")

    def test_momentum_cross_type_volume_and_grouping(self):
        jobs = [job(f"j{i}", "Karen Hospital Ltd", posted=1) for i in range(3)]
        leads, _ = build(jobs=jobs, signals=[signal("s1", "c1", "Karen Hospital Ltd", outlets=2)],
                         contacts=[contact("k1", "c1")])
        L = leads["J-j0"]
        mom = components(L["Why_This_Score"])[1]
        self.assertEqual(mom, 20)                                   # other type 10 + velocity 5 + volume 5
        hot = [x for x in leads.values() if x["Temperature"] == "Hot"]
        self.assertEqual(len(hot), 4)
        primaries = {x["Primary_Lead"] for x in hot}
        self.assertEqual(primaries, {"S-s1"})                       # highest score is primary for the company
        self.assertIn("4 Hot leads", leads["S-s1"]["Flags"])
        self.assertIn("Several open roles", L["Suggested_Angle"])

    def test_old_job_drops_a_tier(self):
        j = job("j1", "Karen Hospital Ltd", posted=40, urgent="1", role="hr")   # 15+0+10+5 = 30
        leads, _ = build(jobs=[j], contacts=[contact("k1", "c1")],
                         signals=[signal("s1", "c1", "Karen Hospital Ltd", days=70, trigger=5)])
        L = leads["J-j1"]
        self.assertEqual(float(L["Score"]), 80.0)                    # 30 + 10 + 20 + 20, but trigger is 40d old
        self.assertEqual(L["Temperature"], "Cold")                  # Warm (not fresh) -> dropped to Cold
        self.assertIn("no new trigger for 30+ days", L["Why_This_Score"])

    def test_contact_change_lead(self):
        cs = [contact("k1", "c1", change="new_hr_head", change_date=d(10), title="Head of People"),
              contact("k2", "c2", change="new_hr_head", change_date=d(61))]
        leads, why = build(contacts=cs)
        self.assertEqual(list(leads), ["C-k1"])
        L = leads["C-k1"]
        self.assertEqual((float(L["Score"]), L["Temperature"]), (60.0, "Warm"))   # 20 + 0 + 20 + 20
        self.assertIn("Head of People", L["Suggested_Angle"])
        self.assertEqual(why["contact_change_expired"], 1)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.leads, _ = build(jobs=[job("j1", "Karen Hospital Ltd", posted=2), job("j2", "Twiga Foods Ltd", posted=3)],
                              contacts=[contact("k1", "c1")])
        self.leads = list(self.leads.values())

    def test_human_columns_kept_and_sheet_override_wins(self):
        prev = [{"Lead_ID": "J-j1", "Owner": "Ngugi", "Status": "Contacted", "Notes": "called Tue",
                 "Temperature": "Hot", "Hot_Since": d(3), "First_Seen": d(3), "Company": "Karen Hospital Ltd"},
                {"Lead_ID": "J-j2", "Status": "Contacted", "Notes": "old note", "Company": "Twiga Foods Ltd"}]
        over = [{"Lead_ID": "J-j2", "Status": "Meeting booked", "Notes": "", "Owner": "Ngugi"}]
        rows = {r["Lead_ID"]: r for r in sc.apply_history(self.leads, prev, over, CFG, TODAY)}
        self.assertEqual((rows["J-j1"]["Status"], rows["J-j1"]["Notes"], rows["J-j1"]["Owner"]),
                         ("Contacted", "called Tue", "Ngugi"))
        self.assertEqual((rows["J-j1"]["Hot_Since"], rows["J-j1"]["First_Seen"]), (d(3), d(3)))
        self.assertEqual((rows["J-j2"]["Status"], rows["J-j2"]["Notes"]), ("Meeting booked", ""))  # blank from Sheet wins

    def test_defaults_and_untouched_hot_alert(self):
        rows = {r["Lead_ID"]: r for r in sc.apply_history(self.leads, [], [], CFG, TODAY)}
        self.assertEqual((rows["J-j1"]["Status"], rows["J-j1"]["Owner"]), ("New", CFG["queue"]["default_owner"]))
        prev = [{"Lead_ID": "J-j1", "Status": "New", "Temperature": "Hot", "Hot_Since": d(8)}]
        rows = {r["Lead_ID"]: r for r in sc.apply_history(self.leads, prev, [], CFG, TODAY)}
        self.assertIn("untouched 7+ days", rows["J-j1"]["Flags"])

    def test_won_lost_frozen_and_expired_lifecycle(self):
        prev = [{"Lead_ID": "J-j1", "Status": "Won", "Score": "65", "Company": "Karen Hospital Ltd", "Company_ID": "c1",
                 "Temperature": "Hot"},
                {"Lead_ID": "J-old1", "Status": "Contacted", "Company": "Twiga Foods Ltd", "Last_Seen": d(5),
                 "Score": "50", "Temperature": "Warm"},
                {"Lead_ID": "J-old2", "Status": "New", "Company": "Twiga Foods Ltd", "Last_Seen": d(5)},
                {"Lead_ID": "J-old3", "Status": "New", "Notes": "x", "Company": "Twiga Foods Ltd", "Last_Seen": d(400)}]
        rows = {r["Lead_ID"]: r for r in sc.apply_history(self.leads, prev, [], CFG, TODAY)}
        self.assertEqual((rows["J-j1"]["Temperature"], rows["J-j1"]["Status"]), ("Closed", "Won"))
        self.assertEqual(rows["J-old1"]["Temperature"], "Expired")      # touched: kept for the notes
        self.assertNotIn("J-old2", rows)                                # untouched: dropped
        self.assertNotIn("J-old3", rows)                                # touched but past the keep window
        order = [r["Temperature"] for r in sc.apply_history(self.leads, prev, [], CFG, TODAY)]
        self.assertEqual(order, sorted(order, key=lambda t: sc.SORT[t]))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data, self.pdir = self.tmp / "data", self.tmp / "private"
        write_csv(self.data / "kenya_companies_master.csv", MASTER, MASTER_FIELDS)
        jobs = [job("j1", "Karen Hospital Ltd", posted=2), job("j2", "Twiga Foods Ltd", posted=20),
                job("j3", "State Hospital")]
        write_csv(self.data / "active_jobs.csv", jobs, list(jobs[0]))
        cs = [contact("k1", "c1", company="Karen Hospital Ltd")]
        write_csv(self.pdir / "hr_contacts.csv", cs, list(cs[0]))
        sigs = [signal("s1", "c2", "Twiga Foods Ltd", sector="retail_fmcg")]
        write_csv(self.pdir / "hiring_signals.csv", sigs, list(sigs[0]))

    def test_end_to_end_private_queue_public_aggregates(self):
        out = sc.run(today=TODAY, data_dir=self.data, pdir=self.pdir)
        self.assertEqual(out["leads"], 3)
        self.assertEqual(out["excluded"], {"excluded_public": 1})
        q = read_csv(self.pdir / "priority_queue.csv")
        self.assertEqual(list(q[0])[:14], sc.QUEUE_FIELDS[:14])        # agreed column order
        self.assertEqual(q[0]["Lead_ID"], "J-j1")                        # Hot first
        for r in q:
            self.assertTrue(r["Source_URL"])
            self.assertAlmostEqual(sum(components(r["Why_This_Score"])), float(r["Score"]))
        summ = read_csv(self.data / "leads_summary.csv")
        self.assertEqual(sum(int(r["count"]) for r in summ), 3)
        self.assertEqual(set(summ[0]), {"sector", "temperature", "count"})
        # the PUBLIC folder must hold no lead names, angles, contacts or ranking text
        public = "".join(p.read_text(encoding="utf-8") for p in self.data.glob("*.csv"))
        public_new = (self.data / "leads_summary.csv").read_text(encoding="utf-8") + \
            "".join(r["detail"] for r in read_csv(self.data / "scrape_log.csv"))
        for needle in ("Karen Hospital", "Twiga", "pitch", "Jane Wanjiru", "jane@karen.co.ke", "Why", "J-j1"):
            self.assertNotIn(needle, public_new)
        for needle in ("Jane Wanjiru", "jane@karen.co.ke", "pitch", "J-j1"):
            self.assertNotIn(needle, public)

        # second run: human edits survive, nothing duplicated
        rows = read_csv(self.pdir / "priority_queue.csv")
        rows[0]["Status"], rows[0]["Notes"] = "Contacted", "spoke to HR"
        write_csv(self.pdir / "priority_queue.csv", rows, sc.QUEUE_FIELDS)
        sc.run(today=TODAY + dt.timedelta(days=1), data_dir=self.data, pdir=self.pdir)
        q2 = {r["Lead_ID"]: r for r in read_csv(self.pdir / "priority_queue.csv")}
        self.assertEqual(len(q2), 3)
        self.assertEqual((q2["J-j1"]["Status"], q2["J-j1"]["Notes"]), ("Contacted", "spoke to HR"))

    def test_suppressed_email_is_not_a_contact_route(self):
        write_csv(self.pdir / "suppression.csv", [{"type": "email", "value": "JANE@karen.co.ke"}], ["type", "value"])
        sc.run(today=TODAY, data_dir=self.data, pdir=self.pdir)
        q = {r["Lead_ID"]: r for r in read_csv(self.pdir / "priority_queue.csv")}
        self.assertIn("Reach 0/20", q["J-j1"]["Why_This_Score"])
        self.assertEqual(q["J-j1"]["Temperature"], "Warm")

    def test_empty_inputs(self):
        empty = self.tmp / "empty"
        out = sc.run(today=TODAY, data_dir=empty / "d", pdir=empty / "p")
        self.assertEqual(out["leads"], 0)
        self.assertEqual(read_csv(empty / "p" / "priority_queue.csv"), [])


class RunAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data, self.pdir = self.tmp / "data", self.tmp / "private"
        self.now = lambda: dt.datetime(2026, 10, 23, 2, 5, tzinfo=dt.timezone.utc)

    def test_failure_is_isolated_and_reported(self):
        calls = []

        def ok(name):
            def f(dry, today):
                calls.append(name)
                return {"rows": 1}
            return f

        def boom(dry, today):
            calls.append("e2")
            raise RuntimeError("secret detail jane@karen.co.ke")

        steps = [("e1", ok("e1")), ("e2", boom), ("e3", ok("e3")), ("scoring", ok("scoring"))]
        with contextlib.redirect_stderr(io.StringIO()):                 # expected traceback
            meta = run_all.run(today=TODAY, data_dir=self.data, pdir=self.pdir, steps=steps, now=self.now, docs_dir=self.tmp / "docs")
        self.assertEqual(calls, ["e1", "e2", "e3", "scoring"])          # later steps still ran
        self.assertEqual((meta["ok"], meta["failed_steps"]), (False, ["e2"]))
        pub = (self.data / "meta.json").read_text(encoding="utf-8")
        priv = json.loads((self.pdir / "meta.json").read_text(encoding="utf-8"))
        self.assertNotIn("jane@karen.co.ke", pub)                       # public copy: exception type only
        self.assertIn("jane@karen.co.ke", priv["steps"]["e2"]["_detail"])
        self.assertEqual(json.loads(pub)["last_refreshed_utc"], "2026-10-23T02:05:00+00:00")
        log = read_csv(self.data / "scrape_log.csv")
        self.assertEqual([(r["engine"], r["status"], r["detail"]) for r in log], [("e2", "error", "RuntimeError")])

    def test_only_and_dry_run(self):
        steps = [("e1", lambda dry, today: {"dry": dry}), ("e2", lambda dry, today: {})]
        meta = run_all.run(dry_run=True, today=TODAY, data_dir=self.data, pdir=self.pdir, steps=steps, only={"e1"})
        self.assertEqual(meta["steps"]["e1"]["summary"], {"dry": True})
        self.assertEqual(meta["steps"]["e2"]["status"], "not_selected")
        self.assertFalse((self.data / "meta.json").exists())             # dry run writes nothing

    def test_trims_logs(self):
        write_csv(self.data / "changes.csv", [{"date": d(100), "summary": "old"}, {"date": d(10), "summary": "new"},
                                              {"date": "", "summary": "undated"}], ["date", "summary"])
        write_csv(self.data / "scrape_log.csv", [{"run_date": d(61)}, {"run_date": d(1)}], ["run_date"])
        meta = run_all.run(today=TODAY, data_dir=self.data, pdir=self.pdir, steps=[], now=self.now, docs_dir=self.tmp / "docs")
        self.assertEqual(meta["trimmed_rows"], {"changes": 1, "scrape_log": 1})
        self.assertEqual([r["summary"] for r in read_csv(self.data / "changes.csv")], ["new", "undated"])

    def test_whole_pipeline_with_shipped_config_runs_offline(self):
        # every source is disabled or manual by default, so nothing touches the network
        meta = run_all.run(today=TODAY, data_dir=self.data, pdir=self.pdir, now=self.now, docs_dir=self.tmp / "docs")
        self.assertTrue(meta["ok"], meta)
        self.assertEqual(list(meta["steps"]), ["engine1_jobs", "engine2_companies", "engine3_contacts",
                                               "engine4_signals", "scoring", "dashboard"])
        self.assertTrue((self.tmp / "docs" / "data" / "dashboard.json").exists())
        self.assertTrue((self.pdir / "priority_queue.csv").exists())
        self.assertTrue((self.data / "leads_summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
