"""Bedrijfsruimte per buurt: m² gebruiksoppervlakte per BAG-gebruiksfunctie (alles behalve wonen).

Doel: een betere verdeelsleutel voor het bedrijfsverbruik per sector dan het aantal
vestigingen (make_buurten_csv.py, _verdeel_energieverbruik_sector). CBS KWB telt elke
vestiging even zwaar, ook een zzp'er op een woonadres; daardoor kregen woonbuurten te veel
en bedrijventerreinen te weinig bedrijfsverbruik. Vloeroppervlak van bedrijfspanden heeft
dat probleem niet: een zzp'er thuis zit in een woonfunctie en telt niet mee.

Getoetst voor de Drechtsteden (2023): verdeeld op vestigingen kwam 149 GWh elektriciteit op
de IBIS-bedrijventerreinen terecht, verdeeld op m² per functie 601 GWh; CBS mat in 2020-2021
530-561 GWh. Per terrein volgde de m²-verdeling de meting ook beter (correlatie 0,90 tegen 0,80).

Bron: BAG-verblijfsobjecten (PDOK WFS), dezelfde download en cache als
make_woningvoorraad_etm_csv.py (raw/bag/bag_verblijfsobject_<gemeenten>.gpkg).
  - alleen bestaande objecten (status in gebruik / niet ingemeten / verbouwing);
  - een object met meerdere functies verdeelt zijn oppervlakte gelijk over die functies;
    het woondeel valt weg (bv. winkel met bovenwoning: de helft telt als winkel);
  - 'overige gebruiksfunctie' (bergingen, garageboxen, schuurtjes) telt niet mee;
  - per buurtjaar alleen panden met dat bouwjaar of eerder: de BAG is de actuele stand, en
    bijvoorbeeld op Dordtse Kil IV staat ruim 20.000 m² die na 2023 is gebouwd.

Output (processed_data_from_loader/):
  bedrijfsruimte_bag_buurten_<datum>.csv  per jaar × buurt: m2_<functie>, m2_bedrijf_totaal,
      n_vbo_bedrijf. Wordt SAMENGEVOEGD met de vorige versie: buurten van gemeenten die nu niet
      zijn gedraaid blijven staan, zodat het bestand per run kan groeien.

Run standalone:  python make_bedrijfsruimte_csv.py [--gemeenten GM0505 GM0642 ...] [--jaren 2023 2024]
Or via:          python run_pipeline.py --bedrijfsruimte
"""

import argparse
import logging
import sys
import time
from datetime import date

import geopandas as gpd
import pandas as pd

from config import CBS_YEARS, CRS_RD, OUTPUT_SEPARATOR, PROCESSED_DIR, RAW_DIR
from make_woningvoorraad_etm_csv import BAG_WFS, DEFAULT_GEMEENTEN, download_laag

log = logging.getLogger(__name__)

_RAW_WB = RAW_DIR / "cbs_wijkenbuurten"
_UIT_PATROON = "bedrijfsruimte_bag_buurten_2*.csv"

# BAG-gebruiksfuncties die als bedrijfsruimte tellen ('overige gebruiksfunctie' bewust niet)
FUNCTIES = [
    "industriefunctie", "kantoorfunctie", "winkelfunctie", "logiesfunctie", "bijeenkomstfunctie",
    "onderwijsfunctie", "gezondheidszorgfunctie", "sportfunctie", "celfunctie",
]
_STATUS_BESTAAND = {
    "Verblijfsobject in gebruik",
    "Verblijfsobject in gebruik (niet ingemeten)",
    "Verbouwing verblijfsobject",
}
_MAX_M2 = 1_000_000  # registratiefouten afkappen


def m2_per_functie(vbo: gpd.GeoDataFrame) -> pd.DataFrame:
    """Per verblijfsobject de m² per bedrijfsfunctie (oppervlakte gelijk verdeeld over alle functies)."""
    vbo = vbo[vbo["status"].isin(_STATUS_BESTAAND)].copy()
    doelen = vbo["gebruiksdoel"].fillna("").astype(str).str.split(",").map(
        lambda d: [x.strip() for x in d if x.strip()])
    opp = pd.to_numeric(vbo["oppervlakte"], errors="coerce").fillna(0).clip(0, _MAX_M2)
    uit = pd.DataFrame(0.0, index=vbo.index, columns=[f"m2_{f}" for f in FUNCTIES])
    for i, (d, a) in enumerate(zip(doelen, opp)):
        if not d or a <= 0:
            continue
        deel = a / len(d)
        for f in d:
            if f in FUNCTIES:
                uit.iat[i, FUNCTIES.index(f)] += deel
    uit["m2_bedrijf_totaal"] = uit.sum(axis=1)
    uit["bouwjaar"] = pd.to_numeric(vbo["bouwjaar"], errors="coerce") if "bouwjaar" in vbo.columns else pd.NA
    uit = uit[uit["m2_bedrijf_totaal"] > 0]
    return gpd.GeoDataFrame(uit, geometry=vbo.loc[uit.index, "geometry"], crs=vbo.crs)


