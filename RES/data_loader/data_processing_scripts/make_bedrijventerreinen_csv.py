"""Bedrijventerreinen: vestigingen, bedrijfsruimte en energiegebruik per IBIS-terrein, verdeeld zoals LUX per buurt.

LUX rekent per CBS-buurt; een bedrijventerrein is daarin geen eenheid. Deze stap rekent de buurtwaarden van
--buurten (kerncijfers_buurten_met_geometrie_<jaar>.csv) terug naar de IBIS-terreinen, in zes stappen:

  1. Terreinen. IBIS-contouren (WarmteAtlas, --process) die in de gekozen gemeenten liggen.
  2. Koppeling. Doorsnede terrein x buurt. Per stuk: het deel van de bedrijfsadressen (BAG, niet-wonen) en van
     alle adressen van de buurt dat erin ligt, en de m² per BAG-gebruiksfunctie (in gebruik, gebouwd t/m het
     buurtjaar; een object met meerdere functies telt naar rato, zoals make_bedrijfsruimte_csv.py).
  3. Energie. Per sectorgroep krijgt een terrein het deel van het buurtverbruik dat hoort bij de m² van de
     functies die make_buurten_csv.py voor die groep als verdeelsleutel gebruikt. Een functie die meerdere
     groepen gebruiken, wordt per buurt op dezelfde manier verdeeld (aandeel_gedeelde_functies). Landbouw, en
     een groep zonder m² in de buurt, gaat naar rato van de bedrijfsadressen. Wat niet op een terrein ligt,
     blijft bij de buurt, dus terreinen plus de rest van de buurten tellen precies op tot de buurtwaarden.
  4. Vestigingen. Het totaal is het KWB-aantal van de buurt x het deel van de bedrijfsadressen op het terrein,
     zoals LUX het ziet. De verdeling over de SBI-letters komt uit de CBS/TNO-telling 2019 op het terrein
     (Tabel 3): de enige telling per terrein en per letter. De buurtsamenstelling zou die verdeling scheeftrekken,
     omdat in een gemengde buurt ook scholen, zorg en zzp'ers aan huis meetellen. Zonder telling volgt een
     terrein de KWB-groepen van zijn buurten, binnen een groep naar de verhouding op de getelde terreinen.
  5. Uitsplitsing. B-F (in de brondata één groep) naar B-E en F, en elke groep naar SBI-letters: vestigingen per
     letter x gemiddelde levering per adres op alle Nederlandse terreinen (CBS/TNO 2019 Tabel 2, zonder
     grootverbruikers). Bij de splitsing B-E/F tellen BE_F_AFRONDING extra vestigingen mee naar de verhouding op
     de getelde terreinen (één afrondingseenheid van Tabel 3), zodat een terrein met 0 geteld in B-E niet al zijn
     industriehallen bij de bouw zet.
  6. Industriegraad. Aandeel van B-E in elektriciteit plus gas (gas in kWh, onderwaarde 8,79 kWh/m³).

Bronnen, alle open:
  - IBIS-bedrijventerreinen via de WarmteAtlas (RVO), processed_data_from_loader/IBISbedrijventerreinen_nl_*.csv;
  - BAG-verblijfsobjecten via PDOK (cache raw/bag, gedeeld met --bedrijfsruimte en --woningvoorraad);
  - kerncijfers_buurten (--buurten): CBS KWB-vestigingen en het sectorverbruik per buurt;
  - CBS, Levering aardgas en elektriciteit via openbaar net aan bedrijventerreinen, 2019 (maatwerk juni 2021,
    in opdracht van TNO; alle 3.740 IBIS-terreinen): data_loader/levering-aardgas-en-elektriciteit-via-openbaar-
    net-aan-bedrijventerreinen-2019.xlsx.

Uitvoer (processed_data_from_loader/):
  bedrijventerreinen_<jaar>_<datum>.csv          één rij per terrein
  bedrijventerreinen_buurten_<jaar>_<datum>.csv  één rij per stuk terrein x buurt, met de aandelen

Run standalone:  python make_bedrijventerreinen_csv.py [--gemeenten GM0505 ...] [--jaren 2023 2024]
Or via:          python run_pipeline.py --bedrijventerreinen   (na --buurten)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import wkt

from config import CBS_YEARS, CRS_RD, OUTPUT_SEPARATOR, PROCESSED_DIR
from make_bedrijfsruimte_csv import FUNCTIES, m2_per_functie
from make_buurten_csv import _FUNCTIES_PER_GROEP, _GROEP_KWB, _OUT_DIR as BUURTEN_DIR, aandeel_gedeelde_functies
from make_woningvoorraad_etm_csv import BAG_WFS, DEFAULT_GEMEENTEN, download_laag

log = logging.getLogger(__name__)

CBS2019 = (Path(__file__).resolve().parent.parent
           / "levering-aardgas-en-elektriciteit-via-openbaar-net-aan-bedrijventerreinen-2019.xlsx")
GAS_KWH_PER_M3 = 8.79      # onderwaarde, zoals het model (AVGC p_gas_kWhpm3_LHV)
BE_F_AFRONDING = 5.0       # extra vestigingen bij de splitsing B-E/F (stap 5)
_NIET_WONEN_UIT = {"woonfunctie", "overige gebruiksfunctie"}   # bergingen, garages e.d. tellen niet als bedrijf
_STATUS_WEG = "buiten gebruik|ingetrokken|niet gerealiseerd"

LETTERS = "ABCDEFGHIJKLMNOPQRSTU"
# Sectorgroepen van make_energieverbruik_sector_csv.py (zoals in het model)
G_CODES = ["a", "bf", "gi", "h", "j", "kl", "mn", "oq", "ru"]
G_LETTERS = {"a": "A", "bf": "BCDEF", "gi": "GI", "h": "H", "j": "J", "kl": "KL", "mn": "MN", "oq": "OPQ",
             "ru": "RSTU"}
# Weergavegroepen: B-F gesplitst in industrie (B-E) en bouw (F)
D_LETTERS = {"be": "BCDE", "f": "F", "h": "H", "gi": "GI", "j": "J", "kl": "KL", "mn": "MN", "oq": "OPQ",
             "ru": "RSTU", "a": "A"}
D_CODES = list(D_LETTERS)
LETTER_DGROEP = {l: g for g, ls in D_LETTERS.items() for l in ls}
# KWB-groepen (KWB telt H en J samen) en hun kolommen in de buurtdata
KWB_KOL = {kol.split("_")[0]: kol for kol in sorted(set(_GROEP_KWB.values()))}
KWB_LETTERS = {k: "".join(l for g, ls in G_LETTERS.items() if _GROEP_KWB[g] == kol for l in ls)
               for k, kol in KWB_KOL.items()}

_KLEIN = {"en", "van", "de", "het", "der", "den", "op", "aan", "in", "bij"}
_ROMEINS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def naam_nl(s):
    """IBIS-naam in hoofdletters naar leesbare vorm: 'DORDTSE KIL III' -> 'Dordtse Kil III',
    '1E MERWEDEHAVEN' -> '1e Merwedehaven', 'GRAVENDEELSEDIJK E.O.' -> 'Gravendeelsedijk e.o.'."""
    if not isinstance(s, str):
        return s
    uit = []
    for i, w in enumerate(s.strip().split()):
        lw = w.lower()
        kern = lw.strip("()';,")
        if kern in _ROMEINS:
            uit.append(w.upper())
        elif re.fullmatch(r"\d+e", lw):
            uit.append(lw)
        elif lw in ("e.o.", "ea", "e.a.", "'t", "'s"):
            uit.append({"ea": "e.a."}.get(lw, lw))
        elif i > 0 and lw in _KLEIN:
            uit.append(lw)
        else:
            uit.append(re.sub(r"(^|[-/+(])(\w)", lambda m: m.group(1) + m.group(2).upper(), lw))
    return " ".join(uit)


def _getal(x):
    """CBS-cel naar float; '.' en -99999 (afgeschermd) worden NaN."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return np.nan
    return np.nan if v <= -99999 else v


