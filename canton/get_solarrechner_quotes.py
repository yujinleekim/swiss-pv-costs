"""
Combined SolarRechner batch runner.

For every address in random_addresses.csv:
  1. Get a cost quote via the Tachion API (/planes -> /buildingReport),
     retrying automatically on transient failures (network errors, or
     'invalid' responses that look like a temporary server hiccup).
  2. If a row is STILL failing after those retries, automatically pick a
     fresh random single-family address in the same canton (excluding
     every EGID already used anywhere in the dataset) and try that
     instead -- repeating until one works or an attempt cap is hit.

Both output files are updated together at the end:
  - solarrechner_quotes.csv  (full results, including a raw_json column
    with everything the API returned, for anything not pulled into a
    named column)
  - random_addresses.csv     (kept in sync -- any row that got replaced
    is updated here too, so a future re-run doesn't resurrect a broken
    address)

USAGE
-----
  python get_solarrechner_quotes.py                 # normal run
  python get_solarrechner_quotes.py --retry          # re-run only rows
      currently marked 'error' in solarrechner_quotes.csv (skips
      addresses that already succeeded); still auto-replaces stragglers
"""

import argparse
import csv
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---- shared endpoints / constants --------------------------------------
SEARCHSERVER = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
HEIGHT = "https://api3.geo.admin.ch/rest/services/height"
GWR_FIND = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
GWR_IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GWR_LAYER = "ch.bfs.gebaeude_wohnungs_register"

TACHION_PLANES = "https://tachionframework.ch/tachionserver/resources/sim/planes"
TACHION_BUILDING_REPORT = "https://tachionframework.ch/tachionserver/resources/sim/buildingReport"
TACHION_API_KEY = "3xs8-51ua-9ky5"

DEFAULT_ELECTRIC_TARIFF = [
    ["120.00", "0.2577", "0.2483", "0.00000"],
    ["0.00000", "0.08440", "0.08440", "3000.00"],
]
DEFAULT_ELECTRICITY_PROVIDER = "454"
GKLAS_EINFAMILIENHAUS = "1110"
GSTAT_EXISTING = "1004"

WORKERS = 3                 # main batch: keep low, this is a real vendor's API
SLEEP_BETWEEN_CALLS = 0.5
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3              # inline retries per address before giving up
MAX_REPLACEMENT_ATTEMPTS_PER_ROW = 6   # fresh-address attempts for stragglers

ADDRESS_FIELDNAMES = ["canton", "address", "plz", "municipality", "egid"]
QUOTE_FIELDNAMES = ADDRESS_FIELDNAMES + [
    "buildingGroundArea_sqm", "roofPlanesFound", "roofPlanesSelected",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
    "error", "raw_json",
]

CANTON_BBOX = {
    "ZH": (2670000, 1225000, 2717000, 1284000), "BE": (2560000, 1130000, 2680000, 1225000),
    "LU": (2630000, 1190000, 2680000, 1230000), "UR": (2660000, 1160000, 2700000, 1205000),
    "SZ": (2680000, 1195000, 2720000, 1230000), "OW": (2645000, 1180000, 2680000, 1210000),
    "NW": (2665000, 1195000, 2690000, 1215000), "GL": (2705000, 1195000, 2735000, 1225000),
    "ZG": (2670000, 1220000, 2695000, 1240000), "FR": (2560000, 1160000, 2610000, 1210000),
    "SO": (2600000, 1215000, 2645000, 1250000), "BS": (2610000, 1265000, 2625000, 1275000),
    "BL": (2605000, 1245000, 2645000, 1270000), "SH": (2670000, 1275000, 2705000, 1295000),
    "AR": (2735000, 1245000, 2760000, 1265000), "AI": (2740000, 1235000, 2760000, 1250000),
    "SG": (2705000, 1215000, 2770000, 1270000), "GR": (2705000, 1140000, 2830000, 1220000),
    "AG": (2630000, 1225000, 2680000, 1270000), "TG": (2700000, 1250000, 2745000, 1285000),
    "TI": (2685000, 1075000, 2740000, 1160000), "VD": (2485000, 1120000, 2580000, 1200000),
    "VS": (2560000, 1075000, 2680000, 1150000), "NE": (2530000, 1185000, 2575000, 1225000),
    "GE": (2485000, 1105000, 2515000, 1135000), "JU": (2565000, 1225000, 2620000, 1260000),
}


