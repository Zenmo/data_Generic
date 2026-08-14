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

Regional fair-share backfill (Klimaatmonitor path only)
------------------------------------------------------
CBS suppresses a sector/municipality cell when too few businesses report in it,
publishing 0 or nothing. Because this script sums ~33 per-SBI-letter variables
into 8 groups, a suppressed letter silently contributes 0: the group total still
looks populated while being understated. That is the bulk of the demand gap
documented in RES/klimaatmonitor_sector_demand_gap.md — measured on the 2023
output, only 0.3–10% of *group*-level municipal cells are exactly 0, far too few
to explain a ~25% electricity / ~17% gas shortfall, so the loss sits below the
group level.

Each variable is therefore fetched with GeoLevels('all') instead of
GeoLevels('gemeente'). That is one request returning gemeente, regio, provincie
and nederland rows together — no extra HTTP traffic — and it lets the script
reconcile: per variable and year, residual = area total − sum of the area's
municipalities, distributed over the municipalities that look suppressed
(value 0 or absent), weighted by their company locations for that SBI group
(CBS KWB, the same counts make_buurten_csv.py uses to reach neighbourhoods).
If no municipality looks suppressed, the residual is spread over all of them.

The escalation ladder is regio -> provincie -> nederland
(_KM_RECONCILIATIE_LADDER): a municipality whose region publishes no total for
a variable falls through to the province, then to the national figure. Areas
that do publish a total are never overwritten by a coarser estimate. Set
RECONCILIEER_MET_REGIO = False for the old gemeente-only behaviour.

Per-letter reconciliation fixes the *suppression* half of the gap. The RVO
"bijschatting" correction in Klimaatmonitor's "Thema's" export exists in no ODS
variable at any geo level, so a final optional rung closes that separately:
drop `Thema's - <regio>.csv` exports in the RES folder and the region's group
totals are scaled up to those control totals, with the residual assigned to
industry (see the RECONCILIEER_MET_THEMA block below for the evidence).

Returned columns: gemeentecode, jaar,
         elec_verbruik_a_<unit>, elec_verbruik_bf_<unit>, ..., elec_verbruik_ru_<unit>,
         gas_verbruik_a_<unit>,  gas_verbruik_bf_<unit>,  ..., gas_verbruik_ru_<unit>,
         elec_verbruik_totaal_<unit>, gas_verbruik_totaal_<unit>  (sum across all 8 groups)
         elec_eenheid, gas_eenheid,           (the same unit, spelled out; belt-and-braces)
         energie_bron, energie_bron_url       (whichever source actually succeeded)
         elec_bijgeschat_<unit>, gas_bijgeschat_<unit>,       how much came from
         elec_bijgeschat_aandeel, gas_bijgeschat_aandeel,     the fair-share backfill
         bijschatting_niveaus                                 rather than own opgave

To measure how much suppression there actually is before/after, run the
companion diagnostic: `python diagnose_sector_geolevels.py 2023 2024`.

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

import csv
import json
import logging
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests

from config import REQUEST_TIMEOUT, PROCESSED_DIR, OUTPUT_SEPARATOR

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
    try:
        import cbsodata
    except ImportError as exc:
        # Not installed is just another reason this source is unavailable — the
        # caller falls back to Klimaatmonitor, so it must not raise.
        log.warning("  cbsodata niet geïnstalleerd (%s) — CBS-bron overgeslagen.", exc)
        return None

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

# Escalation ladder for the fair-share backfill, finest-first. Each rung is a
# Klimaatmonitor GeoLevel whose ExternalCode prefix identifies it (e.g.
# "res_17", "provincie_20"). A rung is used only for the municipalities still
# unreconciled after the previous rung, so a region with its own published
# total never gets overwritten by a provincial estimate.
#
# Level names verified live on 2026-08-13 — Klimaatmonitor publishes these ten:
#   buurt, gemeente, nederland, omgeving, postcode, provincie, res,
#   res_landsdeel, subres, wijk
# and every vbrze_*/vbrzg_* variable is offered at exactly:
#   gemeente, nederland, omgeving, provincie, res, subres
# The RES-region level is called 'res', NOT 'regio'. 'subres' (41 areas) is
# finer than 'res' (31) and would be a better first rung, but municipalities.xlsx
# has no gemeente -> subres mapping, so it is not usable here yet.
#
# RES ONLY — deliberately no provincie/nederland rung. Where a RES total is
# itself suppressed, a province total cannot tell you which RES region inside
# that province the extra demand belongs to, so escalating would invent spatial
# structure that the source data does not contain. Those cases are left
# untouched and reported instead: see _schrijf_dekkingsrapport(), which
# quantifies exactly how much demand is known nationally but not attributable
# to any RES region, so it can be accounted for separately.
_KM_RECONCILIATIE_LADDER = ["res"]

# Where the coverage report is written (one row per variable x year x gap).
_DEKKING_BESTAND = "energieverbruik_sector_res_dekking.csv"

# ---------------------------------------------------------------------------
# Optional final rung: Klimaatmonitor "Thema's" regional control totals
# ---------------------------------------------------------------------------
# The Thema's export carries totals that RVO has corrected ("Optelling en
# bijschatting door RVO o.b.v. CBS-gegevens"). That correction exists in no ODS
# variable at any geo level, so per-letter reconciliation can never reach it.
# Verified against Drechtsteden 2023 after RES reconciliation:
#     SBI A            elec 7.42 vs 7.42 GWh      gas 1.637 vs 1.637 mln m3   (0.0%)
#     Diensten G-U                                gas 36.41 vs 37.00          (1.6%)
#     Industrie B-F                               gas 62.30 vs 79.65         (21.8%)
# Agriculture matches exactly and services are within 1.6%, so the residual
# demonstrably belongs to industry. Two known causes both sit there:
#   - vbrzg_d does not exist in Klimaatmonitor at all (energy-sector gas is not
#     tracked), while the Thema's gas figure is explicitly *incl. SBI D*;
#   - vbrze_d / vbrzg_b / vbrzg_f have no RES total for Drechtsteden, so those
#     municipal values pass through unreconciled.
# Hence: assign the whole residual to the industry group.
#
# Drop any number of Klimaatmonitor "Thema's - <regio>.csv" exports in the RES
# folder; each is picked up automatically and applies ONLY to the RES region it
# names. Regions without an export are left exactly as the RES rung left them.
RECONCILIEER_MET_THEMA = True
_THEMA_RESTGROEP = "bf"          # SBI B-F, "nijverheid en energie"
_THEMA_BESTANDSPATROON = "Thema's - *.csv"

# Column headers in the export, per carrier. The year is appended as "|<jaar>".
_THEMA_KOLOM = {
    "elec": "Totaal elektriciteitsverbruik bedrijven en instellingen, geleverd via openbaar net",
    "gas": "Totaal aardgasverbruik bedrijven/instellingen (incl. SBI D)",
}

