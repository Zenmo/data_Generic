# Data loader pipeline

Downloads national open datasets (WarmteAtlas, CBS, ElaadNL, RIVM, TVW), matches each feature to CBS buurt-, gemeente-, PC4- en PC6-grenzen voor de jaren 2023, 2024 en 2025, en exporteert de resultaten naar `../processed_data_from_loader/`.

---

## Mappenstructuur

`data_Generic/` root bevat **alleen bestanden die het model direct inleest** —
alles wat een tussenresultaat is (met WKT-geometrie voor gemeenten, gedateerde
bestandsnamen, Excel-varianten) staat in `processed_data_from_loader/`. Zie
"Bestanden voor het model" hieronder voor de exacte lijst en waar elk bestand
vandaan komt.

```
data_Generic/
├── AlbatrossProcessedVehicleTrips.csv    ← statische invoer, niet door de pipeline gegenereerd
├── inputECookerPatterns.csv              ← statische invoer, niet door de pipeline gegenereerd
├── inputTruckTripPatterns.csv            ← statische invoer, niet door de pipeline gegenereerd
├── kerncijfers_buurten_<jaar>_met_geometrie.csv   ← kopie voor het model (2023, 2024), bijgewerkt door --buurten
├── kerncijfers_gemeenten_<jaar>.csv               ← voor het model (2023, 2024), geen WKT, bijgewerkt door --gemeenten-potentie
├── windturbines_<jaar>.csv               ← kopie voor het model (2023, 2024), bijgewerkt door --windturbines
├── oude_bestanden/                       ← hier terechtgekomen legacy .xlsx-varianten en de oude gemeente-WKT-kopieën (niet meer gebruikt, niet automatisch bijgewerkt)
├── processed_data_from_loader/        ← pipeline-tussenresultaten (CSVs, gitgetracked)
│   ├── <LaagNaam>_nl_<datum>.csv
│   ├── <LaagNaam>_onbekend_match_<datum>.csv
│   ├── windturbines_<jaar>_<datum>.csv    ← gedateerd; laatste versie wordt gekopieerd naar data_Generic/
│   ├── buurten/
│   │   └── kerncijfers_buurten_met_geometrie_<jaar>.csv   ← bron voor de model-kopie hierboven
│   ├── gemeenten/
│   │   └── kerncijfers_gemeenten_<jaar>_met_geometrie.csv ← WKT-versie, alleen hier (niet in data_Generic/)
│   └── ...
└── data_loader/
    ├── Hoofdverwarmingsinstallaties_woningen_2022_2024.xlsx   ← vereiste invoer (CBS)
    ├── municipalities.xlsx              ← PV/wind potentie, provincie/RES-regio
    ├── Java importers/                ← Java-loaders voor AnyLogic
    │   ├── J_Neighborhood.java
    │   ├── NeighborhoodImporter.java
    │   ├── J_Municipality.java
    │   ├── MunicipalityImporter.java
    │   └── WindTurbineImporter.java
    ├── data_processing_scripts/       ← alle Python-scripts
    │   ├── run_pipeline.py
    │   ├── config.py
    │   ├── download_sources.py
    │   ├── process_features.py
    │   ├── make_buurten_csv.py
    │   ├── make_gemeenten_potentie_csv.py
    │   ├── make_energieverbruik_sector_csv.py
    │   ├── make_verwarmingsinstallaties_csv.py
    │   ├── make_elaadnl_csv.py
    │   ├── make_bedrijfswagens_csv.py
    │   ├── make_windturbines_csv.py
    │   ├── make_solar_csv.py
    │   ├── make_tvw_csv.py
    │   ├── make_warmtetransitie_csv.py
    │   ├── cbs_kwb.py
    │   ├── mapping_kolomnamen_CBS.xlsx
    │   ├── secrets.local.json         ← API-keys (gitgenegeerd — nooit committen)
    │   └── requirements.txt
    ├── raw/                           ← ruwe downloads (gitgenegeerd)
    ├── logs/                          ← pipeline-logs (gitgenegeerd)
    └── old_data/                      ← archief (gitgenegeerd)
```

---

## Bestanden voor het model (data_Generic/ root)

`data_Generic/` root bevat precies deze 9 bestanden, niets meer:

| Bestand | Herkomst |
|---|---|
| `AlbatrossProcessedVehicleTrips.csv` | Statisch, handmatig beheerd |
| `inputECookerPatterns.csv` | Statisch, handmatig beheerd |
| `inputTruckTripPatterns.csv` | Statisch, handmatig beheerd |
| `kerncijfers_buurten_2023_met_geometrie.csv` | Kopie, geschreven door `make_buurten_csv.py` na elke `--buurten` run |
| `kerncijfers_buurten_2024_met_geometrie.csv` | Idem |
| `kerncijfers_gemeenten_2023.csv` | Geschreven door `make_gemeenten_potentie_csv.py` na elke `--gemeenten-potentie` run |
| `kerncijfers_gemeenten_2024.csv` | Idem |
| `windturbines_2023.csv` | Kopie, geschreven door `make_windturbines_csv.py` na elke volledige pipeline-run of `--windturbines` |
| `windturbines_2024.csv` | Idem |

**Belangrijk:** deze 5 gegenereerde bestanden worden bij elke run **overschreven**
(niet gedateerd) — het model leest dus altijd de nieuwste data van een vast pad.
Alles wat een tussenresultaat is (WKT-versie van gemeenten, gedateerde
windturbine-bestanden, Excel-varianten van buurten/windturbines) staat in
`processed_data_from_loader/`, niet in `data_Generic/` root:

| Tussenresultaat | Locatie |
|---|---|
| `kerncijfers_gemeenten_<jaar>_met_geometrie.csv` (WKT, 28 MB) | `processed_data_from_loader/gemeenten/` |
| `kerncijfers_buurten_met_geometrie_<jaar>.csv` (bron van de model-kopie) | `processed_data_from_loader/buurten/` |
| `windturbines_<jaar>_<datum>.csv` (gedateerd, bron van de model-kopie) | `processed_data_from_loader/` |

Oude `.xlsx`-varianten (`kerncijfers_buurten_2023.xlsx`, `windturbines_nl_2026.xlsx`)
en de oude gemeente-WKT-kopieën die ooit in `data_Generic/` root stonden, zijn
verplaatst naar `data_Generic/oude_bestanden/` — niets is verwijderd, maar dit
wordt niet meer automatisch bijgewerkt.

`municipalities.xlsx` hoort hier expliciet **niet** bij — dat is invoer, geen
output, en staat in `data_loader/` (zie "Vereist invoerbestand" hieronder).

---

## Pipeline draaien

