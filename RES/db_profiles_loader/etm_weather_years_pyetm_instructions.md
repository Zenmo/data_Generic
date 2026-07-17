# Task: Retrieve ETM weather-year curves with `pyetm` and append them to `db_profiles.xlsx`

## 0. Context — what this repo/file already contains

`db_profiles.xlsx` is a workbook of normalized hourly/quarter-hourly production and
demand *profiles* for the Netherlands, used to drive an energy model. It currently has:

| Sheet | Rows | Resolution | Notes |
|---|---|---|---|
| `Documentation` | 20 | – | Column definitions, units, source of every profile column |
| `profiles_2025` | 35,040 | 15 min (`t_h` = 0, 0.25, 0.5 … 8759.75) | Real calendar year 2025 |
| `profiles_2025_h` | 8,760 | 1 h (`t_h` = 0 … 8759) | Same data as above, hourly |
| `profiles_2024` / `profiles_2024_h` | 35,040 / 8,760 | 15 min / 1 h | Calendar year 2024 |
| `profiles_2024_366_days` / `_h` | 35,137 / … | 15 min / 1 h | 2024 leap-year (366 days) variant |
| `profiles_2023` / `profiles_2023_h` | 35,040 / 8,760 | 15 min / 1 h | Calendar year 2023 (has 2 extra wind-location columns) |
| `weather2023` | 8,760 | 1 h | Older/simpler legacy layout, 5 columns only |

**Column set** (from the `Documentation` sheet), in order, for the "current" layout
(`profiles_2025`, `profiles_2024`, `profiles_2024_366_days`):

```
t_h                                     Time index (hours, interval-start)
wind_e_prod_normalized                  Wind production, Geldermalsen, normalized so MAX = 1
solar_e_prod_south35deg_normalized      PV production, south 35°, normalized so MAX = 1
solar_e_prod_eastwest15deg_normalized   PV production, east/west 15°, normalized so MAX = 1
ambientTemperature_degC                 Outdoor temperature, De Bilt, °C (NOT normalized)
day_ahead_price_eur_p_mwh               NL day-ahead power price, EUR/MWh (NOT normalized)
house_e_demand_other                    Normalized so the 8760/35040 values SUM to 1
house_h_demand_hot_water                Normalized so values SUM to 1
building_e_demand_other                 Normalized so values SUM to 1
building_h_demand                       Normalized so values SUM to 1
house_cooking_demand                    Normalized so values SUM to 1
industry_steel_e_demand                 Normalized so values SUM to 1
industry_steel_h_demand                 Normalized so values SUM to 1
industry_other_e_demand                 Normalized so values SUM to 1
industry_other_h_demand                 Normalized so values SUM to 1
logistics_fleet_e_hgv                   Normalized so values SUM to 1
wind_e_prod_normalized_hvh              Wind production, Hoek van Holland, normalized so MAX = 1
wind_e_prod_normalized_heibloem         Wind production, Heibloem, normalized so MAX = 1
co2_factor_kg_per_kwh                   Grid CO2 intensity, kg/kWh (NOT normalized)
```

`profiles_2023*` additionally has `wind_e_prod_normalized_herwijnen` and
`wind_e_prod_normalized_ell` between `heibloem` and `co2_factor_kg_per_kwh`.

**Important — two different normalization conventions are in play. Preserve both:**
- Production columns (`wind_*`, `solar_*`) → divide by the max, so the peak hour = 1
  (a capacity-factor-style curve).
- Demand columns (`house_*`, `building_*`, `industry_*`, `logistics_*`) → divide by the
  annual sum, so all 8760 (or 35040) values add up to exactly 1 (a load-shape curve).
- `ambientTemperature_degC`, `day_ahead_price_eur_p_mwh`, `co2_factor_kg_per_kwh` are
  left as absolute values, not normalized.

Only `wind_e_prod_normalized*`, `solar_e_prod_*`, and all the `*_demand*` /
`logistics_fleet_e_hgv` columns come from ETM (per the `Source` column in
`Documentation`). Temperature, day-ahead price and CO2 factor come from KNMI, ENTSO-E
and NED respectively — **not** from ETM — so they are out of scope for the ETM pull
described here (see §6 for optional follow-up).

## 1. What we're adding: ETM "weather years"

