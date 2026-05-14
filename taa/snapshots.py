"""Snapshot DB + historical timeline (v0.3).

Each refresh records a snapshot, then projects the antigen's current source
records (news / filings / approvals / abstracts / preprints / grants / trial
updates) into a flat `timeline_events` table. Events are deduped on a content
hash so the timeline survives feed rollover — when an RSS item ages off
FierceBiotech, we still have it from the prior snapshot.

The timeline is the union across snapshots, sorted reverse-chronologically.
Snapshot rows themselves give us future "what changed this week" deltas
without re-querying the source APIs.

Storage: SQLite at data/snapshots.db. Single file, committed to git so the
historical record travels with the repo.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from taa.schema import AntigenData, TimelineEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    antigens_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_events (
    hash TEXT NOT NULL,
    antigen_slug TEXT NOT NULL,
    kind TEXT NOT NULL,
    event_date TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT,
    url TEXT,
    detail TEXT,
    first_seen_snapshot INTEGER NOT NULL,
    PRIMARY KEY (hash, antigen_slug),
    FOREIGN KEY (first_seen_snapshot) REFERENCES snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_timeline_antigen_date
    ON timeline_events(antigen_slug, event_date DESC);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """Open the snapshot DB, creating tables on first use."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def record_snapshot(conn: sqlite3.Connection, antigen_slugs: list[str]) -> int:
    """Insert a new snapshot row and return its id."""
    cur = conn.execute(
        "INSERT INTO snapshots (generated_at, antigens_json) VALUES (?, ?)",
        (datetime.now(UTC).isoformat(), json.dumps(antigen_slugs)),
    )
    conn.commit()
    snapshot_id = cur.lastrowid
    assert snapshot_id is not None
    return snapshot_id


def upsert_timeline_events(
    conn: sqlite3.Connection,
    antigen_slug: str,
    events: Iterable[TimelineEvent],
    snapshot_id: int,
) -> int:
    """Insert any events whose (hash, antigen_slug) isn't already in the table.

    Returns the count of newly-inserted rows.
    """
    inserted = 0
    for ev in events:
        h = _event_hash(ev)
        try:
            conn.execute(
                """
                INSERT INTO timeline_events
                    (hash, antigen_slug, kind, event_date, title, source, url, detail, first_seen_snapshot)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    h,
                    antigen_slug,
                    ev.kind,
                    ev.date.isoformat(),
                    ev.title,
                    ev.source,
                    ev.url,
                    ev.detail,
                    snapshot_id,
                ),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            # Already seen — that's fine, dedupe is the whole point.
            pass
    conn.commit()
    return inserted


def load_timeline(
    conn: sqlite3.Connection, antigen_slug: str, limit: int = 200
) -> list[TimelineEvent]:
    """Load all known timeline events for an antigen, reverse-chronological."""
    rows = conn.execute(
        """
        SELECT kind, event_date, title, source, url, detail
        FROM timeline_events
        WHERE antigen_slug = ?
        ORDER BY event_date DESC, title ASC
        LIMIT ?
        """,
        (antigen_slug, limit),
    ).fetchall()
    return [
        TimelineEvent(
            kind=kind,
            date=date.fromisoformat(event_date),
            title=title,
            source=source,
            url=url,
            detail=detail,
        )
        for (kind, event_date, title, source, url, detail) in rows
    ]


# ---- Project AntigenData → TimelineEvent[] ------------------------------------


def events_from_antigen_data(data: AntigenData) -> list[TimelineEvent]:
    """Flatten an AntigenData's news/filings/approvals/abstracts/preprints/grants
    into a normalized TimelineEvent stream.

    Trial updates are intentionally excluded — too noisy (every trial gets
    metadata-edit posts that aren't real events). Phase transitions and status
    changes could be added later by diffing snapshots.
    """
    events: list[TimelineEvent] = []

    for n in data.news:
        if n.published_at is None:
            continue
        events.append(
            TimelineEvent(
                kind="news",
                date=n.published_at.date(),
                title=n.title,
                source=n.source,
                url=str(n.url),
                detail=(n.summary or "")[:200] or None,
            )
        )

    for f in data.filings:
        events.append(
            TimelineEvent(
                kind="filing",
                date=f.filed_at,
                title=f"{f.form_type} · {f.company}",
                source=f"EDGAR · {f.form_type}",
                url=str(f.filing_url),
                detail=f.accession,
            )
        )

    for fda in data.fda_approvals:
        brand = fda.brand_names[0] if fda.brand_names else fda.display_name
        # FDA application numbers are <prefix><digits>, prefix ∈ {BLA, NDA, ANDA}.
        # The DAF lookup URL takes the digits only.
        appl_digits = re.sub(r"^[A-Z]+", "", fda.application_number)
        events.append(
            TimelineEvent(
                kind="approval",
                date=fda.first_approved,
                title=f"FDA approval — {brand}",
                source="FDA",
                url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={appl_digits}",
                detail=f"{fda.sponsor} · {fda.application_number}",
            )
        )

    for ema in data.ema_approvals:
        if ema.authorisation_date is None:
            continue
        events.append(
            TimelineEvent(
                kind="approval",
                date=ema.authorisation_date,
                title=f"EMA authorisation — {ema.name}",
                source="EMA",
                url=ema.url,
                detail=f"{ema.marketing_authorisation_holder} · {ema.ema_product_number}",
            )
        )

    for ab in data.abstracts:
        # Abstracts have no posted_date in the schema; use Jan 1 of the year
        # so they sort within their year but don't outrank dated events.
        if not ab.year:
            continue
        primary = ab.meeting or ab.journal
        secondary = ab.journal if ab.meeting and ab.meeting != ab.journal else None
        events.append(
            TimelineEvent(
                kind="abstract",
                date=date(ab.year, 1, 1),
                title=ab.title,
                source=primary,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{ab.pmid}/" if ab.pmid else None,
                detail=secondary,
            )
        )

    for p in data.preprints:
        ev_date = p.posted_date or (date(p.year, 1, 1) if p.year else None)
        if ev_date is None:
            continue
        events.append(
            TimelineEvent(
                kind="preprint",
                date=ev_date,
                title=p.title,
                source=p.server,
                url=f"https://doi.org/{p.doi}" if p.doi else None,
                detail=p.doi,
            )
        )

    for g in data.grants:
        if g.project_start is None:
            continue
        events.append(
            TimelineEvent(
                kind="grant",
                date=g.project_start,
                title=g.title,
                source="NIH RePORTER",
                url=f"https://reporter.nih.gov/search/?term={g.project_num}",
                detail=f"{g.pi_name or '—'} · {g.organization or '—'}",
            )
        )

    return events


# ---- Hashing ------------------------------------------------------------------


def _event_hash(ev: TimelineEvent) -> str:
    """Content hash used as primary key. Stable across refreshes for the same
    underlying event so dedup works even if the URL gains tracking params or
    the source feed reorders fields.
    """
    payload = "|".join(
        [
            ev.kind,
            ev.date.isoformat(),
            ev.title.strip().lower()[:200],
            (ev.url or "").split("?")[0],  # strip query params
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
