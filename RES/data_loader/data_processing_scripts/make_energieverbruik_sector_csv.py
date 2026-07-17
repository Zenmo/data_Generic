"""Download electricity + gas demand per SBI business sector, per municipality.

Two sources, tried in order (same output contract either way, so nothing
downstream needs to know which one actually ran):

  1. CBS StatLine table 82538NED — "Levering aardgas, elektriciteit via
     openbaar net; bedrijven, SBI2008, regio". Public, CC-BY 4.0, same CBS
     OData infrastructure used elsewhere in this pipeline (cbs_kwb.py,
     make_solar_csv.py). As of when this script was written, CBS's own server
     reports this specific table as "TijdelijkNietBeschikbaar" (confirmed via
     both the OData API and the public StatLine website) — this is a real
     outage on CBS's side, not a wrong table ID or a client-side problem; no
     successor table with the same region+SBI-sector breakdown exists.
       https://opendata.cbs.nl/statline/#/CBS/nl/dataset/82538NED/table

  2. Klimaatmonitor Open Data Service (ODS) — a Swing/Jive OData v4 API,
     verified working end-to-end. Requires a personal API key (free, requested
     via the contact form on klimaatmonitor.databank.nl) sent as the `apikey`
     HTTP header — NOT a query parameter, which is the mistake that makes this
     API look unauthenticated-only at first.
       Base:   https://klimaatmonitor.databank.nl/jiveservices/odata
       Manual: https://klimaatmonitor.databank.nl/content/handleiding-open-data-service
     The key is read from secrets.local.json (gitignored — never commit it):
       {"klimaatmonitor_apikey": "<your key>"}
     Klimaatmonitor publishes one variable per individual SBI letter (vbrze_*
     for electricity in kWh, vbrzg_* for gas in m3) rather than CBS's combined
     branch dimension — verified live against klimaatmonitor.databank.nl on
     2026-07-07. Note vbrzg_d (gas for SBI D, Energievoorziening) genuinely
     does not exist on their side — energy-sector gas use isn't tracked, so
     the "bf" gas group sums B/C/E/F only.

Both sources publish per SBI-letter or SBI-branch figures; this script
aggregates them to the same 8-group scheme used elsewhere in this pipeline
(a / bf / gi / hj / kl / mn / oq / ru — see mapping_kolomnamen_CBS.xlsx and
make_buurten_csv.py's company-location columns):

    a  = A                              landbouw, bosbouw en visserij
    bf = B, C, D, E, F                  nijverheid en energie
    gi = G, I                           handel en horeca
    hj = H, J                           vervoer, informatie en communicatie
    kl = K, L                           financiële diensten, onroerend goed
    mn = M, N                           zakelijke dienstverlening
    oq = O, P, Q                        overheid, onderwijs, zorg
    ru = R, S, T, U                     cultuur, recreatie, overige diensten

For CBS, the branch dimension's category Keys are matched to this scheme by
parsing the leading SBI letter(s) out of each category's Title at runtime
(same defensive pattern as cbs_kwb.py's _resolve_keys, which handles CBS's
habit of shifting key suffixes between releases) rather than hardcoding
dimension/column keys. For Klimaatmonitor, the per-letter variable codes are
hardcoded below (_KM_ELEC_VARS / _KM_GAS_VARS) since they were verified live
and Klimaatmonitor's ExternalCodes are stable identifiers, not the
shifting ordinal suffixes CBS uses.

This is a library module, not a standalone pipeline phase: it writes nothing
to disk. `haal_energieverbruik_sector(jaren)` returns the finalized DataFrame
directly, for make_gemeenten_potentie_csv.py to merge in-memory straight into
kerncijfers_gemeenten_<jaar>[_met_geometrie].csv — there is no separate
energieverbruik_sector_gemeenten_*.csv file. make_buurten_csv.py in turn reads
the sector-energy columns back out of the already-generated
kerncijfers_gemeenten_<jaar>.csv (see _laad_energieverbruik_sector() there),
so `--gemeenten-potentie` must run before `--buurten` for the buurten CSV to
get sector-energy columns.

Returned columns: gemeentecode, jaar,
         elec_verbruik_a_<unit>, elec_verbruik_bf_<unit>, ..., elec_verbruik_ru_<unit>,
         gas_verbruik_a_<unit>,  gas_verbruik_bf_<unit>,  ..., gas_verbruik_ru_<unit>,
         elec_verbruik_totaal_<unit>, gas_verbruik_totaal_<unit>  (sum across all 8 groups)
         elec_eenheid, gas_eenheid,           (the same unit, spelled out; belt-and-braces)
         energie_bron, energie_bron_url       (whichever source actually succeeded)

The <unit> suffix is whatever the source that actually ran uses — verified live
against Klimaatmonitor on 2026-07-07: electricity in kWh, gas in m³ (NOT TJ —
no per-letter TJ variant exists there). CBS 82538NED's real unit is unknown
(the table has been unreachable since before this was written); if the two
sources ever report different units, column names will differ between runs —
this is intentional, not a bug: downstream code (make_buurten_csv.py,
make_gemeenten_potentie_csv.py, MunicipalityImporter.java) looks these columns
up by prefix (e.g. "elec_verbruik_a_"), never by a hardcoded full name, so it
is robust to whichever unit shows up.

Run standalone (prints a summary, writes nothing):
    python make_energieverbruik_sector_csv.py [2023 [2024]]
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from config import REQUEST_TIMEOUT

log = logging.getLogger(__name__)

CBS_TABLE_ID = "82538NED"
CBS_SOURCE_NAME = (
    "CBS StatLine 82538NED — Levering aardgas, elektriciteit via openbaar net; "
    "bedrijven, SBI2008, regio"
)
CBS_SOURCE_URL = "https://opendata.cbs.nl/statline/#/CBS/nl/dataset/82538NED/table"

# Only municipality-level rows are kept (region codes starting with "GM").
_REGIO_PREFIX = "GM"

# SBI letter(s) -> our 8-group scheme (see module docstring for the full table).
_LETTER_TO_GROEP = {}
for _letter in "A":
    _LETTER_TO_GROEP[_letter] = "a"
for _letter in "BCDEF":
    _LETTER_TO_GROEP[_letter] = "bf"
for _letter in "GI":
    _LETTER_TO_GROEP[_letter] = "gi"
for _letter in "HJ":
    _LETTER_TO_GROEP[_letter] = "hj"
for _letter in "KL":
    _LETTER_TO_GROEP[_letter] = "kl"
for _letter in "MN":
    _LETTER_TO_GROEP[_letter] = "mn"
for _letter in "OPQ":
    _LETTER_TO_GROEP[_letter] = "oq"
for _letter in "RSTU":
    _LETTER_TO_GROEP[_letter] = "ru"

_GROEPEN = ["a", "bf", "gi", "hj", "kl", "mn", "oq", "ru"]

DEFAULT_YEARS = [2023, 2024]


def _unit_slug(unit: str) -> str:
    """Turn a unit string (e.g. 'kWh', 'm3', 'TJ') into a safe column-name suffix."""
    return re.sub(r"[^a-z0-9]", "", str(unit).lower()) or "onbekend"


def _finaliseer_kolommen(wide: pd.DataFrame, elec_unit: str, gas_unit: str) -> pd.DataFrame:
    """Bake the actual (verified) unit into each elec_/gas_ column name — CBS and
    Klimaatmonitor may report different units, so the suffix is whatever that
    specific run's source actually used, not a hardcoded assumption. Also adds
    elec_verbruik_totaal_<unit> / gas_verbruik_totaal_<unit>, the sum across all
    8 SBI-groups, so a municipality's grand total can be cross-checked against
    the sum of its neighbourhoods' shares in the buurten CSV.

    Downstream code must look up these columns by prefix (e.g.
    "elec_verbruik_a_"), never by the exact old name without a unit suffix,
    since the unit — and therefore the suffix — can differ between runs
    depending on which source (CBS or Klimaatmonitor) actually succeeded.
    """
    elec_slug, gas_slug = _unit_slug(elec_unit), _unit_slug(gas_unit)
    wide = wide.copy()

    elec_cols_oud = [f"elec_verbruik_{g}" for g in _GROEPEN]
    gas_cols_oud = [f"gas_verbruik_{g}" for g in _GROEPEN]
    wide["elec_verbruik_totaal_" + elec_slug] = wide[elec_cols_oud].sum(axis=1, min_count=1)
    wide["gas_verbruik_totaal_" + gas_slug] = wide[gas_cols_oud].sum(axis=1, min_count=1)

    rename = {f"elec_verbruik_{g}": f"elec_verbruik_{g}_{elec_slug}" for g in _GROEPEN}
    rename.update({f"gas_verbruik_{g}": f"gas_verbruik_{g}_{gas_slug}" for g in _GROEPEN})
    return wide.rename(columns=rename)


# ---------------------------------------------------------------------------
# Runtime schema discovery
# ---------------------------------------------------------------------------

def _discover_schema() -> dict | None:
    """Inspect DataProperties for 82538NED and return discovered column keys.

    Returns a dict with keys: branch, region, period, elec, gas — or None if
    the table (or the properties needed) could not be found."""
    import cbsodata

    try:
        props = cbsodata.get_meta(CBS_TABLE_ID, "DataProperties")
    except Exception as exc:
        log.error("  Kon DataProperties van %s niet ophalen: %s", CBS_TABLE_ID, exc)
        return None

    props_df = pd.DataFrame(props)
    log.info("  %s: %d eigenschappen gevonden", CBS_TABLE_ID, len(props_df))

    def _find(keywords: list[str], types: list[str] | None = None) -> str | None:
        cand = props_df
        if types is not None and "Type" in cand.columns:
            cand = cand[cand["Type"].isin(types)]
        for kw in keywords:
            hit = cand[cand["Title"].str.contains(kw, case=False, na=False)]
            if not hit.empty:
                key = hit.iloc[0]["Key"]
                log.info("    '%s' -> Key=%s (Title=%s)", kw, key, hit.iloc[0]["Title"])
                return key
        return None

    branch = _find(["SBI", "bedrijfstak", "branche"], types=["Dimension", "TopicGroup"])
    region = _find(["regio"], types=["Dimension"])
    period = _find(["perioden"], types=["Dimension"])
    elec = _find(["elektriciteit"], types=["Topic"])
    gas = _find(["aardgas"], types=["Topic"])

    missing = [n for n, v in [("branch", branch), ("region", region), ("period", period),
                              ("elec", elec), ("gas", gas)] if v is None]
    if missing:
        log.error(
            "  Kon niet alle benodigde kolommen herkennen in %s (ontbreekt: %s). "
            "Beschikbare eigenschappen: %s",
            CBS_TABLE_ID, missing, props_df[["Key", "Title", "Type"]].to_dict("records"),
        )
        return None

    return {"branch": branch, "region": region, "period": period, "elec": elec, "gas": gas}


def _discover_branch_groups(branch_dim_key: str) -> dict[str, str]:
    """Return {CBS branch category Key -> our groep (a/bf/.../ru)}, by matching
    the leading SBI letter(s) in each category's Title (e.g. 'A Landbouw, ...')."""
    import cbsodata

    cats = pd.DataFrame(cbsodata.get_meta(CBS_TABLE_ID, branch_dim_key))
    mapping: dict[str, str] = {}
    unmapped: list[str] = []

    for _, row in cats.iterrows():
        title = str(row.get("Title", ""))
        m = re.match(r"^([A-U])(?:-([A-U]))?\b", title.strip())
        if not m:
            unmapped.append(title)
            continue
        letter = m.group(1)
        groep = _LETTER_TO_GROEP.get(letter)
        if groep is None:
            unmapped.append(title)
            continue
        mapping[row["Key"]] = groep

    log.info("  Branche-categorieën: %d gemapt naar 8 groepen, %d niet herkend",
              len(mapping), len(unmapped))
    if unmapped:
        log.warning("  Niet-gemapte branche-categorieën (overgeslagen): %s", unmapped)
    return mapping


