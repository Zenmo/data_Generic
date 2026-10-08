"""Generate kerncijfers_gemeenten_met_geometrie_{jaar}.csv for AnyLogic — the
municipality analogue of the buurten CSV, focused on PV and wind potential.

For each CBS year (2023, 2024) this produces two CSVs in data_Generic/:
  - kerncijfers_gemeenten_{jaar}_met_geometrie.csv  (with WKT polygon column)
  - kerncijfers_gemeenten_{jaar}.csv                (same data, no WKT — much
    smaller; keeps lat/lon centroid for tools that don't need the polygons)

Both contain the same columns otherwise:

  1. Municipality identity + geometry — from the CBS Wijk- en Buurtkaart gemeenten
     GeoPackage (raw/cbs_wijkenbuurten/{jaar}/gemeenten.gpkg). Only land parts
     (water == 'NEE') are kept and dissolved to one polygon per gemeente, so each
     row is one municipality (~342). This is the same set of municipalities as the
     CBS "Kerncijfers wijken en buurten" gemeente rows for that year.
        → gemeentecode, gemeentenaam, wkt_geometry, latitude, longitude

  2. PV (zon) potential per municipality — cached in data_Generic/municipalities.xlsx,
     sheet 'municipality_pv_potential', but originally the RVO / Kadaster / NP RES
     "Dataset zon op gebouw" (technical zon-op-dak / zon-op-veld potential). Not
     publicly downloadable — released per RES-region/gemeente on request; see
     PV_SOURCE_URL for the reference page. Matched on gemeentecode.
        → pv_potentie_dak_tj, pv_potentie_veld_*_tj, pv_potentie_water_tj,
          pv_potentie_totaal_tj, pv_potentie_totaal_gwh, pv_kans2030,
          pv_potentie_bron, pv_potentie_bron_url

  3. Wind potential per municipality — cached in the same Excel, sheet
     'municipality_wind_potential', originally Over Morgen / Nationale Energie
     Atlas "potentie windenergie op land" (public, CC-BY 4.0; see WIND_SOURCE_URL).
     Matched on gemeentecode.
        → wind_potentie_land_aantal/tj, wind_potentie_water_aantal/tj,
          wind_potentie_totaal_tj/gwh, wind_vermogen_per_turbine_kw,
          wind_potentie_bron, wind_potentie_bron_url

  4. Electricity + gas demand per SBI business sector (8-group scheme:
     a/bf/gi/hj/kl/mn/oq/ru — same grouping as the buurten company-location
     columns) — fetched live via make_energieverbruik_sector_csv.haal_energieverbruik_sector()
     (CBS 82538NED, falls back to Klimaatmonitor) and merged straight in; no
     separate CSV is written for this. Optional — skipped with a warning if
     both sources fail.
        → elec_verbruik_a_<unit>, ..., elec_verbruik_ru_<unit>,
          gas_verbruik_a_<unit>, ..., gas_verbruik_ru_<unit>,
          elec_verbruik_totaal_<unit>, gas_verbruik_totaal_<unit>,
          elec_eenheid, gas_eenheid, energie_bron, energie_bron_url
          (<unit> is whatever the source that ran actually uses — see that
          script's docstring; columns are looked up by prefix, not exact name)
          make_buurten_csv.py reads these same columns back out of
          kerncijfers_gemeenten_<jaar>.csv to allocate them to buurten, so
          --gemeenten-potentie must run before --buurten for that to work.

  5. Province + RES-region identity — from municipalities.xlsx, sheet
     'municipalities'. Included so a Java MunicipalityImporter can build a
     complete J_Municipality without a second source file.
        → province, provincecode, res_region, res_regioncode

Optionally the Over Morgen "potentie windenergie op land" WFS (hexagon grid) is
downloaded, aggregated to gemeente level, cross-checked against the Excel wind
figures, and used to FILL any municipality that is missing from the Excel wind
sheet (none for 2023/2024, but future-proof for later boundary years).

Centroids are computed in EPSG:28992 (metrically correct) then projected to
WGS84 — never compute centroids in geographic (degree) coordinates. Same
convention as make_buurten_csv.py.

Run standalone:
    python make_gemeenten_potentie_csv.py [2023 [2024]]
    python make_gemeenten_potentie_csv.py --overmorgen   # also download + cross-check Over Morgen wind
"""

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

from config import (
    BASE_DIR,
    CRS_RD,
    CRS_WGS84,
    OUTPUT_SEPARATOR,
    FILL_UNMATCHED,
    PAGE_SIZE,
    REQUEST_TIMEOUT,
    RAW_DIR,
    PROCESSED_DIR,
    CODE_COL_CANDIDATES,
)

log = logging.getLogger(__name__)

TODAY = date.today().isoformat()

# data_Generic/ is the parent of processed_data_from_loader/ (see config.py)
DATA_GENERIC = PROCESSED_DIR.parent

