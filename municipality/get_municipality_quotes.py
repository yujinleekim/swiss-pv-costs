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
csv.field_size_limit(2**31 - 1)  # raw_json fields can exceed the 131072 default
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

TACHION_ATTRS = "https://tachionframework.com/tachionserver/resources/sim/attrs"
TACHION_PLANES = "https://tachionframework.com/tachionserver/resources/sim/planes"
TACHION_BUILDING_REPORT = "https://tachionframework.com/tachionserver/resources/sim/buildingReport"
# Fixed scenario for all quotes: "Bewohner im Haus" = 4, no facade (wall) planes and no roof planes
# tilted MAX_ROOF_TILT or more (wall-like surfaces, where panels are rarely installed).
# The website sends user-changed fields as signature.input [{paramName, value}].
OCCUPANT_NUMBER = "4"
# 75 = the threshold of the federal tilt bonus (EnFV, "Neigungswinkel von mindestens 75 Grad");
# in our tests Tachion's separate pvModuleSteep cost appeared from 75 deg on, never at 72 or less.
MAX_ROOF_TILT = 75
TACHION_API_KEY = "3xs8-51ua-9ky5"


_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "municipality_addresses.csv")
OUTFILE = os.path.join(_HERE, "municipality_quotes.csv")
OUTPUT_FIELDNAMES = [
    "canton", "municipality", "bfs_code", "address", "plz", "egid",
    "buildingGroundArea_sqm", "roofPlanesFound", "roofPlanesSelected",
    "installedArea_sqm", "nominalPower_kW", "existingPV_kW",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
    "error", "raw_json",
]

DEFAULT_WORKERS = 20  # measured 2026-10-15: 5 -> ~5,000 rows/h, 20 -> ~8,300 rows/h, 40 -> barely faster
SLEEP_BETWEEN_CALLS = 0.0   # was 0.5s per completed address -- redundant throttling
                             # on top of the WORKERS concurrency cap; at 21,100
                             # addresses that alone was adding ~3 hours of pure wait
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

SESSION = requests.Session()  # connection reuse instead of a fresh connection per call


