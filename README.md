# DataverseNL reporting for Maastricht University

Monthly statistics on Maastricht University's collection in [DataverseNL](https://dataverse.nl),
published as a website, plus a daily tracker of access requests.

**Website:** https://f-izio12.github.io/DataverseNL_Reporting/

- **Report** (`index.html`): published datasets per year and month per faculty, open and
  restricted files, licences, storage, links to publications, access requests over time.
- **Admin** (`admin.html`): work lists for curators: pending access requests, oldest waiting
  requests, unpublished drafts, datasets without a licence, datasets the token cannot check.

Maintained by Research Support & Development, Maastricht University Library.

## How it runs

| Workflow | When | What it does |
|---|---|---|
| **DataverseNL report** | 1st of every month, 05:00 UTC, or manually | Builds the report and admin page, publishes the website, keeps downloads for 90 days (report) and 30 days (admin) |
| **Access-request tracker** | Every day, 03:00 UTC, or manually | Takes a snapshot of the access-request queue and saves the history in `data/access_requests.json` |

To run either one by hand: **Actions** tab → choose the workflow → **Run workflow**.

## Files

```
.github/workflows/dataverse-report.yml        monthly report and website
.github/workflows/access-request-tracker.yml  daily access-request snapshot
dataverse_report.py                           builds the report and admin page
request_tracker.py                            keeps the access-request history
data/access_requests.json                     history, written by the tracker (do not edit)
assets/um-logo.png                            logo shown in the page header
requirements.txt                              Python dependencies
```

## Setup

1. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `DATAVERSE_API_TOKEN`: API token of a DataverseNL account with curator rights on the UM
     collections. Only needed for access requests and drafts; the public figures work without it.
   - `REQUESTER_HASH_KEY`: a long random string (at least 16 characters). **Never change it once
     set**, or every waiting request will be counted as new.
2. **GitHub Pages** (Settings → Pages): Source = **GitHub Actions**.
3. Run **Access-request tracker** once to start the history, then **DataverseNL report**.

## Settings

Set in the workflow files (`env:`) or at the top of `dataverse_report.py`.

| Setting | Default | Meaning |
|---|---|---|
| `INCLUDE_ACCESS_REQUESTS` | on (scheduled runs always on) | Check the pending access-request queue |
| `PUBLISH_DRAFT_TITLES` | `false` | Show titles of never-published drafts on the website (always in the admin download) |
| `HIGHLIGHT_THRESHOLD` | `10` | Months with at least this many new datasets are marked in the monthly table |
| `LOGO_PATH` | `assets/um-logo.png` | Logo in the page header (PNG or SVG) |
| `ROOT_ALIAS` | `maastricht` | DataverseNL collection to report on |
| `FACULTY_ALIASES`, `SHORT_NAMES` | in the script | Which collections count as faculties, and their short names |

## How the figures are made

- **Published datasets** come from the DataverseNL Metrics API and are counted by first
  publication date, under the collection each dataset is in **today**. Moving a dataset moves
  its history; deaccessioned datasets are not counted.
- Collections that are not faculties (UNU-MERIT, DataHub, MUMC+ and others) are shown separately
  rather than left out.
- **Downloads** include automated traffic and are shown only in an annex, with a warning.
- **Views and citations** are not available for these datasets on DataverseNL.

## Access-request tracking

The DataverseNL API only shows requests that are **still waiting**. Once a request is granted
or rejected it disappears, and no history is available. The tracker therefore checks the queue
every day:

- a request seen for the first time counts as **new**, dated that day;
- a request that leaves the queue counts as **decided**;
- the difference is the **time to a decision**, to the nearest day.

Limitations:

- A decision can be a grant or a rejection; the API does not say which.
- History starts on the first day the tracker ran. Requests already waiting then are left out
  of the figures.
- Requests made and decided between two daily checks are not seen.
- Files the token has no permission for cannot be checked (re-tried every Monday), so the
  figures undercount. The admin page lists these files by collection.

**Privacy.** No names, e-mail addresses or user ids are stored. Each request is saved under a
one-way code made from the requester, the file and the secret `REQUESTER_HASH_KEY`. Without the
key the codes cannot be traced back to a person, and the same person cannot be followed across
files. This is still pseudonymised data, and `data/access_requests.json` is public in this
repository.

## Maintenance

- **API token expiry.** DataverseNL tokens expire. When it does, access requests and drafts stop
  updating while the public figures continue. Renew it in DataverseNL (API Token page) and
  update the `DATAVERSE_API_TOKEN` secret.
- **DataverseNL unavailable.** The scripts retry for about two minutes, then stop with a clear
  message. The website keeps the previous version; re-run the workflow later.
- **60-day rule.** GitHub switches off scheduled workflows in public repositories after 60 days
  without activity. The tracker's daily commit prevents this. If the tracker is ever turned off,
  re-enable the workflows from the Actions tab.
- **Moving the repository** (for example to a UM GitHub organisation) changes the website
  address. Re-create both secrets and set Pages to GitHub Actions in the new location.
