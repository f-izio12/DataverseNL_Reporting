# DataverseNL reporting

Monthly reports on an institution's research data in [DataverseNL](https://dataverse.nl), in English
and Dutch, plus a daily tracker of access requests. Built at Maastricht University Library and set up
so that any institution with a DataverseNL collection can use it by changing one settings file.

**Setting it up for your institution? Start with the [setup guide](docs/SETUP_GUIDE.md):** step by step,
no programming needed.

Two reports, each in English and Dutch:

- **Executive report**, for management: headline figures, trends in charts, the faculties compared,
  and access and openness (open and restricted files, licences, access requests and time to a decision).
- **Admin report**, for curators and data stewards: waiting access requests (longest first), unpublished
  drafts, published datasets with unpublished changes (and what changed), datasets without a licence,
  datasets the token cannot check, all detailed tables and the full method.

Example (Maastricht University, test site): https://f-izio12.github.io/DataverseNL_Reporting/

## Use it for your institution

1. Copy this repository (or download it).
2. Edit **`institution.json`**. It holds everything institution-specific:

   | Setting | Meaning |
   |---|---|
   | `dataverse_url` | Usually `https://dataverse.nl` |
   | `root_alias` | Your institution's top-level collection, as in `dataverse.nl/dataverse/<alias>` |
   | `name`, `short_name`, `department` | Shown in the page header; `name` and `department` in English and Dutch |
   | `logo` | Path to your logo (PNG or SVG), for example `assets/logo.png` |
   | `colours` | `primary` (headings, links, main chart colour) and `accent` (second colour). Dark mode is derived automatically |
   | `default_language` | `en` or `nl`: the language at the website's main address. The other language is in `/nl/` or `/en/` |
   | `timezone` | Usually `Europe/Amsterdam` |
   | `main_units` | The top-level collections that are your main units (faculties, schools), with their short names. All other collections appear as "other collections" |
   | `main_unit_label` | What a main unit is called, in both languages (faculty/faculties, faculteit/faculteiten) |
   | `other_short_names` | Optional short names for the other collections |

3. Put your logo in `assets/`.
4. Follow **Setup** below (GitHub) or **Running on your own server**.

All text of the reports is in `report_text.py`, in English and Dutch side by side. To change a sentence
or improve a translation, edit it there; nothing else needs to change.

## Files

```
institution.json                              institution settings (edit this)
dataverse_report.py                           collects the figures from the DataverseNL API
report_render.py                              builds the two reports as HTML and the CSV files
report_text.py                                all report text, English and Dutch
request_tracker.py                            daily snapshot of the access-request queue
data/access_requests.json                     access-request history, written by the tracker (do not edit)
assets/                                       logo
docs/SETUP_GUIDE.md                           step-by-step setup for a new institution
.github/workflows/dataverse-report.yml        monthly reports and website (GitHub)
.github/workflows/access-request-tracker.yml  daily access-request snapshot (GitHub)
requirements.txt                              Python dependencies
LICENSE                                       MIT licence
```

Output of a run (folder `output/`):

```
site/index.html, site/admin.html     executive and admin report, default language (the website)
site/nl/...  (or site/en/...)        the same in the other language
public/                              executive report in both languages, CSV files, raw_data.json
admin/                               admin report in both languages, with draft titles, and the work lists as CSV
```

## The API token: a placeholder, not a value

This repository contains **no API token**, and it never should. The code only names the place where a
token goes: the environment variable `DATAVERSE_API_TOKEN`. In the GitHub workflow that placeholder is
filled from a repository secret:

```yaml
DATAVERSE_API_TOKEN: ${{ secrets.DATAVERSE_API_TOKEN }}
```

When you copy or fork the repository, the secret does **not** come with it: the placeholder is empty
until you add your own token. The reports still run without one, but with less in them:

| Without a token | With a token (curator rights) |
|---|---|
| Published datasets, files, licences, downloads, links to publications | The same |
| Storage: "not available" | Storage used, per collection |
| Access requests: "not included" | Waiting requests, requests per year, time to a decision |
| Drafts: "not included" | Unpublished drafts and what changed in published datasets |

