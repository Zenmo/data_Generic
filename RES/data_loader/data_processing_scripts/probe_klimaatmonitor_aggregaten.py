"""Find the Klimaatmonitor variables behind the "Thema's" control totals.

Goal
----
make_energieverbruik_sector_csv.py can reconcile a RES region's group totals
against the RVO-corrected totals from a "Thema's - <regio>.csv" export. That
works, but only for regions whose export has been downloaded by hand.

The Thema's columns are themselves ODS variables, so if we know their variable
codes we can fetch them at GeoLevels('res') — one request per variable, all 31
regions at once — and drop the manual exports entirely.

This script finds those codes by searching the variable catalogue on **name**,
not on code prefix. The per-SBI variables are vbrze_*/vbrzg_*, but an aggregate
is not necessarily named that way, which is why an earlier code-prefix-only
version of this probe found nothing.

What it does
------------
  1. Walks the full (paged) Variables catalogue and writes it to CSV.
  2. Scores every variable name against the Thema's column headings and prints
     the best candidates per carrier.
  3. For each candidate, fetches it at RES level and reports: how many of the
     regions have a value, the national sum, and — if a Thema's export is
     present — whether the candidate reproduces that export's number exactly.
     An exact match on a known region is the proof that the code is the right
     one; everything else is circumstantial.

Run:
    python probe_klimaatmonitor_aggregaten.py [jaar]
    python probe_klimaatmonitor_aggregaten.py 2023

Read-only. Writes two CSVs for inspection, changes nothing in the pipeline.
"""

import logging
import re
import sys
from pathlib import Path

import pandas as pd
import requests

from config import REQUEST_TIMEOUT
from make_energieverbruik_sector_csv import (
    _KM_BASE,
    _KM_ELEC_VARS,
    _KM_GAS_VARS,
    _THEMA_KOLOM,
    _laad_klimaatmonitor_apikey,
    _laad_thema_totalen,
    _km_geoitems,
    _normaliseer_gebiedsnaam,
)

log = logging.getLogger(__name__)
_CATALOGUS = Path(__file__).parent / "klimaatmonitor_variabelen.csv"
_KANDIDATEN = Path(__file__).parent / "klimaatmonitor_kandidaat_totalen.csv"

_GEBRUIKT = set([c for cs in _KM_ELEC_VARS.values() for c in cs]
                + [c for cs in _KM_GAS_VARS.values() for c in cs])

# Words that must be present for a variable to be a plausible business total,
# and words that rule it out (households, dwellings, per-capita figures).
_MOET = {
    "elec": ["elektriciteit"],
    "gas": ["aardgas"],
}
_BEDRIJF_WOORDEN = ["bedrijf", "bedrijven", "instelling", "instellingen"]
_UITSLUITEN = ["woning", "huishouden", "per inwoner", "per woning", "gemiddeld"]


def _get(headers: dict, url: str):
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code in (401, 404):
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("  mislukt: %s (%s)", url, exc)
        return None


def catalogus(headers: dict) -> pd.DataFrame:
    """The complete variable catalogue, following @odata.nextLink to the end."""
    rijen, url = [], f"{_KM_BASE}/Variables?$select=ExternalCode,Name"
    while url:
        data = _get(headers, url)
        if not data:
            break
        for item in data.get("value", []):
            rijen.append({"var_code": str(item.get("ExternalCode") or ""),
                          "naam": str(item.get("Name") or "")})
        url = data.get("@odata.nextLink")

    df = pd.DataFrame(rijen)
    if df.empty:
        log.error("Geen variabelen opgehaald — heeft de API-key toegang tot /Variables?")
        return df
    df["in_pipeline"] = df["var_code"].isin(_GEBRUIKT)
    df.to_csv(_CATALOGUS, index=False, sep=";")
    log.info("Catalogus: %d variabelen -> %s", len(df), _CATALOGUS.name)
    return df


def _score(naam: str, carrier: str) -> int:
    """Crude relevance score of a variable name against the Thema's heading."""
    n = naam.lower()
    if any(u in n for u in _UITSLUITEN):
        return 0
    if not all(m in n for m in _MOET[carrier]):
        return 0
    score = 1
    if any(b in n for b in _BEDRIJF_WOORDEN):
        score += 3
    if "totaal" in n:
        score += 2
    if "openbaar net" in n or "geleverd" in n:
        score += 2
    if carrier == "gas" and "sbi d" in n:
        score += 2
    # Prefer names close to the exact Thema's heading.
    doel = _THEMA_KOLOM[carrier].lower()
    gedeeld = len(set(re.findall(r"[a-z]{4,}", n)) & set(re.findall(r"[a-z]{4,}", doel)))
    return score + gedeeld


def kandidaten(cat: pd.DataFrame) -> pd.DataFrame:
    rijen = []
    for carrier in ("elec", "gas"):
        deel = cat[~cat["in_pipeline"]].copy()
        deel["score"] = deel["naam"].map(lambda n: _score(n, carrier))
        deel = deel[deel["score"] > 0].nlargest(8, "score")
        deel["carrier"] = carrier
        rijen.append(deel)
    return pd.concat(rijen, ignore_index=True) if rijen else pd.DataFrame()


