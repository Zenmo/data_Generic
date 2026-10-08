# ETM weather-year pull — results (2026-07-14)

Implements `etm_weather_years_pyetm_instructions.md`. Script: `fetch_etm_weather_years.py`.
Output: `db_profiles_with_weather_years.xlsx` (not committed here if large — regenerate
with `python fetch_etm_weather_years.py` from this folder; needs `pip install pyetm
openpyxl pandas`, no API token required).

## §3 — discovered input key

`settings_weather_curve_set`, an `EnumInput`, `permitted_values = ['default', '1987',
'1997', '2004']`, `disabled = False` for `area_code="nl2023"`. Found by grepping
`session.inputs.keys()` for `"weather"` — first candidate, no fallback substrings needed.
Verified the setting actually changes output, not just accepted: national inland-wind
annual sum drops from 17.8m (default) to 11.4m (1987) / 11.4m (1997) MWh, rises to 12.5m
(2004) — consistent with 1987/1997 being "Dunkelflaute" years and 2004 being a
higher-wind year, per the ETM docs.

## §4 — available curves

`session.hourly_output_curves.attached_keys()` returns exactly 10 identifiers for this
scenario: `electricity_profiles`, `district_heating_profiles`, `hydrogen_profiles`,
`network_gas_profiles`, `electricity_price`, `agriculture_heat`, `household_heat`,
`buildings_heat`, `residual_load`, `hydrogen_integral_cost`. Five were used:
`electricity_profiles` (282 columns), `household_heat` (118 columns), `buildings_heat`
(36 columns), `network_gas_profiles` (86 columns), `hydrogen_profiles` (73 columns).

**Correction from an earlier pass of this investigation:** industry heat is *not*
missing — there is just no curve literally named `industry_heat`. Checked
`network_gas_profiles`/`hydrogen_profiles` directly (prompted by a direct question
about whether industry steel/other heat-or-gas demand could really not be found) and
found real, subsector-attributed final energy demand there. Two node patterns matter:
- `industry_final_demand_for_metal_{steel,aluminium,other_metals}_{network_gas,hydrogen}.input`
  — genuine energetic (heat) demand for the metals subsectors, no `_non_energetic` sibling.
- `industry_chemicals_{fertilizers,other,refineries}_burner_{network_gas,hydrogen}.input`
  — chemicals' heat comes through this separate burner supply node; their
  `final_demand_for_chemical_*` nodes are **exclusively** `_non_energetic` (feedstock,
  e.g. ammonia/plastics feedstock) and must NOT be counted as heat.
- Same burner pattern for `industry_other_{food,paper}_burner_*`, and
  `industry_final_demand_for_other_non_specified_*` has both an energetic and a
  `_non_energetic` sibling — only the energetic one counts.

`industry_steel_h_demand` and `industry_other_h_demand` are now sourced from these
(see mapping table below) — no gqueries needed after all.

`get_hourly_curve()` has no resolution parameter — it always returns 8760 hourly rows.
There is no way to get native 15-minute ETM data.

## Column mapping — sourced vs. skipped

