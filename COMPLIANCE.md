# Compliance and personal data

> **Status: DRAFT, not legal advice.** Written by the developer (an AI assistant) from general knowledge. The
> legal points below were **not checked against the current texts** of Kenya's Data Protection Act 2019 or its
> regulations. A Kenyan data-protection lawyer, or guidance from the Office of the Data Protection Commissioner
> (ODPC), must confirm them **before Engine 3 (contacts) is switched on and before any outreach uses contact data
> from this tool.** Section 7 lists the questions to ask.

## 1. Data protection contact

| | |
|---|---|
| Responsible person | **[TO FILL: name, role]** |
| Email for privacy requests | **[TO FILL: a monitored address]** |
| Organisation | Bridge Talent Management, https://bridgetalentgroup.com |

Fill these in before go-live. The same address should appear in Bridge's privacy notice. Consider whether the
tool's User-Agent contact (`info@dymz.co.ke`, in `config/sources.yaml`) should be changed to match.

## 2. What the tool collects

| Data | Personal? | Where it is stored | Public? | Kept for |
|---|---|---|---|---|
| Job postings: title, company, location, dates, link | No (company facts) | `data/active_jobs.csv`, `closed_jobs.csv` | Yes | Open jobs while listed; closed jobs 60 days |
| Company list: name, sector, location, website, licence number, size band (only with a source), contact **status** | No | `data/kenya_companies_master.csv` | Yes | Until removed from source lists |
| News signals: headline, our own summary, link, outlet | No (company events) | private `hiring_signals.csv`; public counts only | Counts only | 120 days |
| **Business contacts** (Engine 3, **off**): name, job title, work email on the company domain, company landline, page it was published on | **Yes** | private `hr_contacts.csv` | **Never** | Removed if not re-confirmed within 12 months, or immediately on opt-out |
| Lead list with scores and outreach angles | Indirectly (may relate to a named contact) | private `priority_queue.csv` → Google Sheet | **Never** | While the trigger is live; touched leads 180 days after it expires |
| Your notes, statuses, suppression and client lists | May contain personal data | private repo + Google Sheet | **Never** | Until you delete them |
| Public dashboard | No: counts and company facts only | `docs/` | Yes | Replaced daily |

**Never collected, enforced in code:**

