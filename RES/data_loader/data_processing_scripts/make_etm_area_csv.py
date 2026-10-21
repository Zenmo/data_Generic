"""ETM area-data (woningvoorraad + totalen) voor alle Nederlandse ETM-gebieden.

Het AnyLogic-model (J_ETMAreaData, zie java_files/PASTE_HousingStock_ETM_v2.md in het
Drechtsteden-project) leest twee bestanden met een vaste naam in data_Generic/RES/:

  etm_area_woningvoorraad.csv   per ETM-gebied × woningtype × bouwperiode:
                                etm_present_number, etm_typical_useful_demand_kWh_m2,
                                etm_present_share_in_useful_demand
  etm_area_totalen.csv          per ETM-gebied: inwoners, woningen, utiliteitsgebouwen,
                                m² per gebouw-equivalent, warmte-intensiteit utiliteit

Sleutels: etm_area (bv. GM0505_dordrecht, ES18_drechtsteden, PV28_zuid_holland, nl2023) en
gemeentecode (GM0505; voor niet-gemeenten gelijk aan etm_area). Het model zoekt eerst op het
ETM-gebied van het scenario, dan op de gemeentecode van de buurt, en valt anders terug op een
in te stellen regio.

Bron: publieke ETM-API, GET /api/v3/areas (lijst) en /api/v3/areas/{code}. Geen API-key.
Alleen bruikbare gebieden met analysis_year == --jaar (default 2023): gemeenten, RES-regio's,
provincies en Nederland zelf (~385 gebieden, ~2 minuten met 8 parallelle verzoeken).

Run standalone:  python make_etm_area_csv.py [--jaar 2023] [--gebieden GM0505_dordrecht ES18_drechtsteden ...]
Or via:          python run_pipeline.py --etm-area
"""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from config import DATA_GENERIC, OUTPUT_SEPARATOR, REQUEST_TIMEOUT

log = logging.getLogger(__name__)

ETM_AREAS_URL = "https://engine.energytransitionmodel.com/api/v3/areas"
ETM_AREA_URL = ETM_AREAS_URL + "/{code}"

# ETM-indeling (keys exact zoals in ETM / etsource)
TYPES = ["apartments", "terraced_houses", "semi_detached_houses", "detached_houses"]
PERIODS = ["before_1945", "1945_1964", "1965_1984", "1985_2004", "2005_present", "future"]

# Groepen in /api/v3/areas die bij Nederland horen (country alleen "nl<jaar>")
NL_GROEPEN = ("municipality", "res", "province")

# ETM area-totalen die het model nodig heeft als noemer voor groei/sloop: inwoners, woningen,
# utiliteitsgebouwen (in "residence equivalents" van area_per_building_residence_equivalent m²)
# en de utiliteits-warmte-intensiteit bestaand/nieuw.
_ETM_AREA_TOTALEN = [
    "analysis_year", "number_of_inhabitants", "present_number_of_residences",
    "present_number_of_buildings", "area_per_building_residence_equivalent",
    "typical_useful_demand_for_space_heating_buildings_present",
    "typical_useful_demand_for_space_heating_buildings_future",
]

WONINGVOORRAAD_CSV = DATA_GENERIC / "etm_area_woningvoorraad.csv"
TOTALEN_CSV = DATA_GENERIC / "etm_area_totalen.csv"


