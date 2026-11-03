"""Offline tests for Engine 1. Run:  python -m unittest discover -s tests -v
No network is used: a FakeSession stands in for requests."""
import datetime as dt
import json
import tempfile
import unittest

import requests

from scraper import engine1_jobs as e1
from scraper.common.classify import Classifier
from scraper.common.config import gate, load_yaml
from scraper.common.http import PoliteClient, RobotsDisallowed, SourceBlocked
from scraper.common.textutil import (make_job_id, normalize_company, normalize_location,
                                     out_of_scope, parse_date)

TODAY = dt.date(2026, 10, 22)
SETTINGS = {"user_agent": "BridgeLeadRadar/1.0 (+x; contact: a@b.co)", "min_seconds_between_requests": 2,
            "max_requests_per_source_per_run": 50, "cache_ttl_hours": 20,
            "request_timeout_seconds": 5, "retry_count": 1, "stop_source_after_consecutive_errors": 3}


class Resp:
    def __init__(self, status=200, text=""):
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def _resp(self, url):
        r = self.routes.get(url)
        if isinstance(r, Exception):
            raise r
        return r or Resp(404, "")

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return self._resp(url)

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append((method, url))
        return self._resp(url)


def make_client(routes, tmp, sleeps=None):
    sl = sleeps if sleeps is not None else []
    return PoliteClient(SETTINGS, tmp, session=FakeSession(routes), sleep=sl.append,
                        clock=lambda: 0.0, wall=lambda: 0.0), sl


JSONLD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Registered Nurse - URGENT",
 "hiringOrganization":{"@type":"Organization","name":"Karen Hospital Ltd"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Nairobi","addressCountry":"KE"}},
 "datePosted":"2026-10-20","validThrough":"2026-12-01","employmentType":"FULL_TIME",
 "description":"<p>Join our ICU team at the hospital.</p>"}
