"""
DataverseNL report for Maastricht University.

Builds a single self-contained HTML page (output/report.html) plus CSV files
with the underlying numbers (output/*.csv).

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
    for attempt in range(4):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=60)
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
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

    result.update({"enabled": True, "per_unit": {}})

    def check(file_id):
        status, body = get(f"/api/access/datafile/{file_id}/listRequests", auth=True, allow_error=True)
        if status == 200:
            data = body.get("data")
            return ("ok", len(data) if isinstance(data, list) else 0)
        if status == 404:  # Dataverse answers 404 when a file has no pending requests
            return ("ok", 0)
        if status in (401, 403):
            return ("forbidden", 0)
        return ("error", 0)

    for u in units:
        files = search_all({"q": "*", "type": "file", "subtree": u["alias"], "fq": 'fileAccess:"Restricted"'})
        ids = [f["file_id"] for f in files if f.get("file_id")]
        print(f"  {u['short']}: checking {len(ids)} restricted files", flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(check, ids))
        result["per_unit"][u["alias"]] = {
            "restricted_files": len(ids),
            "files_with_requests": sum(1 for s, n in res if s == "ok" and n > 0),
            "pending_requests": sum(n for s, n in res if s == "ok"),
            "no_permission": sum(1 for s, _ in res if s == "forbidden"),
            "errors": sum(1 for s, _ in res if s == "error"),
        }
    return result


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


def write_csv(name: str, headers: list[str], rows: list[list]) -> None:
    with open(OUTPUT_DIR / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def build_report(units: list[dict], access: dict, version: str) -> str:
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
    l_headers = ["Collection"] + lic_names + ["No licence recorded"]
    l_rows = []
    for u in ordered:
        vals = [u["licences"].get(k, 0) for k in lic_names]
        l_rows.append([u["short"]] + vals + [max(u["datasets"] - sum(vals), 0)])
    l_total = ["Total"] + [sum(r[i] for r in l_rows) for i in range(1, len(l_headers))]
    write_csv("licences.csv", l_headers, l_rows + [l_total])
    no_licence = l_total[-1]

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
    d_headers = ["Year", "Published datasets created that year", "Datasets first published that year"]
    d_rows = [[str(yy), sum(int(u["deposit_by_year"].get(str(yy), 0)) for u in ordered),
               sum(year_sum(u["published_monthly"], yy) for u in ordered)] for yy in years[-6:]]
    write_csv("deposit_vs_publication.csv", d_headers, d_rows)

    # ---- downloads annex
    dl_years = years[-6:]
    dl_headers = ["Collection"] + [str(yy) for yy in dl_years]
    dl_rows = [[u["short"]] + [year_sum(u["downloads_monthly"], yy) for yy in dl_years] for u in ordered]
    write_csv("downloads_per_year.csv", dl_headers, dl_rows)

    # ---- access requests section
    if access.get("enabled"):
        a_headers = ["Collection", "Restricted files checked", "Files with pending requests",
                     "Pending requests", "No permission", "Errors"]
        a_rows = [[u["short"]] + [access["per_unit"][u["alias"]][k] for k in
                                  ("restricted_files", "files_with_requests", "pending_requests", "no_permission", "errors")]
                  for u in ordered if u["alias"] in access["per_unit"]]
        write_csv("access_requests_pending.csv", a_headers, a_rows)
        no_perm = sum(r[4] for r in a_rows)
        access_html = table(a_headers, a_rows) + (
            f'<p class="note warn">{fmt(no_perm)} restricted files could not be checked because the token\'s account '
            f'lacks curator rights on them, so these figures undercount.</p>' if no_perm else "") + \
            '<p class="note">Only <strong>pending</strong> requests are visible through the API. Requests that were ' \
            'granted or rejected are no longer listed, so this is a snapshot of the current queue, not a history.</p>'
    else:
        access_html = (f'<p class="note">Not included in this run: {esc(access.get("reason", ""))} '
                       f'The proxy below is the number of restricted files, which are the only files that can '
                       f'receive an access request.</p>')

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
        ("No licence recorded", fmt(no_licence), "datasets"),
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
<main>
<section class="kpis">{kpi_html}</section>

<section><h2>New published datasets per year</h2>
<p class="lead">Counted by first publication date, attributed to the collection each dataset sits in today.</p>
<div class="chart"><canvas id="yearly"></canvas></div>
{table(y_headers, y_rows, y_total)}
<p class="note">* {this_year} is a partial year up to the generation date.</p></section>

<section><h2>New published datasets per month (last {MONTHS_IN_DETAIL} months)</h2>
{table(m_headers, m_rows)}
<p class="note">* Current month is partial. The full monthly history is in <code>published_per_month.csv</code>.</p></section>

<section><h2>Created vs published</h2>
<p class="lead">Only published datasets are visible to the public API, so unpublished drafts are not counted here.
A difference between the columns means datasets created in one year were published in a later one.</p>
{table(d_headers, d_rows)}</section>

<section><h2>Open and restricted files</h2>
{table(o_headers, o_rows, o_total)}</section>

<section><h2>Access requests</h2>
{access_html}</section>

<section><h2>Licences</h2>
{table(l_headers, l_rows, l_total)}
<p class="note">“No licence recorded” is the number of published datasets without a licence in the search index, mostly older deposits.</p></section>

<section><h2>Storage, linkage to publications and collection structure</h2>
{table(s_headers, s_rows, s_total)}
<p class="note">“Linked to publication” means the Related Publication field is filled in.
Sub-collections counts every collection below the faculty level, including per-project and per-PhD collections.</p></section>

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
.lead{margin:0 0 12px;color:var(--muted)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpi-l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.kpi-v{font-size:26px;font-weight:650;margin:4px 0;font-variant-numeric:tabular-nums}
.kpi-s{font-size:13px;color:var(--muted)}
.chart{height:320px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-weight:600;color:var(--muted);background:var(--card);position:sticky;top:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tfoot td{font-weight:650;border-top:2px solid var(--line)}
tbody tr:last-child td{border-bottom:none}
.note{font-size:13px;color:var(--muted);margin:8px 0}
.warn{color:var(--warn)}
.caveats{padding-left:20px;color:var(--ink)}
.caveats li{margin:4px 0}
code{font-size:12.5px}
footer{max-width:1100px;margin:0 auto;padding:16px;color:var(--muted);font-size:12px;border-top:1px solid var(--line)}
@media print{.chart{break-inside:avoid}section{break-inside:avoid-page}}
"""


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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

    (OUTPUT_DIR / "report.html").write_text(build_report(data, access, version), encoding="utf-8")
    (OUTPUT_DIR / "raw_data.json").write_text(json.dumps(
        {"generated": datetime.now(TZ).isoformat(), "version": version, "units": data,
         "access_requests": access}, indent=2, default=str), encoding="utf-8")
    print(f"Done: {OUTPUT_DIR / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
