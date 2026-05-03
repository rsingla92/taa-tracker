"""SEC EDGAR full-text search client.

Docs: https://efts.sec.gov/LATEST/search-index?q=...
Rate limit: ~10 req/s. SEC REQUIRES a User-Agent header with a contact email
(format: "Name email@domain"); requests without it are 403'd. v0.1 stops at
filing-list level — extracting field-level data from 10-K Item 1 / 8-K
narrative is v0.2 (LLM extraction layer).

We surface filings as Citations on the scorecard so a BD reader can click
through to the raw filing for context. No structured Filing → Program
linking yet — that's the v0.2 extraction step.
"""

import asyncio
import os
from datetime import date, datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, Filing, SourceFreshness

EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
MAX_RESULTS = 50  # most-recent N filings per antigen
RELEVANT_FORMS = ["10-K", "10-Q", "8-K", "S-1", "20-F"]

_semaphore = asyncio.Semaphore(8)  # below SEC's ~10 req/s


def _user_agent() -> str:
    """SEC requires `User-Agent: Name email@domain` — read from env."""
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        # Conservative default; SEC will allow this but log it. Better to set EDGAR_USER_AGENT.
        return "TAA Tracker dev@example.com"
    return ua


class EdgarResult:
    def __init__(
        self,
        filings: list[Filing],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.filings = filings
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> EdgarResult:
    """Fetch recent SEC filings mentioning any antigen alias.

    Filters to RELEVANT_FORMS (10-K, 10-Q, 8-K, S-1, 20-F) so we don't pollute
    citations with proxy filings, insider trading, etc. v0.1 returns filings
    as Citations only; field-level extraction is v0.2.
    """
    attempt_at = datetime.now(timezone.utc)
    aliases = [antigen.primary_name, *antigen.aliases]
    query = " OR ".join(f'"{a}"' for a in aliases)

    headers = {"User-Agent": _user_agent()}

    try:
        hits = await _search(query, headers)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return EdgarResult(
            filings=[],
            citations=[],
            freshness=SourceFreshness(
                source="edgar", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    filings, citations = _normalize(hits, citation_id_start, attempt_at)
    return EdgarResult(
        filings=filings,
        citations=citations,
        freshness=SourceFreshness(
            source="edgar", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


async def _search(query: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        params: dict[str, Any] = {
            "q": query,
            "forms": ",".join(RELEVANT_FORMS),
            "dateRange": "custom",
            "startdt": "2024-01-01",  # last ~2 years
            "enddt": date.today().isoformat(),
        }
        async with _semaphore:
            data = await _request_with_retry(client, EDGAR_SEARCH, params)
    hits = data.get("hits", {}).get("hits", [])
    return list(hits)[:MAX_RESULTS]


async def _request_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> dict[str, Any]:
    for attempt in range(RETRY_ATTEMPTS):
        resp = await client.get(url, params=params)
        if resp.status_code == 429:
            await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
            continue
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    raise httpx.HTTPStatusError(
        "exhausted 429 retries", request=httpx.Request("GET", url), response=httpx.Response(429)
    )


def _normalize(
    hits: list[dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[Filing], list[Citation]]:
    filings: list[Filing] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for hit in hits:
        src = hit.get("_source", {})
        accession = src.get("adsh", "").replace("-", "")
        ciks = src.get("ciks", [])
        cik = ciks[0] if ciks else ""
        form_type = src.get("form", "")
        filed_at_str = src.get("file_date")
        company_names = src.get("display_names", [])
        company = company_names[0] if company_names else ""

        if not accession or not cik or not filed_at_str:
            continue

        try:
            filed_at = date.fromisoformat(filed_at_str)
        except ValueError:
            continue

        # EDGAR document URL convention
        primary_doc = src.get("file_type", "") and src.get("xsl", "")
        accession_dashed = src.get("adsh", "")
        cik_no_zeros = cik.lstrip("0") or cik
        primary_filename = src.get("file_type", "")
        # Construct filing index URL — most reliable cross-form
        filing_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?"
            f"action=getcompany&CIK={cik_no_zeros}&type={form_type}"
            f"&dateb=&owner=include&count=40"
        )
        # Better: link directly to the filing by accession
        filing_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
            f"{accession}/{accession_dashed}-index.htm"
        )

        filings.append(
            Filing(
                accession=accession_dashed,
                cik=cik,
                company=company,
                form_type=form_type,
                filed_at=filed_at,
                filing_url=filing_url,  # type: ignore[arg-type]
            )
        )
        citations.append(
            Citation(
                id=cite_id,
                url=filing_url,  # type: ignore[arg-type]
                title=f"{form_type} · {company} · {filed_at_str}",
                source_type="filing",
                retrieved_at=retrieved_at,
                locator=f"{form_type} {accession_dashed}",
            )
        )
        cite_id += 1

    return filings, citations
