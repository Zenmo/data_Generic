"""Diagnose Klimaatmonitor suppression: which sector values are missing, at which geo level.

Standalone diagnostic for the demand gap documented in
`RES/klimaatmonitor_sector_demand_gap.md`. Answers three questions the
gemeenten CSV alone cannot answer:

  1. For which of the ~33 individual SBI-letter variables (vbrze_* / vbrzg_*)
     is the **municipal** value missing or zero, and how often?
  2. When municipal is missing, is the **regional** (RES-regio) value present —
     or is it missing too? If regional is often missing as well, the fallback
     has to escalate to a coarser SBI aggregation or to provincie / nederland.
  3. How large is the **residual** — the difference between a region's own
     reported total and the sum of its municipalities? This is the part that
     the current pipeline silently loses, and the part the fair-share fallback
     in make_energieverbruik_sector_csv.py redistributes.

Why letter level and not the 8-group level: the pipeline sums ~33 per-letter
variables into 8 groups. A suppressed letter contributes 0 to that sum, so the
group total still looks populated while being understated. Measured on
`kerncijfers_gemeenten_2023.csv`, only 0.3–10% of *group*-level municipal cells
are exactly 0, and no RES region has all its municipalities at 0 for any group —
far too little to explain the ~25% electricity / ~17% gas gap. The loss is
therefore below the group level, which is where this script looks.

Requires a Klimaatmonitor API key in secrets.local.json (gitignored):
    {"klimaatmonitor_apikey": "<your key>"}

Run:
    python diagnose_sector_geolevels.py [2023 [2024]]

Writes three CSVs next to this script (prefix `diagnose_sector_`):
    ..._geolevels.csv     available GeoLevels overall and per variable
    ..._missing.csv       per variable x year x level: n areas, n zero, n missing
    ..._residual.csv      per variable x year x RES-regio: regional total vs
                          sum of municipalities, and the residual
and prints a summary with an explicit recommendation on which escalation rung
(regio / provincie / nederland, or coarser SBI grouping) the data supports.

Read-only: touches no pipeline output, changes nothing downstream.
"""

import json
import logging
import sys
import time
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

from config import REQUEST_TIMEOUT
from make_energieverbruik_sector_csv import (
    _KM_BASE,
    _KM_ELEC_VARS,
    _KM_GAS_VARS,
    _GROEPEN,
    _km_geoitems,
    _normaliseer_gebiedsnaam as _normaliseer,
    _gebiedssleutel,
)

log = logging.getLogger(__name__)

_SECRETS_FILE = Path(__file__).parent / "secrets.local.json"
_OUT_PREFIX = Path(__file__).parent / "diagnose_sector_"
_RETRY_MAX = 3
_RETRY_DELAY = 3

# Geo levels we care about, finest-first. Verified live on 2026-08-13:
# Klimaatmonitor offers buurt, gemeente, nederland, omgeving, postcode,
# provincie, res, res_landsdeel, subres, wijk — and every vbrze_*/vbrzg_*
# variable is published at gemeente, nederland, omgeving, provincie, res, subres.
# The RES-region level is 'res'. Anything not actually offered is skipped.
_KANDIDAAT_LEVELS = ["gemeente", "subres", "res", "provincie", "res_landsdeel", "nederland"]

# Which municipalities.xlsx columns identify the area a gemeente belongs to,
# per geo level. Used to pair region totals with their member municipalities.
_LEVEL_NAAR_KOLOMMEN = {
    "res": ["res_regio", "res_regiocode"],
    "provincie": ["provincie", "provinciecode"],
}


def _laad_apikey() -> str | None:
    if not _SECRETS_FILE.exists():
        log.error(
            "%s bestaat niet. Maak het aan met {\"klimaatmonitor_apikey\": \"<key>\"}.",
            _SECRETS_FILE.name,
        )
        return None
    key = json.loads(_SECRETS_FILE.read_text(encoding="utf-8")).get("klimaatmonitor_apikey")
    if not key:
        log.error("%s bevat geen 'klimaatmonitor_apikey'.", _SECRETS_FILE.name)
    return key


