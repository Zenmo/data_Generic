# %%
"""
Download ERA5 point data for one site and save it as hourly CSV.

Important:
ERA5 point time series are hourly. This script therefore downloads hourly data
for the target period plus one extra day so that a downstream script can
interpolate safely to 15-minute resolution and still obtain the last three
quarter-hours of the target period.
"""

import calendar
import cdsapi

# Watthub lat/lon:
lat = 51.86784
lon = 5.323151

# Hoek van Holland lat/lon:
#lat = 51.9803
#lon = 4.1287

# Windpark Neer
#lat = 51.302572
#lon = 5.941777

client = cdsapi.Client()

for month in range(1, 13):
    days_in_month = calendar.monthrange(2025, month)[1]
    request = {
        "product_type": ["reanalysis"],
        "variable": [
            "2m_temperature",
            "surface_pressure",
            "100m_u_component_of_wind",
            "100m_v_component_of_wind",
        ],
        "year": ["2025"],
        "month": [f"{month:02d}"],
        "day": [f"{d:02d}" for d in range(1, days_in_month + 1)],
        "time": [f"{h:02d}:00" for h in range(24)],
        "data_format": "grib",
        "download_format": "zip",
        # small box, not a point
        "area": [lat + 0.125, lon - 0.125, lat - 0.125, lon + 0.125],
    }
    client.retrieve("reanalysis-era5-single-levels", request).download()
# %%
