"""Download WarmteTransitie gebieden from RVO ArcGIS and match to CBS buurten.

Source: WARMTETRANSITIE_publiek FeatureServer (RVO / Warmteatlas backend)
URL:    https://services.arcgis.com/kE0BiyvJHb5SwQv7/arcgis/rest/services/
        WARMTETRANSITIE_publiek/FeatureServer/0
License: Public / open data (RVO).

WarmteTransitie features are irregular polygons — they do not align one-to-one
with CBS buurten. Each buurt is matched to whichever WarmteTransitie polygon
overlaps it the most (largest intersection area). Buurten with no overlap get
n.a. for all wtp_ columns.

As of mid-2026 only ~211 gebieden have published plans, so most buurten will
be unmatched. The dataset grows as more municipalities publish.

Output: processed/warmtetransitie_buurten_{today}.csv
Columns prefixed with wtp_ to avoid clashes with CBS columns.

Run standalone:  python make_warmtetransitie_csv.py
Or via:          python run_pipeline.py --warmtetransitie
"""

import io
import logging
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely

from config import CBS_YEARS, CRS_RD, PROCESSED_DIR, RAW_DIR, OUTPUT_SEPARATOR, FILL_UNMATCHED

log = logging.getLogger(__name__)

TODAY = date.today().isoformat()

_URL = (
    "https://services.arcgis.com/kE0BiyvJHb5SwQv7/arcgis/rest/services"
    "/WARMTETRANSITIE_publiek/FeatureServer/0/query"
)

# Only these attribute columns are kept — the fields that actually carry a
# heat-strategy signal for our region (Dordrecht is the only Drechtsteden
# gemeente with a published plan as of 2026-06; its 159 matched buurten have
# these 6 populated, everything else — identity/metadata fields like
# doc_titel/publ_datum/startjr, and near-empty count/isolation fields like
# n_wng_*/n_util_*/wng_isolatiewaarde — is either constant across the plan or
# blank, so it isn't useful at buurt granularity for scenario decisions).
_KEEP = {
    "actiedoelen",           # doelstelling: Aardgasvrij / CO2-neutraal / Isoleren / ...
    "energiedragers",        # DE=Duurzame Elektriciteit, KNG=Klimaatneutraal Gas, DW=Duurzame Warmte
    "energiebronnen_z_omz",  # primaire warmtebron zonder omzetting: Restwarmte, Omgeving, ...
    "wng_check",             # Ja/Nee — geldt een warmtenet-oplossing voor woningen?
    "wng_type_wnet",         # mini (2-50 woningen) / klein (51-1500) / groot (>1500)
    "util_check",            # Ja/Nee — geldt een warmtenet-oplossing voor utiliteitsgebouwen?
}

_BUURTCODE_KANDIDATEN = ["buurtcode", "BU_CODE", "bu_code", "Buurtcode", "codering", "Codering_3"]

_TIMEOUT = 60
_RETRY_MAX = 3
_RETRY_DELAY = 5