def op_res_niveau(headers: dict, var_code: str, jaar: int) -> pd.Series:
    """{genormaliseerde regionaam -> waarde} for one variable at RES level."""
    url = (f"{_KM_BASE}/Variables('{var_code}')/GeoLevels('res')"
           f"/PeriodLevels('year')/Periods('{jaar}')/Values")
    data = _get(headers, url)
    if not data:
        return pd.Series(dtype=float)
    namen = _km_geoitems(headers, "res")
    uit = {}
    for rij in data.get("value", []):
        code = str(rij.get("ExternalCode", ""))
        suffix = code.split("_", 1)[1] if "_" in code else code
        uit[namen.get(suffix, _normaliseer_gebiedsnaam(suffix))] = pd.to_numeric(
            rij.get("ValueString"), errors="coerce")
    return pd.Series(uit, dtype=float)


def main(jaar: int) -> int:
    apikey = _laad_klimaatmonitor_apikey()
    if apikey is None:
        return 1
    headers = {"apikey": apikey}

    log.info("=== 1. Variabelencatalogus ===")
    cat = catalogus(headers)
    if cat.empty:
        return 1

    log.info("\n=== 2. Kandidaat-totaalvariabelen (op naam gezocht) ===")
    kand = kandidaten(cat)
    if kand.empty:
        log.error("Geen kandidaten gevonden. Bekijk %s handmatig.", _CATALOGUS.name)
        return 1
    for carrier in ("elec", "gas"):
        log.info("  %s — gezocht op: %s", carrier, _THEMA_KOLOM[carrier])
        for _, r in kand[kand["carrier"] == carrier].iterrows():
            log.info("     %-22s score %2d  %s", r["var_code"], r["score"], r["naam"][:80])

    # A downloaded export is the ground truth we validate candidates against.
    thema = _laad_thema_totalen()
    thema_jaar = thema[thema["jaar"] == jaar] if not thema.empty else pd.DataFrame()

    log.info("\n=== 3. Kandidaten getoetst op RES-niveau (%d) ===", jaar)
    resultaten = []
    for _, r in kand.iterrows():
        reeks = op_res_niveau(headers, r["var_code"], jaar)
        if reeks.empty:
            continue
        n_gevuld = int(reeks.notna().sum())
        rij = {"var_code": r["var_code"], "carrier": r["carrier"], "naam": r["naam"],
               "n_regios": len(reeks), "n_met_waarde": n_gevuld,
               "som": reeks.sum(min_count=1), "klopt_met_export": ""}

        for _, t in thema_jaar[thema_jaar["carrier"] == r["carrier"]].iterrows():
            waarde = reeks.get(t["regio_sleutel"])
            if pd.notna(waarde):
                afwijking = abs(waarde - t["totaal"]) / max(t["totaal"], 1)
                rij["klopt_met_export"] = (
                    f"{t['regio']}: {waarde:,.0f} vs {t['totaal']:,.0f} "
                    f"({100 * afwijking:.2f}% afwijking)"
                )
                rij["_afwijking"] = afwijking
        resultaten.append(rij)
        log.info("  %-22s %2d/%d regio's gevuld, som %.4g   %s",
                 r["var_code"], n_gevuld, len(reeks), rij["som"],
                 rij["klopt_met_export"])

    if not resultaten:
        log.error("Geen enkele kandidaat leverde RES-waarden op.")
        return 1
    uit = pd.DataFrame(resultaten)
    uit.drop(columns=[c for c in ["_afwijking"] if c in uit.columns]).to_csv(
        _KANDIDATEN, index=False, sep=";")

    print("\n" + "=" * 78)
    print("CONCLUSIE")
    print("=" * 78)
    treffers = uit[uit.get("_afwijking", pd.Series(dtype=float)) < 0.005] \
        if "_afwijking" in uit.columns else pd.DataFrame()
    if not treffers.empty:
        for _, r in treffers.iterrows():
            print(f"  {r['carrier']:5s} -> {r['var_code']}  ({r['n_met_waarde']}/{r['n_regios']} "
                  f"regio's gevuld)")
            print(f"          {r['naam']}")
        print("\nDeze codes reproduceren de gedownloade Thema's-export exact. Zet ze in")
        print("_THEMA_API_VARS in make_energieverbruik_sector_csv.py; dan gelden de")
        print("controletotalen voor ALLE RES-regio's en zijn handmatige exports overbodig.")
    elif thema_jaar.empty:
        print("  Geen Thema's-export aanwezig om tegen te toetsen. De kandidaten hierboven")
        print("  zijn op naam gevonden — download één export om te bevestigen welke klopt.")
    else:
        print("  Geen enkele kandidaat reproduceert de export. Waarschijnlijk publiceert")
        print("  de ODS de RVO-bijschatting niet als variabele, en blijven handmatige")
        print("  Thema's-exports per regio de enige route. Bekijk %s handmatig."
              % _CATALOGUS.name)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2023))