def lijst_etm_gebieden(jaar: int = 2023) -> list[str]:
    """Alle bruikbare Nederlandse ETM-gebieden met dit analysejaar."""
    r = requests.get(ETM_AREAS_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    codes = []
    for a in r.json():
        if not a.get("useable") or a.get("analysis_year") != jaar:
            continue
        code = str(a.get("area", ""))
        if a.get("group") in NL_GROEPEN or code == f"nl{jaar}":
            codes.append(code)
    return sorted(codes)


def gm_naar_etm_code(gm_codes: list[str], jaar: int = 2023) -> dict[str, str]:
    """GM-code → ETM area code (bv. GM0505 → GM0505_dordrecht)."""
    per_gm = {c[:6]: c for c in lijst_etm_gebieden(jaar) if c.startswith("GM")}
    ontbrekend = [g for g in gm_codes if g not in per_gm]
    if ontbrekend:
        log.warning("  Geen ETM-gebied voor %s", ", ".join(ontbrekend))
    return {g: per_gm[g] for g in gm_codes if g in per_gm}


def _haal_een(code: str) -> tuple[list[dict], dict | None]:
    try:
        r = requests.get(ETM_AREA_URL.format(code=code), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("  ETM area %s niet opgehaald: %s", code, exc)
        return [], None
    gm = code[:6] if code.startswith("GM") else code
    rijen = [{
        "etm_area": code, "gemeentecode": gm, "analysis_year": d.get("analysis_year"),
        "type": t, "periode": p,
        "etm_present_number": d.get(f"present_number_of_{t}_{p}"),
        "etm_typical_useful_demand_kWh_m2": d.get(f"typical_useful_demand_for_space_heating_{t}_{p}"),
        "etm_present_share_in_useful_demand": d.get(f"present_share_of_{t}_{p}_in_useful_demand_for_space_heating"),
    } for t in TYPES for p in PERIODS]
    totaal = {"etm_area": code, "gemeentecode": gm, **{k: d.get(k) for k in _ETM_AREA_TOTALEN}}
    return rijen, totaal


def haal_etm_area_codes(codes: list[str], workers: int = 8) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ETM area-data voor deze ETM-gebieden: (lang per type × periode, totalen per gebied)."""
    rijen, totalen = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r, t in pool.map(_haal_een, codes):
            rijen.extend(r)
            if t is not None:
                totalen.append(t)
    log.info("  ETM area-data opgehaald voor %d van %d gebieden", len(totalen), len(codes))
    return pd.DataFrame(rijen), pd.DataFrame(totalen)


def schrijf_vaste_bestanden(etm: pd.DataFrame, totalen: pd.DataFrame, samenvoegen: bool = True) -> None:
    """Schrijf naar de vaste namen in data_Generic/RES/. Met samenvoegen blijven gebieden die al in
    het bestand staan en hier niet zijn opgehaald behouden; opgehaalde gebieden worden vervangen."""
    for df, pad, sleutel in ((etm, WONINGVOORRAAD_CSV, ["etm_area", "type", "periode"]),
                             (totalen, TOTALEN_CSV, ["etm_area"])):
        if df.empty:
            continue
        if samenvoegen and pad.exists():
            oud = pd.read_csv(pad, sep=OUTPUT_SEPARATOR)
            oud = oud[~oud["etm_area"].isin(df["etm_area"])]
            df = pd.concat([oud, df], ignore_index=True)
        df = df.sort_values(sleutel, key=lambda s: s.map(_sorteer) if s.name in ("type", "periode") else s)
        tmp = pad.with_suffix(".tmp")
        df.to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False)
        tmp.replace(pad)
        log.info("Opgeslagen: %s (%d gebieden)", pad.name, df["etm_area"].nunique())


def _sorteer(v):
    for lijst in (TYPES, PERIODS):
        if v in lijst:
            return lijst.index(v)
    return v


def main(jaar: int = 2023, gebieden: list[str] | None = None) -> bool:
    t0 = time.monotonic()
    codes = gebieden or lijst_etm_gebieden(jaar)
    log.info("ETM area-data voor %d gebieden (analysejaar %d)", len(codes), jaar)
    etm, totalen = haal_etm_area_codes(codes)
    if totalen.empty:
        log.error("Niets opgehaald")
        return False
    schrijf_vaste_bestanden(etm, totalen, samenvoegen=gebieden is not None)
    log.info("ETM area-data klaar in %.0fs", time.monotonic() - t0)
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jaar", type=int, default=2023, help="ETM analysis_year van de gebieden")
    ap.add_argument("--gebieden", nargs="+", default=None,
                    help="alleen deze ETM area codes (samenvoegen met het bestaande bestand)")
    a = ap.parse_args()
    sys.exit(0 if main(a.jaar, a.gebieden) else 1)
