"""Import completed downloads into the library and notify Jellyfin."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection

from kodarr import db, match, nfo, organize
from kodarr.clients import Jellyfin
from kodarr.match import VIDEO_EXTS

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
    series: dict[str, Any] | None = None,  # known from the grab; else matched by title
    from_seadex: bool = False,
) -> int:
    """Import every video file under path. Returns number of files imported."""
    all_series = await db.monitored_series(conn)
    imported = 0
    touched_dirs: set[str] = set()

    for src in _video_files(path):
        parsed = match.parse(src.name)
        if parsed is None:
            log.warning("unparseable file", extra={"event": "match_fail", "file": src.name})
            continue

        row, episode = series, None
        if row is not None:
            if parsed.season == 0:
                # S00 extras inside a season pack belong to a different AniList
                # entry (specials are their own entry) — never this one
                log.info("skip pack special", extra={"event": "skip", "file": src.name})
                continue
            if parsed.episode is not None:
                episode = parsed.episode - row["episode_offset"]
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
            # placeholder title now; the daily NFO pass fills real titles
            nfo.write_episode(dest, row, abs_num, (existing or {}).get("title"), source=src.name)
        touched_dirs.add(str(dest.parent))
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

    if jellyfin:
        for d in touched_dirs:
            await jellyfin.notify(d)
    return imported
