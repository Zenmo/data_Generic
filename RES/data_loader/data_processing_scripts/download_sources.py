"""Download all source data: WarmteAtlas WFS, CBS Wijk/Buurt WFS, CBS PC4/PC6 WFS.

Run standalone:  python download_sources.py
Or via:          python run_pipeline.py
"""

import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import requests

from config import (
    RAW_DIR,
    WARMTEATLAS_WFS,
    CBS_WB_WFS_TPL,
    CBS_PC4_WFS_TPL,
    CBS_PC6_WFS_TPL,
    CBS_YEARS,
    PC_YEAR_FALLBACK,
    PAGE_SIZE,
    REQUEST_TIMEOUT,
    SIZE_LIMIT_BYTES,
    CACHE_MAX_AGE_DAYS,
    CRS_RD,
    WARMTEATLAS_LAYERS,
)

log = logging.getLogger(__name__)
TODAY = date.today().isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_size_bytes() -> int:
    """Return total bytes of all files currently in RAW_DIR."""
    return sum(p.stat().st_size for p in RAW_DIR.rglob("*") if p.is_file())


def _atomic_write_gpkg(gdf: gpd.GeoDataFrame, out_path: Path) -> None:
    """Write GeoDataFrame to GeoPackage via temp file to prevent partial writes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.gpkg")
    try:
        gdf.to_file(tmp, driver="GPKG")
        tmp.replace(out_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _save_meta_sidecar(
    out_path: Path,
    base_url: str,
    typenames: str,
    n_features: int,
    first_resp_headers: dict,
) -> None:
    """Save a JSON sidecar next to the GeoPackage with provenance metadata."""
    meta = {
        "layer": typenames,
        "wfs_url": base_url,
        "retrieved_at": datetime.now().isoformat(timespec="seconds"),
        "n_features": n_features,
        # HTTP Last-Modified: server's claimed last-update timestamp for this resource.
        # GeoServer often omits this; value is None when not provided.
        "source_last_modified": first_resp_headers.get("Last-Modified"),
        # HTTP Date: server clock at response time — useful as a fallback timestamp.
        "source_server_date": first_resp_headers.get("Date"),
    }
    sidecar = out_path.with_suffix(".meta.json")
    sidecar.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def _wfs_get(base_url: str, params: dict, typenames: str, label: str) -> tuple[list, dict, dict] | None:
    """Single WFS HTTP request. Returns (features, response_headers, response_body) or None on error."""
    try:
        resp = requests.get(base_url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("WFS request failed [%s %s]: %s", typenames, label, exc)
        return None
    try:
        data = resp.json()
    except ValueError as exc:
        log.error("Invalid JSON from WFS [%s %s]: %s", typenames, label, exc)
        return None
    return data.get("features") or [], dict(resp.headers), data


def _wfs_download(
    base_url: str,
    typenames: str,
    out_path: Path,
    native_crs: str = CRS_RD,
) -> bool:
    """
    Paginated WFS 2.0.0 GetFeature download.

    Pagination fix: stops only when the server returns 0 features (empty page),
    NOT when it returns fewer than PAGE_SIZE. Some WFS servers (e.g. PDOK) cap
    their own page size at 1000 regardless of the COUNT parameter, so checking
    `len(features) < PAGE_SIZE` would incorrectly stop after the first page.

    Saves a .meta.json sidecar with provenance info alongside each .gpkg.

    Returns True on success, False on recoverable failure.
    Does NOT overwrite an existing file when the download fails or returns 0 features.
    """
    if out_path.exists():
        log.info("Already cached, skipping: %s", out_path.relative_to(RAW_DIR))
        return True

    params_base = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typenames,
        "SRSNAME": native_crs,
        "OUTPUTFORMAT": "application/json",
        "COUNT": PAGE_SIZE,
    }

    all_features: list = []
    start_index = 0
    number_matched: int | str = "?"
    first_resp_headers: dict = {}

    while True:
        params = {**params_base, "STARTINDEX": start_index}
        result = _wfs_get(base_url, params, typenames, f"startIndex={start_index}")
        if result is None:
            return False
        features, headers, data = result

        if start_index == 0:
            first_resp_headers = headers
            raw_nm = data.get("numberMatched", "?")
            # numberMatched can be an int or the string "unknown"
            try:
                number_matched = int(raw_nm)
            except (TypeError, ValueError):
                number_matched = raw_nm
            log.info("  %s: numberMatched=%s", typenames, number_matched)

        # Stop only on an empty page — NOT on len(features) < PAGE_SIZE.
        # PDOK WFS servers cap page size at 1000 regardless of COUNT,
        # so a partial page does not mean we've reached the end.
        if not features:
            break

        all_features.extend(features)
        start_index += len(features)
        log.debug("  %s: downloaded %d / %s", typenames, len(all_features), number_matched)

        # Secondary stop: server reported total and we have it all
        if isinstance(number_matched, int) and len(all_features) >= number_matched:
            break

    if not all_features:
        log.warning("0 features returned for %s — not writing (keeping existing if any)", typenames)
        return False

    try:
        gdf = gpd.GeoDataFrame.from_features(all_features, crs=native_crs)
    except Exception as exc:
        log.error("Could not build GeoDataFrame for %s: %s", typenames, exc)
        return False

    if gdf.empty:
        log.warning("Empty GeoDataFrame for %s — not writing", typenames)
        return False

    _atomic_write_gpkg(gdf, out_path)
    size_mb = out_path.stat().st_size / 1024**2
    log.info("  Saved %d features → %s (%.1f MB)", len(gdf), out_path.relative_to(RAW_DIR), size_mb)

    _save_meta_sidecar(out_path, base_url, typenames, len(gdf), first_resp_headers)
    return True


# Netherlands bounding box in EPSG:28992 (RD New) with a small margin
_NL_BBOX_RD = (0, 289000, 305000, 630000)

# Safe threshold: if a tile returns this many features we recursively split it.
# PAGE_SIZE is the server cap; subtract a buffer to catch near-cap responses.
_TILE_SPLIT_THRESHOLD = 900


def _collect_tile(
    base_url: str,
    params_base: dict,
    typenames: str,
    native_crs: str,
    bbox: tuple[float, float, float, float],
    all_features: list,
    seen_ids: set,
    first_resp_headers: list,
    depth: int = 0,
) -> bool:
    """
    Recursively download one spatial tile.

    If the server returns ≥ _TILE_SPLIT_THRESHOLD features the tile is split
    into 4 quadrants and each is downloaded separately. This avoids guessing
    the right grid size up front and handles any layer size automatically.
    """
    tx0, ty0, tx1, ty1 = bbox
    bbox_str = f"{tx0:.0f},{ty0:.0f},{tx1:.0f},{ty1:.0f},{native_crs}"
    params = {**params_base, "BBOX": bbox_str}

    result = _wfs_get(base_url, params, typenames, f"bbox={bbox_str[:30]}…")
    if result is None:
        return False
    features, headers, _ = result

    if not first_resp_headers:
        first_resp_headers.append(headers)

    if len(features) >= _TILE_SPLIT_THRESHOLD:
        # Tile is at or near the server cap — split into 4 quadrants
        mx, my = (tx0 + tx1) / 2, (ty0 + ty1) / 2
        quadrants = [
            (tx0, ty0, mx, my), (mx, ty0, tx1, my),
            (tx0, my, mx, ty1), (mx, my, tx1, ty1),
        ]
        log.debug(
            "  %s: tile depth=%d returned %d features — splitting into 4 quadrants",
            typenames, depth, len(features),
        )
        for q in quadrants:
            if not _collect_tile(base_url, params_base, typenames, native_crs,
                                  q, all_features, seen_ids, first_resp_headers, depth + 1):
                return False
        return True

    new = 0
    for feat in features:
        fid = feat.get("id") or feat.get("properties", {}).get("fid")
        if fid is None or fid not in seen_ids:
            all_features.append(feat)
            if fid is not None:
                seen_ids.add(fid)
            new += 1

    log.debug("  %s: tile depth=%d: %d features (%d new)", typenames, depth, len(features), new)
    return True


def _wfs_download_tiled(
    base_url: str,
    typenames: str,
    out_path: Path,
    native_crs: str = CRS_RD,
    initial_cols: int = 5,
    initial_rows: int = 7,
) -> bool:
    """
    Adaptive BBOX-tiled WFS 2.0.0 download for layers where STARTINDEX pagination
    is broken (PDOK CBS WFS returns max 1000 features and ignores STARTINDEX).

    Starts with an initial grid of initial_cols × initial_rows tiles. Any tile
    that returns ≥ _TILE_SPLIT_THRESHOLD features is automatically subdivided
    into 4 quadrants and retried, recursively, until all tiles are below the cap.
    Features are deduplicated by GeoJSON feature id to avoid counting boundary
    features twice.

    Returns True on success, False on recoverable failure.
    """
    if out_path.exists():
        log.info("Already cached, skipping: %s", out_path.relative_to(RAW_DIR))
        return True

    minx, miny, maxx, maxy = _NL_BBOX_RD
    x_step = (maxx - minx) / initial_cols
    y_step = (maxy - miny) / initial_rows
    initial_tiles = [
        (minx + c * x_step, miny + r * y_step,
         minx + (c + 1) * x_step, miny + (r + 1) * y_step)
        for r in range(initial_rows) for c in range(initial_cols)
    ]

    params_base = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": typenames,
        "SRSNAME": native_crs,
        "OUTPUTFORMAT": "application/json",
        "COUNT": PAGE_SIZE,
    }

    all_features: list = []
    seen_ids: set = set()
    first_resp_headers: list = []  # mutable container so _collect_tile can set it

    log.info(
        "  %s: adaptive tiled download (initial %dx%d grid, split threshold=%d)",
        typenames, initial_cols, initial_rows, _TILE_SPLIT_THRESHOLD,
    )

    for bbox in initial_tiles:
        if not _collect_tile(base_url, params_base, typenames, native_crs,
                              bbox, all_features, seen_ids, first_resp_headers):
            return False

    log.info("  %s: collected %d features", typenames, len(all_features))

    if not all_features:
        log.warning("0 features returned for %s — not writing", typenames)
        return False

    try:
        gdf = gpd.GeoDataFrame.from_features(all_features, crs=native_crs)
    except Exception as exc:
        log.error("Could not build GeoDataFrame for %s: %s", typenames, exc)
        return False

    if gdf.empty:
        log.warning("Empty GeoDataFrame for %s — not writing", typenames)
        return False

    _atomic_write_gpkg(gdf, out_path)
    size_mb = out_path.stat().st_size / 1024**2
    log.info("  Saved %d features → %s (%.1f MB)", len(gdf), out_path.relative_to(RAW_DIR), size_mb)

    headers = first_resp_headers[0] if first_resp_headers else {}
    _save_meta_sidecar(out_path, base_url, typenames, len(gdf), headers)
    return True


# ---------------------------------------------------------------------------
# Per-source download functions
# ---------------------------------------------------------------------------

def _find_cached_gpkg(layer_dir: Path) -> Path | None:
    """
    Return the most recent .gpkg in layer_dir if it exists and is younger than
    CACHE_MAX_AGE_DAYS, otherwise None.
    """
    gpkg_files = sorted(layer_dir.glob("*.gpkg"))
    if not gpkg_files:
        return None
    newest = gpkg_files[-1]
    age = datetime.now() - datetime.fromtimestamp(newest.stat().st_mtime)
    if age <= timedelta(days=CACHE_MAX_AGE_DAYS):
        return newest
    return None


def _prune_old_gpkg(layer_dir: Path, keep: int = 2) -> None:
    """Delete the oldest .gpkg files in layer_dir, keeping only the `keep` most recent."""
    gpkg_files = sorted(layer_dir.glob("*.gpkg"))
    to_delete = gpkg_files[:-keep] if len(gpkg_files) > keep else []
    for old in to_delete:
        try:
            old.unlink()
            # Also remove the sidecar if present
            old.with_suffix(".meta.json").unlink(missing_ok=True)
            log.info("  Deleted old cache file: %s", old.name)
        except OSError as exc:
            log.warning("  Could not delete %s: %s", old.name, exc)


def download_warmteatlas_layer(layer_name: str) -> bool:
    """Download one WarmteAtlas layer to raw/warmteatlas/<LayerShortName>/<today>.gpkg.

    Skips the download if any .gpkg younger than CACHE_MAX_AGE_DAYS already exists in the
    layer directory (reuses yesterday's download rather than re-fetching every run).
    After a fresh download, prunes old files keeping only the 2 most recent.
    """
    short = layer_name.replace("WarmteAtlas:", "")
    layer_dir = RAW_DIR / "warmteatlas" / short

    cached = _find_cached_gpkg(layer_dir)
    if cached is not None:
        log.info("[WarmteAtlas] %s — gecached (%s), overgeslagen", short, cached.name)
        return True

    total_mb = _raw_size_bytes() / 1024**2
    if total_mb > SIZE_LIMIT_BYTES / 1024**2:
        log.warning(
            "Total raw size %.1f MB exceeds %.0f MB budget (continuing — adjust SIZE_LIMIT_BYTES in config.py to suppress)",
            total_mb, SIZE_LIMIT_BYTES / 1024**2,
        )

    out_path = layer_dir / f"{TODAY}.gpkg"
    log.info("[WarmteAtlas] %s", short)
    ok = _wfs_download(WARMTEATLAS_WFS, layer_name, out_path)
    if ok:
        _prune_old_gpkg(layer_dir, keep=2)
    return ok


def download_cbs_wijkenbuurten(year: int, collection: str) -> bool:
    """Download CBS Wijk- en Buurtkaart (buurten or gemeenten) for one year.

    Buurten (~17,000 features) uses BBOX-tiled download because the PDOK CBS WFS
    returns a maximum of 1000 features and does not support STARTINDEX pagination
    for this layer. Gemeenten (≤424 features) uses standard paginated download.
    """
    out_path = RAW_DIR / "cbs_wijkenbuurten" / str(year) / f"{collection}.gpkg"
    base_url = CBS_WB_WFS_TPL.format(year=year)
    typenames = f"wijkenbuurten:{collection}"
    log.info("[CBS wb] %d / %s", year, collection)
    if collection == "buurten":
        return _wfs_download_tiled(base_url, typenames, out_path)
    return _wfs_download(base_url, typenames, out_path)


def download_cbs_postcode(dataset: str, year: int) -> bool:
    """
    Download CBS PC4 or PC6 for one year.
    If the requested year is not yet published, falls back to PC_YEAR_FALLBACK
    and saves the file under the *requested* year path so process_features.py
    can load it with a consistent path pattern.
    """
    actual_year = PC_YEAR_FALLBACK.get(year, year)
    if actual_year != year:
        log.warning(
            "[CBS %s] %d not yet published → using %d data (saved under year %d path)",
            dataset, year, actual_year, year,
        )

    out_path = RAW_DIR / f"cbs_{dataset}" / str(year) / f"{dataset}.gpkg"
    if dataset == "pc4":
        base_url = CBS_PC4_WFS_TPL.format(year=actual_year)
        typenames = "postcode4:postcode4"
    else:
        base_url = CBS_PC6_WFS_TPL.format(year=actual_year)
        typenames = "postcode6:postcode6"

    log.info("[CBS %s] %d (actual source year: %d)", dataset, year, actual_year)
    # PC4 (~4,100 features) and PC6 (many thousands) both hit the PDOK 1000-feature
    # cap and ignore STARTINDEX, so use the same adaptive tiled download as buurten.
    return _wfs_download_tiled(base_url, typenames, out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> bool:
    """Download all sources in order. Returns True only if every download succeeded."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    all_ok = True
    t0 = time.monotonic()

    # 1 — WarmteAtlas (84 layers)
    log.info("=== Step 1/3: WarmteAtlas layers (%d layers) ===", len(WARMTEATLAS_LAYERS))
    for layer in WARMTEATLAS_LAYERS:
        ok = download_warmteatlas_layer(layer)
        if not ok:
            log.warning("FAILED / skipped: %s", layer)
            all_ok = False

    # 2 — CBS Wijk- en Buurtkaart
    log.info("=== Step 2/3: CBS Wijk- en Buurtkaart (years %s) ===", CBS_YEARS)
    for year in CBS_YEARS:
        for collection in ("buurten", "gemeenten"):
            ok = download_cbs_wijkenbuurten(year, collection)
            if not ok:
                log.error("CRITICAL FAILURE: CBS wijkenbuurten/%d/%s", year, collection)
                all_ok = False

    # 3 — CBS PC4 and PC6
    log.info("=== Step 3/3: CBS PC4 / PC6 (years %s) ===", CBS_YEARS)
    for year in CBS_YEARS:
        for dataset in ("pc4", "pc6"):
            ok = download_cbs_postcode(dataset, year)
            if not ok:
                log.warning("FAILED: CBS %s/%d", dataset, year)
                all_ok = False

    elapsed = time.monotonic() - t0
    total_mb = _raw_size_bytes() / 1024**2
    log.info(
        "=== Downloads done in %.0fs. Total raw data: %.1f MB ===",
        elapsed, total_mb,
    )
    if total_mb > SIZE_LIMIT_BYTES / 1024**2:
        log.warning("Total raw data (%.1f MB) exceeds 1 GB budget.", total_mb)

    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
