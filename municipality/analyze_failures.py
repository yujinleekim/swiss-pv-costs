"""
Deeper breakdown of the remaining failures in municipality_quotes.csv,
beyond finalize_municipality_quotes.py's summary -- for the writeup to
Cloe.
"""

import csv
import os
import re
from collections import Counter, defaultdict

INFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "municipality_quotes.csv")

ERROR_PATTERNS = [
    ("Missing weather data", "readMeteodataFile"),
    ("BUILDING_TOO_BIG (exhausted retries)", "BUILDING_TOO_BIG"),
    ("Opaque server error (null/NPE)", r"buildingReport invalid:.*(null|NullPointerException)"),
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

    failures = [r for r in rows if r.get("error")]
    successes = [r for r in rows if not r.get("error")]
    print(f"Total: {len(rows)} | Success: {len(successes)} | Failed: {len(failures)} "
          f"({len(failures)/len(rows)*100:.2f}%)\n")

    # --- per-canton breakdown ---
    all_cantons = sorted(set(r["canton"] for r in rows))
    print("=== Per-canton failure counts ===")
    print(f"{'Canton':8}{'Total addr':12}{'Failed':8}{'Fail %':8}{'Munis w/ any fail':20}{'Munis fully failed':20}")
    for canton in all_cantons:
        canton_rows = [r for r in rows if r["canton"] == canton]
        canton_fails = [r for r in canton_rows if r.get("error")]
        if not canton_fails:
            continue
        fail_by_muni = Counter(r["municipality"] for r in canton_fails)
        total_by_muni = Counter(r["municipality"] for r in canton_rows)
        fully_failed_munis = sum(1 for m, c in fail_by_muni.items() if c >= total_by_muni[m])
        print(f"{canton:8}{len(canton_rows):<12}{len(canton_fails):<8}"
              f"{len(canton_fails)/len(canton_rows)*100:<8.1f}{len(fail_by_muni):<20}{fully_failed_munis:<20}")

    # --- full listing of every non-weather failure (small enough to list individually) ---
    non_weather = [r for r in failures if classify_error(r["error"]) != "Missing weather data"]
    print(f"\n=== All {len(non_weather)} non-weather-data failures, individually ===")
    for r in non_weather:
        print(f"  [{classify_error(r['error'])}] {r['canton']} {r['municipality']} {r['address']}: {r['error']}")

    # --- are the weather-data failures geographically clustered? ---
    weather_fails = [r for r in failures if classify_error(r["error"]) == "Missing weather data"]
    weather_munis = sorted(set((r["canton"], r["municipality"]) for r in weather_fails))
    print(f"\n=== {len(weather_munis)} municipalities affected by missing weather data ===")
    by_canton = defaultdict(list)
    for canton, muni in weather_munis:
        by_canton[canton].append(muni)
    for canton, munis in sorted(by_canton.items()):
        print(f"  {canton} ({len(munis)}): {', '.join(sorted(munis))}")


if __name__ == "__main__":
    main()