To create a token: log in to DataverseNL → your name (top right) → **API Token** → **Create Token**.
Use a functional (team) account with the **Curator** role on your institution's collections, not a
personal account, so the reports do not depend on one person. Tokens expire, usually after a year;
put the expiry date in a calendar.

Where to put it:

- **GitHub:** Settings → Secrets and variables → Actions → **New repository secret**, name
  `DATAVERSE_API_TOKEN`, value: the token. GitHub hides it from logs and from everyone, including
  repository admins.
- **Your own server:** an environment variable set by the server's secrets store or scheduler, never
  a file inside this folder.
- **Never** paste a token into a file, an issue, a chat or an e-mail. If that happens, revoke it in
  DataverseNL straight away and create a new one.

## Setup (GitHub)

1. **Repository secrets** (Settings → Secrets and variables → Actions):
   - `DATAVERSE_API_TOKEN`: see above. Optional, but without it storage, access requests and drafts
     are left out.
   - `REQUESTER_HASH_KEY`: a long random string (at least 16 characters). **Never change it once
     set**, or every waiting request is counted as new.
2. **GitHub Pages** (Settings → Pages): Source = **GitHub Actions**.
3. Run **Access-request tracker** once to start the history, then **DataverseNL report**.

| Workflow | When | What it does |
|---|---|---|
| **DataverseNL report** | 1st of every month, 05:00 UTC, or manually | Builds both reports, publishes the website, keeps the downloads for 90 days (executive report) and 30 days (admin report) |
| **Access-request tracker** | Every day, 03:00 UTC, or manually | Snapshots the access-request queue and saves the history in `data/access_requests.json` |

To run one by hand: **Actions** tab → choose the workflow → **Run workflow**. Start a new run after
changing a file; **Re-run jobs** repeats the old code.

## Running on your own server

The scripts need Python 3.10 or later and the `requests` package (`pip install -r requirements.txt`).

```
export DATAVERSE_API_TOKEN=...          # from a secrets store, not from a file in this folder
export REQUESTER_HASH_KEY=...
export INCLUDE_ACCESS_REQUESTS=true
python request_tracker.py               # every day, for example by cron at 03:00
python dataverse_report.py              # every month, for example on the 1st at 05:00
```

Serve the folder `output/site/` with any web server. `data/access_requests.json` must persist between
runs and must be backed up: it is the only copy of the access-request history.

## Other settings

Environment variables, or at the top of the scripts.

| Setting | Default | Meaning |
|---|---|---|
| `INCLUDE_ACCESS_REQUESTS` | on (scheduled GitHub runs always on) | Check the access-request queue during the report run |
| `PUBLISH_DRAFT_TITLES` | `false` | Show titles of never-published drafts on the website (always in the admin download) |
| `HIGHLIGHT_THRESHOLD` | `10` | Months with at least this many new datasets are marked in the monthly table |
| `INSTITUTION_FILE` | `institution.json` | Use another settings file |
| `REQUEST_YEARS_SHOWN` | `5` | Years kept in the access-request table (`report_render.py`) |
| `JUMP_FACTOR`, `JUMP_MINIMUM` | `10`, `1000` | A download figure is marked ▲ when at least 10 times the year before and at least 1,000 (`report_render.py`) |
| `FILE_LOOKUP_LIMIT` | `500` | Downloaded files missing from the file list that are looked up one by one to find their dataset |

## How the figures are made

- **Published datasets** come from the DataverseNL Metrics API, counted by first publication date,
  under the collection each dataset is in **today**. Moving a dataset moves its history; withdrawn
  datasets are not counted.
- **Year-on-year changes** in the executive report compare the same complete months of both years.
- **Storage used** is DataverseNL's own storage-size figure (`/api/dataverses/{alias}/storagesize`, needs
  the token): files in published and unpublished datasets, each counted once, the same figure as DANS's
  storage exports. Units are decimal (1 GB = 1,000,000,000 bytes). The Metrics API's "files by type" is
  not used: it counts a file once for every published version it is in.
- **Views and citations** are not available for these datasets on DataverseNL.

### Dataset downloads

