"""Offline tests for Engine 3.  Run:  python -m unittest discover -s tests -t . -v"""
import datetime as dt
import pathlib
import shutil
import tempfile
import unittest

import yaml

from scraper import engine3_contacts as e3
from scraper.common.config import CONFIG_DIR, load_yaml
from scraper.common.contacts_extract import (Rules, classify_title, extract_emails, extract_landlines,
                                             extract_people, inbox_kind, is_name, pick_pages)
from scraper.common.store import read_csv, write_csv
from tests.test_engine1 import Resp, make_client

TODAY = dt.date(2026, 10, 22)
CFG = load_yaml("contacts.yaml")
R = Rules(CFG)

HOME = """<html><body><nav><a href="/contact-us">Contact</a><a href="/our-team">Our Team</a>
<a href="/blog/post">Blog</a><a href="https://other.com/team">Ext</a></nav></body></html>"""

TEAM = """<html><body>
<div class="member"><h3>Jane Wanjiru</h3><p>Head of Human Resources</p>
  <a href="mailto:jane.wanjiru@karen.co.ke">Email</a></div>
<div class="member"><h3>Peter Otieno</h3><p>Chief Financial Officer</p><a href="mailto:peter@gmail.com">x</a></div>
<div class="member"><h3>Mary Achieng - Operations Manager</h3></div>
<div class="member"><h3>Our Team</h3><p>Head of Human Resources</p></div>
</body></html>"""

CONTACT = """<html><body><p>Reach us: info@karen.co.ke, hr@karen.co.ke, careers@partner.com,
sales.guy@karen.co.ke, someone@gmail.com. Tel: +254 20 234 5678 or 0722 123 456 or 0733111222.</p>
<script>var a="x@karen.co.ke"</script></body></html>"""

ROUTES = {"https://karen.co.ke": Resp(200, HOME), "https://karen.co.ke/contact-us": Resp(200, CONTACT),
          "https://karen.co.ke/our-team": Resp(200, TEAM)}


