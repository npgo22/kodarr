"""Nyaa backfill search for missing episodes and movies."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from psycopg import AsyncConnection

from kodarr import db
from kodarr.library import match
from kodarr.acquire import feeds as rss
from kodarr.clients import Qbit

log = logging.getLogger(__name__)


def search_query(title: str, ep: int | None, fmt: str, episode_offset: int) -> str:
    """Nyaa query for one title. Movies are detected/stored as "episode 1" but
    release names carry no episode number — search the bare title, never
    "Title 01" (which finds nothing). Bundled OVAs/specials are named the same
    way ("... - Eris no Goblin Toubatsu", no number), so they search bare too."""
    if ep is None or fmt == "MOVIE" or fmt in match.SIDE_FORMATS:
        return title
    return f"{title} {ep + episode_offset:02d}"


def search_titles(series: dict[str, Any]) -> list[str]:
    """Nyaa query titles for one entry, best first.

    Query with the romaji base title (synonyms[0] by anilist._clean
    construction) — release groups name in romaji. Strip the season phrase:
    groups write "S4"/"4th Season", never AniList's exact title; the season gate
    in rank()/match() filters any wrong-cour results the broad query returns.
    """
    romaji = (series["synonyms"] or [series["title"]])[0]
    if series["format"] in match.SIDE_FORMATS:
        # A special's whole identity is the suffix. The pre-colon form
        # ("Mushoku Tensei") returns only the parent show, whose episode 1 is
        # indistinguishable from the special's single episode — that is how the
        # parent's S2E01 once got grabbed as the Eris OVA. Query the
        # distinguishing tail instead: "... - Eris no Goblin Toubatsu".
        candidates = [t.split(":", 1)[-1] for t in (romaji, series["title"])] + [romaji, series["title"]]
    else:
        # pre-colon short forms first: groups truncate ("Mushoku Tensei S3")
        candidates = [romaji.split(":")[0], series["title"].split(":")[0], romaji, series["title"]]
    return list(dict.fromkeys(match._strip_season(match.normalize(t)) for t in candidates))


def _single_file(series: dict[str, Any]) -> bool:
    """One release == the whole entry: a movie, or a one-episode OVA/special.
    Such releases usually carry no episode number, so rank() must accept an
    unnumbered (but non-batch) result instead of demanding episode == want."""
    return series["format"] == "MOVIE" or (
        series["format"] in match.SIDE_FORMATS and (series.get("episodes") or 1) == 1
    )


def rank(results: list[dict], series: dict[str, Any], want_ep: int | None) -> list[tuple[tuple, dict]]:
    """Preferred-group releases for this exact series+episode; 1080p floor;
    best resolution first, seeders break ties."""
    scored = []
    for res in results:
        parsed = match.parse(res["title"])
        if parsed is None:
            continue
        if not parsed.group or parsed.group.lower() != series["preferred_group"].lower():
            continue
        m = match.match(parsed, [series])
        if m is None:
            continue
        _, ep = m
        if _single_file(series):
            # batches also parse with episode=None; a movie/OVA must not be a batch
            if parsed.episode is None and match.is_batch(res["title"]):
                continue
        elif ep != want_ep:
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

    titles = search_titles(series)

    for ep in missing:
        if await db.active_grab(conn, series["anilist_id"], ep):
            continue
        ranked = []
        best = None
        for title in titles:
            query = search_query(title, ep, series["format"], series["episode_offset"])
            try:
                results = await rss.nyaa_search(http, query, nyaa_url)
            except Exception as e:
                log.error("nyaa search failed", extra={"event": "error", "query": query, "error": str(e)})
                return
            ranked = rank(results, series, ep)
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
