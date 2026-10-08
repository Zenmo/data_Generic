"""Spatial matching of WarmteAtlas features to CBS buurt/gemeente/PC4/PC6 boundaries.

For each WarmteAtlas layer this module:
  1. Reads the raw GeoPackage.
  2. Determines geometry type (point / line / polygon).
  3. For each year (2023/2024/2025) matches to buurten, gemeenten, PC4, PC6.
  4. Adds geometry output columns (lat/lon for points, WKT + centroid for polygons/lines).
  5. Exports one CSV per layer (separator=;) plus an optional *_onbekend_match_*.csv
     for features that couldn't be matched to any admin unit in any year.

Run standalone:  python process_features.py
Or via:          python run_pipeline.py
"""

import json
import logging
import sys
import time
from pathlib import Path
from datetime import date
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from config import (
    RAW_DIR,
    PROCESSED_DIR,
    CBS_YEARS,
    PC_YEAR_FALLBACK,
    CRS_RD,
    CRS_WGS84,
    OUTPUT_SEPARATOR,
    FILL_UNMATCHED,
    WARMTEATLAS_LAYERS,  # used only by standalone main(); run_pipeline.py passes its own list
    CODE_COL_CANDIDATES,
)

log = logging.getLogger(__name__)
TODAY = date.today().isoformat()

# In-memory cache for admin GeoDataFrames (loaded once, reused across layers)
_admin_cache: dict[str, gpd.GeoDataFrame] = {}

# Polygon layers for which geom_wkt is omitted (PC4-based, matched inside the model on postcode)
SKIP_GEOM_WKT = {"GasPerBedrijfsOppervlakte"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_code_col(gdf: gpd.GeoDataFrame, kind: str) -> Optional[str]:
    """Return the first candidate column name that exists in gdf, or None."""
    for name in CODE_COL_CANDIDATES.get(kind, []):
        if name in gdf.columns:
            return name
    log.warning("No recognised code column for '%s' in columns: %s", kind, list(gdf.columns))
    return None


def _load_admin(kind: str, year: int) -> Optional[gpd.GeoDataFrame]:
    """Load a CBS admin GeoDataFrame from raw/, caching in memory."""
    cache_key = f"{kind}_{year}"
    if cache_key in _admin_cache:
        return _admin_cache[cache_key]

    if kind in ("buurten", "gemeenten"):
        path = RAW_DIR / "cbs_wijkenbuurten" / str(year) / f"{kind}.gpkg"
    else:
        path = RAW_DIR / f"cbs_{kind}" / str(year) / f"{kind}.gpkg"

    if not path.exists():
        log.warning("Admin file not found: %s", path)
        return None

    gdf = gpd.read_file(path)
    # Ensure CRS is set correctly regardless of what GeoPackage metadata says
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_RD)
    else:
        gdf = gdf.to_crs(CRS_RD)

    # Pre-validate once at load time so _match_polygon never hits TopologyException
    gdf.geometry = shapely.make_valid(gdf.geometry.values)

    log.info("Loaded %s/%d: %d features, CRS=%s", kind, year, len(gdf), gdf.crs)
    _admin_cache[cache_key] = gdf
    return gdf


def _detect_geom_type(gdf: gpd.GeoDataFrame) -> str:
    """Return 'point', 'line', or 'polygon' based on predominant geometry type."""
    types = set(gdf.geometry.geom_type.dropna().unique())
    if any("Point" in t for t in types):
        return "point"
    if any(("LineString" in t or "LinearRing" in t or "Curve" in t) for t in types):
        return "line"
    return "polygon"


def _match_point_or_line(
    gdf_wa: gpd.GeoDataFrame,
    gdf_admin: gpd.GeoDataFrame,
    code_col: str,
    use_centroid: bool,
) -> pd.Series:
    """
    Spatial join: find which admin polygon each WA point (or line centroid) falls within.
    Returns a Series aligned to gdf_wa.index.
    """
    wa = gdf_wa[["geometry"]].copy().to_crs(CRS_RD).reset_index(drop=True)
    wa["_seq"] = wa.index

    if use_centroid:
        wa["geometry"] = wa.geometry.centroid

    admin = gdf_admin[["geometry", code_col]].copy().to_crs(CRS_RD)

    joined = gpd.sjoin(wa, admin, how="left", predicate="within")
    # A point exactly on a shared boundary may join to multiple polygons — keep first
    joined = joined.sort_values(code_col, ascending=True, na_position="last")
    joined = joined[~joined["_seq"].duplicated(keep="first")]
    matched = joined.set_index("_seq")[code_col]

    result = pd.Series(
        [matched.get(i) for i in range(len(wa))],
        index=gdf_wa.index,
        dtype=object,
    )
    return result


