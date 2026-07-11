"""Extra title aliases from the AniDB anime-titles dump.

AniList lists few synonyms, so a release whose group names a show differently
(romaji vs english, a dropped subtitle, an alternate romanisation) fails the
exact title/synonym set-match and gets dropped. AniDB's anime-titles dump is
the canonical, daily-regenerated superset of aliases; we key it to AniList via
the anidb_id the Fribb id map already carries. Refreshed weekly, merged into
series.synonyms at read time (see db.monitored_series).

Replaces manami-project/anime-offline-database, which was archived 2026-07 (a
static synonym dump rots as new shows air, so a live source is required).
"""

from __future__ import annotations

import gzip
import logging

import httpx
from psycopg import AsyncConnection

log = logging.getLogger(__name__)

# pipe-delimited "<aid>|<type>|<language>|<title>"; https only + a real UA
# (anidb 403s http and blank-UA clients). type 1=primary 2=synonym 3=short 4=official
URL = "https://anidb.net/api/anime-titles.dat.gz"
_UA = "kodarr/1.0 (+https://github.com/npgo22/kodarr)"
# romaji transcription (x-jat), english and native japanese are what release
# names use; drop type 3 abbreviations ("FMA") and other languages as matcher noise
_KEEP_TYPES = {"1", "2", "4"}
_KEEP_LANGS = {"x-jat", "en", "ja"}


def _parse(dump: str, aid_to_anilist: dict[int, int]) -> dict[int, list[str]]:
    """{anilist_id: [titles]} for the aids that map to an AniList entry."""
    out: dict[int, list[str]] = {}
    for line in dump.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        aid_s, typ, lang, title = parts
        # keep the primary title regardless of language; filter synonyms/official by language
        if typ not in _KEEP_TYPES or (typ != "1" and lang not in _KEEP_LANGS):
            continue
        anilist_id = aid_to_anilist.get(int(aid_s))
        if anilist_id is None:
            continue
        titles = out.setdefault(anilist_id, [])
        if title not in titles:
            titles.append(title)
    return out


async def refresh(conn: AsyncConnection, http: httpx.AsyncClient) -> None:
    """Re-download the AniDB dump (~1.5 MB gz) and store aliases per AniList id.
    Depends on id_map.anidb_id, so runs after the Fribb mapping refresh."""
    cur = await conn.execute("SELECT anidb_id, anilist_id FROM id_map WHERE anidb_id IS NOT NULL")
    aid_to_anilist = {r["anidb_id"]: r["anilist_id"] for r in await cur.fetchall()}
    r = await http.get(URL, headers={"User-Agent": _UA}, timeout=120)
    r.raise_for_status()
    dump = gzip.decompress(r.content).decode("utf-8", "replace")
    rows = list(_parse(dump, aid_to_anilist).items())
    async with conn.transaction():
        await conn.execute("DELETE FROM title_synonyms")
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO title_synonyms (anilist_id, synonyms) VALUES (%s, %s) ON CONFLICT (anilist_id) DO NOTHING",
                rows,
            )
    log.info("title synonyms refreshed", extra={"event": "synonyms_refresh", "rows": len(rows)})


async def refresh_if_stale(conn: AsyncConnection, http: httpx.AsyncClient, days: int = 7) -> None:
    cur = await conn.execute(
        "SELECT 1 FROM title_synonyms WHERE updated_at > now() - make_interval(days => %s) LIMIT 1", (days,)
    )
    if await cur.fetchone() is None:
        await refresh(conn, http)
