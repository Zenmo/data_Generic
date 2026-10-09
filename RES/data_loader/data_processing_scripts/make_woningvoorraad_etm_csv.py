"""Woningvoorraad per buurt in de ETM-indeling (woningtype × bouwperiode), met m².

Doel: per buurt het aantal woningen én de gebruiksoppervlakte per ETM-archetype,
zodat het AnyLogic-model de ruimteverwarmingsvraag op dezelfde manier kan
opbouwen als het ETM (aantal × m² × kWh/m² per type × periode) en de
ETM-isolatieschuiven per archetype kan toepassen.

ETM-indeling (keys exact zoals in ETM / etsource):
  typen:    apartments, terraced_houses, semi_detached_houses, detached_houses
  periodes: before_1945, 1945_1964, 1965_1984, 1985_2004, 2005_present, future
            ("future" = gebouwd na het ETM-startjaar, zie --etm-startjaar)
  NB: ETM's terraced_houses = alleen tussenwoningen; hoekwoningen vallen onder
  semi_detached_houses (samen met 2-onder-1-kap). Geverifieerd tegen ETM area-data:
  Dordrecht ETM terraced 21.243 vs CBS tussen 21.182; ETM semi 8.970 vs CBS hoek+2o1k 8.831.

Bronnen:
  1. BAG (PDOK WFS)   — verblijfsobjecten: woonfunctie, status, gebruiksoppervlakte,
                        bouwjaar, pand-id, locatie; panden: footprint-geometrie.
                        Bouwperiode en m² komen hier direct uit.
  2. 3DBAG (WFS)      — bouwlagen / hoogte per pand (optioneel, --zonder-3dbag).
                        Gebruikt om panden met meerdere woningen te splitsen in
                        "gestapeld" (appartementen) vs "naast elkaar" (rij in één pand).
  3. CBS kerncijfers  — % tussen/hoek/2-onder-1-kap/vrijstaand/meergezins per buurt
                        (processed buurten-CSV 2024). Kalibratiedoel voor de typen.
  4. EP-Online        — optioneel (--ep-online <csv>): gebouwtype per verblijfsobject
                        uit het energielabelbestand; overschrijft de geometrische afleiding.
  5. ETM area-API     — present_number_of_<type>_<periode>, typical_useful_demand_...
                        (kWh/m²) per gemeente, ter vergelijking en voor een
                        bottom-up warmtevraag. Publieke GET, geen API-key nodig.

Methode woningtype (BAG kent geen woningtype):
  a. Pand met 1 woning  → grondgebonden.
     Pand met ≥2 woningen → "naast elkaar" (rij in één BAG-pand) als alle
     woningen een eigen VBO-locatie hebben (onderlinge afstand ≥ 4 m),
     het pand ≤ 3 bouwlagen heeft en ≤ 20 woningen; anders appartementen.
  b. Grondgebonden panden die een muur delen (gedeelde rand ≥ 2 m) vormen een
     blok. Woningen per blok: 1 → detached, 2 → semi_detached, ≥3 → eindwoningen
     (≤1 buurpand) semi_detached, tussenwoningen terraced. Bij een rij binnen één
     pand zijn de uiterste woningen langs de hoofdas de eindwoningen.
     Losstaand pand dat alleen aan een appartementengebouw vastzit → semi_detached.
  c. EP-Online gebouwtype (indien opgegeven) overschrijft a/b per woning.
  d. Kalibratie (IPF) per buurt: typetotalen naar CBS-percentages, periodetotalen
     blijven gelijk aan BAG. De geometrische afleiding bepaalt dus alleen de
     samenhang tussen type en bouwjaar; de typeverdeling zelf komt van CBS.
     m² per cel = aantal × gemiddelde m² van die cel in de BAG-afleiding.

Output (processed_data_from_loader/):
  woningvoorraad_etm_buurten_<datum>.csv              gekalibreerd (gebruik dit)
  woningvoorraad_etm_buurten_bag_ongekalibreerd_<datum>.csv
  woningvoorraad_etm_gemeenten_vergelijking_<datum>.csv   BAG vs gekalibreerd vs ETM
  etm_area_woningvoorraad_<datum>.csv                 ETM area-data per gemeente
  data_Generic/RES/etm_area_woningvoorraad.csv        idem, vaste naam voor het model; SAMENGEVOEGD
  data_Generic/RES/etm_area_totalen.csv               (andere gebieden blijven staan, zie make_etm_area_csv.py)

Run standalone:  python make_woningvoorraad_etm_csv.py [--gemeenten GM0505 GM0642 ...]
                 [--buurtjaar 2024] [--zonder-3dbag] [--ep-online pad.csv] [--etm-startjaar 2023]
Or via:          python run_pipeline.py --woningvoorraad
"""

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely
from shapely.geometry import box
from shapely.prepared import prep

