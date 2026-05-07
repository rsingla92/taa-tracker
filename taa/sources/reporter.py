"""NIH RePORTER client — US-funded grant pipeline.

Docs: https://api.reporter.nih.gov/
Rate limit: not formally published; the polite practice is < 1 req/s sustained
and the v2 API is generous on burst. No auth required.

Why grants: RePORTER captures the academic preclinical pipeline (R01s, P01s,
SPOREs, U54 networks) that won't show up in CT.gov for years. For TAAs with
heavy academic CAR-T / ADC activity (B7-H3, ROR1, 5T4) this is leading-indicator
signal — university labs file IND a few years after grants land.

Search strategy: POST /v2/projects/search with `search_field=projecttitle,terms`,
then post-filter the API response to require an antigen alias appear in the
project title. The two-step matters: RePORTER's `terms` field is RCDC-curated
and tags broad CAR-T / immunotherapy grants with every TAA they touch, so a
raw search returns many "is about CAR-T platform, mentions B7-H3 in passing"
hits. Title-mention is the deterministic signal that the grant is *about* the
antigen, not just adjacent to it. We restrict to active fiscal years (last 5)
and oncology-relevant institutes (NCI, NHLBI, NIAID, NIDDK).
"""

import asyncio
import re
from datetime import date, datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, Grant, SourceFreshness

REPORTER_API = "https://api.reporter.nih.gov/v2/projects/search"
TIMEOUT_S = 45  # NIH RePORTER can be slow on large queries
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.5
MAX_RESULTS = 200  # raised because we post-filter heavily on title-mention
USER_AGENT = "TAA Tracker (rsingla92+taa-tracker@gmail.com)"

# Oncology-relevant institute codes. NCI is the main one; NHLBI funds heme
# malignancy work; NIAID funds tumour immunology / CAR-T; NIDDK funds GI cancer
# work where CLDN18.2 / B7-H3 sometimes intersect.
_ONCOLOGY_INSTITUTES = ["NCI", "NHLBI", "NIAID", "NIDDK"]

_semaphore = asyncio.Semaphore(2)  # be polite — single-tenant API


class ReporterResult:
    def __init__(
        self,
        grants: list[Grant],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.grants = grants
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> ReporterResult:
    """Fetch active NIH grants matching antigen aliases."""
    attempt_at = datetime.now(timezone.utc)
    aliases = [antigen.primary_name, *antigen.aliases]

    # RePORTER's `advanced_text_search` field accepts a Boolean string;
    # fielding it to `projecttitle,terms,abstracttext` matches the broadest
    # but still semantically-relevant pool. Quoted alias terms keep multi-word
    # aliases together (e.g., "Claudin 18.2").
    search_text = " OR ".join(f'"{a}"' for a in aliases)

    current_year = datetime.now(timezone.utc).year
    fiscal_years = list(range(current_year - 4, current_year + 1))

    # search_field=projecttitle,terms (NOT abstracttext) — abstract text matches
    # are too loose for short/common aliases ("B7-H3" appears in many CAR-T
    # abstracts as part of a TAA list without the grant being *about* B7-H3).
    # Title + RCDC terms gives strong-relevance hits; if a target is genuinely
    # the topic of the grant it'll show up there.
    payload: dict[str, Any] = {
        "criteria": {
            "advanced_text_search": {
                "operator": "and",
                "search_field": "projecttitle,terms",
                "search_text": search_text,
            },
            "fiscal_years": fiscal_years,
            "agencies": _ONCOLOGY_INSTITUTES,
        },
        "limit": MAX_RESULTS,
        "offset": 0,
        "sort_field": "project_start_date",
        "sort_order": "desc",
    }

    try:
        results = await _post(payload)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return ReporterResult(
            grants=[],
            citations=[],
            freshness=SourceFreshness(
                source="reporter", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    # Post-filter: keep only grants where the antigen alias appears in the
    # project title. RePORTER's RCDC `terms` field tags many CAR-T / IO grants
    # with multiple TAAs even when those antigens are tangential to the grant's
    # actual aim. Title-mention is the cleaner deterministic signal.
    title_pattern = re.compile(
        r"\b(" + "|".join(re.escape(a) for a in aliases) + r")\b", re.IGNORECASE
    )
    results = [r for r in results if title_pattern.search(r.get("project_title") or "")]

    grants, citations = _normalize(results, citation_id_start, attempt_at)
    return ReporterResult(
        grants=grants,
        citations=citations,
        freshness=SourceFreshness(
            source="reporter", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


async def _post(payload: dict[str, Any]) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    ) as client:
        async with _semaphore:
            data = await _request_with_retry(client, REPORTER_API, payload)
    return list(data.get("results", []))[:MAX_RESULTS]


async def _request_with_retry(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any]
) -> dict[str, Any]:
    for attempt in range(RETRY_ATTEMPTS):
        resp = await client.post(url, json=payload)
        if resp.status_code in (429, 502, 503, 504):
            await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
            continue
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    raise httpx.HTTPStatusError(
        "exhausted retries", request=httpx.Request("POST", url), response=httpx.Response(429)
    )


def _normalize(
    results: list[dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[Grant], list[Citation]]:
    grants: list[Grant] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for r in results:
        project_num = r.get("project_num") or r.get("core_project_num") or ""
        title = (r.get("project_title") or "").strip()
        if not project_num or not title:
            continue

        pis = r.get("principal_investigators") or []
        pi_name = None
        if pis and isinstance(pis, list):
            first = pis[0]
            if isinstance(first, dict):
                pi_name = first.get("full_name") or first.get("first_name", "")

        org = r.get("organization") or {}
        org_name = org.get("org_name") if isinstance(org, dict) else None

        grants.append(
            Grant(
                project_num=project_num,
                title=title,
                pi_name=pi_name,
                organization=org_name,
                fiscal_year=r.get("fiscal_year"),
                award_amount=r.get("award_amount"),
                project_start=_parse_date(r.get("project_start_date")),
                project_end=_parse_date(r.get("project_end_date")),
            )
        )

        # Public RePORTER URL by project number
        url = f"https://reporter.nih.gov/search/?term={project_num}"
        citations.append(
            Citation(
                id=cite_id,
                url=url,  # type: ignore[arg-type]
                title=f"{title[:80]}{'…' if len(title) > 80 else ''} · NIH {project_num}",
                source_type="filing",
                retrieved_at=retrieved_at,
                locator=f"NIH {project_num}",
            )
        )
        cite_id += 1

    return grants, citations


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