DataverseNL records a download for every **file**: downloading a dataset with 100 files counts as 100.
The reports therefore estimate **dataset downloads**: for each dataset and year, the download count of
its **most-downloaded file**. Downloading all 100 files once counts as 1; 500 people downloading one
file counts as 500.

- It is an **estimate on the low side**: different people downloading different single files of the
  same dataset in the same year count as one.
- **Automated traffic is still included**; it is only reduced where a program downloads many files of
  one dataset.
- **Repeat downloads count**: anonymous downloaders cannot be told apart.
- Downloads of files **no longer in a dataset** cannot always be linked; the admin report states the share.
- The original file-download counts are kept in `downloads_by_subcollection.csv`.

### Drafts with unpublished changes

For published datasets with an unpublished new version, the admin report shows **what changed**
(metadata fields, files added, removed or replaced, terms of access), using DataverseNL's version
comparison. It shows where to look, never the new values.

## Access-request tracking

The DataverseNL API only shows requests that are **still waiting**: once granted or rejected, a request
disappears. The tracker therefore checks the queue every day. A request seen for the first time counts
as **new**; one that leaves the queue counts as **decided**; the difference is the **time to a decision**.

The reports show, per collection and calendar year, the **number of requests** and the **median days
to a decision**. Each request counts in full in the **year it was made**. Limitations: a decision can be a
grant or a rejection (the API does not say which); nothing before the first run is included; requests
made and decided between two checks are not seen; files the token has no rights on cannot be checked,
so the figures undercount (the admin report lists those datasets).

## Privacy

What is processed, and how:

- **Requesters.** The tracker reads who has requested access to a restricted file, because the API
  returns it, but **stores no names, e-mail addresses or user ids**. Each request is saved under a
  one-way code (HMAC-SHA256) made from the requester, the file and the secret `REQUESTER_HASH_KEY`.
  Without the key the code cannot be traced back to a person, and the same person cannot be followed
  across files. The reports only show **counts**; curators see who is asking in DataverseNL itself.
- **This is pseudonymised data, not anonymous data.** Each saved request also holds the dataset, the file
  and the dates it was seen. Someone who already knows that a particular person asked for a particular
  file (for example the dataset's owner) could recognise that entry. On GitHub, `data/access_requests.json`
  is **public** in the repository.
- **Retention.** The history is kept indefinitely; the reports only show the last five years. Set a
  retention period with your privacy officer and delete older entries accordingly.
- **Unpublished drafts.** Titles of datasets that were never published are not public in DataverseNL.
  The website shows only counts and ages; titles appear only in the admin download, unless
  `PUBLISH_DRAFT_TITLES` is switched on.
- **Draft comparisons** show where a draft differs (field names, numbers of files), never the new values.
- **Downloads** are counts from the DataverseNL Metrics API; no guestbook entries (names, e-mail
  addresses) are read.
- **API token.** Read from the environment only (a GitHub secret or the server's secrets store), never
  written to a file or a report.

Each institution is the controller for its own use of these scripts. Before going live, ask your
privacy officer to confirm the legal basis for the tracker, the retention period, and whether the
history may be public (GitHub) or should stay on an internal server. On your own server, keep
`data/access_requests.json` outside the published web folder.

## Maintenance

- **API token expiry.** DataverseNL tokens expire. Storage, access requests and drafts then stop
  updating while the public figures continue. Renew the token in DataverseNL and update the secret.
- **DataverseNL unavailable.** The scripts retry for about two minutes, then stop with a clear message.
  The website keeps the previous version; run again later.
- **60-day rule (GitHub only).** GitHub switches off scheduled workflows in public repositories after 60
  days without activity. The tracker's daily commit prevents this; if it ever stops, re-enable the
  workflows in the Actions tab.
- **Moving the repository** changes the website address. Re-create both secrets and set Pages to GitHub
  Actions in the new location. Keep the same `REQUESTER_HASH_KEY` and copy `data/access_requests.json`.

## Licence

MIT, see [LICENSE](LICENSE). Maintained by Maastricht University Library.

When this repository moves to the University Library's GitHub, the copyright line in `LICENSE`
becomes "Maastricht University".
