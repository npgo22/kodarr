"""AniDB per-episode identity: the ground truth the rest of metadata hangs off.

Source is Shoko's Anime_HTTP.zip — a bulk cache of AniDB HTTP-API anime XMLs
(one AnimeDoc_<aid>.xml per anime, ~15k anime, 147MB) that sidesteps AniDB's
aggressive rate limits entirely. Kept on the media PVC and re-downloaded
monthly. Entries missing from the zip (it lags new shows by months) fall back
to the live AniDB HTTP API when cfg.anidb_client is set (register a client at
anidb.net); otherwise they stay queued (anidb_mapped IS NULL) and resolve when
the zip catches up.

resolve_pass() is the background mapping queue the user model expects: a
series may be initially mis-numbered from AniList guesses, then self-heals
when its AniDB data lands — episode counts, release offsets and specials all
become derived data instead of manual overrides.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import httpx
from psycopg import AsyncConnection

log = logging.getLogger(__name__)

CACHE_URL = "https://files.shokoanime.com/files/shoko-server/other/Anime_HTTP.zip"
CACHE_MAX_AGE = 30 * 86400
LIVE_URL = "http://api.anidb.net:9001/httpapi"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def parse_anime(xml_text: str) -> dict:
    """{'episodecount', 'enddate', 'episodes': [row dicts]} from an AniDB anime XML."""
    root = ET.fromstring(xml_text)
    eps = []
    eps_el = root.find("episodes")
    for ep in (eps_el if eps_el is not None else []):
        epno = ep.find("epno")
        if epno is None or not epno.text:
            continue
        num = re.sub(r"\D", "", epno.text)
        titles = {t.get(_XML_LANG): t.text for t in ep.findall("title")}
        eps.append({
            "epno": epno.text,
            "type": int(epno.get("type") or 0),
            "number": int(num) if num else 0,
            "title_en": titles.get("en"),
            "title_romaji": titles.get("x-jat"),
            "airdate": ep.findtext("airdate") or None,
            "length_min": int(ep.findtext("length") or 0) or None,
        })
    count = root.findtext("episodecount")
    return {
        "episodecount": int(count) if count and count.isdigit() else None,
        "enddate": root.findtext("enddate") or None,
        "episodes": eps,
    }


async def ensure_cache(http: httpx.AsyncClient, path: Path) -> Path | None:
    """Download/refresh the Shoko zip. Returns the path, or None on failure."""
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_MAX_AGE:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".zip.partial")
    try:
        async with http.stream("GET", CACHE_URL, timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)
        tmp.replace(path)
        log.info("anidb cache downloaded", extra={"event": "anidb_cache", "bytes": path.stat().st_size})
        return path
    except (httpx.HTTPError, OSError) as e:
        tmp.unlink(missing_ok=True)
        log.warning("anidb cache download failed", extra={"event": "error", "error": str(e)})
        return path if path.exists() else None  # a stale zip beats no zip


def from_zip(path: Path, anidb_id: int) -> dict | None:
    try:
        with zipfile.ZipFile(path) as z:
            return parse_anime(z.read(f"Anime_HTTP/AnimeDoc_{anidb_id}.xml").decode("utf-8", "replace"))
    except KeyError:
        return None  # not in this snapshot (new show) — stays queued
    except (zipfile.BadZipFile, ET.ParseError, OSError) as e:
        log.warning("anidb cache read failed", extra={"event": "error", "anidb_id": anidb_id, "error": str(e)})
        return None


async def fetch_live(http: httpx.AsyncClient, anidb_id: int, client: str) -> dict | None:
    """Live AniDB HTTP API. Needs a registered client name; heavily rate-limited
    (we sleep between calls and only ever fetch a handful per pass)."""
    await asyncio.sleep(4)  # ponytail: global-ish pacing; fine while callers are sequential
    r = await http.get(LIVE_URL, params={
        "request": "anime", "client": client, "clientver": "1", "protover": "1", "aid": anidb_id,
    }, timeout=60)
    r.raise_for_status()
    if b"<error" in r.content[:200]:  # AniDB returns 200 with an <error> body (e.g. banned)
        log.warning("anidb api error", extra={"event": "error", "anidb_id": anidb_id, "body": r.text[:120]})
        return None
    return parse_anime(r.text)


async def resolve_series(conn: AsyncConnection, s: dict, anidb_id: int, anime: dict) -> None:
    """Store episode identity and derive what used to be manual overrides."""
    rows = [(anidb_id, e["epno"], e["type"], e["number"], e["title_en"], e["title_romaji"],
             e["airdate"], e["length_min"]) for e in anime["episodes"]]
    async with conn.transaction():
        await conn.execute("DELETE FROM anidb_episodes WHERE anidb_id = %s", (anidb_id,))
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO anidb_episodes (anidb_id, epno, type, number, title_en, title_romaji, airdate, length_min)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        cur = await conn.execute(
            "SELECT episode_offset, season_map FROM anidb_map WHERE anidb_id = %s", (anidb_id,))
        am = await cur.fetchone()
        # AniDB's regular-episode count beats AniList's — the sources disagree on
        # split-cour boundaries (Mushoku S2 part 1: AniList 13, AniDB 12) and
        # AniDB matches how release groups and TVDB/TMDB number things. Only for
        # ended anime: a stale cache snapshot must not truncate an airing show.
        # Exception: entries whose anime-lists map sends "regular" episodes to
        # E0 (recap marker) — there the broadcast episodes are typed as AniDB
        # SPECIALS (Owarimonogatari S2: 7 eps on TV, AniDB regular count 2), so
        # the regular count is meaningless for our numbering.
        regular = sum(1 for e in anime["episodes"] if e["type"] == 1)
        pairs = ((am or {}).get("season_map") or {}).get("pairs") or {}
        recapish = any(v == 0 for v in pairs.values())
        if recapish:
            log.warning("anidb regular eps are recaps per anime-lists — count not corrected", extra={
                "event": "anidb_resolve", "anilist_id": s["anilist_id"], "regular": regular})
        elif anime["enddate"] and regular and regular != (s.get("episodes") or 0):
            log.info("episode count corrected", extra={
                "event": "anidb_resolve", "anilist_id": s["anilist_id"],
                "old": s.get("episodes"), "new": regular})
            await conn.execute(
                "UPDATE series SET episodes = %s, aired = %s WHERE anilist_id = %s",
                (regular, regular, s["anilist_id"]))
        # release/tvdb episode offset from anime-lists (replaces manual entry)
        if am is not None and am["episode_offset"] != s.get("episode_offset", 0):
            log.info("episode offset derived", extra={
                "event": "anidb_resolve", "anilist_id": s["anilist_id"],
                "old": s.get("episode_offset"), "new": am["episode_offset"]})
            await conn.execute(
                "UPDATE series SET episode_offset = %s WHERE anilist_id = %s",
                (am["episode_offset"], s["anilist_id"]))
        await conn.execute(
            "UPDATE series SET anidb_mapped = now() WHERE anilist_id = %s", (s["anilist_id"],))


async def resolve_pass(conn: AsyncConnection, http: httpx.AsyncClient, cache_dir: str,
                       live_client: str = "") -> None:
    """Resolve every queued series (anidb_id known, not yet mapped)."""
    cur = await conn.execute(
        """SELECT s.*, m.anidb_id AS _aid FROM series s JOIN id_map m USING (anilist_id)
           WHERE m.anidb_id IS NOT NULL AND s.anidb_mapped IS NULL""")
    queued = await cur.fetchall()
    if not queued:
        return
    zip_path = await ensure_cache(http, Path(cache_dir) / "Anime_HTTP.zip")
    for s in queued:
        anime = from_zip(zip_path, s["_aid"]) if zip_path else None
        if anime is None and live_client:
            try:
                anime = await fetch_live(http, s["_aid"], live_client)
            except httpx.HTTPError as e:
                log.warning("anidb live fetch failed", extra={"event": "error", "anidb_id": s["_aid"], "error": str(e)})
        if anime is None:
            continue  # stays queued; picked up on a later pass / fresher zip
        await resolve_series(conn, s, s["_aid"], anime)
        log.info("anidb resolved", extra={
            "event": "anidb_resolve", "anilist_id": s["anilist_id"],
            "anidb_id": s["_aid"], "episodes": len(anime["episodes"])})
