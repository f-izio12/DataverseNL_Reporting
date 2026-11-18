# Setup guide: DataverseNL reports for your institution

This guide takes you from an empty GitHub account to a working website with two reports on your
institution's research data in DataverseNL. No programming is needed; you edit one settings file and
click through GitHub's settings. Plan about an hour, plus a day for the first access-request data.

If you only want to know what the reports show and how the figures are made, read the
[README](../README.md) instead.

---

## Contents

1. [What you will get](#1-what-you-will-get)
2. [Before you start](#2-before-you-start)
3. [Copy the repository](#3-copy-the-repository)
4. [Describe your institution: institution.json](#4-describe-your-institution-institutionjson)
5. [Add your logo](#5-add-your-logo)
6. [Add the two secrets](#6-add-the-two-secrets)
7. [Allow GitHub to publish the website](#7-allow-github-to-publish-the-website)
8. [First runs](#8-first-runs)
9. [Check the result](#9-check-the-result)
10. [Everyday use](#10-everyday-use)
11. [Changing texts, languages and the schedule](#11-changing-texts-languages-and-the-schedule)
12. [Troubleshooting](#12-troubleshooting)
13. [Running without GitHub](#13-running-without-github)
14. [Updating to a newer version](#14-updating-to-a-newer-version)

---

## 1. What you will get

A website, rebuilt automatically on the 1st of every month, with:

| Page | For | Contents |
|---|---|---|
| **Executive report** (`index.html`) | Management | Headline figures, trends, your faculties compared, open and restricted files, licences, access requests |
| **Admin report** (`admin.html`) | Curators, data stewards | Waiting access requests, unpublished drafts, datasets with unpublished changes, datasets without a licence, detailed tables, method |

Both pages exist in English and Dutch, with a switch at the top. Every run also produces downloads:
the reports as single HTML files and the figures as CSV files for Excel.

A second, daily job records the access-request queue, so that the reports can show how many requests
arrive and how long a decision takes. DataverseNL itself keeps no such history.

## 2. Before you start

You need:

| What | Why | Who can help |
|---|---|---|
| A **GitHub account**, ideally an organisation account of your library | The reports run and are published there, free of charge for public repositories | Your IT department |
| Your **DataverseNL collection alias** | Tells the reports which collection is yours | Visible in the address of your collection: `dataverse.nl/dataverse/<alias>` |
| A **DataverseNL account with the Curator role** on your collections, preferably a team account | For storage, access requests and drafts. Optional: without it you get the public figures only | DANS or your collection admin |
| A **privacy check** | The tracker processes pseudonymised data about who requests access (see the README, Privacy) | Your privacy officer |

About public and private repositories: GitHub Pages is free for public repositories. A public
repository means the code, and the access-request history in `data/access_requests.json`, can be seen
by anyone. The history contains no names or e-mail addresses (see the README), but discuss it with
your privacy officer. If it must stay private, use a paid GitHub plan or run the scripts on your own
server (section 13).

## 3. Copy the repository

**Option A, with Git or GitHub Desktop (recommended).** Create a new, empty repository on GitHub,
then copy all files of this project into it, including the hidden folder `.github`, and push.

**Option B, in the browser only.**

1. On GitHub: **+** (top right) → **New repository**. Give it a name, for example
   `dataversenl-reports`, choose **Public**, and click **Create repository**.
2. Click **uploading an existing file** and drag in all files and folders from the zip.
3. **Check that the folder `.github/workflows` arrived.** Windows and macOS hide folders that start with
   a dot, and browsers sometimes skip them. If it is missing, create the two files by hand:
   **Add file** → **Create new file**, type the name `.github/workflows/dataverse-report.yml`
   (typing the slashes creates the folders), paste the content of that file from the zip, and
   **Commit**. Repeat for `.github/workflows/access-request-tracker.yml`.
4. Do **not** upload a folder called `output` or any file containing a token.

You should now see in the repository: `.github/`, `assets/`, `docs/`, `institution.json`,
`dataverse_report.py`, `report_render.py`, `report_text.py`, `request_tracker.py`,
`requirements.txt`, `README.md` and `LICENSE`.

## 4. Describe your institution: institution.json

This is the only file you have to change. Open it on GitHub, click the pencil icon to edit, change
the values, and click **Commit changes**.

```json
{
  "dataverse_url": "https://dataverse.nl",
  "root_alias": "maastricht",
  "name": {"en": "Maastricht University", "nl": "Universiteit Maastricht"},
  "short_name": "UM",
  "department": {"en": "University Library · Research Data Management",
                 "nl": "Universiteitsbibliotheek · Research Data Management"},
  "logo": "assets/um-logo.png",
  "colours": {"primary": "#001c3d", "accent": "#e84e10"},
  "default_language": "en",
  "timezone": "Europe/Amsterdam",
  "main_units": {"FHML": "FHML", "law": "Law"},
  "main_unit_label": {"en": {"one": "faculty", "many": "faculties"},
                      "nl": {"one": "faculteit", "many": "faculteiten"}},
  "other_short_names": {"UB-UM": "Library"}
}
```

Field by field:

- **`dataverse_url`**: leave as `https://dataverse.nl`.
- **`root_alias`**: your institution's top-level collection. Open your collection in DataverseNL; the
  alias is the last part of the address, for example `https://dataverse.nl/dataverse/maastricht`.
- **`name`**, **`department`**: shown at the top of every page, in English (`en`) and Dutch (`nl`).
- **`short_name`**: a short form of your institution's name.
- **`logo`**: the path to your logo, see section 5.
- **`colours`**: two hex colour codes from your house style. `primary` is used for headings, links and
  the main chart colour; `accent` for the second colour. The dark-mode colours are worked out
  automatically.
- **`default_language`**: `en` or `nl`. That language is at the main address; the other one is in the
  subfolder `/nl/` or `/en/`.
- **`timezone`**: leave as `Europe/Amsterdam`.
- **`main_units`**: the collections directly under your top-level collection that are your main units,
  usually faculties. Write each as `"alias": "short name"`. The short name appears in tables and charts.
  All other collections are shown under "Other collections"; nothing is left out.
- **`main_unit_label`**: what a main unit is called (faculty/faculteit, school/school, and so on), in the
  singular (`one`) and plural (`many`).
- **`other_short_names`**: optional short names for the other collections. Without one, the
  collection's full name is used.

**How to find the aliases of your faculties.** Open this address in your browser, with your own alias
at the end:

```
https://dataverse.nl/api/info/metrics/tree?parentAlias=maastricht
```

It lists every collection under yours, each with its `alias` and `name`.

**Check your file.** JSON is strict: every text in double quotes, a comma between items, no comma after
the last item. If a run fails with "Expecting ',' delimiter" or similar, paste the file into a JSON
checker (search for "JSON validator") to find the mistake.

## 5. Add your logo

1. Prepare a logo as PNG or SVG, about 300 to 600 pixels wide. Smaller files keep the pages fast: the
   logo is built into every page.
2. In the repository, open the folder `assets` → **Add file** → **Upload files**, upload it.
3. Put its path in `institution.json`, for example `"logo": "assets/my-logo.png"`.
4. Delete the Maastricht University logo if you do not need it.

Without a logo the reports still work; the header simply has no image.

## 6. Add the two secrets

Secrets are values GitHub keeps hidden: nobody can read them back, not even repository admins, and
they never appear in logs. The code only contains their names, as placeholders.

Go to **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

**Secret 1: `DATAVERSE_API_TOKEN`** (optional, but recommended)

1. Log in to DataverseNL with the account that has the Curator role.
2. Click your name (top right) → **API Token** → **Create Token**, and copy it.
3. In GitHub, create a secret with the name `DATAVERSE_API_TOKEN` and the token as its value.
4. Write the token's expiry date in your calendar (usually one year).

Without this secret the reports show the public figures only, and the daily tracker skips itself with a
notice instead of failing.

**Secret 2: `REQUESTER_HASH_KEY`** (needed for the tracker)

A long random text that the tracker uses to turn requester identities into codes that cannot be traced
back. Create one with a password manager (40 characters or more), or run:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Create a secret with the name `REQUESTER_HASH_KEY` and this text as its value. **Never change it
afterwards**: with a new key, every waiting request looks new. Store a copy in your team's password
manager.

**Never** put a token in a file, an issue, a chat or an e-mail. If that happens, revoke it in
DataverseNL straight away (API Token → Revoke) and create a new one.

## 7. Allow GitHub to publish the website

1. **Settings** → **Pages** → **Build and deployment** → **Source**: choose **GitHub Actions**.
2. The daily tracker saves its history back to the repository. Its workflow asks GitHub for that
   permission itself, so usually there is nothing to do. In an organisation account, an owner can
   forbid it: if the tracker later fails at "Save history" with "Permission denied" or 403, ask an
   organisation owner to allow workflows to write (organisation **Settings** → **Actions** →
   **General** → **Workflow permissions**).

## 8. First runs

Open the **Actions** tab. If GitHub asks whether to enable workflows, click the green button.

1. Click **Access-request tracker** → **Run workflow** → **Run workflow**. It records the current queue.
   This takes a few minutes, longer for collections with thousands of restricted files. The first run
   marks every request already waiting as "already waiting"; those are left out of the statistics
   because their start date is unknown.
2. Then click **DataverseNL report** → **Run workflow** → **Run workflow**. This takes 5 to 30 minutes,
   depending on the size of your collection.
3. When both show a green tick, open **Settings** → **Pages**: the address of your website is at the top,
   usually `https://<account>.github.io/<repository>/`.

From then on everything runs by itself: the tracker every night, the reports on the 1st of every month.
To refresh the reports sooner, run **DataverseNL report** by hand. After changing a file, always start a
new run; **Re-run jobs** repeats the old code.

## 9. Check the result

Go through this list once:

- [ ] The header shows your name, logo and colours.
- [ ] Under "Faculties compared" all your main units appear, with the right short names, and other
      collections are under "Other collections".
- [ ] "Published datasets" roughly matches what you see in DataverseNL for your collection.
- [ ] "Storage used" shows a figure, not "not available" (needs the token).
- [ ] The admin report shows drafts and waiting requests (needs the token).
- [ ] Under "Datasets your account cannot check" there are few or no entries. Each entry is a collection
      where the token's account lacks the Curator role; ask the collection's admin to add it.
- [ ] The **EN | NL** switch and the **Executive report | Admin report** switch work.
- [ ] Both reports print properly (browser → Print → Save as PDF), if you plan to share them that way.

A note on the first month: access-request figures start on the day of the first tracker run, and the
access-request table fills up as decisions come in.

## 10. Everyday use

- **Monthly**: nothing to do; the website updates on the 1st.
- **Downloads**: Actions → a finished run of **DataverseNL report** → **Artifacts**:
  `executive-report-…` (reports and CSV files, kept 90 days) and `admin-report-…` (admin report with
  draft titles and the work lists as CSV, kept 30 days).
- **Yearly**: renew the API token before it expires, and update the secret.
- **When people change**: the token belongs to an account; if that account is removed, create a new
  token with another curator account.

## 11. Changing texts, languages and the schedule

- **Texts**: every sentence of both reports is in `report_text.py`, English and Dutch side by side.
  Change the words between the quotes; keep anything in `{curly brackets}` (the script fills those in).
- **Schedule**: in `.github/workflows/dataverse-report.yml`, the line `cron: "0 5 1 * *"` means
  "minute 0, hour 5 (UTC), day 1 of every month". The tracker runs at `0 3 * * *`, every day at 03:00
  UTC. Keep the tracker daily; less often makes the time-to-decision figures less precise.
- **Draft titles on the website**: off by default, because titles of never-published datasets are not
  public in DataverseNL. Set `PUBLISH_DRAFT_TITLES: "true"` in `dataverse-report.yml` only if your
  institution agrees.
- **Other settings** are listed in the README under "Other settings".

## 12. Troubleshooting

To see what went wrong: **Actions** → the run with the red cross → the failed step. The last lines
usually say why.

| What you see | Cause | What to do |
|---|---|---|
| Website shows "404, There isn't a GitHub Pages site here" | Pages not set to GitHub Actions, or no successful report run yet | Section 7, then run **DataverseNL report** |
| Website shows the old version after a run | Browser or GitHub cache | Wait a few minutes, then refresh with Ctrl+F5 |
| No **Run workflow** button, or no workflows in the Actions tab | The `.github/workflows` folder is missing or in the wrong place | Section 3, step 3. The files must be exactly in `.github/workflows/` |
| "Settings file institution.json not found" | File missing or renamed | It must be called `institution.json`, in the main folder |
| "Expecting ',' delimiter" or another JSON error | Typing mistake in `institution.json` | Check it with a JSON validator |
| A faculty appears under "Other collections" | Wrong alias in `main_units` | Compare with the tree address in section 4; aliases are case-sensitive |
| "Storage: not available", drafts "not included" | No token, or it expired | Section 6 |
| Tracker run: "Access-request tracker skipped" | No token secret yet | Section 6; this is a notice, not an error |
| Tracker fails: "REQUESTER_HASH_KEY is missing" | Second secret not set | Section 6 |
| Tracker fails at "Save history": "Permission denied" or 403 | An organisation policy stops workflows writing to the repository | Section 7, step 2 |
| "DataverseNL is not responding (HTTP 503)" or a non-data reply | DataverseNL down, in maintenance, or a bot check | Run again later. If it persists, contact DANS |
| Log lines "file downloads for sub-collection … unavailable" | DataverseNL cannot answer for that sub-collection | Harmless: dataset downloads still count; report it to DANS if you like |
| Scheduled runs stopped after two months | GitHub pauses schedules in inactive public repositories after 60 days | Actions → the workflow → **Enable workflow**. The daily tracker normally prevents this |

## 13. Running without GitHub

The scripts are plain Python and run on any server with Python 3.10 or later:

```
pip install -r requirements.txt
export DATAVERSE_API_TOKEN=...      # from the server's secrets store, never from a file here
export REQUESTER_HASH_KEY=...
export INCLUDE_ACCESS_REQUESTS=true
python request_tracker.py           # daily, for example cron: 0 3 * * *
python dataverse_report.py          # monthly, for example cron: 0 5 1 * *
```

Publish the folder `output/site/` with any web server. Keep `data/access_requests.json` outside the
published folder and back it up: it is the only copy of the access-request history. To move an existing
GitHub setup to a server, copy that file and keep the same `REQUESTER_HASH_KEY`.

## 14. Updating to a newer version

Replace the program files (`*.py`, `requirements.txt`, `.github/workflows/*.yml`, `docs/`) with the new
version, and keep your own `institution.json`, logo and `data/access_requests.json`. If the new version
adds settings to `institution.json`, the README says so; settings you do not add get a sensible default.
