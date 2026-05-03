"""Pydantic models for the TAA scorecard data layer.

All structured data flows through these. Synthesis output (LLM-generated prose)
is a separate concern handled in taa/synth.py and lives in dist/, not data/.
"""

from datetime import date, datetime
from typing import Literal

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

    source: Literal["ctgov", "pubmed", "openalex", "edgar"]
    last_success: datetime | None = None
    last_attempt: datetime
    error: str | None = None  # populated if last_attempt failed

    @property
    def is_stale(self) -> bool:
        """Stale if the last attempt failed, regardless of when last success was."""
        return self.error is not None


# ---- Top-level antigen page data ----------------------------------------------


class AntigenData(BaseModel):
    """Everything needed to render one antigen scorecard. Committed to data/{slug}.json.

    Synthesis output is NOT included here — it's a build artifact in dist/, generated
    fresh from this structured data on every render to avoid git diff churn.
    """

    antigen: Antigen
    programs: list[Program] = Field(default_factory=list)
    trials: list[Trial] = Field(default_factory=list)
    papers: list[Paper] = Field(default_factory=list)
    filings: list[Filing] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    freshness: list[SourceFreshness] = Field(default_factory=list)
    generated_at: datetime


# ---- Synthesis output (LLM, structured-output contract) -----------------------


class SynthSentence(BaseModel):
    """One sentence of the synthesis paragraph. Every sentence MUST cite at least
    one citation_id, validated by the renderer. Sentences with orphan or empty
    citations are dropped before render (per /plan-eng-review decision)."""

    text: str
    citation_ids: list[int] = Field(min_length=1)


class SynthParagraph(BaseModel):
    sentences: list[SynthSentence]


class SynthOutput(BaseModel):
    """Structured synthesis. The LLM returns this JSON; the renderer assembles
    the prose deterministically and emits citation marks as a function of the
    citation_ids array. Free-prose output is rejected at the Pydantic boundary.
    """

    paragraphs: list[SynthParagraph]
