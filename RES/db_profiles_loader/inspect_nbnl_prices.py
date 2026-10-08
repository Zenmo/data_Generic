"""Inspect the 16 NBNL scenario price columns added by fetch_etm_weather_years.py.

Reads db_profiles_with_weather_years.xlsx for the 1987/1997/2004 weather years, and
additionally fetches the "default" weather year live via ETM (not stored in the
workbook - out of scope for the main pipeline, see NOTES.md), for a total of 4 weather
years x 4 NBNL scenarios x 4 target years = 64 hourly price series.

ETM's "default" weather setting uses real 1-1-2019..31-12-2019 measured wind/solar
production data - confirmed via the underlying ETDataset README (not stated plainly in
the ETM UI docs): "For NL2019 we use the inland wind curves from this source for the
climate years 1987, 1997, 2004 and 2019." All 16 NBNL scenarios use area_code=nl2019
(confirmed when fetching each template's metadata), so their "default" is anchored to
that same real 2019 weather year, not an arbitrary/synthetic baseline.

Computes min/max/mean/median per (weather year, scenario, target year), and builds a
self-contained interactive HTML viewer with two panels:
  - Panel A: pick weather year + target year -> compare the 4 scenarios
  - Panel B: pick scenario + target year -> compare the 4 weather years
Each panel toggles between an hourly (t_h) view and a load duration curve (values
sorted descending against duration in hours) view.

Deliverables:
- nbnl_price_stats.csv   - the 64-row stats table (also printed to stdout)
- nbnl_price_viewer.html - standalone Plotly viewer (works offline, no CDN dependency)
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import fetch_etm_weather_years as fetch_mod

SOURCE_XLSX = "db_profiles_with_weather_years.xlsx"
STATS_CSV = "nbnl_price_stats.csv"
VIEWER_HTML = "nbnl_price_viewer.html"
DEFAULT_PRICE_CACHE = "nbnl_default_prices_cache.csv"

XLSX_WEATHER_YEARS = ["1987", "1997", "2004"]
ALL_WEATHER_YEARS = ["default", "1987", "1997", "2004"]
NBNL_SCENARIOS = fetch_mod.NBNL_SCENARIOS  # {"km": {"name": ..., "years": {...}}, ...}
TARGET_YEARS = fetch_mod.NBNL_TARGET_YEARS
N_HOURS = 8760

# Fixed categorical order (dataviz skill palette, validated: node scripts/validate_palette.js).
# Color follows the entity (scenario / weather year), never re-ordered by whatever a
# dropdown currently shows, and the two dimensions get visually distinct hue families
# so both panels can be read at a glance without cross-panel confusion.
SCENARIO_COLORS = {"km": "#2a78d6", "ev": "#1baf7a", "gb": "#eda100", "ha": "#008300"}  # blue/aqua/yellow/green
WEATHER_YEAR_COLORS = {
    "default": "#4a3aa7",  # violet
    "1987": "#e34948",  # red
    "1997": "#e87ba4",  # magenta
    "2004": "#eb6834",  # orange
}
WEATHER_YEAR_LABELS = {
    "default": "Default (2019 measured)",
    "1987": "1987 (Dunkelflaute, cold winter)",
    "1997": "1997 (Dunkelflaute + cold)",
    "2004": "2004 (high renewables)",
}


def _cell_text(c, ns: dict, shared: list[str]) -> str | None:
    """Read a cell's string value regardless of how openpyxl chose to encode it.

    openpyxl writes new sheets with inline strings (t="inlineStr", <is><t>...</t></is>)
    rather than a shared string table - this workbook has no xl/sharedStrings.xml at
    all (confirmed: KeyError on first attempt assumed one always exists). Handle both
    t="s" (shared string index) and t="inlineStr"/t="str" so this keeps working
    whichever an xlsx happens to use.
    """
    t = c.get("t")
    if t == "s":
        v = c.find("m:v", ns)
        return shared[int(v.text)] if v is not None else None
    if t == "inlineStr":
        is_el = c.find("m:is", ns)
        if is_el is None:
            return None
        return "".join((t_el.text or "") for t_el in is_el.findall("m:t", ns))
    v = c.find("m:v", ns)
    return v.text if v is not None else None


def _sheet_xml_to_df(zf: zipfile.ZipFile, sheet_path: str, columns: list[str]) -> pd.DataFrame:
    """Read only the requested columns of a worksheet directly from the xlsx zip/XML.

    Avoids pandas.read_excel parsing all 43 columns of an 8760-row sheet 3 times over
    when only 16 are needed - this file has other much larger sheets, so reading via
    openpyxl/pandas normally still means opening the whole workbook first.
    """
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    try:
        sst_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        shared = ["".join((t.text or "") for t in si.findall(".//m:t", ns)) for si in sst_root.findall("m:si", ns)]
    except KeyError:
        shared = []

    root = ET.fromstring(zf.read(sheet_path))
    sheetdata = root.find("m:sheetData", ns)
    rows = sheetdata.findall("m:row", ns)

    header_row = rows[0]
    header = {}
    for c in header_row.findall("m:c", ns):
        col_letter = "".join(ch for ch in c.get("r") if ch.isalpha())
        header[col_letter] = _cell_text(c, ns, shared)
    wanted_letters = {letter: name for letter, name in header.items() if name in columns}

    data: dict[str, list[float]] = {name: [] for name in columns}
    for row in rows[1:]:
        values = {}
        for c in row.findall("m:c", ns):
            col_letter = "".join(ch for ch in c.get("r") if ch.isalpha())
            if col_letter not in wanted_letters:
                continue
            v = c.find("m:v", ns)
            values[wanted_letters[col_letter]] = float(v.text) if v is not None else np.nan
        for name in columns:
            data[name].append(values.get(name, np.nan))
    return pd.DataFrame(data)


def _resolve_target(target: str) -> str:
    # openpyxl writes rel targets as "/xl/worksheets/sheetN.xml" (absolute-style,
    # leading slash) rather than the more common "worksheets/sheetN.xml" (relative
    # to xl/) - handle both instead of assuming one convention.
    return target[1:] if target.startswith("/") else "xl/" + target


def _load_from_xlsx(price_cols: list[str]) -> pd.DataFrame:
    zf = zipfile.ZipFile(SOURCE_XLSX)
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
    rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {
        rel.get("Id"): rel.get("Target")
        for rel in rels_root.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship")
    }
    sheet_name_to_path = {}
    for sheet in wb_root.find("m:sheets", ns).findall("m:sheet", ns):
        rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        sheet_name_to_path[sheet.get("name")] = _resolve_target(rid_to_target[rid])

    frames = []
    for wy in XLSX_WEATHER_YEARS:
        sheet_name = f"profiles_weather{wy}_h"
        df = _sheet_xml_to_df(zf, sheet_name_to_path[sheet_name], price_cols)
        df["hour"] = np.arange(len(df))
        for code in NBNL_SCENARIOS:
            for year in TARGET_YEARS:
                col = fetch_mod.nbnl_price_column(code, year)
                frames.append(
                    pd.DataFrame(
                        {
                            "weather_year": wy,
                            "scenario": code,
                            "scenario_name": NBNL_SCENARIOS[code]["name"],
                            "year": year,
                            "hour": df["hour"],
                            "price_eur_mwh": df[col],
                        }
                    )
                )
    return pd.concat(frames, ignore_index=True)


def _load_default_live() -> pd.DataFrame:
    """Fetch the 'default' weather year live - not stored in the xlsx (out of scope
    for the main workbook pipeline, see NOTES.md), so this is the only weather year
    that requires a fresh ETM call rather than reading the cached workbook. Cached
    locally after the first fetch (16 loads + 16 copies + 16 curve calls, ~1.5-2 min)
    so re-running this script to tweak the viewer doesn't re-hit ETM every time -
    delete DEFAULT_PRICE_CACHE to force a refresh."""
    if os.path.exists(DEFAULT_PRICE_CACHE):
        cached = pd.read_csv(DEFAULT_PRICE_CACHE)
        print(f"Using cached default-weather prices from {DEFAULT_PRICE_CACHE} (delete it to refetch)")
        return cached
    prices = fetch_mod.fetch_nbnl_prices_multi(["default"])["default"]
    hour = np.arange(N_HOURS)
    frames = []
    for code in NBNL_SCENARIOS:
        for year in TARGET_YEARS:
            col = fetch_mod.nbnl_price_column(code, year)
            frames.append(
                pd.DataFrame(
                    {
                        "weather_year": "default",
                        "scenario": code,
                        "scenario_name": NBNL_SCENARIOS[code]["name"],
                        "year": year,
                        "hour": hour,
                        "price_eur_mwh": prices[col],
                    }
                )
            )
    result = pd.concat(frames, ignore_index=True)
    result.to_csv(DEFAULT_PRICE_CACHE, index=False)
    return result


def load_nbnl_prices() -> pd.DataFrame:
    price_cols = [
        fetch_mod.nbnl_price_column(code, year) for code in NBNL_SCENARIOS for year in TARGET_YEARS
    ]
    from_xlsx = _load_from_xlsx(price_cols)
    default_live = _load_default_live()
    return pd.concat([default_live, from_xlsx], ignore_index=True)


def compute_stats(long_df: pd.DataFrame) -> pd.DataFrame:
    stats = (
        long_df.groupby(["weather_year", "year", "scenario", "scenario_name"], sort=False)["price_eur_mwh"]
        .agg(min="min", max="max", mean="mean", median="median")
        .reset_index()
    )
    weather_order = {wy: i for i, wy in enumerate(ALL_WEATHER_YEARS)}
    stats["_wy_order"] = stats["weather_year"].map(weather_order)
    stats = stats.sort_values(["_wy_order", "year", "scenario"]).drop(columns="_wy_order").reset_index(drop=True)
    for col in ["min", "max", "mean", "median"]:
        stats[col] = stats[col].round(2)
    return stats


def _stats_table_trace(stats: pd.DataFrame) -> go.Table:
    return go.Table(
        columnwidth=[70, 70, 170, 70, 70, 70, 70],
        header=dict(
            values=["Weather year", "Target year", "Scenario", "Min", "Mean", "Median", "Max"],
            fill_color="#e1e0d9",
            font=dict(color="#0b0b0b", size=12),
            align="left",
        ),
        cells=dict(
            values=[
                stats.weather_year,
                stats.year,
                stats.scenario_name,
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


def build_series_data(long_df: pd.DataFrame) -> dict[str, list[float]]:
    """One compact JS-side lookup table, keyed 'weather_year|scenario|year', storing
    each of the 64 unique hourly price series exactly once.

    Both panels show the same underlying 64 series, just grouped by a different pair
    of dimensions - building a full Plotly trace set per panel (as an earlier version
    of this script did) embeds each series twice, doubling the page's data payload for
    no benefit. Traces are instead built on the fly in JS (see renderPanel() in
    _write_viewer) by looking values up here, so every series is serialized once.
    """
    data: dict[str, list[float]] = {}
    for (wy, scenario, year), group in long_df.groupby(["weather_year", "scenario", "year"], sort=False):
        key = f"{wy}|{scenario}|{year}"
        y = group.sort_values("hour")["price_eur_mwh"].to_numpy(dtype=float).round(1)
        data[key] = y.tolist()
    return data


def _select_options(values: list, labels: dict | None = None) -> str:
    opts = []
    for v in values:
        label = labels[v] if labels else str(v)
        opts.append(f'<option value="{v}">{label}</option>')
    return "\n".join(opts)


def _panel_html(
    panel_id: str,
    a_dim: str,
    a_label: str,
    a_values: list,
    a_labels: dict | None,
    b_dim: str,
    b_label: str,
    b_values: list,
    series_dim: str,
    series_values: list[str],
    series_labels: dict[str, str],
    series_colors: dict[str, str],
    default_a,
    default_b,
) -> str:
    config = {
        "aDim": a_dim,
        "bDim": b_dim,
        "seriesDim": series_dim,
        "seriesValues": series_values,
        "seriesLabels": series_labels,
        "seriesColors": series_colors,
    }
    return f"""
  <div class="panel-controls">
    <label>{a_label}
      <select id="{panel_id}-a" onchange="onFilterChange('{panel_id}')">
        {_select_options(a_values, a_labels)}
      </select>
    </label>
    <label>{b_label}
      <select id="{panel_id}-b" onchange="onFilterChange('{panel_id}')">
        {_select_options(b_values)}
      </select>
    </label>
    <div class="view-toggle" role="group" aria-label="Chart type">
      <button type="button" id="{panel_id}-view-hourly" class="view-btn active"
              onclick="setView('{panel_id}', 'hourly')">Hourly</button>
      <button type="button" id="{panel_id}-view-ldc" class="view-btn"
              onclick="setView('{panel_id}', 'ldc')">Load duration curve</button>
    </div>
  </div>
  <div class="card">
    <div id="{panel_id}-chart" style="height: 480px"></div>
  </div>
  <script>
    window.PANEL_CONFIG = window.PANEL_CONFIG || {{}};
    window.PANEL_STATE = window.PANEL_STATE || {{}};
    window.PANEL_CONFIG["{panel_id}"] = {json.dumps(config)};
    window.PANEL_STATE["{panel_id}"] = {{a: "{default_a}", b: "{default_b}", view: "hourly"}};
  </script>