def _match_polygon(
    gdf_wa: gpd.GeoDataFrame,
    gdf_admin: gpd.GeoDataFrame,
    code_col: str,
    layer_name: str,
) -> pd.Series:
    """
    Largest-overlap polygon matching in EPSG:28992.

    Algorithm:
      1. STRtree sjoin to find candidate (WA polygon, admin polygon) pairs.
      2. Vectorised shapely.intersection + shapely.area for each candidate pair.
      3. Per WA feature, pick the admin unit with the largest intersection area.
      4. Tie-break: alphabetically on code (ascending) → deterministic & loggable.

    Returns a Series aligned to gdf_wa.index.
    """
    wa = gdf_wa[["geometry"]].copy().to_crs(CRS_RD).reset_index(drop=True)
    wa["_seq"] = wa.index

    admin = gdf_admin[["geometry", code_col]].copy().to_crs(CRS_RD).reset_index(drop=True)

    # Step 1: candidate pairs via spatial index (fast)
    cands = gpd.sjoin(wa, admin, how="left", predicate="intersects")
    valid = cands.dropna(subset=["index_right"]).copy()
    # Reset index so idxmax() returns unique labels: gpd.sjoin preserves the
    # left index, so a WA polygon matching N admin polygons gets N rows with
    # the *same* integer label. If idxmax() picks that label, valid.loc[label]
    # would return multiple rows and best.get(i) would return a Series instead
    # of a scalar, causing "unhashable type: 'Series'" in _add_change_flags.
    valid = valid.reset_index(drop=True)

    if valid.empty:
        return pd.Series([None] * len(wa), index=gdf_wa.index, dtype=object)

    # Step 2: vectorised intersection areas (shapely 2.x)
    # Geometries are pre-validated at load time (_load_admin / process_layer), so no make_valid() needed here.
    wa_geoms = valid["geometry"].values
    admin_idx = valid["index_right"].astype(int).values
    admin_geoms = admin.loc[admin_idx, "geometry"].values

    inters = shapely.intersection(wa_geoms, admin_geoms)
    valid["_area"] = shapely.area(inters)

    # Step 3 + 4: sort by code for tie-break, then pick max area per WA feature
    valid = valid.sort_values(code_col, ascending=True, na_position="last")
    best_idx = valid.groupby("_seq")["_area"].idxmax()
    best = valid.loc[best_idx].set_index("_seq")[code_col]

    result = pd.Series(
        [best.get(i) for i in range(len(wa))],
        index=gdf_wa.index,
        dtype=object,
    )

    n_tie = (valid.groupby("_seq")["_area"].transform("max") == valid["_area"]).groupby(valid["_seq"]).sum()
    ties = n_tie[n_tie > 1]
    if not ties.empty:
        log.info(
            "%s: %d features had equal-area ties (tie-broken alphabetically on %s)",
            layer_name, len(ties), code_col,
        )

    return result


def _match_to_admin(
    gdf_wa: gpd.GeoDataFrame,
    gdf_admin: gpd.GeoDataFrame,
    kind: str,
    geom_type: str,
    layer_name: str,
) -> pd.Series:
    """Dispatch to point/line or polygon matching; return Series of matched codes."""
    code_col = _find_code_col(gdf_admin, kind)
    if code_col is None:
        return pd.Series([None] * len(gdf_wa), index=gdf_wa.index, dtype=object)

    if geom_type in ("point", "line"):
        codes = _match_point_or_line(
            gdf_wa, gdf_admin, code_col, use_centroid=(geom_type == "line")
        )
    else:
        codes = _match_polygon(gdf_wa, gdf_admin, code_col, layer_name)

    n_unmatched = codes.isna().sum()
    if n_unmatched:
        log.warning(
            "%s: %d/%d features unmatched for admin=%s",
            layer_name, n_unmatched, len(gdf_wa), kind,
        )
    return codes


