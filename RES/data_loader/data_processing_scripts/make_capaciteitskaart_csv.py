"""
Download en verwerk de Capaciteitskaart (data.partnersinenergie.nl) tot een
transformer-netwerk (RNB + TenneT) gekoppeld aan buurten.

Bron: https://data.partnersinenergie.nl/capaciteitskaart/info/algemene-info
Documentatie van de brondata zelf: raw/capaciteitskaart/{datum}/brondata_documentatie.txt
(meegedownload, zie download_capaciteitskaart()).

Structuur van de brondata (geverifieerd tegen de echte bestanden, niet alleen de
documentatie — versie 2.0, 2024-11-20):

  congestie_pc6.csv (~462.600 rijen, 1 per PC6)
      De centrale koppeltabel: postcode -> voedingsgebied_id (RNB) EN tennet_id
      (TenneT) tegelijk, plus RNB_postcode (netbeheerder), Gemeentecode/naam.
  voedingsgebieden.csv (608 rijen, 1 per voedingsgebied_id)
      RNB-laag: transportcapaciteit/wachtrij per gebied. Eenmalige snapshot,
      jaar is altijd 2026 (geen tijdreeks, ondanks de kolomnaam).
  tennetgebieden.csv (150 rijen, 22 unieke congestiegebied x jaar 2026-2036)
      TenneT-laag: transportcapaciteit/wachtrij per congestiegebied. Wél een
      echte meerjarenreeks (2026-2036) — een structureel verschil met de
      RNB-laag, niet alleen "TenneT vs RNB" maar ook "voorspelling vs snapshot".
  tennetcongestie.csv (271 rijen, 1 per tennet_id)
      Koppelt elk TenneT-tussenstation aan zijn congestiegebied — apart voor
      afname en invoeding/opwek-richting (225/271 = 83% heeft dezelfde zone
      voor beide richtingen; 46/271 = 17% heeft een andere zone per richting).

Geverifieerde referentiële integriteit (2026-07-09 download):
  - voedingsgebied_id: 100% match in beide richtingen tussen congestie_pc6.csv
    en voedingsgebieden.csv.
  - tennet_id: elke tennet_id in congestie_pc6.csv bestaat in tennetcongestie.csv
    (tennetcongestie.csv heeft 32 stations die door geen enkele postcode worden
    gerefereerd — waarschijnlijk stations zonder rechtstreeks aangesloten
    postcodegebied, bv. pure koppelstations).
  - congestiegebied-namen komen overeen tussen tennetcongestie.csv en
    tennetgebieden.csv op één spatie-artefact na ("Groningen Oost " met
    trailing space) — hieronder genormaliseerd met .str.strip().

Hiërarchie RNB <-> TenneT (uit congestie_pc6.csv afgeleid, niet uit een
rechtstreekse kolom — voedingsgebieden.csv zelf bevat geen tennet_id):
  536 van de 608 (88%) voedingsgebieden hebben precies 1 TenneT-ouder.
  36 van de 608 (6%) hebben 2-4 TenneT-ouders (redundante voeding, geen fout).
  Vandaar transformer_links.csv als aparte junction-tabel in plaats van een
  enkele parent_id-kolom, die de 6% meervoudige-ouder-gevallen niet zou passen.

Methode
-------
1. Download de 4 brondata-CSV's (+ documentatie) van data.partnersinenergie.nl.
2. Bouw transformers.csv: één "node" per RNB-voedingsgebied of TenneT-station,
   met alle capaciteit/wachtrij-kolommen van beide bronnen (identieke
   kolomnamen in voedingsgebieden.csv en tennetgebieden.csv, dus rechtstreeks
   te unioneren) plus een `type`-kolom ("RNB"/"TenneT"). RNB-rijen worden
   herhaald over alle jaren 2026-2036 (enkel-jaar brondata, maar één
   samenhangende tabel met TenneT's meerjarenreeks is bruikbaarder dan een
   aparte snapshot-tabel). Drie kolommen zijn niet in de brondata aanwezig en
   worden hier afgeleid (zie bouw_transformer_geometrie() / _bouw_primary_parent()
   / _extract_voltage_kv()):
     - latitude/longitude/service_area_polygon: centroid + dissolved PC6-vlakken
       per transformer_id (PC6 -> voedingsgebied_id/tennet_id uit congestie_pc6.csv).
     - primary_parent_id: voor RNB-rijen de TenneT-ouder met de meeste gekoppelde
       PC6's (voor de 6% met 2-4 ouders — zie hierarchy-sectie hieronder); voor
       TenneT-rijen leeg (bovenste laag in dit model).
     - voltage_kv: alleen voor TenneT (110/150/220/380), uit de stationsnaam
       geparsed — RNB-brondata specificeert geen spanningsniveau.
3. Bouw transformer_links.csv: (child=voedingsgebied_id, parent=tennet_id) —
   één rij per écht in de brondata voorkomende koppeling, dus ook de 6%
   meervoudige-ouder-gevallen correct (primary_parent_id in transformers.csv
   kiest er hiervan telkens 1; transformer_links.csv blijft de volledige bron).
4. Bouw per buurt het dominante voedingsgebied_id via een PC6->buurt
   grootste-overlap-matching (dezelfde methode als process_features.py al
   gebruikt voor WarmteAtlas-lagen), gevolgd door PC6->voedingsgebied_id uit
   congestie_pc6.csv. Buurten met PC6's die naar meerdere voedingsgebieden
   wijzen (grensgevallen) krijgen het meest voorkomende, met een dekkingsgraad
   erbij zodat je onbetrouwbare gevallen kunt herkennen.

Belangrijke caveats
--------------------
- Dit is een momentopname van de capaciteitskaart (versie 2.0, 2026-07-09
  gedownload) — de brondata zelf wordt periodiek bijgewerkt door de
  netbeheerders, dus regenereer dit bestand als je een actuelere stand wilt.
- De RNB-laag is een 2026-snapshot herhaald over alle jaren — dit simuleert
  GEEN werkelijke RNB-meerjarenprognose, het maakt alleen de tabel bruikbaar
  als één samenhangend geheel met TenneT's echte meerjarenreeks.
- Alle numerieke brondata gebruikt een komma als decimaalteken (brondata-
  conventie van de netbeheerders zelf, niet deze pipeline) — hier omgezet naar
  punt bij het parsen.

Run standalone:  python make_capaciteitskaart_csv.py [2023 [2024]]
Or via:          python run_pipeline.py --capaciteitskaart
"""

