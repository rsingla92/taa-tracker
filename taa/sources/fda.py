"""FDA Drugs@FDA API client (openFDA).

Docs: https://open.fda.gov/apis/drug/drugsfda/
Endpoint: https://api.fda.gov/drug/drugsfda.json
Free, no auth (240 req/min, 1000/day per IP — generous).

Used to confirm which drugs are *actually* FDA-approved (vs CT.gov phase
"approved" status which means the trial passed phase 3, not the drug itself).
Also captures original approval date and company-of-record (sponsor).

Per-antigen lookup: query by known drug names from the curated TPP. We don't
search by antigen alias here because openFDA doesn't index by mechanism;
the TPP is our anchor for which drugs to confirm.
"""

import asyncio
from datetime import date, datetime, timezone
from typing import Any

import httpx

from taa.schema import Antigen, Citation, FDAApproval, SourceFreshness

OPENFDA_API = "https://api.fda.gov/drug/drugsfda.json"
TIMEOUT_S = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0
LIMIT = 25  # results per drug query

_semaphore = asyncio.Semaphore(8)


class FDAResult:
    def __init__(
        self,
        approvals: list[FDAApproval],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.approvals = approvals
        self.citations = citations
        self.freshness = freshness


async def fetch_for_drugs(
    antigen: Antigen,
    drug_names: list[str],
    citation_id_start: int = 1,
) -> FDAResult:
    """Look up FDA approval records for a list of drug names.

    Caller passes drug_names from the curated drug_modality.yaml so we only
    look up things we believe matter. Returns deduped FDAApproval records.
    """
    attempt_at = datetime.now(timezone.utc)

    # Skip generic markers from drug_modality.yaml that would over-match openFDA.
    GENERIC_MARKERS = {
        "car-t", "car t", "chimeric antigen receptor", "vaccine",
        "peptide vaccine", "dendritic cell", "radioligand",
    }
    safe_names = [
        d for d in drug_names if len(d) >= 4 and d.lower() not in GENERIC_MARKERS
    ]

    if not safe_names:
        return FDAResult(
            approvals=[],
            citations=[],
            freshness=SourceFreshness(
                source="fda", last_attempt=attempt_at, last_success=attempt_at
            ),
        )

    seen_app_numbers: set[str] = set()
    approvals: list[FDAApproval] = []

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        tasks = [
            asyncio.create_task(_fetch_one(client, drug_name)) for drug_name in safe_names
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    error_drugs: list[str] = []
    for drug_name, result in zip(safe_names, results, strict=False):
        if isinstance(result, BaseException):
            error_drugs.append(drug_name)
            continue
        for raw in result:  # type: ignore[union-attr]
            app_no = raw.get("application_number", "")
            if not app_no or app_no in seen_app_numbers:
                continue
            seen_app_numbers.add(app_no)
            normalized = _normalize_record(raw, drug_name)
            if normalized:
                approvals.append(normalized)

    citations = _build_citations(approvals, citation_id_start, attempt_at)
    error_str = "lookups failed: " + ", ".join(error_drugs[:5]) if error_drugs else None
    return FDAResult(
        approvals=approvals,
        citations=citations,
        freshness=SourceFreshness(
            source="fda",
            last_success=attempt_at if approvals or not error_drugs else None,
            last_attempt=attempt_at,
            error=error_str,
        ),
    )


async def _fetch_one(client: httpx.AsyncClient, drug_name: str) -> list[dict[str, Any]]:
    """Query openFDA for a single drug name. Returns raw application records."""
    # Query: openfda.brand_name OR openfda.generic_name OR openfda.substance_name
    # Single drug_name string with quotes for exact-ish match
    safe = drug_name.replace('"', "")
    query = (
        f'(openfda.brand_name:"{safe}" '
        f'OR openfda.generic_name:"{safe}" '
        f'OR openfda.substance_name:"{safe}")'
    )
    params = {"search": query, "limit": LIMIT}

    async with _semaphore:
        for attempt in range(RETRY_ATTEMPTS):
            resp = await client.get(OPENFDA_API, params=params)
            if resp.status_code == 404:
                return []  # no matches; openFDA returns 404 for empty results
            if resp.status_code == 429:
                await asyncio.sleep(RETRY_BACKOFF_S * (2**attempt))
                continue
            resp.raise_for_status()
            return list(resp.json().get("results", []))
    return []


def _normalize_record(raw: dict[str, Any], queried_drug: str) -> FDAApproval | None:
    """Map openFDA application record → FDAApproval row."""
    app_no = raw.get("application_number", "")
    sponsor = raw.get("sponsor_name", "")
    openfda = raw.get("openfda", {})
    brand_names = openfda.get("brand_name") or []
    generic_names = openfda.get("generic_name") or []

    # Pick a display name — first brand, fall back to first generic, fall back to query
    display_name = (
        (brand_names[0] if brand_names else None)
        or (generic_names[0] if generic_names else None)
        or queried_drug
    )

    # Pull most recent submission with submission_status == "AP" (approved)
    submissions = raw.get("submissions") or []
    approved_subs = [
        s
        for s in submissions
        if s.get("submission_status") == "AP" and s.get("submission_status_date")
    ]
    if not approved_subs:
        return None

    # Earliest approval (original)
    approved_subs.sort(key=lambda s: s.get("submission_status_date", ""))
    first_approval = approved_subs[0]
    latest_approval = approved_subs[-1]

    try:
        first_date = _parse_fda_date(first_approval.get("submission_status_date", ""))
        latest_date = _parse_fda_date(latest_approval.get("submission_status_date", ""))
    except ValueError:
        return None

    products = raw.get("products") or []
    routes = sorted({p.get("route") for p in products if p.get("route")})
    product_types = sorted({p.get("dosage_form") for p in products if p.get("dosage_form")})

    return FDAApproval(
        application_number=app_no,
        sponsor=sponsor,
        display_name=display_name,
        brand_names=brand_names[:3],
        generic_names=generic_names[:3],
        first_approved=first_date,
        latest_action=latest_date,
        approval_count=len(approved_subs),
        routes=list(routes),
        dosage_forms=list(product_types),
    )


def _parse_fda_date(s: str) -> date:
    """openFDA dates are YYYYMMDD strings."""
    if len(s) != 8:
        raise ValueError(f"unexpected date format: {s}")
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _build_citations(
    approvals: list[FDAApproval],
    citation_id_start: int,
    retrieved_at: datetime,
) -> list[Citation]:
    citations: list[Citation] = []
    cite_id = citation_id_start
    for app in approvals:
        url = f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app.application_number}"
        citations.append(
            Citation(
                id=cite_id,
                url=url,  # type: ignore[arg-type]
                title=f"FDA {app.application_number} · {app.display_name} · {app.sponsor}",
                source_type="filing",  # closest existing enum
                retrieved_at=retrieved_at,
                locator=f"FDA {app.application_number}",
            )
        )
        cite_id += 1
    return citations
