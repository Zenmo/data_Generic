# Opdracht: schatting bestelauto's en vrachtauto's per buurt (NL)

## Doel
Bouw een script/pipeline die het aantal **bestelauto's en vrachtauto's** (dus expliciet NIET
trekkers, speciale voertuigen of bussen) per CBS-buurt schat, uitgaande van open brondata die
alleen op postcode-4 (PC4) niveau beschikbaar is. De aanpak is een **dasymetrische verdeling**:
PC4-totalen worden over de buurten binnen dat PC4-gebied verdeeld, gewogen naar waarschijnlijke
locatie van bedrijfsvoertuigen (bedrijfsvestigingen, met nadruk op de transportsector).

Dit is een **modelschatting**, geen officiële telling. Wees daar expliciet over in de output en
documentatie — zie sectie "Belangrijke caveats" hieronder, die letterlijk in de output/README
van het eindresultaat terug moet komen.

## Databronnen

### 1. RDW open data — voertuigen per postcode (de telling die verdeeld moet worden)
- Portal: https://opendata.rdw.nl
- Dataset: "Voertuigen met brandstoffen per postcode" (zoek exacte dataset-ID/endpoint op het
  portaal, want RDW hanteert Socrata dataset-ID's die kunnen wijzigen — check eerst het
  actuele endpoint via de RDW open data catalogus of https://opendata.rdw.nl/stories).
- Niveau: numerieke postcode (PC4), per voertuigsoort en brandstofsoort.
- Update: maandelijks.
- Bekende beperking: aantallen <10 per combinatie (postcode × voertuigsoort × brandstof)
  worden niet getoond (privacy-afscherming/AVG). Sommeer dus niet blind — het PC4-totaal kan
  hoger zijn dan de som van getoonde subcategorieën.
- Filter op voertuigsoort = **alleen bestelauto en vrachtauto**. Sluit expliciet uit: personenauto,
  motorfiets, bromfiets, trekker, speciaal voertuig, bus/autobus. Controleer bij het uitlezen van
  de RDW-data de exacte categorienamen in het veld voertuigsoort (bv. "Bedrijfsauto" kan intern
  verder onderverdeeld zijn, of je moet filteren op basis van een gekoppeld veld zoals
  carrosserie/subcategorie om trekkers en bussen eruit te houden) — neem niets aan, verifieer aan
  de hand van een steekproef uit de data zelf welke waarden bestelauto/vrachtauto vertegenwoordigen.
- Download bij voorkeur via de Socrata SODA API (CSV/JSON) i.p.v. handmatige export, zodat het
  script herhaalbaar is.

### 2. CBS PC4-vlakken (geodata)
- Bron: CBS / Kadaster, via PDOK (https://www.pdok.nl) — zoek "Postcode4 vlakken" of
  "CBS Postcode statistieken".
- Nodig voor de geometrische overlay met buurtgrenzen.

### 3. CBS Wijk- en buurtkaart (geodata), meest recente jaargang
- Bron: CBS, ook via PDOK (https://www.pdok.nl, zoek "wijk en buurtkaart").
- Bevat de officiële buurtgrenzen + buurtcode/buurtnaam/gemeentecode.

### 4. CBS Kerncijfers wijken en buurten (KWB) — gewicht/proxy voor de verdeling
- StatLine, meest recente beschikbare jaargang. Tabelcodes (controleer bij uitvoering of er een
  nieuwere jaargang is toegevoegd):
  - 2023: 85618NED
  - 2024: 85984NED
  - 2025: 86165NED
- Gebruik variabele **`a_bed_hj`** (Bedrijfsvestigingen sector H+J: Vervoer, informatie en
  communicatie) per buurt als primair gewicht — dit is de sector waarin transportbedrijven vallen.
- Val terug op **`a_bedv`** (totaal bedrijfsvestigingen per buurt) waar `a_bed_hj` ontbreekt
  (sectoruitsplitsing wordt door CBS alleen getoond bij ≥20 bedrijfsvestigingen in een buurt).
- Gebruik ook **`pst_mvp`** (meest voorkomende PC4 in de buurt) en **`pst_dekp`**
  (dekkingspercentage, klasse 1–6) uit dezelfde tabel — nodig om betrouwbaarheid per buurt te
  kunnen rapporteren (zie caveats).
- Let op: KWB-aantallen zijn afgerond op een veelvoud van 5.

### 5. (Optioneel, indien tijd) Fijnmaziger sectordetail
- Check StatLine op een losse tabel "Bedrijfsvestigingen naar activiteit, wijk en buurt" met
  SBI-niveau specifiek voor transport & logistiek (SBI 49–53), voor een preciezer gewicht dan de
  brede H+J-sector uit KWB.

## Methodologie — stappenplan voor het script

1. **Data inladen**
   - RDW PC4-telling bedrijfsvoertuigen (huidige maand/jaar).
   - CBS PC4-vlakken (geodata).
   - CBS wijk-en-buurtkaart (geodata).
   - CBS KWB (a_bed_hj, a_bedv, pst_mvp, pst_dekp per buurt).

2. **Geometrische overlay**
   - Intersect PC4-vlakken met buurtvlakken (GIS, bv. met `geopandas` in Python).
   - Voor elk PC4-gebied: bepaal welke buurten (deels) binnen liggen en het
     overlap-oppervlak per buurt-binnen-PC4.

3. **Gewicht per buurt-in-PC4 bepalen**
   - Basisgewicht = `a_bed_hj` van die buurt (val terug op `a_bedv` indien `a_bed_hj` ontbreekt/
     onderdrukt, en op kaal oppervlak als ook `a_bedv` ontbreekt).
   - Als een buurt slechts gedeeltelijk in het PC4-gebied ligt: vermenigvuldig het gewicht met de
     oppervlakte-fractie van de buurt die binnen dat PC4 valt (grove aanname dat vestigingen
     gelijk verdeeld zijn binnen de buurt — vermeld deze aanname in de output).

4. **Verdelen**
   - Voor elk PC4-gebied P met RDW-telling N_P:
     `geschat(B) = N_P × gewicht(B) / Σ gewicht(alle buurten-in-P)`
   - Sommeer bijdrages van alle overlappende PC4's per buurt tot het eindtotaal per buurt.

5. **Onzekerheid documenteren**
   - Bereken de schatting ook met een alternatief gewicht (bv. kaal oppervlakte-aandeel i.p.v.
     bedrijfsvestigingen) en rapporteer beide als bandbreedte, niet als één hard getal.
   - Voeg `pst_dekp`-klasse van de buurt toe aan de output, zodat gebruikers zien in welke buurten
     de PC4-buurt-mismatch groot is (klasse 4-6 = onbetrouwbaarder resultaat).

6. **Output**
   - CSV en/of GeoJSON met minimaal: `buurtcode`, `buurtnaam`, `gemeentenaam`,
     `geschat_aantal_bestelauto_vrachtauto`, `schatting_ondergrens`, `schatting_bovengrens`,
     `pst_dekp_klasse`, `gewicht_methode`. Overweeg bestelauto en vrachtauto ook los te
     rapporteren (twee kolommen i.p.v. één gecombineerd getal), als de RDW-brondata dat
     onderscheid toelaat — dat is waarschijnlijk waardevoller dan een samengevoegd cijfer.
   - Een korte README/data-dictionary die de caveats hieronder letterlijk overneemt.

## Belangrijke caveats (verplicht opnemen in output-documentatie)

- **Lease-/verhuurvertekening**: bedrijfswagens van lease- en verhuurbedrijven staan geregistreerd
  op het adres van dát bedrijf, niet van de feitelijke gebruiker. Buurten met veel leasemaatschappijen
  krijgen daardoor een te hoog geschat aantal; buurten waar de voertuigen feitelijk rijden/staan
  juist te laag.
- **RDW-onderdrukking**: PC4-combinaties met <10 voertuigen worden niet getoond, dus PC4-totalen
  kunnen een onderschatting zijn.
- **Geen geneste grenzen**: PC4 (PostNL, logistiek bepaald) en buurtgrenzen (gemeente/CBS,
  sociaal-ruimtelijk bepaald) zijn onafhankelijk vastgesteld en overlappen zelden netjes.
  Resultaat is een modelschatting, geen telling — communiceer dit expliciet, ook richting
  eindgebruikers van de output.
- **Peildata mismatch**: vermeld altijd welke RDW-maand en welk KWB-jaar gebruikt zijn; deze lopen
  mogelijk niet synchroon.

## Technische suggesties
- Taal: Python (pandas, geopandas, requests).
- Haal RDW-data bij voorkeur via een API-call op (Socrata SODA), niet via handmatige CSV-export,
  zodat het script herbruikbaar/herhaalbaar is voor toekomstige updates.
- Cache de geodata lokaal (PC4-vlakken en buurtkaart veranderen niet vaak) om herhaald downloaden
  te voorkomen.
- Valideer aan het eind: som van geschatte buurtaantallen per gemeente moet ongeveer overeenkomen
  met het gemeentetotaal uit de RDW/CBS-brondata (grote afwijkingen wijzen op een fout in de
  overlay of gewichtstoekenning).
