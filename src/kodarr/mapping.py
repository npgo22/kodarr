"""TVDB/TMDB -> AniList id mapping via Fribb/anime-lists.

Jellyseerr speaks TVDB (TV) and TMDB (movies); kodarr speaks AniList. One TVDB
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
        movie = None
        if isinstance(tmdb, dict):
            m = tmdb.get("movie")
            movie = m[0] if isinstance(m, list) and m else m if isinstance(m, int) else None
        tvdb = e.get("tvdb_id")
        if not tvdb and not movie:
            continue
        rows.append((anilist_id, tvdb, movie, (e.get("season") or {}).get("tvdb")))
    async with conn.transaction():
        await conn.execute("DELETE FROM id_map")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO id_map (anilist_id, tvdb_id, tmdb_movie_id, tvdb_season)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (anilist_id) DO NOTHING""",
                rows,
            )
    log.info("id mapping refreshed", extra={"event": "mapping_refresh", "rows": len(rows)})


async def refresh_if_stale(conn: AsyncConnection, http: httpx.AsyncClient, days: int = 7) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM id_map WHERE updated_at > now() - make_interval(days => %s) LIMIT 1", (days,)
    )
    if await cur.fetchone() is None:
        await refresh(conn, http)


async def anilist_ids(
    conn: AsyncConnection, http: httpx.AsyncClient, media_type: str,
    tvdb_id: int | None, tmdb_id: int | None,
) -> list[int]:
    await refresh_if_stale(conn, http)
    if media_type == "movie" and tmdb_id:
        cur = await conn.execute("SELECT anilist_id FROM id_map WHERE tmdb_movie_id = %s", (tmdb_id,))
    elif tvdb_id:
        # tvdb_season 0 = specials/OVAs — don't silently grab those on a show request
        cur = await conn.execute(
            """SELECT anilist_id FROM id_map WHERE tvdb_id = %s
               AND (tvdb_season IS NULL OR tvdb_season > 0) ORDER BY tvdb_season NULLS LAST""",
            (tvdb_id,),
        )
    else:
        return []
    return [r["anilist_id"] for r in await cur.fetchall()]
