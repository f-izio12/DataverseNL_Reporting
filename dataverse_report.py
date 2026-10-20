"""
DataverseNL report for Maastricht University.

Builds two self-contained HTML pages:
  output/public/report.html  the shareable report, plus CSVs with its numbers
  output/admin/admin.html    internal page: pending access requests, unpublished
                             drafts and datasets without a licence, each with
                             links to the datasets, plus CSVs
  output/site/               the website published by GitHub Pages:
                             index.html (report) and admin.html, linked
The GitHub workflow uploads the first two as downloads and publishes the site.

Public data (no token needed):
  - new published datasets per month and year, per collection
  - open / restricted / embargoed files
  - licences
  - storage volume and file types
  - share of datasets linked to a related publication
  - deposit vs publication counts per year
  - downloads (annex only, see caveats in the report)

Token-only data (optional):
  - pending access requests on restricted files
    Set INCLUDE_ACCESS_REQUESTS=true and provide DATAVERSE_API_TOKEN.
    The token's account needs curator rights ("ManageFilePermissions")
    on the datasets, otherwise those files are reported as "no permission".

API TOKEN: never write it in this file. The script reads it from the
environment variable DATAVERSE_API_TOKEN (see the API TOKEN block below).
In GitHub, store it as a repository secret called DATAVERSE_API_TOKEN.
"""

from __future__ import annotations

import base64
import csv
import html
import json
import mimetypes
import os
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URL = os.environ.get("DATAVERSE_URL", "https://dataverse.nl").rstrip("/")
ROOT_ALIAS = os.environ.get("ROOT_ALIAS", "maastricht")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
PUBLIC_DIR = OUTPUT_DIR / "public"   # report.html + CSVs, safe to share
ADMIN_DIR = OUTPUT_DIR / "admin"     # admin.html, lists of datasets, access requests, drafts
SITE_DIR = OUTPUT_DIR / "site"       # what GitHub Pages publishes: index.html (report) + admin.html

# Titles of datasets that were never published are not public in DataverseNL.
# By default the public website shows only counts and ages for them; the titles stay
# in the downloadable admin artifact. Set to "true" to publish the titles on the website too.
PUBLISH_DRAFT_TITLES = os.environ.get("PUBLISH_DRAFT_TITLES", "false").lower() == "true"

# Monthly table: totals at or above this number get an extra marker.
HIGHLIGHT_THRESHOLD = int(os.environ.get("HIGHLIGHT_THRESHOLD", "10"))

# Put the UM logo file in the repository at this path (PNG or SVG).
# If the file is missing, the report is rendered without a logo.
LOGO_PATH = Path(os.environ.get("LOGO_PATH", "assets/um-logo.png"))

# Collections directly under "maastricht" that are UM faculties.
# Everything else (UNU-MERIT, DataHub, MUMC+, Library, Zuyderland) is
# reported as "Other UM-related collections".
FACULTY_ALIASES = {
    "FacultyofPsychologyandNeuroscience",
    "SBE-UM",
    "FHML",
    "FASoS",
    "law",
    "FSE",
}

# Short display names (optional; falls back to the collection name).
SHORT_NAMES = {
    "FacultyofPsychologyandNeuroscience": "FPN",
    "SBE-UM": "SBE",
    "FHML": "FHML",
    "FASoS": "FASoS",
    "law": "Law",
    "FSE": "FSE",
    "UNU-MERIT": "UNU-MERIT",
    "DataHub": "DataHub",
    "MUMC": "MUMC+",
    "UB-UM": "Library",
    "Zuyderland": "Zuyderland",
}

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

TZ = ZoneInfo("Europe/Amsterdam")
SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "UM-Library-DataverseNL-report/1.0"})


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def get(path: str, params: dict | None = None, auth: bool = False, allow_error: bool = False):
    """GET a Dataverse API path and return the parsed JSON body."""
    headers = {"X-Dataverse-key": API_TOKEN} if auth and API_TOKEN else {}
    url = f"{BASE_URL}{path}"
    attempts = 6  # waits 5, 10, 20, 40, 60 s: rides out a short DataverseNL hiccup
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
        return r.json()
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


def monthly_increments(metric: str, alias: str) -> dict[str, int]:
    """Metrics API monthly series are cumulative; convert to per-month counts."""
    data = get(f"/api/info/metrics/{metric}/monthly", params={"parentAlias": alias})["data"]
    out, prev = {}, 0
    for row in sorted(data, key=lambda r: r["date"]):
        out[row["date"]] = row["count"] - prev
        prev = row["count"]
    return out


def count_subtree_collections(alias: str) -> dict[str, int]:
    tree = get("/api/info/metrics/tree", params={"parentAlias": alias})["data"]
    total = 0

    def walk(node):
        nonlocal total
        for c in node.get("children", []) or []:
            total += 1
            walk(c)

    walk(tree)
    return {"subcollections": total}


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
    total_bytes = sum(int(t.get("size") or 0) for t in by_type)

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
        "bytes": total_bytes,
        "linked_to_publication": linked,
        "updated_after_v1": versions_gt1,
        **count_subtree_collections(alias),
        # Which (sub-)collection each dataset sits in; used on the admin page only.
        "pid_collection": {d["global_id"]: [d.get("identifier_of_dataverse", ""), d.get("name_of_dataverse", "")]
                           for d in datasets if d.get("global_id")},
    }


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
    status, body = get("/api/datasets/:persistentId/versions/:latest-published/compare/:draft",
                       params={"persistentId": pid}, auth=True, allow_error=True)
    if status != 200 or not isinstance(body.get("data"), dict):
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


