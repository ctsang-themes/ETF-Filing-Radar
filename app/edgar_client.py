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

import html

import httpx

USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
if not USER_AGENT:
    raise RuntimeError(
        "SEC_USER_AGENT must be set to a real 'Name contact@email.com' string. "
        "SEC will reject (and can block) requests without one."
    )

BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
FULL_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"

ETF_FORMS = ("N-1A", "485APOS", "485BPOS", "485BXT")

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
    filename: str

    @property
    def index_url(self) -> str:
        return f"https://www.sec.gov/Archives/{self.filename}"


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
    header_idx = None
    dash_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Form Type"):
            header_idx = i
        elif header_idx is not None and line.strip() and set(line.strip()) <= {"-"}:
            dash_idx = i
            break

    if header_idx is None or dash_idx is None:
        raise RuntimeError(
            "Could not find the 'Form Type' header / dashed separator line "
            "in form.idx -- SEC may have changed the file format."
        )

    for line in lines[dash_idx + 1 :]:
        if not line.strip():
            continue
        fields = re.split(r"\s{2,}", line.strip())
        if len(fields) != 5:
            continue
        form_type, company_name, cik, date_filed, filename = fields
        if form_type in ETF_FORMS:
            rows.append(IndexRow(form_type, company_name, cik, date_filed, filename))
    return rows


NON_ETF_REGISTRANT_KEYWORDS = (
    "insurance",
    "life insurance",
    "variable account",
    "variable annuity",
    "separate account",
    "annuity",
)


def _looks_like_etf_registrant(company_name: str) -> bool:
    lower = company_name.lower()
    return not any(kw in lower for kw in NON_ETF_REGISTRANT_KEYWORDS)


def discover_filings(start: date, end: date, client: httpx.Client) -> list[IndexRow]:
    """Pull every ETF-relevant filing in [start, end] across the needed quarters."""
    all_rows: list[IndexRow] = []
    for year, quarter in _quarters_between(start, end):
        rows = fetch_full_index(year, quarter, client)
        for r in rows:
            if start.isoformat() <= r.date_filed <= end.isoformat() and _looks_like_etf_registrant(
                r.company_name
            ):
                all_rows.append(r)
    return all_rows


def fetch_filing_index_page(index_url: str, client: httpx.Client) -> str:
    _throttle()
    resp = client.get(index_url, headers=BASE_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_primary_document_url(index_page_html: str, index_url: str) -> str | None:
    hrefs = re.findall(r'href="([^"]+\.htm[l]?)"', index_page_html, re.IGNORECASE)
    base = index_url.rsplit("/", 1)[0]
    candidates = [h for h in hrefs if "index" not in h.lower()]
    if not candidates:
        return None
    doc = candidates[0]
    if doc.startswith("http"):
        return doc
    return f"{base}/{doc.lstrip('/')}"


CHECKBOX_TAG_RE = re.compile(r'<input\b[^>]*type=["\']?checkbox["\']?[^>]*>', re.IGNORECASE)

# SGML document blocks in a full-submission .txt. Each real document is
# wrapped in <DOCUMENT>...<TYPE>FORM<SEQUENCE>N<FILENAME>name.htm...
_SUBMISSION_DOC_RE = re.compile(
    r"<DOCUMENT>\s*"
    r"<TYPE>(?P<type>[^\s<]+).*?"
    r"<FILENAME>(?P<filename>[^\s<]+)",
    re.IGNORECASE | re.DOTALL,
)
# Accession number embedded in the submission filename path, e.g.
# edgar/data/1976322/0001829126-26-007646.txt
_ACCESSION_RE = re.compile(r"(\d{10}-\d{2}-\d{6})\.txt$")


def fetch_submission(url: str, client: httpx.Client) -> str:
    """Fetch a raw full-submission .txt (or primary doc) without cleaning."""
    _throttle()
    resp = client.get(url, headers=BASE_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def primary_document_url(row: "IndexRow", raw_submission: str) -> str | None:
    """Derive the URL of the human-readable primary document.

    Given the full-submission .txt (which lists each contained document with
    its <TYPE> and <FILENAME>), pick the primary prospectus document and build
    the canonical Archives URL:

        https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{filename}
    """
    m_acc = _ACCESSION_RE.search(row.filename)
    if not m_acc:
        return None
    accession_nodash = m_acc.group(1).replace("-", "")

    blocks = [
        (m.group("type").upper(), m.group("filename").strip())
        for m in _SUBMISSION_DOC_RE.finditer(raw_submission)
    ]
    if not blocks:
        return None

    def _is_html(fn: str) -> bool:
        return fn.lower().endswith((".htm", ".html"))

    # Prefer the document whose TYPE matches the filing's form type and is HTML;
    # then any HTML document; then the first document of any kind.
    chosen = None
    for dtype, fn in blocks:
        if dtype == row.form_type.upper() and _is_html(fn):
            chosen = fn
            break
    if chosen is None:
        for _dtype, fn in blocks:
            if _is_html(fn):
                chosen = fn
                break
    if chosen is None:
        chosen = blocks[0][1]

    cik = row.cik.lstrip("0") or row.cik
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession_nodash}/{chosen}"
    )


