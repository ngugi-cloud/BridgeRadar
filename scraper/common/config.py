"""Config loading and the compliance gate.

A source runs only if gate() says so. This is the code form of the rules at the
top of config/sources.yaml. Nothing in the project bypasses it.
"""
from __future__ import annotations

import os
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"


def load_yaml(name: str, config_dir: pathlib.Path | None = None) -> dict:
    with open((config_dir or CONFIG_DIR) / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def gate(src: dict, settings: dict, env: dict | None = None) -> tuple[bool, str]:
    """Return (may_run, reason). Reasons are written to the scrape log."""
    env = os.environ if env is None else env
    if src.get("access") == "manual" or src.get("status") == "manual_required":
        return False, "manual_required"
    if not src.get("enabled"):
        return False, "disabled"
    if src.get("access") == "internal":      # our own data (no website involved)
        return True, "ok"
    if not src.get("tos_reviewed_on") or not src.get("tos_reviewed_by"):
        return False, "tos_not_reviewed"
    if "PLACEHOLDER" in str(settings.get("user_agent", "")):
        return False, "user_agent_placeholder"
    secret = src.get("secret")
    if secret and not env.get(secret):
        return False, f"missing_secret:{secret}"
    return True, "ok"
