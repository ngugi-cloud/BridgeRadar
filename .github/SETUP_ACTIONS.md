# GitHub setup for the daily pipeline

About 15 minutes, done once. You need both repos already on GitHub:

- **bridge-lead-radar** (public): the code, public data and dashboard.
- **bridge-lead-data** (private): contacts, the lead list and your Sheet edits.

## 1. Make a key for the pipeline

Use a **different** key from the Google Sheet's. Then either one can be cancelled without breaking the other.

1. github.com → your **profile picture** → **Settings** → **Developer settings** → **Personal access tokens** →
   **Fine-grained tokens** → **Generate new token**.
2. **Name:** `Bridge Lead Radar pipeline`. **Expiration:** 1 year. Add a calendar reminder for 11 months from now.
3. **Repository access:** **Only select repositories** → **bridge-lead-data**.
4. **Repository permissions** → **Contents: Read and write**. Leave everything else as **No access**.
5. **Generate token** → **copy** it. GitHub shows it only once.

## 2. Give the key to the public repo (as a secret)

1. Open **bridge-lead-radar** on GitHub → **Settings** tab → left menu **Secrets and variables** → **Actions**.
2. **New repository secret** → **Name:** `PRIVATE_REPO_TOKEN` → **Secret:** paste the key → **Add secret**.

GitHub stores secrets encrypted. They never appear in logs or in the public repo.

*Only if your data repo is not named `bridge-lead-data`:* on the same page, open the **Variables** tab →
**New repository variable** → Name `PRIVATE_REPO`, Value `yourusername/your-data-repo-name`.

## 3. Let the workflow save data

1. **bridge-lead-radar** → **Settings** → **Actions** → **General**.
2. Scroll to **Workflow permissions** → choose **Read and write permissions** → **Save**.

## 4. Turn on the dashboard website

1. **bridge-lead-radar** → **Settings** → **Pages** (left menu).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**. (Not "Deploy from a branch".)

## 5. First run by hand

1. **bridge-lead-radar** → **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. On the left, click **Daily pipeline** → **Run workflow** (right side) → **Run workflow**.
3. Wait 2–5 minutes. Click the run to watch it. A green ✓ means it worked.
4. Your dashboard address is shown in the run's **pages** box. It looks like
   `https://yourusername.github.io/bridge-lead-radar/`.

From then on it runs by itself every day at about **05:17 Nairobi time**. GitHub sometimes starts scheduled runs
up to an hour late.

## If something goes wrong

- **A red ✗:** GitHub emails you, and an issue called **"Daily pipeline failed"** opens in the repo with a link to
  the run. Open the run, click the red step, copy the error text and send it to me. The issue never contains data,
  because the repo is public.
- **"Secret PRIVATE_REPO_TOKEN is missing"**: repeat step 2.
- **"… is not private"**: make the data repo private (data repo → Settings → bottom of page → Change visibility).
- **Push failed / 403:** repeat step 3, or the key has expired (repeat steps 1–2 with a new key).
- **"PRIVACY CHECK FAILED"**: the run refused to publish because a private file or column was about to go public.
  Nothing was published. Send me the message.
- **Runs stopped after a quiet period:** GitHub pauses scheduled workflows after 60 days without repo activity.
  The daily data commit normally counts as activity. If it happens, Actions tab → **Daily pipeline** →
  **Enable workflow**.

A second check, **CI**, runs on every change to the code. It runs the tests and refuses any private file that was
committed by mistake.