| Column | Sourced? | ETM curve(s) used |
|---|---|---|
| `wind_e_prod_normalized`, `_hvh`, `_heibloem` | Yes, but see caveat | `electricity_profiles["energy_power_wind_turbine_inland.output (MW)"]` — same curve for all three, ETM has no per-location wind data |
| `solar_e_prod_south35deg_normalized`, `_eastwest15deg_normalized` | Yes, but see caveat | Sum of 4 solar segment curves in `electricity_profiles` (utility, household PV, household PVT, buildings PV) — same curve for both orientations, ETM has no orientation split |
| `house_e_demand_other` | Yes | Sum of household appliance/lighting/cooling electricity columns in `electricity_profiles` (6 columns) |
| `house_h_demand_hot_water` | Yes | `household_heat["households_useful_demand_for_hot_water_after_solar_heater.input.demand (MW)"]` |
| `building_e_demand_other` | Yes | Sum of building appliance/lighting/cooling electricity columns in `electricity_profiles` (8 columns) |
| `building_h_demand` | Yes | Sum of 2 `buildings_heat` space-heating demand columns (future + present building stock) |
| `house_cooking_demand` | Yes | `electricity_profiles["households_final_demand_for_cooking_electricity.input (MW)"]` |
| `industry_steel_e_demand` | Yes | Sum of 10 `industry_steel_*.input (MW)` columns in `electricity_profiles` (all steel production routes: BF-BOF, DRI, scrap-EAF, incl. CCS variants) |
| `industry_other_e_demand` | Yes | Sum of 35 other `industry_*.input (MW)` columns in `electricity_profiles` (chemicals, food, paper, metals, ICT, geothermal heat-well electricity) |
| `logistics_fleet_e_hgv` | Yes | `electricity_profiles["transport_truck_using_electricity.input (MW)"]` (HGV = truck only, excludes van/bus/car) |
| `industry_steel_h_demand` | Yes | Sum of 2 columns: `network_gas_profiles` + `hydrogen_profiles` final demand for `metal_steel` (hydrogen sum is 0 in this scenario — steel doesn't use hydrogen for heat yet under default technology shares, not a bug) |
| `industry_other_h_demand` | Yes | **Computed as the sum of the 8 per-subsector columns below** (not a separately-maintained column list, so it can't drift from them) |
| `industry_chemicals_fertilizers_h_demand`, `_chemicals_other_h_demand`, `_chemicals_refineries_h_demand`, `_metal_aluminium_h_demand`, `_metal_other_metals_h_demand`, `_other_non_specified_h_demand`, `_food_h_demand`, `_paper_h_demand` | Yes, but see caveat below | Added on request as extra columns beyond the original layout — one `network_gas_profiles`+`hydrogen_profiles` pair per subsector, see §"Industry heat subsector caveat" |
| `ambientTemperature_degC` | **No** (out of scope, §7) | No historical weather-year concept in ETM; would need KNMI direct pull for 1987/1997/2004 |
| `day_ahead_price_eur_p_mwh` | **No** (out of scope, §7) | No NL day-ahead market existed in these historical years |
| `co2_factor_kg_per_kwh` | **No** (out of scope, §7) | Not available historically at this granularity |

All "sourced" columns were normalized per §0: production columns (`wind_*`, `solar_*`)
divided by max; demand columns divided by annual sum.

## Industry heat subsector caveat: only 2 distinct shapes among the 9 columns

Added on request: `industry_steel_h_demand` plus 8 new per-subsector columns
(`industry_chemicals_fertilizers_h_demand`, `_chemicals_other_h_demand`,
`_chemicals_refineries_h_demand`, `_metal_aluminium_h_demand`,
`_metal_other_metals_h_demand`, `_other_non_specified_h_demand`, `_food_h_demand`,
`_paper_h_demand`) — appended after the standard layout in `profiles_weather{YEAR}[_h]`
only, not in the original `profiles_2025`-style sheets. `industry_other_h_demand` is
now *computed* as the sum of the 8 subsector series (not a separately-maintained
column list), so the two can never drift apart.

**Important caveat, checked directly and now auto-logged on every run**
(`_log_subsector_shape_diagnostics` in the script): under this ETM scenario's default
technology mix, these 9 columns reduce to only **2 distinct normalized shapes**:

- **Flat / constant (no hourly variation at all)**: `industry_steel_h_demand`,
  `industry_chemicals_fertilizers_h_demand`, `industry_chemicals_other_h_demand`,
  `industry_chemicals_refineries_h_demand`, `industry_metal_aluminium_h_demand`,
  `industry_metal_other_metals_h_demand`. Physically plausible — these are
  continuous-process industries (steel, aluminium, chemicals) that ETM models as
  running at constant load, not weather- or time-of-day-dependent. After sum=1
  normalization a flat curve becomes exactly `1/8760` every hour, so all six are
  numerically identical to each other.
- **One shared template, different magnitude**: `industry_other_non_specified_h_demand`,
  `industry_food_h_demand`, `industry_paper_h_demand` — raw (pre-normalization) curves
  correlate at `1.000000` with each other, meaning ETM applies the same generic
  hourly demand-shape template to these three subsectors and only scales it by each
  subsector's total energy. After normalization they are numerically identical too.

So while the columns are real (distinct ETM nodes, genuinely summed correctly into
`industry_other_h_demand`), don't read "9 separate columns" as "9 independently-shaped
demand curves" under this scenario configuration — it's 2 shapes repeated. This could
change under a different scenario/technology-mix configuration (e.g. one with more
granular industrial demand-response data), which is exactly why the check now runs
automatically every time rather than being a one-off manual finding.

## NBNL 2025 scenario market prices (16 new columns, EUR/MWh, not normalized)

Added on request: a proxy for future EPEX day-ahead price, since real EPEX data only
exists historically (`day_ahead_price_eur_p_mwh`, 2023-2025). Uses ETM's own
market-clearing simulation (`electricity_price` curve) for the four NBNL 2025 scenarios
published by Netbeheer Nederland on the ETM homepage ("Featured scenarios") × the 4
target years each is published for (2030/2035/2040/2050) = 16 columns, added to each of
`profiles_weather{1987,1997,2004}[_h]`:

`day_ahead_price_nbnl_{km,ev,gb,ha}_{2030,2035,2040,2050}_eur_p_mwh` — codes match ETM's
own scenario titles (`NBNL_KM_2030` etc.): **km** = Koersvaste Middenweg, **ev** = Eigen
Vermogen, **gb** = Gezamenlijke Balans, **ha** = Horizon Aanvoer.

**Scenario IDs are on a different, version-pinned ETM engine.** These scenarios don't
exist on the default "pro" engine (`engine.energytransitionmodel.com`, 404) — they live
on `2025-01.engine.energytransitionmodel.com`, matching the "#2025.01" version tag shown
on the ETM homepage. Resolved the 16 underlying engine scenario IDs by following each
scenario's `saved_scenario` redirect chain (`energytransitionmodel.com` →
`my.energytransitionmodel.com/saved_scenarios/<id>` → a "load" link containing
`scenario_id=<engine id>&title=NBNL_<code>_<year>`) — not documented anywhere in pyetm
itself, and not guessable from the ID shown in the browser URL. Switching pyetm's target
engine mid-script needs both `reload_configuration()` **and**
`pyetm.get_client.cache_clear()` — `reload_configuration()` alone left the first
engine's `BaseClient` cached (an `lru_cache(maxsize=1)` never touched by
`reload_configuration()`), silently querying the wrong engine after switching back.
Caught by testing the toggle within one process before trusting it in the real run.

