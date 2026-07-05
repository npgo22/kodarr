"""TMDB enrichment: episode titles/overviews/stills + backdrops.

Enrichment ONLY — structure, seasons, and matching stay pure AniList. Ids
come from the Fribb table (id_map.tmdb_tv_id / tmdb_season); our episode N
maps to TMDB episode N + series.episode_offset (split cours share a TMDB
season, offset covers the second half). Dormant when no API key is set.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/original"


class Tmdb:
    def __init__(self, http: httpx.AsyncClient, api_key: str):
        self.http, self.api_key = http, api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def _get(self, path: str) -> dict | None:
        try:
            r = await self.http.get(f"{API}{path}", params={"api_key": self.api_key}, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            log.warning("tmdb request failed", extra={"event": "error", "path": path, "error": str(e)})
            return None

    async def season_episodes(self, tv_id: int, season: int) -> dict[int, dict[str, Any]]:
        """{tmdb_episode_number: {title, overview, still_url}}"""
        if not self.enabled:
            return {}
        data = await self._get(f"/tv/{tv_id}/season/{season}")
        if not data:
            return {}
        return {
            e["episode_number"]: {
                "title": e.get("name"),
                "overview": e.get("overview"),
                "still_url": f"{IMG}{e['still_path']}" if e.get("still_path") else None,
                "aired": e.get("air_date"),
                "rating": e.get("vote_average") or None,
            }
            for e in data.get("episodes", [])
        }

    async def backdrop(self, tv_id: int | None = None, movie_id: int | None = None) -> str | None:
        if not self.enabled:
            return None
        data = await self._get(f"/tv/{tv_id}" if tv_id else f"/movie/{movie_id}")
        if data and data.get("backdrop_path"):
            return f"{IMG}{data['backdrop_path']}"
        return None
