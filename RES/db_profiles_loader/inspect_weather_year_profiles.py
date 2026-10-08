"""Inspect wind/solar/demand differences across ETM weather years.

Fetches all 4 weather years (default/1987/1997/2004) live via
fetch_etm_weather_years.fetch_weather_year() - a fresh Session.new(area_code="nl2023")
scenario, not a copy of an existing published scenario, so neither of the two bugs
documented in NOTES.md (custom-curve override, copy_with_preset() dropping values)
applies here. Cached locally (RAW_CACHE) after the first fetch.

Two normalization modes, on request - the original "shape" normalization (production
divided by its own peak hour, demand divided by its own annual sum) always maps every
weather year onto the same [0,1]-ish range by construction, which hides genuine
magnitude differences between years - a good property for feeding the AnyLogic model
(which needs a shape independent of any one year's peak), a bad one for comparing years
directly, which is the entire point of this viewer:

  - "shape" - matches db_profiles.xlsx exactly (production / max, demand / annual sum).
  - "physical" - production divided by installed capacity (a genuine capacity factor,
    0-1 but NOT forced to hit 1 - reveals whether one year's wind was truly windier),
    demand divided by household/building count (MW per household/building, not forced
    to sum to 1 - reveals whether total demand differs, not just its shape). Capacity
    and household/building counts are confirmed constant across weather years (they're
    scenario/end-year assumptions, not weather-dependent), so dividing by them doesn't
    introduce a spurious difference of its own. Columns without an obvious physical
    denominator (industry, logistics) fall back to raw MW in this mode.

Also checked before trusting any of this: capacity_of_energy_battery_solar_pv_solar_
radiation / capacity_of_energy_battery_wind_turbine_inland / the grid-scale battery
capacities are all 0 MW in this scenario's default configuration, and the wind/solar
technologies used here (utility/buildings/households PV, inland/offshore wind) all have
0% curtailment_of_* assumptions - so energy_power_wind_turbine_inland.output etc. are
genuine gross production curves, not something already smoothed by storage dispatch or
de-rated by a curtailment assumption. If this scenario is ever reconfigured with nonzero
battery/curtailment settings, that would need rechecking.

Computes min/max/mean/median per (weather year, column, mode), and builds a
self-contained interactive HTML viewer: pick any wind/solar/demand column from a
grouped dropdown, toggle shape vs. physical-units normalization, toggle hourly vs. load
duration curve, all 4 weather years overlaid.

Deliverables:
- weather_year_profiles_stats.csv   - stats table (also printed to stdout)
- weather_year_profiles_viewer.html - standalone Plotly viewer (works offline)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import fetch_etm_weather_years as fetch_mod
from inspect_nbnl_prices import WEATHER_YEAR_COLORS, WEATHER_YEAR_LABELS

RAW_CACHE = "weather_year_profiles_raw_cache.csv"
DENOMINATOR_CACHE = "weather_year_profiles_denominators_cache.json"
STATS_CSV = "weather_year_profiles_stats.csv"
VIEWER_HTML = "weather_year_profiles_viewer.html"

ALL_WEATHER_YEARS = ["default", "1987", "1997", "2004"]
N_HOURS = 8760
NORM_MODES = ["shape", "physical"]

# wind_e_prod_normalized_hvh/_heibloem and solar_e_prod_eastwest15deg_normalized are
# deliberately excluded here - confirmed identical to wind_e_prod_normalized /
# solar_e_prod_south35deg_normalized respectively (ETM has no per-location wind data or
# per-orientation solar split, see NOTES.md), so showing them as separate dropdown
# entries would just be confusing duplicates of the same line.
SERIES_GROUPS: dict[str, dict[str, str]] = {
    "Wind": {
        "wind_e_prod_normalized": "Wind production - inland",
        "wind_e_prod_normalized_offshore": "Wind production - offshore",
    },
    "Solar": {
        "solar_e_prod_south35deg_normalized": "Solar production - ETM has no orientation split",
    },
    "Household & building demand": {
        "house_e_demand_other": "Household electricity demand (other)",
        "house_h_demand_hot_water": "Household hot water heat demand",
        "house_h_demand_space_heating": "Household space heating demand",
        "house_cooking_demand": "Household cooking demand",
        "building_e_demand_other": "Building electricity demand (other)",
        "building_h_demand": "Building heat demand",
    },
    "Industry electricity demand": {
        "industry_steel_e_demand": "Steel",
        "industry_other_e_demand": "Other industry",
    },
    "Industry heat demand": {
        "industry_steel_h_demand": "Steel",
        "industry_other_h_demand": "Other industry (aggregate of the 8 below)",
        "industry_chemicals_fertilizers_h_demand": "Chemicals: fertilizers",
        "industry_chemicals_other_h_demand": "Chemicals: other",
        "industry_chemicals_refineries_h_demand": "Chemicals: refineries",
        "industry_metal_aluminium_h_demand": "Metal: aluminium",
        "industry_metal_other_metals_h_demand": "Metal: other metals",
        "industry_other_non_specified_h_demand": "Non-specified industry",
        "industry_food_h_demand": "Food",
        "industry_paper_h_demand": "Paper",
    },
    "Logistics": {
        "logistics_fleet_e_hgv": "HGV electricity demand",
    },
}
ALL_SERIES = [col for group in SERIES_GROUPS.values() for col in group]

# Physical-unit denominators. "capacity" entries list the ETM input keys to sum for the
# installed-capacity denominator (verified constant across weather years - these are
# scenario/end-year assumptions, not weather-dependent); "households"/"buildings" use
# the fixed counts fetched alongside. Columns not listed here have no single obvious
# physical denominator (industry/logistics - tonnes of steel? fleet size? not modeled
# as a simple ETM input) and fall back to raw MW in "physical" mode.
PHYSICAL_DENOMINATORS: dict[str, tuple[str, list[str] | None]] = {
    "wind_e_prod_normalized": ("capacity", ["capacity_of_energy_power_wind_turbine_inland"]),
    "wind_e_prod_normalized_offshore": ("capacity", ["capacity_of_energy_power_wind_turbine_offshore"]),
    "solar_e_prod_south35deg_normalized": (
        "capacity",
        [
            "capacity_of_energy_power_solar_pv_solar_radiation",
            "capacity_of_buildings_solar_pv_solar_radiation",
            "capacity_of_households_solar_pv_solar_radiation",
        ],
    ),
    "house_e_demand_other": ("households", None),
    "house_h_demand_hot_water": ("households", None),
    "house_h_demand_space_heating": ("households", None),
    "house_cooking_demand": ("households", None),
    "building_e_demand_other": ("buildings", None),
    "building_h_demand": ("buildings", None),
}
PHYSICAL_UNIT_LABELS = {
    "capacity": "capacity factor (output / installed MW)",
    "households": "MW per household",
    "buildings": "MW per building",
    "raw": "raw MW (no per-unit denominator for this series)",
}


def fetch_denominators() -> dict[str, float]:
    """Fetch the fixed capacity/household/building denominators once. Cached locally -
    delete DENOMINATOR_CACHE to force a refresh (e.g. if the scenario config changes)."""
    if os.path.exists(DENOMINATOR_CACHE):
        with open(DENOMINATOR_CACHE) as f:
            return json.load(f)

    from pyetm import Session

    s = Session.new(area_code=fetch_mod.AREA_CODE, end_year=fetch_mod.END_YEAR)
    keys = list(s.inputs.keys())

    battery_keys = [k for k in keys if k.startswith("capacity_of_energy_battery_") or k.startswith("capacity_of_energy_flexibility_") and "batter" in k]
    battery_capacities = {k: s.inputs[k].default for k in battery_keys}
    if any(v for v in battery_capacities.values()):
        fetch_mod.log.warning(
            "Non-zero battery capacity found (%s) - the 'physical' normalization mode "
            "assumes wind/solar .output curves are gross production unaffected by "
            "storage, which was only verified when these were all 0 MW. Re-verify if "
            "this scenario config ever ships with real battery capacity.",
            {k: v for k, v in battery_capacities.items() if v},
        )

    denominators = {
        "capacity_of_energy_power_wind_turbine_inland": s.inputs["capacity_of_energy_power_wind_turbine_inland"].default,
        "capacity_of_energy_power_wind_turbine_offshore": s.inputs["capacity_of_energy_power_wind_turbine_offshore"].default,
        "capacity_of_energy_power_solar_pv_solar_radiation": s.inputs["capacity_of_energy_power_solar_pv_solar_radiation"].default,
        "capacity_of_buildings_solar_pv_solar_radiation": s.inputs["capacity_of_buildings_solar_pv_solar_radiation"].default,
        "capacity_of_households_solar_pv_solar_radiation": s.inputs["capacity_of_households_solar_pv_solar_radiation"].default,
        "households": sum(s.inputs[k].default for k in keys if k.startswith("households_number_of_") and "inhabitants" not in k),
        "buildings": sum(s.inputs[k].default for k in keys if k.startswith("buildings_number_of_buildings")),
    }
    with open(DENOMINATOR_CACHE, "w") as f:
        json.dump(denominators, f, indent=2)
    return denominators


def _denominator_value(col: str, denominators: dict[str, float]) -> tuple[float, str]:
    """Returns (denominator, unit_label) for physical-mode normalization of one column."""
    if col not in PHYSICAL_DENOMINATORS:
        return 1.0, PHYSICAL_UNIT_LABELS["raw"]
    kind, capacity_keys = PHYSICAL_DENOMINATORS[col]
    if kind == "capacity":
        return sum(denominators[k] for k in capacity_keys), PHYSICAL_UNIT_LABELS["capacity"]
    return denominators[kind], PHYSICAL_UNIT_LABELS[kind]


def fetch_raw_profiles() -> pd.DataFrame:
    """Raw (pre-normalization) hourly values for all 4 weather years - the same
    fetch_weather_year() used by the main pipeline, before its own shape normalization
    is applied. Cached locally - delete RAW_CACHE to force a refresh."""
    if os.path.exists(RAW_CACHE):
        print(f"Using cached raw profiles from {RAW_CACHE} (delete it to refetch)")
        # dtype=str on weather_year is required, not cosmetic: without it pandas'
        # chunked type inference reads "1987"/"1997"/"2004" as int64 in some chunks and
        # str in others (mixed dtype, only warned about via DtypeWarning, not raised) -
        # every later `raw_df.weather_year == "1987"` comparison against the string then
        # silently misses whichever chunks got parsed as int, which is exactly how this
        # was caught: 1997/2004 disappeared entirely and 1987 was partially missing from
        # weather_year_profiles_stats.csv with no visible error, just a quiet row-count
        # drop from 176 to 60.
        return pd.read_csv(RAW_CACHE, dtype={"weather_year": str})

    frames = []
    for wy in ALL_WEATHER_YEARS:
        raw = fetch_mod.fetch_weather_year(wy)
        for col in ALL_SERIES:
            series = raw.get(col)
            if series is None:
                continue
            values = series.to_numpy(dtype=float)
            frames.append(pd.DataFrame({"weather_year": wy, "column": col, "hour": np.arange(len(values)), "raw_value": values}))
    result = pd.concat(frames, ignore_index=True)
    result.to_csv(RAW_CACHE, index=False)
    return result


def build_long_df(raw_df: pd.DataFrame, denominators: dict[str, float]) -> pd.DataFrame:
    """Expand raw values into both normalization modes."""
    frames = []
    for col in ALL_SERIES:
        col_raw = raw_df[raw_df.column == col]
        if col_raw.empty:
            continue
        norm_kind = fetch_mod.NORMALIZATION.get(col, "sum")
        physical_denom, physical_unit = _denominator_value(col, denominators)
        for wy in ALL_WEATHER_YEARS:
            sub = col_raw[col_raw.weather_year == wy].sort_values("hour")
            if sub.empty:
                continue
            y = sub["raw_value"].to_numpy(dtype=float)
            shape_y = fetch_mod.normalize(pd.Series(y), norm_kind).to_numpy()
            physical_y = y / physical_denom if physical_denom else y
            frames.append(pd.DataFrame({"weather_year": wy, "column": col, "mode": "shape", "unit": "normalized", "hour": sub["hour"].to_numpy(), "value": shape_y}))
            frames.append(pd.DataFrame({"weather_year": wy, "column": col, "mode": "physical", "unit": physical_unit, "hour": sub["hour"].to_numpy(), "value": physical_y}))
    return pd.concat(frames, ignore_index=True)


def compute_stats(long_df: pd.DataFrame) -> pd.DataFrame:
    stats = long_df.groupby(["weather_year", "column", "mode", "unit"], sort=False)["value"].agg(min="min", max="max", mean="mean", median="median").reset_index()
    weather_order = {wy: i for i, wy in enumerate(ALL_WEATHER_YEARS)}
    column_order = {c: i for i, c in enumerate(ALL_SERIES)}
    mode_order = {m: i for i, m in enumerate(NORM_MODES)}
    stats["_wy"] = stats["weather_year"].map(weather_order)
    stats["_col"] = stats["column"].map(column_order)
    stats["_mode"] = stats["mode"].map(mode_order)
    stats = stats.sort_values(["_col", "_mode", "_wy"]).drop(columns=["_wy", "_col", "_mode"]).reset_index(drop=True)
    for col in ["min", "max", "mean", "median"]:
        stats[col] = stats[col].round(6)
    return stats


def _round_sigfigs(y: np.ndarray, sigfigs: int = 5) -> np.ndarray:
    """Round to a fixed number of significant figures, not decimal places.

    A fixed decimal count is wrong here: shape-mode values are ~1e-4 (demand, sum=1
    over 8760 hours) while physical-mode capacity factors are ~1e-1 to 1, and raw MW
    values range from ~1e-1 to ~1e4. Rounding everything to a fixed number of decimal
    places would keep full precision for the large numbers but truncate the small ones
    to near-zero - tried this first, caught it before shipping by checking a demand
    column's values collapsed to 0.0000 for most hours after rounding.
    """
    with np.errstate(divide="ignore"):
        magnitude = np.where(y != 0, np.floor(np.log10(np.abs(y))), 0)
    decimals = (sigfigs - 1 - magnitude).astype(int)
    return np.array([round(v, d) for v, d in zip(y, decimals)])


def build_chart_data(raw_df: pd.DataFrame, denominators: dict[str, float]) -> tuple[dict[str, list[float]], dict[str, float]]:
    """Raw MW values, stored ONCE per (weather_year, column), plus a scalar multiplier
    per (weather_year, column, mode) to convert raw -> displayed value in JS.

    Storing both the "shape" and "physical" series in full (as an earlier version of
    this script did) means every hourly value is serialized twice into the page for a
    transform that's just multiplying by one number per series - caught via the page
    coming out at 19MB, over the 16MB Artifact limit, even after cutting precision to
    3 significant figures (16.6MB, still over). Storing the raw series once and letting
    JS multiply by the right scale factor removes that duplication entirely instead of
    further degrading precision to chase a smaller file.
    """
    raw_series: dict[str, list[float]] = {}
    scales: dict[str, float] = {}
    for col in ALL_SERIES:
        col_raw = raw_df[raw_df.column == col]
        if col_raw.empty:
            continue
        norm_kind = fetch_mod.NORMALIZATION.get(col, "sum")
        physical_denom, _ = _denominator_value(col, denominators)
        for wy in ALL_WEATHER_YEARS:
            sub = col_raw[col_raw.weather_year == wy].sort_values("hour")
            if sub.empty:
                continue
            y = sub["raw_value"].to_numpy(dtype=float)
            raw_series[f"{wy}|{col}"] = _round_sigfigs(y).tolist()
            shape_denom = y.max() if norm_kind == "max" else y.sum()
            scales[f"{wy}|{col}|shape"] = (1.0 / shape_denom) if shape_denom else 1.0
            scales[f"{wy}|{col}|physical"] = (1.0 / physical_denom) if physical_denom else 1.0
    return raw_series, scales


def _stats_table_trace(stats: pd.DataFrame) -> go.Table:
    # Group-relative labels are only unique *within* their own group (e.g. "Steel"
    # appears under both "Industry electricity demand" and "Industry heat demand" -
    # two different columns, industry_steel_e_demand/industry_steel_h_demand). Flattened
    # into one table without the group context the dropdown provides, "Steel" then
    # appears to show up twice for no reason - qualify every label with its group name
    # here so each row is unambiguous on its own.
    label_by_col = {
        col: f"{group_name} — {label}" for group_name, group in SERIES_GROUPS.items() for col, label in group.items()
    }
    return go.Table(
        columnwidth=[60, 220, 70, 130, 70, 70, 70, 70],
        header=dict(
            values=["Weather year", "Column", "Mode", "Unit", "Min", "Mean", "Median", "Max"],
            fill_color="#e1e0d9",
            font=dict(color="#0b0b0b", size=12),
            align="left",
        ),
        cells=dict(
            values=[
                stats.weather_year,
                [label_by_col.get(c, c) for c in stats.column],
                stats["mode"],
                stats["unit"],
                stats["min"],
                stats["mean"],
                stats["median"],
                stats["max"],
            ],
            fill_color="#fcfcfb",
            font=dict(color="#0b0b0b", size=11),
            align="left",
            height=22,
        ),
    )


def _select_options_grouped() -> str:
    parts = []
    for group_name, cols in SERIES_GROUPS.items():
        opts = "\n".join(f'<option value="{col}">{label}</option>' for col, label in cols.items())
        parts.append(f'<optgroup label="{group_name}">\n{opts}\n</optgroup>')
    return "\n".join(parts)


def _write_viewer(
    table_fig: go.Figure,
    raw_series: dict[str, list[float]],
    scales: dict[str, float],
    unit_by_col_mode: dict[str, str],
) -> None:
    table_html = table_fig.to_html(full_html=False, include_plotlyjs=True, div_id="stats-table")
    raw_series_json = json.dumps(raw_series, separators=(",", ":"))
    scales_json = json.dumps(scales, separators=(",", ":"))
    unit_json = json.dumps(unit_by_col_mode)
    retrieval_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    page = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Weather-year profile viewer &mdash; wind, solar &amp; demand</title>
<style>
  :root {{
    --surface-1: #fcfcfb; --page-plane: #f9f9f7; --text-primary: #0b0b0b;
    --text-secondary: #52514e; --text-muted: #898781; --hairline: #e1e0d9;
    --border: rgba(11,11,11,0.10); --accent: #2a78d6;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: var(--page-plane); color: var(--text-primary); font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }}
  .page {{ max-width: 1180px; margin: 0 auto; padding: 40px 28px 64px; }}
  .eyebrow {{ text-transform: uppercase; letter-spacing: 0.08em; font-size: 11px; font-weight: 600; color: var(--text-muted); margin: 0 0 10px; }}
  h1 {{ font-size: 26px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 8px; text-wrap: balance; }}
  h2 {{ font-size: 17px; font-weight: 650; margin: 40px 0 4px; }}
  .subtitle {{ font-size: 14.5px; color: var(--text-secondary); max-width: 68ch; line-height: 1.55; margin: 0 0 10px; }}
  .card {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px; padding: 8px 8px 4px; overflow-x: auto; margin-top: 12px; }}
  .panel-controls {{ display: flex; flex-wrap: wrap; align-items: end; gap: 20px; margin-top: 18px; }}
  .panel-controls label {{ display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }}
  .panel-controls select {{ font-family: inherit; font-size: 13.5px; font-weight: 400; text-transform: none; letter-spacing: normal; color: var(--text-primary); background: var(--surface-1); border: 1px solid var(--border); border-radius: 5px; padding: 6px 10px; min-width: 260px; }}
  .view-toggle {{ display: flex; border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }}
  .view-btn {{ font-family: inherit; font-size: 13px; padding: 7px 14px; border: none; cursor: pointer; background: var(--surface-1); color: var(--text-secondary); }}
  .view-btn.active {{ background: var(--accent); color: #ffffff; }}
  .view-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: -2px; }}
  .legend-note {{ font-size: 12.5px; color: var(--text-muted); margin: 10px 2px 0; line-height: 1.5; }}
  .unit-note {{ font-size: 12.5px; color: var(--text-secondary); margin: 10px 2px 0; font-weight: 600; }}
  .meta-row {{ display: flex; flex-wrap: wrap; gap: 20px; margin: 30px 2px 0; font-size: 12.5px; color: var(--text-muted); }}
  .meta-row dt {{ font-weight: 600; color: var(--text-secondary); display: inline; }}
  .meta-row dd {{ display: inline; margin: 0 0 0 6px; }}
</style>
</head>
<body>
<div class="page">
  <p class="eyebrow">Zenmo-ZERO-Drechtsteden &middot; db_profiles_loader</p>
  <h1>Weather-year differences: wind, solar &amp; demand</h1>
  <p class="subtitle">
    Compares production/demand across ETM's 4 weather-year settings (default = real
    2019 measured data, plus 1987/1997/2004 extreme historical years) for the nl2023
    pull that feeds <code>db_profiles.xlsx</code>. Two normalization modes: <b>Shape</b>
    matches db_profiles.xlsx exactly (production / peak hour, demand / annual sum) -
    every year is forced onto the same [0,1]-ish range by construction, which is useful
    for the model but hides whether one year's wind/solar/demand was genuinely bigger
    or smaller. <b>Physical units</b> divides production by installed capacity (a true
    capacity factor) and demand by household/building count (MW per household/building)
    instead - these denominators are confirmed constant across weather years, so
    switching to this mode reveals real magnitude differences the Shape mode conceals.
  </p>

  <h2>Summary statistics</h2>
  <div class="card">{table_html}</div>

  <h2>Compare weather years for one series</h2>
  <p class="subtitle">Pick any wind, solar, or demand column; all 4 weather years are drawn together.</p>
  <div class="panel-controls">
    <label>Series
      <select id="panel-a" onchange="onFilterChange()">
        {_select_options_grouped()}
      </select>
    </label>
    <div class="view-toggle" role="group" aria-label="Normalization mode">
      <button type="button" id="mode-shape" class="view-btn active" onclick="setMode('shape')">Shape</button>
      <button type="button" id="mode-physical" class="view-btn" onclick="setMode('physical')">Physical units</button>
    </div>
    <div class="view-toggle" role="group" aria-label="Chart type">
      <button type="button" id="view-hourly" class="view-btn active" onclick="setView('hourly')">Hourly</button>
      <button type="button" id="view-ldc" class="view-btn" onclick="setView('ldc')">Load duration curve</button>
    </div>
  </div>
  <p class="unit-note" id="unit-note"></p>
  <div class="card">
    <div id="panel-chart" style="height: 480px"></div>
  </div>

  <p class="legend-note">
    Click a legend entry to hide or show a single weather year. Drag the range slider
    under the hourly chart's x-axis to zoom into any part of the year. The load
    duration curve sorts all 8760 hourly values from highest to lowest. Series without
    an obvious physical denominator (industry, logistics) show raw MW in Physical mode
    rather than a per-unit value.
  </p>
  <dl class="meta-row">
    <div><dt>Source</dt><dd>ETM electricity_profiles/household_heat/buildings_heat/network_gas_profiles/hydrogen_profiles curves via pyetm, area_code=nl2023</dd></div>
    <div><dt>Retrieved</dt><dd>{retrieval_date}</dd></div>
    <div><dt>Regenerate</dt><dd>python inspect_weather_year_profiles.py (from data_Generic/db_profiles_loader/)</dd></div>
  </dl>
</div>
<script>
// Raw MW values are stored once per (weather_year, column) - "shape" and "physical"
// mode are both just a scalar multiple of the same raw series (divide by peak/sum for
// shape, by installed capacity or household/building count for physical), computed
// here rather than duplicating every hourly series twice in the page (see
// build_chart_data() in inspect_weather_year_profiles.py for why - this halved the
// page's data size vs. storing both modes pre-computed).
window.RAW_SERIES = {raw_series_json};
window.SCALES = {scales_json};
window.UNIT_BY_COL_MODE = {unit_json};
window.WEATHER_YEARS = {json.dumps(ALL_WEATHER_YEARS)};
window.WEATHER_YEAR_LABELS = {json.dumps({wy: WEATHER_YEAR_LABELS[wy] for wy in ALL_WEATHER_YEARS})};
window.WEATHER_YEAR_COLORS = {json.dumps(WEATHER_YEAR_COLORS)};
window.PANEL_STATE = {{a: document.getElementById('panel-a').value, view: 'hourly', mode: 'shape'}};

function renderPanel() {{
  var state = window.PANEL_STATE;
  var traces = window.WEATHER_YEARS.map(function (wy) {{
    var raw = window.RAW_SERIES[wy + '|' + state.a] || [];
    var scale = window.SCALES[wy + '|' + state.a + '|' + state.mode];
    if (scale === undefined) scale = 1;
    var displayed = raw.map(function (v) {{ return v * scale; }});
    var label = window.WEATHER_YEAR_LABELS[wy];
    var y, x;
    if (state.view === 'hourly') {{
      y = displayed;
      x = displayed.map(function (_, i) {{ return i; }});
    }} else {{
      y = displayed.slice().sort(function (a, b) {{ return b - a; }});
      x = y.map(function (_, i) {{ return i + 1; }});
    }}
    return {{
      x: x, y: y, mode: 'lines', type: 'scattergl', name: label, legendgroup: wy,
      line: {{color: window.WEATHER_YEAR_COLORS[wy], width: 1.5}},
      hovertemplate: '%{{y:.5f}}<extra>' + label + '</extra>',
    }};
  }});
  var layout = {{
    hovermode: 'x unified',
    legend: {{orientation: 'h', yanchor: 'bottom', y: -0.22, xanchor: 'left', x: 0}},
    plot_bgcolor: '#fcfcfb', paper_bgcolor: '#fcfcfb',
    font: {{family: "system-ui, -apple-system, 'Segoe UI', sans-serif", color: '#0b0b0b'}},
    margin: {{t: 20, b: 70, l: 60, r: 20}},
    xaxis: {{
      title: state.view === 'hourly' ? 'Hour of year' : 'Duration (hours, sorted descending)',
      rangeslider: {{visible: state.view === 'hourly'}},
      gridcolor: '#e1e0d9',
    }},
    yaxis: {{title: 'Value', gridcolor: '#e1e0d9'}},
  }};
  Plotly.react('panel-chart', traces, layout, {{displaylogo: false}});
  document.getElementById('unit-note').textContent = 'Unit: ' + (window.UNIT_BY_COL_MODE[state.a + '|' + state.mode] || '');
}}

function onFilterChange() {{
  window.PANEL_STATE.a = document.getElementById('panel-a').value;
  renderPanel();
}}

function setView(view) {{
  window.PANEL_STATE.view = view;
  document.getElementById('view-hourly').classList.toggle('active', view === 'hourly');
  document.getElementById('view-ldc').classList.toggle('active', view === 'ldc');
  renderPanel();
}}

function setMode(mode) {{
  window.PANEL_STATE.mode = mode;
  document.getElementById('mode-shape').classList.toggle('active', mode === 'shape');
  document.getElementById('mode-physical').classList.toggle('active', mode === 'physical');
  renderPanel();
}}

renderPanel();
</script>
</body>
</html>
"""
    with open(VIEWER_HTML, "w") as f:
        f.write(page)


def main() -> None:
    denominators = fetch_denominators()
    raw_df = fetch_raw_profiles()
    long_df = build_long_df(raw_df, denominators)
    stats = compute_stats(long_df)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 400)
    print(stats.to_string(index=False))
    stats.to_csv(STATS_CSV, index=False)
    print(f"\nWrote {STATS_CSV} ({len(stats)} rows)")

    unit_by_col_mode = {}
    for col in ALL_SERIES:
        unit_by_col_mode[f"{col}|shape"] = "normalized"
        _, unit = _denominator_value(col, denominators)
        unit_by_col_mode[f"{col}|physical"] = unit

    raw_series, scales = build_chart_data(raw_df, denominators)
    table_fig = go.Figure(data=[_stats_table_trace(stats)])
    table_height = 30 + len(stats) * 22 + 20
    table_fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=table_height, paper_bgcolor="#fcfcfb")

    _write_viewer(table_fig, raw_series, scales, unit_by_col_mode)
    print(f"Wrote {VIEWER_HTML} (generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")


if __name__ == "__main__":
    main()
