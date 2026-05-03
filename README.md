# TAA Tracker

Open-source modality cross-cut scorecards for tumour-associated antigens.
For biotech business development, corp dev, and strategy readers.

**Status:** v0.1 vertical slice — HER2 only, CT.gov only. The pipeline is end-to-end working; broader source coverage and antigen list ships in v0.2+.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Optional: copy and fill out env
cp .env.example .env
# (synthesis is v0.2 — not needed for vertical slice)

# Pull data + render HTML
taa-refresh

# Open the result
open dist/her2.html
```

## What's here

- `taa/schema.py` — Pydantic models (`Antigen`, `Program`, `Trial`, `Citation`, …)
- `taa/sources/ctgov.py` — ClinicalTrials.gov v2 client (per-source semaphore, retry-on-429)
- `taa/normalize.py` — alias matching, exclude rules, modality assignment
- `taa/render.py` — Jinja2 → static HTML
- `templates/` — design system applied (see `DESIGN.md`)
- `static/system.css` — design tokens, hand-edited from DESIGN.md
- `data/antigens.yaml` — curated TAA universe (50 entries planned, 1 in v0.1)
- `data/drug_modality.yaml` — drug name → modality lookup

## Roadmap

- **v0.1** *(current)*: vertical slice — HER2, CT.gov only, render-to-dist pipeline working
- **v0.2**: PubMed + OpenAlex + EDGAR sources, 50 antigens, AI synthesis (citation-grounded), search input
- **v0.3**: Whitespace finder, "What changed this week" digest, conference abstracts
- **v0.4**: Big-pharma investor PDFs (LLM extraction), full eng review test coverage
- **v0.5+**: Snapshot DB for time-axis features, automated false-positive curation tool

See `DESIGN.md` for the design system and `~/.gstack/projects/rsingla92-taa-tracker/` for the design + eng-review documents.

## License

MIT.
