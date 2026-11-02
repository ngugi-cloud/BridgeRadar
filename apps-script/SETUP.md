# Google Sheet setup — Bridge Lead Radar

About 20 minutes. You need:

- Your personal Google account (the Sheet lives there).
- The two GitHub repos already created: public `BridgeRadar` and **private** `bridge-lead-data`.
- The pipeline must have run at least once, so there is data to import. If it hasn't, setup still works and the
  tabs stay empty until the first run.

What the script does each day (Nairobi time):

| Time (approx.) | What happens |
|---|---|
| 03:00–04:00 | Your edits (Status, Notes, Suppression, Clients) are sent to the private repo |
| ~05:00 | GitHub runs the pipeline (set up in a later step) |
| 07:00–08:00 | New data is imported into the Sheet. If you have new Hot leads, you get one email |

If the data isn't ready by 07:00, the script retries every 30 minutes (4 tries). If it still isn't ready, it emails
you and leaves the Sheet unchanged.

---

## Part A — Create the GitHub key (token)

The script uses this key to read the data and to send your edits back. It can only touch the private data repo.

1. Go to **github.com** and sign in.
2. Click your **profile picture** (top right) → **Settings**.
3. In the left menu, scroll to the bottom → **Developer settings**.
4. **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
5. Fill in:
   - **Token name:** `Bridge Lead Radar Sheet`
   - **Expiration:** 1 year. Put a reminder in your calendar 11 months from today to make a new one.
   - **Resource owner:** your account.
   - **Repository access:** **Only select repositories** → choose **bridge-lead-data**.
   - **Permissions** → **Repository permissions** → **Contents** → **Read and write**.
     (GitHub adds "Metadata: Read-only" by itself. Leave everything else as **No access**.)
6. Click **Generate token**, then **copy** it. It starts with `github_pat_`.
   GitHub shows it only once. Keep it somewhere safe until Part C. Never paste it into the Sheet, an email or
   chat.

> The key does not need access to the public repo. Every GitHub key can already read public repos.

## Part B — Create the Sheet and paste the script

1. Go to **sheets.new**. A blank Sheet opens. Click **Untitled spreadsheet** (top left) and name it
   `Bridge Lead Radar`.
2. Menu **Extensions** → **Apps Script**. A new browser tab opens with a file called `Code.gs`.
3. Click inside the code area, press **Ctrl+A** (Mac: **Cmd+A**), then **Delete**, so it is empty.
4. Open `apps-script/Code.gs` from the project in VS Code, press **Ctrl+A** → **Ctrl+C**, and paste it
   (**Ctrl+V**) into the Apps Script tab.
5. Click the **Save** icon (floppy disk) or press **Ctrl+S**.
6. At the top left, click **Untitled project** and rename it `Bridge Lead Radar`.

## Part C — Run first-time setup

1. Go back to the **Sheet** browser tab and **reload the page** (F5).
2. Wait about 10 seconds. A new menu, **Bridge Radar**, appears to the right of **Help**.
3. **Bridge Radar** → **1. First-time setup**.
4. Google asks for permission. This is normal for a script you wrote yourself:
   - **Continue** → choose your Google account.
   - If you see **"Google hasn't verified this app"**: click **Advanced** → **Go to Bridge Lead Radar (unsafe)**.
   - **Allow**.

   It asks for: this spreadsheet only, connecting to an external service (GitHub), sending email as you (the
   digest), and running on a schedule.
5. Click **Bridge Radar** → **1. First-time setup** again. The first click only grants permission.
6. Answer the questions:
   - **GitHub username:** your username, exactly as it appears in your repo addresses.
   - **Public code repo name:** press OK to accept `BridgeRadar`.
   - **PRIVATE data repo name:** press OK to accept `bridge-lead-data`.
   - **Token:** paste the key from Part A.
   - **Owner name:** the name to show in the Owner column, for example your name.
7. The script checks that it can see both repos and that the data repo **is private**. If the data repo is
   public, setup stops and nothing is written.
8. When it says **Setup done**, click **Yes** to import now.

**Check it worked:**

- New tabs appear: Priority_Queue, Active_Job_Leads, Hiring_Signals, HR_Contacts, Kenya_Companies_Master,
  Advisory_Flags, Signal_Candidates, Manual_Sources, Manual_Contact_Check, Scrape_Log, Suppression and Clients.
