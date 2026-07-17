"""
CBS Kerncijfers wijken en buurten inladen via de open data (OData) API.

Vereist eenmalig:  pip install cbsodata pandas openpyxl

Gebruik: vul alleen een jaartal in -> de juiste tabel wordt automatisch opgezocht.
Kolomselectie en doelnamen komen uit een Excel-mapping (standaard: mapping_kolomnamen_CBS.xlsx).

  df = haal_kwb(2024)
  df = haal_kwb(2023, kolommen=["aantal_inwoners", "woningvoorraad_woningvoorraad"])

Structuur mapping-Excel:
  Tabblad 'mappingKolomnamen':
    naam   - gewenste kolomkop in het resultaat
    2023   - CBS Key voor 2023
    2024   - CBS Key voor 2024
    <jaar> - voeg zelf extra kolommen toe voor andere jaren
  Tabblad 'kolommen_<jaar>' (optioneel): volledige kolomlijst voor dat jaar (ter referentie).

Belangrijk:
  - Als een Key voor een bepaald jaar ontbreekt (lege cel), wordt die kolom overgeslagen.
  - Als de exacte Key niet bestaat in het jaar, probeert de code automatisch een Key
    te vinden op basis van de basisnaam (deel vóór het volgnummer).
"""

import re
from pathlib import Path
import cbsodata
import pandas as pd

_SCRIPT_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Bekende tabel-IDs als snelle cache (offline fallback)
# ---------------------------------------------------------------------------
BEKENDE_IDS = {
    2020: "84799NED",
    2021: "85039NED",
    2022: "85318NED",
    2023: "85618NED",
    2024: "85984NED",
    2025: "86165NED",
}


# ---------------------------------------------------------------------------
# 1. Tabel-ID bepalen op basis van het jaartal
# ---------------------------------------------------------------------------
def tabel_id_voor_jaar(jaar):
    """Geeft het tabel-ID voor een jaar. Eerst uit cache, anders live uit de catalogus."""
    if jaar in BEKENDE_IDS:
        return BEKENDE_IDS[jaar]

    titel = f"Kerncijfers wijken en buurten {jaar}"
    tabellen = pd.DataFrame(cbsodata.get_table_list())
    treffers = tabellen[tabellen["Title"].str.strip() == titel]
    if treffers.empty:
        treffers = tabellen[
            tabellen["Title"].str.contains("Kerncijfers wijken en buurten", na=False)
            & tabellen["Title"].str.contains(str(jaar), na=False)
        ]
    if treffers.empty:
        raise ValueError(f"Geen tabel gevonden voor jaar {jaar}.")
    treffers = treffers.sort_values("Modified", ascending=False)
    return treffers.iloc[0]["Identifier"]


# ---------------------------------------------------------------------------
# 2. Mapping inladen vanuit Excel
# ---------------------------------------------------------------------------
def laad_mapping(bestand="mapping_kolomnamen_CBS.xlsx", blad="mappingKolomnamen"):
    """
    Laadt de kolomnamen-mapping uit een Excel-bestand.

    Geeft een DataFrame terug met kolom 'naam' plus één kolom per jaar
    (bv. '2023', '2024'). Lege cellen betekenen: kolom niet beschikbaar dat jaar.
    """
    pad = Path(bestand)
    if not pad.is_absolute():
        pad = _SCRIPT_DIR / pad
    df = pd.read_excel(pad, sheet_name=blad, dtype=str)
    df.columns = df.columns.astype(str).str.strip()
    df["naam"] = df["naam"].str.strip()
    # Lege strings -> NaN zodat we ze makkelijk kunnen overslaan
    df = df.replace("", pd.NA)
    return df


def kolommen_voor_jaar(mapping: pd.DataFrame, jaar: int):
    """
    Geeft een lijst van (doelnaam, cbs_key) tuples voor het opgegeven jaar.
    Rijen zonder Key voor dat jaar worden overgeslagen.
    """
    jaar_str = str(jaar)
    if jaar_str not in mapping.columns:
        raise ValueError(
            f"Jaar {jaar} komt niet voor in de mapping. "
            f"Beschikbare jaren: {[c for c in mapping.columns if c.isdigit()]}"
        )
    resultaat = []
    for _, rij in mapping.iterrows():
        naam = rij["naam"]
        key = rij.get(jaar_str)
        if pd.notna(naam) and pd.notna(key):
            resultaat.append((naam.strip(), key.strip()))
    return resultaat


# ---------------------------------------------------------------------------
# 3. Keys valideren en corrigeren (suffix kan per jaar verschuiven)
# ---------------------------------------------------------------------------
def _basisnaam(key):
    """Naam vóór het volgnummer: 'Woningvoorraad_35' -> 'Woningvoorraad'."""
    return re.sub(r"_\d+$", "", key)


