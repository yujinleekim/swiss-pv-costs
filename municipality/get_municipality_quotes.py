"""
National municipality-level SolarRechner quote fetcher.

Same Tachion API logic as get_solarrechner_quotes.py (the canton-level
version), but redesigned for this much larger run (~21,100 addresses):
  - incremental writes (one row appended per completed address, not held
    in memory until the end)
  - resumable (skips addresses whose egid already has a row in the output
    file, so an interrupted run picks up where it left off)
  - low concurrency on purpose -- this hits a real commercial vendor's
    API, not a free government one, so WORKERS stays small regardless of
    how large the input list is. This stage WILL take much longer than
    address sampling did.

INPUT:  municipality_addresses.csv  (from get_municipality_addresses.py)
OUTPUT: municipality_quotes.csv     (append-only, safe to resume)

USAGE:
  python get_municipality_quotes.py                # start / resume
  python get_municipality_quotes.py --workers 5     # tune concurrency
"""

import argparse
import csv
import json
import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

SEARCHSERVER = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
HEIGHT = "https://api3.geo.admin.ch/rest/services/height"
GWR_FIND = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
GWR_LAYER = "ch.bfs.gebaeude_wohnungs_register"

TACHION_PLANES = "https://tachionframework.ch/tachionserver/resources/sim/planes"
TACHION_BUILDING_REPORT = "https://tachionframework.ch/tachionserver/resources/sim/buildingReport"
TACHION_API_KEY = "3xs8-51ua-9ky5"

DEFAULT_ELECTRIC_TARIFF = [
    ["120.00", "0.2577", "0.2483", "0.00000"],
    ["0.00000", "0.08440", "0.08440", "3000.00"],
]
DEFAULT_ELECTRICITY_PROVIDER = "454"

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "municipality_addresses.csv")
OUTFILE = os.path.join(_HERE, "municipality_quotes.csv")
OUTPUT_FIELDNAMES = [
    "canton", "municipality", "bfs_code", "address", "plz", "egid",
    "buildingGroundArea_sqm", "roofPlanesFound", "roofPlanesSelected",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
    "error", "raw_json",
]

DEFAULT_WORKERS = 5   # modest increase from 3 -- no rate-limit errors seen from
                       # Tachion in this whole project so far, still conservative
SLEEP_BETWEEN_CALLS = 0.0   # was 0.5s per completed address -- redundant throttling
                             # on top of the WORKERS concurrency cap; at 21,100
                             # addresses that alone was adding ~3 hours of pure wait
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

SESSION = requests.Session()  # connection reuse instead of a fresh connection per call


