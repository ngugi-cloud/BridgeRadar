"""Offline tests for Engine 2.  Run:  python -m unittest discover -s tests -v"""
import datetime as dt
import pathlib
import shutil
import tempfile
import unittest

import yaml

from scraper import engine2_companies as e2
from scraper.common.classify import Classifier
from scraper.common.config import CONFIG_DIR, load_yaml
from scraper.common.entities import EntityStore, domain_of, usable_domain
from scraper.common.store import read_csv, write_csv
from tests.test_engine1 import Resp, make_client

TODAY = dt.date(2026, 10, 22)
CFG = load_yaml("sector_map.yaml")
ICP = CFG["icp"]


def rec(name, **kw):
    base = {"name": name, "source_id": "t", "source_url": "https://x.co/list", "rank": 1}
    base.update(kw)
    return base


class ResolutionTests(unittest.TestCase):
    def test_exact_and_domain_merge(self):
        s = EntityStore()
        a, created = s.add_record(rec("Karen Hospital Ltd", website="https://www.karenhospital.org"), TODAY)
        self.assertTrue(created)
        b, created = s.add_record(rec("KAREN HOSPITAL LIMITED"), TODAY)
        self.assertFalse(created)
        c, created = s.add_record(rec("Karen Hosp. Group", website="http://karenhospital.org/about"), TODAY)
        self.assertFalse(created)
        self.assertEqual(len(s.entities), 1)
        self.assertEqual(s.entities[0]["trading_name"], "Karen Hosp. Group")   # name without Ltd = trading name
        _, created = s.add_record(rec("Karen Hospital Holdings Ltd", website="http://karenhospital.org"), TODAY)
        self.assertFalse(created)
        self.assertIn("Karen Hospital Holdings Ltd", s.entities[0]["aliases"])           # second legal name -> alias

    def test_generic_domain_never_merges(self):
        s = EntityStore()
        s.add_record(rec("Alpha Traders", website="https://facebook.com/alpha"), TODAY)
        _, created = s.add_record(rec("Beta Movers", website="https://facebook.com/beta"), TODAY)
        self.assertTrue(created)
        self.assertFalse(usable_domain(domain_of("https://boards.greenhouse.io/acme")))

    def test_similar_names_go_to_review_not_merge(self):
        s = EntityStore()
        s.add_record(rec("Karen Hospital"), TODAY)
        _, created = s.add_record(rec("Karen Hospital Nairobi"), TODAY)
        self.assertTrue(created)
        self.assertEqual(len(s.entities), 2)
        self.assertEqual(len(s.review), 1)

    def test_blanks_filled_and_rank_controls_overwrite(self):
        s = EntityStore()
        s.add_record(rec("Acme Foods Ltd", hq_location="Nairobi", rank=1), TODAY)
        e, _ = s.add_record(rec("Acme Foods Ltd", hq_location="Mombasa", website="https://acme.co.ke", rank=1), TODAY)
        self.assertEqual((e["hq_location"], e["website"]), ("Nairobi", "https://acme.co.ke"))   # blank filled, no overwrite
        e, _ = s.add_record(rec("Acme Foods Ltd", hq_location="Thika", rank=3), TODAY)
        self.assertEqual(e["hq_location"], "Thika")                                              # higher rank wins

    def test_size_needs_source(self):
        s = EntityStore()
        e, _ = s.add_record(rec("Acme Ltd", size="50"), TODAY)
        self.assertEqual(e["size_band"], "")
        e, _ = s.add_record(rec("Acme Ltd", size="50", size_source="KAM directory"), TODAY)
        self.assertEqual((e["size_band"], e["size_source"]), ("50", "KAM directory"))

    def test_sector_hint_beats_inference_and_other_never_overwrites(self):
        s = EntityStore()
        s.add_record(rec("Zed Ltd", sector="tech_ecommerce", vertical="Tech & E-commerce",
                         sector_confidence="low", sector_is_hint=False), TODAY)
        e, _ = s.add_record(rec("Zed Ltd", sector="manufacturing", vertical="Manufacturing",
                                sector_confidence="high", sector_is_hint=True, rank=3), TODAY)
        self.assertEqual(e["sector"], "manufacturing")
        e, _ = s.add_record(rec("Zed Ltd", sector="other", vertical="Other", sector_confidence="low"), TODAY)
        self.assertEqual(e["sector"], "manufacturing")

    def test_ids_stable_across_reload(self):
        s = EntityStore()
        e, _ = s.add_record(rec("Karen Hospital Ltd"), TODAY)
        cid = e["company_id"]
        s2 = EntityStore()
        for r in s.rows():
            s2.add_existing(r)
        e2_, created = s2.add_record(rec("karen hospital limited"), TODAY + dt.timedelta(days=1))
        self.assertFalse(created)
        self.assertEqual(e2_["company_id"], cid)
        self.assertEqual(e2_["first_seen"], TODAY.isoformat())


