"""Company entity store with entity resolution.

Rules (so nothing is silently wrong):
  * Exact normalised-name match            -> merge.
  * Same (non-generic) website domain      -> merge.
  * Fuzzy ratio >= 0.95                    -> merge.
  * Fuzzy ratio 0.85 .. 0.95               -> NOT merged; written to needs_review.
  * Otherwise                              -> new company.
Merging never invents data: blanks are filled; a non-blank value is overwritten
only by a record from a higher-ranked source.
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import re
from collections import defaultdict

from .textutil import clean, normalize_company

AUTO_MERGE = 0.95
REVIEW_FLOOR = 0.85

GENERIC_DOMAINS = {"facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
                   "gmail.com", "yahoo.com", "google.com", "wordpress.com", "blogspot.com",
                   "wixsite.com", "wix.com", "business.site", "sites.google.com"}
ATS_HOSTS = {"greenhouse.io", "lever.co", "workable.com", "myworkdayjobs.com",
             "smartrecruiters.com", "bamboohr.com", "recruitee.com", "breezy.hr",
             "jobvite.com", "icims.com", "teamtailor.com"}
_LEGAL = {"ltd", "limited", "plc", "llc", "inc", "corporation", "corp"}

FIELDS = [
    "company_id", "display_name", "legal_name", "trading_name", "aliases", "parent_group",
    "sector", "sub_industry", "vertical", "classification_confidence", "ownership_type",
    "hq_location", "counties_present", "licence_number", "licence_status", "size_band",
    "size_source", "facility_level", "icp_status", "icp_note", "website",
    "careers_page_url", "ats_type", "ats_token", "contact_status", "source_ids",
    "source_urls", "source_count", "first_seen", "last_seen", "first_seen_baseline",
]
_FILL = ["sub_industry", "hq_location", "website", "careers_page_url", "ats_type", "ats_token",
         "licence_number", "licence_status", "ownership_type", "parent_group", "facility_level",
         "counties_present"]
_CONF = {"high": 2, "medium": 1, "low": 0, "": -1}


def domain_of(url: str) -> str:
    u = clean(url).lower()
    if not u:
        return ""
    u = re.sub(r"^[a-z]+://", "", u).split("/")[0].split("?")[0].split(":")[0]
    return re.sub(r"^www\d*\.", "", u)


def usable_domain(d: str) -> bool:
    if not d or "." not in d:
        return False
    return not any(d == g or d.endswith("." + g) for g in GENERIC_DOMAINS | ATS_HOSTS)


def has_legal_suffix(name: str) -> bool:
    toks = re.sub(r"[^a-z ]", " ", clean(name).lower()).split()
    return bool(toks) and toks[-1] in _LEGAL


def _first_token(norm: str) -> str:
    return norm.split(" ", 1)[0] if norm else ""


class EntityStore:
    def __init__(self):
        self.entities: list[dict] = []
        self.by_norm: dict[str, int] = {}
        self.by_domain: dict[str, int] = {}
        self.blocks: dict[str, list[int]] = defaultdict(list)
        self.review: list[dict] = []
        self.used_ids: set[str] = set()

    # ------------------------------------------------------------------ index
    def _index(self, i: int) -> None:
        e = self.entities[i]
        for n in e["_norms"]:
            self.by_norm.setdefault(n, i)
            if i not in self.blocks[_first_token(n)]:
                self.blocks[_first_token(n)].append(i)
        d = domain_of(e.get("website", ""))
        if usable_domain(d):
            self.by_domain.setdefault(d, i)

    def add_existing(self, row: dict) -> None:
        e = {f: clean(row.get(f)) for f in FIELDS}
        names = [e["display_name"], e["legal_name"], e["trading_name"]] + [
            a for a in e["aliases"].split("|") if a]
        e["_norms"] = {normalize_company(n) for n in names if n} - {""}
        e["_rank"] = 2
        e["_sector_key"] = (0, _CONF.get(e["classification_confidence"], -1))
        if not e["_norms"]:
            return
        self.used_ids.add(e["company_id"])
        self.entities.append(e)
        self._index(len(self.entities) - 1)

    # ---------------------------------------------------------------- resolve
    def _find(self, norm: str, domain: str, name: str):
        if norm in self.by_norm:
            return self.by_norm[norm], None
        if usable_domain(domain) and domain in self.by_domain:
            return self.by_domain[domain], None
        best_i, best_r = None, 0.0
        for i in self.blocks.get(_first_token(norm), []):
            for other in self.entities[i]["_norms"]:
                r = difflib.SequenceMatcher(None, norm, other).ratio()
                a, b = set(norm.split()), set(other.split())
                if min(len(a), len(b)) >= 2 and (a <= b or b <= a):
                    r = max(r, 0.9)          # one name contains the other: branch/variant -> review
                if r > best_r:
                    best_i, best_r = i, r
        if best_i is not None and best_r >= AUTO_MERGE:
            return best_i, None
        if best_i is not None and best_r >= REVIEW_FLOOR:
            e = self.entities[best_i]
            self.review.append({"company_a": name, "company_b": e["display_name"],
                                "ratio": round(best_r, 3),
                                "reason": "similar names: not merged automatically"})
        return None, None

    def _new_id(self, norm: str) -> str:
        n = 10
        while True:
            cid = "c_" + hashlib.sha1(norm.encode()).hexdigest()[:n]
            if cid not in self.used_ids:
                self.used_ids.add(cid)
                return cid
            n += 2

    # ------------------------------------------------------------------ merge
    def add_record(self, rec: dict, today: dt.date, baseline: bool = False) -> tuple[dict, bool]:
        name = clean(rec.get("name"))
        norm = normalize_company(name)
        if not norm:
            raise ValueError("record has no usable name")
        domain = domain_of(rec.get("website", ""))
        idx, _ = self._find(norm, domain, name)
        created = idx is None
        t = today.isoformat()
        rank = int(rec.get("rank", 1))
        if created:
            e = {f: "" for f in FIELDS}
            e.update(company_id=self._new_id(norm), first_seen=t, contact_status="not_found",
                     first_seen_baseline="1" if baseline else "", classification_confidence="")
            e.update(_norms=set(), _rank=0, _sector_key=(-1, -1))
            self.entities.append(e)
            idx = len(self.entities) - 1
        e = self.entities[idx]

        # names
        legal = has_legal_suffix(name)
        slot = "legal_name" if legal else "trading_name"
        if not e[slot]:
            e[slot] = name
        elif normalize_company(e[slot]) != norm:
            aliases = [a for a in e["aliases"].split("|") if a]
            if name not in aliases:
                aliases.append(name)
            e["aliases"] = "|".join(aliases)
        e["display_name"] = e["legal_name"] or e["trading_name"]
        e["_norms"].add(norm)

        # fields: fill blanks; overwrite only with a higher-ranked source
        higher = rank > e["_rank"]
        for f in _FILL:
            v = clean(rec.get(f))
            if v and (not e[f] or higher):
                e[f] = v
        size, ssrc = clean(rec.get("size")), clean(rec.get("size_source"))
        if size and ssrc and (not e["size_band"] or higher):
            e["size_band"], e["size_source"] = size, ssrc

        # sector: a source-declared sector beats keyword inference
        sec, conf = clean(rec.get("sector")), clean(rec.get("sector_confidence"))
        if sec and sec not in ("other", "cross"):
            key = (rank if rec.get("sector_is_hint") else 0, _CONF.get(conf, 0))
            if key > e["_sector_key"]:
                e["sector"], e["classification_confidence"] = sec, conf
                e["vertical"], e["_sector_key"] = clean(rec.get("vertical")), key
        elif not e["sector"] and sec == "other":
            e["sector"], e["classification_confidence"], e["vertical"] = "other", "low", "Other"

        # provenance
        ids = {x for x in e["source_ids"].split(";") if x} | {clean(rec.get("source_id"))} - {""}
        urls = [x for x in e["source_urls"].split(";") if x]
        u = clean(rec.get("source_url"))
        if u and u not in urls and len(urls) < 10:
            urls.append(u)
        e["source_ids"], e["source_urls"] = ";".join(sorted(ids)), ";".join(urls)
        e["source_count"] = len(ids)
        e["last_seen"] = t
        e["_rank"] = max(e["_rank"], rank)
        self._index(idx)
        return e, created

    def rows(self) -> list[dict]:
        return [{f: e.get(f, "") for f in FIELDS} for e in
                sorted(self.entities, key=lambda x: x["display_name"].lower())]
