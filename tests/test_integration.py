"""Integration tests: real Postgres (docker), fake external services (MockTransport).

Covers the full pipeline: announce -> grab -> download complete -> import ->
jellyfin notify, plus backfill ranking, retryable failures, and
search backoff. Skipped entirely if docker isn't available.
"""

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kodarr import daemon as daemon_mod
from kodarr import db
from kodarr.acquire import announce as grab
from kodarr.acquire import backfill as search
from kodarr.clients import Jellyfin, Qbit

PG_PORT = 54331
DSN = f"postgresql://postgres:test@127.0.0.1:{PG_PORT}/kodarr"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")


@pytest.fixture(scope="session")
def postgres():
    subprocess.run(["docker", "rm", "-f", "kodarr-it-pg"], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", "kodarr-it-pg",
         "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_DB=kodarr",
         "-p", f"127.0.0.1:{PG_PORT}:5432", "postgres:18-alpine"],
        check=True, capture_output=True,
    )
    # pg_isready inside the container passes during the throwaway initdb server;
    # only a successful host connection proves the real server is up.
    async def _connect_ok() -> bool:
        try:
            await (await db.connect(DSN)).close()
            return True
        except Exception:
            return False

    for _ in range(60):
        if asyncio.run(_connect_ok()):
            break
        time.sleep(0.5)
    else:
        pytest.fail("postgres container never became ready")
    yield DSN
    subprocess.run(["docker", "rm", "-f", "kodarr-it-pg"], capture_output=True)


async def fresh_conn():
    conn = await db.connect(DSN)
    await conn.execute("TRUNCATE series CASCADE")
    return conn


def series_row(tmp_path: Path, **over):
    base = {
        "anilist_id": 154587,
        "title": "Frieren: Beyond Journey's End",
        "year": 2023,
        "format": "TV",
        "episodes": 28,
        "aired": 28,
        "status": "FINISHED",
        "synonyms": ["Sousou no Frieren"],
    }
    base.update(over)
    return base, str(tmp_path / "media")


class FakeServices:
    """One MockTransport playing nyaa + qbit + jellyfin (+ anilist, upstream sonarr)."""

    def __init__(self):
        self.qbit_added: list[str] = []
        self.jellyfin_paths: list[str] = []
        self.qbit_torrents: list[dict] = []   # what /torrents/info reports
        self.nyaa_results: list[dict] = []    # [{title, url, infohash, seeders}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "graphql.anilist.co":
            import json

            wanted = json.loads(request.read())["variables"]["id"]
            media = {
                "id": wanted,
                "format": "TV",
                "status": "FINISHED",
                "episodes": 2,
                "startDate": {"year": 2023},
                "title": {"romaji": f"Fake Show {wanted}", "english": None, "native": None},
                "synonyms": [],
                "nextAiringEpisode": None,
            }
            return httpx.Response(200, json={"data": {"Media": media}})
        if path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/add":
            self.qbit_added.append(request.read().decode())
            return httpx.Response(200)
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=self.qbit_torrents)
        if request.url.params.get("page") == "rss" and "q" in request.url.params:
            items = "".join(
                f"<item><title>{r['title']}</title><link>{r['url']}</link>"
                f"<nyaa:infoHash>{r.get('infohash', '')}</nyaa:infoHash>"
                f"<nyaa:seeders>{r.get('seeders', 0)}</nyaa:seeders></item>"
                for r in self.nyaa_results
            )
            xml = f'<rss xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel>{items}</channel></rss>'
            return httpx.Response(200, content=xml.encode())
        if path == "/Library/Media/Updated":
            self.jellyfin_paths.append(request.read().decode())
            return httpx.Response(204)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://fake")

    def wire(self):
        http = self.client()
        return SimpleNamespace(
            http=http,
            qbit=Qbit(http, "http://fake", "u", "p", "kodarr"),
            jellyfin=Jellyfin(http, "http://fake", "key"),
        )


