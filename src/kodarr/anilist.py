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
    relations { edges { relationType node { id format title { romaji english } startDate { year } } } }
"""

_SERIES_FORMATS = {"TV", "TV_SHORT", "ONA"}

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
        "relations": [
            {
                "type": e["relationType"],
                "id": e["node"]["id"],
                "format": e["node"]["format"],
                "title": e["node"]["title"]["english"] or e["node"]["title"]["romaji"],
                "year": (e["node"].get("startDate") or {}).get("year"),
            }
            for e in (media.get("relations") or {}).get("edges", [])
        ],
    }


def _prequel(media: dict[str, Any]) -> dict | None:
    # any format: cours chain THROUGH OVAs (Slime S1 <- Coleus OVA <- S2) and
    # movies (Bunny Girl S1 <- 3 movies <- Santa) — the walk must skip nothing;
    # only the season counter filters by format
    for rel in media["relations"]:
        if rel["type"] == "PREQUEL":
            return rel
    return None


async def franchise(client: httpx.AsyncClient, media: dict[str, Any]) -> dict[str, Any]:
    """Walk PREQUEL relations to the franchise root: gives Jellyfin one show
    with one season folder per AniList entry. All AniList data — no TVDB.

    Season = 1 + how many series-format (TV/ONA) entries precede this one in
    the chain. OVA/special entries land in Season 00.
    # ponytail: two specials entries in one franchise would collide in S00
    # numbering — split with --show-root/--season overrides if that ever happens.
    """
    if media["format"] == "MOVIE":
        return {"show_key": media["anilist_id"], "show_title": media["title"],
                "show_year": media["year"], "season": 1}
    chain = [media]
    current = media
    for _ in range(20):  # ponytail: linear-chain walk; weird graphs -> --show-root/--season overrides
        prev = _prequel(current)
        if prev is None:
            break
        await asyncio.sleep(1)  # AniList politeness
        current = await by_id(client, prev["id"])
        chain.append(current)
    root = chain[-1]
    if media["format"] in _SERIES_FORMATS:
        season = 1 + sum(1 for m in chain[1:] if m["format"] in _SERIES_FORMATS)
    else:
        season = 0  # specials
    return {
        "show_key": root["anilist_id"],
        "show_title": root["title"],
        "show_year": root["year"],
        "season": season,
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
