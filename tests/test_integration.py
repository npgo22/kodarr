"""Integration tests: real Postgres (docker), fake external services (MockTransport).

Covers the full pipeline: announce -> grab -> download complete -> import ->
jellyfin notify, plus backfill ranking, the failed-release blocklist, and
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
from kodarr import db, grab, search
from kodarr.clients import Jellyfin, Prowlarr, Qbit, Sab

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
    """One MockTransport playing qbit + sab + prowlarr + jellyfin."""

    def __init__(self):
        self.qbit_added: list[str] = []
        self.sab_added: list[str] = []
        self.jellyfin_paths: list[str] = []
        self.qbit_torrents: list[dict] = []   # what /torrents/info reports
        self.sab_history: list[dict] = []
        self.prowlarr_results: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "upstream-sonarr":
            return httpx.Response(200, json=[{"id": 42, "title": "Breaking Bad", "tvdbId": 81189, "upstream": True}])
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
        if path == "/api" and request.url.params.get("mode") == "addurl":
            self.sab_added.append(request.url.params["name"])
            return httpx.Response(200, json={"nzo_ids": ["SABnzbd_nzo_1"]})
        if path == "/api" and request.url.params.get("mode") == "history":
            return httpx.Response(200, json={"history": {"slots": self.sab_history}})
        if path == "/api/v1/search":
            return httpx.Response(200, json=self.prowlarr_results)
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
            sab=Sab(http, "http://fake", "key", "kodarr"),
            prowlarr=Prowlarr(http, "http://fake", "key"),
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
        d.cfg, d.conn, d.qbit, d.sab, d.jellyfin = SimpleNamespace(dry_run=False), conn, svc.qbit, svc.sab, svc.jellyfin
        await d.watch_pass()

        ep = await db.get_episode(conn, 154587, 3)
        assert ep and ep["release_group"] == "SubsPlease"
        assert Path(ep["file_path"]).exists() and Path(ep["file_path"]).stat().st_ino == dl.stat().st_ino
        assert "anilist-154587" in ep["file_path"]
        assert fake.jellyfin_paths, "jellyfin was not notified"
        assert (await db.grabs_in_flight(conn)) == []

    asyncio.run(main())


def test_backfill_ranking_blocklist_backoff(postgres, tmp_path):
    """Preferred-group torrent beats usenet; failed release is blocklisted; backoff skips."""

    async def main():
        conn = await fresh_conn()
        fake = FakeServices()
        svc = fake.wire()
        media, root = series_row(tmp_path, episodes=1, aired=1)
        await db.add_series(conn, media, root)

        sp = "[SubsPlease] Sousou no Frieren - 01 (1080p).mkv"
        fake.prowlarr_results = [
            {"title": sp, "protocol": "usenet", "downloadUrl": "http://nzb/1"},
            {"title": sp, "protocol": "torrent", "downloadUrl": "magnet:?xt=urn:btih:aaaa"},
            {"title": "[Other] Sousou no Frieren - 01.mkv", "protocol": "torrent", "downloadUrl": "magnet:?xt=urn:btih:bbbb"},
        ]

        series = await db.get_series(conn, 154587)
        await search.backfill_series(conn, svc.prowlarr, svc.qbit, svc.sab, series, force=True)
        assert fake.sab_added == ["http://nzb/1"] and not fake.qbit_added, "preferred-group usenet should win"

        # that grab fails -> blocklisted... but same release name covers the torrent
        # copy too, and [Other] is never eligible -> nothing left to grab
        g = (await db.grabs_in_flight(conn))[0]
        await db.set_grab_status(conn, g["id"], "failed")
        await search.backfill_series(conn, svc.prowlarr, svc.qbit, svc.sab, series, force=True)
        assert not fake.qbit_added, "non-preferred groups must never be grabbed by backfill"

        # without force, the weekly backoff skips the series entirely
        before = len(fake.qbit_added) + len(fake.sab_added)
        await search.backfill_series(conn, svc.prowlarr, svc.qbit, svc.sab, series)
        assert len(fake.qbit_added) + len(fake.sab_added) == before, "backoff should prevent re-search"

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
        assert "old release" in await db.failed_release_names(conn, 154587)

    asyncio.run(main())


def test_sab_failed_download(postgres, tmp_path):
    async def main():
        conn = await fresh_conn()
        fake = FakeServices()
        svc = fake.wire()
        media, root = series_row(tmp_path)
        await db.add_series(conn, media, root)
        await db.insert_grab(conn, 154587, 5, "search", "sabnzbd", "SABnzbd_nzo_9", "Frieren E05 nzb")
        fake.sab_history = [{"nzo_id": "SABnzbd_nzo_9", "name": "Frieren E05 nzb", "status": "Failed", "storage": None}]

        d = object.__new__(daemon_mod.Daemon)
        d.cfg, d.conn, d.qbit, d.sab, d.jellyfin = SimpleNamespace(dry_run=False), conn, svc.qbit, svc.sab, svc.jellyfin
        await d.watch_pass()
        assert "Frieren E05 nzb" in await db.failed_release_names(conn, 154587)

    asyncio.run(main())
