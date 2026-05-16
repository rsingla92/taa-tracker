"""ClinicalTrials.gov v2 API client.

Docs: https://clinicaltrials.gov/data-api/api
No auth required. No published rate limit, but ~5 req/s is courteous.

We query by intervention/condition free text with antigen aliases ORed together,
then filter results in normalize.py against per-antigen exclude_terms.
"""

import asyncio
from datetime import UTC, date, datetime
from typing import Any

import httpx

from taa.schema import Antigen, Citation, SourceFreshness, Trial

CTGOV_API = "https://clinicaltrials.gov/api/v2/studies"
CONCURRENT = 5  # courteous self-imposed cap; CT.gov publishes no limit
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0

_semaphore = asyncio.Semaphore(CONCURRENT)


# ---- Phase + status mapping ---------------------------------------------------

_PHASE_MAP = {
    "EARLY_PHASE1": "1",
    "PHASE1": "1",
    "PHASE1/PHASE2": "1/2",
    "PHASE2": "2",
    "PHASE2/PHASE3": "2/3",
    "PHASE3": "3",
    "PHASE4": "approved",
    "NA": "unknown",
}

_STATUS_MAP = {
    "RECRUITING": "active",
    "ACTIVE_NOT_RECRUITING": "active",
    "ENROLLING_BY_INVITATION": "active",
    "NOT_YET_RECRUITING": "active",
    "COMPLETED": "completed",
    "TERMINATED": "terminated",
    "WITHDRAWN": "withdrawn",
    "SUSPENDED": "terminated",
}


# ---- Public API ---------------------------------------------------------------


