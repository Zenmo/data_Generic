# db_profiles.xlsx — investigation notes (2026-07-14)

`db_profiles_loader/` was empty when this investigation started — no `.md`
instructions, no code, nothing in git history (`git log --all -- db_profiles_loader`
and `git log --all --diff-filter=A --name-only` both come up empty for this
folder). This file records what's actually in `db_profiles.xlsx` and what's
still unknown before a loader can be built. No CSV export or Java parser has
been written yet — see "Open questions" below for why.

## What's in `db_profiles.xlsx`

10 sheets (parsed directly from the underlying zip/XML with stdlib `zipfile`
+ `xml.etree`, since `openpyxl`/`pip` aren't available in this environment):

| Sheet | Rows (excl. header) | Resolution |
|---|---|---|
| `Documentation` | 19 | — column definitions, see below |
| `profiles_2025` | 35,040 | quarter-hour (365 days × 96) |
| `profiles_2025_h` | 8,760 | hourly (365 days × 24) |
| `profiles_2024` | 35,040 | quarter-hour, 365-day variant |
| `profiles_2024_h` | 8,760 | hourly, 365-day variant |
| `profiles_2024_366_days` | 35,136 | quarter-hour, 366-day (leap) variant |
| `profiles_2024_366_days_h` | 8,784 | hourly, 366-day (leap) variant |
| `profiles_2023` | 35,040 | quarter-hour |
| `profiles_2023_h` | 8,760 | hourly |
| `weather2023` | 8,760 | hourly, older/smaller schema (see below) |

Note: the `_h` sheets have a raw Excel dimension (e.g. `A1:S15962`) larger
than their real row count — the extra rows are empty formatting artifacts
(no actual cell values), not missing data. Confirmed by checking cell
content, not just row count.

### Columns in `profiles_YYYY[_h]` sheets (18 data columns + `t_h` index)

`t_h`, `wind_e_prod_normalized`, `solar_e_prod_south35deg_normalized`,
`solar_e_prod_eastwest15deg_normalized`, `ambientTemperature_degC`,
`day_ahead_price_eur_p_mwh`, `house_e_demand_other`,
`house_h_demand_hot_water`, `building_e_demand_other`, `building_h_demand`,
`house_cooking_demand`, `industry_steel_e_demand`, `industry_steel_h_demand`,
`industry_other_e_demand`, `industry_other_h_demand`,
`logistics_fleet_e_hgv`, `wind_e_prod_normalized_hvh`,
`wind_e_prod_normalized_heibloem`, `co2_factor_kg_per_kwh`.

Per the `Documentation` sheet: most are normalized (0-1) production/demand
shape profiles sourced from ETM, pvlib, windpowerlib, KNMI, ENTSO-E and NED;
`day_ahead_price_eur_p_mwh` and `co2_factor_kg_per_kwh` are absolute values.
Conventions vary per column (interval-start vs. interval-end vs.
accumulative) — see the sheet itself for the full per-column notes, including
known caveats (e.g. day-ahead price interpolation between hours is flagged
as something that "should be holded instead of interpolated").

### `weather2023` — smaller/older schema

Only 5 columns: `t_h`, `wind_e_prod_normalized`, `solar_e_prod_normalized`
(single orientation, not split south/east-west), `ambientTemperature_degC`,
`Day-ahead Price [EUR/MWh]`. Looks like a predecessor of the newer
`profiles_YYYY` sheets — no demand/CO2/multi-site-wind columns yet.

## Open questions before building a loader

Unlike the other `*RowParser.java` loaders in `data_Generic/`, there is
**no existing consumer of this data** anywhere in the codebase:

- `grep -r "db_profiles\|profiles_2025\|profiles_2024\|profiles_2023"` across
  all `.java`/`.md`/`.py` files in the repo returns nothing.
- The model's actual profile lookup, `f_createElectricityProfiles.java`, uses
  `energyModel.f_findProfile("default_house_electricity_demand_fr")` — a
  different, `_fr`-suffixed naming convention, not the column names in this
  spreadsheet. So it's not a drop-in replacement for what's already wired up.
- `old_data/db_profiles_old.xlsx`, `old_data/_profiles.xlsx`,
  `old_data/_energy_profiles.xlsx`, `old_data/_dhw_profile.xlsx` suggest this
  has been reworked before (commit `186ba0c04 "Update pv profiles"`), but
  none of those predecessors are referenced by code either.

So before writing a CSV export + `*RowParser.java` (the pattern every other
loader in this folder follows — semicolon-delimited CSV, one row parser per
entity, see `data_loader/README.md`), these need answers:

1. **Which year/resolution should the model actually use?** (2023 final vs.
   2024 vs. 2025; hourly vs. quarter-hourly.) The model's timestep isn't
   established from this file alone.
2. **How do these 18 columns map onto the model's existing profile names**
   (the `_fr`-suffixed ones in `f_createElectricityProfiles.java`), or is
   this meant to *replace* that mechanism rather than feed into it?
3. **Where should the exported CSV(s) live** — `data_Generic/` root (direct
   model input, per the "9 files" rule in `data_loader/README.md`) or
   `processed_data_from_loader/` (intermediate)?
4. Is a Java `*RowParser.java` even the right target, or does this data get
   consumed a different way (e.g. `energyModel.f_findProfile` reads it
   directly from a CSV at runtime, no custom parser class needed)?

Wrote this note instead of guessing at a CSV export + parser, since getting
any of the four points above wrong means throwaway work — flag the intended
integration point and this can move straight to implementation.
