"""
Download en verwerk de Capaciteitskaart (data.partnersinenergie.nl) tot een
transformer-netwerk (RNB + TenneT) gekoppeld aan buurten.

Bron: https://data.partnersinenergie.nl/capaciteitskaart/info/algemene-info
Documentatie van de brondata zelf: raw/capaciteitskaart/{datum}/brondata_documentatie.txt
(meegedownload, zie download_capaciteitskaart()).

Structuur van de brondata (geverifieerd tegen de echte bestanden, download 2026-10-29;
de documentatie zelf is nog versie 2.0, 2024-11-20, en beschrijft de jaarbestanden niet):

  congestie_pc6.csv (~462.900 rijen, 1 per PC6)
      De centrale koppeltabel: postcode -> voedingsgebied_id (RNB) EN tennet_id
      (TenneT) tegelijk, plus kleurcode afname/opwek, voedingsgebied_naam,
      RNB_postcode (netbeheerder), Gemeentecode/naam.
  voedingsgebieden_year_granularity.csv (~2.400 rijen, voedingsgebied_id x jaar)
      RNB-laag als jaarreeks 2026-2036. Niet elk gebied heeft elk jaar: alle 602
      hebben 2026, ~330-375 hebben 2027-2031, daarna nog maar een handvol.
  voedingsgebieden.csv (602 rijen)
      Precies de 2026-rijen van het jaarbestand (geverifieerd: 0 verschillen) --
      daarom niet meer gebruikt.
  tennetgebieden.csv (23 rijen, 1 per congestiegebied)
      TenneT-laag per congestiegebied. In juli 2026 nog een reeks 2026-2036
      (150 rijen); nu alleen 2026 (16 rijen) of geen jaar (7 rijen, vrijwel leeg).
  tennetcongestie.csv (271 rijen, 1 per tennet_id)
      Koppelt elk TenneT-tussenstation aan zijn congestiegebied -- apart voor
      afname en invoeding/opwek-richting -- plus kleurcode per richting.
  projecten.csv (~940 rijen)
      Netuitbreidingen per voedings- of congestiegebied, met jaar/kwartaal van
      oplevering en netbeheerder.

LET OP: de TenneT-capaciteiten gelden voor een heel congestiegebied, niet voor één
station. Alle stations in een gebied verwijzen naar dezelfde capaciteitsrij; tel ze dus
niet per station op.

Uitvoer (drie tabellen + de koppeltabel)
-----------------------------------------
1. transformers.csv -- 1 rij per transformer_id (RNB-voedingsgebied of TenneT-station):
   alles wat niet per jaar verandert. type, naam, operator, voltage_kv,
   latitude/longitude, primary_parent_id, congestiegebied_afname/opwek (alleen
   TenneT: de sleutel naar de capaciteitsrij), afname/opwek_kleurcode, provincie en
   service_area_polygon (WKT, EPSG:4326). Afgeleid, niet in de brondata:
     - latitude/longitude/service_area_polygon: centroid + dissolved PC6-vlakken per
       transformer_id (PC6 -> voedingsgebied_id/tennet_id uit congestie_pc6.csv),
       vereenvoudigd op SIMPLIFY_M meter. De downloads bevatten geen geometrie.
     - naam (RNB) en afname/opwek_kleurcode (RNB): de meest voorkomende waarde onder
       de PC6's van het gebied. 218 van de 602 gebieden hebben PC6's met verschillende
       kleuren; de kaart kleurt een gebied met één status.
     - primary_parent_id: voor RNB-rijen de TenneT-ouder met de meeste gekoppelde
       PC6's; voor TenneT-rijen leeg (bovenste laag in dit model).
     - voltage_kv: alleen voor TenneT (110/150/220/380), uit de stationsnaam.
2. transformer_capacity.csv -- 1 rij per gebied x jaar, precies de jaren die de bron
   geeft: gebied_type ("RNB"/"TenneT"), gebied_id (voedingsgebied_id of TenneT-
   congestiegebied), jaar, en de capaciteit/wachtrij-kolommen (MW). Een RNB-transformer
   leest de rijen van zijn eigen transformer_id; een TenneT-station leest afname uit
   zijn congestiegebied_afname en invoeding uit zijn congestiegebied_opwek.
3. grid_projects.csv -- de netuitbreidingen uit projecten.csv, gebied_id verwijst naar
   een voedingsgebied_id of TenneT-congestiegebied.
+  transformer_links.csv -- (child=voedingsgebied_id, parent=tennet_id), alle
   koppelingen uit de brondata, ook de voedingsgebieden met 2-4 TenneT-ouders.

Daarnaast per jaar capaciteitskaart_buurten_<jaar>_<datum>.csv: per buurt het dominante
voedingsgebied_id via PC6->buurt grootste-overlap-matching, met een dekkingsgraad zodat
je onbetrouwbare gevallen kunt herkennen (gaat via --buurten de kerncijfers in).

Belangrijke caveats
--------------------
- Momentopname van de capaciteitskaart op de downloaddatum. De netbeheerders werken de
  brondata periodiek bij -- en veranderen soms de structuur (zie TenneT hierboven).
- Alle numerieke brondata gebruikt een komma als decimaalteken -- hier omgezet naar
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
    "voedingsgebieden_year_granularity.csv",
    "tennetgebieden.csv",
    "tennetcongestie.csv",
    "projecten.csv",
]
_CK_CACHE_DIR = RAW_DIR / "capaciteitskaart"

# Referentiejaar voor PC6-geometrie (service_area_polygon/lat/lon per transformer) —
# de brondata zelf kent geen tijdreeks voor postcode->transformer-koppeling, dus één
# recent jaar volstaat; PC6-grenzen wijzigen nauwelijks jaar-op-jaar.
_GEOM_JAAR = max(CBS_YEARS)

# Vereenvoudiging van de dissolved PC6-vlakken, in meter (EPSG:28992). Ongeveer de
# breedte van een straat: de vorm blijft herkenbaar, transformers.csv krimpt een factor
# ~3 en de kaart in het model blijft vlot. Het vlak is sowieso een benadering uit PC6's.
SIMPLIFY_M = 10.0

# TenneT-stationsnamen bevatten het spanningsniveau in vrije tekst (bv. "Station
# Aarle Rixtel 150 kV", "Station Almere 150 (CBL)", "Station Dinteloord 150kV" —
# soms zonder spatie voor "kV", soms zonder "kV"-suffix helemaal). Geen trailing
# \b in de regex hieronder (anders mist "150kV": tussen "0" en "k" zit geen
# woordgrens). Geverifieerde spanningsniveaus in de brondata (2026-07-09): 150 kV
# (179x), 110 kV (88x), 220 kV (3x), 2 stations zonder enig cijfer in de naam
# ("Station Bunschoten", "Station Merwedekanaal") — echt onleesbaar, blijft n.a.
# Geen 380 kV of lager gevonden. RNB-laag bevat geen spanningsniveau — blijft leeg
# (n.a.) voor die rijen.
_VOLTAGE_RE = re.compile(r"\b(110|150|220|380)")


def _extract_voltage_kv(transformer_id: str) -> Optional[int]:
    m = _VOLTAGE_RE.search(str(transformer_id))
    return int(m.group(1)) if m else None

# De capaciteit/wachtrij-kolommen, in MW (behalve verzoeken en jaartallen). Dezelfde
# namen in het RNB-jaarbestand en tennetgebieden.csv; wat een bron niet heeft blijft leeg.
_CAPACITEIT_NUM_COLS = [
    "aanwezige_transportcapaciteit_afname", "aanwezige_transportcapaciteit_invoeding",
    "benodigde_transportcapaciteit_afname", "benodigde_transportcapaciteit_invoeding",
    "voorspelde_capaciteit_afname", "voorspelde_capaciteit_invoeding",
    "wachtrij_afname", "wachtrij_invoeding",
    "unieke_verzoeken_afname", "unieke_verzoeken_invoeding",
    "jaartal_opgelost_afname", "jaartal_opgelost_invoeding",
]
# Tekstkolommen die per gebied x jaar in de bron staan. De *_beperking/*_tso_limiet-
# kolommen bestaan alleen bij TenneT en waren bij de download van 2026-10-29 overal leeg.
_CAPACITEIT_TEXT_COLS = [
    "afname_beperking", "afname_tso_limiet", "opwek_beperking", "opwek_tso_limiet",
    "start", "eind", "kwartaal", "info", "congestie_url",
]

_TRANSFORMER_COLS = [
    "transformer_id", "type", "naam", "operator", "voltage_kv", "latitude", "longitude",
    "primary_parent_id", "congestiegebied_afname", "congestiegebied_opwek",
    "afname_kleurcode", "opwek_kleurcode", "provincie", "service_area_polygon",
]

_PROJECT_COLS = [
    "projectnaam", "gebied_id", "netbeheerder", "jaar", "kwartaal", "einddatum",
    "project_fase", "project_type", "beschrijving", "tijdschema_beschrijving",
    "start", "eind", "bron", "project_url",
]


# ---------------------------------------------------------------------------
# 1. Download
# ---------------------------------------------------------------------------

def download_capaciteitskaart(force: bool = False) -> dict[str, Path]:
    """Download de brondata-CSV's + documentatie, gecached in raw/capaciteitskaart/{datum}/."""
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


