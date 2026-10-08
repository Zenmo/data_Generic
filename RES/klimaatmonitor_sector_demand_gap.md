# Klimaatmonitor sector demand: gap vs. official totals

> **This is the investigation log** — how the gap was found, measured and
> closed, in chronological order. For the resulting *method* as it now works,
> see the section "Sectorale elektriciteit/aardgas" in
> `data_loader/README.md`, which documents the reconciliation steps, the
> output columns, the coverage report and the validation results.

Investigation from 2026-08-12. Summarizes why the model's company (agriculture
+ industry + services) electricity/gas demand doesn't match the headline
totals in a Klimaatmonitor "Thema's" export, and whether switching to
RES-region-level queries would fix it.

## Background

Compared `model_log.txt`'s headless-run output against a Klimaatmonitor
"Thema's - Drechtsteden.xlsx" export (2023 column, region-wide totals).

| | Model (sum of 7 gemeenten) | Klimaatmonitor Thema's (2023) | Gap |
|---|---|---|---|
| Households electricity | 318.7 GWh | 306.3 GWh (net) / 334.0 GWh (incl. PV) | close, within ~5% |
| Companies electricity | 650.9 GWh | 870.2 GWh (net) / 896.7 GWh (incl. PV) | **~25% low** |
| Companies gas | 97.6 million m³ | 118.3 million m³ (incl. SBI D) | **~17% low** |
| Households gas | not found in log (line shows all-zero, looks like a display/timing artifact, not a real value) | 104.5 million m³ | unverified |

## Root cause found

1. **Buurten/gemeenten aggregation is correct — not a model bug.** Re-summed
   `kerncijfers_gemeenten_2023.csv` for the 7 Drechtsteden municipalities
   directly: 651.7 GWh electricity / 97.6 million m³ gas, matching the
   model's buurten-level aggregate within ~0.1%. `model_log.txt` also
   self-reports `"OK: all 7 municipalities reconcile within tolerance"`.

2. **The gap is upstream, in the source data itself**, and traces to two
   different Klimaatmonitor products disagreeing:
   - `kerncijfers_gemeenten_2023.csv` (what the model loads) comes from
     Klimaatmonitor's **Open Data Service**, sectoral (SBI-code) breakdown
     per municipality — see
     `data_processing_scripts/make_energieverbruik_sector_csv.py`.
   - The **"Thema's" export** uses different, higher totals. Its own "Bron"
     section says: *"Optelling en bijschatting door RVO o.b.v.
     CBS-gegevens"* — RVO sums the raw CBS numbers **and then applies a
     correction ("bijschatting") for known gaps**. The sectoral ODS variables
     the pipeline uses do not have that correction applied.
   - Concrete evidence of one mechanism: `gas_verbruik_a_m3` (agriculture
     gas) is exactly 0 for 5 of 7 municipalities in the sectoral CSV — CBS
     privacy suppression when too few businesses report in a
     sector/municipality cell. Region-wide that's 139k m³ vs. Thema's
     1.637 million m³ for the same category (~12x gap from suppression
     alone in that one sector).
   - Households match well because they're computed differently (avg.
     consumption × housing stock — no per-sector suppression involved).

## Where the missing values actually are (2026-08-12, follow-up)

Measured directly on `kerncijfers_gemeenten_2023.csv` / `_2024.csv`
(342 municipalities, 30 RES regions). Counting cells that are NaN **or**
exactly 0, per 8-group column:

| kolom | 2023 ontbrekend | 2024 ontbrekend | regio's met ≥1 nul-gemeente | regio's volledig nul |
|---|---|---|---|---|
| `gas_verbruik_a_m3` | 35 (10.2%) | 35 (10.2%) | 12 / 30 | 0 |
| `gas_verbruik_hj_m3` | 13 (3.8%) | 12 (3.5%) | 9 / 30 | 0 |
| `elec_verbruik_a_kwh` | 8 (2.3%) | 6 (1.8%) | 4 / 30 | 0 |
| `gas_verbruik_mn_m3` | 3 (0.9%) | 3 (0.9%) | 2 / 30 | 0 |
| `gas_verbruik_bf_m3`, `gas_verbruik_kl_m3` | 2 (0.6%) | 1–2 | 2 / 30 | 0 |
| `elec_verbruik_bf/gi/ru`, `gas_verbruik_gi/oq/ru` | 1 (0.3%) | 0–2 | 1 / 30 | 0 |
| `elec_verbruik_hj/kl/mn/oq` | 0 | 0–1 | 0 / 30 | 0 |

