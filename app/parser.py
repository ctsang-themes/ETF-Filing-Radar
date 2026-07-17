"""
Extraction logic for the two things that actually trip this project up:

1. Which effective-date box is checked on the Rule 485 facing sheet.
2. Who the real issuer is, as distinct from the registrant Trust name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHECKED_MARKERS = ("\u2612", "[X]", "[x]", "(X)")

FACING_SHEET_OPTIONS = [
    ("immediately upon filing pursuant to paragraph (b)", "485b-immediate"),
    ("on (date) pursuant to paragraph (b)", "485b-date"),
    ("60 days after filing pursuant to paragraph (a)(1)", "485a1-60"),
    ("on (date) pursuant to paragraph (a)(1)", "485a1-date"),
    ("75 days after filing pursuant to paragraph (a)(2)", "485a2-75"),
    ("on (date) pursuant to paragraph (a)(2) of Rule 485", "485a2-date"),
]

DAYS_BY_BASIS = {"485a1-60": 60, "485a2-75": 75}

EXPLICIT_DATE_RE = re.compile(
    r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})"
)

ADVISER_ANCHOR_RE = re.compile(
    r"(?:(?:has\s+)?serv(?:e|es|ed|ing)\s+as|acts?\s+as|is|are|was)\s+"
    r"(?:the\s+)?(?:Fund'?s\s+)?investment adviser",
    re.IGNORECASE,
)
ADVISER_NAME_BEFORE_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.,'\-]*(?:\s+[A-Z(][A-Za-z0-9&.,'\")\-]*){0,4})\s*$"
)

ADVISER_REJECT_TERMS = {
    "the adviser", "adviser", "the fund", "the trust", "the board",
    "sub-adviser", "the sub-adviser", "the firm", "firm",
}
ADVISER_REJECT_WORDS = {
    "act", "amended", "officers", "directors", "registered", "under",
    "for", "is", "are", "was", "the", "in", "on", "at", "to", "by", "of",
    "whereas", "file", "no", "sec",
}

ADVISER_LABEL_RE = re.compile(
    r"(?:(?i:Investment Adviser[s]?))\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,'\-\s]{2,80}?)"
    r"(?=\s*(?:Sub-[Aa]dviser|Distributor|Administrator|Custodian|\.|,\s*(?:LLC|LP|Inc)\b[.,]|$))"
)

SPONSOR_SECTION_RE = re.compile(r"FUND SPONSOR", re.IGNORECASE)
SPONSOR_NAME_RE = re.compile(
    r"sponsorship agreement with\s+[A-Z][A-Za-z0-9&.,'\-\s]*?"
    r"\(\s*[\u201c\"]([A-Z][A-Za-z0-9&.,'\-\s]*?)[\u201d\"]\s*\)",
    re.IGNORECASE,
)

SHARED_TRUST_PLATFORMS = {
    "tidal trust ii",
    "tidal etf trust",
    "listed funds trust",
    "etf series solutions",
    "advisors series trust",
    "northern lights fund trust",
    "exchange traded concepts trust",
}

KNOWN_SHELL_ADVISERS = {
    "tidal investments llc",
    "toroso investments",
    "vident investment advisory",
    "exchange traded concepts, llc",
    "exchange traded concepts llc",
    "zega financial",
}

KNOWN_ISSUER_BRANDS = sorted(
    [
    "Rockefeller Capital Management", "Summit Global Investments",
    "New York Life Investments", "Portfolio Building Block",
    "Measured Risk Portfolios", "Opus Capital Management", "Faith Investor Services",
    "Sound Income Strategies", "Segall Bryant & Hamill", "Little Harbor Advisors",
    "Baillie Gifford Funds", "Parnassus Investments", "CrossingBridge Funds",
    "Point Bridge Capital", "Intelligent Investor", "The Brinsmere Funds",
    "US Benchmark Series", "Armada ETF Advisors", "Russell Investments",
    "Deutsche X-trackers", "McElhenny Sheffield", "Alternative Access",
    "Variant Perception", "Franklin Templeton", "Relative Sentiment",
    "SoundWatch Capital", "Tweedy, Browne Co.", "US Commodity Funds",
    "Symmetry Panoramic", "Ned Davis Research", "Guinness Atkinson",
    "AllianceBernstein", "Carbon Collective", "Performance Trust", "Donoghue Forlines",
    "Clockwise Capital", "Volatility Shares", "Core Alternative", "Discipline Funds",
    "Federated Hermes", "Fidelity Advisor", "Hotchkis & Wiley", "BrandywineGLOBAL",
    "American Century", "REX Microsectors", "Sarmaya Partners", "Neuberger Berman",
    "CoreValues Alpha", "Horizon Kinetics", "Wilmington Funds", "Sterling Capital",
    "First Manhattan", "EA Series Trust", "Alpha Architect", "The Future Fund",
    "American Beacon", "Harrison Street", "Strategy Shares", "Hypatia Capital",
    "Leverage Shares", "Janus Henderson", "AXS Investments", "Applied Finance",
    "Shelton Capital", "Morgan Dempsey", "Prospera Funds", "Tactical Funds",
    "Worth Charting", "Return Stacked", "Tuttle Capital", "Genter Capital",
    "Acquirers Fund", "Myriad Capital", "Northern Trust", "Cohen & Steers",
    "Rareview Funds", "Climate Global", "Morgan Stanley", "Overlay Shares",
    "Brown Advisory", "Northern Funds", "Conductor Fund", "The Nightview",
    "Raymond James", "Texas Capital", "Mairs & Power", "Pacific Funds",
    "Mason Capital", "USCF Advisers", "T. Rowe Price", "Opportunistic",
    "Capital Group", "Palmer Square", "Impact Shares", "ActivePassive",
    "VictoryShares", "Cambiar Funds", "Bahl & Gaynor", "GraniteShares",
    "AdvisorShares", "Goldman Sachs", "Golden Eagle", "LeaderShares", "GQG Partners",
    "Diamond Hill", "Counterpoint", "Cyber Hornet", "Archer Funds", "SanJac Alpha",
    "Transamerica", "RAFI Indices", "John Hancock", "Billionaires", "Essential 40",
    "State Street", "Brendan Wood", "FCF Advisors", "Truth Social", "North Square",
    "Goose Hollow", "TimesSquare", "KraneShares", "Arrow Funds", "First Eagle",
    "Eaton Vance", "Sovereign's", "North Shore", "Income STKd", "Liberty One",
    "Motley Fool", "Stone Ridge", "ROBO Global", "VistaShares", "Leatherback",
    "ArrowShares", "Free Market", "Dimensional", "First Trust", "Renaissance",
    "Asset Class", "Convergence", "WealthTrust", "FolioBeyond", "SonicShares",
    "ClearShares", "ClearBridge", "Mohr Funds", "CoinShares", "Chesapeake",
    "Invesco DB", "Indexperts", "Parametric", "Fitzgerald", "Ocean Park", "Formidable",
    "FlexShares", "TrueShares", "RiverFront", "StockSnips", "Subversive", "Main Funds",
    "DoubleLine", "Brookstone", "BufferLABS", "BNY Mellon", "Reverb ETF", "WBI Shares",
    "RiverNorth", "Vegashares", "Pathfinder", "REX-Osprey", "WisdomTree", "Touchstone",
    "REX Shares", "Guggenheim", "Tidal ETFs", "Distillate", "Kensington", "MarketDesk",
    "Angel Oak", "Xtrackers", "Crossmark", "Quadratic", "Sparkline", "CastleArk",
    "Unlimited", "Arimathea", "Even Herd", "Bridgeway", "Thornburg", "SMI Funds",
    "Arlington", "Yorkville", "Fundstrat", "Bluemonte", "SmartETFs", "Honeytree",
    "Concourse", "ADRhedged", "Oak Funds", "Castellan", "BondBloxx", "Fundsmith",
    "Roundhill", "AMG Funds", "Innovator", "ProShares", "Day Hagan", "M.D. Sass",
    "Euclidean", "NestYield", "Kingsbarn", "TappAlpha", "Oneascent", "Altshares",
    "Grayscale", "WHITEWOLF", "Strategas", "Allspring", "Rainwater", "BlackRock",
    "US Global", "Macquarie", "TradersAI", "Principal", "Breakwave", "Templeton",
    "Tremblant", "Congress", "Moonvest", "Tortoise", "Ritholtz", "Franklin",
    "Simplify", "21Shares", "Defiance", "PlanRock", "Vontobel", "Fairlead", "Nicholas",
    "Columbia", "Teucrium", "Direxion", "Pinnacle", "Matthews", "AB Funds", "Adaptive",
    "Eventide", "YieldMax", "Reckoner", "Suncoast", "Fidelity", "CresAlta", "Leuthold",
    "Aberdeen", "Hennessy", "Global X", "JPMorgan", "InfraCap", "Longview", "Twin Oak",
    "ChinaAMC", "SP Funds", "Defender", "Absolute", "Milliman", "Panagram", "Thrivent",
    "Westwood", "Barclays", "Hartford", "Bancreek", "Meridian", "Optimize", "X-Square",
    "Peerless", "Affinity", "Rayliant", "Horizons", "aberdeen", "NovaTide", "Vanguard",
    "Frontier", "Cultivar", "ERShares", "Hedgeye", "Humilis", "Natixis", "Allianz",
    "Founder", "Calvert", "Coastal", "Anfield", "Anydrus", "Freedom", "BeeHive",
    "Keating", "Emerald", "Horizon", "Fortuna", "Bushido", "Avantis", "Wedbush",
    "Onefund", "Inspire", "Hashdex", "Brandes", "Procure", "Invesco", "Bastion",
    "Astoria", "Amplius", "Adaptiv", "Adasina", "Equable", "Alerian", "Amplify",
    "iShares", "Altrius", "Academy", "Monarch", "Grizzle", "Ballast", "Gadsden",
    "Timothy", "Bitwise", "Stacked", "Madison", "Sapient", "Calamos", "Man GLG",
    "Cambria", "Oakmark", "Gabelli", "FT Vest", "Bridges", "Acuitas", "Beyond",
    "Hilton", "ETRACS", "Wisdom", "Manzil", "Harbor", "Gotham", "Canary", "Pabrai",
    "WarCap", "Praxis", "Pictet", "Advent", "Clough", "Jensen", "Alexis", "Skylar",
    "Nelson", "Virtus", "Schwab", "Lazard", "Sophus", "Nuveen", "Burney", "Strive",
    "Warren", "Pareto", "Cullen", "Sprott", "Osprey", "Miller", "Cabana", "River1",
    "Abacus", "CORE16", "Nomura", "VanEck", "Kovitz", "Aztlan", "Themes", "Langar",
    "Tuttle", "Putnam", "Argent", "Dakota", "Vident", "Select", "Matrix", "Scharf",
    "iPath", "Oasis", "Alger", "LOGIQ", "Polen", "QRAFT", "India", "Armor", "Davis",
    "Draco", "Alpha", "Zacks", "3Edge", "Build", "Baron", "Ionic", "Aptus", "PIMCO",
    "Range", "Pzena", "Towle", "Amana", "Logan", "Eagle", "JLens", "Impax", "Tradr",
    "COtwo", "FundX", "T-Rex", "Regan", "abrdn", "Mango", "Spear", "Wahed", "Smart",
    "Weitz", "Atlas", "Pacer", "Toews", "xETFs", "Corgi", "Swan", "Akre", "iMGP",
    "FINQ", "ALPS", "Dana", "ATAC", "USCF", "Neos", "FMQQ", "Tema", "Avos", "PGIM",
    "NETL", "THOR", "MKAM", "Vert", "EMQQ", "Voya", "OPAL", "Obra", "Guru", "SPDR",
    "SoFi", "Alki", "RPAR", "Aura", "Arin", "MRBL", "CoRe", "Hoya", "ETFB", "MUFG",
    "Saba", "Kurv", "Peak", "Hull", "PLUS", "WEBs", "ZEGA", "DAC", "IDX", "F/m", "L&G",
    "RAM", "ETC", "UVA", "NPF", "ARS", "Max", "Elm", "HCM", "CLS", "CCM", "AAM", "GSR",
    "DFA", "ACV", "DWS", "MFS", "GMO", "SEI", "SWP", "UBS", "AGF", "STF", "TCW", "ARK",
    "Man", "PMV", "REX", "GGM", "DGA", "BBH", "SRH", "AOT", "ROC", "Ruk", "FPA", "CRM",
    "LSV", "OTG", "Q3", "DB", "PL", "iM", "FM", "MC",
    ],
    key=len,
    reverse=True,
)

BRAND_ALIASES = {
    "ft vest": "First Trust",
    "rex shares": "REX Shares",
    "rex microsectors": "REX Shares",
    "rex-osprey": "REX Shares",
    "rex": "REX Shares",
    "tuttle": "Tuttle Capital",
    "tuttle capital": "Tuttle Capital",
    "aberdeen": "abrdn",
    "us commodity funds": "USCF Advisers",
    "uscf": "USCF Advisers",
    "uscf advisers": "USCF Advisers",
    "franklin": "Franklin Templeton",
    "franklin templeton": "Franklin Templeton",
    "fidelity": "Fidelity",
    "fidelity advisor": "Fidelity",
}


def normalize_brand(name: str) -> str:
    return BRAND_ALIASES.get(name.strip().lower(), name)


@dataclass
class FacingSheetResult:
    basis_type: str | None
    designated_date: str | None
    confidence: str


@dataclass
class IssuerResolution:
    issuer: str | None
    trust: str
    confidence: str
    method: str


def parse_facing_sheet_basis(text: str) -> FacingSheetResult:
    window = text
    anchor = window.find("proposed public filing")
    if anchor != -1:
        window = window[anchor : anchor + 2000]

    for label, basis_type in FACING_SHEET_OPTIONS:
        idx = window.lower().find(label.lower())
        if idx == -1:
            continue
        preceding = window[max(0, idx - 15) : idx]
        if any(marker in preceding for marker in CHECKED_MARKERS):
            designated_date = None
            if basis_type.endswith("-date"):
                m = EXPLICIT_DATE_RE.search(window[idx : idx + 120])
                designated_date = m.group(1) if m else None
            return FacingSheetResult(basis_type, designated_date, "checkbox_detected")

    return FacingSheetResult(None, None, "needs_review")


def _clean_adviser_name(name: str) -> str:
    return name.strip(" \"'()").rstrip(",")


def parse_fund_sponsor(text: str) -> str | None:
    section = SPONSOR_SECTION_RE.search(text)
    if not section:
        return None
    window = text[section.end() : section.end() + 500]
    m = SPONSOR_NAME_RE.search(window)
    if not m:
        return None
    candidate = _clean_adviser_name(m.group(1))
    return candidate if len(candidate) >= 2 else None


def _strip_leading_heading_words(name: str) -> str:
    words = name.split()
    if all(w.strip(",.").isupper() for w in words):
        return name
    while words and words[0].strip(",.").isupper() and len(words) > 1:
        words.pop(0)
    return " ".join(words)


ADVISER_LABEL_PREFIX_RE = re.compile(r"^(?:investment\s+advisers?|advisers?)\s+", re.IGNORECASE)


def _strip_leading_adviser_label(name: str) -> str:
    while True:
        stripped = ADVISER_LABEL_PREFIX_RE.sub("", name)
        if stripped == name:
            return name
        name = stripped


def _is_valid_adviser_candidate(name: str) -> bool:
    lower = name.lower()
    if lower in ADVISER_REJECT_TERMS:
        return False
    words = set(re.findall(r"[a-z]+", lower))
    if words & ADVISER_REJECT_WORDS:
        return False
    return True


def parse_adviser(text: str) -> str | None:
    for anchor in ADVISER_ANCHOR_RE.finditer(text):
        preceding = text[: anchor.start()].rstrip()
        last_period = preceding.rfind(". ")
        window = preceding[last_period + 2 :] if last_period != -1 else preceding
        m = ADVISER_NAME_BEFORE_RE.search(window)
        if not m:
            continue
        candidate = _clean_adviser_name(m.group(1))
        candidate = _strip_leading_heading_words(candidate)
        candidate = _strip_leading_adviser_label(candidate)
        if len(candidate) >= 3 and _is_valid_adviser_candidate(candidate):
            return candidate

    m = ADVISER_LABEL_RE.search(text)
    if m:
        candidate = _clean_adviser_name(m.group(1))
        candidate = _strip_leading_adviser_label(candidate)
        if len(candidate) >= 3 and _is_valid_adviser_candidate(candidate):
            return candidate

    return None


def brand_from_fund_name(fund_name: str) -> str | None:
    lower = fund_name.lower()
    for brand in KNOWN_ISSUER_BRANDS:
        b = brand.lower()
        if not lower.startswith(b):
            continue
        boundary_char = fund_name[len(brand) : len(brand) + 1]
        if boundary_char == "" or not boundary_char.isalnum():
            return normalize_brand(brand)
    words = fund_name.split()
    return words[0] if words else None


def resolve_issuer(
    registrant_name: str,
    adviser: str | None,
    sponsor: str | None = None,
    fund_name: str | None = None,
) -> IssuerResolution:
    trust = registrant_name

    if sponsor:
        return IssuerResolution(
            issuer=normalize_brand(sponsor), trust=trust, confidence="high",
            method="fund_sponsor",
        )

    if adviser and adviser.strip().lower() not in KNOWN_SHELL_ADVISERS:
        return IssuerResolution(
            issuer=normalize_brand(adviser), trust=trust, confidence="high",
            method="adviser_field",
        )

    if fund_name:
        guessed = brand_from_fund_name(fund_name)
        if guessed:
            return IssuerResolution(
                issuer=guessed, trust=trust, confidence="alias", method="fund_name_heuristic"
            )

    if trust.strip().lower() in SHARED_TRUST_PLATFORMS:
        return IssuerResolution(
            issuer=None, trust=trust, confidence="low", method="shared_trust_no_signal"
        )

    guessed = trust.replace(" ETF Trust", "").replace(" Trust", "").strip()
    return IssuerResolution(
        issuer=guessed, trust=trust, confidence="alias", method="registrant_name_fallback"
    )


CATEGORY_KEYWORDS = {
    "Leveraged": ["2x", "3x", "daily target", "bull", "leveraged"],
    "Single-Stock": ["daily target", "single stock", "individual stock"],
    "Derivative Income": ["option income", "covered call", "buywrite"],
    "Defined Outcome": ["buffer", "target outcome", "defined outcome"],
    "Biotech": ["biotech", "biotechnology", "drug discovery"],
    "Pharmaceuticals": ["pharmaceutical", "pharma"],
    "Broad Infrastructure": ["infrastructure"],
    "Broad Industrials": ["industrial", "reindustrialization"],
    "Homebuilders": ["homebuilder", "homebuilders"],
    "Crypto-Adjacent": ["bitcoin", "crypto", "digital asset"],
}


def tag_categories(fund_name: str) -> list[str]:
    lower = fund_name.lower()
    return [tag for tag, kws in CATEGORY_KEYWORDS.items() if any(k in lower for k in kws)]