def describe_changes(c: dict | None) -> str:
    """Short HTML note for curators: where to look, not what the new values are."""
    if not c:
        return '<span class="muted">Not checked</span>'
    if not c["ok"]:
        reason = "no permission to view the draft" if c["status"] in (401, 403) else f"HTTP {c['status']}"
        return f'<span class="muted">Could not compare ({esc(reason)})</span>'
    parts = []
    if c["fields"]:
        shown = c["fields"][:6]
        more = len(c["fields"]) - len(shown)
        parts.append(f'<strong>Metadata:</strong> {esc(", ".join(shown))}' + (f" and {more} more" if more else ""))
    f_bits = []
    if c["files_added"]:
        f_bits.append(f'{c["files_added"]} added')
    if c["files_removed"]:
        f_bits.append(f'{c["files_removed"]} removed')
    if c["files_replaced"]:
        f_bits.append(f'{c["files_replaced"]} replaced')
    if c["files_changed"]:
        f_bits.append(f'{c["files_changed"]} with changed details')
    if f_bits:
        parts.append("<strong>Files:</strong> " + ", ".join(f_bits))
    if c["terms"]:
        parts.append(f'<strong>Terms of access:</strong> {esc(", ".join(c["terms"]))}')
    if not parts:
        return '<span class="muted">No differences: the draft is identical to the published version</span>'
    return "<br>".join(parts)


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


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------
def esc(x) -> str:
    return html.escape(str(x))


def fmt(n) -> str:
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else esc(n)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return str(n)


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "n/a"


def table(headers: list[str], rows: list[list], total_row: list | None = None, num_from: int = 1) -> str:
    th = "".join(f'<th class="{"num" if i >= num_from else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td class="{"num" if i >= num_from else ""}">{fmt(c) if isinstance(c, int) else esc(c)}</td>'
            for i, c in enumerate(r)) + "</tr>"
    foot = ""
    if total_row:
        foot = "<tfoot><tr>" + "".join(
            f'<td class="{"num" if i >= num_from else ""}">{fmt(c) if isinstance(c, int) else esc(c)}</td>'
            for i, c in enumerate(total_row)) + "</tr></tfoot>"
    return f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody>{foot}</table></div>'


def logo_tag() -> str:
    if not LOGO_PATH.exists():
        print(f"  (no logo found at {LOGO_PATH}; rendering without logo)")
        return ""
    mime = mimetypes.guess_type(LOGO_PATH.name)[0] or "image/png"
    b64 = base64.b64encode(LOGO_PATH.read_bytes()).decode()
    return f'<img class="logo" src="data:{mime};base64,{b64}" alt="Maastricht University">'


