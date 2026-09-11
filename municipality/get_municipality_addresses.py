"""
National municipality-level address sampler -- 10 random single-family
homes per municipality, for all 2,110 Swiss municipalities.

Builds on everything validated on the Obwalden test:
  - each municipality's own boundary bbox (swissBOUNDARIES3D, exact match,
    current-year filtered with a fallback to highest 'jahr' if the flag
    is ever missing from a response)
  - random-point sampling within that bbox, checked against the GWR
    building registry (single-family homes only: GKLAS 1110, GSTAT 1004)

NEW AT THIS SCALE: checkpointing. This run is large enough that it WILL
likely get interrupted at some point. Progress is written incrementally
(one line per completed municipality, flushed immediately) rather than
held in memory until the end, and on restart, municipalities already
completed are skipped.

INPUT:  all_municipalities.csv  (from build_municipality_list.py)
OUTPUT: municipality_addresses.csv  (append-only, safe to resume)

USAGE:
  python get_municipality_addresses.py                 # start / resume
  python get_municipality_addresses.py --workers 20     # tune concurrency
"""

import argparse
import csv
import os
import random
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

FIND_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
GEMEINDE_LAYER = "ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill"
GWR_IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GWR_LAYER = "ch.bfs.gebaeude_wohnungs_register"
GKLAS_EINFAMILIENHAUS = "1110"
GSTAT_EXISTING = "1004"

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "all_municipalities.csv")
OUTFILE = os.path.join(_HERE, "municipality_addresses.csv")
OUTPUT_FIELDNAMES = ["canton", "municipality", "bfs_code", "address", "plz", "egid"]

TARGET_PER_MUNICIPALITY = 10
MAX_ATTEMPTS_PER_MUNICIPALITY = 600
DEFAULT_WORKERS = 12   # the earlier slowdown wasn't actually about worker count
                        # (see fetch_municipality_bbox) -- back to a faster default


def fetch_municipality_bbox(name: str, retries: int = 4):
    """Returns just the bbox. Canton is no longer sourced here -- the
    boundary layer's 'kanton' attribute turned out to be consistently
    missing for a large batch of municipalities (not flaky, genuinely
    absent), so canton is now taken from the GWR identify() results
    themselves during sampling instead (see sample_municipality)."""
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.get(FIND_URL, params={
                "layer": GEMEINDE_LAYER, "searchField": "gemname", "searchText": name,
                "contains": "false",
                "returnGeometry": "true",
                "geometryFormat": "geojson",
                "sr": "2056",
            }, timeout=15)
            r.raise_for_status()
            all_results = r.json().get("results", [])

            current = [res for res in all_results if res.get("attributes", {}).get("is_current_jahr")]
            chosen = current[0] if current else (
                max(all_results, key=lambda res: res.get("attributes", {}).get("jahr") or 0)
                if all_results else None
            )
            if chosen:
                return chosen["bbox"]
            last_exc = ValueError(f"No boundary results at all for: {name}")
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(1 + attempt)
    raise ValueError(f"Could not get a boundary for '{name}' after {retries} attempts: {last_exc}")