# ---------------------------------------------------------------------------
# Download + aggregate
# ---------------------------------------------------------------------------

def _download(schema: dict, branch_groups: dict[str, str], jaren: list[int]) -> pd.DataFrame | None:
    import cbsodata

    select = [schema["region"], schema["period"], schema["branch"], schema["elec"], schema["gas"]]
    try:
        raw = pd.DataFrame(cbsodata.get_data(CBS_TABLE_ID, select=select))
    except Exception as exc:
        log.error("  Download van %s mislukt: %s", CBS_TABLE_ID, exc)
        return None

    for col in select:
        if raw[col].dtype == object:
            raw[col] = raw[col].astype(str).str.strip()

    raw = raw[raw[schema["region"]].str.startswith(_REGIO_PREFIX, na=False)].copy()
    raw["groep"] = raw[schema["branch"]].map(branch_groups)
    raw = raw.dropna(subset=["groep"])

    # Period column is typically like "2023" or "2023JJ00" — extract the 4-digit year.
    raw["jaar"] = raw[schema["period"]].astype(str).str.extract(r"(\d{4})").astype("Int64")
    raw = raw[raw["jaar"].isin(jaren)]

    raw = raw.rename(columns={schema["region"]: "gemeentecode",
                               schema["elec"]: "elec", schema["gas"]: "gas"})
    raw["elec"] = pd.to_numeric(raw["elec"], errors="coerce")
    raw["gas"] = pd.to_numeric(raw["gas"], errors="coerce")

    log.info("  %d gemeente-rijen na filtering, jaren=%s", len(raw), sorted(raw["jaar"].dropna().unique().tolist()))
    return raw[["gemeentecode", "jaar", "groep", "elec", "gas"]]


