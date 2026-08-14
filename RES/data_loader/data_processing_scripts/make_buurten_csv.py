"""Generate kerncijfers_buurten_met_geometrie_{jaar}.csv for AnyLogic.

Integrates with the WarmteAtlas pipeline as Phase 3.
Produces one enriched CSV per CBS year with three data sources merged:

  1. CBS kerncijfers (households, sectors, vehicles, energy use) — from CBS OData
  2. CBS Hoofdverwarmingsinstallaties (heating type fractions) — from Phase 4 CSV
  3. ElaadNL EV prognoses (car_bev/car_phev/van/truck per scenario/year) — from Phase 6 CSV
     → pivoted wide: columns named  ev_{modality}_{scenario}_{year}  (e.g. ev_car_bev_middle_2030)
  4. Sector electricity/gas demand — read back out of the already-generated
     kerncijfers_gemeenten_<jaar>.csv (see make_gemeenten_potentie_csv.py,
     which fetches it live from CBS StatLine 82538NED or, as a fallback,
     Klimaatmonitor). The municipality's demand per SBI-group (a/bf/gi/hj/kl/
     mn/oq/ru) is allocated to buurten in proportion to each buurt's share of
     that sector's company locations within the gemeente. A buurt with -99999
     company locations for a sector doesn't count towards the total and gets 0
     demand for it; if a gemeente has no buurt with valid data for a sector,
     that sector's demand is split evenly instead so nothing is lost
     (see _verdeel_energieverbruik_sector and 'energieverbruik_sector_gelijk_verdeeld').
     Requires `--gemeenten-potentie` to have run first.
     → elec_verbruik_{groep}_<eenheid>, gas_verbruik_{groep}_<eenheid> for groep
       in a/bf/gi/hj/kl/mn/oq/ru (unit suffix depends on which source succeeded)

Sources 2, 3 and 4 are merged if their data is available; skipped with a warning otherwise.
This means the buurten CSV can be regenerated at any time and will automatically pick up any
newly completed ElaadNL download or verwarmingsinstallaties update.

Centroids are computed in EPSG:28992 (metrically correct) and then projected
to WGS84 — never compute centroids in geographic (degree) coordinates.

Run standalone:  python make_buurten_csv.py [2023 [2024 [2025]]]
Or via:          python run_pipeline.py --buurten
"""

import logging
import sys
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd

from config import CBS_YEARS, CRS_RD, CRS_WGS84, OUTPUT_SEPARATOR, FILL_UNMATCHED, RAW_DIR, PROCESSED_DIR, DATA_GENERIC

log = logging.getLogger(__name__)

_DIR = Path(__file__).parent
_RAW_WB = RAW_DIR / "cbs_wijkenbuurten"
_OUT_DIR = PROCESSED_DIR / "buurten"
_MAPPING = _DIR / "mapping_kolomnamen_CBS.xlsx"

# Years for which a copy is also written to data_Generic/ (see maak_csv()), for
# direct use by the model — see _process_year() in make_windturbines_csv.py for
# the equivalent.
_STABLE_COPY_YEARS = {2023, 2024}

_BUURTCODE_KANDIDATEN = [
    "buurtcode", "BU_CODE", "bu_code", "Buurtcode", "codering", "Codering_3"
]


def _vind_buurtcode_kolom(gdf: gpd.GeoDataFrame) -> str:
    for naam in _BUURTCODE_KANDIDATEN:
        if naam in gdf.columns:
            return naam
    raise ValueError(
        f"Geen buurtcode-kolom gevonden in de GeoPackage.\n"
        f"Beschikbare kolommen: {list(gdf.columns)}\n"
        f"Voeg de juiste naam toe aan _BUURTCODE_KANDIDATEN."
    )


def laad_geometrie(jaar: int) -> pd.DataFrame:
    """Load buurt polygons from GeoPackage, return DataFrame with WKT + centroid."""
    gpkg = _RAW_WB / str(jaar) / "buurten.gpkg"
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

    buurtcode_kolom = _vind_buurtcode_kolom(gdf)
    if buurtcode_kolom != "buurtcode":
        log.info("  buurtcode-kolom heet '%s' (hernoemd naar 'buurtcode')", buurtcode_kolom)

    n = len(gdf)
    log.info("  Geometrie %d: %d buurten geladen", jaar, n)
    if n < 10_000:
        log.warning(
            "  Slechts %d buurten gevonden (verwacht ~12.000-13.000). "
            "Mogelijk zijn de CBS WFS-gegevens incompleet gedownload.", n
        )

    centroids_rd = gpd.GeoSeries(gdf.geometry.centroid, crs=CRS_RD)
    centroids_wgs = centroids_rd.to_crs(CRS_WGS84)
    wkt_series = gdf.to_crs(CRS_WGS84).geometry.to_wkt()

    return pd.DataFrame({
        "buurtcode":    gdf[buurtcode_kolom].values,
        "wkt_geometry": wkt_series.values,
        "latitude":     centroids_wgs.y.values,
        "longitude":    centroids_wgs.x.values,
    })


def _find_latest(pattern: str) -> Path | None:
    """Return the most recently created file matching a glob pattern in PROCESSED_DIR."""
    files = sorted(PROCESSED_DIR.glob(pattern), reverse=True)
    return files[0] if files else None