def geocode_address(address_text, egid=None):
    """First geocoder hit, or -- if `egid` is given -- the hit for that building
    (featureId = "<EGID>_<entrance>"). No hit for the EGID raises ValueError, so a quote
    is never made for a different building than the sampled one."""
    r = SESSION.get(SEARCHSERVER, params={
        "type": "locations", "origins": "address", "searchText": address_text,
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"No geocoding match for: {address_text}")
    if not egid:
        return results[0]["attrs"]
    for res in results:
        if str(res["attrs"].get("featureId", "")).split("_")[0] == str(egid):
            return res["attrs"]
    raise ValueError(f"No geocoding match for EGID {egid}: {address_text}")


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


def build_planes_request(address_text, parcel_half=None, egid=None):
    """Build the /planes request the same way the SolarRechner website does.

    1. Geocode the address (federal geoportal SearchServer) to get featureId/label/num.
    2. Ask Tachion's own /attrs endpoint to fill in everything else: parcel polygon,
       elevation, coordinates, electricity provider and tariff. The website does exactly
       this, so the numbers match what a person sees on the website.
    3. Send the returned attrs unchanged to /planes.

    `parcel_half` is ignored (kept only so older callers don't break): the parcel now
    comes from Tachion instead of an approximate box.
    """
    geo = geocode_address(address_text, egid)
    detail = re.sub(r"<[^>]+>", "", geo.get("label", "")).strip()
    num = geo.get("num") or parse_street_and_number(address_text)[1]
    r = SESSION.post(TACHION_ATTRS, json={
        "apiKey": TACHION_API_KEY,
        "signature": {"request": "603", "event": "attrs"},
        "attrs": {"detail": detail, "num": num, "featureId": geo["featureId"]},
    }, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    resp = r.json()
    if not resp.get("valid"):
        # RequestException so the caller retries it like any other transient failure
        raise requests.RequestException(f"attrs invalid: {resp.get('errorMessageForUser')}")
    # /attrs sometimes answers with another building (other EGID, sometimes another house number);
    # the website sends the same request, so the sampled building cannot be quoted
    returned = str(resp["attrs"].get("featureId", ""))
    if egid and returned.split("_")[0] != str(egid):
        raise ValueError(f"/attrs returned another building (featureId {returned}) for EGID {egid}")
    return {"apiKey": TACHION_API_KEY, "signature": {"request": "603", "event": "planes"},
            "attrs": resp["attrs"]}


def switch_off_walls(walls):
    """All facade planes off. Some buildings have wall=null: sent unchanged
    (wall=[] makes /buildingReport return a 500)."""
    return [dict(w, selected="false") if w else w for w in walls] if walls else walls


def roof_tilt(plane):
    return float(((plane.get("feature") or {}).get("attributes") or {}).get("neigung") or 0)


def switch_off_steep_roofs(roofs):
    """Roof planes tilted MAX_ROOF_TILT or more off. Returns (roof list, ids switched off)."""
    if not roofs:
        return roofs, []
    out, off = [], []
    for p in roofs:
        if p and p.get("selected") == "true" and roof_tilt(p) >= MAX_ROOF_TILT:
            off.append(((p.get("feature") or {}).get("attributes") or {}).get("id"))
            p = dict(p, selected="false")
        out.append(p)
    return out, off


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

    for attempt in range(MAX_RETRIES + 1):
        try:
            payload = build_planes_request(address_text, egid=row.get("egid"))
            r = SESSION.post(TACHION_PLANES, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            planes_resp = r.json()

            if not planes_resp.get("valid"):
                msg = planes_resp.get("errorMessageForUser") or ""
                last_error = f"planes invalid: {msg}"
                if "BUILDING_TOO_BIG" in msg:
                    # parcel now comes from Tachion (not our own box), so widening it is no
                    # longer possible -- record the failure as-is
                    out["error"] = last_error
                    return out
                if "readMeteodataFile" in msg:
                    # the server has no weather file for this postal code (CH<PLZ>.met, chosen
                    # from attrs.plz4): every address with this PLZ fails the same way, so
                    # retrying cannot fix it
                    out["error"] = last_error
                    return out
                time.sleep(1 + attempt)
                continue

            # Same two requests as the website (checked against the website on 2026-10-17:
            # Steingasse 10 Hoechstetten 23,600, Bodenstrasse 17 Russikon 24,200, Brunngasse 10
            # Kleinandelfingen 41,600 CHF):
            # 1. SOLAR_OPT with the planes exactly as /planes returned them (the website's first result);
            # 2. no state, with the planes from response 1 where facade and steep roof planes are switched
            #    off, and the number of residents in signature.input (what a user changes on the site).
            r2 = SESSION.post(TACHION_BUILDING_REPORT, json={
                "apiKey": TACHION_API_KEY,
                "signature": {"request": "603", "event": "buildingReport", "state": "SOLAR_OPT"},
                "attrs": planes_resp["attrs"], "roof": planes_resp.get("roof", []),
                "wall": planes_resp.get("wall", []),
            }, timeout=REQUEST_TIMEOUT)
            r2.raise_for_status()
            report = r2.json()
            steep_off = []
            if report.get("valid"):
                final_roof, steep_off = switch_off_steep_roofs(report.get("roof"))
                r2 = SESSION.post(TACHION_BUILDING_REPORT, json={
                    "apiKey": TACHION_API_KEY,
                    "signature": {"request": "603", "event": "buildingReport",
                                  "input": [{"paramName": "occupantNumber", "value": OCCUPANT_NUMBER}]},
                    "attrs": planes_resp["attrs"], "roof": final_roof,
                    "wall": switch_off_walls(report.get("wall")),
                }, timeout=REQUEST_TIMEOUT)
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

            # Total PV cost as the website shows it: "pvModulePronovo" = pvModuleAll + pvModuleSteep
            # (pvModuleSteep: cost of selected planes tilted 75 deg or more; 0 once those are off).
            pv_all = find_cost_component(report.get("costAnalysis", {}), "pvModulePronovo")
            if not pv_all:
                pv_all = find_cost_component(report.get("costAnalysis", {}), "pvModuleAll")
                if pv_all:
                    print(f"    [warn] {row['municipality']} {row['address']}: pvModulePronovo missing, "
                          f"fell back to pvModuleAll (may exclude facade modules)")
            out["buildingGroundArea_sqm"] = planes_resp["attrs"].get("buildingGroundArea")
            roof_list = [p for p in (final_roof or []) if p]  # guard against malformed null entries
            out["roofPlanesFound"] = len(roof_list)
            out["roofPlanesSelected"] = sum(1 for p in roof_list if p.get("selected") == "true")
            if not pv_all:
                last_error = "pvModulePronovo/pvModuleAll not found in costAnalysis"
                time.sleep(1 + attempt)
                continue

            out["grossInvest_CHF"] = pv_all.get("grossInvest")
            out["subsidies_CHF"] = pv_all.get("subsidies")
            out["netInvest_CHF"] = pv_all.get("netInvest")
            out["paybackTime_years"] = pv_all.get("paybackTime")
            # Capacity as the website shows it: per-plane "Nennleistung, kW" = module area x PV efficiency
            # (20%), so the total is installedArea x 0.2. Not pvSystem.nominalPower: for buildings with an
            # existing PV system (existingNominalPower) it is lower than the website's value (checked
            # 2026-10-24 on Oberalbisstrasse 13, 8915: website 24.5, nominalPower 23.44; costs match).
            pv = report.get("pvSystem") or {}
            out["installedArea_sqm"] = pv.get("installedArea")
            if pv.get("installedArea"):
                eff = float(pv.get("pvEfficiencySTC") or 20) / 100
                out["nominalPower_kW"] = round(float(pv["installedArea"]) * eff, 3)
            out["existingPV_kW"] = pv.get("existingNominalPower")
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
                "steep_roof_planes_off": steep_off,
                "buildingReport_costAnalysis": report.get("costAnalysis"),
                "buildingReport_energySystem": report.get("energySystem"),
                "buildingReport_pvSystem": report.get("pvSystem"),
                "buildingReport_stSystem": report.get("stSystem"),
                "buildingReport_electricUser": report.get("electricUser"),
            }, ensure_ascii=False)
            # Valid response without a PV system (checked 2026-10-24: 39 rows, all grossInvest 0, no
            # nominalPower; 3 of them because the tilt rule switched off the only selected plane).
            # Same answer on every request, so no retry; counted as a failure so it gets replaced.
            if (report.get("pvSystem") or {}).get("isActive") != "true":
                out["error"] = "no PV system: buildingReport pvSystem isActive=false (grossInvest 0)"
                return out
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