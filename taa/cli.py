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
from taa.schema import Antigen, AntigenData
from taa.sources import ctgov, edgar, openalex, pubmed
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
    # event loop and respect their own rate limits independently.
    ctgov_task = asyncio.create_task(ctgov.fetch(antigen, citation_id_start=1))
    pubmed_task = asyncio.create_task(pubmed.fetch(antigen, citation_id_start=10000))
    openalex_task = asyncio.create_task(openalex.fetch(antigen, citation_id_start=20000))
    edgar_task = asyncio.create_task(edgar.fetch(antigen, citation_id_start=30000))

    ctgov_r = await ctgov_task
    pubmed_r = await pubmed_task
    openalex_r = await openalex_task
    edgar_r = await edgar_task

    # Normalize trials → programs (only CT.gov drives Programs in v0.1; v0.2 will
    # also use EDGAR text extraction to discover programs not visible in CT.gov)
    trials = filter_excluded(ctgov_r.trials, antigen)
    modality_map = _load_modality_map()
    programs, unknowns = trials_to_programs(trials, antigen, modality_map)  # type: ignore[arg-type]

    if unknowns:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"_unknowns-{antigen.slug}.txt").write_text("\n".join(unknowns) + "\n")

    return AntigenData(
        antigen=antigen,
        programs=programs,
        trials=trials,
        papers=pubmed_r.papers + openalex_r.papers,
        filings=edgar_r.filings,
        citations=ctgov_r.citations
        + pubmed_r.citations
        + openalex_r.citations
        + edgar_r.citations,
        freshness=[ctgov_r.freshness, pubmed_r.freshness, openalex_r.freshness, edgar_r.freshness],
        generated_at=datetime.now(timezone.utc),
    )


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

        # Render
        html = render_antigen(data, env, synth=synth)
        (DIST_DIR / f"{antigen.slug}.html").write_text(html)

        program_count = len(data.programs)
        paper_count = len(data.papers)
        filing_count = len(data.filings)
        print(
            f"  wrote dist/{antigen.slug}.html "
            f"({program_count} programs · {paper_count} papers · {filing_count} filings)",
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