ETM lets a Dutch scenario be run against different historical weather patterns instead
of "actual measured" production data. Per the docs
(https://docs.energytransitionmodel.com/main/weather-conditions/), there are four
options, only available for Dutch regions:

- **Default** — actual measured production curves (this is what the existing
  `profiles_2023/2024/2025` sheets already represent, sourced independently, not via
  this ETM pull)
- **1987** — "Dunkelflaute" during an extreme cold winter period
- **1997** — Lack of sustainable energy (incl. Dunkelflaute) + extreme cold days
- **2004** — Excessive and scarce sustainable energy

For these three weather years, wind and solar curves are **not** the Open Power System
Data measured curves; ETM instead derives them from actual weather station data for
that historical year, scaled to the scenario's technology mix. Docs:
https://github.com/quintel/etdataset-public/tree/master/curves/supply/wind/script/weather_years
and .../solar/script/weather_years.

Goal: fetch, for each of `1987`, `1997`, `2004` (and, as a sanity check, the "default"
setting too), the equivalent ETM curves for wind production, solar production, and the
demand profiles, and append them to `db_profiles.xlsx` as new sheets
`profiles_weather1987`, `profiles_weather1987_h`, `profiles_weather1997`,
`profiles_weather1997_h`, `profiles_weather2004`, `profiles_weather2004_h` — matching
the exact column layout and both normalization conventions described in §0.

## 2. Setup

```bash
pip install pyetm openpyxl pandas
```

No API token is required for this task — we only need to *create* a scenario and read
public data, not save/share it, so an anonymous `Session` is enough (see
https://quintel.github.io/pyetm/getting-started/configuration/: a token is only
required for saving scenarios or accessing private ones).

```python
from pyetm import Session
```

## 3. Step 1 — find the correct scenario input key for "weather year"

This key is not documented plainly in the PyETM user guide, so **discover it
programmatically** rather than guessing:

```python
from pyetm import Session

session = Session.new(area_code="nl2023", end_year=2050)
inputs = session.inputs
keys = inputs.keys()

candidates = [k for k in keys if "weather" in k.lower()]
print(candidates)
for k in candidates:
    inp = inputs[k]
    print(k, type(inp).__name__, getattr(inp, "permitted_values", None),
          getattr(inp, "default", None), getattr(inp, "unit", None))
```

This should surface an `EnumInput` (permitted values likely include something like
`default`, `2004`, `1997`, `1987` — print `permitted_values` to get the exact strings,
do not assume). If nothing obviously named "weather" turns up, also grep for
`"curve_set"`, `"weather_year"`, and `"dunkelflaute"` as fallback substrings, and as a
last resort inspect the ETM web app's network calls when manually switching the
"Weather conditions" dropdown at
https://pro.energytransitionmodel.com/scenario/flexibility/flexibility_weather — the
slider/input key used there is authoritative.

Also check whether `area_code` needs to be a specific NL dataset year (e.g. `"nl2023"`
vs `"nl"`), since the docs say the weather-year feature is NL-only. Use
`Session.new(area_code=..., end_year=2050)` with whichever NL area code the discovered
input is not `disabled` for — verify via `inputs[key].disabled`.

## 4. Step 2 — build one scenario per weather year and pull curves

```python
import pandas as pd

WEATHER_YEARS = {
    "1987": "1987",   # replace with exact permitted_value strings from step 3
    "1997": "1997",
    "2004": "2004",
    "default": "default",
}

CURVE_NAME = "electricity_profiles"  # merit-order participant curves; also try
                                      # "household_heat", "buildings_heat" for heat demand

sessions = {}
for label, permitted_value in WEATHER_YEARS.items():
    s = Session.new(area_code="nl2023", end_year=2050)
    s.update_user_values({WEATHER_INPUT_KEY: permitted_value})  # key found in step 3
    sessions[label] = s

curves = {}
for label, s in sessions.items():
    curves[label] = s.get_hourly_curves([
        "electricity",     # -> electricity_profiles (per-participant MW, 8760 rows)
        "household_heat",
        "buildings_heat",
    ])
```

`get_hourly_curve("electricity")` / `"electricity_profiles"` returns a DataFrame with
one column per participant in the merit order (e.g. wind onshore, wind offshore, solar
PV, households flexibility, industry demand, etc. — actual column names vary by area
config, so **print `.columns` and inspect them** rather than hardcoding). Each column
is in MW (or similar absolute units), 8760 hourly rows, **not yet normalized** — do the
normalization described in §0 yourself, after mapping columns.

For heat/other-sector demand shapes that are not obviously present in
`electricity_profiles`, check:
- `scenario.get_hourly_curve("household_heat")` — household heat supply/demand
  (candidate source for `house_h_demand_hot_water`, though note that column is
  specifically DHW not total heat — check whether ETM exposes a DHW-only curve
  separately, e.g. via `inputs`/gqueries, before assuming this curve matches)
- `scenario.get_hourly_curve("buildings_heat")` — candidate for `building_h_demand`
- Steel/other industry and logistics/HGV demand curves may not have a direct
  `hourly_output_curve` — check `scenario.hourly_output_curves.attached_keys()` for the
  full available list per scenario, and fall back to relevant `gqueries`
  (see https://quintel.github.io/pyetm/user-guide/gqueries/) if no curve exists.
  Document clearly in the script + a comment in the workbook when a column could not be
  sourced from ETM for a given weather year, rather than silently leaving it blank.

## 5. Step 3 — reshape, normalize, and write into the workbook

1. Load `db_profiles.xlsx` with `openpyxl` (keep `data_only=False` if you need to
   preserve the `Documentation` sheet's formatting; otherwise `pandas` + `openpyxl`
   engine is fine for read/write of the new sheets).
2. For each weather year, build a DataFrame with the exact column order used in
   `profiles_2025` (see §0 table). Populate what's available from ETM; leave any column
   you could not source as `NaN` **and** log a warning listing which columns were
   skipped and why (don't fabricate values).
3. Apply normalization:
   ```python
   df["wind_e_prod_normalized"] = raw_wind / raw_wind.max()
   df["house_e_demand_other"] = raw_house_e / raw_house_e.sum()
   # etc., per §0's two conventions
   ```
4. Write the **hourly** version first (`t_h` = 0..8759, 8760 rows) to a sheet named
   `profiles_weather{YEAR}_h` (e.g. `profiles_weather1987_h`).
5. Derive the **quarter-hourly** version by interpolating each column to 15-minute
   steps (`t_h` = 0, 0.25, … 8759.75, 35040 rows), matching how `profiles_2025` relates
   to `profiles_2025_h`. Use linear interpolation for continuous quantities
   (temperature, price) and check whether production/demand curves in the existing
   quarter-hour sheets look interpolated-from-hourly or independently sourced at 15 min
   resolution (compare `profiles_2025` vs `profiles_2025_h` for a few columns —
   e.g. does every 4th quarter-hour row exactly equal the corresponding hourly row?)
   before choosing your interpolation approach, and note the finding in the script's
   docstring. Write this to `profiles_weather{YEAR}` (no `_h` suffix).
6. Use `openpyxl` to append these as **new sheets** to a copy of `db_profiles.xlsx`
   (don't overwrite the original — write to a new output file, e.g.
   `db_profiles_with_weather_years.xlsx`, and confirm existing sheets are byte-for-byte
   unchanged).
7. Add one row per new sheet to a copied/extended `Documentation` sheet (or a new
   `Documentation_weather_years` sheet) explaining: which ETM scenario settings were
   used (area_code, end_year, weather-year input key + value), the ETM curve(s) each
   column was derived from, and the retrieval date — mirroring the existing
   `Documentation` sheet's `Profile / Definition / Convention / File / Source` columns.

## 6. Step 4 — sanity check before trusting the extreme years

Before fully trusting the 1987/1997/2004 pulls, do one validation pass:
- Pull the ETM curves for `"default"` weather setting on a scenario configured to
  resemble 2023 as closely as possible, and eyeball-compare (e.g. correlation, max
  timing, annual full-load hours) against the existing `profiles_2023` /
  `weather2023` sheets, which are sourced independently (via `windpowerlib`/`pvlib`/
  KNMI/OPSD, not ETM). They won't match exactly (different underlying methodology —
  see the `Documentation` sheet's `Source` column) but gross features (winter peak wind,
  summer peak solar, etc.) should be directionally consistent. Flag clearly in the
  output if they are not.

## 7. Optional follow-up — data ETM does NOT provide

These columns cannot come from the ETM weather-year pull; if you have time after the
above works, look into these ETM-external sources (the existing `Documentation` sheet
already lists the exact tools/sources used for the calendar-year sheets, follow the
same approach, but note none of these vary with "weather year" as an ETM concept —
1987/1997/2004 don't have day-ahead price or CO2 data at all, since those markets
didn't work the way they do today):
- `ambientTemperature_degC` — KNMI hourly data for the historical year (De Bilt station)
  is retrievable directly from KNMI (https://daggegevens.knmi.nl/klimatologie/uurgegevens)
  for 1987/1997/2004 without needing ETM at all.
- `day_ahead_price_eur_p_mwh` — no NL day-ahead market existed for these years in a
  form comparable to today's; this column likely has to stay out of scope for the
  weather-year sheets, or be filled with a clear "N/A — no market data for this
  historical year" placeholder rather than an invented value.
- `co2_factor_kg_per_kwh` — similarly not available historically at this granularity
  from NED for 1987/1997/2004; consider leaving blank with a documented reason.
- Additional wind locations (`_hvh`, `_heibloem`, `_herwijnen`, `_ell`) — these come
  from windpowerlib runs against specific ERA5 grid points per the `Documentation`
  sheet, not ETM; producing per-location weather-year variants would mean re-running
  that windpowerlib pipeline against 1987/1997/2004 ERA5 reanalysis data instead of ETM.

## 8. Deliverables

- `fetch_etm_weather_years.py` — script implementing §3–§5, runnable end-to-end,
  parameterized by weather year list and output path, with clear logging of any column
  it could not populate.
- `db_profiles_with_weather_years.xlsx` — output workbook: original sheets untouched,
  plus new `profiles_weather{1987,1997,2004}` and `_h` sheets, plus documentation of
  the new sheets.
- A short `README` note (can be a docstring or a `NOTES.md`) listing: the exact
  discovered input key/permitted values for weather year, which columns were
  successfully sourced from ETM vs. skipped, and the sanity-check results from §6.