```bash
# Navigeer naar de scripts-map
cd data_Generic/data_loader/data_processing_scripts

# Installeer afhankelijkheden (eenmalig)
pip install -r requirements.txt

# Volledige pipeline (download + verwerken)
python run_pipeline.py

# Alleen downloaden
python run_pipeline.py --download

# Alleen verwerken (als raw data al aanwezig is)
python run_pipeline.py --process

# Bestelauto/vrachtauto-schatting per buurt (RDW + CBS 85236NED) — voor --buurten
python run_pipeline.py --bedrijfswagens

# Capaciteitskaart (RNB+TenneT transformers) downloaden + koppelen aan buurten
python run_pipeline.py --capaciteitskaart

# Alleen buurten-CSV genereren
python run_pipeline.py --buurten

# Gemeente-CSV: PV/wind-potentie + provincie/RES-regio + sectorale energie (CBS 82538NED, valt terug op Klimaatmonitor)
python run_pipeline.py --gemeenten-potentie
python run_pipeline.py --gemeenten-potentie --overmorgen   # + Over Morgen wind-kruischeck/aanvulling
```

Logs worden weggeschreven naar `data_loader/logs/pipeline_<datum>.log`.

`--gemeenten-potentie` draait nooit mee in de volledige pipeline (geen vlag) — altijd expliciet aanroepen.
Er is geen aparte `--energieverbruik-sector` fase: sectorale elektriciteit/aardgas wordt live opgehaald
en direct in `kerncijfers_gemeenten_<jaar>.csv` geschreven zodra je `--gemeenten-potentie` draait. Wil je
deze data ook in de buurten-CSV, draai dan **eerst** `--gemeenten-potentie` en **daarna** `--buurten`
(die leest de sectorale-energiekolommen terug uit `kerncijfers_gemeenten_<jaar>.csv`).

---

## Vereist invoerbestand

| Bestand | Locatie | Reden |
|---------|---------|-------|
| `Hoofdverwarmingsinstallaties_woningen_2022_2024.xlsx` | `data_loader/` | CBS publiceert verwarmingstypen niet via OData/WFS |
| `municipalities.xlsx` | `data_loader/` (of `data_Generic/`, beide werken) | PV/wind-potentie + provincie/RES-regio — niet automatisch te downloaden |

> **Let op**: `municipalities.xlsx` is ooit verplaatst van `data_Generic/` naar
> `data_Generic/data_loader/`. `make_gemeenten_potentie_csv.py` zoekt op **beide**
> plekken (`_find_muni_xlsx()`), dus het werkt in elke locatie — maar als dit
> bestand ooit ergens anders terechtkomt, faalt `--gemeenten-potentie` stil met
> alleen een ERROR-regel in de log (`Kon potentie-data niet laden uit
> municipalities.xlsx`) en wordt `kerncijfers_gemeenten_<jaar>.csv` gewoon niet
> herschreven — het oude bestand blijft dan onopgemerkt staan. Zie je verouderde
> kolommen in de output, check dan eerst of dit bestand nog op een van de twee
> verwachte plekken staat.

Alle andere data wordt automatisch gedownload.

---

## API-key voor Klimaatmonitor (fallback bij CBS-uitval)

`make_energieverbruik_sector_csv.py` (aangeroepen vanuit `make_gemeenten_potentie_csv.py`
tijdens `--gemeenten-potentie`) probeert eerst CBS StatLine tabel `82538NED`. Staat die
(zoals momenteel) uit, dan valt het terug op de Regionale Klimaatmonitor Open Data
Service. Daarvoor is een gratis persoonlijke API-key nodig:

1. Vraag een key aan via het contactformulier op
   <https://klimaatmonitor.databank.nl/content/handleiding-open-data-service>
   (naam, organisatie, e-mailadres).