# municipalities.xlsx has moved location before (data_Generic/ -> data_Generic/data_loader/,
# alongside the other required input Hoofdverwarmingsinstallaties_woningen_*.xlsx) — check
# both spots each time rather than hardcoding one, so a future move doesn't silently break
# --gemeenten-potentie (it did: main() catches FileNotFoundError and bails before writing
# anything, so a missing file here means kerncijfers_gemeenten_<jaar>.csv is just never
# regenerated, with no obvious symptom besides "the output looks stale").
_MUNI_XLSX_CANDIDATES = [
    BASE_DIR / "municipalities.xlsx",      # data_loader/municipalities.xlsx (current, as of 2026-07-07)
    DATA_GENERIC / "municipalities.xlsx",  # data_Generic/municipalities.xlsx (original)
]


def _find_muni_xlsx() -> Path:
    for cand in _MUNI_XLSX_CANDIDATES:
        if cand.exists():
            return cand
    raise FileNotFoundError(
        "municipalities.xlsx niet gevonden. Gezocht op: "
        + ", ".join(str(c) for c in _MUNI_XLSX_CANDIDATES)
    )

# --- Original upstream provenance (stamped per row, not just "which local file") ---
# PV: RVO / Kadaster / NP RES "Dataset zon op gebouw" — the technical zon-op-dak /
# zon-op-veld potential analysis. Not publicly downloadable: distributed per
# RES-region / municipality on request. The copy in municipalities.xlsx was
# obtained via a municipality. Reference (describes the dataset, no direct download):
PV_SOURCE_NAME = (
    "RVO / Kadaster / NP RES — Dataset zon op gebouw "
    "(technische potentie zon-op-dak en zon-op-veld), niet publiek downloadbaar, "
    "per RES-regio/gemeente op aanvraag"
)
PV_SOURCE_URL = "https://www.regionale-energiestrategie.nl/werkwijze/data+monitoring/data+overzicht/2661076.aspx"

# Wind: Over Morgen / Nationale Energie Atlas "potentie windenergie op land"
# (hexagon grid). Public, CC-BY 4.0.
WIND_SOURCE_NAME = "Over Morgen / Nationale Energie Atlas — Potentie windenergie op land"
WIND_SOURCE_URL = "https://data.overheid.nl/dataset/6064-ruimtelijke-belemmeringen-en-potentie-voor-windenergie-op-land"

# Electricity/gas per SBI sector: CBS StatLine 82538NED. Public, CC-BY 4.0.
ENERGIE_SOURCE_NAME = (
    "CBS StatLine 82538NED — Levering aardgas, elektriciteit via openbaar net, "
    "bedrijven, SBI2008, regio"
)
ENERGIE_SOURCE_URL = "https://opendata.cbs.nl/statline/#/CBS/nl/dataset/82538NED/table"

# Only 2023 and 2024 are requested; 2025 gemeenten exist but the Excel potential
# set predates it, so keep the default list to the two requested years.
DEFAULT_YEARS = [2023, 2024]

# --- Over Morgen "potentie windenergie op land" WFS (optional cross-check / fill) ---
# Nationale Energie Atlas lineage, CC-BY 4.0. Hexagon grid (one medium turbine per hex).
OVERMORGEN_WFS = "http://maps.geocoders.nl/overmorgen/ows"
# Layer name is auto-discovered from GetCapabilities (first FeatureType whose name
# or title contains 'wind'); override here if auto-discovery picks the wrong one.
OVERMORGEN_WIND_LAYER: str | None = None
_OVERMORGEN_RAW = RAW_DIR / "overmorgen_wind"


# ---------------------------------------------------------------------------
# 1. Municipality geometry (CBS gemeenten, land only, one polygon per gemeente)
# ---------------------------------------------------------------------------

def _find_gemeentecode_kolom(gdf: gpd.GeoDataFrame) -> str:
    for naam in CODE_COL_CANDIDATES["gemeenten"]:
        if naam in gdf.columns:
            return naam
    raise ValueError(
        f"Geen gemeentecode-kolom gevonden in de GeoPackage.\n"
        f"Beschikbare kolommen: {list(gdf.columns)}"
    )


