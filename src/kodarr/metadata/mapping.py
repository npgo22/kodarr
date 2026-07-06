"""TVDB/TMDB -> AniList id mapping via Fribb/anime-lists.

Seerr speaks TVDB (TV) and TMDB (movies); kodarr speaks AniList. One TVDB
series maps to several AniList entries (one per season) — a request adds all
of them.
"""

from __future__ import annotations

import logging

import httpx
from psycopg import AsyncConnection

log = logging.getLogger(__name__)

URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"


async def refresh(conn: AsyncConnection, http: httpx.AsyncClient) -> None:
    """Re-download the mapping (~13 MB). Called weekly, or when the table is empty."""
    r = await http.get(URL, timeout=120)
    r.raise_for_status()
    rows = []
    for e in r.json():
        anilist_id = e.get("anilist_id")
        if not anilist_id:
            continue
        tmdb = e.get("themoviedb_id")
        movie = tv = None
        if isinstance(tmdb, dict):
            m = tmdb.get("movie")
            movie = m[0] if isinstance(m, list) and m else m if isinstance(m, int) else None
            t = tmdb.get("tv")
            tv = t[0] if isinstance(t, list) and t else t if isinstance(t, int) else None
        tvdb = e.get("tvdb_id")
        if not tvdb and not movie and not tv:
            continue
        season = e.get("season") or {}
        rows.append((anilist_id, tvdb, movie, season.get("tvdb"), tv, season.get("tmdb")))
    async with conn.transaction():
        await conn.execute("DELETE FROM id_map")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO id_map (anilist_id, tvdb_id, tmdb_movie_id, tvdb_season, tmdb_tv_id, tmdb_season)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (anilist_id) DO NOTHING""",
                rows,
            )
        # manual corrections survive the rewrite; overrides with a tv id also
        # cover entries Fribb has no row for at all
        await conn.execute(
            """INSERT INTO id_map (anilist_id, tmdb_tv_id, tmdb_season)
               SELECT anilist_id, tmdb_tv_id, tmdb_season FROM id_map_overrides
               WHERE tmdb_tv_id IS NOT NULL
               ON CONFLICT (anilist_id) DO NOTHING"""
        )
        await conn.execute(
            """UPDATE id_map m SET tmdb_season = o.tmdb_season,
                                   tmdb_tv_id  = COALESCE(o.tmdb_tv_id, m.tmdb_tv_id)
               FROM id_map_overrides o WHERE o.anilist_id = m.anilist_id"""
        )
    log.info("id mapping refreshed", extra={"event": "mapping_refresh", "rows": len(rows)})


async def refresh_if_stale(conn: AsyncConnection, http: httpx.AsyncClient, days: int = 7) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM id_map WHERE updated_at > now() - make_interval(days => %s) LIMIT 1", (days,)
    )
    if await cur.fetchone() is None:
        await refresh(conn, http)
