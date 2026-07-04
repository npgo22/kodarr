"""Library layout: per-AniList-entry folders, absolute numbering, hardlink imports."""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str) -> str:
    return " ".join(_FORBIDDEN.sub(" ", name).split()).rstrip(". ")


def series_dir(series: dict[str, Any]) -> Path:
    """<root>/Title (Year) [anilist-ID]"""
    title = sanitize(series["title"])
    year = f" ({series['year']})" if series.get("year") else ""
    return Path(series["root_path"]) / f"{title}{year} [anilist-{series['anilist_id']}]"


def dest_path(series: dict[str, Any], episode: int | None, group: str | None, ext: str) -> Path:
    title = sanitize(series["title"])
    grp = f" [{sanitize(group)}]" if group else ""
    if series["format"] == "MOVIE" or episode is None:
        year = f" ({series['year']})" if series.get("year") else ""
        name = f"{title}{year}{grp}{ext}"
    else:
        name = f"{title} - {episode:03d}{grp}{ext}"
    return series_dir(series) / name


def import_file(src: Path, dest: Path, *, replace: Path | None = None) -> None:
    """Hardlink src into the library (copy fallback across filesystems).
    Optionally delete the file being upgraded away. Never deletes src —
    torrents keep seeding from the downloads dir."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        tmp.hardlink_to(src)
    except OSError:
        shutil.copy2(src, tmp)  # cross-filesystem
    tmp.replace(dest)  # atomic; safe if dest already exists
    if replace and replace != dest and replace.exists():
        replace.unlink()
        log.info("removed replaced file", extra={"event": "replace", "path": str(replace)})
