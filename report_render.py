"""
Builds the two DataverseNL reports as self-contained HTML pages, in every language in report_text.py.

  Executive report   for management: headline figures, trends, units compared, access and openness.
  Admin report       for curators and data stewards: work lists, detailed tables and the full method.

The figures are collected by dataverse_report.py; this file only turns them into pages and CSV files.
All text is in report_text.py; everything about the institution is in institution.json.
"""

from __future__ import annotations

import base64
import csv
import html
import json
import mimetypes
import re
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from report_text import MONTHS, MONTHS_SHORT, TEXT

JUMP_FACTOR = 10            # a download figure at least 10 times the year before is marked ▲ ...
JUMP_MINIMUM = 1000         # ... if it is at least this high
REQUEST_YEARS_SHOWN = 5     # years kept in the access-request table; the oldest drops off
MONTHS_IN_DETAIL = 24       # months in the monthly table of the admin report
TREND_YEARS = 10            # years in the dataset chart of the executive report
LOCALE = {"en": "en-GB", "nl": "nl-NL"}
THOUSANDS = {"en": ",", "nl": "."}
DECIMAL = {"en": ".", "nl": ","}

# Chart colours after the institution's own two; chosen to stay apart in light and dark mode.
EXTRA_SERIES = ["#4f7cac", "#7a9a3a", "#b6862c", "#8c5a9e", "#2f8f83", "#a33b4a", "#c9a227", "#3d5a80"]
OTHER_SERIES = "#9aa3b2"


def check_text() -> list[str]:
    """Keys missing a language: shown as a warning, so a half-finished translation is noticed."""
    langs = set(LOCALE)
    return [k for k, v in TEXT.items() if set(v) != langs]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
class Raw(str):
    """Text that is already HTML and must not be escaped again."""


def esc(x) -> str:
    return html.escape(str(x))


def ysum(series: dict, y: int) -> int:
    return sum(v for k, v in series.items() if k.startswith(f"{y}-"))


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _mix(h: str, other: str, w: float) -> str:
    a, b = _hex_rgb(h), _hex_rgb(other)
    return _rgb_hex([x + (y - x) * w for x, y in zip(a, b)])


def _lum(h: str) -> float:
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _hex_rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _readable(colour: str, background: str, towards: str, target: float = 4.5) -> str:
    """Lighten or darken a brand colour until text in it is readable on the background.
    Hue is kept and saturation held up, so a dark navy becomes a clear light blue, not grey."""
    import colorsys
    if _contrast(colour, background) >= target:
        return colour
    r, g, b = (c / 255 for c in _hex_rgb(colour))
    h, light, sat = colorsys.rgb_to_hls(r, g, b)
    sat = min(max(sat, 0.5), 0.75) if sat > 0.08 else sat  # greys stay grey
    for i in range(1, 41):
        lv = light + (1 - light) * i / 40 if towards == "#ffffff" else light * (1 - i / 40)
        c = _rgb_hex([x * 255 for x in colorsys.hls_to_rgb(h, lv, sat)])
        if _contrast(c, background) >= target:
            return c
    return towards


# --------------------------------------------------------------------------
# Language-neutral model: every figure both reports need, computed once
# --------------------------------------------------------------------------
def prepare(units: list[dict], access: dict, lists: dict, version: str, storage_total: int | None,
            tracker: dict | None, now: datetime, cfg: dict) -> dict:
    active = [u for u in units if u["datasets"] > 0]
    fac = [u for u in active if u["is_faculty"]]
    oth = [u for u in active if not u["is_faculty"]]
    ordered = fac + oth
    year = now.year
    first = min((int(k[:4]) for u in active for k in u["published_monthly"]), default=year)
    years = list(range(first, year + 1))

    missing_storage = [u["short"] for u in units if u.get("storage_bytes") is None]
    if storage_total is not None:
        total_bytes = storage_total
    elif len(missing_storage) < len(units):
        total_bytes = sum(u["storage_bytes"] for u in units if u.get("storage_bytes") is not None)
    else:
        total_bytes = None

    dl_years = list(range(year - 5, year + 1))
    est = ordered[0].get("_dl_estimates") if ordered else None
    dl = {u["alias"]: {y: (est["unit"].get(u["alias"], {}).get(y, 0) if est else ysum(u["downloads_monthly"], y))
                       for y in dl_years} for u in ordered}

    def published(y: int, upto: int = 12) -> int:
        return sum(u["published_monthly"].get(f"{y}-{mm:02d}", 0) for u in ordered for mm in range(1, upto + 1))

    done = now.month - 1  # complete months this year
    if done:
        compare = {"year": year, "months": done, "cur": published(year, done), "prev": published(year - 1, done)}
    else:  # January: compare the last two full years
        compare = {"year": year - 1, "months": 12, "cur": published(year - 1), "prev": published(year - 2)}

    files = sum(u["files"] for u in ordered)
    access_totals = {"public": 0, "restricted": 0, "embargo": 0}
    per_unit_access = {}
    for u in ordered:
        fa = u["file_access"]
        a = {"public": fa.get("Public", 0), "restricted": fa.get("Restricted", 0),
             "embargo": sum(v for k, v in fa.items() if k.lower().startswith("embargo"))}
        per_unit_access[u["alias"]] = a
        for k in a:
            access_totals[k] += a[k]

    return {
        "cfg": cfg, "now": now, "version": version, "units": units, "ordered": ordered, "fac": fac, "oth": oth,
        "this_year": year, "years": years, "dl_years": dl_years, "est": est, "dl": dl,
        "total_ds": sum(u["datasets"] for u in ordered), "files": files, "total_bytes": total_bytes,
        "missing_storage": missing_storage, "linked": sum(u["linked_to_publication"] for u in ordered),
        "access_files": per_unit_access, "access_totals": access_totals, "compare": compare,
        "new_this_year": {u["alias"]: ysum(u["published_monthly"], year) for u in ordered},
        "access": access, "lists": lists, "tracker": tracker,
    }


def downloads_model(m: dict) -> dict:
    """Downloads per year per collection and per direct sub-collection (dataset estimate if available)."""
    years, est = m["dl_years"], m["est"]
    out, csv_rows, hidden, unavailable = [], [], 0, []
    measure = "dataset downloads (estimate)" if est else "file downloads"
    for u in m["ordered"]:
        files_parent = [ysum(u["downloads_monthly"], y) for y in years]
        files_kids = {c["alias"]: ([ysum(c["monthly"], y) for y in years] if c["monthly"] is not None else None)
                      for c in u.get("child_downloads", [])}
        unavailable += [c["name"] for c in u.get("child_downloads", []) if c["monthly"] is None]
        if est:
            parent = [est["unit"].get(u["alias"], {}).get(y, 0) for y in years]
            kid_vals = {a: [est["child"].get(u["alias"], {}).get(a, {}).get(y, 0) for y in years] for a in files_kids}
            direct = [est["direct"].get(u["alias"], {}).get(y, 0) for y in years]
        else:
            parent, kid_vals, direct = files_parent, files_kids, None
        kids = []
        for c in u.get("child_downloads", []):
            vals = kid_vals[c["alias"]]
            if vals is None:
                continue
            if any(vals):
                kids.append({"name": c["name"], "alias": c["alias"], "vals": vals})
            else:
                hidden += 1
            csv_rows.append([u["short"], c["name"], measure] + vals)
            if est:
                fk = files_kids[c["alias"]]
                csv_rows.append([u["short"], c["name"], "file downloads"] + (fk if fk is not None else ["unavailable"] * len(years)))
        kids.sort(key=lambda k: -sum(k["vals"]))
        if direct is None:
            direct = [max(0, p - sum(k["vals"][i] for k in kids)) for i, p in enumerate(parent)]
        if kids and any(direct):
            kids.append({"name": None, "alias": "", "vals": direct})  # "Directly in ..."
        csv_rows.append([u["short"], "(total)", measure] + parent)
        if est:
            csv_rows.append([u["short"], "(total)", "file downloads"] + files_parent)
        out.append({"unit": u, "parent": parent, "kids": kids})
    return {"rows": out, "csv": csv_rows, "hidden": hidden, "unavailable": unavailable}


def requests_model(m: dict) -> dict | None:
    """Access requests per calendar year per collection, from the daily tracker."""
    s = m["tracker"]
    if not s:
        return None
    start_year = int(s["started"][:4])
    years = [str(y) for y in range(max(start_year, m["this_year"] - REQUEST_YEARS_SHOWN + 1), m["this_year"] + 1)]
    names = [u["short"] for u in m["units"] if u["is_faculty"]] + [u["short"] for u in m["units"] if not u["is_faculty"]]
    names += sorted(set(s["by_unit_year"]) - set(names))
    rows, tot = [], {y: {"new": 0, "days": []} for y in years}
    for name in names:
        cells = []
        for y in years:
            c = s["by_unit_year"].get(name, {}).get(y, {})
            cells.append((c.get("new", 0), list(c.get("days", []))))
            tot[y]["new"] += c.get("new", 0)
            tot[y]["days"] += c.get("days", [])
        rows.append((name, cells))
    return {"years": years, "rows": rows, "total": [(tot[y]["new"], tot[y]["days"]) for y in years], "s": s}


def licence_category(name: str) -> str:
    n = name.strip().upper().replace("-", " ")
    if n.startswith("CC0"):
        return "lic_cc0"
    if re.fullmatch(r"CC BY \d(\.\d)?( INTERNATIONAL)?", n):
        return "lic_ccby"
    if n.startswith("CC "):
        return "lic_other_cc"
    if "CUSTOM" in n:
        return "lic_custom"
    return "lic_other"


