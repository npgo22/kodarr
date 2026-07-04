"""Prowlarr backfill search for missing episodes/movies."""

from __future__ import annotations

import logging
from typing import Any

from psycopg import AsyncConnection

from kodarr import db, match
from kodarr.clients import Prowlarr, Qbit, Sab

log = logging.getLogger(__name__)


def rank(
    results: list[dict], series: dict[str, Any], want_ep: int | None, blocklist: frozenset[str] | set[str] = frozenset()
) -> list[tuple[int, dict]]:
    """Filter to results that really are this series+episode, best first.
    Preference: preferred-group torrent (seed the simulcast) > usenet > other torrent."""
    scored = []
    for res in results:
        if res["title"] in blocklist:
            continue
        parsed = match.parse(res["title"])
        if parsed is None:
            continue
        m = match.match(parsed, [series])
        if m is None:
            continue
        _, ep = m
        if series["format"] != "MOVIE" and ep != want_ep:
            continue
        preferred = bool(parsed.group) and parsed.group.lower() == series["preferred_group"].lower()
        if preferred and res["protocol"] == "torrent":
            score = 3
        elif res["protocol"] == "usenet":
            score = 2
        else:
            score = 1
        scored.append((score, res))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored


async def backfill_series(
    conn: AsyncConnection,
    prowlarr: Prowlarr,
    qbit: Qbit,
    sab: Sab,
    series: dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """Search + grab every aired-but-missing episode of one series.

    Backoff: at most one search pass per series per week (metadata refresh
    clears last_search when new episodes air) — otherwise a never-found
    episode hammers every indexer daily, forever.
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

    for ep in missing:
        if await db.active_grab(conn, series["anilist_id"], ep):
            continue
        query = series["title"] if ep is None else f"{series['title']} {ep + series['episode_offset']:02d}"
        try:
            results = await prowlarr.search(query)
        except Exception as e:
            log.error("prowlarr search failed", extra={"event": "error", "query": query, "error": str(e)})
            return
        ranked = rank(results, series, ep, blocklist)
        if not ranked:
            log.info("nothing found", extra={"event": "search_miss", "anilist_id": series["anilist_id"], "episode": ep})
            continue
        _, best = ranked[0]
        client = "sabnzbd" if best["protocol"] == "usenet" else "qbittorrent"
        log.info(
            "grab",
            extra={
                "event": "grab", "source": "search", "client": client,
                "anilist_id": series["anilist_id"], "series": series["title"],
                "episode": ep, "release": best["title"], "dry_run": dry_run,
            },
        )
        if dry_run:
            continue
        client_id = None
        if client == "sabnzbd":
            client_id = await sab.add(best["url"], best["title"])
        else:
            await qbit.add(best["url"])
        await db.insert_grab(conn, series["anilist_id"], ep, "search", client, client_id, best["title"])
    if not dry_run:
        await db.mark_searched(conn, series["anilist_id"])