def laad_gemeente_geometrie(jaar: int) -> gpd.GeoDataFrame:
    """Load CBS gemeente polygons (land only), dissolved to one polygon per
    municipality. Returns a GeoDataFrame in EPSG:28992 with columns
    gemeentecode, gemeentenaam and geometry."""
    gpkg = RAW_DIR / "cbs_wijkenbuurten" / str(jaar) / "gemeenten.gpkg"
    if not gpkg.exists():
        raise FileNotFoundError(
            f"GeoPackage niet gevonden: {gpkg}\n"
            f"Run eerst: python run_pipeline.py --download"
        )

    gdf = gpd.read_file(gpkg)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_RD)
    else:
        gdf = gdf.to_crs(CRS_RD)

    code_col = _find_gemeentecode_kolom(gdf)
    naam_col = "gemeentenaam" if "gemeentenaam" in gdf.columns else code_col

    # Keep land parts only. CBS marks water polygons with water == 'JA'.
    if "water" in gdf.columns:
        gdf = gdf[gdf["water"].astype(str).str.upper() == "NEE"].copy()

    # Pre-validate geometries so the dissolve / overlay never hits a TopologyException.
    gdf.geometry = shapely.make_valid(gdf.geometry.values)

    # One polygon per municipality (a gemeente can be split across several rows).
    namen = gdf.groupby(code_col)[naam_col].first()
    dissolved = gdf.dissolve(by=code_col).reset_index()
    dissolved["gemeentenaam"] = dissolved[code_col].map(namen)
    dissolved = dissolved.rename(columns={code_col: "gemeentecode"})
    dissolved = dissolved[["gemeentecode", "gemeentenaam", "geometry"]]

    log.info("  Geometrie %d: %d gemeenten (land) geladen", jaar, len(dissolved))
    return dissolved


def _geo_df_met_wkt(gem_rd: gpd.GeoDataFrame) -> pd.DataFrame:
    """Turn the RD GeoDataFrame into a plain DataFrame with WKT + centroid in WGS84."""
    centroids_rd = gpd.GeoSeries(gem_rd.geometry.centroid, crs=CRS_RD)
    centroids_wgs = centroids_rd.to_crs(CRS_WGS84)
    wkt_series = gem_rd.to_crs(CRS_WGS84).geometry.to_wkt()

    return pd.DataFrame({
        "gemeentecode": gem_rd["gemeentecode"].values,
        "gemeentenaam": gem_rd["gemeentenaam"].values,
        "wkt_geometry": wkt_series.values,
        "latitude":     centroids_wgs.y.values,
        "longitude":    centroids_wgs.x.values,
    })


# ---------------------------------------------------------------------------
# 2. PV and wind potential from municipalities.xlsx
# ---------------------------------------------------------------------------

# (source column -> output column) for each potential sheet
_PV_COLS = {
    "tj_dak":      "pv_potentie_dak_tj",
    "tj_lbg_4":    "pv_potentie_veld_lbg4_tj",
    "tj_lbg_10":   "pv_potentie_veld_lbg10_tj",
    "tj_lbg_100":  "pv_potentie_veld_lbg100_tj",
    "tj_wat":      "pv_potentie_water_tj",
    "tj_tot_pot":  "pv_potentie_totaal_tj",
    "gwh_pot":     "pv_potentie_totaal_gwh",
}
_PV_CAT_COLS = {"kans2030": "pv_kans2030"}

_WIND_COLS = {
    "a_pot_lnd":  "wind_potentie_land_aantal",
    "tj_pot_lnd": "wind_potentie_land_tj",
    "a_pot_mr":   "wind_potentie_water_aantal",
    "tj_pot_mr":  "wind_potentie_water_tj",
    "tj_tot":     "wind_potentie_totaal_tj",
    "gwh_tot":    "wind_potentie_totaal_gwh",
    "kw":         "wind_vermogen_per_turbine_kw",
}


