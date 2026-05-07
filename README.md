# TAA Tracker

Open-source modality cross-cut scorecards for tumour-associated antigens.
For biotech business development, corp dev, and strategy readers.

**Status:** v0.2 — 6 antigens · 11 sources · citation-grounded LLM synthesis · curated TPP benchmark layer.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Optional but recommended for synthesis + faster PubMed:
cp .env.example .env  # add ANTHROPIC_API_KEY, NCBI_API_KEY, EDGAR_USER_AGENT

# Pull data + render HTML for every antigen in data/antigens.yaml
taa-refresh

# Open the index
open dist/index.html
```

## Antigens currently tracked

| Slug | Antigen | Why it's interesting |
| --- | --- | --- |
| `her2` | HER2 / ERBB2 | The most heavily-trafficked oncology TAA. Anchor for the cross-cut UX. |
| `bcma` | BCMA / TNFRSF17 | Heme. Two approved CAR-Ts (Carvykti, Abecma) + Tecvayli bispecific. |
| `cldn18-2` | CLDN18.2 | Hot post-zolbetuximab approval. Rapidly developing ADC + bispecific tier. |
| `b7-h3` | B7-H3 / CD276 | Pan-cancer ADC race (DS-7300, MGC018, HS-20093) + paediatric CAR-T. |
| `5t4` | 5T4 / TPBG | Thin clinical pipeline, academic-heavy preclinical — preprints + grants matter. |
| `ror1` | ROR1 | Zilovertamab vedotin (Merck) anchors. CAR-T tier growing (Lyell, Oncternal). |

Add an antigen by appending to `data/antigens.yaml` and re-running `taa-refresh`.

## Sources (11)

Every source is free, public, and rate-limit-respectful. Each has a per-source
asyncio semaphore matching its published limit; failures degrade gracefully
(stale-source banner on the affected page).

| Source | Stream | Why it matters |
| --- | --- | --- |
| **CT.gov v2** | Trials | Ground-truth clinical pipeline. |
| **PubMed (E-utilities)** | Papers | Canonical bibliographic record, peer-reviewed signal. |
| **PubMed conference abstracts** | Abstracts | ASCO / AACR / ESMO / SITC / ASH supplements — readouts pre-publication. |
| **OpenAlex** | Papers + citation counts | Momentum signal (cites/year). |
| **EDGAR** | SEC filings | 10-K / 8-K corporate disclosure of programs. |
| **Open Targets** | Biology | Druggability, tractability, top diseases, safety liabilities. |
| **News RSS** | Press releases | Real-time deal / IND / readout coverage. |
| **openFDA Drugs** | FDA approvals | NDA / BLA ground truth. |
| **EMA EPAR** | EU approvals | Marketing authorisation cross-check. |
| **Europe PMC preprints** | bioRxiv / medRxiv | Leading indicator — 6–18 months ahead of PubMed. |
| **NIH RePORTER** | US-funded grants | Academic preclinical / translational pipeline (NCI · NHLBI · NIAID · NIDDK). |

## What's here

- `taa/schema.py` — Pydantic models for every data type
- `taa/sources/*.py` — one client per source, each with its own rate-limit semaphore
- `taa/normalize.py` — alias matching, exclude rules, modality assignment
- `taa/synth.py` — citation-grounded LLM narrative (Anthropic structured output)
- `taa/render.py` — Jinja2 → static HTML
- `taa/audit_matches.py` — surfaces unknown drug names from CT.gov for one-key curation
- `templates/` + `static/system.css` — design system (see `DESIGN.md`)
- `data/antigens.yaml` — curated TAA universe
- `data/drug_modality.yaml` — drug name → modality lookup
- `data/tpp/*.yaml` — curated Target Product Profiles (current standard-of-care benchmark per antigen)

## Roadmap

- **v0.2** *(current)*: 6 antigens · 11 sources · citation-grounded synthesis · TPP layer · cross-cut grid · sortable / printable scorecards
- **v0.3**: snapshot DB + "What changed this week" deltas (highest-value next move — currently each refresh overwrites, so we have no time signal)
- **v0.4**: cross-antigen whitespace heatmap (rows=antigens × cols=modalities, highlight cells where Open Targets tractability is high but the clinical column is empty)
- **v0.5**: scale to 25–50 antigens with client-side search + modality / indication filters on the index page
- **v0.6+**: investor-deck PDF extraction (LLM), test coverage, weekly digest email

See `DESIGN.md` for the design system.

## License

MIT.