2. Zet de key in `data_processing_scripts/secrets.local.json` (dit bestand
   staat in `.gitignore` — commit 'm nooit):
   ```json
   { "klimaatmonitor_apikey": "<jouw key>" }
   ```
3. Ontbreekt dit bestand, dan logt het script een duidelijke waarschuwing en
   faalt de Klimaatmonitor-poging netjes (geen crash) — CBS blijft dan de
   enige bron, en die faalt op zijn beurt ook netjes als hij niet bereikbaar is.

De API-key wordt als HTTP-header `apikey` meegestuurd (niet als query-parameter —
dat lijkt in eerste instantie op "geen authenticatie mogelijk" omdat de server
dan altijd `401 Guest user group not found` teruggeeft).

---

## Bronnen en endpoints

| Bron | Endpoint | CRS |
|------|----------|-----|
| WarmteAtlas (RVO) | `https://www.warmteatlas.nl/geoserver/WarmteAtlas/wfs` | EPSG:28992 |
| CBS Wijk/Buurt 2023 | `https://service.pdok.nl/cbs/wijkenbuurten/2023/wfs/v1_0` | EPSG:28992 |
| CBS Wijk/Buurt 2024 | `https://service.pdok.nl/cbs/wijkenbuurten/2024/wfs/v1_0` | EPSG:28992 |
| CBS Wijk/Buurt 2025 | `https://service.pdok.nl/cbs/wijkenbuurten/2025/wfs/v1_0` | EPSG:28992 |
| CBS PC4/PC6 2023–2024 | `https://service.pdok.nl/cbs/postcode4/{year}/wfs/v1_0` | EPSG:28992 |
| CBS Kerncijfers (OData) | `cbsodata` Python package | — |
| ElaadNL EV-prognoses | ElaadNL API (per buurt, gecached in `raw/elaadnl/`) | — |
| CBS sectorale energie (82538NED) | `cbsodata` Python package | — |
| Klimaatmonitor ODS (fallback) | `https://klimaatmonitor.databank.nl/jiveservices/odata` | — |
| municipalities.xlsx (PV/wind, provincie/RES) | lokaal Excel-bestand, `data_loader/` of `data_Generic/` | — |
| Over Morgen wind-potentie (optioneel) | `http://maps.geocoders.nl/overmorgen/ows` (WFS) | EPSG:28992 |

> **CBS PC4/PC6 2025**: CBS heeft nog geen 2025-versie gepubliceerd (stand juni 2026).
> De pipeline slaat 2024-data automatisch op onder het 2025-pad en logt een waarschuwing.
> Controleer periodiek of een 2025-versie beschikbaar is en pas `PC_YEAR_FALLBACK` in `config.py` aan.

---

## Sectorale elektriciteit/aardgas: van 21 SBI-secties naar 8 groepen

CBS's kerncijfers wijken en buurten geeft bedrijfsvestigingen per buurt al in
**8 vaste groepen**, niet per losse SBI-sectie (deze kolommen bestaan al in de
buurten-CSV, van vóór deze feature):

| Buurten-kolom | Groepcode | SBI-secties in deze groep |
|---|---|---|
| `a_landbouw_bosbouw_en_visserij` | `a` | **A** |
| `bf_nijverheid_en_energie` | `bf` | **B, C, D, E, F** |
| `gi_handel_en_horeca` | `gi` | **G, I** |
| `hj_vervoer_informatie_en_communicatie` | `hj` | **H, J** |
| `kl_financiele_diensten_onroerend_goed` | `kl` | **K, L** |
| `mn_zakelijke_dienstverlening` | `mn` | **M, N** |
| `oq_overheid_onderwijs_en_zorg` | `oq` | **O, P, Q** |
| `ru_cultuur_recreatie_overige_diensten` | `ru` | **R, S, T, U** |

Samen dekken deze 8 groepen alle 21 SBI 2008-secties (A t/m U) — dit is exact
CBS's eigen indeling, dus de vestigingen-tellingen die al in de buurten-CSV
staan (`bedrijfsvestigingen_totaal` en de 8 kolommen hierboven) zijn compleet.
`make_energieverbruik_sector_csv.py` en `_verdeel_energieverbruik_sector()` in
`make_buurten_csv.py` hergebruiken exact deze 8 groepcodes (`a`/`bf`/`gi`/`hj`/
`kl`/`mn`/`oq`/`ru`) zodat vestigingen-telling en energieverbruik altijd
dezelfde sleutel gebruiken.

De vraag is dus niet "zijn alle SBI-secties er" (ja, bij de vestigingentelling),
maar "heeft elke groep ook energiedata om te verdelen". Dat hangt af van welke
bron het script gebruikt:

### Bron 1 — CBS StatLine 82538NED (primair, momenteel niet bereikbaar)

De branche-dimensie van deze tabel wordt bij elke run automatisch uitgelezen
(`_discover_branch_groups()`): elke categorie-titel (bv. `"C Industrie"`) wordt
op de beginletter(s) gematcht naar bovenstaande 8 groepen. Dit betekent dat
**als CBS zelf alle 21 secties los publiceert — inclusief T — dat automatisch
wordt meegenomen** zodra deze tabel weer bereikbaar is; er is niets hardcoded
dat een sectie zou uitsluiten. Nooit live geverifieerd doordat de tabel al
sinds vóór 2026-07-06 "TijdelijkNietBeschikbaar" is bij CBS zelf (bevestigd via
zowel hun OData-API als de publieke StatLine-website, vanaf twee onafhankelijke
netwerken) — geen alternatieve CBS-tabel met dezelfde regio+SBI-combinatie
bestaat.

### Bron 2 — Klimaatmonitor Open Data Service (fallback, live geverifieerd)

Klimaatmonitor publiceert losse variabelen per SBI-*letter* (niet per groep),
hardcoded in `_KM_ELEC_VARS` / `_KM_GAS_VARS` na live verificatie op
2026-07-07:

| Groep | Elektriciteit-variabelen (kWh) | Aardgas-variabelen (m³) |
|---|---|---|
| `a`  | `vbrze_a` (A) | `vbrzg_a` (A) |
| `bf` | `vbrze_b`, `vbrze_ctot`, `vbrze_d`, `vbrze_afval`, `vbrze_f` (B,C,D,E,F) | `vbrzg_b`, `vbrzg_ctot`, `vbrzg_afval`, `vbrzg_f` (B,C,E,F — **geen D**) |
| `gi` | `vbrze_g1`, `vbrze_i` (G,I) | `vbrzg_g1`, `vbrzg_i` (G,I) |
| `hj` | `vbrze_h`, `vbrze_j` (H,J) | `vbrzg_h`, `vbrzg_j` (H,J) |
| `kl` | `vbrze_k`, `vbrze_l` (K,L) | `vbrzg_k`, `vbrzg_l` (K,L) |
| `mn` | `vbrze_m`, `vbrze_n` (M,N) | `vbrzg_m`, `vbrzg_n` (M,N) |
| `oq` | `vbrze_o`, `vbrze_p`, `vbrze_q` (O,P,Q) | `vbrzg_o`, `vbrzg_p`, `vbrzg_q` (O,P,Q) |
| `ru` | `vbrze_r1`, `vbrze_s`, `vbrze_u` (R,S,U — **geen T**) | `vbrzg_r1`, `vbrzg_s`, `vbrzg_u` (R,S,U — **geen T**) |

**Twee bevestigde, echte gaten in de brondata** (geen keuze van dit script,
geverifieerd door alle 40 verwachte variabelen individueel op te vragen bij
Klimaatmonitor):

1. **SBI D (Energievoorziening), aardgas** — `vbrzg_d` bestaat niet (404).
   Energiebedrijven verbruiken zelf kennelijk geen aardgas dat apart wordt
   bijgehouden; elektriciteit voor SBI D wel (`vbrze_d`, bestaat wél). Geldt
   voor zowel CBS (naam van `gas_bcdef` sluit SBI D expliciet uit) als
   Klimaatmonitor.
2. **SBI T (Huishoudens als werkgever)** — geen enkele Klimaatmonitor-variabele
   bestaat hiervoor (elektriciteit, gas, noch vestigingenaantal). Geverifieerd:
   nationaal is het aantal bedrijfsvestigingen in SBI T t.o.v. het totaal
   **0,2%** (4.697 van 2.358.440, CBS kerncijfers 2023) — verwaarloosbaar.
   `ru`-groep krijgt dus wél zijn volledige R+S+U-energieverbruik, alleen T's
   (nihil) aandeel ontbreekt.

Deze twee gaten raken alleen de **energie**-toewijzing, niet de
vestigingen-telling (die blijft compleet, want die komt uit de buurten-CSV
zelf). Het praktische effect: de `bf`- en `ru`-groepstotalen per gemeente zijn
een fractie lager dan de werkelijkheid (D resp. T ontbreken), en dat plant zich
evenredig door naar alle buurten binnen die gemeente omdat de verdeling
proportioneel gebeurt.

### Verdeling naar buurten

Zie `_verdeel_energieverbruik_sector()` in `make_buurten_csv.py`: elke buurt
krijgt `(buurt_vestigingen_groep / som_geldige_buurt_vestigingen_groep_in_gemeente)
× gemeente_energieverbruik_groep`. Buurten met `-99999` (geen data) tellen niet
mee in de som en krijgen 0.

**Wat betekent `energieverbruik_sector_gelijk_verdeeld`?** Normaal verdeel je
naar rato van vestigingenaantal (10 van de 100 vestigingen in de buurt → 10%
van het gemeente-verbruik). Maar als een hele gemeente voor een bepaalde
SBI-groep **geen enkele buurt met een bekend vestigingenaantal** heeft (alle
buurten staan op `-99999` voor die groep), dan is er geen basis om naar rato
te verdelen — 0 gedeeld door 0 vestigingen levert geen percentage op. In dat
uitzonderingsgeval verdeelt het script het gemeente-verbruik voor die groep
dan maar **gelijk over alle buurten** in die gemeente (ieder evenveel), in
plaats van het verbruik gewoon te laten vervallen. Deze kolom (`True`/`False`
per buurt) markeert precies welke buurten dit is overkomen, zodat je kunt zien
waar de verdeling een schatting is in plaats van een echte naar-rato-berekening.
In de praktijk komt dit zelden voor: bij de test op 2023-data gebeurde dit voor
0 van de 14.421 buurten (elke gemeente had voor elke groep minstens één buurt
met bekende vestigingen).

### Eenheden en totalen

Elke `elec_verbruik_<groep>` / `gas_verbruik_<groep>` kolomnaam eindigt met de
daadwerkelijke eenheid van de bron die die run heeft gebruikt, bv.
`elec_verbruik_a_kwh`, `gas_verbruik_a_m3`. **Klimaatmonitor levert elektriciteit
in kWh en aardgas in m³ — niet in TJ** (geen TJ-variant bestaat per aparte
SBI-letter bij Klimaatmonitor; alleen elektriciteit voor sector A heeft daar
toevallig ook een TJ-variabele, maar die wordt hier niet gebruikt). CBS's
eigen eenheid voor tabel 82538NED is nog niet geverifieerd (tabel al sinds
vóór 2026-07-06 onbereikbaar) — als CBS ooit weer beschikbaar komt en een
andere eenheid gebruikt, veranderen de kolomnamen dus mee tussen runs. Dat is
bewust zo: alle code die deze kolommen leest (`_verdeel_energieverbruik_sector()`
in Python, `MunicipalityImporter.java`) zoekt op **prefix** (`elec_verbruik_a_`),
nooit op de volledige naam inclusief eenheid — dus dit breekt niets.

Naast de 8 groep-kolommen bevat `kerncijfers_gemeenten_<jaar>.csv` ook
`elec_verbruik_totaal_<eenheid>` en `gas_verbruik_totaal_<eenheid>` — de som
van alle 8 groepen. Hiermee kun je controleren dat de optelling van de buurten
van een gemeente (in de buurten-CSV) precies gelijk is aan het gemeente-totaal:
`som(buurten.elec_verbruik_<groep>) == gemeente.elec_verbruik_totaal_<eenheid>`
voor elke groep samen. Geverifieerd op echte data (Dordrecht, 132 buurten):
beide kwamen exact overeen (238.740.000 kWh).

---

## Kolomdefinities output-CSV

### Matchvelden

| Kolom | Beschrijving |
|-------|--------------|
| `buurtcode_2023` | CBS buurtcode op 1-1-2023 (bv. `BU00340000`) |
| `buurtcode_2024` | CBS buurtcode op 1-1-2024 |
| `buurtcode_2025` | CBS buurtcode op 1-1-2025 |
| `gemeentecode_2023` | CBS gemeentecode 2023 (bv. `GM0034`) |
| `gemeentecode_2024` | CBS gemeentecode 2024 |
| `gemeentecode_2025` | CBS gemeentecode 2025 |
| `pc4_code_2023` | PC4-postcode-vlak 2023 |
| `pc6_code_2023` | PC6-postcode-vlak 2023 |
| `buurtcode_gewijzigd` | `True` als buurtcode verschilt tussen jaren |
| `gemeentecode_gewijzigd` | `True` als gemeentecode verschilt tussen jaren |

### Geometrievelden (altijd EPSG:4326)

| Kolom | Beschrijving |
|-------|--------------|
| `lat` / `lon` | Coördinaten van puntfeatures |
| `centroid_lat` / `centroid_lon` | Centroïde van lijn- of polygoonfeatures (berekend in EPSG:28992) |
| `geom_wkt` | Volledige polygoongeometrie als WKT-string |

### Sectorale energie (buurten-CSV en gemeenten-CSV)

Zie het hoofdstuk hierboven voor de volledige uitleg van de groepindeling en
de twee bekende gaten (SBI D gas, SBI T).

| Kolom | Beschrijving |
|-------|--------------|
| `elec_verbruik_a_<eenheid>` … `elec_verbruik_ru_<eenheid>` | Elektriciteitsverbruik per SBI-groep. `<eenheid>` is wat de bron die run daadwerkelijk gaf (geverifieerd: Klimaatmonitor = `kwh`) |
| `gas_verbruik_a_<eenheid>` … `gas_verbruik_ru_<eenheid>` | Aardgasverbruik per SBI-groep (geverifieerd: Klimaatmonitor = `m3`, **niet** TJ) |
| `elec_verbruik_totaal_<eenheid>` / `gas_verbruik_totaal_<eenheid>` | *(alleen gemeenten-CSV)* Som van alle 8 groepen — vergelijk met de som van de buurten-kolommen om de verdeling te controleren |
| `elec_eenheid` / `gas_eenheid` | Dezelfde eenheid nogmaals als losse kolom (voor programmatische toegang zonder de kolomnaam te hoeven parsen) |
| `energie_bron` / `energie_bron_url` | Welke bron dit specifieke bestand daadwerkelijk heeft geleverd (CBS of Klimaatmonitor) |
| `energieverbruik_sector_gelijk_verdeeld` | *(alleen buurten-CSV)* `True` als de gemeente geen enkele buurt had met bekend vestigingenaantal voor die groep, en het verbruik daarom gelijk verdeeld is over alle buurten in plaats van naar rato — zie uitleg hierboven |

> **Let op**: de `<eenheid>`-suffix kan tussen runs verschillen (CBS vs. Klimaatmonitor
> gebruiken mogelijk niet dezelfde eenheid) — lees deze kolommen dus altijd op prefix
> (`elec_verbruik_a_`), nooit op de volledige naam.

### Gemeenten-CSV — overige nieuwe velden

| Kolom | Beschrijving |
|-------|--------------|
| `province`, `provinciecode`, `res_regio`, `res_regiocode` | Uit `municipalities.xlsx`, blad `municipalities` |
| `pv_potentie_*`, `pv_kans2030`, `pv_potentie_bron(_url)` | Zon-potentie, uit `municipalities.xlsx` (oorspronkelijk RVO/Kadaster/NP RES, niet publiek downloadbaar) |
| `wind_potentie_*`, `wind_vermogen_per_turbine_kw`, `wind_potentie_bron(_url)` | Wind-potentie, uit `municipalities.xlsx` (oorspronkelijk Over Morgen / Nationale Energie Atlas) |

---

## Voertuiggegevens: totalen, EV-prognoses en het "realization"-scenario

### Totalen

`personenautos_totaal` komt rechtstreeks uit CBS kerncijfers wijken en buurten.
`bestelautos_totaal` en `vrachtautos_totaal` **bestaan niet als CBS-veld** — CBS
publiceert per buurt alleen personenauto's, geen bestel- of vrachtauto's. Deze
twee kolommen worden daarom geschat door een aparte pipeline-fase,
`make_bedrijfswagens_csv.py` (`--bedrijfswagens`) — zie de sectie
"Bedrijfswagens-schatting" hieronder voor de volledige methode en caveats.
Zonder die fase geregeld te draaien zoekt `NeighborhoodRowParser` hier
tevergeefs naar (`col()` geeft `-1` terug) en komen `totalVans`/`totalTrucks`
op `0` uit.

### Bedrijfswagens-schatting (bestelauto's en vrachtauto's per buurt)

Implementeert `data_loader/bedrijfswagens-per-buurt-schatting.md`, met één
bewuste afwijking van de daar genoemde bron — zie hieronder waarom.

**Databronnen:**

| Bron | Wat | Beperking |
|---|---|---|
| RDW Open Data "Brandstoffen_op_PC4" (Socrata dataset `8wbe-pu7d`) | Actueel (maandelijks) gecombineerd aantal bedrijfsauto's per PC4 | Geen bestelauto/vrachtauto-onderscheid — RDW's `voertuigsoort`-veld heeft hier maar één waarde, "Bedrijfsauto", voor beide (geverifieerd tegen een live steekproef) |
| CBS StatLine `85236NED` ("Motorvoertuigen actief; voertuigtype, postcode, regio, 1 januari, 2019-2023") | Officiële bestelauto/vrachtauto-splitsing, per PC4 **en** per gemeente | Bevroren op 1-1-2023; nog niet bijgewerkt naar een latere jaargang (stand 2026-07) |
| CBS Kerncijfers wijken en buurten (`cbs_kwb.haal_kwb`) | `bedrijfsvestigingen_totaal` / `hj_vervoer_informatie_en_communicatie` per buurt, als dasymetrisch gewicht | Proxy, geen directe voertuigtelling |
| CBS Wijk-en-buurtkaart + CBS PC4-vlakken | Ruimtelijke overlay PC4 ↔ buurt | Onafhankelijk vastgestelde grenzen, overlappen zelden netjes |

**Waarom niet puur RDW, zoals de opdracht voorstelt?** De opdracht
(`bedrijfswagens-per-buurt-schatting.md`) gaat uit van een RDW-dataset die
bestelauto en vrachtauto apart zou publiceren per postcode. Bij uitvoering
bleek dat niet zo te zijn: RDW's enige publieke PC4-datasetvoor
bedrijfsvoertuigen (`8wbe-pu7d` — waar de "Voertuigen met brandstoffen per
postcode"-pagina uit de opdracht, dataset-ID `ivky-pcsj`, zelf een Socrata
"story" die naar dit dataset linkt, naar verwijst) heeft geen gewicht-/
EU-categorieveld om de twee uit elkaar te trekken. In plaats daarvan wordt RDW's actuele, gecombineerde
PC4-telling gesplitst met het officiële CBS-aandeel uit `85236NED` voor
diezelfde PC4 (valt terug op het gemeente-aandeel, dan het landelijke aandeel,
als de PC4 niet in de CBS-tabel voorkomt). Dit combineert RDW's actualiteit
met CBS's officiële splitsing, tegen de prijs van een aanname: het
bestelauto/vrachtauto-aandeel wordt verondersteld sinds 1-1-2023 niet sterk
verschoven te zijn.

**Methode (dasymetrische verdeling):** voor elke PC4 wordt het gesplitste
bestelauto-/vrachtauto-aantal verdeeld over de overlappende buurten, gewogen
naar `gewicht(buurt) × (overlap-oppervlak / totale buurt-oppervlak)`, met
`gewicht` = bedrijfsvestigingen sector H+J → valt terug op bedrijfsvestigingen
totaal → valt terug op kaal buurt-oppervlak. Een tweede, kaal-oppervlakte-
gewogen verdeling wordt apart berekend en samen met de eerste gerapporteerd
als `_ondergrens`/`_bovengrens`-bandbreedte.

**Belangrijke caveats** (letterlijk uit de opdracht overgenomen):
- Dit is een **modelschatting, geen officiële telling**.
- **Lease-/verhuurvertekening**: bedrijfswagens van lease-/verhuurbedrijven
  staan geregistreerd op het adres van dat bedrijf, niet van de feitelijke
  gebruiker.
- **Geen geneste grenzen**: PC4- en buurtgrenzen zijn onafhankelijk
  vastgesteld en overlappen zelden netjes — het resultaat is een schatting,
  geen telling.
- **Peildata-mismatch**: de RDW-telling is actueel, maar de bestelauto/
  vrachtauto-splitsing komt van CBS-cijfers over 1-1-2023.
- **Water-buurten en "Buitenland" worden uitgesloten vóór de verdeling**: RDW's
  PC4-verdeling kent geen CBS-buurtgrenzen en zou zonder ingrijpen ook
  bestelauto/vrachtauto-aantallen toekennen aan buurten die CBS zelf als water
  classificeert (`water == "JA"` op de CBS wijken-en-buurten-geometrie, bv.
  een havengebied zonder woonbevolking) of aan de pseudo-buurt "Buitenland"
  (`BU09989999`). Zulke buurten hebben sowieso geen rij in
  `kerncijfers_buurten_<jaar>_met_geometrie.csv` (CBS' eigen
  kerncijfers_buurten-publicatie negeert ze net zo goed), dus konden ze nooit
  als J_Neighborhood geladen worden — hun aandeel bleef daardoor blijvend
  onbereikbaar voor het model, en zorgde voor een structurele mismatch tussen
  `J_Municipality`'s totaal en de som van zijn geladen buurten (bevestigd
  voorbeeld: Dordrecht/BU05059997, een havenburt, 3592 bestelauto's/146
  vrachtauto's). Sinds de fix worden buurten met `water != "NEE"` uit de
  doellaag gefilterd vóórdat de PC4-overlay draait, zodat hun aandeel van elke
  PC4 automatisch naar de overlappende, wél laadbare buurten herverdeelt —
  geverifieerd: gemeentetotaal en som-van-buurten komen nu exact overeen voor
  alle 7 Drechtsteden-gemeenten.

**Validatie:** twee controles, geen van beide een harde poort:
1. *Conservering per PC4* — de herverdeelde buurt-bijdragen moeten (bijna)
   precies optellen tot de originele PC4-waarde; dit volgt wiskundig uit de
   normalisatie, dus een afwijking wijst op een bug, niet op databeperkingen
   (gelogd als waarschuwing).
2. *Vergelijking per gemeente* — de buurt-som per gemeente wordt vergeleken
   met CBS's eigen officiële gemeentetotaal uit dezelfde `85236NED`-tabel
   (1-1-2023). Géén exacte match verwacht (RDW is actueel, CBS-basis is een
   paar jaar ouder) — weggeschreven als
   `processed/bedrijfswagens_validatie_gemeenten_<jaar>_<datum>.csv` en ook
   als kolommen (`bestelauto_cbs2023`, `bestelauto_afwijking_pct`, …)
   meegenomen in `kerncijfers_gemeenten_<jaar>.csv`.

**Output-kolommen** (buurten-CSV): `bestelautos_totaal`,
`vrachtautos_totaal`, `bestelautos_ondergrens`/`_bovengrens`,
`vrachtautos_ondergrens`/`_bovengrens`, `bedrijfswagens_gewicht_methode`
(welke gewicht-trap voor déze buurt is gebruikt),
`bedrijfswagens_pc4_gelijk_verdeeld`, `bedrijfswagens_pc4_dekkingsklasse`,
`bedrijfswagens_rdw_peildatum`, `bedrijfswagens_cbs_split_periode`.

Draai deze fase vóór `--buurten` en `--gemeenten-potentie` (zelfde conventie
als de sectorale energie): `python run_pipeline.py --bedrijfswagens`.

---

## Capaciteitskaart: transformer-netwerk (RNB + TenneT) gekoppeld aan buurten

Bron: [data.partnersinenergie.nl/capaciteitskaart](https://data.partnersinenergie.nl/capaciteitskaart/info/algemene-info)
— open brondata van alle Nederlandse netbeheerders over transportcapaciteit,
wachtrijen en congestie. Geïmplementeerd in `make_capaciteitskaart_csv.py`
(`--capaciteitskaart`).

### Structuur van de brondata

Vier bestanden, geverifieerd tegen de echte data (niet alleen de meegeleverde
documentatie, versie 2.0, 2024-11-20):

| Bestand | Rijen | Wat |
|---|---|---|
| `congestie_pc6.csv` | ~462.600 (1/PC6) | **De centrale koppeltabel**: postcode → `voedingsgebied_id` (RNB) én `tennet_id` (TenneT) tegelijk |
| `voedingsgebieden.csv` | 608 (1/gebied) | RNB-laag: transportcapaciteit/wachtrij. Eenmalige 2026-snapshot, géén tijdreeks |
| `tennetgebieden.csv` | 150 (22 congestiegebieden × jaar) | TenneT-laag: transportcapaciteit/wachtrij, écht een meerjarenreeks 2026-2036 |
| `tennetcongestie.csv` | 271 (1/tennet_id) | Koppelt elk TenneT-tussenstation aan zijn congestiegebied — apart voor afname/opwek-richting |

**Referentiële integriteit geverifieerd (2026-07-09 download)**: `voedingsgebied_id`
matcht 100% in beide richtingen; elke `tennet_id` in `congestie_pc6.csv` bestaat in
`tennetcongestie.csv` (32 stations in `tennetcongestie.csv` worden door geen enkele
postcode gerefereerd — waarschijnlijk pure koppelstations zonder rechtstreeks
aangesloten postcodegebied); congestiegebied-namen komen overeen op één
spatie-artefact na ("Groningen Oost " — genormaliseerd met `.str.strip()`).

### Hiërarchie RNB ↔ TenneT

Niet rechtstreeks in de brondata (`voedingsgebieden.csv` bevat zelf geen
`tennet_id`) maar afgeleid uit `congestie_pc6.csv`: **536 van de 608 (88%)
voedingsgebieden hebben precies 1 TenneT-ouder; 36 (6%) hebben 2-4 ouders**
(redundante voeding, geen datafout). Dit is een echte
TenneT-(hoogspanning)-boven-RNB-(middenspanning) hiërarchie, vergelijkbaar met
de bestaande `trafo150kVTo50kV`/`trafo50kVTo13kV`-velden in `J_Neighborhood`.
Omdat een klein deel meerdere ouders heeft, wordt dit gemodelleerd als een
aparte junction-tabel (`transformer_links.csv`), niet als een enkele
`parent_id`-kolom die de 6% meervoudige-ouder-gevallen zou dwingen tot een
willekeurige keuze.

Ook 83% (225/271) van de TenneT-stations heeft dezelfde congestiegebied voor
afname én opwek-richting; 17% (46/271) heeft een andere zone per richting —
daarom worden beide richtingen apart gekoppeld in plaats van aangenomen dat ze
altijd samenvallen.

### Methode

1. Download de 4 CSV's + documentatie van data.partnersinenergie.nl.
2. Bouw `transformers.csv`: één rij per (`transformer_id`, jaar), `type`
   "RNB"/"TenneT". Beide bronnen delen exact dezelfde kolomnamen voor
   capaciteit/wachtrij, dus rechtstreeks te unioneren. RNB-rijen (eenmalige
   snapshot) worden herhaald over alle jaren 2026-2036 zodat er één
   samenhangende tabel ontstaat met TenneT's echte meerjarenreeks — dit
   simuleert geen echte RNB-prognose, het maakt de tabel alleen bruikbaar als
   geheel.
3. Bouw `transformer_links.csv`: (`child_transformer_id`=voedingsgebied_id,
   `parent_transformer_id`=tennet_id), gededupliceerd uit `congestie_pc6.csv`.
4. Koppel buurten aan hun dominante voedingsgebied_id: PC6 → buurt via
   grootste-overlap polygon matching (dezelfde methode als
   `process_features.py` al gebruikt voor WarmteAtlas-lagen — geen nieuwe
   geometrie-aanpak nodig, wel een nieuwe overlay tussen CBS PC6- en
   buurt-vlakken), daarna PC6 → voedingsgebied_id uit `congestie_pc6.csv`. Een
   buurt waarvan de PC6's naar meerdere voedingsgebieden wijzen (grensgeval)
   krijgt het meest voorkomende, met een dekkingsgraad erbij
   (`voedingsgebied_dekking`) zodat onbetrouwbare matches herkenbaar zijn.
   99,0% van de buurten (14.420 van 14.574, 2024-run) wordt gekoppeld.

`transformers.csv` bevat ook drie kolommen die niet in de brondata zelf zitten,
afgeleid door deze pipeline (`bouw_transformer_geometrie()` /
`_bouw_primary_parent()` / `_extract_voltage_kv()` in `make_capaciteitskaart_csv.py`):

- **`latitude`/`longitude`/`service_area_polygon`** — centroid + dissolved PC6-
  vlakken per `transformer_id` (RNB via `voedingsgebied_id`, TenneT via
  `tennet_id`), zelfde centroid-conventie als elders (berekend in EPSG:28992,
  herprojecteerd naar EPSG:4326). 759/9669 rijen (69 unieke `transformer_id`'s:
  37 RNB-gebieden zonder gekoppelde PC6, 32 TenneT-stations die door geen enkele
  postcode gerefereerd worden) hebben geen geometrie — verwacht, zie caveats.
- **`primary_parent_id`** — alleen voor RNB-rijen: de TenneT-ouder met de meeste
  gekoppelde PC6's, voor de 6% voedingsgebieden met 2-4 echte ouders (zie
  Hiërarchie-sectie hierboven). Leeg voor TenneT-rijen (bovenste laag in dit
  model) en voor de 36 voedingsgebieden zonder enige PC6-afgeleide koppeling.
- **`voltage_kv`** — alleen voor TenneT (110/150/220/380), geparsed uit de
  stationsnaam (bv. "Station Aarle Rixtel 150 kV"). RNB-brondata specificeert
  geen spanningsniveau, dus leeg voor RNB-rijen. 2 van de 271 TenneT-stations
  hebben geen herkenbaar spanningsniveau in hun naam ("Station Bunschoten",
  "Station Merwedekanaal") en blijven leeg.

### Belangrijke caveats

- Momentopname van de capaciteitskaart (versie 2.0, gedownload 2026-07-09) —
  de netbeheerders werken de brondata periodiek bij; regenereer voor een
  actuelere stand.
- De RNB-laag in `transformers.csv` is een 2026-snapshot herhaald over alle
  jaren, geen echte RNB-meerjarenprognose.
- Alle numerieke brondata gebruikt een komma als decimaalteken (conventie van
  de netbeheerders zelf) — hier omgezet naar punt bij het parsen.

### Output-bestanden

- `data_Generic/transformers.csv`, `data_Generic/transformer_links.csv` —
  directe model-input, geen buurtenjaar-afhankelijkheid (nationaal, eenmalig
  per download).
- `processed/capaciteitskaart_buurten_<jaar>_<datum>.csv` — buurtcode →
  voedingsgebied_id, per geometrie-jaar (zoals bedrijfswagens hierboven), wordt
  gemerged in de buurten-CSV door `--buurten` als kolommen `voedingsgebied_id`,
  `voedingsgebied_dekking`, `voedingsgebied_aantal_pc6`.

Draai deze fase vóór `--buurten` (zelfde conventie als bedrijfswagens):
`python run_pipeline.py --capaciteitskaart`.

### Java-koppeling: TransformerRowParser

`data_Generic/TransformerRowParser.java` parseert `transformers.csv` naar
`J_Transformer`, via `J_Transformer.builder()` — elke kolom van de CSV heeft nu
een eigen veld op de klasse (voorheen was alleen een subset gemodelleerd:
`gridnode_id`/`capacity_kw`/`is_capacity_available`/`description`/
`parent_node_id`/`type`/`latitude`/`longitude`/`service_area_polygon`; de rest
(`naam`, `operator`, `jaar`, `voltage_kv`, alle invoeding-richting-kolommen,
`wachtrij_*`, `voorspelde_capaciteit_*`, `jaartal_opgelost_*`,
`congestiegebied_*`, `*_kleurcode`, `provincie`) werd door de CSV-pipeline wel
gelezen maar vóór deze klasse alweer weggegooid). De twee originele
constructors blijven ongewijzigd bestaan (voor eventuele bestaande call
sites), maar kunnen die nieuwe velden niet zetten — gebruik `builder()` voor
een volledig gevulde instantie.

Twee dingen zijn hier anders dan bij de overige parsers:

- `transformers.csv` heeft een eigen forecast-jaar-as (2026-2036, één rij per
  `transformer_id` per jaar) die **niets te maken heeft** met de
  CBS/buurten-`yearData` (2023-2025) van de rest van het model. `J_Transformer`
  zelf kent geen jaar-concept, dus de parser neemt een `targetYear` in de
  constructor (`new TransformerRowParser(2026)`) en filtert rijen die niet bij
  dat jaar horen — kies onafhankelijk van `yearData` welk van de 2026-2036
  jaren relevant is voor de modelrun.
- Er is geen buurtcode/gemeentecode-kolom in `transformers.csv` (een
  voedingsgebied of TenneT-station is niet aan één buurt/gemeente gebonden),
  dus `extractBuurtcode()` geeft altijd `null` terug — dat is hier de bedoelde
  toepassing van `CsvRowParser`'s default "geen buurtcode → toch laden"-gedrag
  (anders dan de bug die eerder in `WindTurbineRowParser`/
  `SolarPowerPlantRowParser` zat, waar rijen wél een buurtcode hoorden te
  hebben maar die miste). Laad daarom met `gemeenteCodes = null`: een RNB- of
  TenneT-ouder van een Drechtsteden-buurt kan buiten de geselecteerde
  gemeenten liggen.

`capacity_kw`/`is_capacity_available` blijven de afname-richting
(`aanwezige_transportcapaciteit_afname`, `wachtrij_afname`) — relevant voor
buurten die stroom afnemen. De invoeding-richting (opwek/productie) zit nu ook
in de klasse, als eigen velden (`availableCapacityInvoeding_kW`,
`requiredCapacityInvoeding_kW`, `queueInvoeding_kW`,
`forecastCapacityInvoeding_kW`) in plaats van samengevoegd met de
afname-kant. `type` wordt gemapt naar `OL_GridNodeType.HVMV` (RNB) /
`HVHV` (TenneT). `afnameColorCode`/`opwekColorCode` behouden de -1..3 codes uit
de brondata zelf (-1 = geen informatie) — dit is géén CsvUtils
`-99999`-sentinel, dus deze twee worden expliciet apart geparsed
(`TransformerRowParser.parseColorCode`) in plaats van via `asInt()`.

**Geen aparte link-stap**: laad `transformers.csv` vóór `neighborhoods.csv`.
`NeighborhoodRowParser` neemt de resulterende `Map<String, J_Transformer>`
(sleutel = `J_Transformer::gridnode_id`) als constructor-argument en koppelt
`trafo50kVTo13kV`/`trafo150kVTo50kV` meteen tijdens het parsen van elke buurt
— hetzelfde patroon dat `WindTurbineRowParser`/`SolarPowerPlantRowParser` al
gebruiken voor `neighborhoodsByBuurtcode`, alleen in de andere richting (daar
zijn de buurten er al als de turbines/zonneparken laden; hier moeten de
transformers er al zijn als de buurten laden). Per buurt: `voedingsgebiedId`
(uit de `voedingsgebied_id`-kolom) opgezocht als `gridnode_id` van een
RNB-transformer → `trafo50kVTo13kV`; diens `parent_node_id`
(= `primary_parent_id` uit transformers.csv) vervolgens opgezocht als
`gridnode_id` van een TenneT-transformer → `trafo150kVTo50kV`.

Dezelfde directe-koppeling-aanpak is ook toegepast voor `J_Municipality`:
`NeighborhoodRowParser` neemt óók `Map<String, J_Municipality>` (sleutel =
`J_Municipality::getMunicipalityCode`, bv. `"GM0505"`) en zet
`municipality`/roept `J_Municipality.addNeighborhood(...)` aan tijdens het
parsen — die velden bestonden al op beide klassen maar werden nergens gezet.
`neighborhoods.csv` heeft geen eigen gemeentecode-kolom (alleen
`gemeentenaam`), dus de gemeentecode wordt afgeleid uit de buurtcode zelf
("BU05051919" → "GM0505", dezelfde conventie als
`CsvRowParser.passesGmFilter`/`CsvLoader.buildGmFilter` al gebruiken).
`municipalities.csv` heeft geen dependency op transformers/neighborhoods (of
omgekeerd), dus de laadvolgorde t.o.v. die twee maakt niet uit — het moet
alleen al geladen zijn vóórdat `neighborhoods.csv` laadt.

**Dit is een call-site-wijziging in `Startup_agent`** (`NeighborhoodRowParser`
had voorheen geen constructor-argument, nu twee): laad de collecties in deze
volgorde:

```java
Map<String, J_Transformer> c_transformers = CsvLoader.loadEntities(
    csvPathTransformers, null, new TransformerRowParser(2026), J_Transformer::gridnode_id);
Map<String, J_Municipality> c_municipalities = CsvLoader.loadEntities(
    csvPathGemeenten, selectedMunicipalityCodes,
    new MunicipalityRowParser(), J_Municipality::getMunicipalityCode);
Map<String, J_Neighborhood> c_neighborhoods = CsvLoader.loadEntities(
    csvPathNeighborhoods, selectedMunicipalityCodes,
    new NeighborhoodRowParser(c_transformers, c_municipalities), J_Neighborhood::getBuurtCode);
```

### EV-prognoses (ElaadNL) en het "realization"-scenario

`make_elaadnl_csv.py` haalt per buurt 4 scenario's (`low`, `middle`, `high`,
`realization`) × 4 modaliteiten (`car_bev`, `car_phev`, `van`, `truck`) op bij
de ElaadNL Outlook API. **`realization` (het daadwerkelijk gerealiseerde/
geregistreerde aantal) is alleen gevuld voor `car_bev`** — voor `car_phev`,
`van` en `truck` geeft de API voor deze scenario-modaliteit-combinatie altijd
een lege response terug (geverifieerd door de ruwe cache in
`raw/elaadnl/realization/{car_phev,van,truck}/` te inspecteren: elk bestand is
`[]`). Daardoor bevat de buurten-CSV wél `ev_car_bev_realization_2025`, maar
geen `ev_car_phev_realization_2025`, `ev_van_realization_2025` of
`ev_truck_realization_2025` — deze kolommen worden simpelweg niet gegenereerd
omdat er geen data voor is. Dit is ook waarom `J_Neighborhood` een
`realizedCars2025`-veld heeft maar geen `realizedVans2025`/`realizedTrucks2025`.