No NaNs anywhere — suppression always surfaces as an exact 0.

**This is the important negative result:** at the 8-group level the gaps are
far too small to explain the observed shortfall, and **no RES region has all its
municipalities at zero for any group**. Drechtsteden's `gas_verbruik_a_m3` (the
mechanism called out above) is worth ~1.5 million m³ of the ~20.7 million m³
gas gap — roughly 7%. So a fair-share fallback that only fills *group-level*
zeros would recover almost nothing.

**The loss sits one level deeper.** The pipeline sums ~33 individual SBI-letter
variables (`vbrze_b`, `vbrze_ctot`, `vbrze_g1`, …) into each of the 8 groups. A
suppressed letter contributes 0 to that sum, so the group total still looks
populated while being understated, and the suppression becomes invisible once
the CSV is written. That is consistent with the national picture: summing the
CSV gives 63.9 TWh of business electricity and 10.8 billion m³ of business gas
for the whole of NL — both well below the national figures.

The fix therefore has to reconcile **per SBI letter, before aggregation**.

## RES-region-level query — would it help?

Asked whether importing the sectoral fields at RES-region level (Drechtsteden
as a whole) instead of per-municipality would reduce suppression.

- **Not currently possible to confirm from documentation.** The pipeline
  (`make_energieverbruik_sector_csv.py:381`) hardcodes
  `GeoLevels('gemeente')` for every `vbrze_*`/`vbrzg_*` variable — it has
  never tried a coarser geo level. The Klimaatmonitor ODS handleiding page
  only documents `gemeente` and `buurt22` as examples; it doesn't enumerate
  all supported GeoLevels. Querying the `GeoLevels` entity set directly needs
  a valid API key (none present in this repo — `secrets.local.json` doesn't
  exist here).
- **Circumstantial evidence it should exist:** the Thema's export itself is
  scoped to "Drechtsteden" as a region, so Klimaatmonitor's platform clearly
  supports RES-region views — just unconfirmed whether that's exposed for
  the same sectoral SBI variables via the OData API specifically.
- **Even if it works, it's not a drop-in fix:**
  - It would only fix the *suppression* component of the gap, not the
    RVO "bijschatting" correction component — which is a separate,
    unrelated effect baked into the Thema's numbers regardless of geo
    level.
  - It collapses per-municipality granularity to one region-wide number
    per sector. The model currently uses per-gemeente sector totals to
    distribute demand down to municipalities and then neighborhoods; a
    region total would need to be redistributed some other way (e.g.
    proportional to `companyLocations<sector>` counts, already tracked per
    neighborhood).

### Measured live, 2026-08-13 (diagnose_sector_geolevels.py, 39 variables x 2 years)

Klimaatmonitor publishes ten geo levels — `buurt, gemeente, nederland, omgeving,
postcode, provincie, res, res_landsdeel, subres, wijk` — and **every** `vbrze_*`
/ `vbrzg_*` variable is offered at `gemeente, nederland, omgeving, provincie,
res, subres`. The RES-region level is called **`res`** (31 areas), not `regio`.
`subres` gives 41 areas, `provincie` 13.

Average share of missing/zero cells per level:

| level | areas | gem. % ontbrekend |
|---|---|---|
| gemeente | 343 | **18.8%** |
| subres | 41 | 12.9% |
| provincie | 13 | 13.2% |
| res | 31 | 14.0% |
| nederland | 1 | 0.0% |
| omgeving | 31 | 42.0% |

75 of 78 variable-years have at least one municipality missing. Worst offenders
at gemeente level (all NaN — the row is absent, not zero):