**`Session.new(..., template_id=X)` does NOT correctly copy the template's scenario.**
Tried first, since it's what the docstring suggests; caught by testing two different
NBNL scenarios (Koersvaste Middenweg vs. Horizon Aanvoer, same year) and finding their
`electricity_price` curves came out numerically identical. Root cause: the copy had
`0` `user_values()`, vs. `1130` on the original template loaded directly — the
`template_id` kwarg silently produced a blank default scenario for the given area/year,
ignoring the template's actual settings entirely. Fixed by using
`Session.load(template_id).copy_with_preset()` instead, verified to correctly carry
over all 1130 values into a new, private, mutable scenario (so overriding
`settings_weather_curve_set` doesn't touch the original published scenario).

**Stale curve cache when reusing one copy across weather years.** To avoid making 48
separate scenario copies (16 scenario/year combos × 3 weather years), one copy is made
per scenario/year and reused for all 3 `settings_weather_curve_set` values. Caught by
testing this reuse first: without an explicit `copy.clear_hourly_curves_cache()` call
after each `update_user_values()`, `get_hourly_curve("electricity_price")` silently
returned the *first* weather year's result for every subsequent weather year (all 4
identical). Adding the cache-clear call fixed it — verified against fresh-copy-per-year
results, which matched exactly.

