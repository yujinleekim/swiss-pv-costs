# Swiss Residential Solar PV Cost — by Canton and by Municipality

Estimates typical residential solar PV installation costs across Switzerland,
using randomly sampled single-family homes and Switzerland's public
[SolarRechner](https://www.energieschweiz.ch/tools/solarrechner/) tool
(EnergieSchweiz / Swissolar). Two datasets:

- **Canton-level** (`solarrechner_quotes.csv`) — 2–3 addresses per canton, 26 cantons.
- **Municipality-level** (`municipality_quotes.csv`) — 10 addresses per
  municipality, all 2,110 Swiss municipalities.

## Approach

- **Addresses are randomly sampled, not hand-picked.** 2–3 single-family
  homes per canton (10 per municipality) are drawn from Switzerland's
  official building registry (GWR), filtered to confirmed single-family
  houses (`GKLAS 1110`, `GSTAT 1004`). Covers all 26 cantons.
- **Quotes come directly from SolarRechner's own underlying API.** The
  tool has no public documentation for programmatic access, so this
  automates its API directly rather than scripting a browser.
- **The whole pipeline is scripted end-to-end** — address sampling, quote
  retrieval, and failure handling all run without manual steps, so it's
  easy to re-run or extend (e.g. to municipality level).

## Repo structure

```
README.md
canton/
  get_address.py               Step 1 (canton):  sample random single-family addresses per canton
  get_solarrechner_quotes.py   Step 2 (canton):  get a cost quote for every address, with automatic
                                retry and automatic replacement of addresses that keep failing
  summary_csv.py                produce the clean summary CSV for sharing
  random_addresses.csv         canton-level sampled addresses
  solarrechner_quotes.csv      canton-level full results, incl. raw_json
  solarrechner_quotes_summary.csv  canton-level clean version -- no raw_json, no QC columns

municipality/
  build_municipality_list.py   Step 1 (municipality): full list of all 2,110 municipalities from
                                the official BFS register
  get_municipality_addresses.py  Step 2 (municipality): sample 10 single-family addresses per
                                municipality (checkpointed/resumable)
  get_municipality_quotes.py   Step 3 (municipality): get a cost quote for every address
                                (checkpointed/resumable, low concurrency -- see Methodology)
  finalize_municipality_quotes.py  Step 4 (municipality): dedupe retried rows, produce a
                                categorized failure breakdown, write the clean summary CSV
  replace_failed_municipality_addresses.py  Step 5 (municipality): swap out addresses that fail
                                for building/address-specific reasons (skips permanent,
                                location-wide failures automatically -- see Methodology)
  analyze_failures.py           optional: deeper per-canton/clustering breakdown of failures
  all_municipalities.csv       official list of all 2,110 municipalities
  municipality_addresses.csv   municipality-level sampled addresses
  municipality_quotes.csv      municipality-level full results, incl. raw_json
  municipality_quotes_summary.csv  municipality-level clean version -- no raw_json, no QC columns
```

Every script resolves its input/output file paths relative to its own
location, so each one works correctly run from anywhere, e.g. both
`python canton/get_address.py` (from the repo root) and
`cd canton && python get_address.py` behave identically.

## How it works

### 1. Address sampling (`get_address.py`)

Random points are generated inside each canton's approximate bounding box
and checked against the public
[GWR building registry](https://www.housing-stat.ch/) (via
`api3.geo.admin.ch`) until a confirmed single-family home is found. No
login or API key required — this is Switzerland's free federal geoportal.

```
python canton/get_address.py
```

Writes `random_addresses.csv`.

### 2. Quote retrieval (`get_solarrechner_quotes.py`)

For each address:

1. Geocode it via the same free federal geoportal (`SearchServer`) to get
   coordinates, and cross-reference the GWR for canton/municipality/BFS
   number.
2. Call SolarRechner's underlying API (hosted by its vendor, Tachion) in
   two steps: `/sim/planes` (detects roof geometry and orientation from
   Switzerland's solar cadastre) then `/sim/buildingReport` (returns the
   full cost/energy analysis for the detected roof).
3. Extract the headline numbers (installation cost, KLEIV subsidy, net
   cost, payback period, annual production, self-consumption) plus the
   complete raw API response for anything not otherwise captured.

Failures are retried automatically (both network errors and transient
server-side errors); any address that still fails after retries is
automatically swapped for a fresh random address in the same canton.

```
python canton/get_solarrechner_quotes.py            # full run
python canton/get_solarrechner_quotes.py --retry     # re-attempt only failed rows
```

Writes/updates `solarrechner_quotes.csv` and `random_addresses.csv` (kept
in sync if any address gets replaced).

### 3. Clean summary (`canton/summary_csv.py`)

```
python canton/summary_csv.py
```

Reads `solarrechner_quotes.csv` and writes
`solarrechner_quotes_summary.csv`, both in `canton/` — the version meant
for sharing, with the raw API dump and internal QC columns removed.

## Municipality-level dataset

Same underlying method as the canton-level dataset (random single-family
address sampling from the GWR + SolarRechner quotes), scaled up to all
2,110 Swiss municipalities, 10 addresses each (~21,100 addresses total).

### 1. Municipality list (`build_municipality_list.py`)

Pulls the official, current list of all municipalities from BFS's
[Historicized Directory of Swiss Municipalities](https://www.agvchapp.bfs.admin.ch/)
(free REST API, no login). Only municipality name + BFS number are taken
from this source — **not** canton, since this register's `Parent` field
turned out to be unreliable for that (it occasionally links to an
unrelated municipality's historical lineage rather than the true
administrative parent — confirmed wrong for Horgen ZH, which resolved to
VS via that field). Canton is instead taken from the GWR building data
itself during sampling, which has been reliable throughout this project.

```
python municipality/build_municipality_list.py
```

Writes `all_municipalities.csv`.

### 2. Address sampling (`get_municipality_addresses.py`)

For each municipality: fetch its real boundary box from the
`swissboundaries3d-gemeinde-flaeche.fill` layer (not a hand-estimated
canton-wide box like the canton-level script uses — needed at this scale
since municipality size varies hugely, from 3 to several hundred per
canton), then sample random points inside it and check against the GWR,
same filtering as the canton-level script.

Checkpointed and resumable — progress is written incrementally, and a
re-run skips municipalities that already have their full 10 addresses.
Essential at this scale: a single run took long enough that interruptions
(closed laptop, etc.) were routine, not exceptional.

```
python municipality/get_municipality_addresses.py
```

Result: **2,109 of 2,110 municipalities at a full 10/10**; the last
(Mauraz, VD — population 60) capped at 8/10, confirmed to be a genuine
address scarcity (verified against public population/household figures)
rather than a sampling bug.

### 3. Quote retrieval (`get_municipality_quotes.py`)

Same Tachion API logic as the canton-level script, also checkpointed and
resumable. Kept to low concurrency (3–5 workers) throughout, regardless of
the much larger input size, out of the same politeness consideration
toward a real commercial vendor's API as the canton-level script.

```
python municipality/get_municipality_quotes.py
```

### 4–5. Finalize and replace (`finalize_municipality_quotes.py`, `replace_failed_municipality_addresses.py`)

Run in this order once the quote retrieval finishes:

```
python municipality/finalize_municipality_quotes.py
python municipality/replace_failed_municipality_addresses.py
python municipality/finalize_municipality_quotes.py    # once more, to pick up any fixes
```

`finalize` dedupes rows (a retried address can otherwise have both a
stale failed row and a later successful one) and prints a categorized
failure breakdown. `replace` then tries a fresh random address in the
same municipality for failures that are genuinely address-specific —
but **automatically skips** failures diagnosed as location-wide (see
Results below), since swapping the address can't fix those.

### Results

**20,927 / 21,098 addresses succeeded (99.19%).** All 171 failures trace
to two systematic, location-based causes — not scattered per-building
issues:

| Cause | Addresses | Municipalities fully affected |
|---|---|---|
| Missing weather-grid data on Tachion's server (`readMeteodataFile` error) | 161 | 17 |
| Steep alpine terrain modeling issue (confirmed via 20 failed replacement attempts each — Collonges VS, Simplon VS) | 10 | 2 |

The weather-data gaps cluster geographically (e.g. 7 adjacent Vaud
communes west of Lausanne fail together, and 4 adjacent Jura communes) —
consistent with entire weather-grid cells being missing, since
neighbouring municipalities share a cell. Canton Jura is disproportionately
affected (4 of its 51 municipalities, ~8%, have zero data — the worst
relative gap of any canton); every other affected canton has just 1–2
municipalities out of far larger totals.

Both are genuine, verified gaps in the underlying vendor data or model —
not bugs in this pipeline, and not fixable by further address swapping
(confirmed: replacement was attempted and failed identically for both
categories before they were classified as permanent). Spot-checked by
hand on the live SolarRechner site for one address of each type — both
reproduce the same failure there too.

## Output columns

| Column | Meaning |
|---|---|
| `canton`, `address`, `plz`, `municipality` | Sampled address |
| `buildingGroundArea_sqm` | Building footprint (QC check, not from the site) |
| `grossInvest_CHF` | Turnkey installation cost before subsidy (*Kosten schlüsselfertige Anlage*) |
| `subsidies_CHF` | One-time Pronovo/KLEIV subsidy (*Kleine Einmalvergütung*) |
| `netInvest_CHF` | Cost after subsidy |
| `paybackTime_years` | Estimated payback period (*Amortisationsdauer*) |
| `producedKwhYear_kWh` | Estimated annual solar production (*Produzierter Solarstrom*) |
| `selfConsumedKwhYear_kWh` | Annual self-consumed solar electricity (*Solarstrom selber verbraucht*) |
| `selfConsumptionPct_pct` | Self-consumption ratio (*Eigenverbrauchsanteil*) |
| `gridFeedinKwhYear_kWh` | Annual electricity fed into the grid (*Solarstrom ans Netz abgegeben*) |

`solarrechner_quotes.csv` additionally has `roofPlanesFound`/
`roofPlanesSelected` (internal QC — flags roof segments wrongly excluded
by the approximate parcel boundary, see Limitations) and `raw_json` (the
complete API response for both calls, for anything not in a named column).
