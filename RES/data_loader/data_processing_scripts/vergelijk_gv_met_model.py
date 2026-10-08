"""Vergelijk de grootverbruik-meetdata (GV) per gemeente met de modelvraag.

Wat het vergelijkt
------------------
  GV-bestanden : gemeten kwartier- (elektriciteit) resp. uurwaarden (aardgas)
                 per gemeente voor aansluitingen > 3x80A, mei 2025 t/m april 2026.
  Model        : `elec_verbruik_<groep>` / `gas_verbruik_<groep>` uit
                 kerncijfers_gemeenten_<jaar>.csv, gesommeerd over alle 8
                 SBI-groepen — de bedrijfsvraag ná RES-reconciliatie.

Waarom de uitkomst NIET 100% hoort te zijn
------------------------------------------
Drie bekende, structurele verschillen; de vergelijking is bedoeld om de
*orde van grootte* te zien, niet om te sluiten:

1. **Andere periode.** GV loopt mei 2025 – april 2026, het model is een
   kalenderjaar (2023 of 2024). Weer, economie en elektrificatie verschillen.
2. **Alleen grootverbruik.** Kleinverbruikaansluitingen (< 3x80A) zitten niet
   in de GV-data, maar wél in de modelvraag. Een groot deel van de
   bedrijfsvestigingen — kantoren, winkels, horeca — is kleinverbruik. De
   GV-som hoort dus *lager* te liggen dan het model.
3. **Geen sectorsplitsing in GV.** De GV-data kent geen SBI-onderscheid, dus er
   kan alleen op het totaal per gemeente worden vergeleken. Bovendien kan een
   GV-aansluiting bij een woningbouwcomplex of een gemaal horen; die valt in het
   model niet onder bedrijfsvraag.

Punt 2 domineert: verwacht een verhouding onder de 100%. Interessant is welke
gemeenten er ver bóven uitkomen — daar staat waarschijnlijk een grote
industriële aansluiting die het model niet ziet, precies het soort onderdrukking
dat de reconciliatie probeert te repareren.

Eenheden
--------
Elektriciteit is expliciet kWh. Voor aardgas ontbreekt de eenheid in de header;
het script leidt die af door de jaarsom tegen het model te houden: een
verhouding rond 1 wijst op m³, rond ~9,8 op kWh (calorische bovenwaarde
aardgas ≈ 9,769 kWh/m³). De gekozen interpretatie wordt geprint.

Draaien:
    python vergelijk_gv_met_model.py [jaar]        # standaard 2023

Schrijft `vergelijk_gv_model_<jaar>.csv` en print een samenvatting.
Read-only t.o.v. de pipeline.
"""

import logging
import sys
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

_BASIS = Path(__file__).parent.parent            # data_loader/
_RES = _BASIS.parent                             # RES/
_GV = {
    "elec": _BASIS / "GV_ELK_GEMEENTE.csv",
    "gas": _BASIS / "GV_GAS_GEMEENTE.csv",
}
_KOLOM_AFNAME = {"elec": "Verbruik afname (KWH)", "gas": "Verbruik afname"}
_KOLOM_INVOEDING = {"elec": "Verbruik invoeding (KWH)", "gas": None}
_GROEPEN = ["a", "bf", "gi", "hj", "kl", "mn", "oq", "ru"]
_CHUNK = 2_000_000
_KWH_PER_M3 = 9.769          # bovenwaarde aardgas


