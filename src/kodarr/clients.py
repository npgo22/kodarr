"""Thin API wrappers: qBittorrent, SABnzbd, Prowlarr, Jellyfin."""

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
        await self._post("/api/v2/torrents/add", {"urls": url_or_magnet, "category": self.category})

    async def completed(self) -> list[dict[str, Any]]:
        """[{hash, name, content_path}] for finished torrents in our category."""
        r = await self._post("/api/v2/torrents/info", {"category": self.category})
        return [
            {"hash": t["hash"], "name": t["name"], "path": t["content_path"]}
            for t in r.json()
            if t["state"] in _QBIT_DONE or t["progress"] == 1
        ]


class Sab:
    def __init__(self, client: httpx.AsyncClient, url: str, api_key: str, category: str):
        self.http, self.url, self.api_key, self.category = client, url.rstrip("/"), api_key, category

    async def _api(self, **params: Any) -> dict:
        r = await self.http.get(f"{self.url}/api", params={"apikey": self.api_key, "output": "json", **params})
        r.raise_for_status()
        return r.json()

    async def add(self, nzb_url: str, name: str) -> str | None:
        data = await self._api(mode="addurl", name=nzb_url, nzbname=name, cat=self.category)
        ids = data.get("nzo_ids") or []
        return ids[0] if ids else None

    async def history(self) -> list[dict[str, Any]]:
        """[{nzo_id, name, status, path}] — status Completed|Failed."""
        data = await self._api(mode="history", cat=self.category, limit=50)
        return [
            {"nzo_id": s["nzo_id"], "name": s["name"], "status": s["status"], "path": s.get("storage")}
            for s in data["history"]["slots"]
        ]


class Prowlarr:
    def __init__(self, client: httpx.AsyncClient, url: str, api_key: str):
        self.http, self.url, self.api_key = client, url.rstrip("/"), api_key

    async def search(self, query: str) -> list[dict[str, Any]]:
        """[{title, protocol, downloadUrl}] protocol: usenet|torrent."""
        r = await self.http.get(
            f"{self.url}/api/v1/search",
            params={"query": query, "type": "search", "limit": 100},
            headers={"X-Api-Key": self.api_key},
            timeout=120,  # fan-out to all indexers is slow
        )
        r.raise_for_status()
        return [
            {"title": x["title"], "protocol": x["protocol"], "url": x.get("downloadUrl") or x.get("magnetUrl")}
            for x in r.json()
            if x.get("downloadUrl") or x.get("magnetUrl")
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