def _laad_potentie_sheet(
    sheet: str,
    num_cols: dict[str, str],
    cat_cols: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Read one potential sheet from municipalities.xlsx, aggregate to one row per
    gemeentecode (sum numeric parts, first categorical), and rename to output names."""
    muni_xlsx = _find_muni_xlsx()
    df = pd.read_excel(muni_xlsx, sheet_name=sheet)
    df = df[[c for c in df.columns if not str(c).startswith("Unnamed")]]

    if "gem_code" not in df.columns:
        raise ValueError(f"Kolom 'gem_code' ontbreekt in blad '{sheet}'")

    df = df.rename(columns={"gem_code": "gemeentecode"})
    df = df.dropna(subset=["gemeentecode"])
    df["gemeentecode"] = df["gemeentecode"].astype(str).str.strip()

    cat_cols = cat_cols or {}
    keep = [c for c in num_cols if c in df.columns]
    keep_cat = [c for c in cat_cols if c in df.columns]

    for c in keep:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # A gemeente can appear in several rows (multi-part / RES split): sum the
    # potentials, take the first value for categorical / per-turbine fields.
    agg = {c: "sum" for c in keep}
    agg.update({c: "first" for c in keep_cat})
    # kw (power per turbine) is a constant, not additive → take first
    if "kw" in agg:
        agg["kw"] = "first"

    grouped = df.groupby("gemeentecode", as_index=False).agg(agg)
    rename = {**num_cols, **cat_cols}
    grouped = grouped.rename(columns={k: v for k, v in rename.items() if k in grouped.columns})

    log.info("  %s: %d gemeenten met potentie geladen", sheet, len(grouped))
    return grouped


def laad_pv_potentie() -> pd.DataFrame:
    return _laad_potentie_sheet("municipality_pv_potential", _PV_COLS, _PV_CAT_COLS)


def laad_wind_potentie() -> pd.DataFrame:
    return _laad_potentie_sheet("municipality_wind_potential", _WIND_COLS)


def laad_provincie_res() -> pd.DataFrame:
    """Province + RES-region identity per gemeente, from municipalities.xlsx
    sheet 'municipalities'. Needed so a Java loader can build a complete
    J_Municipality from this one CSV."""
    muni_xlsx = _find_muni_xlsx()
    df = pd.read_excel(muni_xlsx, sheet_name="municipalities")
    df = df[[c for c in df.columns if not str(c).startswith("Unnamed")]]
    df = df.rename(columns={
        "gwb_code": "gemeentecode",
        "province": "province",
        "province_code": "provinciecode",
        "RES_region": "res_regio",
        "RES_code": "res_regiocode",
    })
    keep = ["gemeentecode", "province", "provinciecode", "res_regio", "res_regiocode"]
    df = df[[c for c in keep if c in df.columns]].dropna(subset=["gemeentecode"])
    df["gemeentecode"] = df["gemeentecode"].astype(str).str.strip()
    df = df.drop_duplicates(subset=["gemeentecode"])

    log.info("  municipalities: %d gemeenten met provincie/RES-regio geladen", len(df))
    return df


# ---------------------------------------------------------------------------
# 3. Sector-level electricity/gas demand (optional: CBS 82538NED, falls back
#    to Klimaatmonitor — fetched live, no separate CSV, merged straight into
#    kerncijfers_gemeenten below)
# ---------------------------------------------------------------------------

def laad_energieverbruik_sector(jaren: list[int]) -> pd.DataFrame | None:
    """Fetch sector electricity/gas demand for the given years and return it
    ready to merge. Returns None if both CBS and Klimaatmonitor failed —
    kerncijfers_gemeenten is still written, just without these columns.

    Numeric columns are detected by prefix ("elec_verbruik_"/"gas_verbruik_"),
    not exact name, because the unit suffix baked into each column name
    (e.g. "_kwh", "_m3", "_tj") depends on which source actually produced the
    data — CBS and Klimaatmonitor do not necessarily agree on units."""
    import make_energieverbruik_sector_csv as sector_energie

    df = sector_energie.haal_energieverbruik_sector(jaren)
    if df is None:
        log.warning(
            "  Sectorale energieverbruik niet beschikbaar (CBS en Klimaatmonitor beide "
            "mislukt) — elec/gas-per-sector kolommen worden overgeslagen."
        )
        return None
    return df


# ---------------------------------------------------------------------------
# 3b. Bedrijfswagens (bestelauto/vrachtauto) — aggregated up from the per-buurt
#     estimate (make_bedrijfswagens_csv.py), which does the actual PC4->buurt
#     dasymetric distribution and RDW/CBS-split work. This is per geometry year,
#     like the buurten-CSV merge in make_buurten_csv.py, not a one-off national fetch.
# ---------------------------------------------------------------------------

def laad_bedrijfswagens_gemeente(jaar: int) -> pd.DataFrame | None:
    """
    Sum the per-buurt bestelauto/vrachtauto estimate up to gemeente level, and attach
    the official CBS 85236NED 2023 gemeente totals + the deviation-% already computed
    by that script's own validation step, so the comparison is visible directly in the
    gemeenten CSV (not just in the pipeline log / separate validation CSV).
    Returns None if the bedrijfswagens phase has not been run yet for this year.
    """
    buurt_candidates = sorted(PROCESSED_DIR.glob(f"bedrijfswagens_buurten_{jaar}_*.csv"), reverse=True)
    if not buurt_candidates:
        log.warning(
            "  Bedrijfswagens-schatting voor jaar %d niet gevonden — bestelautos_totaal/"
            "vrachtautos_totaal worden overgeslagen in de gemeenten-CSV. "
            "Run: python run_pipeline.py --bedrijfswagens",
            jaar,
        )
        return None

    df = pd.read_csv(buurt_candidates[0], sep=OUTPUT_SEPARATOR, dtype=str)
    telkolommen = [
        "bestelautos_totaal", "vrachtautos_totaal",
        "bestelautos_ondergrens", "bestelautos_bovengrens",
        "vrachtautos_ondergrens", "vrachtautos_bovengrens",
    ]
    for col in telkolommen:
        df[col] = pd.to_numeric(df[col].replace(FILL_UNMATCHED, pd.NA), errors="coerce")
    df["gemeentecode"] = "GM" + df["buurtcode"].str[2:6]
    per_gem = df.groupby("gemeentecode")[telkolommen].sum(min_count=1).reset_index()

    validatie_candidates = sorted(
        PROCESSED_DIR.glob(f"bedrijfswagens_validatie_gemeenten_{jaar}_*.csv"), reverse=True
    )
    if validatie_candidates:
        val = pd.read_csv(validatie_candidates[0], sep=OUTPUT_SEPARATOR, dtype=str)
        keep = [c for c in [
            "gemeentecode", "bestelauto_cbs2023", "vrachtauto_cbs2023",
            "bestelauto_afwijking_pct", "vrachtauto_afwijking_pct",
        ] if c in val.columns]
        per_gem = per_gem.merge(val[keep], on="gemeentecode", how="left")

    log.info(
        "  Bedrijfswagens-schatting %d geladen (naar gemeente geaggregeerd): %s (%d gemeenten)",
        jaar, buurt_candidates[0].name, len(per_gem),
    )
    return per_gem


# ---------------------------------------------------------------------------
# 4. Over Morgen wind potential (optional): download, aggregate, cross-check, fill
# ---------------------------------------------------------------------------

def _discover_overmorgen_layer() -> str | None:
    """Return the WFS FeatureType name for the wind-potential layer via GetCapabilities."""
    import re
    import requests

    if OVERMORGEN_WIND_LAYER:
        return OVERMORGEN_WIND_LAYER
    try:
        resp = requests.get(
            OVERMORGEN_WFS,
            params={"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("  Over Morgen GetCapabilities mislukt: %s", exc)
        return None

    candidates: list[tuple[str, str]] = []
    for m in re.finditer(r"<(?:\w+:)?FeatureType>(.*?)</(?:\w+:)?FeatureType>", resp.text, re.S):
        blk = m.group(1)
        name = re.search(r"<(?:\w+:)?Name>(.*?)</", blk)
        title = re.search(r"<(?:\w+:)?Title>(.*?)</", blk)
        if name:
            candidates.append((name.group(1), title.group(1) if title else ""))

    log.info("  Over Morgen FeatureTypes: %s", [c[0] for c in candidates])
    for name, title in candidates:
        if "wind" in name.lower() or "wind" in title.lower():
            log.info("  Over Morgen wind-laag gekozen: %s", name)
            return name
    if candidates:
        log.warning("  Geen 'wind'-laag herkend; eerste laag gebruikt: %s", candidates[0][0])
        return candidates[0][0]
    return None


def download_overmorgen_wind(force: bool = False) -> Path | None:
    """Download the Over Morgen wind-potential hexagon layer as a GeoPackage (cached)."""
    import requests

    out_dir = _OVERMORGEN_RAW
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "wind_potentie.gpkg"
    if out_path.exists() and not force:
        log.info("  Over Morgen gecached, overgeslagen: %s", out_path.relative_to(RAW_DIR))
        return out_path

    layer = _discover_overmorgen_layer()
    if layer is None:
        log.warning("  Geen Over Morgen wind-laag gevonden — download overgeslagen.")
        return None

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
    log.info("  Over Morgen download %s …", layer)
    while True:
        params = {**params_base, "STARTINDEX": start_index}
        try:
            resp = requests.get(OVERMORGEN_WFS, params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("  Over Morgen WFS-fout [startIndex=%d]: %s", start_index, exc)
            return None
        feats = data.get("features") or []
        if not feats:
            break
        all_features.extend(feats)
        start_index += len(feats)
        if len(feats) < PAGE_SIZE:
            break

    if not all_features:
        log.warning("  Over Morgen: geen features ontvangen.")
        return None

    gdf = gpd.GeoDataFrame.from_features(all_features, crs=CRS_RD)
    log.info("  Over Morgen: %d hexagonen, kolommen: %s", len(gdf), list(gdf.columns))
    tmp = out_path.with_suffix(".tmp.gpkg")
    try:
        gdf.to_file(tmp, driver="GPKG")
        tmp.replace(out_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("  Over Morgen schrijffout: %s", exc)
        return None
    return out_path


# Candidate hexagon attributes holding a per-hex potential magnitude (MW / turbines).
# The exact schema is not published; the first match present in the layer is used,
# else we fall back to counting hexagons. Adjust after inspecting DescribeFeatureType.
_OVERMORGEN_VALUE_CANDIDATES = [
    "mw", "vermogen", "potentie_mw", "n_turbines", "aantal_turbines",
    "turbines", "potentie", "capaciteit",
]


def aggregeer_overmorgen_per_gemeente(
    gpkg: Path, gem_rd: gpd.GeoDataFrame
) -> pd.DataFrame | None:
    """Match Over Morgen hexagons to gemeenten (hex centroid within gemeente) and
    aggregate to a per-gemeente wind-potential table. Returns None on failure."""
    hexes = gpd.read_file(gpkg)
    if hexes.crs is None:
        hexes = hexes.set_crs(CRS_RD)
    else:
        hexes = hexes.to_crs(CRS_RD)
    hexes.geometry = shapely.make_valid(hexes.geometry.values)

    value_col = next((c for c in _OVERMORGEN_VALUE_CANDIDATES if c in hexes.columns), None)
    if value_col is None:
        log.warning(
            "  Over Morgen: geen bekende potentie-kolom gevonden in %s — "
            "alleen hexagon-telling per gemeente. Pas _OVERMORGEN_VALUE_CANDIDATES aan.",
            list(hexes.columns),
        )

    # Match on hexagon centroid (fast, unambiguous for a fine grid).
    pts = hexes[["geometry"]].copy()
    pts["geometry"] = pts.geometry.centroid
    if value_col:
        pts[value_col] = pd.to_numeric(hexes[value_col].values, errors="coerce")

    joined = gpd.sjoin(
        pts, gem_rd[["gemeentecode", "geometry"]], how="left", predicate="within"
    )
    grp = joined.groupby("gemeentecode")
    out = pd.DataFrame({"overmorgen_hex_aantal": grp.size()})
    if value_col:
        out["overmorgen_wind_potentie"] = grp[value_col].sum()
    out = out.reset_index()
    log.info("  Over Morgen geaggregeerd: %d gemeenten", len(out))
    return out


def cross_check_wind(excel_wind: pd.DataFrame, overmorgen: pd.DataFrame) -> None:
    """Log how well the Excel wind figures line up with the Over Morgen aggregation."""
    if "overmorgen_wind_potentie" not in overmorgen.columns:
        log.info("  Cross-check overgeslagen: geen numerieke Over Morgen potentie-kolom.")
        return
    merged = excel_wind.merge(overmorgen, on="gemeentecode", how="inner")
    if merged.empty or "wind_potentie_land_aantal" not in merged.columns:
        log.info("  Cross-check: geen overlappende gemeenten om te vergelijken.")
        return
    a = pd.to_numeric(merged["wind_potentie_land_aantal"], errors="coerce")
    b = pd.to_numeric(merged["overmorgen_wind_potentie"], errors="coerce")
    mask = a.notna() & b.notna()
    if mask.sum() >= 3:
        corr = a[mask].corr(b[mask])
        log.info(
            "  Cross-check Excel-wind vs Over Morgen: n=%d, correlatie=%.3f, "
            "som Excel=%.0f, som Over Morgen=%.0f",
            int(mask.sum()), corr, a[mask].sum(), b[mask].sum(),
        )
    else:
        log.info("  Cross-check: te weinig overlappende numerieke waarden.")


# ---------------------------------------------------------------------------
# 5. Assemble one municipality CSV per year
# ---------------------------------------------------------------------------

def _strip_separator_from_text_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Replaces any literal OUTPUT_SEPARATOR (";") inside object/string columns
    with "," before writing. The Java-side CsvLoader used by the model does a
    naive line.split(";") with no CSV-quote awareness, so a free-text column
    containing a raw ";" silently inserts an extra field and shifts every
    later column on that row — exactly what PV_SOURCE_NAME/ENERGIE_SOURCE_NAME
    did here (discovered because it corrupted bestelautos_totaal/
    vrachtautos_totaal for every single gemeente row). pandas' own quoting on
    to_csv() doesn't help, since it protects against a *correct* CSV reader,
    not this codebase's simpler split-based one — so the safest fix is to
    never emit a raw ";" in the first place, at the one place all rows funnel
    through before being written.
    """
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            # .str.replace() (not .astype(str) first!) leaves real NaN as NaN,
            # so .fillna(FILL_UNMATCHED) downstream still catches genuine gaps
            # instead of them becoming the literal string "nan".
            df[col] = df[col].str.replace(OUTPUT_SEPARATOR, ",", regex=False)
    return df


def _atomic_write_csv(df: pd.DataFrame, out: Path) -> None:
    """Write via a temp file + rename. Falls back to a direct (non-atomic) write
    if the rename fails because the target is locked by another process — seen
    on this repo's WSL2/Windows-mounted workspace, where a file can be briefly
    held by the host (e.g. antivirus scanning a freshly written CSV)."""
    df = _strip_separator_from_text_columns(df)
    tmp = out.with_suffix(".tmp.csv")
    try:
        df.fillna(FILL_UNMATCHED).to_csv(tmp, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
        try:
            tmp.replace(out)
        except OSError:
            log.warning(
                "  %s is vergrendeld door een ander proces — direct overschreven "
                "(niet atomair).", out.name,
            )
            tmp.unlink(missing_ok=True)
            df.fillna(FILL_UNMATCHED).to_csv(out, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def maak_csv(
    jaar: int,
    pv_df: pd.DataFrame,
    wind_df: pd.DataFrame,
    provincie_res_df: pd.DataFrame | None = None,
    energie_df: pd.DataFrame | None = None,
    overmorgen: pd.DataFrame | None = None,
    bedrijfswagens_df: pd.DataFrame | None = None,
) -> bool:
    """Assemble kerncijfers_gemeenten_met_geometrie_{jaar}.csv. Returns True on success."""
    log.info("  --- Jaar %d ---", jaar)

    gem_rd = laad_gemeente_geometrie(jaar)
    base = _geo_df_met_wkt(gem_rd)

    merged = base.merge(pv_df, on="gemeentecode", how="left")
    merged = merged.merge(wind_df, on="gemeentecode", how="left")
    if provincie_res_df is not None:
        merged = merged.merge(provincie_res_df, on="gemeentecode", how="left")

    n_pv   = merged["pv_potentie_totaal_tj"].notna().sum() if "pv_potentie_totaal_tj" in merged else 0
    n_wind = merged["wind_potentie_totaal_tj"].notna().sum() if "wind_potentie_totaal_tj" in merged else 0
    log.info("  Na koppeling: %d gemeenten, PV-data voor %d, wind-data voor %d",
             len(merged), n_pv, n_wind)

    # --- Sector electricity/gas demand (nearest available year) ---
    if energie_df is not None and not energie_df.empty:
        beschikbare_jaren = sorted(energie_df["jaar"].dropna().unique().tolist())
        target_jaar = min(beschikbare_jaren, key=lambda y: abs(y - jaar))
        if target_jaar != jaar:
            log.warning("  Energieverbruik-sector: jaar %d niet beschikbaar, gebruik %d als meest nabije",
                        jaar, target_jaar)
        energie_jaar = energie_df[energie_df["jaar"] == target_jaar].drop(columns=["jaar"])
        merged = merged.merge(energie_jaar, on="gemeentecode", how="left")

        sector_cols = [c for c in energie_jaar.columns
                       if c.startswith(("elec_verbruik_", "gas_verbruik_"))]
        if sector_cols:
            heeft_energie = merged[sector_cols[0]].notna()
            # Prefer provenance already embedded in the sector CSV itself (it records
            # whichever source — CBS or Klimaatmonitor — actually succeeded for that
            # download run). Only fall back to the hardcoded CBS-only labels for older
            # sector CSVs generated before that column existed.
            if "energie_bron" not in merged.columns:
                merged["energie_bron"] = heeft_energie.map({True: ENERGIE_SOURCE_NAME, False: "n.a."})
                merged["energie_bron_url"] = heeft_energie.map({True: ENERGIE_SOURCE_URL, False: "n.a."})
            log.info("  Energieverbruik-sector %d toegevoegd: %d gemeenten met data",
                     target_jaar, int(heeft_energie.sum()))

    # Provenance: the original upstream source, not just "which local file we read
    # it from". municipalities.xlsx is a cached copy of these two sources.
    if "pv_potentie_totaal_tj" in merged.columns:
        heeft_pv = merged["pv_potentie_totaal_tj"].notna()
        merged["pv_potentie_bron"] = heeft_pv.map({True: PV_SOURCE_NAME, False: "n.a."})
        merged["pv_potentie_bron_url"] = heeft_pv.map({True: PV_SOURCE_URL, False: "n.a."})
    if "wind_potentie_totaal_tj" in merged.columns:
        heeft_wind = merged["wind_potentie_totaal_tj"].notna()
        merged["wind_potentie_bron"] = heeft_wind.map({True: WIND_SOURCE_NAME, False: "n.a."})
        merged["wind_potentie_bron_url"] = heeft_wind.map({True: WIND_SOURCE_URL, False: "n.a."})
    if overmorgen is not None and "overmorgen_wind_potentie" in overmorgen.columns:
        merged = merged.merge(overmorgen, on="gemeentecode", how="left")
        missing = merged["wind_potentie_totaal_tj"].isna() if "wind_potentie_totaal_tj" in merged else pd.Series(False, index=merged.index)
        fill_mask = missing & merged["overmorgen_wind_potentie"].notna()
        n_fill = int(fill_mask.sum())
        if n_fill:
            merged.loc[fill_mask, "wind_potentie_bron"] = WIND_SOURCE_NAME + " (live WFS, gemeente ontbrak in municipalities.xlsx)"
            merged.loc[fill_mask, "wind_potentie_bron_url"] = OVERMORGEN_WFS
            log.info("  Over Morgen: %d gemeenten aangevuld die in de Excel ontbraken", n_fill)
        else:
            log.info("  Over Morgen: geen ontbrekende gemeenten om aan te vullen (Excel dekt alles)")

    # --- Bedrijfswagens (bestelauto/vrachtauto), aggregated from the buurt estimate ---
    if bedrijfswagens_df is not None:
        merged = merged.merge(bedrijfswagens_df, on="gemeentecode", how="left")
        log.info(
            "  Bedrijfswagens toegevoegd: %d gemeenten met schatting",
            int(merged["bestelautos_totaal"].notna().sum()) if "bestelautos_totaal" in merged else 0,
        )

    codes_zonder_pv = merged.loc[merged.get("pv_potentie_totaal_tj").isna(), "gemeentecode"].tolist() \
        if "pv_potentie_totaal_tj" in merged else []
    if codes_zonder_pv:
        log.warning("  Zonder PV-potentie: %s", codes_zonder_pv)

    # Written independently — one file being locked (e.g. open in another program)
    # must not prevent the other from being written.
    ok = True

    # WKT-geometry version is an intermediate (28 MB, not used directly by the
    # model) — lives in processed/gemeenten/, mirroring processed/buurten/.
    # data_Generic/ root is kept to only the files used directly by the model.
    _GEMEENTEN_OUT_DIR = PROCESSED_DIR / "gemeenten"
    _GEMEENTEN_OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _GEMEENTEN_OUT_DIR / f"kerncijfers_gemeenten_{jaar}_met_geometrie.csv"
    try:
        _atomic_write_csv(merged, out)
        size_kb = out.stat().st_size / 1024
        log.info("  Opgeslagen: %s  (%d rijen, %d kolommen, %.0f KB)",
                 out.name, len(merged), len(merged.columns), size_kb)
    except Exception as exc:
        log.error("  Kon %s niet schrijven (mogelijk geopend in een ander programma): %s", out.name, exc)
        ok = False

    # Lighter companion file without WKT geometry (lat/lon centroid kept) — same
    # data, a fraction of the size. This one IS used directly by the model, so
    # it lives in data_Generic/ root.
    out_geen_geom = DATA_GENERIC / f"kerncijfers_gemeenten_{jaar}.csv"
    zonder_geom = merged.drop(columns=["wkt_geometry"])
    try:
        _atomic_write_csv(zonder_geom, out_geen_geom)
        size_kb_geen_geom = out_geen_geom.stat().st_size / 1024
        log.info("  Opgeslagen (zonder geometrie): %s  (%d rijen, %d kolommen, %.0f KB)",
                 out_geen_geom.name, len(zonder_geom), len(zonder_geom.columns), size_kb_geen_geom)
    except Exception as exc:
        log.error("  Kon %s niet schrijven (mogelijk geopend in een ander programma): %s", out_geen_geom.name, exc)
        ok = False

    return ok


def main(jaren: list[int] | None = None, met_overmorgen: bool = False,
         force_overmorgen: bool = False) -> bool:
    jaren = jaren or list(DEFAULT_YEARS)

    try:
        pv_df = laad_pv_potentie()
        wind_df = laad_wind_potentie()
        provincie_res_df = laad_provincie_res()
    except Exception as exc:
        log.error("Kon potentie-data niet laden uit municipalities.xlsx: %s", exc)
        return False

    energie_df = laad_energieverbruik_sector(jaren)  # optional — None if both sources fail

    overmorgen: pd.DataFrame | None = None
    if met_overmorgen:
        gpkg = download_overmorgen_wind(force=force_overmorgen)
        if gpkg is not None:
            # Aggregate against the most recent requested year's geometry, then
            # cross-check versus the Excel wind sheet.
            try:
                gem_rd = laad_gemeente_geometrie(max(jaren))
                overmorgen = aggregeer_overmorgen_per_gemeente(gpkg, gem_rd)
                if overmorgen is not None:
                    cross_check_wind(wind_df, overmorgen)
            except Exception as exc:
                log.warning("  Over Morgen-verwerking mislukt (overgeslagen): %s", exc)
                overmorgen = None

    all_ok = True
    t0 = time.monotonic()
    for jaar in jaren:
        try:
            ok = maak_csv(jaar, pv_df, wind_df, provincie_res_df=provincie_res_df,
                          energie_df=energie_df, overmorgen=overmorgen,
                          bedrijfswagens_df=laad_bedrijfswagens_gemeente(jaar))
        except FileNotFoundError as exc:
            log.error("FOUT jaar %d: %s", jaar, exc)
            ok = False
        except Exception as exc:
            log.error("FOUT jaar %d: %s", jaar, exc, exc_info=True)
            ok = False
        all_ok = all_ok and ok

    log.info("Gemeenten-potentie CSV generatie klaar in %.0fs", time.monotonic() - t0)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    parser = argparse.ArgumentParser(description="Gemeente PV/wind potentie CSV")
    parser.add_argument("jaren", nargs="*", type=int, help="CBS-jaren (standaard 2023 2024)")
    parser.add_argument("--overmorgen", action="store_true",
                        help="Download + cross-check + fill met Over Morgen wind-WFS")
    parser.add_argument("--force-overmorgen", action="store_true",
                        help="Negeer de Over Morgen cache en download opnieuw")
    args = parser.parse_args()
    ok = main(args.jaren or None, met_overmorgen=args.overmorgen,
              force_overmorgen=args.force_overmorgen)
    sys.exit(0 if ok else 1)
