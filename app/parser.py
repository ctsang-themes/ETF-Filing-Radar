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
    r"([A-Z][A-Za-z0-9&.,'\-]*(?:\s+[A-Z(][A-Za-z0-9&.,'\")\-]*)*)\s*$"
)

ADVISER_LABEL_RE = re.compile(
    r"(?:(?i:Investment Adviser[s]?))\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,'\-\s]{2,80}?)"
    r"(?=\s*(?:Sub-[Aa]dviser|Distributor|Administrator|Custodian|\.|,\s*(?:LLC|LP|Inc)\b[.,]|$))"
)

# On white-label platforms (Tidal Trust II, etc.), the "Adviser" of record
# is often just a technical/compliance entity shared across many unrelated
# brands -- the actual brand name is disclosed separately under a
# "FUND SPONSOR" heading. Self-advised funds have no such section at all,
# so trying this first is harmless for them; it simply won't match and
# falls through to the adviser field. Confirmed against a live filing.
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


def parse_adviser(text: str) -> str | None:
    anchor = ADVISER_ANCHOR_RE.search(text)
    if anchor:
        preceding = text[: anchor.start()].rstrip()
        last_period = preceding.rfind(". ")
        window = preceding[last_period + 2 :] if last_period != -1 else preceding
        m = ADVISER_NAME_BEFORE_RE.search(window)
        if m:
            candidate = _clean_adviser_name(m.group(1))
            if len(candidate) >= 3:
                return candidate

    m = ADVISER_LABEL_RE.search(text)
    if m:
        candidate = _clean_adviser_name(m.group(1))
        if len(candidate) >= 3:
            return candidate

    return None


def resolve_issuer(
    registrant_name: str, adviser: str | None, sponsor: str | None = None
) -> IssuerResolution:
    """Priority: Fund Sponsor (real brand on white-label platforms) >
    Adviser field (correct on its own for self-advised trusts) > flagged
    fallback for known shared-trust platforms with neither."""
    trust = registrant_name

    if sponsor:
        return IssuerResolution(
            issuer=sponsor, trust=trust, confidence="high", method="fund_sponsor"
        )

    if adviser:
        return IssuerResolution(
            issuer=adviser, trust=trust, confidence="high", method="adviser_field"
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