| variabele | groep | % ontbrekend |
|---|---|---|
| `vbrze_u` / `vbrzg_u` | ru | 99.1–99.4% |
| `vbrzg_b` | bf | 98.3–98.5% |
| `vbrze_b` | bf | 80.2–80.5% |
| `vbrzg_afval` | bf | 77.8–78.7% |
| `vbrzg_j` | hj | 35.3–35.9% |
| `vbrze_d` | bf | 28.3–32.1% |
| `vbrzg_l` | kl | 25.9% |

**This confirms the hypothesis.** The 8-group CSV showed 0.3–10% gaps; the
underlying letters are missing 19% of the time on average and up to 99% for
individual letters. Summing letters into groups hid all of it.

Caveat on reading that table: a high percentage is not automatically
suppression. SBI U (extraterritorial organisations) genuinely does not exist in
most municipalities, and gas for SBI B (mining) is genuinely rare — for those
the missing cells are real zeros and the residual will correctly be near zero.
The residual, not the missing count, is what determines how much gets added.

**Size of the prize: 11.9%.** Summing all municipalities gives 150.03 bn units
against 170.28 bn from the area totals — one eighth of demand currently
disappears between the two. (That first run measured against `provincie`; the
script now uses `res`.)

### GeoLevels: resolved from the Swing ODS documentation

The Klimaatmonitor ODS is a standard Swing/Jive ODS, and the vendor
documentation settles two questions the Klimaatmonitor handleiding left open:

- `GeoLevels` is queryable both globally (`/GeoLevels`) and **per variable**
  (`/Variables('vbrze_a')/GeoLevels`) — so the pipeline can ask which levels a
  given SBI variable is actually published at, instead of guessing.
- There is an `all` parameter: `GeoLevels('all')` returns **every** geo level in
  one response. So fetching gemeente + regio + provincie + nederland together
  costs the *same* number of HTTP requests as the current gemeente-only version.

That removes the main objection to the region-level approach: it is no longer a
choice between per-municipality granularity and regional coverage — both arrive
in the same request, and the municipal figures stay the primary source.

## Implemented fix (2026-08-12)

`make_energieverbruik_sector_csv.py` now does **residual reconciliation** rather
than simple zero-filling. Per SBI-letter variable and year:

1. Fetch with `GeoLevels('all')` — municipal *and* area totals, one request.
2. `residual = area total − sum of that area's municipalities`.
3. Distribute a positive residual over the municipalities that look suppressed
   (value 0 or absent), weighted by their **company locations for that SBI
   group** (CBS KWB — the same counts `make_buurten_csv.py` already uses to push
   demand down to neighbourhoods, so region → gemeente → buurt share one
   allocation basis). Municipalities that reported a real figure are never
   touched. If nothing looks suppressed, the residual is spread over all
   members. A negative residual is a no-op — never shrink a reported figure.
4. **RES level only** — `_KM_RECONCILIATIE_LADDER = ["res"]`. No province or
   national rung, deliberately. The model runs at RES level, and when a RES
   total is itself suppressed a province total cannot say *which* RES region
   inside that province the extra demand belongs to. Escalating would invent
   spatial structure the source data does not contain. Those cases are left
   untouched and reported instead (see below).

   Measured cost of that choice: reconciling at `res` closes 4.1% of the gap
   (150.03 → 156.49 bn units), where reconciling at `provincie` would have
   closed 11.9% (→ 170.28 bn). The ~7.8 pp difference is demand that is
   suppressed at RES level too. It is not silently dropped — it is quantified
   in the coverage report so it can be accounted for separately.

   Klimaatmonitor identifies areas by internal ids (`res_17`), which match
   neither `RES_code` nor `province_code` in municipalities.xlsx. The pipeline
   therefore translates ids to names via `GeoLevels('<level>')/GeoItems` and
   matches on the normalised name, logging how many areas paired
   ("Gebiedskoppeling") — a name mismatch would otherwise silently disable the
   whole mechanism while producing plausible-looking output.

   `subres` (41 areas, 12.9% missing) is both finer and better-populated than
   `res` and would be the better first rung, but municipalities.xlsx has no
   gemeente → subres mapping. Worth adding if that mapping can be sourced.