def test_announce_to_library(postgres, tmp_path):
    """RSS/autobrr announce -> qbit -> completion -> hardlink import -> jellyfin."""

    async def main():
        conn = await fresh_conn()
        fake = FakeServices()
        svc = fake.wire()
        media, root = series_row(tmp_path)
        await db.add_series(conn, media, root)

        name = "[SubsPlease] Sousou no Frieren - 03 (1080p) [ABCD].mkv"
        assert await grab.consider(conn, svc.qbit, name, "magnet:?xt=urn:btih:beef", "rss")
        assert len(fake.qbit_added) == 1
        # duplicate announce is a no-op (active grab)
        assert not await grab.consider(conn, svc.qbit, name, "magnet:?xt=urn:btih:beef", "rss")

        # torrent finishes; watcher imports it
        dl = tmp_path / "downloads" / name
        dl.parent.mkdir(parents=True)
        dl.write_bytes(b"video")
        fake.qbit_torrents = [{"hash": "beef", "name": name.removesuffix(".mkv"), "state": "stalledUP", "progress": 1, "content_path": str(dl)}]

        d = object.__new__(daemon_mod.Daemon)  # skip __init__: wire fakes directly
        d.cfg, d.conn, d.qbit, d.jellyfin = SimpleNamespace(dry_run=False), conn, svc.qbit, svc.jellyfin
        d.http, d.tmdb = svc.http, None
        await d.watch_pass()

        ep = await db.get_episode(conn, 154587, 3)
        assert ep and ep["release_group"] == "SubsPlease"
        assert Path(ep["file_path"]).exists() and Path(ep["file_path"]).stat().st_ino == dl.stat().st_ino
        assert "anilist-154587" in ep["file_path"]
        assert fake.jellyfin_paths, "jellyfin was not notified"
        # first import writes season + show metadata inline (no bare folder
        # names in jellyfin while waiting for the daily nfo pass)
        season_dir = Path(ep["file_path"]).parent
        assert (season_dir / "season.nfo").exists(), "season.nfo missing after first import"
        assert (season_dir.parent / "tvshow.nfo").exists(), "tvshow.nfo missing after first import"
        assert (await db.grabs_in_flight(conn)) == []

    asyncio.run(main())


def test_stale_grab_expiry(postgres, tmp_path):
    async def main():
        conn = await fresh_conn()
        media, root = series_row(tmp_path)
        await db.add_series(conn, media, root)
        await db.insert_grab(conn, 154587, 1, "rss", "qbittorrent", None, "old release")
        await conn.execute("UPDATE grabs SET created_at = now() - interval '4 days'")
        expired = await db.expire_stale_grabs(conn)
        assert [g["release_name"] for g in expired] == ["old release"]
        assert await db.active_grab(conn, 154587, 1) is None

    asyncio.run(main())




def test_pack_special_lands_in_specials(postgres, tmp_path):
    """A season/BD batch carrying an S00 extra: the real episode imports and
    the special is filed under Season 00 (not silently discarded)."""
    from kodarr.library import importer

    async def main():
        conn = await fresh_conn()
        media, root = series_row(tmp_path, episodes=12, aired=12)
        await db.add_series(conn, media, root)
        row = await db.get_series(conn, 154587)

        pack = tmp_path / "pack"
        pack.mkdir()
        (pack / "[Thighs] Sousou no Frieren - 03 (BD 1080p).mkv").write_bytes(b"v")
        (pack / "Sousou no Frieren S00E02 (BD 1080p).mkv").write_bytes(b"v")

        n = await importer.import_path(conn, None, pack, series=row)
        assert n == 1  # only the real episode is tracked in the episodes table
        assert await db.get_episode(conn, 154587, 3) is not None
        show = Path(root) / "Frieren Beyond Journey's End (2023) [anilist-154587]"
        specials = list((show / "Season 00").glob("*.mkv"))
        assert specials and "S00E002" in specials[0].name, "special not filed under Specials"

    asyncio.run(main())


def test_backfill_ranking_retry_backoff(postgres, tmp_path):
    """Preferred-group 1080p wins; failed grabs retry; backoff skips."""

    async def main():
        conn = await fresh_conn()
        fake = FakeServices()
        svc = fake.wire()
        media, root = series_row(tmp_path, episodes=1, aired=1)
        await db.add_series(conn, media, root)

        fake.nyaa_results = [
            {"title": "[SubsPlease] Sousou no Frieren - 01 (1080p) [AAAA].mkv", "url": "http://nyaa/1.torrent", "infohash": "aaaa", "seeders": 500},
            {"title": "[SubsPlease] Sousou no Frieren - 01 (720p) [BBBB].mkv", "url": "http://nyaa/2.torrent", "infohash": "bbbb", "seeders": 900},
            {"title": "[Other] Sousou no Frieren - 01 (1080p).mkv", "url": "http://nyaa/3.torrent", "infohash": "cccc", "seeders": 999},
        ]
        series = await db.get_series(conn, 154587)
        await search.backfill_series(conn, svc.http, svc.qbit, series, nyaa_url="http://fake", force=True)
        assert len(fake.qbit_added) == 1 and "1.torrent" in fake.qbit_added[0], "preferred-group 1080p must win"

        # a failed grab is retryable: the same best release is grabbed again
        # (qbit dedupes by infohash, so retry loops cost nothing)
        g = (await db.grabs_in_flight(conn))[0]
        await db.set_grab_status(conn, g["id"], "failed")
        await search.backfill_series(conn, svc.http, svc.qbit, series, nyaa_url="http://fake", force=True)
        assert len(fake.qbit_added) == 2 and "1.torrent" in fake.qbit_added[1]

        # without force, the weekly backoff skips the series entirely
        await search.backfill_series(conn, svc.http, svc.qbit, series, nyaa_url="http://fake")
        assert len(fake.qbit_added) == 2

    asyncio.run(main())