"""


def _write_viewer(
    table_fig: go.Figure,
    series_data: dict[str, list[float]],
    scenario_labels: dict[str, str],
) -> None:
    table_html = table_fig.to_html(full_html=False, include_plotlyjs=True, div_id="stats-table")

    panel_a_html = _panel_html(
        "panelA",
        a_dim="weather_year",
        a_label="Weather year",
        a_values=ALL_WEATHER_YEARS,
        a_labels={wy: WEATHER_YEAR_LABELS[wy].split(" (")[0] for wy in ALL_WEATHER_YEARS},
        b_dim="year",
        b_label="Target year",
        b_values=TARGET_YEARS,
        series_dim="scenario",
        series_values=list(scenario_labels.keys()),
        series_labels=scenario_labels,
        series_colors=SCENARIO_COLORS,
        default_a="default",
        default_b=TARGET_YEARS[0],
    )
    panel_b_html = _panel_html(
        "panelB",
        a_dim="scenario",
        a_label="Scenario",
        a_values=list(scenario_labels.keys()),
        a_labels=scenario_labels,
        b_dim="year",
        b_label="Target year",
        b_values=TARGET_YEARS,
        series_dim="weather_year",
        series_values=ALL_WEATHER_YEARS,
        series_labels=WEATHER_YEAR_LABELS,
        series_colors=WEATHER_YEAR_COLORS,
        default_a=list(scenario_labels.keys())[0],
        default_b=TARGET_YEARS[0],
    )

    retrieval_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    series_data_json = json.dumps(series_data, separators=(",", ":"))
    page = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>NBNL 2025 scenario prices &mdash; weather-year viewer</title>
<style>
  :root {{
    --surface-1: #fcfcfb;
    --page-plane: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --hairline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
    --accent: #2a78d6;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{
    margin: 0;
    background: var(--page-plane);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }}
  .page {{ max-width: 1180px; margin: 0 auto; padding: 40px 28px 64px; }}
  .eyebrow {{
    text-transform: uppercase; letter-spacing: 0.08em; font-size: 11px; font-weight: 600;
    color: var(--text-muted); margin: 0 0 10px;
  }}
  h1 {{ font-size: 26px; font-weight: 650; letter-spacing: -0.01em; margin: 0 0 8px; text-wrap: balance; }}
  h2 {{ font-size: 17px; font-weight: 650; margin: 40px 0 4px; }}
  .subtitle {{ font-size: 14.5px; color: var(--text-secondary); max-width: 68ch; line-height: 1.55; margin: 0 0 10px; }}
  .card {{
    background: var(--surface-1); border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 8px 4px; overflow-x: auto; margin-top: 12px;
  }}
  .panel-controls {{
    display: flex; flex-wrap: wrap; align-items: end; gap: 20px; margin-top: 18px;
  }}
  .panel-controls label {{
    display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600;
    color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .panel-controls select {{
    font-family: inherit; font-size: 13.5px; font-weight: 400; text-transform: none;
    letter-spacing: normal; color: var(--text-primary); background: var(--surface-1);
    border: 1px solid var(--border); border-radius: 5px; padding: 6px 10px; min-width: 170px;
  }}
  .view-toggle {{ display: flex; border: 1px solid var(--border); border-radius: 5px; overflow: hidden; }}
  .view-btn {{
    font-family: inherit; font-size: 13px; padding: 7px 14px; border: none; cursor: pointer;
    background: var(--surface-1); color: var(--text-secondary);
  }}
  .view-btn.active {{ background: var(--accent); color: #ffffff; }}
  .view-btn:focus-visible {{ outline: 2px solid var(--accent); outline-offset: -2px; }}
  .legend-note {{ font-size: 12.5px; color: var(--text-muted); margin: 10px 2px 0; line-height: 1.5; }}
  .meta-row {{ display: flex; flex-wrap: wrap; gap: 20px; margin: 30px 2px 0; font-size: 12.5px; color: var(--text-muted); }}
  .meta-row dt {{ font-weight: 600; color: var(--text-secondary); display: inline; }}
  .meta-row dd {{ display: inline; margin: 0 0 0 6px; }}
</style>
</head>
<body>
<div class="page">
  <p class="eyebrow">Zenmo-ZERO-Drechtsteden &middot; db_profiles_loader</p>
  <h1>NBNL 2025 scenario market prices</h1>
  <p class="subtitle">
    ETM's own market-clearing price (EUR/MWh) for the four Netbeheer Nederland 2025
    scenarios &mdash; Koersvaste Middenweg, Eigen Vermogen, Gezamenlijke Balans, Horizon
    Aanvoer &mdash; at 2030/2035/2040/2050, under 4 weather-year settings (ETM's
    "default", which uses real 2019 measured wind/solar production, plus the three
    extreme historical years 1987/1997/2004). Proxy for future EPEX day-ahead price;
    real EPEX data only exists for 2023&ndash;2025.
  </p>

  <h2>Summary statistics (EUR/MWh)</h2>
  <div class="card">{table_html}</div>

  <h2>Compare scenarios &mdash; fix weather year &amp; target year</h2>
  <p class="subtitle">Pick a weather year and target year; the four scenarios are drawn together so you can compare narratives directly.</p>
  {panel_a_html}

  <h2>Compare weather years &mdash; fix scenario &amp; target year</h2>
  <p class="subtitle">Pick a scenario and target year; the four weather-year settings are drawn together so you can see how much weather actually moves the price for that one scenario.</p>
  {panel_b_html}

  <p class="legend-note">
    Click a legend entry to hide or show a single line. Drag the range slider under
    the hourly chart's x-axis to zoom into any part of the year. The load duration
    curve sorts all 8760 hourly prices from highest to lowest, showing how many hours
    per year the price exceeds a given level &mdash; the same underlying data as the
    hourly view, just re-ordered.
  </p>
  <dl class="meta-row">
    <div><dt>Source</dt><dd>ETM electricity_price curve, pyetm, scenario templates on the pinned 2025-01 engine</dd></div>
    <div><dt>Retrieved</dt><dd>{retrieval_date}</dd></div>
    <div><dt>Regenerate</dt><dd>python inspect_nbnl_prices.py (from data_Generic/db_profiles_loader/)</dd></div>
  </dl>
</div>
<script>
// window.SERIES_DATA holds each of the 64 unique hourly price series exactly once,
// keyed "weather_year|scenario|year" (see build_series_data() in
// inspect_nbnl_prices.py). Both panels look values up from this one shared table and
// build their own Plotly traces in JS - nothing chart-shaped is embedded per panel, so
// switching a filter or the hourly/load-duration-curve view never needs new data from
// the page, just a different combination of already-loaded numbers.
window.SERIES_DATA = {series_data_json};

function seriesKey(weatherYear, scenario, year) {{
  var dims = {{}};
  dims.weather_year = weatherYear;
  dims.scenario = scenario;
  dims.year = year;
  return dims.weather_year + '|' + dims.scenario + '|' + dims.year;
}}

function lookupKey(cfg, aVal, bVal, seriesVal) {{
  var dims = {{}};
  dims[cfg.aDim] = aVal;
  dims[cfg.bDim] = bVal;
  dims[cfg.seriesDim] = seriesVal;
  return seriesKey(dims.weather_year, dims.scenario, dims.year);
}}

function renderPanel(panelId) {{
  var cfg = window.PANEL_CONFIG[panelId];
  var state = window.PANEL_STATE[panelId];
  var traces = cfg.seriesValues.map(function (s) {{
    var key = lookupKey(cfg, state.a, state.b, s);
    var raw = window.SERIES_DATA[key] || [];
    var label = cfg.seriesLabels[s];
    var y, x;
    if (state.view === 'hourly') {{
      y = raw;
      x = raw.map(function (_, i) {{ return i; }});
    }} else {{
      y = raw.slice().sort(function (a, b) {{ return b - a; }});
      x = y.map(function (_, i) {{ return i + 1; }});
    }}
    return {{
      x: x,
      y: y,
      mode: 'lines',
      type: 'scattergl',
      name: label,
      legendgroup: s,
      line: {{color: cfg.seriesColors[s], width: 1.5}},
      hovertemplate: '%{{y:.1f}} EUR/MWh<extra>' + label + '</extra>',
    }};
  }});
  var layout = {{
    hovermode: 'x unified',
    legend: {{orientation: 'h', yanchor: 'bottom', y: -0.22, xanchor: 'left', x: 0}},
    plot_bgcolor: '#fcfcfb',
    paper_bgcolor: '#fcfcfb',
    font: {{family: "system-ui, -apple-system, 'Segoe UI', sans-serif", color: '#0b0b0b'}},
    margin: {{t: 20, b: 70, l: 60, r: 20}},
    xaxis: {{
      title: state.view === 'hourly' ? 'Hour of year' : 'Duration (hours, sorted descending)',
      rangeslider: {{visible: state.view === 'hourly'}},
      gridcolor: '#e1e0d9',
    }},
    yaxis: {{title: 'EUR/MWh', gridcolor: '#e1e0d9'}},
  }};
  Plotly.react(panelId + '-chart', traces, layout, {{displaylogo: false}});
}}

function onFilterChange(panelId) {{
  var state = window.PANEL_STATE[panelId];
  state.a = document.getElementById(panelId + '-a').value;
  state.b = document.getElementById(panelId + '-b').value;
  renderPanel(panelId);
}}

function setView(panelId, view) {{
  window.PANEL_STATE[panelId].view = view;
  document.getElementById(panelId + '-view-hourly').classList.toggle('active', view === 'hourly');
  document.getElementById(panelId + '-view-ldc').classList.toggle('active', view === 'ldc');
  renderPanel(panelId);
}}

renderPanel('panelA');
renderPanel('panelB');
</script>
</body>
</html>
"""
    with open(VIEWER_HTML, "w") as f:
        f.write(page)


def main() -> None:
    long_df = load_nbnl_prices()
    stats = compute_stats(long_df)

    pd.set_option("display.width", 160)
    pd.set_option("display.max_rows", 200)
    print(stats.to_string(index=False))
    stats.to_csv(STATS_CSV, index=False)
    print(f"\nWrote {STATS_CSV} ({len(stats)} rows)")

    scenario_labels = {code: info["name"] for code, info in NBNL_SCENARIOS.items()}
    series_data = build_series_data(long_df)

    table_fig = go.Figure(data=[_stats_table_trace(stats)])
    # Sized to content, not a fixed guess - see the equivalent fix in
    # inspect_weather_year_profiles.py for why a fixed height clipped/compressed rows.
    table_height = 30 + len(stats) * 22 + 20
    table_fig.update_layout(margin=dict(t=10, b=10, l=10, r=10), height=table_height, paper_bgcolor="#fcfcfb")

    _write_viewer(table_fig, series_data, scenario_labels)
    print(f"Wrote {VIEWER_HTML} (generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")


if __name__ == "__main__":
    main()
