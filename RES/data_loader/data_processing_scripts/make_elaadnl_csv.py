"""Download EV prognoses from ElaadNL Outlook Scenariotool per buurt (heel Nederland).

Source:  https://api-outlook-v2-prd.thankfulrock-fcd5ae60.westeurope.azurecontainerapps.io
         Public API (no auth required), CC BY-NC-ND 4.0.
         Uses CBS 2024 buurt boundaries.

License note: CC BY-NC-ND 4.0 — internal non-commercial use permitted.
              Distributing merged/adapted output to third parties is NOT permitted.
              Contact ElaadNL for written permission before public sharing.

Scope:   All buurten in the Netherlands (derived from CBS 2024 buurten GeoPackage).
         All scenarios: low, middle, high, realization.
         All modalities: car_bev, car_phev, van, truck.

Output:  processed/elaadnl_ev_prognoses_{today}.csv
         Long format: one row per (buurtcode, jaar, scenario, modality)
         Years 2025–2050 (26 data points per combination).

Raw responses cached in raw/elaadnl/{scenario}/{modality}/{buurtcode}.json
so re-runs skip already-fetched combinations. First full run ~18 hours;
subsequent runs complete in seconds.

Run standalone:  python make_elaadnl_csv.py
Or via:          python run_pipeline.py --elaadnl
"""

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from config import PROCESSED_DIR, RAW_DIR, OUTPUT_SEPARATOR, FILL_UNMATCHED

log = logging.getLogger(__name__)

_ELAADNL_API = "https://api-outlook-v2-prd.thankfulrock-fcd5ae60.westeurope.azurecontainerapps.io"
_CACHE_DIR = RAW_DIR / "elaadnl"
TODAY = date.today().isoformat()

SCENARIOS  = ["low", "middle", "high", "realization"]
MODALITIES = ["car_bev", "car_phev", "van", "truck"]

_TIMEOUT      = 30   # seconds per request
_RETRY_MAX    = 3
_RETRY_DELAY  = 5    # seconds between retries
_POLITE_DELAY = 0.05 # seconds between successful requests


# ---------------------------------------------------------------------------
# Buurtcodes from CBS 2024
# ---------------------------------------------------------------------------

def _get_all_buurtcodes() -> list[tuple[str, str]]:
    """
    Read CBS 2024 buurten GeoPackage, return all NL buurten.
    Returns list of (buurtcode, gemeentecode) tuples sorted for deterministic ordering.
    """
    gpkg = RAW_DIR / "cbs_wijkenbuurten" / "2024" / "buurten.gpkg"
    if not gpkg.exists():
        raise FileNotFoundError(
            f"CBS 2024 buurten niet gevonden: {gpkg}\n"
            "Run eerst: python run_pipeline.py --download"
        )

    gdf = gpd.read_file(gpkg)

    # Detect buurtcode column
    for col in ["buurtcode", "BU_CODE", "bu_code", "Buurtcode", "codering", "Codering_3"]:
        if col in gdf.columns:
            bu_col = col
            break
    else:
        raise ValueError(
            f"Geen buurtcode-kolom gevonden. Beschikbare kolommen: {list(gdf.columns)}"
        )

    # Detect or derive gemeente code
    for col in ["gemeentecode", "GM_CODE", "gm_code"]:
        if col in gdf.columns:
            gm_col = col
            break
    else:
        gdf["_gm"] = "GM" + gdf[bu_col].str[2:6]
        gm_col = "_gm"

    subset = gdf[[bu_col, gm_col]].dropna(subset=[bu_col])
    pairs = sorted(set(zip(subset[bu_col].tolist(), subset[gm_col].tolist())))

    log.info("Nederland: %d buurten gevonden in CBS 2024", len(pairs))
    return pairs


# ---------------------------------------------------------------------------
# ElaadNL API
# ---------------------------------------------------------------------------

