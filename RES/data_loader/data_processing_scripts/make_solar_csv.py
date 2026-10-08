"""Download CBS Zonnestroom per buurt and produce a per-buurt solar capacity CSV.

Source: CBS OData — "Zonnestroom; wijken en buurten" series
  86044NED (2022) — most recent as of 2026
  85775NED (2021)
  85447NED (2020)
  85010NED (2019)

Columns:
  - Codering_3 / SoortRegio_2: identifies buurt rows (prefix BU)
  - AantalInstallatiesBijWoningen_5: count of installations at homes
  - OpgesteldVermogenVanZonnepanelen_6: installed capacity in kWp (kW in 2020/2021 editions)

Coverage: ALL solar installations at homes (not just SDE-subsidized).
No small/large split at buurt level — CBS only publishes that split at gemeente level.

Pagination: CBS ODataApi has a 5000-row $top limit. The ODataFeed endpoint is used
with $skip for full retrieval.

Output: processed/solar_buurten_{today}.csv
Columns: codering, jaar, solar_woningen_kwp, solar_woningen_count

The buurten CSV (Phase 6) picks the nearest available year for each CBS boundary year
(same pattern as verwarmingsinstallaties).

Run standalone:  python make_solar_csv.py
Or via:          python run_pipeline.py --solar
"""

import logging
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from config import OUTPUT_SEPARATOR, FILL_UNMATCHED, PROCESSED_DIR

log = logging.getLogger(__name__)

TODAY = date.today().isoformat()

# CBS dataset IDs per year — extend this dict when CBS publishes newer editions
_DATASETS: dict[int, str] = {
    2022: "86044NED",
    2021: "85775NED",
    2020: "85447NED",
    2019: "85010NED",
}

_ODATA_FEED_URL = "https://opendata.cbs.nl/ODataFeed/odata/{dataset}/TypedDataSet"
_SELECT = "Codering_3,SoortRegio_2,AantalInstallatiesBijWoningen_5,OpgesteldVermogenVanZonnepanelen_6"
_PAGE_SIZE = 5000
_TIMEOUT = 60
_RETRY_MAX = 3
_RETRY_DELAY = 5


def _fetch_dataset(dataset_id: str, jaar: int) -> pd.DataFrame | None:
    """Download all buurt-level rows for one CBS zonnestroom dataset."""
    url = _ODATA_FEED_URL.format(dataset=dataset_id)
    frames: list[pd.DataFrame] = []
    skip = 0

    log.info("  %d (%s): paginerend ophalen...", jaar, dataset_id)

    while True:
        params = {
            "$select": _SELECT,
            "$top": _PAGE_SIZE,
            "$skip": skip,
            "$format": "json",
        }
        for attempt in range(1, _RETRY_MAX + 1):
            try:
                resp = requests.get(url, params=params, timeout=_TIMEOUT)
                resp.raise_for_status()
                break
            except Exception as exc:
                if attempt < _RETRY_MAX:
                    log.warning("    Poging %d/%d mislukt (skip=%d): %s", attempt, _RETRY_MAX, skip, exc)
                    time.sleep(_RETRY_DELAY)
                else:
                    log.error("    Download mislukt na %d pogingen (skip=%d): %s", _RETRY_MAX, skip, exc)
                    return None

        data = resp.json()
        rows = data.get("value", [])
        if not rows:
            break

        frames.append(pd.DataFrame(rows))
        log.info("    skip=%d: %d rijen opgehaald", skip, len(rows))
        if len(rows) < _PAGE_SIZE:
            break
        skip += _PAGE_SIZE

    if not frames:
        log.warning("  %d (%s): geen rijen ontvangen", jaar, dataset_id)
        return None

    df = pd.concat(frames, ignore_index=True)
    log.info("  %d (%s): %d rijen totaal", jaar, dataset_id, len(df))
    return df


def _extract_buurten(df: pd.DataFrame, jaar: int) -> pd.DataFrame:
    """Filter to buurt rows only and normalise column names."""
    buurt_mask = df["SoortRegio_2"].str.strip() == "Buurt"
    buurten = df[buurt_mask].copy()

    buurten["codering"] = buurten["Codering_3"].str.strip()
    buurten["jaar"] = jaar
    buurten["solar_woningen_kwp"] = pd.to_numeric(
        buurten["OpgesteldVermogenVanZonnepanelen_6"], errors="coerce"
    )
    buurten["solar_woningen_count"] = pd.to_numeric(
        buurten["AantalInstallatiesBijWoningen_5"], errors="coerce"
    )

    result = buurten[["codering", "jaar", "solar_woningen_kwp", "solar_woningen_count"]].reset_index(drop=True)

    n = len(result)
    total_kwp = result["solar_woningen_kwp"].sum()
    log.info(
        "  %d: %d buurten, %.0f kWp totaal, %d installaties",
        jaar, n, total_kwp, result["solar_woningen_count"].sum(),
    )
    return result


def main() -> bool:
    t0 = time.monotonic()
    log.info("CBS Zonnestroom per buurt downloaden (%d datasets)...", len(_DATASETS))
    log.info(
        "  Let op: CBS buurt-niveau data beschikbaar t/m 2022. "
        "Geen klein/groot-splitsing op buurtniveau (alleen op gemeenteniveau in 85005NED)."
    )

    frames: list[pd.DataFrame] = []

    for jaar in sorted(_DATASETS.keys(), reverse=True):
        dataset_id = _DATASETS[jaar]
        raw = _fetch_dataset(dataset_id, jaar)
        if raw is None:
            continue
        buurten = _extract_buurten(raw, jaar)
        if not buurten.empty:
            frames.append(buurten)

    if not frames:
        log.error("Geen data gegenereerd.")
        return False

    result = pd.concat(frames, ignore_index=True)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROCESSED_DIR / f"solar_buurten_{TODAY}.csv"
    tmp = out_path.with_suffix(".tmp.csv")
    try:
        result.fillna(FILL_UNMATCHED).to_csv(
            tmp, sep=OUTPUT_SEPARATOR, index=False, encoding="utf-8"
        )
        tmp.replace(out_path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("Schrijffout: %s", exc)
        return False

    elapsed = time.monotonic() - t0
    log.info(
        "Solar buurten CSV geschreven: %d rijen, %d kolommen → %s (%.0fs)",
        len(result), len(result.columns), out_path.name, elapsed,
    )
    return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        stream=sys.stdout,
    )
    sys.exit(0 if main() else 1)