def _pivot_per_groep(raw: pd.DataFrame) -> pd.DataFrame:
    """One row per (gemeentecode, jaar), one elec_verbruik_<groep> / gas_verbruik_<groep>
    column pair per of the 8 groups."""
    elec_wide = raw.pivot_table(
        index=["gemeentecode", "jaar"], columns="groep", values="elec", aggfunc="sum"
    )
    elec_wide.columns = [f"elec_verbruik_{g}" for g in elec_wide.columns]

    gas_wide = raw.pivot_table(
        index=["gemeentecode", "jaar"], columns="groep", values="gas", aggfunc="sum"
    )
    gas_wide.columns = [f"gas_verbruik_{g}" for g in gas_wide.columns]

    wide = elec_wide.join(gas_wide, how="outer").reset_index()

    # Ensure all 8 groups are present as columns even if a group had zero matches.
    for g in _GROEPEN:
        for prefix in ("elec_verbruik_", "gas_verbruik_"):
            col = prefix + g
            if col not in wide.columns:
                wide[col] = pd.NA

    return wide


def _discover_units(schema: dict) -> tuple[str, str]:
    import cbsodata

    props = pd.DataFrame(cbsodata.get_meta(CBS_TABLE_ID, "DataProperties"))
    elec_unit = props.loc[props["Key"] == schema["elec"], "Unit"]
    gas_unit = props.loc[props["Key"] == schema["gas"], "Unit"]
    return (
        elec_unit.iloc[0] if not elec_unit.empty else "onbekend",
        gas_unit.iloc[0] if not gas_unit.empty else "onbekend",
    )