def _add_geometry_columns(
    result: pd.DataFrame,
    gdf_wa: gpd.GeoDataFrame,
    geom_type: str,
    layer_short: str = "",
) -> pd.DataFrame:
    """
    Add geometry output columns in EPSG:4326.
    - point:   lat, lon
    - line:    centroid_lat, centroid_lon  (centroid of line in RD, then projected to 4326)
    - polygon: geom_wkt (full polygon), centroid_lat, centroid_lon
      (geom_wkt omitted for layers in SKIP_GEOM_WKT)
    """
    result = result.copy()

    if geom_type == "point":
        gdf_4326 = gdf_wa.to_crs(CRS_WGS84)
        result["lat"] = gdf_4326.geometry.y.values
        result["lon"] = gdf_4326.geometry.x.values

    else:
        # Centroid in RD (projected, metrically correct), then to 4326
        gdf_rd = gdf_wa.to_crs(CRS_RD)
        centroids_rd = gpd.GeoSeries(gdf_rd.geometry.centroid, crs=CRS_RD)
        centroids_4326 = centroids_rd.to_crs(CRS_WGS84)
        result["centroid_lat"] = centroids_4326.y.values
        result["centroid_lon"] = centroids_4326.x.values

        if geom_type == "polygon" and layer_short not in SKIP_GEOM_WKT:
            gdf_4326 = gdf_wa.to_crs(CRS_WGS84)
            result["geom_wkt"] = gdf_4326.geometry.to_wkt().values

    return result


def _add_change_flags(result: pd.DataFrame) -> pd.DataFrame:
    """Add boolean columns indicating whether buurt/gemeente code changed across years."""
    result = result.copy()

    buurt_cols = [f"buurtcode_{y}" for y in CBS_YEARS if f"buurtcode_{y}" in result.columns]
    if len(buurt_cols) > 1:
        result["buurtcode_gewijzigd"] = result[buurt_cols].nunique(axis=1) > 1

    gem_cols = [f"gemeentecode_{y}" for y in CBS_YEARS if f"gemeentecode_{y}" in result.columns]
    if len(gem_cols) > 1:
        result["gemeentecode_gewijzigd"] = result[gem_cols].nunique(axis=1) > 1

    pc4_cols = [f"pc4_code_{y}" for y in CBS_YEARS if f"pc4_code_{y}" in result.columns]
    if len(pc4_cols) > 1:
        result["pc4_gewijzigd"] = result[pc4_cols].nunique(axis=1) > 1

    return result


# ---------------------------------------------------------------------------
# Main processing function per layer
# ---------------------------------------------------------------------------

