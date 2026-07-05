"""AniList GraphQL client (no auth needed for reads)."""

from __future__ import annotations

import asyncio
import re
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
    description(asHtml: false)
    averageScore
    genres
    coverImage { extraLarge }
    bannerImage
    studios(isMain: true) { nodes { name } }
    season
    seasonYear
    duration
    source
    startDate { year month day }
    endDate { year month day }
    characters(perPage: 15, sort: [ROLE, RELEVANCE]) {
      edges {
        role
        node { name { full } image { large } }
        voiceActors(language: JAPANESE) { name { full } image { large } }
      }
    }
    streamingEpisodes { title thumbnail }
    nextAiringEpisode { episode airingAt }
    relations { edges { relationType node { id format title { romaji english } startDate { year } } } }
"""

_SERIES_FORMATS = {"TV", "TV_SHORT", "ONA"}

_SEARCH = f"query ($q: String) {{ Page(perPage: 5) {{ media(search: $q, type: ANIME) {{ {_MEDIA_FIELDS} }} }} }}"
_BY_ID = f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{ {_MEDIA_FIELDS} }} }}"
_BY_IDS = f"query ($ids: [Int]) {{ Page(perPage: 10) {{ media(id_in: $ids, type: ANIME) {{ {_MEDIA_FIELDS} }} }} }}"


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
        "description": media.get("description") or "",
        "score": media.get("averageScore"),
        "genres": media.get("genres") or [],
        "cover_url": (media.get("coverImage") or {}).get("extraLarge"),
        "banner_url": media.get("bannerImage"),
        "studio": next((s["name"] for s in (media.get("studios") or {}).get("nodes", [])), None),
        "premiered": _date(media.get("startDate")),
        "ended": _date(media.get("endDate")),
        "runtime": media.get("duration"),
        "source_material": media.get("source"),  # MANGA, LIGHT_NOVEL, ORIGINAL...
        "characters": [
            {
                "character": e["node"]["name"]["full"],
                "character_image": (e["node"].get("image") or {}).get("large"),
                "role": e["role"],
                "va": e["voiceActors"][0]["name"]["full"] if e.get("voiceActors") else None,
                "va_image": (e["voiceActors"][0].get("image") or {}).get("large") if e.get("voiceActors") else None,
            }
            for e in (media.get("characters") or {}).get("edges", [])
        ],
        "episode_titles": _episode_titles(media.get("streamingEpisodes") or []),
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


def _date(d: dict | None) -> str | None:
    if not d or not d.get("year"):
        return None
    return f"{d['year']:04d}-{d.get('month') or 1:02d}-{d.get('day') or 1:02d}"


_EP_TITLE = re.compile(r"^Episode (\d+) - (.+)$")  # decimals ("Episode 5.5 - Recap") intentionally don't match


def _episode_titles(streaming: list[dict]) -> dict[int, str]:
    """CR-sourced streamingEpisodes titles: 'Episode 5 - The Title' -> {5: 'The Title'}."""
    out = {}
    for ep in streaming:
        m = _EP_TITLE.match(ep.get("title") or "")
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def _prequel(media: dict[str, Any]) -> dict | None:
    # cours can chain through OVAs and movies; the walk follows every PREQUEL
    # edge and only the season counter filters by format
    for rel in media["relations"]:
        if rel["type"] == "PREQUEL":
            return rel
    return None


async def franchise_members(client: httpx.AsyncClient, media: dict[str, Any], conn) -> list[dict[str, Any]]:
    """All franchise entries: walk to the prequel root, then follow SEQUEL
    edges forward. Movies and OVAs are members too."""
    root_fr = await franchise(client, media, conn)
    root = await by_id(client, root_fr["show_key"], conn)
    seen: dict[int, dict] = {root["anilist_id"]: root}
    queue = [root]
    while queue and len(seen) < 30:  # bounded; pathological graphs exist
        current = queue.pop(0)
        for rel in current["relations"]:
            if rel["type"] != "SEQUEL" or rel["id"] in seen:
                continue
            entry = await by_id(client, rel["id"], conn)
            seen[entry["anilist_id"]] = entry
            queue.append(entry)
    return list(seen.values())


async def franchise(client: httpx.AsyncClient, media: dict[str, Any], conn=None) -> dict[str, Any]:
    """Walk PREQUEL relations to the franchise root: gives Jellyfin one show
    with one season folder per AniList entry. All AniList data — no TVDB.

    Season = 1 + count of series-format entries earlier in the chain;
    OVA/special entries land in Season 00. Non-linear relation graphs need
    the --show-root/--season overrides.
    """
    if media["format"] == "MOVIE":
        return {"show_key": media["anilist_id"], "show_title": media["title"],
                "show_year": media["year"], "season": 1}
    chain = [media]
    current = media
    for _ in range(20):  # bounded walk; non-linear graphs need manual overrides
        prev = _prequel(current)
        if prev is None:
            break
        current = await by_id(client, prev["id"], conn)  # throttled + cached
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


# Global pacing: AniList's degraded limit is 30 req/min and repeat offenders
# get temp IP bans. One lock + minimum spacing across ALL callers (daily loops,
# franchise walks, seerr adds) keeps us at <=20/min no matter what overlaps.
_throttle = asyncio.Lock()
_last_request = 0.0
_MIN_INTERVAL = 3.0


async def _query(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    global _last_request
    async with _throttle:
        wait = _last_request + _MIN_INTERVAL - asyncio.get_event_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)
        r = await client.post(API, json={"query": query, "variables": variables})
        if r.status_code == 429:  # honor Retry-After, retry once
            await asyncio.sleep(int(r.headers.get("Retry-After", 60)))
            r = await client.post(API, json={"query": query, "variables": variables})
        _last_request = asyncio.get_event_loop().time()
    r.raise_for_status()
    return r.json()["data"]


async def search(client: httpx.AsyncClient, term: str) -> list[dict[str, Any]]:
    data = await _query(client, _SEARCH, {"q": term})
    return [_clean(m) for m in data["Page"]["media"]]


async def _cache_get(conn, anilist_ids: list[int]) -> dict[int, dict]:
    """FINISHED media is immutable on the axes we use -> cached permanently;
    airing entries expire after 6 hours (episode counts move)."""
    cur = await conn.execute(
        """SELECT anilist_id, payload FROM anilist_cache WHERE anilist_id = ANY(%s)
           AND (payload->>'status' = 'FINISHED' OR fetched_at > now() - interval '6 hours')""",
        (anilist_ids,),
    )
    return {r["anilist_id"]: r["payload"] for r in await cur.fetchall()}


async def _cache_put(conn, media_list: list[dict]) -> None:
    import json

    async with conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO anilist_cache (anilist_id, payload, fetched_at) VALUES (%s, %s, now())
               ON CONFLICT (anilist_id) DO UPDATE SET payload = EXCLUDED.payload, fetched_at = now()""",
            [(m["anilist_id"], json.dumps(m)) for m in media_list],
        )


async def by_id(client: httpx.AsyncClient, anilist_id: int, conn=None) -> dict[str, Any]:
    if conn is not None:
        cached = await _cache_get(conn, [anilist_id])
        if anilist_id in cached:
            return cached[anilist_id]
    data = await _query(client, _BY_ID, {"id": anilist_id})
    media = _clean(data["Media"])
    if conn is not None:
        await _cache_put(conn, [media])
    return media


async def by_ids(client: httpx.AsyncClient, anilist_ids: list[int], conn) -> dict[int, dict[str, Any]]:
    """Batch fetch: one Page(id_in:) request per 10 ids for whatever the cache
    doesn't already hold. A whole franchise costs one API call instead of N."""
    out = await _cache_get(conn, anilist_ids)
    missing = [i for i in anilist_ids if i not in out]
    for chunk in (missing[i : i + 10] for i in range(0, len(missing), 10)):
        data = await _query(client, _BY_IDS, {"ids": chunk})
        fetched = [_clean(m) for m in data["Page"]["media"]]
        await _cache_put(conn, fetched)
        out.update({m["anilist_id"]: m for m in fetched})
    return out
