"""Offline tests for Engine 4.  Run:  python -m unittest discover -s tests -t . -v"""
import datetime as dt
import pathlib
import shutil
import tempfile
import unittest

import yaml

from scraper import engine4_signals as e4
from scraper.common.classify import Classifier
from scraper.common.config import CONFIG_DIR, load_yaml
from scraper.common.store import read_csv, write_csv
from tests.test_engine1 import Resp, make_client

TODAY = dt.date(2026, 10, 22)
SIG, SCORING = load_yaml("signals.yaml"), load_yaml("scoring.yaml")
DET = e4.Detector(SIG, SCORING)
CLF = Classifier(load_yaml("sector_map.yaml"))
MASTER = [
    {"company_id": "c1", "display_name": "Karen Hospital Ltd", "legal_name": "Karen Hospital Ltd", "trading_name": "",
     "aliases": "", "sector": "healthcare", "icp_status": "unknown_size"},
    {"company_id": "c2", "display_name": "Safaricom PLC", "legal_name": "Safaricom PLC", "trading_name": "",
     "aliases": "", "sector": "telecoms", "icp_status": "large_outside_band"},
    {"company_id": "c3", "display_name": "Twiga Foods Ltd", "legal_name": "Twiga Foods Ltd", "trading_name": "",
     "aliases": "", "sector": "retail_fmcg", "icp_status": "unknown_size"},
    {"company_id": "c4", "display_name": "State Hospital", "legal_name": "", "trading_name": "State Hospital",
     "aliases": "", "sector": "healthcare", "icp_status": "excluded_public"},
]
INDEX = e4.CompanyIndex(MASTER)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Twiga Foods raises $8M seed round - Business Daily</title><link>https://n.co/1</link>
<pubDate>Tue, 20 Oct 2026 08:00:00 GMT</pubDate><source url="https://bd.co">Business Daily</source></item>
<item><title>Karen Hospital expands with new wing creating 250 jobs - The Star</title><link>https://n.co/2</link>
<pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate><source url="https://star.co">The Star</source></item>
<item><title>No date here</title><link>https://n.co/3</link></item>
</channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Safaricom launches new platform</title>
<link href="https://a.co/1"/><published>2026-10-18T09:00:00Z</published><summary>&lt;p&gt;text&lt;/p&gt;</summary></entry></feed>"""
BOMB = """<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><rss><channel><item>
<title>&b;</title><link>https://x.co</link></item></channel></rss>"""


def raw(head, link, d="2026-10-20", outlet="Business Daily", text=""):
    return {"headline": head, "link": link, "date": dt.date.fromisoformat(d), "outlet": outlet, "text": text}


def enrich(items, **kw):
    return e4.enrich_items(items, DET, INDEX, CLF, kw.get("today", TODAY), 90, True)


class FeedTests(unittest.TestCase):
    def test_rss_and_atom(self):
        items = e4.parse_feed(RSS)
        self.assertEqual(len(items), 3)                      # undated item is parsed (date None); enrich drops it later
        self.assertIsNone(items[2]["date"])
        self.assertEqual(items[0]["headline"], "Twiga Foods raises $8M seed round")   # publisher suffix removed
        self.assertEqual((items[0]["outlet"], items[0]["date"]), ("Business Daily", dt.date(2026, 10, 20)))
        atom = e4.parse_feed(ATOM, "Some Feed")
        self.assertEqual((atom[0]["link"], atom[0]["date"], atom[0]["outlet"]), ("https://a.co/1", dt.date(2026, 10, 18), "Some Feed"))

    def test_unsafe_or_garbage_xml_rejected(self):
        self.assertEqual(e4.parse_feed(BOMB), [])
        self.assertEqual(e4.parse_feed("not xml at all"), [])

    def test_google_news_urls(self):
        urls = e4.google_news_urls(SIG)
        self.assertEqual(len(urls), len(SIG["google_news"]["queries"]))
        self.assertTrue(urls[0].startswith("https://news.google.com/rss/search?q=Kenya+company+raises+funding+when%3A30d"))


