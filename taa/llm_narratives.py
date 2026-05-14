"""LLM-generated editorial narratives layered on top of the structured data.

Adds four narrative surfaces beyond the main synthesis paragraph (taa/synth.py):

A. **pick_top_catalysts** — choose ~5 highest-impact catalysts from the antigen's
   full list, with one-line reasoning per pick.
B. **narrate_modalities** — short paragraph above each modality's program table
   explaining the competitive state of that modality.
D. **annotate_catalysts** — one-line "what to watch for" attached to every
   catalyst. Batched per-antigen (one LLM call returns all annotations).
E. **index_commentary** — short cross-antigen paragraph for the index page.

Same defensive contract as synth.py: every function returns None on any failure
(missing API key, network error, malformed JSON, schema violation). The
renderer's job is to omit the section gracefully when None is returned.

Pinned to claude-haiku-4-5 for cost / latency. Each call uses Anthropic's
structured-output JSON discipline via prompt — no tool-use, easier to debug.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from taa.schema import (
    AntigenData,
    Catalyst,
    CatalystAnnotations,
    CatalystPicks,
    IndexCommentary,
    ModalityNarratives,
    Program,
    TargetProductProfile,
)
from taa.synth import _extract_first_json_object

MODEL = "claude-haiku-4-5"
MAX_TOKENS_PICKS = 1200
MAX_TOKENS_ANNOTATIONS = 4000  # roughly: 60 catalysts at 60-80 tokens each
MAX_TOKENS_MODALITY = 1500
MAX_TOKENS_INDEX = 800

TOP_CATALYSTS_N = 5


# ---- Common scaffolding -------------------------------------------------------


async def _call_llm(
    system: str,
    user: str,
    max_tokens: int,
    schema_cls: type[BaseModel],
    tag: str,
) -> BaseModel | None:
    """Single LLM call returning a Pydantic-validated object or None on any failure."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    client = AsyncAnthropic()
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        print(f"  [{tag}] API error: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    extracted = _extract_first_json_object(raw)
    if extracted is None:
        print(f"  [{tag}] no JSON in response", file=sys.stderr)
        return None
    try:
        parsed = json.loads(extracted)
        return schema_cls.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as e:
        print(f"  [{tag}] parse failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _catalysts_json(catalysts: list[Catalyst]) -> list[dict[str, Any]]:
    """Compact list of catalysts indexed 0..N-1 for the LLM to reference."""
    return [
        {
            "index": i,
            "kind": c.kind,
            "date": c.date.isoformat(),
            "title": c.title[:200],
            "detail": c.detail,
            "sponsor": c.sponsor,
            "is_anticipated": c.is_anticipated,
        }
        for i, c in enumerate(catalysts)
    ]


# ---- A. Top catalyst picks ---------------------------------------------------


_PICKS_SYSTEM = """You triage a list of upcoming oncology catalysts for a \
sophisticated biotech BD reader. Pick the {n} most landscape-moving events and \
explain in ONE sentence each why a BD reader should care.

Inputs you receive:
- antigen_context: the antigen and its current competitive state
- catalysts: numbered list (with `index`) of candidate events

Score on these signals, in priority order:
1. Probability the readout meaningfully shifts the standard of care (Phase 3 > 2/3 > 2)
2. Sponsor's strategic importance and prior commitment to the antigen
3. Indication unmet need and TAM
4. Novelty of the mechanism vs the existing leader

DROP conference dates from your picks — those are not readouts. Focus on \
trial completions and readout-guidance entries.

Output STRICT JSON:
{
  "picks": [
    {"index": 12, "reason": "<one sentence — concrete, no hedging>"}
  ]
}

Rules:
- Up to {n} picks; fewer is fine if the list is thin.
- `index` must reference an item from the input catalysts.
- Reasons must be specific (drug name, mechanism, what it would prove) — \
no "important", "exciting", "promising", "robust". No em dashes.
- Skip conferences (kind=conference) — pick only trial_completion and \
readout_guidance entries.
"""


async def pick_top_catalysts(
    antigen_name: str,
    indication_tags: list[str],
    catalysts: list[Catalyst],
    tpp: TargetProductProfile | None,
    programs: list[Program],
) -> CatalystPicks | None:
    """Return a CatalystPicks with up to N top-impact catalysts."""
    eligible = [c for c in catalysts if c.kind != "conference"]
    if len(eligible) < 2:
        return None  # no value picking from a one-element list

    payload = {
        "antigen_context": {
            "name": antigen_name,
            "indications": indication_tags,
            "current_benchmark": (
                {
                    "leader": tpp.leading_drug,
                    "leader_sponsor": tpp.leading_sponsor,
                    "leader_modality": tpp.leading_modality,
                    "indication": tpp.indication,
                }
                if tpp
                else None
            ),
            "program_summary_by_modality": _program_summary(programs),
        },
        "catalysts": _catalysts_json(catalysts),
    }
    system = _PICKS_SYSTEM.replace("{n}", str(TOP_CATALYSTS_N))
    return await _call_llm(  # type: ignore[return-value]
        system=system,
        user=json.dumps(payload, indent=2),
        max_tokens=MAX_TOKENS_PICKS,
        schema_cls=CatalystPicks,
        tag=f"picks {antigen_name}",
    )


# ---- D. Per-catalyst annotation (batched) ------------------------------------


_ANNOTATE_SYSTEM = """You attach a one-line forward-looking note to each \
upcoming catalyst. Reader is a biotech BD analyst scanning the list.

For each catalyst in the input list, emit ONE short sentence (max ~24 words) \
explaining what to watch for at that event.

Output STRICT JSON:
{
  "annotations": [
    {"index": 0, "note": "<one sentence about what to watch>"}
  ]
}

Rules:
- Cover every input catalyst (one annotation per `index`), unless an event has \
genuinely no clinical implication.
- Be specific: name the endpoint, comparator, line of therapy, or signal that \
will be visible. Don't write "data readout" — say what data.
- Conferences: name the type of session and what to expect (ADC late-breakers, \
bispecific oral presentations, etc.).
- No marketing prose. No "important", "exciting", "robust". No em dashes."""


async def annotate_catalysts(
    antigen_name: str,
    indication_tags: list[str],
    catalysts: list[Catalyst],
    tpp: TargetProductProfile | None,
) -> CatalystAnnotations | None:
    """One-line annotation per catalyst, batched as a single LLM call."""
    if not catalysts:
        return None
    payload = {
        "antigen": antigen_name,
        "indications": indication_tags,
        "benchmark": (
            {"leader": tpp.leading_drug, "sponsor": tpp.leading_sponsor}
            if tpp
            else None
        ),
        "catalysts": _catalysts_json(catalysts),
    }
    return await _call_llm(  # type: ignore[return-value]
        system=_ANNOTATE_SYSTEM,
        user=json.dumps(payload, indent=2),
        max_tokens=MAX_TOKENS_ANNOTATIONS,
        schema_cls=CatalystAnnotations,
        tag=f"annotate {antigen_name}",
    )


# ---- B. Per-modality narrative ------------------------------------------------


_MODALITY_SYSTEM = """You write a one- or two-sentence editorial paragraph for \
each modality (ADC, mAb, bispecific, CAR-T, vaccine, radioligand) targeting one \
TAA. Reader is a biotech BD analyst.

For each modality present in the data, name the entrenched leader, the most \
credible challengers, the differentiation that matters, and any obvious \
liabilities or whitespace. Specific. Concrete. No hedging.

Output STRICT JSON:
{
  "narratives": [
    {"modality": "adc", "text": "<one or two sentences>"}
  ]
}

Rules:
- Cover every modality in `programs_by_modality` that has >= 1 program.
- Use drug names from the input. Use trial counts. Use sponsor names.
- Phase-3 / approved programs are the anchor. Don't write generically about \
preclinical noise.
- No marketing prose. No "exciting", "robust", "promising". No em dashes."""


async def narrate_modalities(
    antigen_name: str,
    programs: list[Program],
    tpp: TargetProductProfile | None,
) -> ModalityNarratives | None:
    """Short paragraph per modality. Skipped if no programs."""
    if not programs:
        return None
    payload = {
        "antigen": antigen_name,
        "benchmark": (
            {
                "leader": tpp.leading_drug,
                "leader_sponsor": tpp.leading_sponsor,
                "leader_modality": tpp.leading_modality,
            }
            if tpp
            else None
        ),
        "programs_by_modality": _programs_by_modality(programs),
    }
    return await _call_llm(  # type: ignore[return-value]
        system=_MODALITY_SYSTEM,
        user=json.dumps(payload, indent=2),
        max_tokens=MAX_TOKENS_MODALITY,
        schema_cls=ModalityNarratives,
        tag=f"modality {antigen_name}",
    )


# ---- E. Index-page cross-antigen commentary ----------------------------------


_INDEX_SYSTEM = """You write a single short editorial paragraph (3-5 sentences) \
introducing a competitive-intelligence dashboard covering several tumour- \
associated antigens. Reader is a biotech BD analyst — surface what's most \
interesting across the antigens this week.

You receive per-antigen summary stats: program counts by modality, top \
phase-3 drug, top sponsor.

Output STRICT JSON:
{
  "text": "<a single paragraph of editorial prose>"
}

Rules:
- Lead with the most interesting cross-cutting observation: which antigens \
are heating up, who's investing across multiple antigens, which modalities \
are crowded vs whitespace.
- Use specific numbers and names from the input.
- 3-5 sentences total. No paragraphs, no lists, no headings.
- No marketing prose. No em dashes. No hedging."""


async def index_commentary(antigens: list[AntigenData]) -> IndexCommentary | None:
    """Cross-antigen editorial paragraph for the index page."""
    if not antigens:
        return None
    payload = {
        "antigens": [
            {
                "name": d.antigen.primary_name,
                "indications": d.antigen.indication_tags,
                "program_count": len(d.programs),
                "by_modality": _program_summary(d.programs),
                "phase3_leaders": [
                    {
                        "drug": p.canonical_drug,
                        "modality": p.modality,
                        "sponsor_lead": p.sponsors[0] if p.sponsors else None,
                        "phase": p.most_advanced_phase,
                    }
                    for p in d.programs
                    if p.most_advanced_phase in ("3", "approved")
                ][:5],
            }
            for d in antigens
        ]
    }
    return await _call_llm(  # type: ignore[return-value]
        system=_INDEX_SYSTEM,
        user=json.dumps(payload, indent=2),
        max_tokens=MAX_TOKENS_INDEX,
        schema_cls=IndexCommentary,
        tag="index",
    )


# ---- Helpers ------------------------------------------------------------------


def _program_summary(programs: list[Program]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in programs:
        out[p.modality] = out.get(p.modality, 0) + 1
    return out


def _programs_by_modality(programs: list[Program]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for p in programs:
        out.setdefault(p.modality, []).append(
            {
                "drug": p.canonical_drug,
                "phase": p.most_advanced_phase,
                "status": p.status,
                "lead_sponsor": p.sponsors[0] if p.sponsors else None,
                "trial_count": p.trial_count,
            }
        )
    for m in out:
        out[m].sort(key=lambda p: (-_phase_rank(p["phase"]), -p["trial_count"]))
    return out


_PHASE_RANK_MAP: dict[str, int] = {
    "approved": 7, "3": 6, "2/3": 5, "2": 4, "1/2": 3, "1": 2,
    "preclinical": 1, "unknown": 0,
}


def _phase_rank(phase: str) -> int:
    return _PHASE_RANK_MAP.get(phase, 0)
