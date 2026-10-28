"""Offline tests for the public dashboard data (docs/data/dashboard.json)."""
import datetime as dt
import json
import pathlib
import re
import tempfile
import unittest

from scraper import dashboard as db
from scraper import scoring as sc
from scraper.common.config import ROOT
from scraper.common.store import write_csv
from tests.test_scoring import MASTER, MASTER_FIELDS, TODAY, contact, d, job, signal

JOB_FIELDS = ["job_id", "company", "title", "posted_date", "first_seen", "first_seen_baseline", "role_family",
              "urgency_flag", "repost_count", "days_open", "posted_by", "status", "sector", "source_url"]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data, self.pdir = self.tmp / "data", self.tmp / "private"
        master = [dict(m, contact_status=("found" if m["company_id"] == "c1" else ""), hq_location="Nairobi")
                  for m in MASTER]
        write_csv(self.data / "kenya_companies_master.csv", master, MASTER_FIELDS + ["contact_status", "hq_location"])
        jobs = [job("j1", "Karen Hospital Ltd", posted=1, first_seen=TODAY.isoformat(), sector="healthcare"),
                job("j2", "Karen Hospital Ltd", posted=3, first_seen=d(3), sector="healthcare"),
                job("j3", "Twiga Foods Ltd", posted=5, first_seen=d(5), sector="retail_fmcg"),
                job("j4", "Some Staffing", posted_by="agency", first_seen=TODAY.isoformat(), sector="logistics"),
                job("j5", "Old Baseline Co", posted=None, first_seen=d(40), baseline="1", sector="")]
        write_csv(self.data / "active_jobs.csv", jobs, JOB_FIELDS)
        cs = [contact("k1", "c1", company="Karen Hospital Ltd")]
        write_csv(self.pdir / "hr_contacts.csv", cs, list(cs[0]))
        sigs = [signal("s1", "c2", "Twiga Foods Ltd", sector="retail_fmcg")]
        write_csv(self.pdir / "hiring_signals.csv", sigs, list(sigs[0]))
        write_csv(self.data / "signals_summary.csv", [{"signal_type": "expansion_announced", "sector": "retail_fmcg",
                                                       "probability": "High", "count": 1}],
                  ["signal_type", "sector", "probability", "count"])
        write_csv(self.data / "scrape_log.csv", [
            {"run_date": d(1), "engine": "engine1_jobs", "source_id": "fuzu", "status": "skipped:disabled"},
            {"run_date": TODAY.isoformat(), "engine": "engine1_jobs", "source_id": "fuzu", "status": "ok"},
            {"run_date": TODAY.isoformat(), "engine": "engine1_jobs", "source_id": "brightermonday", "status": "skipped:manual_required"},
            {"run_date": TODAY.isoformat(), "engine": "scoring", "source_id": "_run", "status": "ok"}],
            ["run_date", "engine", "source_id", "status", "items", "detail"])
        sc.run(today=TODAY, data_dir=self.data, pdir=self.pdir)         # writes public leads_summary.csv

    def payload(self, **kw):
        return db.build(self.data, TODAY, kw.get("meta", {"ok": True, "failed_steps": [], "last_refreshed_utc": "x"}))

    def test_kpis_and_charts(self):
        p = self.payload()
        k = p["kpis"]
        self.assertEqual((k["active_jobs"], k["new_today"], k["companies_hiring"]), (5, 2, 3))
        self.assertEqual((k["companies_tracked"], k["companies_with_contact"], k["live_signals"]), (7, 1, 1))
        self.assertEqual(p["top_hiring"][0], {"label": "Karen Hospital Ltd", "sector": "Healthcare", "count": 2})
        self.assertNotIn("Some Staffing", [r["label"] for r in p["top_hiring"]])       # agency postings excluded
        sectors = {r["label"]: r for r in p["jobs_by_sector"]}
        self.assertEqual((sectors["Healthcare"]["count"], sectors["Healthcare"]["target"]), (2, True))
        self.assertIn("Other / unclassified", sectors)
        self.assertEqual(len(p["new_jobs_trend"]), 30)
        self.assertEqual(p["new_jobs_trend"][-1], {"date": TODAY.isoformat(), "count": 2})
        self.assertEqual(sum(p["leads_by_temperature"].values()), k["leads"])
        self.assertGreater(k["leads"], 0)
        self.assertEqual(p["signals_by_type"], [{"label": "Expansion announced", "count": 1}])
        src = {s["source"]: s["status"] for s in p["sources"]}
        self.assertEqual(src, {"fuzu": "ok", "brightermonday": "manual only"})       # latest run wins; _run hidden
        self.assertEqual(p["baseline"], {"active": False, "since": d(40)})

    def test_company_table_shows_contact_status_only(self):
        p = self.payload()
        rows = {c["name"]: c for c in p["companies"]}
        self.assertEqual(rows["Karen Hospital Ltd"]["contact"], "Found")
        self.assertEqual(rows["Twiga Foods Ltd"]["contact"], "Not checked")
        self.assertEqual(rows["Karen Hospital Ltd"]["open_jobs"], 2)
        self.assertEqual(rows["State Hospital"]["fit"], "Public sector (excluded)")
        for c in p["companies"]:
            self.assertEqual(list(c), db.COMPANY_KEYS)
        self.assertEqual(p["companies"][0]["name"], "Karen Hospital Ltd")            # most postings first

    def test_no_private_data_in_public_json(self):
        out = db.write(self.payload(), self.tmp / "docs")
        text = out.read_text(encoding="utf-8")
        # nothing from the private folder: contacts, lead IDs, scores, angles, explanations
        for needle in ("Jane Wanjiru", "jane@karen.co.ke", "HR Manager", "J-j1", "S-s1", "Why", "Score",
                       "pitch", "Suggested", "Lead_ID", "Hot_Since", "Primary_Lead", "https://jobs/", "https://news/"):
            self.assertNotIn(needle, text, needle)
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "an email address leaked")
        self.assertIsNone(re.search(r"\+?254\s?\d{3}", text), "a phone number leaked")
        p = json.loads(text)
        allowed = {"generated_utc", "run_date", "last_refreshed_utc", "pipeline_ok", "failed_steps", "baseline", "kpis",
                   "jobs_by_sector", "top_hiring", "new_jobs_trend", "leads_by_temperature", "leads_by_sector",
                   "signals_by_type", "sources", "companies", "label", "steps_ok"}
        self.assertEqual(set(p), allowed)                     # a new key must be reviewed and added here on purpose

    def test_baseline_banner_and_failed_steps(self):
        write_csv(self.data / "active_jobs.csv", [job("j1", "Karen Hospital Ltd", posted=None, first_seen=d(3),
                                                       baseline="1", sector="healthcare")], JOB_FIELDS)
        p = self.payload(meta={"ok": False, "failed_steps": ["engine4_signals"]})
        self.assertEqual(p["baseline"], {"active": True, "since": d(3)})
        self.assertEqual((p["kpis"]["new_today"], p["pipeline_ok"], p["failed_steps"]), (0, False, ["engine4_signals"]))
        self.assertTrue(all(r["count"] == 0 for r in p["new_jobs_trend"]))

    def test_empty_data_folder(self):
        p = db.build(self.tmp / "nothing", TODAY, {})
        self.assertEqual(p["kpis"]["active_jobs"], 0)
        self.assertEqual(p["companies"], [])

    def test_page_reads_only_public_json_and_stays_self_contained(self):
        html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
        self.assertIn('fetch("data/dashboard.json"', html)
        self.assertNotRegex(html, r"<script[^>]+src=")                 # no external scripts
        self.assertNotIn("innerHTML", html)                            # data goes in via textContent only
        self.assertIn('prefers-color-scheme: dark', html)
        self.assertIn(':root[data-theme="dark"]', html)
        self.assertTrue((ROOT / "docs" / ".nojekyll").exists())


if __name__ == "__main__":
    unittest.main()
