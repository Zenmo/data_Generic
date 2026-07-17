"""Main pipeline orchestrator.

Usage:
    python run_pipeline.py                  # full pipeline: all phases in order
    python run_pipeline.py --download       # Phase 1 only: download raw data
    python run_pipeline.py --process        # Phase 2 only: match WarmteAtlas features to CBS
    python run_pipeline.py --process --force  # Phase 2: re-process all layers, ignoring cached CSVs
    python run_pipeline.py --force          # full pipeline, re-processing all layers
    python run_pipeline.py --verwarming     # Phase 3 only: parse verwarmingsinstallaties Excel
    python run_pipeline.py --windturbines   # Phase 4 only: download + match RIVM windturbines
    python run_pipeline.py --elaadnl        # Phase 5 only: download ElaadNL EV prognoses (NL)
    python run_pipeline.py --tvw            # Phase 5b only: match TVW_voortgang to buurten
    python run_pipeline.py --warmtetransitie # Phase 5c only: download WarmteTransitie from RVO
    python run_pipeline.py --solar          # Phase 5d only: download CBS Zonnestroom per buurt
    python run_pipeline.py --bedrijfswagens # Phase 5e only: estimate bestelauto/vrachtauto per buurt (RDW + CBS 85236NED)
    python run_pipeline.py --capaciteitskaart # Phase 5f only: download capaciteitskaart (RNB+TenneT transformers) + link to buurten
    python run_pipeline.py --buurten        # Phase 6 only: generate enriched buurten CSVs
    python run_pipeline.py --gemeenten-potentie              # Municipality PV/wind potential CSVs (2023, 2024)
    python run_pipeline.py --gemeenten-potentie --overmorgen # ... also cross-check/fill with Over Morgen wind WFS

Phase 6 (buurten) merges CBS kerncijfers + Phase 3 verwarmingsinstallaties + Phase 5 ElaadNL
+ Phase 5b TVW + Phase 5c WarmteTransitie + Phase 5d solar + Phase 5e bedrijfswagens + Phase 5f
capaciteitskaart into a single CSV per year. Run it last so all source data is available. All
phase 5* datasets are optional: if they have not yet completed, buurten CSVs are generated
without those columns and can be regenerated later.

Phase 5e (bedrijfswagens) also feeds bestelautos_totaal/vrachtautos_totaal (summed to gemeente
level) into kerncijfers_gemeenten_<jaar>.csv via --gemeenten-potentie, alongside the CBS
85236NED validation columns — same "run before --buurten / --gemeenten-potentie" convention as
the sector-energy data.

Phase 5f (capaciteitskaart) is independent of buurten geometry for its two national outputs
(transformers.csv, transformer_links.csv — copied straight into data_Generic/ root, no
buurten-CSV merge needed) but, like bedrijfswagens, needs a geometry year for its buurt link
(capaciteitskaart_buurten_<jaar>_<datum>.csv, feeding voedingsgebied_id into --buurten).

Sector electricity/gas demand per SBI-group (CBS 82538NED, falls back to
Klimaatmonitor) is NOT a separate phase here: it is fetched live and merged
straight into kerncijfers_gemeenten_<jaar>.csv by --gemeenten-potentie, and
--buurten reads it back out of that file. Run --gemeenten-potentie before
--buurten if you want that data in the buurten CSV too.

Logs to stdout AND logs/<date>.log.
If the download step reports any failure the processing step is skipped
to protect the last known-good processed output.
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from datetime import date

from config import LOG_DIR, WARMTEATLAS_LAYERS
import download_sources
import process_features
import make_verwarmingsinstallaties_csv
import make_windturbines_csv
import make_elaadnl_csv
import make_tvw_csv
import make_warmtetransitie_csv
import make_solar_csv
import make_bedrijfswagens_csv
import make_capaciteitskaart_csv
import make_buurten_csv
import make_gemeenten_potentie_csv

TODAY = date.today().isoformat()


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"pipeline_{TODAY}.log"
    fmt = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    logger = logging.getLogger("pipeline")
    logger.info("Logging to %s", log_file)
    return logger


def run_download(log: logging.Logger) -> bool:
    log.info("--- Phase 1: Download sources ---")
    ok = download_sources.main()
    if ok:
        log.info("--- Phase 1 complete: all downloads succeeded ---")
    else:
        log.error("--- Phase 1 FAILED: one or more downloads failed (see warnings above) ---")
    return ok


def run_process(log: logging.Logger, force: bool = False) -> bool:
    log.info("--- Phase 2: Process and match WarmteAtlas features (%d layers) ---", len(WARMTEATLAS_LAYERS))
    if force:
        log.info("  --force: skipping cache checks, re-processing all layers")
    layer_results: dict[str, bool] = {}

    for layer in WARMTEATLAS_LAYERS:
        short = layer.replace("WarmteAtlas:", "")
        try:
            ok = process_features.process_layer(layer, force=force)
        except Exception as exc:
            log.error("Unhandled exception in %s: %s", short, exc, exc_info=True)
            ok = False
        layer_results[short] = ok

    failed = [k for k, v in layer_results.items() if not v]
    succeeded = len(layer_results) - len(failed)
    log.info("--- Phase 2 summary: %d succeeded, %d failed ---", succeeded, len(failed))
    if failed:
        log.warning("Failed layers: %s", ", ".join(failed))
    return len(failed) == 0


def run_verwarming(log: logging.Logger) -> bool:
    log.info("--- Phase 3: Parse verwarmingsinstallaties Excel ---")
    ok = make_verwarmingsinstallaties_csv.main()
    if ok:
        log.info("--- Phase 3 complete ---")
    else:
        log.error("--- Phase 3 FAILED ---")
    return ok


def run_windturbines(log: logging.Logger) -> bool:
    log.info("--- Phase 4: Download + match RIVM windturbines ---")
    ok = make_windturbines_csv.main()
    if ok:
        log.info("--- Phase 4 complete ---")
    else:
        log.error("--- Phase 4 FAILED ---")
    return ok


def run_elaadnl(log: logging.Logger) -> bool:
    log.info("--- Phase 5: Download ElaadNL EV prognoses (NL) ---")
    ok = make_elaadnl_csv.main()
    if ok:
        log.info("--- Phase 5 complete ---")
    else:
        log.error("--- Phase 5 FAILED ---")
    return ok


def run_warmtetransitie(log: logging.Logger) -> bool:
    log.info("--- Phase 5c: Download WarmteTransitie per-gebied (RVO ArcGIS) ---")
    ok = make_warmtetransitie_csv.main()
    if ok:
        log.info("--- Phase 5c complete ---")
    else:
        log.error("--- Phase 5c FAILED ---")
    return ok


def run_solar(log: logging.Logger) -> bool:
    log.info("--- Phase 5d: Download CBS Zonnestroom per buurt ---")
    ok = make_solar_csv.main()
    if ok:
        log.info("--- Phase 5d complete ---")
    else:
        log.error("--- Phase 5d FAILED ---")
    return ok


def run_tvw(log: logging.Logger) -> bool:
    log.info("--- Phase 5b: Match TVW_voortgang to buurten ---")
    ok = make_tvw_csv.main()
    if ok:
        log.info("--- Phase 5b complete ---")
    else:
        log.error("--- Phase 5b FAILED ---")
    return ok


def run_bedrijfswagens(log: logging.Logger) -> bool:
    log.info("--- Phase 5e: Estimate bestelauto/vrachtauto totals per buurt (RDW + CBS 85236NED) ---")
    ok = make_bedrijfswagens_csv.main()
    if ok:
        log.info("--- Phase 5e complete ---")
    else:
        log.error("--- Phase 5e FAILED ---")
    return ok


def run_capaciteitskaart(log: logging.Logger) -> bool:
    log.info("--- Phase 5f: Download capaciteitskaart (RNB+TenneT transformers) + link to buurten ---")
    ok = make_capaciteitskaart_csv.main()
    if ok:
        log.info("--- Phase 5f complete ---")
    else:
        log.error("--- Phase 5f FAILED ---")
    return ok


def run_buurten(log: logging.Logger) -> bool:
    log.info("--- Phase 6: Generate enriched buurten CSVs (CBS + verwarming + ElaadNL) ---")
    ok = make_buurten_csv.main()
    if ok:
        log.info("--- Phase 6 complete ---")
    else:
        log.error("--- Phase 6 FAILED ---")
    return ok


def run_gemeenten_potentie(log: logging.Logger, met_overmorgen: bool = False) -> bool:
    log.info("--- Municipality PV/wind potential CSVs (2023, 2024) ---")
    ok = make_gemeenten_potentie_csv.main(met_overmorgen=met_overmorgen)
    if ok:
        log.info("--- Municipality potential complete ---")
    else:
        log.error("--- Municipality potential FAILED ---")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description="WarmteAtlas data pipeline")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--download",     action="store_true", help="Phase 1 only: download raw data")
    group.add_argument("--process",      action="store_true", help="Phase 2 only: match WarmteAtlas to CBS")
    group.add_argument("--verwarming",   action="store_true", help="Phase 3 only: parse verwarmingsinstallaties Excel")
    group.add_argument("--windturbines", action="store_true", help="Phase 4 only: download + match RIVM windturbines")
    group.add_argument("--elaadnl",      action="store_true", help="Phase 5 only: download ElaadNL EV prognoses (NL)")
    group.add_argument("--warmtetransitie", action="store_true", help="Phase 5c only: download WarmteTransitie per-gebied from RVO ArcGIS")
    group.add_argument("--tvw",          action="store_true", help="Phase 5b only: match TVW_voortgang to buurten")
    group.add_argument("--solar",        action="store_true", help="Phase 5d only: download CBS Zonnestroom per buurt (86044NED etc.)")
    group.add_argument("--bedrijfswagens", action="store_true", help="Phase 5e only: estimate bestelauto/vrachtauto totals per buurt (RDW + CBS 85236NED)")
    group.add_argument("--capaciteitskaart", action="store_true", help="Phase 5f only: download capaciteitskaart (RNB+TenneT transformers) + link to buurten")
    group.add_argument("--buurten",      action="store_true", help="Phase 6 only: generate enriched buurten CSVs")
    group.add_argument("--gemeenten-potentie", action="store_true", help="Municipality PV/wind potential CSVs (2023, 2024); opt-in, not part of the full run")
    parser.add_argument("--overmorgen", action="store_true", help="With --gemeenten-potentie: also download + cross-check + fill using the Over Morgen wind WFS")
    parser.add_argument("--force", action="store_true", help="Re-process even if output CSVs already exist for the current raw data")
    args = parser.parse_args()

    log = _setup_logging()
    t0 = time.monotonic()
    log.info("=== WarmteAtlas pipeline starting (date: %s) ===", TODAY)

    no_flag = not (
        args.download or args.process or args.verwarming
        or args.windturbines or args.elaadnl or args.tvw
        or args.warmtetransitie or args.solar or args.bedrijfswagens
        or args.capaciteitskaart or args.buurten or args.gemeenten_potentie
    )
    do_download          = no_flag or args.download
    do_process           = no_flag or args.process
    do_verwarming        = no_flag or args.verwarming
    do_windturbines      = no_flag or args.windturbines
    do_elaadnl           = no_flag or args.elaadnl
    do_tvw               = no_flag or args.tvw
    do_warmtetransitie   = no_flag or args.warmtetransitie
    do_solar             = no_flag or args.solar
    do_bedrijfswagens    = no_flag or args.bedrijfswagens
    do_capaciteitskaart  = no_flag or args.capaciteitskaart
    do_buurten           = no_flag or args.buurten

    exit_code = 0

    if do_download:
        download_ok = run_download(log)
        if not download_ok:
            log.error(
                "Download failures detected. "
                "Skipping processing to protect existing processed output."
            )
            if do_process or do_buurten:
                exit_code = 1
                log.info("Run with --process or --buurten to force-run with existing raw data.")
    else:
        download_ok = True  # single-phase mode: assume raw data is present

    if do_process and download_ok:
        process_ok = run_process(log, force=args.force)
        if not process_ok:
            exit_code = 1

    if do_verwarming:
        verwarming_ok = run_verwarming(log)
        if not verwarming_ok:
            exit_code = 1

    if do_windturbines:
        windturbines_ok = run_windturbines(log)
        if not windturbines_ok:
            exit_code = 1

    if do_elaadnl:
        elaadnl_ok = run_elaadnl(log)
        if not elaadnl_ok:
            exit_code = 1

    if do_tvw:
        tvw_ok = run_tvw(log)
        if not tvw_ok:
            exit_code = 1

    if do_warmtetransitie:
        warmtetransitie_ok = run_warmtetransitie(log)
        if not warmtetransitie_ok:
            exit_code = 1

    if do_solar:
        solar_ok = run_solar(log)
        if not solar_ok:
            exit_code = 1

    if do_bedrijfswagens:
        bedrijfswagens_ok = run_bedrijfswagens(log)
        if not bedrijfswagens_ok:
            exit_code = 1

    if do_capaciteitskaart:
        capaciteitskaart_ok = run_capaciteitskaart(log)
        if not capaciteitskaart_ok:
            exit_code = 1

    if do_buurten and download_ok:
        buurten_ok = run_buurten(log)
        if not buurten_ok:
            exit_code = 1

    # Opt-in only: never runs as part of the default full pipeline.
    if args.gemeenten_potentie:
        gemeenten_potentie_ok = run_gemeenten_potentie(log, met_overmorgen=args.overmorgen)
        if not gemeenten_potentie_ok:
            exit_code = 1

    elapsed = time.monotonic() - t0
    status = "SUCCESS" if exit_code == 0 else "FAILED"
    log.info("=== Pipeline %s in %.0fs ===", status, elapsed)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