# ---- geocoding / building lookup helpers -------------------------------
def geocode_address(address_text):
    r = requests.get(SEARCHSERVER, params={
        "type": "locations", "origins": "address", "searchText": address_text,
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"No geocoding match for: {address_text}")
    return results[0]["attrs"]


def gwr_lookup_by_egid(egid):
    r = requests.get(GWR_FIND, params={
        "layer": GWR_LAYER, "searchField": "egid", "searchText": egid,
        "contains": "false", "returnGeometry": "false", "sr": "2056",
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"No GWR record for EGID {egid}")
    return results[0]["attributes"]


def get_elevation(x_lv03, y_lv03):
    r = requests.get(HEIGHT, params={"easting": y_lv03, "northing": x_lv03, "sr": "21781"}, timeout=15)
    r.raise_for_status()
    return r.json().get("height")


def parse_street_and_number(address_text):
    street_part = address_text.split(",")[0].strip()
    m = re.match(r"^(.*?)\s+(\d+[a-zA-Z]?)$", street_part)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return street_part, ""


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


def find_one_random_address(canton_code, excluded_egids, max_attempts=150):
    """Used only for replacing a consistently-failing row."""
    min_e, min_n, max_e, max_n = CANTON_BBOX[canton_code]
    for _ in range(max_attempts):
        e = random.uniform(min_e, max_e)
        n = random.uniform(min_n, max_n)
        try:
            results = identify_building(e, n)
        except requests.RequestException:
            time.sleep(1)
            continue
        time.sleep(0.1)
        for res in results:
            attrs = res.get("attributes", {})
            egid = str(attrs.get("egid"))
            if egid in excluded_egids:
                continue
            if str(attrs.get("gdekt")) != canton_code:
                continue
            if str(attrs.get("gklas")) != GKLAS_EINFAMILIENHAUS:
                continue
            if str(attrs.get("gstat")) != GSTAT_EXISTING:
                continue
            return {
                "canton": canton_code,
                "address": attrs.get("strname_deinr", ""),
                "plz": str(attrs.get("plz_plz6", "")).split("/")[0],
                "municipality": attrs.get("ggdename", ""),
                "egid": egid,
            }
    return None


# ---- Tachion request building / cost extraction -------------------------
def build_planes_request(address_text):
    geo = geocode_address(address_text)
    egid = geo["featureId"].split("_")[0]
    gwr = gwr_lookup_by_egid(egid)
    x, y = geo["x"], geo["y"]
    z = get_elevation(x, y)
    x_f, y_f = float(x), float(y)
    street, num = parse_street_and_number(address_text)

    half = 30  # metres -- approximate parcel box (no public nationwide cadastral API)
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
    """row needs: canton, address, plz, municipality, egid.
    Retries inline on transient failures (network errors or an 'invalid'
    API response); gives up after MAX_RETRIES and returns row + 'error'."""
    address_text = f"{row['address']}, {row['plz']} {row['municipality']}"
    out = dict(row)
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            payload = build_planes_request(address_text)
            r = requests.post(TACHION_PLANES, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            planes_resp = r.json()

            if not planes_resp.get("valid"):
                last_error = f"planes invalid: {planes_resp.get('errorMessageForUser')}"
                time.sleep(1 + attempt)
                continue

            report_payload = {
                "apiKey": TACHION_API_KEY,
                "signature": {"request": "603", "event": "buildingReport", "state": "SOLAR_OPT"},
                "attrs": planes_resp["attrs"], "roof": planes_resp.get("roof", []),
                "wall": planes_resp.get("wall", []),
            }
            r2 = requests.post(TACHION_BUILDING_REPORT, json=report_payload, timeout=REQUEST_TIMEOUT)
            r2.raise_for_status()
            report = r2.json()

            if not report.get("valid"):
                last_error = f"buildingReport invalid: {report.get('errorMessageForUser')}"
                time.sleep(1 + attempt)
                continue

            pv_all = find_cost_component(report.get("costAnalysis", {}), "pvModuleAll")
            out["buildingGroundArea_sqm"] = planes_resp["attrs"].get("buildingGroundArea")
            out["roofPlanesFound"] = len(planes_resp.get("roof", []))
            out["roofPlanesSelected"] = sum(
                1 for p in planes_resp.get("roof", []) if p.get("selected") == "true"
            )
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


# ---- straggler replacement ----------------------------------------------
def replace_stragglers(quote_rows, address_rows):
    """For every row still marked 'error', find and confirm a fresh
    replacement address in the same canton. Updates quote_rows and
    address_rows in place. Returns the list of rows that still couldn't
    be fixed."""
    used_egids_by_canton = {}
    for row in quote_rows:
        used_egids_by_canton.setdefault(row["canton"], set()).add(str(row["egid"]))

    failed = [(i, r) for i, r in enumerate(quote_rows) if r.get("error")]
    if not failed:
        return []

    print(f"\n{len(failed)} row(s) still failing after inline retries -- finding replacements.")
    still_broken = []

    for i, row in failed:
        canton = row["canton"]
        old_egid = str(row["egid"])
        fixed = None
        for attempt in range(1, MAX_REPLACEMENT_ATTEMPTS_PER_ROW + 1):
            candidate = find_one_random_address(canton, used_egids_by_canton.get(canton, set()))
            if candidate is None:
                print(f"  [{canton}] couldn't find a new candidate address (attempt {attempt})")
                continue
            used_egids_by_canton.setdefault(canton, set()).add(candidate["egid"])
            print(f"  [{canton}] trying replacement: {candidate['address']} (attempt {attempt})")
            result = get_quote_for_row(candidate)
            if not result.get("error"):
                fixed = result
                print(f"  [{canton}] replacement succeeded: {candidate['address']}")
                break
            print(f"    -> also failed: {result['error']}")

        if fixed:
            quote_rows[i] = fixed
            for j, arow in enumerate(address_rows):
                if arow["canton"] == canton and str(arow["egid"]) == old_egid:
                    address_rows[j] = {k: fixed.get(k, "") for k in ADDRESS_FIELDNAMES}
                    break
        else:
            print(f"  [{canton}] giving up after {MAX_REPLACEMENT_ATTEMPTS_PER_ROW} attempts "
                  f"-- original row kept, needs a manual look.")
            still_broken.append(row)

    return still_broken


def safe_write_csv(path, fieldnames, rows):
    """Write a CSV, retrying if the file is locked (e.g. still open in
    Excel) instead of crashing and losing an already-completed run."""
    while True:
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for r in rows:
                    writer.writerow({k: r.get(k, "") for k in fieldnames})
            return
        except PermissionError:
            input(f"\n'{path}' appears to be open in another program (e.g. Excel) and can't "
                  f"be written to. Close it, then press Enter to try again...")


# ---- main ----------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    _here = os.path.dirname(os.path.abspath(__file__))
    parser.add_argument("--in", dest="infile", default=os.path.join(_here, "random_addresses.csv"))
    parser.add_argument("--out", dest="outfile", default=os.path.join(_here, "solarrechner_quotes.csv"))
    parser.add_argument("--retry", action="store_true",
                         help="only re-attempt rows currently marked 'error' in --out; "
                              "successful rows are left untouched")
    args = parser.parse_args()

    with open(args.infile, newline="", encoding="utf-8-sig") as f:
        address_rows = list(csv.DictReader(f))

    if args.retry:
        with open(args.outfile, newline="", encoding="utf-8-sig") as f:
            existing = list(csv.DictReader(f))
        already_ok = [r for r in existing if not r.get("error")]
        to_process = [r for r in existing if r.get("error")]
        print(f"Retrying {len(to_process)} failed rows out of {len(existing)} total "
              f"({len(already_ok)} already succeeded and will be left as-is).")
    else:
        already_ok = []
        to_process = address_rows
        print(f"Loaded {len(to_process)} addresses from {args.infile}")

    results = []
    if to_process:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(get_quote_for_row, row): row for row in to_process}
            done = 0
            for future in as_completed(futures):
                row = futures[future]
                done += 1
                try:
                    res = future.result()
                except Exception as exc:
                    res = dict(row)
                    res["error"] = f"unexpected failure: {exc}"
                results.append(res)
                status = "OK" if not res.get("error") else f"ERROR: {res.get('error')}"
                print(f"[{done}/{len(to_process)}] {row['canton']} {row['address']}: {status}")
                time.sleep(SLEEP_BETWEEN_CALLS)

    all_rows = already_ok + results

    # Safety net: dump results immediately, before the (possibly locked)
    # final write, so a permission error can never lose a completed run.
    try:
        with open(args.outfile + ".partial", "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=QUOTE_FIELDNAMES)
            writer.writeheader()
            for r in all_rows:
                writer.writerow({k: r.get(k, "") for k in QUOTE_FIELDNAMES})
    except OSError:
        pass  # best-effort only

    still_broken = replace_stragglers(all_rows, address_rows)

    safe_write_csv(args.outfile, QUOTE_FIELDNAMES, all_rows)
    safe_write_csv(args.infile, ADDRESS_FIELDNAMES, address_rows)

    try:
        import os
        os.remove(args.outfile + ".partial")
    except OSError:
        pass

    n_ok = sum(1 for r in all_rows if not r.get("error"))
    print(f"\nDone. {n_ok}/{len(all_rows)} succeeded overall.")
    print(f"Wrote {args.outfile} and {args.infile}.")
    if still_broken:
        print(f"\n{len(still_broken)} row(s) could not be fixed automatically:")
        for r in still_broken:
            print(f"  {r['canton']} {r['address']}: {r.get('error')}")


if __name__ == "__main__":
    main()