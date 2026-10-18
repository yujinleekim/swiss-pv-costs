"""
Replace addresses whose quote failed with other single-family homes from the SAME
municipality, drawn with the same rules and the same seeded order as
get_municipality_addresses.py (the next candidates in that municipality's shuffled list).

All failed addresses are replaced if possible:
  - readMeteodataFile: the missing weather file is CH<PLZ>.met for the address's postal code
    (156/156 on 2026-10-18), and no address with such a PLZ ever succeeded. So candidates with a
    PLZ that ever had this error are excluded; municipalities with only such PLZs stay failed.
  - BUILDING_TOO_BIG from Tachion /planes -- normal-sized houses (median 107 m2); the website
    showed no result for the one address tested
  - buildingReport NullPointerException / null, /attrs errors, anything else
A municipality gets at most MAX_CANDIDATES_PER_SLOT candidates per failed address in total,
counting candidates tried in earlier runs (e.g. Collonges VS, where every address failed).

Quote rows for every candidate tried are APPENDED to municipality_quotes.csv, like
get_municipality_quotes.py does. municipality_addresses.csv is rewritten after each
municipality, with each failed address replaced in place by a successful one. Afterwards run
finalize_municipality_quotes.py: it keeps one row per address in municipality_addresses.csv.
Re-running is safe: an address that was ever tried is never picked again, and the
per-municipality candidate budget counts earlier runs.

USAGE (after get_municipality_quotes.py has run):
  python municipality/replace_failed_municipality_addresses.py
  python municipality/replace_failed_municipality_addresses.py --workers 10
"""

import argparse
import csv
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import finalize_municipality_quotes as finalize
import get_municipality_addresses as sampler
import get_municipality_quotes as quotes

MAX_CANDIDATES_PER_SLOT = 5
DEFAULT_WORKERS = 20
WEATHER_ERROR = "readMeteodataFile"


def load_quote_status():
    """Returns (egid -> None if any quote row succeeded, else the last error message;
    bfs_code -> EGIDs ever quoted there; PLZs that ever had a missing-weather-data error)."""
    status, tried, weather_plz = {}, defaultdict(set), set()
    # attempts file first (older rows), so the current quotes file wins for each EGID
    for path in (finalize.ATTEMPTS_FILE, quotes.OUTFILE):
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            egid = row["egid"]
            tried[row["bfs_code"]].add(egid)
            if not row.get("error"):
                status[egid] = None
            elif status.get(egid, "") is not None:
                status[egid] = row["error"]
            if WEATHER_ERROR in (row.get("error") or ""):
                weather_plz.add(row["plz"])
    return status, tried, weather_plz


def write_addresses(rows):
    """Write to a temp file, then swap it in. Retries when Windows briefly locks the file
    (OSError Errno 22 / PermissionError seen on 2026-10-16 mid-run)."""
    tmp = sampler.OUTFILE + ".tmp"
    for _ in range(30):
        try:
            with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=sampler.OUTPUT_FIELDNAMES, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
            os.replace(tmp, sampler.OUTFILE)
            return
        except OSError as exc:
            print(f"  could not write {sampler.OUTFILE} ({exc}), retrying...")
            time.sleep(2)
    raise OSError(f"could not write {sampler.OUTFILE} after 30 attempts -- is it open in another program?")


def replace_in_municipality(homes, seed, rows, slots, tried, weather_plz):
    """Quote the next candidates until every slot has a success or the budget is used up.
    `rows` = this municipality's address rows, `slots` = the failed ones among them,
    `tried` = EGIDs ever quoted in this municipality, `weather_plz` = PLZs without weather data.
    Returns (successful candidate homes, quote rows of all candidates tried)."""
    by_egid = {h["egid"]: h for h in homes}
    slot_egids = {r["egid"] for r in slots}
    keep = [by_egid[r["egid"]] for r in rows if r["egid"] not in slot_egids and r["egid"] in by_egid]
    exclude = set(tried) | {h["egid"] for h in homes if h["plz"] in weather_plz}
    new_ok, quote_rows = [], []
    earlier_candidates = len(set(tried) - {r["egid"] for r in rows})
    budget = max(0, MAX_CANDIDATES_PER_SLOT * len(slots) - earlier_candidates)
    while len(new_ok) < len(slots) and budget > 0:
        candidates, _, _ = sampler.sample_municipality(
            homes, sampler.municipality_rng(seed, rows[0]["bfs_code"]),
            keep=keep + new_ok, exclude=exclude)
        if not candidates:
            break
        for home in candidates[:len(slots) - len(new_ok)]:
            if budget == 0:
                break
            budget -= 1
            exclude.add(home["egid"])
            result = quotes.get_quote_for_row({k: home[k] for k in sampler.OUTPUT_FIELDNAMES})
            quote_rows.append(result)
            if not result.get("error"):
                new_ok.append(home)
    return new_ok, quote_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=sampler.DEFAULT_SEED,
                        help="must be the seed get_municipality_addresses.py used")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    with open(sampler.OUTFILE, newline="", encoding="utf-8-sig") as f:
        addresses = list(csv.DictReader(f))
    status, tried, weather_plz = load_quote_status()
    not_quoted = [a for a in addresses if a["egid"] not in status]
    if not_quoted:
        print(f"{len(not_quoted)} addresses have no quote row yet -- run get_municipality_quotes.py first.")
        return

    failed = [a for a in addresses if status[a["egid"]]]
    n_weather = sum(WEATHER_ERROR in status[a["egid"]] for a in failed)
    slots_by_muni = defaultdict(list)
    for a in failed:
        slots_by_muni[a["bfs_code"]].append(a)
    rows_by_muni = defaultdict(list)
    for a in addresses:
        rows_by_muni[a["bfs_code"]].append(a)

    n_slots = sum(len(v) for v in slots_by_muni.values())
    print(f"{n_slots} failed addresses in {len(slots_by_muni)} municipalities ({n_weather} missing weather "
          f"data; {len(weather_plz)} PLZs without weather data are excluded as candidates)\n")
    if not slots_by_muni:
        return

    cantons = sorted({rows_by_muni[b][0]["canton"] for b in slots_by_muni})
    homes = sampler.load_all_homes(cantons)

    fixed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(replace_in_municipality, homes.get(bfs, []), args.seed,
                        rows_by_muni[bfs], slots, tried[bfs], weather_plz): bfs
            for bfs, slots in slots_by_muni.items()
        }
        for future in as_completed(futures):
            bfs = futures[future]
            slots = slots_by_muni[bfs]
            muni = f"{slots[0]['canton']} {slots[0]['municipality']}"
            done += 1
            try:
                new_ok, quote_rows = future.result()
            except Exception as exc:
                print(f"[{done}/{len(futures)}] {muni}: unexpected error, skipped: {exc}")
                continue
            if quote_rows:
                quotes.append_rows(quote_rows)
            # replace failed addresses in place, in their original order
            for old, new in zip(slots, new_ok):
                addresses[addresses.index(old)] = {k: new[k] for k in sampler.OUTPUT_FIELDNAMES}
            if new_ok:
                write_addresses(addresses)
            fixed += len(new_ok)
            print(f"[{done}/{len(futures)}] {muni}: replaced {len(new_ok)}/{len(slots)} "
                  f"({len(quote_rows)} candidates tried)")

    print(f"\nDone. Replaced {fixed}/{n_slots} failed addresses; {n_slots - fixed} still failing.")
    print("Next: python municipality/finalize_municipality_quotes.py")


if __name__ == "__main__":
    main()