class ExtractTests(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(classify_title("Head of Human Resources", R), "hr")
        self.assertEqual(classify_title("HR Manager", R), "hr")
        self.assertEqual(classify_title("Talent Acquisition Lead", R), "hr")
        self.assertEqual(classify_title("People & Culture Director", R), "hr")
        self.assertEqual(classify_title("Chief Financial Officer", R), "finance")
        self.assertEqual(classify_title("Plant Manager", R), "operations")
        self.assertEqual(classify_title("Managing Director", R), "executive")
        self.assertIsNone(classify_title("Sales Manager", R))
        self.assertIsNone(classify_title("x" * 120 + " HR Manager", R))

    def test_names(self):
        self.assertTrue(is_name("Jane Wanjiru", R))
        self.assertTrue(is_name("Dr. Peter Otieno", R))
        self.assertFalse(is_name("Our Team", R))
        self.assertFalse(is_name("Read More", R))
        self.assertFalse(is_name("Head of HR", R))
        self.assertFalse(is_name("info@karen.co.ke", R))

    def test_emails_domain_and_blocklist(self):
        ems = extract_emails(CONTACT, "karen.co.ke", R)
        self.assertIn("info@karen.co.ke", ems)
        self.assertNotIn("careers@partner.com", ems)       # other domain
        self.assertNotIn("someone@gmail.com", ems)         # personal-mail domain
        self.assertEqual(inbox_kind("hr@karen.co.ke", R), "hr")
        self.assertEqual(inbox_kind("info@karen.co.ke", R), "generic")
        self.assertIsNone(inbox_kind("sales.guy@karen.co.ke", R))

    def test_landlines_only(self):
        self.assertEqual(extract_landlines(CONTACT), ["+254202345678"])
        self.assertEqual(extract_landlines("Call 0722 123 456 or +254 733 111 222"), [])
        self.assertEqual(extract_landlines("Mombasa 041 222 3344"), ["+254412223344"])

    def test_people_blocks(self):
        ppl = {p["name"]: p for p in extract_people(TEAM, "karen.co.ke", R)}
        self.assertEqual(set(ppl), {"Jane Wanjiru", "Peter Otieno", "Mary Achieng"})
        self.assertEqual(ppl["Jane Wanjiru"]["role_category"], "hr")
        self.assertEqual(ppl["Jane Wanjiru"]["email"], "jane.wanjiru@karen.co.ke")   # same block
        self.assertEqual(ppl["Peter Otieno"]["email"], "")                            # gmail dropped
        self.assertEqual(ppl["Mary Achieng"]["role_category"], "operations")          # "Name - Title" line
        self.assertNotIn("Our Team", ppl)

    def test_ambiguous_block_skipped(self):
        html = "<div><p>Jane Wanjiru</p><p>Head of HR</p><p>Sam Kimani</p><p>Finance Director</p></div>"
        self.assertEqual(extract_people(html, "k.co", R), [])

    def test_pick_pages_same_domain_priority(self):
        pages = pick_pages(HOME, "https://karen.co.ke", R, 5)
        self.assertEqual(pages, ["https://karen.co.ke/contact-us", "https://karen.co.ke/our-team"])


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_crawl_found(self):
        c, _ = make_client(ROUTES, self.tmp / "c")
        res = e3.crawl_company(c, {"website": "https://karen.co.ke"}, R, CFG, check_mx=lambda d: "1")
        self.assertEqual(res["outcome"], "found")
        by = {(x["contact_type"], x["contact_name"] or x["email"] or x["phone"]): x for x in res["found"]}
        self.assertIn(("named_person", "Jane Wanjiru"), by)
        self.assertIn(("role_inbox", "hr@karen.co.ke"), by)
        self.assertIn(("switchboard", "+254202345678"), by)
        self.assertNotIn(("role_inbox", "sales.guy@karen.co.ke"), by)   # person-style address, no name -> dropped
        self.assertTrue(all(x["source_url"].startswith("https://karen.co.ke") for x in res["found"]))
        self.assertEqual(by[("role_inbox", "hr@karen.co.ke")]["mx_valid"], "1")

    def test_robots_and_blocked(self):
        c, _ = make_client({"https://b.co/robots.txt": Resp(200, "User-agent: *\nDisallow: /")}, self.tmp / "c")
        self.assertEqual(e3.crawl_company(c, {"website": "b.co"}, R, CFG)["outcome"], "robots_disallowed")
        c, _ = make_client({"https://c.co": Resp(429, "")}, self.tmp / "c2")
        self.assertEqual(e3.crawl_company(c, {"website": "https://c.co"}, R, CFG)["outcome"], "blocked")
        c, _ = make_client({}, self.tmp / "c3")
        self.assertEqual(e3.crawl_company(c, {"website": "https://d.co"}, R, CFG)["outcome"], "no_response")


def found(name, title, cat="hr", **kw):
    d = {"contact_type": "named_person", "contact_name": name, "contact_title": title, "role_category": cat,
         "email": "", "phone": "", "source_url": "https://k.co/team", "email_domain_matches_site": "", "mx_valid": ""}
    d.update(kw)
    return d


class MergeTests(unittest.TestCase):
    cos = {"c1": {"display_name": "Karen Hospital", "sector": "healthcare"}}
    nosup = {"email": set(), "domain": set(), "company_id": set(), "name": set()}

    def merge(self, prev, results, today=TODAY, sup=None):
        return e3.merge_contacts(prev, results, self.cos, today, CFG, sup or self.nosup)

    def test_new_contact_high_confidence_and_buys(self):
        rows, ch = self.merge([], {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}})
        self.assertEqual((rows[0]["confidence"], rows[0]["reconfirmed"], rows[0]["buys"]),
                         ("High", "yes",
                          "Recruitment; HR Consulting; HR Outsourcing; Payroll Outsourcing; "
                          "Staff Outsourcing (EOR); HRIS; Impact Projects"))
        self.assertEqual(ch, [])                           # first ever contact is not a "change"

    def test_new_hr_head_detected_and_old_marked_unconfirmed(self):
        rows, _ = self.merge([], {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}})
        later = TODAY + dt.timedelta(days=30)
        rows2, ch = self.merge(rows, {"c1": {"outcome": "found", "found": [found("Sam Kimani", "Head of HR")]}}, later)
        by = {r["contact_name"]: r for r in rows2}
        self.assertEqual(by["Sam Kimani"]["change_detected"], "new_hr_head")
        self.assertEqual((by["Jane Wanjiru"]["reconfirmed"], by["Jane Wanjiru"]["confidence"]), ("no", "Medium"))
        self.assertEqual(len(ch), 1)
        self.assertNotIn("Sam", str(ch))                   # public change log carries no names

    def test_stale_and_retention(self):
        rows, _ = self.merge([], {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}})
        r95, _ = self.merge(rows, {}, TODAY + dt.timedelta(days=95))
        self.assertEqual(r95[0]["confidence"], "Medium")
        r400, _ = self.merge(rows, {}, TODAY + dt.timedelta(days=400))
        self.assertEqual(r400, [])                         # purged after 12 months

    def test_blocked_crawl_keeps_old_data(self):
        rows, _ = self.merge([], {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}})
        rows2, _ = self.merge(rows, {"c1": {"outcome": "blocked", "found": []}}, TODAY + dt.timedelta(days=5))
        self.assertEqual((len(rows2), rows2[0]["reconfirmed"]), (1, "yes"))

    def test_suppression(self):
        sup = {"email": {"jane@k.co"}, "domain": set(), "company_id": set(), "name": {"sam kimani"}}
        res = {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager", email="jane@k.co"),
                                                     found("Sam Kimani", "Head of HR"),
                                                     found("Ann Mwangi", "HR Officer")]}}
        rows, _ = self.merge([], res, sup=sup)
        self.assertEqual([r["contact_name"] for r in rows], ["Ann Mwangi"])
        rows, _ = self.merge([], res, sup={**sup, "company_id": {"c1"}})
        self.assertEqual(rows, [])

    def test_opt_out_flag_removes_contact(self):
        rows, _ = self.merge([], {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}})
        rows[0]["opt_out"] = "yes"
        rows2, _ = self.merge(rows, {}, TODAY + dt.timedelta(days=1))
        self.assertEqual(rows2, [])

    def test_human_columns_preserved(self):
        rows, _ = self.merge([], {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}})
        rows[0]["notes"], rows[0]["bounce_flag"] = "met at expo", "no"
        rows2, _ = self.merge(rows, {"c1": {"outcome": "found", "found": [found("Jane Wanjiru", "HR Manager")]}},
                              TODAY + dt.timedelta(days=2))
        self.assertEqual((rows2[0]["notes"], rows2[0]["bounce_flag"]), ("met at expo", "no"))


