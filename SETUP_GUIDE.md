# Setup guide (Windows): from zero to a daily lead list

Do this at your own pace. Each step ends with a **✅ check**, so you know it worked before moving on. If a
check fails, copy the exact error text and ask for help. Don't skip ahead: later steps depend on earlier ones.

| Step | What | Time |
|---|---|---|
| 1 | Install VS Code, Git, Python | 15 min |
| 2 | GitHub account and the **private** data repo | 10 min |
| 3 | Put the code on GitHub from VS Code (creates the **public** repo) | 10 min |
| 4 | Run the checks on your computer | 5 min |
| 5 | GitHub settings and the first automatic run | 15 min |
| 6 | Google Sheet | 20 min |
| 7 | Give it its first companies to watch | 30 min+ |

---

## Step 1: Install the tools

Install them **in this order**.

**VS Code**

1. Go to **code.visualstudio.com** → **Download for Windows** → open the file.
2. Accept → **Next**. On **Select Additional Tasks**, tick both **"Open with Code"** boxes and keep **Add to PATH**
   ticked → **Install** → **Finish**.

**Git**

1. Go to **git-scm.com** → **Download for Windows** → **Click here to download** → open the file.
2. Click **Next** on every screen, except these two:
   - **Default editor:** choose **Use Visual Studio Code as Git's default editor**.
   - **Initial branch name:** choose **Override the default branch name** and make sure it says `main`.
3. **Install** → **Finish**.

**Python**

1. Go to **python.org** → **Downloads** → yellow **Download Python 3.x** button. Version 3.12 or newer.
2. On the first screen, **tick "Add python.exe to PATH"** at the bottom. This is the box that matters.
3. **Install Now** → if offered, click **Disable path length limit** → **Close**.

**✅ Check:** open VS Code → menu **Terminal → New Terminal**, then type:

```
git --version
python --version
```

You should see `git version 2…` and `Python 3.12…` (or newer).
*If `python` opens the Microsoft Store or says "not found":* try `py --version`. If that works, type `py`
wherever this guide says `python`.

---

## Step 2: GitHub account and the private data repo

1. Go to **github.com**. **Sign up** if you don't have an account. Use your work email; GitHub won't show it
   publicly if you do part 3 below. Choose a username you're happy for clients to see: it appears in the
   dashboard's web address.
2. **Turn on two-factor authentication:** profile picture (top right) → **Settings** → **Password and
   authentication** → **Enable two-factor authentication**. Follow the steps. An authenticator app is best.
   This protects the contact data.
3. **Hide your email in public commits:** **Settings** → **Emails** → tick **Keep my email addresses private**.
   Underneath, GitHub shows an address like `12345678+yourname@users.noreply.github.com`. **Copy it**; you
   need it in step 3.
4. **Create the private data repo:** the **+** at the top right → **New repository**.
   - **Repository name:** `bridge-lead-data`
   - Choose **Private**. *(Important.)*
   - **Tick "Add a README file".** The pipeline needs the repo to already contain one file.
   - **Create repository**.

**✅ Check:** the repo page shows a 🔒 **Private** label next to `bridge-lead-data`.

---

## Step 3: Put the code on GitHub (this creates the public repo)

1. **Unzip** `bridge-lead-radar.zip`: right-click → **Extract All…** → choose your **Documents** folder →
   **Extract**. You now have `Documents\bridge-lead-radar`. Make sure there's no second folder of the same name
   inside it. If there is, use the inner one.
2. In **VS Code**: **File → Open Folder…** → choose `Documents\bridge-lead-radar` → **Select Folder**. If asked
   **"Do you trust the authors?"**, click **Yes, I trust the authors**.
3. Tell Git who you are, once only. In **Terminal → New Terminal** type these two lines, using your own name and
   the *noreply* address from step 2.3:

   ```
   git config --global user.name "Your Name"
   git config --global user.email "12345678+yourname@users.noreply.github.com"
   ```
4. In the left bar, click the **Source Control** icon (three dots joined by lines) → **Publish to GitHub**.
   - A browser opens: **Allow** VS Code to sign in to GitHub → back in VS Code.
   - Choose **Publish to GitHub public repository**. The name should be `bridge-lead-radar`.
   - VS Code may ask which files to include: **keep everything ticked** → **OK**.
5. Wait for **"Successfully published"** → click **Open on GitHub**.

> Why public? The free dashboard website needs a public repo. The public repo only ever contains code and
> non-personal data. Contacts and the lead list live in the private repo, and an automatic check blocks them
> from the public one.