- mobile numbers (landlines only);
- personal email domains (Gmail, Yahoo, etc.);
- guessed email addresses;
- SMTP mailbox probing (only a DNS check that the company's email domain exists);
- anything behind a login, CAPTCHA or paywall;
- anything from LinkedIn;
- article or job-ad text;
- sensitive personal data;
- data about job seekers.

## 3. Safeguards built into the tool

- **Public vs private split.** Personal data is written only to the private repo `bridge-lead-data` and the Google
  Sheet. Three checks enforce this:
  - the tests;
  - a privacy check that runs before every public commit;
  - a second check on every code change.

  All three block any private file, private column, email address or phone number in the public repo or website.
- **Private repo checks.** Both the workflow and the Sheet refuse to run if the data repo is not private.
- **Suppression list.** The Suppression tab in the Sheet, or `suppression.csv`, covers emails, domains, company
  names and company IDs:
  - suppressed contacts are never stored again;
  - suppressed companies never become leads;
  - existing clients and recruitment agencies are excluded too.
- **Retention is automatic** (see the table above).
- **Narrow access keys.** Each GitHub key can reach the private data repo only. The Sheet script can reach only
  its own spreadsheet.
- **Honest crawling.** The tool identifies itself with a contact address, obeys robots.txt and is rate-limited.

## 4. Lawful basis (to confirm)

The intended basis is **legitimate interest**: Bridge contacting businesses, through their published business
contacts, about HR services they may need. This is a business-to-business use, and contacts are limited to people
who published their details in a professional capacity. A short written **legitimate-interest assessment** should
be kept on file. It covers:

- the purpose;
- why it is necessary;
- the balance with individuals' interests;
- the safeguards above.

**Main open risk:** as far as I know, the Act has a specific rule on using personal data for **commercial
purposes**, including direct marketing. It may require **consent** (or another stated condition) rather than
legitimate interest, and the regulations add conditions for direct marketing, such as an easy, free opt-out in
every message. If that applies to B2B outreach to named HR staff, then either:

- outreach to *named* contacts needs consent first (for example, a first message to the company's general or
  HR inbox only), or
- Engine 3 should stay limited to role inboxes (`hr@`, `careers@`) and switchboards.

**This is the first question for the lawyer.**

## 5. Telling people and handling requests

**Every outreach message** to a contact found by this tool should say:

- who Bridge is;
- where the contact details came from ("your company's website, [page]");
- how to opt out in one step ("reply STOP" or a link).

The source page is stored with every contact.

**When someone asks to opt out, or to be deleted:**

1. In the Sheet, select their row → **Bridge Radar → Suppress the selected row** (this suppresses their email).
   If they want the whole company left alone, also add a `company_id` or `name` row to Suppression.
2. **Bridge Radar → Send my edits to the pipeline now.** The next run removes their contact from
   `hr_contacts.csv`, and the Sheet import removes it from HR_Contacts. Removed contacts are never kept in the
   Sheet.
3. Delete any notes you wrote about them, in the Sheet and in your email or CRM.
4. **Known gap:** older versions of files stay in the private repo's **git history**, in the **Sheet's version
   history**, and for up to 7 days in the Sheet's hidden **Backup_** tabs (your notes only). For a full erasure request, the history must be cleaned too. Ask the maintainer to squash the private
   repo's history, and restore or delete the affected Sheet versions. The suppression entry itself must stay, so
   they are never collected again. Record the date the request was completed.
5. **Requests to see their data:** export their row(s) from HR_Contacts and the related Priority_Queue rows, and
   send them. The Act sets a response deadline; confirm it with the lawyer.

## 6. Where the data physically is

- **GitHub** (private repo) and **Google** (Sheet, Apps Script, Gmail digest) store data outside Kenya. The Act
  has rules on transferring personal data abroad. Confirm that these services satisfy them, and whether ODPC
  needs to be told.
- **Accounts:** use personal accounts with **two-factor authentication** on both GitHub and Google.
- **Sharing:** never share the Sheet by public link. Share it only with named people who need it.
- **Keys:** GitHub keys expire after 1 year. Replace them rather than extending their access.
- **Breach:** if the private repo, Sheet or a key leaks, revoke the keys first (GitHub → Settings → Developer
  settings), then get advice at once. The Act has a short deadline for notifying ODPC (to confirm).

## 7. Questions for the lawyer / ODPC

1. Does emailing a **named HR manager** at their company address, found on the company's own website, count as
   "commercial use" or direct marketing that needs **consent**? Or can **legitimate interest** cover it? If
   consent is needed, is contacting the company's role inbox (`hr@`, `info@`) first acceptable?
2. Must Bridge **register with ODPC** as a data controller? If it is already registered, does this purpose
   (business development / lead generation) need to be added?
3. What must every outreach message include (identity, source of data, opt-out wording)? Please provide approved
   wording.
4. Are **GitHub** and **Google** acceptable for storing this data outside Kenya, and on what conditions?
5. Is a 12-month retention for unconfirmed contacts reasonable? Is a 180-day retention for worked leads
   reasonable?
6. For deletion requests: is removal from current files plus suppression enough, or must git and Sheet version
   history also be purged (see §5 step 4)?
7. Response deadlines for access and deletion requests, and the breach-notification deadline.
8. Is publishing the **company list** (names, sectors, websites, whether a contact is on file) on a public
   dashboard acceptable? It contains no personal data.
9. Anything specific to **healthcare** facilities (a target sector) or other regulated sectors?

## 8. Before switching on Engine 3 (contacts)

- [ ] Lawyer / ODPC answers to §7 questions 1–4 received and filed
- [ ] §1 contact details filled in
- [ ] Bridge's website privacy notice mentions this use
- [ ] Legitimate-interest assessment (or consent process) written
- [ ] Outreach template with source + opt-out approved
- [ ] `company_site` reviewed in `config/sources.yaml` (`tos_reviewed_by` / `tos_reviewed_on`) and `enabled: true`