def _laatste(patroon: str) -> Path:
    paden = sorted(PROCESSED_DIR.glob(patroon), reverse=True)
    if not paden:
        raise FileNotFoundError(f"{patroon} niet gevonden in {PROCESSED_DIR}")
    return paden[0]


# ---------------------------------------------------------------------------------------------
# Inlezen
# ---------------------------------------------------------------------------------------------
def lees_ibis(gemeenten: list[str]) -> gpd.GeoDataFrame:
    df = pd.read_csv(_laatste("IBISbedrijventerreinen_nl_*.csv"), sep=OUTPUT_SEPARATOR, low_memory=False)
    df = df[df.GEM_CODES.astype(str).isin(gemeenten) | df.gemeentecode_2023.astype(str).isin(gemeenten)]
    g = gpd.GeoDataFrame(df[["RIN_NUMMER", "PLAN_NAAM", "GEM_CODES", "GEM_NAMEN", "HA_BRUTO", "HA_NETTO",
                             "MAX_MILIEU", "WLOC_TYPE", "STATUS", "PLAN_FASE"]].copy(),
                         geometry=df.geom_wkt.map(wkt.loads), crs="EPSG:4326").to_crs(CRS_RD)
    g = g.rename(columns={"RIN_NUMMER": "rin"})
    g["rin"] = g.rin.astype(int)
    g["geometry"] = g.geometry.buffer(0)
    return g


