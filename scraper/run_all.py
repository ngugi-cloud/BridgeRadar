"""Daily pipeline: Engine 1 -> 2 -> 3 -> 4 -> scoring, in that order.

* Each step is isolated: if one fails, the error is logged and the next step still
  runs on the latest files it has (yesterday's data at worst).
* Writes meta.json (last refreshed time + per-step status, counts only) to the
  public data folder AND the private folder. Apps Script reads the private copy
  to check the data is fresh before importing.
* Trims changes.csv and scrape_log.csv so they do not grow forever.
* Exit code 1 if any step failed (so GitHub Actions can notify), after all
  files have been written.

* Builds docs/data/dashboard.json for the public GitHub Pages dashboard.

Run:  python -m scraper.run_all [--dry-run] [--only engine1,scoring]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time
import traceback

from . import dashboard, engine1_jobs, engine2_companies, engine3_contacts, engine4_signals, scoring
from .common.config import DATA_DIR
from .common.store import CHANGE_FIELDS, LOG_FIELDS, append_csv, log_row, read_csv, write_csv
from .common.textutil import parse_date
from .engine3_contacts import private_dir

CHANGES_KEEP_DAYS = 90      # changes.csv: rolling 90 days
LOG_KEEP_DAYS = 60          # scrape_log.csv: rolling 60 days


def default_steps(data_dir: pathlib.Path, pdir: pathlib.Path, config_dir=None) -> list[tuple[str, object]]:
    """(name, callable(dry_run, today) -> summary dict). Order matters."""
    return [
        ("engine1_jobs", lambda dry, today: engine1_jobs.run(dry_run=dry, today=today, data_dir=data_dir,
                                                            config_dir=config_dir)),
        ("engine2_companies", lambda dry, today: engine2_companies.run(dry_run=dry, today=today, data_dir=data_dir,
                                                                      config_dir=config_dir)),
        ("engine3_contacts", lambda dry, today: engine3_contacts.run(dry_run=dry, today=today, data_dir=data_dir,
                                                                    pdir=pdir, config_dir=config_dir)),
        ("engine4_signals", lambda dry, today: engine4_signals.run(dry_run=dry, today=today, data_dir=data_dir,
                                                                  pdir=pdir, config_dir=config_dir)),
        ("scoring", lambda dry, today: scoring.run(dry_run=dry, today=today, data_dir=data_dir, pdir=pdir,
                                                   config_dir=config_dir)),
    ]


def trim_csv(path: pathlib.Path, date_field: str, keep_days: int, fields: list[str], today: dt.date) -> int:
    """Drop rows older than keep_days. Rows with an unreadable date are kept. Returns rows removed."""
    rows = read_csv(path)
    if not rows:
        return 0
    cutoff = today - dt.timedelta(days=keep_days)
    kept = [r for r in rows if not (parse_date(r.get(date_field), today) and parse_date(r.get(date_field), today) < cutoff)]
    if len(kept) != len(rows):
        write_csv(path, kept, fields)
    return len(rows) - len(kept)


def _public_safe(summary) -> object:
    """meta.json is public: keep numbers, booleans and short status words only."""
    if isinstance(summary, dict):
        return {str(k): _public_safe(v) for k, v in summary.items()}
    if isinstance(summary, (int, float, bool)) or summary is None:
        return summary
    return str(summary)[:60]


def run(dry_run: bool = False, today: dt.date | None = None, data_dir=None, pdir=None, config_dir=None,
        steps=None, only: set | None = None, now=None, docs_dir=None) -> dict:
    today = today or dt.date.today()
    data_dir, pdir = pathlib.Path(data_dir or DATA_DIR), pathlib.Path(pdir or private_dir())
    steps = steps if steps is not None else default_steps(data_dir, pdir, config_dir)
    started = (now or (lambda: dt.datetime.now(dt.timezone.utc)))()
    results, failed = {}, []

    for name, fn in steps:
        if only and name not in only:
            results[name] = {"status": "not_selected"}
            continue
        t0 = time.monotonic()
        try:
            summary = fn(dry_run, today)
            results[name] = {"status": "ok", "seconds": round(time.monotonic() - t0, 1),
                             "summary": _public_safe(summary)}
        except Exception as e:                          # isolate: one engine must not stop the rest
            failed.append(name)
            results[name] = {"status": "error", "seconds": round(time.monotonic() - t0, 1),
                             "error": type(e).__name__, "_detail": f"{type(e).__name__}: {e}"[:300]}
            traceback.print_exc(file=sys.stderr)
            if not dry_run:
                append_csv(data_dir / "scrape_log.csv",           # public log: exception type only
                           [log_row(name, "_run", "error", 0, type(e).__name__, today)], LOG_FIELDS)

    trimmed = {}
    if not dry_run:
        trimmed["changes"] = trim_csv(data_dir / "changes.csv", "date", CHANGES_KEEP_DAYS, CHANGE_FIELDS, today)
        trimmed["scrape_log"] = trim_csv(data_dir / "scrape_log.csv", "run_date", LOG_KEEP_DAYS, LOG_FIELDS, today)

    finished = (now or (lambda: dt.datetime.now(dt.timezone.utc)))()
    meta = {
        "last_refreshed_utc": finished.replace(microsecond=0).isoformat(),
        "run_date": today.isoformat(),
        "started_utc": started.replace(microsecond=0).isoformat(),
        "ok": not failed, "failed_steps": failed, "steps": results, "trimmed_rows": trimmed,
        "label": scoring.LABEL,
    }
    public = {**meta, "steps": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                for k, v in results.items()}}
    if not dry_run:
        try:                                            # public dashboard data (aggregates only)
            dashboard.write(dashboard.build(data_dir, today, public), docs_dir)
            results["dashboard"] = {"status": "ok"}
        except Exception as e:
            failed.append("dashboard")
            results["dashboard"] = {"status": "error", "error": type(e).__name__, "_detail": f"{type(e).__name__}: {e}"[:300]}
            traceback.print_exc(file=sys.stderr)
        meta["ok"] = not failed
        public = {**meta, "steps": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                                    for k, v in results.items()}}
        for folder, body in ((data_dir, public), (pdir, meta)):   # private copy keeps error details
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "meta.json").write_text(json.dumps(body, indent=2), encoding="utf-8")
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the whole Bridge Lead Radar pipeline")
    ap.add_argument("--dry-run", action="store_true", help="run everything but write no files")
    ap.add_argument("--only", default="", help="comma-separated step names, e.g. engine4_signals,scoring")
    args = ap.parse_args(argv)
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    meta = run(dry_run=args.dry_run, only=only)
    print(json.dumps(meta, indent=2))
    return 0 if meta["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
