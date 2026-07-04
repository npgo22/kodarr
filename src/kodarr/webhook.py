"""autobrr webhook + health endpoint.

autobrr action: type Webhook, endpoint http://kodarr:7878/webhook/autobrr,
payload {"release_name": "{{ .TorrentName }}", "download_url": "{{ .TorrentUrl }}"},
header X-Kodarr-Token: <token>.

Seerr does NOT use webhooks — it talks to the Sonarr/Radarr API in arr_api.py.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

log = logging.getLogger(__name__)


def make_app(grab_handler, token: str) -> web.Application:
    """grab_handler(release_name, download_url) -> bool (grabbed)."""

    async def autobrr(request: web.Request) -> web.Response:
        got = request.headers.get("X-Kodarr-Token", "")
        if token and not hmac.compare_digest(got, token):
            return web.Response(status=401)
        try:
            body = await request.json()
            release_name = body["release_name"]
            download_url = body["download_url"]
        except (ValueError, KeyError):
            return web.Response(status=400, text="need JSON with release_name, download_url")
        grabbed = await grab_handler(release_name, download_url)
        return web.json_response({"grabbed": grabbed})

    async def healthz(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_post("/webhook/autobrr", autobrr)
    app.router.add_get("/healthz", healthz)
    return app
