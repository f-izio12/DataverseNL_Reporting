"""
Daily access-request tracker for DataverseNL.

DataverseNL's API only shows the CURRENT queue of access requests: once a request is
granted or rejected it disappears. This script takes a snapshot of the queue every day
and keeps a history in data/access_requests.json:

  - a request that appears for the first time      -> new request (date = first seen)
  - a request that disappears from a checked file  -> decided (granted OR rejected)
  - the difference                                  -> days to decision (to the nearest day)

Run it with:  python request_tracker.py

PRIVACY: no names, e-mail addresses or user ids are stored. Each request is stored under
a one-way code made from (requester + file) and a secret key (repository secret
REQUESTER_HASH_KEY). Without that key the codes cannot be traced back to a person, and
because the file is part of the code, the same person cannot be followed across files.

What it CANNOT tell:
  - whether a request was granted or rejected (the API does not say)
  - anything that happened before the first run
  - requests made and decided between two runs (they are never seen)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import dataverse_report as dr

TRACKER_FILE = Path(os.environ.get("TRACKER_FILE", "data/access_requests.json"))
HASH_KEY = os.environ.get("REQUESTER_HASH_KEY", "")
# Files the token is not allowed to check are re-checked once a week (on this weekday, 0 = Monday),
# not every day, to keep the number of API calls down.
FORBIDDEN_RECHECK_WEEKDAY = 0


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------
def empty_state() -> dict:
    return {"version": 1, "started": None, "runs": [], "files": {}, "requests": {}, "forbidden": {}}


def load_state(path: Path = TRACKER_FILE) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict, path: Path = TRACKER_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Pseudonymisation
# --------------------------------------------------------------------------
def user_identity(u) -> str:
    """Pick a stable identifier from one entry of listRequests. The exact fields are not
    documented, so try the likely ones and fall back to the whole entry."""
    if isinstance(u, dict):
        for k in ("persistentUserId", "identifier", "userIdentifier", "id", "email"):
            if u.get(k) not in (None, ""):
                return f"{k}:{u[k]}"
        return json.dumps(u, sort_keys=True)
    return str(u)


def request_key(file_id: str, user) -> str:
    msg = f"{file_id}|{user_identity(user)}".encode()
    return hmac.new(HASH_KEY.encode(), msg, hashlib.sha256).hexdigest()[:20]


# --------------------------------------------------------------------------
# One scan of the queue
# --------------------------------------------------------------------------
def check_file(file_id: str):
    status, body = dr.get(f"/api/access/datafile/{file_id}/listRequests", auth=True, allow_error=True)
    if status == 200:
        data = body.get("data")
        return "ok", (data if isinstance(data, list) else [])
    if status == 404:  # Dataverse answers 404 when a file has no pending requests
        return "ok", []
    if status in (401, 403):
        return "forbidden", []
    return "error", []


def scan(units: list[dict], state: dict, today: str, recheck_forbidden: bool) -> dict:
    files_now = {}       # file_id -> metadata for every restricted file found today
    results = {}         # file_id -> (status, [request keys])
    field_names = set()  # names of fields returned by listRequests (for diagnostics only)
    skipped = 0

    for u in units:
        files = [f for f in dr.search_all({"q": "*", "type": "file", "subtree": u["alias"],
                                           "fq": 'fileAccess:"Restricted"'}) if f.get("file_id")]
        to_check = []
        for f in files:
            fid = str(f["file_id"])
            files_now[fid] = {"unit": u["short"], "dataset_pid": f.get("dataset_persistent_id", ""),
                              "dataset": f.get("dataset_name", ""), "dataset_id": str(f.get("dataset_id", ""))}
            if fid in state["forbidden"] and not recheck_forbidden:
                results[fid] = ("forbidden", [])
                skipped += 1
            else:
                to_check.append(fid)
        print(f"  {u['short']}: {len(files)} restricted files, checking {len(to_check)}", flush=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for fid, (status, users) in zip(to_check, pool.map(check_file, to_check)):
                for usr in users:
                    if isinstance(usr, dict):
                        field_names.update(usr.keys())
                results[fid] = (status, [request_key(fid, usr) for usr in users])

    if field_names:
        # Field NAMES only, never values: helps confirm which identifier is being used.
        print("  listRequests fields seen:", ", ".join(sorted(field_names)))
    if skipped:
        print(f"  {skipped} files skipped (no permission last week; re-checked on Mondays)")
    return {"files": files_now, "results": results}


# --------------------------------------------------------------------------
# Update the history
# --------------------------------------------------------------------------
def update_state(state: dict, snapshot: dict, today: str) -> dict:
    files_now, results = snapshot["files"], snapshot["results"]
    stats = {"new": 0, "decided": 0, "file_gone": 0, "baseline": 0}

    # File bookkeeping. Requests found on a file we could not see before (first run of the tracker,
    # or a file the token had no permission for) were already waiting: their start date is unknown,
    # so they are marked "baseline". A file that is new to DataverseNL has no such history.
    first_run = not state["runs"]
    first_time_ok = set()
    for fid, meta in files_now.items():
        status = results.get(fid, ("error", []))[0]
        was_known = fid in state["files"]
        rec = state["files"].setdefault(fid, {**meta, "first_observed": None, "last_observed": None})
        rec.update(meta)
        if status == "ok":
            if rec["first_observed"] is None:
                rec["first_observed"] = today
                if first_run or was_known:
                    first_time_ok.add(fid)
            rec["last_observed"] = today
            state["forbidden"].pop(fid, None)
        elif status == "forbidden":
            state["forbidden"][fid] = today

    # Requests visible today
    open_by_file: dict = {}
    for key, r in state["requests"].items():
        if r["closed"] is None:
            open_by_file.setdefault(r["file_id"], {})[r["base_key"]] = key

    for fid, (status, keys) in results.items():
        if status != "ok":
            continue
        meta = files_now[fid]
        open_here = open_by_file.get(fid, {})
        for base in keys:
            if base in open_here:
                state["requests"][open_here[base]]["last_seen"] = today
                continue
            # New request (or the same person asking again after a decision)
            n = 1
            key = base
            while key in state["requests"]:
                n += 1
                key = f"{base}-{n}"
            baseline = fid in first_time_ok
            state["requests"][key] = {
                "base_key": base, "file_id": fid, "unit": meta["unit"],
                "dataset_pid": meta["dataset_pid"], "dataset": meta["dataset"], "dataset_id": meta["dataset_id"],
                "first_seen": today, "last_seen": today, "closed": None, "closed_reason": None,
                "baseline": baseline,
            }
            stats["baseline" if baseline else "new"] += 1

        # Requests that were open on this file but are no longer in the queue -> decided
        visible = set(keys)
        for base, key in open_here.items():
            if base not in visible:
                state["requests"][key].update(closed=today, closed_reason="decided")
                stats["decided"] += 1

    # Open requests on files that are no longer restricted (made public, deleted, moved out of UM)
    for key, r in state["requests"].items():
        if r["closed"] is None and r["file_id"] not in files_now:
            r.update(closed=today, closed_reason="file_no_longer_restricted")
            stats["file_gone"] += 1

    if state["started"] is None:
        state["started"] = today
    if today not in state["runs"]:
        state["runs"].append(today)
    return stats


# --------------------------------------------------------------------------
# Statistics for the report (used by dataverse_report.py)
# --------------------------------------------------------------------------
def summarise(state: dict, today: str | None = None) -> dict:
    today_d = date.fromisoformat(today) if today else date.today()
    reqs = list(state["requests"].values())

    def days(a: str, b: str) -> int:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days

    by_unit_year: dict = {}
    for r in reqs:
        if r["baseline"]:
            continue
        y = r["first_seen"][:4]
        cell = by_unit_year.setdefault(r["unit"], {}).setdefault(y, {"new": 0, "decided": 0, "days": []})
        cell["new"] += 1
        if r["closed_reason"] == "decided":
            cell["decided"] += 1
            cell["days"].append(days(r["first_seen"], r["closed"]))

    runs = sorted(state["runs"])
    # Waiting time is measured up to the last check, because that is when the request was last seen.
    as_of = runs[-1] if runs else today_d.isoformat()
    open_reqs = [dict(r) for r in reqs if r["closed"] is None]
    for r in open_reqs:
        r["_waiting"] = days(r["first_seen"], as_of)

    all_days = [days(r["first_seen"], r["closed"]) for r in reqs
                if not r["baseline"] and r["closed_reason"] == "decided"]
    # Largest gap between two consecutive runs: the precision of the "days to decision" figures.
    gaps = [days(a, b) for a, b in zip(runs, runs[1:])]
    return {
        "started": state["started"],
        "last_run": runs[-1] if runs else None,
        "runs": len(runs),
        "max_gap": max(gaps) if gaps else 0,
        "by_unit_year": by_unit_year,
        "median_days": statistics.median(all_days) if all_days else None,
        "decided": len(all_days),
        "within_14": sum(1 for d in all_days if d <= 14),
        "open": sorted(open_reqs, key=lambda r: -r["_waiting"]),
        "baseline_open": sum(1 for r in open_reqs if r["baseline"]),
        "forbidden_files": len(state["forbidden"]),
    }


# --------------------------------------------------------------------------
def main() -> int:
    if not dr.API_TOKEN:
        raise SystemExit("DATAVERSE_API_TOKEN is missing: the queue can only be read with a curator token.")
    if len(HASH_KEY) < 16:
        raise SystemExit("REQUESTER_HASH_KEY is missing or shorter than 16 characters. "
                         "Add it as a repository secret; without it requests cannot be stored safely.")

    today = datetime.now(dr.TZ).date().isoformat()
    state = load_state() or empty_state()
    if today in state["runs"]:
        print(f"Already ran today ({today}); nothing to do.")
        return 0

    dr.get("/api/info/version")  # fails early with a clear message if DataverseNL is down
    units = [u for u in dr.get_units()]
    recheck = date.fromisoformat(today).weekday() == FORBIDDEN_RECHECK_WEEKDAY or not state["runs"]
    print(f"Access-request tracker {today} ({'full check' if recheck else 'skipping known no-permission files'})")
    snapshot = scan(units, state, today, recheck)

    errors = sum(1 for s, _ in snapshot["results"].values() if s == "error")
    if errors > 0.2 * max(1, len(snapshot["results"])):
        # Too many failures: saving would wrongly mark requests as decided. Keep yesterday's state.
        raise SystemExit(f"{errors} files could not be checked; state not updated. Re-run later.")

    stats = update_state(state, snapshot, today)
    save_state(state)
    print(f"  new requests: {stats['new']}, decided: {stats['decided']}, "
          f"closed because file no longer restricted: {stats['file_gone']}, "
          f"already waiting when first checked: {stats['baseline']}")
    print(f"Saved {TRACKER_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
