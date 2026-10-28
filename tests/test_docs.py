"""The owner-facing documents exist, their links work, and the key promises stay written down."""
import re
import unittest

from scraper.check_public import PRIVATE_FILES
from scraper.common.config import ROOT, load_yaml

DOCS = ["README.md", "SETUP_GUIDE.md", "DATA_SOURCES.md", "COMPLIANCE.md", "LIMITATIONS.md", "HANDOFF.md",
        "apps-script/SETUP.md", ".github/SETUP_ACTIONS.md"]


class DocsTests(unittest.TestCase):
    def test_docs_exist_and_relative_links_resolve(self):
        for rel in DOCS:
            path = ROOT / rel
            self.assertTrue(path.is_file(), rel)
            for target in re.findall(r"\]\(([^)#\s]+)\)", path.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                self.assertTrue((path.parent / target).exists(), f"{rel} links to missing {target}")

    def test_compliance_is_marked_draft_with_lawyer_questions(self):
        text = (ROOT / "COMPLIANCE.md").read_text(encoding="utf-8")
        self.assertIn("not legal advice", text)
        self.assertIn("Questions for the lawyer", text)
        self.assertIn("[TO FILL", text)                     # contact must be filled by a person, never invented
        for f in ("hr_contacts.csv", "priority_queue.csv"):
            self.assertIn(f, text)

    def test_data_sources_mentions_every_source(self):
        cfg = load_yaml("sources.yaml")
        text = (ROOT / "DATA_SOURCES.md").read_text(encoding="utf-8").lower()
        missing = []
        for eng in ("engine1_jobs", "engine2_companies", "engine3_contacts", "engine4_signals"):
            srcs = cfg[eng]["sources"] if isinstance(cfg[eng], dict) else cfg[eng]
            for s in srcs:
                words = re.findall(r"[a-z]{3,}", (s.get("name") or s["id"]).lower())
                if not any(w in text for w in words[:2]):
                    missing.append(s["id"])
        self.assertEqual(missing, [])

    def test_private_files_never_listed_as_public(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("must stay **private**", readme)
        self.assertTrue(PRIVATE_FILES)

    def test_setup_guide_step7_really_enables_company_ats(self):
        """Following SETUP_GUIDE.md step 7 literally must open the gate."""
        from scraper.common.config import gate
        cfg = load_yaml("sources.yaml")
        src = next(s for s in cfg["engine1_jobs"] if s["id"] == "company_ats")
        self.assertIn("tos_reviewed_by", src)                    # the guide tells you to fill this field in
        self.assertEqual(gate(src, cfg["settings"]), (False, "disabled"))
        src.update(tos_reviewed_by="Your Name", tos_reviewed_on="2026-10-23", enabled=True)
        self.assertEqual(gate(src, cfg["settings"]), (True, "ok"))
        header = (ROOT / "config" / "career_pages.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(header, "company,careers_url,ats_type,ats_token")

    def test_every_reviewable_source_has_both_review_fields(self):
        cfg = load_yaml("sources.yaml")
        for eng in ("engine1_jobs", "engine2_companies", "engine3_contacts", "engine4_signals"):
            srcs = cfg[eng]["sources"] if isinstance(cfg[eng], dict) else cfg[eng]
            for s in srcs:
                if "tos_reviewed_on" in s:
                    self.assertIn("tos_reviewed_by", s, s["id"])


if __name__ == "__main__":
    unittest.main()