Set `RECONCILIEER_MET_REGIO = False` to restore the old gemeente-only behaviour.

### Validation against the Thema's export (Drechtsteden 2023)

After RES reconciliation, compared per the three categories the model actually
uses — A (landbouw), B-F (industrie), G-U (diensten):

| | model | Thema's | gat |
|---|---|---|---|
| elec A | 7.42 GWh | 7.42 | **0.0%** |
| elec B-F + G-U | 674.67 GWh | 862.76 | 21.8% |
| elec totaal | 682.09 GWh | 870.18 | 21.6% |
| gas A | 1.637 mln m³ | 1.637 | **0.0%** |
| gas G-U | 36.41 mln m³ | 37.00 | **1.6%** |
| gas B-F (afgeleid) | 62.30 mln m³ | 79.65 | 21.8% |
| gas totaal | 100.34 mln m³ | 118.29 | 15.2% |

Agriculture matches **exactly** on both carriers — gas was 0.14 vs 1.637 before
reconciliation, a 12x gap, now closed. Services gas is within 1.6%. So
effectively the entire remaining gap is industry: 17.36 of the 17.95 mln m³.

Two known causes both sit in industry:

- **`vbrzg_d` does not exist in Klimaatmonitor at all** — energy-sector gas is
  not tracked (404 on their API, noted in the module docstring since 2026-07).
  The Thema's gas figure is explicitly *incl. SBI D*, so the model can never
  contain it at any geo level, and no reconciliation will recover it.
- **`vbrze_d`, `vbrzg_b` and `vbrzg_f` have no RES total for Drechtsteden**, so
  those municipal values pass through unreconciled.

### Final rung: Thema's regional control totals — nationwide via the API

`RECONCILIEER_MET_THEMA = True`. The control totals come from the ODS itself, so
this covers **all RES regions** with no manual downloads
(`probe_klimaatmonitor_aggregaten.py` found the codes on 2026-08-13; both
reproduce the downloaded Drechtsteden 2023 export to 0.00%):

| carrier | variabele | naam | dekking |
|---|---|---|---|
| elec | `vbrze_tot` | Totaal elektriciteitsverbruik bedrijven en instellingen, geleverd via openbaar net | 31/31 regio's |
| gas | `vbrzg_tot` | Totaal aardgasverbruik bedrijven/instellingen (**excl. SBI D**) | 19/31 regio's |

National check, 2023: electricity model 69.45 TWh vs control 71.59 TWh — a **3.0%**
gap. So Drechtsteden's 21.6% is a strong outlier, i.e. locally suppressed large
consumers rather than a general correction.

**Why gas uses the excl-SBI-D variable.** `vbrzg_tot_incl_sbid` covers all 31
regions and totals 23.06 mld m³ against 14.59 in the model — but that ~8.5 mld m³
difference is essentially SBI D, Energievoorziening: gas burned in power
stations. That is fuel input to electricity generation, not industrial end-use
demand. Folding it into the industry group would make `bf` 75% of all business
gas and double-count against any generation modelled separately. The 12 regions
without a published excl-SBI-D value keep RES letter-level reconciliation.

Beware the unit-suffixed twins in the catalogue (`vbrze_tot_gwh`, `_tj`,
`vbrzg_tot_mm3`, ...) — same figure, other units. The pipeline works in kWh/m³,
so always the unsuffixed code.

Downloaded `Thema's - <regio>.csv` exports in `RES/` are still read and take
precedence over the API for the region they name — useful for a manual override,
though in practice the two agree exactly.

Mechanism: after aggregation to the 8 groups, the region's total per carrier is
compared against the export's control total, and the whole positive residual is
assigned to group `bf` (industry), weighted across the region's municipalities
by their company locations for that group. Never reduces a value, never
extrapolates to other regions.

Assigning everything to industry is justified by the table above, not by
convenience: A is exact, services are within 1.6%, and both structural causes
are industrial. Columns read: `Totaal elektriciteitsverbruik bedrijven en
instellingen, geleverd via openbaar net` and `Totaal aardgasverbruik
bedrijven/instellingen (incl. SBI D)`.

