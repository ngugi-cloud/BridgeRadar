# Bridge Lead Radar

A free, automatic sales-lead finder for **Bridge Talent Management**, covering Kenya only.

Every morning it looks at public information: company job postings, public company lists and business news. From
that it builds a ranked list of companies that may need Bridge's services (Recruitment, Outsourcing, HR
Consulting, Payroll, Compliance). The list lands in your Google Sheet, with an email when there are new **Hot**
leads. A public web page shows the overall picture: counts only, never personal data.

> **Signal-based prioritization.** The scores are informed guesses from public information, not predictions.
> Always check the source link before contacting anyone.

---

## How it works

```
 every day ~05:17 Nairobi time (GitHub Actions, free)

 1 Jobs ─────────► who is hiring (company career pages)
 2 Companies ────► one clean list of Kenyan companies
 3 Contacts ─────► published business contacts on company websites   (OFF until legal review)
 4 Signals ──────► funding / expansion / contract news               (OFF until sources reviewed)
 5 Scoring ──────► each lead gets a score out of 100 and Hot / Warm / Cold
 6 Dashboard ────► public web page, aggregates only

 private results ──► private repo "bridge-lead-data" ──► your Google Sheet (07:00) + email
 public results ───► this repo's data/ and docs/      ──► dashboard website
 your Sheet edits ─► private repo (03:00) ──► used by the next day's run
```

**Score (0–100)** = trigger (40) + momentum (20) + company fit (20) + how reachable they are (20). The
`Why_This_Score` column in the Sheet shows the full sum for every lead.

| Temperature | Rule | Action |
|---|---|---|
| **Hot** | 65+, a fresh trigger (job ≤ 14 days or news ≤ 30 days), **and** a way to contact them | Outreach within 48 hours |
| **Warm** | 40–64, or would be Hot but has no contact route yet (flagged "find contact") | Weekly nurture |
| **Cold** | below 40 | Monthly review |

All the numbers live in `config/scoring.yaml`. Change the numbers there, not the code.

---

## Where things are

| Folder / file | What it is |
|---|---|
| `config/` | **All settings.** Sources and their review status (`sources.yaml`), scores (`scoring.yaml`), sectors and ideal client profile (`sector_map.yaml`), contact rules (`contacts.yaml`), news rules (`signals.yaml`), your own lists (`imports.yaml`, `career_pages.csv`, `sector_events.csv`) |
| `scraper/` | The code |
| `data/` | **Public** results (job postings, company list, counts). Updated daily by the workflow |
| `docs/` | The public dashboard website |
| `apps-script/` | The Google Sheet script, plus [`apps-script/SETUP.md`](apps-script/SETUP.md) (Sheet setup) |
| `.github/` | The daily workflow, plus [`.github/SETUP_ACTIONS.md`](.github/SETUP_ACTIONS.md) (GitHub setup) |
| `tests/` | Automatic checks that run before every daily run |
| [`DATA_SOURCES.md`](DATA_SOURCES.md) | Every source, whether it is on, and why |
| [`COMPLIANCE.md`](COMPLIANCE.md) | What personal data is handled, how, and what a lawyer must confirm |
| [`LIMITATIONS.md`](LIMITATIONS.md) | What this tool cannot do. Read it before relying on it |
| [`SETUP_GUIDE.md`](SETUP_GUIDE.md) | **Start here:** step-by-step setup on Windows |
| [`HANDOFF.md`](HANDOFF.md) | Technical notes for whoever maintains the code next |

The private repo **bridge-lead-data** holds the contacts, the ranked lead list, your Sheet edits, and your
suppression and client lists. It must stay **private**. The workflow and the Sheet both refuse to run if it isn't.

---

## First-time setup (once)

**Start with [`SETUP_GUIDE.md`](SETUP_GUIDE.md)**: the complete Windows walk-through in 7 steps, with a check
after each. In short:

1. **GitHub:** follow [`.github/SETUP_ACTIONS.md`](.github/SETUP_ACTIONS.md): two repos, a key, a secret,
   permissions, the dashboard, and a first run.