def _lees_gv(carrier: str) -> tuple[pd.DataFrame, tuple[str, str]]:
    """Aggregeer een GV-bestand naar één rij per gemeente.

    Gechunkt ingelezen: de elektriciteitsfile heeft ~12 mln regels (633 MB) en
    past niet comfortabel in het geheugen. Alleen de benodigde kolommen worden
    gelezen. De periode wordt bepaald met een string-min/max op de
    ISO-datumkolom — lexicografisch gelijk aan chronologisch, en dat scheelt het
    parsen van 12 mln timestamps.
    """
    pad = _GV[carrier]
    if not pad.exists():
        log.error("  %s niet gevonden.", pad)
        return pd.DataFrame(), ("", "")

    afname = _KOLOM_AFNAME[carrier]
    invoeding = _KOLOM_INVOEDING[carrier]
    kolommen = ["Datumtijd", "Gemeente", afname,
                "Aantal aansluitingen => 10 afname", "SJV Afname outlier"]
    if invoeding:
        kolommen.append(invoeding)

    delen, t_min, t_max = [], None, None
    for chunk in pd.read_csv(pad, sep=";", usecols=kolommen, chunksize=_CHUNK,
                             low_memory=False):
        d = chunk["Datumtijd"].astype(str)
        t_min = min(x for x in (t_min, d.min()) if x)
        t_max = max(x for x in (t_max, d.max()) if x)

        chunk[afname] = pd.to_numeric(chunk[afname], errors="coerce")
        # Lege waarde = privacy-onderdrukking (< 10 aansluitingen); die mag NIET
        # als 0 meetellen. min_count=1 laat een volledig lege gemeente op NaN
        # staan in plaats van op 0, zodat 'onbekend' en 'nul' onderscheiden
        # blijven. Zonder dit lijken 141 van de 336 gemeenten 0 m3 gas te
        # gebruiken en klopt elk landelijk percentage niet meer.
        chunk["_leeg"] = chunk[afname].isna()
        agg = {afname: lambda s: s.sum(min_count=1),
               "_leeg": "sum",
               "Aantal aansluitingen => 10 afname": "mean",
               "SJV Afname outlier": "mean",
               "Datumtijd": "size"}
        if invoeding:
            chunk[invoeding] = pd.to_numeric(chunk[invoeding], errors="coerce")
            agg[invoeding] = lambda s: s.sum(min_count=1)
        delen.append(chunk.groupby("Gemeente").agg(agg))

    if not delen:
        return pd.DataFrame(), ("", "")

    samen = pd.concat(delen)
    uit = samen.groupby(level=0).agg(
        {afname: lambda s: s.sum(min_count=1),
         **({invoeding: (lambda s: s.sum(min_count=1))} if invoeding else {}),
         "_leeg": "sum",
         "Datumtijd": "sum",
         "Aantal aansluitingen => 10 afname": "mean",
         "SJV Afname outlier": "mean"}
    )
    uit = uit.rename(columns={
        afname: f"gv_{carrier}_afname",
        **({invoeding: f"gv_{carrier}_invoeding"} if invoeding else {}),
        "_leeg": f"gv_{carrier}_n_onderdrukt",
        "Datumtijd": f"gv_{carrier}_n_metingen",
        "Aantal aansluitingen => 10 afname": f"gv_{carrier}_aandeel_min10_aansl",
        "SJV Afname outlier": f"gv_{carrier}_aandeel_outlier",
    })
    # Volledig onderdrukte gemeenten: elke meting leeg -> niet vergelijkbaar.
    uit[f"gv_{carrier}_volledig_onderdrukt"] = (
        uit[f"gv_{carrier}_n_onderdrukt"] >= uit[f"gv_{carrier}_n_metingen"])
    n_vol = int(uit[f"gv_{carrier}_volledig_onderdrukt"].sum())
    n_leeg = int(uit[f"gv_{carrier}_n_onderdrukt"].sum())
    n_tot = int(uit[f"gv_{carrier}_n_metingen"].sum())
    log.info("  %s: %d gemeenten, %d metingen, periode %s t/m %s",
             pad.name, len(uit), n_tot, t_min[:10], t_max[:10])
    log.info("     onderdrukt (< 10 aansluitingen, waarde leeg): %d metingen (%.1f%%); "
             "%d gemeenten volledig zonder data", n_leeg, 100 * n_leeg / max(n_tot, 1), n_vol)
    return uit, (t_min, t_max)


