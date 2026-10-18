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

# Result columns in the order and with the names of the SolarRechner website: "Ergebnisse
# Photovoltaik" panel (same API fields: Eac, suppliedOcn, ownConsumptionFraction, feedinCurrent,
# grossInvest, subsidies, paybackTime) and "Nettoinvestition" from the cost dialog.
WEBSITE_LABELS = {
    "producedKwhYear_kWh": "Produzierter Solarstrom [kWh/Jahr]",
    "selfConsumedKwhYear_kWh": "Solarstrom selber verbraucht [kWh/Jahr]",
    "selfConsumptionPct_pct": "Eigenverbrauchsanteil [%]",
    "gridFeedinKwhYear_kWh": "Solarstrom ans Netz abgegeben [kWh/Jahr]",
    "grossInvest_CHF": "Kosten schlüsselfertige Anlage [CHF]",
    "subsidies_CHF": "Kleine Einmalvergütung KLEIV [CHF]",
    "netInvest_CHF": "Nettoinvestition [CHF]",
    "paybackTime_years": "Amortisationsdauer der Anlage [Jahre]",
}
SUMMARY_COLUMNS = ["canton", "address", "plz", "municipality", "buildingGroundArea_sqm", *WEBSITE_LABELS]

with open(INFILE, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

with open(OUTFILE, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow([WEBSITE_LABELS.get(k, k) for k in SUMMARY_COLUMNS])
    for r in rows:
        writer.writerow([r.get(k, "") for k in SUMMARY_COLUMNS])

print(f"Wrote {OUTFILE} ({len(rows)} rows, {len(SUMMARY_COLUMNS)} columns).")