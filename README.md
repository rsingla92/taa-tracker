# TAA Tracker

One scorecard per drug target, built from 11 public biotech data feeds and grounded in real citations.

**Rohit Singla** · rsingla@ece.ubc.ca · [LinkedIn](https://www.linkedin.com/in/rsingla92/)

**Status:** v0.3 — 7 [tumor-associated antigens](https://en.wikipedia.org/wiki/Tumor_antigen) tracked · 11 sources · citation-grounded LLM synthesis · curated TPP benchmark layer · snapshot DB · upcoming-catalysts view · historic timeline.

```
┌──────────────────────────────────────────────────────────────────┐
│ 11 PUBLIC SOURCES  (async, per-source rate limits)                │
│ CT.gov, PubMed, PubMed abstracts, OpenAlex, EDGAR,                │
│ Open Targets, News RSS, Europe PMC, NIH RePORTER,                 │
│ openFDA, EMA EPAR                                                 │
└──────────────────────────────────────────────────────────────────┘
                                 │  raw Trial / Paper / Filing / Grant rows
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ NORMALIZE  (taa/normalize.py)                                     │
│ alias match (longest-substring, cross-antigen guard)              │
│ -> exclude rules (curated false-positive filter)                  │
│ -> modality lookup (data/drug_modality.yaml)                      │
└──────────────────────────────────────────────────────────────────┘
                                 │  Program rollups (citation_ids, trial_ncts)
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ ENRICH                                                            │
│ TPP benchmark, catalyst date extraction (regex)                   │
│ -> citation-grounded LLM synthesis (Claude Haiku, strict          │
│    JSON, validated against real citation IDs)                     │
└──────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ SNAPSHOT DB  (SQLite, taa/snapshots.py)                           │
│ content-hash dedupe -> union of events across every               │
│ historical refresh, survives RSS feed rollover                    │
└──────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────┐
│ RENDER  (Jinja2 -> static HTML)                                   │
│ scorecard per antigen + cross-antigen index                       │
└──────────────────────────────────────────────────────────────────┘
```

## Table of contents

- [Current state](#current-state)
- [Antigens tracked](#antigens-tracked)
- [How this project came about](#how-this-project-came-about)
- [What this project is](#what-this-project-is)
- [Why it's designed this way](#why-its-designed-this-way)
- [Why the results look the way they do](#why-the-results-look-the-way-they-do)
- [Completed work](#completed-work)
- [Running it](#running-it)
- [CLI entry points](#cli-entry-points)
- [Data sources](#data-sources)
- [Test suite](#test-suite)
- [Installation](#installation)
- [How I built this, and what I learned](#how-i-built-this-and-what-i-learned)
- [Read these files first](#read-these-files-first)
- [Roadmap](#roadmap)

## Current state

**What is built:**

- Async clients for 11 public sources, each with its own rate-limit semaphore and graceful degradation (a stale-source banner rather than a crash if one API is down).
- A normalization pipeline that rolls raw trial/paper/filing rows into per-drug `Program` records: alias matching, per-antigen exclude-term filtering, and a curated drug-to-modality lookup table.
- A citation-grounded LLM synthesis layer (Claude Haiku, strict JSON output) where every sentence must cite a real citation ID or it gets dropped before rendering.
- A curated [Target Product Profile](https://en.wikipedia.org/wiki/Target_product_profile) (TPP) benchmark layer, currently covering 5 of 7 tracked antigens.
- An upcoming-catalysts view: [ClinicalTrials.gov](https://clinicaltrials.gov/) primary completion dates for active Phase 2+ trials, a curated oncology conference calendar, and regex-detected readout-guidance windows from news.
- A SQLite snapshot database that content-hashes timeline events so they survive RSS feed rollover, producing a historic per-antigen timeline.
- A human-in-the-loop curation tool (`taa-audit-matches`) for categorizing drug names the normalization pipeline doesn't recognize.
- An editorial design system (see [`DESIGN.md`](DESIGN.md)) with sortable, printable scorecard tables.

**Current results:**

- Live scorecards for 7 antigens: HER2, BCMA, CLDN18.2, B7-H3, 5T4, ROR1, B7-H4.
- No automated test suite yet. `pytest` is configured in `pyproject.toml` with markers for live-API tests, but no test files have been written. This is the most significant gap between what the tooling implies and what's actually verified.
- TPP benchmarks exist for 5 of the 7 antigens (5T4 and B7-H4 don't have one curated yet).
- The catalyst-date extractor is deliberately conservative (regex, not LLM) and will miss guidance phrasing it doesn't recognize rather than guess (see below).

## Antigens tracked

| Slug | Antigen | Why it's interesting |
| --- | --- | --- |
| `her2` | [HER2](https://en.wikipedia.org/wiki/HER2) / ERBB2 | The most heavily-trafficked oncology TAA. Anchor for the cross-cut UX. |
| `bcma` | [BCMA](https://en.wikipedia.org/wiki/B-cell_maturation_antigen) / TNFRSF17 | Heme. Two approved CAR-Ts (Carvykti, Abecma) + Tecvayli bispecific. |
| `cldn18-2` | [CLDN18.2](https://en.wikipedia.org/wiki/Claudin_18) | Hot post-zolbetuximab approval. Rapidly developing ADC + bispecific tier. |
| `b7-h3` | [B7-H3](https://en.wikipedia.org/wiki/CD276) / CD276 | Pan-cancer ADC race (DS-7300, MGC018, HS-20093) + paediatric CAR-T. |
| `5t4` | 5T4 / TPBG | Thin clinical pipeline, academic-heavy preclinical — preprints + grants matter. |
| `ror1` | [ROR1](https://en.wikipedia.org/wiki/ROR1) | Zilovertamab vedotin (Merck) anchors. CAR-T tier growing (Lyell, Oncternal). |
| `b7-h4` | B7-H4 / VTCN1 | Breast/ovarian/endometrial ADC race (AZD8205, XMT-1660, FPA150). Naming overlaps B7-H3 — alias matching is the gotcha. |

Add an antigen by appending to `data/antigens.yaml` and re-running `taa-refresh`.

## How this project came about

I kept running into the same problem doing biotech BD work: the picture of "who's developing what against this target" is scattered across a dozen sites that don't talk to each other. ClinicalTrials.gov has the trials, EDGAR has the corporate disclosure, PubMed and preprint servers have the biology, and none of them agree on what to call a drug. I wanted to know whether an LLM-assisted pipeline could stitch that together into something trustworthy enough to actually use, not just a demo that looks good until you check the citations. HER2 was the first target because it's the most heavily trafficked TAA in oncology and a good stress test for the alias-matching logic before scaling to less common targets.

## What this project is

TAA Tracker pulls trial, publication, filing, approval, and grant data for a curated list of tumor-associated antigens from 11 free public APIs, normalizes it into per-drug program records, and renders a static HTML scorecard per antigen plus a cross-antigen index. Each scorecard includes a modality cross-cut grid (ADC, CAR-T, bispecific, mAb, radioligand, vaccine, other), a TPP benchmark comparison where curated, an LLM-written narrative synthesis where every claim traces back to a source citation, an upcoming-catalysts panel, and a historic timeline. The whole thing runs as a single CLI command (`taa-refresh`) intended to be cron-scheduled.

## Why it's designed this way

**Alias matching is longest-substring with a cross-antigen guard, not exact match.** An early version matched on first hit, which meant a short generic term like "CAR-T" could swallow a longer, more specific drug name before the specific match was tried. It's longest-match now. There's also an explicit guard that drops a match if the matched drug is registered to a *different* antigen than the one being processed, because combination trials cross-contaminate otherwise: HS-20093 and HS-20089 are both Hansoh B7-H3/B7-H4 ADCs that co-appear in the same combination trials, and without the guard, HS-20093 leaks into the B7-H4 scorecard and vice versa.

**Exclude rules are a curated per-antigen false-positive filter**, applied after the alias match and before modality assignment. Some aliases are short enough to false-positive against unrelated trials (`data/antigens.yaml` notes that HER2's "NEU" alias, if kept, would match neurology, neuroendocrine, and neutropenia trials that have nothing to do with the antigen). Antigens with unambiguous long aliases (ERBB2) don't need exclude terms at all; this is a per-antigen decision, not a global one.

**Modality is assigned from a curated YAML lookup table, not inferred.** Drug names that don't match anything in `data/drug_modality.yaml` are collected as unknowns rather than guessed at, and surfaced through `taa-audit-matches` for one-keypress human categorization. This is the actual scaling bottleneck for adding antigens past the current 7: `data/drug_modality.yaml` and each antigen's `exclude_terms` are the trust layer, and they're both hand-curated.

**LLM synthesis is citation-grounded by schema, not by prompt instruction alone.** The model doesn't see raw trial text; it sees pre-summarized program rollups and a citation list restricted to IDs actually referenced by those programs. Every output sentence carries a `citation_ids` field, and a post-hoc validation step drops any sentence whose citation isn't in the real, source-derived citation set (`validate_against_citations` in `taa/synth.py`). If the API call fails, the JSON doesn't parse, or Pydantic validation fails, the synthesis is omitted entirely rather than shown partially wrong. This fail-closed pattern is used consistently: no synthesis is worse than fabricated synthesis.

**Catalyst date extraction is regex, not LLM**, and deliberately trades recall for precision. It requires a data-signal keyword (topline, interim, primary endpoint, data readout, results) within 80 characters of a trigger word (expected, anticipated, by, in) followed by a parseable date phrase (a quarter, a half-year, a fuzzy bucket like "mid 2026", or a named month and year). Phrasing outside that pattern, like "on track for Q3" or "will report results in," is silently missed. That's intentional: a wrong catalyst date is worse than a missing one for a BD reader making a calendar decision, and running an LLM over every news item to catch more phrasing variants wasn't worth the added inference cost and hallucination surface for what is fundamentally a date-parsing problem.

**The snapshot database dedupes on a content hash, not a source-native ID.** Each timeline event is hashed from its kind, date, truncated lowercase title, and query-stripped URL. RSS items and API records don't have stable IDs across refreshes (or the ID scheme differs per source), so a content hash is what makes "is this the same event I saw last refresh" answerable at all, and it's what lets the timeline survive a news item aging off a feed.

## Why the results look the way they do

Every rendered program carries `citation_ids` and `trial_ncts`, so a BD reader can trace any claim on the page back to the actual [ClinicalTrials.gov](https://clinicaltrials.gov/) record, paper, or filing it came from. The synthesis paragraph is intentionally short and can be entirely absent for an antigen if the model didn't produce anything that survived citation validation; a missing paragraph means the pipeline had nothing it could stand behind, not that nothing happened for that antigen.

The catalysts panel works the same way: an antigen with no listed catalysts doesn't mean no program has upcoming data, it means no news text matched the conservative extraction pattern closely enough to be trusted. This is the tradeoff described above, made visible in the output rather than hidden behind an artificially complete-looking list.

The TPP layer only appears for 5 of 7 antigens because it's hand-curated against public label and guideline data, and curation hasn't caught up to the two newest antigens (5T4, B7-H4) yet.

## Completed work

| Version | Date | What shipped |
| --- | --- | --- |
| v0.1 | 2026-05-02 | HER2 vertical slice from CT.gov, 4 sources, 3 antigens, TPP table, `audit-matches` tool, editorial design system |
| v0.2 | 2026-05-07 | Europe PMC preprints + NIH RePORTER (11 sources total), B7-H3/5T4/ROR1 added (6 antigens) |
| v0.3 | 2026-05-15 | Snapshot DB, upcoming-catalysts view, historic timeline, drug→antigen registry, B7-H4 added (7 antigens) |

Full detail in [`CHANGELOG.md`](CHANGELOG.md). Each version bump in the table above corresponds to a real data refresh, not just a code change; refreshed output for all 7 antigens is committed under `data/*.json`.

## Running it

**Interactive:**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env  # add ANTHROPIC_API_KEY, NCBI_API_KEY, EDGAR_USER_AGENT

taa-refresh
open dist/index.html
```

**Automated / scripted:** `taa-refresh` is a single, idempotent CLI command with no required arguments, intended to be run on a schedule (cron or similar). It reads `data/antigens.yaml` for the antigen list and writes `data/{slug}.json`, updates `data/snapshots.db`, and renders `dist/`.

**Individual tools:**

```bash
taa-refresh              # full pipeline: fetch, normalize, synthesize, render
taa-audit                # coverage audit against curated drug lists
taa-audit-matches        # interactive curation of unrecognized drug names
```

## CLI entry points

| Command | Mode | What it does |
| --- | --- | --- |
| `taa-refresh` | Automated | Runs the full pipeline for every antigen in `data/antigens.yaml`: fetch all 11 sources, normalize, synthesize, extract catalysts, snapshot, render. |
| `taa-audit` | Automated | Cross-checks normalized programs against curated drug lists per antigen and reports coverage gaps. |
| `taa-audit-matches` | Interactive | One-keypress terminal tool to categorize drug names the normalizer didn't recognize into `data/drug_modality.yaml`. |

## Data sources

Every source is free, public, and rate-limit-respectful. Each has its own asyncio semaphore matched to its published or reasonable-courtesy limit; failures degrade gracefully (a stale-source banner on the affected page, not a pipeline crash).

| Source | Stream | Concurrency cap | Why it matters |
| --- | --- | --- | --- |
| [CT.gov v2](https://clinicaltrials.gov/data-api/api) | Trials | 5 | Ground-truth clinical pipeline. |
| [PubMed](https://www.ncbi.nlm.nih.gov/home/develop/api/) (E-utilities) | Papers | 3 (10 with an NCBI API key) | Canonical bibliographic record, peer-reviewed signal. |
| PubMed conference abstracts | Abstracts | 3 (10 with an NCBI API key) | ASCO / AACR / ESMO / SITC / ASH supplements — readouts pre-publication. |
| [OpenAlex](https://openalex.org/) | Papers + citation counts | 8 | Momentum signal (cites/year). |
| [EDGAR](https://www.sec.gov/edgar) | SEC filings | 8 | 10-K / 8-K corporate disclosure of programs. |
| [Open Targets](https://www.opentargets.org/) | Biology | 5 | Druggability, tractability, top diseases, safety liabilities. |
| News RSS | Press releases | 8 | Real-time deal / IND / readout coverage. |
| [openFDA](https://open.fda.gov/) Drugs | FDA approvals | 8 | NDA / BLA ground truth. |
| [EMA EPAR](https://www.ema.europa.eu/en/medicines) | EU approvals | — | Marketing authorisation cross-check. |
| [Europe PMC](https://europepmc.org/) preprints | bioRxiv / medRxiv | 6 | Leading indicator, 6-18 months ahead of PubMed. |
| [NIH RePORTER](https://reporter.nih.gov/) | US-funded grants | 2 | Academic preclinical / translational pipeline (NCI, NHLBI, NIAID, NIDDK). |

## Test suite

There isn't one yet. `pyproject.toml` configures `pytest` with `pytest-asyncio`, `pytest-cov`, and `respx` as dev dependencies, plus markers to separate live-API tests (`live_llm`, `live_http`) from unit tests, but no `tests/` directory exists in the repo. The normalization pipeline (alias matching, exclude rules, cross-antigen guard) and the citation-validation logic in `taa/synth.py` are the parts I'd prioritize testing first, since they're the parts a silent regression would be hardest to notice from the rendered output alone.

## Installation

```bash
git clone git@github.com:rsingla92/taa-tracker.git
cd taa-tracker
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY (required for synthesis), NCBI_API_KEY (optional,
# raises PubMed rate limit from 3 req/s to 10 req/s), EDGAR_USER_AGENT (required
# by SEC EDGAR — "Your Name your-email@example.com")
```

Requires Python 3.11+.

## How I built this, and what I learned

**What I owned:** the normalization rules (which aliases need exclude terms and why, the cross-antigen guard, the longest-substring match), the curated data (`drug_modality.yaml`, `antigens.yaml`, TPP benchmarks, the conference calendar), the citation-grounding contract for the LLM layer (what the model is and isn't allowed to assert, and how that gets checked in code rather than trusted), the precision-over-recall call on catalyst extraction, and the design system in `DESIGN.md`.

**What AI accelerated:** the source client implementations (11 async HTTP clients with broadly similar retry/semaphore/pagination shapes), the Pydantic schema boilerplate, and the Jinja2 template scaffolding.

**Lessons:**

- Free-form "cite your sources" prompting isn't enough on its own. The synthesis contract needed a hard structural check, not just prompt instructions, which is why `taa/synth.py` validates every returned citation ID against the real citation set built from actual API responses and drops anything that doesn't match, rather than trusting the model's self-reported citations.
- Alias matching on first-hit was wrong. Short generic modality terms (like "CAR-T" appearing inside a longer specific drug name) would match before the specific drug did, so the match order had to change to longest-string-wins.
- Combination trials break naive per-antigen matching. Two drugs from the same sponsor's combination trial for related targets (HS-20093 and HS-20089, both Hansoh B7-H3/B7-H4 ADCs) will cross-contaminate each other's antigen pages unless there's an explicit guard rejecting a drug that's registered to a different antigen than the one currently being processed.
- Automating modality assignment looked tempting but would have traded a small amount of manual curation time for an unbounded amount of silent misclassification risk, so it stayed a curated lookup table with unknowns explicitly surfaced for a human to categorize, rather than inferred.
- Catalyst-date guidance in news text is one of the least standardized things to parse: "expected Q3 2026," "topline data in mid-2026," and "on track for later this year" are all common phrasings and only some of them are safely machine-parseable. Rather than chase every variant with an LLM (and inherit its hallucination risk for something as consequence-bearing as a date), the extractor stays regex-based and conservative, and simply omits catalysts it isn't confident about.

## Read these files first

| File | Why |
| --- | --- |
| [`taa/schema.py`](taa/schema.py) | The data contracts. Closed-vocabulary `Literal` types and required citation IDs are what keep bad data from propagating downstream. |
| [`taa/normalize.py`](taa/normalize.py) | Alias matching, exclude rules, and modality assignment — the trust layer described above. |
| [`taa/synth.py`](taa/synth.py) | The citation-grounded LLM synthesis layer and its fail-closed validation. |
| [`taa/catalysts.py`](taa/catalysts.py) | The regex-based, precision-over-recall catalyst date extractor. |
| [`taa/snapshots.py`](taa/snapshots.py) | The content-hash SQLite dedupe behind the historic timeline. |
| [`DESIGN.md`](DESIGN.md) | The design system: typography, color, and the anti-slop list of what this project deliberately avoids looking like. |

## Roadmap

- **v0.4**: snapshot diffs — "what changed since last refresh" per antigen (phase advances, new programs, withdrawn trials, terminated programs). The snapshot DB already has what's needed; it's a diff query and a render away.
- **v0.5**: cross-antigen whitespace heatmap (antigens × modalities, highlighting cells where Open Targets tractability is high but the clinical column is empty).
- **v0.6**: scale to 25-50 antigens with client-side search and modality/indication filters on the index page.
- **v0.7+**: LLM-based readout-date extraction to replace the v0.3 regex, investor-deck PDF extraction, a real test suite, weekly digest email.

## License

[MIT](LICENSE).
