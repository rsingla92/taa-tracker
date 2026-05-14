"""Biotech news aggregator — RSS feed client.

Pulls from FierceBiotech, Fierce Pharma, BioPharma Dive, STAT Pharma,
Endpoints News, and FDA Press releases. Filters per-antigen by alias
keyword match in title or summary.

Why RSS: simple, no auth, no rate limit hell, captures real-time material
events (deal announcements, IND filings, financings, trial readouts) that
8-K filings will surface 24h later. Many BD events appear in news first.

We deliberately keep this dumb: parse the feed, regex-match aliases, return
items. No NLP, no entity resolution. The synthesis layer can use these as
"recent events" context for the narrative paragraph.
"""

import asyncio
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from taa.schema import Antigen, Citation, NewsItem, SourceFreshness

# Each source is (name, RSS URL). All are free, no auth.
#
# Drift watch: feed URLs change over time. Verified live 2026-05-13. Previous
# entries (PRNewswire pharma + biotech, BioSpace) 404'd and were replaced.
# GlobeNewswire pharma was dropped — the category feeds consumer-marketing
# noise (SHEIN, gift sets, fashion) rather than real pharma news.
# Add new feeds only after confirming items > 0 and TAA-relevance via a probe.
FEEDS: list[tuple[str, str]] = [
    ("FierceBiotech", "https://www.fiercebiotech.com/rss/xml"),
    ("Fierce Pharma", "https://www.fiercepharma.com/rss/xml"),
    ("BioPharma Dive", "https://www.biopharmadive.com/feeds/news/"),
    ("STAT Pharma", "https://www.statnews.com/category/pharma/feed/"),
    ("Endpoints News", "https://endpts.com/feed/"),
    ("FDA Press", "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"),
]

TIMEOUT_S = 20
RETRY_ATTEMPTS = 2
MAX_PER_FEED = 200  # most-recent items per feed before keyword filtering

_semaphore = asyncio.Semaphore(8)


class NewsResult:
    def __init__(
        self,
        items: list[NewsItem],
        citations: list[Citation],
        freshness: SourceFreshness,
    ) -> None:
        self.items = items
        self.citations = citations
        self.freshness = freshness


async def fetch(antigen: Antigen, citation_id_start: int = 1) -> NewsResult:
    """Pull all RSS feeds in parallel, filter to items mentioning antigen aliases."""
    attempt_at = datetime.now(UTC)
    aliases = [antigen.primary_name, *antigen.aliases]
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(a) for a in aliases) + r")\b", re.IGNORECASE
    )

    async with httpx.AsyncClient(
        timeout=TIMEOUT_S,
        headers={"User-Agent": "TAA Tracker (rsingla92@gmail.com)"},
        follow_redirects=True,
    ) as client:
        feed_tasks = [
            asyncio.create_task(_fetch_feed(client, name, url)) for name, url in FEEDS
        ]
        results = await asyncio.gather(*feed_tasks, return_exceptions=True)

    all_items: list[tuple[str, dict[str, str]]] = []
    feed_errors: list[str] = []
    for (feed_name, _), result in zip(FEEDS, results, strict=False):
        if isinstance(result, BaseException):
            feed_errors.append(f"{feed_name}: {type(result).__name__}")
            continue
        for raw in result:
            haystack = raw.get("title", "") + " " + raw.get("summary", "")
            if pattern.search(haystack):
                all_items.append((feed_name, raw))

    # Sort by published date desc; keep top 30
    all_items.sort(
        key=lambda pair: pair[1].get("published_dt") or datetime.min,
        reverse=True,
    )
    all_items = all_items[:30]

    items, citations = _normalize(all_items, citation_id_start, attempt_at)

    # News is "stale" only if ALL feeds failed and zero items returned. Partial
    # feed failure with at least one item is success — the section has fresh data.
    error_str = (
        ("partial: " + "; ".join(feed_errors)) if feed_errors and items
        else "; ".join(feed_errors) if feed_errors and not items
        else None
    )
    return NewsResult(
        items=items,
        citations=citations,
        freshness=SourceFreshness(
            source="news",
            last_success=attempt_at if items else None,
            last_attempt=attempt_at,
            error=error_str if not items else None,  # only flag stale if no items
        ),
    )


