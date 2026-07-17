"""
FastAPI service: GET /scrape?start=YYYY-MM-DD&end=YYYY-MM-DD
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime, timedelta

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
_cache: dict[str, tuple[float, list[dict]]] = {}


@app.get("/health")
def health():
    return {"ok": True, "user_agent_configured": bool(os.environ.get("SEC_USER_AGENT"))}


def _build_record(
    row: edgar_client.IndexRow,
    text: str | None,
) -> dict:
    adviser = parser.parse_adviser(text) if text else None
    sponsor = parser.parse_fund_sponsor(text) if text else None
    resolution = parser.resolve_issuer(row.company_name, adviser, sponsor, row.company_name)
    facing = parser.parse_facing_sheet_basis(text) if text else parser.FacingSheetResult(
        None, None, "needs_review"
    )

    extracted_fund_name = parser.parse_fund_name(text) if text else None
    display_fund_name = extracted_fund_name or row.company_name

    filed = row.date_filed
    basis_type = facing.basis_type
    designated_date = facing.designated_date
    effective_date = None
    basis_confidence = facing.confidence

    if basis_type is None:
        basis = {"type": "unresolved", "days": None}
    elif basis_type.endswith("-date"):
        basis = {"type": basis_type, "designatedDate": designated_date}
        effective_date = designated_date
    else:
        days = parser.DAYS_BY_BASIS[basis_type]
        basis = {"type": basis_type, "days": days}
        filed_dt = datetime.strptime(filed, "%Y-%m-%d").date()
        effective_date = (filed_dt + timedelta(days=days)).isoformat()

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
    if not extracted_fund_name:
        resolved_via_parts.append(
            "Fund name not found in document text via the standard "
            '\'(the "Fund")\' convention -- showing the registrant/Trust '
            "name instead of the specific fund."
        )

    return {
        "filed": filed,
        "status": "Newly filed",
        "fund": display_fund_name,
        "ticker": None,
        "issuer": resolution.issuer,
        "trust": resolution.trust,
        "confidence": resolution.confidence,
        "basis": basis,
        "effectiveDate": effective_date,
        "resolvedVia": " ".join(resolved_via_parts),
        "tags": parser.tag_categories(display_fund_name),
        "filingUrl": row.index_url,
        "form_type": row.form_type,
        "cik": row.cik,
    }


async def _scrape(start: date, end: date) -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True) as client_async:
        def _run():
            with httpx.Client(follow_redirects=True) as client:
                rows = edgar_client.discover_filings(start, end, client)
                rows = rows[:MAX_DOCS_PER_REQUEST]
                records = []
                for row in rows:
                    try:
                        text = edgar_client.fetch_document_text(row.index_url, client)
                    except Exception:
                        text = None
                    records.append(_build_record(row, text))
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