import logging
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

import geopandas as gpd
import pandas as pd
import requests

import download_sources
import process_features
from config import CBS_YEARS, CRS_RD, CRS_WGS84, OUTPUT_SEPARATOR, FILL_UNMATCHED, PROCESSED_DIR, RAW_DIR, DATA_GENERIC

log = logging.getLogger(__name__)
TODAY = date.today().isoformat()

_CK_BASE = "https://data.partnersinenergie.nl/api/download"
_CK_FILES = [
    "brondata_documentatie.txt",
    "congestie_pc6.csv",
    "voedingsgebieden.csv",
    "tennetgebieden.csv",
    "tennetcongestie.csv",
]
_CK_CACHE_DIR = RAW_DIR / "capaciteitskaart"

# Referentiejaar voor PC6-geometrie (service_area_polygon/lat/lon per transformer) —
# de brondata zelf kent geen tijdreeks voor postcode->transformer-koppeling, dus één
# recent jaar volstaat; PC6-grenzen wijzigen nauwelijks jaar-op-jaar.
_GEOM_JAAR = max(CBS_YEARS)

# TenneT-stationsnamen bevatten het spanningsniveau in vrije tekst (bv. "Station
# Aarle Rixtel 150 kV", "Station Almere 150 (CBL)", "Station Dinteloord 150kV" —
# soms zonder spatie voor "kV", soms zonder "kV"-suffix helemaal). Geen trailing
# \b in de regex hieronder (anders mist "150kV": tussen "0" en "k" zit geen
# woordgrens). Geverifieerde spanningsniveaus in de brondata (2026-07-09): 150 kV
# (179x), 110 kV (88x), 220 kV (3x), 2 stations zonder enig cijfer in de naam
# ("Station Bunschoten", "Station Merwedekanaal") — echt onleesbaar, blijft n.a.
# Geen 380 kV of lager gevonden. RNB-laag (voedingsgebieden.csv) bevat geen
# spanningsniveau — blijft leeg (n.a.) voor die rijen.
_VOLTAGE_RE = re.compile(r"\b(110|150|220|380)")