def test_anidb_resolve_and_reconcile(postgres, tmp_path):
    """The Mushoku bug end-to-end: a wrong manual offset (-1) absorbed the
    'Guardian Fitz'-style special as episode 1 and shifted every episode.
    AniDB resolve corrects count+offset from data; reconcile renumbers the
    files and parks the special in Season 00 at its anime-lists TVDB slot."""
    import json
    from types import SimpleNamespace

    from kodarr.config import Config
    from kodarr.library import organize
    from kodarr.metadata import anidb
    from kodarr import cli

    async def main():
        conn = await fresh_conn()
        media, root = series_row(
            tmp_path, anilist_id=900001, title="Fake Show Season 2",
            episodes=3, aired=3, synonyms=[])
        await db.add_series(conn, media, root)
        await conn.execute(
            "UPDATE series SET episode_offset = -1, season = 2 WHERE anilist_id = 900001")
        await conn.execute("DELETE FROM id_map")
        await conn.execute(
            "INSERT INTO id_map (anilist_id, anidb_id, tmdb_tv_id, tmdb_season) VALUES (900001, 55555, 1, 2)")
        await conn.execute(
            "INSERT INTO anidb_map (anidb_id, tvdb_id, default_tvdb_season, episode_offset, special_map) "
            "VALUES (55555, '1', '2', 0, %s)", (json.dumps({"1": 2}),))
        # pre-seed the anilist cache (status FINISHED = permanent) so the
        # reconcile NFO refresh never leaves the test environment
        payload = {
            "anilist_id": 900001, "title": "Fake Show Season 2", "year": 2023, "format": "TV",
            "episodes": 2, "aired": 2, "status": "FINISHED", "synonyms": ["Fake Show Season 2"],
            "description": "", "score": None, "genres": [], "cover_url": None, "banner_url": None,
            "studio": None, "premiered": "2023-07-10", "ended": "2023-09-25", "runtime": 24,
            "source_material": None, "characters": [], "episode_titles": {}, "relations": [],
        }
        await conn.execute(
            "INSERT INTO anilist_cache (anilist_id, payload) VALUES (%s, %s) "
            "ON CONFLICT (anilist_id) DO UPDATE SET payload = EXCLUDED.payload",
            (900001, json.dumps(payload)))

        # wrongly-imported state: MTBB 00 (the special) sits at E001, eps shifted
        s = await db.get_series(conn, 900001)
        for n, src in ((1, "[MTBB] Fake Show S2 - 00 (BD 1080p).mkv"),
                       (2, "[MTBB] Fake Show S2 - 01 (BD 1080p).mkv"),
                       (3, "[MTBB] Fake Show S2 - 02 (BD 1080p).mkv")):
            p = organize.dest_path(s, n, "MTBB", ".mkv")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"v")
            await db.upsert_episode(conn, 900001, n, str(p), "MTBB", False, src)

        # AniDB ground truth: 2 regular episodes + special S1 (a stale-zip stand-in)
        anime = anidb.parse_anime("""<anime id="55555"><episodecount>2</episodecount>
          <enddate>2023-09-25</enddate><episodes>
          <episode><epno type="1">1</epno><airdate>2023-07-10</airdate><title xml:lang="en">Ep One</title></episode>
          <episode><epno type="1">2</epno><airdate>2023-07-17</airdate><title xml:lang="en">Ep Two</title></episode>
          <episode><epno type="2">S1</epno><airdate>2023-07-03</airdate><title xml:lang="en">The Special</title></episode>
          </episodes></anime>""")
        await anidb.resolve_series(conn, s, 55555, anime)
        s = await db.get_series(conn, 900001)
        assert s["episodes"] == 2, "AniDB regular count must beat AniList's"
        assert s["episode_offset"] == 0, "offset must be derived, manual -1 gone"
        assert s["anidb_mapped"] is not None

        cfg = Config(db_dsn=DSN, anime_root=str(tmp_path / "media"), movie_root=str(tmp_path / "m2"),
                     downloads_dir="/tmp", jellyfin_url="", jellyfin_api_key="", jellyfin_path_from="",
                     jellyfin_path_to="", nyaa_url="http://fake", qbit_url="", qbit_user="", qbit_pass="",
                     qbit_category="k", anidb_cache=str(tmp_path / "cache"))
        await cli.cmd_reconcile(cfg, SimpleNamespace(anilist_id=[900001], apply=True))

        # special moved to Season 00 at its anime-lists slot (S1 -> S0E2)
        show = organize.series_dir(s).parent
        specials = list((show / "Season 00").glob("*.mkv"))
        assert len(specials) == 1 and "S00E002" in specials[0].name, specials
        sp_nfo = specials[0].with_suffix(".nfo").read_text()
        assert "The Special" in sp_nfo and "2023-07-03" in sp_nfo, sp_nfo
        # episodes renumbered 1..2 from their source names
        cur = await conn.execute(
            "SELECT absolute_number, source_name FROM episodes WHERE anilist_id=900001 ORDER BY 1")
        rows = await cur.fetchall()
        assert [(r["absolute_number"], r["source_name"][-17:]) for r in rows] == [
            (1, "01 (BD 1080p).mkv"), (2, "02 (BD 1080p).mkv")], rows
        for r in rows:
            ep = await db.get_episode(conn, 900001, r["absolute_number"])
            assert Path(ep["file_path"]).exists()
        # AniDB titles flowed into the refreshed NFOs
        ep1 = await db.get_episode(conn, 900001, 1)
        nfo_text = Path(ep1["file_path"]).with_suffix(".nfo").read_text()
        assert "Ep One" in nfo_text and "2023-07-10" in nfo_text

    asyncio.run(main())


