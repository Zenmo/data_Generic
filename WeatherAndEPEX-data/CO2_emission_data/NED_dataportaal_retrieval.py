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

# Electricity mix components listed by NED (commonly used to reconstruct totals)
MIX_TYPES = [1, 2, 18, 19, 20, 21, 22, 25, 26, 35]

# %%
BASE_PARAMS = {
    "point": 0,
    "activity": 1,              # Providing
    "classification": 2,        # Current (this is the key change)
    "granularity": 5,           # Hour
    "granularitytimezone": 1,   # CET; otherwise aggregated granularities can return nothing :contentReference[oaicite:2]{index=2}
}

SLEEP_SECONDS = 0.25

# %%
def year(year: int):
    d = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    while d < end:
        nxt = date(d.year + (d.month == 12), 1 if d.month == 12 else d.month + 1, 1)
        yield d, min(nxt, end)
        d = nxt

def extract_members(data):
    if isinstance(data, list):
        return data
    return data.get("hydra:member", [])

def fetch_window(type_id: int, start_d: date, end_d: date):
    params = dict(BASE_PARAMS)
    params["type"] = type_id
    params["validfrom[after]"] = start_d.isoformat()
    params["validfrom[strictly_before]"] = end_d.isoformat()

    items = []
    url = BASE_URL
    p = params
    while True:
        r = requests.get(url, headers=HEADERS, params=p, timeout=60)
        r.raise_for_status()
        data = r.json()
        items.extend(extract_members(data))

        nxt = None
        if isinstance(data, dict):
            nxt = data.get("hydra:view", {}).get("hydra:next")
        if not nxt:
            break

        url = "https://api.ned.nl" + nxt
        p = None
        time.sleep(SLEEP_SECONDS)

    return items

# %%
rows = []
for start_d, end_d in year(2023):
    for t in MIX_TYPES:
        items = fetch_window(t, start_d, end_d)
        for it in items:
            rows.append({
                "type": t,
                "validfrom_utc": it.get("validfrom"),
                "volume_kwh": it.get("volume"),
                "emission_kg": it.get("emission"),
            })
        time.sleep(SLEEP_SECONDS)

# %%
df = pd.DataFrame(rows)
if df.empty:
    raise RuntimeError("Still no data returned. Run the 1-day proof for a single type (e.g., type=2) and share the record count.")

df["validfrom_utc"] = pd.to_datetime(df["validfrom_utc"], utc=True, errors="coerce")
df["volume_kwh"] = pd.to_numeric(df["volume_kwh"], errors="coerce")
df["emission_kg"] = pd.to_numeric(df["emission_kg"], errors="coerce")

# %%
# Hourly mix factor = sum(emission)/sum(volume)
g = df.groupby("validfrom_utc", as_index=False).agg(
    volume_kwh=("volume_kwh", "sum"),
    emission_kg=("emission_kg", "sum"),
)
g["co2_factor_kg_per_kwh"] = g["emission_kg"] / g["volume_kwh"]
g["co2_factor_g_per_kwh"] = g["co2_factor_kg_per_kwh"] * 1000.0
g = g.sort_values("validfrom_utc").reset_index(drop=True)

# %%
out_path = "ned_co2_hourly_nl_2023.csv"
g.to_csv(out_path, index=False)
print(f"Saved {len(g):,} hourly rows to {out_path}")
# %%
