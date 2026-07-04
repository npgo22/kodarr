"""autobrr webhook receiver + health endpoint.

autobrr action: type Webhook, endpoint http://kodarr:7878/webhook/autobrr,
payload {"release_name": "{{ .TorrentName }}", "download_url": "{{ .TorrentUrl }}"},
header X-Kodarr-Token: <token>.
"""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


def make_app(handler, token: str) -> web.Application:
    """handler(release_name, download_url) -> bool (grabbed)."""

    async def autobrr(request: web.Request) -> web.Response:
        if token and request.headers.get("X-Kodarr-Token") != token:
            return web.Response(status=401)
        try:
            body = await request.json()
            release_name = body["release_name"]
            download_url = body["download_url"]
        except (ValueError, KeyError):
            return web.Response(status=400, text="need JSON with release_name, download_url")
        grabbed = await handler(release_name, download_url)
        return web.json_response({"grabbed": grabbed})

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/webhook/autobrr", autobrr)
    app.router.add_get("/healthz", healthz)
    return app