from make_etm_area_csv import gm_naar_etm_code, haal_etm_area_codes, schrijf_vaste_bestanden
from config import (CACHE_MAX_AGE_DAYS, CRS_RD, DATA_GENERIC, FILL_UNMATCHED, OUTPUT_SEPARATOR,
                    PROCESSED_DIR, RAW_DIR, REQUEST_TIMEOUT)

log = logging.getLogger(__name__)

# --- Gebied -------------------------------------------------------------------------------
# Drechtsteden, zelfde zeven gemeenten als _DRECHTSTEDEN_GEMEENTEN in make_buurten_csv.py.
DEFAULT_GEMEENTEN = ["GM0482", "GM0505", "GM0523", "GM0531", "GM0590", "GM0610", "GM0642"]

# ETM-regio die naast de gemeenten wordt opgehaald (de ETM area code per gemeente komt uit
# /api/v3/areas, zie make_etm_area_csv.py; landelijk vullen: python make_etm_area_csv.py)
ETM_REGIO_CODE = "ES18_drechtsteden"

# --- ETM-indeling ----------------------------------------------------------------------------
TYPES = ["apartments", "terraced_houses", "semi_detached_houses", "detached_houses"]
PERIODS = ["before_1945", "1945_1964", "1965_1984", "1985_2004", "2005_present", "future"]
EXISTING_PERIODS = PERIODS[:-1]

# ETM area-keys gebruiken "apartments_<p>" maar "<x>_houses_<p>" — zelfde als TYPES.

# --- Bronnen -----------------------------------------------------------------------------------
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
BAG3D_WFS = "https://data.3dbag.nl/api/BAG3D/wfs"
_RAW_BAG = RAW_DIR / "bag"
_RAW_WB = RAW_DIR / "cbs_wijkenbuurten"
_CBS_BUURTEN_CSV = PROCESSED_DIR / "buurten" / "kerncijfers_buurten_met_geometrie_{jaar}.csv"

# Verblijfsobjecten die als bestaande woning tellen (zelfde idee als CBS woningvoorraad).
_VBO_STATUS_WONING = {
    "Verblijfsobject in gebruik",
    "Verblijfsobject in gebruik (niet ingemeten)",
    "Verbouwing verblijfsobject",
}

_TILE_M = 1000            # starttegelgrootte (m); wordt gesplitst als een tegel vol zit
_WFS_COUNT = 2000
_MIN_TILE_M = 125

# Typologie-parameters (zie module-docstring)
_GEDEELDE_MUUR_MIN_M = 2.0
_BUFFER_M = 0.3
_RIJ_IN_PAND_MIN_AFSTAND_M = 4.0
_RIJ_IN_PAND_MAX_LAGEN = 3
_RIJ_IN_PAND_MAX_WONINGEN = 20
_VERDIEPINGSHOOGTE_M = 3.0

# CBS-kolommen (percentage van totale woningvoorraad, buurten-CSV 2024)
_CBS_TYPE_KOLOMMEN = {
    "apartments":           ["percentage_meergezinswoning"],
    # ETM-definitie: hoekwoning hoort bij semi_detached, niet bij terraced (zie classificeer)
    "terraced_houses":      ["percentage_tussenwoning_eengezings"],
    "semi_detached_houses": ["percentage_hoekwoning_eengezings", "percentage_twee_onder_een_kap_eengezings"],
    "detached_houses":      ["percentage_vrijstaande_woning_eengezings"],
}

# EP-Online gebouwtype → ETM-type (substring-match op kleine letters, eerste treffer wint)
_EP_ONLINE_TYPE_MAP = [
    ("vrijstaand", "detached_houses"),
    ("2 onder 1", "semi_detached_houses"),
    ("2-onder-1", "semi_detached_houses"),
    ("twee onder", "semi_detached_houses"),
    ("hoek", "semi_detached_houses"),      # ETM: hoekwoning = semi_detached
    ("tussen", "terraced_houses"),
    ("rijwoning", "terraced_houses"),
    ("appartement", "apartments"),
    ("maisonnette", "apartments"),
    ("galerij", "apartments"),
    ("portiek", "apartments"),
    ("flat", "apartments"),
    ("meergezins", "apartments"),
]


# =========================================================================================
# Download
# =========================================================================================

def _wfs_page(url: str, params: dict) -> list:
    for poging in range(3):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json().get("features") or []
        except (requests.RequestException, ValueError) as exc:
            log.warning("  WFS-fout (poging %d/3): %s", poging + 1, exc)
            time.sleep(2 * (poging + 1))
    raise RuntimeError(f"WFS-request blijft mislukken: {url} {params.get('BBOX')}")