def lees_buurten(jaar: int, gemeenten: list[str]) -> gpd.GeoDataFrame:
    """Buurten uit --buurten met k_<kwb> (vestigingen), e_<groep> (kWh) en g_<groep> (m³)."""
    df = pd.read_csv(BUURTEN_DIR / f"kerncijfers_buurten_met_geometrie_{jaar}.csv", sep=OUTPUT_SEPARATOR,
                     low_memory=False, dtype={"codering": str})
    df = df[("GM" + df.codering.str[2:6]).isin(gemeenten)].copy()

    def num(col):
        v = pd.to_numeric(df[col], errors="coerce")
        return v.where(v != -99999).fillna(0.0).clip(lower=0)

    for k, kol in KWB_KOL.items():
        df[f"k_{k}"] = num(kol)
    df["k_totaal"] = num("bedrijfsvestigingen_totaal")
    for g in G_CODES:
        df[f"e_{g}"] = num(f"elec_verbruik_{g}_kwh")
        df[f"g_{g}"] = num(f"gas_verbruik_{g}_m3")
    gdf = gpd.GeoDataFrame(df, geometry=df.wkt_geometry.map(wkt.loads), crs="EPSG:4326").to_crs(CRS_RD)
    gdf["geometry"] = gdf.geometry.buffer(0)
    gdf["buurt_m2"] = gdf.geometry.area
    return gdf


def lees_bag(gemeenten: list[str], buurten: gpd.GeoDataFrame, jaar: int):
    """BAG-verblijfsobjecten: alle adressen (met vlag 'bedrijf') en de bedrijfsruimte per functie t/m jaar."""
    vbo = download_laag("bag_verblijfsobject", BAG_WFS, "bag:verblijfsobject", buurten.union_all().buffer(50),
                        "_".join(sorted(gemeenten))).to_crs(CRS_RD)
    a = vbo[~vbo.status.str.contains(_STATUS_WEG, case=False, na=False)]
    doelen = a.gebruiksdoel.fillna("").astype(str).str.split(",")
    adressen = gpd.GeoDataFrame(
        {"bedrijf": doelen.map(lambda d: any(x.strip() and x.strip() not in _NIET_WONEN_UIT for x in d))},
        geometry=a.geometry, crs=CRS_RD)
    obj = m2_per_functie(vbo)
    obj = obj[obj.bouwjaar.isna() | (obj.bouwjaar <= jaar)]
    return adressen, obj[[f"m2_{f}" for f in FUNCTIES] + ["geometry"]]


