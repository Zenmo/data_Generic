"""Gasgebruik van grote uitstoters per inrichting, geschat uit hun CO2-emissie.

Doel: in make_buurten_csv.py (_verdeel_energieverbruik_sector) eerst het gas van de grote
bedrijven op hun eigen locatie zetten en pas daarna de rest van het gemeentelijk gas per
sector over de buurten verdelen. Zonder deze stap wordt het gas van één fabriek over alle
bedrijfspanden van de gemeente uitgesmeerd.

Bron: Emissieregistratie, CO2 per inrichting (kton per jaar, 1990-2024), zoals al opgehaald
door de WarmteAtlas-fase (processed_data_from_loader/CO2EmissieBedrijven_nl_<datum>.csv,
laag WarmteAtlas:CO2EmissieBedrijven). Bevat SBI-code, adres en buurtcode.

Omrekening naar aardgas: aangenomen is dat de CO2 uit aardgasverbranding komt.
  56,6 kg CO2 per GJ (Nederlandse standaardemissiefactor aardgas)
  × 31,65 MJ per m³ (onderwaarde, Groningen-equivalent, zoals het model)
  = 1,79 kg CO2 per m³ aardgas.
Uitgesloten, omdat die CO2 niet (vooral) uit aardgas komt of Klimaatmonitor er geen gas voor kent:
  SBI 35 energievoorziening, 37 rioolwaterzuivering, 38 afvalverwerking (afvalverbranding).
Kanttekeningen:
  - Procesemissies en andere brandstoffen (chemie, staal) worden zo ook als gas geteld: een
    bovengrens. make_buurten_csv begrenst daarom de som per gemeente en sector.
  - Alleen gas: voor elektriciteit van grote verbruikers bestaat geen open bron per bedrijf.
  - De ETS-lijst (NEa) bevat dezelfde grote bedrijven en wordt niet opgeteld (dubbeltelling);
    grote stookinstallaties (met echt brandstofgebruik in TJ) dekken maar een paar honderd
    installaties in NL.

Locatie: buurtcode uit de bron (op basis van de coördinaat). Als de BAG-cache voor de gemeente
er is (zie --bedrijfsruimte / --woningvoorraad), heeft het BAG-adres (postcode + huisnummer)
voorrang; dat corrigeert coördinaten die net in een buurbuurt vallen.

Output (processed_data_from_loader/):
  grootverbruikers_gas_<datum>.csv  per inrichting: naam, sbi_code, groep, gemeentecode,
      buurtcode_<jaar>, locatiebron, co2_ton_<jaar>, gas_m3_<jaar> (jaar 2020-2024).

Run standalone:  python make_grootverbruikers_gas_csv.py
Or via:          python run_pipeline.py --grootverbruikers
"""

import logging
import sys
from datetime import date

import geopandas as gpd
import pandas as pd

from config import CRS_RD, OUTPUT_SEPARATOR, PROCESSED_DIR, RAW_DIR

log = logging.getLogger(__name__)

KG_CO2_PER_M3 = 56.6 * 31.65 / 1000          # 1,79 kg CO2 per m³ aardgas
JAREN = list(range(2020, 2025))   # de bron heeft 1990, 2000, 2010, 2015 en 2020-2024
SBI_UITGESLOTEN = {35, 37, 38}
_RAW_BAG = RAW_DIR / "bag"
_RAW_WB = RAW_DIR / "cbs_wijkenbuurten"


def sbi_groep(sbi2: int) -> str:
    """SBI-afdeling (2 cijfers) naar de sectorgroep van make_energieverbruik_sector_csv.py."""
    if sbi2 <= 3:
        return "a"
    if sbi2 <= 43:
        return "bf"
    if sbi2 in (45, 46, 47, 55, 56):
        return "gi"
    if 49 <= sbi2 <= 53:
        return "h"
    if 58 <= sbi2 <= 63:
        return "j"
    if 64 <= sbi2 <= 68:
        return "kl"
    if 69 <= sbi2 <= 82:
        return "mn"
    if 84 <= sbi2 <= 88:
        return "oq"
    return "ru"


def _bag_adressen() -> gpd.GeoDataFrame | None:
    """Alle verblijfsobjecten uit de BAG-caches (postcode, huisnummer, punt)."""
    frames = []
    for gpkg in sorted(_RAW_BAG.glob("bag_verblijfsobject_*.gpkg")):
        try:
            frames.append(gpd.read_file(gpkg, columns=["postcode", "huisnummer"]).to_crs(CRS_RD))
        except Exception as exc:  # noqa: BLE001 - een kapotte cache mag de rest niet tegenhouden
            log.warning("  BAG-cache %s niet leesbaar: %s", gpkg.name, exc)
    if not frames:
        return None
    bag = pd.concat(frames, ignore_index=True)
    bag["sleutel"] = bag["postcode"].astype(str).str.replace(" ", "").str.upper() + "_" + \
        pd.to_numeric(bag["huisnummer"], errors="coerce").astype("Int64").astype(str)
    return gpd.GeoDataFrame(bag.drop_duplicates("sleutel"), geometry="geometry", crs=CRS_RD)


