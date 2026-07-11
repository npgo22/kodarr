"""Extra title aliases from manami-project/anime-offline-database.

AniList lists a handful of synonyms; manami aggregates every alias a show has
across AniDB / MAL / Kitsu / AnimePlanet / AniSearch / etc. Feeding those into
the matcher means a release still routes to the right series when the group
names a show differently from AniList (romaji vs english, a dropped subtitle,
an alternate romanisation). Refreshed weekly like the Fribb id map, and merged
into series.synonyms at read time (see db.monitored_series).
"""

from __future__ import annotations

import logging
import re

import httpx
from psycopg import AsyncConnection

log = logging.getLogger(__name__)

# minified = identical data without the pretty-print whitespace (~1/3 the size)
URL = "https://raw.githubusercontent.com/manami-project/anime-offline-database/master/anime-offline-database-minified.json"

_ANILIST_RE = re.compile(r"anilist\.co/anime/(\d+)")


def _extract(entry: dict) -> tuple[int, list[str]] | None:
    """(anilist_id, [title, *synonyms]) for one manami entry, or None when it
    carries no AniList source (kodarr is AniList-keyed)."""
    aid = next((int(m.group(1)) for s in entry.get("sources", []) if (m := _ANILIST_RE.search(s))), None)
    if aid is None:
        return None
    return aid, list(dict.fromkeys([entry["title"], *entry.get("synonyms", [])]))


async def refresh(conn: AsyncConnection, http: httpx.AsyncClient) -> None:
    """Re-download manami (~20 MB) and store title+synonyms per AniList id."""
    r = await http.get(URL, timeout=180)
    r.raise_for_status()
    rows = [ex for e in r.json()["data"] if (ex := _extract(e)) and ex[1]]
    async with conn.transaction():
        await conn.execute("DELETE FROM manami_synonyms")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO manami_synonyms (anilist_id, synonyms) VALUES (%s, %s)
                   ON CONFLICT (anilist_id) DO NOTHING""",
                rows,
            )
    log.info("manami synonyms refreshed", extra={"event": "synonyms_refresh", "rows": len(rows)})


async def refresh_if_stale(conn: AsyncConnection, http: httpx.AsyncClient, days: int = 7) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM manami_synonyms WHERE updated_at > now() - make_interval(days => %s) LIMIT 1", (days,)
    )
    if await cur.fetchone() is None:
        await refresh(conn, http)
