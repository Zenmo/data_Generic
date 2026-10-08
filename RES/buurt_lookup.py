"""
Lookup average electricity / gas / car ownership per dwelling for a Dutch buurt.

Source: kerncijfers_buurten_<year>_met_geometrie.csv (CBS Kerncijfers wijken en buurten).

Usage (CLI):
    python buurt_lookup.py BU16800000
    python buurt_lookup.py "Annen"
    python buurt_lookup.py "Annen" --gemeente "Aa en Hunze"
    python buurt_lookup.py "Centrum" --gemeente Dordrecht --year 2024

Usage (import):
    from buurt_lookup import lookup
    res = lookup("BU16800000")
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DATA_DIR = Path(__file__).resolve().parent
FILE_TEMPLATE = "kerncijfers_buurten_{year}_met_geometrie.csv"
DEFAULT_YEAR = 2023

# CBS/SBI sector codes used in the elec_verbruik_<sector>_kwh / gas_verbruik_<sector>_m3 columns
SECTORS = {
    "a": "Landbouw, bosbouw en visserij",
    "bf": "Nijverheid en energie",
    "gi": "Handel en horeca",
    "hj": "Vervoer, informatie en communicatie",
    "kl": "Financiele diensten, onroerend goed",
    "mn": "Zakelijke dienstverlening",
    "oq": "Overheid, onderwijs en zorg",
    "ru": "Cultuur, recreatie, overige diensten",
}

COLS = [
    "wijken_en_buurten",
    "gemeentenaam",
    "soort_regio",
    "codering",
    "aantal_inwoners",
    "huishoudens_totaal",
    "woningvoorraad_woningvoorraad",
    "gemiddelde_elektriciteitslevering",
    "gemiddeld_aardgasverbruik",
    "personenautos_totaal",
    "bestelautos_totaal",
    "vrachtautos_totaal",
    "bedrijfsvestigingen_totaal",
    "data_from_mun_average",
    "energieverbruik_sector_gelijk_verdeeld",
]
COLS += [f"elec_verbruik_{s}_kwh" for s in SECTORS]
COLS += [f"gas_verbruik_{s}_m3" for s in SECTORS]


def _num(value):
    """Parse a CBS numeric cell; return None when missing/suppressed."""
    if value is None:
        return None
    value = value.strip().replace(",", ".")
    if value in ("", ".", "-", "nan", "NaN", "None"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _data_path(year: int) -> Path:
    path = DATA_DIR / FILE_TEMPLATE.format(year=year)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")
    return path


def _iter_rows(year: int):
    with open(_data_path(year), encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            yield row


def find_rows(query: str, gemeente: str | None = None, year: int = DEFAULT_YEAR,
              soort_regio: str = "Buurt") -> list[dict]:
    """Match on codering (buurtcode) or on buurtnaam, optionally within a gemeente."""
    q = query.strip().lower()
    g = gemeente.strip().lower() if gemeente else None
    hits = []
    for row in _iter_rows(year):
        if soort_regio and (row.get("soort_regio") or "").strip().lower() != soort_regio.lower():
            continue
        code = (row.get("codering") or "").strip().lower()
        name = (row.get("wijken_en_buurten") or "").strip().lower()
        if q not in (code, name):
            continue
        if g and (row.get("gemeentenaam") or "").strip().lower() != g:
            continue
        hits.append({c: row.get(c) for c in COLS})
    return hits


def summarize(row: dict) -> dict:
    """Turn a raw CSV row into the requested per-dwelling indicators."""
    dwellings = _num(row["woningvoorraad_woningvoorraad"])
    households = _num(row["huishoudens_totaal"])
    cars = _num(row["personenautos_totaal"])
    vans = _num(row["bestelautos_totaal"])
    trucks = _num(row["vrachtautos_totaal"])

    fleet = [v for v in (cars, vans, trucks) if v is not None]
    vehicles = sum(fleet) if fleet else None

    # Business/company energy use, estimated per SBI sector group
    elec_sectors = {s: _num(row.get(f"elec_verbruik_{s}_kwh")) for s in SECTORS}
    gas_sectors = {s: _num(row.get(f"gas_verbruik_{s}_m3")) for s in SECTORS}
    elec_vals = [v for v in elec_sectors.values() if v is not None]
    gas_vals = [v for v in gas_sectors.values() if v is not None]

    return {
        "buurtnaam": row["wijken_en_buurten"],
        "codering": row["codering"],
        "gemeente": row["gemeentenaam"],
        "inwoners": _num(row["aantal_inwoners"]),
        "woningvoorraad": dwellings,
        "huishoudens": households,
        # CBS already reports these as averages per dwelling
        "elektriciteit_kwh_per_woning": _num(row["gemiddelde_elektriciteitslevering"]),
        "gas_m3_per_woning": _num(row["gemiddeld_aardgasverbruik"]),
        "personenautos_totaal": cars,
        "bestelautos_totaal": vans,
        "vrachtautos_totaal": trucks,
        "voertuigen_totaal": vehicles,
        "autos_per_woning": round(cars / dwellings, 3) if cars is not None and dwellings else None,
        "autos_per_huishouden": round(cars / households, 3) if cars is not None and households else None,
        "voertuigen_per_woning": round(vehicles / dwellings, 3) if vehicles is not None and dwellings else None,
        # Bedrijven / companies
        "bedrijfsvestigingen_totaal": _num(row["bedrijfsvestigingen_totaal"]),
        "bedrijven_elektriciteit_kwh_totaal": round(sum(elec_vals)) if elec_vals else None,
        "bedrijven_gas_m3_totaal": round(sum(gas_vals)) if gas_vals else None,
        "bedrijven_elektriciteit_kwh_per_sector": elec_sectors,
        "bedrijven_gas_m3_per_sector": gas_sectors,
        "bedrijven_data_from_mun_average": row.get("data_from_mun_average"),
        "bedrijven_sector_gelijk_verdeeld": row.get("energieverbruik_sector_gelijk_verdeeld"),
    }


def lookup(query: str, gemeente: str | None = None, year: int = DEFAULT_YEAR) -> list[dict]:
    return [summarize(r) for r in find_rows(query, gemeente, year)]


def _fmt(v, unit=""):
    if v is None:
        return "n/a"
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        return f"{int(v):,}" + unit
    return f"{v}{unit}"


def _print(res: dict, year: int, sectors: bool = False) -> None:
    print(f"\n{res['buurtnaam']}  ({res['codering']}, gemeente {res['gemeente']}, {year})")
    print("-" * 60)
    print(f"  Inwoners                      : {_fmt(res['inwoners'])}")
    print(f"  Woningvoorraad                : {_fmt(res['woningvoorraad'])}")
    print(f"  Huishoudens                   : {_fmt(res['huishoudens'])}")
    print(f"  Gem. elektriciteit per woning : {_fmt(res['elektriciteit_kwh_per_woning'], ' kWh')}")
    print(f"  Gem. aardgas per woning       : {_fmt(res['gas_m3_per_woning'], ' m3')}")
    print(f"  Personenauto's totaal         : {_fmt(res['personenautos_totaal'])}")
    print(f"  Bestelauto's totaal           : {_fmt(res['bestelautos_totaal'])}")
    print(f"  Vrachtauto's totaal           : {_fmt(res['vrachtautos_totaal'])}")
    print(f"  Voertuigen totaal (p+b+v)     : {_fmt(res['voertuigen_totaal'])}")
    print(f"  Auto's per woning             : {_fmt(res['autos_per_woning'])}")
    print(f"  Auto's per huishouden         : {_fmt(res['autos_per_huishouden'])}")
    print(f"  Voertuigen per woning         : {_fmt(res['voertuigen_per_woning'])}")

    print("  " + "-" * 56)
    print(f"  Bedrijfsvestigingen           : {_fmt(res['bedrijfsvestigingen_totaal'])}")
    print(f"  Bedrijven elektriciteit totaal: {_fmt(res['bedrijven_elektriciteit_kwh_totaal'], ' kWh')}")
    print(f"  Bedrijven aardgas totaal      : {_fmt(res['bedrijven_gas_m3_totaal'], ' m3')}")

    if sectors:
        print("\n  Per sector:")
        print(f"    {'sector':<38}{'elektra (kWh)':>16}{'gas (m3)':>14}")
        elec = res["bedrijven_elektriciteit_kwh_per_sector"]
        gas = res["bedrijven_gas_m3_per_sector"]
        for code, label in SECTORS.items():
            e = elec.get(code)
            g = gas.get(code)
            e_s = f"{round(e):,}" if e is not None else "n/a"
            g_s = f"{round(g):,}" if g is not None else "n/a"
            print(f"    {label:<38}{e_s:>16}{g_s:>14}")

    flags = []
    if str(res.get("bedrijven_data_from_mun_average")).lower() == "true":
        flags.append("afgeleid van gemeentegemiddelde")
    if str(res.get("bedrijven_sector_gelijk_verdeeld")).lower() == "true":
        flags.append("gelijk verdeeld over sectoren")
    if flags:
        print(f"  [!] Bedrijfsverbruik is een schatting: {'; '.join(flags)}.")


def main() -> int:
    p = argparse.ArgumentParser(description="Buurt kerncijfers lookup (elektriciteit, gas, auto's).")
    p.add_argument("query", help="Buurtcode/codering (e.g. BU16800000) or buurtnaam (e.g. Annen)")
    p.add_argument("--gemeente", "-g", default=None, help="Disambiguate a buurtnaam by gemeente")
    p.add_argument("--year", "-y", type=int, default=DEFAULT_YEAR, choices=[2023, 2024])
    p.add_argument("--sectors", "-s", action="store_true",
                   help="Show the company energy use broken down per sector")
    args = p.parse_args()

    results = lookup(args.query, args.gemeente, args.year)

    if not results:
        print(f"No buurt found for '{args.query}'"
              + (f" in gemeente '{args.gemeente}'" if args.gemeente else "")
              + f" ({args.year}).")
        return 1

    for res in results:
        _print(res, args.year, sectors=args.sectors)

    if len(results) > 1:
        print(f"\n{len(results)} matches — use --gemeente to narrow down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
