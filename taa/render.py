"""Render AntigenData → static HTML via Jinja2."""

from collections import Counter
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from taa.schema import AntigenData, Modality, Phase, Program, SynthOutput, TargetProductProfile

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Modality display order for the cross-cut grid (most likely to be active first).
MODALITY_ORDER: list[Modality] = [
    "adc",
    "mab",
    "bispecific",
    "car-t",
    "vaccine",
    "radioligand",
    "other",
]

MODALITY_LABEL: dict[Modality, str] = {
    "adc": "ADC",
    "mab": "mAb",
    "bispecific": "Bispecific",
    "car-t": "CAR-T",
    "vaccine": "Vaccine",
    "radioligand": "Radioligand",
    "other": "Other",
}

# Phase severity (mirrors normalize.py — kept here to avoid a circular import for
# what is essentially display-side data)
_PHASE_RANK: dict[Phase, int] = {
    "preclinical": 0,
    "1": 1,
    "1/2": 2,
    "2": 3,
    "2/3": 4,
    "3": 5,
    "approved": 6,
    "unknown": -1,
}


def make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_antigen(
    data: AntigenData,
    env: Environment | None = None,
    synth: SynthOutput | None = None,
    tpp: TargetProductProfile | None = None,
) -> str:
    """Render one antigen scorecard page.

    The citations footer shows citations referenced by either a Program OR a
    synthesis sentence (synthesis sentences validated against citations in
    taa.synth.validate_against_citations before reaching here).
    """
    env = env or make_env()
    tmpl = env.get_template("antigen.html")

    referenced_ids: set[int] = {cid for p in data.programs for cid in p.citation_ids}
    if synth:
        for para in synth.paragraphs:
            for sent in para.sentences:
                referenced_ids.update(sent.citation_ids)

    visible_citations = sorted(
        (c for c in data.citations if c.id in referenced_ids), key=lambda c: c.id
    )

    return tmpl.render(
        antigen=data.antigen,
        programs=data.programs,
        citations=visible_citations,
        freshness=data.freshness,
        generated_at=data.generated_at,
        modality_summary=_summarize_modalities(data.programs),
        modality_label=MODALITY_LABEL,
        modality_order=MODALITY_ORDER,
        total_sources=len(data.citations),
        synth=synth,
        tpp=tpp,
        open_targets=data.open_targets,
        news=data.news[:8],
        news_total=len(data.news),
        fda_approvals=data.fda_approvals,
        ema_approvals=data.ema_approvals,
        abstracts=data.abstracts[:10],
        abstract_count=len(data.abstracts),
        preprints=data.preprints[:10],
        preprint_count=len(data.preprints),
        grants=data.grants[:10],
        grant_count=len(data.grants),
        paper_count=len(data.papers),
        filing_count=len(data.filings),
    )


def render_index(antigens: list[AntigenData], env: Environment | None = None) -> str:
    env = env or make_env()
    tmpl = env.get_template("index.html")
    return tmpl.render(
        antigens=antigens,
        modality_summary_for=lambda d: _summarize_modalities(d.programs),
        modality_label=MODALITY_LABEL,
    )


def _summarize_modalities(programs: list[Program]) -> list[dict[str, Any]]:
    """Per-modality summary used by the cross-cut grid hero block.

    Returns a list of dicts in MODALITY_ORDER, each with:
      - modality (key)
      - count (number of programs)
      - max_phase (most-advanced phase across this modality's programs)
      - bar_pct (relative bar length, 0..100, longest modality = 100)
    """
    counts: Counter[Modality] = Counter(p.modality for p in programs)
    max_phase_per_modality: dict[Modality, Phase] = {}
    for p in programs:
        cur = max_phase_per_modality.get(p.modality, "unknown")
        if _PHASE_RANK.get(p.most_advanced_phase, -1) > _PHASE_RANK.get(cur, -1):
            max_phase_per_modality[p.modality] = p.most_advanced_phase

    longest = max(counts.values(), default=1)
    summary: list[dict[str, Any]] = []
    for m in MODALITY_ORDER:
        c = counts.get(m, 0)
        if c == 0:
            continue
        summary.append(
            {
                "modality": m,
                "label": MODALITY_LABEL[m],
                "count": c,
                "max_phase": max_phase_per_modality.get(m, "unknown"),
                "bar_pct": int(100 * c / longest),
            }
        )
    return summary
