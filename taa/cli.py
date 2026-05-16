"""CLI entry points.

`taa-refresh` — pull all 4 sources concurrently, normalize, synthesize, render.
`taa-audit`   — sample-and-print spot-check tool (v0.2 stub for now).
"""

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()  # Pick up ANTHROPIC_API_KEY, NCBI_API_KEY, EDGAR_USER_AGENT from .env

from taa.catalysts import extract_catalysts
from taa.llm_narratives import (
    annotate_catalysts,
    index_commentary,
    narrate_modalities,
    pick_top_catalysts,
)
from taa.normalize import (
    drug_aliases_for_antigen,
    filter_excluded,
    load_canonical_aliases,
    load_drug_antigens,
    load_modality_map,
    trials_to_programs,
)
from taa.render import make_env, render_antigen, render_index
from taa.schema import Antigen, AntigenData, TargetProductProfile
from taa.snapshots import (
    events_from_antigen_data,
    load_timeline,
    open_db,
    record_snapshot,
    upsert_timeline_events,
)
from taa.sources import (
    ctgov,
    edgar,
    ema,
    europepmc,
    fda,
    news,
    openalex,
    opentargets,
    pubmed,
    reporter,
)
from taa.synth import synthesize, validate_against_citations

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DIST_DIR = ROOT / "dist"
STATIC_DIR = ROOT / "static"


def _load_antigens() -> list[Antigen]:
    raw = yaml.safe_load((DATA_DIR / "antigens.yaml").read_text())
    return [Antigen(**entry) for entry in raw]


def _load_modality_map() -> dict[str, str]:
    raw = yaml.safe_load((DATA_DIR / "drug_modality.yaml").read_text())
    return load_modality_map(raw)  # type: ignore[return-value]


def _load_canonical_aliases() -> dict[str, str]:
    raw = yaml.safe_load((DATA_DIR / "drug_modality.yaml").read_text())
    return load_canonical_aliases(raw)


def _load_drug_antigens() -> dict[str, str]:
    raw = yaml.safe_load((DATA_DIR / "drug_modality.yaml").read_text())
    return load_drug_antigens(raw)


def _coverage_audit(
    antigen_slug: str,
    expected_drugs: list[str],
    trials: list,
) -> list[str]:
    """Return drugs in `expected_drugs` that no retrieved trial references.

    A drug is considered "covered" if it appears as a case-insensitive substring
    of any trial's title or any of its interventions. Drugs with zero hits are
    surfaced as a hard signal that the CT.gov query didn't find anything for a
    drug we curated — typically a licensing relabel (Hansoh → GSK) or an
    intentionally-preclinical entry. Both deserve human attention.
    """
    missing: list[str] = []
    haystacks = [
        " | ".join([t.title, *t.interventions]).lower() for t in trials
    ]
    for drug in expected_drugs:
        needle = drug.lower()
        if not any(needle in h for h in haystacks):
            missing.append(drug)
    return missing


