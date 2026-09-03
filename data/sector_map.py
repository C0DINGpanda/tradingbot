"""
data/sector_map.py — Maps NSE stocks to their sector indices.

Sector indices (yfinance tickers):
  ^NSEBANK   — Bank Nifty
  ^CNXIT     — Nifty IT
  ^CNXAUTO   — Nifty Auto
  ^CNXMETAL  — Nifty Metal
  ^CNXPHARMA — Nifty Pharma
  ^CNXENERGY — Nifty Energy
  ^CNXFMCG   — Nifty FMCG
  ^CNXREALTY — Nifty Realty
  ^CNXPSUBANK— Nifty PSU Bank
  ^CNXINFRA  — Nifty Infra
  ^NIFTY50   — Nifty 50 (broad market)
"""

# Sector index → yfinance symbol
SECTOR_INDICES = {
    "banking":      "^NSEBANK",
    "it":           "^CNXIT",
    "auto":         "^CNXAUTO",
    "metal":        "^CNXMETAL",
    "pharma":       "^CNXPHARMA",
    "energy":       "^CNXENERGY",
    "fmcg":         "^CNXFMCG",
    "realty":       "^CNXREALTY",
    "psu_bank":     "^CNXPSUBANK",
    "infra":        "^CNXINFRA",
    "broad":        "^NSEI",          # Nifty 50 — fallback for unmapped stocks
}

# Angel One sector index tokens (for AngelFetcher)
SECTOR_ANGEL_TOKENS = {
    "banking":  "26009",
    "it":       "26013",
    "auto":     "26008",
    "metal":    "26015",
    "pharma":   "26016",
    "energy":   "26012",
    "fmcg":     "26011",
    "realty":   "26018",
    "psu_bank": "26017",
    "infra":    "26010",
    "broad":    "26000",
}

# Stock → sector mapping
STOCK_SECTOR: dict[str, str] = {
    # Banking
    "HDFCBANK":    "banking", "ICICIBANK":   "banking", "SBIN":        "banking",
    "AXISBANK":    "banking", "KOTAKBANK":   "banking", "INDUSINDBK":  "banking",
    "BANKBARODA":  "banking", "PNB":         "banking", "CANBK":       "banking",
    "FEDERALBNK":  "banking", "IDFCFIRSTB":  "banking", "BANDHANBNK":  "banking",
    "YESBANK":     "banking", "UJJIVANSFB":  "banking", "KARURVYSYA":  "banking",

    # PSU Banks
    "UCOBANK":     "psu_bank", "UNIONBANK":  "psu_bank", "MAHABANK":   "psu_bank",

    # IT
    "TCS":         "it", "INFY":        "it", "WIPRO":       "it",
    "HCLTECH":     "it", "TECHM":       "it", "LTIM":        "it",
    "MPHASIS":     "it", "COFORGE":     "it", "PERSISTENT":  "it",
    "KPITTECH":    "it", "TATAELXSI":   "it", "HAPPSTMNDS":  "it",
    "OFSS":        "it", "CYIENT":      "it", "INTELLECT":   "it",

    # Auto
    "MARUTI":      "auto", "TATAMOTORS":  "auto", "BAJAJ-AUTO":  "auto",
    "HEROMOTOCO":  "auto", "EICHERMOT":   "auto", "TVSMOTOR":    "auto",
    "ASHOKLEY":    "auto", "ESCORTS":     "auto", "BALKRISIND":  "auto",
    "MOTHERSON":   "auto", "BOSCHLTD":    "auto", "BHARATFORG":  "auto",

    # Metal & Mining
    "TATASTEEL":   "metal", "JSWSTEEL":    "metal", "HINDALCO":    "metal",
    "VEDL":        "metal", "SAIL":        "metal", "NMDC":        "metal",
    "COALINDIA":   "metal", "JINDALSTEL":  "metal", "NATIONALUM":  "metal",
    "HINDCOPPER":  "metal", "APLAPOLLO":   "metal",

    # Pharma & Healthcare
    "SUNPHARMA":   "pharma", "DRREDDY":     "pharma", "CIPLA":       "pharma",
    "DIVISLAB":    "pharma", "APOLLOHOSP":  "pharma", "LUPIN":       "pharma",
    "BIOCON":      "pharma", "AUROPHARMA":  "pharma", "IPCALAB":     "pharma",
    "ALKEM":       "pharma", "TORNTPHARM":  "pharma", "GRANULES":    "pharma",

    # Energy & Oil
    "RELIANCE":    "energy", "ONGC":        "energy", "BPCL":        "energy",
    "IOC":         "energy", "GAIL":        "energy", "HINDPETRO":   "energy",
    "MRPL":        "energy", "PETRONET":    "energy", "GSPL":        "energy",
    "ATGL":        "energy", "TATAPOWER":   "energy", "NTPC":        "energy",
    "POWERGRID":   "energy", "ADANIGREEN":  "energy", "JSWENERGY":   "energy",

    # FMCG
    "HINDUNILVR":  "fmcg", "ITC":         "fmcg", "NESTLEIND":   "fmcg",
    "BRITANNIA":   "fmcg", "DABUR":       "fmcg", "MARICO":      "fmcg",
    "COLPAL":      "fmcg", "GODREJCP":    "fmcg", "EMAMILTD":    "fmcg",
    "TATACONSUM":  "fmcg", "VBL":         "fmcg", "VARUNBEV":    "fmcg",

    # Realty
    "DLF":         "realty", "GODREJPROP":  "realty", "OBEROIRLTY":  "realty",
    "MACROTECH":   "realty", "PRESTIGE":    "realty", "PHOENIXLTD":  "realty",

    # Infra & Capital Goods
    "LT":          "infra", "SIEMENS":     "infra", "ABB":         "infra",
    "HAVELLS":     "infra", "POLYCAB":     "infra", "ADANIPORTS":  "infra",
    "ADANIENT":    "infra", "GMRINFRA":    "infra", "IRB":         "infra",

    # Telecom
    "BHARTIARTL":  "broad", "IDEA":        "broad",

    # Financials (NBFC)
    "BAJFINANCE":  "banking", "BAJAJFINSV":  "banking", "CHOLAFIN":    "banking",
    "MUTHOOTFIN":  "banking", "LICHSGFIN":   "banking", "HDFCLIFE":    "banking",
    "SBILIFE":     "banking", "ICICIGI":     "banking",

    # Cement & Materials
    "ULTRACEMCO":  "infra", "SHREECEM":    "infra", "AMBUJACEM":   "infra",
    "ACC":         "infra", "JKCEMENT":    "infra",

    # Consumer Durables / Others
    "TITAN":       "fmcg", "ASIANPAINT":  "fmcg", "BERGEPAINT":  "fmcg",
    "PIDILITIND":  "fmcg",
}


def get_sector(symbol: str) -> str:
    """Return the sector name for a symbol, defaulting to 'broad'."""
    return STOCK_SECTOR.get(symbol, "broad")


def get_sector_index(symbol: str) -> str:
    """Return the yfinance sector index ticker for a symbol."""
    sector = get_sector(symbol)
    return SECTOR_INDICES.get(sector, SECTOR_INDICES["broad"])


def get_all_sector_indices() -> list[str]:
    """Return all unique sector index tickers."""
    return list(set(SECTOR_INDICES.values()))