class DetectTests(unittest.TestCase):
    def test_signal_types(self):
        cases = {
            "Twiga Foods raises $8M seed round": "funding_raised",
            "Karen Hospital expands to Mombasa": "expansion_announced",
            "Firm opens new factory in Athi River": "expansion_announced",
            "Kenya signs PPP agreement for highway": "government_investment_deal",
            "Acme acquires rival in Nairobi": "mna_or_ipo",
            "Acme secures licence to operate in Kenya": "licence_or_approval",
            "Acme wins tender to supply hospitals": "contract_or_tender_won",
            "Bank appoints new chief executive": "leadership_hire",
            "Acme launches new mobile platform": "product_launch",
            "Acme raises $5M": "funding_raised",                     # amount alone is enough
            "Acme raises prices in Nairobi": None,
            "Weather turns cold in Nairobi": None,
        }
        for head, want in cases.items():
            self.assertEqual(DET.signal_type(head), want, head)

    def test_one_article_one_signal_highest_points(self):
        self.assertEqual(DET.signal_type("Acme raises $5M and opens new factory"), "funding_raised")

    def test_advisory_precedence(self):
        self.assertTrue(DET.is_advisory("Acme announces layoffs after expansion plans collapse"))
        sigs, advs, _ = enrich([raw("Karen Hospital announces retrenchment of staff", "https://n/1")])
        self.assertEqual((len(sigs), len(advs)), (0, 1))

    def test_candidates_amounts_jobs(self):
        self.assertEqual(DET.candidate_company("Acme Foods raises $8M in seed funding"), "Acme Foods")
        self.assertEqual(DET.candidate_company("Nairobi-based Zuri Labs secures grant"), "Zuri Labs")
        self.assertEqual(DET.candidate_company("Kenya raises funds for roads"), "")
        self.assertEqual(DET.candidate_company("how firms are raising money"), "")
        self.assertEqual(e4.extract_amount("raises $8M in seed"), "$8M")
        self.assertEqual(e4.extract_amount("Sh3 billion feed factory"), "Sh3 billion")
        self.assertEqual(e4.extract_amount("nothing here"), "")
        self.assertEqual(e4.extract_jobs("factory to create 250 direct jobs"), 250)
        self.assertEqual(e4.extract_jobs("will create 1,000 jobs"), 1000)
        self.assertEqual(e4.extract_jobs("price rises"), 0)

    def test_company_index(self):
        self.assertEqual(INDEX.match("Twiga Foods raises $8M")["company_id"], "c3")
        self.assertEqual(INDEX.match("Safaricom launches new service")["company_id"], "c2")
        self.assertIsNone(INDEX.match("the safaricom effect on markets"))        # single word needs capital letter
        self.assertIsNone(INDEX.match("Private equity firm raises funds"))

    def test_ambiguous_match_is_not_guessed(self):
        idx = e4.CompanyIndex(MASTER + [{"company_id": "c9", "display_name": "Foods Twiga Ltd", "legal_name": "",
                                          "trading_name": "", "aliases": "twiga foods kenya", "sector": "x", "icp_status": ""}])
        self.assertIsNotNone(idx.match("Twiga Foods raises"))      # longest/most specific still resolves
        # equal-length candidates -> None
        a = e4.CompanyIndex([{"company_id": "x1", "display_name": "Alpha Beta", "legal_name": "", "trading_name": "", "aliases": "", "sector": "", "icp_status": ""},
                             {"company_id": "x2", "display_name": "Beta Alpha", "legal_name": "", "trading_name": "", "aliases": "", "sector": "", "icp_status": ""}])
        self.assertIsNone(a.match("Alpha Beta and Beta Alpha merge"))