class CtgovResult:
    def __init__(
        self,
        trials: list[Trial],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.trials = trials
        self.citations = citations
        self.freshness = freshness


async def fetch(
    antigen: Antigen,
    citation_id_start: int = 1,
    extra_drug_aliases: list[str] | None = None,
) -> CtgovResult:
    """Fetch all trials matching any antigen alias OR any extra drug alias.

    Returns trials + a Citation for each trial (one cite per NCT id) + a freshness
    record for the stale-data UX. citation_id_start lets the caller continue
    page-local citation numbering.

    Query is constrained to intervention name + title fields (not full text)
    to suppress the "alias matches every neurology trial" failure mode (e.g.,
    "NEU" matching neurology / neuroendocrine / neutropenia studies).

    `extra_drug_aliases` widens the search to drug names known to target this
    antigen (from drug_modality.yaml `drug_antigens`). Without it, trials whose
    title only mentions a proprietary codename ("HS-20089 in Ovarian Cancer")
    are silently dropped because no antigen alias appears in the indexable
    fields. Per-antigen exclude_terms downstream prevent cross-contamination
    when a drug ends up being relabelled.
    """
    attempt_at = datetime.now(UTC)
    aliases = [antigen.primary_name, *antigen.aliases]
    if extra_drug_aliases:
        aliases.extend(extra_drug_aliases)
    # Dedupe while preserving order; quote-escape any embedded double-quotes.
    seen: set[str] = set()
    deduped: list[str] = []
    for a in aliases:
        if a and a not in seen:
            seen.add(a)
            deduped.append(a.replace('"', '\\"'))
    aliases = deduped
    # AREA filter: search only Intervention name + BriefTitle + OfficialTitle.
    # CT.gov v2 query syntax: AREA[FieldName]"value"
    alias_clauses = [
        f'AREA[InterventionName]"{a}" OR AREA[BriefTitle]"{a}" OR AREA[OfficialTitle]"{a}"'
        for a in aliases
    ]
    query = " OR ".join(alias_clauses)

    try:
        raw = await _fetch_with_retry(query)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return CtgovResult(
            trials=[],
            citations=[],
            freshness=SourceFreshness(
                source="ctgov", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    trials, citations = _normalize(raw, citation_id_start, attempt_at)
    return CtgovResult(
        trials=trials,
        citations=citations,
        freshness=SourceFreshness(
            source="ctgov", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


# ---- Implementation ----------------------------------------------------------


async def _fetch_with_retry(query: str) -> list[dict[str, Any]]:
    """Query CT.gov, page through results, retry on 429 with exponential backoff."""
    studies: list[dict[str, Any]] = []
    page_token: str | None = None

    async with httpx.AsyncClient(http2=True, timeout=TIMEOUT_S) as client:
        for _ in range(50):  # max 50 pages = 5000 studies hard cap
            params: dict[str, Any] = {
                "query.term": query,
                "fields": "NCTId,BriefTitle,Phase,OverallStatus,LeadSponsorName,InterventionName,Condition,LastUpdatePostDate,PrimaryCompletionDate,LocationCountry",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token

            async with _semaphore:
                data = await _request_with_429_retry(client, params)

            studies.extend(data.get("studies", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break

    return studies


async def _request_with_429_retry(
    client: httpx.AsyncClient, params: dict[str, Any]
) -> dict[str, Any]:
    """Single GET with retry-on-429 (exponential backoff)."""
    for attempt in range(RETRY_ATTEMPTS):
        resp = await client.get(CTGOV_API, params=params)
        if resp.status_code == 429:
            sleep_for = RETRY_BACKOFF_S * (2**attempt)
            await asyncio.sleep(sleep_for)
            continue
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    raise httpx.HTTPStatusError(
        "exhausted 429 retries",
        request=httpx.Request("GET", CTGOV_API),
        response=httpx.Response(429),
    )


def _normalize(
    studies: list[dict[str, Any]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[Trial], list[Citation]]:
    """Map CT.gov v2 study payloads to Trial + Citation rows."""
    trials: list[Trial] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for study in studies:
        protocol = study.get("protocolSection", {})
        ident = protocol.get("identificationModule", {})
        status = protocol.get("statusModule", {})
        sponsor = protocol.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
        design = protocol.get("designModule", {})
        arms = protocol.get("armsInterventionsModule", {})
        conds = protocol.get("conditionsModule", {})
        contacts = protocol.get("contactsLocationsModule", {})

        nct_id = ident.get("nctId")
        if not nct_id:
            continue

        phases = design.get("phases", [])
        phase_str = phases[0] if phases else "NA"
        phase = _PHASE_MAP.get(phase_str, "unknown")

        status_str = status.get("overallStatus", "")
        normalized_status = _STATUS_MAP.get(status_str, "unknown")

        last_update_str = status.get("lastUpdatePostDateStruct", {}).get("date")
        last_update = (
            date.fromisoformat(last_update_str) if last_update_str else date(1970, 1, 1)
        )

        # primaryCompletionDateStruct: {date: "YYYY-MM-DD" | "YYYY-MM", type: "ACTUAL" | "ESTIMATED"}
        # Month-only dates (no day) appear for far-future ESTIMATED dates; default to day 15.
        pcd_struct = status.get("primaryCompletionDateStruct", {})
        pcd_raw = pcd_struct.get("date")
        pcd: date | None = None
        if pcd_raw:
            try:
                if len(pcd_raw) == 7:  # "YYYY-MM"
                    pcd = date.fromisoformat(pcd_raw + "-15")
                else:
                    pcd = date.fromisoformat(pcd_raw)
            except ValueError:
                pcd = None
        pcd_is_actual = pcd_struct.get("type") == "ACTUAL"

        interventions = [
            i.get("name", "")
            for i in arms.get("interventions", [])
            if i.get("name")
        ]

        # Unique set of country names where the trial has at least one site.
        # Drives the North-America catalyst filter; CT.gov sometimes omits
        # locations entirely (early/protocol-only studies) — empty list means
        # "geography unknown", catalysts logic treats that as a non-match.
        countries = sorted({
            loc.get("country", "")
            for loc in contacts.get("locations", [])
            if loc.get("country")
        })

        trials.append(
            Trial(
                nct_id=nct_id,
                title=ident.get("briefTitle", ""),
                phase=phase,
                status=normalized_status,
                sponsors=[sponsor.get("name", "")] if sponsor.get("name") else [],
                interventions=interventions,
                conditions=conds.get("conditions", []),
                last_update=last_update,
                primary_completion_date=pcd,
                primary_completion_is_actual=pcd_is_actual,
                location_countries=countries,
                citation_ids=[cite_id],
            )
        )

        citations.append(
            Citation(
                id=cite_id,
                url=f"https://clinicaltrials.gov/study/{nct_id}",
                title=f"{nct_id} · ClinicalTrials.gov",
                source_type="trial",
                retrieved_at=retrieved_at,
                locator=nct_id,
            )
        )
        cite_id += 1

    return trials, citations