2. **Google Sheet:** follow [`apps-script/SETUP.md`](apps-script/SETUP.md).
3. **Give it something to look at.** On day one every source is off, so the first runs are almost empty. That is
   expected. See *Getting data flowing* below.

## Getting data flowing

Each step is optional and can be done at your own pace.

1. **Add companies to watch.** Open `config/career_pages.csv` and add one row per company:
   `company,careers_url,ats_type,ats_token`, for example `Acme Hospital Ltd,https://acme.co.ke/careers,,`.
   Leave `ats_type` and `ats_token` blank unless the company uses Greenhouse or Lever.
   Then, in `config/sources.yaml`, review **company_ats** (see below) and set it to `enabled: true`.
2. **Add public company lists** (regulators, associations). Download the list as CSV into `data/imports/` and
   describe it in `config/imports.yaml`. That file shows an example.
3. **Turn on news signals.** Review **gnews_rss** (Google News) in `config/sources.yaml`.
4. **Contacts (Engine 3): only after legal advice.** See [`COMPLIANCE.md`](COMPLIANCE.md).

### How to review and switch on a source (about 15 minutes each)

1. Open the site's `/robots.txt` in your browser, e.g. `https://example.co.ke/robots.txt`. Does it `Disallow`
   the pages you need?
2. Open the site's Terms of Use. Search the page for *scrape*, *robot*, *crawl*, *automated* and *data mining*.
   If any of these is prohibited, the source stays **manual**.
3. If all is clear, edit that source in `config/sources.yaml`: put your name in `tos_reviewed_by`, today's date in
   `tos_reviewed_on`, and set `enabled: true`.
4. Save and commit (see below). The next daily run uses it.

The code double-checks robots.txt on every run. If a site later blocks the tool, it stops by itself and the source
appears under **Manual_Sources** in the Sheet.

---

## Running it on your own computer (optional)

You only need this to test a change before committing it. The daily run happens on GitHub without your computer.

**One-time install**

1. Install **Python 3.12** from python.org. On Windows, tick **"Add python.exe to PATH"** in the first
   installer screen.
2. *(Optional, for the Sheet-script tests)* Install **Node.js LTS** from nodejs.org.
3. In **VS Code**: **File → Open Folder…** → choose the `bridge-lead-radar` folder.
4. **Terminal → New Terminal**, then type:

   ```
   pip install -r requirements.txt
   ```

**Every time**

```
python -m unittest discover -s tests -t .      # the checks: should end with "OK"
python -m scraper.run_all --dry-run            # a full run that writes nothing
python -m scraper.run_all                      # a real run (writes data/ and private_data/)
```

On your computer, private results go into a `private_data` folder inside the project. Git ignores that folder,
so it is never uploaded. Don't move it, and don't rename the `.gitignore` file.

**Saving a change to GitHub (VS Code):** left bar → **Source Control** icon → type a short message → **Commit**
→ **Sync Changes**. The **CI** check on GitHub then runs the tests and the privacy check. If either fails, the
commit shows a red ✗.

---

## Day to day

- **Morning:** open the Sheet → **Priority_Queue** tab. Work the **Hot** rows first, and set **Status** as you go
  (New / Contacted / Meeting booked / Won / Lost).
- **Someone asks not to be contacted:** select their row → **Bridge Radar → Suppress the selected row**. Then follow
  the deletion steps in [`COMPLIANCE.md`](COMPLIANCE.md).
- **A company becomes a client:** select the row → **Bridge Radar → Mark selected company as a client**.
- **Monthly:** compare outcomes (Won / Lost / Meeting booked) with scores, and adjust `config/scoring.yaml` if
  Hot leads aren't converting.

## When something breaks

- **GitHub** emails you, and an issue called **"Daily pipeline failed"** opens in this repo.
- **The Sheet** emails you if the data didn't arrive. It never imports stale data.
- **The dashboard** shows a yellow banner if the data is 2+ days old.

Open the failed run, click the red step, copy the error text and send it to whoever maintains the code.
