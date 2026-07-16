"""
Thin client for pulling ETF registration filings straight from SEC EDGAR.

Two data sources, used together:

1. The quarterly full-index (`form.idx`), which lists every filing of a
   given form type in a date range. This is how we discover *which*
   filings exist without needing a search query.
   https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx

2. The filing's own primary document, fetched and parsed for the facing
   sheet checkboxes and the Investment Adviser field. This is where the
   issuer/trust resolution and effective-date-basis detection happens.

SEC requires a descriptive User-Agent with a real contact on every
request (10.10.1 of the EDGAR access rules) -- set SEC_USER_AGENT before
running this against real EDGAR, or every request will be rejected.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterable

import httpx

USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
if not USER_AGENT:
    raise RuntimeError(
        "SEC_USER_AGENT must be set to a real 'Name contact@email.com' string. "
        "SEC will reject (and can block) requests without one."
    )

BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"

# Registration-related forms relevant to a new-or-amended ETF launch.
ETF_FORMS = ("N-1A", "485APOS", "485BPOS", "485BXT")

# SEC rate limit is 10 req/sec; stay well under it since we're fetching
# individual documents too.
_MIN_INTERVAL = 0.15
_last_request_ts = 0.0


def _throttle() -> None:
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request_ts = time.monotonic()


@dataclass
class IndexRow:
    form_type: str
    company_name: str
    cik: str
    date_filed: str
    filename: str  # relative path under Archives/, e.g. edgar/data/.../0000...-index.htm

    @property
    def index_url(self) -> str:
        return f"https://www.sec.gov/{self.filename}"


def _quarter_for(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _quarters_between(start: date, end: date) -> Iterable[tuple[int, int]]:
    seen = set()
    y, q = _quarter_for(start)
    while (y, q) <= _quarter_for(end):
        if (y, q) not in seen:
            seen.add((y, q))
            yield (y, q)
        q += 1
        if q > 4:
            q = 1
            y += 1


def fetch_full_index(year: int, quarter: int, client: httpx.Client) -> list[IndexRow]:
    """Download and parse one quarter's form.idx."""
    _throttle()
    url = FULL_INDEX_URL.format(year=year, q=quarter)
    resp = client.get(url, headers=BASE_HEADERS, timeout=30)
    resp.raise_for_status()

    rows: list[IndexRow] = []
    lines = resp.text.splitlines()
    started = False
    for line in lines:
        if line.startswith("Form Type"):
            started = True
            continue
        if not started or not line.strip() or line.startswith("---"):
            continue
        # Fixed-width columns: Form Type, Company Name, CIK, Date Filed, File Name
        form_type = line[0:12].strip()
        company_name = line[12:74].strip()
        cik = line[74:86].strip()
        date_filed = line[86:98].strip()
        filename = line[98:].strip()
        if form_type in ETF_FORMS:
            rows.append(IndexRow(form_type, company_name, cik, date_filed, filename))
    return rows


def discover_filings(start: date, end: date, client: httpx.Client) -> list[IndexRow]:
    """Pull every ETF-relevant filing in [start, end] across the needed quarters."""
    all_rows: list[IndexRow] = []
    for year, quarter in _quarters_between(start, end):
        rows = fetch_full_index(year, quarter, client)
        for r in rows:
            if start.isoformat() <= r.date_filed <= end.isoformat():
                all_rows.append(r)
    return all_rows


def fetch_filing_index_page(index_url: str, client: httpx.Client) -> str:
    _throttle()
    resp = client.get(index_url, headers=BASE_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_primary_document_url(index_page_html: str, index_url: str) -> str | None:
    """The index page lists all documents in the filing; grab the main one
    (usually the largest .htm that isn't the index itself)."""
    hrefs = re.findall(r'href="([^"]+\.htm[l]?)"', index_page_html, re.IGNORECASE)
    base = index_url.rsplit("/", 1)[0]
    candidates = [h for h in hrefs if "index" not in h.lower()]
    if not candidates:
        return None
    # Prefer the first non-index htm -- EDGAR lists the primary document first.
    doc = candidates[0]
    if doc.startswith("http"):
        return doc
    return f"{base}/{doc.lstrip('/')}"


def fetch_document_text(doc_url: str, client: httpx.Client) -> str:
    _throttle()
    resp = client.get(doc_url, headers=BASE_HEADERS, timeout=30)
    resp.raise_for_status()
    # Strip tags crudely -- good enough for regex-based field extraction.
    text = re.sub(r"<[^>]+>", " ", resp.text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text
