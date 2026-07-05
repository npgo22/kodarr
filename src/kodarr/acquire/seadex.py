"""SeaDex best-release sweep: upgrade finished series to the curated best."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg import AsyncConnection
from seadex import EntryNotFoundError, SeaDexEntry, TorrentRecord

from kodarr import db
from kodarr.clients import Qbit

log = logging.getLogger(__name__)

# open trackers appended to infohash magnets so grabs work even with slow DHT
_TRACKERS = "&tr=" + "&tr=".join(
    [
        "http://nyaa.tracker.wf:7777/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.opentrackr.org:1337/announce",
    ]
)


def magnet(torrent: TorrentRecord) -> str:
    return f"magnet:?xt=urn:btih:{torrent.infohash}{_TRACKERS}"


def pick_best(torrents: tuple[TorrentRecord, ...]) -> TorrentRecord | None:
    """Best public torrent with an infohash; if every "best" is private-only,
    fall back to the entry's public alt (still curated, not the #1 pick)."""
    public = [t for t in torrents if t.tracker.is_public() and t.infohash]
    best = [t for t in public if t.is_best]
    if best:
        return best[0]
    return public[0] if public else None


async def sweep_series(
    conn: AsyncConnection,
    seadex_entry: SeaDexEntry,
    qbit: Qbit,
    series: dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    # every check that can skip the SeaDex API call comes before it
    if series["format"] != "MOVIE" and series["status"] == "RELEASING":
        return  # wait for the season to finish before grabbing a pack
    if await db.active_grab(conn, series["anilist_id"], None):
        return
    total = series["episodes"] if series["format"] != "MOVIE" else 1
    if not force:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM episodes WHERE anilist_id = %s AND from_seadex",
            (series["anilist_id"],),
        )
        row = await cur.fetchone()
        # fully-upgraded series are not re-queried; --force re-checks everything
        if row and total and row["n"] >= total:
            return

    try:
        # seadex lib is sync httpx; don't block the loop
        entry = await asyncio.to_thread(seadex_entry.from_id, series["anilist_id"])
    except EntryNotFoundError:
        return
    best = pick_best(entry.torrents)
    if best is None:
        return

    # already fully on this exact release group?
    cur = await conn.execute(
        """SELECT count(*) AS n FROM episodes
           WHERE anilist_id = %s AND from_seadex AND lower(release_group) = lower(%s)""",
        (series["anilist_id"], best.release_group),
    )
    row = await cur.fetchone()
    if row and total and row["n"] >= total:
        return

    release_name = best.files[0].name if best.files else series["title"]

    log.info(
        "seadex grab",
        extra={
            "event": "grab", "source": "seadex", "client": "qbittorrent",
            "anilist_id": series["anilist_id"], "series": series["title"],
            "group": best.release_group, "release": release_name, "dry_run": dry_run,
        },
    )
    if dry_run:
        return
    await qbit.add(magnet(best))
    await db.insert_grab(conn, series["anilist_id"], None, "seadex", "qbittorrent", best.infohash, release_name)


async def sweep_all(
    conn: AsyncConnection, seadex_entry: SeaDexEntry, qbit: Qbit,
    *, dry_run: bool = False, force: bool = False,
) -> None:
    for series in await db.monitored_series(conn):
        try:
            await sweep_series(conn, seadex_entry, qbit, series, dry_run=dry_run, force=force)
        except Exception:
            log.exception("seadex sweep failed", extra={"event": "error", "anilist_id": series["anilist_id"]})
        await asyncio.sleep(1)  # be polite to SeaDex — it's a small community service