def _get(headers: dict, url: str) -> dict | None:
    """GET with retries. Returns parsed JSON, or None on persistent failure."""
    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 401:
                log.error("  401 — API-key ongeldig of geen toegang: %s", url)
                return None
            if resp.status_code == 404:
                # Legitimate answer for "this variable has no data at this level".
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            if attempt < _RETRY_MAX:
                time.sleep(_RETRY_DELAY)
            else:
                log.warning("  mislukt na %d pogingen: %s (%s)", _RETRY_MAX, url, exc)
                return None
    return None


# ---------------------------------------------------------------------------
# 1. Which geo levels exist?
# ---------------------------------------------------------------------------

def inventariseer_geolevels(headers: dict, var_codes: list[str]) -> pd.DataFrame:
    """List GeoLevels globally and per variable, so we stop guessing."""
    rijen = []

    algemeen = _get(headers, f"{_KM_BASE}/GeoLevels")
    globale_codes = []
    if algemeen:
        for item in algemeen.get("value", []):
            code = item.get("ExternalCode") or item.get("Code") or item.get("Id")
            globale_codes.append(code)
            rijen.append({"scope": "<alle variabelen>", "geolevel": code, "naam": item.get("Name")})
        log.info("GeoLevels globaal (%d): %s", len(globale_codes), globale_codes)
    else:
        log.warning("Kon de globale GeoLevels-lijst niet ophalen.")

    # Per variable — a variable may be published at fewer levels than the
    # platform supports overall, which is exactly what we need to know.
    for var_code in var_codes:
        data = _get(headers, f"{_KM_BASE}/Variables('{var_code}')/GeoLevels")
        codes = []
        if data:
            for item in data.get("value", []):
                code = item.get("ExternalCode") or item.get("Code") or item.get("Id")
                codes.append(code)
                rijen.append({"scope": var_code, "geolevel": code, "naam": item.get("Name")})
        log.info("  %-14s -> %s", var_code, codes or "GEEN")

    return pd.DataFrame(rijen)


# ---------------------------------------------------------------------------
# 2. Pull values at every level at once
# ---------------------------------------------------------------------------

def _parse_externalcode(code: str) -> tuple[str, str]:
    """Split 'gemeente_106' into ('gemeente', '106'). Unprefixed codes are
    reported under level '<onbekend>' rather than silently dropped."""
    if not isinstance(code, str) or "_" not in code:
        return "<onbekend>", str(code)
    level, _, rest = code.partition("_")
    return level, rest


def haal_alle_niveaus(headers: dict, var_code: str, jaar: int) -> pd.DataFrame | None:
    """One request per variable+year for *all* geo levels at once.

    GeoLevels('all') is a documented Swing ODS parameter, so this costs no more
    requests than the current gemeente-only pipeline while returning gemeente,
    regio, provincie and nederland rows together — which is what makes the
    residual comparison cheap.
    """
    url = (
        f"{_KM_BASE}/Variables('{var_code}')/GeoLevels('all')"
        f"/PeriodLevels('year')/Periods('{jaar}')/Values"
    )
    data = _get(headers, url)
    if not data:
        return None
    rows = data.get("value", [])
    if not rows:
        return None

    df = pd.DataFrame(rows)
    if "ExternalCode" not in df.columns:
        log.warning("  %s (%d): antwoord zonder ExternalCode-kolom", var_code, jaar)
        return None

    parsed = df["ExternalCode"].map(_parse_externalcode)
    df["level"] = [p[0] for p in parsed]
    df["gebiedscode"] = [p[1] for p in parsed]
    df["waarde"] = pd.to_numeric(df.get("ValueString"), errors="coerce")
    df["var_code"] = var_code
    df["jaar"] = jaar
    return df[["var_code", "jaar", "level", "gebiedscode", "ExternalCode", "waarde"]]


def haal_alles(headers: dict, jaren: list[int]) -> pd.DataFrame:
    frames = []
    taken = [(c, "elec", g) for g, cs in _KM_ELEC_VARS.items() for c in cs]
    taken += [(c, "gas", g) for g, cs in _KM_GAS_VARS.items() for c in cs]

    for jaar in jaren:
        for i, (var_code, carrier, groep) in enumerate(taken, 1):
            df = haal_alle_niveaus(headers, var_code, jaar)
            if df is None:
                log.warning("  [%2d/%2d] %s %d: geen data", i, len(taken), var_code, jaar)
                continue
            df["carrier"] = carrier
            df["groep"] = groep
            frames.append(df)
            niveaus = df["level"].value_counts().to_dict()
            log.info("  [%2d/%2d] %s %d: %s", i, len(taken), var_code, jaar, niveaus)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# 3. Missingness per variable per level