class EnrichTests(unittest.TestCase):
    def test_filters(self):
        items = [
            raw("Twiga Foods raises $8M seed round", "https://n/1"),
            raw("Twiga Foods raises $8M seed round", "https://n/1"),                         # duplicate link
            raw("Zuri Labs raises $2M in London", "https://n/2"),                           # unknown + no Kenya marker
            raw("Nairobi-based Zuri Labs secures $2M grant", "https://n/3"),                # unknown + Kenya marker -> candidate
            raw("Twiga Foods raises $1M seed", "https://n/4", d="2026-05-01"),              # too old
            {**raw("Twiga Foods raises $1M", "https://n/5"), "date": None},                  # undated: never invented
            raw("Football results", "https://n/6"),
        ]
        sigs, advs, stats = enrich(items)
        self.assertEqual([(s["company"], s["matched"]) for s in sigs], [("Twiga Foods Ltd", True), ("Zuri Labs", False)])
        self.assertEqual((stats["duplicate_link"], stats["unattributed"], stats["no_or_old_date"], stats["no_signal"]), (1, 1, 2, 1))


class ScoringTests(unittest.TestCase):
    def test_decay_and_reliability(self):
        self.assertEqual([e4.decay_for(d, SCORING) for d in (0, 30, 31, 60, 61, 90, 91)], [1.0, 1.0, 0.75, 0.75, 0.5, 0.5, 0.0])
        self.assertEqual(e4.reliability_for(["major_press"], 1, SCORING), ("major_press", 1.0))
        self.assertEqual(e4.reliability_for(["major_press", "tracker"], 2, SCORING)[1], 1.2)      # corroboration, capped
        self.assertEqual(e4.reliability_for(["single_unverified"], 1, SCORING)[1], 0.7)
        self.assertEqual(e4.reliability_for(["single_unverified", "single_unverified"], 2, SCORING)[1], 0.9)

    def test_score_values(self):
        s = e4.score_signal("funding_raised", dt.date(2026, 10, 20), 0, ["major_press"], 1, SCORING, TODAY)
        self.assertEqual((s["signal_score"], s["trigger_strength"], s["predicted_hiring_probability"]), (15.0, 37.5, "High"))
        s = e4.score_signal("funding_raised", dt.date(2026, 10, 20), 0, ["single_unverified"], 1, SCORING, TODAY)
        self.assertEqual((s["signal_score"], s["predicted_hiring_probability"]), (10.5, "Medium"))
        s = e4.score_signal("product_launch", dt.date(2026, 10, 20), 0, ["single_unverified"], 1, SCORING, TODAY)
        self.assertEqual((s["signal_score"], s["predicted_hiring_probability"]), (5.6, "Low"))
        s = e4.score_signal("expansion_announced", dt.date(2026, 10, 20), 600, ["government"], 1, SCORING, TODAY)
        self.assertEqual(s["trigger_strength"], 40.0)                                         # 37.5 + 3, capped at 40
        s = e4.score_signal("funding_raised", dt.date(2026, 7, 1), 600, ["major_press"], 1, SCORING, TODAY)
        self.assertEqual((s["decay"], s["trigger_strength"]), (0.0, 0.0))                     # expired: bonus ignored

    def test_angle(self):
        self.assertEqual(e4.make_angle("funding_raised", "$8M", 0, SIG), "Raised $8M — pitch scale-up recruitment and contract staffing")
        self.assertIn("(~250 jobs stated)", e4.make_angle("expansion_announced", "", 250, SIG))
        self.assertNotIn("{", e4.make_angle("contract_or_tender_won", "", 0, SIG))