def lees_cbs2019(rins: set[int], gemeentenamen: set[str]) -> pd.DataFrame:
    """CBS/TNO 2019 per terrein: vestigingen per SBI-letter (Tabel 3) en gemeten levering (Tabel 1)."""
    t1 = pd.read_excel(CBS2019, "Tabel 1", header=None).iloc[8:, :12]
    t1.columns = ["rin", "naam", "gemcode", "gemeente", "pv", "provincie", "adressen", "gas_km3",
                  "elec_mwh", "gv_adressen", "gv_gas_km3", "gv_elec_mwh"]
    t1 = t1[pd.to_numeric(t1.rin, errors="coerce").notna()].copy()
    t1["rin"] = t1.rin.astype(int)
    for k in ["adressen", "gas_km3", "elec_mwh", "gv_adressen", "gv_gas_km3", "gv_elec_mwh"]:
        t1[k] = t1[k].map(_getal)
    t3 = pd.read_excel(CBS2019, "Tabel 3", header=None).iloc[8:, :25]
    t3.columns = ["rin", "naam", "gemeente", "totaal"] + list(LETTERS)
    t3 = t3[pd.to_numeric(t3.rin, errors="coerce").notna()].copy()
    t3["rin"] = t3.rin.astype(int)
    for k in ["totaal"] + list(LETTERS):
        t3[k] = t3[k].map(_getal)
    t3 = t3[t3.rin.isin(rins) | t3.gemeente.isin(gemeentenamen)]
    t1 = t1[t1.rin.isin(rins) | t1.gemeente.isin(gemeentenamen)]
    return t3.merge(t1.drop(columns=["naam", "gemeente", "pv", "provincie", "gemcode"]), on="rin", how="outer")


def _tabel2() -> pd.DataFrame:
    """CBS/TNO 2019 Tabel 2: levering op alle Nederlandse bedrijventerreinen per SBI-letter."""
    t2 = pd.read_excel(CBS2019, "Tabel 2", header=None).iloc[10:30, :11]
    t2.columns = ["l", "naam", "adr", "gas", "elec", "m_adr", "m_gas", "m_elec", "g_adr", "g_gas", "g_elec"]
    t2 = t2[t2.l.astype(str).str.len() == 1].set_index("l")
    for c in t2.columns[1:]:
        t2[c] = t2[c].map(_getal)
    return t2


def intensiteit_groepen() -> dict:
    """Gemiddelde levering per adres op Nederlandse terreinen per weergavegroep, zonder grootverbruikers
    (> 1 mln m³ gas of > 10 GWh stroom): {groep: (MWh elektriciteit, 1000 m³ gas)}."""
    t2 = _tabel2()
    uit = {}
    for g, letters in D_LETTERS.items():
        r = t2.loc[[l for l in letters if l in t2.index]]
        r = r[r.elec.notna() & r.gas.notna()]          # S en U zijn afgeschermd
        adr = (r.adr - r.g_adr.fillna(0)).sum()
        uit[g] = ((r.elec - r.g_elec.fillna(0)).sum() / adr, (r.gas - r.g_gas.fillna(0)).sum() / adr)
    # Landbouw op terreinen is landelijk vooral glastuinbouw (circa 80.000 m³ gas per bedrijf); voor de
    # hoveniers en loonwerkers op gewone terreinen is dat veel te hoog. Daarom het gemiddelde van handel.
    uit["a"] = uit["gi"]
    return uit