# ---------------------------------------------------------------------------

def rapport_missing(lang: pd.DataFrame) -> pd.DataFrame:
    """Per variable x year x level: how many areas, how many exactly 0, how many
    absent. Zero and absent are counted separately: CBS privacy suppression
    surfaces as either, depending on the variable."""
    rijen = []
    for (var_code, jaar, level, carrier, groep), g in lang.groupby(
        ["var_code", "jaar", "level", "carrier", "groep"], dropna=False
    ):
        n = len(g)
        n_nan = int(g["waarde"].isna().sum())
        n_nul = int((g["waarde"] == 0).sum())
        rijen.append({
            "var_code": var_code, "carrier": carrier, "groep": groep,
            "jaar": jaar, "level": level,
            "n_gebieden": n, "n_nan": n_nan, "n_nul": n_nul,
            "n_ontbrekend": n_nan + n_nul,
            "pct_ontbrekend": round(100 * (n_nan + n_nul) / n, 1) if n else float("nan"),
            "som": g["waarde"].sum(min_count=1),
        })
    return pd.DataFrame(rijen).sort_values(["carrier", "groep", "var_code", "jaar", "level"])


# ---------------------------------------------------------------------------
# 4. Residual: region total vs sum of its municipalities
# ---------------------------------------------------------------------------

def _laad_gemeente_naar_regio() -> pd.DataFrame | None:
    """gemeentecode -> RES-regio / provincie, from municipalities.xlsx (the same
    file make_gemeenten_potentie_csv.py uses, so the mapping stays consistent)."""
    kandidaten = [
        Path(__file__).parent.parent / "municipalities.xlsx",
        Path(__file__).parent / "municipalities.xlsx",
    ]
    pad = next((p for p in kandidaten if p.exists()), None)
    if pad is None:
        log.warning("municipalities.xlsx niet gevonden — residual-analyse overgeslagen.")
        return None

    df = pd.read_excel(pad, sheet_name="municipalities")
    df = df.rename(columns={
        "gwb_code": "gemeentecode",
        "province": "provincie",
        "province_code": "provinciecode",
        "RES_region": "res_regio",
        "RES_code": "res_regiocode",
    })
    keep = ["gemeentecode", "provincie", "provinciecode", "res_regio", "res_regiocode"]
    df = df[[c for c in keep if c in df.columns]].dropna(subset=["gemeentecode"])
    df["gemeentecode"] = df["gemeentecode"].astype(str).str.strip()
    return df.drop_duplicates(subset=["gemeentecode"])


def _gemeentecode_uit_level(g: pd.DataFrame) -> pd.Series:
    """'gemeente_106' -> 'GM0106' (numeric part is the CBS gemeente code)."""
    return "GM" + g["gebiedscode"].astype(str).str.zfill(4)


