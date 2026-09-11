"""
Randomly sample single-family-home (EFH) addresses per Swiss canton,
using the free public GWR (Gebaeude- und Wohnungsregister) layer on
Switzerland's federal geoportal (geo.admin.ch). No login/API key needed.

HOW IT WORKS
------------
1. For each canton, we don't need its exact polygon -- we just need a
   bounding box in LV95 coordinates (EPSG:2056) that's generous enough
   to fully contain it (it's fine if it overlaps neighbouring cantons).
2. We throw random points at that bounding box and ask the GWR
   "identify" endpoint what building (if any) is near that point.
3. We keep a hit only if:
     - it actually returned a building (random points often land on
       a field, forest, road, lake -> no hit -> discard & retry)
     - gdekt (canton code in the response) matches the canton we're
       sampling for -- this is what actually guarantees correctness,
       not the bounding box, so imprecise/generous boxes are fine
     - gklas == 1110 (Einfamilienhaus) and gstat == 1004 (existing,
       not planned/demolished)
4. Repeat with fresh random points until N valid addresses are found
   per canton (with a retry cap so an unlucky canton doesn't loop
   forever), then write everything to a CSV.

VERIFY BEFORE A FULL RUN
-------------------------
I designed this against a real, working example of the identify
endpoint (see the `test_single_lookup()` function below -- run that
first). I could NOT test this live end-to-end myself (no network
access in the environment that wrote this script), so:
  - Run `python sample_gwr_addresses.py --test` first. It does one
    single lookup and prints the raw JSON so you can eyeball the
    field names before trusting the bulk run.
  - The per-canton bounding boxes below are hand-estimated from
    general geography, generously padded. If a canton is
    consistently returning 0 hits after many attempts, its box is
    probably off -- widen it.
"""

import argparse
import csv
import os
import random
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

API_IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
LAYER = "ch.bfs.gebaeude_wohnungs_register"

N_PER_CANTON = 3          # how many addresses to collect per canton
MAX_ATTEMPTS_PER_CANTON = 400   # safety cap so one bad bbox doesn't hang forever
SLEEP_BETWEEN_CALLS = 0.1       # be polite to the free public API, but don't crawl
REQUEST_TIMEOUT = 15
IDENTIFY_TOLERANCE = 150        # metres -- wider radius = far fewer wasted misses
CANTON_WORKERS = 8              # cantons run in parallel; raise/lower if the API complains

GKLAS_EINFAMILIENHAUS = "1110"
GSTAT_EXISTING = "1004"

# Rough LV95 bounding boxes per canton: (min_easting, min_northing, max_easting, max_northing)
# Generously padded -- the gdekt check in the response is the real filter, not this box.
CANTON_BBOX = {
    "ZH": (2670000, 1225000, 2717000, 1284000),
    "BE": (2560000, 1130000, 2680000, 1225000),
    "LU": (2630000, 1190000, 2680000, 1230000),
    "UR": (2660000, 1160000, 2700000, 1205000),
    "SZ": (2680000, 1195000, 2720000, 1230000),
    "OW": (2645000, 1180000, 2680000, 1210000),
    "NW": (2665000, 1195000, 2690000, 1215000),
    "GL": (2705000, 1195000, 2735000, 1225000),
    "ZG": (2670000, 1220000, 2695000, 1240000),
    "FR": (2560000, 1160000, 2610000, 1210000),
    "SO": (2600000, 1215000, 2645000, 1250000),
    "BS": (2610000, 1265000, 2625000, 1275000),
    "BL": (2605000, 1245000, 2645000, 1270000),
    "SH": (2670000, 1275000, 2705000, 1295000),
    "AR": (2735000, 1245000, 2760000, 1265000),
    "AI": (2740000, 1235000, 2760000, 1250000),
    "SG": (2705000, 1215000, 2770000, 1270000),
    "GR": (2705000, 1140000, 2830000, 1220000),
    "AG": (2630000, 1225000, 2680000, 1270000),
    "TG": (2700000, 1250000, 2745000, 1285000),
    "TI": (2685000, 1075000, 2740000, 1160000),
    "VD": (2485000, 1120000, 2580000, 1200000),
    "VS": (2560000, 1075000, 2680000, 1150000),
    "NE": (2530000, 1185000, 2575000, 1225000),
    "GE": (2485000, 1105000, 2515000, 1135000),
    "JU": (2565000, 1225000, 2620000, 1260000),
}