</script></head><body><a href="/careers/job-2">Job 2</a></body></html>"""


class GateTests(unittest.TestCase):
    def test_gate_cases(self):
        s = {"user_agent": "ok"}
        base = {"enabled": True, "tos_reviewed_on": "2026-01-01", "tos_reviewed_by": "me", "access": "api"}
        self.assertEqual(gate(base, s, {}), (True, "ok"))
        self.assertEqual(gate({**base, "access": "manual"}, s, {})[1], "manual_required")
        self.assertEqual(gate({**base, "status": "manual_required"}, s, {})[1], "manual_required")
        self.assertEqual(gate({**base, "enabled": False}, s, {})[1], "disabled")
        self.assertEqual(gate({**base, "tos_reviewed_on": ""}, s, {})[1], "tos_not_reviewed")
        self.assertEqual(gate({**base, "secret": "KEY"}, s, {})[1], "missing_secret:KEY")
        self.assertEqual(gate({**base, "secret": "KEY"}, s, {"KEY": "x"})[0], True)
        self.assertEqual(gate(base, {"user_agent": "x PLACEHOLDER"}, {})[1], "user_agent_placeholder")

    def test_shipped_default_runnable_sources(self):
        cfg = load_yaml("sources.yaml")
        runnable = [s["id"] for s in cfg["engine1_jobs"] if gate(s, cfg["settings"], {})[0]]
        self.assertEqual(runnable, ["remotive_api", "arbeitnow_api", "remoteok_api", "company_ats"])


class HttpTests(unittest.TestCase):
    def test_robots_disallow_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = make_client({"https://x.co/robots.txt": Resp(200, "User-agent: *\nDisallow: /jobs"),
                                "https://x.co/jobs": Resp(200, "hi")}, tmp)
            with self.assertRaises(RobotsDisallowed):
                c.get("https://x.co/jobs")

    def test_robots_unreadable_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = make_client({"https://x.co/robots.txt": Resp(403, ""), "https://x.co/a": Resp(200, "hi")}, tmp)
            with self.assertRaises(RobotsDisallowed):
                c.get("https://x.co/a")
            c2, _ = make_client({"https://y.co/robots.txt": requests.ConnectionError()}, tmp)
            with self.assertRaises(RobotsDisallowed):
                c2.get("https://y.co/a")

    def test_missing_robots_allows_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = make_client({"https://x.co/a": Resp(200, "hello")}, tmp)
            self.assertEqual(c.get("https://x.co/a"), "hello")
            n = len(c.session.calls)
            self.assertEqual(c.get("https://x.co/a"), "hello")
            self.assertEqual(len(c.session.calls), n)          # served from cache
            self.assertEqual(c.stats["cache_hits"], 1)

    def test_rate_limit_sleeps_between_same_domain_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, sl = make_client({"https://x.co/a": Resp(200, "1"), "https://x.co/b": Resp(200, "2")}, tmp)
            c.get("https://x.co/a")
            c.get("https://x.co/b")
            self.assertTrue(any(abs(s - 2.0) < 1e-6 for s in sl))

    def test_429_raises_blocked_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = make_client({"https://x.co/a": Resp(429, "")}, tmp)
            with self.assertRaises(SourceBlocked):
                c.get("https://x.co/a")
            self.assertEqual(sum(1 for m, u in c.session.calls if u == "https://x.co/a"), 1)

    def test_repeated_403_blocks_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            routes = {f"https://x.co/{i}": Resp(403, "") for i in range(5)}
            c, _ = make_client(routes, tmp)
            c.get("https://x.co/0")
            c.get("https://x.co/1")
            with self.assertRaises(SourceBlocked):
                c.get("https://x.co/2")

    def test_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = make_client({"https://x.co/a": Resp(200, "1")}, tmp)
            c.begin_source(budget=1)
            c.get("https://x.co/a")
            with self.assertRaises(SourceBlocked):
                c.get("https://x.co/b")


class TextTests(unittest.TestCase):
    def test_normalisation_and_ids(self):
        self.assertEqual(normalize_company("Karen Hospital (K) Ltd."), "karen hospital")
        self.assertEqual(normalize_company("M&M Foods Limited"), "m and m foods")
        self.assertEqual(normalize_location("Nairobi, Nairobi County, Kenya"), "nairobi")
        self.assertEqual(make_job_id("Karen Hospital Ltd", "Registered Nurse", "Nairobi, Kenya"),
                         make_job_id("KAREN HOSPITAL LIMITED", "registered  nurse", "Nairobi"))

    def test_dates_and_scope(self):
        self.assertEqual(parse_date("2026-10-20", TODAY), dt.date(2026, 10, 20))
        self.assertEqual(parse_date("20/10/2026", TODAY), dt.date(2026, 10, 20))
        self.assertEqual(parse_date("2026-12-01", TODAY), dt.date(2026, 12, 1))   # not 12 Jan
        self.assertEqual(parse_date("2026-12-01T00:00:00Z", TODAY), dt.date(2026, 12, 1))
        self.assertIsNone(parse_date("2026-13-45", TODAY))
        self.assertIsNone(parse_date("garbage", TODAY))
        self.assertIsNone(parse_date("", TODAY))
        self.assertTrue(out_of_scope("Kampala, Uganda"))
        self.assertFalse(out_of_scope("Nairobi, Kenya"))
        self.assertFalse(out_of_scope("Nairobi"))
        self.assertFalse(out_of_scope("Kenya / Uganda"))


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clf = Classifier(load_yaml("sector_map.yaml"))

    def test_sector(self):
        self.assertEqual(self.clf.sector("Registered Nurse", "Karen Hospital Ltd")[0], "healthcare")
        self.assertEqual(self.clf.sector("Software Developer", "PayFlow Fintech")[0], "tech_ecommerce")
        s, v, conf = self.clf.sector("Assistant", "Acme Holdings")
        self.assertEqual((s, v, conf), ("other", "Other", "low"))

    def test_seniority_roles_urgency(self):
        self.assertEqual(self.clf.seniority("Head of Finance"), "executive")
        self.assertEqual(self.clf.seniority("Assistant Manager"), "junior")
        self.assertEqual(self.clf.seniority("Accountant"), "unspecified")
        self.assertEqual(self.clf.role_family("HR Manager"), "hr")
        self.assertTrue(self.clf.is_urgent("URGENT: nurse needed"))


class AdapterTests(unittest.TestCase):
    def test_jsonld_extraction(self):
        raws = e1.extract_jobposting_jsonld(JSONLD, "https://k.co/careers")
        self.assertEqual(len(raws), 1)
        r = raws[0]
        self.assertEqual((r["title"], r["company"], r["location"]),
                         ("Registered Nurse - URGENT", "Karen Hospital Ltd", "Nairobi, KE"))

    def test_malformed_jsonld_ignored(self):
        self.assertEqual(e1.extract_jobposting_jsonld('<script type="application/ld+json">{bad</script>', "u"), [])

    def test_careers_scan_follows_job_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, _ = make_client({"https://k.co/careers": Resp(200, JSONLD),
                                "https://k.co/careers/job-2": Resp(200, JSONLD.replace("Registered Nurse - URGENT", "Lab Technician"))}, tmp)
            raws = e1.scan_careers_page(c, {"company": "Karen Hospital", "careers_url": "https://k.co/careers"})
            self.assertEqual({r["title"] for r in raws}, {"Registered Nurse - URGENT", "Lab Technician"})

    def test_greenhouse_and_lever(self):
        t = {"company": "Acme"}
        gh = e1.parse_greenhouse(json.dumps({"jobs": [{"title": "Dev", "location": {"name": "Nairobi"},
                                                       "absolute_url": "u", "updated_at": "2026-10-01"}]}), t)
        self.assertEqual(gh[0]["posted_date"], None)          # never use updated_at as posted date
        lv = e1.parse_lever(json.dumps([{"text": "Dev", "categories": {"location": "Nairobi"},
                                          "createdAt": 1760000000000, "hostedUrl": "u"}]), t)
        self.assertEqual(lv[0]["title"], "Dev")
        self.assertEqual(e1.parse_greenhouse("not json", t), [])
        wk = e1.parse_workable(json.dumps({"jobs": [{"title": "Accountant", "location": {"city": "Nairobi", "country": "Kenya"},
                                                     "published_on": "2026-11-03", "application_url": "https://example.test/jobs/1"}]}), t)
        self.assertEqual((wk[0]["title"], wk[0]["location"], wk[0]["posted_date"]),
                         ("Accountant", "Nairobi, Kenya", "2026-11-03"))

    def test_global_remote_adapters(self):
        remotive = e1.parse_remotive(json.dumps({"jobs": [{
            "title": "Python Developer - Work from Kenya",
            "company_name": "Global Acme",
            "candidate_required_location": "Worldwide",
            "publication_date": "2026-11-03T12:00:00",
            "url": "https://example.test/remotive/1",
        }]}))
        arbeitnow = e1.parse_arbeitnow(json.dumps({"data": [{
            "title": "Remote Data Analyst", "company_name": "Example Co",
            "location": "Remote", "created_at": "2026-11-03", "url": "https://example.test/arbeitnow/1",
        }]}))
        remoteok = e1.parse_remoteok(json.dumps([{
            "position": "Remote Engineer", "company": "Example Inc",
            "location": "Worldwide", "date": "2026-11-03", "url": "https://example.test/remoteok/1",
        }]))
        self.assertEqual(remotive[0]["location"], "Worldwide")
        self.assertEqual(arbeitnow[0]["title"], "Remote Data Analyst")
        self.assertEqual(remoteok[0]["company"], "Example Inc")

    def test_additional_public_ats_adapters(self):
        target = {"company": "Acme"}
        smart = e1.parse_smartrecruiters(json.dumps({"content": [{
            "name": "Data Analyst", "location": {"city": "Nairobi", "country": "Kenya"},
            "releasedDate": "2026-11-03", "ref": "https://example.test/job/1",
        }]}), target)
        ashby = e1.parse_ashby(json.dumps({"jobs": [{
            "title": "Finance Officer", "location": {"name": "Nairobi, Kenya"},
            "publishedAt": "2026-11-03", "jobUrl": "https://example.test/job/2",
        }]}), target)
        recruitee = e1.parse_recruitee(json.dumps({"offers": [{
            "title": "HR Manager", "location": "Nairobi",
            "created_at": "2026-11-03", "careers_url": "https://example.test/job/3",
        }]}), target)
        self.assertEqual((smart[0]["title"], smart[0]["location"]), ("Data Analyst", "Nairobi, Kenya"))
        self.assertEqual((ashby[0]["title"], ashby[0]["location"]), ("Finance Officer", "Nairobi, Kenya"))
        self.assertEqual((recruitee[0]["title"], recruitee[0]["location"]), ("HR Manager", "Nairobi"))


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clf = Classifier(load_yaml("sector_map.yaml"))

    def cand(self, raw, src="s1", today=TODAY):
        return e1.to_candidate(raw, src, self.clf, set(), today)

    def test_filters(self):
        base = {"title": "Nurse", "company": "Karen Hospital", "location": "Nairobi"}
        self.assertEqual(self.cand({**base, "expiry_date": "2026-10-01"})[1], "expired")
        self.assertEqual(self.cand({**base, "location": "Kampala, Uganda"})[1], "out_of_scope")
        self.assertEqual(self.cand({**base, "title": ""})[1], "missing_title_or_company")
        c, _ = self.cand({**base, "company": "Confidential"})
        self.assertEqual(c["posted_by"], "confidential")
        c, _ = self.cand({**base, "company": "Prime Staffing Solutions"})
        self.assertEqual(c["posted_by"], "agency")
        c, _ = self.cand(base)
        self.assertEqual(c["posted_by"], "unknown")
        self.assertEqual(c["posted_date"], "")

    def test_global_remote_is_classified_not_out_of_scope(self):
        raw = {
            "title": "Remote Software Engineer",
            "company": "Global Acme",
            "location": "United States (Remote)",
            "text": "Work from Kenya or Africa for a global distributed team",
        }
        cand, reason = self.cand(raw, src="remotive_api")
        self.assertEqual(reason, "ok")
        self.assertEqual(cand["work_arrangement"], "remote")
        self.assertEqual(cand["talent_market"], "Kenya / Africa eligible")

    def test_dedupe_keeps_newest_and_counts_sources(self):
        a = self.cand({"title": "Nurse", "company": "Karen Hospital Ltd", "location": "Nairobi", "posted_date": "2026-10-10"}, "A")[0]
        b = self.cand({"title": "NURSE", "company": "Karen Hospital Limited", "location": "Nairobi, Kenya", "posted_date": "2026-10-18"}, "B")[0]
        rows, _, _ = e1.merge([a, b], [], {"A", "B"}, TODAY, baseline=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source_id"], rows[0]["posted_date"], rows[0]["source_count"]), ("B", "2026-10-18", 2))

    def test_lifecycle(self):
        raw = {"title": "Nurse", "company": "Karen Hospital", "location": "Nairobi", "posted_date": "2026-10-20"}
        c = self.cand(raw)[0]
        # day 1 baseline
        rows, closed, ch = e1.merge([c], [], {"s1"}, TODAY, baseline=True)
        self.assertEqual((len(rows), len(ch), rows[0]["first_seen_baseline"]), (1, 0, "1"))
        # day 2 new job (different one) -> change logged
        c2 = self.cand({**raw, "title": "Doctor"})[0]
        d2 = TODAY + dt.timedelta(days=1)
        rows, closed, ch = e1.merge([c, c2], rows, {"s1"}, d2, baseline=False)
        self.assertEqual([x["change_type"] for x in ch], ["new_job"])
        # repost: newer posted_date
        c_re = self.cand({**raw, "posted_date": "2026-10-25"})[0]
        rows, closed, ch = e1.merge([c_re, c2], rows, {"s1"}, d2 + dt.timedelta(days=1), baseline=False)
        self.assertEqual({r["title"]: r["repost_count"] for r in rows}["Nurse"], 1)
        # disappears: closes after 3 clean runs of its source
        day = d2 + dt.timedelta(days=1)
        for i in range(3):
            day += dt.timedelta(days=1)
            rows, closed, ch = e1.merge([c2], rows, {"s1"}, day, baseline=False)
        self.assertEqual([r["title"] for r in closed], ["Nurse"])
        self.assertIn("job_closed", [x["change_type"] for x in ch])

    def test_source_that_did_not_run_does_not_close_jobs(self):
        c = self.cand({"title": "Nurse", "company": "Karen Hospital", "location": "Nairobi"}, "s1")[0]
        rows, _, _ = e1.merge([c], [], {"s1"}, TODAY, baseline=True)
        for i in range(1, 6):
            rows, closed, _ = e1.merge([], rows, set(), TODAY + dt.timedelta(days=i), baseline=False)
        self.assertEqual((len(rows), len(closed)), (1, 0))

    def test_reopened_job_increments_repost(self):
        c = self.cand({"title": "Nurse", "company": "Karen Hospital", "location": "Nairobi"})[0]
        closed_prev = {**c, "first_seen": "2026-10-01", "status": "closed", "closed_date": "2026-10-15",
                       "repost_count": "0", "missing_runs": "3", "first_seen_baseline": ""}
        rows, _, ch = e1.merge([c], [closed_prev], {"s1"}, TODAY, baseline=False)
        self.assertEqual((rows[0]["repost_count"], rows[0]["first_seen"]), (1, "2026-10-01"))
        self.assertEqual(ch[0]["change_type"], "job_reopened")


class RunnerTests(unittest.TestCase):
    def test_end_to_end_with_temp_config(self):
        import pathlib, shutil, yaml
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            cfg = tmp / "config"
            shutil.copytree(e1.CONFIG_DIR, cfg)
            sources = yaml.safe_load((cfg / "sources.yaml").read_text(encoding="utf-8"))
            for s in sources["engine1_jobs"]:
                if s["id"] == "company_ats":
                    s.update(enabled=True, status="approved", tos_reviewed_by="tester", tos_reviewed_on="2026-10-22")
            (cfg / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
            orig = e1.load_career_targets
            e1.load_career_targets = lambda: [{"company": "Karen Hospital", "careers_url": "https://k.co/careers",
                                               "ats_type": "", "ats_token": ""},
                                              {"company": "Blocked Co", "careers_url": "https://b.co/careers",
                                               "ats_type": "", "ats_token": ""}]
            try:
                client, _ = make_client({"https://k.co/careers": Resp(200, JSONLD),
                                         "https://b.co/robots.txt": Resp(200, "User-agent: *\nDisallow: /")}, tmp / "cache")
                out = e1.run(today=TODAY, client=client, data_dir=tmp / "data", config_dir=cfg)
            finally:
                e1.load_career_targets = orig
            self.assertEqual((out["open"], out["baseline"]), (1, True))
            rows = e1.read_csv(tmp / "data" / "active_jobs.csv")
            self.assertEqual(rows[0]["vertical"], "Healthcare")
            self.assertEqual(rows[0]["urgency_flag"], "1")
            manual = {m["source_id"] for m in e1.read_csv(tmp / "data" / "manual_integration_required.csv")}
            self.assertIn("brightermonday", manual)
            self.assertTrue(any(m.startswith("company_ats:b.co") for m in manual))
            log = e1.read_csv(tmp / "data" / "scrape_log.csv")
            self.assertTrue(any(l["status"] == "baseline_run" for l in log))


if __name__ == "__main__":
    unittest.main()