def test_arr_api_for_seerr(postgres, tmp_path):
    """The Sonarr-shaped surface Seerr drives: auth, discovery endpoints,
    lookup, and a season request adding franchise entries."""
    from aiohttp.test_utils import TestClient, TestServer

    from kodarr import api

    async def main():
        conn = await fresh_conn()
        fake = FakeServices()
        svc = fake.wire()
        await conn.execute("DELETE FROM id_map")
        await conn.execute(
            "INSERT INTO id_map (anilist_id, tvdb_id, tvdb_season) VALUES (101280, 352408, 1)"
        )

        d = object.__new__(daemon_mod.Daemon)
        d.cfg = SimpleNamespace(dry_run=True, anime_root=str(tmp_path / "anime"), movie_root=str(tmp_path / "movies"))
        d.conn, d.http, d.tmdb = conn, svc.http, None
        d.qbit, d.jellyfin = svc.qbit, svc.jellyfin
        d._bg = set()

        client = TestClient(TestServer(api.build_app(d, "tok")))
        await client.start_server()
        h = {"X-Api-Key": "tok"}

        assert (await client.get("/api/v3/system/status")).status == 401
        for path in ("/api/v3/system/status", "/api/v3/rootfolder", "/api/v3/qualityprofile", "/api/v3/tag"):
            assert (await client.get(path, headers=h)).status == 200, path
        assert (await client.get("/healthz")).status == 200

        r = await client.get("/api/v3/series/lookup", params={"term": "tvdb:99999"}, headers=h)
        assert await r.json() == []  # unmapped: not anime, seerr routes elsewhere

        r = await client.get("/api/v3/series/lookup", params={"term": "tvdb:352408"}, headers=h)
        [series] = await r.json()
        assert series["id"] == 0 and series["tvdbId"] == 352408

        r = await client.post("/api/v3/series", headers=h, json={
            "tvdbId": 352408, "qualityProfileId": 1, "rootFolderPath": str(tmp_path / "anime"),
            "seasons": [{"seasonNumber": 1, "monitored": True}],
        })
        assert r.status == 201
        await asyncio.gather(*d._bg)  # adds run in the background
        assert await db.get_series(conn, 101280) is not None

        # seerr "Remove from Sonarr" (deleteFiles omitted -> keep files)
        r = await client.delete("/api/v3/series/352408", headers=h)
        assert r.status == 200
        assert await db.get_series(conn, 101280) is None
        assert (await client.delete("/api/v3/series/352408", headers=h)).status == 404  # gone now
        await client.close()

    asyncio.run(main())
