# %%
"""
Read hourly ERA5 point data, interpolate the weather inputs to 15-minute
resolution, run windpowerlib, and save normalized turbine production.

The 15-minute output is interpolated from hourly ERA5 data. It is therefore a
quarter-hour series derived from ERA5, not native 15-minute ERA5 data.
"""

import re
import numpy as np
import pandas as pd
from windpowerlib import ModelChain, WindTurbine

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
INPUT_WEATHER_FILE = "era5_point_hourly_2025_plus1day.csv"
INTERPOLATED_WEATHER_OUTPUT = "era5_point_weather_15min_2025.csv"
PRODUCTION_OUTPUT = "wind_turbine_normalized_production_15min_2025.csv"

TARGET_START = pd.Timestamp("2025-01-01 00:00:00", tz="UTC")
TARGET_END = pd.Timestamp("2025-12-31 23:45:00", tz="UTC")

ROUGHNESS_LENGTH_M = 0.2

TURBINE = {
    "turbine_type": "E-126/4200",
    "nominal_power": 4_200_000,  # W
    "hub_height": 135,            # m
    "rotor_diameter": 127,        # m
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _normalize_column_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def _find_column(df: pd.DataFrame, exact_candidates, token_candidates):
    normalized_to_original = {
        _normalize_column_name(col): col for col in df.columns
    }

    for candidate in exact_candidates:
        candidate_norm = _normalize_column_name(candidate)
        if candidate_norm in normalized_to_original:
            return normalized_to_original[candidate_norm]

    for normalized, original in normalized_to_original.items():
        if all(token in normalized for token in token_candidates):
            return original

    raise KeyError(
        f"Could not find a column matching {exact_candidates}. "
        f"Available columns: {list(df.columns)}"
    )


def load_era5_timeseries_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)

    time_col = _find_column(
        df,
        exact_candidates=["date", "time", "valid_time", "datetime", "analysis_time"],
        token_candidates=["time"],
    )

    u_col = _find_column(
        df,
        exact_candidates=[
            "u100",
            "100m_u_component_of_wind",
            "u_component_of_wind_100m",
            "u_component_of_wind_at_100m",
        ],
        token_candidates=["u", "wind", "100"],
    )
    v_col = _find_column(
        df,
        exact_candidates=[
            "v100",
            "100m_v_component_of_wind",
            "v_component_of_wind_100m",
            "v_component_of_wind_at_100m",
        ],
        token_candidates=["v", "wind", "100"],
    )
    t_col = _find_column(
        df,
        exact_candidates=["t2m", "2m_temperature", "temperature_2m"],
        token_candidates=["temperature", "2"],
    )
    p_col = _find_column(
        df,
        exact_candidates=["sp", "surface_pressure"],
        token_candidates=["pressure"],
    )

    out = df[[time_col, u_col, v_col, t_col, p_col]].copy()
    out.columns = ["time", "u100", "v100", "t2m", "sp"]

    out["time"] = pd.to_datetime(out["time"], utc=True, errors="coerce")
    out = out.dropna(subset=["time"]).sort_values("time")

    for col in ["u100", "v100", "t2m", "sp"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=["u100", "v100", "t2m", "sp"])
    out = out.set_index("time")
    out = out[~out.index.duplicated(keep="first")]

    return out


# -----------------------------------------------------------------------------
# 1) Load hourly ERA5 point data
# -----------------------------------------------------------------------------
meteo_hourly = load_era5_timeseries_csv(INPUT_WEATHER_FILE)

# -----------------------------------------------------------------------------
# 2) Interpolate meteorology to 15-minute resolution
#    Interpolate u and v first, then calculate wind speed.
# -----------------------------------------------------------------------------
meteo_15min = (
    meteo_hourly[["u100", "v100", "t2m", "sp"]]
    .resample("15min")
    .interpolate(method="time")
)

meteo_15min = meteo_15min.loc[TARGET_START:TARGET_END].copy()
meteo_15min["wind_speed_100m"] = np.hypot(meteo_15min["u100"], meteo_15min["v100"])

# Save interpolated weather for inspection / reuse
meteo_15min.to_csv(INTERPOLATED_WEATHER_OUTPUT, index_label="time_utc")

# -----------------------------------------------------------------------------
# 3) Build windpowerlib weather DataFrame
#    Units expected by windpowerlib:
#    - wind speed in m/s
#    - temperature in K
#    - pressure in Pa
#    - roughness length in m
# -----------------------------------------------------------------------------
weather_columns = pd.MultiIndex.from_tuples(
    [
        ("wind_speed", 100),
        ("temperature", 2),
        ("pressure", 0),
        ("roughness_length", 0),
    ],
    names=["variable", "height"],
)

weather_df = pd.DataFrame(
    data=np.column_stack(
        [
            meteo_15min["wind_speed_100m"].to_numpy(dtype=float),
            meteo_15min["t2m"].to_numpy(dtype=float),
            meteo_15min["sp"].to_numpy(dtype=float),
            np.full(len(meteo_15min), ROUGHNESS_LENGTH_M, dtype=float),
        ]
    ),
    index=meteo_15min.index,
    columns=weather_columns,
)

# -----------------------------------------------------------------------------
# 4) Wind turbine and model chain
# -----------------------------------------------------------------------------
turbine = WindTurbine(**TURBINE)

mc = ModelChain(
    turbine,
    density_correction=True,
)
mc.run_model(weather_df)

production_w = mc.power_output.astype(float).clip(lower=0, upper=turbine.nominal_power)
normalized_production = (production_w / turbine.nominal_power).clip(lower=0, upper=1)

results = pd.DataFrame(
    {
        "power_W": production_w,
        "normalized_production": normalized_production,
        "energy_MWh_15min": production_w * 0.25 / 1e6,
    },
    index=weather_df.index,
)

results.to_csv(PRODUCTION_OUTPUT, index_label="time_utc")

annual_energy_mwh = results["energy_MWh_15min"].sum()
full_load_hours = annual_energy_mwh / (turbine.nominal_power / 1e6)

print(f"Saved interpolated weather to: {INTERPOLATED_WEATHER_OUTPUT}")
print(f"Saved normalized quarter-hour production to: {PRODUCTION_OUTPUT}")
print(f"Number of 15-minute steps: {len(results)}")
print(f"Annual energy: {annual_energy_mwh:.2f} MWh")
print(f"Full-load hours: {full_load_hours:.2f} h")
print(results.head())
# %%
