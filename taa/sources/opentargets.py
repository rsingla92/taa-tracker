"""Open Targets GraphQL API client.

Docs: https://platform-docs.opentargets.org/data-access/graphql-api
Endpoint: https://api.platform.opentargets.org/api/v4/graphql
Free, no auth, no rate-limit headers (we self-pace at ~5 req/s).

Adds the biology layer: druggability, mechanism of action, top disease
associations, known drug-target interactions, target safety profile. None
of CT.gov / PubMed / OpenAlex / EDGAR have this; Open Targets is THE source
for "what does this target look like as a drug development opportunity?"

Open Targets uses Ensembl gene IDs as primary target identifier. We store
ensembl_id in antigens.yaml to avoid a search-then-lookup round-trip.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, OpenTargetsData, SourceFreshness

OPENTARGETS_API = "https://api.platform.opentargets.org/api/v4/graphql"
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0

_semaphore = asyncio.Semaphore(5)

# GraphQL query — pulls the fields we render on the scorecard biology section.
# Open Targets v25 schema (introspected May 2026). Field names are different
# from older docs: knownDrugs → drugAndClinicalCandidates, maximumClinicalTrialPhase → maximumClinicalStage.
TARGET_QUERY = """
query TargetByEnsembl($ensemblId: String!) {
  target(ensemblId: $ensemblId) {
    id
    approvedSymbol
    approvedName
    biotype
    proteinIds { id source }
    tractability { label modality value }
    drugAndClinicalCandidates {
      count
      rows {
        drug { id name drugType maximumClinicalStage }
        maxClinicalStage
        diseases { disease { name } }
      }
    }
    associatedDiseases(page: { size: 8, index: 0 }) {
      count
      rows {
        disease { id name therapeuticAreas { name } }
        score
      }
    }
    safetyLiabilities {
      event
      eventId
      effects { direction dosing }
    }
  }
}
"""


class OpenTargetsResult:
    def __init__(
        self,
        data: OpenTargetsData | None,
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.data = data
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> OpenTargetsResult:
    """Fetch target biology from Open Targets. Skips silently if no Ensembl ID."""
    attempt_at = datetime.now(timezone.utc)

    if not antigen.ensembl_id:
        return OpenTargetsResult(
            data=None,
            citations=[],
            freshness=SourceFreshness(
                source="opentargets",
                last_attempt=attempt_at,
                error="no ensembl_id in antigens.yaml — skipping",
            ),
        )

    try:
        raw = await _query(antigen.ensembl_id)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        return OpenTargetsResult(
            data=None,
            citations=[],
            freshness=SourceFreshness(
                source="opentargets", last_attempt=attempt_at, error=f"{type(e).__name__}: {e}"
            ),
        )

    target = raw.get("data", {}).get("target")
    if not target:
        return OpenTargetsResult(
            data=None,
            citations=[],
            freshness=SourceFreshness(
                source="opentargets",
                last_attempt=attempt_at,
                error=f"no target found for {antigen.ensembl_id}",
            ),
        )

    data, citations = _normalize(target, citation_id_start, attempt_at)
    return OpenTargetsResult(
        data=data,
        citations=citations,
        freshness=SourceFreshness(
            source="opentargets", last_success=attempt_at, last_attempt=attempt_at
        ),
    )


async def _query(ensembl_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        async with _semaphore:
            for attempt in range(RETRY_ATTEMPTS):
                resp = await client.post(
                    OPENTARGETS_API,
                    json={"query": TARGET_QUERY, "variables": {"ensemblId": ensembl_id}},
                )
                if resp.status_code == 429:
                    await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
                    continue
                resp.raise_for_status()
                return resp.json()  # type: ignore[no-any-return]
    raise httpx.HTTPStatusError(
        "exhausted 429 retries",
        request=httpx.Request("POST", OPENTARGETS_API),
        response=httpx.Response(429),
    )


def _normalize(
    target: dict[str, Any], citation_id_start: int, retrieved_at: datetime
) -> tuple[OpenTargetsData, list[Citation]]:
    cite_id = citation_id_start
    citations: list[Citation] = []

    # Tractability scores (druggability per modality) — pick the small-mol + AB axes
    tract = target.get("tractability") or []
    tractability_summary: list[dict[str, Any]] = []
    for entry in tract:
        if entry.get("value") and entry.get("modality") in (
            "SM",
            "AB",
            "PR",
            "OC",
            "Other",
        ):
            tractability_summary.append(
                {
                    "modality": entry.get("modality"),
                    "label": entry.get("label"),
                }
            )

    # Top diseases by association score
    diseases = (target.get("associatedDiseases") or {}).get("rows") or []
    top_diseases = [
        {
            "name": d["disease"]["name"],
            "score": round(d["score"], 3),
            "therapeutic_areas": [
                ta["name"] for ta in (d["disease"].get("therapeuticAreas") or [])
            ],
        }
        for d in diseases[:6]
    ]

    # Known drugs — flatten and uniq by drug name
    known = (target.get("drugAndClinicalCandidates") or {}).get("rows") or []
    seen_drugs: set[str] = set()
    drug_summary: list[dict[str, Any]] = []
    for row in known:
        drug = row.get("drug") or {}
        name = drug.get("name", "")
        if not name or name in seen_drugs:
            continue
        seen_drugs.add(name)
        diseases = [
            d.get("disease", {}).get("name")
            for d in (row.get("diseases") or [])[:3]
            if d.get("disease")
        ]
        drug_summary.append(
            {
                "name": name,
                "drug_type": drug.get("drugType"),
                "max_phase": drug.get("maximumClinicalStage") or row.get("maxClinicalStage"),
                "diseases": [d for d in diseases if d],
            }
        )

    # Safety liabilities (top 5 events)
    safety = target.get("safetyLiabilities") or []
    safety_summary = [
        {"event": s.get("event"), "id": s.get("eventId")}
        for s in safety[:5]
        if s.get("event")
    ]

    data = OpenTargetsData(
        ensembl_id=target.get("id", ""),
        approved_symbol=target.get("approvedSymbol", ""),
        approved_name=target.get("approvedName", ""),
        biotype=target.get("biotype"),
        tractability=tractability_summary,
        top_diseases=top_diseases,
        known_drugs=drug_summary,
        safety_liabilities=safety_summary,
    )

    # One citation pointing back to the Open Targets target page (anchor for all OT facts)
    citations.append(
        Citation(
            id=cite_id,
            url=f"https://platform.opentargets.org/target/{target.get('id')}",  # type: ignore[arg-type]
            title=f"Open Targets · {target.get('approvedSymbol')} ({target.get('id')})",
            source_type="paper",  # closest match in our enum; will widen schema in v0.3
            retrieved_at=retrieved_at,
            locator=target.get("id"),
        )
    )

    return data, citations