def rapport_residual(lang: pd.DataFrame, regio_niveau: str, headers: dict) -> pd.DataFrame:
    """For each variable x year x region: the region's own reported value, the
    sum of its municipalities, and the residual between them.

    A positive residual is demand the current pipeline loses: the region knows
    about it, but it never appears in any municipality's cell. That is precisely
    what the fair-share fallback redistributes.
    """
    mapping = _laad_gemeente_naar_regio()
    if mapping is None:
        return pd.DataFrame()

    gem = lang[lang["level"] == "gemeente"].copy()
    if gem.empty:
        log.warning("Geen gemeente-rijen — residual-analyse overgeslagen.")
        return pd.DataFrame()
    gem["gemeentecode"] = _gemeentecode_uit_level(gem)
    gem = gem.merge(mapping, on="gemeentecode", how="left")

    reg = lang[lang["level"] == regio_niveau].copy()
    if reg.empty:
        log.warning("Geen '%s'-rijen — residual-analyse overgeslagen.", regio_niveau)
        return pd.DataFrame()

    # Klimaatmonitor's area codes are internal ids that match neither
    # municipalities.xlsx's RES_code nor province_code, so translate them to
    # names via GeoItems and pair on the normalised name. Which municipal column
    # to compare against depends on the level being reconciled — using the RES
    # column while reconciling provinces silently pairs nothing.
    namen = _km_geoitems(headers, regio_niveau)
    reg["regio_sleutel"] = reg["gebiedscode"].astype(str).map(
        lambda c: namen.get(c, _normaliseer(c))
    )

    kandidaat_kolommen = _LEVEL_NAAR_KOLOMMEN.get(regio_niveau, ["res_regio", "res_regiocode"])
    sleutels_regio = set(reg["regio_sleutel"])
    beste_kolom, beste_overlap = None, 0
    for kolom in kandidaat_kolommen:
        if kolom not in gem.columns:
            continue
        overlap = len(sleutels_regio & set(gem[kolom].dropna().map(_gebiedssleutel)))
        if overlap > beste_overlap:
            beste_kolom, beste_overlap = kolom, overlap

    if beste_kolom is None:
        log.warning(
            "Geen koppeling tussen '%s'-gebieden en municipalities.xlsx (%s) — "
            "residual-analyse overgeslagen. Klimaatmonitor-namen: %s...",
            regio_niveau, kandidaat_kolommen, sorted(sleutels_regio)[:5],
        )
        return pd.DataFrame()

    log.info("  '%s' gekoppeld via '%s': %d van %d gebieden gematcht",
             regio_niveau, beste_kolom, beste_overlap, len(sleutels_regio))
    gem["regio_sleutel"] = gem[beste_kolom].map(_gebiedssleutel)

    gem_som = gem.groupby(["var_code", "jaar", "regio_sleutel"], as_index=False).agg(
        som_gemeenten=("waarde", lambda s: s.sum(min_count=1)),
        n_gemeenten=("waarde", "size"),
        n_gemeenten_nul=("waarde", lambda s: int(((s == 0) | s.isna()).sum())),
    )
    reg_val = reg.groupby(["var_code", "jaar", "regio_sleutel"], as_index=False).agg(
        regio_waarde=("waarde", "sum")
    )

    uit = gem_som.merge(reg_val, on=["var_code", "jaar", "regio_sleutel"], how="outer")
    ongepaard = int(uit["regio_waarde"].isna().sum() + uit["som_gemeenten"].isna().sum())
    if ongepaard:
        log.warning("  %d van %d rijen ongepaard (regio zonder gemeenten of andersom) — "
                    "percentages per combinatie zijn voor die rijen betekenisloos.",
                    ongepaard, len(uit))
    uit["residual"] = uit["regio_waarde"] - uit["som_gemeenten"]
    uit["residual_pct_van_regio"] = (
        100 * uit["residual"] / uit["regio_waarde"].where(uit["regio_waarde"] != 0)
    ).round(1)
    return uit.sort_values(["var_code", "jaar", "regio_sleutel"])


# ---------------------------------------------------------------------------
# 5. Verdict
# ---------------------------------------------------------------------------