def _laad_bronnen(paths: dict) -> dict[str, pd.DataFrame]:
    bron = {
        "pc6": pd.read_csv(paths["congestie_pc6.csv"], sep=";", dtype=str),
        "vg": pd.read_csv(paths["voedingsgebieden_year_granularity.csv"], sep=";", dtype=str),
        "tg": pd.read_csv(paths["tennetgebieden.csv"], sep=";", dtype=str),
        "tc": pd.read_csv(paths["tennetcongestie.csv"], sep=";", dtype=str),
        "pr": pd.read_csv(paths["projecten.csv"], sep=";", dtype=str),
    }
    # Spatie-artefacten in de brondata ("Groningen Oost ", "Veenendaal 2 ").
    for name, cols in {
        "pc6": ["voedingsgebied_id", "voedingsgebied_naam", "tennet_id"],
        "vg": ["voedingsgebied_id"],
        "tg": ["congestiegebied"],
        "tc": ["tennet_id", "congestiegebied_afname", "congestiegebied_opwek"],
        "pr": ["gebied_id", "projectnaam"],
    }.items():
        for col in cols:
            bron[name][col] = bron[name][col].str.strip()
    return bron


def _bouw_primary_parent(pc6: pd.DataFrame) -> pd.Series:
    """
    Eén TenneT-ouder per voedingsgebied_id, voor J_Transformer.parent_node_id (een
    enkele String, geen lijst). Het overgrote deel van de voedingsgebieden heeft maar
    1 TenneT-ouder; voor de gebieden met 2-4 ouders wordt de ouder met de meeste
    gekoppelde PC6's gekozen ("grootste-aandeel"). De volledige meervoudige-ouder-
    situatie blijft apart beschikbaar in transformer_links.csv.
    """
    counts = pc6[["voedingsgebied_id", "tennet_id"]].dropna()
    counts = counts[(counts["voedingsgebied_id"] != "") & (counts["tennet_id"] != "")]
    counts = counts.groupby(["voedingsgebied_id", "tennet_id"]).size().reset_index(name="n")
    idx = counts.groupby("voedingsgebied_id")["n"].idxmax()
    return counts.loc[idx].set_index("voedingsgebied_id")["tennet_id"]


