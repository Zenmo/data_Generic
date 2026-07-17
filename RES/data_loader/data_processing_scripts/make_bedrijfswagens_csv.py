"""
Estimate bestelauto and vrachtauto totals per buurt (dasymetrische PC4->buurt verdeling).

Implements the brief in bedrijfswagens-per-buurt-schatting.md, with one deliberate
deviation from its literal data source — documented in detail below and in
data_loader/README.md — because the RDW dataset that doc points to turns out not to
support the bestelauto/vrachtauto split it assumes.

Databronnen
-----------
1. RDW Open Data "Brandstoffen_op_PC4" (Socrata dataset 8wbe-pu7d) — CURRENT,
   maandelijks bijgewerkt aantal bedrijfsvoertuigen per PC4. Verified against a live
   sample: RDW's `voertuigsoort` veld heeft hier geen aparte bestelauto/vrachtauto-
   categorie — beide zitten samengevoegd in een enkele waarde "Bedrijfsauto". (De
   "Voertuigen met brandstoffen per postcode" pagina die de opdracht noemt, dataset-ID
   ivky-pcsj, is zelf een Socrata "story" die naar dit dataset linkt, niet de data zelf.)
2. CBS StatLine 85236NED ("Motorvoertuigen actief; voertuigtype, postcode, regio,
   1 januari, 2019-2023") — de enige gevonden bron die Bestelauto en Vrachtauto (excl.
   trekker) apart per PC4 EN per gemeente publiceert, verified against live sample data
   (geen onderdrukking van kleine aantallen zoals bij RDW). Beperking: bevroren op
   1 januari 2023 — nog niet bijgewerkt naar een latere jaargang op het moment van
   schrijven (2026-07).
3. CBS Kerncijfers wijken en buurten (cbs_kwb.haal_kwb) — bedrijfsvestigingen_totaal en
   hj_vervoer_informatie_en_communicatie per buurt, als dasymetrisch gewicht (proxy voor
   waar bedrijfsvoertuigen waarschijnlijk staan). Gehaald los van de buurten-CSV zelf
   (niet via make_buurten_csv.py) zodat deze fase geen volgorde-afhankelijkheid heeft
   op --buurten.
4. CBS Wijk-en-buurtkaart + CBS PC4-vlakken (download_sources.py, al gedownload voor
   de rest van de pipeline) — voor de ruimtelijke overlay tussen PC4 en buurt.

Methode
-------
RDW's huidige gecombineerde "Bedrijfsauto"-aantal per PC4 wordt gesplitst in bestelauto/
vrachtauto met het officiele CBS-aandeel voor diezelfde PC4 (peildatum 1-1-2023; valt
terug op het gemeente-aandeel, dan het landelijke aandeel, als de PC4 niet in de
CBS-tabel voorkomt — bijv. een postcode die na 2023 is toegevoegd). Vervolgens wordt elk
van die twee aantallen dasymetrisch verdeeld over de buurten die met dat PC4-vlak
overlappen, gewogen naar
    gewicht(buurt) x (overlap-oppervlak / totale buurt-oppervlak)
met gewicht = bedrijfsvestigingen sector H+J, valt terug op bedrijfsvestigingen totaal,
valt terug op kaal buurt-oppervlak. Een tweede, kaal-oppervlakte-gewogen verdeling wordt
apart berekend en samen met de eerste gerapporteerd als ondergrens/bovengrens-bandbreedte
(zie stap 5 van de opdracht).

Belangrijke caveats (zie ook data_loader/README.md)
----------------------------------------------------
- Dit is een MODELSCHATTING, geen officiele telling.
- Lease-/verhuurvertekening: bedrijfswagens van lease-/verhuurbedrijven staan
  geregistreerd op het adres van dat bedrijf, niet van de feitelijke gebruiker.
- PC4- en buurtgrenzen zijn onafhankelijk vastgesteld en overlappen zelden netjes.
- Peildata-mismatch: de RDW-telling is actueel (zie bedrijfswagens_rdw_peildatum in de
  output), maar de bestelauto/vrachtauto-splitsing komt van CBS-cijfers over 1-1-2023 —
  een aandeelsverschuiving sindsdien wordt niet meegenomen.
- De validatie tegen CBS-gemeentetotalen (zie _valideer_gemeenten) vergelijkt dus per
  definitie een actueel RDW-gebaseerd cijfer met een CBS-cijfer van (vaak) een paar jaar
  eerder — een verschil is te verwachten en is geen teken van een fout in de verdeling.

Output: processed/bedrijfswagens_buurten_{jaar}_{datum}.csv (per buurt) en
processed/bedrijfswagens_validatie_gemeenten_{jaar}_{datum}.csv (validatie-overzicht per
gemeente). Wordt gemerged in de buurten/gemeenten-CSV's door make_buurten_csv.py resp.
make_gemeenten_potentie_csv.py, net als de ElaadNL- en solar-fases.

Run standalone:  python make_bedrijfswagens_csv.py [2023 [2024]]
Or via:          python run_pipeline.py --bedrijfswagens
"""