def _fetch_tile(url: str, typename: str, bounds: tuple, extra: dict, out: list) -> None:
    """Haal één tegel op; splits in vier als de server de pagina vol teruggeeft."""
    minx, miny, maxx, maxy = bounds
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": typename, "OUTPUTFORMAT": "application/json",
        "SRSNAME": CRS_RD, "COUNT": _WFS_COUNT,
        "BBOX": f"{minx},{miny},{maxx},{maxy},{CRS_RD}",
        **extra,
    }
    feats = _wfs_page(url, params)
    # PDOK knipt soms af op 1000 i.p.v. COUNT; beide gevallen als "vol" behandelen.
    vol = len(feats) >= _WFS_COUNT or len(feats) == 1000
    if vol and (maxx - minx) > _MIN_TILE_M:
        mx, my = (minx + maxx) / 2, (miny + maxy) / 2
        for b in [(minx, miny, mx, my), (mx, miny, maxx, my), (minx, my, mx, maxy), (mx, my, maxx, maxy)]:
            _fetch_tile(url, typename, b, extra, out)
        return
    if vol:
        log.warning("  Tegel %s zit vol op minimale grootte — mogelijk features gemist", bounds)
    out.extend(feats)


def _tiles_over(gebied) -> list[tuple]:
    minx, miny, maxx, maxy = gebied.bounds
    xs = np.arange(np.floor(minx / _TILE_M) * _TILE_M, maxx, _TILE_M)
    ys = np.arange(np.floor(miny / _TILE_M) * _TILE_M, maxy, _TILE_M)
    prepared = prep(gebied)
    return [(x, y, x + _TILE_M, y + _TILE_M) for x in xs for y in ys
            if prepared.intersects(box(x, y, x + _TILE_M, y + _TILE_M))]


def _cache_vers(pad: Path) -> bool:
    return pad.exists() and (time.time() - pad.stat().st_mtime) / 86400 < CACHE_MAX_AGE_DAYS


def download_laag(naam: str, url: str, typename: str, gebied, gm_key: str, extra: dict | None = None,
                  geometrie: bool = True) -> gpd.GeoDataFrame | pd.DataFrame:
    """Tegelgewijze WFS-download over het gebied, met cache in raw/bag/."""
    _RAW_BAG.mkdir(parents=True, exist_ok=True)
    pad = _RAW_BAG / f"{naam}_{gm_key}.{'gpkg' if geometrie else 'csv'}"
    if _cache_vers(pad):
        log.info("  %s uit cache: %s", naam, pad.name)
        return gpd.read_file(pad) if geometrie else pd.read_csv(pad, dtype={"identificatie": str})

    tiles = _tiles_over(gebied)
    log.info("  %s downloaden: %d tegels …", naam, len(tiles))
    feats: list = []
    t0 = time.monotonic()
    for i, t in enumerate(tiles, 1):
        _fetch_tile(url, typename, t, extra or {}, feats)
        if i % 50 == 0:
            log.info("    %d/%d tegels, %d features (%.0fs)", i, len(tiles), len(feats), time.monotonic() - t0)
    if not feats:
        raise RuntimeError(f"0 features voor {typename}")

    if geometrie:
        df = gpd.GeoDataFrame.from_features(feats, crs=CRS_RD)
    else:
        df = pd.DataFrame([f["properties"] for f in feats])
    df = df.drop_duplicates(subset="identificatie").reset_index(drop=True)
    log.info("  %s: %d unieke features", naam, len(df))

    tmp = pad.with_suffix(".tmp" + pad.suffix)
    if geometrie:
        df.to_file(tmp, driver="GPKG")
    else:
        df.to_csv(tmp, index=False)
    tmp.replace(pad)
    return df


