"""kodarr CLI: library management + daemon entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import httpx

from kodarr.metadata import anilist
from kodarr import db
from kodarr.library import importer
from kodarr import log
from kodarr.metadata import nfo
from kodarr.acquire import backfill as search
from kodarr.acquire import seadex as seadex_sweep
from kodarr.clients import Jellyfin, Qbit
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
    from kodarr.metadata.tmdb import Tmdb

    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient() as http:
        jf = (
            Jellyfin(http, cfg.jellyfin_url, cfg.jellyfin_api_key, cfg.jellyfin_path_from, cfg.jellyfin_path_to)
            if cfg.jellyfin_url
            else None
        )
        n = await importer.import_path(conn, jf, Path(args.path), http=http,
                                       tmdb=Tmdb(http, cfg.tmdb_api_key), from_seadex=args.seadex)
    print(f"imported {n} file(s)")


async def cmd_backfill(cfg: Config, args) -> None:
    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient(follow_redirects=True) as http:
        qbit = Qbit(http, cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, cfg.qbit_category)
        for s in await db.monitored_series(conn):
            if args.anilist_id and s["anilist_id"] != args.anilist_id:
                continue
            await search.backfill_series(conn, http, qbit, s, nyaa_url=cfg.nyaa_url, dry_run=args.dry_run, force=True)


async def cmd_seadex(cfg: Config, args) -> None:
    from seadex import SeaDexEntry

    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient(follow_redirects=True) as http:
        qbit = Qbit(http, cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, cfg.qbit_category)
        await seadex_sweep.sweep_all(conn, SeaDexEntry(), qbit, dry_run=args.dry_run, force=args.force)


async def cmd_reconcile(cfg: Config, args) -> None:
    """Re-derive every episode's (entry, number) from its original release name
    through the AniDB-corrected offsets/counts, and move files to match.
    Fixes libraries imported under wrong manual offsets (specials absorbed as
    episode 1, everything shifted). Dry-run unless --apply."""
    from kodarr.library import match, organize
    from kodarr.metadata import anidb, animelists
    from kodarr.metadata.tmdb import Tmdb

    conn = await db.connect(cfg.db_dsn)
    async with httpx.AsyncClient(follow_redirects=True) as http:
        await animelists.refresh_if_stale(conn, http)
        await anidb.resolve_pass(conn, http, cfg.anidb_cache, cfg.anidb_client)
        all_series = await db.monitored_series(conn)
        targets = [s for s in all_series if not args.anilist_id or s["anilist_id"] in args.anilist_id]

        moves: list[tuple[Path, Path]] = []        # video file renames (two-phase)
        deletes: list[tuple[int, int]] = []        # episodes rows to drop (specials & moved-away)
        inserts: list[tuple] = []                  # rows to (re)create
        sp_nfos: list[tuple[Path, dict, int, str | None, str | None]] = []  # specials: nfo after move
        touched: dict[int, dict] = {}
        for s in targets:
            cur = await conn.execute(
                "SELECT * FROM episodes WHERE anilist_id=%s AND file_path IS NOT NULL ORDER BY absolute_number",
                (s["anilist_id"],))
            for e in await cur.fetchall():
                old = Path(e["file_path"])
                name = e["source_name"] or old.name
                parsed = match.parse(name)
                if parsed is None or parsed.episode is None or not old.exists():
                    continue
                row, num = s, parsed.episode - s["episode_offset"]
                if parsed.season == 0 or num == 0:
                    # belongs in Specials, not the episode table
                    sp_num = parsed.episode if parsed.season == 0 and parsed.episode else 1
                    disp, sp_title, sp_aired = await importer.special_slot(conn, s["anilist_id"], sp_num)
                    dest = organize.dest_path({**s, "season": 0}, disp, e["release_group"], old.suffix)
                    print(f"  special: {old.name} -> {dest.relative_to(dest.parents[2])}")
                    moves.append((old, dest))
                    deletes.append((s["anilist_id"], e["absolute_number"]))
                    sp_nfos.append((dest, {**s, "season": 0}, disp, sp_title, sp_aired))
                    touched[s["anilist_id"]] = s
                    continue
                total = row.get("episodes") or (row.get("aired") or 0) + 1
                if not 1 <= num <= total:
                    m = match.match(parsed, all_series)
                    if m is None or m[1] is None:
                        print(f"  ?? unmatchable: {name}")
                        continue
                    row, num = m
                if (row["anilist_id"], num) == (e["anilist_id"], e["absolute_number"]):
                    continue  # already correct
                dest = organize.dest_path(row, num, e["release_group"], old.suffix)
                print(f"  E{e['absolute_number']:03d} -> {row['anilist_id']} E{num:03d}: {dest.name}")
                moves.append((old, dest))
                deletes.append((e["anilist_id"], e["absolute_number"]))
                inserts.append((row["anilist_id"], num, str(dest), e["release_group"],
                                e["from_seadex"], e["source_name"], e["imported_at"]))
                touched[row["anilist_id"]] = row
                touched[e["anilist_id"]] = s

        if not moves:
            print("nothing to reconcile")
            return
        if not args.apply:
            print(f"\n{len(moves)} change(s) planned — re-run with --apply")
            return
        # two-phase rename: an E002->E001 shift must not clobber E001 mid-move
        staged = []
        for old, dest in moves:
            tmp = old.with_suffix(old.suffix + ".reconcile")
            old.rename(tmp)
            # stale per-episode metadata: regenerated by the refresh below
            old.with_suffix(".nfo").unlink(missing_ok=True)
            old.with_name(old.stem + "-thumb.jpg").unlink(missing_ok=True)
            staged.append((tmp, dest))
        for tmp, dest in staged:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.rename(dest)
        # specials aren't in the episodes table, so refresh_series won't touch
        # them — write their NFOs (AniDB title/airdate) here
        for dest, sp_row, disp, sp_title, sp_aired in sp_nfos:
            nfo.write_episode(dest, sp_row, disp, sp_title, aired=sp_aired)
        async with conn.transaction():
            for aid, num in deletes:
                await conn.execute("DELETE FROM episodes WHERE anilist_id=%s AND absolute_number=%s", (aid, num))
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO episodes (anilist_id, absolute_number, file_path, release_group, from_seadex, source_name, imported_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (anilist_id, absolute_number) DO UPDATE SET
                         file_path=EXCLUDED.file_path, release_group=EXCLUDED.release_group,
                         source_name=EXCLUDED.source_name, title=NULL""",
                    inserts)
        tmdb = Tmdb(http, cfg.tmdb_api_key)
        for s in touched.values():
            fresh = await db.get_series(conn, s["anilist_id"])
            if fresh:
                await nfo.refresh_series(conn, http, tmdb, fresh)
        print(f"applied {len(moves)} move(s), refreshed {len(touched)} entr(y/ies)")


async def cmd_nfo(cfg: Config, args) -> None:
    from kodarr.metadata.tmdb import Tmdb

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

    p = sub.add_parser("reconcile", help="re-derive numbering from AniDB data, move/renumber files (dry-run without --apply)")
    p.add_argument("anilist_id", type=int, nargs="*", help="limit to these entries (default: all)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_reconcile)

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