import logging
import sys
import time
from datetime import date

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely

import download_sources
import process_features
from config import CBS_YEARS, OUTPUT_SEPARATOR, FILL_UNMATCHED, PROCESSED_DIR, RAW_DIR

log = logging.getLogger(__name__)
TODAY = date.today().isoformat()

# --- RDW: huidig gecombineerd "Bedrijfsauto"-aantal per PC4 ---
# Dataset-ID gevonden via https://opendata.rdw.nl/api/catalog/v1?q=... ; de
# "Voertuigen met brandstoffen per postcode"-pagina (ivky-pcsj) is een Socrata "story"
# die naar dit onderliggende tabulaire dataset linkt.
_RDW_DATASET_ID = "8wbe-pu7d"
_RDW_RESOURCE_URL = f"https://opendata.rdw.nl/resource/{_RDW_DATASET_ID}.json"
_RDW_CACHE_DIR = RAW_DIR / "rdw" / "bedrijfsauto_pc4"

# --- CBS: officiele bestelauto/vrachtauto-splitsing, per PC4 en per gemeente ---
_CBS_SPLIT_TABLE = "85236NED"
_CBS_SPLIT_PERIODE = "2023JJ00"  # meest recente periode in deze tabel (gecontroleerd 2026-07)

_MISSING = -99999  # zelfde sentinel-conventie als make_buurten_csv.py


# ---------------------------------------------------------------------------
# 1. RDW: huidig aantal bedrijfsauto's per PC4
# ---------------------------------------------------------------------------

def download_rdw_bedrijfsauto_pc4(force: bool = False) -> pd.DataFrame:
    """
    Download het huidige (maandelijks bijgewerkte) aantal RDW-geregistreerde
    bedrijfsauto's per PC4, gesommeerd over brandstof/extern_oplaadbaar (we hebben
    alleen het totaal-aantal nodig, niet de brandstofuitsplitsing).

    RDW's voertuigsoort-indeling maakt in dit dataset GEEN onderscheid tussen
    bestelauto en vrachtauto — beide vallen onder "Bedrijfsauto" (geverifieerd tegen
    een live steekproef: de enige voertuigsoort-waarden zijn Personenauto, Bedrijfsauto,
    Motorfiets, Bromfiets en een aantal niet-relevante categorieen). De splitsing wordt
    achteraf toegepast met het CBS-aandeel, zie haal_cbs_bestel_vracht_split().

    Cache: raw/rdw/bedrijfsauto_pc4/{datum}.csv — hergebruikt binnen dezelfde dag
    tenzij force=True.
    """
    _RDW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _RDW_CACHE_DIR / f"{TODAY}.csv"
    if cache_path.exists() and not force:
        log.info("  RDW Bedrijfsauto/PC4: gecached, overgeslagen (%s)", cache_path.name)
        return pd.read_csv(cache_path, sep=OUTPUT_SEPARATOR, dtype={"pc4": str})

    log.info("  RDW Bedrijfsauto/PC4 downloaden (dataset %s)...", _RDW_DATASET_ID)
    rows: list[dict] = []
    limit = 5000
    offset = 0
    while True:
        params = {
            "$select": "postcode, sum(aantal) as aantal",
            "$where": "voertuigsoort='Bedrijfsauto'",
            "$group": "postcode",
            "$limit": limit,
            "$offset": offset,
        }
        resp = requests.get(_RDW_RESOURCE_URL, params=params, timeout=60)
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        rows.extend(page)
        offset += limit
        if len(page) < limit:
            break

    if not rows:
        raise RuntimeError(
            "RDW Bedrijfsauto/PC4: 0 rijen ontvangen — controleer dataset-ID/endpoint "
            f"({_RDW_RESOURCE_URL})"
        )

    df = pd.DataFrame(rows)
    df = df.rename(columns={"postcode": "pc4"})
    df["pc4"] = pd.to_numeric(df["pc4"], errors="coerce").astype("Int64").astype(str)
    df["aantal"] = pd.to_numeric(df["aantal"], errors="coerce").fillna(0).astype(int)

    tmp = cache_path.with_suffix(".tmp.csv")
    df.to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
    tmp.replace(cache_path)
    log.info(
        "  RDW Bedrijfsauto/PC4: %d postcodes, landelijk totaal %d voertuigen",
        len(df), int(df["aantal"].sum()),
    )
    return df