def haal_etm_area(gm_codes: list[str], regio: str | None = ETM_REGIO_CODE) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ETM area-data per gemeente (+ regio): (lang per type × periode, totalen per gebied)."""
    codes = list(gm_naar_etm_code(gm_codes).values()) + ([regio] if regio else [])
    return haal_etm_area_codes(codes)


# =========================================================================================
# Classificatie
# =========================================================================================

def periode_van(bouwjaar: pd.Series, etm_startjaar: int) -> pd.Series:
    bj = pd.to_numeric(bouwjaar, errors="coerce")
    out = pd.Series("before_1945", index=bj.index)  # incl. onbekend/onwaarschijnlijk (<1945)
    out[bj >= 1945] = "1945_1964"
    out[bj >= 1965] = "1965_1984"
    out[bj >= 1985] = "1985_2004"
    out[bj >= 2005] = "2005_present"
    out[bj > etm_startjaar] = "future"
    out[bj.isna()] = "before_1945"
    return out


def _gedeelde_muren(panden: gpd.GeoDataFrame) -> pd.DataFrame:
    """Paren panden met een gedeelde rand ≥ _GEDEELDE_MUUR_MIN_M (op index van `panden`)."""
    buf = panden[["geometry"]].copy()
    buf["geometry"] = panden.geometry.buffer(_BUFFER_M)
    paren = gpd.sjoin(panden[["geometry"]], buf, predicate="intersects", how="inner")
    paren = paren[paren.index < paren["index_right"]]
    a = panden.geometry.loc[paren.index].boundary.values
    b = buf.geometry.loc[paren["index_right"]].values
    lengte = shapely.length(shapely.intersection(a, b))
    paren = pd.DataFrame({"a": paren.index.values, "b": paren["index_right"].values, "lengte": lengte})
    return paren[paren["lengte"] >= _GEDEELDE_MUUR_MIN_M]


def _componenten(n: int, paren: pd.DataFrame) -> np.ndarray:
    """Union-find over n knopen; geeft componentlabel per knoop."""
    ouder = np.arange(n)

    def vind(x):
        while ouder[x] != x:
            ouder[x] = ouder[ouder[x]]
            x = ouder[x]
        return x

    for a, b in zip(paren["a"].values, paren["b"].values):
        ra, rb = vind(a), vind(b)
        if ra != rb:
            ouder[ra] = rb
    return np.array([vind(i) for i in range(n)])


def classificeer(vbo: gpd.GeoDataFrame, pand: gpd.GeoDataFrame, lagen: pd.DataFrame | None) -> pd.Series:
    """ETM-woningtype per woning-VBO (index = vbo.index)."""
    vbo = vbo.copy()
    vbo["pid"] = vbo["pandidentificatie"].astype(str).str.split(",").str[0].str.strip()

    # --- a. panden met meerdere woningen: gestapeld of naast elkaar? ---
    per_pand = vbo.groupby("pid")
    n_won = per_pand.size().rename("n_won")
    xy = np.column_stack([vbo.geometry.x.values, vbo.geometry.y.values])
    vbo["_x"], vbo["_y"] = xy[:, 0], xy[:, 1]

    def min_afstand(g: pd.DataFrame) -> float:
        if len(g) < 2:
            return np.inf
        p = g[["_x", "_y"]].to_numpy()
        d = np.sqrt(((p[:, None, :] - p[None, :, :]) ** 2).sum(-1))
        np.fill_diagonal(d, np.inf)
        return float(d.min())

    multi_pids = n_won[n_won >= 2].index
    multi = vbo[vbo["pid"].isin(multi_pids)]
    afst = multi.groupby("pid")[["_x", "_y"]].apply(min_afstand).rename("min_afstand")
    info = pd.concat([n_won, afst], axis=1)

    if lagen is not None and not lagen.empty:
        info = info.join(lagen.set_index("pid")["lagen"], how="left")
    else:
        info["lagen"] = np.nan

    # zonder 3DBAG (of zonder hoogte voor dit pand) beslist alleen de VBO-spreiding
    lagen_ok = info["lagen"].isna() | (info["lagen"] <= _RIJ_IN_PAND_MAX_LAGEN)
    rij_in_pand = ((info["n_won"] >= 2) & (info["n_won"] <= _RIJ_IN_PAND_MAX_WONINGEN)
                   & (info["min_afstand"] >= _RIJ_IN_PAND_MIN_AFSTAND_M) & lagen_ok)
    info["grondgebonden"] = (info["n_won"] == 1) | rij_in_pand

    # --- b. blokken van grondgebonden panden met gedeelde muren ---
    woonpanden = pand[pand["identificatie"].astype(str).isin(info.index)].copy()
    woonpanden["pid"] = woonpanden["identificatie"].astype(str)
    woonpanden = woonpanden.drop_duplicates("pid").reset_index(drop=True)
    woonpanden = woonpanden.join(info, on="pid")
    woonpanden["grondgebonden"] = woonpanden["grondgebonden"].fillna(False).astype(bool)

    paren = _gedeelde_muren(woonpanden)
    gg = woonpanden["grondgebonden"].values
    paren_gg = paren[gg[paren["a"].values] & gg[paren["b"].values]]
    # grondgebonden pand dat aan een niet-grondgebonden (appartementen)pand vastzit
    raakt_app = np.zeros(len(woonpanden), dtype=bool)
    gemengd = paren[gg[paren["a"].values] != gg[paren["b"].values]]
    raakt_app[gemengd["a"].values[gg[gemengd["a"].values]]] = True
    raakt_app[gemengd["b"].values[gg[gemengd["b"].values]]] = True

    comp = _componenten(len(woonpanden), paren_gg)
    woonpanden["comp"] = comp
    woonpanden["n_won_gg"] = np.where(gg, woonpanden["n_won"].fillna(0), 0)
    woningen_per_blok = woonpanden.groupby("comp")["n_won_gg"].transform("sum").to_numpy()
    graad = np.bincount(np.concatenate([paren_gg["a"].values, paren_gg["b"].values]).astype(int),
                        minlength=len(woonpanden))

    # Panden met één woning. ETM telt hoekwoningen als semi_detached (geverifieerd: ETM
    # terraced ≈ CBS tussenwoning, ETM semi_detached ≈ CBS hoekwoning + 2-onder-1-kap),
    # dus in een blok van ≥3 woningen is een eindwoning (≤1 buurpand) semi_detached.
    type_enkel = np.where(woningen_per_blok >= 3,
                          np.where(graad >= 2, "terraced_houses", "semi_detached_houses"),
                 np.where(woningen_per_blok == 2, "semi_detached_houses",
                 np.where(raakt_app, "semi_detached_houses", "detached_houses")))
    type_enkel = np.where(gg, type_enkel, "apartments")
    pand_type = pd.Series(type_enkel, index=woonpanden["pid"].values)
    pand_graad = pd.Series(graad, index=woonpanden["pid"].values)

    typen = vbo["pid"].map(pand_type)

    # Rij in één pand: de twee uiterste woningen langs de hoofdas zijn eindwoningen
    # (semi_detached), tenzij daar een buurpand tegenaan staat; de rest is tussenwoning.
    rij_pids = info.index[rij_in_pand]
    rij = vbo[vbo["pid"].isin(rij_pids)]
    for pid, g in rij.groupby("pid"):
        p = g[["_x", "_y"]].to_numpy()
        as_ = np.linalg.svd(p - p.mean(0), full_matrices=False)[2][0]
        volgorde = g.index[np.argsort((p - p.mean(0)) @ as_)]
        n_eind = int(min(len(g), max(0, 2 - pand_graad.get(pid, 0))))
        typen.loc[volgorde] = "terraced_houses"
        if n_eind >= 1:
            typen.loc[volgorde[0]] = "semi_detached_houses"
        if n_eind >= 2:
            typen.loc[volgorde[-1]] = "semi_detached_houses"

    # VBO's waarvan het pand niet in de pandlaag zit (rand van het gebied, inconsistentie)
    fallback = typen.isna()
    if fallback.any():
        typen[fallback] = np.where(vbo.loc[fallback, "pid"].map(n_won) >= 2, "apartments", "detached_houses")
        log.info("  %d woningen zonder pandgeometrie → type via aantal woningen in pand", int(fallback.sum()))
    return typen


def pas_ep_online_toe(vbo: gpd.GeoDataFrame, typen: pd.Series, pad: Path) -> pd.Series:
    """Overschrijf typen met EP-Online gebouwtype waar beschikbaar."""
    ep = pd.read_csv(pad, sep=None, engine="python", dtype=str)
    id_kol = next((c for c in ep.columns if "verblijfsobject" in c.lower()), None)
    type_kol = next((c for c in ep.columns if "gebouwtype" in c.lower() and "sub" not in c.lower()), None)
    sub_kol = next((c for c in ep.columns if "gebouwsubtype" in c.lower()), None)
    if id_kol is None or type_kol is None:
        log.warning("  EP-Online: kolommen voor VBO-id/gebouwtype niet gevonden in %s — overgeslagen", pad.name)
        return typen

    def map_type(s: str) -> str | None:
        s = (s or "").lower()
        for sleutel, etm in _EP_ONLINE_TYPE_MAP:
            if sleutel in s:
                return etm
        return None

    tekst = ep[type_kol].fillna("") + " " + (ep[sub_kol].fillna("") if sub_kol else "")
    ep_map = pd.Series(tekst.map(map_type).values, index=ep[id_kol].str.zfill(16).values).dropna()
    ep_map = ep_map[~ep_map.index.duplicated(keep="last")]
    nieuw = vbo["identificatie"].astype(str).str.zfill(16).map(ep_map)
    n = int(nieuw.notna().sum())
    gewijzigd = int((nieuw.notna() & (nieuw != typen)).sum())
    log.info("  EP-Online: %d woningen met gebouwtype (%.0f%%), %d wijken af van geometrische afleiding",
             n, 100 * n / max(len(vbo), 1), gewijzigd)
    return nieuw.fillna(typen)


# =========================================================================================
# Aggregatie + kalibratie
# =========================================================================================

def _kolom(prefix: str, t: str, p: str) -> str:
    return f"{prefix}_{t}_{p}"


def aggregeer(vbo: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aantal en m² per buurt × type × periode (breed)."""
    n = vbo.pivot_table(index="buurtcode", columns=["type", "periode"], values="identificatie",
                        aggfunc="count", fill_value=0)
    m2 = vbo.pivot_table(index="buurtcode", columns=["type", "periode"], values="oppervlakte",
                         aggfunc="sum", fill_value=0)
    full = pd.MultiIndex.from_product([TYPES, PERIODS])
    return n.reindex(columns=full, fill_value=0), m2.reindex(columns=full, fill_value=0)


