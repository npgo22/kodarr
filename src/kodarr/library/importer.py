"""Import completed downloads into the library and notify Jellyfin."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

import httpx

from kodarr import db
from kodarr.library import match
from kodarr.metadata import nfo
from kodarr.library import organize
from kodarr.clients import Jellyfin
from kodarr.library.match import VIDEO_EXTS

log = logging.getLogger(__name__)


def _video_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in VIDEO_EXTS else []
    return sorted(
        p for p in path.rglob("*")
        if p.suffix.lower() in VIDEO_EXTS and "sample" not in p.stem.lower()
    )


async def special_slot(conn: AsyncConnection, anilist_id: int, sp_num: int) -> tuple[int, str | None]:
    """(display number, title) for special #sp_num: the anime-lists TVDB S0
    episode slot + the AniDB special's title, falling back to the raw number."""
    cur = await conn.execute(
        """SELECT am.special_map, ae.title_en
           FROM id_map m
           LEFT JOIN anidb_map am USING (anidb_id)
           LEFT JOIN anidb_episodes ae ON ae.anidb_id = m.anidb_id AND ae.type = 2 AND ae.number = %s
           WHERE m.anilist_id = %s""",
        (sp_num, anilist_id),
    )
    r = await cur.fetchone()
    if r is None:
        return sp_num, None
    return int((r["special_map"] or {}).get(str(sp_num), sp_num)), r["title_en"]


async def _import_special(conn: AsyncConnection, row: dict[str, Any], sp_num: int,
                          group: str | None, src: Path, touched_dirs: set[str]) -> None:
    """File special #sp_num of this entry under the show's Season 00."""
    disp, title = await special_slot(conn, row["anilist_id"], sp_num)
    sp_row = {**row, "season": 0}
    dest = organize.dest_path(sp_row, disp, group, src.suffix.lower())
    if dest.exists():
        return
    organize.import_file(src, dest)
    nfo.write_episode(dest, sp_row, disp, title)
    touched_dirs.add(str(dest.parent))
    log.info("imported special", extra={
        "event": "import", "file": src.name, "anilist_id": row["anilist_id"],
        "episode": disp, "title": title})


async def import_path(
    conn: AsyncConnection,
    jellyfin: Jellyfin | None,
    path: Path,
    *,
    http: httpx.AsyncClient | None = None,  # for inline NFO enrichment
    tmdb=None,  # Tmdb client; enriches titles/stills at import time
    series: dict[str, Any] | None = None,  # known from the grab; else matched by title
    from_seadex: bool = False,
) -> int:
    """Import every video file under path. Returns number of files imported."""
    all_series = await db.monitored_series(conn)
    imported = 0
    touched_dirs: set[str] = set()
    imported_entries: dict[int, dict] = {}

    for src in _video_files(path):
        parsed = match.parse(src.name)
        if parsed is None:
            log.warning("unparseable file", extra={"event": "match_fail", "file": src.name})
            continue

        row, episode = series, None
        if row is not None and row["format"] == "MOVIE":
            # a movie grab IS the movie however the release names it — SeaDex
            # indexes movies as franchise specials (e.g. S00E09), so the season/
            # episode gate below would wrongly reject or misroute it.
            # ponytail: single feature file assumed; a multi-file movie pack with
            # extras would need largest-file selection.
            episode = None
        elif row is not None:
            if parsed.season == 0 or (
                parsed.episode is not None and parsed.episode - row["episode_offset"] == 0
            ):
                # Specials: an explicit S00 extra in a pack, OR a release
                # numbered 00 (groups slot a pre-season special before ep 1 —
                # MTBB "S2 - 00" is Guardian Fitz, not episode 1). Filed under
                # the show's Season 00 with the AniDB special's title and its
                # anime-lists TVDB S0 slot so TMDB/TVDB numbering agrees.
                # Not tracked in the episodes table: special numbering would
                # collide with real episodes on (anilist_id, absolute_number).
                # ponytail: a bare 00 is assumed to be special #1; exact ID
                # needs file hashing.
                sp_num = parsed.episode if parsed.season == 0 and parsed.episode else 1
                await _import_special(conn, row, sp_num, parsed.group, src, touched_dirs)
                continue
            if parsed.episode is not None:
                episode = parsed.episode - row["episode_offset"]
                total = row.get("episodes") or (row.get("aired") or 0) + 1
                if not 1 <= episode <= total:
                    # packs can span split cours; route overflow through full matching
                    m = match.match(parsed, all_series)
                    if m is None:
                        log.warning("pack file out of range", extra={"event": "match_fail", "file": src.name})
                        continue
                    row, episode = m
        else:
            m = match.match(parsed, all_series)
            if m is None:
                log.warning("no series match", extra={"event": "match_fail", "file": src.name})
                continue
            row, episode = m

        if row["format"] != "MOVIE" and episode is None:
            log.warning("no episode number", extra={"event": "match_fail", "file": src.name})
            continue
        abs_num = 1 if row["format"] == "MOVIE" else episode
        assert abs_num is not None

        existing = await db.get_episode(conn, row["anilist_id"], abs_num)
        replace = None
        if existing and existing["file_path"]:
            if existing["from_seadex"] and not from_seadex:
                log.info(
                    "skip: seadex copy already on disk",
                    extra={"event": "skip", "file": src.name, "anilist_id": row["anilist_id"]},
                )
                continue
            replace = Path(existing["file_path"])

        dest = organize.dest_path(row, abs_num, parsed.group, src.suffix.lower())
        organize.import_file(src, dest, replace=replace)
        await db.upsert_episode(conn, row["anilist_id"], abs_num, str(dest), parsed.group, from_seadex, src.name)
        if row["format"] != "MOVIE":
            # placeholder title now; refresh_series below fills real titles
            nfo.write_episode(dest, row, abs_num, (existing or {}).get("title"))
        touched_dirs.add(str(dest.parent))
        imported_entries[row["anilist_id"]] = row
        imported += 1
        log.info(
            "imported",
            extra={
                "event": "upgrade" if replace else "import",
                "anilist_id": row["anilist_id"],
                "series": row["title"],
                "episode": abs_num,
                "group": parsed.group,
                "seadex": from_seadex,
            },
        )

    # full metadata refresh for each touched entry, inline: show/season NFOs
    # plus episode titles/stills from AniList+TMDB. Jellyfin's realtime monitor
    # then picks up complete metadata in one scan instead of a stub that waits
    # up to a day for the nfo pass.
    for row in imported_entries.values():
        if http is None:
            break
        try:
            await nfo.refresh_series(conn, http, tmdb, row)
        except Exception:
            log.exception("inline nfo refresh failed", extra={"event": "error", "anilist_id": row["anilist_id"]})

    if jellyfin:
        for d in touched_dirs:
            await jellyfin.notify(d)
    return imported