def ent(**kw):
    e = {"display_name": "Acme Ltd", "_norms": {"acme"}, "sector": "manufacturing", "ownership_type": "",
         "size_band": "", "size_source": "", "facility_level": "", "parent_group": "", "sub_industry": ""}
    e.update(kw)
    return e


class IcpTests(unittest.TestCase):
    def st(self, **kw):
        return e2.icp_status(ent(**kw), ICP, {"karibu agency"})[0]

    def test_cases(self):
        self.assertEqual(self.st(), "unknown_size")
        self.assertEqual(self.st(size_band="50", size_source="s"), "in_band")
        self.assertEqual(self.st(size_band="10-50", size_source="s"), "in_band")
        self.assertEqual(self.st(size_band="300", size_source="s"), "large_outside_band")
        self.assertEqual(self.st(size_band="5", size_source="s"), "excluded_micro")
        self.assertEqual(self.st(size_band="50"), "unknown_size")                   # no source -> ignored
        self.assertEqual(self.st(size_band="100-400", size_source="s"), "unknown_size")
        self.assertEqual(self.st(ownership_type="multinational"), "large_outside_band")
        self.assertEqual(self.st(ownership_type="state_corporation"), "excluded_public")
        self.assertEqual(self.st(sector="ngo_development"), "excluded_ngo")
        self.assertEqual(self.st(_norms={"karibu agency"}), "competitor_agency")
        self.assertEqual(self.st(display_name="Prime Staffing Solutions"), "competitor_agency")

    def test_healthcare_rules(self):
        self.assertEqual(self.st(sector="healthcare", facility_level="Level 2"), "excluded_facility")
        self.assertEqual(self.st(sector="healthcare", facility_level="Level 3"), "unknown_size")
        self.assertEqual(self.st(sector="healthcare", display_name="Town Dispensary"), "excluded_facility")
        self.assertEqual(self.st(sector="healthcare", display_name="Town Dispensary", parent_group="Afya Group"),
                         "unknown_size")                                             # part of a group


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.clf = Classifier(CFG)
        write_csv(self.tmp / "data/imports/l.csv",
                  [{"Institution": "Alpha Insurance Ltd", "Town": "Nairobi", "Staff": "120"},
                   {"Institution": "", "Town": "x", "Staff": ""}], ["Institution", "Town", "Staff"])

    def imp(self, **kw):
        d = {"id": "t", "file": "data/imports/l.csv", "source_ref": "ira_cma", "source_url": "https://ira.go.ke/list",
             "sector": "financial_services", "output": "master",
             "columns": {"name": "Institution", "hq_location": "Town", "size": "Staff"}}
        d.update(kw)
        return d

    def test_import_maps_columns_and_ignores_unsourced_size(self):
        recs, ags, problems = e2.adapter_csv_import(self.imp(), self.clf, self.tmp)
        self.assertEqual((len(recs), len(ags), problems), (1, 0, []))
        self.assertEqual((recs[0]["sector"], recs[0]["size"], recs[0]["source_url"]),
                         ("financial_services", "", "https://ira.go.ke/list"))
        recs, _, _ = e2.adapter_csv_import(self.imp(size_source="IRA annual report"), self.clf, self.tmp)
        self.assertEqual(recs[0]["size"], "120")

    def test_requires_source_url(self):
        recs, ags, problems = e2.adapter_csv_import(self.imp(source_url=""), self.clf, self.tmp)
        self.assertEqual((recs, ags), ([], []))
        self.assertTrue(problems)

    def test_agency_output(self):
        recs, ags, _ = e2.adapter_csv_import(self.imp(output="competitor_agencies"), self.clf, self.tmp)
        self.assertEqual((len(recs), ags[0]["name"]), (0, "Alpha Insurance Ltd"))

    def test_html_table_adapter(self):
        html = """<table><tr><th>Company</th><th>Town</th></tr>
                  <tr><td>Delta Plastics Ltd</td><td>Nairobi</td></tr>
                  <tr><td></td><td>x</td></tr></table>"""
        c, _ = make_client({"https://m.co/list": Resp(200, html)}, self.tmp / "cache")
        src = {"id": "kam", "sector": "manufacturing",
               "table": {"url": "https://m.co/list", "index": 0, "columns": {"name": "Company", "hq_location": "Town"}}}
        recs = e2.adapter_html_table(c, src, self.clf)
        self.assertEqual((len(recs), recs[0]["name"], recs[0]["sector"], recs[0]["source_url"]),
                         (1, "Delta Plastics Ltd", "manufacturing", "https://m.co/list"))