# ---------------------------------------------------------------------------
# Fallback source: Klimaatmonitor Open Data Service
# ---------------------------------------------------------------------------

_KM_BASE = "https://klimaatmonitor.databank.nl/jiveservices/odata"
_KM_SOURCE_NAME = "Regionale Klimaatmonitor (Open Data Service) — sectoraal elektriciteit-/aardgasverbruik per gemeente"
_KM_SOURCE_URL = "https://klimaatmonitor.databank.nl/content/handleiding-open-data-service"
_KM_ELEC_UNIT = "kWh"
_KM_GAS_UNIT = "m3"
_KM_RETRY_MAX = 3
_KM_RETRY_DELAY = 3

_SECRETS_FILE = Path(__file__).parent / "secrets.local.json"

# Verified live against klimaatmonitor.databank.nl on 2026-07-07 (see module docstring).
_KM_ELEC_VARS: dict[str, list[str]] = {
    "a":  ["vbrze_a"],
    "bf": ["vbrze_b", "vbrze_ctot", "vbrze_d", "vbrze_afval", "vbrze_f"],
    "gi": ["vbrze_g1", "vbrze_i"],
    "hj": ["vbrze_h", "vbrze_j"],
    "kl": ["vbrze_k", "vbrze_l"],
    "mn": ["vbrze_m", "vbrze_n"],
    "oq": ["vbrze_o", "vbrze_p", "vbrze_q"],
    "ru": ["vbrze_r1", "vbrze_s", "vbrze_u"],
}
# No vbrzg_d: Klimaatmonitor does not track gas consumption for SBI D
# (Energievoorziening) — confirmed absent (404) on their live API.
_KM_GAS_VARS: dict[str, list[str]] = {
    "a":  ["vbrzg_a"],
    "bf": ["vbrzg_b", "vbrzg_ctot", "vbrzg_afval", "vbrzg_f"],
    "gi": ["vbrzg_g1", "vbrzg_i"],
    "hj": ["vbrzg_h", "vbrzg_j"],
    "kl": ["vbrzg_k", "vbrzg_l"],
    "mn": ["vbrzg_m", "vbrzg_n"],
    "oq": ["vbrzg_o", "vbrzg_p", "vbrzg_q"],
    "ru": ["vbrzg_r1", "vbrzg_s", "vbrzg_u"],
}


