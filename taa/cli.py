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

from taa.normalize import filter_excluded, load_modality_map, trials_to_programs
from taa.render import make_env, render_antigen, render_index
from taa.schema import Antigen, AntigenData, TargetProductProfile
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

    # Per-source semaphores live in each module — these tasks all share the asyncio
    # event loop and respect their own rate limits independently. Citation ID ranges
    # keep page-local IDs unique across sources without coordination.
    ctgov_task = asyncio.create_task(ctgov.fetch(antigen, citation_id_start=1))
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
    programs, unknowns = trials_to_programs(trials, antigen, modality_map)  # type: ignore[arg-type]

    if unknowns:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"_unknowns-{antigen.slug}.txt").write_text("\n".join(unknowns) + "\n")

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

        # Render
        html = render_antigen(data, env, synth=synth, tpp=tpp)
        (DIST_DIR / f"{antigen.slug}.html").write_text(html)

        ot_drugs = len(data.open_targets.known_drugs) if data.open_targets else 0
        tpp_marker = "·TPP" if tpp else ""
        print(
            f"  wrote dist/{antigen.slug}.html "
            f"({len(data.programs)}p·{len(data.papers)}pap·{len(data.abstracts)}abs·"
            f"{len(data.preprints)}pre·{len(data.grants)}grnt·"
            f"{len(data.filings)}fil·{ot_drugs}OT·{len(data.news)}news·"
            f"{len(data.fda_approvals)}FDA·{len(data.ema_approvals)}EMA{tpp_marker})",
            file=sys.stderr,
        )
        rendered.append(data)

    # Render the index page
    index_html = render_index(rendered, env)
    (DIST_DIR / "index.html").write_text(index_html)

    print(f"\nDone. {len(rendered)} antigen(s) rendered to dist/.", file=sys.stderr)


def refresh() -> None:
    """Entry point for `taa-refresh`."""
    asyncio.run(_refresh_all())


def audit() -> None:
    """Entry point for `taa-audit` — placeholder for v0.2."""
    print("audit: not yet implemented (v0.2)", file=sys.stderr)
    sys.exit(1)