def _unwrap_records(data) -> list[dict]:
    """
    Extract the actual year-value records from whatever the API returned.
    The ElaadNL API returns [{scenario, data: [actual records]}] — a list
    containing one wrapper object. This unwraps it to the inner record list.
    Also handles plain list-of-records and dict responses defensively.
    """
    if isinstance(data, list):
        if not data:
            return []
        first = data[0]
        # Real API response: [{scenario, data: [{year, number, ...}, ...]}]
        if isinstance(first, dict) and isinstance(first.get("data"), list):
            return first["data"]
        # Flat list of records (future-proofing)
        return data
    if isinstance(data, dict):
        for key in ("data", "results", "values", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _fetch_ev_numbers(buurtcode: str, scenario: str, modality: str) -> list[dict] | None:
    """
    Fetch EV prognose records from ElaadNL API.
    Returns list of year-value records on success, [] on 404, None on unrecoverable error.
    Raw API responses are cached in raw/elaadnl/{scenario}/{modality}/{buurtcode}.json.
    """
    cache_path = _CACHE_DIR / scenario / modality / f"{buurtcode}.json"

    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            return _unwrap_records(raw)
        except Exception:
            pass  # corrupt cache — refetch

    url = f"{_ELAADNL_API}/ev_numbers"
    params = {
        "area_type":       "neighborhoods",
        "area_identifier": buurtcode,
        "modality":        modality,
        "scenario":        scenario,
    }

    for attempt in range(1, _RETRY_MAX + 1):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.HTTPError:
            if resp.status_code == 404:
                log.debug("  404: %s / %s / %s", buurtcode, scenario, modality)
                _write_cache(cache_path, [])
                return []
            if attempt < _RETRY_MAX:
                log.warning(
                    "  HTTP %d voor %s/%s/%s (poging %d/%d)",
                    resp.status_code, buurtcode, scenario, modality, attempt, _RETRY_MAX,
                )
                time.sleep(_RETRY_DELAY)
            else:
                log.error(
                    "  Opgegeven na %d pogingen voor %s/%s/%s (HTTP %d)",
                    _RETRY_MAX, buurtcode, scenario, modality, resp.status_code,
                )
                return None
        except Exception as exc:
            if attempt < _RETRY_MAX:
                time.sleep(_RETRY_DELAY)
            else:
                log.error("  Fout voor %s/%s/%s: %s", buurtcode, scenario, modality, exc)
                return None
    else:
        return None

    # Save raw API response to cache, return unwrapped records
    _write_cache(cache_path, data)
    return _unwrap_records(data)


def _write_cache(path: Path, data: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _extract_jaar_waarde(rec: dict) -> tuple[int | None, float | None]:
    """Extract year and value from one API record, normalising possible field names."""
    jaar = rec.get("year") or rec.get("jaar") or rec.get("Year") or rec.get("period")
    waarde = (
        rec.get("value")
        or rec.get("count")
        or rec.get("ev_count")
        or rec.get("total")
        or rec.get("amount")
        or rec.get("Value")
        or rec.get("number")
    )
    if jaar is None:
        return None, None
    try:
        jaar = int(jaar)
    except (TypeError, ValueError):
        return None, None
    try:
        waarde = float(waarde) if waarde is not None else None
    except (TypeError, ValueError):
        waarde = None
    return jaar, waarde


# ---------------------------------------------------------------------------
# Build CSV from cache
# ---------------------------------------------------------------------------

def build_csv_from_cache(gemeentecode_lookup: dict[str, str]) -> bool:
    """
    Read all cached JSON files and write the processed CSV.
    Always reflects the complete current state of the cache —
    call this at any point during or after the download to get
    a CSV with everything downloaded so far.

    gemeentecode_lookup: {buurtcode: gemeentecode}  (used to populate the column;
                         falls back to deriving GM code from buurtcode digits if missing)
    """
    if not _CACHE_DIR.exists():
        log.error("Cache-map niet gevonden: %s", _CACHE_DIR)
        return False

    rows: list[dict] = []
    n_files = 0

    for scenario in SCENARIOS:
        for modality in MODALITIES:
            subdir = _CACHE_DIR / scenario / modality
            if not subdir.exists():
                continue
            for json_file in subdir.glob("*.json"):
                buurtcode = json_file.stem
                gemeentecode = gemeentecode_lookup.get(
                    buurtcode, "GM" + buurtcode[2:6]
                )
                try:
                    raw = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for rec in _unwrap_records(raw):
                    jaar, waarde = _extract_jaar_waarde(rec)
                    if jaar is None:
                        continue
                    rows.append({
                        "buurtcode":    buurtcode,
                        "gemeentecode": gemeentecode,
                        "scenario":     scenario,
                        "modality":     modality,
                        "jaar":         jaar,
                        "ev_aantal":    waarde,
                    })
                n_files += 1

    log.info("Cache gelezen: %d bestanden, %d records", n_files, len(rows))

    if not rows:
        log.error("Geen data in cache — niets om te schrijven")
        return False

    df = pd.DataFrame(rows)
    df["jaar"] = pd.to_numeric(df["jaar"], errors="coerce").astype("Int64")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / f"elaadnl_ev_prognoses_{TODAY}.csv"
    tmp = out_path.with_suffix(".tmp.csv")
    try:
        df.fillna(FILL_UNMATCHED).to_csv(
            tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8"
        )
        tmp.replace(out_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("Schrijffout: %s", exc)
        return False

    n_buurten = df["buurtcode"].nunique()
    log.info(
        "ElaadNL CSV geschreven: %d rijen, %d buurten → %s",
        len(df), n_buurten, out_path.name,
    )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> bool:
    t0 = time.monotonic()

    try:
        buurt_pairs = _get_all_buurtcodes()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return False
    except ValueError as exc:
        log.error("%s", exc)
        return False

    if not buurt_pairs:
        log.error("Geen buurtcodes gevonden — controleer CBS 2024 buurten GeoPackage")
        return False

    gemeentecode_lookup = {code: gm for code, gm in buurt_pairs}
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Write CSV from whatever is already in the cache before starting new downloads.
    # This makes the CSV available immediately for --buurten even mid-download.
    if _CACHE_DIR.exists() and any(_CACHE_DIR.rglob("*.json")):
        log.info("Cache gevonden — CSV genereren vanuit huidige cache ...")
        build_csv_from_cache(gemeentecode_lookup)

    total  = len(buurt_pairs) * len(SCENARIOS) * len(MODALITIES)
    done   = 0
    errors = 0
    cached = 0

    log.info(
        "ElaadNL download: %d buurten × %d scenarios × %d modaliteiten = %d API-calls",
        len(buurt_pairs), len(SCENARIOS), len(MODALITIES), total,
    )

    for buurtcode, gemeentecode in buurt_pairs:
        for scenario in SCENARIOS:
            for modality in MODALITIES:
                cache_path = _CACHE_DIR / scenario / modality / f"{buurtcode}.json"
                was_cached = cache_path.exists()

                records = _fetch_ev_numbers(buurtcode, scenario, modality)
                done += 1

                if records is None:
                    errors += 1
                    continue

                if was_cached:
                    cached += 1
                elif _POLITE_DELAY > 0:
                    time.sleep(_POLITE_DELAY)

                if done % 200 == 0 or done == total:
                    log.info(
                        "  Voortgang: %d/%d (%.0f%%), cache-hits: %d, fouten: %d",
                        done, total, 100 * done / total, cached, errors,
                    )

    log.info(
        "Download klaar: %d geslaagd (%d nieuw, %d cached), %d fouten",
        done - errors, done - errors - cached, cached, errors,
    )

    # Build CSV from full cache (includes all previous + newly downloaded files)
    ok = build_csv_from_cache(gemeentecode_lookup)

    elapsed = time.monotonic() - t0
    log.info("ElaadNL klaar in %.0fs", elapsed)
    return ok and errors == 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
