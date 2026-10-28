"""Privacy guard for the PUBLIC repo. Fails (exit 1) if private data is about to be published.

Checks, for each path given:
  * file name is one of the private files, or it sits in a private folder;
  * a CSV has a private column (contact names, lead IDs, scores, outreach angles);
  * anything under docs/ (the public website) contains an email address or a Kenyan phone number.

Run:  python -m scraper.check_public --staged     (in the workflow, before every public commit)
      python -m scraper.check_public --tracked    (in CI, on every push)
      python -m scraper.check_public path1 path2  (explicit files)
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import re
import subprocess
import sys

from .common.config import ROOT

PRIVATE_FILES = {
    "hr_contacts.csv", "priority_queue.csv", "hiring_signals.csv", "advisory_flags.csv", "suppression.csv",
    "clients.csv", "lead_status.csv", "crawl_state.csv", "manual_contact_check.csv",
}
PRIVATE_DIRS = ("private_data/", "_private/")
PRIVATE_COLUMNS = {
    "contact_name", "contact_title", "Lead_ID", "Why_This_Score", "Suggested_Angle", "suggested_outreach_angle",
    "Primary_Lead", "Hot_Since", "suggested_angle",
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+254[\s-]?|\b0)(?:[17]\d{2}|20)[\s-]?\d{3}[\s-]?\d{3,4}\b")


def check(paths: list[str], root: pathlib.Path = ROOT) -> list[str]:
    problems = []
    for rel in paths:
        rel = rel.replace("\\", "/").removeprefix("./")
        name = rel.rsplit("/", 1)[-1]
        if name in PRIVATE_FILES:
            problems.append(f"{rel}: private file must never be in the public repo")
            continue
        if any(rel.startswith(d) or f"/{d}" in rel for d in PRIVATE_DIRS):
            problems.append(f"{rel}: inside a private folder")
            continue
        path = root / rel
        if not path.is_file():
            continue                                       # deleted file: nothing to publish
        if rel.endswith(".csv"):
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                header = next(csv.reader(f), [])
            bad = sorted(PRIVATE_COLUMNS & set(header))
            if bad:
                problems.append(f"{rel}: has private column(s) {', '.join(bad)}")
        if rel.startswith("docs/") and path.suffix in (".json", ".html", ".csv", ".js", ".txt"):
            text = path.read_text(encoding="utf-8", errors="replace")
            m = EMAIL.search(text)
            if m:
                problems.append(f"{rel}: contains an email address ({m.group(0)[:3]}...)")
            m = PHONE.search(text)
            if m:
                problems.append(f"{rel}: contains what looks like a phone number")
    return problems


def _git(*args) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Block private data from the public repo")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="check files staged for commit")
    g.add_argument("--tracked", action="store_true", help="check every tracked file")
    ap.add_argument("paths", nargs="*")
    a = ap.parse_args(argv)
    if a.staged:
        paths = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    elif a.tracked:
        paths = _git("ls-files")
    else:
        paths = a.paths
    problems = check(paths)
    if problems:
        print("PRIVACY CHECK FAILED - nothing should be published:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1
    print(f"Privacy check passed ({len(paths)} file(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
