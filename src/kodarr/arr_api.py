"""Sonarr/Radarr v3 API subset — just enough for Seerr to use kodarr as its
TV (Sonarr) and movie (Radarr) server.

Identity model: a Seerr "series id" is the TVDB id; a "movie id" is the TMDB
id. Requested TVDB seasons map to AniList entries via id_map.tvdb_season, so
per-season requests add exactly those cours. Auth: X-Api-Key == webhook token.

# ponytail: implements what seerr's sonarr.ts/radarr.ts actually call —
# status/rootfolder/profiles/tags/lookup/get/add/update/command. Anything
# else 404s; extend when a seerr version starts calling it.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)


def _series_shape(tvdb_id: int, title: str, year: int | None, seasons: list[dict], exists: bool) -> dict[str, Any]:
    return {
        "id": tvdb_id if exists else 0,
        "title": title,
        "sortTitle": title.lower(),
        "year": year or 0,
        "tvdbId": tvdb_id,
        "titleSlug": str(tvdb_id),
        "seasons": seasons,
        "monitored": True,
        "seasonFolder": True,
        "seriesType": "anime",
        "rootFolderPath": "",
        "qualityProfileId": 1,
        "languageProfileId": 1,
        "added": "2026-01-01T00:00:00Z",
        "images": [],
        "tags": [],
        "statistics": {"seasonCount": len([s for s in seasons if s["seasonNumber"] > 0])},
    }


def _movie_shape(tmdb_id: int, title: str, year: int | None, exists: bool, has_file: bool) -> dict[str, Any]:
    return {
        "id": tmdb_id if exists else 0,
        "title": title,
        "originalTitle": title,
        "year": year or 0,
        "tmdbId": tmdb_id,
        "titleSlug": str(tmdb_id),
        "monitored": True,
        "hasFile": has_file,
        "isAvailable": True,
        "minimumAvailability": "released",
        "qualityProfileId": 1,
        "rootFolderPath": "",
        "added": "2026-01-01T00:00:00Z",
        "images": [],
        "tags": [],
    }


class ArrApi:
    """d = the Daemon (conn, http, cfg, add/search plumbing)."""

    def __init__(self, daemon):
        self.d = daemon

    # -- shared handlers -------------------------------------------------
    async def system_status(self, request):
        return web.json_response({"version": "4.0.0.0", "appName": "kodarr", "instanceName": "kodarr"})

    async def rootfolder(self, request):
        # one server plays both sonarr and radarr; expose both roots and let
        # seerr pick the default per instance
        return web.json_response([
            {"id": 1, "path": self.d.cfg.anime_root, "accessible": True, "freeSpace": 1 << 40},
            {"id": 2, "path": self.d.cfg.movie_root, "accessible": True, "freeSpace": 1 << 40},
        ])

    async def qualityprofile(self, request):
        return web.json_response([{"id": 1, "name": "SubsPlease + SeaDex"}])

    async def languageprofile(self, request):
        return web.json_response([{"id": 1, "name": "Japanese"}])

    async def tag(self, request):
        return web.json_response([])

    async def command(self, request):
        return web.json_response({"id": 1, "name": (await request.json()).get("name", ""), "status": "completed"})

    # -- sonarr: tvdb-keyed series ---------------------------------------
    async def _tvdb_entries(self, tvdb_id: int) -> list[dict]:
        cur = await self.d.conn.execute(
            """SELECT m.anilist_id, m.tvdb_season, s.anilist_id IS NOT NULL AS in_library,
                      COALESCE(s.episodes, s.aired, 0) AS total,
                      (SELECT count(*) FROM episodes e WHERE e.anilist_id = m.anilist_id AND e.file_path IS NOT NULL) AS have,
                      s.title, s.year
               FROM id_map m LEFT JOIN series s USING (anilist_id)
               WHERE m.tvdb_id = %s AND (m.tvdb_season IS NULL OR m.tvdb_season > 0)
               ORDER BY m.tvdb_season""",
            (tvdb_id,),
        )
        return await cur.fetchall()

    def _tvdb_seasons(self, entries: list[dict]) -> list[dict]:
        by_season: dict[int, dict] = {}
        for e in entries:
            sn = e["tvdb_season"] or 1
            agg = by_season.setdefault(sn, {"seasonNumber": sn, "monitored": False, "have": 0, "total": 0})
            agg["monitored"] = agg["monitored"] or e["in_library"]
            agg["have"] += e["have"]
            agg["total"] += e["total"] or 0
        out = []
        for sn, agg in sorted(by_season.items()):
            total, have = agg.pop("total"), agg.pop("have")
            agg["statistics"] = {
                "episodeFileCount": have, "episodeCount": total, "totalEpisodeCount": total,
                "percentOfEpisodes": (have / total * 100) if total else 0,
            }
            out.append(agg)
        return out

    async def series_lookup(self, request):
        term = request.query.get("term", "")
        if not term.startswith("tvdb:"):
            return web.json_response([])
        tvdb_id = int(term.removeprefix("tvdb:"))
        entries = await self._tvdb_entries(tvdb_id)
        if not entries:
            return web.json_response([])
        exists = any(e["in_library"] for e in entries)
        titled = next((e for e in entries if e["title"]), None)
        title = titled["title"] if titled else f"tvdb-{tvdb_id}"
        year = titled["year"] if titled else None
        return web.json_response([_series_shape(tvdb_id, title, year, self._tvdb_seasons(entries), exists)])

    async def series_list(self, request):
        tvdb_q = request.query.get("tvdbId")
        cur = await self.d.conn.execute(
            """SELECT DISTINCT m.tvdb_id FROM id_map m JOIN series s USING (anilist_id)
               WHERE m.tvdb_id IS NOT NULL""" + (" AND m.tvdb_id = %s" if tvdb_q else ""),
            (int(tvdb_q),) if tvdb_q else (),
        )
        out = []
        for r in await cur.fetchall():
            entries = await self._tvdb_entries(r["tvdb_id"])
            titled = next((e for e in entries if e["title"]), None)
            out.append(_series_shape(r["tvdb_id"], titled["title"] if titled else "", titled["year"] if titled else None,
                                     self._tvdb_seasons(entries), True))
        return web.json_response(out)

    async def series_get(self, request):
        tvdb_id = int(request.match_info["id"])
        entries = await self._tvdb_entries(tvdb_id)
        if not any(e["in_library"] for e in entries):
            raise web.HTTPNotFound
        titled = next((e for e in entries if e["title"]), None)
        return web.json_response(_series_shape(tvdb_id, titled["title"] if titled else "", titled["year"] if titled else None,
                                               self._tvdb_seasons(entries), True))

    async def series_add(self, request):
        body = await request.json()
        tvdb_id = int(body["tvdbId"])
        wanted = {s["seasonNumber"] for s in body.get("seasons", []) if s.get("monitored")}
        added = await self._add_tvdb_seasons(tvdb_id, wanted)
        entries = await self._tvdb_entries(tvdb_id)
        titled = next((e for e in entries if e["title"]), None)
        log.info("seerr add", extra={"event": "request", "tvdb": tvdb_id, "seasons": sorted(wanted), "added": added})
        return web.json_response(
            _series_shape(tvdb_id, titled["title"] if titled else "", titled["year"] if titled else None,
                          self._tvdb_seasons(entries), True),
            status=201,
        )

    async def series_update(self, request):
        # seerr PUTs the full series with newly-monitored seasons on follow-up requests
        return await self.series_add(request)

    async def _add_tvdb_seasons(self, tvdb_id: int, seasons: set[int]) -> list[int]:
        from kodarr import anilist, db

        cur = await self.d.conn.execute(
            "SELECT anilist_id, tvdb_season FROM id_map WHERE tvdb_id = %s AND tvdb_season > 0", (tvdb_id,)
        )
        added = []
        for r in await cur.fetchall():
            if seasons and r["tvdb_season"] not in seasons:
                continue
            if await db.get_series(self.d.conn, r["anilist_id"]):
                continue
            media = await anilist.by_id(self.d.http, r["anilist_id"])
            fr = await anilist.franchise(self.d.http, media)
            root = self.d.cfg.movie_root if media["format"] == "MOVIE" else self.d.cfg.anime_root
            await db.add_series(self.d.conn, {**media, **fr}, root)
            added.append(r["anilist_id"])
        if added:
            self.d.process_new(added)
        return added

    # -- radarr: tmdb-keyed movies ---------------------------------------
    async def _movie_row(self, tmdb_id: int) -> dict | None:
        cur = await self.d.conn.execute(
            """SELECT s.*, (SELECT count(*) FROM episodes e WHERE e.anilist_id = s.anilist_id AND e.file_path IS NOT NULL) AS have
               FROM id_map m JOIN series s USING (anilist_id) WHERE m.tmdb_movie_id = %s""",
            (tmdb_id,),
        )
        return await cur.fetchone()

    async def movie_lookup(self, request):
        term = request.query.get("term", "")
        if not term.startswith("tmdb:"):
            return web.json_response([])
        tmdb_id = int(term.removeprefix("tmdb:"))
        row = await self._movie_row(tmdb_id)
        if row:
            return web.json_response([_movie_shape(tmdb_id, row["title"], row["year"], True, row["have"] > 0)])
        cur = await self.d.conn.execute("SELECT anilist_id FROM id_map WHERE tmdb_movie_id = %s", (tmdb_id,))
        if await cur.fetchone() is None:
            return web.json_response([])
        return web.json_response([_movie_shape(tmdb_id, f"tmdb-{tmdb_id}", None, False, False)])

    async def movie_list(self, request):
        tmdb_q = request.query.get("tmdbId")
        if tmdb_q:
            row = await self._movie_row(int(tmdb_q))
            return web.json_response(
                [_movie_shape(int(tmdb_q), row["title"], row["year"], True, row["have"] > 0)] if row else []
            )
        cur = await self.d.conn.execute(
            "SELECT m.tmdb_movie_id FROM id_map m JOIN series s USING (anilist_id) WHERE m.tmdb_movie_id IS NOT NULL"
        )
        out = []
        for r in await cur.fetchall():
            row = await self._movie_row(r["tmdb_movie_id"])
            if row:
                out.append(_movie_shape(r["tmdb_movie_id"], row["title"], row["year"], True, row["have"] > 0))
        return web.json_response(out)

    async def movie_get(self, request):
        row = await self._movie_row(int(request.match_info["id"]))
        if not row:
            raise web.HTTPNotFound
        cur = await self.d.conn.execute("SELECT tmdb_movie_id FROM id_map WHERE anilist_id = %s", (row["anilist_id"],))
        tmdb_id = (await cur.fetchone())["tmdb_movie_id"]
        return web.json_response(_movie_shape(tmdb_id, row["title"], row["year"], True, row["have"] > 0))

    async def movie_add(self, request):
        from kodarr import anilist, db

        body = await request.json()
        tmdb_id = int(body["tmdbId"])
        cur = await self.d.conn.execute("SELECT anilist_id FROM id_map WHERE tmdb_movie_id = %s", (tmdb_id,))
        r = await cur.fetchone()
        if r is None:
            raise web.HTTPBadRequest(text="no anilist mapping for this tmdb movie")
        if not await db.get_series(self.d.conn, r["anilist_id"]):
            media = await anilist.by_id(self.d.http, r["anilist_id"])
            fr = await anilist.franchise(self.d.http, media)
            await db.add_series(self.d.conn, {**media, **fr}, self.d.cfg.movie_root)
            self.d.process_new([r["anilist_id"]])
            log.info("seerr add", extra={"event": "request", "tmdb_movie": tmdb_id, "anilist_id": r["anilist_id"]})
        row = await self._movie_row(tmdb_id)
        return web.json_response(_movie_shape(tmdb_id, row["title"], row["year"], True, row["have"] > 0), status=201)


def add_routes(app: web.Application, daemon, token: str) -> None:
    api = ArrApi(daemon)

    @web.middleware
    async def auth(request, handler):
        # real sonarr accepts header or query param; seerr uses ?apikey=
        got = request.headers.get("X-Api-Key") or request.query.get("apikey")
        if request.path.startswith("/api/v3") and got != token:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth)
    r = app.router
    r.add_get("/api/v3/system/status", api.system_status)
    r.add_get("/api/v3/rootfolder", api.rootfolder)
    r.add_get("/api/v3/qualityprofile", api.qualityprofile)
    r.add_get("/api/v3/qualityProfile", api.qualityprofile)  # radarr spells it camelCase
    r.add_get("/api/v3/languageprofile", api.languageprofile)
    r.add_get("/api/v3/tag", api.tag)
    r.add_post("/api/v3/command", api.command)
    r.add_get("/api/v3/series/lookup", api.series_lookup)
    r.add_get("/api/v3/series", api.series_list)
    r.add_get("/api/v3/series/{id}", api.series_get)
    r.add_post("/api/v3/series", api.series_add)
    r.add_put("/api/v3/series/{id}", api.series_update)
    r.add_put("/api/v3/series", api.series_update)
    r.add_get("/api/v3/movie/lookup", api.movie_lookup)
    r.add_get("/api/v3/movie", api.movie_list)
    r.add_get("/api/v3/movie/{id}", api.movie_get)
    r.add_post("/api/v3/movie", api.movie_add)