Export quirks handled: headers repeat in display units (`870,2`) and base units
(`870181000`) — the largest value per header is taken, and scaled by 1e6 only if
it is still below 1e6 (GWh→kWh and mln m³→m³ are both exactly that factor).
Decimal commas and `?`/empty cells are handled.

Caveat for 2024: the export also carries 2024 totals, so 2024 will close to them
as well — but since 40% of 2024 gas is unattributable at RES level, far more of
that year is estimated. Check `elec_bijgeschat_aandeel` / `gas_bijgeschat_aandeel`
before using 2024.

### Coverage report: what is knowingly NOT included

`processed_data_from_loader/energieverbruik_sector_res_dekking.csv`, written on
every run, one row per SBI-letter variable × year × gap. Two statuses:

- `res_ontbreekt` — the RES region publishes no total for that variable/year, so
  its municipalities keep their own suppressed figures and nothing was added.
  `waarde_gemeenten` is what the model actually uses for that region.
- `niet_toewijsbaar` — the national total exceeds the sum of all RES totals.
  That difference is demand Klimaatmonitor knows exists but attributes to no RES
  region. `ontbrekend_bedrag` is the amount; sum it per `carrier` to get the
  total the model is knowingly missing. `nederland` is never suppressed (0%
  missing), so this is always computable and is an honest upper bound.

The run also logs any RES region whose name doesn't match between Klimaatmonitor
and municipalities.xlsx, in both directions — those regions get no reconciliation
at all, so they are named explicitly rather than left in a match count. The first
live run matched 28 of 31.

New provenance columns (deliberately outside the `elec_verbruik_`/`gas_verbruik_`
prefix namespace so downstream prefix lookups don't mistake them for a 9th
sector): `elec_bijgeschat_<unit>`, `gas_bijgeschat_<unit>`,
`elec_bijgeschat_aandeel`, `gas_bijgeschat_aandeel`, `bijschatting_niveaus`.

Also fixed: `_discover_schema()` raised `ModuleNotFoundError` when `cbsodata`
wasn't installed, instead of returning None and falling back to Klimaatmonitor.

### Diagnostic

`diagnose_sector_geolevels.py` (new, read-only) answers the open question this
document could not: how often the *regional* value is missing when the municipal
one is. It enumerates GeoLevels globally and per variable, pulls all 33
variables at every level, and writes three CSVs — `diagnose_sector_geolevels.csv`,
`..._missing.csv` (n areas / n zero / n absent per variable × year × level) and
`..._residual.csv` (region total vs. sum of municipalities). It prints an explicit
verdict on which rung the data supports:

- regional missing < 5% → fair-share at region level is enough;
- 5–25% → keep the province rung for the regions that are missing;
- \> 25% → go to province/nederland directly, **or** aggregate to coarser SBI
  groups first (i.e. reconcile at the 8-group level rather than per letter,
  which trades sector precision for coverage).

Run it before trusting the reconciled output:

```
python diagnose_sector_geolevels.py 2023 2024
```

(It could not be run from the analysis sandbox: `klimaatmonitor.databank.nl` is
outside the sandbox's network allowlist. It needs to run wherever the pipeline
normally runs.)

## TODO

- [ ] Run `diagnose_sector_geolevels.py 2023 2024` and record: for which SBI
      letters is regional itself missing, and how often. If regional coverage is
      poor, decide between the province rung and coarser SBI aggregation.
- [ ] Re-run `--gemeenten-potentie` then `--buurten`, and re-compare the
      7 Drechtsteden totals against the Thema's export. Expect the electricity
      and gas gaps to shrink but not close — see the next item.
- [ ] Separately: decide whether/how to reconcile against the RVO
      "bijschatting"-corrected Thema's totals at all. That correction is applied
      by RVO at every geo level, so reconciliation fixes the suppression half of
      the gap only and some residual difference will remain by construction.
- [ ] Sanity-check `elec_bijgeschat_aandeel` / `gas_bijgeschat_aandeel` per
      municipality after the run — a small municipality showing a very high
      imputed share is a sign the company-location weights are doing too much
      work there.