# ---------------------------------------------------------------------------
# 2. CBS: officiele bestelauto/vrachtauto-splitsing (85236NED, peildatum 1-1-2023)
# ---------------------------------------------------------------------------

def haal_cbs_bestel_vracht_split() -> tuple[pd.Series, pd.Series, float, pd.DataFrame, pd.Series]:
    """
    Haalt het officiele CBS-aandeel bestelauto (t.o.v. bestelauto+vrachtauto) op uit
    StatLine-tabel 85236NED, voor de meest recente beschikbare periode (1-1-2023 —
    deze tabel is nog niet bijgewerkt naar een latere jaargang, gecontroleerd 2026-07).

    Retourneert:
      pc4_ratio      : Series, index=PC4-code (str, bv. "1000"), bestelauto-aandeel.
      gem_ratio      : Series, index=gemeentecode (bv. "GM0505"), zelfde aandeel op
                       gemeenteniveau — gebruikt als terugval voor PC4's die niet in de
                       tabel voorkomen, en als validatie-basis.
      national_ratio : float, landelijk aandeel — laatste terugval.
      gem_totalen    : DataFrame, index=gemeentecode, kolommen 'bestelauto_cbs2023' en
                       'vrachtauto_cbs2023' — officiele gemeente-totalen, alleen gebruikt
                       voor de validatie-cross-check (niet gemengd met de RDW-gebaseerde
                       schatting zelf).
      pc4_to_gemcode : Series, index=PC4-code, gemeentecode zoals CBS die zelf aan die
                       PC4 koppelt (Gemeentecode_22) — gebruikt om, voor een PC4 die wel
                       in de tabel staat maar zonder positieve bestelauto+vrachtauto-som,
                       het juiste gemeente-aandeel op te zoeken.

    Haalt de data rechtstreeks op via de OData TypedDataSet-endpoint (met `requests`),
    NIET via `cbsodata.get_data()`: die laatste vervangt `RegioS`-waarden stilzwijgend
    door hun leesbare titel (bv. "GM0505" -> "Dordrecht", "PC1000" -> "1000") voor elke
    kolom die in de brontabel als OData "Dimension" is getypeerd — geverifieerd doordat
    `cbsodata.get_data('85236NED', ...)` voor elke rij `RegioS` als "Nederland, totaal"/
    "Dordrecht"/etc. teruggaf in plaats van "NL01"/"GM0505". Dat breekt de PC4/gemeente/
    landelijk-herkenning hieronder volledig (alle `str.startswith("PC"/"GM")`-checks
    treffen dan niets), vandaar de rechtstreekse OData-aanroep. `cbs_kwb.haal_kwb()`
    heeft dit probleem niet: in de "kerncijfers wijken en buurten"-tabellen die dat
    gebruikt is de buurtcode-kolom een gewoon Topic-attribuut, geen Dimension.
    """
    log.info(
        "  CBS %s ophalen (bestelauto/vrachtauto-splitsing, peildatum 1-1-2023)...",
        _CBS_SPLIT_TABLE,
    )
    # This legacy "ODataApi" endpoint (as opposed to the newer ODataFeed) does not
    # support $skip at all (confirmed: a $skip param, even 0, gets a 500). A single
    # request suffices here — the filtered result (one period, all regio-levels
    # combined) is ~5100 rows, comfortably under this endpoint's per-request cap.
    select = "RegioS,Bestelauto_5,VrachtautoExclTrekkerVoorOplegger_6,Gemeentecode_22"
    filt = f"Perioden eq '{_CBS_SPLIT_PERIODE}'"
    url = f"https://opendata.cbs.nl/ODataApi/OData/{_CBS_SPLIT_TABLE}/TypedDataSet"
    resp = requests.get(url, params={"$filter": filt, "$select": select, "$format": "json"}, timeout=60)
    resp.raise_for_status()
    rows = resp.json().get("value", [])

    if not rows:
        raise RuntimeError(f"CBS {_CBS_SPLIT_TABLE}: 0 rijen ontvangen voor periode {_CBS_SPLIT_PERIODE}")

    df = pd.DataFrame(rows)
    df["RegioS"] = df["RegioS"].str.strip()
    df["Gemeentecode_22"] = df["Gemeentecode_22"].astype(str).str.strip()

    bestel = pd.to_numeric(df["Bestelauto_5"], errors="coerce")
    vracht = pd.to_numeric(df["VrachtautoExclTrekkerVoorOplegger_6"], errors="coerce")
    totaal = bestel + vracht
    ratio = bestel / totaal.replace(0, np.nan)

    is_pc4 = df["RegioS"].str.startswith("PC")
    is_gem = df["RegioS"].str.startswith("GM")
    is_nl = df["RegioS"] == "NL01"

    pc4_codes = df.loc[is_pc4, "RegioS"].str.replace("PC", "", regex=False)
    pc4_ratio = pd.Series(ratio[is_pc4].values, index=pc4_codes).dropna()
    gem_ratio = pd.Series(ratio[is_gem].values, index=df.loc[is_gem, "RegioS"]).dropna()

    if is_nl.any() and pd.notna(ratio[is_nl].iloc[0]):
        national_ratio = float(ratio[is_nl].iloc[0])
    else:
        national_ratio = float(gem_ratio.mean())

    gem_totalen = pd.DataFrame(
        {"bestelauto_cbs2023": bestel[is_gem].values, "vrachtauto_cbs2023": vracht[is_gem].values},
        index=df.loc[is_gem, "RegioS"],
    )

    gemcode_padded = "GM" + df.loc[is_pc4, "Gemeentecode_22"].str.zfill(4)
    pc4_to_gemcode = pd.Series(gemcode_padded.values, index=pc4_codes)

    log.info(
        "  CBS-splitsing geladen: %d PC4's, %d gemeenten, landelijk aandeel bestelauto=%.1f%%",
        len(pc4_ratio), len(gem_ratio), national_ratio * 100,
    )
    return pc4_ratio, gem_ratio, national_ratio, gem_totalen, pc4_to_gemcode