class ClusterTests(unittest.TestCase):
    def build(self, items, prev=None, today=TODAY, baseline=True):
        sigs, _, _ = enrich(items, today=today)
        return e4.build_signals(sigs, prev or [], DET, SCORING, SIG, today, baseline)

    def test_same_event_many_outlets_is_one_signal(self):
        rows, _ = self.build([raw("Twiga Foods raises $8M seed round", "https://n/1", "2026-10-19", "Business Daily"),
                              raw("Twiga Foods secures $8M in funding", "https://n/2", "2026-10-20", "TechCabal"),
                              raw("Twiga Foods raises $8m", "https://n/3", "2026-10-20", "TechCabal")])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["outlets_count"], r["signal_date"], r["source_outlet"], r["amount_text"]),
                         (2, "2026-10-19", "Business Daily", "$8M"))
        self.assertEqual((r["reliability_multiplier"], r["label"]), (1.2, "signal-based prioritization"))
        self.assertEqual(r["also_reported_by"], "TechCabal")

    def test_far_apart_events_stay_separate(self):
        rows, _ = self.build([raw("Twiga Foods raises $8M seed round", "https://n/1", "2026-09-10"),
                              raw("Twiga Foods raises $20M Series A", "https://n/2", "2026-10-20")])
        self.assertEqual(len(rows), 2)

    def test_history_merge_and_new_ids(self):
        rows1, new1 = self.build([raw("Twiga Foods raises $8M seed round", "https://n/1", "2026-10-19")])
        self.assertEqual(new1, set())                                    # baseline: no "new" alerts
        t2 = TODAY + dt.timedelta(days=1)
        rows2, new2 = self.build([raw("Twiga Foods secures $8M funding", "https://n/2", "2026-10-21", "TechCabal"),
                                  raw("Karen Hospital expands to Kisumu", "https://n/9", "2026-10-22")],
                                 prev=rows1, today=t2, baseline=False)
        by = {r["company"]: r for r in rows2}
        self.assertEqual(len(rows2), 2)
        self.assertEqual(by["Twiga Foods Ltd"]["signal_id"], rows1[0]["signal_id"])      # same event, same id
        self.assertEqual(by["Twiga Foods Ltd"]["outlets_count"], 2)
        self.assertEqual(new2, {by["Karen Hospital Ltd"]["signal_id"]})                  # only the new event is "new"

    def test_stale_rows_rescored_and_retained(self):
        rows1, _ = self.build([raw("Twiga Foods raises $8M seed round", "https://n/1", "2026-10-19")])
        later = TODAY + dt.timedelta(days=60)
        rows2, _ = e4.build_signals([], rows1, DET, SCORING, SIG, later, False)
        self.assertEqual((len(rows2), rows2[0]["decay"]), (1, 0.5))
        rows3, _ = e4.build_signals([], rows1, DET, SCORING, SIG, TODAY + dt.timedelta(days=130), False)
        self.assertEqual(rows3, [])                                       # past retention