### Totaal-reconciliatie (`VehicleFleet.ofKnownAtLeastTotal`)

Zolang `totalVans`/`totalTrucks` `0` waren (voordat de bedrijfswagens-schatting
hierboven bestond) en `ev_car_bev_*` voor latere jaren (2040/2050) vaak hoger
uitkomt dan het huidige `personenautos_totaal`, zou een strikte
"electric+hydrogen+hybrid ≤ total"-check (`VehicleFleet.ofKnown()`) voor bijna
elke buurt een `IllegalArgumentException` opleveren. De referentiekopie in
`data_Generic/NeighborhoodRowParser.java` gebruikt daarom `VehicleFleet.
ofKnownAtLeastTotal()` voor alle cars/vans/trucks-fleets (de scenario-loop én
`realizedCars2025`): als het bekende aantal (EV/PHEV) de total overschrijdt,
wordt de total opgehoogd tot dat bekende aantal in plaats van dat het
laadproces crasht.

**Let op:** deze `ofKnownAtLeastTotal()`-fix staat alleen in de standalone
referentiebestanden onder `data_Generic/` (voor leesbaarheid/versiebeheer buiten
AnyLogic om) — bewust **niet** overgenomen in `LUX_Drechtsteden_RES.alp`, want
dat bestand wordt rechtstreeks in AnyLogic bewerkt en niet vanuit deze repo
gesynchroniseerd. Wie de fix in het echte model wil hebben, moet 'm handmatig
overnemen in de `VehicleFleet`- en `NeighborhoodRowParser`-klassen in het
AnyLogic-project. Met de bedrijfswagens-schatting op zijn plaats zou een
overschrijding voor vans/trucks nu vooral nog voorkomen bij een sterk
verouderde CBS-splitsing (peildatum 1-1-2023) t.o.v. een nieuwer EV-scenario —
`VehicleFleet.ofKnown()` blijft daarom bestaan voor plekken waar een
overschrijding wél een echte bug zou betekenen.

---

## Technische keuzes

| Onderwerp | Keuze | Reden |
|-----------|-------|-------|
| CSV-scheidingsteken | `;` | Vermijdt conflict met komma als decimaalteken in Nederlandse data |
| Rekening-CRS | EPSG:28992 (RD New) | Metrische berekening; nooit EPSG:4326 voor area/centroid |
| Output-CRS | EPSG:4326 | Compatibel met AnyLogic |
| Polygon matching | Grootste overlappend oppervlak | Robuust bij randpolygonen |
| ElaadNL cache | Per buurt als JSON in `raw/elaadnl/` | Veilig te onderbreken en hervatten |
| Ongematchte features | Apart bestand `*_onbekend_match_*.csv` | Transparant; niet stilletjes weggooien |