# ---------------------------------------------------------------------------
# 3. Ruimtelijke overlay PC4 <-> buurt
# ---------------------------------------------------------------------------

def _overlay_pc4_buurt(
    pc4_gdf: gpd.GeoDataFrame, buurt_gdf: gpd.GeoDataFrame, pc4_col: str, buurt_col: str,
) -> pd.DataFrame:
    """
    Voor elk (PC4, buurt)-paar dat ruimtelijk overlapt: bereken het overlap-oppervlak
    en de totale oppervlakte van die buurt (nodig voor de 'fractie van de buurt die
    binnen dit PC4 valt'-weging uit stap 3 van de opdracht).

    Zelfde vectorized sjoin + shapely.intersection/area-aanpak als
    process_features._match_polygon, maar hier bewaren we ALLE overlappende paren met
    hun oppervlak (niet alleen de grootste), want we willen een aandeel per buurt
    berekenen, niet de ene 'beste' match kiezen.

    Retourneert een DataFrame met kolommen: pc4, buurtcode, overlap_m2, buurt_area_m2.
    Sliver-overlaps kleiner dan 1 m2 (afrondingsartefacten van onafhankelijk
    vastgestelde grenzen) worden weggefilterd.
    """
    buurt = buurt_gdf[[buurt_col, "geometry"]].rename(columns={buurt_col: "buurtcode"}).copy()
    buurt = buurt.reset_index(drop=True)
    buurt["_seq"] = buurt.index
    buurt["buurt_area_m2"] = shapely.area(buurt.geometry.values)

    pc4 = pc4_gdf[[pc4_col, "geometry"]].rename(columns={pc4_col: "pc4"}).copy()
    pc4 = pc4.reset_index(drop=True)
    # Normalise to plain "1011"-style strings (matches rdw/CBS pc4 index formatting
    # below) — the PDOK GeoPackage column can come back as int64 or object depending
    # on the GDAL driver, so a bare .astype(str) risks producing "1011.0".
    pc4["pc4"] = pd.to_numeric(pc4["pc4"], errors="coerce").astype("Int64").astype(str)

    cands = gpd.sjoin(buurt, pc4, how="inner", predicate="intersects")
    cands = cands.reset_index(drop=True)

    buurt_geoms = buurt.loc[cands["_seq"].values, "geometry"].values
    pc4_idx = cands["index_right"].astype(int).values
    pc4_geoms = pc4.loc[pc4_idx, "geometry"].values

    inter = shapely.intersection(buurt_geoms, pc4_geoms)
    cands["overlap_m2"] = shapely.area(inter)

    result = cands[["pc4", "buurtcode", "overlap_m2"]].merge(
        buurt[["buurtcode", "buurt_area_m2"]].drop_duplicates("buurtcode"),
        on="buurtcode", how="left",
    )
    result = result[result["overlap_m2"] > 1.0].reset_index(drop=True)
    return result