def heat_table(headers: list[str], rows: list[list], threshold: int) -> str:
    """Monthly table: each number is shaded relative to the best month in its own
    column, and the best month per column is bold. Totals at or above `threshold`
    get an extra marker."""
    ncols = len(headers)
    col_max = [max((r[i] for r in rows if isinstance(r[i], int)), default=0) for i in range(ncols)]
    th = "".join(f'<th class="{"num" if i else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = ""
    for r in rows:
        cells = [f"<td>{esc(r[0])}</td>"]
        for i in range(1, ncols):
            v = r[i]
            cls = ["num"]
            style = ""
            if v and col_max[i]:
                # Scale against at least 4, so a collection whose best month is 1 or 2
                # does not look as strong as one that publishes 15 a month.
                scale = max(col_max[i], 4)
                style = f' style="background:rgba(var(--heat-rgb),{0.08 + 0.42 * v / scale:.2f})"'
                if v == col_max[i] and col_max[i] >= 3:
                    cls.append("best")
            elif not v:
                cls.append("zero")
            if i == ncols - 1 and v >= threshold:
                cls.append("hit")
            cells.append(f'<td class="{" ".join(cls)}"{style}>{fmt(v)}</td>')
        body += "<tr>" + "".join(cells) + "</tr>"
    return f'<div class="tbl"><table class="heat"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def signed_pct(new: int, base: int) -> str:
    if not base:
        return "n/a"
    d = 100 * (new - base) / base
    return f"{d:+.0f}%"


def write_csv(name: str, headers: list[str], rows: list[list], folder: Path | None = None) -> None:
    with open((folder or PUBLIC_DIR) / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def nav_bar(current: str) -> str:
    """Links between the two pages; only used on the website."""
    items = [("index.html", "Report"), ("admin.html", "Admin")]
    links = "".join(
        f'<a href="{href}"{" aria-current=page" if href == current else ""}>{label}</a>' for href, label in items)
    return f'<nav class="pages">{links}</nav>'


def build_report(units: list[dict], access: dict, version: str, nav: str = "") -> str:
    now = datetime.now(TZ)
    this_year = now.year
    active = [u for u in units if u["datasets"] > 0]
    fac = [u for u in active if u["is_faculty"]]
    oth = [u for u in active if not u["is_faculty"]]
    ordered = fac + oth

    first_year = min((int(m[:4]) for u in active for m in u["published_monthly"]), default=this_year)
    years = list(range(first_year, this_year + 1))

    def year_sum(series: dict, y: int) -> int:
        return sum(v for k, v in series.items() if k.startswith(f"{y}-"))

    total_ds = sum(u["datasets"] for u in active)
    total_files = sum(u["files"] for u in active)
    total_bytes = sum(u["bytes"] for u in active)
    restricted = sum(u["file_access"].get("Restricted", 0) for u in active)
    pub_this_year = sum(year_sum(u["published_monthly"], this_year) for u in active)
    pub_last_year = sum(year_sum(u["published_monthly"], this_year - 1) for u in active)
    linked = sum(u["linked_to_publication"] for u in active)
    other_share = pct(sum(u["datasets"] for u in oth), total_ds)

    # ---- yearly table
    y_headers = ["Collection"] + [str(y) for y in years] + ["Total"]
    y_rows = [[u["short"]] + [year_sum(u["published_monthly"], y) for y in years] + [u["datasets"]] for u in ordered]
    y_total = ["Total"] + [sum(r[i + 1] for r in y_rows) for i in range(len(years))] + [total_ds]
    write_csv("published_per_year.csv", y_headers, y_rows + [y_total])

    # ---- monthly table (last N months)
    months = []
    y, m = now.year, now.month
    for _ in range(MONTHS_IN_DETAIL):
        months.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    m_headers = ["Month"] + [u["short"] for u in ordered] + ["Total"]
    m_rows = []
    for mo in months:
        vals = [u["published_monthly"].get(mo, 0) for u in ordered]
        label = mo + (" *" if mo == months[-1] else "")
        m_rows.append([label] + vals + [sum(vals)])
    # full monthly history to CSV
    all_months = sorted({k for u in ordered for k in u["published_monthly"]})
    write_csv("published_per_month.csv", ["Month"] + [u["short"] for u in ordered] + ["Total"],
              [[mo] + [u["published_monthly"].get(mo, 0) for u in ordered]
               + [sum(u["published_monthly"].get(mo, 0) for u in ordered)] for mo in all_months])

    # ---- openness
    o_headers = ["Collection", "Files", "Public", "Restricted", "Embargoed", "% restricted"]
    o_rows = []
    for u in ordered:
        fa = u["file_access"]
        emb = sum(v for k, v in fa.items() if k.lower().startswith("embargo"))
        o_rows.append([u["short"], u["files"], fa.get("Public", 0), fa.get("Restricted", 0), emb,
                       pct(fa.get("Restricted", 0), u["files"])])
    o_total = ["Total", total_files, sum(r[2] for r in o_rows), restricted, sum(r[4] for r in o_rows),
               pct(restricted, total_files)]
    write_csv("file_access.csv", o_headers, o_rows + [o_total])

    # ---- licences
    lic_names = sorted({k for u in ordered for k in u["licences"]},
                       key=lambda k: -sum(u["licences"].get(k, 0) for u in ordered))
    l_headers = ["Collection"] + lic_names
    l_rows = [[u["short"]] + [u["licences"].get(k, 0) for k in lic_names] for u in ordered]
    l_total = ["Total"] + [sum(r[i] for r in l_rows) for i in range(1, len(l_headers))]
    write_csv("licences.csv", l_headers, l_rows + [l_total])

    # ---- storage, linkage, structure
    s_headers = ["Collection", "Datasets", "Files", "Storage", "Linked to publication", "% linked",
                 "Updated after first version", "Sub-collections"]
    s_rows = [[u["short"], u["datasets"], u["files"], fmt_bytes(u["bytes"]), u["linked_to_publication"],
               pct(u["linked_to_publication"], u["datasets"]), u["updated_after_v1"], u["subcollections"]]
              for u in ordered]
    s_total = ["Total", total_ds, total_files, fmt_bytes(total_bytes), linked, pct(linked, total_ds),
               sum(u["updated_after_v1"] for u in ordered), sum(u["subcollections"] for u in ordered)]
    write_csv("storage_linkage_structure.csv", s_headers,
              [[r[0], r[1], r[2], u["bytes"]] + r[4:] for r, u in zip(s_rows, ordered)])

    # ---- deposit vs publication
    d_headers = ["Year", "Created that year (and since published)", "First published that year", "Difference"]
    d_rows = []
    for yy in years[-6:]:
        created = sum(int(u["deposit_by_year"].get(str(yy), 0)) for u in ordered)
        published = sum(year_sum(u["published_monthly"], yy) for u in ordered)
        d_rows.append([str(yy), created, published, signed_pct(published, created)])
    write_csv("deposit_vs_publication.csv", d_headers, d_rows)

    # ---- downloads annex
    dl_years = years[-6:]
    dl_headers = ["Collection"] + [str(yy) for yy in dl_years]
    dl_rows = [[u["short"]] + [year_sum(u["downloads_monthly"], yy) for yy in dl_years] for u in ordered]
    write_csv("downloads_per_year.csv", dl_headers, dl_rows)

    # ---- access requests: the public page only shows the proxy; details are on the admin page
    access_html = request_history_html(ordered) + (
                   f'<p class="note">{fmt(restricted)} files ({pct(restricted, total_files)}) are restricted. '
                   f'Only restricted files can receive an access request, so this is the upper bound of the '
                   f'access-request workload. Pending requests are listed on the admin page.</p>')

    # ---- chart data
    chart_years = years[-8:]
    chart_series = [{"label": u["short"], "data": [year_sum(u["published_monthly"], yy) for yy in chart_years]}
                    for u in ordered]

    kpis = [
        ("Published datasets", fmt(total_ds), f"{other_share} outside the six faculties"),
        (f"Published in {this_year}", fmt(pub_this_year), f"{fmt(pub_last_year)} in {this_year - 1} (full year)"),
        ("Files", fmt(total_files), fmt_bytes(total_bytes) + " in total"),
        ("Restricted files", pct(restricted, total_files), f"{fmt(restricted)} files"),
        ("Linked to a publication", pct(linked, total_ds), f"{fmt(linked)} datasets"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-l">{esc(a)}</div><div class="kpi-v">{b}</div><div class="kpi-s">{esc(c)}</div></div>'
        for a, b, c in kpis)

    generated = now.strftime("%d %B %Y, %H:%M")
    css = CSS
    js_data = json.dumps({"years": chart_years, "series": chart_series})

    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DataverseNL report</title>
<style>{css}</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
</head><body>
<header class="top">
  {logo_tag()}
  <div>
    <p class="eyebrow">University Library · Research Data Management</p>
    <h1>DataverseNL at Maastricht University</h1>
    <p class="meta">Generated {esc(generated)} · Source: {esc(BASE_URL)} (Dataverse {esc(version)}) · Collection “{esc(ROOT_ALIAS)}”</p>
  </div>
</header>
{nav}
<main>
<section class="kpis">{kpi_html}</section>

<section><h2>New published datasets per year</h2>
<p class="lead">Counted by first publication date, attributed to the collection each dataset sits in today.</p>
<div class="chart"><canvas id="yearly"></canvas></div>
{table(y_headers, y_rows, y_total)}
<p class="note">* {this_year} is a partial year up to the generation date.</p></section>

<section><h2>New published datasets per month (last {MONTHS_IN_DETAIL} months)</h2>
<p class="lead">Darker cells are stronger months <em>for that collection</em>; the best month per collection is in bold
(only where it published at least 3 that month).
Months with a total of {HIGHLIGHT_THRESHOLD} or more are marked <span class="hit-key">like this</span>.</p>
{heat_table(m_headers, m_rows, HIGHLIGHT_THRESHOLD)}
<p class="note">* Current month is partial. The full monthly history is in <code>published_per_month.csv</code>.</p></section>

<section><h2>Created vs published</h2>
<p class="lead">“Created” counts datasets by the year they were created, but only those that have since been published:
the public API cannot see unpublished drafts. “Difference” is published relative to created.
A positive value means more datasets went public that year than were started that year, usually because
earlier work was published; a negative value means work started that year is published later.
It is <strong>not</strong> a publication rate. Unpublished drafts are counted on the admin page.</p>
{table(d_headers, d_rows)}</section>

<section><h2>Open and restricted files</h2>
{table(o_headers, o_rows, o_total)}</section>

<section><h2>Access requests</h2>
{access_html}</section>

<section><h2>Licences</h2>
{table(l_headers, l_rows, l_total)}
<p class="note">Datasets without a recorded licence are not included here; they are listed on the admin page.</p></section>

<section><h2>Storage, linkage to publications and collection structure</h2>
{table(s_headers, s_rows, s_total)}
<dl class="defs">
<dt>Linked to publication</dt><dd>The dataset's <em>Related Publication</em> field is filled in, normally with the
citation or DOI of the article the data supports. It shows how often data is deposited alongside a paper.
The field is free text, so it can also hold other links (for example a code repository).</dd>
<dt>Updated after first version</dt><dd>Datasets published more than once (version 1.1, 2.0 and so on).</dd>
<dt>Sub-collections</dt><dd>Collections nested inside the faculty collection: departments, research groups,
projects and, at SHE, one collection per PhD candidate. A high number usually means researchers create a collection
per project instead of depositing in the department collection.</dd>
</dl></section>

<section><h2>Methodology and caveats</h2>
<ul class="caveats">
<li>Publication counts come from the Dataverse Metrics API (<code>/api/info/metrics/datasets/monthly</code>), which reports cumulative totals; monthly figures are the differences.</li>
<li>Datasets are attributed to the collection they are in today. Moving a dataset moves its history; deaccessioned datasets are excluded.</li>
<li>Six collections are treated as faculties; the others are listed separately so that they are not silently dropped ({other_share} of all datasets).</li>
<li>Subject metadata is not used for the breakdown: most datasets carry the default subject, so a subject split would be misleading.</li>
<li>Views and citations are not reported: Make Data Count statistics are not available for these datasets on DataverseNL.</li>
</ul></section>

<section class="annex"><h2>Annex: downloads per year</h2>
<p class="note warn">Use with care. Download counts include automated traffic and many-file datasets.
Sudden jumps (for example a collection rising from hundreds to hundreds of thousands in one year) are not evidence of real reuse.</p>
{table(dl_headers, dl_rows)}</section>
</main>
<footer>Data: DataverseNL public API, plus authenticated calls where stated. Report script maintained by the University Library.</footer>
<script>
(function(){{
  const d = {js_data};
  const css = getComputedStyle(document.documentElement);
  const pal = ["--c1","--c2","--c3","--c4","--c5","--c6","--c7","--c8","--c9","--c10","--c11"].map(v=>css.getPropertyValue(v).trim());
  if (!window.Chart) {{ document.querySelector('.chart').style.display = 'none'; return; }}
  const ink = css.getPropertyValue('--muted').trim();
  const grid = css.getPropertyValue('--line').trim();
  new Chart(document.getElementById('yearly'), {{
    type: 'bar',
    data: {{ labels: d.years, datasets: d.series.map((s,i)=>({{label:s.label, data:s.data, backgroundColor: pal[i % pal.length], borderWidth:0}})) }},
    options: {{ responsive:true, maintainAspectRatio:false,
      plugins: {{ legend: {{ position:'bottom', labels: {{ color: ink, boxWidth:12 }} }} }},
      scales: {{ x: {{ stacked:true, ticks:{{color:ink}}, grid:{{display:false}} }},
                 y: {{ stacked:true, ticks:{{color:ink}}, grid:{{color:grid}} }} }} }}
  }});
}})();
</script>
</body></html>"""


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


def request_history_html(ordered: list[dict], admin: bool = False) -> str:
    s = tracker_summary()
    if s is None:
        return ('<p class="note">Access-request history is not being tracked yet. Once the daily tracker runs, '
                'this section shows requests per year per collection and the time to a decision.</p>')

    units = [u["short"] for u in ordered]
    units += sorted(set(s["by_unit_year"]) - set(units))
    years = sorted({y for d in s["by_unit_year"].values() for y in d})
    rows = []
    tot = {y: {"new": 0, "decided": 0, "days": []} for y in years}
    for un in units:
        d = s["by_unit_year"].get(un, {})
        if not d:
            continue
        row = [un]
        for y in years:
            c = d.get(y, {"new": 0, "decided": 0, "days": []})
            med = statistics.median(c["days"]) if c["days"] else None
            row += [c["new"], f"{med:g}" if med is not None else "–"]
            for k in ("new", "decided"):
                tot[y][k] += c[k]
            tot[y]["days"] += c["days"]
        rows.append(row)
    total_row = ["Total"]
    for y in years:
        med = statistics.median(tot[y]["days"]) if tot[y]["days"] else None
        total_row += [tot[y]["new"], f"{med:g}" if med is not None else "–"]
    headers = ["Collection"] + [h for y in years for h in (f"{y} requests", f"{y} median days")]
    write_csv("access_requests_per_year.csv", headers, rows + [total_row])

    precision = ("to the nearest day" if s["max_gap"] <= 1
                 else f"to within {s['max_gap']} days (the longest gap between two daily checks)")
    med_all = ("not yet available" if s["median_days"] is None
               else f"{s['median_days']:g} day" + ("" if s["median_days"] == 1 else "s"))
    out = (
        f'<p class="lead">Tracked daily since {esc(s["started"])} (last check {esc(s["last_run"])}). '
        f'Median time from request to decision: <strong>{med_all}</strong>'
        + (f'; {pct(s["within_14"], s["decided"])} decided within 14 days ({fmt(s["decided"])} decisions)'
           if s["decided"] else "")
        + f'. Currently waiting: {fmt(len(s["open"]))}.</p>'
        + (table(headers, rows, total_row) if rows else '<p class="note">No new requests recorded yet.</p>')
        + f'<p class="note">“Requests” counts requests by the day they first appeared; “median days” is the time until '
          f'they left the queue, measured {precision}. A decision can be a grant or a rejection: the API does not say '
          f'which. Requests made and decided between two daily checks are not seen, and nothing before '
          f'{esc(s["started"])} is included.'
        + (f' {fmt(s["baseline_open"])} requests were already waiting when tracking started; their start date is '
           f'unknown, so they are left out of the figures.' if s["baseline_open"] else "")
        + (f' {fmt(s["forbidden_files"])} restricted files cannot be checked with the current token, so these '
           f'figures undercount.' if s["forbidden_files"] else "")
        + '</p>')

    if admin and s["open"]:
        top = s["open"][:50]
        out += ('<h3>Oldest waiting requests</h3>'
                + html_list_table(["Collection", "Dataset", "Waiting (days)", ""],
                                  [[esc(r["unit"]), dataset_link(r["dataset_pid"], r["dataset"]),
                                    (f'{fmt(r["_waiting"])}+' if r["baseline"] else fmt(r["_waiting"])),
                                    (f'<a href="{esc(BASE_URL)}/permissions-manage-files.xhtml?id={esc(r["dataset_id"])}" '
                                     f'target="_blank" rel="noopener">Manage access</a>') if r.get("dataset_id") else ""]
                                   for r in top], {2})
                + '<p class="note">“+” means the request was already waiting when tracking started, so it has been '
                  'waiting at least this long.</p>')
    return out


def dataset_link(pid: str, title: str) -> str:
    if not pid:
        return esc(title)
    return f'<a href="{esc(BASE_URL)}/dataset.xhtml?persistentId={esc(pid)}" target="_blank" rel="noopener">{esc(title or pid)}</a>'


def html_list_table(headers: list[str], rows: list[list[str]], num_cols: set[int] = frozenset(),
                    wrap_cols: set[int] = frozenset({1})) -> str:
    """Table whose cells are already-escaped HTML (used for rows containing links)."""
    th = "".join(f'<th class="{"num" if i in num_cols else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(
        f'<td class="{"num" if i in num_cols else ("wrap" if i in wrap_cols else "")}">{c}</td>'
        for i, c in enumerate(r))
        + "</tr>" for r in rows)
    return f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def build_admin(units: list[dict], access: dict, lists: dict, version: str,
                nav: str = "", website: bool = False) -> str:
    hide_draft_titles = website and not PUBLISH_DRAFT_TITLES
    now = datetime.now(TZ)
    ordered = [u for u in units if u["datasets"] > 0]
    ordered = [u for u in ordered if u["is_faculty"]] + [u for u in ordered if not u["is_faculty"]]

    # ---- access requests
    if access.get("enabled"):
        a_headers = ["Collection", "Restricted files checked", "Files with pending requests",
                     "Pending requests", "No permission", "Errors"]
        a_rows = [[u["short"]] + [access["per_unit"][u["alias"]][k] for k in
                                  ("restricted_files", "files_with_requests", "pending_requests", "no_permission", "errors")]
                  for u in ordered if u["alias"] in access["per_unit"]]
        write_csv("access_requests_summary.csv", a_headers, a_rows, ADMIN_DIR)
        items = sorted(access["items"], key=lambda x: (-x["pending"], x["unit"], x["dataset"]))
        write_csv("access_requests_pending.csv", ["Collection", "Dataset", "DOI", "File", "File id", "Pending requests"],
                  [[i["unit"], i["dataset"], i["dataset_pid"], i["file"], i["file_id"], i["pending"]] for i in items],
                  ADMIN_DIR)
        rows = []
        for i in items:
            manage = (f'<a href="{esc(BASE_URL)}/permissions-manage-files.xhtml?id={esc(i["dataset_id"])}" '
                      f'target="_blank" rel="noopener">Manage access</a>') if i["dataset_id"] else ""
            rows.append([esc(i["unit"]), dataset_link(i["dataset_pid"], i["dataset"]), esc(i["file"]),
                         fmt(i["pending"]), manage])
        no_perm = sum(r[4] for r in a_rows)

        # ---- datasets the token cannot check, grouped by the collection where rights are granted
        np_items = sorted(access.get("no_permission", {}).values(),
                          key=lambda x: (x["unit"], x["collection"], x["dataset"]))
        write_csv("no_permission_datasets.csv",
                  ["Collection", "Sub-collection", "Sub-collection alias", "Dataset", "DOI", "Restricted files"],
                  [[i["unit"], i["collection"], i["collection_alias"], i["dataset"], i["pid"], i["files"]]
                   for i in np_items], ADMIN_DIR)
        groups: dict = {}
        for i in np_items:
            g = groups.setdefault((i["unit"], i["collection_alias"]),
                                  {"unit": i["unit"], "alias": i["collection_alias"], "name": i["collection"],
                                   "datasets": 0, "files": 0})
            g["datasets"] += 1
            g["files"] += i["files"]

        def coll_link(alias: str, name: str) -> str:
            if not alias:
                return esc(name or "unknown")
            return (f'<a href="{esc(BASE_URL)}/dataverse/{esc(alias)}" target="_blank" rel="noopener">'
                    f'{esc(name or alias)}</a>')

        group_rows = [[esc(g["unit"]), coll_link(g["alias"], g["name"]), fmt(g["datasets"]), fmt(g["files"])]
                      for g in sorted(groups.values(), key=lambda g: -g["files"])]
        ds_rows = [[esc(i["unit"]), dataset_link(i["pid"], i["dataset"]), coll_link(i["collection_alias"], i["collection"]),
                    fmt(i["files"])] for i in np_items]
        no_perm_html = "" if not np_items else (
            f'<h3>Datasets your account cannot check ({fmt(len(np_items))} datasets, {fmt(no_perm)} files)</h3>'
            '<p class="note">Rights in DataverseNL are usually granted per collection, so the first table shows '
            'where to ask. Giving the token\'s account the <em>Curator</em> role (or any role with '
            '“Manage File Permissions”) on these collections closes the gap.</p>'
            + html_list_table(["Collection", "Sub-collection", "Datasets", "Restricted files"], group_rows, {2, 3})
            + '<details><summary>Show every dataset</summary>'
            + html_list_table(["Collection", "Dataset", "Sub-collection", "Restricted files"], ds_rows, {3})
            + '</details>')

        access_html = (
            table(a_headers, a_rows)
            + (f'<p class="note warn">{fmt(no_perm)} restricted files could not be checked because the token\'s '
               f'account lacks curator rights on them, so these figures undercount. '
               f'They are listed further down.</p>' if no_perm else "")
            + "<h3>Files with pending requests</h3>"
            + (html_list_table(["Collection", "Dataset", "File", "Pending", ""], rows, {3}) if rows
               else '<p class="note">No pending requests found.</p>')
            + '<p class="note">Only <strong>pending</strong> requests are visible through the API: granted or rejected '
              'requests are no longer listed. Requesters\' names and e-mail addresses are deliberately not stored in '
              'this report; open “Manage access” in DataverseNL to see who is asking.</p>'
            + no_perm_html)
    else:
        access_html = f'<p class="note">Not included in this run: {esc(access.get("reason", ""))}</p>'

    # ---- drafts
    dr = lists["drafts"]
    if dr["enabled"]:
        d_items = sorted(dr["items"], key=lambda x: -(x["age_days"] or 0))
        per_unit = defaultdict(int)
        for i in d_items:
            per_unit[i["unit"]] += 1
        write_csv("unpublished_drafts.csv", ["Collection", "Title", "DOI", "Created", "Age (days)"],
                  [[i["unit"], i["title"], i["pid"], i["created"], i["age_days"]] for i in d_items], ADMIN_DIR)
        if hide_draft_titles:
            bands = [("Under 3 months", 0, 91), ("3 to 12 months", 91, 366), ("1 to 2 years", 366, 731),
                     ("Over 2 years", 731, 10 ** 6)]
            band_rows = []
            for u in ordered:
                ages = [i["age_days"] or 0 for i in d_items if i["unit"] == u["short"]]
                band_rows.append([u["short"]] + [sum(1 for a in ages if lo <= a < hi) for _, lo, hi in bands]
                                 + [len(ages)])
            band_total = ["Total"] + [sum(r[c] for r in band_rows) for c in range(1, len(bands) + 2)]
            drafts_html = (
                table(["Collection"] + [b[0] for b in bands] + ["Total"], band_rows, band_total)
                + '<p class="note">Titles of datasets that were never published are not public in DataverseNL, '
                  'so they are not listed on this website. The full list is in the downloadable admin report '
                  '(<code>unpublished_drafts.csv</code>). Only drafts the token\'s account can see are included.</p>')
        else:
            drafts_html = (
                table(["Collection", "Unpublished drafts"], [[u["short"], per_unit.get(u["short"], 0)] for u in ordered],
                      ["Total", len(d_items)])
                + "<h3>Drafts, oldest first</h3>"
                + (html_list_table(["Collection", "Dataset", "Created", "Age (days)"],
                                   [[esc(i["unit"]), dataset_link(i["pid"], i["title"]), esc(i["created"]),
                                     fmt(i["age_days"]) if i["age_days"] is not None else ""] for i in d_items], {3})
                   if d_items else '<p class="note">No unpublished drafts visible to this token.</p>')
                + '<p class="note">Only drafts the token\'s account can see are included.</p>')

        nv = sorted(dr.get("new_versions", []), key=lambda x: -(x["age_days"] or 0))
        write_csv("published_with_unpublished_changes.csv",
                  ["Collection", "Title", "DOI", "Draft last changed", "Age (days)"],
                  [[i["unit"], i["title"], i["pid"], i["changed"], i["age_days"]] for i in nv], ADMIN_DIR)
        if nv:
            drafts_html += (
                f'<h3>Published datasets with unpublished changes ({fmt(len(nv))})</h3>'
                '<p class="note">These are public, but someone has started a new version that has not been published. '
                'Opening the link shows the published version unless you are logged in with edit rights.</p>'
                + html_list_table(["Collection", "Dataset", "Draft last changed", "Age (days)", "What changed"],
                                  [[esc(i["unit"]), dataset_link(i["pid"], i["title"]), esc(i["changed"]),
                                    fmt(i["age_days"]) if i["age_days"] is not None else "",
                                    describe_changes(i.get("changes"))] for i in nv], {3}, wrap_cols={1, 4})
                + '<p class="note">“What changed” shows where the draft differs from the published version, not the '
                  'new values, because the draft is not public. To see the details, open the dataset while logged in, '
                  'go to the <em>Versions</em> tab, tick both versions and click <em>View Differences</em>.</p>')
    else:
        drafts_html = f'<p class="note">Not included in this run: {esc(dr["reason"])}</p>'

    # ---- no licence
    nl = sorted(lists["no_licence"], key=lambda x: (x["unit"], x["published"]))
    write_csv("datasets_without_licence.csv", ["Collection", "Title", "DOI", "Sub-collection", "Last published"],
              [[i["unit"], i["title"], i["pid"], i["collection"], i["published"]] for i in nl], ADMIN_DIR)
    nl_count = defaultdict(int)
    for i in nl:
        nl_count[i["unit"]] += 1
    licence_html = (
        table(["Collection", "Datasets without licence", "% of collection"],
              [[u["short"], nl_count.get(u["short"], 0), pct(nl_count.get(u["short"], 0), u["datasets"])] for u in ordered],
              ["Total", len(nl), pct(len(nl), sum(u["datasets"] for u in ordered))])
        + "<h3>Datasets without a licence</h3>"
        + html_list_table(["Collection", "Dataset", "Sub-collection", "Last published"],
                          [[esc(i["unit"]), dataset_link(i["pid"], i["title"]), esc(i["collection"]),
                            esc(i["published"])] for i in nl])
        + '<p class="note">These datasets have no licence in the search index; most predate the licence field. '
          'The fix is a new version with a licence, which needs the depositor or a curator.</p>')

    generated = now.strftime("%d %B %Y, %H:%M")
    return f"""<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>DataverseNL admin</title>
<style>{CSS}</style>
</head><body>
<header class="top">
  {logo_tag()}
  <div>
    <p class="eyebrow">University Library · Research Data Management · Curation</p>
    <h1>DataverseNL admin page</h1>
    <p class="meta">Generated {esc(generated)} · Source: {esc(BASE_URL)} (Dataverse {esc(version)})</p>
  </div>
</header>
{nav}
<main>
<div class="banner"><strong>For curators.</strong> Work lists for the RDM team: the access-request queue,
unpublished drafts and datasets that need a licence.</div>

<section><h2>Access requests over time</h2>
{request_history_html(ordered, admin=True)}</section>

<section><h2>Pending access requests</h2>
{access_html}</section>

<section><h2>Unpublished drafts</h2>
{drafts_html}</section>

<section><h2>Datasets without a licence</h2>
{licence_html}</section>
</main>
<footer>Data: DataverseNL API. Authenticated sections use the token's own permissions.</footer>
</body></html>"""


CSS = """
:root{--bg:#fafaf7;--card:#ffffff;--ink:#1d2433;--muted:#5b6475;--line:#e3e5ea;--accent:#001c3d;--warn:#8a4b00;
--c1:#001c3d;--c2:#e84e10;--c3:#4f7cac;--c4:#7a9a3a;--c5:#b6862c;--c6:#8c5a9e;--c7:#2f8f83;--c8:#a33b4a;--c9:#6b7a8f;--c10:#c9a227;--c11:#3d5a80}
@media (prefers-color-scheme: dark){:root{--bg:#11151c;--card:#181d26;--ink:#e8ebf0;--muted:#a3abb9;--line:#2a313d;--accent:#9db8e0;--warn:#f0b36b;
--c1:#9db8e0;--c2:#f08a5d;--c3:#7fa7d4;--c4:#a3c26b;--c5:#d9b062;--c6:#b993c9;--c7:#63bdb1;--c8:#d47786;--c9:#9aa7b8;--c10:#e3c65a;--c11:#7e9cc4}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.top{display:flex;gap:24px;align-items:center;padding:28px 16px;max-width:1100px;margin:0 auto;border-bottom:1px solid var(--line)}
.logo{height:56px;width:auto;flex:none}
.eyebrow{margin:0;color:var(--muted);font-size:13px;letter-spacing:.04em;text-transform:uppercase}
h1{margin:4px 0;font-size:26px;color:var(--accent)}
.meta{margin:0;color:var(--muted);font-size:13px}
main{max-width:1100px;margin:0 auto;padding:8px 16px 40px}
section{margin:32px 0}
h2{font-size:19px;margin:0 0 8px;color:var(--accent)}
h3{font-size:15px;margin:20px 0 8px}
.lead{margin:0 0 12px;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi-l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.kpi-v{font-size:26px;font-weight:650;margin:4px 0;font-variant-numeric:tabular-nums}
.kpi-s{font-size:13px;color:var(--muted)}
.chart{height:320px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{vertical-align:top;padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-weight:600;color:var(--muted);background:var(--card);position:sticky;top:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tfoot td{font-weight:650;border-top:2px solid var(--line)}
tbody tr:last-child td{border-bottom:none}
.note{font-size:13px;color:var(--muted);margin:8px 0}
.warn{color:var(--warn)}
.caveats{padding-left:20px;color:var(--ink)}
.caveats li{margin:4px 0}
code{font-size:12.5px}
:root{--heat-rgb:232,78,16}
@media (prefers-color-scheme: dark){:root{--heat-rgb:240,138,93}}
table.heat td.best{font-weight:700}
table.heat td.zero{color:var(--line)}
table.heat td.hit{box-shadow:inset 0 0 0 2px var(--accent);font-weight:700}
.hit-key{display:inline-block;padding:0 6px;border-radius:4px;box-shadow:inset 0 0 0 2px var(--accent);font-weight:700;color:var(--ink)}
.defs{margin:12px 0 0;font-size:13.5px}
.defs dt{font-weight:650;margin-top:8px}
.defs dd{margin:2px 0 0;color:var(--muted)}
.banner{background:rgba(var(--heat-rgb),.12);border:1px solid rgba(var(--heat-rgb),.5);border-radius:10px;padding:10px 14px;margin:16px 0;font-size:14px}
a{color:var(--accent)}
td.wrap{white-space:normal;min-width:260px;vertical-align:top}
.muted{color:var(--muted)}
nav.pages{max-width:1100px;margin:0 auto;padding:10px 16px 0;display:flex;gap:6px}
nav.pages a{padding:6px 14px;border-radius:999px;border:1px solid var(--line);text-decoration:none;
  font-size:14px;font-weight:600;color:var(--muted);background:var(--card)}
nav.pages a[aria-current]{color:var(--card);background:var(--accent);border-color:var(--accent)}
details{margin-top:12px}
summary{cursor:pointer;color:var(--accent);font-weight:600;font-size:14px;margin-bottom:8px}
footer{max-width:1100px;margin:0 auto;padding:16px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
@media print{.chart{break-inside:avoid}section{break-inside:avoid-page}}
"""


def main() -> int:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    ADMIN_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"DataverseNL report for '{ROOT_ALIAS}' at {BASE_URL}")
    print("Token: " + ("provided" if API_TOKEN else "not provided") +
          f"; access requests: {'on' if INCLUDE_ACCESS_REQUESTS else 'off'}")

    version = get("/api/info/version")["data"].get("version", "unknown")
    units = get_units()
    print(f"Found {len(units)} top-level collections. Collecting:")
    data = [collect_unit(u) for u in units]

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

    (PUBLIC_DIR / "report.html").write_text(build_report(data, access, version), encoding="utf-8")
    (PUBLIC_DIR / "raw_data.json").write_text(json.dumps(
        {"generated": datetime.now(TZ).isoformat(), "version": version,
         "units": [{k: v for k, v in u.items() if k != "pid_collection"} for u in data]},
        indent=2, default=str), encoding="utf-8")
    (ADMIN_DIR / "admin.html").write_text(build_admin(data, access, lists, version), encoding="utf-8")

    # Website for GitHub Pages: same pages, linked to each other.
    (SITE_DIR / "index.html").write_text(
        build_report(data, access, version, nav=nav_bar("index.html")), encoding="utf-8")
    (SITE_DIR / "admin.html").write_text(
        build_admin(data, access, lists, version, nav=nav_bar("admin.html"), website=True), encoding="utf-8")
    print(f"Done: {PUBLIC_DIR / 'report.html'} and {ADMIN_DIR / 'admin.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
