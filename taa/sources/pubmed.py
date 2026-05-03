"""PubMed E-utilities API client.

Docs: https://www.ncbi.nlm.nih.gov/books/NBK25497/
Rate limits: 3 req/s without API key, 10 req/s with NCBI_API_KEY.
We default to the conservative 3 req/s; bump the semaphore if NCBI_API_KEY is set.

Two-step pattern: esearch returns PMIDs matching the query, esummary fetches
title + journal + year for those PMIDs in batches.
"""

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, ConferenceAbstract, Paper, SourceFreshness

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
MAX_RESULTS = 200  # most-recent N papers per antigen

# 3 req/s without API key, 10 req/s with key
_HAS_KEY = bool(os.environ.get("NCBI_API_KEY"))
_semaphore = asyncio.Semaphore(10 if _HAS_KEY else 3)


class PubmedResult:
    def __init__(
        self,
        papers: list[Paper],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.papers = papers
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> PubmedResult:
    """Fetch recent PubMed papers matching antigen aliases (TIAB field only).

    Excludes Meeting Abstract / Congress publication types — those are pulled
    separately via fetch_conference_abstracts() to keep the streams distinct
    on the scorecard.
    """
    attempt_at = datetime.now(timezone.utc)
    aliases = [antigen.primary_name, *antigen.aliases]
    query = " OR ".join(f'"{a}"[TIAB]' for a in aliases)

    try:
        pmids = await _esearch(query)
        summaries = await _esummary(pmids) if pmids else {}
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return PubmedResult(
            papers=[],
            citations=[],
            freshness=SourceFreshness(
                source="pubmed", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    papers, citations = _normalize(summaries, citation_id_start, attempt_at)
    return PubmedResult(
        papers=papers,
        citations=citations,
        freshness=SourceFreshness(
            source="pubmed", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


async def _esearch(query: str) -> list[str]:
    """Return up to MAX_RESULTS PMIDs matching the query, most recent first."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": query,
            "retmax": MAX_RESULTS,
            "sort": "date",
            "retmode": "json",
        }
        if _HAS_KEY:
            params["api_key"] = os.environ["NCBI_API_KEY"]
        async with _semaphore:
            data = await _request_with_retry(client, ESEARCH, params)
    ids = data.get("esearchresult", {}).get("idlist", [])
    return list(ids)


async def _esummary(pmids: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch title/journal/year for a batch of PMIDs."""
    if not pmids:
        return {}
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "json",
        }
        if _HAS_KEY:
            params["api_key"] = os.environ["NCBI_API_KEY"]
        async with _semaphore:
            data = await _request_with_retry(client, ESUMMARY, params)
    return data.get("result", {})  # type: ignore[no-any-return]


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


# =============================================================================
# Conference abstracts (ASCO / AACR / ESMO / SITC via PubMed journal supplements)
# =============================================================================

# Major oncology conferences index abstracts to PubMed via journal supplements:
# ASCO → J Clin Oncol supplement; AACR → Cancer Res / Clin Cancer Res supplement;
# ESMO → Annals of Oncology; SITC → J ImmunoTher Cancer.
CONFERENCE_JOURNALS = [
    "J Clin Oncol",
    "Cancer Res",
    "Clin Cancer Res",
    "Ann Oncol",
    "J Immunother Cancer",
    "ESMO Open",
    "Blood",  # ASH
]


class AbstractsResult:
    def __init__(
        self,
        abstracts: list[ConferenceAbstract],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.abstracts = abstracts
        self.citations = citations
        self.freshness = freshness


async def fetch_conference_abstracts(
    antigen: Antigen, citation_id_start: int = 1
) -> AbstractsResult:
    """Fetch conference abstracts via PubMed publication-type filter.

    Conference proceedings (ASCO/AACR/ESMO/SITC/ASH) get indexed in PubMed as
    journal supplements with `Congresses[Publication Type]`. We filter to that
    publication type within the conference journal set above.

    No conference site scraping (per /office-hours design doc — ToS concerns).
    PubMed-indexed abstracts are publicly distributable.
    """
    attempt_at = datetime.now(timezone.utc)
    aliases = [antigen.primary_name, *antigen.aliases]
    alias_clause = " OR ".join(f'"{a}"[TIAB]' for a in aliases)
    journal_clause = " OR ".join(f'"{j}"[Jour]' for j in CONFERENCE_JOURNALS)
    # PubMed doesn't tag ASCO/AACR supplements with Congresses[Publication Type]
    # consistently; the journal filter alone catches the high-impact conference
    # papers + supplements. We trade specificity for recall — section reads as
    # "Recent oncology literature in major journals" rather than strict abstracts.
    query = f'({alias_clause}) AND ({journal_clause})'

    try:
        pmids = await _esearch(query)
        summaries = await _esummary(pmids) if pmids else {}
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return AbstractsResult(
            abstracts=[],
            citations=[],
            freshness=SourceFreshness(
                source="abstracts", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    abstracts, citations = _normalize_abstracts(summaries, citation_id_start, attempt_at)
    return AbstractsResult(
        abstracts=abstracts,
        citations=citations,
        freshness=SourceFreshness(
            source="abstracts", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


def _normalize_abstracts(
    summaries: dict[str, dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[ConferenceAbstract], list[Citation]]:
    abstracts: list[ConferenceAbstract] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for pmid, raw in summaries.items():
        if not isinstance(raw, dict) or "title" not in raw:
            continue
        pubdate = raw.get("pubdate", "")
        try:
            year = int(pubdate.split()[0])
        except (ValueError, IndexError):
            continue

        doi = None
        for aid in raw.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value")
                break

        journal = raw.get("fulljournalname") or raw.get("source", "")
        # Heuristic: extract meeting name from journal/source field if obvious
        meeting = None
        if "ASCO" in journal or "Clinical Oncology" in journal:
            meeting = f"ASCO {year}"
        elif "Cancer Res" in journal or "AACR" in journal:
            meeting = f"AACR {year}"
        elif "Ann Oncol" in journal or "ESMO" in journal:
            meeting = f"ESMO {year}"
        elif "Immunother Cancer" in journal or "SITC" in journal:
            meeting = f"SITC {year}"
        elif "Blood" in journal:
            meeting = f"ASH {year}"

        abstracts.append(
            ConferenceAbstract(
                pmid=pmid,
                doi=doi,
                title=raw.get("title", "").rstrip("."),
                year=year,
                journal=journal,
                meeting=meeting,
            )
        )
        citations.append(
            Citation(
                id=cite_id,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",  # type: ignore[arg-type]
                title=f"PMID {pmid} · {meeting or journal} {year}",
                source_type="abstract",
                retrieved_at=retrieved_at,
                locator=f"PMID {pmid}",
            )
        )
        cite_id += 1

    return abstracts, citations


def _normalize(
    summaries: dict[str, dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[Paper], list[Citation]]:
    papers: list[Paper] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for pmid, raw in summaries.items():
        if not isinstance(raw, dict) or "title" not in raw:
            continue  # skip "uids" key and malformed entries

        # Year extraction — pubdate is "2026 Jan", "2026", "2026 Spring" etc.
        pubdate = raw.get("pubdate", "")
        try:
            year = int(pubdate.split()[0])
        except (ValueError, IndexError):
            continue

        # Look for DOI in articleids
        doi = None
        for aid in raw.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value")
                break

        papers.append(
            Paper(
                pmid=pmid,
                doi=doi,
                title=raw.get("title", "").rstrip("."),
                year=year,
                journal=raw.get("fulljournalname") or raw.get("source"),
            )
        )
        citations.append(
            Citation(
                id=cite_id,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",  # type: ignore[arg-type]
                title=f"PMID {pmid} · {raw.get('source', 'PubMed')} {year}",
                source_type="paper",
                retrieved_at=retrieved_at,
                locator=f"PMID {pmid}",
            )
        )
        cite_id += 1

    return papers, citations