# ---------------------------------------------------------------------------
# 4. Buurt-gewicht (dasymetrische weging)
# ---------------------------------------------------------------------------

def _bedrijfsvestigingen_gewicht(
    kwb_hj: pd.Series, kwb_totaal: pd.Series, buurt_area_m2: pd.Series,
) -> tuple[pd.Series, pd.Series]:
    """
    Gewicht per buurt voor de 'bedrijfsvestigingen'-variant: hj_vervoer_informatie_en_
    communicatie (a_bed_hj-proxy) als aanwezig en > 0, anders bedrijfsvestigingen_totaal
    (a_bedv-proxy) als aanwezig en > 0, anders kaal buurt-oppervlak als laatste redmiddel
    (een buurt zonder enige bekende bedrijfsvestiging moet toch een gewicht > 0 hebben,
    anders valt hij weg uit de verdeling van zijn PC4).

    Retourneert (gewicht, methode) — methode registreert per buurt welke trap is
    gebruikt, voor de 'bedrijfswagens_gewicht_methode'-kolom in de output.
    """
    # Callers pass kwb_hj/kwb_totaal already reindexed to buurt_area_m2.index.
    hj = pd.to_numeric(kwb_hj, errors="coerce").replace(_MISSING, np.nan)
    tot = pd.to_numeric(kwb_totaal, errors="coerce").replace(_MISSING, np.nan)

    methode = pd.Series("oppervlakte", index=buurt_area_m2.index)
    gewicht = buurt_area_m2.copy()

    use_tot = tot.notna() & (tot > 0)
    gewicht[use_tot] = tot[use_tot]
    methode[use_tot] = "bedrijfsvestigingen_totaal"

    use_hj = hj.notna() & (hj > 0)
    gewicht[use_hj] = hj[use_hj]
    methode[use_hj] = "bedrijfsvestigingen_hj"

    return gewicht, methode


# ---------------------------------------------------------------------------
# 5. Dasymetrische verdeling PC4 -> buurt
# ---------------------------------------------------------------------------

def _distribueer(pc4_totalen: pd.Series, overlay: pd.DataFrame, gewicht: pd.Series) -> pd.DataFrame:
    """
    Verdeelt elke PC4-waarde over zijn overlappende buurten, gewogen naar
        gewicht(buurt) x (overlap_m2 / buurt_area_m2)
    genormaliseerd zodat de aandelen binnen een PC4 optellen tot 1. Als alle
    overlappende buurten van een PC4 gewicht 0 hebben, wordt gelijk verdeeld in plaats
    van de PC4-waarde te laten vervallen (zelfde terugval als
    make_buurten_csv._verdeel_energieverbruik_sector's 'gelijk_verdeeld'-mechanisme).

    Retourneert de detail-DataFrame (een rij per (pc4, buurt)-paar) met een
    'bijdrage'-kolom en een 'gelijk_verdeeld'-vlag — zowel de buurt-som als de
    conserveringscontrole per PC4 worden hieruit afgeleid door de aanroeper.
    """
    df = overlay.copy()
    df["gewicht_buurt"] = df["buurtcode"].map(gewicht).fillna(0.0)
    df["fractie_in_pc4"] = df["overlap_m2"] / df["buurt_area_m2"]
    df["gewicht_component"] = df["gewicht_buurt"] * df["fractie_in_pc4"]

    totaal_per_pc4 = df.groupby("pc4")["gewicht_component"].transform("sum")
    n_per_pc4 = df.groupby("pc4")["buurtcode"].transform("count")

    df["gelijk_verdeeld"] = totaal_per_pc4 <= 0
    df["aandeel"] = np.where(
        df["gelijk_verdeeld"], 1.0 / n_per_pc4, df["gewicht_component"] / totaal_per_pc4.replace(0, np.nan),
    )

    df["pc4_waarde"] = df["pc4"].map(pc4_totalen).fillna(0.0)
    df["bijdrage"] = df["pc4_waarde"] * df["aandeel"]
    return df


# ---------------------------------------------------------------------------
# 6. Validatie
# ---------------------------------------------------------------------------

