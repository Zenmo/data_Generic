# %%
import time
import requests
import pandas as pd
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# %%
API_KEY = "9a93a6982957f588aead6d46342cdf0fa3ab4085f4de7c9f887004895d718bec"
BASE_URL = "https://api.ned.nl/v1/utilizations"
HEADERS = {"X-AUTH-TOKEN": API_KEY, "accept": "application/ld+json"}

# %%
BASE_PARAMS = {
    "point": 0,
    "type": 27,                 # ElectricityMix
    "activity": 1,              # Providing
    "classification": 2,        # Current
    "granularity": 5,           # Hour:5; Quarter-hour:4;
    "granularitytimezone": 1,   # CET; otherwise aggregated granularities can return nothing :contentReference[oaicite:2]{index=2}
    "validfrom[after]": "2023-01-01",
    "validfrom[strictly_before]": "2024-01-01",
}

SLEEP_SECONDS = 0.25

# %%
def extract_members(data):
    if isinstance(data, list):
        return data
    return data.get("hydra:member", [])

def fetch_factor():
    rows = []
    url = BASE_URL
    params = BASE_PARAMS

    with requests.Session() as s:
        s.headers.update(HEADERS)

        while True:
            r = s.get(url, params=params, timeout=60)
            r.raise_for_status()
            data = r.json()

            for it in extract_members(data):
                rows.append({
                    "validfrom_utc": it.get("validfrom"),
                    "co2_factor_kg_per_kwh": it.get("emissionfactor"),
                })

            nxt = data.get("hydra:view", {}).get("hydra:next") if isinstance(data, dict) else None
            if not nxt:
                break

            url = "https://api.ned.nl" + nxt
            params = None

    return pd.DataFrame(rows)

# %%
df = fetch_factor()
df["validfrom_utc"] = pd.to_datetime(df["validfrom_utc"], utc=True, errors="coerce")
df["co2_factor_kg_per_kwh"] = pd.to_numeric(df["co2_factor_kg_per_kwh"], errors="coerce")
df = df.sort_values("validfrom_utc").reset_index(drop=True)

out_path = "ned_co2_factor_hourly_nl_2023.csv"
df.to_csv(out_path, index=False)
print(f"Saved {len(df):,} rows to {out_path}")
# %%
