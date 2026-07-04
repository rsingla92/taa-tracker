# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.3] - 2026-05-15

### Added
- SQLite snapshot database (`data/snapshots.db`) that dedupes timeline events across refreshes so feed rollover no longer loses history.
- Upcoming-catalysts view: CT.gov primary completion dates for active Phase 2+ trials, a curated oncology conference calendar, and regex-detected readout-guidance windows from news.
- Historic timeline view per antigen, backed by the snapshot database.
- LLM editorial synthesis layer on top of the citation-grounded narrative.
- Drug-to-antigen registry with per-program trial rollups and conference enrichment.
- B7-H4 as a seventh tracked antigen.

## [0.2] - 2026-05-07

### Added
- Two new sources: Europe PMC preprints and NIH RePORTER grants.
- Three new antigens: B7-H3, 5T4, ROR1 (bringing the total to 6 before B7-H4 landed in v0.3).
- Curated Target Product Profile (TPP) benchmarks for B7-H3 and ROR1.

### Fixed
- Relative asset paths so the static site works when served from a subpath.
- NIH RePORTER matching tightened to title-mentions only, reducing false positives.

## [0.1] - 2026-05-02

### Added
- Vertical slice: HER2 scorecard sourced from CT.gov, with the v1 design system applied (Source Serif 4 display, editorial aesthetic).
- LLM-synthesized citation-grounded narrative per scorecard.
- Four initial sources, three initial antigens (HER2, BCMA, CLDN18.2).
- TPP benchmark table, Open Targets biology enrichment, news RSS, FDA/EMA approval sources, and the `audit-matches` curation tool.
- Sortable, printable scorecard tables.