def _model(jaar: int) -> pd.DataFrame:
    """Bedrijfsvraag per gemeente uit de kerncijfers-CSV (som over 8 groepen)."""
    pad = _RES / f"kerncijfers_gemeenten_{jaar}.csv"
    if not pad.exists():
        log.error("  %s niet gevonden — draai eerst --gemeenten-potentie.", pad)
        return pd.DataFrame()
    d = pd.read_csv(pad, sep=";", low_memory=False)

    def _som(prefix, eenheid):
        kolommen = [f"{prefix}_{g}_{eenheid}" for g in _GROEPEN]
        aanwezig = [c for c in kolommen if c in d.columns]
        return d[aanwezig].apply(pd.to_numeric, errors="coerce").sum(axis=1)

    uit = pd.DataFrame({
        "gemeentecode": d["gemeentecode"],
        "gemeentenaam": d["gemeentenaam"],
        "res_regio": d.get("res_regio"),
        "model_elec": _som("elec_verbruik", "kwh"),
        "model_gas": _som("gas_verbruik", "m3"),
        "elec_bijgeschat_aandeel": pd.to_numeric(
            d.get("elec_bijgeschat_aandeel"), errors="coerce"),
        "gas_bijgeschat_aandeel": pd.to_numeric(
            d.get("gas_bijgeschat_aandeel"), errors="coerce"),
    })
    log.info("  model %d: %d gemeenten", jaar, len(uit))
    return uit


_CBS_KLASSEN = _BASIS / "energieleveringen_bedrijven_en_instellingen_naar_verbruiksklasse_2023_2024.xlsx"
_CBS_TABEL = {2023: "Tabel 4", 2024: "Tabel 5"}