def intensiteit_letters() -> dict:
    """Als intensiteit_groepen, per SBI-letter. Afgeschermde letters (S, U) en T krijgen het gemiddelde van
    hun weergavegroep, landbouw dat van handel."""
    t2, groep = _tabel2(), intensiteit_groepen()
    uit = {}
    for l in LETTERS:
        r = t2.loc[l] if l in t2.index else None
        adr = (r.adr - (0 if pd.isna(r.g_adr) else r.g_adr)) if r is not None else 0
        if r is not None and pd.notna(r.elec) and pd.notna(r.gas) and adr > 0:
            uit[l] = ((r.elec - (0 if pd.isna(r.g_elec) else r.g_elec)) / adr,
                      (r.gas - (0 if pd.isna(r.g_gas) else r.g_gas)) / adr)
        else:
            uit[l] = groep[LETTER_DGROEP[l]]
    uit["A"] = groep["a"]
    return uit


# ---------------------------------------------------------------------------------------------
# Stap 2: koppeling terrein x buurt
# ---------------------------------------------------------------------------------------------
def koppel(parken: gpd.GeoDataFrame, buurten: gpd.GeoDataFrame, adressen, objecten) -> gpd.GeoDataFrame:
    """Stukken terrein x buurt met de aandelen: pct_terrein, pct_buurt (oppervlak), a_bedrijf en a_adres
    (adressen), m2x_<f> / m2b_<f> (m² bedrijfsruimte in het stuk en in de hele buurt), s_<f>_<g> (deel van
    een gedeelde functie per groep in de buurt) en a_<g> (deel van het buurtverbruik van groep g)."""
    x = gpd.overlay(parken[["rin", "geometry"]], buurten[["codering", "buurt_m2", "geometry"]],
                    how="intersection", keep_geom_type=True)
    x["m2"] = x.geometry.area
    x = x[x.m2 > 100].copy()
    park_m2 = parken.assign(m2=parken.geometry.area).groupby("rin").m2.sum()
    x["pct_terrein"] = x.m2 / x.rin.map(park_m2)
    x["pct_buurt"] = x.m2 / x.buurt_m2
    sleutel = list(zip(x.rin, x.codering))

    in_x = gpd.sjoin(adressen, x[["rin", "codering", "geometry"]], predicate="within") \
        .groupby(["rin", "codering"]).agg(n=("bedrijf", "size"), nb=("bedrijf", "sum"))
    in_b = gpd.sjoin(adressen, buurten[["codering", "geometry"]], predicate="within") \
        .groupby("codering").agg(n=("bedrijf", "size"), nb=("bedrijf", "sum"))
    nb_x = pd.Series([in_x.nb.get(k, 0) for k in sleutel], index=x.index, dtype=float)
    n_x = pd.Series([in_x.n.get(k, 0) for k in sleutel], index=x.index, dtype=float)
    nb_b = x.codering.map(in_b.nb).fillna(0)
    n_b = x.codering.map(in_b.n).fillna(0)
    x["a_bedrijf"] = np.where(nb_b > 0, nb_x / nb_b.where(nb_b > 0, 1), x.pct_buurt)
    x["a_adres"] = np.where(n_b > 0, n_x / n_b.where(n_b > 0, 1), x.pct_buurt)

    kol = [f"m2_{f}" for f in FUNCTIES]
    m2_x = gpd.sjoin(objecten, x[["rin", "codering", "geometry"]], predicate="within").groupby(["rin", "codering"])[kol].sum()
    m2_b = gpd.sjoin(objecten, buurten[["codering", "geometry"]], predicate="within").groupby("codering")[kol].sum()
    for f in FUNCTIES:
        x[f"m2x_{f}"] = [m2_x[f"m2_{f}"].get(k, 0.0) for k in sleutel]
        x[f"m2b_{f}"] = x.codering.map(m2_b[f"m2_{f}"]).fillna(0.0)
    noemer = sum(x[f"m2b_{f}"] for f in FUNCTIES)
    x["aandeel_m2"] = (sum(x[f"m2x_{f}"] for f in FUNCTIES) / noemer.where(noemer > 0)).fillna(0.0)

    # Gedeelde functies per buurt verdeeld zoals make_buurten_csv (H+J naar het gemeentelijk verbruik H:J)
    b = buurten.set_index("codering")
    som = b.groupby(b.index.str[2:6])[["e_h", "e_j"]].transform("sum")
    h = (som.e_h / (som.e_h + som.e_j).where(som.e_h + som.e_j > 0)).fillna(0.5)
    for (f, g), s in aandeel_gedeelde_functies(b, h).items():
        x[f"s_{f}_{g}"] = x.codering.map(s).fillna(1.0)

    for g in G_CODES:
        functies = _FUNCTIES_PER_GROEP.get(g)
        if not functies:
            x[f"a_{g}"] = x.a_bedrijf
            continue
        w = {f: x[f"s_{f}_{g}"] if f"s_{f}_{g}" in x else 1.0 for f in functies}
        teller = sum(x[f"m2x_{f}"] * w[f] for f in functies)
        noemer = sum(x[f"m2b_{f}"] * w[f] for f in functies)
        x[f"a_{g}"] = np.where(noemer > 0, teller / noemer.where(noemer > 0, 1), x.a_bedrijf)
    return x