async def _fetch_feed(
    client: httpx.AsyncClient, name: str, url: str
) -> list[dict[str, Any]]:
    """Fetch one RSS feed, parse, return items as dicts."""
    async with _semaphore:
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.get(url)
                if resp.status_code in (429, 503):
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return _parse_rss(resp.text)
            except httpx.HTTPError:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(1.0)
    return []


def _all_text(element: ET.Element | None) -> str:
    """Concatenate all text within an element, including nested children.

    FierceBiotech wraps each <title> in an <a href>...</a> tag, so
    ``findtext("title")`` returns the empty string before the anchor instead
    of the actual title. ``itertext()`` walks descendants and collects every
    text node, which is what we want for RSS titles + descriptions.
    """
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _parse_rss(xml_text: str) -> list[dict[str, Any]]:
    """Parse RSS/Atom-flavored XML into a flat list of items.

    Handles RSS 2.0 (most feeds) and Atom (Endpoints News uses Atom).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    items: list[dict[str, Any]] = []

    # RSS 2.0: <rss><channel><item>...</item></channel></rss>
    for item in root.findall(".//item")[:MAX_PER_FEED]:
        title = _all_text(item.find("title"))
        link = _all_text(item.find("link"))
        summary = _all_text(item.find("description"))
        pub = _all_text(item.find("pubDate"))
        items.append(
            {
                "title": _strip_html(title),
                "link": link,
                "summary": _strip_html(summary)[:400],
                "published_dt": _parse_date(pub),
                "published_str": pub,
            }
        )

    # Atom: <feed><entry>...</entry></feed>
    if not items:
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(f".//{ns}entry")[:MAX_PER_FEED]:
            title = _all_text(entry.find(f"{ns}title"))
            link_el = entry.find(f"{ns}link")
            link = link_el.get("href", "") if link_el is not None else ""
            summary = _all_text(entry.find(f"{ns}summary"))
            pub = _all_text(entry.find(f"{ns}published")) or _all_text(
                entry.find(f"{ns}updated")
            )
            items.append(
                {
                    "title": _strip_html(title),
                    "link": link,
                    "summary": _strip_html(summary)[:400],
                    "published_dt": _parse_iso(pub),
                    "published_str": pub,
                }
            )

    return items


def _parse_date(rfc822: str) -> datetime | None:
    if not rfc822:
        return None
    try:
        dt = parsedate_to_datetime(rfc822)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (TypeError, ValueError):
        return None


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # Atom uses ISO 8601; some feeds put 'Z' which fromisoformat handles in 3.11+
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    """Lightweight HTML/CDATA stripper. RSS summaries often contain markup."""
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    return text.strip()


def _normalize(
    items: list[tuple[str, dict[str, Any]]],
    citation_id_start: int,
    retrieved_at: datetime,
) -> tuple[list[NewsItem], list[Citation]]:
    news: list[NewsItem] = []
    citations: list[Citation] = []
    cite_id = citation_id_start

    for feed_name, raw in items:
        url = raw.get("link", "")
        if not url:
            continue
        published = raw.get("published_dt")
        news.append(
            NewsItem(
                title=raw.get("title", ""),
                summary=raw.get("summary", "") or None,
                url=url,
                source=feed_name,
                published_at=published,
            )
        )
        date_str = published.strftime("%Y-%m-%d") if published else "undated"
        citations.append(
            Citation(
                id=cite_id,
                url=url,
                title=f"{feed_name} · {raw.get('title', '')[:80]}",
                source_type="filing",  # closest existing enum; widen in v0.3
                retrieved_at=retrieved_at,
                locator=f"{feed_name} {date_str}",
            )
        )
        cite_id += 1

    return news, citations