class SectorModifierTests(unittest.TestCase):
    def test_modifiers_require_source(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        write_csv(tmp / "e.csv", [
            {"sector": "healthcare", "type": "hiring_intent_survey", "event_date": "2026-10-01", "summary": "s", "source_url": "https://cbk/x"},
            {"sector": "healthcare", "type": "regulatory_trigger", "event_date": "2026-10-01", "summary": "no src", "source_url": ""},
            {"sector": "healthcare", "type": "regulatory_trigger", "event_date": "2026-05-01", "summary": "old", "source_url": "https://x", "expires_days": "90"},
            {"sector": "healthcare", "type": "bogus", "event_date": "2026-10-01", "summary": "", "source_url": "https://x"},
        ], ["sector", "type", "event_date", "summary", "source_url", "expires_days"])
        mods = e4.load_sector_modifiers(tmp / "e.csv", SCORING, TODAY)
        self.assertEqual([(m["type"], m["points"], m["active"]) for m in mods],
                         [("hiring_intent_survey", 4, "1"), ("regulatory_trigger", 3, "0")])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data, self.pdir = self.tmp / "data", self.tmp / "private"
        write_csv(self.data / "kenya_companies_master.csv", MASTER,
                  ["company_id", "display_name", "legal_name", "trading_name", "aliases", "sector", "icp_status"])

    def test_end_to_end_with_injected_items(self):
        items = [raw("Twiga Foods raises $8M seed round", "https://n/1"),
                 raw("State Hospital wins tender for equipment", "https://n/2"),
                 raw("Nairobi-based Zuri Labs secures $2M grant", "https://n/3", outlet="TechCabal"),
                 raw("Karen Hospital announces layoffs", "https://n/4")]
        out = e4.run(today=TODAY, data_dir=self.data, pdir=self.pdir, raw_items=items)
        self.assertEqual((out["signals"], out["advisories"], out["candidates"], out["baseline"]), (3, 1, 1, True))
        priv = read_csv(self.pdir / "hiring_signals.csv")
        self.assertTrue(all(r["label"] == "signal-based prioritization" for r in priv))
        self.assertTrue(all(r["source_url"] for r in priv))
        self.assertTrue(any("pitch" in r["suggested_outreach_angle"] for r in priv))
        # live summary excludes the public-sector company; public files contain no outreach angles
        summ = read_csv(self.data / "signals_summary.csv")
        self.assertEqual(sum(int(r["count"]) for r in summ), 2)
        public = "".join(p.read_text(encoding="utf-8") for p in self.data.glob("*.csv"))
        self.assertNotIn("pitch", public)
        self.assertEqual(read_csv(self.data / "changes.csv"), [])                # baseline: no change events
        adv = read_csv(self.pdir / "advisory_flags.csv")
        self.assertEqual(adv[0]["suggested_angle"], "HR Consulting / outplacement")

        # second run: a genuinely new event produces a change row; old ones do not
        e4.run(today=TODAY + dt.timedelta(days=1), data_dir=self.data, pdir=self.pdir,
               raw_items=items + [raw("Karen Hospital expands to Kisumu", "https://n/7", "2026-10-22")])
        ch = read_csv(self.data / "changes.csv")
        self.assertEqual([(c["company"], c["change_type"]) for c in ch], [("Karen Hospital Ltd", "new_signal")])

    def test_default_config_fetches_nothing(self):
        out = e4.run(today=TODAY, data_dir=self.data, pdir=self.pdir, dry_run=True,
                     client=make_client({}, self.tmp / "c")[0])
        self.assertEqual(out["signals"], 0)

    def test_fetch_through_polite_client(self):
        cfg = self.tmp / "config"
        shutil.copytree(CONFIG_DIR, cfg)
        sources = yaml.safe_load((cfg / "sources.yaml").read_text(encoding="utf-8"))
        for s in sources["engine4_signals"]["sources"]:
            if s["id"] == "gnews_rss":
                s.update(enabled=True, tos_reviewed_by="tester", tos_reviewed_on="2026-10-22")
        (cfg / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
        urls = e4.google_news_urls(SIG)
        routes = {urls[0]: Resp(200, RSS),
                  "https://news.google.com/robots.txt": Resp(200, "User-agent: *\nAllow: /rss/")}
        client, _ = make_client(routes, self.tmp / "cache")
        out = e4.run(today=TODAY, client=client, data_dir=self.data, pdir=self.pdir, config_dir=cfg)
        self.assertEqual((out["signals"], out["baseline"]), (2, True))
        priv = {r["company"]: r for r in read_csv(self.pdir / "hiring_signals.csv")}
        self.assertEqual(priv["Karen Hospital Ltd"]["jobs_stated"], "250")
        self.assertEqual(priv["Twiga Foods Ltd"]["reliability_class"], "major_press")
        log = {(r["source_id"], r["status"]) for r in read_csv(self.data / "scrape_log.csv")}
        self.assertIn(("gnews_rss", "ok"), log)

    def test_robots_disallow_marks_source_blocked(self):
        cfg = self.tmp / "config"
        shutil.copytree(CONFIG_DIR, cfg)
        sources = yaml.safe_load((cfg / "sources.yaml").read_text(encoding="utf-8"))
        for s in sources["engine4_signals"]["sources"]:
            if s["id"] == "gnews_rss":
                s.update(enabled=True, tos_reviewed_by="tester", tos_reviewed_on="2026-10-22")
        (cfg / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")
        client, _ = make_client({"https://news.google.com/robots.txt": Resp(200, "User-agent: *\nDisallow: /")}, self.tmp / "cache")
        out = e4.run(today=TODAY, client=client, data_dir=self.data, pdir=self.pdir, config_dir=cfg)
        self.assertEqual(out["signals"], 0)
        log = {(r["source_id"], r["status"]) for r in read_csv(self.data / "scrape_log.csv")}
        self.assertIn(("gnews_rss", "blocked"), log)


if __name__ == "__main__":
    unittest.main()
