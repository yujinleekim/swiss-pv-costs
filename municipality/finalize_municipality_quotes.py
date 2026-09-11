"""
Finalize municipality_quotes.csv:
  1. Dedupe by egid -- if an address has multiple rows (e.g. a failed
     attempt from an earlier pass and a successful one from a later
     retry), keep the successful one. If all rows for an egid failed,
     keep just one (the most recent).
  2. Print a breakdown of remaining errors by type, so the final error
     count has a clear "why", not just a number.

Overwrites municipality_quotes.csv with the deduped version, and also
writes a smaller Excel-friendly summary (no raw_json) the same way
misc/summary_csv.py did for the canton-level dataset.
"""

import csv
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "municipality_quotes.csv")
SUMMARY_OUTFILE = os.path.join(_HERE, "municipality_quotes_summary.csv")

SUMMARY_COLUMNS = [
    "canton", "municipality", "bfs_code", "address", "plz",
    "buildingGroundArea_sqm",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
]

ERROR_PATTERNS = [
    ("Missing weather data (server-side, permanent)", "readMeteodataFile"),
    ("Ran out of BUILDING_TOO_BIG retries", "BUILDING_TOO_BIG"),
    ("Opaque server error (null / NullPointerException)", r"buildingReport invalid:.*(null|NullPointerException)"),
    ("Geocoding/GWR lookup failed", r"No geocoding match|No GWR record"),
]


def classify_error(msg):
    if not msg:
        return None
    for label, pattern in ERROR_PATTERNS:
        if re.search(pattern, msg):
            return label
    return "Other / unclassified"


def main():
    with open(INFILE, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())
    print(f"Loaded {len(rows)} raw rows from {INFILE}")

    # dedupe by egid: prefer a successful row, else keep the last failed one
    best_by_egid = {}
    for row in rows:
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

    deduped = list(best_by_egid.values())
    print(f"After deduping by egid: {len(deduped)} unique addresses "
          f"({len(rows) - len(deduped)} duplicate rows removed)")

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
                  f"(likely near-total coverage gaps, e.g. Wildberg-style):")
            for (canton, muni), count in sorted(fully_failed, key=lambda x: -x[1])[:20]:
                print(f"  {canton} {muni}: {count} failed")

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