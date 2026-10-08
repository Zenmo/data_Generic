"""Download RIVM windturbine WFS layers and match to CBS buurten/gemeenten.

Source:  https://data.rivm.nl/geo/alo/wfs  (EPSG:28992, WFS 2.0.0)
         One snapshot layer per CBS year (2023 / 2024 / 2025).
         Points are matched by spatial join to CBS buurten and gemeenten
         for the corresponding CBS year.

Output:
    processed/windturbines_{jaar}_{today}.csv   — one CSV per CBS year

Run standalone:  python make_windturbines_csv.py [2023 [2024 [2025]]]
Or via:          python run_pipeline.py
"""

import logging
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

from config import (
    CBS_YEARS,
    CRS_RD,
    CRS_WGS84,
    OUTPUT_SEPARATOR,
    FILL_UNMATCHED,
    PAGE_SIZE,
    RAW_DIR,
    PROCESSED_DIR,
    DATA_GENERIC,
    REQUEST_TIMEOUT,
)
from process_features import _load_admin, _find_code_col, _match_point_or_line, _add_change_flags

log = logging.getLogger(__name__)

_RIVM_WFS = "https://data.rivm.nl/geo/alo/wfs"

# RIVM snapshot layer name for each CBS year (both ashoogte/vermogen have same fields;
# ashoogte includes ash/diam/kw so we use that)
_LAYER_PER_CBS_YEAR: dict[int, str] = {
    2023: "alo:rivm_20230101_Windturbines_2022_ashoogte",
    2024: "alo:rivm_20240101_Windturbines_ashoogte",
    2025: "alo:rivm_20250101_windturbines_ashoogte",
}

_RAW_BASE = RAW_DIR / "rivm_windturbines"
TODAY = date.today().isoformat()

# Years for which a stable-name (no date) copy is also written to data_Generic/,
# for direct use by the model — see _process_year().
_STABLE_COPY_YEARS = {2023, 2024}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download_year(cbs_year: int) -> Path | None:
    """Download RIVM windturbine layer for one CBS year; return path or None on failure."""
    import requests

    layer = _LAYER_PER_CBS_YEAR.get(cbs_year)
    if layer is None:
        log.warning("Geen RIVM-laag geconfigureerd voor CBS-jaar %d", cbs_year)
        return None

    out_dir = _RAW_BASE / str(cbs_year)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "windturbines.gpkg"

    if out_path.exists():
        log.info("  Gecached, overgeslagen: %s", out_path.relative_to(RAW_DIR))
        return out_path

    params_base = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": layer,
        "SRSNAME": CRS_RD,
        "OUTPUTFORMAT": "application/json",
        "COUNT": PAGE_SIZE,
    }

    all_features: list = []
    start_index = 0
    number_matched: int | str = "?"

    log.info("  Download %s …", layer)
    while True:
        params = {**params_base, "STARTINDEX": start_index}
        try:
            resp = requests.get(_RIVM_WFS, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("  WFS-fout [%s @ startIndex=%d]: %s", layer, start_index, exc)
            return None

        if start_index == 0:
            raw_nm = data.get("numberMatched", "?")
            try:
                number_matched = int(raw_nm)
            except (TypeError, ValueError):
                number_matched = raw_nm
            log.info("  %s: numberMatched=%s", layer, number_matched)

        features = data.get("features") or []
        if not features:
            break

        all_features.extend(features)
        start_index += len(features)

        if isinstance(number_matched, int) and len(all_features) >= number_matched:
            break

    if not all_features:
        log.warning("  Geen features voor %s", layer)
        return None

    gdf = gpd.GeoDataFrame.from_features(all_features, crs=CRS_RD)
    tmp = out_path.with_suffix(".tmp.gpkg")
    try:
        gdf.to_file(tmp, driver="GPKG")
        tmp.replace(out_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("  Schrijffout: %s", exc)
        return None

    log.info("  Opgeslagen: %d turbines → %s", len(gdf), out_path.relative_to(RAW_DIR))
    return out_path


# ---------------------------------------------------------------------------
# Process one year
# ---------------------------------------------------------------------------

def _process_year(cbs_year: int) -> bool:
    """Download (if needed), match to admin boundaries, write CSV. Returns True on success."""
    log.info("--- Windturbines CBS-jaar %d ---", cbs_year)

    gpkg = _download_year(cbs_year)
    if gpkg is None:
        return False

    gdf = gpd.read_file(gpkg)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_RD)
    else:
        gdf = gdf.to_crs(CRS_RD)

    n = len(gdf)
    log.info("  %d turbines geladen", n)
    if n == 0:
        log.warning("  Geen turbines — overgeslagen")
        return False

    # Drop geometry column early; keep attribute columns + add matched codes
    result = gdf.drop(columns=["geometry"]).copy()

    # x/y zijn al RD-coördinaten in de brondata; voeg ook WGS84 toe
    gdf_4326 = gdf.to_crs(CRS_WGS84)
    result["lat"] = gdf_4326.geometry.y.values
    result["lon"] = gdf_4326.geometry.x.values

    # Match to buurten and gemeenten for all CBS years (same pattern as process_features.py)
    for year in CBS_YEARS:
        for kind, col_prefix in [("buurten", "buurtcode"), ("gemeenten", "gemeentecode")]:
            out_col = f"{col_prefix}_{year}"
            admin_gdf = _load_admin(kind, year)
            if admin_gdf is None or admin_gdf.empty:
                result[out_col] = None
                continue
            code_col = _find_code_col(admin_gdf, kind)
            if code_col is None:
                result[out_col] = None
                continue
            codes = _match_point_or_line(gdf, admin_gdf, code_col, use_centroid=False)
            result[out_col] = codes.values
            n_unmatched = codes.isna().sum()
            if n_unmatched:
                log.warning(
                    "  %d/%d turbines niet gematcht voor %s/%d",
                    n_unmatched, n, kind, year,
                )

    result = _add_change_flags(result)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / f"windturbines_{cbs_year}_{TODAY}.csv"
    tmp = out_path.with_suffix(".tmp.csv")
    try:
        result.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(out_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("  CSV-schrijffout: %s", exc)
        return False

    log.info("  %d turbines → %s", len(result), out_path.name)

    # Stable-name copy directly in data_Generic/ for the years the model actually
    # uses (no date in the name, so the model always reads the latest run's data
    # from a fixed path). Not written for other CBS years — data_Generic/ root is
    # kept to only the files used directly by the model.
    if cbs_year in _STABLE_COPY_YEARS:
        stable_path = DATA_GENERIC / f"windturbines_{cbs_year}.csv"
        stable_tmp = stable_path.with_suffix(".tmp.csv")
        try:
            result.fillna(FILL_UNMATCHED).to_csv(stable_tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
            stable_tmp.replace(stable_path)
            log.info("  Kopie zonder datum: %s", stable_path.name)
        except Exception as exc:
            stable_tmp.unlink(missing_ok=True)
            log.warning("  Kon %s niet schrijven: %s", stable_path.name, exc)

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(jaren: list[int] | None = None) -> bool:
    jaren = jaren or [y for y in CBS_YEARS if y in _LAYER_PER_CBS_YEAR]
    all_ok = True
    t0 = time.monotonic()

    for jaar in jaren:
        try:
            ok = _process_year(jaar)
        except Exception as exc:
            log.error("Fout voor jaar %d: %s", jaar, exc, exc_info=True)
            ok = False
        if not ok:
            all_ok = False

    log.info("Windturbines klaar in %.0fs", time.monotonic() - t0)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    jaren = [int(a) for a in sys.argv[1:]] or None
    sys.exit(0 if main(jaren) else 1)
