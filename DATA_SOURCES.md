# Data sources

The authoritative list is `config/sources.yaml`. This page explains it in plain language. **On day one every
external source is OFF.** A source runs only when all of these are true:

1. `enabled: true`.
2. Its access is automatic (API, RSS feed, web page or ATS feed), not `manual`.
3. A person has read its Terms of Use and robots.txt, and filled in `tos_reviewed_by` and `tos_reviewed_on`.
4. Its robots.txt allows the page at the moment of the run. This is checked automatically every run, and the
   tool fails closed: if robots.txt can't be read cleanly, the site is treated as "disallowed".
5. Any key it needs is set as a GitHub secret.

If any of these fails, the source is skipped, logged in `data/scrape_log.csv`, and (for manual sources) listed
in `data/manual_integration_required.csv` and the Sheet's **Manual_Sources** tab. Nothing is ever "scraped anyway".

**How the tool behaves:** honest User-Agent `BridgeLeadRadar/1.0 (+https://bridgetalentgroup.com; contact:
info@dymz.co.ke)`, at least 2 seconds between requests to the same site, a request budget per source, and it stops
on repeated refusals (403/429). No logins, CAPTCHAs, paywalls or proxy rotation. Job ads and news articles are
read in memory only. What's stored is structured facts, a headline, our own one-line summary and the link,
never the article or ad text (copyright).

> **Check:** the contact address in the User-Agent (`info@dymz.co.ke`) is not on the bridgetalentgroup.com
> domain. Site owners who want the tool to stop will write to it, so make sure someone reads that mailbox, or
> change it in `config/sources.yaml`.

## Status key

| Status | Meaning |
|---|---|
| **approved** | An official API, feed or open data. Still needs a human ToS review before switching on |
| **pending review** | Probably fine. A person must read its ToS and robots.txt first (about 15 minutes) |
| **manual only** | Terms prohibit automation, or there is no reliable access. Never scraped. Check by hand |

The first-pass ToS findings below are **unverified**, except where marked *read*. They must be confirmed by the
person filling in `tos_reviewed_by`.

## Engine 1: job postings

| Source | Status | Notes |
|---|---|---|
| Company career pages and ATS feeds (`company_ats`) | pending review | **Main source.** Only companies you list in `config/career_pages.csv` (or that are in the master list with a careers URL). Reads schema.org JobPosting data, plus Greenhouse/Lever public job feeds. robots.txt checked per site. Pages built entirely by JavaScript yield nothing |
| BrighterMonday | **manual only** | *Read:* terms allow personal, non-commercial use only and prohibit robots, scraping and data mining without written consent |
| LinkedIn jobs | **manual only** | Treated as prohibited from general knowledge of LinkedIn's terms (not re-read). Never automated |
| Pigiame | manual only | Classifieds with fake-listing risk |
| Fuzu | manual only | ToS not found on first pass. Manual until read |
| MyJobMag, Talent.com, KenyaMoja, CareerPointKenya, Advance-Africa | pending review | **No code built yet.** An adapter is written only after you review and approve the site |
| Jooble publisher API | pending review | Free key needed. Kenya coverage unconfirmed. No adapter yet |
| ReliefWeb jobs API | approved, **off** | *Read:* free API. Needs a pre-approved `appname` (secret `RELIEFWEB_APPNAME`). Mostly NGO jobs, which are outside the ideal client profile. Field names unverified until the first live run |

## Engine 2: company list

| Source | Status | Notes |
|---|---|---|
| Employers from Engine 1 | on (internal) | Companies seen hiring. Agencies go to the competitor list; confidential postings are skipped |
| Your seed list (`config/career_pages.csv`) | on (internal) | Maintained by you |
| Public lists you download (`config/imports.yaml`) | per list | **Preferred route for regulator and association lists.** Save the CSV into `data/imports/`, add `source_url` and your review name/date. Size is kept only with a stated `size_source` |
| Kenya Master Health Facility List, CBK licensed institutions, IRA/CMA lists, KenInvest, Wikidata | approved, off | Use the import route. Wikidata is deliberately not used (it skews to large firms) |
| KMPDC/PPB, EPRA, NSE, KAM, EPZA, AFA/KFC/FPEAK/Tea Board, TRA, Communications Authority, FKE/KEPSA, Kenya Gazette | pending review | Use the import route after review |
| NEA licensed recruitment agencies | pending review | Feeds the **competitor** list: these companies are never leads |
| Business Registration Service | manual only | Search by hand |

## Engine 3: contacts (personal data; see COMPLIANCE.md)

| Source | Status | Notes |
|---|---|---|
| Company's own website (team, leadership, contact and careers pages) | pending review, **off** | **Do not enable until the legal questions in COMPLIANCE.md are answered.** Up to 6 pages per company, company-domain emails only, landlines only, and person emails only when printed next to that person's name and title |
| Web search API | pending review, off | Not built |
| HR leads named in press articles | listed as on, **not built** | Needs Engine 4 integration. Does nothing today |
| LinkedIn company pages | **manual only** | Never automated. The Sheet's **Manual_Contact_Check** tab gives a LinkedIn search link to open by hand |

## Engine 4: news signals

| Source | Status | Notes |
|---|---|---|
| Google News RSS (`gnews_rss`) | approved, off | Main feed: about 15 Kenya searches over the last 30 days. **robots.txt not checked yet.** The tool checks it every run and records "blocked" if disallowed. If so, use direct outlet feeds |
| Google Alerts RSS | approved, off | Add your own alert feed URLs to `alert_feeds` in `config/signals.yaml` |
| Kenya News Agency, TechCabal, Disrupt Africa, Capital FM, Standard Media, The Kenya Times | approved, on | Direct RSS feeds configured in `config/signals.yaml` and compliance-gated by runtime robots.txt checks |
| KenInvest, Business Daily, The Star, Kenyan Wallstreet, NewsTrendsKE | approved / pending, off | No reviewed working feed or no approved automated access confirmed |
| World Bank procurement awards | approved, off | No adapter yet |
| S&P Global, CBK/sector hiring surveys | manual only | Add notable surveys or regulatory changes by hand to `config/sector_events.csv` (a source URL is required) |

Outlet reliability classes (government / major press / tracker) in `config/signals.yaml` are judgement calls, not
measurements.

## Not built on purpose

- Scrapers for job boards nobody has reviewed yet.
- Regulator-site scrapers (use imports instead).
- Anything on LinkedIn.
- Logins, CAPTCHAs or paywalls.
- Guessed email addresses, and SMTP mailbox probing.
