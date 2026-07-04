"""RSS feed polling (SubsPlease / Nyaa style RSS 2.0)."""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)


async def fetch_items(
    client: httpx.AsyncClient, feed_url: str, cache: dict[str, dict[str, str]] | None = None
) -> list[tuple[str, str]]:
    """Return (title, link) pairs from an RSS feed. Pass the same `cache` dict
    every poll for conditional GETs (ETag/Last-Modified) — unchanged feeds
    return [] off a cheap 304 instead of a full fetch.

    ponytail: stdlib ElementTree over plain RSS 2.0 — SubsPlease and Nyaa
    both emit it. Swap in feedparser if a broken feed ever shows up.
    """
    headers = dict(cache.get(feed_url, {})) if cache is not None else {}
    r = await client.get(feed_url, timeout=30, headers=headers)
    if r.status_code == 304:
        return []
    r.raise_for_status()
    if cache is not None:
        cond = {}
        if etag := r.headers.get("ETag"):
            cond["If-None-Match"] = etag
        if modified := r.headers.get("Last-Modified"):
            cond["If-Modified-Since"] = modified
        cache[feed_url] = cond
    root = ET.fromstring(r.content)
    items = []
    for item in root.iterfind(".//item"):
        title = item.findtext("title")
        link = item.findtext("link")
        if title and link:
            items.append((title, link))
    return items
