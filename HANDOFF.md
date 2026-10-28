# HANDOFF — Bridge Lead Radar

Written 2026-10-23, updated 2026-10-23 after sections 7.1–7.6 (7.6 = guide written; owner has not run it yet), for the next Claude (or developer) taking over. It is self-contained: you do not
need the earlier conversation. Read sections 1–4 before touching anything.

---

## 0. Kickoff prompt (paste this into the new Claude together with the project files)

> You are taking over a half-built project called **Bridge Lead Radar** for Bridge Talent Management
> (Kenya HR solutions firm). Read `HANDOFF.md` fully, then `config/*.yaml`, then skim `scraper/`.
> Run `python -m unittest discover -s tests -t .` and confirm it passes (expect 126 tests, all OK; needs Node.js for the Apps Script tests, otherwise 4 are skipped)
> before changing anything. Follow the non-negotiable rules in section 2. Work one section at a time,
> and stop for my "next" after each. All build sections are done. The owner (Windows, nothing installed yet) will follow `SETUP_GUIDE.md` later.
> Help them through it one step at a time, fix errors they paste, then help review sources (section 10).
> If anything in `HANDOFF.md` contradicts the code, trust the code and tell me.

---

## 1. What this is

**Owner:** Head of Business Development, Bridge Talent Management (https://bridgetalentgroup.com),
a Kenya/East Africa HR firm selling Recruitment, Outsourcing, HR Consulting, Payroll, Compliance.
Core verticals: Healthcare, Tech & E-commerce, Manufacturing, Agriculture.

**Goal:** a daily, unattended pipeline (GitHub Actions) that produces ranked sales leads, shown in a
Google Sheet (via Apps Script) and a free static GitHub Pages dashboard. Modelled on an earlier
project, `impactradar` (github.com/countryfarmke-netizen/impactradar): public repo, `docs/` served by
Pages, `apps-script/`, workflow in `.github/workflows/`. Only its top-level layout was ever visible;
its `daily.yml` and `scrape.py` were never seen, so the workflow uses the standard pattern.

**Working constraints from the owner**
- **Zero budget.** Free tools only: public GitHub repo + Pages, free private repo, personal Gmail/Sheets.
- **Kenya only.** No Uganda/Tanzania/Rwanda.
- **Not a developer.** Plain language, click-by-click setup steps, and one section per message with a
  checkpoint ("next") before continuing.
- **No CRM.** Google Sheets is the CRM for now. Single user (owner), so one `Owner` value.
- **Setup will be done by the owner in VS Code + GitHub.** The assistant cannot operate their accounts;
  it must write instructions they can follow, and fix errors they paste back.

## 2. Non-negotiable rules (do not relax these without the owner's explicit say-so)

1. **robots.txt and ToS first.** The client fails closed. A source runs only if `gate()` passes:
   `enabled`, not manual, `tos_reviewed_by` and `tos_reviewed_on` filled, no placeholder User-Agent,
   required secrets present. Blocked or unreviewed sources go to the manual list, never "scraped anyway".
2. **No fabrication.** Blank beats invented. Every row carries its source URL. Dates are never guessed.
   Company size is stored only with a `size_source`.
3. **No personal-data creep.** Own-website, own-domain business contacts only; no personal-mail domains;
   person-style emails only when inside the same page block as the person's name + title; landlines only
   (mobiles dropped); no guessed emails; no SMTP probing; no LinkedIn scraping; no logins/CAPTCHAs/paywalls.
4. **Private data never goes in the public repo.** Contacts, suppression list, client list, outreach angles,
   and the ranked lead queue live in the private folder/repo. A test already checks the public data folder
   for leaked names/numbers; extend it for any new output.
5. **"Signal-based prioritization."** Hiring signals are heuristics, labelled as such. Never claim certainty.
6. **No copyrighted text stored.** Headline + our own summary + link only.
7. **Competitors and exclusions.** NEA-licensed recruitment agencies, public sector, NGOs, suppressed
   entries and existing clients are never queued as leads.
8. **Tests first.** Every section shipped with offline unit tests, and the tests have caught real bugs
   (see section 9). Keep adding them.

## 3. Decisions log (made with the owner)

| Decision | Choice |
|---|---|
| Contact data location | **Option A:** public code repo + **private** `bridge-lead-data` repo for personal data |
| Dashboard | Public GitHub Pages serving `/docs`, **aggregates only** (no contacts, no ranked leads, no angles). Deployed by the workflow (Pages source = **GitHub Actions**, not "Deploy from branch", so the push-by-bot rebuild question never arises) |
| Temperature | **Hot / Warm / Cold applies to LEADS, not companies** |
| ICP | Private sector, **10–250 employees**, Kenya; large multinationals **included but flagged**; healthcare = private facilities Level 3+ |
| Public sector, NGOs | Excluded from leads by default |
| Layoffs/freezes | Tracked as an **advisory flag only** (HR Consulting / outplacement angle); never changes scores |
| Phase 1 sources | Only approved/reviewed sources run; everything else disabled until the owner reviews it |
| Repo ownership | Owner's **personal** GitHub; personal Google account |
| Folder names | `docs/` (dashboard) and `apps-script/` (hyphen), matching impactradar |
| Run cadence | Engines 1 and 4 daily; Engines 2 and 3 on a rolling crawl budget (150 companies/day) |
| GitHub token for the Sheet | **Fine-grained, only `bridge-lead-data`, Contents: Read and write.** Write is needed for the write-back (the original plan said read-only, which cannot send edits back). The script refuses to write to any other repo and checks the repo is private first |
| Private repo layout | CSV files at the **root** of `bridge-lead-data` (Script Property `PRIVATE_DATA_PATH`, default empty). Section 7.4 must push them there, plus `meta.json` |
| Daily timing (EAT) | 03:00 Sheet write-back → ~05:00 pipeline → 07:00 Sheet import + digest |
| Contact email in User-Agent | `info@dymz.co.ke` (differs from the bridgetalentgroup.com domain; may confuse site owners) |

## 4. Architecture

```
Engine 1 jobs  ──► data/active_jobs.csv ─┐
Engine 2 master ◄─ employers(E1) + imports + seeds ──► data/kenya_companies_master.csv
Engine 3 contacts ◄─ master ──► private/hr_contacts.csv (+ master.contact_status only)
Engine 4 signals ◄─ master ──► private/hiring_signals.csv (+ public aggregates)
                         └──► scoring.py: leads + Hot/Warm/Cold ──► private/priority_queue.csv (+ public leads_summary.csv)
run_all.py runs all five in order ──► meta.json (public + private copy)
Apps Script reads the private repo ──► Google Sheet      Dashboard reads public aggregates only
```

Run order must be sequential: **E1 → E2 → E3 → E4 → scoring.** E3 rewrites the master but changes only
`contact_status`.

**Public (`data/`):** `active_jobs.csv`, `closed_jobs.csv`, `kenya_companies_master.csv`,
`competitor_agencies.csv`, `needs_review.csv`, `changes.csv`, `scrape_log.csv`,
`manual_integration_required.csv`, `signals_summary.csv`, `sector_modifiers.csv`,
`signal_company_candidates.csv`, `leads_summary.csv` (counts by sector x temperature), `meta.json`.
**Private (`PRIVATE_DATA_DIR`, default `private_data/`, git-ignored locally):** `hr_contacts.csv`,
`crawl_state.csv`, `manual_contact_check.csv`, `hiring_signals.csv`, `advisory_flags.csv`,
`suppression.csv` (owner-maintained), `clients.csv` (owner-maintained), `priority_queue.csv`, `meta.json`,
and `lead_status.csv` (to be written by Apps Script: Sheet edits flowing back, see 7.2).

### File map

| Path | Purpose |
|---|---|
| `config/sources.yaml` | Every source, access method, compliance status, the ToS gate and its review instructions |
| `config/scoring.yaml` | **All** weights: lead score, signal points, decay, reliability, Hot/Warm/Cold, exclusions |
| `config/sector_map.yaml` | ICP definition, sector keywords, role families, seniority, overrides |
| `config/contacts.yaml` | Engine 3 rules: page keywords, title regexes, buys-mapping, retention, crawl budget |
| `config/signals.yaml` | Engine 4: Google News queries, feed slots, outlet classes, signal regexes, angle templates |
| `config/imports.yaml` | Public lists the owner downloads manually, mapped by column |
| `config/career_pages.csv` | Seed list of company career pages (`company,careers_url,ats_type,ats_token`) |
| `config/sector_events.csv` | Manual sector-wide events (surveys, regulatory changes); source URL required |
| `scraper/common/` | `config.py` (gate), `http.py` (polite client), `textutil.py`, `classify.py`, `entities.py` (resolution), `contacts_extract.py`, `store.py` |
| `scraper/engine1_jobs.py` … `engine4_signals.py` | The four engines; each runs as `python -m scraper.engineN_… [--dry-run]` |
| `scraper/scoring.py` | Builds and ranks leads → private `priority_queue.csv`; public `leads_summary.csv` |
| `scraper/run_all.py` | E1→E2→E3→E4→scoring, failure isolation, `meta.json`, log trimming; exit 1 if a step failed |
| `apps-script/Code.gs` | Google Sheet sync: import/upsert, write-back, digest, menu. Pure logic is Node-testable |
| `apps-script/SETUP.md` | Click-by-click Sheet setup for the owner |
| `apps-script/tests/logic.test.js` | 15 Node tests (`node --test apps-script/tests/logic.test.js`) |
| `scraper/dashboard.py` | Builds PUBLIC `docs/data/dashboard.json` (aggregates only); called by `run_all.py` after the steps |
| `docs/index.html` | Static dashboard (GitHub Pages from `/docs`), no external scripts, light/dark/auto theme |
| `docs/.nojekyll` | Stops GitHub Pages running Jekyll on `/docs` |
| `scraper/check_public.py` | Privacy guard: blocks private files/columns and emails/phones under `docs/` from the public repo |
| `.github/workflows/daily.yml` | Daily run 02:17 UTC: tests → pipeline → private push → guarded public push → Pages → failure issue |
| `.github/workflows/ci.yml` | On every push: privacy guard on all tracked files + tests |
| `.github/SETUP_ACTIONS.md` | Click-by-click: pipeline token, secret, permissions, Pages source, first run |
| `README.md`, `DATA_SOURCES.md`, `COMPLIANCE.md`, `LIMITATIONS.md` | Owner-facing docs (plain language). `tests/test_docs.py` checks links and that every source in `sources.yaml` is described |
| `SETUP_GUIDE.md` | **Owner's start point:** Windows setup in 7 steps, each with a ✅ check |
| `tests/` | 126 offline tests (no network): `python -m unittest discover -s tests -t .` (runs the Node tests too) |

### Key design behaviours worth knowing
- **Stable IDs:** `job_id` (hash of normalised company+title+location), `company_id` (kept across re-runs),
  `contact_id`, `signal_id`. Sheet upserts must key on these.
- **Baseline runs:** the first run marks everything as baseline so "new today" and velocity are not
  misleading for about 30 days.
- **Entity resolution (E2):** exact name, same website domain, or similarity ≥ 0.95 merge; similar or
  containing names go to `needs_review.csv` and are **not** merged.
- **Signals (E4):** one article gives at most one signal type; the same company+type within 14 days is
  one event however many outlets; corroboration raises the reliability multiplier (capped 1.2).
- **Retention:** contacts 12 months; closed jobs 60 days; signals 120 days.

## 5. Status

| Section | State |
|---|---|
| Configs (sources, scoring, sector_map, contacts, signals, imports) | Done |
| Engine 1 – jobs | Done, tested offline. Only the **company career page / ATS** adapter (+ ReliefWeb, off) exists |
| Engine 2 – companies | Done, tested offline. Works from E1 employers, seed list, **CSV imports**, simple HTML tables |
| Engine 3 – contacts | Done, tested offline. **Disabled** until owner reviews and enables `company_site` |
| Engine 4 – signals | Done, tested offline. All sources disabled until reviewed; Google News RSS is the main feed |
| Lead scoring + `run_all.py` | Done, tested offline (22 tests in `tests/test_scoring.py`) |
| Apps Script (`apps-script/`) | Done; logic tested offline in Node + a Sheet→pipeline round-trip test. **Never run inside Google** |
| Dashboard (`docs/`) | Done; data builder tested (6 tests incl. a privacy test); page checked in Chromium (light, dark, 390px) with throwaway demo data |
| GitHub Actions (`.github/`) | Done; actionlint-clean; git steps rehearsed locally with two bare repos (concurrent Sheet push rebased fine; guard blocked a planted private file). **Never run on GitHub** |
| Docs, README | Done. **COMPLIANCE.md is a draft**: legal points written from general knowledge, web search was unavailable, so nothing was checked against the current Act/regulations |

**Nothing has ever run against a live website.** The sandbox could not reach them. All 126 tests use
fixtures and fake HTTP. Expect first-run surprises.

## 6. Assumptions and unverified items (treat as open)

- **ToS/robots first pass:** only BrighterMonday (terms forbid scraping, so Manual) and ReliefWeb
  (free API, appname required) were actually read. Fuzu, MyJobMag, Talent.com, KenyaMoja,
  CareerPointKenya, Advance-Africa, regulators and news sites are **unconfirmed** and disabled.
  LinkedIn is treated as manual from general knowledge, not re-read.
- **Google News RSS** (`news.google.com/rss/search`): robots.txt not checked. The runner checks it every run
  and records "blocked" if disallowed. If so, use `feeds:` / `alert_feeds:` in `signals.yaml`.
- **ReliefWeb v2 field names** follow a reading of the docs; verify on first live run.
- **Sector scoring:** target 8 / adjacent 4 / other 1 (only "target = 8" was given). SME ceiling 250 and the
  job-numbers bonus (+1/+2/+3 at 50/200/500 jobs) were agreed as defaults.
- **Facts not verified and to be checked with a lawyer/regulator before relying on them:** Kenya Data
  Protection Act 2019 obligations (ODPC registration, notification, opt-out wording, cross-border transfer),
  MSE Act size thresholds, Apps Script/Gmail quotas, and whether Pages on private repos is restricted.
- **Outlet reliability classes** in `signals.yaml` are judgement calls, not measurements.
- **"KIICO-type deals"** from the original brief was never identified; treated as generic investment deals.

## 7. Remaining work (in this order; one section per message, wait for "next")

1. ✅ **DONE 2026-10-23.** **`scraper/scoring.py` + `scraper/run_all.py`.** Build leads from the engines and rank them. Spec is in
   `config/scoring.yaml` and below.
   - Lead types: `job_lead` (each open job), `signal_lead` (each live signal), `contact_change_lead`
     (a `change_detected = new_hr_head` contact, 60-day shelf life). Companies get **no** temperature.
   - Score = trigger (≤40) + momentum (≤20) + account fit (≤20) + reachability (≤20). Hot ≥ 65 **and** a
     fresh trigger (job ≤ 14 days or signal ≤ 30 days) **and** reachability ≥ 5; Warm 40–64 (a would-be Hot
     with no contact route is capped at Warm and flagged "find contact"); Cold < 40.
   - Lifecycle: job lead drops a tier after 30 days without a new trigger; signal leads expire at 90 days;
     a new trigger re-scores. Group several Hot leads per company under one `Primary_Lead`.
   - Exclusions: statuses Won/Lost, suppression, clients, competitor agencies, public sector, NGOs. The
     advisory track never affects scores.
   - Output `Why_This_Score` showing every component. Columns: `Lead_ID, Lead_Type, Company, Temperature,
     Score, Why_This_Score, Suggested_Angle, Primary_Lead, Owner, Status, Last_contacted, Notes, Outcome,
     Trigger_Type`. Write to the **private** folder only.
   - `run_all.py` runs E1 → E2 → E3 → E4 → scoring, isolates failures per engine, writes `meta.json`
     (last-refreshed time) and keeps `changes.csv` / `scrape_log.csv` from growing forever (both do today).
2. ✅ **DONE 2026-10-23.** **Apps Script (`apps-script/Code.gs`).** Tabs: Active_Job_Leads, Kenya_Companies_Master, HR_Contacts,
   Hiring_Signals, Priority_Queue, Scrape_Log (plus Signal_Candidates, Manual_Sources, Suppression,
   Clients). Shared columns Owner, Status (New / Contacted / Meeting booked / Won / Lost), Last_contacted,
   Notes. **Upsert by stable ID and never overwrite human-entered columns** (protected ranges + daily
   backup tab). Read the private repo with a read-only token kept in Script Properties. Time trigger about
   07:00 EAT (the workflow runs earlier); read `meta.json` and retry if stale. Daily email digest of new Hot
   leads. Provide click-by-click setup (Sheet → Extensions → Apps Script → paste → trigger → authorize).
   Suppression/client edits in the Sheet must flow back so the pipeline honours them.
3. ✅ **DONE 2026-10-23.** **Dashboard (`docs/`).** Static, aggregates only: total active jobs, new today, top hiring companies,
   leads by sector and temperature counts, searchable company table (contact **status** only). Same
   look as impactradar where possible. Must follow the light/dark theme conventions in this environment.
4. ✅ **DONE 2026-10-23.** **`.github/workflows/daily.yml`.** Cron in UTC (about 02:00 UTC for 05:00 EAT; cron can drift 10–60
   minutes). Check out the public repo and the private repo (token in Secrets), set `PRIVATE_DATA_DIR`, run
   `run_all.py`, commit public `data/` and `docs/`, push private data to the private repo, deploy Pages.
   Add a keepalive (scheduled workflows can be disabled after inactivity), a failure notification, and a
   CI check that fails if any private path is staged in the public repo. Secrets: `PRIVATE_REPO_TOKEN`,
   optional `RELIEFWEB_APPNAME`, `JOOBLE_API_KEY`, `SEARCH_API_KEY`.
5. ✅ **DONE 2026-10-23.** **Docs:** `README.md` (non-developer: run locally, deploy), `DATA_SOURCES.md`, `COMPLIANCE.md`
   (what is collected, lawful basis = legitimate business interest, retention, deletion/opt-out process,
   named contact; flag that a lawyer must review), `LIMITATIONS.md` (contacts partial and must be verified;
   signals are heuristics; no personal data beyond published business contacts; some sources manual-only).
6. ✅ **GUIDE WRITTEN 2026-10-23 (`SETUP_GUIDE.md`); owner will do it later.** **Setup with the owner** (VS Code + GitHub): create the two repos, install Python, `pip install -r
   requirements.txt`, run tests, add Secrets, enable Pages (`main` / `/docs`), first manual workflow run,
   Actions read/write permission. The owner is not a developer. Every step must be click-by-click.

**Not built on purpose:** scrapers for MyJobMag, Talent.com, KenyaMoja etc. (add one only after the owner
reviews that site's ToS), regulator-site scrapers (use `imports.yaml` instead), press-article HR contacts
(needs E4 integration), LinkedIn anything, Wikidata (skews to large firms).

### Scoring as built (section 7.1): behaviour the next sections depend on
- **Lead_IDs are stable:** `J-<job_id>`, `S-<signal_id>`, `C-<contact_id>`. Apps Script must upsert on `Lead_ID`.
- **Queue columns:** the 14 agreed columns first, then `Action, Flags, Sector, Company_ID, Trigger_Date, Source_URL,
  Hot_Since, First_Seen, Last_Seen, Label` (Source_URL is mandatory by rule 2; the rest drive lifecycle and the Sheet).
- **Human columns** `Owner, Status, Last_contacted, Notes, Outcome` are never overwritten. Precedence: `lead_status.csv`
  (Sheet write-back, columns `Lead_ID` + those five; a blank there wins, so clearing a note in the Sheet works) >
  previous `priority_queue.csv` > defaults (`Owner` = `queue.default_owner`, `Status` = New).
- **Won / Lost** leads are frozen as Temperature `Closed`. Other leads at a Won company get the flag
  "company already Won: add to clients.csv?" (it is not automatic).
- **Leads that vanish** (job closed, signal expired) are dropped, unless the owner touched them (status, notes,
  last contacted, outcome): then they stay as `Expired` for 180 days so notes are not lost.
- Sort order: Hot, Warm, Cold, Expired, Closed; then score.
- Interpretations made while building (all in `config/scoring.yaml`, confirm with the owner):
  - Contact-change leads count as a "fresh trigger" for 30 days (`contact_change_lead_max_days`).
  - "Hiring velocity up" needs 2+ new postings this week (more than last week), so one fresh posting does not
    earn momentum on top of its freshness bonus.
  - Sector modifiers (surveys, regulatory triggers) add to **momentum**, each type at most once.
  - Jobs first seen on the baseline run **without** a posted date get no freshness bonus and cannot be Hot
    ("age unknown"), because the age would be a guess.
  - Signals about companies not in the master list are included but flagged "verify company" and capped at Warm
    (`queue.unmatched_signals`, or set to `exclude`).
  - Level 1–2 facilities and micro firms are excluded like Engine 3 does; agency and confidential job postings are
    excluded (no buyer).
  - A signal row's stale `icp_status` is ignored; the master's current status is used (fixes a section 8 issue).
- `meta.json`: public copy holds counts and the exception **type** only; the private copy also holds the error text.

### Apps Script as built (section 7.2): behaviour the next sections depend on
- Reads public files from `<PUBLIC_REPO>/data/` and private files from the root of `<PRIVATE_REPO>`, through the
  GitHub Contents API with the token (no raw.githubusercontent caching).
- **Freshness:** imports only if the private `meta.json` `run_date` equals today's date in Nairobi and the public
  `meta.json` matches. `run_all.py` uses the runner's date (UTC), which equals the Nairobi date only between 00:00 and
  21:00 UTC, so **the workflow must finish inside that window; 02:00 UTC is fine**. If stale, it retries
  every 30 minutes, 4 times, then emails a warning and leaves the Sheet unchanged.
- **Upsert:** keyed per tab (`Lead_ID`, `job_id`, `signal_id`, `contact_id`, `company_id`, `source_url`,
  `source_id`). Human columns in the Sheet always win. New rows take the CSV's values (Priority_Queue carries
  them) or defaults. Pipeline columns are written as plain text (IDs like `12e45` stay text). Text starting with
  `= + - @` gets an apostrophe (formula-injection guard).
- **Rows that leave the data:** kept with `Sync_State = removed <date>` for 180 days if touched; otherwise dropped.
  **HR_Contacts never keeps removed rows** (privacy: retention and opt-out deletions must reach the Sheet).
- **Write-back:** Priority_Queue → `lead_status.csv`; Suppression tab → `suppression.csv` (`type,value,reason,added_on`);
  Clients tab → `clients.csv` (`name,company_id,since,notes`). It commits only when content changed, and refuses to
  replace a non-empty file with an empty one.
- Scoring now also ignores contacts whose email or domain is suppressed (so a suppression works even on days
  Engine 3 does not run).
- **Digest:** sent only when there are new Hot leads (`Hot_Since == run_date`), Hot leads untouched 7+ days, or
  failed pipeline steps. It never includes contact names or emails.
- **Backups:** hidden `Backup_YYYY-MM-DD` tabs (last 7) with every human column plus the Suppression and Clients rows.
- **Permissions:** `@OnlyCurrentDoc`, so the script can touch this one Sheet only. No manifest file is needed.
- **Verify on the first live run** (could not be tested outside Google): how the apostrophe guard displays on
  text-formatted cells; that warning-only protections and dropdown validation look right; trigger hours;
  Apps Script / Gmail free quotas.

### Dashboard as built (section 7.3)
- `run_all.py` writes `docs/data/dashboard.json` after all steps (as a pseudo-step `dashboard` in `meta.json`).
  **Section 7.4 must commit `docs/data/dashboard.json`** with the public data. Until it exists the page shows a
  "no data published yet" state. No sample data is shipped (no fabrication).
- Contents: KPIs (active postings, new today, companies hiring, companies tracked, companies with a contact route,
  live signals), postings by sector (core verticals marked), top 10 hiring companies (agency/confidential
  excluded), new postings per day (30 days, baseline rows excluded), leads by sector × temperature (counts only),
  signals by type, latest status per source, and a searchable/sortable company table with **contact status only**.
- Privacy: built from the public `data/` folder only. `tests/test_dashboard.py` fails if an email, phone number,
  lead ID, score, angle or private column appears, or if a new top-level key is added without review.
- Banners: data 2+ days old, failed steps, and the first-30-days baseline caveat.
- Look: impactradar's styling was never visible, so this uses a plain neutral layout and the validated dataviz
  palette (blue single series; Cold/Warm/Hot as a 3-step blue ramp, checked light and dark).
- Not done: no per-company lead temperature (by design); no historical trend beyond 30 days.

### Workflow as built (section 7.4)
- Schedule `17 2 * * *` (05:17 EAT) + manual "Run workflow". Concurrency group stops overlapping runs.
- Order: secret present → checkout public → checkout private into `_private/` (git-ignored) → **refuse if the data
  repo is not private** → Python 3.12 + Node 22 → install → **tests** (stop if red) → `run_all` (continue on error;
  `PRIVATE_DATA_DIR` set on this step only, so tests can never touch real data) → commit/push private (`pull
  --rebase`, 3 tries; the Sheet only writes files the pipeline never writes) → `git add data docs` →
  **`check_public --staged`** → commit/push public → upload `docs/` → final step fails the job if a pipeline step
  failed → `pages` job deploys if the artifact was uploaded → `notify` job opens/comments a `pipeline-failure`
  issue (link only, no data).
- Secrets: `PRIVATE_REPO_TOKEN` (required, separate from the Sheet's token), optional `RELIEFWEB_APPNAME`,
  `JOOBLE_API_KEY`, `SEARCH_API_KEY`. Optional repo variable `PRIVATE_REPO`.
- Keepalive: relies on the daily commit of `data/meta.json` (timestamp always changes). No third-party action.
- Owner setup steps: `.github/SETUP_ACTIONS.md`.
- **Verify on the first live run:** Pages deploy with Source = GitHub Actions; that `notify` fires as expected;
  run time; that `cache/` growing in the runner is harmless (not committed).

### Docs as built (section 7.5)
- `COMPLIANCE.md`: what is collected (table), safeguards, lawful basis, the opt-out and deletion procedure, data
  location, and 9 questions for a lawyer/ODPC, plus a go-live checklist for Engine 3. The data-protection contact
  is left as **[TO FILL]** (never invented).
  **Main legal risk flagged:** the Act's rule on commercial use / direct marketing may require consent for
  outreach to *named* contacts. Until answered, keep Engine 3 off or limit it to role inboxes.
- **Known erasure gap:** deleted contacts survive in the private repo's git history and the Sheet's version
  history. A full erasure needs a history squash. **Not automated**; consider a manual/periodic squash script
  if the lawyer requires it.
- Added the **Advisory_Flags** Sheet tab (private `advisory_flags.csv`, Notes column only). Before this,
  advisories never reached the Sheet.

### Setup guide (section 7.6)
- `SETUP_GUIDE.md` (Windows): install VS Code → Git (editor = VS Code, branch = main) → Python (Add to PATH);
  GitHub account + 2FA + "keep email private" (noreply address for commits, because the code repo is public);
  create **private** `bridge-lead-data` **with a README** (checkout needs a commit); publish the code from VS Code
  as **public** `bridge-lead-radar`; local tests; `.github/SETUP_ACTIONS.md`; `apps-script/SETUP.md`; first
  companies (`company_ats` review + `config/career_pages.csv`).
- **Windows fixes made while writing it:** all test file I/O is now explicitly UTF-8 (Windows Python defaults to
  cp1252; checked with `-X warn_default_encoding`, 0 warnings). Privacy-check messages are ASCII-only. Every
  source in `sources.yaml` now has a `tos_reviewed_by` field (33 entries were missing it, so the documented
  review steps could not be followed literally). Tests cover both.
- Not verified on a real Windows machine.

## 8. Known limitations and rough edges

- JavaScript-rendered career pages and Cloudflare-obfuscated emails yield nothing (by design for the latter).
- E3 name detection is a heuristic and can misread title-case phrases as names; every row needs human check.
- E4 regexes will produce false positives; unmatched-company extraction is crude (`signal_company_candidates.csv`
  is for human review and is **not** auto-added to the master).
- Rows kept from earlier runs in `hiring_signals.csv` keep their old `icp_status` until refreshed (scoring ignores it and uses the master).
- `needs_review.csv` is rewritten every run and lists every low-confidence sector row, so it will be long.
- `changes.csv` (90 days) and `scrape_log.csv` (60 days) are trimmed by `run_all.py`; running an engine on its own does not trim.
- No CRM export path yet. A Sheet export is the migration route if a CRM is adopted.
- Sector classification is keyword-based; use `overrides` in `sector_map.yaml` to correct it.

## 9. Lessons from testing (do not reintroduce)

- `dateutil.parse(..., dayfirst=True)` reads ISO `2026-12-01` as 12 January. ISO dates are parsed first.
- `\b` cannot sit before `$` in a regex; "raises $5M" was missed until the funding pattern was fixed.
- Fuzzy similarity alone missed branch-style names ("X Hospital" vs "X Hospital Nairobi"); a containment
  rule now sends these to review.
- Normalise names with `normalize_company` on both sides of any comparison (a test failed by comparing
  a raw name to a normalised one).

## 10. Open questions for the owner

1. Who reads the ToS for each "Verify" source (about 15 minutes each) and fills `tos_reviewed_by/on`?
2. Which job board should get the first adapter?
3. Has a lawyer or the ODPC been consulted before `company_site` (Engine 3) is enabled? (Questions are ready in COMPLIANCE.md §7.)
   Who is the named data-protection contact (COMPLIANCE.md §1)? Should the User-Agent email `info@dymz.co.ke` change?
4. Confirm the SME ceiling (250), adjacent/other sector scores (4 / 1), and the Hot threshold (65).
   Also confirm the scoring interpretations listed in section 4 (contact-change freshness 30 days, velocity
   needs 2+ postings, unmatched signals capped at Warm, default Owner text).
5. Will the owner supply `clients.csv` and `suppression.csv`, and a first `career_pages.csv`?
6. Move the repo to an organisation account later? (Currently the owner's personal account.)

## 11. How to run

```
pip install -r requirements.txt
python -m unittest discover -s tests -t .          # 126 tests, all offline (Node needed for 4)
python -m scraper.run_all [--dry-run] [--only engine4_signals,scoring]   # the daily pipeline
python -m scraper.scoring --dry-run                # scoring only
python -m scraper.engine1_jobs --dry-run           # also engine2_companies, engine3_contacts, engine4_signals
export PRIVATE_DATA_DIR=/path/to/private           # default: ./private_data (git-ignored)
```

With the shipped config every source is disabled or manual, so a first run produces empty files and a
log explaining why. To get data flowing: add companies to `config/career_pages.csv` and enable
`company_ats`; drop reviewed public lists into `data/imports/` and describe them in `config/imports.yaml`;
review and enable the news sources; then enable `company_site` for contacts.
