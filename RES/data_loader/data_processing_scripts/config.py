"""Configuration for the WarmteAtlas download-and-match pipeline."""
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent  # data_loader/
RAW_DIR = BASE_DIR / "raw"
PROCESSED_DIR = BASE_DIR.parent / "processed_data_from_loader"
DATA_GENERIC = BASE_DIR.parent  # data_Generic/ — final, user-facing outputs (not intermediates)
LOG_DIR = BASE_DIR / "logs"

# WFS / API endpoints
WARMTEATLAS_WFS = "https://www.warmteatlas.nl/geoserver/WarmteAtlas/wfs"
CBS_WB_WFS_TPL = "https://service.pdok.nl/cbs/wijkenbuurten/{year}/wfs/v1_0"
CBS_PC4_WFS_TPL = "https://service.pdok.nl/cbs/postcode4/{year}/wfs/v1_0"
CBS_PC6_WFS_TPL = "https://service.pdok.nl/cbs/postcode6/{year}/wfs/v1_0"

CBS_YEARS = [2023, 2024, 2025]

# PC4/PC6 2025 not yet published; fall back to nearest available year
PC_YEAR_FALLBACK: dict[int, int] = {2025: 2024}

PAGE_SIZE = 2000
REQUEST_TIMEOUT = 180  # seconds per HTTP request
SIZE_LIMIT_BYTES = 1 * 1024**3  # 1 GB — logs a warning but does NOT abort the pipeline
CACHE_MAX_AGE_DAYS = 30  # reuse existing raw file if younger than this

CRS_RD = "EPSG:28992"
CRS_WGS84 = "EPSG:4326"

# Semicolon separator: avoids ambiguity with decimal commas in Dutch numerical data
OUTPUT_SEPARATOR = ";"

# Fill value written to CSV for any unmatched / missing cell (NaN → this string)
FILL_UNMATCHED = "n.a."