def _valideer_conservering(detail: pd.DataFrame, pc4_totalen: pd.Series, label: str) -> None:
    """
    Sanity check: de herverdeelde buurt-bijdragen per PC4 moeten (bijna) precies
    optellen tot de originele PC4-waarde — dit volgt wiskundig uit de normalisatie in
    _distribueer(), dus een afwijking wijst op een fout in de overlay/weging, niet op
    een data-kwaliteitsprobleem. Gelogd als waarschuwing, niet als caveat in de output.
    """
    som_per_pc4 = detail.groupby("pc4")["bijdrage"].sum()
    vergelijk = pd.DataFrame({"verdeeld": som_per_pc4, "origineel": pc4_totalen}).dropna()
    afwijking = (vergelijk["verdeeld"] - vergelijk["origineel"]).abs()
    n_afwijkend = int((afwijking > 0.5).sum())
    if n_afwijkend:
        log.warning(
            "  [%s] Conserveringscontrole: %d/%d PC4's wijken >0.5 af tussen verdeeld "
            "en origineel totaal — controleer de overlay/weging (dit hoort ~0 te zijn "
            "door constructie).",
            label, n_afwijkend, len(vergelijk),
        )
    else:
        log.info(
            "  [%s] Conserveringscontrole OK: alle %d PC4's herverdelen exact naar hun origineel totaal.",
            label, len(vergelijk),
        )


def _valideer_gemeenten(
    out: pd.DataFrame, buurt_to_gem: pd.Series, gem_totalen_cbs: pd.DataFrame, jaar: int,
) -> pd.DataFrame:
    """
    Vergelijkt de buurt-sommen per gemeente (uit onze RDW-gebaseerde, actuele schatting)
    met CBS's eigen officiele gemeente-totalen uit dezelfde 85236NED-tabel (peildatum
    1-1-2023). Geen exacte match verwacht — RDW is actueel, CBS-basis is een paar jaar
    ouder — dit is een informatieve cross-check, geen pass/fail-poort.

    Retourneert het validatie-overzicht (ook weggeschreven als aparte CSV in main()).
    """
    gem_som = out.copy()
    gem_som["gemeentecode"] = gem_som["buurtcode"].map(buurt_to_gem)
    per_gem = gem_som.groupby("gemeentecode")[["bestelautos_totaal", "vrachtautos_totaal"]].sum()
    per_gem = per_gem.rename(columns={
        "bestelautos_totaal": "bestelauto_buurtsom_actueel",
        "vrachtautos_totaal": "vrachtauto_buurtsom_actueel",
    })

    vergelijk = per_gem.join(gem_totalen_cbs, how="left")
    for kind in ("bestelauto", "vrachtauto"):
        actueel = vergelijk[f"{kind}_buurtsom_actueel"]
        basis = vergelijk[f"{kind}_cbs2023"]
        vergelijk[f"{kind}_afwijking_pct"] = np.where(
            basis > 0, 100 * (actueel - basis) / basis, np.nan,
        )

    log.info(
        "  [%d] Validatie tegen CBS-gemeentetotalen (1-1-2023): mediane afwijking "
        "bestelauto=%.1f%%, vrachtauto=%.1f%% (positief = onze actuele schatting hoger "
        "dan CBS-2023 — verwacht bij fleet-groei sinds 2023, geen fout op zich)",
        jaar,
        vergelijk["bestelauto_afwijking_pct"].median(),
        vergelijk["vrachtauto_afwijking_pct"].median(),
    )
    return vergelijk.reset_index().rename(columns={"index": "gemeentecode"})


# ---------------------------------------------------------------------------
# 7. Orchestratie per jaar
# ---------------------------------------------------------------------------

