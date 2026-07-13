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
            if parsed.season == 0:
                # S00 extras (OVAs, recaps) ride along in a season/BD batch but
                # aren't a monitored entry of their own. Drop them in the show's
                # Specials (Season 00) folder so Jellyfin surfaces them instead of
                # silently discarding files we already downloaded. Not tracked in
                # the episodes table: their upstream numbering would collide with
                # real episodes on the (anilist_id, absolute_number) key.
                sp_row = {**row, "season": 0}
                sp_ep = parsed.episode or 1
                sp = organize.dest_path(sp_row, sp_ep, parsed.group, src.suffix.lower())
                if not sp.exists():
                    organize.import_file(src, sp)
                    nfo.write_episode(sp, sp_row, sp_ep, None)
                    touched_dirs.add(str(sp.parent))
                    log.info("imported special", extra={
                        "event": "import", "file": src.name,
                        "anilist_id": row["anilist_id"], "episode": sp_ep})
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
