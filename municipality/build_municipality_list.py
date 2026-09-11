"""
Build the full national list of Swiss municipality NAMES (Level 3 in the
BFS register), for get_municipality_addresses.py to loop over.

NOTE: this deliberately does NOT resolve canton via the 'Parent' field.
That field turned out to be unreliable for this purpose -- for some
municipalities (confirmed with Horgen ZH) it points into an unrelated
municipality's historical lineage rather than the administrative parent,
so walking it produced wrong cantons (e.g. Horgen resolving to VS).
Canton is resolved separately and more reliably in
get_municipality_addresses.py, using the 'kanton' attribute returned
directly by the swissBOUNDARIES3D layer when fetching each municipality's
bbox -- the same lookup already needed for sampling, so no extra cost.
"""

import csv
import io
import os
import requests

OUTFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_municipalities.csv")

SNAPSHOT_URL = "https://www.agvchapp.bfs.admin.ch/api/communes/snapshot"


def fetch_snapshot(date_str="01-01-2026"):
    r = requests.get(SNAPSHOT_URL, params={"date": date_str}, timeout=30)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return list(reader)


def main():
    rows = fetch_snapshot()
    print(f"Total records in snapshot: {len(rows)}")

    municipalities = [row for row in rows if row.get("Level") == "3"]
    print(f"Municipalities (Level 3): {len(municipalities)}")

    output_rows = [
        {"municipality": row["Name"], "bfs_code": row["BfsCode"]}
        for row in municipalities
    ]

    with open(OUTFILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["municipality", "bfs_code"])
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"\nWrote {len(output_rows)} municipalities to {OUTFILE} "
          f"(no canton column -- resolved later from the boundary lookup)")


if __name__ == "__main__":
    main()