def _resolve_keys(tid, gewenste_paren):
    """
    Controleert (doelnaam, cbs_key) paren tegen het gekozen jaar.
    Een verschoven suffix wordt automatisch gecorrigeerd op basisnaam.

    Geeft (select_keys, kop_map, problemen) terug:
      select_keys : lijst van geldige CBS Keys voor dit jaar
      kop_map     : {gevonden_key -> doelnaam}
      problemen   : lijst van doelnamen die niet gevonden konden worden
    """
    meta = pd.DataFrame(cbsodata.get_meta(tid, "DataProperties")).dropna(subset=["Key"])
    beschikbaar = set(meta["Key"])

    # basisnaam -> alle volledige Keys in dit jaar
    naar_key = {}
    for k in meta["Key"]:
        naar_key.setdefault(_basisnaam(k), []).append(k)

    select_keys, kop_map, problemen, afwijkend = [], {}, [], 0

    for doelnaam, cbs_key in gewenste_paren:
        if cbs_key in beschikbaar:
            select_keys.append(cbs_key)
            kop_map[cbs_key] = doelnaam
        else:
            # Probeer op basisnaam
            kandidaten = naar_key.get(_basisnaam(cbs_key), [])
            if len(kandidaten) == 1:
                gevonden = kandidaten[0]
                print(f"  [AANGEPAST] '{cbs_key}' -> '{gevonden}'  (voor '{doelnaam}')")
                select_keys.append(gevonden)
                kop_map[gevonden] = doelnaam
                afwijkend += 1
            elif len(kandidaten) > 1:
                print(
                    f"  [MEERDERE] '{cbs_key}' past op meerdere keys: {kandidaten}  "
                    f"(voor '{doelnaam}') -> overgeslagen"
                )
                problemen.append(doelnaam)
            else:
                print(f"  [GEEN] '{cbs_key}' bestaat niet in dit jaar  (voor '{doelnaam}')")
                problemen.append(doelnaam)

    gevonden_n = len(gewenste_paren) - len(problemen)
    print(
        f"Keys: {gevonden_n}/{len(gewenste_paren)} gevonden"
        + (f", waarvan {afwijkend} via automatische correctie" if afwijkend else "")
        + (f"; {len(problemen)} ontbreken: {problemen}" if problemen else "")
    )
    return list(dict.fromkeys(select_keys)), kop_map, problemen


# ---------------------------------------------------------------------------
# 4. Alle kolommen (variabelen) bekijken / exporteren
# ---------------------------------------------------------------------------
def toon_kolommen(jaar, naar_bestand=None):
    """Geeft een DataFrame met Key + Title + Unit van alle variabelen voor een jaar.
    naar_bestand: optioneel pad (.xlsx of .csv) om de lijst weg te schrijven."""
    tid = tabel_id_voor_jaar(jaar)
    meta = pd.DataFrame(cbsodata.get_meta(tid, "DataProperties"))
    kolommen = [c for c in ["Key", "Title", "Unit", "Type"] if c in meta.columns]
    overzicht = meta[kolommen].dropna(subset=["Key"]).reset_index(drop=True)

    if naar_bestand:
        if naar_bestand.endswith(".xlsx"):
            overzicht.to_excel(naar_bestand, index=False)
        else:
            overzicht.to_csv(naar_bestand, index=False)
        print(f"Kolomlijst opgeslagen in {naar_bestand}")
    else:
        print(overzicht.to_string(index=False))
    return overzicht