def process_layer(layer_name: str, force: bool = False) -> bool:
    """
    Match one WarmteAtlas layer to all CBS admin boundaries and export CSV.
    Returns True on success. Pass force=True to re-process even if output already exists.
    """
    short = layer_name.replace("WarmteAtlas:", "")

    # Find the most recent raw GeoPackage for this layer
    layer_dir = RAW_DIR / "warmteatlas" / short
    gpkg_files = sorted(layer_dir.glob("*.gpkg")) if layer_dir.exists() else []
    if not gpkg_files:
        log.warning("[skip] No raw data for %s", short)
        return False

    raw_path = gpkg_files[-1]

    # Skip if the CSV for this exact raw file already exists (bypass with force=True)
    raw_stem = raw_path.stem  # e.g. "2026-06-24"
    existing = list(PROCESSED_DIR.glob(f"{short}_nl_{raw_stem}.csv"))
    if existing and not force:
        log.info("[skip] %s — already processed (%s)", short, existing[0].name)
        return True

    log.info("[process] %s ← %s", short, raw_path.name)

    # Read provenance sidecar written by download_sources.py
    sidecar_path = raw_path.with_suffix(".meta.json")
    source_meta: dict = {}
    if sidecar_path.exists():
        try:
            source_meta = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Could not read sidecar %s: %s", sidecar_path.name, exc)

    gdf_wa = gpd.read_file(raw_path)
    if gdf_wa.crs is None:
        gdf_wa = gdf_wa.set_crs(CRS_RD)
    else:
        gdf_wa = gdf_wa.to_crs(CRS_RD)

    # Detect geometry type BEFORE make_valid — make_valid can coerce degenerate
    # lines/polygons to Points, which would corrupt the type detection.
    n_original = len(gdf_wa)
    if n_original == 0:
        log.warning("[skip] %s has 0 features", short)
        return False

    geom_type = _detect_geom_type(gdf_wa)
    gdf_wa.geometry = shapely.make_valid(gdf_wa.geometry.values)
    log.info("  %s: %d features, type=%s", short, n_original, geom_type)

    # Build result dataframe (attributes only, no geometry)
    result = gdf_wa.drop(columns=["geometry"]).copy()

    # --- Match to all admin layers for each year ---
    # PC4/PC6 only for point and line layers: polygon layers matched against 464k PC6
    # patches take hours and the result (one postcode for a large heat zone) is not meaningful.
    for year in CBS_YEARS:
        buurten   = _load_admin("buurten",   year)
        gemeenten = _load_admin("gemeenten", year)

        admin_matches = [
            (buurten,   "buurten",   f"buurtcode_{year}"),
            (gemeenten, "gemeenten", f"gemeentecode_{year}"),
        ]
        if geom_type != "polygon":
            pc4 = _load_admin("pc4", year)
            pc6 = _load_admin("pc6", year)
            admin_matches += [
                (pc4, "pc4", f"pc4_code_{year}"),
                (pc6, "pc6", f"pc6_code_{year}"),
            ]

        for admin_gdf, kind, out_col in admin_matches:
            if admin_gdf is None or admin_gdf.empty:
                result[out_col] = None
                continue
            codes = _match_to_admin(gdf_wa, admin_gdf, kind, geom_type, short)
            result[out_col] = codes.values

    # --- Sanity check: no features lost ---
    assert len(result) == n_original, (
        f"{short}: feature count changed during matching ({len(result)} != {n_original})"
    )

    # --- Change-detection flags ---
    result = _add_change_flags(result)

    # --- Geometry output columns ---
    result = _add_geometry_columns(result, gdf_wa, geom_type, layer_short=short)

    # --- Provenance / metadata columns (same value for every row in this layer) ---
    result["meta_source_layer"]         = source_meta.get("layer", layer_name)
    result["meta_source_wfs_url"]       = source_meta.get("wfs_url", "")
    result["meta_retrieved_at"]         = source_meta.get("retrieved_at", "")
    # Last-Modified from HTTP response header: server's own update timestamp.
    # Empty string when the server did not provide this header (common for dynamic WFS).
    result["meta_source_last_modified"] = source_meta.get("source_last_modified") or ""

    # --- Split out features with no match in any year ---
    buurt_cols = [f"buurtcode_{y}" for y in CBS_YEARS if f"buurtcode_{y}" in result.columns]
    if buurt_cols:
        unmatched_mask = result[buurt_cols].isna().all(axis=1)
    else:
        unmatched_mask = pd.Series(False, index=result.index)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    if unmatched_mask.any():
        unmatched = result[unmatched_mask]
        unk_path = PROCESSED_DIR / f"{short}_onbekend_match_{TODAY}.csv"
        _atomic_write_csv(unmatched, unk_path)
        log.warning(
            "  %d unmatched features (no buurtcode in any year) → %s",
            len(unmatched), unk_path.name,
        )
        result = result[~unmatched_mask]

    out_path = PROCESSED_DIR / f"{short}_nl_{TODAY}.csv"
    _atomic_write_csv(result, out_path)
    log.info("  Exported %d features → %s", len(result), out_path.name)
    return True


def _atomic_write_csv(df: pd.DataFrame, out_path: Path) -> None:
    """Write DataFrame to CSV via temp file to prevent partial writes."""
    tmp = out_path.with_suffix(".tmp.csv")
    try:
        df.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(out_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main(layers: list[str] | None = None, force: bool = False) -> bool:
    all_ok = True
    t0 = time.monotonic()

    for layer in (layers or WARMTEATLAS_LAYERS):
        try:
            ok = process_layer(layer, force=force)
        except Exception as exc:
            log.error("Exception in process_layer(%s): %s", layer, exc, exc_info=True)
            ok = False
        if not ok:
            all_ok = False

    elapsed = time.monotonic() - t0
    log.info("=== Processing done in %.0fs ===", elapsed)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