# ---------------------------------------------------------------------------------------------
# Stappen 3-6 per terrein
# ---------------------------------------------------------------------------------------------
@dataclass
class Resultaat:
    parken: gpd.GeoDataFrame      # IBIS-terreinen (stap 1)
    buurten: gpd.GeoDataFrame     # buurten met k_, e_ (kWh), g_ (m³)
    stukken: gpd.GeoDataFrame     # terrein x buurt met de aandelen (stap 2)
    cbs2019: pd.DataFrame         # CBS/TNO 2019 per terrein
    terreinen: pd.DataFrame       # één rij per terrein (stappen 3-6), index rin


def bereken(gemeenten: list[str], jaar: int) -> Resultaat:
    parken = lees_ibis(gemeenten)
    buurten = lees_buurten(jaar, gemeenten)
    adressen, objecten = lees_bag(gemeenten, buurten, jaar)
    x = koppel(parken, buurten, adressen, objecten)
    cbs = lees_cbs2019(set(parken.rin), set(buurten.gemeentenaam.astype(str).str.strip()))
    cbs_i = cbs.set_index("rin")
    b = buurten.set_index("codering")

    # Stap 3 en 4 per stuk, opgeteld per terrein
    for g in G_CODES:
        x[f"elec_{g}"] = x.codering.map(b[f"e_{g}"]) * x[f"a_{g}"]
        x[f"gas_{g}"] = x.codering.map(b[f"g_{g}"]) * x[f"a_{g}"]
    for k in KWB_KOL:
        x[f"kwb_{k}"] = x.codering.map(b[f"k_{k}"]) * x.a_bedrijf
    som = x.groupby("rin")[[f"elec_{g}" for g in G_CODES] + [f"gas_{g}" for g in G_CODES]
                           + [f"kwb_{k}" for k in KWB_KOL]].sum()
    in_park = gpd.sjoin(objecten, parken[["rin", "geometry"]], predicate="within").groupby("rin")[
        [f"m2_{f}" for f in FUNCTIES]].sum()

    # Verhoudingen op de getelde terreinen in de selectie (terugval) en de landelijke intensiteiten
    regio_l = {l: float(cbs[l].sum()) for l in LETTERS}
    regio_be, regio_f = sum(regio_l[l] for l in "BCDE"), regio_l["F"]
    inten, inten_l = intensiteit_groepen(), intensiteit_letters()

    def aandeel_bouw(n_be, n_f):
        """Stap 5: deel van de B-F-energie dat bij bouw (F) hoort, per drager (elektriciteit, gas)."""
        return [n_f * inten["f"][i] / (n_be * inten["be"][i] + n_f * inten["f"][i])
                if n_be * inten["be"][i] + n_f * inten["f"][i] > 0 else 0.0 for i in (0, 1)]

    r_be = regio_be / (regio_be + regio_f) if regio_be + regio_f > 0 else 0.5
    bouw_regio = aandeel_bouw(regio_be, regio_f)

    def per_letter(per_groep: dict, n_l: dict, i: int) -> dict:
        """Stap 5: verdeel de waarde van elke weergavegroep over zijn SBI-letters."""
        uit = {}
        for g in D_CODES:
            w = {l: n_l.get(l, 0) * inten_l[l][i] for l in D_LETTERS[g]}
            if sum(w.values()) <= 0:
                w = {l: regio_l[l] * inten_l[l][i] for l in D_LETTERS[g]}
            if sum(w.values()) <= 0:
                w = {l: 1.0 for l in D_LETTERS[g]}
            t = sum(w.values())
            for l in D_LETTERS[g]:
                uit[l] = per_groep[g] * w[l] / t
        return uit

    rijen = []
    for _, park in parken.iterrows():
        rin = int(park.rin)
        s = som.loc[rin] if rin in som.index else pd.Series(0.0, index=som.columns)
        c = cbs_i.loc[rin] if rin in cbs_i.index else None
        n_l = {l: (0.0 if pd.isna(c[l]) else float(c[l])) for l in LETTERS} if c is not None else {}

        # Stap 4: vestigingen 2023 per letter
        kwb = {k: float(s[f"kwb_{k}"]) for k in KWB_KOL}
        totaal, n19 = sum(kwb.values()), sum(n_l.values())
        if n19 > 0:
            vest, bron = {l: n_l[l] * totaal / n19 for l in LETTERS}, "cbs2019"
        else:
            vest, bron = {}, "kwb"
            for k, n in kwb.items():
                w = {l: regio_l[l] for l in KWB_LETTERS[k]}
                t = sum(w.values())
                for l in KWB_LETTERS[k]:
                    vest[l] = n * (w[l] / t if t > 0 else 1 / len(w))

        # Stap 5: B-F naar B-E en F, alles naar letters
        bouw = bouw_regio
        if c is not None:
            n_be, n_f = sum(n_l[l] for l in "BCDE"), n_l["F"]
            if n_be + n_f > 0:
                bouw = aandeel_bouw(n_be + BE_F_AFRONDING * r_be, n_f + BE_F_AFRONDING * (1 - r_be))
        elec = {g: float(s[f"elec_{g}"]) for g in G_CODES if g != "bf"}
        gas = {g: float(s[f"gas_{g}"]) for g in G_CODES if g != "bf"}
        elec["f"], elec["be"] = s.elec_bf * bouw[0], s.elec_bf * (1 - bouw[0])
        gas["f"], gas["be"] = s.gas_bf * bouw[1], s.gas_bf * (1 - bouw[1])
        elec_l, gas_l = per_letter(elec, vest, 0), per_letter(gas, vest, 1)

        # Stap 6: industriegraad
        e_tot = sum(elec.values()) + sum(gas.values()) * GAS_KWH_PER_M3
        ind = (elec["be"] + gas["be"] * GAS_KWH_PER_M3) / e_tot if e_tot > 0 else np.nan

        stuk = x[x.rin == rin].sort_values("pct_terrein", ascending=False)
        m2 = in_park.loc[rin] if rin in in_park.index else pd.Series(0.0, index=in_park.columns)
        rij = {"rin": rin, "naam": naam_nl(park.PLAN_NAAM), "gemeentecodes": park.GEM_CODES,
               "gemeenten": park.GEM_NAMEN, "ha_bruto": park.HA_BRUTO, "ha_netto": park.HA_NETTO,
               "max_milieu": park.MAX_MILIEU,
               "buurten": "; ".join(f"{r.codering} ({r.pct_terrein:.0%})" for r in stuk.itertuples()),
               **{f"m2_{f}": float(m2[f"m2_{f}"]) for f in FUNCTIES},
               "m2_bedrijf_totaal": float(m2.sum()),
               "vest_totaal": sum(vest.values()), "vest_bron": bron,
               **{f"vest_{l}": vest[l] for l in LETTERS},
               **{f"elec_kwh_{g}": elec[g] for g in D_CODES},
               "elec_kwh_totaal": sum(elec.values()),
               **{f"gas_m3_{g}": gas[g] for g in D_CODES},
               "gas_m3_totaal": sum(gas.values()),
               "industriegraad": ind,
               **{f"elec_kwh_sbi_{l}": elec_l[l] for l in LETTERS},
               **{f"gas_m3_sbi_{l}": gas_l[l] for l in LETTERS}}
        if c is not None:
            rij.update({"cbs2019_vest_totaal": c.totaal, "cbs2019_adressen": c.adressen,
                        "cbs2019_elec_mwh": c.elec_mwh, "cbs2019_gas_km3": c.gas_km3,
                        "cbs2019_gv_elec_mwh": c.gv_elec_mwh,
                        **{f"cbs2019_vest_{l}": c[l] for l in LETTERS}})
        rijen.append(rij)
    terreinen = pd.DataFrame(rijen).set_index("rin")
    return Resultaat(parken, buurten, x, cbs, terreinen)


