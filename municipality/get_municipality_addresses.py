"""
National municipality-level address sampler -- 10 random single-family
homes per municipality, for all 2,110 Swiss municipalities.

Samples directly from the federal building register (GWR) public bulk download
(https://public.madd.bfs.admin.ch/<canton>.zip; the sample of 2026-10-15 used the export of that day):
  1. Download each canton's GWR zip once (cached in gwr_cache/, gitignored).
  2. Eligible buildings: single-family home (GKLAS 1110), existing (GSTAT 1004),
     with a street address whose house number is plain numeric
     (5 -- not 5a, 30.1, 714a.99: those are usually extra buildings on the same plot).
     DOFFADR (official address flag) is NOT used: many cantons leave it empty, and
     whole alpine municipalities have 0 there for ordinary street addresses.
  3. Per municipality (GGDENR = bfs_code), shuffle the eligible buildings with a
     fixed seed and take the first 10, at most MAX_PER_STREET per street.
  4. Only for municipalities that still have fewer than 10, fill up in two steps:
     a. ignore MAX_PER_STREET (small villages with 1-3 streets);
     b. allow letter-suffixed numbers (5a), but at most one building per base address
        (street + leading number), so 5 / 5a / 5b are never sampled together.
        Dotted numbers (30.1, 714a.99) stay excluded: the TI ones all fail at
        Tachion /attrs (tested 2026-10-15, Lavizzara / Cerentino).
     Municipalities with 10 strict picks are not affected by these steps.

Every eligible single-family home has the same chance of being picked (the earlier
random-point method favoured isolated houses and needed hundreds of API calls for
sparse municipalities). Municipalities with fewer than 10 homes even after step 4
get all of them.

INPUT:  all_municipalities.csv  (from build_municipality_list.py)
OUTPUT: municipality_addresses.csv  (overwritten)

USAGE:
  python get_municipality_addresses.py              # all cantons, seed 42
  python get_municipality_addresses.py --seed 7
"""

import argparse
import csv
import io
import os
import random
import re
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

GWR_ZIP_URL = "https://public.madd.bfs.admin.ch/{canton}.zip"
GKLAS_EINFAMILIENHAUS = "1110"
GSTAT_EXISTING = "1004"

_HERE = os.path.dirname(os.path.abspath(__file__))
INFILE = os.path.join(_HERE, "all_municipalities.csv")
OUTFILE = os.path.join(_HERE, "municipality_addresses.csv")
CACHE_DIR = os.path.join(_HERE, "gwr_cache")
OUTPUT_FIELDNAMES = ["canton", "municipality", "bfs_code", "address", "plz", "egid"]

TARGET_PER_MUNICIPALITY = 10
MAX_PER_STREET = 2     # at most this many sampled houses on the same street (avoids one housing estate
                       # filling the whole municipality)
DEFAULT_SEED = 42

PLAIN_HOUSE_NO = re.compile(r"\d+")
LEADING_NUMBER = re.compile(r"\d+")
LETTER_SUFFIX_NO = re.compile(r"\d+[A-Za-z]+")


