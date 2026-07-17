"""Match TVW_voortgang polygons to CBS buurten via largest-area intersection.

TVW_voortgang is a gemeente-level polygon layer from WarmteAtlas containing
the status of each municipality's Transitie Visie Warmte (TVW).

This script inverts the usual process_features.py direction:
  process_features.py: per TVW polygon → find which buurt/gemeente it sits in
  make_tvw_csv.py:     per buurt      → find which TVW polygon covers it most

The output is one row per buurt per CBS year, matched to the TVW polygon with
the largest intersection area. Unmatched buurten get "n.a." for all TVW fields.

Output: processed/tvw_voortgang_buurten_{today}.csv
Columns: codering, jaar, tvw_gemeentenaam, tvw_stand_tvw, tvw_status, tvw_online_url

Run standalone:  python make_tvw_csv.py
Or via:          python run_pipeline.py --tvw
"""

import logging
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

from config import CBS_YEARS, CRS_RD, OUTPUT_SEPARATOR, FILL_UNMATCHED, RAW_DIR, PROCESSED_DIR

log = logging.getLogger(__name__)

TODAY = date.today().isoformat()

_TVW_DIR = RAW_DIR / "warmteatlas" / "TVW_voortgang"

_BUURTCODE_KANDIDATEN = ["buurtcode", "BU_CODE", "bu_code", "Buurtcode", "codering", "Codering_3"]

_TVW_COLS = {
    "gemeentenaam":  "tvw_gemeentenaam",
    "stand_tvw":     "tvw_stand_tvw",
    "tvw_status":    "tvw_status",
    "online_url":    "tvw_online_url",
}


def _load_tvw() -> gpd.GeoDataFrame:
    """Load the most recent TVW_voortgang GeoPackage."""
    gpkg_files = sorted(_TVW_DIR.glob("*.gpkg")) if _TVW_DIR.exists() else []
    if not gpkg_files:
        raise FileNotFoundError(
            f"Geen TVW_voortgang GeoPackage gevonden in {_TVW_DIR}\n"
            "Run eerst: python run_pipeline.py --download"
        )
    path = gpkg_files[-1]
    log.info("TVW laden: %s", path.name)

    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_RD)
    else:
        gdf = gdf.to_crs(CRS_RD)
    gdf.geometry = shapely.make_valid(gdf.geometry.values)

    log.info("  TVW: %d gemeenten geladen", len(gdf))
    return gdf


def _load_buurten(jaar: int) -> gpd.GeoDataFrame:
    """Load CBS buurt polygons for the given year."""
    path = RAW_DIR / "cbs_wijkenbuurten" / str(jaar) / "buurten.gpkg"
    if not path.exists():
        raise FileNotFoundError(
            f"CBS buurten niet gevonden: {path}\n"
            "Run eerst: python run_pipeline.py --download"
        )
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_RD)
    else:
        gdf = gdf.to_crs(CRS_RD)
    gdf.geometry = shapely.make_valid(gdf.geometry.values)

    # Normalise buurtcode column to "codering"
    for naam in _BUURTCODE_KANDIDATEN:
        if naam in gdf.columns:
            if naam != "codering":
                gdf = gdf.rename(columns={naam: "codering"})
            break
    else:
        raise ValueError(
            f"Geen buurtcode-kolom gevonden. Beschikbare kolommen: {list(gdf.columns)}"
        )

    log.info("  Buurten %d: %d features geladen", jaar, len(gdf))
    return gdf


def _match_buurten_to_tvw(
    buurten: gpd.GeoDataFrame,
    tvw: gpd.GeoDataFrame,
    jaar: int,
) -> pd.DataFrame:
    """
    For each buurt find the TVW polygon with the largest intersection area.
    Returns a DataFrame with columns: codering, jaar, tvw_*
    """
    bu = buurten[["codering", "geometry"]].copy().reset_index(drop=True)
    bu["_seq"] = bu.index

    tvw_attrs = tvw[list(_TVW_COLS.keys()) + ["geometry"]].copy().reset_index(drop=True)

    # Candidate pairs via spatial index
    cands = gpd.sjoin(bu, tvw_attrs, how="left", predicate="intersects")
    no_match = cands["index_right"].isna().all()

    matched = cands.dropna(subset=["index_right"]).copy().reset_index(drop=True)

    if not matched.empty:
        # Compute intersection areas
        bu_geoms    = matched["geometry"].values
        tvw_idx     = matched["index_right"].astype(int).values
        tvw_geoms   = tvw_attrs.loc[tvw_idx, "geometry"].values
        inters      = shapely.intersection(bu_geoms, tvw_geoms)
        matched["_area"] = shapely.area(inters)

        # Pick TVW polygon with largest overlap per buurt
        best_idx = matched.groupby("_seq")["_area"].idxmax()
        best = matched.loc[best_idx].set_index("_seq")

        bu = bu.join(best[list(_TVW_COLS.keys())], on="_seq", how="left")
    else:
        for col in _TVW_COLS:
            bu[col] = None

    n_matched   = bu[list(_TVW_COLS.keys())[0]].notna().sum()
    n_unmatched = len(bu) - n_matched
    log.info(
        "  Jaar %d: %d buurten gekoppeld aan TVW, %d zonder match",
        jaar, n_matched, n_unmatched,
    )

    bu = bu.drop(columns=["geometry", "_seq"])
    bu["jaar"] = jaar
    bu = bu.rename(columns=_TVW_COLS)

    cols = ["codering", "jaar"] + list(_TVW_COLS.values())
    return bu[cols]


def main() -> bool:
    t0 = time.monotonic()

    try:
        tvw = _load_tvw()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return False

    jaren = [y for y in CBS_YEARS if (RAW_DIR / "cbs_wijkenbuurten" / str(y) / "buurten.gpkg").exists()]
    if not jaren:
        log.error("Geen CBS buurten GeoPackages gevonden. Run eerst: python run_pipeline.py --download")
        return False

    frames = []
    for jaar in jaren:
        try:
            buurten = _load_buurten(jaar)
        except (FileNotFoundError, ValueError) as exc:
            log.error("Fout bij laden buurten %d: %s", jaar, exc)
            continue

        df = _match_buurten_to_tvw(buurten, tvw, jaar)
        frames.append(df)

    if not frames:
        log.error("Geen data gegenereerd.")
        return False

    result = pd.concat(frames, ignore_index=True)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / f"tvw_voortgang_buurten_{TODAY}.csv"
    tmp = out_path.with_suffix(".tmp.csv")
    try:
        result.fillna(FILL_UNMATCHED).to_csv(
            tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8"
        )
        tmp.replace(out_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("Schrijffout: %s", exc)
        return False

    elapsed = time.monotonic() - t0
    log.info(
        "TVW buurten CSV geschreven: %d rijen → %s (%.0fs)",
        len(result), out_path.name, elapsed,
    )
    return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
