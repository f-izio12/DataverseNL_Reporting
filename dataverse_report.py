"""
DataverseNL reports for one institution: an executive report for management and an
admin report for curators and data stewards, each in English and Dutch.

Everything specific to the institution (name, collection, logo, colours, faculties)
is in institution.json. Another institution copies that file and changes the values.

Files:
  dataverse_report.py   collects the figures from the DataverseNL API (this file)
  report_render.py      builds the two reports as HTML
  report_text.py        all report text, in English and Dutch
  request_tracker.py    daily snapshot of the access-request queue

Output:
  output/site/                 the website published by GitHub Pages:
                                 index.html  executive report      admin.html  admin report
                                 <lang>/...  the same in the other language
  output/public/               executive report (both languages) and CSVs, safe to share
  output/admin/                admin report with draft titles, and the work lists as CSV

Public data (no token needed): published datasets, open and restricted files, licences,
links to publications, downloads. Token-only data: storage used, access requests,
unpublished drafts and what changed in them.

API TOKEN: never write it in a file. The scripts read it from the environment variable
DATAVERSE_API_TOKEN. In GitHub, store it as a repository secret with that name.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Configuration: institution.json, with environment variables taking precedence
# --------------------------------------------------------------------------
SETTINGS_PATH = Path(os.environ.get("INSTITUTION_FILE", "institution.json"))


def _load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        raise SystemExit(f"Settings file {SETTINGS_PATH} not found. Copy institution.json and fill it in.")
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        data = json.load(f)

    def clean(d):  # drop the "_help" notes
        return {k: clean(v) if isinstance(v, dict) else v for k, v in d.items() if not k.startswith("_")}
    return clean(data)


SETTINGS = _load_settings()
LANGUAGES = ("en", "nl")
DEFAULT_LANGUAGE = SETTINGS.get("default_language", "en") if SETTINGS.get("default_language") in LANGUAGES else "en"

BASE_URL = os.environ.get("DATAVERSE_URL", SETTINGS.get("dataverse_url", "https://dataverse.nl")).rstrip("/")
ROOT_ALIAS = os.environ.get("ROOT_ALIAS", SETTINGS["root_alias"])
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
PUBLIC_DIR = OUTPUT_DIR / "public"   # executive report + CSVs, safe to share
ADMIN_DIR = OUTPUT_DIR / "admin"     # admin report with draft titles + work lists as CSV
SITE_DIR = OUTPUT_DIR / "site"       # what GitHub Pages publishes

# Titles of datasets that were never published are not public in DataverseNL.
# By default the public website shows only counts and ages for them; the titles stay
# in the downloadable admin report. Set to "true" to publish the titles on the website too.
PUBLISH_DRAFT_TITLES = os.environ.get("PUBLISH_DRAFT_TITLES", "false").lower() == "true"

# Monthly table: totals at or above this number get an extra marker.
HIGHLIGHT_THRESHOLD = int(os.environ.get("HIGHLIGHT_THRESHOLD", "10"))

# Logo (PNG or SVG). If the file is missing, the reports have no logo.
LOGO_PATH = Path(os.environ.get("LOGO_PATH", SETTINGS.get("logo", "")))

# Main units (faculties) and short names for every top-level collection
FACULTY_ALIASES = set(SETTINGS.get("main_units", {}))
SHORT_NAMES = {**SETTINGS.get("other_short_names", {}), **SETTINGS.get("main_units", {})}

MONTHS_IN_DETAIL = 24  # months shown in the monthly table

# ----------------------------- API TOKEN ----------------------------------
# The token is read from the environment, never from this file.
#   GitHub Actions: Settings > Secrets and variables > Actions >
#                   New repository secret, name DATAVERSE_API_TOKEN
#   Locally:        export DATAVERSE_API_TOKEN=...   (macOS/Linux)
#                   setx DATAVERSE_API_TOKEN ...     (Windows)
API_TOKEN = os.environ.get("DATAVERSE_API_TOKEN", "").strip()
INCLUDE_ACCESS_REQUESTS = os.environ.get("INCLUDE_ACCESS_REQUESTS", "false").lower() == "true"
# ---------------------------------------------------------------------------

TZ = ZoneInfo(SETTINGS.get("timezone", "Europe/Amsterdam"))
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "DataverseNL-report/2.0"})


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def get(path: str, params: dict | None = None, auth: bool = False, allow_error: bool = False, attempts: int = 6):
    """GET a Dataverse API path and return the parsed JSON body.
    attempts=6 waits 5, 10, 20, 40, 60 s between tries: rides out a short DataverseNL hiccup."""
    headers = {"X-Dataverse-key": API_TOKEN} if auth and API_TOKEN else {}
    url = f"{BASE_URL}{path}"
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=60)
        except requests.RequestException:
            if last:
                raise
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        if r.status_code in (429, 500, 502, 503, 504) and not last:
            print(f"  DataverseNL returned {r.status_code} for {path}; retrying", flush=True)
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        if r.status_code >= 500 and path == "/api/info/version":
            raise SystemExit(f"DataverseNL is not responding (HTTP {r.status_code}). "
                             f"This is a problem on the DataverseNL side; re-run the workflow later.")
        if allow_error:
            try:
                return r.status_code, r.json()
            except ValueError:
                return r.status_code, {"message": r.text[:200]}
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            # "OK" but not data: usually a maintenance page, a security check or an empty reply.
            if not last:
                print(f"  DataverseNL sent a non-data reply for {path}; retrying", flush=True)
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            kind = r.headers.get("Content-Type", "unknown type")
            preview = " ".join(r.text[:120].split()) or "(empty)"
            raise SystemExit(
                f"DataverseNL answered {path} with something that is not data ({kind}): {preview!r}. "
                f"This is a problem on the DataverseNL side, not in the report; re-run the workflow later.")
    raise RuntimeError(f"Failed after retries: {url}")


def search(params: dict) -> dict:
    return get("/api/search", params=params)["data"]


def search_all(params: dict) -> list[dict]:
    """Page through the Search API (max 1000 per page)."""
    items, start = [], 0
    while True:
        data = search({**params, "per_page": 1000, "start": start})
        items.extend(data["items"])
        start += len(data["items"])
        if not data["items"] or start >= data["total_count"]:
            return items


def facet_counts(data: dict, name: str) -> dict[str, int]:
    for block in data.get("facets", []):
        if name in block:
            return {k: v for label in block[name]["labels"] for k, v in label.items()}
    return {}


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------
def get_units() -> list[dict]:
    tree = get("/api/info/metrics/tree", params={"parentAlias": ROOT_ALIAS})["data"]
    units = []
    for child in tree.get("children", []):
        alias = child["alias"]
        units.append({
            "alias": alias,
            "name": child["name"].strip(),
            "short": SHORT_NAMES.get(alias, child["name"].strip()),
            "is_faculty": alias in FACULTY_ALIASES,
        })
    units.sort(key=lambda u: (not u["is_faculty"], u["short"].lower()))
    return units


def monthly_increments(metric: str, alias: str, attempts: int = 6) -> dict[str, int]:
    """Metrics API monthly series are cumulative; convert to per-month counts."""
    data = get(f"/api/info/metrics/{metric}/monthly", params={"parentAlias": alias}, attempts=attempts)["data"]
    out, prev = {}, 0
    for row in sorted(data, key=lambda r: r["date"]):
        out[row["date"]] = row["count"] - prev
        prev = row["count"]
    return out


def count_subtree_collections(alias: str) -> dict:
    tree = get("/api/info/metrics/tree", params={"parentAlias": alias})["data"]
    total = 0

    def walk(node):
        nonlocal total
        for c in node.get("children", []) or []:
            total += 1
            walk(c)

    walk(tree)
    # Direct sub-collections (departments, institutes, projects). Deeper levels are counted
    # inside these, because the Metrics API always includes everything below a collection.
    children = [{"alias": c["alias"], "name": (c.get("name") or c["alias"]).strip()}
                for c in tree.get("children", []) or [] if c.get("alias")]
    alias_to_child = {}

    def mark(node, top):
        for c in node.get("children", []) or []:
            if c.get("alias"):
                alias_to_child[c["alias"]] = top
            mark(c, top)

    for c in tree.get("children", []) or []:
        if c.get("alias"):
            alias_to_child[c["alias"]] = c["alias"]
            mark(c, c["alias"])
    return {"subcollections": total, "children": children, "alias_to_child": alias_to_child}


STORAGE_TOTAL: int | None = None  # storage of the whole UM collection, set in main()


def storage_size(alias: str) -> int | None:
    """Space used by a collection and everything below it, in bytes: files in published AND
    unpublished datasets, each file counted once (DataverseNL's own storage-size function, the
    same figure as DANS's storage exports). Needs the API token; None if not available."""
    if not API_TOKEN:
        return None
    status, body = get(f"/api/dataverses/{alias}/storagesize", auth=True, allow_error=True, attempts=3)
    if status != 200 or not isinstance(body, dict):
        return None
    msg = str((body.get("data") or {}).get("message", ""))
    # "Total size of the files stored in this dataverse: 1,067,873,738 bytes"
    m = re.search(r":\s*([0-9][0-9.,\s\u00a0\u202f']*)\s*bytes", msg)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _count_years(dates) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for d in dates:
        if d:
            out[str(d)[:4]] += 1
    return dict(out)


def collect_unit(unit: dict) -> dict:
    alias = unit["alias"]
    print(f"  {unit['short']} ...", flush=True)

    ds = search({"q": "*", "type": "dataset", "subtree": alias, "per_page": 0, "show_facets": "true"})
    fl = search({"q": "*", "type": "file", "subtree": alias, "per_page": 0, "show_facets": "true"})

    datasets = search_all({"q": "*", "type": "dataset", "subtree": alias}) if ds["total_count"] else []
    linked = sum(1 for d in datasets if d.get("publications"))
    versions_gt1 = sum(1 for d in datasets if (d.get("majorVersion") or 1) > 1 or (d.get("minorVersion") or 0) > 0)

    by_type = get("/api/info/metrics/files/byType", params={"parentAlias": alias})["data"] if fl["total_count"] else []
    # Sum of file sizes per published VERSION (Metrics API): a file in three published versions counts
    # three times, so this is NOT used for storage. Kept only for reference in raw_data.json.
    released_version_bytes = sum(int(t.get("size") or 0) for t in by_type)

    sub = count_subtree_collections(alias)
    # Downloads per direct sub-collection, for the expandable rows in the downloads annex
    child_downloads = []
    if ds["total_count"]:
        for c in sub["children"]:
            # Not essential (file downloads per sub-collection are only used for the CSV and as a fallback),
            # so a sub-collection DataverseNL cannot answer for is skipped instead of stopping the report.
            try:
                monthly = monthly_increments("downloads", c["alias"], attempts=2)
            except Exception as exc:
                print(f"    file downloads for sub-collection {c['alias']} unavailable ({type(exc).__name__}); skipped",
                      flush=True)
                monthly = None
            child_downloads.append({**c, "monthly": monthly})

    return {
        **unit,
        "datasets": ds["total_count"],
        "files": fl["total_count"],
        "published_monthly": monthly_increments("datasets", alias) if ds["total_count"] else {},
        "downloads_monthly": monthly_increments("downloads", alias) if ds["total_count"] else {},
        # Counted from the items, not from the facet: facets only return the top 8 values.
        "deposit_by_year": _count_years(d.get("createdAt") for d in datasets),
        "licences": facet_counts(ds, "license"),
        "file_access": facet_counts(fl, "fileAccess"),
        "file_groups": facet_counts(fl, "fileTypeGroupFacet"),
        "released_version_bytes": released_version_bytes,
        "storage_bytes": storage_size(alias),
        "linked_to_publication": linked,
        "updated_after_v1": versions_gt1,
        "subcollections": sub["subcollections"],
        "child_downloads": child_downloads,
        "alias_to_child": sub["alias_to_child"],
        # Which (sub-)collection each dataset sits in; used on the admin page only.
        "pid_collection": {d["global_id"]: [d.get("identifier_of_dataverse", ""), d.get("name_of_dataverse", "")]
                           for d in datasets if d.get("global_id")},
    }


FILE_LOOKUP_LIMIT = 500  # files not in the current file list are looked up one by one, up to this many


def collect_dataset_downloads(units: list[dict], years: list[int]) -> dict:
    """Estimated dataset downloads per year: for each dataset, the downloads of its most-downloaded file.

    DataverseNL records one download per FILE, so downloading a 100-file dataset counts 100 times.
    Taking the highest single-file count per dataset counts that as 1, while 500 people downloading
    one file still counts as 500."""
    now = datetime.now(TZ)
    print("  dataset downloads: listing files", flush=True)
    file_to_ds = {str(f["file_id"]): f.get("dataset_persistent_id", "")
                  for f in search_all({"q": "*", "type": "file", "subtree": ROOT_ALIAS}) if f.get("file_id")}

    # Cumulative downloads per file at the end of each year (current year: up to this month)
    cum = {}
    for y in [years[0] - 1] + years:
        month = f"{y}-12" if y < now.year else now.strftime("%Y-%m")
        rows = get(f"/api/info/metrics/filedownloads/toMonth/{month}", params={"parentAlias": ROOT_ALIAS})["data"]
        cum[y] = {str(r["id"]): int(r.get("count") or 0) for r in rows if r.get("id") is not None}
    per_file = {}
    for y in years:
        for fid, c in cum[y].items():
            d = c - cum[y - 1].get(fid, 0)
            if d > 0:
                per_file.setdefault(fid, {})[y] = d

    # Files with downloads that are not in the current file list (replaced, removed): look up their dataset
    missing = sorted((f for f in per_file if f not in file_to_ds), key=lambda f: -sum(per_file[f].values()))
    looked_up = 0
    for fid in missing[:FILE_LOOKUP_LIMIT]:
        status, body = get(f"/api/files/{fid}", params={"returnOwners": "true"}, allow_error=True)
        node = (body.get("data") or {}).get("isPartOf") if status == 200 and isinstance(body, dict) else None
        while isinstance(node, dict):
            if node.get("type") == "DATASET" and node.get("persistentIdentifier"):
                file_to_ds[fid] = node["persistentIdentifier"]
                looked_up += 1
                break
            node = node.get("isPartOf")
    print(f"  dataset downloads: {len(per_file)} files with downloads, {len(missing)} not in the file list, "
          f"{looked_up} of those linked", flush=True)

    # Highest single-file count per dataset and year
    per_ds: dict = {}
    unlinked = {y: 0 for y in years}     # file downloads that could not be linked to a dataset
    unplaced = {y: 0 for y in years}     # estimated downloads of datasets no longer in a UM collection
    total_files = {y: 0 for y in years}
    for fid, ys in per_file.items():
        pid = file_to_ds.get(fid)
        for y, d in ys.items():
            total_files[y] += d
            if not pid:
                unlinked[y] += d
                continue
            cur = per_ds.setdefault(pid, {})
            cur[y] = max(cur.get(y, 0), d)

    # Roll datasets up to collection and sub-collection
    where = {}
    for u in units:
        for pid, (coll_alias, _name) in u.get("pid_collection", {}).items():
            where[pid] = (u["alias"], u.get("alias_to_child", {}).get(coll_alias))
    unit_years, child_years, direct_years = {}, {}, {}
    for pid, ys in per_ds.items():
        if pid not in where:
            for y, n in ys.items():   # e.g. deaccessioned datasets: no longer in any collection
                unplaced[y] += n
            continue
        ua, child = where[pid]
        for y, n in ys.items():
            unit_years.setdefault(ua, {}).setdefault(y, 0)
            unit_years[ua][y] += n
            if child:
                child_years.setdefault(ua, {}).setdefault(child, {}).setdefault(y, 0)
                child_years[ua][child][y] += n
            else:
                direct_years.setdefault(ua, {}).setdefault(y, 0)
                direct_years[ua][y] += n
    return {"unit": unit_years, "child": child_years, "direct": direct_years,
            "unlinked": unlinked, "unplaced": unplaced, "file_downloads": total_files}


def collect_access_requests(units: list[dict]) -> dict:
    """Pending access requests on restricted files. Needs a curator token."""
    result = {"enabled": False, "reason": ""}
    if not INCLUDE_ACCESS_REQUESTS:
        result["reason"] = "Not requested (set INCLUDE_ACCESS_REQUESTS=true)."
        return result
    if not API_TOKEN:
        result["reason"] = "No API token found in DATAVERSE_API_TOKEN."
        return result

    result.update({"enabled": True, "per_unit": {}, "items": [], "no_permission": {}})

    def check(file_id):
        status, body = get(f"/api/access/datafile/{file_id}/listRequests", auth=True, allow_error=True)
        if status == 200:
            data = body.get("data")
            # Only the NUMBER of requesters is kept. Names and e-mail addresses of
            # requesters are personal data and are deliberately not stored.
            return ("ok", len(data) if isinstance(data, list) else 0)
        if status == 404:  # Dataverse answers 404 when a file has no pending requests
            return ("ok", 0)
        if status in (401, 403):
            return ("forbidden", 0)
        return ("error", 0)

    for u in units:
        files = [f for f in search_all({"q": "*", "type": "file", "subtree": u["alias"],
                                        "fq": 'fileAccess:"Restricted"'}) if f.get("file_id")]
        print(f"  {u['short']}: checking {len(files)} restricted files", flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(lambda f: check(f["file_id"]), files))
        for f, (status, n) in zip(files, res):
            if status == "forbidden":
                pid = f.get("dataset_persistent_id", "")
                coll_alias, coll_name = u.get("pid_collection", {}).get(pid, ["", ""])
                entry = result["no_permission"].setdefault(pid, {
                    "unit": u["short"], "dataset": f.get("dataset_name", ""), "pid": pid,
                    "collection_alias": coll_alias, "collection": coll_name, "files": 0})
                entry["files"] += 1
            if status == "ok" and n > 0:
                result["items"].append({
                    "unit": u["short"],
                    "dataset": f.get("dataset_name", ""),
                    "dataset_pid": f.get("dataset_persistent_id", ""),
                    "dataset_id": f.get("dataset_id", ""),
                    "file": f.get("name", ""),
                    "file_id": f["file_id"],
                    "pending": n,
                })
        result["per_unit"][u["alias"]] = {
            "restricted_files": len(files),
            "files_with_requests": sum(1 for s, n in res if s == "ok" and n > 0),
            "pending_requests": sum(n for s, n in res if s == "ok"),
            "no_permission": sum(1 for s, _ in res if s == "forbidden"),
            "errors": sum(1 for s, _ in res if s == "error"),
        }
    return result


def compare_with_draft(pid: str) -> dict:
    """What differs between the published version and the draft of one dataset.
    Uses /api/datasets/{id}/versions/:latest-published/compare/:draft (Dataverse 6.5+).
    Only WHERE something changed is kept (field names, counts), never the new values:
    the draft is unpublished and the admin page is public."""
    try:
        status, body = get("/api/datasets/:persistentId/versions/:latest-published/compare/:draft",
                           params={"persistentId": pid}, auth=True, allow_error=True)
    except Exception as exc:  # one failed comparison must never stop the whole report
        print(f"  compare failed for {pid}: {type(exc).__name__}", flush=True)
        return {"ok": False, "status": "error"}
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return {"ok": False, "status": status}
    d = body["data"]
    fields = []
    for block in d.get("metadataChanges") or []:
        for ch in block.get("changed") or []:
            name = ch.get("fieldName")
            if name and name not in fields:
                fields.append(name)
    terms = [t.get("fieldName") for t in ((d.get("TermsOfAccess") or {}).get("changed") or []) if t.get("fieldName")]
    return {
        "ok": True,
        "fields": fields,
        "files_added": len(d.get("filesAdded") or []),
        "files_removed": len(d.get("filesRemoved") or []),
        "files_replaced": len(d.get("filesReplaced") or []),
        "files_changed": len(d.get("fileChanges") or []),
        "terms": terms,
        "draft_version": (d.get("newVersion") or {}).get("versionNumber", ""),
        "published_version": (d.get("oldVersion") or {}).get("versionNumber", ""),
    }


def collect_admin_lists(units: list[dict]) -> dict:
    """Lists for the admin page: datasets without a licence (public data) and
    unpublished drafts (only visible with a token that can see them)."""
    no_licence = []
    for u in units:
        for d in search_all({"q": "*", "type": "dataset", "subtree": u["alias"], "fq": "-license:[* TO *]"}):
            no_licence.append({
                "unit": u["short"],
                "title": d.get("name", ""),
                "pid": d.get("global_id", ""),
                "collection": d.get("name_of_dataverse", ""),
                "published": (d.get("published_at") or "")[:10],
            })

    drafts: dict = {"enabled": False, "reason": "", "items": [], "new_versions": []}
    if not API_TOKEN:
        drafts["reason"] = "No API token provided, so unpublished drafts are not visible."
    else:
        drafts["enabled"] = True
        today = datetime.now(TZ).date()

        def age_of(ts: str):
            day = (ts or "")[:10]
            return day, ((today - datetime.fromisoformat(day).date()).days if day else None)

        for u in units:
            # Every dataset the PUBLIC search returned is published. The search index stores a
            # separate "Draft" entry when a published dataset gets a new unpublished version,
            # and that entry is NOT labelled "Published", so the label cannot be trusted here.
            published_pids = set(u.get("pid_collection", {}))
            seen = set()
            start = 0
            while True:
                status, body = get("/api/search", params={"q": "*", "type": "dataset", "subtree": u["alias"],
                                                          "fq": 'publicationStatus:"Draft"',
                                                          "per_page": 1000, "start": start},
                                   auth=True, allow_error=True)
                if status != 200:
                    drafts["enabled"] = False
                    drafts["reason"] = f"The search API refused the draft query (HTTP {status})."
                    return {"no_licence": no_licence, "drafts": drafts}
                data = body.get("data", {})
                items = data.get("items", [])
                for d in items:
                    pid = d.get("global_id", "")
                    if pid in seen:
                        continue
                    seen.add(pid)
                    if pid in published_pids:
                        changed, age = age_of(d.get("updatedAt"))
                        drafts["new_versions"].append({"unit": u["short"], "title": d.get("name", ""), "pid": pid,
                                                       "changed": changed, "age_days": age})
                    else:
                        created, age = age_of(d.get("createdAt"))
                        drafts["items"].append({"unit": u["short"], "title": d.get("name", ""), "pid": pid,
                                                "created": created, "age_days": age})
                start += len(items)
                if not items or start >= data.get("total_count", 0):
                    break

        # What changed in each draft of an already published dataset
        if drafts["new_versions"]:
            print(f"  comparing {len(drafts['new_versions'])} drafts with their published version", flush=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                for item, cmp in zip(drafts["new_versions"],
                                     pool.map(lambda i: compare_with_draft(i["pid"]), drafts["new_versions"])):
                    item["changes"] = cmp
    return {"no_licence": no_licence, "drafts": drafts}


def tracker_summary() -> dict | None:
    """History collected by request_tracker.py (daily). None if tracking has not started."""
    try:
        import request_tracker as rt
    except ImportError:
        return None
    state = rt.load_state()
    if not state or not state.get("runs"):
        return None
    return rt.summarise(state, datetime.now(TZ).date().isoformat())


def main() -> int:
    import report_render as rr

    for d in (PUBLIC_DIR, ADMIN_DIR, SITE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    gaps = rr.check_text()
    if gaps:
        print("WARNING: report_text.py has texts missing a language: " + ", ".join(gaps))
    print(f"DataverseNL report for '{ROOT_ALIAS}' at {BASE_URL}")
    print("Token: " + ("provided" if API_TOKEN else "not provided") +
          f"; access requests: {'on' if INCLUDE_ACCESS_REQUESTS else 'off'}")

    version = get("/api/info/version")["data"].get("version", "unknown")
    storage_total = storage_size(ROOT_ALIAS)
    units = get_units()
    print(f"Found {len(units)} top-level collections. Collecting:")
    data = [collect_unit(u) for u in units]

    this_year = datetime.now(TZ).year
    dl_years = list(range(this_year - 5, this_year + 1))
    try:
        estimates = collect_dataset_downloads(data, dl_years)
    except Exception as exc:  # the report must not fail because of this estimate
        print(f"  dataset downloads could not be estimated ({type(exc).__name__}); showing file downloads instead")
        estimates = None
    for u in data:
        u["_dl_estimates"] = estimates

    print("Access requests:")
    access = collect_access_requests([u for u in data if u["datasets"] > 0])
    if not access.get("enabled"):
        print("  skipped -", access["reason"])

    # Consistency check: units should add up to the root collection.
    root_total = search({"q": "*", "type": "dataset", "subtree": ROOT_ALIAS, "per_page": 0})["total_count"]
    unit_total = sum(u["datasets"] for u in data)
    if root_total != unit_total:
        print(f"WARNING: collections sum to {unit_total}, root has {root_total} "
              f"(datasets deposited directly in '{ROOT_ALIAS}' are not in any collection).")

    print("Admin lists (datasets without licence, drafts):")
    lists = collect_admin_lists([u for u in data if u["datasets"] > 0])
    print(f"  {len(lists['no_licence'])} datasets without licence; drafts: "
          + (str(len(lists['drafts']['items'])) if lists['drafts']['enabled'] else "skipped - " + lists['drafts']['reason']))

    now = datetime.now(TZ)
    cfg = {"settings": SETTINGS, "base_url": BASE_URL, "root_alias": ROOT_ALIAS, "logo": str(LOGO_PATH),
           "publish_draft_titles": PUBLISH_DRAFT_TITLES, "highlight_threshold": HIGHLIGHT_THRESHOLD}
    model = rr.prepare(data, access, lists, version, storage_total, tracker_summary(), now, cfg)
    rr.write_csvs(model, PUBLIC_DIR, ADMIN_DIR)
    written = rr.write_all(model, LANGUAGES, DEFAULT_LANGUAGE, SITE_DIR, PUBLIC_DIR, ADMIN_DIR)
    (PUBLIC_DIR / "raw_data.json").write_text(json.dumps(
        {"generated": now.isoformat(), "version": version, "storage_total_bytes": storage_total,
         "units": [{k: v for k, v in u.items() if k not in ("pid_collection", "alias_to_child", "_dl_estimates")}
                   for u in data]},
        indent=2, default=str), encoding="utf-8")
    print(f"Done: {len(written)} pages written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
