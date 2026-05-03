"""Normalize raw source rows into Programs (one row per company × modality × antigen).

Strategy (decision 1E from /plan-eng-review):
1. Per-antigen alias regex matching (already done by source clients via OR query).
2. Per-antigen exclude_terms filter (this module) — drops false positives.
3. Modality assignment from data/drug_modality.yaml — known drug → modality mapping.
4. Unknown drugs go to the "other" bucket; bin/audit-matches will surface them.
"""

import re
from collections import Counter, defaultdict
from typing import Any

from taa.schema import Antigen, Modality, Phase, Program, Trial

# Phase severity for "most-advanced phase" rollup
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


def filter_excluded(trials: list[Trial], antigen: Antigen) -> list[Trial]:
    """Drop trials whose title or interventions match any per-antigen exclude term.

    Case-insensitive substring match. Conservative: if a single exclude term
    matches anywhere in the title or any intervention, the trial is dropped.
    """
    if not antigen.exclude_terms:
        return trials

    excludes = [re.compile(re.escape(t), re.IGNORECASE) for t in antigen.exclude_terms]
    kept: list[Trial] = []
    for trial in trials:
        haystack = " | ".join([trial.title, *trial.interventions])
        if any(pat.search(haystack) for pat in excludes):
            continue
        kept.append(trial)
    return kept


def match_canonical_drug(
    drug_name: str, modality_map: dict[str, Modality]
) -> tuple[str, Modality] | None:
    """Look up a drug name and return (canonical_key, modality) if it matches.

    Returns None if unknown. The canonical_key is the YAML map key (e.g.,
    "Trastuzumab", "T-DXd"), used to roll up trial-sponsor variants into one
    Program. Match is case-insensitive substring; first hit wins, so order
    drug_modality.yaml most-specific first ("trastuzumab deruxtecan" before
    "trastuzumab" so the ADC is picked before the bare mAb).
    """
    drug_lc = drug_name.lower()
    for known, modality in modality_map.items():
        if known.lower() in drug_lc:
            return known, modality
    return None


def trials_to_programs(
    trials: list[Trial],
    antigen: Antigen,
    modality_map: dict[str, Modality],
) -> tuple[list[Program], list[str]]:
    """Roll up trials → one Program per (canonical_drug, modality).

    Only interventions that match the curated modality_map become Programs.
    Unknowns (combo chemo partners, control arms, supportive care drugs) are
    dropped from Programs but returned as `unknown_interventions` so
    bin/audit-matches can surface them for one-key curation.

    The (canonical_drug, modality) rollup means trial-sponsor variants collapse
    into one Program. Sponsors are aggregated across all trials with that drug,
    sorted by trial count descending (the originator usually leads).

    Phase reflects the most-advanced phase across all rolled-up trials. Status
    is "active" if any trial is active, else "terminated" if any was terminated,
    else "unknown".
    """
    # group_key = (canonical_drug, modality)
    groups: dict[tuple[str, Modality], list[Trial]] = defaultdict(list)
    sponsor_trials: dict[tuple[str, Modality], Counter[str]] = defaultdict(Counter)
    unknown_interventions: set[str] = set()

    for trial in trials:
        sponsor = trial.sponsors[0] if trial.sponsors and trial.sponsors[0] else "Unknown"
        for intervention in trial.interventions or []:
            match = match_canonical_drug(intervention, modality_map)
            if match is None:
                unknown_interventions.add(intervention)
                continue
            canonical_drug, modality = match
            key = (canonical_drug, modality)
            groups[key].append(trial)
            sponsor_trials[key][sponsor] += 1

    programs: list[Program] = []
    for (canonical_drug, modality), grp in groups.items():
        phase = _max_phase([t.phase for t in grp])
        any_active = any(t.status == "active" for t in grp)
        any_term = any(t.status in ("terminated", "withdrawn") for t in grp)
        status = "active" if any_active else ("terminated" if any_term else "unknown")
        latest = max((t.last_update for t in grp), default=None)
        # Sponsors: most-trials first (the originator usually has the most trials)
        sponsors = [s for s, _ in sponsor_trials[(canonical_drug, modality)].most_common()]
        # Citations: dedupe, then cap to top 6 most-recent for per-row display
        all_cites = sorted({cid for t in grp for cid in t.citation_ids})
        cite_ids = all_cites[:6]  # render cap; full list still lives in page footer

        programs.append(
            Program(
                antigen_slug=antigen.slug,
                modality=modality,
                canonical_drug=canonical_drug,
                sponsors=sponsors,
                trial_count=len(grp),
                most_advanced_phase=phase,
                status=status,  # type: ignore[arg-type]
                latest_update=latest,
                citation_ids=cite_ids,
            )
        )

    # Sort: most-advanced phase desc, then trial_count desc (popularity), then drug name.
    sorted_programs = sorted(
        programs,
        key=lambda p: (
            -_PHASE_RANK.get(p.most_advanced_phase, -1),
            -p.trial_count,
            p.canonical_drug.lower(),
        ),
    )
    return sorted_programs, sorted(unknown_interventions)


def _max_phase(phases: list[Phase]) -> Phase:
    """Pick the most-advanced phase across a set of trials."""
    if not phases:
        return "unknown"
    return max(phases, key=lambda p: _PHASE_RANK.get(p, -1))


def load_modality_map(yaml_data: dict[str, Any]) -> dict[str, Modality]:
    """Convert drug_modality.yaml → flat dict[drug_name → modality].

    YAML structure:
      adc:
        - "T-DXd"
        - "Trastuzumab deruxtecan"
      bispecific:
        - "Zanidatamab"
    """
    flat: dict[str, Modality] = {}
    for modality, drugs in yaml_data.items():
        if modality not in (
            "adc",
            "car-t",
            "bispecific",
            "mab",
            "radioligand",
            "vaccine",
            "other",
        ):
            continue
        for drug in drugs or []:
            flat[drug] = modality  # type: ignore[assignment]
    return flat