WARMTEATLAS_LAYERS_FULL = [
    "WarmteAtlas:AardwarmteKrijtJura",
    "WarmteAtlas:AardwarmteP50Vermogen",
    "WarmteAtlas:AardwarmteRotliegend",
    "WarmteAtlas:AardwarmteTrias",
    "WarmteAtlas:AardwarmteVergunningen",
    "WarmteAtlas:AquathermieEffluent",
    # AquathermieOpenWater      — verwijderd (248 MB, te groot)
    # AquathermieOpenWater_QuickScan — verwijderd (zelfde categorie)
    "WarmteAtlas:AquathermieRWZI",
    "WarmteAtlas:AquathermieRioolGemalen",
    "WarmteAtlas:TEA_assets_met_wko",
    "WarmteAtlas:TEA_assets_minimale_potentie",
    "WarmteAtlas:TEA_assets_zonder_wko",
    "WarmteAtlas:TEA_leidingen_met_wko",
    "WarmteAtlas:TEA_leidingen_minimale_potentie",
    "WarmteAtlas:TEA_leidingen_zonder_wko",
    "WarmteAtlas:TED_potentieel_warmteonttrekking",
    # TEO_potentie   — verwijderd (153 MB, te groot)
    # TEO_quickscan  — verwijderd (zelfde categorie als TEO_potentie)
    "WarmteAtlas:TEA_quickscan",
    "WarmteAtlas:TED_quickscan",
    "WarmteAtlas:warmtevraag_2050",
    "WarmteAtlas:warmtevraag_huidig",
    # Archeologische_Monumentenkaart_2014 — verwijderd (HTTP 400, laag niet beschikbaar via WFS)
    "WarmteAtlas:BuurtEnergieAttributen",
    "WarmteAtlas:BuurtEnergieCodes",
    "WarmteAtlas:BuurtEnergieInfo",
    "WarmteAtlas:CO2EmissieBedrijven",
    "WarmteAtlas:CO2emissieETSbedrijven",
    "WarmteAtlas:CO2emissieEcentrales",
    "WarmteAtlas:CO2emissieGasBewoner",
    "WarmteAtlas:CO2emissieGroteIndustrie",
    "WarmteAtlas:CondensWarmte",
    "WarmteAtlas:DataCentraWarmte",
    "WarmteAtlas:DataCentraWarmte_2021",
    "WarmteAtlas:EnergieBedrijvenPC4",
    "WarmteAtlas:EnergieTopo",
    # GasLeidingenEnexis2020_v2 — verwijderd (323 MB, individuele leidingsegmenten heel NL)
    # GasLeidingenStedin        — verwijderd (zelfde categorie)
    # GasLeveringperWoningPC6 — verwijderd (verouderde data, trage download)
    "WarmteAtlas:GasPerBedrijfsOppervlakte",
    # GasPerWoningOppervlakte — verwijderd (data uit 2014, verouderd; 417k features, traagste laag)
    "WarmteAtlas:GemeenteEnergieCodes",
    "WarmteAtlas:GemeenteEnergieInfo",
    # GroteGebouwen — verwijderd (data uit 2016, verouderd; 12+ min verwerkingstijd)
    "WarmteAtlas:GroteStookInstallaties",
    "WarmteAtlas:IBISbedrijventerreinen",
    "WarmteAtlas:Kassen",
    "WarmteAtlas:KoudeUitGemalenStuwen",
    "WarmteAtlas:LTAardwarmte",
    "WarmteAtlas:LT_MT_WarmteBronnen",
    "WarmteAtlas:LT_WarmteBronnen_ECW",
    "WarmteAtlas:MT_WarmteBronnen",
    "WarmteAtlas:MT_WarmteBronnen_ECW",
    "WarmteAtlas:BioGas",
    "WarmteAtlas:BioMassa",
    "WarmteAtlas:ProductieInstallaties",
    "WarmteAtlas:ProeftuinAardgasvrijeWijken",
    "WarmteAtlas:PAW_monitor_2024",
    "WarmteAtlas:SDE_CO2arm",
    "WarmteAtlas:SDE_elektra",
    "WarmteAtlas:SDE_hernBrandstof",
    "WarmteAtlas:SDE_warmte",
    "WarmteAtlas:TVW_voortgang",
    "WarmteAtlas:Transities",
    # AandachtsgebiedNatuur — verwijderd (niet relevant voor warmtevisie; ~5 min verwerkingstijd)
    "WarmteAtlas:AardkundigeWaarden",
    "WarmteAtlas:RestrictieDiepte",
    "WarmteAtlas:RestrSpecProvBeleid",
    "WarmteAtlas:RestrictieOrdening",
    "WarmteAtlas:Verbodsgebieden",
    "WarmteAtlas:WKOgeslKoudeOpslag",
    "WarmteAtlas:WKOgeslWarmteOpslag",
    "WarmteAtlas:WKOopenKoudeOpslag",
    "WarmteAtlas:WKOopenWarmteOpslag",
    "WarmteAtlas:WarmteCollectorPotentieVelden",
    "WarmteAtlas:WarmteNetten",
    "WarmteAtlas:WarmteNetten_reeks",
    "WarmteAtlas:WarmteUitGemalenStuwen",
    "WarmteAtlas:WarmtenettenInOntwikkeling",
    "WarmteAtlas:WoonKernen",
    "WarmteAtlas:WoonKernen_2012",
    "WarmteAtlas:sportparken",
    "WarmteAtlas:sportterreinen",
]