class RunnerTests(unittest.TestCase):
    def test_end_to_end(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        cfg = tmp / "config"
        shutil.copytree(CONFIG_DIR, cfg)
        imports = {"imports": [{"id": "ira", "file": "data/imports/ira.csv", "source_ref": "ira_cma",
                                "source_url": "https://ira.go.ke/list", "reviewed_by": "tester",
                                "reviewed_on": "2026-10-22", "sector": "financial_services", "output": "master",
                                "columns": {"name": "Name", "hq_location": "Town"}},
                               {"id": "unreviewed", "file": "data/imports/x.csv", "source_url": "u", "columns": {"name": "N"}},
                               {"id": "nea", "file": "data/imports/nea.csv", "source_ref": "nea_agencies",
                                "source_url": "https://nea.go.ke/agencies", "reviewed_by": "tester",
                                "reviewed_on": "2026-10-22", "output": "competitor_agencies",
                                "columns": {"name": "Agency", "email": "Email"}}]}
        (cfg / "imports.yaml").write_text(yaml.safe_dump(imports), encoding="utf-8")
        write_csv(tmp / "data/imports/ira.csv", [{"Name": "Alpha Insurance Ltd", "Town": "Nairobi"},
                                                 {"Name": "Karibu Staffing Kenya", "Town": "Nairobi"}], ["Name", "Town"])
        write_csv(tmp / "data/imports/nea.csv", [{"Agency": "Karibu Staffing Kenya", "Email": "info@karibu.co.ke"}],
                  ["Agency", "Email"])
        job = {"job_id": "1", "company": "Karen Hospital Ltd", "sector": "healthcare", "posted_by": "unknown",
               "source_url": "https://k.co/jobs/1"}
        agency_job = {"job_id": "2", "company": "Quick Recruitment Ltd", "sector": "other", "posted_by": "agency",
                      "source_url": "https://b.co/2"}
        conf = {"job_id": "3", "company": "Confidential", "sector": "other", "posted_by": "confidential",
                "source_url": "https://b.co/3"}
        write_csv(tmp / "data/active_jobs.csv", [job, agency_job, conf],
                  ["job_id", "company", "sector", "posted_by", "source_url"])

        out = e2.run(today=TODAY, data_dir=tmp / "data", config_dir=cfg, root=tmp)
        master = {r["display_name"]: r for r in read_csv(tmp / "data/kenya_companies_master.csv")}
        self.assertEqual(set(master), {"Karen Hospital Ltd", "Alpha Insurance Ltd", "Karibu Staffing Kenya"})
        self.assertEqual(master["Alpha Insurance Ltd"]["sector"], "financial_services")
        self.assertEqual(master["Alpha Insurance Ltd"]["source_urls"], "https://ira.go.ke/list")
        self.assertEqual(master["Karen Hospital Ltd"]["classification_confidence"], "low")
        self.assertEqual(master["Karibu Staffing Kenya"]["icp_status"], "competitor_agency")
        self.assertEqual(master["Alpha Insurance Ltd"]["contact_status"], "not_found")
        agencies = {r["name"] for r in read_csv(tmp / "data/competitor_agencies.csv")}
        self.assertEqual(agencies, {"Karibu Staffing Kenya", "Quick Recruitment Ltd"})
        self.assertTrue(out["baseline"])
        log = {(l["source_id"], l["status"]) for l in read_csv(tmp / "data/scrape_log.csv")}
        self.assertIn(("import:unreviewed", "skipped:not_reviewed"), log)

        # second run: stable IDs, new company logged as change, nothing duplicated
        ids = {n: r["company_id"] for n, r in master.items()}
        write_csv(tmp / "data/active_jobs.csv", [job, {**job, "company": "Delta Plastics Ltd", "sector": "manufacturing"}],
                  ["job_id", "company", "sector", "posted_by", "source_url"])
        e2.run(today=TODAY + dt.timedelta(days=1), data_dir=tmp / "data", config_dir=cfg, root=tmp)
        master2 = {r["display_name"]: r for r in read_csv(tmp / "data/kenya_companies_master.csv")}
        self.assertEqual(len(master2), 4)
        self.assertEqual({n: master2[n]["company_id"] for n in ids}, ids)
        changes = read_csv(tmp / "data/changes.csv")
        self.assertEqual([c["company"] for c in changes if c["change_type"] == "new_company"], ["Delta Plastics Ltd"])


if __name__ == "__main__":
    unittest.main()