# --------------------------------------------------------------------------
# CSV files: written once, with English column names
# --------------------------------------------------------------------------
def _csv(folder: Path, name: str, headers: list, rows: list) -> None:
    with open(folder / name, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def _median_value(days: list):
    if not days:
        return ""
    v = statistics.median(days)
    return int(v) if float(v).is_integer() else round(v, 1)


def write_csvs(m: dict, public: Path, admin: Path) -> None:
    o, years = m["ordered"], m["years"]
    rows = [[u["short"]] + [ysum(u["published_monthly"], y) for y in years] + [u["datasets"]] for u in o]
    _csv(public, "published_per_year.csv", ["Collection"] + [str(y) for y in years] + ["Total"],
         rows + [["Total"] + [sum(r[i + 1] for r in rows) for i in range(len(years))] + [m["total_ds"]]])

    all_months = sorted({k for u in o for k in u["published_monthly"]})
    _csv(public, "published_per_month.csv", ["Month"] + [u["short"] for u in o] + ["Total"],
         [[mo] + [u["published_monthly"].get(mo, 0) for u in o] + [sum(u["published_monthly"].get(mo, 0) for u in o)]
          for mo in all_months])

    fa = m["access_files"]
    _csv(public, "file_access.csv", ["Collection", "Files", "Public", "Restricted", "Embargoed"],
         [[u["short"], u["files"], fa[u["alias"]]["public"], fa[u["alias"]]["restricted"], fa[u["alias"]]["embargo"]]
          for u in o])

    lic = sorted({k for u in o for k in u["licences"]}, key=lambda k: -sum(u["licences"].get(k, 0) for u in o))
    _csv(public, "licences.csv", ["Collection"] + lic + ["No licence"],
         [[u["short"]] + [u["licences"].get(k, 0) for k in lic]
          + [sum(1 for i in m["lists"]["no_licence"] if i["unit"] == u["short"])] for u in o])

    _csv(public, "storage_linkage_structure.csv",
         ["Collection", "Datasets", "Files", "Storage used (bytes)", "Linked to publication",
          "Updated after first version", "Sub-collections"],
         [[u["short"], u["datasets"], u["files"], u.get("storage_bytes"), u["linked_to_publication"],
           u["updated_after_v1"], u["subcollections"]] for u in o]
         + [["Total (whole collection)", m["total_ds"], m["files"], m["total_bytes"], m["linked"],
             sum(u["updated_after_v1"] for u in o), sum(u["subcollections"] for u in o)]])

    _csv(public, "deposit_vs_publication.csv", ["Year", "Created that year (and since published)", "First published that year"],
         [[y, sum(int(u["deposit_by_year"].get(str(y), 0)) for u in o), sum(ysum(u["published_monthly"], y) for u in o)]
          for y in years[-6:]])

    _csv(public, "downloads_per_year.csv",
         ["Collection", "Measure"] + [str(y) for y in m["dl_years"]],
         [[u["short"], "dataset downloads (estimate)" if m["est"] else "file downloads"]
          + [m["dl"][u["alias"]][y] for y in m["dl_years"]] for u in o]
         + [[u["short"], "file downloads"] + [ysum(u["downloads_monthly"], y) for y in m["dl_years"]] for u in o])
    _csv(public, "downloads_by_subcollection.csv", ["Collection", "Sub-collection", "Measure"]
         + [str(y) for y in m["dl_years"]], downloads_model(m)["csv"])

    rq = requests_model(m)
    if rq:
        _csv(public, "access_requests_per_year.csv",
             ["Collection"] + [h for y in rq["years"] for h in (f"{y} requests", f"{y} median days to decision")],
             [[name] + [v for new, days in cells for v in (new, _median_value(days))] for name, cells in rq["rows"]]
             + [["Total"] + [v for new, days in rq["total"] for v in (new, _median_value(days))]])

    # ---- admin work lists
    acc = m["access"]
    if acc.get("enabled"):
        _csv(admin, "access_requests_summary.csv",
             ["Collection", "Restricted files checked", "Files with requests", "Waiting requests", "No permission", "Errors"],
             [[u["short"]] + [acc["per_unit"][u["alias"]][k] for k in
                              ("restricted_files", "files_with_requests", "pending_requests", "no_permission", "errors")]
              for u in o if u["alias"] in acc["per_unit"]])
        _csv(admin, "access_requests_pending.csv", ["Collection", "Dataset", "DOI", "File", "File id", "Requesters"],
             [[i["unit"], i["dataset"], i["dataset_pid"], i["file"], i["file_id"], i["pending"]]
              for i in sorted(acc["items"], key=lambda x: (-x["pending"], x["unit"], x["dataset"]))])
        _csv(admin, "no_permission_datasets.csv",
             ["Collection", "Sub-collection", "Sub-collection alias", "Dataset", "DOI", "Restricted files"],
             [[i["unit"], i["collection"], i["collection_alias"], i["dataset"], i["pid"], i["files"]]
              for i in sorted(acc.get("no_permission", {}).values(), key=lambda x: (x["unit"], x["collection"], x["dataset"]))])
    dr = m["lists"]["drafts"]
    if dr["enabled"]:
        _csv(admin, "unpublished_drafts.csv", ["Collection", "Title", "DOI", "Created", "Age (days)"],
             [[i["unit"], i["title"], i["pid"], i["created"], i["age_days"]]
              for i in sorted(dr["items"], key=lambda x: -(x["age_days"] or 0))])
        _csv(admin, "published_with_unpublished_changes.csv",
             ["Collection", "Title", "DOI", "Draft last changed", "Age (days)", "Changed metadata fields",
              "Files added", "Files removed", "Files replaced", "Files with changed details", "Terms of access changed"],
             [[i["unit"], i["title"], i["pid"], i["changed"], i["age_days"]]
              + ([", ".join(c["fields"]), c["files_added"], c["files_removed"], c["files_replaced"],
                  c["files_changed"], ", ".join(c["terms"])] if (c := i.get("changes")) and c.get("ok") else [""] * 6)
              for i in sorted(dr.get("new_versions", []), key=lambda x: -(x["age_days"] or 0))])
    _csv(admin, "datasets_without_licence.csv", ["Collection", "Title", "DOI", "Sub-collection", "Last published"],
         [[i["unit"], i["title"], i["pid"], i["collection"], i["published"]]
          for i in sorted(m["lists"]["no_licence"], key=lambda x: (x["unit"], x["published"]))])


# --------------------------------------------------------------------------
# One page in one language
# --------------------------------------------------------------------------
class Page:
    def __init__(self, m: dict, lang: str, kind: str, links: dict, website: bool = False):
        self.m, self.lang, self.kind, self.links, self.website = m, lang, kind, links, website
        self.cfg = m["cfg"]
        self.base = self.cfg["base_url"]
        self._n = 0

    # ---- text and numbers
    def t(self, key: str, **kw) -> str:
        s = TEXT[key].get(self.lang) or TEXT[key]["en"]
        return s.format(**kw) if kw else s

    def setting(self, key: str) -> str:
        v = self.cfg["settings"].get(key, "")
        return (v.get(self.lang) or v.get("en", "")) if isinstance(v, dict) else str(v)

    def unit_word(self, many: bool = False) -> str:
        lab = self.cfg["settings"].get("main_unit_label", {}).get(self.lang) or {}
        return lab.get("many" if many else "one") or (self.t("collection").lower() + ("s" if many else ""))

    def n(self, x) -> str:
        if x is None:
            return "–"
        if isinstance(x, bool):
            return esc(x)
        if isinstance(x, int):
            return f"{x:,}".replace(",", THOUSANDS[self.lang])
        if isinstance(x, float):
            return self.dec(x)
        return esc(x)

    def dec(self, x: float, digits: int = 1) -> str:
        s = f"{x:,.{digits}f}"
        return s.replace(",", "\0").replace(".", DECIMAL[self.lang]).replace("\0", THOUSANDS[self.lang])

    def pct(self, a: int, b: int) -> str:
        if not b:
            return "–"
        v = 100 * a / b
        if 0 < v < 1:
            return "<1%"
        if 99 < v < 100:
            return ">99%"
        return f"{round(v)}%"

    def bytes(self, b: int | None) -> str:
        if b is None:
            return self.t("not_available")
        if b < 1000:
            return f"{b} B"
        x = float(b)
        for unit in ("KB", "MB", "GB", "TB"):
            x /= 1000
            if x < 1000 or unit == "TB":
                return f"{self.dec(x, 2 if unit == 'TB' else 1)} {unit}"
        return str(b)

    def median(self, days: list) -> str:
        if not days:
            return "–"
        v = statistics.median(days)
        return self.n(int(v)) if float(v).is_integer() else self.dec(v)

    def date(self, d) -> str:
        if isinstance(d, str):
            d = datetime.fromisoformat(d[:10]).date()
        return f"{d.day} {MONTHS[self.lang][d.month - 1]} {d.year}"

    def month(self, ym: str) -> str:
        y, mo = ym.split("-")
        return f"{MONTHS_SHORT[self.lang][int(mo) - 1]} {y}"

    def month_range(self, year: int, months: int) -> str:
        if months >= 12:
            return str(year)
        a, b = MONTHS_SHORT[self.lang][0], MONTHS_SHORT[self.lang][months - 1]
        return f"{a} {year}" if months == 1 else f"{a}–{b} {year}"

    def delta(self, cur: int, prev: int, period: str) -> str:
        if not prev:
            return self.t("delta_none", period=period) if cur else ""
        change = round(100 * (cur - prev) / prev)
        if change == 0:
            return self.t("delta_same", period=period)
        return self.t("delta_up" if change > 0 else "delta_down", pct=f"{abs(change)}%", period=period)

    def reason(self, text: str) -> str:
        """Why a part was skipped: the data script says it in English, the page says it in its own language."""
        low = (text or "").lower()
        if "not requested" in low:
            return self.t("reason_not_requested")
        if "no api token" in low:
            return self.t("reason_no_token")
        m = re.search(r"http (\d{3})", low)
        return self.t("reason_http", code=m.group(1)) if m else text

    # ---- links
    def dataset_link(self, pid: str, title: str) -> Raw:
        if not pid:
            return Raw(esc(title))
        return Raw(f'<a href="{esc(self.base)}/dataset.xhtml?persistentId={esc(pid)}" target="_blank" '
                   f'rel="noopener">{esc(title or pid)}</a>')

    def collection_link(self, alias: str, name: str) -> Raw:
        if not alias:
            return Raw(esc(name or self.t("unknown")))
        return Raw(f'<a href="{esc(self.base)}/dataverse/{esc(alias)}" target="_blank" rel="noopener">'
                   f'{esc(name or alias)}</a>')

    # ---- tables
    def table(self, headers: list, rows: list, total: list | None = None, num=None, wrap=frozenset(),
              cls: str = "", tid: str = "", group_at: int | None = None, group_label: str = "",
              full: bool = False) -> str:
        num = set(range(1, len(headers))) if num is None else set(num)

        def cell(i: int, c, tag: str = "td") -> str:
            k = (["num"] if i in num else []) + (["wrap"] if i in wrap else [])
            if isinstance(c, Raw):
                txt = c
            elif isinstance(c, (int, float)) and not isinstance(c, bool):
                txt = self.n(c)
            else:
                txt = esc(c)
            scope = ' scope="row"' if tag == "th" else ""
            return f'<{tag}{scope}{" class=" + chr(34) + " ".join(k) + chr(34) if k else ""}>{txt}</{tag}>'

        th = "".join(f'<th scope="col"{" class=num" if i in num else ""}>{h if isinstance(h, Raw) else esc(h)}</th>'
                     for i, h in enumerate(headers))
        body = ""
        for r_i, r in enumerate(rows):
            if group_at is not None and r_i == group_at:
                body += f'<tr class="group"><th colspan="{len(headers)}" scope="rowgroup">{esc(group_label)}</th></tr>'
            body += "<tr>" + "".join(cell(i, c) for i, c in enumerate(r)) + "</tr>"
        foot = ("<tfoot><tr>" + "".join(cell(i, c) for i, c in enumerate(total)) + "</tr></tfoot>") if total else ""
        idattr = f' id="{tid}"' if tid else ""
        return (f'<div class="tbl{" full" if full else ""}"><table{idattr}{" class=" + chr(34) + cls + chr(34) if cls else ""}>'
                f'<thead><tr>{th}</tr></thead><tbody>{body}</tbody>{foot}</table></div>')

    def work_list(self, tid: str, headers: list, rows: list, num=frozenset(), wrap=frozenset({1})) -> str:
        """A list curators work through: filter box and sortable columns."""
        if len(rows) < 8:
            return self.table(headers, rows, num=num, wrap=wrap, cls="list sortable", tid=tid, full=True)
        return (f'<div class="filter"><label>{esc(self.t("filter_label"))} '
                f'<input type="search" data-filter="{tid}" autocomplete="off"></label>'
                f'<span class="fcount" data-for="{tid}" data-tpl="{esc(self.t("filter_count", n="{n}", m="{m}"))}" '
                f'aria-live="polite"></span></div>'
                + self.table(headers, rows, num=num, wrap=wrap, cls="list sortable", tid=tid, full=True))

    def heading(self, tag: str, text: str, info: str = "", hid: str = "") -> str:
        """A heading; with `info`, an (i) button next to it opens the explanation."""
        idattr = f' id="{hid}"' if hid else ""
        if not info:
            return f"<{tag}{idattr}>{esc(text)}</{tag}>"
        self._n += 1
        pid = f"info-{self._n}"
        return (f'<div class="hwrap"><{tag}{idattr}>{esc(text)}</{tag}>'
                f'<button type="button" class="info-btn" aria-expanded="false" aria-controls="{pid}" '
                f'aria-label="{esc(self.t("info_about", title=text))}" title="{esc(self.t("info"))}">i</button></div>'
                f'<div class="info-panel" id="{pid}" hidden>{info}</div>')

    @staticmethod
    def pair(*parts: str) -> str:
        """Two blocks side by side on a wide screen, one under the other on a narrow one."""
        return '<div class="pair">' + "".join(f"<div>{x}</div>" for x in parts if x) + "</div>"

    # ---- page frame
    def logo(self) -> str:
        path = Path(self.cfg.get("logo") or "")
        if not path.is_file():
            return ""
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f'<img class="logo" src="data:{mime};base64,{b64}" alt="{esc(self.setting("name"))}">'

    def css_tokens(self) -> str:
        colours = self.cfg["settings"].get("colours", {})
        primary = colours.get("primary") or "#1f3a5f"
        accent = colours.get("accent") or "#c8553d"
        light_bg, dark_bg = "#ffffff", "#171c24"
        brand_l = _readable(primary, light_bg, "#000000")
        brand_d = _readable(primary, dark_bg, "#ffffff")
        accent_d = _readable(accent, dark_bg, "#ffffff", 3.0)
        series_l = [brand_l, accent] + EXTRA_SERIES
        series_d = [brand_d, accent_d] + [_mix(c, "#ffffff", 0.25) for c in EXTRA_SERIES]

        def block(brand, acc, series, dark):
            r, g, b = _hex_rgb(acc)
            pr, pg, pb = _hex_rgb(series[0])
            s = ";".join(f"--s{i + 1}:{c}" for i, c in enumerate(series))
            return (f"--brand:{brand};--accent:{acc};--heat-rgb:{r},{g},{b};{s};"
                    f"--s-other:{_mix(OTHER_SERIES, '#ffffff', 0.2) if dark else OTHER_SERIES};"
                    f"--s-partial:rgba({pr},{pg},{pb},.45);--s-emb:{'#c9a227' if not dark else '#e3c65a'}")
        light = block(brand_l, accent, series_l, False)
        dark = block(brand_d, accent_d, series_d, True)
        return f":root{{{light}}}@media (prefers-color-scheme:dark){{:root{{{dark}}}}}"

    def nav(self, sections: list[tuple[str, str]]) -> str:
        other_kind = "admin" if self.kind == "exec" else "exec"
        reports = ""
        if self.links.get(other_kind):
            items = [("exec", self.t("report_exec")), ("admin", self.t("report_admin"))]
            reports = (f'<div class="seg" role="group" aria-label="{esc(self.t("nav_reports"))}">' + "".join(
                f'<a href="{esc(self.links[k][self.lang])}"{" aria-current=page" if k == self.kind else ""}>{esc(lab)}</a>'
                for k, lab in items) + "</div>")
        langs = (f'<div class="seg lang" role="group" aria-label="{esc(self.t("nav_language"))}">' + "".join(
            f'<a href="{esc(self.links[self.kind][lg])}" lang="{lg}" hreflang="{lg}" title="{esc(TEXT["lang_name"][lg])}"'
            f'{" aria-current=true" if lg == self.lang else ""}>{lg.upper()}</a>' for lg in LOCALE) + "</div>")
        toc = "".join(f'<a href="#{sid}">{lab}</a>' for sid, lab in sections)
        return (f'<nav class="bar" aria-label="{esc(self.t("nav_sections"))}"><div class="wrap bar-in">{reports}'
                f'<div class="toc">{toc}</div>{langs}</div></nav>')

    def frame(self, title: str, description: str, eyebrow: str, intro: str, sections: list[tuple[str, str]],
              body: str, extra_head: str = "", data: dict | None = None) -> str:
        m = self.m
        meta = self.t("meta", date=esc(self.date(m["now"].date())), url=esc(self.base),
                      alias=esc(self.cfg["root_alias"]), version=esc(m["version"]))
        when = f'{self.date(m["now"].date())}, {m["now"].strftime("%H:%M")}'
        payload = json.dumps(data).replace("</", "<\\/") if data else ""
        data_tag = f'<script type="application/json" id="report-data">{payload}</script>' if data else ""
        return f"""<!doctype html>
<html lang="{LOCALE[self.lang]}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {esc(self.setting("name"))}</title>
<meta name="description" content="{esc(description)}">
{extra_head}<style>{self.css_tokens()}{CSS}</style>
</head><body>
<a class="skip" href="#main">{esc(self.t("skip"))}</a>
<header class="top"><div class="wrap top-in">
  {self.logo()}
  <div>
    <p class="eyebrow">{esc(eyebrow)}</p>
    <h1>{esc(self.setting("name"))}</h1>
    <p class="meta">{meta}</p>
  </div>
</div></header>
{self.nav(sections)}
<main id="main" class="wrap">
<p class="intro">{intro}</p>
{body}
</main>
<footer class="wrap">{self.t("footer", when=esc(when))}</footer>
{data_tag}<script>{JS}</script>
</body></html>"""

    # ---- shared sections
    def requests_section(self, full: bool) -> tuple[str, str]:
        rq = requests_model(self.m)
        if rq is None:
            return f'<p class="note">{self.t("req_not_tracked")}</p>', ""
        s = rq["s"]
        headers = [self.t("collection")] + [Raw(esc(h)) for y in rq["years"]
                                            for h in (self.t("col_req_requests", year=y), self.t("col_req_median", year=y))]
        rows = [[name] + [v for new, days in cells for v in (new, self.median(days))] for name, cells in rq["rows"]]
        total = [self.t("total")] + [v for new, days in rq["total"] for v in (new, self.median(days))]
        lead = self.t("req_lead", start=esc(self.date(s["started"])), start_year=s["started"][:4],
                      keep=REQUEST_YEARS_SHOWN, last=esc(self.date(s["last_run"])))
        out = f'<p class="lead">{lead}</p>' + self.table(headers, rows, total, cls="req")
        info = (f'<p>{self.t("req_note_requests")}</p><p>{self.t("req_note_median")}</p>'
                f'<p>{self.t("req_note_sameday")}</p>')
        if full:
            precision = (self.t("precision_day") if s["max_gap"] <= 1 else self.t("precision_gap", n=s["max_gap"]))
            extra = self.t("req_note_decision", precision=precision)
            if s["baseline_open"]:
                extra += " " + (self.t("req_baseline_one") if s["baseline_open"] == 1
                                else self.t("req_baseline_many", n=self.n(s["baseline_open"])))
            if s["forbidden_files"]:
                extra += " " + self.t("req_forbidden", n=self.n(s["forbidden_files"]))
            info += f"<p>{extra}</p>"
        return out, info


# --------------------------------------------------------------------------
# Executive report
# --------------------------------------------------------------------------
def build_executive(m: dict, lang: str, links: dict) -> str:
    p = Page(m, lang, "exec", links)
    year, o = m["this_year"], m["ordered"]
    last_full = year - 1
    rq = requests_model(m)
    name = p.setting("name")

    # ---- headline figures
    cmp_ = m["compare"]
    cur_period = p.month_range(cmp_["year"], cmp_["months"])
    prev_period = p.month_range(cmp_["year"] - 1, cmp_["months"])
    dl_cur = sum(m["dl"][u["alias"]][last_full] for u in o)
    dl_prev = sum(m["dl"][u["alias"]][last_full - 1] for u in o)
    at = m["access_totals"]
    files = m["files"]

    if rq:
        cy = str(year)
        days_cy = [d for _, cells in rq["rows"] for (new, days), y in zip(cells, rq["years"]) if y == cy for d in days]
        new_cy = sum(new for _, cells in rq["rows"] for (new, _d), y in zip(cells, rq["years"]) if y == cy)
        if days_cy:
            req_value = p.median(days_cy) + f' <span class="unit">{p.t("days" if p.median(days_cy) != "1" else "day")}</span>'
            req_sub = p.t("kpi_requests_sub", n=p.n(new_cy), year=year, d=p.n(len(days_cy)))
        else:
            req_value, req_sub = "–", p.t("kpi_requests_none", date=esc(p.date(rq["s"]["started"])))
    else:
        req_value, req_sub = "–", p.t("kpi_requests_off")

    kpis = [
        (p.t("kpi_datasets"), p.n(m["total_ds"]), p.t("files_count", n=p.n(files))),
        (p.t("kpi_new", period=esc(cur_period)), p.n(cmp_["cur"]), p.delta(cmp_["cur"], cmp_["prev"], esc(prev_period))),
        (p.t("kpi_storage"), esc(p.bytes(m["total_bytes"])), p.t("kpi_storage_sub")),
        (p.t("kpi_downloads" if m["est"] else "kpi_file_downloads", year=last_full), p.n(dl_cur),
         p.delta(dl_cur, dl_prev, str(last_full - 1))),
        (p.t("kpi_open"), p.pct(at["public"], files),
         p.t("kpi_open_sub", pct=p.pct(at["restricted"], files), emb=p.pct(at["embargo"], files))),
        (p.t("kpi_requests"), req_value, req_sub),
    ]
    kpi_html = '<div class="kpis">' + "".join(
        f'<div class="kpi"><p class="kpi-l">{a}</p><p class="kpi-v">{b}</p><p class="kpi-s">{c}</p></div>'
        for a, b, c in kpis) + "</div>"

    # ---- trends
    t_years = m["years"][-TREND_YEARS:]
    labels = [str(y) if y != year else p.t("so_far", year=y) for y in t_years]
    series = [{"label": u["short"], "data": [ysum(u["published_monthly"], y) for y in t_years],
               "colour": f"--s{(i % 10) + 1}"} for i, u in enumerate(m["fac"])]
    if m["oth"]:
        series.append({"label": p.t("other_collections"),
                       "data": [sum(ysum(u["published_monthly"], y) for u in m["oth"]) for y in t_years],
                       "colour": "--s-other"})
    ds_table = p.table([p.t("collection")] + labels,
                       [[s["label"]] + s["data"] for s in series],
                       [p.t("total")] + [sum(s["data"][i] for s in series) for i in range(len(t_years))])
    dl_labels = [str(y) if y != year else p.t("so_far", year=y) for y in m["dl_years"]]
    dl_values = [sum(m["dl"][u["alias"]][y] for u in o) for y in m["dl_years"]]
    dl_title = p.t("trend_downloads" if m["est"] else "trend_file_downloads")
    dl_table = p.table([p.t("year"), p.t("downloads")], [[lab, v] for lab, v in zip(dl_labels, dl_values)])
    unit_one = p.unit_word()

    def chart(cid: str, title: str, lead: str, tbl: str) -> str:
        return (f'<figure class="card chart"><h3>{esc(title)}</h3><p class="lead">{lead}</p>'
                f'<div class="canvas"><canvas id="{cid}" role="img" aria-label="{esc(title)}"></canvas></div>'
                f'<details><summary>{esc(p.t("show_table"))}</summary>{tbl}</details></figure>')

    trends_html = ('<div class="charts">'
                   + chart("c-datasets", p.t("trend_datasets"), p.t("trend_datasets_lead", unit=esc(unit_one)), ds_table)
                   + chart("c-downloads", dl_title,
                           p.t("trend_downloads_lead" if m["est"] else "trend_file_downloads_lead"), dl_table)
                   + "</div>")

    # ---- units compared
    tb = m["total_bytes"]

    def share(b):
        if b is None or not tb:
            return Raw('<span class="muted">–</span>')
        w = 100 * b / tb
        return Raw(f'<span class="share"><span class="track"><i style="width:{min(w, 100):.1f}%"></i></span>'
                   f'<span>{p.pct(b, tb)}</span></span>')

    def unit_row(u):
        a = m["access_files"][u["alias"]]
        return [u["short"], u["datasets"], m["new_this_year"][u["alias"]], p.bytes(u.get("storage_bytes")),
                share(u.get("storage_bytes")), p.pct(a["public"], u["files"]),
                p.pct(u["linked_to_publication"], u["datasets"]), m["dl"][u["alias"]][last_full]]

    cmp_headers = [p.t("collection"), p.t("col_datasets"), p.t("col_new", year=year), p.t("col_storage"),
                   p.t("col_storage_share"), p.t("col_open"), p.t("col_linked"),
                   p.t("col_downloads" if m["est"] else "col_file_downloads", year=last_full)]
    cmp_total = [p.t("total"), m["total_ds"], sum(m["new_this_year"].values()), p.bytes(tb), "",
                 p.pct(at["public"], files), p.pct(m["linked"], m["total_ds"]),
                 sum(m["dl"][u["alias"]][last_full] for u in o)]
    units_html = (f'<p class="lead">{p.t("units_lead", year=year, date=esc(p.date(m["now"].date())))}</p>'
                  + p.table(cmp_headers, [unit_row(u) for u in o], cmp_total, num={1, 2, 3, 5, 6, 7},
                            cls="compare", group_at=len(m["fac"]) if m["fac"] and m["oth"] else None,
                            group_label=p.t("other_collections")))

    # ---- access and openness
    def stack_row(label: str, a: dict, n_files: int, strong: bool = False) -> str:
        if not n_files:
            return ""
        parts = [("pub", a["public"], p.t("legend_public")), ("res", a["restricted"], p.t("legend_restricted")),
                 ("emb", a["embargo"], p.t("legend_embargo"))]
        aria = ", ".join(f"{p.pct(v, n_files)} {lab.lower()}" for _, v, lab in parts if v)
        segs = "".join(f'<span class="s-{k}" style="width:{100 * v / n_files:.2f}%"></span>' for k, v, _ in parts if v)
        return (f'<div class="srow{" strong" if strong else ""}"><span class="slab">{esc(label)}</span>'
                f'<span class="stack" role="img" aria-label="{esc(label)}: {esc(aria)}">{segs}</span>'
                f'<span class="sfig">{p.pct(a["public"], n_files)} <span class="muted">· '
                f'{p.t("files_count", n=p.n(n_files))}</span></span></div>')

    legend = ('<p class="legend">' + "".join(
        f'<span><i class="s-{k}"></i>{esc(p.t(key))}</span>'
        for k, key in (("pub", "legend_public"), ("res", "legend_restricted"), ("emb", "legend_embargo"))) + "</p>")
    bars = (stack_row(p.t("total"), at, files, strong=True)
            + "".join(stack_row(u["short"], m["access_files"][u["alias"]], u["files"]) for u in o))

    lic_counts: dict = defaultdict(int)
    for u in o:
        for k, v in u["licences"].items():
            lic_counts[licence_category(k)] += v
    no_lic = len(m["lists"]["no_licence"])
    lic_counts["lic_none"] += no_lic
    lic_order = ["lic_cc0", "lic_ccby", "lic_other_cc", "lic_custom", "lic_other", "lic_none"]
    lic_total = sum(lic_counts.values())
    lic_cols = {"lic_cc0": "--s1", "lic_ccby": "--s3", "lic_other_cc": "--s4", "lic_custom": "--s5",
                "lic_other": "--s6", "lic_none": "--s-other"}
    lic_bar = ""
    if lic_total:
        segs = "".join(f'<span style="width:{100 * lic_counts[k] / lic_total:.2f}%;background:var({lic_cols[k]})"></span>'
                       for k in lic_order if lic_counts[k])
        aria = ", ".join(f"{p.t(k)} {p.pct(lic_counts[k], lic_total)}" for k in lic_order if lic_counts[k])
        lic_bar = (f'<span class="stack big" role="img" aria-label="{esc(aria)}">{segs}</span>'
                   '<ul class="lic-legend">' + "".join(
                       f'<li><i style="background:var({lic_cols[k]})"></i><span>{esc(p.t(k))}</span>'
                       f'<span class="num">{p.n(lic_counts[k])}</span><span class="num muted">{p.pct(lic_counts[k], lic_total)}</span></li>'
                       for k in lic_order if lic_counts[k]) + "</ul>")
    req_body, req_info = p.requests_section(full=False)
    access_html = (p.pair(f'<div class="card"><h3>{esc(p.t("access_files_title", unit=p.t("collection").lower()))}</h3>'
                          f'{legend}{bars}</div>',
                          f'<div class="card"><h3>{esc(p.t("licences_title"))}</h3>{lic_bar}'
                          + (f'<p class="note">{p.t("licences_note", n=p.n(no_lic))}</p>' if no_lic else "")
                          + '</div>')
                   + p.heading("h3", p.t("requests_title"), req_info) + req_body)

    # ---- about
    about = [p.t("about_source", alias=esc(m["cfg"]["root_alias"]), url=esc(p.base)), p.t("about_attribution"),
             p.t("about_period", year=year, date=esc(p.date(m["now"].date()))),
             p.t("about_downloads"), p.t("about_storage")]
    if rq:
        about.append(p.t("about_requests", date=esc(p.date(rq["s"]["started"]))))
    if links.get("admin"):
        about.append(p.t("about_more", href=esc(links["admin"][lang])))
    about_html = '<ul class="about">' + "".join(f"<li>{a}</li>" for a in about) + "</ul>"

    units_title = p.t("sec_units", units=p.unit_word(many=True)).capitalize()
    sections = [("headlines", p.t("sec_headlines")), ("trends", p.t("sec_trends")), ("units", units_title),
                ("access", p.t("sec_access")), ("about", p.t("sec_about"))]
    infos = {"units": f'<p>{p.t("units_note", name=esc(name))}</p>'}
    body = "".join(f'<section id="{sid}" aria-labelledby="h-{sid}">{p.heading("h2", title, infos.get(sid, ""), f"h-{sid}")}'
                   f'{content}</section>'
                   for (sid, title), content in zip(sections, [kpi_html, trends_html, units_html, access_html, about_html]))
    data = {"locale": LOCALE[lang],
            "datasets": {"labels": labels, "series": series},
            "downloads": {"labels": dl_labels, "data": dl_values, "label": dl_title}}
    return p.frame(p.t("title_exec"), p.t("desc_exec", name=name),
                   f'{p.t("report_exec")} · {p.setting("department")}', p.t("exec_intro", name=esc(name)),
                   [(sid, esc(title)) for sid, title in sections], body,
                   extra_head='<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js" '
                              'defer></script>\n', data=data)


# --------------------------------------------------------------------------
# Admin report
# --------------------------------------------------------------------------
def describe_changes(p: Page, c: dict | None) -> Raw:
    """Where the draft differs from the published version; never the new values."""
    if not c:
        return Raw(f'<span class="muted">{esc(p.t("chg_not_checked"))}</span>')
    if not c["ok"]:
        reason = (p.t("chg_no_permission") if c["status"] in (401, 403)
                  else p.t("chg_no_answer") if c["status"] == "error" else p.t("chg_http", code=c["status"]))
        return Raw(f'<span class="muted">{esc(reason)}</span>')
    parts = []
    if c["fields"]:
        shown = c["fields"][:6]
        more = len(c["fields"]) - len(shown)
        parts.append(f'<strong>{esc(p.t("chg_metadata"))}:</strong> {esc(", ".join(shown))}'
                     + (" " + esc(p.t("chg_more", n=more)) if more else ""))
    bits = [p.t(key, n=c[k]) for k, key in (("files_added", "chg_added"), ("files_removed", "chg_removed"),
                                             ("files_replaced", "chg_replaced"), ("files_changed", "chg_details")) if c[k]]
    if bits:
        parts.append(f'<strong>{esc(p.t("chg_files"))}:</strong> ' + esc(", ".join(bits)))
    if c["terms"]:
        parts.append(f'<strong>{esc(p.t("chg_terms"))}:</strong> {esc(", ".join(c["terms"]))}')
    if not parts:
        return Raw(f'<span class="muted">{esc(p.t("chg_none"))}</span>')
    return Raw("<br>".join(parts))


def _waiting_html(p: Page, live_items: list | None) -> tuple[str, str, int]:
    """All waiting requests in one table: the live check says what waits now, the tracker how long."""
    s = p.m["tracker"]
    waiting: dict = {}
    if s:
        for r in s["open"]:
            w = waiting.setdefault(str(r["file_id"]), {"days": -1, "baseline": False, "count": 0, "meta": r})
            w["count"] += 1
            if r["_waiting"] > w["days"]:
                w["days"], w["baseline"] = r["_waiting"], r["baseline"]
    rows = []
    if live_items is not None:
        for i in live_items:
            w = waiting.get(str(i["file_id"]))
            rows.append({"unit": i["unit"], "pid": i["dataset_pid"], "dataset": i["dataset"], "dataset_id": i["dataset_id"],
                         "file": i["file"], "requesters": i["pending"],
                         "days": w["days"] if w else None, "baseline": w["baseline"] if w else False})
    else:
        for fid, w in waiting.items():
            mt = w["meta"]
            rows.append({"unit": mt["unit"], "pid": mt["dataset_pid"], "dataset": mt["dataset"],
                         "dataset_id": mt.get("dataset_id", ""), "file": p.t("file_n", id=fid), "requesters": w["count"],
                         "days": w["days"], "baseline": w["baseline"]})
    if not rows:
        return f'<p class="note">{esc(p.t("no_pending"))}</p>', "", 0
    rows.sort(key=lambda r: (r["days"] is None, -(r["days"] or 0), r["unit"], r["dataset"]))

    def waiting_cell(r) -> str:
        if s is None:
            return "–"
        if r["days"] is None:
            return p.t("new_today")
        return f'{p.n(r["days"])}+' if r["baseline"] else p.n(r["days"])

    def manage(r) -> Raw:
        if not r["dataset_id"]:
            return Raw("")
        return Raw(f'<a href="{esc(p.base)}/permissions-manage-files.xhtml?id={esc(r["dataset_id"])}" '
                   f'target="_blank" rel="noopener">{esc(p.t("manage_access"))}</a>')

    tbl = p.work_list("t-waiting", [p.t("collection"), p.t("col_dataset"), p.t("col_file"), p.t("col_requesters"),
                                    p.t("col_waiting_days"), p.t("col_manage")],
                      [[r["unit"], p.dataset_link(r["pid"], r["dataset"]), r["file"], r["requesters"],
                        waiting_cell(r), manage(r)] for r in rows], num={3, 4}, wrap={1, 2})
    note = p.t("waiting_note") if s else p.t("waiting_note_off")
    if live_items is None and s:
        note += " " + p.t("waiting_from_tracker", date=esc(p.date(s["last_run"])))
    note += " " + p.t("waiting_privacy")
    return tbl, f"<p>{note}</p>", sum(r["requesters"] for r in rows)


def build_admin(m: dict, lang: str, links: dict, website: bool) -> str:
    p = Page(m, lang, "admin", links, website)
    o = m["ordered"]
    hide_titles = website and not m["cfg"]["publish_draft_titles"]
    name = p.setting("name")
    counts = {}

    # ---- waiting requests
    acc = m["access"]
    if acc.get("enabled"):
        a_rows = [[u["short"]] + [acc["per_unit"][u["alias"]][k] for k in
                                  ("restricted_files", "files_with_requests", "pending_requests", "no_permission", "errors")]
                  for u in o if u["alias"] in acc["per_unit"]]
        a_total = [p.t("total")] + [sum(r[i] for r in a_rows) for i in range(1, 6)]
        no_perm = a_total[4]
        items = sorted(acc["items"], key=lambda x: (-x["pending"], x["unit"], x["dataset"]))
        waiting_list, waiting_info, counts["waiting"] = _waiting_html(p, items)

        np_items = sorted(acc.get("no_permission", {}).values(), key=lambda x: (x["unit"], x["collection"], x["dataset"]))
        groups: dict = {}
        for i in np_items:
            g = groups.setdefault((i["unit"], i["collection_alias"]), {"unit": i["unit"], "alias": i["collection_alias"],
                                                                       "name": i["collection"], "datasets": 0, "files": 0})
            g["datasets"] += 1
            g["files"] += i["files"]
        no_perm_groups = "" if not np_items else (
            p.heading("h3", p.t("no_perm_title", n=p.n(len(np_items)), f=p.n(no_perm)),
                      f'<p>{p.t("no_perm_note")}</p>', "no-permission")
            + p.table([p.t("collection"), p.t("col_subcollection"), p.t("col_datasets"), p.t("col_restricted_files")],
                      [[g["unit"], p.collection_link(g["alias"], g["name"]), g["datasets"], g["files"]]
                       for g in sorted(groups.values(), key=lambda g: -g["files"])], num={2, 3}, wrap={1}))
        no_perm_list = "" if not np_items else (
            f'<details><summary>{esc(p.t("show_every_dataset"))}</summary>'
            + p.work_list("t-noperm", [p.t("collection"), p.t("col_dataset"), p.t("col_subcollection"),
                                       p.t("col_restricted_files")],
                          [[i["unit"], p.dataset_link(i["pid"], i["dataset"]),
                            p.collection_link(i["collection_alias"], i["collection"]), i["files"]] for i in np_items],
                          num={3}, wrap={1, 2})
            + "</details>")
        s_headers = [p.t("collection"), p.t("col_restricted_checked"), p.t("col_files_with_requests"),
                     p.t("col_pending"), p.t("col_no_permission"), p.t("col_errors")]
        s_info = ""
        tr = m["tracker"]
        if tr:
            since = p.date(tr["started"])
            s_headers += [p.t("col_since_requests", date=since), p.t("col_since_decided", date=since)]
            byu = tr["by_unit_year"]
            for r in a_rows:
                cells = byu.get(r[0], {}).values()
                r += [sum(c.get("new", 0) for c in cells), sum(c.get("decided", 0) for c in cells)]
            a_total += [sum(r[6] for r in a_rows), sum(r[7] for r in a_rows)]
            s_info = f'<p>{p.t("per_collection_note")}</p>'
        summary = p.heading("h3", p.t("per_collection"), s_info) + p.table(s_headers, a_rows, a_total)
        waiting_html = (
            p.heading("h3", p.t("waiting_list_title"), waiting_info) + waiting_list
            + (f'<p class="note warn">{p.t("no_perm_warn", n=p.n(no_perm))}</p>' if no_perm else "")
            + summary + no_perm_groups + no_perm_list)
    else:
        waiting_html = f'<p class="note">{esc(p.t("queue_not_checked", reason=p.reason(acc.get("reason", ""))))}</p>'
        if m["tracker"]:
            lst, waiting_info, counts["waiting"] = _waiting_html(p, None)
            waiting_html += p.heading("h3", p.t("waiting_list_title"), waiting_info) + lst

    # ---- drafts
    dr = m["lists"]["drafts"]
    infos = {}
    if dr["enabled"]:
        d_items = sorted(dr["items"], key=lambda x: -(x["age_days"] or 0))
        counts["drafts"] = len(d_items)
        if hide_titles:
            bands = [("band_3m", 0, 91), ("band_12m", 91, 366), ("band_2y", 366, 731), ("band_old", 731, 10 ** 6)]
            rows = []
            for u in o:
                ages = [i["age_days"] or 0 for i in d_items if i["unit"] == u["short"]]
                rows.append([u["short"]] + [sum(1 for a in ages if lo <= a < hi) for _, lo, hi in bands] + [len(ages)])
            drafts_html = p.table([p.t("collection")] + [p.t(b[0]) for b in bands] + [p.t("total")], rows,
                                  [p.t("total")] + [sum(r[c] for r in rows) for c in range(1, len(bands) + 2)])
            infos["drafts"] = f'<p>{p.t("drafts_hidden_note")}</p>'
        else:
            per_unit = defaultdict(int)
            for i in d_items:
                per_unit[i["unit"]] += 1
            drafts_html = (
                p.table([p.t("collection"), p.t("col_drafts")], [[u["short"], per_unit.get(u["short"], 0)] for u in o],
                        [p.t("total"), len(d_items)])
                + p.heading("h3", p.t("drafts_list_title"))
                + (p.work_list("t-drafts", [p.t("collection"), p.t("col_dataset"), p.t("col_created"), p.t("col_age")],
                               [[i["unit"], p.dataset_link(i["pid"], i["title"]), i["created"],
                                 i["age_days"] if i["age_days"] is not None else ""] for i in d_items], num={3})
                   if d_items else f'<p class="note">{esc(p.t("no_drafts"))}</p>'))
            infos["drafts"] = f'<p>{esc(p.t("drafts_token_note"))}</p>'
        nv = sorted(dr.get("new_versions", []), key=lambda x: -(x["age_days"] or 0))
        counts["changed"] = len(nv)
        changed_html = (
            f'<p class="lead">{p.t("changed_lead")}</p>'
            + p.work_list("t-changed", [p.t("collection"), p.t("col_dataset"), p.t("col_draft_changed"), p.t("col_age"),
                                        p.t("col_what_changed")],
                          [[i["unit"], p.dataset_link(i["pid"], i["title"]), i["changed"],
                            i["age_days"] if i["age_days"] is not None else "", describe_changes(p, i.get("changes"))]
                           for i in nv], num={3}, wrap={1, 4})) if nv else f'<p class="note">{esc(p.t("no_changed"))}</p>'
        if nv:
            infos["changed"] = f'<p>{p.t("changed_note")}</p>'
    else:
        drafts_html = changed_html = f'<p class="note">{esc(p.t("drafts_not_included", reason=p.reason(dr["reason"])))}</p>'

    # ---- no licence
    nl = sorted(m["lists"]["no_licence"], key=lambda x: (x["unit"], x["published"]))
    counts["no_licence"] = len(nl)
    nl_count = defaultdict(int)
    for i in nl:
        nl_count[i["unit"]] += 1
    licence_html = (
        p.table([p.t("collection"), p.t("col_no_licence"), p.t("col_pct_collection")],
                [[u["short"], nl_count.get(u["short"], 0), p.pct(nl_count.get(u["short"], 0), u["datasets"])] for u in o],
                [p.t("total"), len(nl), p.pct(len(nl), m["total_ds"])])
        + (p.heading("h3", p.t("no_licence_list_title"))
           + p.work_list("t-nolicence", [p.t("collection"), p.t("col_dataset"), p.t("col_subcollection"),
                                         p.t("col_last_published")],
                         [[i["unit"], p.dataset_link(i["pid"], i["title"]), i["collection"], i["published"]] for i in nl],
                         num=set(), wrap={1, 2}) if nl else ""))
    infos["no-licence"] = f'<p>{p.t("no_licence_note")}</p>'

    detail_html = _detail_tables(p)
    method = [p.t("m_source"), p.t("m_attribution"),
              p.t("m_units", n=len(m["cfg"]["settings"].get("main_units", {})), units=esc(p.unit_word(many=True)),
                  share=p.pct(sum(u["datasets"] for u in m["oth"]), m["total_ds"])),
              p.t("m_subject"), p.t("m_views"), p.t("m_storage"), p.t("about_downloads"), p.t("m_comparison"),
              p.t("m_csv")]
    method_html = '<ul class="about">' + "".join(f"<li>{x}</li>" for x in method) + "</ul>"
    req_body, infos["requests"] = p.requests_section(full=True)

    def lab(key, k=None):
        c = counts.get(k) if k else None
        return esc(p.t(key)) + (f' <span class="count">{p.n(c)}</span>' if c else "")

    sections = [("waiting", "sec_waiting", "nav_waiting", "waiting", waiting_html),
                ("requests", "sec_requests_year", "nav_requests_year", None, req_body),
                ("drafts", "sec_drafts", "nav_drafts", "drafts", drafts_html),
                ("changed", "sec_changed", "nav_changed", "changed", changed_html),
                ("no-licence", "sec_no_licence", "nav_no_licence", "no_licence", licence_html),
                ("detail", "sec_detail", "nav_detail", None, detail_html),
                ("method", "sec_method", "nav_method", None, method_html)]
    parts = {sid: f'<section id="{sid}" aria-labelledby="h-{sid}">{p.heading("h2", p.t(h), infos.get(sid, ""), f"h-{sid}")}'
                  f'{c}</section>' for sid, h, _n, _k, c in sections}
    side_by_side = hide_titles or not dr["enabled"]  # the drafts section is then a short table, not a long list
    body = ""
    for sid, *_ in sections:
        if sid == "requests" and side_by_side:
            body += f'<div class="pair secpair">{parts["requests"]}{parts["drafts"]}</div>'
        elif sid == "drafts" and side_by_side:
            continue
        else:
            body += parts[sid]
    return p.frame(p.t("title_admin"), p.t("desc_admin", name=name), f'{p.t("report_admin")} · {p.setting("department")}',
                   p.t("admin_intro"), [(sid, lab(nk, k)) for sid, _h, nk, k, _c in sections], body,
                   extra_head='<meta name="robots" content="noindex">\n')


def _heat_table(p: Page, headers: list, rows: list, threshold: int) -> str:
    """Each number shaded against the best month in its own column; the best month is bold."""
    ncols = len(headers)
    col_max = [max((r[i] for r in rows if isinstance(r[i], int)), default=0) for i in range(ncols)]
    th = "".join(f'<th scope="col"{" class=num" if i else ""}>{esc(h)}</th>' for i, h in enumerate(headers))
    body = ""
    for r in rows:
        cells = [f'<th scope="row">{esc(r[0])}</th>']
        for i in range(1, ncols):
            v, cls, style = r[i], ["num"], ""
            if v and col_max[i]:
                scale = max(col_max[i], 4)  # a best month of 1 or 2 must not look as strong as 15
                style = f' style="background:rgba(var(--heat-rgb),{0.08 + 0.42 * v / scale:.2f})"'
                if v == col_max[i] and col_max[i] >= 3:
                    cls.append("best")
            elif not v:
                cls.append("zero")
            if i == ncols - 1 and v >= threshold:
                cls.append("hit")
            cells.append(f'<td class="{" ".join(cls)}"{style}>{p.n(v)}</td>')
        body += "<tr>" + "".join(cells) + "</tr>"
    return f'<div class="tbl"><table class="heat"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _detail_tables(p: Page) -> str:
    m = p.m
    o, years = m["ordered"], m["years"]
    year = m["this_year"]
    today = esc(p.date(m["now"].date()))
    col = p.t("collection")
    out = []

    # per year
    rows = [[u["short"]] + [ysum(u["published_monthly"], y) for y in years] + [u["datasets"]] for u in o]
    out.append(p.heading("h3", p.t("det_per_year"), f'<p>{p.t("det_per_year_lead", year=year, date=today)}</p>', "d-year")
               + p.table([col] + [str(y) for y in years] + [p.t("total")], rows,
                         [p.t("total")] + [sum(r[i + 1] for r in rows) for i in range(len(years))] + [m["total_ds"]]))

    # per month
    months, y_, mo = [], m["now"].year, m["now"].month
    for _ in range(MONTHS_IN_DETAIL):
        months.append(f"{y_}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y_, mo = y_ - 1, 12
    months.reverse()
    m_rows = []
    for ym in months:
        vals = [u["published_monthly"].get(ym, 0) for u in o]
        m_rows.append([p.month(ym) + (" *" if ym == months[-1] else "")] + vals + [sum(vals)])
    thr = m["cfg"]["highlight_threshold"]
    out.append(p.heading("h3", p.t("det_per_month", n=MONTHS_IN_DETAIL), f'<p>{p.t("det_per_month_lead", t=thr)}</p>',
                         "d-month")
               + _heat_table(p, [p.t("month")] + [u["short"] for u in o] + [p.t("total")], m_rows, thr))

    # created vs published
    d_rows = []
    for y in years[-6:]:
        created = sum(int(u["deposit_by_year"].get(str(y), 0)) for u in o)
        pub = sum(ysum(u["published_monthly"], y) for u in o)
        diff = f"{100 * (pub - created) / created:+.0f}%" if created else "–"
        d_rows.append([str(y), created, pub, diff])
    created_html = (p.heading("h3", p.t("det_created"), f'<p>{p.t("det_created_lead")}</p>', "d-created")
                    + p.table([p.t("year"), p.t("col_created_year"), p.t("col_published_year"), p.t("col_difference")],
                              d_rows))

    # open and restricted
    fa = m["access_files"]
    at = m["access_totals"]
    o_rows = [[u["short"], u["files"], fa[u["alias"]]["public"], fa[u["alias"]]["restricted"], fa[u["alias"]]["embargo"],
               p.pct(fa[u["alias"]]["restricted"], u["files"])] for u in o]
    open_html = (p.heading("h3", p.t("det_open"), hid="d-open")
               + p.table([col, p.t("col_files"), p.t("col_public"), p.t("col_restricted"), p.t("col_embargoed"),
                          p.t("col_pct_restricted")], o_rows,
                         [p.t("total"), m["files"], at["public"], at["restricted"], at["embargo"],
                          p.pct(at["restricted"], m["files"])]))
    out.append(p.pair(open_html, created_html))

    # licences
    lic = sorted({k for u in o for k in u["licences"]}, key=lambda k: -sum(u["licences"].get(k, 0) for u in o))
    l_rows = [[u["short"]] + [u["licences"].get(k, 0) for k in lic] for u in o]
    out.append(p.heading("h3", p.t("det_licences"), f'<p>{esc(p.t("det_licences_note"))}</p>', "d-licences")
               + p.table([col] + lic, l_rows, [p.t("total")] + [sum(r[i] for r in l_rows) for i in range(1, len(lic) + 1)]))

    # storage and structure
    s_rows = [[u["short"], u["datasets"], u["files"], p.bytes(u.get("storage_bytes")), u["linked_to_publication"],
               p.pct(u["linked_to_publication"], u["datasets"]), u["updated_after_v1"], u["subcollections"]] for u in o]
    s_total = [p.t("total"), m["total_ds"], m["files"], p.bytes(m["total_bytes"]), m["linked"],
               p.pct(m["linked"], m["total_ds"]), sum(u["updated_after_v1"] for u in o), sum(u["subcollections"] for u in o)]
    missing = (f'<p class="note warn">{esc(p.t("storage_missing", names=", ".join(m["missing_storage"])))}</p>'
               if m["missing_storage"] else "")
    defs = (f'<dl class="defs"><dt>{esc(p.t("col_linked_n"))}</dt><dd>{p.t("def_linked")}</dd>'
            f'<dt>{esc(p.t("col_updated"))}</dt><dd>{p.t("def_updated")}</dd>'
            f'<dt>{esc(p.t("col_subcollections"))}</dt><dd>{p.t("def_sub")}</dd></dl>'
            f'<p>{p.t("m_storage")}</p>')
    out.append(p.heading("h3", p.t("det_storage"), defs, "d-storage")
               + p.table([col, p.t("col_datasets"), p.t("col_files"), p.t("col_storage_used"), p.t("col_linked_n"),
                          p.t("col_pct_linked"), p.t("col_updated"), p.t("col_subcollections")], s_rows, s_total)
               + missing)

    out.append(_downloads_html(p))
    return "".join(out)


def _downloads_html(p: Page) -> str:
    m = p.m
    years, est = m["dl_years"], m["est"]
    dm = downloads_model(m)

    def cells(values: list[int]) -> str:
        res = ""
        for i, v in enumerate(values):
            prev = values[i - 1] if i else None
            jump = prev is not None and v >= JUMP_MINIMUM and v >= JUMP_FACTOR * max(prev, 1)
            mark = (f' <span class="jump" title="{esc(p.t("dl_jump", f=JUMP_FACTOR))}" '
                    f'aria-label="{esc(p.t("dl_jump", f=JUMP_FACTOR))}">▲</span>') if jump else ""
            res += f'<td class="num">{p.n(v)}{mark}</td>'
        return res

    body = ""
    for n, row in enumerate(dm["rows"]):
        u, kids = row["unit"], row["kids"]
        gid = f"dl{n}"
        if kids:
            label = (f'<button type="button" class="expand" aria-expanded="false" aria-controls="{gid}">'
                     f'<span class="chev" aria-hidden="true">▸</span>{esc(u["short"])}'
                     f'<span class="count">{len(kids)}</span></button>')
        else:
            label = f'<span class="noexpand">{esc(u["short"])}</span>'
        body += f'<tbody class="grp"><tr class="parent"><th scope="row">{label}</th>{cells(row["parent"])}</tr></tbody>'
        if kids:
            body += f'<tbody id="{gid}" class="kids" hidden>'
            for k in kids:
                klabel = (p.collection_link(k["alias"], k["name"]) if k["name"] is not None
                          else f'<span class="muted">{esc(p.t("dl_directly", name=u["short"]))}</span>')
                body += f'<tr class="child"><th scope="row" class="wrap">{klabel}</th>{cells(k["vals"])}</tr>'
            body += "</tbody>"

    if est:
        tot_files = sum(est["file_downloads"].get(y, 0) for y in years)
        tot_unlinked = sum(est["unlinked"].get(y, 0) for y in years)
        tot_unplaced = sum(est["unplaced"].get(y, 0) for y in years)
        cav = [p.t("dl_cav_low"), p.t("dl_cav_bots"), p.t("dl_cav_repeat")]
        if tot_unlinked:
            cav.append(p.t("dl_cav_unlinked", pct=p.pct(tot_unlinked, tot_files), n=p.n(tot_unlinked), m=p.n(tot_files)))
        if tot_unplaced:
            cav.append(p.t("dl_cav_unplaced", n=p.n(tot_unplaced), name=esc(p.setting("name"))))
        method = (f'<p><strong>{esc(p.t("dl_method_title"))}</strong></p><p>{p.t("dl_method_1")}</p>'
                  f'<p><strong>{esc(p.t("dl_caveats"))}</strong></p><ul>' + "".join(f"<li>{c}</li>" for c in cav)
                  + f'</ul><p>{p.t("dl_csv_note")}</p>')
        title = p.t("det_downloads")
    else:
        method = f'<p class="note warn">{p.t("dl_fallback")}</p>'
        title = p.t("det_file_downloads")
    extra = ""
    if dm["hidden"]:
        extra += " " + (p.t("dl_hidden_one") if dm["hidden"] == 1 else p.t("dl_hidden_many", n=p.n(dm["hidden"])))
    if dm["unavailable"]:
        extra += " " + p.t("dl_unavailable", names=esc(", ".join(dm["unavailable"]))) + " " + (
            p.t("dl_unavailable_est") if est else p.t("dl_unavailable_files"))
    jump_note = f'<p><span class="jump">▲</span> {esc(p.t("dl_jump_note", f=JUMP_FACTOR, m=p.n(JUMP_MINIMUM)))}</p>'
    info = (method if est else "") + (f"<p>{extra.strip()}</p>" if extra else "") + jump_note
    warn = f'<p class="note warn">{p.t("dl_warn")}</p>' + ("" if est else method)
    head = "".join(f'<th scope="col" class="num">{y}</th>' for y in years)
    return (p.heading("h3", title, info, "d-downloads") + warn
            + f'<p class="lead">{p.t("dl_lead")}</p>'
            f'<p><button type="button" class="expand-all" data-show="{esc(p.t("dl_show_all"))}" '
            f'data-hide="{esc(p.t("dl_hide_all"))}">{esc(p.t("dl_show_all"))}</button></p>'
            f'<div class="tbl"><table class="dl"><thead><tr><th scope="col">{esc(p.t("collection"))}</th>{head}</tr></thead>'
            f'{body}</table></div>')


# --------------------------------------------------------------------------
# Style and behaviour (shared by both reports)
# --------------------------------------------------------------------------
CSS = """
:root{--bg:#f5f6f8;--card:#ffffff;--ink:#1a2130;--muted:#566173;--line:#e1e4ea;--soft:#eceff3;--warn:#8a4b00;--warn-bg:#fff5e6;
--shadow:0 1px 2px rgba(16,24,40,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c24;--ink:#e6e9ef;--muted:#a0a9b7;--line:#2a313c;--soft:#222934;
--warn:#f0b36b;--warn-bg:#2b2218;--shadow:none}}
*{box-sizing:border-box}
html{scroll-padding-top:64px}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-text-size-adjust:100%}
.wrap{max-width:1160px;margin:0 auto;padding-left:16px;padding-right:16px}
a{color:var(--brand)}
.skip{position:absolute;left:-999px;top:8px;background:var(--card);padding:8px 12px;border-radius:6px;z-index:20}
.skip:focus{left:8px}
:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.top{background:var(--card);border-bottom:1px solid var(--line)}
.top-in{display:flex;gap:24px;align-items:center;padding-top:24px;padding-bottom:20px}
.logo{height:52px;width:auto;flex:none}
@media (prefers-color-scheme:dark){.logo{background:#fff;padding:6px;border-radius:6px}}
.eyebrow{margin:0;color:var(--muted);font-size:13px;letter-spacing:.04em;text-transform:uppercase;font-weight:600}
h1{margin:2px 0 4px;font-size:28px;line-height:1.2;color:var(--brand);letter-spacing:-.01em}
.meta{margin:0;color:var(--muted);font-size:13px}
.bar{position:sticky;top:0;z-index:10;background:var(--card);border-bottom:1px solid var(--line)}
.bar-in{display:flex;align-items:center;gap:12px;min-height:52px}
.toc{display:flex;gap:2px;overflow-x:auto;flex:1;scrollbar-width:none;padding:8px 0}
.toc::-webkit-scrollbar{display:none}
.toc a{white-space:nowrap;padding:6px 10px;border-radius:6px;color:var(--muted);text-decoration:none;font-size:14px;font-weight:550}
.toc a:hover{background:var(--soft);color:var(--ink)}
.seg{display:flex;flex:none;border:1px solid var(--line);border-radius:8px;padding:2px;background:var(--bg)}
.seg a{padding:5px 11px;border-radius:6px;text-decoration:none;font-size:13.5px;font-weight:600;color:var(--muted);white-space:nowrap}
.seg a[aria-current]{background:var(--brand);color:var(--card)}
.seg.lang a{padding:5px 9px}
.count{display:inline-block;min-width:20px;font-size:11.5px;font-weight:700;padding:0 6px;border-radius:999px;
background:var(--soft);color:var(--ink);text-align:center;margin-left:4px;font-variant-numeric:tabular-nums}
main{padding-top:8px;padding-bottom:48px}
.intro{font-size:16px;color:var(--muted);margin:20px 0 4px;max-width:760px}
section{padding:28px 0 8px}
section+section{border-top:1px solid var(--line);margin-top:20px}
h2{font-size:22px;margin:0 0 14px;color:var(--brand);letter-spacing:-.01em}
h3{font-size:16px;margin:24px 0 8px}
.card h3{margin-top:0}
.lead{margin:0 0 12px;color:var(--muted);max-width:820px}
.note{font-size:13.5px;color:var(--muted);margin:10px 0;max-width:900px}
.note.warn{color:var(--warn);background:var(--warn-bg);padding:8px 12px;border-radius:8px}
.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:0 0 16px;box-shadow:var(--shadow)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:12px}
@media (max-width:640px){.kpis{grid-template-columns:1fr 1fr}.kpi{padding:12px}.kpi-v{font-size:24px!important}}
.kpi{background:var(--card);border:1px solid var(--line);border-top:3px solid var(--brand);border-radius:12px;padding:16px;margin:0;box-shadow:var(--shadow)}
.kpi p{margin:0}
.kpi-l{font-size:13px;color:var(--muted);font-weight:600}
.kpi-v{font-size:32px;font-weight:700;line-height:1.15;margin:6px 0 4px!important;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.kpi-v .unit{font-size:16px;font-weight:600;color:var(--muted);letter-spacing:0}
.kpi-s{font-size:13px;color:var(--muted)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,480px),1fr));gap:16px}
figure{margin:0}
.chart .canvas{position:relative;height:300px}
.chart.nochart .canvas{display:none}
.chart details{margin-top:10px}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;box-shadow:var(--shadow);width:fit-content;max-width:100%}
.tbl.full{width:auto}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:8px 24px;align-items:start}
.pair>div,.pair>section{min-width:0}
.secpair{border-top:1px solid var(--line);margin-top:20px}
.secpair>section{border-top:none;margin-top:0}
section+.secpair,.secpair+section{border-top:1px solid var(--line)}
.pair .card{height:100%}
.hwrap{display:flex;align-items:center;gap:10px;margin:24px 0 8px}
.hwrap>h2,.hwrap>h3{margin:0}
section>.hwrap:first-child{margin:0 0 14px}
.info-btn{flex:none;width:22px;height:22px;border-radius:50%;border:1.5px solid var(--brand);background:var(--card);color:var(--brand);font:italic 700 13px/1 Georgia,'Times New Roman',serif;cursor:pointer;display:inline-grid;place-items:center;padding:0}
.info-btn:hover,.info-btn[aria-expanded=true]{background:var(--brand);color:var(--card)}
.info-panel{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--brand);border-radius:8px;padding:4px 16px;margin:0 0 12px;font-size:14px;max-width:920px;color:var(--ink)}
.info-panel p,.info-panel li{margin:8px 0}
.info-panel ul{padding-left:20px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
thead th{font-weight:650;color:var(--muted);background:var(--card);font-size:12.5px;vertical-align:bottom;white-space:normal;min-width:64px}
tbody th{font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums}
tfoot td{font-weight:700;border-top:2px solid var(--line);border-bottom:none}
tbody tr:last-child td,tbody tr:last-child th{border-bottom:none}
tr.group th{background:var(--soft);color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:6px 10px}
td.wrap,th.wrap{white-space:normal;min-width:240px}
.share{display:flex;align-items:center;gap:8px;justify-content:flex-end}
.share .track{display:block;width:90px;height:8px;border-radius:4px;background:var(--soft);overflow:hidden}
.share i{display:block;height:100%;background:var(--brand);border-radius:4px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:0 0 10px;font-size:13px;color:var(--muted)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i,.lic-legend i{display:inline-block;width:10px;height:10px;border-radius:3px}
.s-pub{background:var(--s1)}.s-res{background:var(--accent)}.s-emb{background:var(--s-emb)}
.srow{display:grid;grid-template-columns:minmax(70px,120px) 1fr auto;gap:12px;align-items:center;padding:5px 0;font-size:14px}
.srow.strong{font-weight:700;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:4px}
.slab{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sfig{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;font-size:13px}
.stack{display:flex;height:12px;border-radius:999px;overflow:hidden;background:var(--soft);gap:1px}
.stack>span{display:block;height:100%}
.stack.big{height:22px;border-radius:6px;margin:4px 0 12px}
.lic-legend{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:4px 32px;font-size:14px}
.lic-legend li{display:grid;grid-template-columns:12px 1fr 56px 44px;gap:8px;align-items:center;padding:3px 0}
@media (max-width:560px){.srow{grid-template-columns:1fr auto}.srow .stack{grid-column:1/-1;order:3}
.top-in{flex-direction:column;align-items:flex-start;gap:12px}.bar-in{flex-wrap:wrap;gap:6px;padding-top:6px}
.toc{order:3;flex-basis:100%}h1{font-size:24px}}
.about{padding-left:20px;max-width:900px}
.about li{margin:6px 0}
.defs{margin:12px 0 0;font-size:13.5px;max-width:900px}
.defs dt{font-weight:650;margin-top:8px}
.defs dd{margin:2px 0 0;color:var(--muted)}
code{font-size:12.5px}
details>summary{cursor:pointer;color:var(--brand);font-weight:600;font-size:14px;margin:6px 0}
.method{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 16px;margin:12px 0;font-size:14px}
.method ul{padding-left:20px}
.method li{margin:4px 0}
.filter{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin:8px 0;font-size:13.5px;color:var(--muted)}
.filter input{font:inherit;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin-left:6px;width:min(280px,60vw)}
th button.sort{all:unset;cursor:pointer;display:inline-flex;gap:4px;align-items:center}
th button.sort::after{content:"↕";opacity:.35;font-size:11px}
th[aria-sort=ascending] button.sort::after{content:"↑";opacity:1}
th[aria-sort=descending] button.sort::after{content:"↓";opacity:1}
table.heat td.best{font-weight:700}
table.heat td.zero{color:var(--line)}
table.heat td.hit{box-shadow:inset 0 0 0 2px var(--brand);font-weight:700}
.hit-key{display:inline-block;padding:0 6px;border-radius:4px;box-shadow:inset 0 0 0 2px var(--brand);font-weight:700;color:var(--ink)}
table.dl button.expand{all:unset;cursor:pointer;font-weight:600;color:var(--brand);display:inline-flex;align-items:center;gap:6px}
table.dl .chev{display:inline-block;transition:transform .15s;width:10px}
table.dl button[aria-expanded="true"] .chev{transform:rotate(90deg)}
table.dl .noexpand{padding-left:16px}
table.dl tr.child th,table.dl tr.child td{background:var(--bg);font-size:13px;font-weight:400}
table.dl tr.child th{padding-left:32px}
.jump{color:var(--warn);font-size:11px;cursor:help}
button.expand-all{font:inherit;font-size:13px;padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--brand);cursor:pointer}
footer{padding-top:16px;padding-bottom:32px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line)}
@media print{body{background:#fff;font-size:12px}.bar,.skip,.filter,.expand-all,.info-btn,th button.sort::after{display:none}
.info-panel[hidden]{display:block!important}
.card,.kpi,.tbl{box-shadow:none}section{break-inside:auto}.card,.kpi,figure,tr{break-inside:avoid}
h2,h3{break-after:avoid}.tbl{overflow:visible}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""
(function(){
  // ---- charts (executive report)
  var el = document.getElementById('report-data');
  function charts(){
    if (!el) return;
    var d = JSON.parse(el.textContent);
    if (!window.Chart) {
      document.querySelectorAll('.chart').forEach(function(c){ c.classList.add('nochart'); var x=c.querySelector('details'); if(x) x.open=true; });
      return;
    }
    var cs = getComputedStyle(document.documentElement);
    function v(n){ return cs.getPropertyValue(n).trim(); }
    var ink = v('--muted'), grid = v('--line'), nf = new Intl.NumberFormat(d.locale);
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    var common = { responsive:true, maintainAspectRatio:false, animation:false,
      plugins:{ tooltip:{ callbacks:{ label:function(c){ return (c.dataset.label ? c.dataset.label+': ' : '') + nf.format(c.parsed.y); } } } },
      scales:{ x:{ ticks:{color:ink}, grid:{display:false} },
               y:{ beginAtZero:true, ticks:{color:ink, precision:0, callback:function(x){ return nf.format(x); }}, grid:{color:grid} } } };
    var a = document.getElementById('c-datasets');
    if (a) new Chart(a, { type:'bar',
      data:{ labels:d.datasets.labels, datasets:d.datasets.series.map(function(s){ return {label:s.label, data:s.data, backgroundColor:v(s.colour), borderWidth:0}; }) },
      options:Object.assign({}, common, {
        plugins:Object.assign({}, common.plugins, { legend:{ position:'bottom', labels:{ color:ink, boxWidth:12, boxHeight:12 } } }),
        scales:{ x:Object.assign({stacked:true}, common.scales.x), y:Object.assign({stacked:true}, common.scales.y) } }) });
    var b = document.getElementById('c-downloads');
    if (b) new Chart(b, { type:'bar',
      data:{ labels:d.downloads.labels, datasets:[{ label:d.downloads.label, data:d.downloads.data, borderWidth:0,
        backgroundColor:d.downloads.data.map(function(_, i){ return i === d.downloads.data.length-1 ? v('--s-partial') : v('--s1'); }) }] },
      options:Object.assign({}, common, { plugins:Object.assign({}, common.plugins, { legend:{display:false} }) }) });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', charts); else charts();

  // ---- filter boxes
  document.querySelectorAll('input[data-filter]').forEach(function(inp){
    var table = document.getElementById(inp.dataset.filter);
    var out = document.querySelector('.fcount[data-for="'+inp.dataset.filter+'"]');
    var rows = Array.prototype.slice.call(table.tBodies[0].rows);
    function run(){
      var q = inp.value.trim().toLowerCase(), shown = 0;
      rows.forEach(function(r){ var ok = !q || r.textContent.toLowerCase().indexOf(q) >= 0; r.hidden = !ok; if (ok) shown++; });
      out.textContent = q ? out.dataset.tpl.replace('{n}', shown).replace('{m}', rows.length) : '';
    }
    inp.addEventListener('input', run);
  });

  // ---- sortable columns
  function key(cell, numeric){
    var t = (cell ? cell.textContent : '').trim();
    if (!numeric) return t.toLowerCase();
    var digits = t.replace(/[^0-9]/g, '');
    return digits === '' ? -1 : parseInt(digits, 10);
  }
  document.querySelectorAll('table.sortable').forEach(function(table){
    var ths = table.tHead.rows[0].cells;
    Array.prototype.forEach.call(ths, function(th, i){
      var label = th.innerHTML;
      th.innerHTML = '<button type="button" class="sort">' + label + '</button>';
      th.firstChild.addEventListener('click', function(){
        var asc = th.getAttribute('aria-sort') !== 'ascending';
        Array.prototype.forEach.call(ths, function(o){ o.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        var numeric = th.classList.contains('num'), body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function(a, b){
          var x = key(a.cells[i], numeric), y = key(b.cells[i], numeric);
          return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1);
        });
        rows.forEach(function(r){ body.appendChild(r); });
      });
    });
  });

  // ---- expandable download rows
  function setGroup(btn, open){
    var g = document.getElementById(btn.getAttribute('aria-controls'));
    if (!g) return;
    g.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  document.querySelectorAll('table.dl button.expand').forEach(function(b){
    b.addEventListener('click', function(){ setGroup(b, b.getAttribute('aria-expanded') !== 'true'); });
  });
  var all = document.querySelector('button.expand-all');
  if (all) all.addEventListener('click', function(){
    var open = all.dataset.state !== 'open';
    document.querySelectorAll('table.dl button.expand').forEach(function(b){ setGroup(b, open); });
    all.dataset.state = open ? 'open' : 'closed';
    all.textContent = open ? all.dataset.hide : all.dataset.show;
  });

  // ---- (i) explanation buttons
  document.querySelectorAll('button.info-btn').forEach(function(b){
    b.addEventListener('click', function(){
      var panel = document.getElementById(b.getAttribute('aria-controls')), open = panel.hidden;
      panel.hidden = !open;
      b.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
  });

  // ---- print everything, including folded parts
  window.addEventListener('beforeprint', function(){
    document.querySelectorAll('details').forEach(function(x){ x.dataset.wasOpen = x.open; x.open = true; });
    document.querySelectorAll('table.dl tbody.kids').forEach(function(x){ x.hidden = false; });
  });
})();
"""


# --------------------------------------------------------------------------
# Write every page
# --------------------------------------------------------------------------
def write_all(m: dict, languages: tuple, default: str, site: Path, public: Path, admin: Path) -> list[Path]:
    """Website: index.html and admin.html in the default language, <lang>/ for the others.
    Downloads: the executive report in each language (public), the admin report with draft titles (admin)."""
    written = []

    def site_href(page_lang: str, kind: str, lang: str) -> str:
        up = "" if page_lang == default else "../"
        sub = "" if lang == default else f"{lang}/"
        return up + sub + ("index.html" if kind == "exec" else "admin.html")

    for lang in languages:
        folder = site if lang == default else site / lang
        folder.mkdir(parents=True, exist_ok=True)
        links = {k: {lg: site_href(lang, k, lg) for lg in languages} for k in ("exec", "admin")}
        for fname, page in (("index.html", build_executive(m, lang, links)),
                            ("admin.html", build_admin(m, lang, links, website=True))):
            (folder / fname).write_text(page, encoding="utf-8")
            written.append(folder / fname)

        pub_links = {"exec": {lg: f"executive-report-{lg}.html" for lg in languages}, "admin": None}
        (public / f"executive-report-{lang}.html").write_text(build_executive(m, lang, pub_links), encoding="utf-8")
        adm_links = {"admin": {lg: f"admin-report-{lg}.html" for lg in languages}, "exec": None}
        (admin / f"admin-report-{lang}.html").write_text(build_admin(m, lang, adm_links, website=False), encoding="utf-8")
        written += [public / f"executive-report-{lang}.html", admin / f"admin-report-{lang}.html"]
    return written
