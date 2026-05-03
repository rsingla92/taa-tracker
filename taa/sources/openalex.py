"""OpenAlex API client.

Docs: https://docs.openalex.org/
Rate limit: 10 req/s without key; the "polite pool" gives priority if you set
a contact email via the `mailto` query param (no auth/key needed). Free.

We use OpenAlex primarily for citation counts and momentum signal — papers/year
trend and total cited-by count per matching work. Complements PubMed which
gives us the canonical bibliographic record.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, Paper, SourceFreshness

OPENALEX_API = "https://api.openalex.org/works"
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
MAX_RESULTS = 200
MAILTO = "rsingla92+taa-tracker@gmail.com"  # polite-pool contact

_semaphore = asyncio.Semaphore(8)  # below the 10 req/s limit


class OpenalexResult:
    def __init__(
        self,
        papers: list[Paper],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.papers = papers
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> OpenalexResult:
    """Fetch recent OpenAlex works matching antigen aliases (title.search filter).

    Constrains to title search (not full-text abstract) for noise control.
    Sorts by publication_date desc, takes top MAX_RESULTS.
    """
    attempt_at = datetime.now(timezone.utc)
    aliases = [antigen.primary_name, *antigen.aliases]
    # OpenAlex query syntax: title.search:"foo" OR title.search:"bar"
    search_clause = " OR ".join(f'"{a}"' for a in aliases)

    try:
        works = await _fetch(search_clause)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return OpenalexResult(
            papers=[],
            citations=[],
            freshness=SourceFreshness(
                source="openalex", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    papers, citations = _normalize(works, citation_id_start, attempt_at)
    return OpenalexResult(
        papers=papers,
        citations=citations,
        freshness=SourceFreshness(
            source="openalex", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


async def _fetch(search_clause: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        params: dict[str, Any] = {
            "search": search_clause,
            "per-page": min(MAX_RESULTS, 200),  # OpenAlex max
            "sort": "publication_date:desc",
            "mailto": MAILTO,
            "select": "id,doi,title,publication_year,cited_by_count,primary_location",
        }
        async with _semaphore:
            data = await _request_with_retry(client, OPENALEX_API, params)
    results = data.get("results", [])
    return list(results)[:MAX_RESULTS]


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
    works: list[dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[Paper], list[Citation]]:
    papers: list[Paper] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for work in works:
        title = work.get("title")
        year = work.get("publication_year")
        if not title or not year:
            continue

        doi_url = work.get("doi", "")
        doi = doi_url.replace("https://doi.org/", "") if doi_url else None
        primary = work.get("primary_location") or {}
        source = primary.get("source") or {}
        journal = source.get("display_name")

        papers.append(
            Paper(
                pmid=None,
                doi=doi,
                title=title,
                year=year,
                journal=journal,
                citations_count=work.get("cited_by_count"),
            )
        )

        # Prefer DOI URL over OpenAlex internal URL — DOIs are stable
        cite_url = f"https://doi.org/{doi}" if doi else work.get("id")
        if not cite_url:
            cite_id += 1
            continue

        citations.append(
            Citation(
                id=cite_id,
                url=cite_url,  # type: ignore[arg-type]
                title=f"{title[:80]}{'…' if len(title) > 80 else ''} · {journal or 'OpenAlex'} {year}",
                source_type="paper",
                retrieved_at=retrieved_at,
                locator=f"DOI {doi}" if doi else None,
            )
        )
        cite_id += 1

    return papers, citations
