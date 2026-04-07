# %%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from windpowerlib import ModelChain, WindTurbine
from windpowerlib import data as wt

import xarray as xr
import numpy as np

# %%
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_Geldermalsen_2025_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_Geldermalsen_2024_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_Geldermalsen_2023_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_HoekVanHolland_2025_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_HoekVanHolland_2024_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_HoekVanHolland_2023_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_Heibloem_2025_reanalysis-era5-single-levels-timeseries.csv"
#INPUT_WEATHER_FILE = "ERA5_Windspeeds_Heibloem_2024_reanalysis-era5-single-levels-timeseries.csv"
INPUT_WEATHER_FILE = "ERA5_Windspeeds_Heibloem_2023_reanalysis-era5-single-levels-timeseries.csv"

TARGET_START = pd.Timestamp("2022-12-31 23:00:00", tz="UTC")
TARGET_END = pd.Timestamp("2023-12-31 22:45:00", tz="UTC")
times = pd.date_range("2023-01-01", "2024-01-01", freq="15min", tz="Europe/Amsterdam")
times = times[:-1]  # drop last timestamp to have 8784 values

#ds = xr.open_dataset("data_ERA5_Windspeeds_Heibloem_2025.grib", engine="cfgrib")
df = pd.read_csv(INPUT_WEATHER_FILE)
ds = df[["valid_time", "u100", "v100", "t2m", "sp"]].copy()
ds.columns = ["time", "u100", "v100", "t2m", "sp"]

ds["time"] = pd.to_datetime(ds["time"], utc=True, errors="coerce")
ds = ds.dropna(subset=["time"]).sort_values("time")

for col in ["u100", "v100", "t2m", "sp"]:
    ds[col] = pd.to_numeric(ds[col], errors="coerce")

ds = ds.set_index("time")
ds_15min = (
    ds[["u100", "v100", "t2m", "sp"]]
    .resample("15min")
    .interpolate(method="time")
)

ds_15min = ds_15min.loc[TARGET_START:TARGET_END].copy()

u = ds_15min["u100"]
v = ds_15min["v100"]
T = ds_15min["t2m"]
p = ds_15min["sp"]

speed = np.sqrt(u**2 + v**2)
speed.name = "wind_speed_100m"

# For a single point, select nearest lat/lon:
# point_speed = speed.sel(latitude=lat, longitude=lon, method="nearest")
# point_T = T.sel(latitude=lat, longitude=lon, method="nearest")
# point_p = p.sel(latitude=lat, longitude=lon, method="nearest")
# wind_speed_series = point_speed.to_series()
# temp_series = point_T.to_series()
# pressure_series = point_p.to_series()

# example data (replace with your arrays)
wind_speed = speed.to_numpy(dtype=float)   # ← not np.array(…)
temp_k = T.to_numpy(dtype=float)
pressure = p.to_numpy(dtype=float)


# IMPORTANT: create a MultiIndex columns where second level is the measurement height
hub_height = 100  # m
cols = pd.MultiIndex.from_tuples(
    [('wind_speed', hub_height),
     ('temperature', 2),
     ('pressure', 0),
     ('roughness_length', 0)],
    names=['variable', 'height']
)

data = np.column_stack([wind_speed, temp_k, pressure, np.full_like(wind_speed, 0.2)])
weather_df = pd.DataFrame(data, index=times, columns=cols)

# --------------------------------------------------
# 2) Define a wind turbine
#    (built-in example from the “turbine library”)
# --------------------------------------------------
turbine = WindTurbine(
    turbine_type='E-126/4200',       # Enercon 4.2 MW
    nominal_power=4_200_000,        # nominal power [W]
    hub_height=135,           # hub height [m] 
    rotor_diameter=127         # rotor diameter [m]
)

# --------------------------------------------------
# 3) Set up a ModelChain and run it
# --------------------------------------------------
mc = ModelChain(turbine).run_model(weather_df)

# Electricity production (per turbine) in W
production = mc.power_output
print(production.head())
print(production.sum() / 1e6, "MWh")
print(production.sum() / 1e6 / 4.2, "full load hours")


# write to csv
production_normalized = production / 4.2e6  # 
production_normalized.to_csv("data_Heibloem_2023_quarterhourly_normalized_production.csv", header=True)
# %%
