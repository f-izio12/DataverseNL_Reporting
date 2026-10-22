# DataverseNL reporting for Maastricht University

Monthly statistics on Maastricht University's collection in [DataverseNL](https://dataverse.nl),
published as a website, plus a daily tracker of access requests.

**Website:** https://f-izio12.github.io/DataverseNL_Reporting/

- **Report** (`index.html`): published datasets per year and month per faculty, open and
  restricted files, licences, storage, links to publications, access requests per year, and
  dataset downloads per year with a breakdown by sub-collection.
- **Admin** (`admin.html`): work lists for curators: waiting access requests (longest first),
  unpublished drafts, published datasets with unpublished changes (and what changed), datasets
  without a licence, and datasets the token cannot check.

Maintained by Research Support & Development, Maastricht University Library.

## How it runs

| Workflow | When | What it does |
|---|---|---|
| **DataverseNL report** | 1st of every month, 05:00 UTC, or manually | Builds the report and admin page, publishes the website, keeps downloads for 90 days (report) and 30 days (admin) |
| **Access-request tracker** | Every day, 03:00 UTC, or manually | Takes a snapshot of the access-request queue and saves the history in `data/access_requests.json` |

To run either one by hand: **Actions** tab → choose the workflow → **Run workflow**.
Always start a new run after changing a file; **Re-run jobs** repeats the old code.

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
     collections. Needed for access requests, drafts and draft comparisons; the public figures
     work without it.
   - `REQUESTER_HASH_KEY`: a long random string (at least 16 characters). **Never change it once
     set**, or every waiting request will be counted as new.
2. **GitHub Pages** (Settings → Pages): Source = **GitHub Actions**.
3. Run **Access-request tracker** once to start the history, then **DataverseNL report**.

## Settings

Set in the workflow files (`env:`) or at the top of `dataverse_report.py`.

| Setting | Default | Meaning |
|---|---|---|
| `INCLUDE_ACCESS_REQUESTS` | on (scheduled runs always on) | Check the access-request queue during the report run |
| `PUBLISH_DRAFT_TITLES` | `false` | Show titles of never-published drafts on the website (always in the admin download) |
| `HIGHLIGHT_THRESHOLD` | `10` | Months with at least this many new datasets are marked in the monthly table |
| `REQUEST_YEARS_SHOWN` | `5` | Years kept in the access-request table; the oldest drops off |
| `JUMP_FACTOR`, `JUMP_MINIMUM` | `10`, `1000` | A download figure is marked ▲ when it is at least 10 times the year before and at least 1,000 |
| `FILE_LOOKUP_LIMIT` | `500` | Downloaded files missing from the file list that are looked up individually to find their dataset |
| `LOGO_PATH` | `assets/um-logo.png` | Logo in the page header (PNG or SVG) |
| `ROOT_ALIAS` | `maastricht` | DataverseNL collection to report on |
| `FACULTY_ALIASES`, `SHORT_NAMES` | in the script | Which collections count as faculties, and their short names |

## How the figures are made

- **Published datasets** come from the DataverseNL Metrics API and are counted by first
  publication date, under the collection each dataset is in **today**. Moving a dataset moves
  its history; deaccessioned datasets are not counted.
- Collections that are not faculties (UNU-MERIT, DataHub, MUMC+ and others) are shown separately
  rather than left out.
- **Views and citations** are not available for these datasets on DataverseNL.

### Dataset downloads

DataverseNL records a download for every **file**: downloading a dataset with 100 files counts
as 100. The report therefore estimates **dataset downloads**: for each dataset and year it takes
the download count of its **most-downloaded file**. Downloading all 100 files once counts as 1;
500 people downloading one file counts as 500.

- It is an **estimate on the low side**: different people downloading different single files of
  the same dataset in the same year count as one.
- **Automated traffic is still included**; it is only reduced where a program downloads many
  files of one dataset.
- **Repeat downloads count**: anonymous downloaders cannot be told apart.
- Downloads of files that are **no longer in a dataset** (removed or replaced) cannot always be
  linked to a dataset; the report states the share that is left out.
- Each faculty opens to show its **sub-collections**; deeper levels are counted in the
  sub-collection above them. Sub-collections without downloads are not listed (the report says
  how many).
- The original file-download counts are kept in `downloads_by_subcollection.csv`.

### Drafts with unpublished changes

For published datasets with an unpublished new version, the admin page shows **what changed**
(metadata fields, files added, removed or replaced, terms of access), using DataverseNL's
version comparison. It shows where to look, never the new values. Full details: open the dataset
while logged in → **Versions** → tick both versions → **View Differences**.

## Access-request tracking

The DataverseNL API only shows requests that are **still waiting**. Once a request is granted
or rejected it disappears, and no history is available. The tracker therefore checks the queue
every day:

- a request seen for the first time counts as **new**, dated that day;
- a request that leaves the queue counts as **decided**;
- the difference is the **time to a decision**, to the nearest day.

The report shows, per collection and per calendar year, the **number of requests** and the
**median days to a decision**. The median is the middle value: half of the requests were
decided faster, half slower, and one very slow request does not distort it. Each request counts
in full in the **year it was made**, even when the decision falls in the next year. Requests
still waiting are added once decided, so the current year can still change. Measuring started
on **19 October 2026**; the table keeps the last five years.

The admin page lists every waiting request in one table, longest wait first.

Limitations:

- A decision can be a grant or a rejection; the API does not say which.
- Nothing before 19 October 2026 is included. Requests already waiting on that day are left out
  of the figures (shown as "N+" days on the admin page).
- Requests made and decided between two daily checks are not seen.
- Files the token has no permission for cannot be checked (re-tried every Monday), so the
  figures undercount. The admin page lists these files by collection.

**Privacy.** No names, e-mail addresses or user ids are stored. Each request is saved under a
one-way code made from the requester, the file and the secret `REQUESTER_HASH_KEY`. Without the
key the codes cannot be traced back to a person, and the same person cannot be followed across
files. This is still pseudonymised data, and `data/access_requests.json` is public in this
repository.

## Maintenance

- **API token expiry.** DataverseNL tokens expire. When it does, access requests, drafts and
  draft comparisons stop updating while the public figures continue. Renew the token in
  DataverseNL (API Token page) and update the `DATAVERSE_API_TOKEN` secret.
- **DataverseNL unavailable.** The scripts retry for about two minutes, then stop with a clear
  message. The website keeps the previous version; re-run the workflow later.
- **60-day rule.** GitHub switches off scheduled workflows in public repositories after 60 days
  without activity. The tracker's daily commit prevents this. If the tracker is ever turned off,
  re-enable the workflows from the Actions tab.
- **Moving the repository** (for example to a UM GitHub organisation) changes the website
  address. Re-create both secrets and set Pages to GitHub Actions in the new location.