def download_canton_zip(canton):
    """Download <canton>.zip into CACHE_DIR unless it is already there."""
    path = os.path.join(CACHE_DIR, f"{canton.lower()}.zip")
    if os.path.exists(path):
        return path
    tmp = path + ".part"
    with requests.get(GWR_ZIP_URL.format(canton=canton.lower()), stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    os.replace(tmp, path)
    print(f"  downloaded {canton} ({os.path.getsize(path) / 1e6:.0f} MB)")
    return path


def read_tsv(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"), delimiter="\t")


def eligible_homes(zip_path):
    """Eligible single-family homes in one canton zip, grouped by bfs_code (GGDENR)."""
    with zipfile.ZipFile(zip_path) as zf:
        buildings = {
            b["EGID"]: b for b in read_tsv(zf, "gebaeude_batiment_edificio.csv")
            if b["GKLAS"] == GKLAS_EINFAMILIENHAUS and b["GSTAT"] == GSTAT_EXISTING
        }
        entrances = defaultdict(list)
        for e in read_tsv(zf, "eingang_entree_entrata.csv"):
            # house number must start with a digit (plain "5", or suffixed "5a" / "30.1" for step 4b)
            if e["EGID"] in buildings and e["STRNAME"] and LEADING_NUMBER.match(e["DEINR"]):
                entrances[e["EGID"]].append(e)

    by_municipality = defaultdict(list)
    for egid, ents in entrances.items():
        b = buildings[egid]
        plain = [x for x in ents if PLAIN_HOUSE_NO.fullmatch(x["DEINR"])]
        e = min(plain or ents, key=lambda x: int(x["EDID"] or 0))  # main entrance if a house has several
        by_municipality[b["GGDENR"]].append({
            "canton": b["GDEKT"], "municipality": b["GGDENAME"], "bfs_code": b["GGDENR"],
            "address": f"{e['STRNAME']} {e['DEINR']}", "plz": e["DPLZ4"], "egid": egid,
            # internal, not written to the CSV
            "_plain": bool(plain), "_letter_suffix": bool(LETTER_SUFFIX_NO.fullmatch(e["DEINR"])),
            "_street": e["STRNAME"].lower(),
            "_base": (e["STRNAME"].lower(), LEADING_NUMBER.match(e["DEINR"]).group()),
        })
    return by_municipality


def sample_municipality(homes, rng, keep=(), exclude=frozenset()):
    """Returns (newly picked homes, number added by step 4a, number added by step 4b).

    `keep` = homes already in the sample: they count towards the target, the street limit and
    the base addresses. `exclude` = EGIDs never to pick (e.g. addresses whose quote failed).
    With the same rng seed the shuffled order is the same, so replacements continue the
    original draw (replace_failed_municipality_addresses.py)."""
    by_egid = lambda h: int(h["egid"])  # independent of file order
    plain = sorted((h for h in homes if h["_plain"]), key=by_egid)
    rng.shuffle(plain)
    need = TARGET_PER_MUNICIPALITY - len(keep)
    taken = {h["egid"] for h in keep} | set(exclude)
    found, street_counts = [], defaultdict(int)
    for h in keep:
        street_counts[h["_street"]] += 1
    for h in plain:
        if len(found) >= need:
            break
        if h["egid"] in taken or street_counts[h["_street"]] >= MAX_PER_STREET:
            continue
        street_counts[h["_street"]] += 1
        taken.add(h["egid"])
        found.append(h)

    # step 4a: same shuffled order, without the street limit
    n_strict = len(found)
    for h in plain:
        if len(found) >= need:
            break
        if h["egid"] not in taken:
            taken.add(h["egid"])
            found.append(h)
    n_street = len(found) - n_strict

    # step 4b: letter-suffixed numbers (5a), one building per base address
    if len(found) < need:
        bases = {h["_base"] for h in [*keep, *found]}
        others = sorted((h for h in homes if h["_letter_suffix"]), key=by_egid)
        rng.shuffle(others)
        for h in others:
            if len(found) >= need:
                break
            if h["egid"] not in taken and h["_base"] not in bases:
                bases.add(h["_base"])
                taken.add(h["egid"])
                found.append(h)
    return found, n_street, len(found) - n_strict - n_street


def load_municipalities():
    with open(INFILE, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_all_homes(cantons):
    """Eligible homes for the given cantons, grouped by bfs_code (downloads missing zips)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    print("Downloading GWR canton files (cached after the first run)...")
    with ThreadPoolExecutor(max_workers=4) as pool:
        zip_paths = list(pool.map(download_canton_zip, cantons))
    homes = {}
    for path in zip_paths:
        homes.update(eligible_homes(path))
    return homes


def municipality_rng(seed, bfs_code):
    # one RNG per municipality: its sample does not depend on the others
    return random.Random(f"{seed}-{bfs_code}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    all_munis = load_municipalities()
    cantons = sorted({m["canton"] for m in all_munis})
    print(f"Loaded {len(all_munis)} municipalities in {len(cantons)} cantons from {INFILE}")
    homes = load_all_homes(cantons)

    rows, short, filled = [], [], []
    for m in all_munis:
        picked, n_street, n_suffix = sample_municipality(
            homes.get(m["bfs_code"], []), municipality_rng(args.seed, m["bfs_code"]))
        rows.extend(picked)
        label = f"{m['canton']} {m['municipality']} ({m['bfs_code']}): {len(picked)}"
        if n_street or n_suffix:
            filled.append(f"{label} (+{n_street} without street limit, +{n_suffix} letter-suffixed numbers)")
        if len(picked) < TARGET_PER_MUNICIPALITY:
            short.append(label)

    with open(OUTFILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nWrote {len(rows)} addresses for {len(all_munis)} municipalities to {OUTFILE}")
    print(f"{len(filled)} municipalities filled up with step 4:")
    for line in filled:
        print(f"  {line}")
    print(f"{len(short)} municipalities have fewer than {TARGET_PER_MUNICIPALITY}:")
    for line in short:
        print(f"  {line}")


if __name__ == "__main__":
    main()