def _laad_klimaatmonitor_apikey() -> str | None:
    if not _SECRETS_FILE.exists():
        log.warning(
            "  Klimaatmonitor API-key niet gevonden: %s bestaat niet. Maak dit bestand aan "
            'met inhoud {"klimaatmonitor_apikey": "<jouw key>"} — vraag een key aan via '
            "%s (contactformulier).",
            _SECRETS_FILE.name, _KM_SOURCE_URL,
        )
        return None
    try:
        data = json.loads(_SECRETS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("  Kon %s niet lezen: %s", _SECRETS_FILE.name, exc)
        return None
    key = data.get("klimaatmonitor_apikey")
    if not key:
        log.warning("  '%s' bevat geen 'klimaatmonitor_apikey'.", _SECRETS_FILE.name)
        return None
    return key


def _km_fetch_variable(headers: dict, var_code: str, jaar: int) -> pd.DataFrame | None:
    """Fetch one Klimaatmonitor variable at gemeente level for one year.
    Returns a DataFrame with columns gemeentecode, waarde — or None on failure."""
    url = (
        f"{_KM_BASE}/Variables('{var_code}')/GeoLevels('gemeente')"
        f"/PeriodLevels('year')/Periods('{jaar}')/Values"
    )
    for attempt in range(1, _KM_RETRY_MAX + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 401:
                log.error("  Klimaatmonitor: API-key ongeldig of geen toegang (401).")
                return None
            resp.raise_for_status()
            break
        except Exception as exc:
            if attempt < _KM_RETRY_MAX:
                time.sleep(_KM_RETRY_DELAY)
            else:
                log.warning("    Klimaatmonitor %s (jaar %d) mislukt: %s", var_code, jaar, exc)
                return None

    rows = resp.json().get("value", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    # ExternalCode looks like "gemeente_106" -> GM0106 (numeric part == CBS gemeente code).
    df["gemeentecode"] = "GM" + df["ExternalCode"].str.replace("gemeente_", "", regex=False).str.zfill(4)
    df["waarde"] = pd.to_numeric(df["ValueString"], errors="coerce")
    return df[["gemeentecode", "waarde"]]


def _km_download(jaren: list[int]) -> pd.DataFrame | None:
    """Download + aggregate Klimaatmonitor sector elec/gas demand for all years.
    Returns a long DataFrame (gemeentecode, jaar, groep, elec, gas) matching the
    same shape _download() produces for CBS, so _pivot_per_groep() is shared."""
    apikey = _laad_klimaatmonitor_apikey()
    if apikey is None:
        return None
    headers = {"apikey": apikey}

    frames = []
    for jaar in jaren:
        for carrier, var_map in (("elec", _KM_ELEC_VARS), ("gas", _KM_GAS_VARS)):
            for groep, var_codes in var_map.items():
                for var_code in var_codes:
                    df = _km_fetch_variable(headers, var_code, jaar)
                    if df is None:
                        continue
                    df["jaar"] = jaar
                    df["groep"] = groep
                    df["carrier"] = carrier
                    frames.append(df)

    if not frames:
        log.error("  Klimaatmonitor: geen data ontvangen voor enige variabele.")
        return None

    long_df = pd.concat(frames, ignore_index=True)
    agg = long_df.groupby(["gemeentecode", "jaar", "groep", "carrier"], as_index=False)["waarde"].sum()

    elec = agg[agg["carrier"] == "elec"].rename(columns={"waarde": "elec"})
    gas = agg[agg["carrier"] == "gas"].rename(columns={"waarde": "gas"})
    merged = elec[["gemeentecode", "jaar", "groep", "elec"]].merge(
        gas[["gemeentecode", "jaar", "groep", "gas"]],
        on=["gemeentecode", "jaar", "groep"], how="outer",
    )
    log.info("  Klimaatmonitor: %d gemeente x jaar x groep rijen opgehaald", len(merged))
    return merged


def _van_cbs(jaren: list[int]) -> tuple[pd.DataFrame, str, str] | None:
    """Try CBS 82538NED. Returns (wide_df, elec_unit, gas_unit) or None on failure."""
    log.info("Poging 1: CBS %s ...", CBS_TABLE_ID)
    schema = _discover_schema()
    if schema is None:
        log.warning("  CBS %s niet beschikbaar.", CBS_TABLE_ID)
        return None

    branch_groups = _discover_branch_groups(schema["branch"])
    if not branch_groups:
        log.warning("  CBS %s: geen branche-categorieën herkend.", CBS_TABLE_ID)
        return None

    raw = _download(schema, branch_groups, jaren)
    if raw is None or raw.empty:
        log.warning("  CBS %s: geen data ontvangen.", CBS_TABLE_ID)
        return None

    wide = _pivot_per_groep(raw)
    elec_unit, gas_unit = _discover_units(schema)
    return wide, elec_unit, gas_unit


def _van_klimaatmonitor(jaren: list[int]) -> tuple[pd.DataFrame, str, str] | None:
    """Try Klimaatmonitor ODS. Returns (wide_df, elec_unit, gas_unit) or None on failure."""
    log.info("Poging 2: Klimaatmonitor Open Data Service ...")
    raw = _km_download(jaren)
    if raw is None or raw.empty:
        log.error("  Klimaatmonitor: geen data ontvangen.")
        return None
    return _pivot_per_groep(raw), _KM_ELEC_UNIT, _KM_GAS_UNIT


def haal_energieverbruik_sector(jaren: list[int]) -> pd.DataFrame | None:
    """Fetch sector electricity/gas demand per gemeente for the given years.

    Tries CBS 82538NED first, falls back to Klimaatmonitor. Returns the
    finalized wide DataFrame (unit-suffixed columns, totals, provenance
    columns already added) — or None if both sources failed. Writes nothing
    to disk; the caller (make_gemeenten_potentie_csv.py) merges this directly
    into kerncijfers_gemeenten_<jaar>[_met_geometrie].csv.
    """
    t0 = time.monotonic()
    log.info("Sectorale elektriciteit/aardgas per gemeente ophalen (jaren=%s)...", jaren)

    result = _van_cbs(jaren)
    bron_naam, bron_url = CBS_SOURCE_NAME, CBS_SOURCE_URL
    if result is None:
        result = _van_klimaatmonitor(jaren)
        bron_naam, bron_url = _KM_SOURCE_NAME, _KM_SOURCE_URL

    if result is None:
        log.error("Geen enkele bron beschikbaar (CBS en Klimaatmonitor beide mislukt).")
        return None

    wide, elec_unit, gas_unit = result
    wide = _finaliseer_kolommen(wide, elec_unit, gas_unit)
    wide["elec_eenheid"] = elec_unit
    wide["gas_eenheid"] = gas_unit
    wide["energie_bron"] = bron_naam
    wide["energie_bron_url"] = bron_url

    elapsed = time.monotonic() - t0
    log.info(
        "Sectorale energieverbruik opgehaald: %d rijen (%d gemeenten x %d jaren), "
        "bron=%s, eenheden elec=%s gas=%s (%.0fs)",
        len(wide), wide["gemeentecode"].nunique(), wide["jaar"].nunique(),
        bron_naam, elec_unit, gas_unit, elapsed,
    )
    return wide


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    jaren = [int(a) for a in sys.argv[1:]] or list(DEFAULT_YEARS)
    result = haal_energieverbruik_sector(jaren)
    if result is None:
        sys.exit(1)
    log.info("Kolommen: %s", list(result.columns))
    log.info("Voorbeeldrij:\n%s", result.iloc[0].to_string())