def _extract_voltage_kv(transformer_id: str) -> Optional[int]:
    m = _VOLTAGE_RE.search(str(transformer_id))
    return int(m.group(1)) if m else None

# Identieke kolomnaam-set in voedingsgebieden.csv en tennetgebieden.csv — daardoor
# rechtstreeks te unioneren in één transformers-tabel zonder hernoemen.
_CAPACITEIT_NUM_COLS = [
    "aanwezige_transportcapaciteit_invoeding", "aanwezige_transportcapaciteit_afname",
    "benodigde_transportcapaciteit_invoeding", "benodigde_transportcapaciteit_afname",
    "unieke_verzoeken_invoeding", "unieke_verzoeken_afname",
    "wachtrij_invoeding", "wachtrij_afname",
    "voorspelde_capaciteit_invoeding", "voorspelde_capaciteit_afname",
    "jaartal_opgelost_invoeding", "jaartal_opgelost_afname",
]


# ---------------------------------------------------------------------------
# 1. Download
# ---------------------------------------------------------------------------

def download_capaciteitskaart(force: bool = False) -> dict[str, Path]:
    """Download de 4 brondata-CSV's + documentatie, gecached in raw/capaciteitskaart/{datum}/."""
    out_dir = _CK_CACHE_DIR / TODAY
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for fname in _CK_FILES:
        out_path = out_dir / fname
        if out_path.exists() and not force:
            log.info("  [capaciteitskaart] %s: gecached, overgeslagen", fname)
            paths[fname] = out_path
            continue
        log.info("  [capaciteitskaart] %s downloaden...", fname)
        resp = requests.get(f"{_CK_BASE}/{fname}", timeout=120)
        resp.raise_for_status()
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(out_path)
        paths[fname] = out_path
    return paths


def _to_num(s: pd.Series) -> pd.Series:
    """Komma-decimaal (brondata-conventie) -> float."""
    return pd.to_numeric(s.astype(str).str.replace(",", ".", regex=False), errors="coerce")