def _laad_verwarming() -> pd.DataFrame | None:
    """
    Load the full verwarmingsinstallaties_buurten CSV (all years).
    Returns None if the file does not exist yet (Phase 4 not yet run).
    """
    csv = _find_latest("verwarmingsinstallaties_buurten_*.csv")
    if csv is None:
        log.warning(
            "  Verwarmingsinstallaties CSV niet gevonden in processed/ — "
            "verwarming-kolommen worden overgeslagen. "
            "Run: python run_pipeline.py --verwarming"
        )
        return None

    df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
    df["jaar"] = pd.to_numeric(df["jaar"], errors="coerce").astype("Int64")
    log.info("  Verwarmingsinstallaties geladen: %s (%d rijen)", csv.name, len(df))
    return df


def _laad_warmtetransitie() -> pd.DataFrame | None:
    """
    Load WarmteTransitie per-gebied CSV.
    Returns None if not yet generated (run: python run_pipeline.py --warmtetransitie).
    """
    csv = _find_latest("warmtetransitie_buurten_*.csv")
    if csv is None:
        log.warning(
            "  WarmteTransitie CSV niet gevonden in processed/ — wtp-kolommen worden overgeslagen. "
            "Run: python run_pipeline.py --warmtetransitie"
        )
        return None

    df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
    log.info("  WarmteTransitie geladen: %s (%d rijen)", csv.name, len(df))
    return df


def _laad_solar() -> pd.DataFrame | None:
    """
    Load CBS Zonnestroom per-buurt CSV (multiple years).
    Returns None if not yet generated (run: python run_pipeline.py --solar).
    """
    csv = _find_latest("solar_buurten_*.csv")
    if csv is None:
        log.warning(
            "  Solar CSV niet gevonden in processed/ — solar-kolommen worden overgeslagen. "
            "Run: python run_pipeline.py --solar"
        )
        return None

    df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
    df["jaar"] = pd.to_numeric(df["jaar"], errors="coerce").astype("Int64")
    log.info("  Solar geladen: %s (%d rijen)", csv.name, len(df))
    return df


def _laad_energieverbruik_sector() -> pd.DataFrame | None:
    """
    Load sector electricity/gas demand by reading it back out of the already-
    generated kerncijfers_gemeenten_<jaar>.csv files (written by
    make_gemeenten_potentie_csv.py) — there is no separate CSV for this data.
    Requires `python run_pipeline.py --gemeenten-potentie` to have run first
    for at least one year; returns None (with a warning) if none of the
    expected files exist yet, or none of them have sector-energy columns.

    Numeric columns are detected by prefix ("elec_verbruik_"/"gas_verbruik_"),
    not exact name: each column's unit suffix (e.g. "_kwh", "_m3", "_tj")
    depends on which source (CBS or Klimaatmonitor) actually produced the data.
    """
    frames = []
    for jaar in CBS_YEARS:
        csv = DATA_GENERIC / f"kerncijfers_gemeenten_{jaar}.csv"
        if not csv.exists():
            continue
        df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
        sector_cols = [c for c in df.columns if c.startswith(("elec_verbruik_", "gas_verbruik_"))]
        if not sector_cols or "gemeentecode" not in df.columns:
            continue
        subset = df[["gemeentecode"] + sector_cols].copy()
        subset["jaar"] = jaar
        for c in sector_cols:
            subset[c] = pd.to_numeric(subset[c].replace(FILL_UNMATCHED, pd.NA), errors="coerce")
        frames.append(subset)

    if not frames:
        log.warning(
            "  Geen kerncijfers_gemeenten_<jaar>.csv met sectorale energie gevonden in "
            "data_Generic/ — elec/gas-per-sector kolommen per buurt worden overgeslagen. "
            "Run eerst: python run_pipeline.py --gemeenten-potentie"
        )
        return None

    result = pd.concat(frames, ignore_index=True)
    log.info(
        "  Energieverbruik-sector geladen uit kerncijfers_gemeenten (%d rijen, %d jaren)",
        len(result), result["jaar"].nunique(),
    )
    return result


def _laad_bedrijfswagens(jaar: int) -> pd.DataFrame | None:
    """
    Load the per-buurt bestelauto/vrachtauto estimate for one geometry year
    (make_bedrijfswagens_csv.py — dasymetric PC4->buurt distribution of RDW's
    current combined total, split via the CBS 85236NED bestelauto/vrachtauto ratio).
    Unlike the other optional sources this one is generated per geometry year (the
    PC4<->buurt overlay depends on the buurt boundaries), not once for all years.
    Returns None if not yet generated for this year.
    """
    csv = _find_latest(f"bedrijfswagens_buurten_{jaar}_*.csv")
    if csv is None:
        log.warning(
            "  Bedrijfswagens-schatting voor jaar %d niet gevonden in processed/ — "
            "bestelautos_totaal/vrachtautos_totaal worden overgeslagen. "
            "Run: python run_pipeline.py --bedrijfswagens",
            jaar,
        )
        return None
    df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
    df = df.rename(columns={"buurtcode": "codering"})
    log.info("  Bedrijfswagens-schatting %d geladen: %s (%d buurten)", jaar, csv.name, len(df))
    return df


def _laad_capaciteitskaart(jaar: int) -> pd.DataFrame | None:
    """
    Load the per-buurt dominant voedingsgebied_id (make_capaciteitskaart_csv.py —
    PC6->buurt largest-overlap matching, then dominant voedingsgebied_id among a
    buurt's PC6's from congestie_pc6.csv). Per geometry year, like bedrijfswagens
    above (the PC6<->buurt overlay depends on the buurt boundaries).
    Returns None if not yet generated for this year.
    """
    csv = _find_latest(f"capaciteitskaart_buurten_{jaar}_*.csv")
    if csv is None:
        log.warning(
            "  Capaciteitskaart-koppeling voor jaar %d niet gevonden in processed/ — "
            "voedingsgebied_id wordt overgeslagen. Run: python run_pipeline.py --capaciteitskaart",
            jaar,
        )
        return None
    df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
    log.info("  Capaciteitskaart-koppeling %d geladen: %s (%d buurten)", jaar, csv.name, len(df))
    return df