def _ipf(seed: np.ndarray, rij_doel: np.ndarray, kol_doel: np.ndarray, iteraties: int = 100) -> np.ndarray:
    x = seed.astype(float).copy()
    for _ in range(iteraties):
        rs = x.sum(1)
        x *= np.divide(rij_doel, rs, out=np.zeros_like(rs), where=rs > 0)[:, None]
        ks = x.sum(0)
        x *= np.divide(kol_doel, ks, out=np.zeros_like(ks), where=ks > 0)[None, :]
        if np.allclose(x.sum(1), rij_doel, rtol=1e-6, atol=1e-6):
            break
    return x


def kalibreer(n: pd.DataFrame, m2: pd.DataFrame, cbs: pd.DataFrame, gm_van: pd.Series
              ) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """IPF per buurt: type-totalen → CBS-percentages × BAG-totaal, periode-totalen → BAG."""
    n_cal, m2_cal = n.copy().astype(float), m2.copy().astype(float)
    status = pd.Series("bag", index=n.index)

    # gemeentelijke prior voor (type, periode) — vult cellen die in de buurt-seed leeg zijn
    gm_n = n.groupby(gm_van.reindex(n.index)).sum()
    gm_m2 = m2.groupby(gm_van.reindex(n.index)).sum()
    gm_m2_per = (gm_m2 / gm_n.where(gm_n > 0)).fillna(0)
    type_m2_per = (m2.T.groupby(level=0).sum().T.sum() / n.T.groupby(level=0).sum().T.sum().replace(0, np.nan))

    for bu in n.index:
        if bu not in cbs.index:
            continue
        pct = cbs.loc[bu]
        if pct.isna().any() or pct.sum() <= 0:
            continue
        seed = n.loc[bu].to_numpy().reshape(len(TYPES), len(PERIODS)).astype(float)
        totaal = seed.sum()
        if totaal <= 0:
            continue
        rij_doel = (pct[TYPES].to_numpy() / pct.sum()) * totaal
        kol_doel = seed.sum(0)
        gm = gm_van.get(bu)
        prior = gm_n.loc[gm].to_numpy().reshape(len(TYPES), len(PERIODS)) if gm in gm_n.index else seed
        prior = prior / max(prior.sum(), 1)
        x = _ipf(seed + 1e-3 * totaal * prior, rij_doel, kol_doel)

        # m² per cel: buurtgemiddelde, anders gemeentegemiddelde, anders typegemiddelde
        seed_m2 = m2.loc[bu].to_numpy().reshape(len(TYPES), len(PERIODS))
        per = np.divide(seed_m2, seed, out=np.full_like(seed, np.nan), where=seed > 0)
        if gm in gm_m2_per.index:
            gm_per = gm_m2_per.loc[gm].to_numpy().reshape(len(TYPES), len(PERIODS))
            per = np.where(np.isnan(per) & (gm_per > 0), gm_per, per)
        per = np.where(np.isnan(per), type_m2_per.reindex(TYPES).to_numpy()[:, None], per)

        n_cal.loc[bu] = x.reshape(-1)
        m2_cal.loc[bu] = (x * np.nan_to_num(per)).reshape(-1)
        status[bu] = "cbs_ipf"
    return n_cal, m2_cal, status


