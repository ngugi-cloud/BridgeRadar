"""Tests for the public-repo privacy guard and the GitHub Actions workflows (offline)."""
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

from scraper import check_public as cp
from scraper.common.config import ROOT
from scraper.common.store import write_csv

WF = ROOT / ".github" / "workflows"


class PrivacyGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return rel

    def test_private_files_and_folders_blocked(self):
        for rel in ("data/hr_contacts.csv", "priority_queue.csv", "data/sub/lead_status.csv",
                    "_private/meta.json", "private_data/x.csv", "docs/_private/a.txt"):
            self.assertTrue(cp.check([rel], self.root), rel)

    def test_private_columns_blocked(self):
        self.write("data/leaks.csv", "company,Lead_ID,Score\nX,J-1,70\n")
        self.write("data/angles.csv", "company,suggested_outreach_angle\nX,pitch\n")
        self.write("data/ok.csv", "company,sector,count\nX,healthcare,3\n")
        probs = cp.check(["data/leaks.csv", "data/angles.csv", "data/ok.csv"], self.root)
        self.assertEqual(len(probs), 2)
        self.assertIn("Lead_ID", probs[0])

    def test_website_scanned_for_emails_and_phones(self):
        self.write("docs/data/dashboard.json", '{"companies":[{"name":"A","note":"jane@karen.co.ke"}]}')
        self.write("docs/data/p.json", '{"x":"+254 722 123 456"}')
        self.write("docs/data/p2.json", '{"x":"call 0722123456"}')
        self.write("docs/data/clean.json", '{"active_jobs":188,"date":"2026-10-23","count":2026102300}')
        probs = cp.check(["docs/data/dashboard.json", "docs/data/p.json", "docs/data/p2.json", "docs/data/clean.json"], self.root)
        self.assertEqual(len(probs), 3, probs)
        # public agency list may carry the agencies' own published emails; it is not under docs/
        self.write("data/competitor_agencies.csv", "name,norm,email\nAcme,acme,info@acme.co.ke\n")
        self.assertEqual(cp.check(["data/competitor_agencies.csv"], self.root), [])

    def test_deleted_paths_are_ignored(self):
        self.assertEqual(cp.check(["data/gone.csv"], self.root), [])

    def test_real_repo_and_pipeline_output_pass(self):
        files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()
                 and not any(part in (".git", "cache", "__pycache__", "private_data", "_private") for part in p.parts)]
        self.assertEqual(cp.check(files), [])

    def test_cli_exit_codes(self):
        bad = self.write("x/hr_contacts.csv", "a\n")
        r = subprocess.run([sys.executable, "-m", "scraper.check_public", bad], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn("PRIVACY CHECK FAILED", r.stderr)
        r = subprocess.run([sys.executable, "-m", "scraper.check_public", "README.md"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0)

    def test_full_run_output_passes_guard(self):
        """Run the whole pipeline into temp folders, then guard every public file it wrote."""
        from scraper import run_all, scoring
        from tests.test_scoring import MASTER, MASTER_FIELDS, TODAY, contact, job
        data, pdir, docs = self.root / "data", self.root / "_priv", self.root / "docs"
        write_csv(data / "kenya_companies_master.csv", MASTER, MASTER_FIELDS)
        jobs = [job("j1", "Karen Hospital Ltd", posted=2)]
        write_csv(data / "active_jobs.csv", jobs, list(jobs[0]))
        cs = [contact("k1", "c1", company="Karen Hospital Ltd")]
        write_csv(pdir / "hr_contacts.csv", cs, list(cs[0]))
        steps = [("scoring", lambda dry, today: scoring.run(dry_run=dry, today=today, data_dir=data, pdir=pdir))]
        run_all.run(today=TODAY, data_dir=data, pdir=pdir, steps=steps, docs_dir=docs)
        public = [str(p.relative_to(self.root)).replace("\\", "/")
                  for p in list(data.rglob("*")) + list(docs.rglob("*")) if p.is_file()]
        self.assertIn("docs/data/dashboard.json", public)
        self.assertEqual(cp.check(public, self.root), [])
        self.assertTrue(cp.check(["_priv/priority_queue.csv"], self.root))     # and the private output is refused


class WorkflowTests(unittest.TestCase):
    def load(self, name):
        y = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
        y["on"] = y.pop(True, y.get("on"))          # YAML 1.1 reads the key `on` as True
        return y

    def test_daily_workflow_shape(self):
        y = self.load("daily.yml")
        self.assertEqual(y["on"]["schedule"][0]["cron"], "17 2 * * *")       # 05:17 EAT
        self.assertIn("workflow_dispatch", y["on"])
        steps = y["jobs"]["pipeline"]["steps"]
        names = [s.get("name", s.get("uses", "")) for s in steps]
        order = ["Tests (offline)", "Run the pipeline", "Save PRIVATE data to the private repo",
                 "Privacy check, then save PUBLIC data here", "Package the dashboard"]
        self.assertEqual([n for n in names if n in order], order)
        run_step = next(s for s in steps if s.get("name") == "Run the pipeline")
        self.assertTrue(run_step["continue-on-error"])
        self.assertIn("_private", run_step["env"]["PRIVATE_DATA_DIR"])
        self.assertNotIn("PRIVATE_DATA_DIR", y.get("env", {}))         # tests must never see the real private dir
        test_step = next(s for s in steps if s.get("name") == "Tests (offline)")
        self.assertNotIn("PRIVATE_DATA_DIR", test_step.get("env", {}))
        pub = next(s for s in steps if s.get("name", "").startswith("Privacy check"))["run"]
        self.assertLess(pub.index("check_public --staged"), pub.index("git commit"))
        self.assertIn("git add data docs", pub)
        self.assertNotIn("git pull --rebase", pub)
        self.assertIn("git merge --no-edit -X ours", pub)
        private = next(s for s in steps if s.get("name", "").startswith("Save PRIVATE"))["run"]
        self.assertNotIn("git pull --rebase", private)
        self.assertIn("git merge --no-edit -X ours", private)
        self.assertIn("_private/", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        self.assertEqual(set(y["jobs"]), {"pipeline", "pages", "notify"})

    def test_ci_runs_guard_and_tests(self):
        y = self.load("ci.yml")
        runs = " ".join(s.get("run", "") for s in y["jobs"]["test"]["steps"])
        self.assertIn("check_public --tracked", runs)
        self.assertIn("unittest discover", runs)


if __name__ == "__main__":
    unittest.main()
