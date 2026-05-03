"""Biotech news aggregator — RSS feed client.

Pulls from FierceBiotech, PRNewswire pharma category, GlobeNewswire pharma
category, and Endpoints News headlines. Filters per-antigen by alias keyword
match in title or summary.

Why RSS: simple, no auth, no rate limit hell, captures real-time material
events (deal announcements, IND filings, financings, trial readouts) that
8-K filings will surface 24h later. Many BD events appear in news first.

We deliberately keep this dumb: parse the feed, regex-match aliases, return
items. No NLP, no entity resolution. The synthesis layer can use these as
"recent events" context for the narrative paragraph.
"""

import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from xml.etree import ElementTree as ET

from taa.schema import Antigen, Citation, NewsItem, SourceFreshness

# Each source is (name, RSS URL). All are free, no auth.
FEEDS: list[tuple[str, str]] = [
    ("FierceBiotech", "https://www.fiercebiotech.com/rss/xml"),
    ("PRNewswire pharma", "https://www.prnewswire.com/rss/health/pharmaceuticals-news.rss"),
    ("PRNewswire biotech", "https://www.prnewswire.com/rss/health/biotechnology-news.rss"),
    ("GlobeNewswire pharma", "https://www.globenewswire.com/RssFeed/subjectcode/15-Pharmaceuticals/feedTitle/GlobeNewswire-Pharmaceuticals"),
    ("Endpoints News", "https://endpts.com/feed/"),
    ("BioSpace", "https://www.biospace.com/rss/news/biotech.aspx"),
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
    attempt_at = datetime.now(timezone.utc)
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
        for raw in result:  # type: ignore[union-attr]
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
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
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
            title = (entry.findtext(f"{ns}title") or "").strip()
            link_el = entry.find(f"{ns}link")
            link = link_el.get("href", "") if link_el is not None else ""
            summary = (entry.findtext(f"{ns}summary") or "").strip()
            pub = (entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or "").strip()
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
            dt = dt.replace(tzinfo=timezone.utc)
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
