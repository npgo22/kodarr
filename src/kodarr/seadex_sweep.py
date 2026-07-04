"""SeaDex best-release sweep: upgrade library entries to the curated best.

Named seadex_sweep (not seadex) so the PyPI `seadex` client stays importable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg import AsyncConnection
from seadex import EntryNotFoundError, SeaDexEntry, TorrentRecord

from kodarr import db, match
from kodarr.clients import Prowlarr, Qbit, Sab

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
    """Best public torrent with an infohash. SeaDex often lists the same
    release on Nyaa and AnimeTosho; any one of them works as a magnet."""
    candidates = [t for t in torrents if t.is_best and t.tracker.is_public() and t.infohash]
    return candidates[0] if candidates else None


async def sweep_series(
    conn: AsyncConnection,
    seadex_entry: SeaDexEntry,
    prowlarr: Prowlarr,
    qbit: Qbit,
    sab: Sab,
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
        # ponytail: fully-seadex series are never re-queried, so a changed SeaDex
        # pick goes unnoticed — `kodarr seadex --force` re-checks everything.
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

    # prefer usenet: same group + title via Prowlarr newznab (AnimeTosho mirrors most of Nyaa)
    client, url, client_id = "qbittorrent", magnet(best), None
    romaji = (series["synonyms"] or [series["title"]])[0]
    try:
        results = await prowlarr.search(f"{romaji} {best.release_group}")
        # the candidate must actually BE this series: groups like smol renumber
        # franchise packs ("Monogatari Season 7" = Owarimonogatari), so a
        # group+substring match alone imported the wrong show. When in doubt,
        # the SeaDex magnet is the exact curated content — fall back to it.
        usenet = [
            r for r in results
            if r["protocol"] == "usenet"
            and best.release_group.lower() in r["title"].lower()
            and (p := match.parse(r["title"])) is not None
            and match.match(p, [series]) is not None
        ]
        if usenet:
            client, url = "sabnzbd", usenet[0]["url"]
            release_name = usenet[0]["title"]
    except Exception as e:
        log.error("prowlarr search failed", extra={"event": "error", "error": str(e)})

    if release_name in await db.failed_release_names(conn, series["anilist_id"]):
        return  # blocklisted: this exact release already failed once

    log.info(
        "seadex grab",
        extra={
            "event": "grab", "source": "seadex", "client": client,
            "anilist_id": series["anilist_id"], "series": series["title"],
            "group": best.release_group, "release": release_name, "dry_run": dry_run,
        },
    )
    if dry_run:
        return
    if client == "sabnzbd":
        client_id = await sab.add(url, release_name)
    else:
        await qbit.add(url)
        client_id = best.infohash  # lets the watcher match the finished torrent
    await db.insert_grab(conn, series["anilist_id"], None, "seadex", client, client_id, release_name)


async def sweep_all(
    conn: AsyncConnection, seadex_entry: SeaDexEntry, prowlarr: Prowlarr, qbit: Qbit, sab: Sab,
    *, dry_run: bool = False, force: bool = False,
) -> None:
    for series in await db.monitored_series(conn):
        try:
            await sweep_series(conn, seadex_entry, prowlarr, qbit, sab, series, dry_run=dry_run, force=force)
        except Exception:
            log.exception("seadex sweep failed", extra={"event": "error", "anilist_id": series["anilist_id"]})
        await asyncio.sleep(1)  # be polite to SeaDex — it's a small community service
