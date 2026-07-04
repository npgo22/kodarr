"""AniList GraphQL client (no auth needed for reads)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

API = "https://graphql.anilist.co"

_MEDIA_FIELDS = """
    id
    format
    status
    episodes
    startDate { year }
    title { romaji english native }
    synonyms
    nextAiringEpisode { episode airingAt }
"""

_SEARCH = f"query ($q: String) {{ Page(perPage: 5) {{ media(search: $q, type: ANIME) {{ {_MEDIA_FIELDS} }} }} }}"
_BY_ID = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{ {_MEDIA_FIELDS} }} }}"


def _clean(media: dict[str, Any]) -> dict[str, Any]:
    titles = media["title"]
    names = [t for t in (titles["romaji"], titles["english"], titles["native"]) if t]
    episodes = media["episodes"]
    next_airing = media.get("nextAiringEpisode") or {}
    if media["status"] == "RELEASING":
        aired = (next_airing.get("episode") or 1) - 1
    else:
        aired = episodes or 0
    return {
        "anilist_id": media["id"],
        "title": titles["english"] or titles["romaji"],
        "year": (media.get("startDate") or {}).get("year"),
        "format": media["format"] or "TV",
        "episodes": episodes,
        "aired": aired,
        "status": media["status"],
        "synonyms": list(dict.fromkeys(names + (media.get("synonyms") or []))),
    }


async def _query(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    r = await client.post(API, json={"query": query, "variables": variables})
    if r.status_code == 429:  # AniList rate limit (30/min degraded) — honor and retry once
        await asyncio.sleep(int(r.headers.get("Retry-After", 60)))
        r = await client.post(API, json={"query": query, "variables": variables})
    r.raise_for_status()
    return r.json()["data"]


async def search(client: httpx.AsyncClient, term: str) -> list[dict[str, Any]]:
    data = await _query(client, _SEARCH, {"q": term})
    return [_clean(m) for m in data["Page"]["media"]]


async def by_id(client: httpx.AsyncClient, anilist_id: int) -> dict[str, Any]:
    data = await _query(client, _BY_ID, {"id": anilist_id})
    return _clean(data["Media"])