def _cbs_verbruiksklassen(jaar: int) -> pd.DataFrame:
    """CBS-leveringen per gemeente, uitgesplitst naar verbruiksklasse.

    Bron: CBS-maatwerk "Energieleveringen bedrijven en instellingen naar
    verbruiksklasse, 2023-2024" (PR004610, i.o.v. RVO). Tabel 4 = 2023,
    Tabel 5 = 2024. Eenheden in het bestand: aardgas in 1.000 m3,
    elektriciteit in 1.000 kWh; hier omgerekend naar m3 en kWh.

    Klassegrenzen (Tabel 1 van dezelfde publicatie), op JAARVERBRUIK:
        M  elektriciteit  50.001 -    200.000 kWh | aardgas  25.001 -  75.000 m3
        G  elektriciteit 200.001 - 10.000.000 kWh | aardgas  75.001 - 170.000 m3
        ZG elektriciteit      > 10.000.000 kWh    | aardgas     > 170.000 m3

    Waarom dit als ijkpunt voor GV/KV kan dienen: een 3x80A-aansluiting is ruwweg
    55 kVA, dus maximaal ~480 MWh/jaar bij volcontinu vollast. Klasse ZG (>10 GWh)
    kan dus onmogelijk kleinverbruik zijn, en klasse G (>200 MWh) vrijwel evenmin
    — dat vergt al een belastingfactor van >40% op een maximale KV-aansluiting.
    Klasse M (50-200 MWh) zit juist grotendeels ónder de grens. Vandaar:
        ondergrens GV = ZG
        bovengrens GV = G + ZG
    De gemeten GV-reeks hoort tussen die twee te liggen.

    Let op: de klasse-indeling is per ADRES en gecombineerd over beide dragers
    (zie de matrix in Tabel 1). Een adres met veel gas maar weinig stroom valt in
    een hoge klasse, en zijn kleine elektriciteitslevering telt dan in die hoge
    klasse mee. De opsplitsing is dus niet zuiver per drager.

    Adressen onder M (<50.000 kWh én <25.000 m3) zitten NIET in deze tabel; de
    som van M+G+ZG is daarom lager dan de totale bedrijfslevering.
    """
    if not _CBS_KLASSEN.exists():
        log.warning("  CBS-verbruiksklassen niet gevonden (%s) — ijkpunt overgeslagen.",
                    _CBS_KLASSEN.name)
        return pd.DataFrame()
    blad = _CBS_TABEL.get(jaar)
    if blad is None:
        log.warning("  Geen CBS-tabblad voor jaar %d.", jaar)
        return pd.DataFrame()

    kolommen = ["nr", "provinciecode", "provincienaam", "gemeentecode", "gemeente",
                "adres_M", "adres_G", "adres_ZG",
                "gas_M", "gas_G", "gas_ZG", "elec_M", "elec_G", "elec_ZG"]
    ruw = pd.read_excel(_CBS_KLASSEN, sheet_name=blad, header=None, skiprows=5,
                        names=kolommen)
    # De TOTAAL-rij apart bewaren: CBS onderdrukt veel gemeentecellen ('.'),
    # dus de som over gemeenten is stelselmatig te laag. Voor het landelijke
    # ijkpunt moet de gepubliceerde TOTAAL-rij gebruikt worden, anders vergelijk
    # je een volledige GV-reeks met een uitgedund CBS-totaal en lijkt GV
    # onterecht te hoog.
    totaalrij = ruw[ruw["gemeentecode"].astype(str).str.upper() == "GMTOTAAL"]
    d = ruw[ruw["gemeentecode"].astype(str).str.match(r"^GM\d{4}$", na=False)].copy()
    for c in kolommen[5:]:
        # '.' = onderdrukt door CBS -> NaN, niet 0.
        d[c] = pd.to_numeric(d[c].replace(".", pd.NA), errors="coerce")
    for c in ["gas_M", "gas_G", "gas_ZG"]:
        d[c] = d[c] * 1000          # 1.000 m3 -> m3
    for c in ["elec_M", "elec_G", "elec_ZG"]:
        d[c] = d[c] * 1000          # 1.000 kWh -> kWh

    d["cbs_elec_gv_onder"] = d["elec_ZG"]
    d["cbs_elec_gv_boven"] = d[["elec_G", "elec_ZG"]].sum(axis=1, min_count=1)
    d["cbs_elec_klassen_totaal"] = d[["elec_M", "elec_G", "elec_ZG"]].sum(axis=1, min_count=1)
    d["cbs_gas_gv_onder"] = d["gas_ZG"]
    d["cbs_gas_gv_boven"] = d[["gas_G", "gas_ZG"]].sum(axis=1, min_count=1)
    d["cbs_gas_klassen_totaal"] = d[["gas_M", "gas_G", "gas_ZG"]].sum(axis=1, min_count=1)

    n_onderdrukt = int(ruw.loc[d.index, ["elec_G", "elec_ZG", "gas_G", "gas_ZG"]]
                       .isna().sum().sum())
    log.info("  CBS-verbruiksklassen (%s, jaar %d): %d gemeenten, %d onderdrukte cellen",
             blad, jaar, len(d), n_onderdrukt)

    uit = d[["gemeentecode"] + [c for c in d.columns if c.startswith("cbs_")]].copy()
    # Landelijke TOTAAL-rij als attribuut meesturen (pandas bewaart .attrs).
    if not totaalrij.empty:
        t = totaalrij.iloc[0]
        uit.attrs["nl_totaal"] = {
            "elec_M": float(t["elec_M"]) * 1000, "elec_G": float(t["elec_G"]) * 1000,
            "elec_ZG": float(t["elec_ZG"]) * 1000,
            "gas_M": float(t["gas_M"]) * 1000, "gas_G": float(t["gas_G"]) * 1000,
            "gas_ZG": float(t["gas_ZG"]) * 1000,
        }
    return uit


