"""Parse CBS Hoofdverwarmingsinstallaties and match to CBS buurten/gemeenten.

Source:  Hoofdverwarmingsinstallaties_woningen_2022_2024.xlsx  (Tabel 2)
         Column B  = code (GM…/WK…/BU…)
         Column C  = year (2022, 2023, 2024)
         Columns G–M = heating-type percentages; missing values are "."

Fallback for missing buurt values:
  BU19790622  → try WK197906  (parent wijk:    "WK" + bu_code[2:8])
             → try GM1979    (parent gemeente: "GM" + bu_code[2:6])
             → NaN if all missing

Output (one file per year, in processed/):
  verwarmingsinstallaties_buurten_{jaar}.csv
  verwarmingsinstallaties_gemeenten_{jaar}.csv

Run standalone:  python make_verwarmingsinstallaties_csv.py
Or via:          python run_pipeline.py
"""

import logging
import sys
import time
from pathlib import Path
from datetime import date

import pandas as pd

from config import PROCESSED_DIR, OUTPUT_SEPARATOR, FILL_UNMATCHED

log = logging.getLogger(__name__)

_DIR = Path(__file__).parent
_EXCEL = _DIR.parent / "Hoofdverwarmingsinstallaties_woningen_2022_2024.xlsx"
_SHEET = "Tabel 2"
_HEADER_ROW = 3        # 0-based index → Excel row 4
_CODE_COL   = "Wijken en buurten"
_YEAR_COL   = "Periode"

# Columns G–M from the sheet (exactly as they appear in the header row)
_HEATING_COLS = [
    "Individuele CV",
    "Blok- verwarming",
    "Stadsverwarming met hoog gasverbruik",
    "Stadsverwarming met laag gasverbruik",
    "Stadsverwarming zonder gasverbruik",
    "Hoofdzakelijk elektrisch verwarmd met hoog gasverbruik",
    "Hoofdzakelijk elektrisch verwarmd met laag gasverbruik",
]

# Short output column names (same order as _HEATING_COLS)
_OUT_NAMES = [
    "individuele_cv",
    "blokverwarming",
    "stadsverwarming_hoog_gas",
    "stadsverwarming_laag_gas",
    "stadsverwarming_zonder_gas",
    "elektrisch_hoog_gas",
    "elektrisch_laag_gas",
]

_MISSING = "."


def _parent_wk(bu_code: str) -> str:
    return "WK" + bu_code[2:8]


def _parent_gm(code: str) -> str:
    """Works for both BU and WK codes (first 4 numeric digits = gemeente)."""
    return "GM" + code[2:6]


def _load_excel() -> pd.DataFrame:
    """Read Tabel 2, return raw DataFrame."""
    if not _EXCEL.exists():
        raise FileNotFoundError(
            f"Excel niet gevonden: {_EXCEL}\n"
            f"Verwacht bestand: Hoofdverwarmingsinstallaties_woningen_2022_2024.xlsx"
        )
    log.info("Lees %s …", _EXCEL.name)
    df = pd.read_excel(_EXCEL, sheet_name=_SHEET, header=_HEADER_ROW, dtype=str)
    log.info("  Geladen: %d rijen, %d kolommen", len(df), len(df.columns))
    return df


def _build_lookup(df: pd.DataFrame) -> dict[tuple[str, int], dict[str, float | None]]:
    """
    Build {(code, year): {short_col: value_or_None}} from the raw DataFrame.
    "." is treated as missing (None).
    """
    missing_cols = [c for c in _HEATING_COLS if c not in df.columns]
    if missing_cols:
        raise ValueError(
            f"Verwachte kolommen niet gevonden in '{_SHEET}': {missing_cols}\n"
            f"Beschikbare kolommen: {list(df.columns)}"
        )

    lookup: dict[tuple[str, int], dict[str, float | None]] = {}

    for _, row in df.iterrows():
        code = str(row.get(_CODE_COL, "") or "").strip()
        year_raw = str(row.get(_YEAR_COL, "") or "").strip()
        if not code or not code[:2] in ("GM", "WK", "BU", "NL"):
            continue
        try:
            year = int(float(year_raw))
        except (ValueError, TypeError):
            continue

        vals: dict[str, float | None] = {}
        for src, dst in zip(_HEATING_COLS, _OUT_NAMES):
            raw = str(row.get(src, "") or "").strip()
            if raw == _MISSING or raw == "" or raw.lower() == "nan":
                vals[dst] = None
            else:
                try:
                    vals[dst] = float(raw)
                except ValueError:
                    vals[dst] = None
        lookup[(code, year)] = vals

    log.info("  Lookup gebouwd: %d (code, jaar) combinaties", len(lookup))
    return lookup