def laad_cbs_typen(jaar: int) -> pd.DataFrame:
    pad = Path(str(_CBS_BUURTEN_CSV).format(jaar=jaar))
    if not pad.exists():
        log.warning("  CBS buurten-CSV %s ontbreekt — geen kalibratie", pad.name)
        return pd.DataFrame(columns=TYPES)
    with open(pad, encoding="utf-8") as f:
        sep = ";" if ";" in f.readline() else ","
    df = pd.read_csv(pad, dtype=str, sep=sep)
    df = df[df["codering"].str.startswith("BU", na=False)].set_index("codering")
    out = pd.DataFrame(index=df.index)
    for t, kols in _CBS_TYPE_KOLOMMEN.items():
        if not all(k in df.columns for k in kols):
            log.warning("  CBS-kolommen %s ontbreken in %s — geen kalibratie", kols, pad.name)
            return pd.DataFrame(columns=TYPES)
        vals = df[kols].apply(pd.to_numeric, errors="coerce")
        vals = vals.where(vals >= 0)  # -99999 = geheim/onbekend
        out[t] = vals.sum(axis=1, min_count=len(kols))
    out["cbs_woningvoorraad"] = pd.to_numeric(df.get("woningvoorraad_woningvoorraad"), errors="coerce")
    return out


def _breed(n: pd.DataFrame, m2: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=n.index)
    for t in TYPES:
        for p in PERIODS:
            out[_kolom("woningen", t, p)] = n[(t, p)].values
    for t in TYPES:
        for p in PERIODS:
            out[_kolom("m2", t, p)] = m2[(t, p)].values
    return out


# =========================================================================================
# Main
# =========================================================================================