def _normaliseer(naam) -> str:
    """'s-Gravenhage -> sgravenhage; 'Beek (L.)' -> beek.

    De GV-export zet een provincie-achtervoegsel achter namen die in meerdere
    provincies voorkomen — Beek (L.), Hengelo (O.), Laren (NH.), Middelburg (Z.),
    Rijswijk (ZH.), Stein (L.). CBS doet dat niet, dus zonder het strippen van
    dat haakje vallen precies die zes gemeenten buiten de vergelijking.
    """
    import re
    zonder_suffix = re.sub(r"\s*\([^)]*\)\s*$", "", str(naam))
    return re.sub(r"[^a-z0-9]", "", zonder_suffix.lower())


def _bepaal_gaseenheid(samen: pd.DataFrame) -> tuple[float, str]:
    """Leid af of de gaskolom in m³ of kWh staat.

    Vergelijkt de landelijke GV-som met de modelsom. Rond 1 -> m³; rond 9,8 ->
    kWh. Bij twijfel wordt m³ aangehouden (geen omrekening) en dat gemeld —
    beter een zichtbaar rare verhouding dan een stille factor 10.
    """
    # Alleen gemeenten mét gaswaarde, anders vergelijk je een deelverzameling
    # GV tegen de volledige modelvraag en komt de ratio structureel te laag uit.
    vgl = samen[samen["gv_gas_afname"].notna()]
    gv = vgl["gv_gas_afname"].sum(min_count=1)
    model = vgl["model_gas"].sum(min_count=1)
    if not (gv and model):
        return 1.0, "onbepaald (geen data)"
    ratio = gv / model
    if 0.2 <= ratio <= 3:
        return 1.0, f"m3 (ratio {ratio:.2f})"
    if 3 < ratio <= 30:
        return 1.0 / _KWH_PER_M3, f"kWh -> gedeeld door {_KWH_PER_M3} (ratio {ratio:.2f})"
    return 1.0, f"ONBEKEND, niet omgerekend (ratio {ratio:.2f} — controleer handmatig)"


def main(jaar: int) -> int:
    log.info("=== 1. GV-bestanden inlezen ===")
    gv_elec, per_e = _lees_gv("elec")
    gv_gas, per_g = _lees_gv("gas")
    if gv_elec.empty and gv_gas.empty:
        return 1

    log.info("\n=== 2. Model inlezen ===")
    model = _model(jaar)
    if model.empty:
        return 1

    gv = gv_elec.join(gv_gas, how="outer") if not gv_gas.empty else gv_elec
    gv.index.name = "gv_gemeente"
    gv = gv.reset_index()
    gv["sleutel"] = gv["gv_gemeente"].map(_normaliseer)
    model["sleutel"] = model["gemeentenaam"].map(_normaliseer)

    samen = model.merge(gv, on="sleutel", how="outer", indicator=True)
    alleen_model = samen[samen["_merge"] == "left_only"]["gemeentenaam"].dropna().tolist()
    alleen_gv = samen[samen["_merge"] == "right_only"]["gv_gemeente"].dropna().tolist()
    if alleen_model:
        log.warning("  %d gemeenten alleen in het model (geen GV-data): %s%s",
                    len(alleen_model), alleen_model[:8],
                    " ..." if len(alleen_model) > 8 else "")
    if alleen_gv:
        log.warning("  %d gemeentenamen alleen in GV (niet gematcht): %s%s",
                    len(alleen_gv), alleen_gv[:8], " ..." if len(alleen_gv) > 8 else "")
    samen = samen[samen["_merge"] == "both"].drop(columns="_merge")
    log.info("  %d gemeenten gematcht", len(samen))

    cbs = _cbs_verbruiksklassen(jaar)
    nl_totaal = cbs.attrs.get("nl_totaal") if not cbs.empty else None
    if not cbs.empty:
        samen = samen.merge(cbs, on="gemeentecode", how="left")
        samen.attrs["nl_totaal"] = nl_totaal

    factor, toelichting = _bepaal_gaseenheid(samen)
    log.info("\n=== 3. Eenheid aardgas ===\n  %s", toelichting)
    if "gv_gas_afname" in samen.columns:
        samen["gv_gas_afname_m3"] = samen["gv_gas_afname"] * factor

    for carrier, gvcol, modelcol in (
        ("elec", "gv_elec_afname", "model_elec"),
        ("gas", "gv_gas_afname_m3", "model_gas"),
    ):
        if gvcol not in samen.columns:
            continue
        samen[f"{carrier}_verschil"] = samen[gvcol] - samen[modelcol]
        samen[f"{carrier}_gv_pct_van_model"] = (
            100 * samen[gvcol] / samen[modelcol].where(samen[modelcol] > 0)
        ).round(1)

    uit = _RES / "data_loader" / "data_processing_scripts" / f"vergelijk_gv_model_{jaar}.csv"
    samen.sort_values("gemeentenaam").to_csv(uit, index=False, sep=";")
    log.info("\n  -> %s", uit.name)
    _rapport(samen, jaar, per_e, per_g)
    return 0


