# Limitations: read before relying on the results

**This tool prioritises; it does not predict.** It turns public clues into a ranked to-do list. Every score is a
heuristic. Every lead must be checked by a person, using the source link on its row, before anyone is contacted.

## Coverage

- **Partial by design.** On day one every external source is off. Results only cover the companies, lists and
  feeds you have reviewed and switched on. Many Kenyan SMEs post jobs only on boards whose terms forbid automated
  collection (BrighterMonday, LinkedIn), so those must be checked by hand.
- **Kenya only.** Postings that name another country are dropped. Remote roles abroad are excluded.
- **JavaScript-only career pages** show nothing to the tool. Companies whose sites hide jobs behind scripts, logins
  or ATS systems other than Greenhouse/Lever may show no postings even when they are hiring.
- **Never tested live.** All the automatic checks use sample data. Expect surprises on the first real runs, and
  read the run log.

## The first 30 days

The first run treats everything it finds as "already there". Until about 30 days have passed, "new today", the
daily trend and the "hiring speeding up" bonus undercount. Jobs found on that first run with no posted date can't
be Hot, because their age is unknown.

## Accuracy

- **Company names and merging.** Exact names, the same website, or 95%+ name similarity are merged automatically.
  Near matches (for example, "X Hospital" vs "X Hospital Nairobi") are listed in `data/needs_review.csv`, not
  merged, so some companies appear twice until you review them.
- **Sector** is guessed from keywords. Low-confidence guesses go to `needs_review.csv`. Correct them with
  `overrides` in `config/sector_map.yaml`.
- **Company size** is shown only when a public source states it. Most companies will say "size unverified". The
  tool never guesses size.
- **News signals** are keyword matches on headlines. Expect false positives (a headline about a different
  company, or a deal that isn't new) and misses. A company named in a headline but not in your list goes to
  `signal_company_candidates.csv` for you to check. It is never added automatically.
- **Layoff and restructuring news** appears in the Sheet's **Advisory_Flags** tab, as an HR consulting /
  outplacement angle. It never changes scores.
- **Recruitment agencies** are detected by name keywords and the NEA list. Some will slip through, and a few real
  employers with words like "recruitment", "staffing", "placement" or "HR consulting" in their name may be
  wrongly treated as agencies.

## Contacts (when Engine 3 is switched on)

- **Incomplete.** Contacts are found only on the company's own website, on up to 6 pages. Many companies publish
  no HR contact at all.
- **Must be verified.** Name detection is a heuristic and can mistake a title-case phrase for a person. Every
  contact row needs a human check before use.
- **Ages.** Confidence drops after 90 days without re-confirmation. Contacts are removed after 12 months.
- **Not collected:** mobile numbers, personal emails, guessed emails, LinkedIn data. See COMPLIANCE.md.

## Scores

- The weights in `config/scoring.yaml` are starting assumptions agreed with the owner, not measured from results.
  Recalibrate monthly using the Outcome column.
- "Reachability" counts only contacts the tool found. A company you already know well can still show "find
  contact".
- Scores don't know about relationships, past conversations or competitors' activity unless you record them
  (Status, Notes, Clients, Suppression).

## Operations

- **Timing.** GitHub may start the daily run up to an hour late. If the data isn't ready by 07:00, the Sheet
  retries until about 09:00, then emails you and keeps yesterday's data.
- **Keys expire** after a year. Both GitHub keys must be renewed, or the run and the Sheet stop.
- **Scheduled runs pause** after 60 days with no repo activity. The daily commit normally prevents this.
- **The Sheet** works best below a few thousand rows per tab. Scrape_Log is capped at 3,000 rows.
- **No CRM.** The Sheet is the CRM for now. To move to a CRM later, export the Priority_Queue and HR_Contacts tabs.