WARMTEATLAS_LAYERS = [
    # "WarmteAtlas:AardwarmteKrijtJura",
    # "WarmteAtlas:AardwarmteP50Vermogen",
    # "WarmteAtlas:AardwarmteRotliegend",
    # "WarmteAtlas:AardwarmteTrias",
    # "WarmteAtlas:AardwarmteVergunningen",
    # "WarmteAtlas:AquathermieEffluent",
    # # AquathermieOpenWater      — verwijderd (248 MB, te groot)
    # # AquathermieOpenWater_QuickScan — verwijderd (zelfde categorie)
    # "WarmteAtlas:AquathermieRWZI",
    # "WarmteAtlas:AquathermieRioolGemalen",
    # "WarmteAtlas:TEA_assets_met_wko",
    # "WarmteAtlas:TEA_assets_minimale_potentie",
    # "WarmteAtlas:TEA_assets_zonder_wko",
    # # "WarmteAtlas:TEA_leidingen_met_wko",
    # "WarmteAtlas:TEA_leidingen_minimale_potentie",
    # "WarmteAtlas:TEA_leidingen_zonder_wko",
    # "WarmteAtlas:TED_potentieel_warmteonttrekking",
    # TEO_potentie   — verwijderd (153 MB, te groot)
    # TEO_quickscan  — verwijderd (zelfde categorie als TEO_potentie)
    # "WarmteAtlas:TEA_quickscan",
    # "WarmteAtlas:TED_quickscan",
    "WarmteAtlas:warmtevraag_2050",
    "WarmteAtlas:warmtevraag_huidig",
    # Archeologische_Monumentenkaart_2014 — verwijderd (HTTP 400, laag niet beschikbaar via WFS)
    # "WarmteAtlas:BuurtEnergieAttributen",
    # "WarmteAtlas:BuurtEnergieCodes",
    # "WarmteAtlas:BuurtEnergieInfo",
    # "WarmteAtlas:CO2EmissieBedrijven",
    # "WarmteAtlas:CO2emissieETSbedrijven",
    # "WarmteAtlas:CO2emissieEcentrales",
    # "WarmteAtlas:CO2emissieGasBewoner",
    # "WarmteAtlas:CO2emissieGroteIndustrie",
    # "WarmteAtlas:CondensWarmte",
    # "WarmteAtlas:DataCentraWarmte",
    # "WarmteAtlas:DataCentraWarmte_2021",
    # "WarmteAtlas:EnergieBedrijvenPC4",
    # "WarmteAtlas:EnergieTopo",
    # GasLeidingenEnexis2020_v2 — verwijderd (323 MB, individuele leidingsegmenten heel NL)
    # GasLeidingenStedin        — verwijderd (zelfde categorie)
    # GasLeveringperWoningPC6 — verwijderd (verouderde data, trage download)
    "WarmteAtlas:GasPerBedrijfsOppervlakte",
    # GasPerWoningOppervlakte — verwijderd (data uit 2014, verouderd; 417k features, traagste laag)
    "WarmteAtlas:GemeenteEnergieCodes",
    "WarmteAtlas:GemeenteEnergieInfo",
    # GroteGebouwen — verwijderd (data uit 2016, verouderd; 12+ min verwerkingstijd)
    "WarmteAtlas:GroteStookInstallaties",
    # "WarmteAtlas:IBISbedrijventerreinen",
    # "WarmteAtlas:Kassen",
    # "WarmteAtlas:KoudeUitGemalenStuwen",
    "WarmteAtlas:LTAardwarmte",
    "WarmteAtlas:LT_MT_WarmteBronnen",
    "WarmteAtlas:LT_WarmteBronnen_ECW",
    "WarmteAtlas:MT_WarmteBronnen",
    "WarmteAtlas:MT_WarmteBronnen_ECW",
    "WarmteAtlas:BioGas",
    "WarmteAtlas:BioMassa",
    "WarmteAtlas:ProductieInstallaties",
    # "WarmteAtlas:ProeftuinAardgasvrijeWijken",
    # "WarmteAtlas:PAW_monitor_2024",
    "WarmteAtlas:SDE_CO2arm",
    "WarmteAtlas:SDE_elektra",
    "WarmteAtlas:SDE_hernBrandstof",
    "WarmteAtlas:SDE_warmte",
    "WarmteAtlas:TVW_voortgang",
    # "WarmteAtlas:Transities",
    # # AandachtsgebiedNatuur — verwijderd (niet relevant voor warmtevisie; ~5 min verwerkingstijd)
    # "WarmteAtlas:AardkundigeWaarden",
    # "WarmteAtlas:RestrictieDiepte",
    # "WarmteAtlas:RestrSpecProvBeleid",
    # "WarmteAtlas:RestrictieOrdening",
    # "WarmteAtlas:Verbodsgebieden",
    "WarmteAtlas:WKOgeslKoudeOpslag",
    "WarmteAtlas:WKOgeslWarmteOpslag",
    "WarmteAtlas:WKOopenKoudeOpslag",
    "WarmteAtlas:WKOopenWarmteOpslag",
    # "WarmteAtlas:WarmteCollectorPotentieVelden",
    "WarmteAtlas:WarmteNetten",
    "WarmteAtlas:WarmteNetten_reeks",
    "WarmteAtlas:WarmteUitGemalenStuwen",
    "WarmteAtlas:WarmtenettenInOntwikkeling",
    # "WarmteAtlas:WoonKernen",
    # "WarmteAtlas:WoonKernen_2012",
    # "WarmteAtlas:sportparken",
    # "WarmteAtlas:sportterreinen",
]

# Known candidate column names per admin layer (auto-detected at runtime)
CODE_COL_CANDIDATES = {
    "buurten":   ["buurtcode", "BU_CODE", "bu_code"],
    "gemeenten": ["gemeentecode", "GM_CODE", "gm_code"],
    # PDOK CBS PC4/PC6 WFS returns the code column as 'postcode' (not postcode4/postcode6)
    "pc4":       ["postcode4", "pc4", "POSTCODE4", "PC4", "postcode"],
    "pc6":       ["postcode6", "pc6", "POSTCODE6", "PC6", "postcode"],
}
