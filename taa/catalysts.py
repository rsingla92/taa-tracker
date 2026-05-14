"""Upcoming-catalyst extraction (v0.3).

Surfaces the events that will change the antigen landscape over the next
~18 months:

1. **CT.gov primary completion dates** — every active trial has a planned
   (ESTIMATED) or hit (ACTUAL) primary completion date; the planned ones
   in the near future are the most BD-relevant.
2. **Curated conference calendar** — ASCO / AACR / ESMO / SITC / ASH +
   indication-specific symposia, loaded from data/conferences.yaml.
3. **Readout-guidance regex on news** — light-touch scan of news titles +
   summaries for "topline expected Q3 2026" / "data readout in H2 2026"
   patterns. Conservative; high-precision over high-recall.

Each catalyst carries a `kind`, date, title, optional sponsor + URL,
and an `is_anticipated` flag for non-firm dates.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import yaml

from taa.schema import Antigen, Catalyst, NewsItem, Trial

# Look-ahead window — catalysts beyond this are noise (CT.gov PCDs slip;
# conference dates beyond ~18mo are placeholder).
LOOKAHEAD_DAYS = 540  # ~18 months

# CT.gov statuses that mean "this PCD is still meaningful". Completed or
# terminated trials' PCDs are historical, not anticipatory.
_LIVE_STATUSES = {"active"}

# Only phase-2+ trial completions count as catalysts. Phase 1 readouts rarely
# move a TAA landscape — too small, too exploratory, and HER2-class antigens
# have hundreds of Phase 1 combos that drown out the signal.
_CATALYST_PHASES = {"2", "2/3", "3"}

# Academic / IIT sponsors get dropped — high volume, low BD signal. A BD reader
# at a biotech wants to know what corporate-sponsored programs are reading out,
# not which Chinese university hospital just finished a 30-patient IIT.
_ACADEMIC_SPONSOR_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\buniversity\b",
        r"\bhospital\b",
        r"\binstitute\b",
        r"\bschool of (medicine|public health)\b",
        r"\bmedical center\b",
        r"\bcancer center\b",
        r"\bchildren['']?s\b",
        r"\bnational cancer\b",
        r"\bclinic\b",
        r"^M\.?D\.? Anderson",
        r"^Memorial Sloan",
        r"^Dana[- ]Farber",
        r"\bVA\b|Veterans Affairs",
        r"\bResearch Foundation\b",
        r"\bConsort(ium|ia)\b",
        r"^NCI\b|National Institute",
    )
]

# Countries we consider "North America" for the catalyst-geography filter.
# CT.gov returns full English country names.
_NA_COUNTRIES = {"United States", "Canada"}


def _is_academic_sponsor(name: str) -> bool:
    """Heuristic: sponsor name looks like an academic / hospital / IIT."""
    if not name:
        return False
    return any(p.search(name) for p in _ACADEMIC_SPONSOR_PATTERNS)


def _has_na_site(trial: Trial) -> bool:
    """True if the trial has at least one US or Canada site.

    If `location_countries` is empty (CT.gov didn't return locations for this
    trial — common for protocol-only or just-registered studies), we treat it
    as a NON-match. Conservative: drop trials we can't geo-confirm.
    """
    return any(c in _NA_COUNTRIES for c in trial.location_countries)


def extract_catalysts(
    antigen: Antigen,
    trials: list[Trial],
    news: list[NewsItem],
    conferences_path: Path,
    today: date,
) -> list[Catalyst]:
    """Return all upcoming catalysts for one antigen, sorted by date ascending."""
    horizon = today + timedelta(days=LOOKAHEAD_DAYS)

    catalysts: list[Catalyst] = []
    catalysts.extend(_from_trials(trials, today, horizon))
    catalysts.extend(_from_conferences(antigen, conferences_path, today, horizon))
    catalysts.extend(_from_news_guidance(news, today, horizon))

    # Sort by date ascending; firm dates before anticipated on tie.
    catalysts.sort(key=lambda c: (c.date, c.is_anticipated))
    return catalysts


# ---- Trial primary completion dates -------------------------------------------


def _from_trials(trials: list[Trial], today: date, horizon: date) -> list[Catalyst]:
    out: list[Catalyst] = []
    for t in trials:
        if t.status not in _LIVE_STATUSES:
            continue
        if t.phase not in _CATALYST_PHASES:
            continue
        if t.primary_completion_date is None:
            continue
        if t.primary_completion_date < today or t.primary_completion_date > horizon:
            continue
        sponsor = t.sponsors[0] if t.sponsors else None
        # Drop academic / IIT sponsors — low BD signal, high volume noise.
        if sponsor and _is_academic_sponsor(sponsor):
            continue
        # Require a North-American site. Older Trial JSONs predate location_countries,
        # in which case the list is empty → drop. Re-run taa-refresh to populate.
        if not _has_na_site(t):
            continue
        # Pull lead intervention as drug detail
        drug = t.interventions[0] if t.interventions else None
        detail_bits = [f"Phase {t.phase}", drug]
        detail = " · ".join(b for b in detail_bits if b) or None
        out.append(
            Catalyst(
                kind="trial_completion",
                date=t.primary_completion_date,
                title=f"{t.nct_id} primary completion — {t.title[:120]}",
                detail=detail,
                url=f"https://clinicaltrials.gov/study/{t.nct_id}",
                sponsor=sponsor,
                is_anticipated=not t.primary_completion_is_actual,
            )
        )
    return out


# ---- Conference calendar ------------------------------------------------------


def _from_conferences(
    antigen: Antigen,
    conferences_path: Path,
    today: date,
    horizon: date,
) -> list[Catalyst]:
    if not conferences_path.exists():
        return []
    raw = yaml.safe_load(conferences_path.read_text()) or []
    indication_set = {tag.lower() for tag in antigen.indication_tags}

    out: list[Catalyst] = []
    for entry in raw:
        start = entry.get("start_date")
        if not isinstance(start, date):
            continue
        if start < today or start > horizon:
            continue
        relevance = {r.lower() for r in (entry.get("relevance") or [])}
        # Empty relevance = always include (general onc meeting)
        if relevance and indication_set.isdisjoint(relevance):
            continue
        end = entry.get("end_date")
        end_str = end.isoformat() if isinstance(end, date) else ""
        loc = entry.get("location") or ""
        detail = " · ".join(b for b in [end_str and f"thru {end_str}", loc] if b) or None
        out.append(
            Catalyst(
                kind="conference",
                date=start,
                title=entry["name"],
                detail=detail,
                url=entry.get("url"),
                sponsor=None,
                is_anticipated=False,
            )
        )
    return out


# ---- Readout-guidance regex on news ------------------------------------------

# Conservative patterns — high precision over high recall. We don't want noise
# in the catalysts table; the timeline already shows every news item.
_GUIDANCE_PATTERN = re.compile(
    r"\b(?:topline|interim|primary(?:\s+endpoint)?|data\s+readout|results)\b"
    r"[^.]{0,80}?"
    r"\b(?:expected|anticipated|by|in)\b\s+"
    r"(?P<when>"
    r"(?:Q[1-4]|H[12])\s+\d{4}"  # "Q3 2026"
    r"|(?:early|mid|late|end\s+of)\s+\d{4}"  # "mid 2026"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}"
    r")",
    re.IGNORECASE,
)

_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _from_news_guidance(news: list[NewsItem], today: date, horizon: date) -> list[Catalyst]:
    out: list[Catalyst] = []
    seen_titles: set[str] = set()
    for item in news:
        haystack = f"{item.title} {item.summary or ''}"
        m = _GUIDANCE_PATTERN.search(haystack)
        if not m:
            continue
        when = m.group("when")
        target = _parse_when(when)
        if target is None:
            continue
        if target < today or target > horizon:
            continue
        # Dedupe on cleaned title — same press release picked up by multiple feeds
        key = item.title.strip().lower()[:120]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        out.append(
            Catalyst(
                kind="readout_guidance",
                date=target,
                title=item.title,
                detail=f"guidance: {when}",
                url=str(item.url),
                sponsor=item.source,
                is_anticipated=True,
            )
        )
    return out


def _parse_when(s: str) -> date | None:
    """Convert 'Q3 2026' / 'mid 2026' / 'October 2026' to a representative date."""
    s = s.strip().lower()

    # Q1-Q4 YYYY → quarter midpoint (Feb 15, May 15, Aug 15, Nov 15)
    m = re.match(r"q([1-4])\s+(\d{4})", s)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        month = {1: 2, 2: 5, 3: 8, 4: 11}[q]
        return date(y, month, 15)

    # H1/H2 YYYY → half midpoint (Apr 1, Oct 1)
    m = re.match(r"h([12])\s+(\d{4})", s)
    if m:
        h, y = int(m.group(1)), int(m.group(2))
        return date(y, 4 if h == 1 else 10, 1)

    # early/mid/late/end of YYYY
    m = re.match(r"(early|mid|late|end\s+of)\s+(\d{4})", s)
    if m:
        bucket, y = m.group(1).strip(), int(m.group(2))
        month = {"early": 2, "mid": 6, "late": 10, "end of": 12}[bucket]
        return date(y, month, 15)

    # Month YYYY
    m = re.match(r"([a-z]+)\s+(\d{4})", s)
    if m and m.group(1) in _MONTH_MAP:
        return date(int(m.group(2)), _MONTH_MAP[m.group(1)], 15)

    return None
