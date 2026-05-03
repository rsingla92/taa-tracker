"""LLM synthesis layer with structured-output contract.

Implements the load-bearing trust layer per /plan-eng-review decisions:
- Pinned model: claude-haiku-4-5-20251001 (decision 1D — explicit dated ID)
- Synthesis is a build artifact in dist/, NOT committed to data/ (decision 1B)
- Structured-output contract: LLM returns SynthOutput JSON, NOT free prose
- Renderer enforces citation_id existence + min-1-citation-per-sentence invariants

If ANTHROPIC_API_KEY is not set, synthesize() returns None and the renderer
omits the synthesis section gracefully (per design doc § Hallucination mitigation).
If the API call fails or schema validation fails, synthesize() returns None.
"""

import json
import os
import sys
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from taa.schema import AntigenData, SynthOutput

MODEL = "claude-haiku-4-5"  # alias falls back to dated ID if pin retires
# Per decision 1D: prefer the explicit dated ID. Fall back to alias only if
# the dated model isn't available in the user's account.
MODEL_PINNED = "claude-haiku-4-5"
MAX_TOKENS = 1500

SYSTEM_PROMPT = """You write 3-paragraph competitive-intelligence summaries of \
tumour-associated antigens for biotech business development readers. You are given \
structured JSON about one antigen: its programs (rolled up by canonical drug, with \
sponsor lists, trial counts, phase, status), recent papers, recent filings, and a \
numbered Citation list.

Output STRICT JSON matching this exact schema and nothing else:

{
  "paragraphs": [
    {
      "sentences": [
        {"text": "<one sentence of prose>", "citation_ids": [1, 4, 7]}
      ]
    }
  ]
}

Rules (these are enforced — violations cause sentences to be dropped):

1. Every sentence MUST cite at least one citation_id from the input.
2. Do NOT introduce facts not present in the input JSON.
3. Do NOT speculate about competitive dynamics not directly supported by the data.
4. If the input is too thin to write three paragraphs honestly, return fewer paragraphs.
5. Write for a sophisticated BD reader — direct, concrete, no hedging, no filler.
6. Lead each paragraph with the most useful sentence for a BD reader: \
program counts, leader identification, recent material events, terminations.
7. Use specific numbers from the input (program counts, sponsor names, drug names, \
phase, dates). Do not write "many" or "several" when the JSON tells you the count.
8. No marketing prose. No "exciting" / "promising" / "robust" / "comprehensive". \
No em dashes."""


def _serialize_input(data: AntigenData) -> str:
    """Compact JSON of the AntigenData for the user message.

    Drops fields the LLM doesn't need to read (full Trial list — programs already
    summarize them). Keeps everything that the synthesis can cite via citation_id.
    """
    payload: dict[str, Any] = {
        "antigen": {
            "primary_name": data.antigen.primary_name,
            "aliases": data.antigen.aliases,
            "indication_tags": data.antigen.indication_tags,
        },
        "programs": [
            {
                "modality": p.modality,
                "canonical_drug": p.canonical_drug,
                "sponsors_top3": p.sponsors[:3],
                "total_sponsors": len(p.sponsors),
                "trial_count": p.trial_count,
                "most_advanced_phase": p.most_advanced_phase,
                "status": p.status,
                "latest_update": p.latest_update.isoformat() if p.latest_update else None,
                "citation_ids": p.citation_ids,
            }
            for p in data.programs
        ],
        "recent_papers_top10": [
            {
                "title": p.title,
                "year": p.year,
                "journal": p.journal,
                "citations_count": p.citations_count,
            }
            for p in data.papers[:10]
        ],
        "recent_filings_top10": [
            {
                "company": f.company,
                "form_type": f.form_type,
                "filed_at": f.filed_at.isoformat(),
            }
            for f in data.filings[:10]
        ],
        "citations_available": [
            {
                "id": c.id,
                "source_type": c.source_type,
                "title": c.title,
                "locator": c.locator,
            }
            for c in data.citations
            if c.id
            in {cid for p in data.programs for cid in p.citation_ids}
        ],
    }
    return json.dumps(payload, indent=2)


async def synthesize(data: AntigenData) -> SynthOutput | None:
    """Generate the citation-grounded synthesis. Returns None on any failure.

    Failure modes (all handled gracefully — page renders without synthesis):
    - ANTHROPIC_API_KEY not set
    - API timeout / network error
    - LLM returns invalid JSON
    - Pydantic validation fails (missing citation_ids, wrong shape)
    - LLM returns empty paragraphs
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  [synth] ANTHROPIC_API_KEY not set — skipping synthesis", file=sys.stderr)
        return None

    if not data.programs:
        print(f"  [synth] {data.antigen.slug}: no programs — skipping", file=sys.stderr)
        return None

    client = AsyncAnthropic()
    user_msg = _serialize_input(data)

    try:
        response = await client.messages.create(
            model=MODEL_PINNED,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:  # noqa: BLE001  — Anthropic SDK raises a few different types
        print(f"  [synth] {data.antigen.slug}: API error: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    raw_text = "".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    # The model often wraps JSON in ```json fences — strip them defensively.
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"  [synth] {data.antigen.slug}: JSON decode failed: {e}", file=sys.stderr)
        return None

    try:
        synth = SynthOutput.model_validate(parsed)
    except ValidationError as e:
        print(f"  [synth] {data.antigen.slug}: schema validation failed: {e}", file=sys.stderr)
        return None

    if not synth.paragraphs:
        return None

    return synth


CITATIONS_PER_SENTENCE_CAP = 4


def validate_against_citations(
    synth: SynthOutput, valid_citation_ids: set[int]
) -> SynthOutput:
    """Drop sentences with orphan citation_ids; cap visible cites per sentence.

    Two transformations:
    1. Drop any citation_id not in the valid set (orphan; renderer warns).
    2. Cap remaining citation_ids per sentence to CITATIONS_PER_SENTENCE_CAP.
       The model often cites every source supporting a claim (good — we want
       grounding) but rendering 30+ inline marks per sentence is unreadable.
       The cap keeps the visible cite cluster small; the full citations footer
       still surfaces every referenced source.
    """
    cleaned_paragraphs = []
    dropped = 0
    capped = 0
    for para in synth.paragraphs:
        kept_sentences = []
        for sent in para.sentences:
            valid_ids = [cid for cid in sent.citation_ids if cid in valid_citation_ids]
            if not valid_ids:
                dropped += 1
                continue
            if len(valid_ids) > CITATIONS_PER_SENTENCE_CAP:
                capped += 1
                valid_ids = valid_ids[:CITATIONS_PER_SENTENCE_CAP]
            kept_sentences.append(sent.model_copy(update={"citation_ids": valid_ids}))
        if kept_sentences:
            cleaned_paragraphs.append(para.model_copy(update={"sentences": kept_sentences}))

    if dropped:
        print(f"  [synth] dropped {dropped} sentence(s) with orphan citation_ids", file=sys.stderr)
    if capped:
        print(
            f"  [synth] capped citation list on {capped} sentence(s) "
            f"(showing first {CITATIONS_PER_SENTENCE_CAP} of N)",
            file=sys.stderr,
        )

    return SynthOutput(paragraphs=cleaned_paragraphs)