def _resolve(code: str, year: int, lookup: dict) -> dict[str, float | None]:
    """Return values for a code+year with BU→WK→GM fallback for missing cols."""
    base = lookup.get((code, year), {col: None for col in _OUT_NAMES})

    if not code.startswith("BU"):
        return base

    # Per column: fill None from parent wijk, then parent gemeente
    wk_code = _parent_wk(code)
    gm_code = _parent_gm(code)
    wk_vals = lookup.get((wk_code, year), {})
    gm_vals = lookup.get((gm_code, year), {})

    result = {}
    for col in _OUT_NAMES:
        v = base.get(col)
        if v is None:
            v = wk_vals.get(col)
        if v is None:
            v = gm_vals.get(col)
        result[col] = v

    return result


def _make_df_for_prefix(
    prefix: str,
    years: list[int],
    lookup: dict,
    id_col: str,
) -> pd.DataFrame:
    """Build output DataFrame for one prefix (BU or GM) and all years."""
    codes = sorted({code for (code, _) in lookup if code.startswith(prefix)})
    rows = []
    for code in codes:
        for year in years:
            vals = _resolve(code, year, lookup)
            rows.append({id_col: code, "jaar": year, **vals})
    return pd.DataFrame(rows)


def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".tmp.csv")
    try:
        df.fillna(FILL_UNMATCHED).to_csv(tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def main() -> bool:
    t0 = time.monotonic()

    try:
        df_raw = _load_excel()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return False
    except Exception as exc:
        log.error("Fout bij lezen Excel: %s", exc, exc_info=True)
        return False

    try:
        lookup = _build_lookup(df_raw)
    except ValueError as exc:
        log.error("%s", exc)
        return False

    years = sorted({yr for (_, yr) in lookup})
    log.info("  Jaren in data: %s", years)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    all_ok = True

    # --- Buurten ---
    log.info("  Buurten verwerken …")
    df_bu = _make_df_for_prefix("BU", years, lookup, id_col="buurtcode")
    n_fallback = 0
    for col in _OUT_NAMES:
        n_fallback += df_bu[col].notna().sum()
    log.info(
        "  Buurten: %d codes × %d jaren = %d rijen",
        df_bu["buurtcode"].nunique(), len(years), len(df_bu),
    )

    # Count rows where fallback was used (original value was missing)
    bu_codes_in_lookup = {code for (code, _) in lookup if code.startswith("BU")}
    n_needed_fallback = sum(
        1 for code in bu_codes_in_lookup
        for year in years
        for col in _OUT_NAMES
        if lookup.get((code, year), {}).get(col) is None
        and _resolve(code, year, lookup).get(col) is not None
    )
    log.info("  Buurten: %d celvullingen via wijk/gemeente-fallback", n_needed_fallback)

    out_bu = PROCESSED_DIR / f"verwarmingsinstallaties_buurten_{today}.csv"
    _atomic_write_csv(df_bu, out_bu)
    log.info("  Opgeslagen: %s (%d rijen)", out_bu.name, len(df_bu))

    # --- Gemeenten ---
    log.info("  Gemeenten verwerken …")
    df_gm = _make_df_for_prefix("GM", years, lookup, id_col="gemeentecode")
    log.info(
        "  Gemeenten: %d codes × %d jaren = %d rijen",
        df_gm["gemeentecode"].nunique(), len(years), len(df_gm),
    )
    out_gm = PROCESSED_DIR / f"verwarmingsinstallaties_gemeenten_{today}.csv"
    _atomic_write_csv(df_gm, out_gm)
    log.info("  Opgeslagen: %s (%d rijen)", out_gm.name, len(df_gm))

    elapsed = time.monotonic() - t0
    log.info("Verwarmingsinstallaties CSV klaar in %.0fs", elapsed)
    return all_ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