**✅ Check:** on GitHub, `bridge-lead-radar` shows **Public** and has folders including `.github`, `apps-script`,
`config`, `docs`, `scraper` and `tests`. A **CI** check may start running (a yellow dot, then a green ✓ beside
the latest commit).

---

## Step 4: Run the checks on your computer

In the VS Code terminal, still inside the `bridge-lead-radar` folder:

```
python -m pip install -r requirements.txt
python -m unittest discover -s tests -t .
```

**✅ Check:** the last line says **`OK`**, perhaps followed by something like `(skipped=4)`. The skipped checks
need Node.js, which is optional on your computer; GitHub runs them for you.

Optional: `python -m scraper.run_all --dry-run` runs the whole pipeline without saving anything. On day one it
reports that every source is off. That is expected.

---

## Step 5: GitHub settings and the first automatic run

Follow **[.github/SETUP_ACTIONS.md](.github/SETUP_ACTIONS.md)** from top to bottom. In short:

1. Create a GitHub key for the pipeline: only `bridge-lead-data`, **Contents: Read and write**.
2. Save it as a secret named `PRIVATE_REPO_TOKEN` in the **public** repo.
3. **Settings → Actions → General → Workflow permissions → Read and write**.
4. **Settings → Pages → Source: GitHub Actions**.
5. **Actions** tab → **Daily pipeline** → **Run workflow**.

**✅ Check:**

- The run turns green ✓.
- `bridge-lead-data` now contains files such as `meta.json` and `priority_queue.csv`.
- The dashboard opens at `https://<your-username>.github.io/bridge-lead-radar/`. It will be mostly zeros: no
  sources are switched on yet.

---

## Step 6: Google Sheet

Follow **[apps-script/SETUP.md](apps-script/SETUP.md)**. In short:

1. Make a **second** GitHub key (same settings as step 5). This one is for the Sheet.
2. **sheets.new** → name it **Bridge Lead Radar**.
3. **Extensions → Apps Script** → paste the whole of `apps-script/Code.gs` (open it in VS Code, Ctrl+A, Ctrl+C)
   → **Save**.
4. Reload the Sheet → **Bridge Radar** menu → **1. First-time setup** → allow permissions → run it again → answer
   the questions.

**✅ Check:**

- The tabs appear.
- The Apps Script **Triggers** page (clock icon) shows `dailyImport` and `writeBack`.
- **Bridge Radar → Send me a test digest** delivers an email.

---

## Step 7: Give it its first companies to watch

On day one the radar has nothing to look at. Start small.

1. **Review the company career-page source** (it reads companies' own job pages):
   - In VS Code open `config/sources.yaml` and find `id: company_ats`.
   - Set `tos_reviewed_by: "Your Name"`, `tos_reviewed_on: "2026-10-23"` (today's date) and `enabled: true`.
2. **Add 10–20 target companies** to `config/career_pages.csv`, one per line under the header:

   ```
   company,careers_url,ats_type,ats_token
   Example Hospital Ltd,https://www.example-hospital.co.ke/careers,,
   ```
   Use companies in your core sectors, each with the page where they list vacancies. Leave the last two columns
   empty.
3. **Save** both files → **Source Control** → message `Add first companies` → **Commit** → **Sync Changes**.
4. **Actions** tab → **Daily pipeline** → **Run workflow**. When it's green: open the Sheet → **Bridge Radar →
   Import latest data now**.

**✅ Check:** jobs appear in **Active_Job_Leads**, companies in **Kenya_Companies_Master**, and leads in
**Priority_Queue**. Most will be Warm or Cold, flagged "find contact", because contacts (Engine 3) stay **off**
until the legal questions in [COMPLIANCE.md](COMPLIANCE.md) are answered.

From then on it runs every morning by itself.

**When you're ready:**

- **News signals:** review `gnews_rss` the same way (see [DATA_SOURCES.md](DATA_SOURCES.md)).
- **Public company lists:** use `config/imports.yaml`.
- **Contacts:** only after legal advice.

---

## Before relying on it

- Read **[LIMITATIONS.md](LIMITATIONS.md)** (what it can't do) and **[COMPLIANCE.md](COMPLIANCE.md)**.
  Fill in the data-protection contact in COMPLIANCE.md §1.
- Put calendar reminders in 11 months' time to renew **both** GitHub keys.
- Never share the Google Sheet by public link.
