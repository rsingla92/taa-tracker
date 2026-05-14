"""Pydantic models for the TAA scorecard data layer.

All structured data flows through these. Synthesis output (LLM-generated prose)
is a separate concern handled in taa/synth.py and lives in dist/, not data/.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

# ---- Vocabulary ----------------------------------------------------------------

Modality = Literal[
    "adc",
    "car-t",
    "bispecific",
    "mab",
    "radioligand",
    "vaccine",
    "other",
]

Phase = Literal[
    "preclinical",
    "1",
    "1/2",
    "2",
    "2/3",
    "3",
    "approved",
    "unknown",
]

Status = Literal["active", "completed", "terminated", "withdrawn", "unknown"]

SourceType = Literal["trial", "paper", "filing", "abstract", "deck"]


# ---- Citations -----------------------------------------------------------------


class Citation(BaseModel):
    """A single source pointer rendered in the scorecard's citation footer.

    `id` is page-local and 1-indexed. The renderer assigns these deterministically
    based on order-of-first-reference in the page's structured data.
    """

    id: int = Field(ge=1)
    url: HttpUrl
    title: str
    source_type: SourceType
    retrieved_at: datetime
    locator: str | None = None  # e.g., "Item 1, p.4" or "NCT01234567"


# ---- Antigen + program data ----------------------------------------------------


class Antigen(BaseModel):
    slug: str  # url path component (must be lowercased, hyphenated)
    primary_name: str  # display name, e.g., "HER2"
    aliases: list[str] = Field(default_factory=list)
    uniprot_id: str | None = None
    hgnc_symbol: str | None = None
    ensembl_id: str | None = None  # Open Targets primary identifier
    indication_tags: list[str] = Field(default_factory=list)  # e.g., ["breast", "gastric"]
    notes: str | None = None
    exclude_terms: list[str] = Field(default_factory=list)  # false-positive filters per source


class Trial(BaseModel):
    nct_id: str
    title: str
    phase: Phase = "unknown"
    status: Status = "unknown"
    sponsors: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    last_update: date
    primary_completion_date: date | None = None  # planned or actual PCD — drives the catalysts view
    primary_completion_is_actual: bool = False  # CT.gov distinguishes ESTIMATED vs ACTUAL
    location_countries: list[str] = Field(default_factory=list)  # ISO-ish country names from CT.gov sites — used to filter catalysts to N. America
    citation_ids: list[int] = Field(default_factory=list)


class Paper(BaseModel):
    pmid: str | None = None
    doi: str | None = None
    title: str
    year: int
    journal: str | None = None
    citations_count: int | None = None  # from OpenAlex


class Filing(BaseModel):
    accession: str
    cik: str
    company: str
    form_type: str  # "10-K", "8-K", "S-1"
    filed_at: date
    filing_url: HttpUrl


class FDAApproval(BaseModel):
    """One FDA-approved application (NDA / BLA) record from openFDA."""

    application_number: str  # "BLA125550", "NDA214511"
    sponsor: str
    display_name: str
    brand_names: list[str] = Field(default_factory=list)
    generic_names: list[str] = Field(default_factory=list)
    first_approved: date
    latest_action: date
    approval_count: int = 0  # # of approval submissions on this application
    routes: list[str] = Field(default_factory=list)
    dosage_forms: list[str] = Field(default_factory=list)


class EMAApproval(BaseModel):
    """One EMA-authorized medicine record from the EPAR bulk download."""

    name: str
    active_substance: str
    marketing_authorisation_holder: str
    ema_product_number: str
    authorisation_date: date | None = None
    atc_code: str | None = None
    url: str | None = None


class ConferenceAbstract(BaseModel):
    """A conference proceeding abstract sourced via PubMed (journal supplement).

    Distinct from Paper because conference abstracts have different metadata
    (meeting name, abstract number) and BD readers want them broken out.
    """

    pmid: str | None = None
    doi: str | None = None
    title: str
    year: int
    journal: str | None = None  # e.g., "J Clin Oncol", "Cancer Res"
    meeting: str | None = None  # e.g., "ASCO Annual Meeting 2025"
    abstract_number: str | None = None


class Preprint(BaseModel):
    """A bioRxiv / medRxiv preprint, sourced via Europe PMC.

    Preprints lead PubMed by 6–18 months for clinical-stage readouts and even
    longer for academic CAR-T / ADC preclinical work. We surface them as a
    distinct stream because BD readers value the leading-indicator nature.
    """

    doi: str | None = None
    title: str
    year: int
    server: str  # "bioRxiv" or "medRxiv"
    posted_date: date | None = None


class Grant(BaseModel):
    """An NIH-funded grant from RePORTER. Preclinical / translational signal.

    Captures the academic pipeline that doesn't show up in CT.gov or EDGAR —
    especially relevant for CAR-T / ADC targets where academic centers run
    early-stage IIT programs years before a sponsor picks them up.
    """

    project_num: str  # e.g., "5R01CA123456-03"
    title: str
    pi_name: str | None = None
    organization: str | None = None
    fiscal_year: int | None = None
    award_amount: int | None = None  # USD
    project_start: date | None = None
    project_end: date | None = None


class Program(BaseModel):
    """A canonical drug development effort against one antigen with one modality.

    Derived in taa/normalize.py from Trial / Filing rows by alias matching
    + per-antigen exclude rules + drug_modality.yaml lookup. Programs are
    rolled up by (canonical_drug, modality) — many trial-sponsor combinations
    collapse into one Program (e.g., 200 Trastuzumab+combo trials → 1 mAb
    Program "Trastuzumab" with sponsors=[Genentech, ...academic centers]).
    """

    antigen_slug: str
    modality: Modality
    canonical_drug: str  # the matching key from drug_modality.yaml
    sponsors: list[str] = Field(default_factory=list)  # all distinct sponsors, lead first
    trial_count: int = 0  # how many trials rolled up into this Program
    most_advanced_phase: Phase = "unknown"
    status: Status = "active"
    latest_update: date | None = None
    citation_ids: list[int] = Field(default_factory=list)


# ---- Stale-source tracking -----------------------------------------------------


class SourceFreshness(BaseModel):
    """Per-source freshness metadata for the stale-data UX."""

    source: Literal[
        "ctgov",
        "pubmed",
        "openalex",
        "edgar",
        "opentargets",
        "news",
        "fda",
        "ema",
        "abstracts",
        "preprints",
        "reporter",
    ]
    last_success: datetime | None = None
    last_attempt: datetime
    error: str | None = None  # populated if last_attempt failed

    @property
    def is_stale(self) -> bool:
        """Stale if the last attempt failed, regardless of when last success was."""
        return self.error is not None


# ---- Open Targets biology ------------------------------------------------------


class OpenTargetsData(BaseModel):
    """Per-target biology summary from Open Targets.

    Adds the layer none of CT.gov / PubMed / OpenAlex / EDGAR have: druggability
    score, mechanism, top disease associations, known drug list, safety profile.
    """

    ensembl_id: str
    approved_symbol: str
    approved_name: str
    biotype: str | None = None
    tractability: list[dict[str, Any]] = Field(default_factory=list)  # modality + label
    top_diseases: list[dict[str, Any]] = Field(default_factory=list)  # name + score
    known_drugs: list[dict[str, Any]] = Field(default_factory=list)
    safety_liabilities: list[dict[str, Any]] = Field(default_factory=list)


# ---- News (RSS aggregation) ---------------------------------------------------


class NewsItem(BaseModel):
    title: str
    summary: str | None = None
    url: HttpUrl
    source: str  # "FierceBiotech", "PRNewswire pharma", etc.
    published_at: datetime | None = None


# ---- Target Product Profile (curated) -----------------------------------------


class TPPEndpoint(BaseModel):
    """One efficacy endpoint with metric, comparator, source citation."""

    name: str  # "ORR", "PFS", "OS", "DCR"
    metric: str  # "9.9 months", "52%", "HR 0.50"
    comparator: str | None = None  # "vs 5.1 months chemo (DESTINY-Breast04)"
    citation_url: str | None = None  # link to the trial readout / paper / FDA label


class TPPSafety(BaseModel):
    common_aes: list[str] = Field(default_factory=list)  # ["nausea (50%)", "fatigue (45%)"]
    aes_of_special_interest: list[str] = Field(default_factory=list)  # ILD, CRS, etc.
    discontinuation_rate: str | None = None  # "13.6%"
    boxed_warning: str | None = None  # FDA black-box warning if applicable


class TPPDosing(BaseModel):
    regimen: str  # "5.4 mg/kg IV q3w"
    route: str  # "IV", "SC", "Oral"
    line_of_therapy: str | None = None  # "2L+ HER2+ MBC"
    biomarker_selection: str | None = None  # "HER2 IHC 3+ or 2+/ISH+"
    half_life: str | None = None  # "5.7 days"


class TargetProductProfile(BaseModel):
    """Curated TPP for one antigen — captures the current clinical-development
    benchmark a new program needs to meet or beat. Lives in data/tpp/{slug}.yaml,
    hand-curated. The biology layer (Open Targets) feeds the synthesis prompt;
    the TPP feeds the explicit benchmark section on the scorecard.
    """

    indication: str  # "HER2+ metastatic breast cancer (2L+)"
    leading_modality: Modality
    leading_drug: str  # "Trastuzumab deruxtecan (T-DXd / Enhertu)"
    leading_sponsor: str  # "Daiichi-Sankyo / AstraZeneca"
    mechanism: str  # one-line, plain English

    pivotal_trial: str | None = None  # "DESTINY-Breast04 (NCT03734029)"
    pivotal_trial_url: str | None = None

    primary_endpoints: list[TPPEndpoint] = Field(default_factory=list)
    secondary_endpoints: list[TPPEndpoint] = Field(default_factory=list)

    safety: TPPSafety
    dosing: TPPDosing

    differentiation: list[str] = Field(default_factory=list)  # bullets
    unmet_need: list[str] = Field(default_factory=list)  # bullets
    competitive_pressure: list[str] = Field(default_factory=list)  # bullets

    last_curated: date
    curator_notes: str | None = None


# ---- Catalysts + timeline (v0.3) ----------------------------------------------


CatalystKind = Literal[
    "trial_completion",  # CT.gov primary completion date
    "conference",        # ASCO / AACR / ESMO / SITC / ASH — relevant data drop window
    "readout_guidance",  # company-guided "topline expected Q3" from news / filings
]


class Catalyst(BaseModel):
    """A forward-looking event that will change the antigen landscape.

    Sourced from CT.gov primary completion dates, curated conference calendar,
    and (future) regex/LLM extraction of company guidance from news + filings.
    Kept distinct from TimelineEvent because catalysts are anticipatory; the
    timeline is retrospective.
    """

    kind: CatalystKind
    date: date  # event date (or anticipated)
    title: str
    detail: str | None = None  # e.g., "Phase 3 · trastuzumab deruxtecan · NSCLC"
    url: str | None = None  # link to trial / conference / press release
    sponsor: str | None = None
    is_anticipated: bool = False  # True for ESTIMATED PCDs or guidance windows


TimelineEventKind = Literal[
    "news",
    "filing",
    "approval",
    "abstract",
    "preprint",
    "grant",
    "trial_update",
]


class TimelineEvent(BaseModel):
    """A retrospective event — the historical record of the antigen.

    Built by aggregating news, filings, approvals, abstracts, preprints, grants,
    and trial updates from the current snapshot. Snapshot DB dedupes across
    refreshes so events don't drop off when RSS feeds roll over.
    """

    kind: TimelineEventKind
    date: date
    title: str
    source: str | None = None  # e.g., "FierceBiotech", "FDA", "EDGAR · 8-K"
    url: str | None = None
    detail: str | None = None  # optional subtitle (sponsor, phase, etc.)


# ---- Top-level antigen page data ----------------------------------------------


class AntigenData(BaseModel):
    """Everything needed to render one antigen scorecard. Committed to data/{slug}.json.

    Synthesis output and TPP are NOT included here. Synthesis is a dist/ build
    artifact (decision 1B). TPP is loaded from data/tpp/{slug}.yaml and merged
    at render time.
    """

    antigen: Antigen
    programs: list[Program] = Field(default_factory=list)
    trials: list[Trial] = Field(default_factory=list)
    papers: list[Paper] = Field(default_factory=list)
    filings: list[Filing] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    freshness: list[SourceFreshness] = Field(default_factory=list)
    open_targets: OpenTargetsData | None = None
    news: list[NewsItem] = Field(default_factory=list)
    fda_approvals: list[FDAApproval] = Field(default_factory=list)
    ema_approvals: list[EMAApproval] = Field(default_factory=list)
    abstracts: list[ConferenceAbstract] = Field(default_factory=list)
    preprints: list[Preprint] = Field(default_factory=list)
    grants: list[Grant] = Field(default_factory=list)
    generated_at: datetime


# ---- Synthesis output (LLM, structured-output contract) -----------------------


class SynthSentence(BaseModel):
    """One sentence of the synthesis paragraph. Every sentence SHOULD cite at
    least one citation_id; the renderer drops sentences whose citations are
    empty or orphaned. We don't enforce min_length at the Pydantic boundary
    because a single bad sentence shouldn't fail the whole synthesis batch
    (validate_against_citations handles the drop downstream)."""

    text: str
    citation_ids: list[int] = Field(default_factory=list)


class SynthParagraph(BaseModel):
    sentences: list[SynthSentence]


class SynthOutput(BaseModel):
    """Structured synthesis. The LLM returns this JSON; the renderer assembles
    the prose deterministically and emits citation marks as a function of the
    citation_ids array. Free-prose output is rejected at the Pydantic boundary.
    """

    paragraphs: list[SynthParagraph]


# ---- v0.3.1 LLM narrative outputs (catalyst pick + modality + index intro) ----


class CatalystPick(BaseModel):
    """One of the top-N picks the LLM made from the antigen's catalyst list.

    `index` is the 0-based offset into the catalysts list passed to the LLM.
    `reason` is a single sentence — why this readout matters for BD readers.
    """

    index: int = Field(ge=0)
    reason: str


class CatalystPicks(BaseModel):
    picks: list[CatalystPick]


class CatalystAnnotation(BaseModel):
    """A one-line "what to watch for" for a specific catalyst.

    `index` aligns with the catalysts list; `note` is the short forward look.
    """

    index: int = Field(ge=0)
    note: str


class CatalystAnnotations(BaseModel):
    annotations: list[CatalystAnnotation]


class ModalityNarrative(BaseModel):
    """One short paragraph contextualising a modality's program table.

    Rendered immediately above the modality's table. The renderer drops
    narratives whose modality has no programs after normalization.
    """

    modality: Modality
    text: str


class ModalityNarratives(BaseModel):
    narratives: list[ModalityNarrative]


class IndexCommentary(BaseModel):
    """Cross-antigen editorial paragraph shown above the index page table."""

    text: str
