"""Anime-Lists/anime-lists ingest: AniDB <-> TVDB/TMDB cross-source identity.

anime-list-master.xml is the community-curated map Shoko, Sonarr's anime mode
and the Jellyfin AniDB plugin all share. Per AniDB anime it gives the TVDB
series + default season, the episode offset (tvdb episode = anidb episode +
offset — the thing kodarr previously stored as hand-entered
series.episode_offset), and where each AniDB special lands in TVDB season 0
(';1-2;' = special S1 -> S0E2). Refreshed weekly into anidb_map.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET

import httpx
from psycopg import AsyncConnection

log = logging.getLogger(__name__)

URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"


def _specials(anime: ET.Element) -> dict[str, int]:
    """{anidb special number: tvdb S0 episode} from mapping-list ';a-b;' pairs."""
    out: dict[str, int] = {}
    ml = anime.find("mapping-list")
    if ml is None:
        return out
    for m in ml.findall("mapping"):
        if m.get("anidbseason") != "0" or m.get("tvdbseason") != "0":
            continue
        for a, b in re.findall(r";(\d+)-(\d+)", m.text or ""):
            out[a] = int(b)
    return out


def parse(xml_text: str) -> list[tuple[int, str | None, str | None, int, dict[str, int]]]:
    rows = []
    for anime in ET.fromstring(xml_text):
        aid = anime.get("anidbid")
        if not aid or not aid.isdigit():
            continue
        off = anime.get("episodeoffset")
        rows.append((
            int(aid),
            anime.get("tvdbid"),
            anime.get("defaulttvdbseason"),
            int(off) if off and off.lstrip("-").isdigit() else 0,
            _specials(anime),
        ))
    return rows


async def refresh(conn: AsyncConnection, http: httpx.AsyncClient) -> None:
    r = await http.get(URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    rows = [(a, t, s, o, json.dumps(sp)) for a, t, s, o, sp in parse(r.text)]
    async with conn.transaction():
        await conn.execute("DELETE FROM anidb_map")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO anidb_map (anidb_id, tvdb_id, default_tvdb_season, episode_offset, special_map)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (anidb_id) DO NOTHING""",
                rows,
            )
    log.info("anime-lists map refreshed", extra={"event": "animelists_refresh", "rows": len(rows)})


async def refresh_if_stale(conn: AsyncConnection, http: httpx.AsyncClient, days: int = 7) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM anidb_map WHERE updated_at > now() - make_interval(days => %s) LIMIT 1", (days,)
    )
    if await cur.fetchone() is None:
        await refresh(conn, http)
