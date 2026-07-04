"""kodarr CLI: library management + daemon entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx

from kodarr import anilist, db, importer, log, nfo, search, seadex_sweep
from kodarr.clients import Jellyfin, Prowlarr, Qbit, Sab
from kodarr.config import Config, load


async def cmd_add(cfg: Config, args) -> None:
    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient() as http:
        if args.query.isdigit():
            media = await anilist.by_id(http, int(args.query))
        else:
            results = await anilist.search(http, args.query)
            if not results:
                print("no results")
                return
            print("pick an ID and re-run `kodarr add <id>`:")
            for m in results:
                print(f"  {m['anilist_id']:>7}  {m['title']} ({m['year']}, {m['format']}, {m['episodes']} eps)")
            return
    root = cfg.movie_root if media["format"] == "MOVIE" else cfg.anime_root
    async with httpx.AsyncClient() as http:
        fr = await anilist.franchise(http, media, conn)
    if args.show_root or args.season:
        fr = {**fr, **({"show_key": args.show_root} if args.show_root else {}),
              **({"season": args.season} if args.season else {})}
        if args.show_root:
            async with httpx.AsyncClient() as http:
                root_media = await anilist.by_id(http, args.show_root)
            fr["show_title"], fr["show_year"] = root_media["title"], root_media["year"]
    await db.add_series(conn, {**media, **fr}, root)
    if args.offset or args.group:
        await conn.execute(
            "UPDATE series SET episode_offset = %s, preferred_group = %s WHERE anilist_id = %s",
            (args.offset, args.group or "SubsPlease", media["anilist_id"]),
        )
    print(f"added {media['title']} [anilist-{media['anilist_id']}] -> {fr['show_title']} / Season {fr['season']:02d}")


async def cmd_list(cfg: Config, args) -> None:
    conn = await db.connect(cfg.db_dsn)
    cur = await conn.execute(
        """SELECT s.anilist_id, s.title, s.format, s.status, s.episodes,
                  count(e.file_path) AS have
           FROM series s LEFT JOIN episodes e USING (anilist_id)
           GROUP BY s.anilist_id ORDER BY s.title"""
    )
    for r in await cur.fetchall():
        total = r["episodes"] or "?"
        print(f"{r['anilist_id']:>7}  {r['have']}/{total}  {r['status'] or '':<10} {r['title']}")


async def cmd_remove(cfg: Config, args) -> None:
    conn = await db.connect(cfg.db_dsn)
    await conn.execute("DELETE FROM series WHERE anilist_id = %s", (args.anilist_id,))
    print(f"removed anilist-{args.anilist_id} (files left on disk)")


async def cmd_import(cfg: Config, args) -> None:
    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient() as http:
        jf = (
            Jellyfin(http, cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_path_from, cfg.jellyfin_path_to)
            if cfg.jellyfin_url
            else None
        )
        n = await importer.import_path(conn, jf, Path(args.path), from_seadex=args.seadex)
    print(f"imported {n} file(s)")


async def cmd_backfill(cfg: Config, args) -> None:
    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient(follow_redirects=True) as http:
        prowlarr = Prowlarr(http, cfg.prowlarr_url, cfg.prowlarr_api_key)
        qbit = Qbit(http, cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, cfg.qbit_category)
        sab = Sab(http, cfg.sab_url, cfg.sab_api_key, cfg.sab_category)
        for s in await db.monitored_series(conn):
            if args.anilist_id and s["anilist_id"] != args.anilist_id:
                continue
            await search.backfill_series(conn, prowlarr, qbit, sab, s, dry_run=args.dry_run, force=True)


async def cmd_seadex(cfg: Config, args) -> None:
    from seadex import SeaDexEntry

    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient(follow_redirects=True) as http:
        prowlarr = Prowlarr(http, cfg.prowlarr_url, cfg.prowlarr_api_key)
        qbit = Qbit(http, cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, cfg.qbit_category)
        sab = Sab(http, cfg.sab_url, cfg.sab_api_key, cfg.sab_category)
        await seadex_sweep.sweep_all(conn, SeaDexEntry(), prowlarr, qbit, sab, dry_run=args.dry_run, force=args.force)


async def cmd_nfo(cfg: Config, args) -> None:
    from kodarr.tmdb import Tmdb

    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient(follow_redirects=True) as http:
        await nfo.refresh_all(conn, http, Tmdb(http, cfg.tmdb_api_key))
    print("nfo refresh complete")


async def cmd_run(cfg: Config, args) -> None:
    from kodarr.daemon import Daemon

    cfg.dry_run = args.dry_run
    conn = await db.connect(cfg.db_dsn)
    await Daemon(cfg, conn).run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="kodarr", description="anime-only sonarr replacement")
    parser.add_argument("--config", default=os.environ.get("KODARR_CONFIG", "config.toml"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="add by AniList ID, or search by name to find the ID")
    p.add_argument("query")
    p.add_argument("--offset", type=int, default=0, help="release abs number - anilist episode")
    p.add_argument("--group", default=None, help="preferred release group (default SubsPlease)")
    p.add_argument("--show-root", type=int, default=None, help="override franchise root anilist id")
    p.add_argument("--season", type=int, default=None, help="override season number in the show folder")
    p.set_defaults(fn=cmd_add)

    sub.add_parser("list", help="list library").set_defaults(fn=cmd_list)

    p = sub.add_parser("remove", help="remove series (keeps files)")
    p.add_argument("anilist_id", type=int)
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("import", help="import a downloaded file/dir manually")
    p.add_argument("path")
    p.add_argument("--seadex", action="store_true", help="mark as seadex-quality (upgrades existing)")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("backfill", help="search + grab missing episodes now")
    p.add_argument("anilist_id", type=int, nargs="?", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("seadex", help="run the SeaDex upgrade sweep now")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="re-check series already fully on seadex releases")
    p.set_defaults(fn=cmd_seadex)

    sub.add_parser("nfo", help="write NFO metadata + artwork for the whole library").set_defaults(fn=cmd_nfo)

    p = sub.add_parser("run", help="run the daemon")
    p.add_argument("--dry-run", action="store_true", help="log intended grabs, send nothing")
    p.set_defaults(fn=cmd_run)

    args = parser.parse_args()
    log.setup(os.environ.get("KODARR_LOG_LEVEL", "INFO"))
    cfg = load(args.config)
    asyncio.run(args.fn(cfg, args))