def identify_building(easting, northing, tolerance=150):
    params = {
        "geometryType": "esriGeometryPoint", "geometry": f"{easting},{northing}",
        "imageDisplay": "500,600,96",
        "mapExtent": f"{easting-500},{northing-500},{easting+500},{northing+500}",
        "tolerance": tolerance, "layers": f"all:{GWR_LAYER}", "returnGeometry": "false",
        "sr": "2056",
    }
    r = requests.get(GWR_IDENTIFY, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("results", [])


def sample_municipality(muni_name: str, bfs_code: str, n: int):
    try:
        min_e, min_n, max_e, max_n = fetch_municipality_bbox(muni_name)
    except ValueError as exc:
        return [], 0, str(exc)

    found = []
    seen_egids = set()
    attempts = 0
    discovered_canton = None  # taken from the first real building match, not the boundary API

    while len(found) < n and attempts < MAX_ATTEMPTS_PER_MUNICIPALITY:
        attempts += 1
        e = random.uniform(min_e, max_e)
        n_coord = random.uniform(min_n, max_n)
        try:
            results = identify_building(e, n_coord)
        except requests.RequestException:
            time.sleep(1)
            continue
        time.sleep(0.1)

        for res in results:
            attrs = res.get("attributes", {})
            egid = str(attrs.get("egid"))
            if egid in seen_egids:
                continue
            if attrs.get("ggdename") != muni_name:
                continue
            if str(attrs.get("gklas")) != GKLAS_EINFAMILIENHAUS:
                continue
            if str(attrs.get("gstat")) != GSTAT_EXISTING:
                continue

            this_canton = str(attrs.get("gdekt"))
            if discovered_canton is None:
                discovered_canton = this_canton
            elif this_canton != discovered_canton:
                # extremely unlikely (would mean two different-canton
                # municipalities sharing an exact name), but don't silently
                # mix cantons within one municipality's results if it happens
                continue

            seen_egids.add(egid)
            found.append({
                "canton": discovered_canton, "municipality": muni_name, "bfs_code": bfs_code,
                "address": attrs.get("strname_deinr", ""),
                "plz": str(attrs.get("plz_plz6", "")).split("/")[0],
                "egid": egid,
            })
            if len(found) >= n:
                break

    error = None if len(found) >= n else f"only found {len(found)}/{n} in {attempts} attempts"
    return found, attempts, error


def load_completed_municipalities():
    """Which municipalities already have output rows -- for resuming."""
    if not os.path.exists(OUTFILE):
        return set()
    with open(OUTFILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # a municipality counts as "done" if it already has TARGET_PER_MUNICIPALITY rows
    # keyed by bfs_code, since canton is no longer known from the input file
    counts = {}
    for row in rows:
        key = row["bfs_code"]
        counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count >= TARGET_PER_MUNICIPALITY}


def append_rows(rows, file_lock_retry_msg="municipality_addresses.csv"):
    """Append rows to OUTFILE, creating it with a header if needed. Retries
    on Windows file-lock errors instead of crashing and losing progress."""
    file_exists = os.path.exists(OUTFILE)
    while True:
        try:
            with open(OUTFILE, "a", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
                if not file_exists:
                    writer.writeheader()
                writer.writerows(rows)
                f.flush()
            return
        except PermissionError:
            input(f"\n'{file_lock_retry_msg}' appears to be open in another program (e.g. Excel). "
                  f"Close it, then press Enter to retry...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    with open(INFILE, newline="", encoding="utf-8-sig") as f:
        all_munis = list(csv.DictReader(f))
    print(f"Loaded {len(all_munis)} municipalities from {INFILE}")

    done = load_completed_municipalities()
    todo = [m for m in all_munis if m["bfs_code"] not in done]
    print(f"{len(done)} already completed (resuming), {len(todo)} remaining\n")

    if not todo:
        print("Nothing left to do.")
        return

    completed_count = len(done)
    error_log = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(sample_municipality, m["municipality"], m["bfs_code"],
                        TARGET_PER_MUNICIPALITY): m
            for m in todo
        }
        for future in as_completed(futures):
            m = futures[future]
            try:
                rows, attempts, error = future.result()
            except Exception as exc:
                rows, attempts, error = [], 0, f"unexpected failure: {exc}"

            if rows:
                append_rows(rows)
            completed_count += 1

            status = "OK" if not error else f"ISSUE: {error}"
            print(f"[{completed_count}/{len(all_munis)}] {m['municipality']} "
                  f"({attempts} attempts): {status}")

            if error:
                error_log.append(f"{m['municipality']}: {error}")

    print(f"\nDone with this pass. {len(error_log)} municipalities had issues.")
    if error_log:
        print("Re-run this same script to retry them (it resumes automatically) "
              "-- or investigate individually if the same ones keep failing:")
        for line in error_log[:30]:
            print(f"  {line}")


if __name__ == "__main__":
    main()