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


def _mappings(anime: ET.Element) -> tuple[dict[str, int], dict]:
    """(specials, season_map) from the mapping-list.

    specials: {anidb special number: tvdb S0 episode} from anidbseason=0 pairs.
    season_map: for anidbseason=1 entries, where the REGULAR episodes live when
    they don't follow defaulttvdbseason (+episodeoffset) — e.g. Nekomonogatari's
    4 episodes are TVDB S0 E5-8: {'tvdbseason': 0, 'offset': 4, 'pairs': {..}}.
    """
    specials: dict[str, int] = {}
    season_map: dict = {}
    ml = anime.find("mapping-list")
    if ml is None:
        return specials, season_map
    for m in ml.findall("mapping"):
        pairs = {a: int(b) for a, b in re.findall(r";(\d+)-(\d+)", m.text or "")}
        if m.get("anidbseason") == "0" and m.get("tvdbseason") == "0":
            specials.update(pairs)
        elif m.get("anidbseason") == "0":
            # AniDB specials that TVDB counts in-season (Bakemonogatari's web
            # episodes: S1-S3 -> S1 E13-15). Inverted {tvdb_ep: anidb_special}
            # under a reserved key — numeric special_slot lookups never hit it.
            specials["in_season"] = {str(b): int(a) for a, b in pairs.items()}
        elif m.get("anidbseason") == "1":
            off = m.get("offset")
            season_map = {
                "tvdbseason": int(m.get("tvdbseason") or 0),
                "offset": int(off) if off and off.lstrip("-").isdigit() else 0,
                "pairs": pairs,
                "start": int(m.get("start")) if m.get("start") else None,
                "end": int(m.get("end")) if m.get("end") else None,
            }
    return specials, season_map


def parse(xml_text: str) -> list[tuple[int, str | None, str | None, int, dict[str, int], dict]]:
    rows = []
    for anime in ET.fromstring(xml_text):
        aid = anime.get("anidbid")
        if not aid or not aid.isdigit():
            continue
        off = anime.get("episodeoffset")
        specials, season_map = _mappings(anime)
        rows.append((
            int(aid),
            anime.get("tvdbid"),
            anime.get("defaulttvdbseason"),
            int(off) if off and off.lstrip("-").isdigit() else 0,
            specials,
            season_map,
        ))
    return rows


async def refresh(conn: AsyncConnection, http: httpx.AsyncClient) -> None:
    r = await http.get(URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    rows = [(a, t, s, o, json.dumps(sp), json.dumps(sm)) for a, t, s, o, sp, sm in parse(r.text)]
    async with conn.transaction():
        await conn.execute("DELETE FROM anidb_map")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO anidb_map (anidb_id, tvdb_id, default_tvdb_season, episode_offset, special_map, season_map)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (anidb_id) DO NOTHING""",
                rows,
            )
    log.info("anime-lists map refreshed", extra={"event": "animelists_refresh", "rows": len(rows)})


async def refresh_if_stale(conn: AsyncConnection, http: httpx.AsyncClient, days: int = 7) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM anidb_map WHERE updated_at > now() - make_interval(days => %s) LIMIT 1", (days,)
    )
    if await cur.fetchone() is None:
        await refresh(conn, http)