def _download_geojson() -> gpd.GeoDataFrame:
    """Download all WarmteTransitie features with geometry as GeoDataFrame."""
    params = {
        "where":          "1=1",
        "outFields":      "*",
        "returnGeometry": "true",
        "f":              "geojson",
    }

    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = requests.get(_URL, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            gdf = gpd.read_file(io.BytesIO(resp.content))
            log.info("  Downloaded %d WarmteTransitie gebieden", len(gdf))
            return gdf
        except Exception as exc:
            if attempt < _RETRY_MAX:
                log.warning("  Poging %d/%d mislukt: %s", attempt, _RETRY_MAX, exc)
                time.sleep(_RETRY_DELAY)
            else:
                log.error("  Download mislukt na %d pogingen: %s", _RETRY_MAX, exc)
                raise


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


def _match_buurten_to_wt(
    buurten: gpd.GeoDataFrame,
    wt: gpd.GeoDataFrame,
    attr_cols: list[str],
    jaar: int,
) -> pd.DataFrame:
    """
    For each buurt find the WarmteTransitie polygon with the largest intersection area.
    Returns a DataFrame with columns: codering, jaar, wtp_*
    """
    bu = buurten[["codering", "geometry"]].copy().reset_index(drop=True)
    bu["_seq"] = bu.index

    wt_sub = wt[["geometry"] + attr_cols].copy().reset_index(drop=True)

    # Candidate pairs via spatial index
    cands = gpd.sjoin(bu, wt_sub, how="left", predicate="intersects")
    matched = cands.dropna(subset=["index_right"]).copy().reset_index(drop=True)

    if not matched.empty:
        bu_geoms   = matched["geometry"].values
        wt_idx     = matched["index_right"].astype(int).values
        wt_geoms   = wt_sub.loc[wt_idx, "geometry"].values
        inters     = shapely.intersection(bu_geoms, wt_geoms)
        matched["_area"] = shapely.area(inters)

        best_idx = matched.groupby("_seq")["_area"].idxmax()
        best = matched.loc[best_idx].set_index("_seq")[attr_cols]
        bu = bu.join(best, on="_seq", how="left")
    else:
        for col in attr_cols:
            bu[col] = None

    n_matched   = bu[attr_cols[0]].notna().sum()
    n_unmatched = len(bu) - n_matched
    log.info(
        "  Jaar %d: %d buurten gekoppeld aan WarmteTransitie, %d zonder match",
        jaar, n_matched, n_unmatched,
    )

    bu = bu.drop(columns=["geometry", "_seq"])
    bu["jaar"] = jaar
    cols = ["codering", "jaar"] + attr_cols
    return bu[cols]


def main() -> bool:
    t0 = time.monotonic()
    log.info("WarmteTransitie downloaden van RVO ArcGIS FeatureServer ...")

    try:
        wt = _download_geojson()
    except Exception as exc:
        log.error("Download mislukt: %s", exc)
        return False

    if wt.empty:
        log.error("Geen features ontvangen.")
        return False

    # Reproject to RD New for metric intersection areas
    if wt.crs is None:
        wt = wt.set_crs("EPSG:4326")
    wt = wt.to_crs(CRS_RD)
    wt.geometry = shapely.make_valid(wt.geometry.values)

    # Determine attribute columns (only the approved heat-strategy-signal fields)
    attr_cols_raw = [c for c in wt.columns if c in _KEEP]
    missing = _KEEP - set(attr_cols_raw)
    if missing:
        log.warning("  Verwachte kolommen niet gevonden in RVO-service: %s", sorted(missing))

    # Rename to wtp_ prefix for the output CSV
    rename = {c: f"wtp_{c}" for c in attr_cols_raw}
    wt = wt.rename(columns=rename)
    attr_cols = [f"wtp_{c}" for c in attr_cols_raw]

    jaren = [
        y for y in CBS_YEARS
        if (RAW_DIR / "cbs_wijkenbuurten" / str(y) / "buurten.gpkg").exists()
    ]
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

        df = _match_buurten_to_wt(buurten, wt, attr_cols, jaar)
        frames.append(df)

    if not frames:
        log.error("Geen data gegenereerd.")
        return False

    result = pd.concat(frames, ignore_index=True)

    # Strip embedded newlines from text fields — some RVO field values (e.g.
    # wtp_gbd_naam) contain \n, which breaks CSV parsers that don't handle
    # RFC-4180 quoted multi-line fields (awk, Excel import, AnyLogic CSV reader).
    for col in result.select_dtypes(include="object").columns:
        result[col] = result[col].str.replace(r"[\r\n]+", " ", regex=True)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / f"warmtetransitie_buurten_{TODAY}.csv"
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
        "WarmteTransitie buurten CSV geschreven: %d rijen, %d kolommen → %s (%.0fs)",
        len(result), len(result.columns), out_path.name, elapsed,
    )
    return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
