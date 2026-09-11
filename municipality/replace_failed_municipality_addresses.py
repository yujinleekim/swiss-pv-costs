"""
For failed addresses in municipality_quotes.csv, try replacing them with
a fresh random single-family address in the SAME municipality -- but only
for failure types that are actually address/building-specific, not
location-wide.

SKIPPED (not attempted -- would just fail identically again):
  - readMeteodataFile: missing weather data is tied to the whole area's
    weather grid cell, not the specific building. Any other address in
    the same municipality hits the same missing file.

ATTEMPTED (genuinely building/address-specific, worth retrying fresh):
  - BUILDING_TOO_BIG (exhausted the automatic parcel-widening retries)
  - opaque server errors (null / NullPointerException)
  - geocoding/GWR lookup failures

Updates municipality_quotes.csv AND municipality_addresses.csv together
so they stay in sync, same pattern as replace_failed_addresses.py did at
the canton level.
"""

import csv
import os
import random
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor

# ---- shared endpoints -----------------------------------------------------
FIND_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
GEMEINDE_LAYER = "ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill"
GWR_IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GWR_LAYER = "ch.bfs.gebaeude_wohnungs_register"
SEARCHSERVER = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
HEIGHT = "https://api3.geo.admin.ch/rest/services/height"

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

_HERE = os.path.dirname(os.path.abspath(__file__))
QUOTES_FILE = os.path.join(_HERE, "municipality_quotes.csv")
ADDRESSES_FILE = os.path.join(_HERE, "municipality_addresses.csv")
MAX_REPLACEMENT_ATTEMPTS = 20  # bumped up from 5 for one more aggressive pass on the
                                # remaining non-weather failures (Collonges/Simplon)
SESSION = requests.Session()

SKIP_PATTERN = re.compile("readMeteodataFile")


def is_skippable(error_msg):
    return bool(error_msg and SKIP_PATTERN.search(error_msg))


