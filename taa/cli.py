"""CLI entry points for v0.1.

`taa-refresh` — pull all sources, normalize, render. Used by the weekly cron.
`taa-audit`   — sample-and-print spot-check tool for citation grounding.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from taa.normalize import filter_excluded, load_modality_map, trials_to_programs
from taa.render import make_env, render_antigen
from taa.schema import Antigen, AntigenData
from taa.sources import ctgov

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
    """Pull all sources for one antigen, normalize, return AntigenData."""
    print(f"  fetching {antigen.primary_name} from CT.gov…", file=sys.stderr)
    ctgov_result = await ctgov.fetch(antigen)

    trials = filter_excluded(ctgov_result.trials, antigen)
    modality_map = _load_modality_map()
    programs, unknowns = trials_to_programs(trials, antigen, modality_map)  # type: ignore[arg-type]
    if unknowns:
        # Tee unknowns to a per-antigen file so bin/audit-matches can surface them
        # in v0.2 for one-key flagging into drug_modality.yaml.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        unknowns_file = DATA_DIR / f"_unknowns-{antigen.slug}.txt"
        unknowns_file.write_text("\n".join(unknowns) + "\n")
        print(
            f"  {len(unknowns)} unknown interventions logged to {unknowns_file.name}",
            file=sys.stderr,
        )

    return AntigenData(
        antigen=antigen,
        programs=programs,
        trials=trials,
        papers=[],  # v0.1 vertical slice: only CT.gov for now
        filings=[],
        citations=ctgov_result.citations,
        freshness=[ctgov_result.freshness],
        generated_at=datetime.now(timezone.utc),
    )


async def _refresh_all() -> None:
    antigens = _load_antigens()
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # Copy static assets to dist/ so Cloudflare Pages serves them at /static/
    static_out = DIST_DIR / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    for asset in STATIC_DIR.glob("*"):
        (static_out / asset.name).write_bytes(asset.read_bytes())

    env = make_env()
    rendered: list[AntigenData] = []
    for antigen in antigens:
        data = await _refresh_one(antigen)
        # Persist structured data (gitignore handles dist/)
        (DATA_DIR / f"{antigen.slug}.json").write_text(
            data.model_dump_json(indent=2)
        )
        # Render HTML to dist/
        html = render_antigen(data, env)
        (DIST_DIR / f"{antigen.slug}.html").write_text(html)
        print(f"  wrote dist/{antigen.slug}.html ({len(data.programs)} programs)", file=sys.stderr)
        rendered.append(data)

    print(f"\nDone. {len(rendered)} antigen(s) rendered to dist/.", file=sys.stderr)


def refresh() -> None:
    """Entry point for `taa-refresh`."""
    asyncio.run(_refresh_all())


def audit() -> None:
    """Entry point for `taa-audit` — placeholder for v0.2."""
    print("audit: not yet implemented (v0.2)", file=sys.stderr)
    sys.exit(1)