def geocode_address(address_text):
    r = SESSION.get(SEARCHSERVER, params={
        "type": "locations", "origins": "address", "searchText": address_text,
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"No geocoding match for: {address_text}")
    return results[0]["attrs"]


def gwr_lookup_by_egid(egid):
    r = SESSION.get(GWR_FIND, params={
        "layer": GWR_LAYER, "searchField": "egid", "searchText": egid,
        "contains": "false", "returnGeometry": "false", "sr": "2056",
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"No GWR record for EGID {egid}")
    return results[0]["attributes"]


def get_elevation(x_lv03, y_lv03):
    r = SESSION.get(HEIGHT, params={"easting": y_lv03, "northing": x_lv03, "sr": "21781"}, timeout=15)
    r.raise_for_status()
    return r.json().get("height")


def parse_street_and_number(address_text):
    street_part = address_text.split(",")[0].strip()
    m = re.match(r"^(.*?)\s+(\d+[a-zA-Z]?)$", street_part)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return street_part, ""


def build_planes_request(address_text, parcel_half=30):
    geo = geocode_address(address_text)
    egid = geo["featureId"].split("_")[0]
    x, y = geo["x"], geo["y"]

    # gwr lookup and elevation are both independent of each other -- run in parallel
    with ThreadPoolExecutor(max_workers=2) as sub_pool:
        gwr_future = sub_pool.submit(gwr_lookup_by_egid, egid)
        z_future = sub_pool.submit(get_elevation, x, y)
        gwr = gwr_future.result()
        z = z_future.result()

    x_f, y_f = float(x), float(y)
    street, num = parse_street_and_number(address_text)

    half = parcel_half
    parcel = [
        [str(y_f - half), str(x_f - half)], [str(y_f + half), str(x_f - half)],
        [str(y_f + half), str(x_f + half)], [str(y_f - half), str(x_f + half)],
        [str(y_f - half), str(x_f - half)],
    ]
    attrs = {
        "bbox": [str(y_f - half), str(x_f - half), str(y_f + half), str(x_f + half)],
        "bfsNumber": str(gwr.get("ggdenr", "")), "canton": gwr.get("gdekt", ""), "country": "CH",
        "detail": geo.get("detail", ""), "electricTariff": DEFAULT_ELECTRIC_TARIFF,
        "electricityProvider": DEFAULT_ELECTRICITY_PROVIDER, "featureId": geo["featureId"],
        "gdename": gwr.get("ggdename", ""), "geom_st_box2d": geo.get("geom_st_box2d", ""),
        "label": geo.get("label", ""), "lat": str(geo.get("lat", "")), "lon": str(geo.get("lon", "")),
        "num": num or geo.get("num", ""), "numAddOn": "", "origin": "address", "parcel": parcel,
        "plz4": str(gwr.get("dplz4", "")), "strname1": street, "x": str(x), "y": str(y), "z": str(z),
        "zip": str(gwr.get("dplz4", "")),
    }
    return {"apiKey": TACHION_API_KEY, "signature": {"request": "603", "event": "planes"}, "attrs": attrs}


def find_cost_component(node, target_name):
    if not isinstance(node, dict):
        return None
    if node.get("component") == target_name:
        return node
    for child in node.get("costComponent", []):
        found = find_cost_component(child, target_name)
        if found:
            return found
    return None


def get_quote_for_row(row):
    address_text = f"{row['address']}, {row['plz']} {row['municipality']}"
    out = dict(row)
    last_error = None
    parcel_half = 30  # widened automatically if BUILDING_TOO_BIG shows up (see below)

    for attempt in range(MAX_RETRIES + 1):
        try:
            payload = build_planes_request(address_text, parcel_half=parcel_half)
            r = SESSION.post(TACHION_PLANES, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            planes_resp = r.json()

            if not planes_resp.get("valid"):
                msg = planes_resp.get("errorMessageForUser") or ""
                last_error = f"planes invalid: {msg}"
                if "BUILDING_TOO_BIG" in msg:
                    parcel_half *= 2  # real fix for this specific error -- retrying
                    print(f"    [{row['municipality']} {row['address']}] BUILDING_TOO_BIG, "
                          f"widening parcel box to {parcel_half}m half-width and retrying")
                    continue  # no point sleeping -- this isn't a transient failure
                if "readMeteodataFile" in msg:
                    # server-side missing weather data for this location -- permanent,
                    # every address in the same area will hit this identically, retrying
                    # (here or on a future run) can never fix it
                    out["error"] = last_error
                    return out
                time.sleep(1 + attempt)
                continue

            report_payload = {
                "apiKey": TACHION_API_KEY,
                "signature": {"request": "603", "event": "buildingReport", "state": "SOLAR_OPT"},
                "attrs": planes_resp["attrs"], "roof": planes_resp.get("roof", []),
                "wall": planes_resp.get("wall", []),
            }
            r2 = SESSION.post(TACHION_BUILDING_REPORT, json=report_payload, timeout=REQUEST_TIMEOUT)
            r2.raise_for_status()
            report = r2.json()

            if not report.get("valid"):
                msg = report.get("errorMessageForUser") or ""
                last_error = f"buildingReport invalid: {msg}"
                if "readMeteodataFile" in msg:
                    out["error"] = last_error
                    return out
                time.sleep(1 + attempt)
                continue

            pv_all = find_cost_component(report.get("costAnalysis", {}), "pvModuleAll")
            out["buildingGroundArea_sqm"] = planes_resp["attrs"].get("buildingGroundArea")
            roof_list = [p for p in (planes_resp.get("roof") or []) if p]  # guard against malformed null entries
            out["roofPlanesFound"] = len(roof_list)
            out["roofPlanesSelected"] = sum(1 for p in roof_list if p.get("selected") == "true")
            if not pv_all:
                last_error = "pvModuleAll not found in costAnalysis"
                time.sleep(1 + attempt)
                continue

            out["grossInvest_CHF"] = pv_all.get("grossInvest")
            out["subsidies_CHF"] = pv_all.get("subsidies")
            out["netInvest_CHF"] = pv_all.get("netInvest")
            out["paybackTime_years"] = pv_all.get("paybackTime")
            try:
                out["producedKwhYear_kWh"] = report["pvSystem"]["Eac"][0]
            except (KeyError, IndexError):
                pass
            try:
                ocn = report["energySystem"]["ownConsumptionNetwork"]
                out["selfConsumedKwhYear_kWh"] = ocn["suppliedOcn"][0]
                out["selfConsumptionPct_pct"] = ocn["ownConsumptionFraction"][0]
                out["gridFeedinKwhYear_kWh"] = ocn["feedinCurrent"][0]
            except (KeyError, IndexError):
                pass
            out["raw_json"] = json.dumps({
                "planes_attrs": planes_resp.get("attrs"), "buildingReport_param": report.get("param"),
                "buildingReport_costAnalysis": report.get("costAnalysis"),
                "buildingReport_energySystem": report.get("energySystem"),
                "buildingReport_pvSystem": report.get("pvSystem"),
                "buildingReport_stSystem": report.get("stSystem"),
                "buildingReport_electricUser": report.get("electricUser"),
            }, ensure_ascii=False)
            out.pop("error", None)
            return out

        except requests.RequestException as exc:
            last_error = f"request failed: {exc}"
            time.sleep(1 + attempt)
            continue
        except (ValueError, KeyError) as exc:
            out["error"] = str(exc)
            return out

    out["error"] = f"failed after {MAX_RETRIES + 1} attempts: {last_error}"
    return out


def load_completed_egids():
    if not os.path.exists(OUTFILE):
        return set()
    with open(OUTFILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return {row["egid"] for row in rows if not row.get("error")}


def append_rows(rows):
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
            input(f"\n'{OUTFILE}' appears to be open in another program (e.g. Excel). "
                  f"Close it, then press Enter to retry...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    with open(INFILE, newline="", encoding="utf-8-sig") as f:
        all_rows = list(csv.DictReader(f))
    print(f"Loaded {len(all_rows)} addresses from {INFILE}")

    done = load_completed_egids()
    todo = [r for r in all_rows if r["egid"] not in done]
    print(f"{len(done)} already completed (resuming), {len(todo)} remaining\n")

    if not todo:
        print("Nothing left to do.")
        return

    completed_count = len(done)
    error_count = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(get_quote_for_row, row): row for row in todo}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = dict(row)
                result["error"] = f"unexpected failure: {exc}"

            append_rows([result])
            completed_count += 1
            if result.get("error"):
                error_count += 1

            status = "OK" if not result.get("error") else f"ERROR: {result.get('error')}"
            print(f"[{completed_count}/{len(all_rows)}] {row['canton']} {row['municipality']} "
                  f"{row['address']}: {status}")
            time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone with this pass. {error_count} error(s) this run.")
    if error_count:
        print("Re-run this same script to retry failed rows (it resumes automatically).")


if __name__ == "__main__":
    main()