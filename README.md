# Swiss Residential Solar PV Cost — by Canton and by Municipality

Estimates typical residential solar PV installation costs across Switzerland,
using randomly sampled single-family homes and Switzerland's public
[SolarRechner](https://www.energieschweiz.ch/tools/solarrechner/) tool
(EnergieSchweiz). Two datasets:

- **Canton-level** (`canton/solarrechner_quotes.csv`) — 3 addresses per canton, 26 cantons (78).
- **Municipality-level** (`municipality/municipality_quotes.csv`) — up to 10 addresses per
  municipality, all 2,110 Swiss municipalities (21,077).

## Approach

- **Addresses are randomly sampled, not hand-picked**, from Switzerland's official
  building registry (GWR), filtered to existing single-family houses (`GKLAS 1110`,
  `GSTAT 1004`) with a plain numeric house number (5, not 5a / 30.1 / 714a.99).
- **Quotes come from SolarRechner's own backend API** (Tachion), with the same
  requests the website sends.
- **Fixed scenario for every quote:** 4 residents ("Bewohner im Haus"), no facade
  planes, no roof planes tilted 75° or more (wall-like surfaces; 75° is the threshold of
  the federal tilt bonus in the EnFV, "Neigungswinkel von mindestens 75 Grad").
- **Checked against the website** on 5 addresses with this scenario: gross investment,
  KLEIV subsidy, tax savings and net investment are identical; payback period, total
  balance and average return were compared on 3 addresses and are identical with the
  same number of residents (these three depend on the residents, the costs do not).

## Repo structure

```
README.md
canton/
  get_address.py               Step 1: sample 3 random single-family addresses per canton
  get_solarrechner_quotes.py   Step 2: quote every address; addresses that keep failing are
                                replaced by a fresh random address in the same canton
  summary_csv.py               Step 3: clean summary CSV for sharing
  random_addresses.csv         sampled addresses
  solarrechner_quotes.csv      full results, incl. raw_json
  solarrechner_quotes_summary.csv  clean version -- no raw_json, no QC columns

municipality/
  build_municipality_list.py   Step 1: list of all 2,110 municipalities (BFS register)
  get_municipality_addresses.py  Step 2: sample 10 single-family addresses per municipality
                                from the GWR bulk download (seeded, reproducible)
  get_municipality_quotes.py   Step 3: quote every address (resumable)
  replace_failed_municipality_addresses.py  Step 4: replace failed addresses with the next
                                candidates of the same municipality
  finalize_municipality_quotes.py  Step 5: one row per address, failure breakdown, summary CSV
  repair_quotes_csv.py         utility: drop lines cut off by an interrupted run (--fix)
  all_municipalities.csv       list of all 2,110 municipalities
  municipality_addresses.csv   sampled addresses (after replacement)
  municipality_quotes_summary.csv  clean results -- no raw_json, no QC columns
```

Not in the repo (gitignored, large or local): `municipality/municipality_quotes.csv`
(full results with raw_json, ~460 MB), `municipality/municipality_quote_attempts.csv`
(quote rows of replaced addresses), `municipality/gwr_cache/` (GWR downloads, ~0.9 GB).

Every script resolves its file paths relative to its own location, so
`python canton/get_address.py` (from the repo root) and
`cd canton && python get_address.py` behave identically.

## How the quotes are requested

For each address (same in both quote scripts):

1. Geocode the address (`api3.geo.admin.ch` SearchServer) to get the feature id.
2. `/sim/attrs`: Tachion fills in parcel, coordinates, electricity provider and tariff.
3. `/sim/planes`: roof and facade planes of the building.
4. `/sim/buildingReport` with `state: SOLAR_OPT` on the planes exactly as returned by
   `/planes` — the website's first result.
5. `/sim/buildingReport` without state, on the planes from step 4 with all facade planes
   and all roof planes tilted ≥ 75° switched off, and `occupantNumber = 4` in
   `signature.input` — what a user does on the website.

Steps 4–5 are the website's own sequence (read from its frontend code and confirmed
with its numbers). A single `SOLAR_OPT` call with the planes already switched off gave
higher costs in 35 of 60 tested buildings.

## Canton-level dataset

### 1. Address sampling (`canton/get_address.py`)

Random points inside each canton's approximate bounding box are checked against the
GWR (`api3.geo.admin.ch` identify) until 3 single-family homes with a plain house
number are found.

```
python canton/get_address.py
```

### 2. Quotes (`canton/get_solarrechner_quotes.py`)

```
python canton/get_solarrechner_quotes.py            # full run
python canton/get_solarrechner_quotes.py --retry    # re-attempt only failed rows
```

Failures are retried; an address that still fails is replaced by a fresh random
address in the same canton (same address rules), and `random_addresses.csv` is
updated accordingly.

### 3. Summary (`canton/summary_csv.py`)

```
python canton/summary_csv.py
```

### Result (2026-10-18)

78 of 78 addresses have quotes (3 per canton). 4 addresses were replaced after
server errors.

## Municipality-level dataset

