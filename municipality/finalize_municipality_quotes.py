"""
Finalize municipality_quotes.csv:
  1. Dedupe by egid -- if an address has multiple rows (e.g. a failed
     attempt from an earlier pass and a successful one from a later
     retry), keep the successful one. If all rows for an egid failed,
     keep just one (the most recent).
     Only addresses still listed in municipality_addresses.csv are kept, so
     addresses swapped out by replace_failed_municipality_addresses.py drop out.
     Every row that is not kept is appended to municipality_quote_attempts.csv, so the
     replacement script still knows which candidates were already tried.
  2. Print a breakdown of remaining errors by type, so the final error
     count has a clear "why", not just a number.

Overwrites municipality_quotes.csv with the deduped version, and also
writes a smaller Excel-friendly summary (no raw_json) the same way
canton/summary_csv.py does for the canton-level dataset.
"""

import argparse
import csv
import os
import re

csv.field_size_limit(2**31 - 1)  # raw_json fields can exceed the 131072 default

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "municipality_quotes.csv")
ADDRESSES_FILE = os.path.join(_HERE, "municipality_addresses.csv")
ATTEMPTS_FILE = os.path.join(_HERE, "municipality_quote_attempts.csv")
SUMMARY_OUTFILE = os.path.join(_HERE, "municipality_quotes_summary.csv")

SUITABILITY_COLUMNS = ["nominalPowerTop_kW", "nominalPowerVeryGood_kW", "nominalPowerGood_kW",
                       "nominalPowerMean_kW", "nominalPowerLow_kW"]
SUMMARY_COLUMNS = [
    "canton", "municipality", "bfs_code", "address", "plz",
    "buildingGroundArea_sqm", "installedArea_sqm", "nominalPower_kW", *SUITABILITY_COLUMNS, "existingPV_kW",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
    "electricConsumption_kWh",
]

# "BUILDING_TOO_BIG" is Tachion's own code (/planes, Utils.getParcelBuilding()). Checked 2026-10-16:
# the failing houses are normal-sized (median GWR footprint 107 m2 vs 108 m2 for successful ones),
# and the website showed no result for the one address tested (Hauptstrasse 17, 8543 Gundetswil).
ERROR_PATTERNS = [
    ("Missing weather data for the postal code (server has no CH<PLZ>.met)", "readMeteodataFile"),
    ("Server error in planes, code BUILDING_TOO_BIG (normal-sized houses, median 107 m2)",
     "BUILDING_TOO_BIG"),
    ("Opaque server error in buildingReport (null / NullPointerException)",
     r"buildingReport invalid:.*(null|NullPointerException)"),
    ("Opaque server error in attrs", r"attrs invalid"),
    ("Geocoding/GWR lookup failed", r"No geocoding match|No GWR record"),
    ("Tachion /attrs returned another building", r"/attrs returned another building"),
    ("No PV system in buildingReport (pvSystem isActive false, grossInvest 0)", r"no PV system"),
    ("Excluded manually (reason in the error text)", r"excluded manually"),
]


def classify_error(msg):
    if not msg:
        return None
    for label, pattern in ERROR_PATTERNS:
        if re.search(pattern, msg):
            return label
    return "Other / unclassified"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--solar-thermal-off", action="store_true",
                        help="finalize the solar thermal variant (get_municipality_quotes.py --solar-thermal-off); "
                             "same addresses, no replacement, so nothing is written to the attempts file")
    args = parser.parse_args()
    global INFILE, SUMMARY_OUTFILE
    if args.solar_thermal_off:
        INFILE = os.path.join(_HERE, "municipality_quotes_solar_thermal_off.csv")
        SUMMARY_OUTFILE = os.path.join(_HERE, "municipality_quotes_solar_thermal_off_summary.csv")

    with open(INFILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())
    print(f"Loaded {len(rows)} raw rows from {INFILE}")

    with open(ADDRESSES_FILE, newline="", encoding="utf-8-sig") as f:
        address_egids = [r["egid"] for r in csv.DictReader(f)]
    listed = set(address_egids)

    # dedupe by egid: prefer a successful row, else keep the last failed one
    best_by_egid = {}
    for row in rows:
        if row["egid"] not in listed:
            continue  # replaced by replace_failed_municipality_addresses.py
        egid = row["egid"]
        existing = best_by_egid.get(egid)
        if existing is None:
            best_by_egid[egid] = row
        elif existing.get("error") and not row.get("error"):
            best_by_egid[egid] = row  # replace a failure with a success
        elif not existing.get("error"):
            pass  # already have a success, keep it
        else:
            best_by_egid[egid] = row  # both failed -- keep the more recent attempt

    deduped = [best_by_egid[e] for e in address_egids if e in best_by_egid]  # address-file order
    print(f"After deduping by egid: {len(deduped)} unique addresses "
          f"({len(rows) - len(deduped)} duplicate / replaced rows removed)")
    missing = len(address_egids) - len(deduped)
    if missing:
        print(f"WARNING: {missing} addresses have no quote row yet -- run get_municipality_quotes.py")

    successes = [r for r in deduped if not r.get("error")]
    failures = [r for r in deduped if r.get("error")]
    print(f"\n{len(successes)} succeeded, {len(failures)} still failing "
          f"({len(failures)/len(deduped)*100:.1f}%)")

    if failures:
        from collections import Counter
        categories = Counter(classify_error(r["error"]) for r in failures)
        print("\nFailure breakdown:")
        for label, count in categories.most_common():
            print(f"  {label}: {count}")

        # which municipalities are entirely (or mostly) missing?
        muni_fail_counts = Counter((r["canton"], r["municipality"]) for r in failures)
        fully_failed = [(m, c) for m, c in muni_fail_counts.items() if c >= 8]
        if fully_failed:
            print(f"\n{len(fully_failed)} municipalities with 8+ failed addresses "
                  f"(listed by count only):")
            for (canton, muni), count in sorted(fully_failed, key=lambda x: -x[1])[:20]:
                print(f"  {canton} {muni}: {count} failed")

    # keep the dropped rows (duplicates, replaced addresses) as attempt history
    kept = {id(r) for r in deduped}
    dropped = [r for r in rows if id(r) not in kept]
    if dropped and not args.solar_thermal_off:
        new_file = not os.path.exists(ATTEMPTS_FILE)
        with open(ATTEMPTS_FILE, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if new_file:
                writer.writeheader()
            writer.writerows(dropped)
        print(f"Appended {len(dropped)} dropped rows to {ATTEMPTS_FILE}")

    # rewrite the main file, deduped
    with open(INFILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(deduped)
    print(f"\nRewrote {INFILE} (deduped, {len(deduped)} rows)")

    # clean summary CSV, successes only, no raw_json
    with open(SUMMARY_OUTFILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        for r in successes:
            writer.writerow({k: r.get(k, "") for k in SUMMARY_COLUMNS})
    print(f"Wrote {SUMMARY_OUTFILE} ({len(successes)} successful rows, no raw_json)")


if __name__ == "__main__":
    main()