def genereer_schatting(
    jaar: int,
    rdw_pc4: pd.DataFrame,
    pc4_ratio: pd.Series,
    gem_ratio: pd.Series,
    national_ratio: float,
    pc4_to_gemcode: pd.Series,
) -> pd.DataFrame:
    """Bouwt de volledige per-buurt bestelauto/vrachtauto-schatting voor 1 geometrie-jaar."""
    if not (RAW_DIR / "cbs_wijkenbuurten" / str(jaar) / "buurten.gpkg").exists():
        download_sources.download_cbs_wijkenbuurten(jaar, "buurten")
    if not (RAW_DIR / "cbs_pc4" / str(jaar) / "pc4.gpkg").exists():
        download_sources.download_cbs_postcode("pc4", jaar)

    buurt_gdf = process_features._load_admin("buurten", jaar)
    pc4_gdf = process_features._load_admin("pc4", jaar)
    if buurt_gdf is None or pc4_gdf is None:
        raise FileNotFoundError(f"Buurten- of PC4-geometrie voor {jaar} ontbreekt na download-poging.")

    # Sluit water-buurten en "Buitenland" uit vóór de overlay: CBS' eigen
    # kerncijfers_buurten-publicatie bevat sowieso geen rij voor ze (water=="NEE"
    # telt exact 14.421/14.574 = precies het aantal rijen in
    # kerncijfers_buurten_{2023,2024}_met_geometrie.csv), dus een bestelauto/
    # vrachtauto-schatting daarvoor kan toch nooit als J_Neighborhood geladen
    # worden. Zonder dit filter kreeg zo'n buurt (bv. BU05059997, een haven-/
    # industriegebied zonder woonbevolking) een deel van de PC4-verdeling
    # toebedeeld dat daarmee permanent onbereikbaar was voor het model — nu
    # valt dat aandeel automatisch terug naar de overlappende buurten die wél
    # geladen worden.
    if "water" in buurt_gdf.columns:
        n_voor = len(buurt_gdf)
        buurt_gdf = buurt_gdf[buurt_gdf["water"] == "NEE"].copy()
        log.info("  %d/%d buurten uitgesloten (water of Buitenland, geen rij in kerncijfers_buurten)",
                 n_voor - len(buurt_gdf), n_voor)

    buurt_col = process_features._find_code_col(buurt_gdf, "buurten")
    pc4_col = process_features._find_code_col(pc4_gdf, "pc4")
    overlay = _overlay_pc4_buurt(pc4_gdf, buurt_gdf, pc4_col, buurt_col)

    from cbs_kwb import haal_kwb
    kwb = haal_kwb(jaar, kolommen=[
        "hj_vervoer_informatie_en_communicatie", "bedrijfsvestigingen_totaal",
        "dekkingspercentage",
    ]).set_index("codering")

    buurt_area = overlay.drop_duplicates("buurtcode").set_index("buurtcode")["buurt_area_m2"]
    gewicht_bedrijven, methode_bedrijven = _bedrijfsvestigingen_gewicht(
        kwb["hj_vervoer_informatie_en_communicatie"].reindex(buurt_area.index),
        kwb["bedrijfsvestigingen_totaal"].reindex(buurt_area.index),
        buurt_area,
    )
    gewicht_oppervlak = buurt_area

    buurt_to_gem = pd.Series("GM" + buurt_area.index.str[2:6], index=buurt_area.index)

    # PC4 -> gemeente-aandeel bepalen: CBS's eigen koppeling (Gemeentecode_22) als de
    # PC4 in de splitsingstabel voorkomt; anders de gemeente met het grootste
    # overlap-oppervlak uit onze eigen ruimtelijke overlay.
    overlay_gem = overlay.copy()
    overlay_gem["gemeentecode"] = overlay_gem["buurtcode"].map(buurt_to_gem)
    dominante_gem_per_pc4 = (
        overlay_gem.groupby(["pc4", "gemeentecode"])["overlap_m2"].sum()
        .groupby(level="pc4").idxmax().apply(lambda t: t[1])
    )

    def _aandeel_voor_pc4(pc4: str) -> float:
        if pc4 in pc4_ratio.index:
            return pc4_ratio[pc4]
        gem = pc4_to_gemcode.get(pc4, dominante_gem_per_pc4.get(pc4))
        if gem in gem_ratio.index:
            return gem_ratio[gem]
        return national_ratio

    rdw = rdw_pc4.set_index("pc4")["aantal"]
    aandelen = pd.Series({pc4: _aandeel_voor_pc4(pc4) for pc4 in rdw.index})
    bestelauto_pc4 = rdw * aandelen
    vrachtauto_pc4 = rdw * (1 - aandelen)

    detail_bestel_bedrijven = _distribueer(bestelauto_pc4, overlay, gewicht_bedrijven)
    detail_vracht_bedrijven = _distribueer(vrachtauto_pc4, overlay, gewicht_bedrijven)
    detail_bestel_opp = _distribueer(bestelauto_pc4, overlay, gewicht_oppervlak)
    detail_vracht_opp = _distribueer(vrachtauto_pc4, overlay, gewicht_oppervlak)

    _valideer_conservering(detail_bestel_bedrijven, bestelauto_pc4, f"{jaar}/bestelauto")
    _valideer_conservering(detail_vracht_bedrijven, vrachtauto_pc4, f"{jaar}/vrachtauto")

    bestel_primair = detail_bestel_bedrijven.groupby("buurtcode")["bijdrage"].sum()
    vracht_primair = detail_vracht_bedrijven.groupby("buurtcode")["bijdrage"].sum()
    bestel_alt = detail_bestel_opp.groupby("buurtcode")["bijdrage"].sum()
    vracht_alt = detail_vracht_opp.groupby("buurtcode")["bijdrage"].sum()
    gelijk_verdeeld = (
        detail_bestel_bedrijven.groupby("buurtcode")["gelijk_verdeeld"].any()
        | detail_vracht_bedrijven.groupby("buurtcode")["gelijk_verdeeld"].any()
    )

    out = pd.DataFrame(index=buurt_area.index)
    out.index.name = "buurtcode"
    out["bestelautos_totaal"] = bestel_primair.reindex(out.index).round().fillna(0).astype(int)
    out["vrachtautos_totaal"] = vracht_primair.reindex(out.index).round().fillna(0).astype(int)
    bestel_range = pd.concat([bestel_primair, bestel_alt], axis=1)
    vracht_range = pd.concat([vracht_primair, vracht_alt], axis=1)
    out["bestelautos_ondergrens"] = bestel_range.min(axis=1).reindex(out.index).round().fillna(0).astype(int)
    out["bestelautos_bovengrens"] = bestel_range.max(axis=1).reindex(out.index).round().fillna(0).astype(int)
    out["vrachtautos_ondergrens"] = vracht_range.min(axis=1).reindex(out.index).round().fillna(0).astype(int)
    out["vrachtautos_bovengrens"] = vracht_range.max(axis=1).reindex(out.index).round().fillna(0).astype(int)
    out["bedrijfswagens_gewicht_methode"] = methode_bedrijven.reindex(out.index)
    out["bedrijfswagens_pc4_gelijk_verdeeld"] = gelijk_verdeeld.reindex(out.index).fillna(False)
    out["bedrijfswagens_pc4_dekkingsklasse"] = kwb["dekkingspercentage"].reindex(out.index)
    out["bedrijfswagens_rdw_peildatum"] = TODAY
    out["bedrijfswagens_cbs_split_periode"] = _CBS_SPLIT_PERIODE

    return out.reset_index()