# ---------------------------------------------------------------------------
# 5. Hoofdfunctie
# ---------------------------------------------------------------------------
def haal_kwb(
    jaar,
    mapping_bestand="mapping_kolomnamen_CBS.xlsx",
    mapping_blad="mappingKolomnamen",
    kolommen=None,
    alleen_buurten=True,
    verwijder_lege_kolommen=False,
    ontbrekende_waarde=-99999,
):
    """
    Haalt CBS Kerncijfers wijken en buurten op voor het opgegeven jaar.

    jaar                   : int, bv. 2024
    mapping_bestand        : pad naar het Excel-bestand met de kolomnamen-mapping
    mapping_blad           : naam van het tabblad in de mapping (standaard 'mappingKolomnamen')
    kolommen               : None = alle kolommen uit de mapping voor dit jaar.
                             Of een lijst van 'naam'-waarden uit de mapping om
                             een subset te selecteren.
    alleen_buurten         : True -> filter op SoortRegio == 'Buurt'
    verwijder_lege_kolommen: True -> volledig lege kolommen worden weggelaten

    Kolomkoppen in het resultaat zijn de 'naam'-waarden uit de mapping.
    """
    tid = tabel_id_voor_jaar(jaar)
    print(f"Jaar {jaar} -> tabel {tid}")

    # Mapping inladen
    mapping = laad_mapping(mapping_bestand, mapping_blad)

    # Bepaal welke (doelnaam, cbs_key) paren we willen
    alle_paren = kolommen_voor_jaar(mapping, jaar)
    if kolommen is not None:
        kolommen_set = set(kolommen)
        alle_paren = [(naam, key) for naam, key in alle_paren if naam in kolommen_set]
        ontbrekend = kolommen_set - {naam for naam, _ in alle_paren}
        if ontbrekend:
            print(f"  [WAARSCHUWING] Gevraagde namen niet in mapping: {sorted(ontbrekend)}")

    # Altijd basiskolommen meenemen
    basis_keys = ["WijkenEnBuurten", "Gemeentenaam_1", "SoortRegio_2", "Codering_3"]
    gewenste_keys, kop_map, _ = _resolve_keys(tid, alle_paren)
    select = list(dict.fromkeys(basis_keys + gewenste_keys))

    # Data ophalen
    df = pd.DataFrame(cbsodata.get_data(tid, select=select))

    # Opschonen
    for kol in df.select_dtypes(include="object").columns:
        df[kol] = df[kol].str.strip()
    df = df.replace("", pd.NA)

    # Filter op buurten
    if alleen_buurten and "SoortRegio_2" in df.columns:
        df = df[df["SoortRegio_2"] == "Buurt"].reset_index(drop=True)

    # Lege kolommen verwijderen
    if verwijder_lege_kolommen:
        leeg = [c for c in df.columns if df[c].isna().all()]
        if leeg:
            print(f"Volledig lege kolommen verwijderd ({len(leeg)}): {leeg}")
            df = df.drop(columns=leeg)

    # Hernoemen: CBS Key -> doelnaam uit de mapping
    # Basiskolommen krijgen een leesbare vaste naam
    basis_namen = {
        "WijkenEnBuurten": "wijken_en_buurten",
        "Gemeentenaam_1": "gemeentenaam",
        "SoortRegio_2": "soort_regio",
        "Codering_3": "codering",
    }
    rename = {**basis_namen, **kop_map}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # Ontbrekende waarden in numerieke kolommen vervangen door sentinelwaarde
    if ontbrekende_waarde is not None:
        num_kolommen = df.select_dtypes(include="number").columns
        df[num_kolommen] = df[num_kolommen].fillna(ontbrekende_waarde)

    return df


# ---------------------------------------------------------------------------
# 6. Diagnose: hoeveel ontbreekt er per kolom?
# ---------------------------------------------------------------------------
def missing_rapport(df, alleen_vanaf=0.0):
    """Print per kolom het aandeel ontbrekende waarden (hoog -> laag).
    alleen_vanaf: toon alleen kolommen met minstens dit aandeel (bv. 0.5 = 50%)."""
    n = len(df)
    aandeel = df.isna().mean().sort_values(ascending=False)
    aandeel = aandeel[aandeel >= alleen_vanaf]
    print(f"\nOntbrekende waarden per kolom ({n} buurten):")
    for kol, frac in aandeel.items():
        markering = "   <-- VOLLEDIG LEEG" if frac == 1.0 else ""
        print(f"  {frac * 100:5.1f}%  {kol}{markering}")
    leeg = aandeel[aandeel == 1.0].index.tolist()
    if leeg:
        print(
            f"\n  {len(leeg)} kolom(men) volledig leeg voor dit jaar. Voor de "
            f"recentste jaargang zijn thema's als bedrijven/energie/auto's vaak "
            f"nog niet gepubliceerd -> probeer een eerder, afgerond jaar (bv. 2023)."
        )
    return aandeel


# ---------------------------------------------------------------------------
# Voorbeeldgebruik
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    MAPPING = "mapping_kolomnamen_CBS.xlsx"

    # --- 2024: alle kolommen uit de mapping ---
    df_2024 = haal_kwb(2024, mapping_bestand=MAPPING, verwijder_lege_kolommen=False)
    print(df_2024.head())
    missing_rapport(df_2024, alleen_vanaf=0.20)
    df_2024.to_excel(_SCRIPT_DIR / "kerncijfers_buurten_2024.xlsx", index=False)
    print(f"\n{len(df_2024)} buurten opgeslagen in kerncijfers_buurten_2024.xlsx")

    # --- 2023: alle kolommen uit de mapping ---
    df_2023 = haal_kwb(2023, mapping_bestand=MAPPING, verwijder_lege_kolommen=False)
    print(df_2023.head())
    missing_rapport(df_2023, alleen_vanaf=0.20)
    df_2023.to_excel(_SCRIPT_DIR / "kerncijfers_buurten_2023.xlsx", index=False)
    print(f"\n{len(df_2023)} buurten opgeslagen in kerncijfers_buurten_2023.xlsx")

    # --- Subset: alleen inwoners, woningvoorraad en energie ---
    df_subset = haal_kwb(
        2024,
        mapping_bestand=MAPPING,
        kolommen=[
            "aantal_inwoners",
            "woningvoorraad_woningvoorraad",
            "gemiddelde_elektriciteitslevering",
            "gemiddeld_aardgasverbruik",
        ],
    )
    print(df_subset.head())

    # --- Eenmalig: bekijk alle beschikbare kolommen voor een jaar ---
    # toon_kolommen(2024, naar_bestand="kolommen_2024.xlsx")
