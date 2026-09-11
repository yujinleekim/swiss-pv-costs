"""
Definitive check: how many single-family homes actually exist in Mauraz?

Instead of random sampling (which could theoretically miss some by bad
luck even at 600 attempts), this scans a dense, evenly-spaced GRID across
Mauraz's entire bounding box -- covering the area far more thoroughly --
and counts every unique confirmed single-family home found. This is
about as close to an exhaustive count as we can get without a bulk GWR
export.
"""

import requests
import time

FIND_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/find"
GEMEINDE_LAYER = "ch.swisstopo.swissboundaries3d-gemeinde-flaeche.fill"
GWR_IDENTIFY = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GWR_LAYER = "ch.bfs.gebaeude_wohnungs_register"
GKLAS_EINFAMILIENHAUS = "1110"
GSTAT_EXISTING = "1004"

MUNI_NAME = "Mauraz"
CANTON = "VD"
GRID_SPACING = 15  # metres between grid points -- dense enough to catch every building


def fetch_bbox(name):
    r = requests.get(FIND_URL, params={
        "layer": GEMEINDE_LAYER, "searchField": "gemname", "searchText": name,
        "contains": "false", "returnGeometry": "true", "geometryFormat": "geojson", "sr": "2056",
    }, timeout=15)
    r.raise_for_status()
    all_results = r.json().get("results", [])
    current = [res for res in all_results if res.get("attributes", {}).get("is_current_jahr")]
    chosen = current[0] if current else (
        max(all_results, key=lambda res: res.get("attributes", {}).get("jahr") or 0)
        if all_results else None
    )
    if not chosen:
        raise ValueError(f"No boundary found for {name}")
    return chosen["bbox"]


def identify_building(easting, northing, tolerance=20):
    params = {
        "geometryType": "esriGeometryPoint", "geometry": f"{easting},{northing}",
        "imageDisplay": "500,600,96",
        "mapExtent": f"{easting-500},{northing-500},{easting+500},{northing+500}",
        "tolerance": tolerance, "layers": f"all:{GWR_LAYER}", "returnGeometry": "false", "sr": "2056",
    }
    r = requests.get(GWR_IDENTIFY, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("results", [])


def main():
    min_e, min_n, max_e, max_n = fetch_bbox(MUNI_NAME)
    print(f"{MUNI_NAME} bbox: {min_e}, {min_n}, {max_e}, {max_n}")
    width = max_e - min_e
    height = max_n - min_n
    n_cols = int(width // GRID_SPACING) + 1
    n_rows = int(height // GRID_SPACING) + 1
    total_points = n_cols * n_rows
    print(f"Grid: {n_cols} x {n_rows} = {total_points} points to check\n")

    found_efh = {}   # egid -> address
    found_other = {} # egid -> (address, gklas) for any OTHER building type, for context
    checked = 0

    for row in range(n_rows):
        for col in range(n_cols):
            e = min_e + col * GRID_SPACING
            n = min_n + row * GRID_SPACING
            try:
                results = identify_building(e, n)
            except requests.RequestException:
                time.sleep(1)
                continue
            checked += 1
            for res in results:
                attrs = res.get("attributes", {})
                egid = str(attrs.get("egid"))
                if attrs.get("ggdename") != MUNI_NAME or str(attrs.get("gdekt")) != CANTON:
                    continue
                if str(attrs.get("gklas")) == GKLAS_EINFAMILIENHAUS and str(attrs.get("gstat")) == GSTAT_EXISTING:
                    found_efh[egid] = attrs.get("strname_deinr", "")
                else:
                    found_other[egid] = (attrs.get("strname_deinr", ""), attrs.get("gklas"))
            if checked % 20 == 0:
                print(f"  ...{checked}/{total_points} grid points checked, "
                      f"{len(found_efh)} single-family homes found so far")
            time.sleep(0.05)

    print(f"\n=== FINAL COUNT ===")
    print(f"Confirmed single-family homes (GKLAS 1110, existing) in {MUNI_NAME}: {len(found_efh)}")
    for egid, addr in found_efh.items():
        print(f"  {addr} (egid {egid})")
    print(f"\nOther buildings found nearby (context, not single-family): {len(found_other)}")


if __name__ == "__main__":
    main()