async def _refresh_one(antigen: Antigen) -> AntigenData:
    """Pull all 4 sources concurrently, normalize, return AntigenData.

    Citation IDs are page-local and assigned in source order so that:
    - CT.gov citations occupy IDs 1..N
    - PubMed continues from N+1
    - OpenAlex continues from M+1
    - EDGAR fills the tail
    Each source's `fetch()` accepts `citation_id_start` so numbering stays unique.
    """
    print(f"  fetching {antigen.primary_name}…", file=sys.stderr)

    drug_antigens = _load_drug_antigens()
    extra_drug_aliases = drug_aliases_for_antigen(drug_antigens, antigen.slug)

    # Per-source semaphores live in each module — these tasks all share the asyncio
    # event loop and respect their own rate limits independently. Citation ID ranges
    # keep page-local IDs unique across sources without coordination.
    ctgov_task = asyncio.create_task(
        ctgov.fetch(antigen, citation_id_start=1, extra_drug_aliases=extra_drug_aliases)
    )
    pubmed_task = asyncio.create_task(pubmed.fetch(antigen, citation_id_start=10000))
    abstracts_task = asyncio.create_task(
        pubmed.fetch_conference_abstracts(antigen, citation_id_start=15000)
    )
    openalex_task = asyncio.create_task(openalex.fetch(antigen, citation_id_start=20000))
    edgar_task = asyncio.create_task(edgar.fetch(antigen, citation_id_start=30000))
    ot_task = asyncio.create_task(opentargets.fetch(antigen, citation_id_start=40000))
    news_task = asyncio.create_task(news.fetch(antigen, citation_id_start=50000))
    preprints_task = asyncio.create_task(europepmc.fetch(antigen, citation_id_start=80000))
    grants_task = asyncio.create_task(reporter.fetch(antigen, citation_id_start=90000))

    ctgov_r = await ctgov_task
    pubmed_r = await pubmed_task
    abstracts_r = await abstracts_task
    openalex_r = await openalex_task
    edgar_r = await edgar_task
    ot_r = await ot_task
    news_r = await news_task
    preprints_r = await preprints_task
    grants_r = await grants_task

    trials = filter_excluded(ctgov_r.trials, antigen)
    modality_map = _load_modality_map()
    aliases = _load_canonical_aliases()
    programs, unknowns = trials_to_programs(
        trials, antigen, modality_map, aliases, drug_antigens  # type: ignore[arg-type]
    )

    if unknowns:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"_unknowns-{antigen.slug}.txt").write_text("\n".join(unknowns) + "\n")

    # Coverage audit: every drug curated as targeting this antigen should have
    # at least one trial mentioning it. Zero-hit drugs typically mean a
    # licensing relabel (e.g., Hansoh → GSK) where the canonical codename
    # changed on CT.gov, or a preclinical drug with no registered trial.
    # Either way the human curator needs to know — silent dropouts are how
    # we missed HS-20089 (B7-H4 / GSK) and HS-20093 (B7-H3 / Hansoh) in v0.3.
    missing_drugs = _coverage_audit(antigen.slug, extra_drug_aliases, ctgov_r.trials)
    if missing_drugs:
        (DATA_DIR / f"_coverage-{antigen.slug}.txt").write_text(
            "# Drugs in drug_antigens[{slug}] with zero CT.gov hits.\n"
            "# Resolve by adding the new alias to drug_modality.yaml + drug_antigens,\n"
            "# or by removing the entry if the program is dead/preclinical.\n".format(
                slug=antigen.slug
            )
            + "\n".join(missing_drugs)
            + "\n"
        )
        print(
            f"  [coverage] {antigen.slug}: {len(missing_drugs)} curated drug(s) "
            f"with zero CT.gov hits → data/_coverage-{antigen.slug}.txt",
            file=sys.stderr,
        )
    else:
        # Clean up a stale coverage file from a previous run.
        cov_file = DATA_DIR / f"_coverage-{antigen.slug}.txt"
        if cov_file.exists():
            cov_file.unlink()

    # FDA + EMA: query against the curated drug list (fast, deterministic)
    drug_names = [d.canonical_drug for d in programs if d.canonical_drug]
    fda_r = await fda.fetch_for_drugs(antigen, drug_names, citation_id_start=60000)
    ema_r = await ema.fetch_for_drugs(antigen, drug_names, citation_id_start=70000)

    return AntigenData(
        antigen=antigen,
        programs=programs,
        trials=trials,
        papers=pubmed_r.papers + openalex_r.papers,
        filings=edgar_r.filings,
        citations=ctgov_r.citations
        + pubmed_r.citations
        + abstracts_r.citations
        + openalex_r.citations
        + edgar_r.citations
        + ot_r.citations
        + news_r.citations
        + fda_r.citations
        + ema_r.citations
        + preprints_r.citations
        + grants_r.citations,
        freshness=[
            ctgov_r.freshness,
            pubmed_r.freshness,
            abstracts_r.freshness,
            openalex_r.freshness,
            edgar_r.freshness,
            ot_r.freshness,
            news_r.freshness,
            fda_r.freshness,
            ema_r.freshness,
            preprints_r.freshness,
            grants_r.freshness,
        ],
        open_targets=ot_r.data,
        news=news_r.items,
        fda_approvals=fda_r.approvals,
        ema_approvals=ema_r.approvals,
        abstracts=abstracts_r.abstracts,
        preprints=preprints_r.preprints,
        grants=grants_r.grants,
        generated_at=datetime.now(timezone.utc),
    )


def _load_tpp(slug: str) -> TargetProductProfile | None:
    """Load curated TPP from data/tpp/{slug}.yaml; None if not curated yet."""
    tpp_file = DATA_DIR / "tpp" / f"{slug}.yaml"
    if not tpp_file.exists():
        return None
    try:
        raw = yaml.safe_load(tpp_file.read_text())
        return TargetProductProfile.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        print(f"  [tpp] {slug}: failed to load TPP: {e}", file=sys.stderr)
        return None