def _laad_bronnen(paths: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    pc6 = pd.read_csv(paths["congestie_pc6.csv"], sep=";", dtype=str)
    vg = pd.read_csv(paths["voedingsgebieden.csv"], sep=";", dtype=str)
    tg = pd.read_csv(paths["tennetgebieden.csv"], sep=";", dtype=str)
    tc = pd.read_csv(paths["tennetcongestie.csv"], sep=";", dtype=str)

    # Genormaliseerd: één bekend spatie-artefact ("Groningen Oost ") in de brondata.
    pc6["voedingsgebied_id"] = pc6["voedingsgebied_id"].str.strip()
    pc6["tennet_id"] = pc6["tennet_id"].str.strip()
    tc["tennet_id"] = tc["tennet_id"].str.strip()
    tc["congestiegebied_afname"] = tc["congestiegebied_afname"].str.strip()
    tc["congestiegebied_opwek"] = tc["congestiegebied_opwek"].str.strip()
    tg["congestiegebied"] = tg["congestiegebied"].str.strip()

    return pc6, vg, tg, tc


def _bouw_primary_parent(pc6: pd.DataFrame) -> pd.Series:
    """
    Eén TenneT-ouder per voedingsgebied_id, voor J_Transformer.parent_node_id (een
    enkele String, geen lijst). 536/608 (88%) voedingsgebieden hebben toch al maar
    1 TenneT-ouder; voor de 36/608 (6%) met 2-4 ouders wordt de ouder met de meeste
    gekoppelde PC6's gekozen ("grootste-aandeel"). De volledige meervoudige-ouder-
    situatie blijft apart beschikbaar in transformer_links.csv.
    """
    counts = pc6[["voedingsgebied_id", "tennet_id"]].dropna()
    counts = counts[(counts["voedingsgebied_id"] != "") & (counts["tennet_id"] != "")]
    counts = counts.groupby(["voedingsgebied_id", "tennet_id"]).size().reset_index(name="n")
    idx = counts.groupby("voedingsgebied_id")["n"].idxmax()
    return counts.loc[idx].set_index("voedingsgebied_id")["tennet_id"]


def bouw_transformer_geometrie(pc6: pd.DataFrame, jaar: int = _GEOM_JAAR) -> pd.DataFrame:
    """
    Service_area_polygon + centroid (latitude/longitude) per transformer_id, NIET
    aanwezig in de brondata zelf — afgeleid door alle PC6-vlakken die naar een
    transformer_id wijzen (RNB via voedingsgebied_id, TenneT via tennet_id) samen
    te voegen (dissolve). Centroid berekend in EPSG:28992 (metrisch correct),
    daarna herprojecteerd naar EPSG:4326 — zelfde conventie als process_features.py
    hanteert voor WarmteAtlas-polygonen. service_area_polygon zelf als WKT in
    EPSG:4326 (voor rechtstreekse weergave/gebruik, net als geom_wkt elders).
    """
    if not (RAW_DIR / "cbs_pc6" / str(jaar) / "pc6.gpkg").exists():
        download_sources.download_cbs_postcode("pc6", jaar)
    pc6_gdf = process_features._load_admin("pc6", jaar)
    if pc6_gdf is None:
        raise FileNotFoundError(f"PC6-geometrie voor {jaar} ontbreekt na download-poging.")
    pc6_col = process_features._find_code_col(pc6_gdf, "pc6")
    pc6_gdf = pc6_gdf[[pc6_col, "geometry"]].rename(columns={pc6_col: "postcode6"})
    pc6_gdf["postcode6"] = pc6_gdf["postcode6"].astype(str)

    frames = []
    for id_col in ("voedingsgebied_id", "tennet_id"):
        mapping = pc6[["postcode", id_col]].dropna()
        mapping = mapping[mapping[id_col] != ""]
        merged = pc6_gdf.merge(mapping, left_on="postcode6", right_on="postcode", how="inner")
        dissolved = merged.dissolve(by=id_col)
        frames.append(dissolved.geometry)
    all_geoms = pd.concat(frames)
    all_geoms = gpd.GeoSeries(all_geoms, crs=CRS_RD)
    all_geoms = all_geoms[~all_geoms.index.duplicated(keep="first")]  # zelfde id kan in theorie in beide lagen zitten

    centroids_rd = gpd.GeoSeries(all_geoms.centroid, crs=CRS_RD)
    centroids_4326 = centroids_rd.to_crs(CRS_WGS84)
    polys_4326 = all_geoms.to_crs(CRS_WGS84)

    out = pd.DataFrame({
        "transformer_id": all_geoms.index,
        "latitude": centroids_4326.y.values,
        "longitude": centroids_4326.x.values,
        "service_area_polygon": polys_4326.to_wkt().values,
    })
    log.info("  Transformer-geometrie (%d): %d/%d met service_area_polygon", jaar, len(out), len(all_geoms))
    return out


# ---------------------------------------------------------------------------
# 2+3. Transformers + links
# ---------------------------------------------------------------------------

def bouw_transformers(
    pc6: pd.DataFrame, vg: pd.DataFrame, tg: pd.DataFrame, tc: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Bouwt transformers.csv (1 rij per transformer_id x jaar), transformer_links.csv,
    en transformer_geometry.csv (1 rij per transformer_id -- zie service_area_polygon
    caveat hieronder)."""
    jaren = sorted(_to_num(tg["jaar"]).dropna().astype(int).unique().tolist())
    log.info("  Jaren in TenneT-reeks: %s", jaren)

    # --- RNB-laag: eenmalige 2026-snapshot, herhaald over alle jaren (zie caveat in docstring) ---
    vg2 = vg.copy()
    for col in _CAPACITEIT_NUM_COLS:
        vg2[col] = _to_num(vg2[col])
    rnb_frames = []
    for jaar in jaren:
        block = vg2.copy()
        block["jaar"] = jaar
        rnb_frames.append(block)
    rnb = pd.concat(rnb_frames, ignore_index=True)
    rnb["transformer_id"] = rnb["voedingsgebied_id"]
    rnb["type"] = "RNB"
    rnb["naam"] = rnb["voedingsgebied_id"]
    rnb["operator"] = rnb["RNB"]
    rnb["congestiegebied_afname"] = pd.NA
    rnb["congestiegebied_opwek"] = pd.NA
    rnb["afname_kleurcode"] = pd.NA
    rnb["opwek_kleurcode"] = pd.NA
    rnb["voltage_kv"] = pd.NA  # RNB-brondata bevat geen spanningsniveau (MS-net, niet gespecificeerd)
    primary_parent = _bouw_primary_parent(pc6)
    rnb["primary_parent_id"] = rnb["voedingsgebied_id"].map(primary_parent)

    # --- TenneT-laag: elk station (tennet_id) x jaar, capaciteit per richting uit
    #     zijn eigen congestiegebied_afname/opwek (kunnen van elkaar verschillen). ---
    tg2 = tg.copy()
    for col in _CAPACITEIT_NUM_COLS:
        tg2[col] = _to_num(tg2[col])
    tg2["jaar"] = _to_num(tg2["jaar"]).astype("Int64")

    afname_cols = [c for c in _CAPACITEIT_NUM_COLS if c.endswith("afname")]
    invoeding_cols = [c for c in _CAPACITEIT_NUM_COLS if c.endswith("invoeding")]
    tg_afname = tg2.set_index(["congestiegebied", "jaar"])[afname_cols]
    tg_invoeding = tg2.set_index(["congestiegebied", "jaar"])[invoeding_cols]

    tennet_rows = []
    for _, station in tc.iterrows():
        tennet_id = station["tennet_id"]
        zone_afname = station["congestiegebied_afname"]
        zone_opwek = station["congestiegebied_opwek"]
        for jaar in jaren:
            row = {
                "transformer_id": tennet_id,
                "type": "TenneT",
                "naam": tennet_id,
                "operator": "TenneT",
                "jaar": jaar,
                "congestiegebied_afname": zone_afname,
                "congestiegebied_opwek": zone_opwek,
                "afname_kleurcode": station.get("afname"),
                "opwek_kleurcode": station.get("opwek"),
                "voltage_kv": _extract_voltage_kv(tennet_id),
                "primary_parent_id": pd.NA,  # TenneT is de bovenste laag in dit model, geen ouder
            }
            if (zone_afname, jaar) in tg_afname.index:
                row.update(tg_afname.loc[(zone_afname, jaar)].to_dict())
            if (zone_opwek, jaar) in tg_invoeding.index:
                row.update(tg_invoeding.loc[(zone_opwek, jaar)].to_dict())
            tennet_rows.append(row)
    tennet = pd.DataFrame(tennet_rows)

    transformers = pd.concat([rnb, tennet], ignore_index=True, sort=False)

    geometrie = bouw_transformer_geometrie(pc6)
    transformers = transformers.merge(
        geometrie[["transformer_id", "latitude", "longitude"]], on="transformer_id", how="left"
    )
    n_zonder_geom = int(transformers["latitude"].isna().sum())
    if n_zonder_geom:
        log.warning("  %d/%d transformer-rijen zonder geometrie (geen gekoppelde PC6 gevonden)",
                    n_zonder_geom, len(transformers))

    # service_area_polygon (WKT) blijft BEWUST uit transformers.csv: identiek voor alle
    # 11 jaar-rijen van eenzelfde transformer_id (geverifieerd 2026-07-09 -- 0/879
    # transformer_id's met >1 unieke polygoonwaarde over hun jaar-rijen), dus 11x
    # herhalen zou de polygoon-tekst 11x nutteloos dupliceren. transformers.csv
    # zelf was daardoor 268MB (99,3% daarvan puur deze ene, herhaalde kolom) --
    # te groot om te committen. Apart weggeschreven als transformer_geometry.csv
    # (1 rij per transformer_id, ~24MB), zie main().
    keep_cols = (
        ["transformer_id", "type", "naam", "operator", "jaar", "voltage_kv",
         "latitude", "longitude", "primary_parent_id"]
        + _CAPACITEIT_NUM_COLS
        + ["congestiegebied_afname", "congestiegebied_opwek", "afname_kleurcode", "opwek_kleurcode",
           "provincie", "info"]
    )
    transformers = transformers[[c for c in keep_cols if c in transformers.columns]]

    # --- transformer_links.csv: (child=voedingsgebied_id) -> (parent=tennet_id), gededupliceerd ---
    links = pc6[["voedingsgebied_id", "tennet_id"]].dropna().drop_duplicates()
    links = links.rename(columns={"voedingsgebied_id": "child_transformer_id", "tennet_id": "parent_transformer_id"})
    links = links[links["child_transformer_id"] != ""]

    n_multi = links.groupby("child_transformer_id")["parent_transformer_id"].nunique()
    log.info(
        "  Transformers: %d RNB-rijen (%d gebieden x %d jaar), %d TenneT-rijen (%d stations x %d jaar)",
        len(rnb), rnb["transformer_id"].nunique(), len(jaren),
        len(tennet), tennet["transformer_id"].nunique(), len(jaren),
    )
    log.info(
        "  Transformer-links: %d koppelingen, %d/%d voedingsgebieden met >1 TenneT-ouder",
        len(links), int((n_multi > 1).sum()), len(n_multi),
    )
    return transformers, links, geometrie[["transformer_id", "latitude", "longitude", "service_area_polygon"]]


# ---------------------------------------------------------------------------
# 4. Buurt -> dominant voedingsgebied_id
# ---------------------------------------------------------------------------

def koppel_buurten(jaar: int, pc6: pd.DataFrame) -> pd.DataFrame:
    """
    Matcht elke buurt aan zijn dominante voedingsgebied_id via PC6-overlap:
    grootste-overlap polygon matching (process_features._match_polygon, dezelfde
    methode als voor WarmteAtlas-lagen) geeft buurtcode per PC6-vlak; daarna wordt
    per buurt het meest voorkomende voedingsgebied_id onder zijn PC6's genomen.
    Een buurt waarvan de PC6's naar meerdere voedingsgebieden wijzen (grensgeval)
    krijgt een dekkingsgraad < 1.0, zodat onbetrouwbare matches herkenbaar zijn.
    """
    if not (RAW_DIR / "cbs_wijkenbuurten" / str(jaar) / "buurten.gpkg").exists():
        download_sources.download_cbs_wijkenbuurten(jaar, "buurten")
    if not (RAW_DIR / "cbs_pc6" / str(jaar) / "pc6.gpkg").exists():
        download_sources.download_cbs_postcode("pc6", jaar)

    buurt_gdf = process_features._load_admin("buurten", jaar)
    pc6_gdf = process_features._load_admin("pc6", jaar)
    if buurt_gdf is None or pc6_gdf is None:
        raise FileNotFoundError(f"Buurten- of PC6-geometrie voor {jaar} ontbreekt na download-poging.")

    buurt_col = process_features._find_code_col(buurt_gdf, "buurten")
    pc6_col = process_features._find_code_col(pc6_gdf, "pc6")

    buurtcodes = process_features._match_polygon(pc6_gdf, buurt_gdf, buurt_col, "capaciteitskaart_pc6")
    pc6_to_buurt = pd.DataFrame({
        "postcode6": pc6_gdf[pc6_col].astype(str).values,
        "buurtcode": buurtcodes.values,
    }).dropna()

    merged = pc6_to_buurt.merge(pc6[["postcode", "voedingsgebied_id"]], left_on="postcode6", right_on="postcode", how="inner")
    merged = merged.dropna(subset=["voedingsgebied_id"])
    merged = merged[merged["voedingsgebied_id"] != ""]

    grp = merged.groupby("buurtcode")["voedingsgebied_id"]

    def _dominant(s):
        return s.value_counts().idxmax()

    def _dekking(s):
        return (s == s.value_counts().idxmax()).mean()

    out = pd.DataFrame({
        "codering": grp.size().index,
        "voedingsgebied_id": grp.agg(_dominant).values,
        "voedingsgebied_dekking": grp.agg(_dekking).values,
        "voedingsgebied_aantal_pc6": grp.size().values,
    })

    n_onzeker = int((out["voedingsgebied_dekking"] < 1.0).sum())
    log.info(
        "  Buurt-koppeling %d: %d buurten gekoppeld, %d (%.1f%%) met PC6's die naar >1 voedingsgebied wijzen",
        jaar, len(out), n_onzeker, n_onzeker / len(out) * 100 if len(out) else 0,
    )
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(jaren: list[int] | None = None) -> bool:
    jaren = jaren or list(CBS_YEARS)
    t0 = time.monotonic()

    try:
        paths = download_capaciteitskaart()
        pc6, vg, tg, tc = _laad_bronnen(paths)
    except Exception as exc:
        log.error("Kon capaciteitskaart-brondata niet laden: %s", exc, exc_info=True)
        return False

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    try:
        transformers, links, geometrie = bouw_transformers(pc6, vg, tg, tc)
    except Exception as exc:
        log.error("FOUT bij bouwen transformers/links: %s", exc, exc_info=True)
        return False

    transformers_path = PROCESSED_DIR / f"transformers_{TODAY}.csv"
    links_path = PROCESSED_DIR / f"transformer_links_{TODAY}.csv"
    geometry_path = PROCESSED_DIR / f"transformer_geometry_{TODAY}.csv"
    for df, path, label in [
        (transformers, transformers_path, "transformers"),
        (links, links_path, "transformer_links"),
        (geometrie, geometry_path, "transformer_geometry"),
    ]:
        tmp = path.with_suffix(".tmp.csv")
        df.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(path)
        log.info("  Opgeslagen: %s (%d rijen)", path.name, len(df))

    # Stable copies (overwritten every run, no date suffix) for the model to read directly.
    for df, name in [
        (transformers, "transformers.csv"),
        (links, "transformer_links.csv"),
        (geometrie, "transformer_geometry.csv"),
    ]:
        model_path = DATA_GENERIC / name
        tmp = model_path.with_suffix(".tmp.csv")
        df.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(model_path)
        log.info("  Kopie voor model: %s", model_path.name)

    all_ok = True
    for jaar in jaren:
        try:
            buurt_koppeling = koppel_buurten(jaar, pc6)
        except Exception as exc:
            log.error("FOUT buurt-koppeling jaar %d: %s", jaar, exc, exc_info=True)
            all_ok = False
            continue
        out_path = PROCESSED_DIR / f"capaciteitskaart_buurten_{jaar}_{TODAY}.csv"
        tmp = out_path.with_suffix(".tmp.csv")
        buurt_koppeling.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(out_path)
        log.info("  Opgeslagen: %s", out_path.name)

    elapsed = time.monotonic() - t0
    log.info("Capaciteitskaart-verwerking klaar in %.0fs", elapsed)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    jaren = [int(a) for a in sys.argv[1:]] or None
    sys.exit(0 if main(jaren) else 1)
