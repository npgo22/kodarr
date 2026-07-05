"""Nyaa backfill search for missing episodes/movies. Torrent-only:
SubsPlease/SeaDex torrents are well-seeded, and torrents can't import the
wrong content the way usenet name-matching could."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from psycopg import AsyncConnection

from kodarr import db, match, rss
from kodarr.clients import Qbit

log = logging.getLogger(__name__)


def rank(
    results: list[dict], series: dict[str, Any], want_ep: int | None, blocklist: frozenset[str] | set[str] = frozenset()
) -> list[tuple[tuple, dict]]:
    """Preferred-group releases for this exact series+episode; 1080p floor;
    best resolution first, seeders break ties."""
    scored = []
    for res in results:
        if res["title"] in blocklist:
            continue
        parsed = match.parse(res["title"])
        if parsed is None:
            continue
        if not parsed.group or parsed.group.lower() != series["preferred_group"].lower():
            continue
        m = match.match(parsed, [series])
        if m is None:
            continue
        _, ep = m
        if series["format"] != "MOVIE" and ep != want_ep:
            continue
        if (parsed.resolution or 0) < 1080:
            continue  # 1080p is the floor, never grab less
        scored.append(((parsed.resolution, res.get("seeders", 0)), res))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored


async def backfill_series(
    conn: AsyncConnection,
    http: httpx.AsyncClient,
    qbit: Qbit,
    series: dict[str, Any],
    *,
    nyaa_url: str = "https://nyaa.si",
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """Search + grab every aired-but-missing episode of one series.

    Backoff: at most one search pass per series per week (metadata refresh
    clears last_search when new episodes air) — otherwise a never-found
    episode hammers the indexer daily, forever.
    """
    if not force and await db.searched_recently(conn, series["anilist_id"]):
        return
    blocklist = await db.failed_release_names(conn, series["anilist_id"])
    if series["format"] == "MOVIE":
        missing: list[int | None] = [] if await db.get_episode(conn, series["anilist_id"], 1) else [1]
    else:
        aired = series.get("aired") or 0
        cur = await conn.execute(
            "SELECT absolute_number FROM episodes WHERE anilist_id = %s AND file_path IS NOT NULL",
            (series["anilist_id"],),
        )
        have = {r["absolute_number"] for r in await cur.fetchall()}
        missing = [ep for ep in range(1, aired + 1) if ep not in have]

    # Query with the romaji base title (synonyms[0] by anilist._clean
    # construction) — release groups name in romaji. Strip the season phrase:
    # groups write "S4"/"4th Season", never AniList's exact title; the season
    # gate in rank()/match() filters any wrong-cour results the broad query
    # returns.
    titles = list(
        dict.fromkeys(
            match._strip_season(match.normalize(t))
            for t in [(series["synonyms"] or [series["title"]])[0], series["title"]]
        )
    )

    for ep in missing:
        if await db.active_grab(conn, series["anilist_id"], ep):
            continue
        ranked = []
        best = None
        for title in titles:
            query = title if ep is None else f"{title} {ep + series['episode_offset']:02d}"
            try:
                results = await rss.nyaa_search(http, query, nyaa_url)
            except Exception as e:
                log.error("nyaa search failed", extra={"event": "error", "query": query, "error": str(e)})
                return
            ranked = rank(results, series, ep, blocklist)
            if ranked:
                best = ranked[0][1]
                break
        if best is None:
            log.info("nothing found", extra={"event": "search_miss", "anilist_id": series["anilist_id"], "episode": ep})
            continue
        log.info(
            "grab",
            extra={
                "event": "grab", "source": "search", "client": "qbittorrent",
                "anilist_id": series["anilist_id"], "series": series["title"],
                "episode": ep, "release": best["title"], "dry_run": dry_run,
            },
        )
        if dry_run:
            continue
        await qbit.add(best["url"])
        await db.insert_grab(
            conn, series["anilist_id"], ep, "search", "qbittorrent", best.get("infohash"), best["title"]
        )
    if not dry_run:
        await db.mark_searched(conn, series["anilist_id"])