# ---------------------------------------------------------------------------
# 8. Main
# ---------------------------------------------------------------------------

def main(jaren: list[int] | None = None) -> bool:
    jaren = jaren or list(CBS_YEARS)
    all_ok = True
    t0 = time.monotonic()

    try:
        rdw_pc4 = download_rdw_bedrijfsauto_pc4()
        pc4_ratio, gem_ratio, national_ratio, gem_totalen_cbs, pc4_to_gemcode = haal_cbs_bestel_vracht_split()
    except Exception as exc:
        log.error("Kon RDW/CBS bron-data niet laden: %s", exc, exc_info=True)
        return False

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    for jaar in jaren:
        try:
            out = genereer_schatting(jaar, rdw_pc4, pc4_ratio, gem_ratio, national_ratio, pc4_to_gemcode)
        except Exception as exc:
            log.error("FOUT jaar %d: %s", jaar, exc, exc_info=True)
            all_ok = False
            continue

        buurt_path = PROCESSED_DIR / f"bedrijfswagens_buurten_{jaar}_{TODAY}.csv"
        tmp = buurt_path.with_suffix(".tmp.csv")
        out.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(buurt_path)
        log.info("  Opgeslagen: %s (%d buurten)", buurt_path.name, len(out))

        buurt_to_gem = pd.Series("GM" + out["buurtcode"].str[2:6].values, index=out["buurtcode"])
        validatie = _valideer_gemeenten(out, buurt_to_gem, gem_totalen_cbs, jaar)
        val_path = PROCESSED_DIR / f"bedrijfswagens_validatie_gemeenten_{jaar}_{TODAY}.csv"
        val_tmp = val_path.with_suffix(".tmp.csv")
        validatie.fillna(FILL_UNMATCHED).to_csv(val_tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        val_tmp.replace(val_path)
        log.info("  Validatie-overzicht opgeslagen: %s", val_path.name)

    elapsed = time.monotonic() - t0
    log.info("Bedrijfswagens-schatting klaar in %.0fs", elapsed)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    jaren = [int(a) for a in sys.argv[1:]] or None
    sys.exit(0 if main(jaren) else 1)