# ---- fresh address sampling (same logic as get_municipality_addresses.py) --
def fetch_municipality_bbox(name, retries=4):
    last_exc = None
    for attempt in range(retries):
        try:
            r = SESSION.get(FIND_URL, params={
                "layer": GEMEINDE_LAYER, "searchField": "gemname", "searchText": name,
                "contains": "false", "returnGeometry": "true", "geometryFormat": "geojson", "sr": "2056",
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
            last_exc = ValueError(f"No boundary results for: {name}")
        except requests.RequestException as exc:
            last_exc = exc
        time.sleep(1 + attempt)
    raise ValueError(f"Could not get boundary for '{name}': {last_exc}")


def identify_building(easting, northing, tolerance=150):
    params = {
        "geometryType": "esriGeometryPoint", "geometry": f"{easting},{northing}",
        "imageDisplay": "500,600,96",
        "mapExtent": f"{easting-500},{northing-500},{easting+500},{northing+500}",
        "tolerance": tolerance, "layers": f"all:{GWR_LAYER}", "returnGeometry": "false", "sr": "2056",
    }
    r = SESSION.get(GWR_IDENTIFY, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("results", [])


def find_one_replacement_address(muni_name, canton, excluded_egids, max_attempts=400):
    bbox = fetch_municipality_bbox(muni_name)
    min_e, min_n, max_e, max_n = bbox
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
            if attrs.get("ggdename") != muni_name:
                continue
            if str(attrs.get("gdekt")) != canton:
                continue
            if str(attrs.get("gklas")) != GKLAS_EINFAMILIENHAUS:
                continue
            if str(attrs.get("gstat")) != GSTAT_EXISTING:
                continue
            return {
                "canton": canton, "municipality": muni_name,
                "address": attrs.get("strname_deinr", ""),
                "plz": str(attrs.get("plz_plz6", "")).split("/")[0],
                "egid": egid,
            }
    return None


# ---- quoting (same logic as get_municipality_quotes.py) -------------------
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
    r = SESSION.get(FIND_URL, params={
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


def get_quote_for_row(row, max_retries=3):
    address_text = f"{row['address']}, {row['plz']} {row['municipality']}"
    out = dict(row)
    parcel_half = 30
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            payload = build_planes_request(address_text, parcel_half=parcel_half)
            r = SESSION.post(TACHION_PLANES, json=payload, timeout=30)
            r.raise_for_status()
            planes_resp = r.json()
            if not planes_resp.get("valid"):
                msg = planes_resp.get("errorMessageForUser") or ""
                last_error = f"planes invalid: {msg}"
                if "BUILDING_TOO_BIG" in msg:
                    parcel_half *= 2
                    continue
                if "readMeteodataFile" in msg:
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
            r2 = SESSION.post(TACHION_BUILDING_REPORT, json=report_payload, timeout=30)
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
            roof_list = [p for p in (planes_resp.get("roof") or []) if p]  # guard against malformed null entries
            out["buildingGroundArea_sqm"] = planes_resp["attrs"].get("buildingGroundArea")
            out["roofPlanesFound"] = len(roof_list)
            out["roofPlanesSelected"] = sum(1 for p in roof_list if p.get("selected") == "true")
            if not pv_all:
                last_error = "pvModuleAll not found"
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
            import json
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
        except (ValueError, KeyError) as exc:
            out["error"] = str(exc)
            return out

    out["error"] = f"failed after {max_retries + 1} attempts: {last_error}"
    return out


# ---- main -------------------------------------------------------------
def main():
    with open(QUOTES_FILE, newline="", encoding="utf-8-sig") as f:
        quote_rows = list(csv.DictReader(f))
    fieldnames = list(quote_rows[0].keys())

    with open(ADDRESSES_FILE, newline="", encoding="utf-8-sig") as f:
        address_rows = list(csv.DictReader(f))

    used_egids_by_muni = {}
    for row in quote_rows:
        used_egids_by_muni.setdefault((row["canton"], row["municipality"]), set()).add(row["egid"])

    failed = [(i, r) for i, r in enumerate(quote_rows) if r.get("error")]
    skipped = [r for i, r in failed if is_skippable(r["error"])]
    to_retry = [(i, r) for i, r in failed if not is_skippable(r["error"])]

    print(f"{len(failed)} failed rows total.")
    print(f"  {len(skipped)} skipped (permanent location-wide failures, e.g. missing weather data)")
    print(f"  {len(to_retry)} eligible for address replacement\n")

    fixed_count = 0
    still_broken = []

    for i, row in to_retry:
        canton, muni, old_egid = row["canton"], row["municipality"], row["egid"]
        key = (canton, muni)
        fixed = None
        try:
            for attempt in range(1, MAX_REPLACEMENT_ATTEMPTS + 1):
                try:
                    candidate = find_one_replacement_address(muni, canton, used_egids_by_muni.get(key, set()))
                except ValueError as exc:
                    print(f"  [{canton} {muni}] boundary lookup failed: {exc}")
                    break
                if candidate is None:
                    print(f"  [{canton} {muni}] no new candidate found (attempt {attempt})")
                    continue
                used_egids_by_muni.setdefault(key, set()).add(candidate["egid"])
                candidate["bfs_code"] = row["bfs_code"]
                print(f"  [{canton} {muni}] trying replacement: {candidate['address']} (attempt {attempt})")
                result = get_quote_for_row(candidate)
                if not result.get("error"):
                    fixed = result
                    print(f"  [{canton} {muni}] replacement succeeded: {candidate['address']}")
                    break
                print(f"    -> also failed: {result['error']}")
        except Exception as exc:
            # don't let one unexpected error (e.g. a malformed API response
            # we haven't seen before) take down the whole run and lose
            # everything fixed so far -- log it and move on
            print(f"  [{canton} {muni}] unexpected error, skipping this one: {exc}")

        if fixed:
            quote_rows[i] = fixed
            fixed_count += 1
            for j, arow in enumerate(address_rows):
                if arow["canton"] == canton and arow["municipality"] == muni and arow["egid"] == old_egid:
                    address_rows[j] = {k: fixed.get(k, "") for k in
                                        ["canton", "municipality", "bfs_code", "address", "plz", "egid"]}
                    break
        else:
            still_broken.append(row)

    with open(QUOTES_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(quote_rows)

    with open(ADDRESSES_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["canton", "municipality", "bfs_code", "address", "plz", "egid"])
        writer.writeheader()
        writer.writerows(address_rows)

    print(f"\nDone. Fixed {fixed_count}/{len(to_retry)} eligible failures.")
    print(f"Still failing: {len(still_broken) + len(skipped)} "
          f"({len(skipped)} permanent, {len(still_broken)} exhausted replacement attempts)")
    print(f"Rewrote {QUOTES_FILE} and {ADDRESSES_FILE}.")


if __name__ == "__main__":
    main()