def _buurt_via_bag(df: pd.DataFrame, bag: gpd.GeoDataFrame, jaar: int) -> pd.Series:
    gpkg = _RAW_WB / str(jaar) / "buurten.gpkg"
    if bag is None or not gpkg.exists():
        return pd.Series(pd.NA, index=df.index, dtype="object")
    sleutel = df["POSTCODE"].astype(str).str.replace(" ", "").str.upper() + "_" + \
        pd.to_numeric(df["NR"], errors="coerce").astype("Int64").astype(str)
    punten = bag.set_index("sleutel").geometry.reindex(sleutel.values)
    punten = gpd.GeoDataFrame({"i": df.index}, geometry=punten.values, crs=CRS_RD).dropna(subset=["geometry"])
    if punten.empty:
        return pd.Series(pd.NA, index=df.index, dtype="object")
    buurten = gpd.read_file(gpkg, columns=["buurtcode"]).to_crs(CRS_RD)
    j = gpd.sjoin(punten, buurten, predicate="within").drop_duplicates("i").set_index("i")
    return j["buurtcode"].reindex(df.index)


def main() -> bool:
    bronnen = sorted(PROCESSED_DIR.glob("CO2EmissieBedrijven_nl_2*.csv"), reverse=True)
    if not bronnen:
        log.error("CO2EmissieBedrijven_nl_<datum>.csv niet gevonden — run eerst: python run_pipeline.py --process")
        return False
    e = pd.read_csv(bronnen[0], sep=OUTPUT_SEPARATOR, low_memory=False)
    e = e[e["EMISSIE"].astype(str).str.upper().eq("CO2")].copy()
    e["sbi2"] = pd.to_numeric(e["SBI_CODE"].astype(str).str[:2], errors="coerce")
    n_voor = len(e)
    e = e[e["sbi2"].notna() & ~e["sbi2"].isin(SBI_UITGESLOTEN)].copy()
    log.info("Emissieregistratie: %d inrichtingen, %d na uitsluiten SBI 35/37/38 (%s)",
             n_voor, len(e), bronnen[0].name)

    uit = pd.DataFrame({
        "naam": e["INRICHTING"], "sbi_code": e["SBI_CODE"].astype(str), "sector": e["SECTOR"],
        "groep": e["sbi2"].astype(int).map(sbi_groep), "gemeentecode": e["gemeentecode_2023"],
        "adres": e["STRAATNAAM"].astype(str) + " " + e["NR"].astype(str).str.replace(r"\.0$", "", regex=True)
                 + ", " + e["POSTCODE"].astype(str),
    })
    bag = _bag_adressen()
    for jaar in (2023, 2024):
        bron_kol = f"buurtcode_{jaar}"
        via_bag = _buurt_via_bag(e, bag, jaar)
        uit[bron_kol] = via_bag.fillna(e[bron_kol])
        if jaar == 2023:
            uit["locatiebron"] = via_bag.notna().map({True: "bag_adres", False: "coordinaat_bron"})
            verschil = via_bag.notna() & (via_bag != e[bron_kol])
            for i in e.index[verschil]:
                log.info("  Buurt via BAG-adres: %s %s -> %s (bron: %s)", e.at[i, "INRICHTING"],
                         uit.at[i, "adres"], via_bag[i], e.at[i, bron_kol])
    for jaar in JAREN:
        kol = f"kton{jaar}"
        if kol not in e.columns:
            log.warning("  Kolom %s ontbreekt in de bron; jaar overgeslagen", kol)
            continue
        co2 = pd.to_numeric(e[kol], errors="coerce").fillna(0) * 1e6  # kg
        uit[f"co2_ton_{jaar}"] = (co2 / 1000).round(1)
        uit[f"gas_m3_{jaar}"] = (co2 / KG_CO2_PER_M3).round(0)
    uit = uit[uit[[c for c in uit.columns if c.startswith("gas_m3_")]].sum(axis=1) > 0]

    pad = PROCESSED_DIR / f"grootverbruikers_gas_{date.today().isoformat()}.csv"
    tmp = pad.with_suffix(".tmp.csv")
    uit.to_csv(tmp, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
    tmp.replace(pad)
    log.info("Geschreven: %s (%d inrichtingen, %.0f mln m³ gas in 2023)", pad.name, len(uit),
             uit["gas_m3_2023"].sum() / 1e6)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", stream=sys.stdout)
    sys.exit(0 if main() else 1)