class SelectTests(unittest.TestCase):
    def test_selection_rules(self):
        m = lambda cid, name, icp="unknown_size", site="https://x.co": {
            "company_id": cid, "display_name": name, "icp_status": icp, "website": site}
        master = [m("1", "Active Co"), m("2", "Quiet Co"), m("3", "Public Co", "excluded_public"),
                  m("4", "No Site", site=""), m("5", "Facebook Co", site="https://facebook.com/x"),
                  m("6", "Big Co", "large_outside_band"), m("7", "Done Co"), m("8", "Client Co")]
        state = {"7": {"next_crawl_date": "2026-12-01"}}
        cfg = {"settings": {**CFG["settings"], "crawl_budget_companies_per_run": 3, "eligible_icp": CFG["settings"]["eligible_icp"]}}
        norm = e3.normalize_company
        picked = e3.select_companies(master, state, {norm("Quiet Co")}, set(), {norm("Client Co")}, cfg, TODAY)
        self.assertEqual([p["display_name"] for p in picked], ["Quiet Co", "Active Co", "Big Co"])   # hiring-now first, large last


class RunnerTests(unittest.TestCase):
    def test_end_to_end(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        cfgdir = tmp / "config"
        shutil.copytree(CONFIG_DIR, cfgdir)
        sources = yaml.safe_load((cfgdir / "sources.yaml").read_text(encoding="utf-8"))
        for s in sources["engine3_contacts"]["sources"]:
            if s["id"] == "company_site":
                s.update(enabled=True, tos_reviewed_by="tester", tos_reviewed_on="2026-10-22")
        (cfgdir / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
        data, pdir = tmp / "data", tmp / "private"
        base = {"sector": "healthcare", "contact_status": "not_found"}
        write_csv(data / "kenya_companies_master.csv", [
            {**base, "company_id": "c1", "display_name": "Karen Hospital Ltd", "website": "https://karen.co.ke", "icp_status": "unknown_size"},
            {**base, "company_id": "c2", "display_name": "Blocked Clinic", "website": "https://b.co", "icp_status": "unknown_size"},
            {**base, "company_id": "c3", "display_name": "State Hospital", "website": "https://s.go.ke", "icp_status": "excluded_public"},
            {**base, "company_id": "c4", "display_name": "Optout Ltd", "website": "https://o.co", "icp_status": "unknown_size"},
        ], e3.MASTER_FIELDS)
        write_csv(pdir / "suppression.csv", [{"type": "company_id", "value": "c4"}], ["type", "value"])
        routes = {**ROUTES, "https://b.co/robots.txt": Resp(200, "User-agent: *\nDisallow: /")}
        client, _ = make_client(routes, tmp / "cache")
        out = e3.run(today=TODAY, client=client, data_dir=data, pdir=pdir, config_dir=cfgdir, check_mx=False)
        self.assertEqual(out["outcomes"], {"found": 1, "robots_disallowed": 1})
        contacts = read_csv(pdir / "hr_contacts.csv")
        self.assertTrue(contacts and all(c["company_id"] == "c1" for c in contacts))
        master = {r["company_id"]: r for r in read_csv(data / "kenya_companies_master.csv")}
        self.assertEqual((master["c1"]["contact_status"], master["c2"]["contact_status"]), ("found", "not_found"))
        manual = read_csv(pdir / "manual_contact_check.csv")
        self.assertEqual([m["company"] for m in manual], ["Blocked Clinic"])
        self.assertIn("linkedin.com/search", manual[0]["linkedin_search_url"])
        # the PUBLIC folder must hold no personal data
        public_text = "".join(p.read_text(encoding="utf-8") for p in data.glob("*.csv"))
        for needle in ("Jane Wanjiru", "hr@karen.co.ke", "+254202345678"):
            self.assertNotIn(needle, public_text)
        # second run the same day: nothing due, nothing re-crawled
        out2 = e3.run(today=TODAY, client=client, data_dir=data, pdir=pdir, config_dir=cfgdir, check_mx=False)
        self.assertEqual(out2["crawled"], 0)

    def test_gate_blocks_run_by_default(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        out = e3.run(today=TODAY, data_dir=tmp / "d", pdir=tmp / "p", check_mx=False)
        self.assertEqual((out["ran"], out["reason"]), (False, "disabled"))


if __name__ == "__main__":
    unittest.main()
