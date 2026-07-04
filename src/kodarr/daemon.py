"""The kodarr daemon: all loops in one asyncio process."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
from aiohttp import web
from psycopg import AsyncConnection
from seadex import SeaDexEntry

from kodarr import anilist, db, grab, importer, mapping, nfo, rss, search, seadex_sweep, webhook
from kodarr.clients import Jellyfin, Prowlarr, Qbit, Sab
from kodarr.config import Config
from kodarr.tmdb import Tmdb

log = logging.getLogger(__name__)

DAY = 86400


class Daemon:
    def __init__(self, cfg: Config, conn: AsyncConnection):
        self.cfg = cfg
        self.conn = conn
        self.http = httpx.AsyncClient(follow_redirects=True)
        self.qbit = Qbit(self.http, cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, cfg.qbit_category)
        self.sab = Sab(self.http, cfg.sab_url, cfg.sab_api_key, cfg.sab_category)
        self.prowlarr = Prowlarr(self.http, cfg.prowlarr_url, cfg.prowlarr_api_key)
        self.jellyfin = Jellyfin(
            self.http, cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_path_from, cfg.jellyfin_path_to
        )
        self.seadex = SeaDexEntry()
        self.tmdb = Tmdb(self.http, cfg.tmdb_api_key)
        self.rss_cache: dict[str, dict[str, str]] = {}
        self._bg: set[asyncio.Task] = set()  # keep fire-and-forget tasks alive

    async def _every(self, seconds: int, fn, name: str, first_delay: int = 0) -> None:
        # stagger the daily passes so they don't all hit AniList at boot
        await asyncio.sleep(first_delay)
        while True:
            try:
                await fn()
            except Exception:
                log.exception("loop iteration failed", extra={"event": "error", "loop": name})
            await asyncio.sleep(seconds)

    async def rss_pass(self) -> None:
        for feed in self.cfg.rss_feeds:
            for title, link in await rss.fetch_items(self.http, feed, self.rss_cache):
                await grab.consider(self.conn, self.qbit, title, link, "rss", dry_run=self.cfg.dry_run)

    async def watch_pass(self) -> None:
        for g in await db.expire_stale_grabs(self.conn):
            log.warning("grab expired (stalled)", extra={"event": "download_failed", "release": g["release_name"]})
        inflight = await db.grabs_in_flight(self.conn)
        if not inflight:
            return
        qbit_grabs = [g for g in inflight if g["client"] == "qbittorrent"]
        if qbit_grabs:
            by_hash = {g["client_id"]: g for g in qbit_grabs if g["client_id"]}
            for t in await self.qbit.completed():
                g = by_hash.get(t["hash"]) or next(
                    # qbit's display name often carries a [CRC].mkv suffix the
                    # indexer title lacks (or vice versa) — prefix-match both ways
                    (g for g in qbit_grabs
                     if t["name"].startswith(g["release_name"].removesuffix(".mkv"))
                     or g["release_name"].startswith(t["name"])),
                    None,
                )
                if g:
                    await self._finish(g, Path(t["path"]))
        sab_grabs = [g for g in inflight if g["client"] == "sabnzbd"]
        if sab_grabs:
            by_id = {g["client_id"]: g for g in sab_grabs if g["client_id"]}
            by_name = {g["release_name"]: g for g in sab_grabs}
            for slot in await self.sab.history():
                g = by_id.get(slot["nzo_id"]) or by_name.get(slot["name"])
                if not g:
                    continue
                if slot["status"] == "Completed" and slot["path"]:
                    await self._finish(g, Path(slot["path"]))
                elif slot["status"] == "Failed":
                    await db.set_grab_status(self.conn, g["id"], "failed")
                    log.warning("download failed", extra={"event": "download_failed", "release": g["release_name"]})

    async def _finish(self, g: dict, path: Path) -> None:
        series = await db.get_series(self.conn, g["anilist_id"])
        n = await importer.import_path(
            self.conn, self.jellyfin, path, series=series, from_seadex=g["source"] == "seadex"
        )
        await db.set_grab_status(self.conn, g["id"], "imported" if n else "failed")

    async def metadata_pass(self) -> None:
        for s in await db.monitored_series(self.conn):
            if s["status"] == "FINISHED":
                continue
            media = await anilist.by_id(self.http, s["anilist_id"], self.conn)
            await db.add_series(self.conn, media, s["root_path"])
            if media["aired"] != s["aired"]:
                # new episodes exist — let backfill re-search without waiting out its backoff
                await self.conn.execute(
                    "UPDATE series SET last_search = NULL WHERE anilist_id = %s", (s["anilist_id"],)
                )

    async def backfill_pass(self) -> None:
        for s in await db.monitored_series(self.conn):
            await search.backfill_series(
                self.conn, self.prowlarr, self.qbit, self.sab, s, dry_run=self.cfg.dry_run
            )

    async def seadex_pass(self) -> None:
        await seadex_sweep.sweep_all(
            self.conn, self.seadex, self.prowlarr, self.qbit, self.sab, dry_run=self.cfg.dry_run
        )

    async def handle_autobrr(self, release_name: str, download_url: str) -> bool:
        return await grab.consider(
            self.conn, self.qbit, release_name, download_url, "autobrr", dry_run=self.cfg.dry_run
        )

    def run_bg(self, coro) -> None:
        """Fire-and-forget task; exceptions logged, reference kept until done."""

        async def wrapped():
            try:
                await coro
            except Exception:
                log.exception("background task failed", extra={"event": "error"})

        t = asyncio.create_task(wrapped())
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def process_new(self, anilist_ids: list[int]) -> None:
        """Backfill + seadex sweep for freshly requested series, without waiting for the daily loops."""
        for anilist_id in anilist_ids:
            s = await db.get_series(self.conn, anilist_id)
            if s is None:
                continue
            try:
                await search.backfill_series(self.conn, self.prowlarr, self.qbit, self.sab, s, dry_run=self.cfg.dry_run)
                await seadex_sweep.sweep_series(self.conn, self.seadex, self.prowlarr, self.qbit, self.sab, s, dry_run=self.cfg.dry_run)
            except Exception:
                log.exception("processing new request failed", extra={"event": "error", "anilist_id": anilist_id})

    async def mapping_pass(self) -> None:
        await mapping.refresh_if_stale(self.conn, self.http)

    async def nfo_pass(self) -> None:
        # picks up newly-published episode titles/art for airing shows
        await nfo.refresh_all(self.conn, self.http, self.tmdb)

    async def run(self) -> None:
        from kodarr import arr_api

        app = webhook.make_app(self.handle_autobrr, self.cfg.webhook_token)
        arr_api.add_routes(app, self, self.cfg.webhook_token)
        runner = web.AppRunner(app, access_log=None)  # healthz probes would spam the log pipeline
        await runner.setup()
        await web.TCPSite(runner, port=self.cfg.webhook_port).start()
        log.info("kodarr started", extra={"event": "start", "webhook_port": self.cfg.webhook_port})
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._every(self.cfg.rss_interval, self.rss_pass, "rss"))
            tg.create_task(self._every(30, self.watch_pass, "watch"))
            tg.create_task(self._every(DAY, self.metadata_pass, "metadata", first_delay=120))
            tg.create_task(self._every(DAY, self.backfill_pass, "backfill", first_delay=600))
            tg.create_task(self._every(DAY, self.seadex_pass, "seadex", first_delay=1200))
            tg.create_task(self._every(DAY, self.mapping_pass, "mapping", first_delay=60))
            tg.create_task(self._every(DAY, self.nfo_pass, "nfo", first_delay=1800))