def _meest_voorkomend(pc6: pd.DataFrame, col: str) -> pd.Series:
    """Per voedingsgebied_id de meest voorkomende waarde van col onder zijn PC6's."""
    sub = pc6[["voedingsgebied_id", col]].dropna()
    sub = sub[(sub["voedingsgebied_id"] != "") & (sub[col] != "")]
    return sub.groupby("voedingsgebied_id")[col].agg(lambda s: s.value_counts().idxmax())


def bouw_transformer_geometrie(pc6: pd.DataFrame, jaar: int = _GEOM_JAAR) -> pd.DataFrame:
    """
    Service_area_polygon + centroid (latitude/longitude) per transformer_id, NIET
    aanwezig in de brondata zelf — afgeleid door alle PC6-vlakken die naar een
    transformer_id wijzen (RNB via voedingsgebied_id, TenneT via tennet_id) samen
    te voegen (dissolve). Centroid berekend in EPSG:28992 (metrisch correct) op het
    onvereenvoudigde vlak, daarna herprojecteerd naar EPSG:4326 — zelfde conventie als
    process_features.py hanteert voor WarmteAtlas-polygonen. service_area_polygon zelf
    als WKT in EPSG:4326, vereenvoudigd op SIMPLIFY_M meter.
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
    polys_4326 = all_geoms.simplify(SIMPLIFY_M, preserve_topology=True).to_crs(CRS_WGS84)

    out = pd.DataFrame({
        "transformer_id": all_geoms.index,
        "latitude": centroids_4326.y.values,
        "longitude": centroids_4326.x.values,
        "service_area_polygon": polys_4326.to_wkt(rounding_precision=6).values,
    })
    log.info("  Transformer-geometrie (%d): %d transformers met service_area_polygon (vereenvoudigd op %.0f m)",
             jaar, len(out), SIMPLIFY_M)
    return out


# ---------------------------------------------------------------------------
# 2. Transformers (1 rij per transformer) + links
# ---------------------------------------------------------------------------

def bouw_transformers(bron: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Bouwt transformers.csv (1 rij per transformer_id) en transformer_links.csv."""
    pc6, vg, tc = bron["pc6"], bron["vg"], bron["tc"]

    # --- RNB-laag: wat niet per jaar verandert, uit de vroegste rij per gebied ---
    vg_vast = (
        vg.assign(_jaar=_to_num(vg["jaar"]))
        .sort_values("_jaar")
        .drop_duplicates("voedingsgebied_id", keep="first")
        .set_index("voedingsgebied_id")
    )
    rnb = pd.DataFrame({"transformer_id": vg_vast.index})
    rnb["type"] = "RNB"
    namen = _meest_voorkomend(pc6, "voedingsgebied_naam")
    rnb["naam"] = rnb["transformer_id"].map(namen).fillna(rnb["transformer_id"])
    rnb["operator"] = rnb["transformer_id"].map(vg_vast["RNB"])
    rnb["provincie"] = rnb["transformer_id"].map(vg_vast["provincie"])
    rnb["primary_parent_id"] = rnb["transformer_id"].map(_bouw_primary_parent(pc6))
    rnb["afname_kleurcode"] = rnb["transformer_id"].map(_meest_voorkomend(pc6, "afname"))
    rnb["opwek_kleurcode"] = rnb["transformer_id"].map(_meest_voorkomend(pc6, "opwek"))

    # --- TenneT-laag: elk station, met de sleutel naar de capaciteitsrij van zijn congestiegebied per richting ---
    tennet = pd.DataFrame({
        "transformer_id": tc["tennet_id"],
        "type": "TenneT",
        "naam": tc["tennet_id"],
        "operator": "TenneT",
        "voltage_kv": tc["tennet_id"].map(_extract_voltage_kv).astype("Int64"),
        "congestiegebied_afname": tc["congestiegebied_afname"],
        "congestiegebied_opwek": tc["congestiegebied_opwek"],
        "afname_kleurcode": tc["afname"],
        "opwek_kleurcode": tc["opwek"],
    })

    transformers = pd.concat([rnb, tennet], ignore_index=True, sort=False)
    transformers = transformers.merge(bouw_transformer_geometrie(pc6), on="transformer_id", how="left")
    n_zonder_geom = int(transformers["latitude"].isna().sum())
    if n_zonder_geom:
        log.warning("  %d/%d transformers zonder geometrie (geen gekoppelde PC6 gevonden)",
                    n_zonder_geom, len(transformers))
    transformers = transformers.reindex(columns=_TRANSFORMER_COLS)

    # --- transformer_links.csv: (child=voedingsgebied_id) -> (parent=tennet_id), gededupliceerd ---
    links = pc6[["voedingsgebied_id", "tennet_id"]].dropna().drop_duplicates()
    links = links.rename(columns={"voedingsgebied_id": "child_transformer_id", "tennet_id": "parent_transformer_id"})
    links = links[links["child_transformer_id"] != ""]

    n_multi = links.groupby("child_transformer_id")["parent_transformer_id"].nunique()
    log.info("  Transformers: %d RNB-voedingsgebieden, %d TenneT-stations", len(rnb), len(tennet))
    log.info(
        "  Transformer-links: %d koppelingen, %d/%d voedingsgebieden met >1 TenneT-ouder",
        len(links), int((n_multi > 1).sum()), len(n_multi),
    )
    return transformers, links


