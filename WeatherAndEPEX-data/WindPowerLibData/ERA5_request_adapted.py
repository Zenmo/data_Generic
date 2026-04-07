# %%
import pandas as pd
import cdsapi

# %%
# -----------------------------------------------------------------------------
# Site selection
# -----------------------------------------------------------------------------
# Watthub
# lat = 51.86784
# lon = 5.323151

# Hoek van Holland
# lat = 51.9803
# lon = 4.1287

# Windpark Neer / Heibloem
lat = 51.302572
lon = 5.941777

# -----------------------------------------------------------------------------
# Target period for the final quarter-hour series
# -----------------------------------------------------------------------------
TARGET_START = pd.Timestamp("2024-12-31 23:00:00", tz="UTC")
TARGET_END = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")

# Request one extra day so interpolation can fill the last 45 minutes.
DOWNLOAD_START = TARGET_START.strftime("%Y-%m-%d")
DOWNLOAD_END = (TARGET_END + pd.Timedelta(hours=1)).normalize().strftime("%Y-%m-%d")

# -----------------------------------------------------------------------------
# ERA5 request
# -----------------------------------------------------------------------------
dataset = "reanalysis-era5-single-levels-timeseries"
request = {
    "variable": [
        "2m_temperature",
        "surface_pressure",
        "100m_u_component_of_wind",
        "100m_v_component_of_wind",
    ],
    "location": {
        "latitude": lat,
        "longitude": lon,
    },
    "date": [f"{DOWNLOAD_START}/{DOWNLOAD_END}"],
    "data_format": "csv",
    "download_format": "zip"
}


client = cdsapi.Client()
client.retrieve(dataset, request).download()

print(f"Downloaded range: {DOWNLOAD_START} to {DOWNLOAD_END} (UTC)")
print(
    "This file is hourly. Use WindPowerTimeSeries_adapted.py to build the "
    "quarter-hour turbine production series."
)
# %%
