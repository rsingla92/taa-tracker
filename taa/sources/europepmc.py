"""Europe PMC client — bioRxiv / medRxiv preprint stream.

Docs: https://europepmc.org/RestfulWebService
Rate limit: not enforced as far as Europe PMC publishes; the polite-pool
convention is to identify yourself in the User-Agent and stay under ~10 req/s.
No auth required.

Why preprints: bioRxiv / medRxiv lead PubMed by 6–18 months for clinical-stage
readouts and even longer for academic CAR-T / ADC preclinical work. The Europe
PMC search lets us pull both servers through one endpoint with a `SRC:PPR`
filter; we keep the bioRxiv/medRxiv subset because those are the two that
oncology readers expect to see broken out.

Search strategy: title-or-abstract match on antigen aliases, restricted to
preprints (`SRC:PPR`), most recent first. Doesn't double-count anything that's
already in PubMed because PubMed-indexed preprints get `SRC:MED`/`SRC:PMC`.
"""

import asyncio
from datetime import date, datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, Preprint, SourceFreshness

EPMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
MAX_RESULTS = 100
USER_AGENT = "TAA Tracker (rsingla92+taa-tracker@gmail.com)"

_semaphore = asyncio.Semaphore(6)

# Servers we surface — Europe PMC indexes more preprint servers but BD readers
# expect bioRxiv/medRxiv specifically. Other sources (ChemRxiv, Research Square)
# carry less oncology signal and add noise.
_KEEP_SOURCES = {"PPR"}  # Europe PMC source code for all preprints
_KEEP_SERVERS = {"biorxiv", "medrxiv"}


class EuropepmcResult:
    def __init__(
        self,
        preprints: list[Preprint],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.preprints = preprints
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> EuropepmcResult:
    """Fetch recent bioRxiv/medRxiv preprints matching antigen aliases."""
    attempt_at = datetime.now(timezone.utc)
    aliases = [antigen.primary_name, *antigen.aliases]

    # Europe PMC query syntax: TITLE:"X" OR TITLE:"Y" — restrict to preprints.
    # Use TITLE rather than the broader free-text default to control noise.
    alias_clause = " OR ".join(f'TITLE:"{a}"' for a in aliases)
    query = f"({alias_clause}) AND SRC:PPR"

    try:
        results = await _search(query)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return EuropepmcResult(
            preprints=[],
            citations=[],
            freshness=SourceFreshness(
                source="preprints", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    preprints, citations = _normalize(results, citation_id_start, attempt_at)
    return EuropepmcResult(
        preprints=preprints,
        citations=citations,
        freshness=SourceFreshness(
            source="preprints", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


async def _search(query: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}
    ) as client:
        params: dict[str, Any] = {
            "query": query,
            "format": "json",
            "pageSize": MAX_RESULTS,
            "resultType": "core",
            "sort": "FIRST_PDATE_D desc",
        }
        async with _semaphore:
            data = await _request_with_retry(client, EPMC_API, params)
    hit_list = data.get("resultList", {}).get("result", [])
    return list(hit_list)[:MAX_RESULTS]


async def _request_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, Any]
) -> dict[str, Any]:
    for attempt in range(RETRY_ATTEMPTS):
        resp = await client.get(url, params=params)
        if resp.status_code in (429, 503):
            await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
            continue
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    raise httpx.HTTPStatusError(
        "exhausted retries", request=httpx.Request("GET", url), response=httpx.Response(429)
    )


def _normalize(
    results: list[dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[Preprint], list[Citation]]:
    preprints: list[Preprint] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for r in results:
        # bookOrReport / hasBook etc. — keep only items where the bibliographic
        # source string identifies a preprint server we surface.
        # Europe PMC stores the preprint server name in `bookOrReportDetails`
        # for some, in `journalInfo` for others — we check both.
        server_label = _extract_server(r)
        if server_label is None:
            continue

        title = (r.get("title") or "").strip().rstrip(".")
        if not title:
            continue

        year_raw = r.get("pubYear") or r.get("firstPublicationDate", "")[:4]
        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            continue

        doi = r.get("doi") or None

        posted_date = _parse_date(r.get("firstPublicationDate"))

        preprints.append(
            Preprint(
                doi=doi,
                title=title,
                year=year,
                server=server_label,
                posted_date=posted_date,
            )
        )

        # Prefer DOI URL; fall back to Europe PMC permalink.
        if doi:
            url = f"https://doi.org/{doi}"
        else:
            url = f"https://europepmc.org/article/PPR/{r.get('id', '')}"

        citations.append(
            Citation(
                id=cite_id,
                url=url,  # type: ignore[arg-type]
                title=f"{title[:80]}{'…' if len(title) > 80 else ''} · {server_label} {year}",
                source_type="paper",
                retrieved_at=retrieved_at,
                locator=f"DOI {doi}" if doi else f"EPMC PPR/{r.get('id', '')}",
            )
        )
        cite_id += 1

    return preprints, citations


def _extract_server(r: dict[str, Any]) -> str | None:
    """Identify whether this preprint is on bioRxiv / medRxiv. Returns label or None."""
    # Europe PMC tucks this in different places — check all known locations.
    candidates: list[str] = []
    book = r.get("bookOrReportDetails") or {}
    if isinstance(book, dict):
        candidates.append(str(book.get("publisher", "")))
        candidates.append(str(book.get("yearOfPublication", "")))
    journal = r.get("journalInfo") or {}
    if isinstance(journal, dict):
        j = journal.get("journal") or {}
        if isinstance(j, dict):
            candidates.append(str(j.get("title", "")))
            candidates.append(str(j.get("medlineAbbreviation", "")))
    candidates.append(str(r.get("source", "")))
    candidates.append(str(r.get("publisher", "")))
    blob = " ".join(candidates).lower()

    if "biorxiv" in blob:
        return "bioRxiv"
    if "medrxiv" in blob:
        return "medRxiv"
    # Some bioRxiv DOIs are unambiguous even without source metadata
    doi = (r.get("doi") or "").lower()
    if "10.1101/" in doi:
        # Both bioRxiv and medRxiv use the 10.1101 prefix; distinguish by DOI
        # path component when present.
        if "/medrxiv" in doi or doi.endswith("rxiv-medrxiv"):
            return "medRxiv"
        return "bioRxiv"
    return None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