def _laad_elaadnl_wide() -> pd.DataFrame | None:
    """
    Load the ElaadNL processed CSV and pivot to wide format.
    Column names: ev_{modality}_{scenario}_{year}  (e.g. ev_car_bev_middle_2030)
    Returns None if no processed CSV exists yet.
    Generate one at any time with: python run_pipeline.py --elaadnl
    """
    csv = _find_latest("elaadnl_ev_prognoses_*.csv")
    if csv is None:
        log.info(
            "  ElaadNL CSV niet gevonden — EV-kolommen worden overgeslagen. "
            "Run: python run_pipeline.py --elaadnl"
        )
        return None

    log.info("  ElaadNL laden en pivoteren: %s ...", csv.name)
    df = pd.read_csv(csv, sep=OUTPUT_SEPARATOR, dtype=str)
    df["jaar"]      = pd.to_numeric(df["jaar"], errors="coerce")
    df["ev_aantal"] = pd.to_numeric(
        df["ev_aantal"].replace(FILL_UNMATCHED, None), errors="coerce"
    )
    df = df.dropna(subset=["buurtcode", "jaar", "scenario", "modality"])
    df["jaar"] = df["jaar"].astype(int)

    # Keep only selected years to limit column count
    _EV_JAREN = {2025, 2030, 2040, 2050}
    df = df[df["jaar"].isin(_EV_JAREN)]

    # Column name: ev_{modality}_{scenario}_{year}
    df["_col"] = "ev_" + df["modality"] + "_" + df["scenario"] + "_" + df["jaar"].astype(str)

    wide = df.pivot_table(
        index="buurtcode",
        columns="_col",
        values="ev_aantal",
        aggfunc="first",
    ).reset_index()
    wide = wide.rename(columns={"buurtcode": "codering"})
    wide.columns.name = None

    n_ev_cols = len(wide.columns) - 1
    log.info("  ElaadNL breed: %d buurten, %d EV-kolommen", len(wide), n_ev_cols)
    return wide


# ---------------------------------------------------------------------------
# Missing-value fill strategy
# ---------------------------------------------------------------------------

_MISSING = -99999  # sentinel for numeric columns with no available data

# Share / average columns: fill with the CBS gemeente value, then -99999.
# haal_kwb() substitutes NaN → -99999 in the buurt data, so we undo that
# sentinel and replace with the gemeente-level figure from CBS.
_GEMEENTE_VAL_COLS = [
    "percentage_eengezinswoning",
    "percentage_meergezinswoning",
    "koopwoningen_koopwoningen",
    "huurwoningen_totaal",
    "in_bezit_woningcorporatie",
    "in_bezit_overige_verhuurders",
    "eigendom_onbekend",
    "bouwjaar_voor_2000",
    "bouwjaar_vanaf_2000",
    "gemiddelde_elektriciteitslevering",
    "gemiddeld_aardgasverbruik",
    "percentage_woningen_met_stadsverwarming",
    # verwarmingsinstallaties fractions (arrive as "n.a." strings, not -99999)
    "individuele_cv",
    "blokverwarming",
    "elektrisch_hoog_gas",
    "elektrisch_laag_gas",
]

# CBS reports stadsverwarming as n.a. for buurten/gemeenten with no district
# heating. n.a. genuinely means 0% — fill with gemeente value first, then 0.
_GEMEENTE_VAL_THEN_ZERO_COLS = [
    "stadsverwarming_hoog_gas",
    "stadsverwarming_laag_gas",
    "stadsverwarming_zonder_gas",
]

# Business sub-categories: fill as round(buurt_total × gemeente_fraction).
_BEDRIJF_TOTAAL = "bedrijfsvestigingen_totaal"
_BEDRIJF_CATS = [
    "a_landbouw_bosbouw_en_visserij",
    "bf_nijverheid_en_energie",
    "gi_handel_en_horeca",
    "hj_vervoer_informatie_en_communicatie",
    "kl_financiele_diensten_onroerend_goed",
    "mn_zakelijke_dienstverlening",
    "oq_overheid_onderwijs_en_zorg",
    "ru_cultuur_recreatie_overige_diensten",
]

# _BEDRIJF_CATS column -> the matching 8-group code used by
# make_energieverbruik_sector_csv.py (elec_verbruik_<groep> / gas_verbruik_<groep>).
_BEDRIJF_COL_TO_GROEP = {
    "a_landbouw_bosbouw_en_visserij":         "a",
    "bf_nijverheid_en_energie":               "bf",
    "gi_handel_en_horeca":                    "gi",
    "hj_vervoer_informatie_en_communicatie":  "hj",
    "kl_financiele_diensten_onroerend_goed":  "kl",
    "mn_zakelijke_dienstverlening":           "mn",
    "oq_overheid_onderwijs_en_zorg":          "oq",
    "ru_cultuur_recreatie_overige_diensten":  "ru",
}

# Columns from external datasets (solar, EV, bedrijfswagens) with no gemeente fallback → -99999.
_MISSING_SENTINEL_PREFIXES = ("solar_", "ev_", "bestelautos_", "vrachtautos_")


