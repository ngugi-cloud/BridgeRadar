"""Contract tests between the pipeline and apps-script/Code.gs.

Runs the Node unit tests for the Apps Script logic, and checks end to end that a
status typed in the Sheet survives the round trip Sheet -> lead_status.csv -> scoring.
Skipped (not failed) when Node is not installed.
"""
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

from scraper import scoring as sc
from scraper.common.config import ROOT
from scraper.common.store import read_csv, write_csv
from tests.test_scoring import MASTER, MASTER_FIELDS, TODAY, contact, job

NODE = shutil.which("node")
GS = ROOT / "apps-script" / "Code.gs"


def node_eval(js: str) -> str:
    return subprocess.run([NODE, "-e", f"const g=require({json.dumps(str(GS))});{js}"],
                          capture_output=True, text=True, encoding="utf-8", check=True, timeout=60).stdout


@unittest.skipUnless(NODE, "Node.js not installed")
class AppsScriptTests(unittest.TestCase):
    def test_node_unit_tests_pass(self):
        r = subprocess.run([NODE, "--test", str(ROOT / "apps-script" / "tests" / "logic.test.js")], capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])

    def test_config_contract(self):
        cfg = json.loads(node_eval("console.log(JSON.stringify({tabs:g.TABS,owned:g.OWNED,ls:g.LEAD_STATUS_HEADER,st:g.STATUSES}))"))
        self.assertEqual(cfg["ls"], ["Lead_ID"] + sc.HUMAN)
        self.assertEqual(cfg["st"], yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))["queue"]["statuses"])
        code = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "scraper").rglob("*.py"))
        for t in cfg["tabs"]:
            self.assertIn(t["file"], code, f"{t['name']} reads a file no engine writes")
        q = next(t for t in cfg["tabs"] if t["name"] == "Priority_Queue")
        self.assertEqual((q["repo"], q["key"]), ("private", "Lead_ID"))
        # every private pipeline file is read from the PRIVATE repo, never the public one
        private_files = {"priority_queue.csv", "hiring_signals.csv", "hr_contacts.csv", "manual_contact_check.csv",
                         "advisory_flags.csv"}
        for t in cfg["tabs"]:
            if t["file"] in private_files:
                self.assertEqual(t["repo"], "private", t["name"])
        # write-back files are exactly the ones the pipeline reads back
        self.assertEqual({o["file"] for o in cfg["owned"]}, {"suppression.csv", "clients.csv"})
        sup = next(o for o in cfg["owned"] if o["name"] == "Suppression")
        self.assertEqual(sup["header"][:2], ["type", "value"])
        self.assertIn("lead_status.csv", (ROOT / "scraper" / "scoring.py").read_text(encoding="utf-8"))

    def test_round_trip_sheet_edit_reaches_scoring(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        data, pdir = tmp / "data", tmp / "private"
        write_csv(data / "kenya_companies_master.csv", MASTER, MASTER_FIELDS)
        jobs = [job("j1", "Karen Hospital Ltd", posted=2), job("j2", "Twiga Foods Ltd", posted=3)]
        write_csv(data / "active_jobs.csv", jobs, list(jobs[0]))
        cs = [contact("k1", "c1")]
        write_csv(pdir / "hr_contacts.csv", cs, list(cs[0]))
        sc.run(today=TODAY, data_dir=data, pdir=pdir)
        # Apps Script: import the queue into a "sheet", the owner edits it, then write back
        js = f"""
        const fs=require('fs');
        const c=g.csvToObjects(fs.readFileSync({json.dumps(str(pdir / 'priority_queue.csv'))},'utf8'));
        const spec=g.TABS.find(t=>t.name==='Priority_Queue');
        const m=g.mergeTab(spec,c,null,'2026-10-23',{{Owner:'Head of BD',Status:'New'}});
        const s=m.header.indexOf('Status'), n=m.header.indexOf('Notes'), l=m.header.indexOf('Last_contacted');
        const row=m.values.find(r=>r[0]==='J-j1'); row[s]='Won'; row[n]='signed, "retainer"'; row[l]=new Date('2026-10-22T00:00:00Z');
        const out=g.leadStatusRows(m.header,m.values,d=>d.toISOString().slice(0,10));
        fs.writeFileSync({json.dumps(str(pdir / 'lead_status.csv'))}, g.toCsv(out));
        """
        node_eval(js)
        ls = {r["Lead_ID"]: r for r in read_csv(pdir / "lead_status.csv")}
        self.assertEqual(ls["J-j1"]["Notes"], 'signed, "retainer"')
        sc.run(today=TODAY, data_dir=data, pdir=pdir)
        q = {r["Lead_ID"]: r for r in read_csv(pdir / "priority_queue.csv")}
        self.assertEqual((q["J-j1"]["Status"], q["J-j1"]["Temperature"]), ("Won", "Closed"))
        self.assertEqual((q["J-j1"]["Last_contacted"], q["J-j1"]["Notes"]), ("2026-10-22", 'signed, "retainer"'))
        self.assertEqual(q["J-j2"]["Status"], "New")

    def test_no_secrets_or_private_names_in_script(self):
        text = GS.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", text))
        self.assertNotIn("@bridgetalentgroup.com", text)


if __name__ == "__main__":
    unittest.main()