- In the Apps Script tab, click the **clock icon** (Triggers) on the left. You should see two daily triggers:
  `dailyImport` and `writeBack`.
- **Bridge Radar** → **Send me a test digest**. An email should arrive within a minute. Check spam the first time.

You can delete the empty default tab, **Sheet1**.

---

## Daily use

- **Yellow column headers are yours:** Owner, Status, Last_contacted, Notes and Outcome. The pipeline never
  overwrites them.
- **Other columns come from the pipeline.** If you edit one, Google warns you, and your change is replaced at the
  next import.
- **Status** has a dropdown: New / Contacted / Meeting booked / Won / Lost. A lead you mark **Won** or **Lost** is
  frozen as "Closed" and is never re-scored.
- **To stop contacting a person or company:** click any cell in that row, then **Bridge Radar** →
  **Suppress the selected row**. On HR_Contacts this suppresses the email address. On other tabs it suppresses the
  company.
- **To record a client:** select the row → **Bridge Radar** → **Mark selected company as a client**. Clients never
  appear as leads.
- **Your edits reach the pipeline at about 03:00.** If you want them in sooner, use **Bridge Radar** →
  **Send my edits to the pipeline now**.
- **Rows that leave the data:** if you worked on a lead (changed Status, wrote Notes, and so on) and its job closes
  or its signal expires, the row stays for 180 days. Its Sync_State column reads `removed <date>`. Rows you never
  touched simply disappear.
  **Exception: HR_Contacts.** Contacts the pipeline deletes, for example after 12 months without re-verification,
  are removed from the Sheet too. That is a privacy rule.
- **Backups:** before every import, the script copies all your own columns into a hidden tab named
  `Backup_YYYY-MM-DD`. It keeps the last 7. To see them: right-click any tab name → **Show sheet**.
  GitHub also keeps every version of the files sent back.

**Privacy:** this Sheet contains business contact details. Share it only with people who need it. Never use
"Anyone with the link".

---

## Troubleshooting

| You see | Do this |
|---|---|
| No **Bridge Radar** menu | Reload the Sheet. Check the code was saved (Part B, step 5). |
| "Cannot see the private repo (HTTP 404)" | The key does not include `bridge-lead-data`, or a name is misspelt. Fix the setting (below) or make a new key. |
| "HTTP 401" | The key has expired or was copied wrongly. Make a new key (Part A) and update `GITHUB_TOKEN` (below). |
| "STOPPED: … is not private" | On GitHub, open the repo → **Settings** → bottom of page → **Change visibility** → **Private**. |
| Email "no fresh data today" | The pipeline didn't run. Open the public repo on GitHub → **Actions** tab and look for a red ✗. Once it's fixed, use **Import latest data now**. |
| "Write-back skipped … tab is empty" | You emptied the Suppression or Clients tab. To protect the list, the script refuses to overwrite it. Undo in the Sheet (**Ctrl+Z**, or **File → Version history**). |
| Anything else | Apps Script tab → **Executions** (list icon on the left) → click the failed run → copy the red error and send it to me. |

**To change a setting later:** Apps Script tab → **Project Settings** (gear icon) → **Script Properties** →
**Edit script properties**.

| Property | Meaning | Default |
|---|---|---|
| `GITHUB_OWNER` | your GitHub username | — |
| `PUBLIC_REPO` / `PRIVATE_REPO` | repo names | `BridgeRadar` / `bridge-lead-data` |
| `GITHUB_TOKEN` | the key from Part A | — |
| `DEFAULT_OWNER` | Owner column for new rows | `Head of BD` |
| `DIGEST_TO` *(optional)* | send the digest to another address | your Google account |
| `BRANCH` *(optional)* | git branch | `main` |
| `PRIVATE_DATA_PATH` *(optional)* | folder inside the private repo | repo root |
| `PUBLIC_DATA_PATH` *(optional)* | folder inside the public repo | `data` |

**Usage limits:** the script sends 1–2 emails a day and runs for a few minutes. That should be far below
Google's free-account limits. These limits were not checked for this project, so confirm them on Google's
Apps Script quotas page.