def _print_conclusie(missing: pd.DataFrame, residual: pd.DataFrame, regio_niveau: str) -> None:
    print("\n" + "=" * 78)
    print("CONCLUSIE")
    print("=" * 78)

    if missing.empty:
        print("Geen data opgehaald — geen conclusie mogelijk.")
        return

    per_level = missing.groupby("level").agg(
        variabelen=("var_code", "nunique"),
        gem_pct_ontbrekend=("pct_ontbrekend", "mean"),
    ).round(1)
    print("\nGemiddeld aandeel ontbrekende/nul cellen per geo-niveau:")
    print(per_level.to_string())

    gem = missing[missing["level"] == "gemeente"]
    if not gem.empty:
        erg = gem[gem["pct_ontbrekend"] > 0].sort_values("pct_ontbrekend", ascending=False)
        print(f"\nGemeente-niveau: {len(erg)} van {len(gem)} variabele-jaren hebben "
              f"minstens één ontbrekende gemeente.")
        print("Top-15 zwaarst onderdrukte variabelen op gemeenteniveau:")
        print(erg.head(15)[["var_code", "carrier", "groep", "jaar",
                            "n_gebieden", "n_nul", "n_nan", "pct_ontbrekend"]].to_string(index=False))

    reg = missing[missing["level"] == regio_niveau]
    if reg.empty:
        print(f"\n[!] Niveau '{regio_niveau}' leverde GEEN data op voor deze variabelen.")
        print("    -> Regionale fair-share is niet mogelijk op dit niveau. Escaleer naar")
        print("       provincie/nederland, of naar een grovere SBI-aggregatie.")
    else:
        pct = reg["pct_ontbrekend"].mean()
        print(f"\nNiveau '{regio_niveau}': gemiddeld {pct:.1f}% van de regio's ontbreekt/is nul.")
        if pct < 5:
            print("    -> Regionaal is vrijwel volledig: fair-share op regioniveau volstaat.")
        elif pct < 25:
            print("    -> Regionaal heeft gaten: houd de escalatieladder naar provincie aan"
                  " voor de regio's die ontbreken.")
        else:
            print("    -> Regionaal is te vaak leeg: ga direct naar provincie/nederland, of"
                  " aggregeer eerst naar grovere SBI-groepen.")

    if not residual.empty:
        # Split by year and carrier: elec is kWh, gas is m3, so one combined
        # total would be dimensionally meaningless, and averaging years hides
        # that the most recent year is often far less complete.
        res = residual.copy()
        if "var_code" in res.columns:
            res["carrier"] = res["var_code"].str.startswith("vbrze").map(
                {True: "elec", False: "gas"})
        print("\nResidual (regio-totaal vs. som van de gemeenten), per jaar en drager:")
        for sleutel, deel in res.groupby([c for c in ("jaar", "carrier") if c in res.columns]):
            tot_regio = deel["regio_waarde"].sum(min_count=1)
            tot_gem = deel["som_gemeenten"].sum(min_count=1)
            if not tot_regio:
                continue
            verlies = 100 * (tot_regio - tot_gem) / tot_regio
            print(f"  {sleutel}: som gemeenten = {tot_gem:,.0f}, regio-totalen = "
                  f"{tot_regio:,.0f} ({verlies:.1f}% gaat nu verloren)")
        groot = residual[residual["residual_pct_van_regio"] > 10]
        print(f"Variabele-regio-combinaties met >10% residual: {len(groot)} van {len(residual)}.")


def main(jaren: list[int]) -> int:
    apikey = _laad_apikey()
    if apikey is None:
        return 1
    headers = {"apikey": apikey}

    alle_vars = [c for cs in _KM_ELEC_VARS.values() for c in cs]
    alle_vars += [c for cs in _KM_GAS_VARS.values() for c in cs]
    log.info("Variabelen: %d (%d SBI-groepen)", len(alle_vars), len(_GROEPEN))

    log.info("\n--- 1. GeoLevels inventariseren ---")
    geolevels = inventariseer_geolevels(headers, alle_vars)
    if not geolevels.empty:
        geolevels.to_csv(f"{_OUT_PREFIX}geolevels.csv", index=False, sep=";")
        log.info("  -> %sgeolevels.csv", _OUT_PREFIX)

    beschikbaar = set(geolevels["geolevel"].dropna()) if not geolevels.empty else set()
    # Only levels we can actually map gemeenten to are usable for the residual —
    # 'subres' is finer than 'res' but municipalities.xlsx has no subres column.
    regio_niveau = next((lvl for lvl in _LEVEL_NAAR_KOLOMMEN if lvl in beschikbaar), "res")
    log.info("Regio-niveau voor de residual-analyse: '%s' (beschikbaar: %s)",
             regio_niveau, sorted(beschikbaar))

    log.info("\n--- 2. Waarden ophalen op alle niveaus ---")
    lang = haal_alles(headers, jaren)
    if lang.empty:
        log.error("Geen enkele waarde opgehaald.")
        return 1

    log.info("\n--- 3. Missingness per niveau ---")
    missing = rapport_missing(lang)
    missing.to_csv(f"{_OUT_PREFIX}missing.csv", index=False, sep=";")
    log.info("  -> %smissing.csv (%d rijen)", _OUT_PREFIX, len(missing))

    log.info("\n--- 4. Residual regio vs. som gemeenten ---")
    residual = rapport_residual(lang, regio_niveau, headers)
    if not residual.empty:
        residual.to_csv(f"{_OUT_PREFIX}residual.csv", index=False, sep=";")
        log.info("  -> %sresidual.csv (%d rijen)", _OUT_PREFIX, len(residual))

    _print_conclusie(missing, residual, regio_niveau)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    jaren = [int(a) for a in sys.argv[1:]] or [2023, 2024]
    sys.exit(main(jaren))