### 1. Municipality list (`build_municipality_list.py`)

Official list of all municipalities from BFS's
[Historicized Directory of Swiss Municipalities](https://www.agvchapp.bfs.admin.ch/).
Only name and BFS number are taken from it; the register's `Parent` field gave wrong
cantons (e.g. Horgen ZH resolved to VS), so the canton comes from the GWR data.

```
python municipality/build_municipality_list.py
```

### 2. Address sampling (`get_municipality_addresses.py`)

Samples from the GWR public bulk download (`https://public.madd.bfs.admin.ch/<canton>.zip`,
cached in `municipality/gwr_cache/`): all existing single-family homes with a street
address and a plain numeric house number. Per municipality, 10 are drawn with a fixed
seed (`--seed`, default 42), at most 2 per street, so every eligible home has the same
chance of being picked. Only municipalities that stay below 10 are filled up: first
without the street limit, then with letter-suffixed numbers (5a), at most one building
per base address. About 1–2 minutes (the first run downloads ~1 GB). The sample was
drawn from the GWR export of 2026-10-15 (kept in `gwr_cache/`); a new download gets the
current export, which can give a different sample.

```
python municipality/get_municipality_addresses.py
```

Result (seed 42, GWR of 2026-10-15): 21,077 addresses; 2,106 of 2,110 municipalities
at 10 (12 via the fill-up steps). Below 10: Berken BE 8, Mauraz VD 8, Lavizzara TI 1,
Cerentino TI 0.

### 3–5. Quotes, replacement, finalize

```
python municipality/get_municipality_quotes.py                  # resumable; run 1-2 more times
python municipality/replace_failed_municipality_addresses.py
python municipality/finalize_municipality_quotes.py
```

- `get_municipality_quotes.py`: 20 workers by default (measured ~8,300 addresses/hour;
  40 workers was barely faster and caused more server errors). A re-run only retries
  addresses without a successful quote.
- `replace_failed_municipality_addresses.py`: replaces each failed address with the
  next candidate of the same municipality in the seeded sampling order (same rules), at
  most 5 candidates per failed address over all runs. Candidates in a postal code that
  had a missing-weather-data error are skipped. Addresses already tried are never tried
  again.
- `finalize_municipality_quotes.py`: keeps one row per address in
  `municipality_addresses.csv`, prints the failure breakdown, writes the summary CSV,
  and moves dropped rows to `municipality_quote_attempts.csv`.

### Result (2026-10-18)

**20,932 of 21,077 addresses have quotes (99.3%)**; 2,096 of 2,110 municipalities have
at least one quote, 2,091 have 10. The 145 failures are errors on Tachion's server:

| Failure | Addresses | What was checked |
|---|---|---|
| Missing weather data (`readMeteodataFile`) | 133 | The missing file is `CH<PLZ>.met` for the address's postal code (20 postal codes); the server picks it from `attrs.plz4`. No address in these postal codes got a quote. 12 municipalities have only such a postal code (e.g. Yens, Brüttelen, Montfaucon); Les Enfers and Saint-Brais have no other homes left. |
| `buildingReport` server error (`null` / NullPointerException) | 11 | All have no wall data in `/planes`; every such building tested failed (77 of 77). In Collonges VS no tested building has wall data (60 tried); the website gives no quote there either (1 address checked). |
| `BUILDING_TOO_BIG` (`/planes`) | 1 | Not related to building size (median footprint 107 m² vs 108 m² for successful ones). In 37 of 38 tested cases no roof plane lies inside the parcel returned by `/attrs`. |

After the first quote run 802 addresses had failed; 657 of them were replaced by other
addresses of the same municipality that got a quote.

## Output columns

| Column | Meaning |
|---|---|
| `canton`, `municipality`, `bfs_code`, `address`, `plz`, `egid` | Sampled address (GWR) |
| `buildingGroundArea_sqm` | Building footprint from Tachion (QC) |
| `grossInvest_CHF` | Investment incl. VAT for the PV system, without battery (*Investitionskosten Solar*) |
| `subsidies_CHF` | One-time federal subsidy (*Kleine Einmalvergütung KLEIV*) |
| `netInvest_CHF` | Gross investment minus KLEIV minus tax savings (*Nettoinvestition*) |
| `paybackTime_years` | Payback period (*Amortisationsdauer*), depends on the residents through self-consumption |
| `producedKwhYear_kWh` | Annual solar production (*Produzierter Solarstrom*) |
| `selfConsumedKwhYear_kWh` | Self-consumed solar electricity per year |
| `selfConsumptionPct_pct` | Self-consumption ratio (*Eigenverbrauchsanteil*) |
| `gridFeedinKwhYear_kWh` | Electricity fed into the grid per year |
| `error` | Server or request error, as returned (empty if the quote succeeded) |

The full CSVs additionally have `roofPlanesFound` / `roofPlanesSelected` (roof planes
after the tilt rule) and `raw_json` (attrs, parameters, cost analysis and energy results
of the final request, plus `steep_roof_planes_off`: ids of the planes switched off by the
75° rule).
