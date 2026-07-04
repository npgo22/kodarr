"""Kodi-style NFO + local artwork, written from AniList data kodarr already
fetched. Jellyfin reads these natively — scans need no metadata plugin, no
remote API calls, no fuzzy title matching. Same idea as Shoko, without a
custom Jellyfin plugin to maintain.

Layout written per show folder:
  tvshow.nfo, poster.jpg, fanart.jpg           (franchise root entry)
  Season NN/season.nfo, Season NN/folder.jpg   (per AniList entry)
  Season NN/<episode>.nfo                      (title when AniList knows it)
Movies: <movie>.nfo + poster.jpg in the movie folder.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

from kodarr import anilist, db, organize

log = logging.getLogger(__name__)


def _write_xml(root: ET.Element, path: Path) -> None:
    ET.indent(root)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _el(parent: ET.Element, tag: str, text: Any) -> None:
    if text is None or text == "":
        return
    e = ET.SubElement(parent, tag)
    e.text = str(text)


async def _download(http: httpx.AsyncClient, url: str | None, dest: Path) -> None:
    if not url or dest.exists():
        return
    try:
        r = await http.get(url, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
    except httpx.HTTPError as e:
        log.warning("artwork download failed", extra={"event": "error", "url": url, "error": str(e)})


def _common(root: ET.Element, media: dict[str, Any]) -> None:
    _el(root, "title", media["title"])
    _el(root, "originaltitle", next(iter(media.get("synonyms") or []), None))
    _el(root, "plot", media.get("description"))
    _el(root, "year", media.get("year"))
    _el(root, "premiered", media.get("premiered"))
    _el(root, "enddate", media.get("ended"))
    _el(root, "status", {"RELEASING": "Continuing", "FINISHED": "Ended"}.get(media.get("status") or "", None))
    _el(root, "runtime", media.get("runtime"))
    if media.get("score") is not None:
        _el(root, "rating", media["score"] / 10)  # anilist 0-100 -> nfo 0-10
    for g in media.get("genres") or []:
        _el(root, "genre", g)
    _el(root, "studio", media.get("studio"))
    if media.get("source_material"):
        _el(root, "tag", media["source_material"].replace("_", " ").title())
    uid = ET.SubElement(root, "uniqueid")
    uid.set("type", "anilist")
    uid.set("default", "true")
    uid.text = str(media["anilist_id"])
    # characters + japanese VAs, AniList images; jellyfin shows these as Cast
    for i, c in enumerate(media.get("characters") or []):
        if not c.get("va"):
            continue
        actor = ET.SubElement(root, "actor")
        _el(actor, "name", c["va"])
        _el(actor, "role", c["character"])
        _el(actor, "thumb", c.get("va_image"))
        _el(actor, "order", i)


async def write_show(http: httpx.AsyncClient, show_dir: Path, root_media: dict[str, Any]) -> None:
    """tvshow.nfo + poster/fanart from the franchise root entry."""
    show_dir.mkdir(parents=True, exist_ok=True)
    tv = ET.Element("tvshow")
    _common(tv, root_media)
    _write_xml(tv, show_dir / "tvshow.nfo")
    await _download(http, root_media.get("cover_url"), show_dir / "poster.jpg")
    await _download(http, root_media.get("banner_url"), show_dir / "fanart.jpg")


async def write_season(http: httpx.AsyncClient, series: dict[str, Any], media: dict[str, Any]) -> None:
    """season.nfo + folder.jpg inside the entry's Season NN dir, plus episode
    title NFOs for files already on disk."""
    season_dir = organize.series_dir(series)
    season_dir.mkdir(parents=True, exist_ok=True)
    sn = ET.Element("season")
    _common(sn, media)
    _el(sn, "seasonnumber", series.get("season") if series.get("season") is not None else 1)
    _write_xml(sn, season_dir / "season.nfo")
    await _download(http, media.get("cover_url"), season_dir / "folder.jpg")


def write_episode(
    video_path: Path, series: dict[str, Any], episode: int, title: str | None,
    overview: str | None = None, source: str | None = None,
    aired: str | None = None, rating: float | None = None,
) -> None:
    ep = ET.Element("episodedetails")
    _el(ep, "title", title or f"Episode {episode}")
    _el(ep, "season", series.get("season") if series.get("season") is not None else 1)
    _el(ep, "episode", episode)
    _el(ep, "aired", aired)
    _el(ep, "rating", rating)
    # AniDB-style provenance: the release this file came from, visible in the
    # episode info panel (tells BD vs WEB at a glance)
    plot = (overview or "").strip()
    if source:
        plot = f"{plot}\n\nSource: {source}" if plot else f"Source: {source}"
    _el(ep, "plot", plot or None)
    _write_xml(ep, video_path.with_suffix(".nfo"))


async def write_movie(http: httpx.AsyncClient, series: dict[str, Any], media: dict[str, Any]) -> None:
    d = organize.series_dir(series)
    d.mkdir(parents=True, exist_ok=True)
    mv = ET.Element("movie")
    _common(mv, media)
    _write_xml(mv, d / "movie.nfo")
    await _download(http, media.get("cover_url"), d / "poster.jpg")
    await _download(http, media.get("banner_url"), d / "fanart.jpg")


async def refresh_all(conn, http: httpx.AsyncClient, tmdb_client=None) -> None:
    """Write/update NFOs + artwork for the whole library. AniList for
    structure/plot/ratings; TMDB (when keyed) enriches episode titles,
    overviews, stills and show backdrops."""
    import asyncio

    rows = await db.monitored_series(conn)
    # one batched fetch covers every entry + franchise root
    ids = list({i for s in rows for i in (s["anilist_id"], s.get("show_key") or s["anilist_id"])})
    await anilist.by_ids(http, ids, conn)
    roots_done: set[int] = set()
    for s in rows:
        media = await anilist.by_id(http, s["anilist_id"], conn)
        idmap = await db.get_id_map(conn, s["anilist_id"])
        if s["format"] == "MOVIE":
            await write_movie(http, s, media)
            if tmdb_client and idmap and idmap.get("tmdb_movie_id"):
                url = await tmdb_client.backdrop(movie_id=idmap["tmdb_movie_id"])
                if url:
                    await _download(http, url, organize.series_dir(s) / "fanart.jpg")
            continue
        key = s.get("show_key") or s["anilist_id"]
        show_dir = organize.series_dir(s).parent
        if key not in roots_done:
            root_media = media if key == s["anilist_id"] else await anilist.by_id(http, key, conn)
            if s.get("show_title"):  # manual overrides ("Monogatari Series") beat the root entry's own title
                root_media = {**root_media, "title": s["show_title"]}
            await write_show(http, show_dir, root_media)
            if tmdb_client and idmap and idmap.get("tmdb_tv_id"):
                url = await tmdb_client.backdrop(tv_id=idmap["tmdb_tv_id"])
                if url:
                    (show_dir / "fanart.jpg").unlink(missing_ok=True)
                    await _download(http, url, show_dir / "fanart.jpg")
            roots_done.add(key)
        await write_season(http, s, media)

        # episode enrichment: anilist streamingEpisodes, then TMDB titles/overviews/stills
        titles: dict[int, dict] = {n: {"title": t} for n, t in media["episode_titles"].items()}
        if tmdb_client and idmap and idmap.get("tmdb_tv_id") and idmap.get("tmdb_season") is not None:
            tmdb_eps = await tmdb_client.season_episodes(idmap["tmdb_tv_id"], idmap["tmdb_season"])
            for our_ep in range(1, (s.get("episodes") or s.get("aired") or 0) + 1):
                info = tmdb_eps.get(our_ep + s["episode_offset"])
                if info:
                    titles[our_ep] = {**titles.get(our_ep, {}), **{k: v for k, v in info.items() if v}}
        cur = await conn.execute(
            "SELECT absolute_number, file_path, title, source_name FROM episodes WHERE anilist_id=%s AND file_path IS NOT NULL",
            (s["anilist_id"],),
        )
        for e in await cur.fetchall():
            info = titles.get(e["absolute_number"], {})
            title = info.get("title") or e["title"]
            if title and title != e["title"]:
                await conn.execute(
                    "UPDATE episodes SET title=%s WHERE anilist_id=%s AND absolute_number=%s",
                    (title, s["anilist_id"], e["absolute_number"]),
                )
            p = Path(e["file_path"])
            if p.exists():
                write_episode(p, s, e["absolute_number"], title, info.get("overview"), e["source_name"],
                              info.get("aired"), info.get("rating"))
                if info.get("still_url"):
                    await _download(http, info["still_url"], p.with_name(p.stem + "-thumb.jpg"))
        log.info("nfo written", extra={"event": "nfo", "anilist_id": s["anilist_id"], "series": s["title"]})