# ---------------------------------------------------------------------------
# 3. Capaciteit per gebied x jaar
# ---------------------------------------------------------------------------

def bouw_capaciteit(bron: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    transformer_capacity.csv: 1 rij per gebied x jaar, alleen de jaren die de bron geeft.
    Een TenneT-congestiegebied zonder jaar (7 van de 23 in de download van 2026-10-29,
    vrijwel leeg) krijgt het vroegste jaar van de TenneT-laag: het is de huidige stand.
    """
    vg, tg = bron["vg"], bron["tg"]

    rnb = vg.rename(columns={"voedingsgebied_id": "gebied_id"}).assign(gebied_type="RNB")
    tennet = tg.rename(columns={"congestiegebied": "gebied_id"}).assign(gebied_type="TenneT")
    tennet_jaren = _to_num(tennet["jaar"])
    n_zonder_jaar = int(tennet_jaren.isna().sum())
    if n_zonder_jaar:
        log.info("  %d TenneT-congestiegebieden zonder jaar -> %d (huidige stand)", n_zonder_jaar, int(tennet_jaren.min()))
    tennet["jaar"] = tennet_jaren.fillna(tennet_jaren.min())

    capaciteit = pd.concat([rnb, tennet], ignore_index=True, sort=False)
    capaciteit["jaar"] = _to_num(capaciteit["jaar"]).astype("Int64")
    for col in _CAPACITEIT_NUM_COLS:
        capaciteit[col] = _to_num(capaciteit[col]) if col in capaciteit.columns else pd.NA
    for col in ("jaartal_opgelost_afname", "jaartal_opgelost_invoeding", "unieke_verzoeken_afname", "unieke_verzoeken_invoeding"):
        capaciteit[col] = capaciteit[col].round().astype("Int64")
    for col in _CAPACITEIT_TEXT_COLS:
        if col not in capaciteit.columns:
            capaciteit[col] = pd.NA
        capaciteit[col] = capaciteit[col].replace("", pd.NA)

    capaciteit = capaciteit[["gebied_type", "gebied_id", "jaar"] + _CAPACITEIT_NUM_COLS + _CAPACITEIT_TEXT_COLS]
    capaciteit = capaciteit.sort_values(["gebied_type", "gebied_id", "jaar"]).reset_index(drop=True)
    jaren = capaciteit.groupby("gebied_type")["jaar"].agg(["min", "max", "size"])
    for gebied_type, row in jaren.iterrows():
        log.info("  Capaciteit %s: %d rijen, jaren %d-%d", gebied_type, row["size"], row["min"], row["max"])
    return capaciteit


# ---------------------------------------------------------------------------
# 4. Netuitbreidingsprojecten
# ---------------------------------------------------------------------------

def bouw_projecten(bron: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """grid_projects.csv: projecten.csv zonder de interne hulpkolommen van de bron (index, *_num)."""
    pr = bron["pr"].rename(columns={"einddatum_string": "einddatum"})
    pr["jaar"] = _to_num(pr["jaar"]).astype("Int64")
    pr["kwartaal"] = _to_num(pr["kwartaal"]).astype("Int64")
    pr = pr.reindex(columns=_PROJECT_COLS).sort_values(["gebied_id", "jaar", "projectnaam"]).reset_index(drop=True)
    log.info("  Netuitbreidingen: %d projecten in %d gebieden", len(pr), pr["gebied_id"].nunique())
    return pr


# ---------------------------------------------------------------------------
# 5. Buurt -> dominant voedingsgebied_id
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

def _schrijf(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".tmp.csv")
    # object first: an Int64 column (jaar, kleurcode) cannot hold the FILL_UNMATCHED text
    df.astype(object).where(df.notna(), FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
    tmp.replace(path)


def main(jaren: list[int] | None = None) -> bool:
    jaren = jaren or list(CBS_YEARS)
    t0 = time.monotonic()

    try:
        paths = download_capaciteitskaart()
        bron = _laad_bronnen(paths)
    except Exception as exc:
        log.error("Kon capaciteitskaart-brondata niet laden: %s", exc, exc_info=True)
        return False

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    try:
        transformers, links = bouw_transformers(bron)
        capaciteit = bouw_capaciteit(bron)
        projecten = bouw_projecten(bron)
    except Exception as exc:
        log.error("FOUT bij bouwen transformers/capaciteit/projecten: %s", exc, exc_info=True)
        return False

    tabellen = [
        (transformers, "transformers"),
        (capaciteit, "transformer_capacity"),
        (projecten, "grid_projects"),
        (links, "transformer_links"),
    ]
    for df, name in tabellen:
        path = PROCESSED_DIR / f"{name}_{TODAY}.csv"
        _schrijf(df, path)
        log.info("  Opgeslagen: %s (%d rijen)", path.name, len(df))

    # Stable copies (overwritten every run, no date suffix) for the model to read directly.
    for df, name in tabellen:
        _schrijf(df, DATA_GENERIC / f"{name}.csv")
        log.info("  Kopie voor model: %s.csv", name)
    # Since the 2026-10-29 restructure the polygon sits in transformers.csv itself.
    oud = DATA_GENERIC / "transformer_geometry.csv"
    if oud.exists():
        oud.unlink()
        log.info("  Verwijderd: %s (service_area_polygon staat nu in transformers.csv)", oud.name)

    all_ok = True
    for jaar in jaren:
        try:
            buurt_koppeling = koppel_buurten(jaar, bron["pc6"])
        except Exception as exc:
            log.error("FOUT buurt-koppeling jaar %d: %s", jaar, exc, exc_info=True)
            all_ok = False
            continue
        out_path = PROCESSED_DIR / f"capaciteitskaart_buurten_{jaar}_{TODAY}.csv"
        _schrijf(buurt_koppeling, out_path)
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