def main(gemeenten: list[str] | None = None, buurtjaar: int = 2024, met_3dbag: bool = True,
         ep_online: Path | None = None, etm_startjaar: int = 2023) -> bool:
    t0 = time.monotonic()
    gemeenten = gemeenten or DEFAULT_GEMEENTEN
    gm_key = "_".join(sorted(gemeenten))
    today = date.today().isoformat()

    # --- buurtgrenzen ---
    gpkg = _RAW_WB / str(buurtjaar) / "buurten.gpkg"
    if not gpkg.exists():
        log.error("Buurtgrenzen ontbreken: %s — run eerst: python run_pipeline.py --download", gpkg)
        return False
    buurten = gpd.read_file(gpkg, columns=["buurtcode", "buurtnaam", "gemeentecode", "gemeentenaam", "water"])
    buurten = buurten.to_crs(CRS_RD)
    buurten = buurten[buurten["gemeentecode"].isin(gemeenten)]
    if buurten.empty:
        log.error("Geen buurten gevonden voor %s in %s", gemeenten, gpkg.name)
        return False
    gebied = buurten.union_all().buffer(50)
    log.info("Gebied: %d gemeenten, %d buurten", len(gemeenten), len(buurten))

    # --- downloads ---
    try:
        vbo = download_laag("bag_verblijfsobject", BAG_WFS, "bag:verblijfsobject", gebied, gm_key)
        pand = download_laag("bag_pand", BAG_WFS, "bag:pand", gebied, gm_key)
        lagen = None
        if met_3dbag:
            b3 = download_laag("3dbag_pand", BAG3D_WFS, "BAG3D:lod13", gebied, gm_key, geometrie=False,
                               extra={"PROPERTYNAME": "identificatie,b3_bouwlagen,b3_h_70p,b3_h_maaiveld"})
            b3["pid"] = b3["identificatie"].astype(str).str.replace("NL.IMBAG.Pand.", "", regex=False)
            hoogte_lagen = ((pd.to_numeric(b3["b3_h_70p"], errors="coerce")
                             - pd.to_numeric(b3["b3_h_maaiveld"], errors="coerce")) / _VERDIEPINGSHOOGTE_M).round()
            b3["lagen"] = pd.to_numeric(b3["b3_bouwlagen"], errors="coerce").fillna(hoogte_lagen.clip(lower=1))
            lagen = b3[["pid", "lagen"]].drop_duplicates("pid")
    except RuntimeError as exc:
        log.error("Download mislukt: %s", exc)
        return False

    # --- woningen selecteren ---
    vbo = vbo.to_crs(CRS_RD)
    is_woning = (vbo["gebruiksdoel"].astype(str).str.contains("woonfunctie", na=False)
                 & vbo["status"].isin(_VBO_STATUS_WONING))
    woningen = vbo[is_woning].copy()
    woningen["identificatie"] = woningen["identificatie"].astype(str)
    woningen["oppervlakte"] = pd.to_numeric(woningen["oppervlakte"], errors="coerce")
    # <10 m² en >1000 m² zijn registratieruis of zorg-/complexwoningen → mediaan van de cel
    woningen.loc[(woningen["oppervlakte"] < 10) | (woningen["oppervlakte"] > 1000), "oppervlakte"] = np.nan
    log.info("Woningen (woonfunctie, in gebruik): %d van %d verblijfsobjecten", len(woningen), len(vbo))

    woningen = gpd.sjoin(woningen, buurten[["buurtcode", "gemeentecode", "geometry"]],
                         predicate="within", how="inner").drop(columns="index_right")
    log.info("Woningen binnen de buurten van het gebied: %d", len(woningen))
    # ontbrekende m² → mediaan van hetzelfde type/periode volgt na classificatie

    # --- classificatie ---
    woningen["periode"] = periode_van(woningen["bouwjaar"], etm_startjaar)
    woningen["type"] = classificeer(woningen, pand.to_crs(CRS_RD), lagen).values
    if ep_online is not None:
        woningen["type"] = pas_ep_online_toe(woningen, woningen["type"], ep_online).values
    med = woningen.groupby(["gemeentecode", "type", "periode"])["oppervlakte"].transform("median")
    woningen["oppervlakte"] = woningen["oppervlakte"].fillna(med).fillna(woningen["oppervlakte"].median())

    n, m2 = aggregeer(woningen)
    gm_van = buurten.set_index("buurtcode")["gemeentecode"]

    # --- kalibratie op CBS ---
    cbs = laad_cbs_typen(buurtjaar)
    n_cal, m2_cal, status = kalibreer(n, m2, cbs[TYPES] if not cbs.empty else cbs, gm_van)
    log.info("Kalibratie: %d buurten via CBS-IPF, %d alleen BAG", int((status == "cbs_ipf").sum()),
             int((status == "bag").sum()))

    # --- ETM area ---
    etm, etm_totalen = haal_etm_area(gemeenten)

    # --- output buurten ---
    basis = buurten.drop(columns="geometry").set_index("buurtcode").reindex(n.index)
    basis["woningen_bag_totaal"] = n.sum(axis=1)
    basis["m2_bag_totaal"] = m2.sum(axis=1).round(0)
    if not cbs.empty:
        basis["cbs_woningvoorraad"] = cbs["cbs_woningvoorraad"].reindex(n.index)
        typen_bag = n.T.groupby(level=0).sum().T
        for t in TYPES:
            basis[f"aandeel_{t}_bag_pct"] = (100 * typen_bag[t] / basis["woningen_bag_totaal"]).round(1)
            basis[f"aandeel_{t}_cbs_pct"] = cbs[t].reindex(n.index)
    basis["typeverdeling_bron"] = status

    gekal = pd.concat([basis, _breed(n_cal, m2_cal).round(2)], axis=1)
    ruw = pd.concat([basis, _breed(n, m2)], axis=1)

    # bottom-up ruimteverwarming op ETM-intensiteit van de gemeente (validatie, kWh/jaar)
    if not etm.empty:
        q = etm[etm["gemeentecode"].str.startswith("GM")].set_index(["gemeentecode", "type", "periode"])[
            "etm_typical_useful_demand_kWh_m2"]
        bottom_up = pd.Series(0.0, index=gekal.index)
        for t in TYPES:
            for p in PERIODS:
                qq = gekal["gemeentecode"].map(lambda g: q.get((g, t, p), np.nan)).astype(float)
                bottom_up += gekal[_kolom("m2", t, p)].astype(float) * qq.fillna(0)
        gekal["ruimteverwarming_nuttig_etm_bottomup_kWh"] = bottom_up.round(0)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    uit = {
        f"woningvoorraad_etm_buurten_{today}.csv": gekal,
        f"woningvoorraad_etm_buurten_bag_ongekalibreerd_{today}.csv": ruw,
    }
    for naam, df in uit.items():
        pad = PROCESSED_DIR / naam
        tmp = pad.with_suffix(".tmp.csv")
        df.reset_index(names="buurtcode").fillna(FILL_UNMATCHED).to_csv(
            tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(pad)
        log.info("Opgeslagen: %s (%d buurten, %d kolommen)", naam, len(df), df.shape[1] + 1)

    # --- vergelijking per gemeente ---
    rijen = []
    for gm in gemeenten:
        sel = gekal.index[gekal["gemeentecode"] == gm]
        for t in TYPES:
            for p in PERIODS:
                rijen.append({"gemeentecode": gm, "type": t, "periode": p,
                              "bag_woningen": float(n.loc[sel, (t, p)].sum()),
                              "gekalibreerd_woningen": float(n_cal.loc[sel, (t, p)].sum()),
                              "gekalibreerd_m2": float(m2_cal.loc[sel, (t, p)].sum())})
    verg = pd.DataFrame(rijen)
    if not etm.empty:
        verg = verg.merge(etm[["gemeentecode", "type", "periode", "etm_present_number",
                               "etm_typical_useful_demand_kWh_m2", "etm_present_share_in_useful_demand"]],
                          on=["gemeentecode", "type", "periode"], how="left")
        verg["m2_per_woning"] = (verg["gekalibreerd_m2"] / verg["gekalibreerd_woningen"].replace(0, np.nan)).round(1)
        etm_pad = PROCESSED_DIR / f"etm_area_woningvoorraad_{today}.csv"
        etm.to_csv(etm_pad, sep=OUTPUT_SEPARATOR, index=False)
        # vaste naam in data_Generic/RES/ voor het model (J_ETMAreaData); samenvoegen, zodat gebieden
        # van andere projecten (of de landelijke run van make_etm_area_csv.py) blijven staan
        schrijf_vaste_bestanden(etm, etm_totalen, samenvoegen=True)
        log.info("Opgeslagen: %s (+ samengevoegd in data_Generic/RES/etm_area_*.csv)", etm_pad.name)
        tot = verg.groupby("gemeentecode")[["bag_woningen", "etm_present_number"]].sum()
        for gm, r in tot.iterrows():
            log.info("  %s: BAG %6.0f woningen  vs ETM %6.0f  (%+.1f%%)", gm, r["bag_woningen"],
                     r["etm_present_number"], 100 * (r["bag_woningen"] / r["etm_present_number"] - 1)
                     if r["etm_present_number"] else float("nan"))
    verg_pad = PROCESSED_DIR / f"woningvoorraad_etm_gemeenten_vergelijking_{today}.csv"
    verg.to_csv(verg_pad, sep=OUTPUT_SEPARATOR, index=False)
    log.info("Opgeslagen: %s", verg_pad.name)

    log.info("Woningvoorraad ETM-indeling klaar in %.0fs", time.monotonic() - t0)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gemeenten", nargs="+", default=None, help="GM-codes (default: Drechtsteden)")
    ap.add_argument("--buurtjaar", type=int, default=2024, help="CBS buurtindeling + kerncijfers-jaar")
    ap.add_argument("--zonder-3dbag", action="store_true", help="3DBAG overslaan (snellere, grovere typering)")
    ap.add_argument("--ep-online", type=Path, default=None, help="EP-Online CSV met gebouwtype per VBO")
    ap.add_argument("--etm-startjaar", type=int, default=2023, help="bouwjaar > dit jaar → 'future'")
    a = ap.parse_args()
    sys.exit(0 if main(a.gemeenten, a.buurtjaar, not a.zonder_3dbag, a.ep_online, a.etm_startjaar) else 1)
