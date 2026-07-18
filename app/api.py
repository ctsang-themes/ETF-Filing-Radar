"""
FastAPI service: GET /scrape?start=YYYY-MM-DD&end=YYYY-MM-DD
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from . import edgar_client, parser

app = FastAPI(title="ETF Filing Radar API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOW_ORIGINS", "*").split(","),
    allow_methods=["GET"],
    allow_headers=["*"],
)

CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "3600"))
MAX_DOCS_PER_REQUEST = int(os.environ.get("MAX_DOCS_PER_REQUEST", "60"))

# The EDGAR form type tells us what kind of event a filing is, so a date
# extension (485BXT) doesn't masquerade as a brand-new launch.
_STATUS_BY_FORM = {
    "N-1A": "Initial registration",
    "485APOS": "Newly filed",
    "485BPOS": "Effective",
    "485BXT": "Effective date set",
}
_cache: dict[str, tuple[float, list[dict]]] = {}


@app.get("/health")
def health():
    return {"ok": True, "user_agent_configured": bool(os.environ.get("SEC_USER_AGENT"))}


def _build_record(
    row: edgar_client.IndexRow,
    text: str | None,
    doc_url: str,
    series: "edgar_client.SeriesInfo | None" = None,
) -> dict:
    adviser = parser.parse_adviser(text) if text else None
    sponsor = parser.parse_fund_sponsor(text) if text else None

    # Fund identity comes from the structured Series/Class header when present
    # (exact name + ticker), otherwise from the prose '(the "Fund")' convention,
    # otherwise the registrant/Trust name as a last resort.
    if series is not None:
        display_fund_name = series.name
        ticker = series.tickers[0] if series.tickers else None
        fund_name_for_resolution = series.name
        fund_name_source = "series_header"
    else:
        extracted_fund_name = parser.parse_fund_name(text) if text else None
        display_fund_name = extracted_fund_name or row.company_name
        ticker = None
        fund_name_for_resolution = row.company_name
        fund_name_source = "prose" if extracted_fund_name else "registrant_fallback"

    resolution = parser.resolve_issuer(
        row.company_name, adviser, sponsor, fund_name_for_resolution
    )
    facing = parser.parse_facing_sheet_basis(text) if text else parser.FacingSheetResult(
        None, None, "needs_review"
    )

    filed = row.date_filed
    basis_type = facing.basis_type
    designated_date = facing.designated_date
    effective_date = None
    basis_confidence = facing.confidence

    if basis_type is None:
        basis = {"type": "unresolved", "days": None}
    elif basis_type == "485b-immediate":
        # 485(b) is effective immediately on filing -- this is a genuinely
        # known effective date, so it's the one case we report as confirmed.
        basis = {"type": basis_type}
        effective_date = filed
    elif basis_type.endswith("-date"):
        # An accelerated/designated date the filer *requested*; not yet
        # confirmed. Pass it through basis and let the frontend tag it
        # 'requested' -- do NOT set effectiveDate (that means 'confirmed').
        iso_designated = designated_date
        if designated_date:
            try:
                iso_designated = datetime.strptime(
                    designated_date, "%B %d, %Y"
                ).date().isoformat()
            except ValueError:
                iso_designated = designated_date  # leave as-is if unparseable
        basis = {"type": basis_type, "designatedDate": iso_designated}
    else:
        # 485(a) 60/75-day clock: the fund auto-goes-effective on that day
        # absent SEC action. It's a projection, not a confirmed date, so we
        # pass the day count and let the frontend compute + tag it 'estimated'
        # rather than sending it as a confirmed effectiveDate.
        days = parser.DAYS_BY_BASIS.get(basis_type)
        basis = {"type": basis_type, "days": days}

    # Fall back to the form type -- which comes reliably from the index, no
    # parsing -- when the facing-sheet checkbox couldn't be read. The form type
    # itself is a strong effective-date signal:
    #   485BPOS  : post-effective amendment, effective on filing (immediate)
    #   485BXT   : designates/extends an effective date stated in prose
    if basis_type is None:
        form = row.form_type.upper()
        if form == "485BPOS":
            basis_type = "485b-immediate"
            effective_date = filed
            basis = {"type": basis_type}
            basis_confidence = "form_type_default"
        elif form == "485BXT":
            iso = parser.parse_designated_effective_date(text) if text else None
            if iso:
                basis_type = "485b-date"
                basis = {"type": basis_type, "designatedDate": iso}
                designated_date = iso
                basis_confidence = "form_type_default"
            else:
                # Known to be a date designation, but the date couldn't be read.
                basis = {"type": "unresolved", "days": None}

    if resolution.method == "fund_sponsor":
        resolved_via_parts = [
            f"Fund Sponsor section names '{sponsor}' as the brand sponsor "
            f"(distinct from the Adviser of record"
            f"{', ' + adviser if adviser else ''})."
        ]
    elif resolution.method == "adviser_field":
        resolved_via_parts = [f"Adviser field found: '{adviser}'."]
    elif resolution.method == "fund_name_heuristic":
        resolved_via_parts = [
            f"Neither Fund Sponsor nor a usable Adviser field found (adviser of record "
            f"was a known shell/back-office entity); guessed '{resolution.issuer}' from "
            f"the fund's own name -- treat as lower-confidence, not a document fact."
        ]
    elif resolution.method == "shared_trust_no_signal":
        resolved_via_parts = [
            "Neither a Fund Sponsor section nor an Adviser field could be found, "
            "and the registrant is a known shared-trust platform; issuer left "
            "unresolved rather than guessed."
        ]
    else:
        resolved_via_parts = [
            f"Neither Fund Sponsor nor Adviser field found; guessed '{resolution.issuer}' "
            "from the registrant name -- treat as low-confidence."
        ]
    resolved_via_parts.append(f"Facing sheet basis: {basis_confidence}.")
    if fund_name_source == "series_header":
        status_note = {
            "new": "newly registered series in this filing",
            "existing": "existing series being amended",
            "merger": "series involved in a merger",
        }.get(series.status, "series listed in this filing")
        resolved_via_parts.append(
            f"Fund name/ticker taken from the filing's structured Series/Class "
            f"header ({status_note})."
        )
    elif fund_name_source == "registrant_fallback":
        resolved_via_parts.append(
            "No structured Series/Class header and no "
            '\'(the "Fund")\' match in the document text -- showing the '
            "registrant/Trust name instead of the specific fund."
        )

    return {
        "filed": filed,
        "status": _STATUS_BY_FORM.get(row.form_type.upper(), "Newly filed"),
        "fund": display_fund_name,
        "ticker": ticker,
        "seriesId": series.series_id if series is not None else None,
        "seriesStatus": series.status if series is not None else None,
        "issuer": resolution.issuer,
        "trust": resolution.trust,
        "confidence": resolution.confidence,
        "basis": basis,
        "effectiveDate": effective_date,
        "resolvedVia": " ".join(resolved_via_parts),
        "tags": parser.tag_categories(display_fund_name),
        "filingUrl": doc_url,
        "form_type": row.form_type,
        "cik": row.cik,
    }


def _build_records(
    row: edgar_client.IndexRow,
    text: str | None,
    doc_url: str,
    series_list: "list[edgar_client.SeriesInfo]",
) -> list[dict]:
    """One record per fund when the structured header lists series; else one."""
    if series_list:
        return [_build_record(row, text, doc_url, s) for s in series_list]
    return [_build_record(row, text, doc_url, None)]


async def _scrape(start: date, end: date) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True) as client_async:
        def _run():
            with httpx.Client(follow_redirects=True) as client:
                rows = edgar_client.discover_filings(start, end, client)
                rows = rows[:MAX_DOCS_PER_REQUEST]
                records = []
                for row in rows:
                    try:
                        raw = edgar_client.fetch_submission(row.index_url, client)
                        text = edgar_client.clean_submission_text(raw)
                        doc_url = edgar_client.primary_document_url(row, raw) or row.index_url
                        series_list = edgar_client.parse_series_classes(raw)
                    except Exception:
                        text = None
                        doc_url = row.index_url
                        series_list = []
                    records.extend(_build_records(row, text, doc_url, series_list))
                return records

        return await asyncio.to_thread(_run)


@app.get("/scrape")
async def scrape(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "start/end must be YYYY-MM-DD")

    if end_d < start_d:
        raise HTTPException(400, "end must be on or after start")
    if (end_d - start_d).days > 365:
        raise HTTPException(400, "max range per request is 365 days -- split into smaller calls")

    cache_key = f"{start}:{end}"
    cached = _cache.get(cache_key)
    if cached and (time.time() - cached[0]) < CACHE_TTL:
        return {"filings": cached[1], "from_cache": True}

    records = await _scrape(start_d, end_d)
    _cache[cache_key] = (time.time(), records)
    return {"filings": records, "from_cache": False}