# The same control totals, straight from the ODS — so this works for ALL RES
# regions without downloading an export per region. Codes verified live on
# 2026-08-13 (probe_klimaatmonitor_aggregaten.py): both reproduce the downloaded
# Drechtsteden 2023 export to 0.00%.
#
#   elec: vbrze_tot            "Totaal elektriciteitsverbruik bedrijven en
#                               instellingen, geleverd via openbaar net"
#                              31/31 regions, NL 71.59 TWh vs model 69.45 (3.0% gap)
#   gas:  vbrzg_tot            "Totaal aardgasverbruik bedrijven/instellingen
#                               (excl. SBI D)"  — 19/31 regions
#
# Gas deliberately uses the EXCL-SBI-D variable, even though
# vbrzg_tot_incl_sbid covers all 31 regions. SBI D is Energievoorziening: the
# incl. variant is 23.06 mld m3 against 14.59 in the model, and that ~8.5 mld m3
# difference is essentially gas burned in power stations. That is fuel input to
# electricity generation, not industrial end-use demand — folding it into the
# industry group would make bf 75% of all business gas and double-count against
# any generation the model handles separately. The 12 regions where vbrzg_tot is
# not published simply keep RES letter-level reconciliation.
#
# Note the unit-suffixed twins in the catalogue (vbrze_tot_gwh, _tj,
# vbrzg_tot_mm3, ...) are the SAME figure in other units. Always use the
# unsuffixed code: the pipeline works in kWh and m3.
_THEMA_API_VARS = {
    "elec": "vbrze_tot",
    "gas": "vbrzg_tot",
}

# Set False to get the old behaviour (gemeente-level only, no backfill).
RECONCILIEER_MET_REGIO = True

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