def _rapport(s: pd.DataFrame, jaar: int, per_e, per_g) -> None:
    print("\n" + "=" * 78)
    print(f"GV (mei 2025 – apr 2026) vs. model {jaar}")
    print("=" * 78)
    print(f"  elektriciteit GV-periode: {per_e[0][:10]} t/m {per_e[1][:10]}")
    print(f"  aardgas       GV-periode: {per_g[0][:10]} t/m {per_g[1][:10]}")

    for carrier, gvcol, modelcol, eenheid, deler in (
        ("Elektriciteit", "gv_elec_afname", "model_elec", "GWh", 1e6),
        ("Aardgas", "gv_gas_afname_m3", "model_gas", "mln m3", 1e6),
    ):
        if gvcol not in s.columns:
            continue
        c = "elec" if "elec" in gvcol else "gas"
        kol = f"{c}_gv_pct_van_model"
        # Alleen gemeenten met daadwerkelijk gepubliceerde GV-data vergelijken.
        # Een volledig onderdrukte gemeente meetellen als 0 zou het landelijke
        # percentage naar beneden trekken op grond van niet-bestaande nullen.
        vgl = s[s[gvcol].notna()]
        onderdrukt = s[s[gvcol].isna()]

        gv, mo = vgl[gvcol].sum(min_count=1), vgl[modelcol].sum(min_count=1)
        print(f"\n--- {carrier} ---")
        print(f"  vergelijkbaar: {len(vgl)} gemeenten | zonder GV-data (onderdrukt): "
              f"{len(onderdrukt)}")
        if len(onderdrukt):
            gemist = onderdrukt[modelcol].sum(min_count=1)
            print(f"     die {len(onderdrukt)} vertegenwoordigen {gemist/deler:,.0f} "
                  f"{eenheid} modelvraag ({100*gemist/s[modelcol].sum():.1f}% van NL) — "
                  f"buiten de vergelijking gelaten")
        print(f"  NL (vergelijkbare gemeenten): GV {gv/deler:,.0f} {eenheid}  vs  "
              f"model {mo/deler:,.0f} {eenheid}   -> GV is {100*gv/mo:.1f}% van het model")

        dr = s[s["res_regio"].astype(str).str.contains("Drecht", na=False)]
        if not dr.empty:
            drv = dr[dr[gvcol].notna()]
            g2, m2 = drv[gvcol].sum(min_count=1), drv[modelcol].sum(min_count=1)
            print(f"  Drechtsteden ({len(drv)} van {len(dr)} gemeenten met data): "
                  f"GV {g2/deler:,.1f} vs model {m2/deler:,.1f} {eenheid}"
                  f"   -> {100*g2/m2:.1f}%")
            toon = dr[["gemeentenaam", gvcol, modelcol, kol]].copy()
            toon[gvcol] = (toon[gvcol] / deler).round(1)
            toon[modelcol] = (toon[modelcol] / deler).round(1)
            toon[kol] = toon[kol].map(lambda v: "onderdrukt" if pd.isna(v) else f"{v:.1f}")
            print(toon.to_string(index=False))

        boven = vgl[vgl[kol] > 100].sort_values(kol, ascending=False)
        print(f"\n  {len(boven)} van {len(vgl)} vergelijkbare gemeenten hebben MEER gemeten "
              f"grootverbruik dan modelvraag —")
        print("  daar mist het model waarschijnlijk een grote aansluiting:")
        print(boven.head(10)[["gemeentenaam", kol]].to_string(index=False))

    if "cbs_elec_gv_boven" not in s.columns:
        print("\n(CBS-verbruiksklassen niet geladen — geen onafhankelijk ijkpunt)")
        return

    print("\n" + "=" * 78)
    print("IJKPUNT: CBS-verbruiksklassen (onafhankelijke bron)")
    print("=" * 78)
    print("  ZG = ondergrens voor GV, G+ZG = bovengrens. De gemeten GV-reeks")
    print("  hoort daartussen te liggen; zie de docstring voor de onderbouwing.")

    for carrier, gvcol, modelcol, eenheid, deler in (
        ("Elektriciteit", "gv_elec_afname", "model_elec", "GWh", 1e6),
        ("Aardgas", "gv_gas_afname_m3", "model_gas", "mln m3", 1e6),
    ):
        c = "elec" if "elec" in gvcol else "gas"
        onder, boven = f"cbs_{c}_gv_onder", f"cbs_{c}_gv_boven"
        totaal = f"cbs_{c}_klassen_totaal"
        if gvcol not in s.columns or boven not in s.columns:
            continue
        nl = s.attrs.get("nl_totaal")
        if not nl:
            continue
        # Landelijk ijkpunt uit de gepubliceerde TOTAAL-rij, tegen de volledige
        # GV-som. Optellen van gemeentecellen zou het CBS-totaal uithollen door
        # de onderdrukte cellen en GV kunstmatig hoog laten lijken.
        lo = nl[f"{c}_ZG"]
        hi = nl[f"{c}_G"] + nl[f"{c}_ZG"]
        tot = nl[f"{c}_M"] + hi
        gv = s[gvcol].sum(min_count=1)
        mo = s[modelcol].sum(min_count=1)
        n_gv = int(s[gvcol].notna().sum())

        print(f"\n--- {carrier} ---")
        print(f"  gemeten GV        {gv/deler:>10,.0f} {eenheid}   ({n_gv} gemeenten met data)")
        print(f"  CBS ZG   (onder)  {lo/deler:>10,.0f} {eenheid}   -> GV is {100*gv/lo:.0f}% hiervan")
        print(f"  CBS G+ZG (boven)  {hi/deler:>10,.0f} {eenheid}   -> GV is {100*gv/hi:.0f}% hiervan")
        binnen = lo <= gv <= hi
        print("  " + ("PLAUSIBEL: gemeten GV valt binnen de verwachte band"
                      if binnen else "LET OP: gemeten GV valt BUITEN de verwachte band"))
        if n_gv < len(s):
            print(f"     (let op: {len(s)-n_gv} gemeenten zonder GV-data, dus GV is hier "
                  f"een ondertelling)")
        print(f"  model             {mo/deler:>10,.0f} {eenheid}")
        print(f"  CBS M+G+ZG        {tot/deler:>10,.0f} {eenheid}   -> model is {100*mo/tot:.0f}% hiervan")
        print("     (CBS mist adressen onder de M-grens, dus model > CBS-totaal is normaal)")

    print("\nLet op: GV bevat geen kleinverbruik (< 3x80A) en volgens de")
    print("producentdocumentatie ook geen rechtstreeks op TenneT aangesloten")
    print("verbruikers. Een percentage onder de 100% van de modelvraag is dus")
    print("normaal; het CBS-ijkpunt hierboven zegt of het de goede orde heeft.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2023))
