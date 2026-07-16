"""
Extraction logic for the two things that actually trip this project up:

1. Which effective-date box is checked on the Rule 485 facing sheet.
2. Who the real issuer is, as distinct from the registrant Trust name.

Both are regex/heuristic based against the flattened document text from
edgar_client.fetch_document_text(). Checkbox rendering on EDGAR is
inconsistent across filers and years (Unicode ballot boxes, bracketed
X's, Wingdings-mapped glyphs), so this is deliberately conservative:
when it can't tell, it returns None / "needs_review" rather than guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHECKED_MARKERS = ("\u2612", "[X]", "[x]", "(X)")  # ballot-box-with-x, bracket forms

# The six facing-sheet options, in the order Rule 485 lists them, each
# with the basis code we report downstream.
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

ADVISER_FIELD_RE = re.compile(
    r"Investment Adviser[s]?\s*[:\-]?\s*([A-Z][A-Za-z0-9&.,'\-\s]{2,80}?)"
    r"(?=\s*(?:Sub-[Aa]dviser|Distributor|Administrator|Custodian|\.|,\s*(?:LLC|LP|Inc)\b[.,]|$))",
    re.IGNORECASE,
)

# Known shared, multi-brand trust platforms. When the adviser field can't
# be found, registrant name alone is NOT trustworthy for these -- fall
# back to "needs_review" rather than guessing a brand.
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
    confidence: str  # "checkbox_detected" | "needs_review"


@dataclass
class IssuerResolution:
    issuer: str | None
    trust: str
    confidence: str  # "high" | "alias" | "low"
    method: str


def parse_facing_sheet_basis(text: str) -> FacingSheetResult:
    """Find which of the six Rule 485 checkboxes is marked."""
    window = text
    # Narrow to the facing sheet region if we can find the anchor phrase --
    # keeps false positives from prospectus body text mentioning "485(a)"
    # elsewhere.
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


def parse_adviser(text: str) -> str | None:
    m = ADVISER_FIELD_RE.search(text)
    if not m:
        return None
    candidate = m.group(1).strip().rstrip(".,")
    if len(candidate) < 3:
        return None
    return candidate


def resolve_issuer(registrant_name: str, adviser: str | None) -> IssuerResolution:
    """Never treat registrant/Trust name as issuer by default -- only when
    the adviser field independently confirms it, or as a flagged fallback
    on non-shared trusts."""
    trust = registrant_name

    if adviser:
        return IssuerResolution(
            issuer=adviser, trust=trust, confidence="high", method="adviser_field"
        )

    if trust.strip().lower() in SHARED_TRUST_PLATFORMS:
        # Can't safely guess a brand for a shared platform with no adviser
        # field found -- this is exactly the Defiance/Tidal mistake to
        # avoid repeating.
        return IssuerResolution(
            issuer=None, trust=trust, confidence="low", method="shared_trust_no_adviser"
        )

    # Self-filed-looking trust name (e.g. "Example ETF Trust") with no
    # adviser field found on this pass -- fall back to the registrant
    # name as a low-confidence guess, clearly flagged, not asserted.
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
