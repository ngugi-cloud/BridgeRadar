"""Small CSV helpers: read/write tables, scrape log, changes log, manual list."""
from __future__ import annotations

import csv
import datetime as dt
import pathlib

LOG_FIELDS = ["run_date", "engine", "source_id", "status", "items", "detail"]
CHANGE_FIELDS = ["date", "change_type", "entity_type", "entity_id", "company", "summary", "source_url"]
MANUAL_FIELDS = ["source_id", "name", "reason", "helper_url", "date"]


def read_csv(path) -> list[dict]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows: list[dict], fields: list[str]) -> None:
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)


def append_csv(path, rows: list[dict], fields: list[str]) -> None:
    if not rows:
        return
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", restval="")
        if new:
            w.writeheader()
        w.writerows(rows)


def log_row(engine: str, source_id: str, status: str, items: int = 0,
            detail: str = "", today: dt.date | None = None) -> dict:
    return {"run_date": (today or dt.date.today()).isoformat(), "engine": engine,
            "source_id": source_id, "status": status, "items": items, "detail": detail}