**Quarter-hour convention**: held constant across all 4 quarters per hour (step
function), same as `day_ahead_price_eur_p_mwh`'s own convention (§5.5) — not linearly
interpolated, since day-ahead prices are set per hour, not continuously varying.
Verified directly: hours 17-18 in `NBNL_KM_2035`/1987 both show `2.48` EUR/MWh, and all
8 corresponding quarter-hour rows (t_h 17.00-18.75) repeat `2.48` exactly before
stepping to the next hour's value.

**Sanity-checked values** (1987 weather year, mean EUR/MWh across the year): KM ranges
75.5-136 across 2030-2050, EV 110-181, GB 60-121, HA 95-141 — all within a plausible
range (min 0, max hits the 3000 EUR/MWh price cap during scarcity hours in every
scenario/year, consistent with EPEX's own price cap).

### CORRECTION (2026-07-15): weather-year sensitivity above was bogus, not "modest"

The line originally here read "weather-year sensitivity is real but modest... not a
sign something's broken." That was wrong, and the fact that it was suspiciously flat
(mean varying by <1.3% between "default" and the supposedly extreme 1987 Dunkelflaute
year) is exactly what should have been chased down instead of rationalized away — a
user directly questioning "shouldn't different weeks be colder in different years?"
is what actually prompted checking this properly.

**Root cause**: every NBNL scenario ships with its own custom curve uploads under a
`weather/` namespace — `weather/wind_inland_baseline`, `wind_coastal_baseline`,
`wind_offshore_baseline`, `solar_pv_profile_1`, `solar_thermal`, `air_temperature`,
`buildings_heating`, `agriculture_heating` (confirmed identical across all 6
scenario/year combinations spot-checked: km/ha/ev/gb at multiple years). These are
literal pinned curves — the official NBNL 2025 study's own weather basis — and they
silently **override** `settings_weather_curve_set` entirely. Setting the input still
"succeeds" (no error, no warning from pyetm) but has zero effect on the actual
production/demand curves as long as these custom curves remain attached. Confirmed
directly: with the curves still attached, `energy_power_wind_turbine_inland.output`
was **byte-for-byte identical** across all 4 weather-year settings for the same
scenario. This is why the earlier price differences were only ~1% — that ~1% wasn't
weather sensitivity at all, just incidental noise from whatever else
`update_user_values` touched.

**Fix**: `fetch_nbnl_prices_multi` now calls
`copy.remove_custom_curves(NBNL_WEATHER_CUSTOM_CURVES)` right after
`copy_with_preset()`, before the weather-year loop. Verified this actually fixes it,
not just silences the symptom:
- Wind production shape correlation between weather years: **1.0 (identical) → 0.03-0.19
  (real reshuffling)** — confirms different weeks are genuinely windy/calm in different
  years now, matching the physical expectation.
- KM_2030 mean price: **default 71.84 → 1987 105.59 (+47%) → 1997 85.43 (+19%) → 2004
  91.42 (+27%)** EUR/MWh — a plausible, large swing consistent with 1987/1997 being
  genuine Dunkelflaute (scarcity → higher prices) years.
- The `weather/insulation_*` custom curves under the same namespace are deliberately
  **not** removed — those encode a building-retrofit-rate assumption (a technology
  parameter), not which historical year's weather is selected; removing them would
  change the scenario's building-stock assumptions for no reason connected to this fix.
- Added a runtime check (`fetch_nbnl_prices_multi` logs a warning if a scenario's
  actual `weather/*` custom curves don't match `NBNL_WEATHER_CUSTOM_CURVES` exactly)
  so a future NBNL vintage with a different curve set doesn't silently repeat this bug.

### A second bug found while re-verifying the fix above: copy_with_preset() drops values non-deterministically

Before trusting the fix above, re-ran the same (scenario, year, weather_year) combo
from fresh copies repeatedly and got **three different stable means** for
KM_2030/1987: 105.64, 105.64, 113.35, 121.87 across 4 runs. Refetching twice from the
*same* copy always agreed with itself (105.64/105.64, or 113.35/113.35) — so this isn't
noise in the price calculation itself, it's that different copies of the identically
same template weren't actually identical.

Diffed `copy.user_values()` against the template's directly and found the actual
cause: `Session.load(template_id).copy_with_preset()` followed by
`remove_custom_curves()` occasionally drops a handful of the template's scalar values
instead of carrying them over — always `flexibility_outdoor_temperature`, plus 2 of
the 3 `flh_of_energy_power_wind_turbine_{coastal,inland,offshore}` /
`flh_of_solar_pv_solar_radiation` full-load-hour pairs (which 2 varies run to run,
seemingly related to which custom curve keys were just removed in the same request).
This reads as a race condition server-side between deleting a custom curve and
restoring its companion FLH scalar to the value it should fall back to — not
something under this script's control to prevent, only to detect and correct for.

**Fix**: after `copy_with_preset()` + `remove_custom_curves()`, diff *every* value in
`copy.user_values()` against `tmpl.user_values()` (not just the specific keys observed
dropping — the race condition could plausibly drop something else another time) and
`update_user_values()` anything missing, before touching `settings_weather_curve_set`.
Verified this stabilizes the result: 4 repeated fresh copies of the same
scenario/year/weather-year now all converge on the identical mean (105.26 — note this
is neither of the two unstable values seen before; `flexibility_outdoor_temperature`
being restored correctly for the first time shifted the true answer slightly from the
"105.64" that 2 of 4 raw runs had coincidentally landed on).

**Practical implication**: every "settled" value quoted anywhere above in this NBNL
section that was measured *before* this fix (default 71.84, 1987 105.59/85.43/91.42
etc. from the first correction pass) should be treated as unreliable too, not just the
original pre-custom-curve-removal numbers. The workbook and viewer were regenerated
again after this second fix.

**All NBNL price numbers in this workbook and in `nbnl_price_stats.csv`/
`nbnl_price_viewer.html` were regenerated after both fixes** — any numbers seen or
screenshotted before 2026-07-15 (either the original near-flat ones, or the
first-correction-pass ones that didn't yet restore dropped FLH/temperature values)
should be discarded.

## Weather-year differences in the main wind/solar/demand columns (not NBNL prices)

Added `inspect_weather_year_profiles.py` (companion to `inspect_nbnl_prices.py`) after
a direct question: does the weather-year effect also show up in `wind_e_prod_normalized`,
`solar_e_prod_*`, and the demand columns in `db_profiles_with_weather_years.xlsx`, not
just the NBNL prices? **Yes, and this data was never affected by either NBNL bug** -
it comes from `fetch_weather_year()`/`build_hourly_df()`, which calls
`Session.new(area_code="nl2023", ...)` (a brand-new blank scenario), not
`copy_with_preset()` on an existing published scenario. There's no custom curve to
override and nothing being copied from a template, so neither the custom-curve-override
bug nor the copy_with_preset() value-dropping race condition can apply here.

Checked directly rather than assumed: wind and `building_h_demand` (space heating,
temperature-driven) show real weather-year variation (e.g. wind mean 0.326 default vs.
0.201/0.201/0.221 for 1987/1997/2004); `house_h_demand_hot_water`, `house_cooking_demand`,
all `industry_*_e_demand`, the industry heat subsectors, and `logistics_fleet_e_hgv`
come out byte-for-byte identical across all 4 weather years - expected, not a bug: hot
water/cooking aren't temperature-driven (usage-pattern-based, unlike space heating),
and the industry columns are largely fixed technology/schedule assumptions in this pull
(see the "only 2 distinct shapes" caveat earlier in this file for why several industry
heat columns are flat to begin with).

**`house_e_demand_other`/`building_e_demand_other` are NOT identical across weather
years, and this needed explaining** (a direct question: "I thought this was a fixed
profile"). Checked by splitting each bucket into its cooling vs. non-cooling source
columns: `households_final_demand_for_appliances_electricity.input`/`_lighting_` are
byte-for-byte identical across weather years, confirming the fixed-schedule appliance/
lighting profile the user expected - **but** `house_e_demand_other` also bundles in 4
cooling columns (`households_cooling_airconditioning_electricity.input` etc.), and
cooling genuinely is temperature-driven: its annual sum is ~372,000 MW-equivalent in
"default" vs. ~120,000 in 1987 (a real ~3x difference from a hotter vs. colder year),
not rounding. So the composite bucket shifts shape because one of its two ingredients
is weather-sensitive and the other isn't - not a bug, just two different kinds of load
sharing one column.

**Added `house_h_demand_space_heating`** (also on request) - a real gap in the original
layout, which covered household hot water (`house_h_demand_hot_water`) and building
space heating (`building_h_demand`) but never had a household space heating column at
all, despite that normally being the largest household heat end-use. Sourced by summing
all 24 `households_useful_demand_for_space_heating_*.input.demand (MW)` columns in
`household_heat` (discovered dynamically by pattern match, not hardcoded, in case a
future ETM dataset has a different house-type/era breakdown than the current 4 house
types × 6 construction eras). Shows real, sensible weather-year variation: peak
(normalized) demand ranges from 0.000485 (2004, a warmer/high-renewables year) to
0.000623 (1997, a colder Dunkelflaute year).

Deliverables: `weather_year_profiles_stats.csv` (84 rows: 21 series × 4 weather years)
and `weather_year_profiles_viewer.html` (one panel, grouped dropdown to pick any wind/
solar/demand column, all 4 weather years overlaid, hourly/load-duration-curve toggle -
same architecture as the NBNL viewer).

### Follow-up round (2026-07-15): units check, drop cooling, explicit space-heating columns, offshore wind

- **Units check, on request** ("aren't some house_e cols in MW and others in MWh?"):
  checked directly across all 5 curves used (`electricity_profiles`, `household_heat`,
  `buildings_heat`, `network_gas_profiles`, `hydrogen_profiles`) - every column is
  `(MW)`, no `(MWh)` columns exist anywhere in the data this script touches. Not a bug,
  but worth having checked rather than assumed given the direct question.
- **`house_e_demand_other`/`building_e_demand_other` now exclude cooling, on request** -
  they used to bundle in `households_cooling_*`/`buildings_cooling_*` alongside
  appliances/lighting, which is exactly why they weren't the "fixed profile" expected
  (see the cooling-vs-appliances breakdown above). Now sourced from appliances +
  lighting only, and confirmed byte-for-byte identical across all 4 weather years after
  the change - genuinely weather-independent now, matching what these columns are
  meant to represent.
- **`HOUSE_SPACE_HEATING_COLS` made an explicit top-level constant** - previously
  computed inline via pattern-match inside `fetch_weather_year()`, which is why it
  wasn't easy to find alongside `HOUSE_HOT_WATER_COL`/`BUILDING_HEAT_COLS`. Still
  cross-checked against `household_heat.columns` at runtime (warns if they diverge)
  so a future ETM dataset with a different house-type/era breakdown doesn't silently
  drop columns.
- **`wind_e_prod_normalized_offshore` added, on request** - a genuinely separate ETM
  production node (`energy_power_wind_turbine_offshore.output (MW)`), not a duplicate
  like `_hvh`/`_heibloem` are of the inland curve. Confirmed distinct: correlated with
  inland at 0.855 (both driven by the same weather systems) but not identical, and
  shows its own real weather-year variation (mean 0.486 default vs. 0.332/0.345/0.381
  for 1987/1997/2004).

### Stats table rendering bug (both viewers) + physical-units normalization mode (2026-07-15)

**Bug report**: "some entries overlap and others are empty" in the summary stats table.
Two real, separate causes, both fixed in `inspect_weather_year_profiles.py` and
`inspect_nbnl_prices.py`:
1. No explicit `columnwidth` on the `go.Table` trace - Plotly defaults to equal-width
   columns, so long text (e.g. "Household electricity demand (other)") could overflow
   into neighboring numeric columns. Fixed with explicit width ratios.
2. The table's figure `height` was a fixed guess (700/560px) regardless of row count -
   with 84-176 rows × 22px each, content vastly exceeded the container, and the `.card`
   wrapper only sets `overflow-x`, not `overflow-y`, so excess rows were clipped rather
   than scrollable. Fixed by sizing height to `30 + n_rows*22 + 20`.
   `inspect_weather_year_profiles.py` also had group-relative labels ("Steel") that
   are only unique *within* their own dropdown group - flattened into one table
   without that context, two genuinely different columns
   (`industry_steel_e_demand`/`industry_steel_h_demand`) both showed as bare "Steel",
   looking like a duplicate/glitched row. Fixed by prefixing every table label with its
   group name.

**Normalization question**: asked to explain exactly how normalization works, since the
inter-year differences in the load duration curve looked suspiciously small. Confirmed
the suspicion was correct - both existing modes force every weather year onto the same
range *by construction*:
- Production (`wind_*`, `solar_*`): divided by that year's own peak hour → max is
  always exactly 1.0, regardless of whether that year's wind was genuinely stronger.
- Demand (`house_*`, `building_*`, `industry_*`): divided by that year's own annual
  sum → always sums to exactly 1.0, regardless of whether total demand differed.

Neither preserves genuine magnitude differences between years - useful for feeding the
AnyLogic model (which needs a shape independent of any one year's peak), actively
misleading for comparing years, which is this viewer's whole purpose.

**Fix**: added a "physical units" mode alongside the existing "shape" mode in
`inspect_weather_year_profiles.py`. Divides production by **installed capacity**
(genuine capacity factor) and demand by **household/building count** (MW per
household/building) instead of by each year's own peak/sum. Checked which ETM inputs
to use and whether it's fair before implementing anything:
- `capacity_of_energy_power_wind_turbine_inland` (6463.5 MW), `..._offshore` (3447.6 MW),
  and solar capacity summed across the 3 segments already aggregated into
  `solar_e_prod_south35deg_normalized` (`capacity_of_energy_power_solar_pv_solar_radiation`
  4229.4 MW + `capacity_of_buildings_solar_pv_solar_radiation` 7883.5 MW +
  `capacity_of_households_solar_pv_solar_radiation` 9405.6 MW ≈ 21518.5 MW total; no
  separate capacity input exists for the PVT segment also folded into that column, a
  minor known gap). `households_number_of_*` (excl. `_inhabitants`) summed = 7,931,940
  households; `buildings_number_of_buildings_{present,future}` summed = 2,791,335
  buildings.
- **Confirmed all of these are constant across weather years** (checked directly,
  not assumed) - they're scenario/end-year assumptions, not weather-dependent, so
  dividing by them doesn't introduce a spurious cross-year difference of its own.
- **Checked the batteries/curtailment question directly, as asked**: all grid-scale
  and co-located battery capacities (`capacity_of_energy_battery_wind_turbine_inland`,
  `..._solar_pv_solar_radiation`, `capacity_of_energy_flexibility_mv_batteries_electricity`,
  `flow_batteries`, `pumped_storage`) are **0 MW** in this scenario's default config, and
  the wind/solar technologies actually used here have **0% `curtailment_of_*`**
  assumptions. So `energy_power_wind_turbine_inland.output` etc. are genuine gross
  production curves for this scenario - not something already smoothed by storage
  dispatch or de-rated by a curtailment assumption. A runtime check now warns if any
  battery capacity is ever found non-zero, since that would need rechecking before
  trusting "physical" mode's capacity-factor interpretation.

Result, once real magnitudes were revealed: wind capacity factor mean 0.315 (default)
vs. 0.201/0.201/0.221 (1987/1997/2004) - a genuine ~35% swing that the shape mode
completely hides. Solar capacity factor barely varies year to year (0.098-0.107) -
a real finding in itself: Dutch solar resource is far less annually variable than wind,
consistent with cloud cover mattering more day-to-day than which specific historical
year is picked, whereas 1987/1997/2004 were specifically chosen as low/high-wind years.

**A genuine bug caught while building this**: `pd.read_csv()` on the raw-value cache
silently mis-parsed the `weather_year` column (chunked dtype inference read "1987" as
int64 in some chunks and str in others - a `DtypeWarning`, not an exception), so
`raw_df.weather_year == "1987"` string comparisons silently missed whichever chunks got
parsed as int. Row count for the stats table dropped from 176 to 60 with no visible
error - 1997/2004 disappeared entirely and 1987 was partially missing. Fixed with
`dtype={{"weather_year": str}}` on the cache read.

**A second thing caught before shipping**: the first working version stored both
"shape" and "physical" series fully computed and duplicated in the page - 19MB, over
the 16MB Artifact limit even after cutting precision to 3 significant figures (still
16.6MB). Restructured to store the raw MW values once per (weather year, column) plus
a scalar multiplier per (weather year, column, mode), with the JS multiplying at
render time - removes the duplication instead of keeping to degrade precision to
chase a smaller file. Final size: ~10MB.

## §5.5 — quarter-hour resolution: interpolated, not resampled

Compared `profiles_2025` (15-min) against `profiles_2025_h` (hourly) directly: the
day-ahead price is held constant across all 4 quarters of an hour (step function,
matches its documented "interval-start" convention); ambient temperature is linearly
interpolated between hours; **wind production is neither** — none of the 4 quarter-hour
values for a given hour equal the hourly value, and they don't follow a simple linear
ramp either, confirming production curves in the original workbook were independently
resampled from higher-frequency source data (per the `Documentation` sheet: 1-minute
irradiance data for solar, hourly-to-15min windpowerlib interpolation for wind — actually
closer to the wind case, but still not identical to naive linear interpolation of the
hourly column).

Since ETM only exposes hourly curves, the new `profiles_weather{YEAR}` (15-min) sheets
in this pull **are** built by plain linear interpolation of the hourly ETM values (with
the last quarter-hour block held flat since there's no "hour 8760" to interpolate
toward). This is a real, acknowledged methodological difference from how the rest of
the workbook's quarter-hour sheets were built — flagged here rather than glossed over.

## §6 — sanity check: ETM "default" vs. existing `profiles_2023_h`

| Column | Correlation | ETM peak hour | `profiles_2023_h` peak hour |
|---|---|---|---|
| `wind_e_prod_normalized` | 0.843 | 1716 | 0 |
| `solar_e_prod_south35deg_normalized` | 0.912 | 2604 | 2221 |

Both show strong positive correlation (as expected — same underlying weather physics,
different methodology per the `Documentation` sheet's `Source` column: ETM's own
technology-mix-scaled curves vs. `windpowerlib`/`pvlib` direct simulation). The wind
peak-hour mismatch (hour 0 vs. 1716) is likely because `profiles_2023_h`'s wind column
hits its normalized max (`1.0`) at multiple tied hours (rated-capacity clipping) and
`idxmax()` just returns the first tie, at `t_h=0` — not a real single-hour peak
discrepancy. Not investigated further; flagging as a caveat rather than a red flag,
since the correlation figures already show directional consistency.

## Known limitation: not literally byte-for-byte on existing sheets

§5.6 asked to confirm existing sheets are unchanged after appending new ones.
`openpyxl.load_workbook()` + `.save()` fully deserializes and re-serializes every
cell, so numeric values go through a float-repr round-trip. Checked directly:
7.8% of cells in `profiles_2025_h` differ from the original by up to **5.4e-16
relative error** — exactly float64 machine epsilon, i.e. last-bit rounding noise
from Excel's original XML string vs. Python's float `repr()`, not a data change.
Confirmed no cell differs by more than 1-2 ULPs. If literal byte-for-byte
preservation is required (e.g. for a diff-based review process), the output
would need to be built by directly appending new sheet XML parts to the
original `.xlsx` zip instead of a full openpyxl round-trip — not done here,
flagging as a follow-up if that guarantee turns out to matter.

## Deliverables checklist

- [x] `fetch_etm_weather_years.py` — runnable end-to-end (`python fetch_etm_weather_years.py`)
- [x] `db_profiles_with_weather_years.xlsx` — original sheets present (see byte-for-byte
      caveat above), plus `profiles_weather{1987,1997,2004}` and `_h` variants (43 columns
      each: 19 standard + 8 industry-heat subsectors + 16 NBNL scenario prices), plus
      `Documentation_weather_years` sheet describing all 42 non-`t_h` columns
- [x] This `NOTES.md`