def _laad_gemeente_kwb(jaar: int, haal_kwb_fn) -> pd.DataFrame | None:
    """
    Download CBS kerncijfers at gemeente level for jaar.
    Returns a DataFrame indexed by gemeente codering (e.g. 'GM0034'),
    with NaN preserved (ontbrekende_waarde=None) so we can use real gaps.
    """
    try:
        log.info("  CBS gemeente-kerncijfers %d ophalen (voor fill)...", jaar)
        df = haal_kwb_fn(
            jaar,
            mapping_bestand=str(_MAPPING),
            alleen_buurten=False,
            ontbrekende_waarde=None,
        )
        gem = df[df["soort_regio"] == "Gemeente"].copy()
        if gem.empty:
            log.warning("  Geen gemeente-rijen ontvangen van CBS voor jaar %d", jaar)
            return None
        gem = gem.set_index("codering")
        log.info("  CBS gemeente %d: %d gemeenten geladen", jaar, len(gem))
        return gem
    except Exception as exc:
        log.warning("  CBS gemeente data niet beschikbaar voor jaar %d: %s", jaar, exc)
        return None


def _vul_ontbrekende_waarden(
    df: pd.DataFrame,
    gemeente_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Fill missing / sentinel numeric values in the merged buurten DataFrame.

    CBS buurt data arrives with NaN already replaced by -99999 (from haal_kwb).
    We undo that sentinel for the share/count columns and fill from CBS gemeente
    data matched on GM code (BU00340000 → GM0034).

      1. Share / average columns → CBS gemeente value, then -99999.
      1b. Stadsverwarming → CBS gemeente value, then 0 (n.a. = no district heating).
      2. Business sub-categories → round(buurt_total × gemeente_fraction).
      3. Solar and EV columns → -99999 sentinel (no gemeente fallback available).

    Also adds a boolean column 'data_from_mun_average': True when one or more
    share / business columns were filled from the gemeente value because the
    neighbourhood-level data was missing.
    """
    df = df.copy()

    # Track rows that get any column filled from gemeente data
    filled_from_gemeente = pd.Series(False, index=df.index)

    # GM code derived from buurtcode: BU00340000 → GM0034
    if gemeente_df is not None and "codering" in df.columns:
        gm_codes = "GM" + df["codering"].str[2:6]
    else:
        gm_codes = None
        if gemeente_df is not None:
            log.warning("  'codering' kolom niet gevonden — gemeente fill overgeslagen.")

    def _fill_from_gemeente(col: str, zero_fallback: bool = False) -> None:
        if col not in df.columns:
            return
        # Normalise: string "n.a." and numeric -99999 both → NaN
        df[col] = pd.to_numeric(df[col], errors="coerce")
        df[col] = df[col].replace(_MISSING, float("nan"))

        n_before = int(df[col].isna().sum())
        if n_before == 0:
            return

        if gm_codes is not None and gemeente_df is not None and col in gemeente_df.columns:
            gem_col = pd.to_numeric(gemeente_df[col], errors="coerce")
            gem_vals = gm_codes.map(gem_col)
            was_missing = df[col].isna()
            df[col] = df[col].fillna(gem_vals)
            # Mark rows that were actually filled (not still NaN after fill)
            filled_from_gemeente[was_missing & df[col].notna()] = True

        n_after = int(df[col].isna().sum())
        fallback = "0" if zero_fallback else str(_MISSING)
        log.info(
            "    %s: %d missing → %d gevuld met gemeente-waarde, %d resteren → %s",
            col, n_before, n_before - n_after, n_after, fallback,
        )
        df[col] = df[col].fillna(0 if zero_fallback else _MISSING)

    # 1. Share / average columns → gemeente value, then -99999
    for col in _GEMEENTE_VAL_COLS:
        _fill_from_gemeente(col, zero_fallback=False)

    # 1b. Stadsverwarming → gemeente value, then 0
    for col in _GEMEENTE_VAL_THEN_ZERO_COLS:
        _fill_from_gemeente(col, zero_fallback=True)

    # 2. Business sub-categories → round(buurt_total × gemeente_fraction)
    if _BEDRIJF_TOTAAL in df.columns:
        df[_BEDRIJF_TOTAAL] = pd.to_numeric(df[_BEDRIJF_TOTAAL], errors="coerce")
        df[_BEDRIJF_TOTAAL] = df[_BEDRIJF_TOTAAL].replace(_MISSING, float("nan"))

        if gm_codes is not None and gemeente_df is not None and _BEDRIJF_TOTAAL in gemeente_df.columns:
            gem_total_col = pd.to_numeric(gemeente_df[_BEDRIJF_TOTAAL], errors="coerce")
            gem_total = gm_codes.map(gem_total_col)
        else:
            gem_total = None

        for col in _BEDRIJF_CATS:
            if col not in df.columns:
                continue
            df[col] = pd.to_numeric(df[col], errors="coerce")
            df[col] = df[col].replace(_MISSING, float("nan"))
            n_before = int(df[col].isna().sum())
            if n_before == 0:
                continue

            if gem_total is not None and col in gemeente_df.columns:
                gem_cat_col = pd.to_numeric(gemeente_df[col], errors="coerce")
                gem_cat = gm_codes.map(gem_cat_col)
                gem_frac = gem_cat / gem_total.replace(0, float("nan"))
                mask = df[col].isna() & df[_BEDRIJF_TOTAAL].notna()
                df.loc[mask, col] = (df.loc[mask, _BEDRIJF_TOTAAL] * gem_frac[mask]).round()
                filled_from_gemeente[mask & df[col].notna()] = True

            n_after = int(df[col].isna().sum())
            log.info(
                "    %s: %d missing → %d gevuld via gemeente-aandeel, %d resteren",
                col, n_before, n_before - n_after, n_after,
            )
            df[col] = df[col].fillna(_MISSING)

    # 3. External dataset columns with no gemeente fallback
    for col in df.columns:
        if any(col.startswith(p) for p in _MISSING_SENTINEL_PREFIXES):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(_MISSING)

    n_filled = int(filled_from_gemeente.sum())
    log.info(
        "  data_from_mun_average: %d buurten (%.1f%%) hadden ≥1 kolom gevuld via gemeente-waarde",
        n_filled, n_filled / len(df) * 100 if len(df) else 0,
    )
    df["data_from_mun_average"] = filled_from_gemeente

    return df


_EV_SCENARIOS = ["low", "middle", "high", "realization"]
_EV_JAREN = [2025, 2030, 2040, 2050]


def _rapporteer_ev_overschrijdingen(merged: pd.DataFrame, jaar: int) -> None:
    """
    Data-kwaliteitsrapport: lijst elke (buurt, categorie, scenario/jaar)-combinatie
    waar het ElaadNL EV/PHEV-aantal de bijbehorende total-kolom overschrijdt
    (personenautos_totaal voor auto's, bestelautos_totaal voor bestelauto's,
    vrachtautos_totaal voor vrachtauto's).

    NeighborhoodRowParser gebruikt VehicleFleet.ofKnownAtLeastTotal() (niet de
    strengere ofKnown()), dus dit crasht het laadproces niet meer — maar het
    betekent wel dat de total voor die buurt/categorie is opgehoogd tot het
    EV-aantal, wat het waard is om te kunnen controleren.

    Schrijft processed/ev_overschrijdingen_buurten_{jaar}.csv (één rij per
    overschrijding, overschreven bij elke run — zelfde conventie als de
    buurten-CSV zelf) en logt een samenvatting per categorie.
    """
    if "codering" not in merged.columns:
        return

    def _numeriek(naam: str) -> pd.Series | None:
        if naam not in merged.columns:
            return None
        return pd.to_numeric(merged[naam].replace(FILL_UNMATCHED, pd.NA), errors="coerce")

    totalen = {
        "auto": _numeriek("personenautos_totaal"),
        "bestelauto": _numeriek("bestelautos_totaal"),
        "vrachtauto": _numeriek("vrachtautos_totaal"),
    }

    rijen: list[dict] = []
    for s in _EV_SCENARIOS:
        for y in _EV_JAREN:
            bev = _numeriek(f"ev_car_bev_{s}_{y}")
            phev = _numeriek(f"ev_car_phev_{s}_{y}")
            van = _numeriek(f"ev_van_{s}_{y}")
            truck = _numeriek(f"ev_truck_{s}_{y}")

            if bev is not None or phev is not None:
                bekend_auto = (bev.fillna(0) if bev is not None else 0) + \
                              (phev.fillna(0) if phev is not None else 0)
            else:
                bekend_auto = None
            bekend_per_categorie = {"auto": bekend_auto, "bestelauto": van, "vrachtauto": truck}

            for categorie, bekend in bekend_per_categorie.items():
                totaal = totalen[categorie]
                if bekend is None or totaal is None:
                    continue
                overschrijding = bekend.fillna(0) > totaal
                for idx in merged.index[overschrijding.fillna(False)]:
                    rijen.append({
                        "codering": merged.at[idx, "codering"],
                        "categorie": categorie,
                        "scenario": s,
                        "jaar": y,
                        "ev_aantal": bekend.at[idx],
                        "totaal": totaal.at[idx],
                    })

    if not rijen:
        log.info("  EV-overschrijdingscheck %d: geen enkele buurt/scenario/jaar waar EV-aantal > total.", jaar)
        return

    df_over = pd.DataFrame(rijen)
    samenvatting = df_over.groupby("categorie").size().to_dict()
    out_path = PROCESSED_DIR / f"ev_overschrijdingen_buurten_{jaar}.csv"
    tmp = out_path.with_suffix(".tmp.csv")
    df_over.to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
    tmp.replace(out_path)
    log.warning(
        "  EV-overschrijdingscheck %d: %d (buurt, scenario, jaar)-combinaties waar het EV/PHEV-aantal "
        "de total overschrijdt (per categorie: %s) — deze buurten hebben een opgehoogde total "
        "(VehicleFleet.ofKnownAtLeastTotal). Detail: %s",
        jaar, len(df_over), samenvatting, out_path.name,
    )


def _vind_sector_kolom(columns, carrier: str, groep: str) -> str | None:
    """Find the actual column name for one carrier ('elec'/'gas') + SBI-groep,
    whatever its unit suffix — e.g. groep='a' matches 'elec_verbruik_a_kwh' or
    'elec_verbruik_a_tj', depending on which source produced the file."""
    prefix = f"{carrier}_verbruik_{groep}_"
    for c in columns:
        if c.startswith(prefix):
            return c
    return None


def _verdeel_energieverbruik_sector(
    df: pd.DataFrame,
    energie_df: pd.DataFrame | None,
    jaar: int,
) -> pd.DataFrame:
    """
    Assign each municipality's sector electricity/gas demand (make_energieverbruik_sector_csv.py)
    down to its neighbourhoods, in proportion to each buurt's share of that sector's
    company locations within the gemeente.

    Example: if a gemeente has 100 company locations of SBI-group "a" spread across
    its buurten, and one buurt has 10 of them, that buurt gets 10% of the gemeente's
    "a" electricity and gas demand.

    Rules (as specified):
      - A buurt with -99999 for a sector's company-location count does not count
        towards the gemeente total, and its demand for that sector is 0.
      - The gemeente's entire demand for a sector must end up assigned to its
        buurten. If a gemeente has zero buurten with a valid (non -99999) company
        count for a sector, there is no basis to allocate proportionally — the
        demand is split evenly across all of that gemeente's buurten instead
        (flagged via 'energieverbruik_sector_gelijk_verdeeld') so no demand is lost.

    Must run AFTER _vul_ontbrekende_waarden(), so the -99999 sentinel reflects
    "truly no data anywhere" rather than a gap that could have been filled from
    the gemeente average.
    """
    df = df.copy()

    if energie_df is None or energie_df.empty or "codering" not in df.columns:
        for groep in _BEDRIJF_COL_TO_GROEP.values():
            df[f"elec_verbruik_{groep}"] = _MISSING
            df[f"gas_verbruik_{groep}"] = _MISSING
        df["energieverbruik_sector_gelijk_verdeeld"] = False
        return df

    beschikbare_jaren = sorted(energie_df["jaar"].dropna().unique().tolist())
    target_jaar = min(beschikbare_jaren, key=lambda y: abs(y - jaar))
    if target_jaar != jaar:
        log.warning(
            "  Energieverbruik-sector: jaar %d niet beschikbaar, gebruik %d als meest nabije",
            jaar, target_jaar,
        )
    energie_jaar = energie_df[energie_df["jaar"] == target_jaar].set_index("gemeentecode")

    gm_code = "GM" + df["codering"].str[2:6]
    n_buurten_per_gm = gm_code.groupby(gm_code).transform("size")
    fallback_flag = pd.Series(False, index=df.index)

    for bedrijf_col, groep in _BEDRIJF_COL_TO_GROEP.items():
        waarden = pd.to_numeric(df.get(bedrijf_col), errors="coerce")
        valid_mask = waarden.notna() & (waarden != _MISSING)
        valid_vals = waarden.where(valid_mask)

        total_valid = valid_vals.groupby(gm_code).transform(lambda s: s.sum(min_count=1))
        n_valid = valid_mask.groupby(gm_code).transform("sum")

        share = pd.Series(0.0, index=df.index)
        proportioneel_mask = valid_mask & (total_valid > 0)
        share[proportioneel_mask] = valid_vals[proportioneel_mask] / total_valid[proportioneel_mask]

        # No basis to allocate proportionally. Two distinct cases, both of which
        # leave every share at 0 and would silently drop the gemeente's entire
        # demand for this sector:
        #   n_valid == 0      — every buurt is -99999, i.e. no data anywhere;
        #   total_valid == 0  — every buurt reports a *valid* count of zero.
        # The second is easy to miss (the counts are present and legal, they just
        # sum to nothing) and did drop demand: Renkum 2023 lost its entire
        # 1,098,000 kWh of SBI-A electricity, and 8 gemeenten together lost
        # 2.3 GWh, which broke the reconciliation against the RES control totals.
        # Split evenly in both cases, so the invariant in this docstring — all
        # demand ends up assigned — actually holds.
        geen_data_mask = (n_valid == 0) | (total_valid.fillna(0) <= 0)
        if geen_data_mask.any():
            share[geen_data_mask] = 1.0 / n_buurten_per_gm[geen_data_mask]
            fallback_flag |= geen_data_mask

        elec_col = _vind_sector_kolom(energie_jaar.columns, "elec", groep)
        gas_col = _vind_sector_kolom(energie_jaar.columns, "gas", groep)

        elec_col = elec_col or f"elec_verbruik_{groep}"
        gas_col = gas_col or f"gas_verbruik_{groep}"
        if elec_col in energie_jaar.columns:
            df[elec_col] = share * gm_code.map(energie_jaar[elec_col])
        else:
            df[elec_col] = pd.NA
        if gas_col in energie_jaar.columns:
            df[gas_col] = share * gm_code.map(energie_jaar[gas_col])
        else:
            df[gas_col] = pd.NA

        df[elec_col] = pd.to_numeric(df[elec_col], errors="coerce").fillna(_MISSING)
        df[gas_col] = pd.to_numeric(df[gas_col], errors="coerce").fillna(_MISSING)

    n_fallback = int(fallback_flag.sum())
    if n_fallback:
        log.warning(
            "  Energieverbruik-sector: %d buurten kregen ≥1 sector gelijk verdeeld "
            "(gemeente had geen enkele buurt met bekend bedrijfsvestigingenaantal voor die sector)",
            n_fallback,
        )
    df["energieverbruik_sector_gelijk_verdeeld"] = fallback_flag

    return df


def maak_csv(
    jaar: int,
    haal_kwb_fn,
    verwarming_df: pd.DataFrame | None = None,
    elaadnl_wide: pd.DataFrame | None = None,
    warmtetransitie_df: pd.DataFrame | None = None,
    solar_df: pd.DataFrame | None = None,
    energie_df: pd.DataFrame | None = None,
    bedrijfswagens_df: pd.DataFrame | None = None,
    capaciteitskaart_df: pd.DataFrame | None = None,
) -> bool:
    """Generate enriched kerncijfers_buurten_met_geometrie_{jaar}.csv. Returns True on success."""
    log.info("  --- Jaar %d ---", jaar)

    geo_df = laad_geometrie(jaar)

    log.info("  CBS OData kerncijfers %d ophalen...", jaar)
    kwb = haal_kwb_fn(jaar, mapping_bestand=str(_MAPPING))
    log.info("  Kerncijfers %d: %d buurten, %d kolommen", jaar, len(kwb), len(kwb.columns))

    merged = kwb.merge(
        geo_df.rename(columns={"buurtcode": "codering"}),
        on="codering",
        how="left",
    )

    n_met    = merged["wkt_geometry"].notna().sum()
    n_zonder = merged["wkt_geometry"].isna().sum()
    log.info("  Na koppeling: %d buurten met geometrie, %d zonder", n_met, n_zonder)
    if n_zonder > 0:
        codes_zonder = merged.loc[merged["wkt_geometry"].isna(), "codering"].tolist()
        preview = codes_zonder[:10]
        suffix  = f" (+{n_zonder - 10} meer)" if n_zonder > 10 else ""
        log.warning("  Zonder geometrie (eerste 10): %s%s", preview, suffix)

    # --- Merge verwarmingsinstallaties (heating type fractions) ---
    if verwarming_df is not None:
        available_years = sorted(verwarming_df["jaar"].dropna().unique().tolist())
        target_year = min(available_years, key=lambda y: abs(y - jaar))
        if target_year != jaar:
            log.warning(
                "  Verwarmingsinstallaties: jaar %d niet beschikbaar, gebruik %d als meest nabije",
                jaar, target_year,
            )
        vw = (
            verwarming_df[verwarming_df["jaar"] == target_year]
            .drop(columns=["jaar"])
            .rename(columns={"buurtcode": "codering"})
        )
        merged = merged.merge(vw, on="codering", how="left")
        log.info(
            "  Verwarming %d toegevoegd: %d kolommen, %d buurten met data",
            target_year, len(vw.columns) - 1, vw["codering"].nunique(),
        )

    # --- Merge ElaadNL EV prognoses (wide format) ---
    if elaadnl_wide is not None:
        merged = merged.merge(elaadnl_wide, on="codering", how="left")
        n_ev_cols = len(elaadnl_wide.columns) - 1
        n_matched = merged[elaadnl_wide.columns[1]].notna().sum()
        log.info("  ElaadNL toegevoegd: %d EV-kolommen, %d buurten met data", n_ev_cols, n_matched)

    # --- Merge WarmteTransitie per-gebied (spatial match, one row per buurt per year) ---
    if warmtetransitie_df is not None:
        wt_jaar = warmtetransitie_df[warmtetransitie_df["jaar"] == str(jaar)].drop(columns=["jaar"])
        merged = merged.merge(wt_jaar, on="codering", how="left")
        wtp_cols = [c for c in wt_jaar.columns if c != "codering"]
        n_matched = merged[wtp_cols[0]].notna().sum()
        log.info(
            "  WarmteTransitie toegevoegd: %d kolommen, %d buurten met gepubliceerd plan",
            len(wtp_cols), n_matched,
        )

    # --- Merge CBS Zonnestroom solar (nearest available CBS year) ---
    if solar_df is not None:
        available_years = sorted(solar_df["jaar"].dropna().unique().tolist())
        target_year = min(available_years, key=lambda y: abs(y - jaar))
        if target_year != jaar:
            log.warning(
                "  Solar: jaar %d niet beschikbaar, gebruik %d als meest nabije",
                jaar, target_year,
            )
        sol_jaar = (
            solar_df[solar_df["jaar"] == target_year]
            .drop(columns=["jaar"])
        )
        merged = merged.merge(sol_jaar, on="codering", how="left")
        solar_cols = [c for c in sol_jaar.columns if c != "codering"]
        n_matched = merged[solar_cols[0]].notna().sum()
        log.info(
            "  Solar %d toegevoegd: %d kolommen, %d buurten met data",
            target_year, len(solar_cols), n_matched,
        )

    # --- Merge bedrijfswagens estimate (bestelauto/vrachtauto totals per buurt) ---
    if bedrijfswagens_df is not None:
        merged = merged.merge(bedrijfswagens_df, on="codering", how="left")
        n_matched = merged["bestelautos_totaal"].notna().sum()
        log.info("  Bedrijfswagens toegevoegd: %d buurten met schatting", n_matched)

    # --- Merge capaciteitskaart link (dominant voedingsgebied_id per buurt) ---
    if capaciteitskaart_df is not None:
        merged = merged.merge(capaciteitskaart_df, on="codering", how="left")
        n_matched = merged["voedingsgebied_id"].notna().sum()
        log.info("  Capaciteitskaart toegevoegd: %d buurten gekoppeld aan een voedingsgebied", n_matched)

    # --- Data-quality check: ElaadNL EV/PHEV counts vs. the vehicle-category totals ---
    _rapporteer_ev_overschrijdingen(merged, jaar)

    gemeente_df = _laad_gemeente_kwb(jaar, haal_kwb_fn)
    merged = _vul_ontbrekende_waarden(merged, gemeente_df=gemeente_df)

    # --- Sector electricity/gas demand, allocated to buurten by company-location share ---
    merged = _verdeel_energieverbruik_sector(merged, energie_df, jaar)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"kerncijfers_buurten_met_geometrie_{jaar}.csv"
    tmp = out.with_suffix(".tmp.csv")
    try:
        merged.fillna(FILL_UNMATCHED).to_csv(tmp, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
        tmp.replace(out)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    size_mb = out.stat().st_size / 1024**2
    log.info(
        "  Opgeslagen: %s  (%d rijen, %d kolommen, %.1f MB)",
        out.name, len(merged), len(merged.columns), size_mb,
    )

    # Copy directly into data_Generic/ for the years the model actually uses,
    # under the filename the model expects (year before "met_geometrie" — the
    # reverse order from the processed/ copy above). data_Generic/ root is kept
    # to only the files used directly by the model; this is that file for buurten.
    if jaar in _STABLE_COPY_YEARS:
        model_path = DATA_GENERIC / f"kerncijfers_buurten_{jaar}_met_geometrie.csv"
        model_tmp = model_path.with_suffix(".tmp.csv")
        try:
            merged.fillna(FILL_UNMATCHED).to_csv(model_tmp, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
            model_tmp.replace(model_path)
            log.info("  Kopie voor model: %s", model_path.name)
        except Exception as exc:
            model_tmp.unlink(missing_ok=True)
            log.warning("  Kon %s niet schrijven: %s", model_path.name, exc)

    # --- Excel export: Drechtsteden municipalities only, no WKT geometry column ---
    _schrijf_drechtsteden_excel(merged, jaar)

    return True


_DRECHTSTEDEN_GEMEENTEN = {
    "Alblasserdam",
    "Dordrecht",
    "Hardinxveld-Giessendam",
    "Hendrik-Ido-Ambacht",
    "Papendrecht",
    "Sliedrecht",
    "Zwijndrecht",
}


def _schrijf_drechtsteden_excel(merged: pd.DataFrame, jaar: int) -> None:
    """Write a filtered Excel file for the Drechtsteden municipalities."""
    if "gemeentenaam" not in merged.columns:
        log.warning("  Excel export overgeslagen: kolom 'gemeentenaam' niet gevonden.")
        return

    mask = merged["gemeentenaam"].isin(_DRECHTSTEDEN_GEMEENTEN)
    subset = merged[mask].copy()

    if subset.empty:
        log.warning("  Excel export: geen buurten gevonden voor Drechtsteden gemeenten in jaar %d.", jaar)
        return

    # Drop WKT geometry — too large and not useful in Excel
    cols = [c for c in subset.columns if c != "wkt_geometry"]
    subset = subset[cols].fillna(FILL_UNMATCHED)

    xlsx = _OUT_DIR / f"kerncijfers_buurten_drechtsteden_{jaar}.xlsx"
    tmp = xlsx.with_suffix(".tmp.xlsx")
    try:
        subset.to_excel(tmp, index=False, sheet_name=str(jaar))
        try:
            tmp.replace(xlsx)
        except OSError:
            # File is open in Excel (Windows file lock) — write directly instead
            log.warning(
                "  Excel %s is geopend door een ander programma. "
                "Schrijf direct naar het doelbestand (sluit Excel voor een atomaire schrijfoperatie).",
                xlsx.name,
            )
            tmp.unlink(missing_ok=True)
            subset.to_excel(xlsx, index=False, sheet_name=str(jaar))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    log.info(
        "  Excel Drechtsteden: %s  (%d buurten, %d kolommen)",
        xlsx.name, len(subset), len(subset.columns),
    )


def main(jaren=None) -> bool:
    """Generate buurten CSVs for all requested years. Returns True if all succeeded."""
    try:
        from cbs_kwb import haal_kwb
    except ImportError as exc:
        log.error("Kan cbs_kwb niet importeren: %s", exc)
        log.error("Installeer ontbrekende afhankelijkheid: pip install cbsodata")
        return False

    if jaren is None:
        jaren = list(CBS_YEARS)

    # Load supporting datasets once — all are optional (skip if not yet generated)
    verwarming_df       = _laad_verwarming()
    elaadnl_wide        = _laad_elaadnl_wide()
    warmtetransitie_df  = _laad_warmtetransitie()
    solar_df            = _laad_solar()
    energie_df          = _laad_energieverbruik_sector()

    all_ok = True
    t0 = time.monotonic()

    for jaar in jaren:
        try:
            ok = maak_csv(
                jaar, haal_kwb,
                verwarming_df=verwarming_df,
                elaadnl_wide=elaadnl_wide,
                warmtetransitie_df=warmtetransitie_df,
                solar_df=solar_df,
                energie_df=energie_df,
                bedrijfswagens_df=_laad_bedrijfswagens(jaar),
                capaciteitskaart_df=_laad_capaciteitskaart(jaar),
            )
        except FileNotFoundError as exc:
            log.error("FOUT jaar %d: %s", jaar, exc)
            ok = False
        except ValueError as exc:
            msg = str(exc)
            if "komt niet voor in de mapping" in msg or "niet voor" in msg:
                log.warning(
                    "Jaar %d overgeslagen: CBS OData statistieken nog niet beschikbaar (%s)",
                    jaar, msg,
                )
                ok = True  # not a pipeline failure — data simply not published yet
            else:
                log.error("FOUT jaar %d: %s", jaar, exc, exc_info=True)
                ok = False
        except Exception as exc:
            log.error("FOUT jaar %d: %s", jaar, exc, exc_info=True)
            ok = False
        if not ok:
            all_ok = False

    elapsed = time.monotonic() - t0
    log.info("Buurten CSV generatie klaar in %.0fs", elapsed)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    jaren = [int(a) for a in sys.argv[1:]] or None
    sys.exit(0 if main(jaren) else 1)