def per_buurt(objecten: gpd.GeoDataFrame, jaar: int, gemeenten: list[str]) -> pd.DataFrame | None:
    gpkg = _RAW_WB / str(jaar) / "buurten.gpkg"
    if not gpkg.exists():
        log.warning("  Buurtgrenzen %d ontbreken (%s); jaar overgeslagen", jaar, gpkg)
        return None
    buurten = gpd.read_file(gpkg, columns=["buurtcode", "gemeentecode"]).to_crs(CRS_RD)
    buurten = buurten[buurten["gemeentecode"].isin(gemeenten)]
    bestaand = objecten[objecten["bouwjaar"].isna() | (objecten["bouwjaar"] <= jaar)]
    j = gpd.sjoin(bestaand, buurten, predicate="within", how="inner")
    kol = [c for c in objecten.columns if c.startswith("m2_")]
    agg = j.groupby("buurtcode")[kol].sum().round(0)
    agg["n_vbo_bedrijf"] = j.groupby("buurtcode").size()
    # Buurten zonder bedrijfsruimte expliciet op 0, zodat 'gedekt' en 'niets gevonden' te onderscheiden zijn
    agg = agg.reindex(buurten["buurtcode"]).fillna(0)
    agg.index.name = "codering"
    agg = agg.reset_index()
    agg.insert(0, "gemeentecode", agg["codering"].map(buurten.set_index("buurtcode")["gemeentecode"]))
    agg.insert(0, "jaar", jaar)
    log.info("  %d: %d buurten, %.1f mln m² bedrijfsruimte", jaar, len(agg), agg["m2_bedrijf_totaal"].sum() / 1e6)
    return agg


def schrijf_samengevoegd(nieuw: pd.DataFrame) -> None:
    """Voeg samen met de vorige versie: rijen van (jaar, gemeente) uit deze run vervangen de oude."""
    vorige = sorted(PROCESSED_DIR.glob(_UIT_PATROON), reverse=True)
    if vorige:
        oud = pd.read_csv(vorige[0], sep=OUTPUT_SEPARATOR, dtype={"codering": str, "gemeentecode": str})
        sleutel_nieuw = set(zip(nieuw["jaar"], nieuw["gemeentecode"]))
        oud = oud[[(j, g) not in sleutel_nieuw for j, g in zip(oud["jaar"], oud["gemeentecode"])]]
        nieuw = pd.concat([oud, nieuw], ignore_index=True)
    pad = PROCESSED_DIR / f"bedrijfsruimte_bag_buurten_{date.today().isoformat()}.csv"
    tmp = pad.with_suffix(".tmp.csv")
    nieuw.sort_values(["jaar", "codering"]).to_csv(tmp, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
    tmp.replace(pad)
    log.info("  Geschreven: %s (%d rijen, %d gemeenten)", pad.name, len(nieuw), nieuw["gemeentecode"].nunique())


def main(gemeenten: list[str] | None = None, jaren: list[int] | None = None) -> bool:
    t0 = time.monotonic()
    gemeenten = gemeenten or DEFAULT_GEMEENTEN
    jaren = jaren or list(CBS_YEARS)
    gm_key = "_".join(sorted(gemeenten))

    # Gebied uit de meest recente buurtgrenzen (de BAG-cache is gedeeld met --woningvoorraad)
    gpkgs = [p for p in (_RAW_WB / str(j) / "buurten.gpkg" for j in sorted(jaren, reverse=True)) if p.exists()]
    if not gpkgs:
        log.error("Geen buurtgrenzen gevonden in %s — run eerst: python run_pipeline.py --download", _RAW_WB)
        return False
    buurten = gpd.read_file(gpkgs[0], columns=["buurtcode", "gemeentecode"]).to_crs(CRS_RD)
    buurten = buurten[buurten["gemeentecode"].isin(gemeenten)]
    if buurten.empty:
        log.error("Geen buurten voor %s", gemeenten)
        return False
    gebied = buurten.union_all().buffer(50)

    try:
        vbo = download_laag("bag_verblijfsobject", BAG_WFS, "bag:verblijfsobject", gebied, gm_key)
    except RuntimeError as exc:
        log.error("BAG-download mislukt: %s", exc)
        return False
    objecten = m2_per_functie(vbo.to_crs(CRS_RD))
    log.info("Bedrijfsruimte: %d verblijfsobjecten met een bedrijfsfunctie", len(objecten))

    frames = [f for f in (per_buurt(objecten, j, gemeenten) for j in jaren) if f is not None]
    if not frames:
        return False
    schrijf_samengevoegd(pd.concat(frames, ignore_index=True))
    log.info("Bedrijfsruimte klaar in %.0fs", time.monotonic() - t0)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gemeenten", nargs="+", help="GM-codes (standaard de Drechtsteden)")
    ap.add_argument("--jaren", nargs="+", type=int, help="buurtjaren (standaard CBS_YEARS)")
    a = ap.parse_args()
    sys.exit(0 if main(a.gemeenten, a.jaren) else 1)