def _km_fetch_variable(headers: dict, var_code: str, jaar: int,
                       geolevel: str = "gemeente") -> pd.DataFrame | None:
    """Fetch one Klimaatmonitor variable for one year at one or all geo levels.

    geolevel='gemeente' keeps the historical behaviour. geolevel='all' is a
    documented Swing ODS parameter that returns every published level
    (gemeente, regio, provincie, nederland, ...) in the *same single request* —
    so asking for all levels costs no extra HTTP round-trips, which is what
    makes the regional backfill essentially free.

    Returns a DataFrame with columns level, gebiedscode, gemeentecode, waarde —
    or None on failure. `gemeentecode` is filled only for gemeente-level rows.
    """
    url = (
        f"{_KM_BASE}/Variables('{var_code}')/GeoLevels('{geolevel}')"
        f"/PeriodLevels('year')/Periods('{jaar}')/Values"
    )
    for attempt in range(1, _KM_RETRY_MAX + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 401:
                log.error("  Klimaatmonitor: API-key ongeldig of geen toegang (401).")
                return None
            if resp.status_code == 404:
                # Legitimate "not published at this level" — not an error.
                return None
            resp.raise_for_status()
            break
        except Exception as exc:
            if attempt < _KM_RETRY_MAX:
                time.sleep(_KM_RETRY_DELAY)
            else:
                log.warning("    Klimaatmonitor %s (jaar %d, %s) mislukt: %s",
                            var_code, jaar, geolevel, exc)
                return None

    rows = resp.json().get("value", [])
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "ExternalCode" not in df.columns:
        log.warning("    Klimaatmonitor %s (jaar %d): antwoord zonder ExternalCode", var_code, jaar)
        return None

    # ExternalCode looks like "gemeente_106" -> level 'gemeente', code '106'
    # (numeric part == CBS gemeente code). Splitting on the prefix rather than
    # assuming a level makes this work unchanged for regio_/provincie_/... rows.
    split = df["ExternalCode"].astype(str).str.split("_", n=1, expand=True)
    df["level"] = split[0]
    df["gebiedscode"] = split[1] if split.shape[1] > 1 else pd.NA
    df["waarde"] = pd.to_numeric(df["ValueString"], errors="coerce")

    is_gem = df["level"].eq("gemeente")
    df["gemeentecode"] = pd.NA
    df.loc[is_gem, "gemeentecode"] = "GM" + df.loc[is_gem, "gebiedscode"].astype(str).str.zfill(4)

    return df[["level", "gebiedscode", "gemeentecode", "waarde"]]


# ---------------------------------------------------------------------------
# Fair-share backfill: redistribute what municipalities lost to CBS suppression
# ---------------------------------------------------------------------------

def _laad_bedrijfsvestigingen_per_gemeente(jaar: int) -> pd.DataFrame | None:
    """Company locations per SBI-group per gemeente, from CBS Kerncijfers wijken
    en buurten — the weights for the fair-share redistribution.

    These are the same counts make_buurten_csv.py already uses to push gemeente
    demand down to neighbourhoods (_BEDRIJF_COL_TO_GROEP), so weighting the
    regional residual by them keeps one consistent allocation basis across the
    whole chain: region -> gemeente -> buurt.

    Returns a DataFrame indexed by gemeentecode with one column per groep, or
    None if the KWB fetch fails (the caller then falls back to equal shares).
    """
    kolom_naar_groep = {
        "a_landbouw_bosbouw_en_visserij": "a",
        "bf_nijverheid_en_energie": "bf",
        "gi_handel_en_horeca": "gi",
        "hj_vervoer_informatie_en_communicatie": "hj",
        "kl_financiele_diensten_onroerend_goed": "kl",
        "mn_zakelijke_dienstverlening": "mn",
        "oq_overheid_onderwijs_en_zorg": "oq",
        "ru_cultuur_recreatie_overige_diensten": "ru",
    }
    try:
        import cbs_kwb

        df = cbs_kwb.haal_kwb(
            jaar,
            kolommen=list(kolom_naar_groep),
            alleen_buurten=False,      # we need the 'Gemeente' rows, not buurten
            ontbrekende_waarde=None,   # keep NaN; -99999 would poison the weights
        )
    except Exception as exc:
        log.warning("  Bedrijfsvestigingen (KWB %d) niet beschikbaar: %s — "
                    "fair-share valt terug op gelijke verdeling.", jaar, exc)
        return None

    if "soort_regio" in df.columns:
        df = df[df["soort_regio"].astype(str).str.strip() == "Gemeente"]
    if "codering" not in df.columns or df.empty:
        log.warning("  Bedrijfsvestigingen (KWB %d): geen gemeenterijen gevonden.", jaar)
        return None

    df = df.rename(columns={"codering": "gemeentecode", **kolom_naar_groep})
    df["gemeentecode"] = df["gemeentecode"].astype(str).str.strip()
    kolommen = [g for g in _GROEPEN if g in df.columns]
    gewichten = df.set_index("gemeentecode")[kolommen].apply(pd.to_numeric, errors="coerce")
    # A negative sentinel that slipped through would invert the weighting.
    gewichten = gewichten.where(gewichten >= 0)
    log.info("  Bedrijfsvestigingen-gewichten geladen: %d gemeenten x %d groepen",
             len(gewichten), len(kolommen))
    return gewichten


def _laad_gebiedsindeling() -> pd.DataFrame | None:
    """gemeentecode -> res_regio(code) / provincie(code), from municipalities.xlsx.

    Same source and column names as make_gemeenten_potentie_csv.laad_provincie_res(),
    so the region a gemeente is reconciled against is the same region it is
    reported under downstream.
    """
    kandidaten = [
        Path(__file__).parent.parent / "municipalities.xlsx",
        Path(__file__).parent / "municipalities.xlsx",
    ]
    pad = next((p for p in kandidaten if p.exists()), None)
    if pad is None:
        log.warning("  municipalities.xlsx niet gevonden — geen regio-indeling, "
                    "reconciliatie overgeslagen.")
        return None
    try:
        df = pd.read_excel(pad, sheet_name="municipalities")
    except Exception as exc:
        log.warning("  municipalities.xlsx niet leesbaar (%s) — reconciliatie overgeslagen.", exc)
        return None

    df = df.rename(columns={
        "gwb_code": "gemeentecode",
        "province": "provincie",
        "province_code": "provinciecode",
        "RES_region": "res_regio",
        "RES_code": "res_regiocode",
    })
    keep = ["gemeentecode", "provincie", "provinciecode", "res_regio", "res_regiocode"]
    df = df[[c for c in keep if c in df.columns]].dropna(subset=["gemeentecode"])
    df["gemeentecode"] = df["gemeentecode"].astype(str).str.strip()
    return df.drop_duplicates(subset=["gemeentecode"])


# {geolevel: {gebiedscode: original, unnormalised area name}} — filled by
# _km_geoitems(). Matching needs the normalised form, but logs and the coverage
# report need the name a human recognises ('Noord-Holland Zuid', not
# 'noordhollandzuid'), so both are kept.
_KM_GEOITEM_LABELS: dict[str, dict[str, str]] = {}


def _km_gebiedslabel(geolevel: str, code: str) -> str | None:
    """The human-readable area name, if _km_geoitems() has already fetched it."""
    return _KM_GEOITEM_LABELS.get(geolevel, {}).get(str(code))


def _km_geoitems(headers: dict, geolevel: str, _cache: dict = {}) -> dict[str, str]:
    """{gebiedscode -> normalised area name} for one GeoLevel, e.g.
    {'17': 'drechtsteden', ...} for 'res'.

    Klimaatmonitor identifies areas by its own internal codes, which do not match
    municipalities.xlsx's RES_code / province_code. Their *names* do match once
    normalised, so the reconciliation keys on names and uses GeoItems as the
    translation table. One request per level per run, cached.
    """
    if geolevel in _cache:
        return _cache[geolevel]

    mapping: dict[str, str] = {}
    labels: dict[str, str] = _KM_GEOITEM_LABELS.setdefault(geolevel, {})
    url = f"{_KM_BASE}/GeoLevels('{geolevel}')/GeoItems"
    while url:
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (401, 404):
                break
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("  GeoItems('%s') mislukt: %s — terugval op codematching.", geolevel, exc)
            break
        for item in data.get("value", []):
            code = item.get("ExternalCode")
            naam = item.get("Name")
            if not code:
                continue
            # ExternalCode here is the full "res_17" form; keep the suffix so it
            # lines up with what _km_fetch_variable() parses out of Values rows.
            suffix = str(code).split("_", 1)[1] if "_" in str(code) else str(code)
            mapping[suffix] = _normaliseer_gebiedsnaam(naam if naam else suffix)
            labels[suffix] = str(naam) if naam else suffix
        url = data.get("@odata.nextLink")

    if mapping:
        log.info("  GeoItems('%s'): %d gebieden opgehaald", geolevel, len(mapping))
    _cache[geolevel] = mapping
    return mapping


def _normaliseer_gebiedsnaam(naam) -> str:
    """'Noord-Holland Zuid' / 'noordhollandzuid' -> 'noordhollandzuid'.
    Klimaatmonitor and municipalities.xlsx spell region names differently;
    matching on a stripped-down form avoids maintaining a hand-written map."""
    return re.sub(r"[^a-z0-9]", "", str(naam).lower())


# Two RES regions are genuinely named differently in the two sources — not a
# spelling variant that normalisation can bridge. Verified against the live
# GeoItems list on 2026-08-13:
#   municipalities.xlsx 'Friesland' = Klimaatmonitor 'Fryslân'
#   municipalities.xlsx 'Cleantech' = Klimaatmonitor 'Stedendriehoek'
#     (the RES region is officially "Cleantech Regio", formerly Stedendriehoek)
# Klimaatmonitor's third unmatched entry, 'RES regio onbekend', is a genuine
# catch-all bucket with no counterpart and is intentionally left unmatched.
_KM_GEBIED_ALIASSEN = {
    "friesland": "frysln",
    "cleantech": "stedendriehoek",
}


def _gebiedssleutel(naam) -> str:
    """Normalised area name, translated to Klimaatmonitor's spelling.

    Use this for anything derived from municipalities.xlsx; use
    _normaliseer_gebiedsnaam() directly for names that already come from
    Klimaatmonitor.
    """
    genormaliseerd = _normaliseer_gebiedsnaam(naam)
    return _KM_GEBIED_ALIASSEN.get(genormaliseerd, genormaliseerd)


def _verdeel_residual(
    waarden: pd.Series,          # gemeentecode -> value for one variable (may hold 0/NaN)
    residual: float,             # area total minus the sum of these values
    gewichten: pd.Series | None, # gemeentecode -> company locations for this groep
    toegestaan: pd.Index | None = None,  # subset eligible to receive (None = all)
) -> tuple[pd.Series, pd.Series]:
    """Spread one area's residual over its municipalities. Returns
    (corrected values, imputed amount per gemeente).

    Targeting rule: suppression shows up as an exact 0 or an absent cell, so if
    any eligible municipality looks suppressed, the residual goes *only* to
    those — municipalities that did report a real figure keep it untouched.
    Only when nothing looks suppressed (the area total simply exceeds the sum,
    e.g. because of rounding or an unallocated remainder) is the residual spread
    across every eligible municipality.

    `toegestaan` restricts the eligible set to municipalities not already
    reconciled at a finer rung of the ladder, so a coarser area's total can
    never reopen a figure a finer area already settled.
    """
    bijschatting = pd.Series(0.0, index=waarden.index)
    if not pd.notna(residual) or residual <= 0:
        return waarden, bijschatting

    kandidaten = waarden.index if toegestaan is None else waarden.index.intersection(toegestaan)
    if len(kandidaten) == 0:
        return waarden, bijschatting

    onderdrukt = waarden.loc[kandidaten].isna() | (waarden.loc[kandidaten] == 0)
    doelen = kandidaten[onderdrukt] if onderdrukt.any() else kandidaten

    w = None
    if gewichten is not None:
        w = pd.to_numeric(gewichten.reindex(doelen), errors="coerce")
        if not (w.notna().any() and w.sum(min_count=1) and w.sum() > 0):
            w = None  # no usable company counts here
    if w is None:
        w = pd.Series(1.0, index=doelen)     # equal shares as last resort
    w = w.fillna(0.0)
    if w.sum() <= 0:
        w = pd.Series(1.0, index=doelen)

    aandeel = w / w.sum()
    toegekend = aandeel * residual

    gecorrigeerd = waarden.copy()
    gecorrigeerd.loc[doelen] = gecorrigeerd.loc[doelen].fillna(0.0) + toegekend
    bijschatting.loc[doelen] = toegekend
    return gecorrigeerd, bijschatting


def _reconcilieer_variabele(
    per_gemeente: pd.Series,             # gemeentecode -> value
    gebiedstotalen: dict[str, pd.Series], # level -> (genormaliseerde gebiedsnaam/code -> value)
    indeling: pd.DataFrame,              # gemeentecode -> regio/provincie
    gewichten: pd.Series | None,
) -> tuple[pd.Series, pd.Series, list[str]]:
    """Walk the escalation ladder for one variable+year.

    At every rung: group the municipalities by that rung's area, compare the
    area's own published total against the (already partially corrected) sum of
    its municipalities, and hand the positive difference to _verdeel_residual().
    Municipalities whose area has no published total at this rung simply carry
    on to the next, coarser rung — that is the whole point of the ladder.

    Once an area *does* publish a total, its municipalities are marked settled
    and are skipped by every coarser rung. A rung is a fallback for missing
    area totals, not a second correction stacked on top of the first: without
    this, a municipality would be topped up once per rung and the result would
    depend on how many rungs happen to exist.
    """
    waarden = per_gemeente.copy()
    bijschatting_totaal = pd.Series(0.0, index=waarden.index)
    gebruikte_rungs: list[str] = []
    afgehandeld = pd.Series(False, index=waarden.index)

    sleutel_per_rung = {
        "res": ["res_regio", "res_regiocode"],
        "provincie": ["provincie", "provinciecode"],
    }

    for rung in _KM_RECONCILIATIE_LADDER:
        if afgehandeld.all():
            break  # every gemeente already settled at a finer rung
        totalen = gebiedstotalen.get(rung)
        if totalen is None or totalen.empty:
            continue

        if rung == "nederland":
            # One area containing everything.
            groepen = {"nederland": waarden.index}
            landelijk = totalen.dropna()
            lookup = {"nederland": landelijk.iloc[0] if len(landelijk) else float("nan")}
        else:
            kolommen = [c for c in sleutel_per_rung.get(rung, []) if c in indeling.columns]
            if not kolommen:
                continue
            groepen, lookup = {}, {}
            for kolom in kolommen:
                sleutels = indeling.set_index("gemeentecode")[kolom].reindex(waarden.index)
                genormaliseerd = sleutels.map(_gebiedssleutel)
                treffers = genormaliseerd[genormaliseerd.isin(totalen.index)]
                if treffers.empty:
                    continue  # try the other spelling (name vs. code)
                for sleutel, leden in treffers.groupby(treffers):
                    groepen[sleutel] = leden.index
                    lookup[sleutel] = totalen.get(sleutel, float("nan"))
                break  # first column that matches wins

        rung_gebruikt = False
        for sleutel, leden in groepen.items():
            open_leden = leden[~afgehandeld.reindex(leden).fillna(False).to_numpy()]
            if len(open_leden) == 0:
                continue  # nothing left here that a coarser total may touch
            gebiedstotaal = lookup.get(sleutel)
            if not pd.notna(gebiedstotaal):
                continue  # no published total here — fall through to next rung

            # Residual is measured against *all* members (settled ones included,
            # since their figures are already the best estimate) but handed only
            # to the ones still open.
            huidige_som = waarden.loc[leden].sum(min_count=1)
            residual = gebiedstotaal - (huidige_som if pd.notna(huidige_som) else 0.0)
            if pd.notna(residual) and residual > 0:
                gecorrigeerd, bijschatting = _verdeel_residual(
                    waarden.loc[leden], residual,
                    gewichten.reindex(leden) if gewichten is not None else None,
                    toegestaan=open_leden,
                )
                waarden.loc[leden] = gecorrigeerd
                bijschatting_totaal.loc[leden] += bijschatting
                rung_gebruikt = True

            # The area published a total, so its members are settled either way —
            # including when the residual was zero or negative.
            afgehandeld.loc[leden] = True

        if rung_gebruikt:
            gebruikte_rungs.append(rung)

    return waarden, bijschatting_totaal, gebruikte_rungs


def _laad_thema_totalen() -> pd.DataFrame:
    """Parse every "Thema's - <regio>.csv" export found in the RES folder.

    Returns a DataFrame (regio_sleutel, jaar, carrier, totaal) — empty if no
    export is present, which simply disables this rung.

    Two quirks of the export are handled deliberately:

    1. The same header appears more than once, once in display units (870,2 for
       GWh) and once in base units (870181000 for kWh). We take the largest
       value per header, which is the base-unit one, and only if the largest is
       still below 1e6 do we scale by 1e6 — both GWh->kWh and mln m3 -> m3 are
       exactly that factor, so one rule covers both carriers.
    2. Numbers use a decimal comma, and unavailable cells are "?" or empty.
    """
    zoekpaden = [Path(__file__).parent.parent.parent, Path(__file__).parent.parent]
    bestanden: list[Path] = []
    for basis in zoekpaden:
        if basis.is_dir():
            bestanden.extend(sorted(basis.glob(_THEMA_BESTANDSPATROON)))
    bestanden = list(dict.fromkeys(bestanden))
    if not bestanden:
        return pd.DataFrame()

    rijen: list[dict] = []
    for pad in bestanden:
        try:
            with pad.open(encoding="utf-8-sig", newline="") as fh:
                tabel = list(csv.reader(fh, delimiter=";"))
        except Exception as exc:
            log.warning("  Kon %s niet lezen: %s", pad.name, exc)
            continue
        if len(tabel) < 2:
            continue
        kop = tabel[0]

        for regel in tabel[1:]:
            if not regel or not regel[0].strip():
                continue
            regio = regel[0].strip()
            # header -> every value published under it, for this region
            per_kop: dict[str, list[float]] = {}
            for h, v in zip(kop[1:], regel[1:]):
                v = str(v).strip().replace(".", "").replace(",", ".")
                if v in ("", "?", "-"):
                    continue
                try:
                    per_kop.setdefault(h, []).append(float(v))
                except ValueError:
                    continue

            for carrier, basisnaam in _THEMA_KOLOM.items():
                for h, waarden in per_kop.items():
                    if not h.startswith(basisnaam + "|"):
                        continue
                    jaar = h.rsplit("|", 1)[1]
                    if not jaar.isdigit():
                        continue
                    totaal = max(waarden)
                    if totaal < 1e6:      # display units -> base units
                        totaal *= 1e6
                    rijen.append({
                        "regio_sleutel": _gebiedssleutel(regio),
                        "regio": regio,
                        "jaar": int(jaar),
                        "carrier": carrier,
                        "totaal": totaal,
                        "bestand": pad.name,
                    })

    if not rijen:
        log.warning("  Thema's-exports gevonden (%d) maar geen bruikbare totalen erin.",
                    len(bestanden))
        return pd.DataFrame()

    df = pd.DataFrame(rijen).drop_duplicates(
        subset=["regio_sleutel", "jaar", "carrier"], keep="first")
    log.info("  Thema's-controletotalen geladen: %d regio-jaar-drager combinaties uit %s",
             len(df), [p.name for p in bestanden])
    return df


def _thema_totalen_via_api(headers: dict, jaren: list[int]) -> pd.DataFrame:
    """Control totals for every RES region, straight from the ODS.

    One request per carrier per year returns all 31 regions, so this scales to
    the whole country at the cost of 4 extra calls — no manual exports needed.
    Same shape as _laad_thema_totalen() so the two can be concatenated.
    """
    if not _THEMA_API_VARS:
        return pd.DataFrame()

    rijen = []
    for carrier, var_code in _THEMA_API_VARS.items():
        for jaar in jaren:
            df = _km_fetch_variable(headers, var_code, jaar, geolevel="res")
            if df is None or df.empty:
                log.warning("  Thema's-API: %s (%s, %d) leverde niets op.",
                            var_code, carrier, jaar)
                continue
            namen = _km_geoitems(headers, "res")
            for _, r in df.iterrows():
                waarde = r["waarde"]
                if not pd.notna(waarde):
                    continue
                code = str(r["gebiedscode"])
                rijen.append({
                    "regio_sleutel": namen.get(code, _normaliseer_gebiedsnaam(code)),
                    "regio": _km_gebiedslabel("res", code) or code,
                    "jaar": jaar,
                    "carrier": carrier,
                    "totaal": float(waarde),
                    "bestand": f"ODS:{var_code}",
                })

    if not rijen:
        return pd.DataFrame()
    df = pd.DataFrame(rijen)
    for carrier in df["carrier"].unique():
        deel = df[df["carrier"] == carrier]
        log.info("  Thema's-controletotalen via API: %s -> %d regio-jaar waarden "
                 "(%d regio's)", _THEMA_API_VARS[carrier], len(deel),
                 deel["regio_sleutel"].nunique())
    return df


def _reconcilieer_met_thema(
    merged: pd.DataFrame,
    indeling: pd.DataFrame | None,
    gewichten_per_jaar: dict[int, pd.DataFrame | None],
    headers: dict | None = None,
    jaren: list[int] | None = None,
) -> tuple[pd.DataFrame, list[pd.DataFrame], list[dict]]:
    """Final rung: scale a region's group totals up to its Thema's control total.

    Operates on the 8-group aggregate (not per SBI letter), because the RVO
    correction is only published as a whole-region total. The entire positive
    residual is assigned to _THEMA_RESTGROEP, weighted across the region's
    municipalities by their company locations for that group — see the module
    constant block for the evidence that industry is where it belongs.

    Never reduces a figure, and never touches a region without an export.
    """
    if not RECONCILIEER_MET_THEMA or indeling is None or merged.empty:
        return merged, [], []

    # API first (all regions), then any downloaded exports. The export wins on a
    # collision: if someone deliberately dropped a file in, honour it — though
    # in practice the two agree exactly, which is how the codes were verified.
    bronnen = []
    if headers is not None and jaren:
        api = _thema_totalen_via_api(headers, jaren)
        if not api.empty:
            bronnen.append(api)
    export = _laad_thema_totalen()
    if not export.empty:
        bronnen.append(export)
    if not bronnen:
        return merged, [], []

    thema = pd.concat(bronnen, ignore_index=True).drop_duplicates(
        subset=["regio_sleutel", "jaar", "carrier"], keep="last")

    merged = merged.copy()
    bijschat_frames: list[pd.DataFrame] = []
    dekking_rijen: list[dict] = []
    regio_van_gemeente = indeling.set_index("gemeentecode")["res_regio"].map(_gebiedssleutel)

    for _, rij in thema.iterrows():
        jaar, carrier, doel = int(rij["jaar"]), rij["carrier"], float(rij["totaal"])
        if carrier not in merged.columns:
            continue

        leden = regio_van_gemeente[regio_van_gemeente == rij["regio_sleutel"]].index
        masker = merged["jaar"].eq(jaar) & merged["gemeentecode"].isin(leden)
        if not masker.any():
            log.warning("  Thema's %s %d: geen gemeenten gevonden voor regio '%s' — "
                        "overgeslagen.", carrier, jaar, rij["regio"])
            continue

        huidig = pd.to_numeric(merged.loc[masker, carrier], errors="coerce").sum(min_count=1)
        residual = doel - (huidig if pd.notna(huidig) else 0.0)
        aandeel = 100 * residual / doel if doel else float("nan")

        if not pd.notna(residual) or residual <= 0:
            log.info("  Thema's %s %d %s: model (%.4g) is al >= controletotaal (%.4g) — "
                     "niets bijgeschat.", carrier, jaar, rij["regio"], huidig, doel)
            continue

        # Distribute the residual over the region's municipalities, weighted by
        # their company locations for the industry group.
        gewichten_df = gewichten_per_jaar.get(jaar)
        gewichten = None
        if gewichten_df is not None and _THEMA_RESTGROEP in gewichten_df.columns:
            gewichten = gewichten_df[_THEMA_RESTGROEP].reindex(leden)
        if gewichten is None or not (gewichten.notna().any() and gewichten.sum() > 0):
            gewichten = pd.Series(1.0, index=leden)
        gewichten = gewichten.fillna(0.0)
        if gewichten.sum() <= 0:
            gewichten = pd.Series(1.0, index=leden)
        deel = gewichten / gewichten.sum()

        doelrijen = masker & merged["groep"].eq(_THEMA_RESTGROEP)
        if not doelrijen.any():
            log.warning("  Thema's %s %d %s: groep '%s' ontbreekt — overgeslagen.",
                        carrier, jaar, rij["regio"], _THEMA_RESTGROEP)
            continue

        toegekend = merged.loc[doelrijen, "gemeentecode"].map(deel).fillna(0.0) * residual
        merged.loc[doelrijen, carrier] = (
            pd.to_numeric(merged.loc[doelrijen, carrier], errors="coerce").fillna(0.0)
            + toegekend.to_numpy()
        )

        bij = pd.DataFrame({
            "gemeentecode": merged.loc[doelrijen, "gemeentecode"].to_numpy(),
            "bijgeschat": toegekend.to_numpy(),
        })
        bij["jaar"] = jaar
        bij["carrier"] = carrier
        bijschat_frames.append(bij)

        dekking_rijen.append({
            "jaar": jaar, "var_code": f"<thema:{_THEMA_RESTGROEP}>", "carrier": carrier,
            "groep": _THEMA_RESTGROEP, "res_regio": rij["regio"],
            "status": "thema_bijschatting",
            "waarde_gemeenten": huidig, "waarde_res": doel,
            "ontbrekend_bedrag": residual,
        })
        log.info("  Thema's %s %d %s: %.4g bijgeschat (%.1f%% van het controletotaal "
                 "%.4g), toegewezen aan groep '%s'",
                 carrier, jaar, rij["regio"], residual, aandeel, doel, _THEMA_RESTGROEP)

    return merged, bijschat_frames, dekking_rijen


def _dekking_voor_variabele(
    var_code: str, carrier: str, groep: str, jaar: int,
    df: pd.DataFrame,                    # raw all-levels response for this variable
    res_totalen: pd.Series | None,       # normalised RES name -> value
    per_gemeente: pd.Series,             # post-reconciliation municipal values
    indeling: pd.DataFrame,
    headers: dict,
) -> list[dict]:
    """Record what this variable's RES coverage failed to attribute.

    Called per variable+year so the report is at the same granularity as the
    suppression itself — the 8-group CSV cannot show this, since a group total
    that looks fine can still hide a fully-suppressed letter.
    """
    rijen: list[dict] = []
    if "res_regio" not in indeling.columns:
        return rijen

    res_totalen = res_totalen if res_totalen is not None else pd.Series(dtype=float)
    gemeente_per_regio = indeling.set_index("gemeentecode")["res_regio"].reindex(per_gemeente.index)
    sleutels = gemeente_per_regio.map(_gebiedssleutel)

    # 1. RES regions with no published total for this variable.
    for sleutel, leden in sleutels.dropna().groupby(sleutels.dropna()):
        waarde = res_totalen.get(sleutel)
        if pd.notna(waarde):
            continue
        naam = gemeente_per_regio.loc[leden.index].dropna()
        rijen.append({
            "jaar": jaar, "var_code": var_code, "carrier": carrier, "groep": groep,
            "res_regio": naam.iloc[0] if len(naam) else sleutel,
            "status": "res_ontbreekt",
            "waarde_gemeenten": per_gemeente.loc[leden.index].sum(min_count=1),
            "waarde_res": pd.NA,
            "ontbrekend_bedrag": pd.NA,
        })

    # 2. Demand known nationally but not attributable to any RES region.
    #    'nederland' is never suppressed (0% missing, measured 2026-08-13), so
    #    this difference is always computable and is the honest upper bound on
    #    what the RES-level model is missing.
    nl = df[df["level"].eq("nederland")]["waarde"]
    nl_totaal = nl.sum(min_count=1) if not nl.empty else pd.NA
    res_som = res_totalen.sum(min_count=1) if len(res_totalen) else 0.0
    if pd.notna(nl_totaal) and pd.notna(res_som):
        verschil = nl_totaal - res_som
        if verschil > 0:
            rijen.append({
                "jaar": jaar, "var_code": var_code, "carrier": carrier, "groep": groep,
                "res_regio": "<niet toewijsbaar>",
                "status": "niet_toewijsbaar",
                "waarde_gemeenten": per_gemeente.sum(min_count=1),
                "waarde_res": res_som,
                "ontbrekend_bedrag": verschil,
            })
    return rijen


def _schrijf_dekkingsrapport(rijen: list[dict]) -> None:
    """Write the RES coverage report: which demand could NOT be attributed.

    Two distinct gaps are recorded, because they mean different things:

      status='res_ontbreekt' — the RES region publishes no total for this
        variable/year, so its municipalities keep their own (suppressed) figures
        and nothing was added. `waarde_gemeenten` is what the model will use.

      status='niet_toewijsbaar' — the national total exceeds the sum of all RES
        totals. That difference is demand Klimaatmonitor knows exists but does
        not attribute to any RES region. It cannot be placed spatially without
        inventing structure, so it is excluded from the model and quantified
        here instead. `ontbrekend_bedrag` is the amount.

    Aggregate the `ontbrekend_bedrag` column per carrier to get the total
    demand the model is knowingly missing.
    """
    if not rijen:
        return
    df = pd.DataFrame(rijen)
    pad = Path(PROCESSED_DIR) / _DEKKING_BESTAND
    try:
        pad.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(pad, index=False, sep=OUTPUT_SEPARATOR)
    except Exception as exc:
        log.warning("  Kon dekkingsrapport niet schrijven (%s): %s", pad, exc)
        return

    n_res_ontbreekt = int((df["status"] == "res_ontbreekt").sum())
    niet_toewijsbaar = df[df["status"] == "niet_toewijsbaar"]
    log.info("  RES-dekkingsrapport geschreven: %s (%d rijen)", pad, len(df))
    log.info("     %d variabele-jaar-regio combinaties zonder RES-totaal "
             "(gemeenten blijven daar ongecorrigeerd)", n_res_ontbreekt)

    # Per year AND per carrier — never one lump sum. Electricity is in kWh and
    # gas in m3, so adding them is dimensionally meaningless, and adding years
    # together hides that a recent year can be far less complete than an older
    # one (2024 vs 2023 differ by a factor of ~4 for gas).
    for (jaar, carrier), deel in niet_toewijsbaar.groupby(["jaar", "carrier"]):
        ontbreekt = deel["ontbrekend_bedrag"].sum()
        landelijk = ontbreekt + deel["waarde_res"].sum()
        aandeel = 100 * ontbreekt / landelijk if landelijk else float("nan")
        niveau = log.warning if aandeel >= 25 else log.info
        niveau(
            "     %d %s: %.4g eenheden (%.1f%% van het landelijke totaal) niet "
            "toewijsbaar aan een RES-regio — bewust NIET in het model opgenomen, "
            "zie %s", jaar, carrier, ontbreekt, aandeel, _DEKKING_BESTAND,
        )
        if aandeel >= 50:
            log.warning(
                "     ^ meer dan de helft ontbreekt: %d %s is op RES-niveau "
                "waarschijnlijk nog niet volledig gepubliceerd — gebruik dit jaar "
                "niet zonder controle.", jaar, carrier,
            )


def _controleer_gebiedskoppeling(headers: dict, indeling: pd.DataFrame) -> None:
    """Log how many Klimaatmonitor areas actually match municipalities.xlsx.

    A name mismatch here is the one failure mode that would be invisible
    otherwise: every area total would be discarded as 'unknown region', the
    ladder would fall through to nederland, and the output would look plausible
    while the reconciliation had done nothing. So it is checked up front, loudly.
    """
    for niveau, kolommen in [(rung, {"res": ["res_regio", "res_regiocode"],
                                     "provincie": ["provincie", "provinciecode"]}.get(rung, []))
                             for rung in _KM_RECONCILIATIE_LADDER]:
        if not kolommen:
            continue
        namen = set(_km_geoitems(headers, niveau).values())
        if not namen:
            log.warning("  Gebiedskoppeling '%s': GeoItems leverde niets op.", niveau)
            continue

        beste, beste_kolom, beste_eigen = 0, None, set()
        for kolom in kolommen:
            if kolom not in indeling.columns:
                continue
            eigen = set(indeling[kolom].dropna().map(_gebiedssleutel))
            overlap = len(namen & eigen)
            if overlap > beste:
                beste, beste_kolom, beste_eigen = overlap, kolom, eigen

        if beste == 0:
            log.warning(
                "  Gebiedskoppeling '%s': GEEN overeenkomst tussen Klimaatmonitor-namen "
                "(%s...) en municipalities.xlsx (%s). Reconciliatie op dit niveau doet "
                "niets — controleer de naamgeving.",
                niveau, sorted(namen)[:3], kolommen,
            )
            continue

        log.info("  Gebiedskoppeling '%s': %d van %d gebieden gematcht via '%s'",
                 niveau, beste, len(namen), beste_kolom)
        # Unmatched names are the actionable bit: those regions get no
        # reconciliation at all, so they must not stay buried in a count.
        alleen_km = sorted(namen - beste_eigen)
        alleen_eigen = sorted(beste_eigen - namen)
        if alleen_km:
            log.warning("     alleen in Klimaatmonitor (niet gekoppeld): %s", alleen_km)
        if alleen_eigen:
            log.warning("     alleen in municipalities.xlsx (blijft ongecorrigeerd): %s",
                        alleen_eigen)


def _km_download(jaren: list[int]) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Download + aggregate Klimaatmonitor sector elec/gas demand for all years.

    Returns (long_df, bijschatting_df):
      long_df        — (gemeentecode, jaar, groep, elec, gas), the same shape
                       _download() produces for CBS, so _pivot_per_groep() is shared.
      bijschatting_df— (gemeentecode, jaar, elec_bijgeschat, gas_bijgeschat,
                       bijschatting_niveaus): how much of each municipality's
                       demand came from the regional fair-share backfill rather
                       than from its own published figure.

    Each variable is fetched with GeoLevels('all') — one request, every geo level
    — so the regional/provincial totals needed for reconciliation arrive without
    extra HTTP traffic compared with the old gemeente-only version.
    """
    apikey = _laad_klimaatmonitor_apikey()
    if apikey is None:
        return None
    headers = {"apikey": apikey}

    geolevel = "all" if RECONCILIEER_MET_REGIO else "gemeente"
    indeling = _laad_gebiedsindeling() if RECONCILIEER_MET_REGIO else None
    if RECONCILIEER_MET_REGIO and indeling is None:
        log.warning("  Reconciliatie uitgeschakeld: geen gebiedsindeling beschikbaar.")
    if indeling is not None:
        _controleer_gebiedskoppeling(headers, indeling)

    frames: list[pd.DataFrame] = []
    bijschat_frames: list[pd.DataFrame] = []
    dekking_rijen: list[dict] = []
    rungs_gezien: set[str] = set()
    n_gereconcilieerd = 0

    gewichten_per_jaar: dict[int, pd.DataFrame | None] = {}

    for jaar in jaren:
        gewichten_df = (
            _laad_bedrijfsvestigingen_per_gemeente(jaar)
            if (RECONCILIEER_MET_REGIO and indeling is not None) else None
        )
        gewichten_per_jaar[jaar] = gewichten_df

        for carrier, var_map in (("elec", _KM_ELEC_VARS), ("gas", _KM_GAS_VARS)):
            for groep, var_codes in var_map.items():
                gewichten = None
                if gewichten_df is not None and groep in gewichten_df.columns:
                    gewichten = gewichten_df[groep]

                for var_code in var_codes:
                    df = _km_fetch_variable(headers, var_code, jaar, geolevel=geolevel)
                    if df is None or df.empty:
                        continue

                    per_gemeente = (
                        df[df["level"].eq("gemeente")]
                        .dropna(subset=["gemeentecode"])
                        .set_index("gemeentecode")["waarde"]
                    )
                    if per_gemeente.empty:
                        continue
                    per_gemeente = per_gemeente.groupby(level=0).sum(min_count=1)

                    bijschatting = pd.Series(0.0, index=per_gemeente.index)
                    if RECONCILIEER_MET_REGIO and indeling is not None:
                        gebiedstotalen = {}
                        for niveau in _KM_RECONCILIATIE_LADDER:
                            deel = df[df["level"].eq(niveau)]
                            if deel.empty:
                                continue
                            # Translate Klimaatmonitor's internal area codes to
                            # normalised names, which is what municipalities.xlsx
                            # can be matched on. Codes that GeoItems doesn't know
                            # fall back to the normalised code itself.
                            namen = _km_geoitems(headers, niveau)
                            sleutels = deel["gebiedscode"].map(
                                lambda c: namen.get(str(c), _normaliseer_gebiedsnaam(c))
                            )
                            reeks = deel.set_index(sleutels)["waarde"]
                            gebiedstotalen[niveau] = reeks.groupby(level=0).sum(min_count=1)

                        if gebiedstotalen:
                            per_gemeente, bijschatting, rungs = _reconcilieer_variabele(
                                per_gemeente, gebiedstotalen, indeling, gewichten,
                            )
                            if rungs:
                                rungs_gezien.update(rungs)
                                n_gereconcilieerd += 1

                        dekking_rijen.extend(_dekking_voor_variabele(
                            var_code, carrier, groep, jaar, df,
                            gebiedstotalen.get("res"), per_gemeente, indeling, headers,
                        ))

                    deel = per_gemeente.rename("waarde").reset_index()
                    deel["jaar"] = jaar
                    deel["groep"] = groep
                    deel["carrier"] = carrier
                    frames.append(deel)

                    if bijschatting.abs().sum() > 0:
                        bij = bijschatting.rename("bijgeschat").reset_index()
                        bij["jaar"] = jaar
                        bij["carrier"] = carrier
                        bijschat_frames.append(bij)

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

    # Final rung, on the 8-group aggregate: scale up to the RVO-corrected
    # Thema's regional totals where an export is available.
    merged, thema_bij, thema_dekking = _reconcilieer_met_thema(
        merged, indeling, gewichten_per_jaar, headers=headers, jaren=jaren)
    bijschat_frames.extend(thema_bij)
    dekking_rijen.extend(thema_dekking)
    if thema_bij:
        rungs_gezien.add("thema")
        n_gereconcilieerd += len(thema_bij)

    if n_gereconcilieerd:
        log.info(
            "  Fair-share reconciliatie toegepast op %d variabele-jaren; gebruikte "
            "niveaus: %s", n_gereconcilieerd, sorted(rungs_gezien),
        )
    elif RECONCILIEER_MET_REGIO and indeling is not None:
        log.warning(
            "  Fair-share reconciliatie stond aan maar corrigeerde NIETS. Meestal "
            "betekent dit dat de gebiedsnamen niet matchen (zie de "
            "'Gebiedskoppeling'-regels hierboven) of dat elk gebiedstotaal al "
            "gelijk was aan de som van zijn gemeenten."
        )

    _schrijf_dekkingsrapport(dekking_rijen)

    bijschatting_df = _bundel_bijschatting(bijschat_frames, rungs_gezien)
    return merged, bijschatting_df


def _bundel_bijschatting(frames: list[pd.DataFrame], rungs: set[str]) -> pd.DataFrame:
    """Collapse the per-variable imputation bookkeeping into one row per
    (gemeentecode, jaar), so the provenance can ride along in the output CSV."""
    kolommen = ["gemeentecode", "jaar", "elec_bijgeschat", "gas_bijgeschat",
                "bijschatting_niveaus"]
    if not frames:
        return pd.DataFrame(columns=kolommen)

    alles = pd.concat(frames, ignore_index=True)
    breed = alles.pivot_table(
        index=["gemeentecode", "jaar"], columns="carrier", values="bijgeschat", aggfunc="sum"
    ).reset_index()
    breed = breed.rename(columns={"elec": "elec_bijgeschat", "gas": "gas_bijgeschat"})
    for kol in ("elec_bijgeschat", "gas_bijgeschat"):
        if kol not in breed.columns:
            breed[kol] = 0.0
    breed["bijschatting_niveaus"] = "+".join(sorted(rungs)) if rungs else ""
    return breed[kolommen]


def _voeg_bijschatting_toe(
    wide: pd.DataFrame,
    bijschatting: pd.DataFrame | None,
    elec_unit: str,
    gas_unit: str,
) -> pd.DataFrame:
    """Attach the fair-share provenance columns to the finalized wide frame.

    Deliberately named `<carrier>_bijgeschat_<unit>` rather than
    `<carrier>_verbruik_...`: downstream code (make_buurten_csv.py,
    make_gemeenten_potentie_csv.py, MunicipalityImporter.java) selects sector
    columns by the "elec_verbruik_"/"gas_verbruik_" prefix, so these must not
    collide with that namespace or they would be treated as a ninth sector.

    Columns added:
      elec_bijgeschat_<unit>, gas_bijgeschat_<unit>  absolute imputed amount
      elec_bijgeschat_aandeel, gas_bijgeschat_aandeel  imputed / total (0–1)
      bijschatting_niveaus                            which ladder rungs fired
    """
    elec_slug, gas_slug = _unit_slug(elec_unit), _unit_slug(gas_unit)
    elec_kol, gas_kol = f"elec_bijgeschat_{elec_slug}", f"gas_bijgeschat_{gas_slug}"
    wide = wide.copy()

    if bijschatting is None or bijschatting.empty:
        wide[elec_kol] = 0.0
        wide[gas_kol] = 0.0
        wide["elec_bijgeschat_aandeel"] = 0.0
        wide["gas_bijgeschat_aandeel"] = 0.0
        wide["bijschatting_niveaus"] = ""
        return wide

    bij = bijschatting.rename(columns={
        "elec_bijgeschat": elec_kol, "gas_bijgeschat": gas_kol,
    })
    wide = wide.merge(bij, on=["gemeentecode", "jaar"], how="left")
    wide[elec_kol] = pd.to_numeric(wide[elec_kol], errors="coerce").fillna(0.0)
    wide[gas_kol] = pd.to_numeric(wide[gas_kol], errors="coerce").fillna(0.0)
    wide["bijschatting_niveaus"] = wide["bijschatting_niveaus"].fillna("")

    for carrier, kol, slug in (("elec", elec_kol, elec_slug), ("gas", gas_kol, gas_slug)):
        totaal = pd.to_numeric(wide.get(f"{carrier}_verbruik_totaal_{slug}"), errors="coerce")
        wide[f"{carrier}_bijgeschat_aandeel"] = (
            wide[kol] / totaal.where(totaal > 0)
        ).fillna(0.0).round(4)

    aandeel_elec = wide["elec_bijgeschat_aandeel"].mean()
    aandeel_gas = wide["gas_bijgeschat_aandeel"].mean()
    log.info(
        "  Bijschatting: gemiddeld %.1f%% van elektriciteits- en %.1f%% van gasvraag "
        "per gemeente komt uit de regionale fair-share (0%% = volledig eigen opgave)",
        100 * aandeel_elec, 100 * aandeel_gas,
    )
    return wide


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


def _van_klimaatmonitor(jaren: list[int]) -> tuple[pd.DataFrame, str, str, pd.DataFrame] | None:
    """Try Klimaatmonitor ODS. Returns (wide_df, elec_unit, gas_unit, bijschatting_df)
    or None on failure."""
    log.info("Poging 2: Klimaatmonitor Open Data Service ...")
    resultaat = _km_download(jaren)
    if resultaat is None:
        log.error("  Klimaatmonitor: geen data ontvangen.")
        return None
    raw, bijschatting = resultaat
    if raw.empty:
        log.error("  Klimaatmonitor: geen data ontvangen.")
        return None
    return _pivot_per_groep(raw), _KM_ELEC_UNIT, _KM_GAS_UNIT, bijschatting


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

    bijschatting: pd.DataFrame | None = None
    result = _van_cbs(jaren)
    bron_naam, bron_url = CBS_SOURCE_NAME, CBS_SOURCE_URL
    if result is None:
        km = _van_klimaatmonitor(jaren)
        bron_naam, bron_url = _KM_SOURCE_NAME, _KM_SOURCE_URL
        if km is not None:
            *result, bijschatting = km
            result = tuple(result)

    if result is None:
        log.error("Geen enkele bron beschikbaar (CBS en Klimaatmonitor beide mislukt).")
        return None

    wide, elec_unit, gas_unit = result
    wide = _finaliseer_kolommen(wide, elec_unit, gas_unit)
    wide["elec_eenheid"] = elec_unit
    wide["gas_eenheid"] = gas_unit
    wide["energie_bron"] = bron_naam
    wide["energie_bron_url"] = bron_url

    wide = _voeg_bijschatting_toe(wide, bijschatting, elec_unit, gas_unit)

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