# ---------------------------------------------------------------------------------------------
# Uitvoer
# ---------------------------------------------------------------------------------------------
def _schrijf(df: pd.DataFrame, naam: str) -> None:
    pad = PROCESSED_DIR / f"{naam}_{date.today().isoformat()}.csv"
    tmp = pad.with_suffix(".tmp.csv")
    df.to_csv(tmp, sep=OUTPUT_SEPARATOR, encoding="utf-8", float_format="%.6g")
    tmp.replace(pad)
    log.info("  Geschreven: %s (%d rijen)", pad.name, len(df))


def main(gemeenten: list[str] | None = None, jaren: list[int] | None = None) -> bool:
    t0 = time.monotonic()
    gemeenten = gemeenten or DEFAULT_GEMEENTEN
    jaren = [j for j in (jaren or CBS_YEARS) if (BUURTEN_DIR / f"kerncijfers_buurten_met_geometrie_{j}.csv").exists()]
    if not jaren:
        log.error("Geen buurtbestanden in %s — run eerst: python run_pipeline.py --buurten", BUURTEN_DIR)
        return False
    if not CBS2019.exists():
        log.error("CBS/TNO 2019 ontbreekt: %s", CBS2019)
        return False
    for jaar in jaren:
        r = bereken(gemeenten, jaar)
        t = r.terreinen
        log.info("  %d: %d terreinen; %.0f GWh elektriciteit, %.1f mln m³ gas, %.0f vestigingen", jaar, len(t),
                 t.elec_kwh_totaal.sum() / 1e6, t.gas_m3_totaal.sum() / 1e6, t.vest_totaal.sum())
        _schrijf(t, f"bedrijventerreinen_{jaar}")
        kol = ["rin", "codering", "pct_terrein", "pct_buurt", "a_bedrijf", "a_adres", "aandeel_m2"] \
            + [f"a_{g}" for g in G_CODES]
        _schrijf(r.stukken[kol].set_index(["rin", "codering"]), f"bedrijventerreinen_buurten_{jaar}")
    log.info("Bedrijventerreinen klaar in %.0fs", time.monotonic() - t0)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gemeenten", nargs="+", help="GM-codes (standaard de Drechtsteden)")
    ap.add_argument("--jaren", nargs="+", type=int, help="buurtjaren (standaard CBS_YEARS met buurtbestand)")
    a = ap.parse_args()
    sys.exit(0 if main(a.gemeenten, a.jaren) else 1)