async def _refresh_all() -> None:
    antigens = _load_antigens()
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # Copy static assets to dist/
    static_out = DIST_DIR / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for asset in STATIC_DIR.glob("*"):
        (static_out / asset.name).write_bytes(asset.read_bytes())

    env = make_env()
    rendered: list[AntigenData] = []

    # v0.3 snapshot DB — opened once per refresh, records the run, accumulates
    # the deduped historical timeline across runs.
    snapshot_db = open_db(DATA_DIR / "snapshots.db")
    snapshot_id = record_snapshot(snapshot_db, [a.slug for a in antigens])
    conferences_path = DATA_DIR / "conferences.yaml"
    today = datetime.now(timezone.utc).date()

    for antigen in antigens:
        data = await _refresh_one(antigen)

        # Persist structured data (synthesis is NOT included — built fresh per render
        # per /plan-eng-review decision 1B)
        (DATA_DIR / f"{antigen.slug}.json").write_text(data.model_dump_json(indent=2))

        # Synthesize (LLM call). None on any failure — page renders without the section.
        synth = await synthesize(data)
        if synth:
            valid_ids = {c.id for c in data.citations}
            synth = validate_against_citations(synth, valid_ids)
            print(
                f"  [synth] {antigen.slug}: {sum(len(p.sentences) for p in synth.paragraphs)} sentences kept",
                file=sys.stderr,
            )

        # Load curated TPP if present
        tpp = _load_tpp(antigen.slug)

        # v0.3: project this run's events into the snapshot DB and load the
        # deduped historical timeline back (current run + all priors).
        events = events_from_antigen_data(data)
        new_count = upsert_timeline_events(snapshot_db, antigen.slug, events, snapshot_id)
        timeline = load_timeline(snapshot_db, antigen.slug)
        catalysts = extract_catalysts(
            antigen,
            data.trials,
            data.news,
            conferences_path,
            today,
            abstracts=data.abstracts,
        )

        # v0.3.1: LLM editorial layer — top picks, per-catalyst notes,
        # per-modality narratives. Each returns None on any failure; renderer
        # omits the section gracefully.
        picks = await pick_top_catalysts(
            antigen.primary_name, antigen.indication_tags, catalysts, tpp, data.programs
        )
        annotations = await annotate_catalysts(
            antigen.primary_name, antigen.indication_tags, catalysts, tpp
        )
        modality_text = await narrate_modalities(antigen.primary_name, data.programs, tpp)

        # Render
        html = render_antigen(
            data, env,
            synth=synth, tpp=tpp,
            catalysts=catalysts, timeline=timeline,
            picks=picks, annotations=annotations, modality_narratives=modality_text,
        )
        (DIST_DIR / f"{antigen.slug}.html").write_text(html)

        ot_drugs = len(data.open_targets.known_drugs) if data.open_targets else 0
        tpp_marker = "·TPP" if tpp else ""
        print(
            f"  wrote dist/{antigen.slug}.html "
            f"({len(data.programs)}p·{len(data.papers)}pap·{len(data.abstracts)}abs·"
            f"{len(data.preprints)}pre·{len(data.grants)}grnt·"
            f"{len(data.filings)}fil·{ot_drugs}OT·{len(data.news)}news·"
            f"{len(data.fda_approvals)}FDA·{len(data.ema_approvals)}EMA·"
            f"{len(catalysts)}cat·{len(timeline)}tl[+{new_count}]{tpp_marker})",
            file=sys.stderr,
        )
        rendered.append(data)

    snapshot_db.close()

    # v0.3.1: cross-antigen commentary for the index page
    commentary = await index_commentary(rendered)

    # Render the index page
    index_html = render_index(rendered, env, commentary=commentary)
    (DIST_DIR / "index.html").write_text(index_html)

    print(f"\nDone. {len(rendered)} antigen(s) rendered to dist/.", file=sys.stderr)


def refresh() -> None:
    """Entry point for `taa-refresh`."""
    asyncio.run(_refresh_all())


def audit() -> None:
    """Entry point for `taa-audit` — placeholder for v0.2."""
    print("audit: not yet implemented (v0.2)", file=sys.stderr)
    sys.exit(1)
