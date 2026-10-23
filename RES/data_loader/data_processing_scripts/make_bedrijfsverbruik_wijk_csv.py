"""Gemeten bedrijfsverbruik per wijk (Klimaatmonitor): ijkpunt voor de verdeling over buurten.

Klimaatmonitor publiceert het verbruik van bedrijven en instellingen per sector (SBI-letter) alleen
per gemeente en hoger, maar het TOTAAL ook per wijk:
  vbrze_tot_wijk        totaal elektriciteitsverbruik bedrijven en instellingen, geleverd via het
                        openbaar net (kWh), wijkindeling 2024
  vbrzg_tot_incl_sbid   totaal aardgasverbruik bedrijven/instellingen, INCLUSIEF SBI D (m³)
make_buurten_csv.py (_kalibreer_elektriciteit_op_wijk) schaalt de elektriciteit per buurt zodat de
som per wijk gelijk is aan dit gemeten totaal, terwijl de gemeentetotalen per sector gelijk blijven.
Gas wordt (nog) niet gekalibreerd: het wijkcijfer bevat SBI D (energievoorziening), het model niet.

Getoetst voor de Drechtsteden (2023): de samenhang tussen model en gemeten wijktotaal was 0,44 bij
verdeling op vestigingen en 0,88 bij verdeling op BAG-m²; deze stap maakt de wijktotalen gelijk.

Vereist een Klimaatmonitor API-key in secrets.local.json (zie make_energieverbruik_sector_csv.py).

Output (processed_data_from_loader/):
  bedrijfsverbruik_wijken_<datum>.csv   jaar, wijkcode (WKggggww), gemeentecode, elec_kwh,
                                        gas_incl_sbid_m3 (leeg = niet gepubliceerd)

Run standalone:  python make_bedrijfsverbruik_wijk_csv.py [2023 [2024]]
Or via:          python run_pipeline.py --wijkverbruik
"""

import logging
import sys
from datetime import date

import pandas as pd

from config import OUTPUT_SEPARATOR, PROCESSED_DIR
from make_energieverbruik_sector_csv import _km_fetch_variable, _laad_klimaatmonitor_apikey

log = logging.getLogger(__name__)

VARIABELEN = {"elec_kwh": "vbrze_tot_wijk", "gas_incl_sbid_m3": "vbrzg_tot_incl_sbid"}
STANDAARD_JAREN = [2023, 2024]


def main(jaren: list[int] | None = None) -> bool:
    jaren = jaren or STANDAARD_JAREN
    apikey = _laad_klimaatmonitor_apikey()
    if apikey is None:
        return False
    headers = {"apikey": apikey}

    frames = []
    for jaar in jaren:
        per_var = []
        for kolom, var in VARIABELEN.items():
            df = _km_fetch_variable(headers, var, jaar, geolevel="wijk")
            if df is None or df.empty:
                log.warning("  %s (%d): geen wijkcijfers", var, jaar)
                continue
            df = df[df["level"].eq("wijk")]
            s = df.set_index("gebiedscode")["waarde"].rename(kolom)
            per_var.append(s)
            log.info("  %s (%d): %d wijken met een waarde", var, jaar, int(s.notna().sum()))
        if not per_var:
            continue
        w = pd.concat(per_var, axis=1).reset_index().rename(columns={"index": "gebiedscode"})
        w["gebiedscode"] = w["gebiedscode"].astype(str).str.zfill(6)
        w.insert(0, "wijkcode", "WK" + w["gebiedscode"])
        w.insert(1, "gemeentecode", "GM" + w["gebiedscode"].str[:4])
        w.insert(0, "jaar", jaar)
        frames.append(w.drop(columns="gebiedscode"))

    if not frames:
        log.error("Geen wijkcijfers opgehaald")
        return False
    uit = pd.concat(frames, ignore_index=True)
    pad = PROCESSED_DIR / f"bedrijfsverbruik_wijken_{date.today().isoformat()}.csv"
    tmp = pad.with_suffix(".tmp.csv")
    uit.to_csv(tmp, index=False, sep=OUTPUT_SEPARATOR, encoding="utf-8")
    tmp.replace(pad)
    log.info("Geschreven: %s (%d rijen)", pad.name, len(uit))
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", stream=sys.stdout)
    sys.exit(0 if main([int(a) for a in sys.argv[1:]] or None) else 1)