def identify_building(easting: float, northing: float, tolerance: int = IDENTIFY_TOLERANCE):
    """Ask the GWR layer what building (if any) is near this LV95 point."""
    params = {
        "geometryType": "esriGeometryPoint",
        "geometry": f"{easting},{northing}",
        "imageDisplay": "500,600,96",
        "mapExtent": f"{easting-500},{northing-500},{easting+500},{northing+500}",
        "tolerance": tolerance,
        "layers": f"all:{LAYER}",
        "returnGeometry": "false",
        "sr": "2056",  # LV95 -- without this the API defaults to old LV03 and every
                        # lookup silently returns [] since our coords are the wrong scale for it
    }
    r = requests.get(API_IDENTIFY, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("results", [])


def sample_canton(canton_code: str, n: int):
    print(f"Starting {canton_code}...")
    min_e, min_n, max_e, max_n = CANTON_BBOX[canton_code]
    found = []
    seen_egids = set()
    attempts = 0

    while len(found) < n and attempts < MAX_ATTEMPTS_PER_CANTON:
        attempts += 1
        e = random.uniform(min_e, max_e)
        n_coord = random.uniform(min_n, max_n)

        try:
            results = identify_building(e, n_coord)
        except requests.RequestException as exc:
            print(f"  [{canton_code}] request failed, retrying: {exc}")
            time.sleep(1)
            continue

        time.sleep(SLEEP_BETWEEN_CALLS)

        for res in results:
            attrs = res.get("attributes", {})
            egid = attrs.get("egid")
            if egid in seen_egids:
                continue
            if str(attrs.get("gdekt")) != canton_code:
                continue
            if str(attrs.get("gklas")) != GKLAS_EINFAMILIENHAUS:
                continue
            if str(attrs.get("gstat")) != GSTAT_EXISTING:
                continue

            seen_egids.add(egid)
            found.append({
                "canton": canton_code,
                "address": attrs.get("strname_deinr", ""),
                "plz": str(attrs.get("plz_plz6", "")).split("/")[0],
                "municipality": attrs.get("ggdename", ""),
                "egid": egid,
            })
            print(f"  [{canton_code}] found {len(found)}/{n}: {attrs.get('strname_deinr')}")
            if len(found) >= n:
                break

    if len(found) < n:
        print(f"  [{canton_code}] WARNING: only found {len(found)}/{n} after "
              f"{attempts} attempts -- bounding box may need widening.")
    return found


def test_single_lookup():
    """Sanity check: one known-ish point, print raw response so you can
    confirm field names before trusting the bulk logic above."""
    # Roughly central Bern -- just needs to land near *some* building.
    results = identify_building(2600000, 1199500, tolerance=200)
    import json
    print(json.dumps(results[:2], indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="run a single test lookup and exit")
    parser.add_argument("--n", type=int, default=N_PER_CANTON, help="addresses per canton")
    _default_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "random_addresses.csv")
    parser.add_argument("--out", default=_default_out)
    args = parser.parse_args()

    if args.test:
        test_single_lookup()
        return

    all_rows = []
    with ThreadPoolExecutor(max_workers=CANTON_WORKERS) as pool:
        futures = {pool.submit(sample_canton, code, args.n): code for code in CANTON_BBOX}
        for future in as_completed(futures):
            code = futures[future]
            try:
                all_rows.extend(future.result())
            except Exception as exc:
                print(f"  [{code}] FAILED: {exc}")

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["canton", "address", "plz", "municipality", "egid"])
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone. Wrote {len(all_rows)} addresses to {args.out}")


if __name__ == "__main__":
    main()