def clean_submission_text(raw: str) -> str:
    """Strip a raw submission/document down to searchable plain text."""

    def _checkbox_to_bracket(m: "re.Match") -> str:
        is_checked = bool(re.search(r"\bchecked\b", m.group(0), re.IGNORECASE))
        return "[X]" if is_checked else "[ ]"

    raw = CHECKBOX_TAG_RE.sub(_checkbox_to_bracket, raw)

    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text


def fetch_document_text(doc_url: str, client: httpx.Client) -> str:
    """Fetch and clean a document in one step (compat wrapper)."""
    return clean_submission_text(fetch_submission(doc_url, client))


# --- Structured Series / Class (Contract) data ------------------------------
#
# Investment-company filings (485APOS/485BPOS/N-1A) carry a machine-readable
# block in the SEC header listing each *series* (i.e. each fund) by name, with
# its Series ID and per-class ticker(s). This is far more reliable than
# regexing the prospectus prose for '(the "Fund")', and it naturally yields one
# entry per fund -- so a multi-series trust filing expands into the right rows
# instead of collapsing onto the registrant/Trust name.
#
# Layout (SGML, values on the same line, no leaf close tags):
#   <SERIES-AND-CLASSES-CONTRACTS-DATA>
#     <NEW-SERIES-AND-CLASSES-CONTRACTS> | <EXISTING-...> | <MERGER-...>
#       <SERIES>
#         <SERIES-ID>S000...
#         <SERIES-NAME>Some Fund Name
#         <CLASS-CONTRACT>
#           <CLASS-CONTRACT-TICKER-SYMBOL>ABCD
#         </CLASS-CONTRACT>
#       </SERIES>

_SUBSECTION_RE = re.compile(
    r"<(NEW|EXISTING|MERGER)-SERIES-AND-CLASSES-CONTRACTS>(?P<body>.*?)"
    r"</\1-SERIES-AND-CLASSES-CONTRACTS>",
    re.IGNORECASE | re.DOTALL,
)
_SERIES_BLOCK_RE = re.compile(r"<SERIES>(?P<body>.*?)</SERIES>", re.IGNORECASE | re.DOTALL)
_SERIES_NAME_RE = re.compile(r"<SERIES-NAME>\s*([^\r\n<]+)", re.IGNORECASE)
_SERIES_ID_RE = re.compile(r"<SERIES-ID>\s*([^\r\n<]+)", re.IGNORECASE)
_TICKER_RE = re.compile(r"<CLASS-CONTRACT-TICKER-SYMBOL>\s*([^\r\n<]+)", re.IGNORECASE)


@dataclass
class SeriesInfo:
    series_id: str | None
    name: str
    tickers: list[str]
    status: str  # "new" | "existing" | "merger" | "unknown"


def _parse_series_blocks(body: str, status: str) -> list[SeriesInfo]:
    out: list[SeriesInfo] = []
    for m in _SERIES_BLOCK_RE.finditer(body):
        block = m.group("body")
        name_m = _SERIES_NAME_RE.search(block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        if not name:
            continue
        sid_m = _SERIES_ID_RE.search(block)
        tickers = []
        for t in _TICKER_RE.findall(block):
            t = t.strip()
            # ticker is often blank/placeholder at the 485APOS stage
            if t and t.lower() not in {"n/a", "none"}:
                tickers.append(t)
        out.append(
            SeriesInfo(
                series_id=sid_m.group(1).strip() if sid_m else None,
                name=name,
                tickers=tickers,
                status=status,
            )
        )
    return out


def parse_series_classes(raw_submission: str) -> list[SeriesInfo]:
    """Extract every fund (series) declared in the submission's SGML header.

    Preference order for the 'status' tag is derived from which subsection the
    series sits in. When the filing uses the subsection wrappers, series are
    tagged new/existing/merger accordingly; otherwise they're tagged 'unknown'.
    Deduplicates by series_id (falling back to name) since some filings repeat
    a series across sections.
    """
    results: list[SeriesInfo] = []
    matched_any_subsection = False
    for sm in _SUBSECTION_RE.finditer(raw_submission):
        matched_any_subsection = True
        status = sm.group(1).lower()
        results.extend(_parse_series_blocks(sm.group("body"), status))

    if not matched_any_subsection:
        # No subsection wrappers -- parse any bare <SERIES> blocks in the file.
        results.extend(_parse_series_blocks(raw_submission, "unknown"))

    seen: set[str] = set()
    deduped: list[SeriesInfo] = []
    for s in results:
        key = (s.series_id or s.name).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped
