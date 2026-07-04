"""Webhook receivers (autobrr, Jellyseerr) + health endpoint.

autobrr action: type Webhook, endpoint http://kodarr:7878/webhook/autobrr,
payload {"release_name": "{{ .TorrentName }}", "download_url": "{{ .TorrentUrl }}"},
header X-Kodarr-Token: <token>.

Jellyseerr: Settings -> Notifications -> Webhook, URL
http://kodarr:7878/webhook/jellyseerr, Authorization Header = the token,
default JSON payload, notification type "Request Approved" (and
auto-approved). Requested anime lands in the library mapped via
TVDB/TMDB -> AniList; a TVDB show adds every AniList season entry.
"""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


def make_app(grab_handler, request_handler, token: str) -> web.Application:
    """grab_handler(release_name, download_url) -> bool.
    request_handler(media_type, tvdb_id, tmdb_id) -> list of added anilist ids."""

    def authed(request: web.Request) -> bool:
        got = request.headers.get("X-Kodarr-Token") or request.headers.get("Authorization", "")
        return not token or got.removeprefix("Bearer ").strip() == token

    async def autobrr(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        try:
            body = await request.json()
            release_name = body["release_name"]
            download_url = body["download_url"]
        except (ValueError, KeyError):
            return web.Response(status=400, text="need JSON with release_name, download_url")
        grabbed = await grab_handler(release_name, download_url)
        return web.json_response({"grabbed": grabbed})

    async def jellyseerr(request: web.Request) -> web.Response:
        if not authed(request):
            return web.Response(status=401)
        try:
            body = await request.json()
        except ValueError:
            return web.Response(status=400, text="need JSON")
        ntype = body.get("notification_type", "")
        if ntype == "TEST_NOTIFICATION":
            return web.json_response({"ok": True})
        if ntype not in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"):
            return web.json_response({"ignored": ntype})
        media = body.get("media") or {}
        tvdb = int(media["tvdbId"]) if media.get("tvdbId") else None
        tmdb = int(media["tmdbId"]) if media.get("tmdbId") else None
        added = await request_handler(media.get("media_type", "tv"), tvdb, tmdb)
        if not added:
            log.warning(
                "request not mapped to anilist",
                extra={"event": "request_unmapped", "tvdb": tvdb, "tmdb": tmdb, "subject": body.get("subject")},
            )
        return web.json_response({"added": added})

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/webhook/autobrr", autobrr)
    app.router.add_post("/webhook/jellyseerr", jellyseerr)
    app.router.add_get("/healthz", healthz)
    return app
