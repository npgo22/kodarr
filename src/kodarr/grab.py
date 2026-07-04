"""Shared match-and-grab path for RSS items and autobrr announces (torrents)."""

from __future__ import annotations

import logging
import re

from psycopg import AsyncConnection

from kodarr import db, match
from kodarr.clients import Qbit

log = logging.getLogger(__name__)


async def consider(
    conn: AsyncConnection,
    qbit: Qbit,
    release_name: str,
    download_url: str,
    source: str,  # 'rss' | 'autobrr'
    *,
    dry_run: bool = False,
) -> bool:
    """Grab release_name if it's a monitored, missing, not-in-flight episode."""
    parsed = match.parse(release_name)
    if parsed is None:
        return False
    m = match.match(parsed, await db.monitored_series(conn))
    if m is None:
        return False
    series, episode = m

    if series["format"] != "MOVIE" and episode is None:
        return False  # batch or unnumbered; backfill search handles those
    abs_num = 1 if series["format"] == "MOVIE" else episode
    assert abs_num is not None

    # only grab from the series' preferred group on the announce path
    if parsed.group and parsed.group.lower() != series["preferred_group"].lower():
        return False

    existing = await db.get_episode(conn, series["anilist_id"], abs_num)
    if existing and existing["file_path"]:
        return False
    if await db.active_grab(conn, series["anilist_id"], abs_num):
        return False
    if release_name in await db.failed_release_names(conn, series["anilist_id"]):
        return False

    log.info(
        "grab",
        extra={
            "event": "grab",
            "source": source,
            "client": "qbittorrent",
            "anilist_id": series["anilist_id"],
            "series": series["title"],
            "episode": abs_num,
            "group": parsed.group,
            "release": release_name,
            "dry_run": dry_run,
        },
    )
    if dry_run:
        return True
    await qbit.add(download_url)
    # infohash (from magnet links) lets the watcher match the torrent exactly;
    # non-magnet grabs fall back to name matching
    m = re.search(r"btih:([^&]+)", download_url)
    infohash = m.group(1).lower() if m else None
    await db.insert_grab(conn, series["anilist_id"], abs_num, source, "qbittorrent", infohash, release_name)
    return True
