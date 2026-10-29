"""
Strip solarrechner_quotes.csv down to a clean, human-readable summary:
drops raw_json (unreadable JSON blob) and the internal QC columns
(roofPlanesFound/Selected), keeping just what's meant for a reader.

Lives in canton/, alongside solarrechner_quotes.csv.
"""

import csv
import os

csv.field_size_limit(2**31 - 1)  # raw_json fields can exceed the 131072 default

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "solarrechner_quotes.csv")
OUTFILE = os.path.join(_HERE, "solarrechner_quotes_summary.csv")

SUITABILITY_COLUMNS = ["nominalPowerTop_kW", "nominalPowerVeryGood_kW", "nominalPowerGood_kW",
                       "nominalPowerMean_kW", "nominalPowerLow_kW"]
SUMMARY_COLUMNS = [
    "canton", "address", "plz", "municipality",
    "buildingGroundArea_sqm", "installedArea_sqm", "nominalPower_kW", *SUITABILITY_COLUMNS, "existingPV_kW",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
    "electricConsumption_kWh",
]

with open(INFILE, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

with open(OUTFILE, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in SUMMARY_COLUMNS})

print(f"Wrote {OUTFILE} ({len(rows)} rows, {len(SUMMARY_COLUMNS)} columns).")