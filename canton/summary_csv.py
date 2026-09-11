"""
Strip solarrechner_quotes.csv down to a clean, human-readable summary:
drops raw_json (unreadable JSON blob) and the internal QC columns
(roofPlanesFound/Selected), keeping just what's meant for a reader.

Lives in canton/, alongside solarrechner_quotes.csv.
"""

import csv
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "solarrechner_quotes.csv")
OUTFILE = os.path.join(_HERE, "solarrechner_quotes_summary.csv")

SUMMARY_COLUMNS = [
    "canton", "address", "plz", "municipality",
    "buildingGroundArea_sqm",
    "grossInvest_CHF", "subsidies_CHF", "netInvest_CHF", "paybackTime_years",
    "producedKwhYear_kWh", "selfConsumedKwhYear_kWh", "selfConsumptionPct_pct", "gridFeedinKwhYear_kWh",
]

with open(INFILE, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

with open(OUTFILE, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in SUMMARY_COLUMNS})

print(f"Wrote {OUTFILE} ({len(rows)} rows, {len(SUMMARY_COLUMNS)} columns).")