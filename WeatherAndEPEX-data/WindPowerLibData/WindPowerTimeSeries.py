# %%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from windpowerlib import ModelChain, WindTurbine
from windpowerlib import data as wt

import xarray as xr
import numpy as np

# Watthub lat/lon:
#lat = 51.86784
#lon = 5.323151

# Hoek van Holland lat/lon:
#lat = 51.9803
#lon = 4.1287

# Windpark Neer
lat = 51.302572
lon = 5.941777

ds = xr.open_dataset("data_ERA5_Windspeeds_Heibloem_2025.grib", engine="cfgrib")

# $$
u = ds["u100"]
v = ds["v100"]
T = ds["t2m"]
p = ds["sp"]

# %%

speed = np.sqrt(u**2 + v**2)
speed.name = "wind_speed_100m"

# For a single point, select nearest lat/lon:
point_speed = speed.sel(latitude=lat, longitude=lon, method="nearest")
point_T = T.sel(latitude=lat, longitude=lon, method="nearest")
point_p = p.sel(latitude=lat, longitude=lon, method="nearest")
wind_speed_series = point_speed.to_series()
temp_series = point_T.to_series()
pressure_series = point_p.to_series()
# %%

times = pd.date_range("2025-01-01", "2026-01-01", freq="h", tz="UTC")
times = times[:-1]  # drop last timestamp to have 8784 values

# %%
# example data (replace with your arrays)
wind_speed = wind_speed_series.to_numpy(dtype=float)   # ← not np.array(…)
temp_k = temp_series.to_numpy(dtype=float)
pressure = pressure_series.to_numpy(dtype=float)


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

# %%
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


# %%
# write to csv
production_normalized = production / 4.2e6  # 
production_normalized.to_csv("data_Heibloem_2025_normalized_production.csv", header=True)
# %%
