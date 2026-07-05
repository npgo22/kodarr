"""Thin API wrappers: qBittorrent and Jellyfin."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_QBIT_DONE = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"}


class Qbit:
    def __init__(self, client: httpx.AsyncClient, url: str, user: str, password: str, category: str):
        self.http, self.url, self.user, self.password, self.category = client, url.rstrip("/"), user, password, category

    async def _login(self) -> None:
        r = await self.http.post(f"{self.url}/api/v2/auth/login", data={"username": self.user, "password": self.password})
        r.raise_for_status()

    async def _post(self, path: str, data: dict) -> httpx.Response:
        r = await self.http.post(f"{self.url}{path}", data=data)
        if r.status_code == 403:  # cookie expired
            await self._login()
            r = await self.http.post(f"{self.url}{path}", data=data)
        r.raise_for_status()
        return r

    async def add(self, url_or_magnet: str) -> None:
        # stopped/paused=false overrides qbit's global "add stopped" preference
        # (common in cross-seed setups); both spellings for qbit 4.x/5.x
        try:
            await self._post(
                "/api/v2/torrents/add",
                {"urls": url_or_magnet, "category": self.category, "stopped": "false", "paused": "false"},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 409:  # 409 = torrent already in client
                raise

    async def by_hashes(self, hashes: list[str]) -> list[dict[str, Any]]:
        """Done torrents matching these hashes, regardless of category —
        a release autobrr/cross-seed added first lives outside ours."""
        if not hashes:
            return []
        r = await self._post("/api/v2/torrents/info", {"hashes": "|".join(hashes)})
        return [
            {"hash": t["hash"], "name": t["name"], "path": t["content_path"]}
            for t in r.json()
            if t["state"] in _QBIT_DONE or t["progress"] == 1
        ]

    async def completed(self) -> list[dict[str, Any]]:
        """[{hash, name, content_path}] for finished torrents in our category."""
        r = await self._post("/api/v2/torrents/info", {"category": self.category})
        return [
            {"hash": t["hash"], "name": t["name"], "path": t["content_path"]}
            for t in r.json()
            if t["state"] in _QBIT_DONE or t["progress"] == 1
        ]


class Jellyfin:
    def __init__(self, client: httpx.AsyncClient, url: str, api_key: str, path_from: str = "", path_to: str = ""):
        self.http, self.url, self.api_key = client, url.rstrip("/"), api_key
        self.path_from, self.path_to = path_from, path_to

    async def notify(self, path: str) -> None:
        """Tell Jellyfin a library path changed (Shoko-style targeted refresh)."""
        if self.path_from and path.startswith(self.path_from):
            path = self.path_to + path[len(self.path_from):]
        try:
            r = await self.http.post(
                f"{self.url}/Library/Media/Updated",
                json={"Updates": [{"Path": path, "UpdateType": "Created"}]},
                headers={"X-Emby-Token": self.api_key},
            )
            r.raise_for_status()
            log.info("jellyfin refresh", extra={"event": "jellyfin_refresh", "path": path})
        except httpx.HTTPError as e:
            # import already succeeded; a failed refresh must not fail the pipeline
            log.error("jellyfin refresh failed", extra={"event": "error", "error": str(e)})
