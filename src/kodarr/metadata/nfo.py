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

from kodarr.metadata import anilist
from kodarr import db
from kodarr.library import organize

log = logging.getLogger(__name__)


def _write_xml(root: ET.Element, path: Path) -> None:
    # atomic: jellyfin scans race NFO rewrites, and a torn read leaves items
    # with whatever parsed before the truncation (title but no episode number)
    ET.indent(root)
    tmp = path.with_suffix(".nfo.tmp")
    tmp.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    tmp.replace(path)


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


def _external_ids(root: ET.Element, idmap: dict | None) -> None:
    """tvdb/tmdb uniqueids alongside the anilist one. Jellyfin surfaces these
    as ProviderIds, which is how Seerr maps library items to requests —
    without them everything sits at 'Requested' forever."""
    if not idmap:
        return
    for typ, val in (("tvdb", idmap.get("tvdb_id")), ("tmdb", idmap.get("tmdb_tv_id") or idmap.get("tmdb_movie_id"))):
        if val:
            uid = ET.SubElement(root, "uniqueid")
            uid.set("type", typ)
            uid.text = str(val)


async def write_show(
    http: httpx.AsyncClient, show_dir: Path, root_media: dict[str, Any], idmap: dict | None = None
) -> None:
    """tvshow.nfo + poster/fanart from the franchise root entry."""
    show_dir.mkdir(parents=True, exist_ok=True)
    tv = ET.Element("tvshow")
    _common(tv, root_media)
    _external_ids(tv, idmap)
    _write_xml(tv, show_dir / "tvshow.nfo")
    await _download(http, root_media.get("cover_url"), show_dir / "poster.jpg")
    await _download(http, root_media.get("banner_url"), show_dir / "fanart.jpg")


def season_title(entry_title: str, show_title: str | None, season: int | None) -> str:
    """Season display name: the entry title minus the show-title prefix, so
    sequels shorten to "Season 2"/"Part 2" and arc titles keep their names."""
    short = entry_title
    if show_title and entry_title.lower().startswith(show_title.lower()):
        short = entry_title[len(show_title):].strip(" :-–")
    if not short:
        return "Specials" if season == 0 else f"Season {season if season is not None else 1}"
    return short


async def write_season(http: httpx.AsyncClient, series: dict[str, Any], media: dict[str, Any]) -> None:
    """season.nfo + folder.jpg inside the entry's Season NN dir."""
    season_dir = organize.series_dir(series)
    season_dir.mkdir(parents=True, exist_ok=True)
    sn = ET.Element("season")
    _common(sn, media)
    t = sn.find("title")
    if t is not None:
        sn.remove(t)
    _el(sn, "title", season_title(media["title"], series.get("show_title"), series.get("season")))
    _el(sn, "seasonnumber", series.get("season") if series.get("season") is not None else 1)
    _write_xml(sn, season_dir / "season.nfo")
    await _download(http, media.get("cover_url"), season_dir / "folder.jpg")


def write_episode(
    video_path: Path, series: dict[str, Any], episode: int, title: str | None,
    overview: str | None = None,
    aired: str | None = None, rating: float | None = None, still_url: str | None = None,
) -> None:
    ep = ET.Element("episodedetails")
    _el(ep, "title", title or f"Episode {episode}")
    _el(ep, "season", series.get("season") if series.get("season") is not None else 1)
    _el(ep, "episode", episode)
    _el(ep, "aired", aired)
    _el(ep, "rating", rating)
    # records which TMDB still the local -thumb.jpg came from, so a mapping
    # correction invalidates the stale image instead of leaving it forever
    _el(ep, "thumb", still_url)
    # no release-name suffix in the plot: jellyfin already shows the filename
    _el(ep, "plot", (overview or "").strip() or None)
    _write_xml(ep, video_path.with_suffix(".nfo"))


async def write_movie(
    http: httpx.AsyncClient, series: dict[str, Any], media: dict[str, Any], idmap: dict | None = None
) -> None:
    d = organize.series_dir(series)
    d.mkdir(parents=True, exist_ok=True)
    mv = ET.Element("movie")
    _common(mv, media)
    _external_ids(mv, idmap)
    _write_xml(mv, d / "movie.nfo")
    await _download(http, media.get("cover_url"), d / "poster.jpg")
    await _download(http, media.get("banner_url"), d / "fanart.jpg")


async def refresh_series(conn, http: httpx.AsyncClient, tmdb_client, s: dict[str, Any],
                         roots_done: set[int] | None = None) -> None:
    """Write/update NFOs + artwork for one series entry. AniList for
    structure/plot/ratings; TMDB (when keyed) enriches episode titles,
    overviews, stills and show backdrops. Called per-series after imports
    and from the recent/daily passes; refresh_all shares roots_done so a
    franchise's show NFO is only rewritten once per sweep."""
    if roots_done is None:
        roots_done = set()
    media = await anilist.by_id(http, s["anilist_id"], conn)
    idmap = await db.get_id_map(conn, s["anilist_id"])
    if s["format"] == "MOVIE":
        await write_movie(http, s, media, idmap)
        if tmdb_client and idmap and idmap.get("tmdb_movie_id"):
            url = await tmdb_client.backdrop(movie_id=idmap["tmdb_movie_id"])
            if url:
                await _download(http, url, organize.series_dir(s) / "fanart.jpg")
        return
    key = s.get("show_key") or s["anilist_id"]
    show_dir = organize.series_dir(s).parent
    if key not in roots_done:
        root_media = media if key == s["anilist_id"] else await anilist.by_id(http, key, conn)
        if s.get("show_title"):  # manual show-title overrides beat the root entry's title
            root_media = {**root_media, "title": s["show_title"]}
        await write_show(http, show_dir, root_media, idmap)
        if tmdb_client and idmap and idmap.get("tmdb_tv_id"):
            url = await tmdb_client.backdrop(tv_id=idmap["tmdb_tv_id"])
            if url:
                (show_dir / "fanart.jpg").unlink(missing_ok=True)
                await _download(http, url, show_dir / "fanart.jpg")
        roots_done.add(key)
    cur = await conn.execute(
        "SELECT count(*) AS n FROM episodes WHERE anilist_id = %s AND file_path IS NOT NULL", (s["anilist_id"],)
    )
    if (await cur.fetchone())["n"] == 0:
        return  # no files yet: don't create empty season dirs jellyfin would render
    await write_season(http, s, media)

    # episode enrichment, weakest to strongest: anilist streamingEpisodes,
    # AniDB episode identity (official titles + airdates), TMDB (stills/overviews).
    # int(n): payloads read back from the anilist_cache jsonb have string keys
    titles: dict[int, dict] = {int(n): {"title": t} for n, t in media["episode_titles"].items()}
    if idmap and idmap.get("anidb_id"):
        cur = await conn.execute(
            "SELECT number, title_en, airdate FROM anidb_episodes WHERE anidb_id = %s AND type = 1",
            (idmap["anidb_id"],),
        )
        for e in await cur.fetchall():
            info = titles.setdefault(e["number"], {})
            if e["title_en"]:
                info["title"] = e["title_en"]
            if e["airdate"]:
                info["aired"] = str(e["airdate"])
    if tmdb_client and idmap and idmap.get("tmdb_tv_id") and idmap.get("tmdb_season") is not None:
        # split cours share one TMDB season; our episode N is TMDB episode
        # N + offset. Priority: manual override (TMDB "Specials" seasons can
        # defy arithmetic) > anime-lists derived offset > summed siblings.
        cur = await conn.execute(
            "SELECT tmdb_offset FROM id_map_overrides WHERE anilist_id = %s AND tmdb_offset IS NOT NULL",
            (s["anilist_id"],),
        )
        row = await cur.fetchone()
        am = None
        if idmap.get("anidb_id"):
            cur = await conn.execute(
                "SELECT episode_offset, season_map FROM anidb_map WHERE anidb_id = %s", (idmap["anidb_id"],))
            am = await cur.fetchone()
        season_map = (am or {}).get("season_map") or {}
        tmdb_season = idmap["tmdb_season"]
        if row:
            tmdb_off = row["tmdb_offset"]
        elif season_map:
            # entry's regular episodes live outside defaulttvdbseason (e.g.
            # Nekomonogatari -> TVDB/TMDB S0 E5-8): explicit per-season mapping
            tmdb_season = season_map.get("tvdbseason", tmdb_season)
            tmdb_off = season_map.get("offset", 0)
        elif am is not None:
            tmdb_off = am["episode_offset"]
        else:
            cur = await conn.execute(
                """SELECT COALESCE(SUM(sib.episodes), 0) AS off
                   FROM series sib JOIN id_map im ON im.anilist_id = sib.anilist_id
                   WHERE im.tmdb_tv_id = %s AND im.tmdb_season = %s AND sib.season < %s""",
                (idmap["tmdb_tv_id"], idmap["tmdb_season"], s.get("season") or 1),
            )
            tmdb_off = (await cur.fetchone())["off"]
        tmdb_eps = await tmdb_client.season_episodes(idmap["tmdb_tv_id"], tmdb_season)
        # include episodes on disk beyond the counted range (web extras like
        # Bakemonogatari 13-15 live past `episodes` but exist upstream)
        cur = await conn.execute(
            "SELECT absolute_number FROM episodes WHERE anilist_id=%s AND file_path IS NOT NULL", (s["anilist_id"],))
        nums = {r["absolute_number"] for r in await cur.fetchall()}
        nums |= set(range(1, (s.get("episodes") or s.get("aired") or 0) + 1))
        if tmdb_season == 0 and not row:
            # Specials seasons: anime-lists numbering is TVDB's, and TMDB's S0
            # orders differently — numeric mapping lies. Match by air date
            # (AniDB airdates are already in `titles`), skip ambiguous dates.
            by_date: dict[str, list[dict]] = {}
            for info in tmdb_eps.values():
                if info.get("aired"):
                    by_date.setdefault(info["aired"], []).append(info)
            for our_ep in sorted(nums):
                hits = by_date.get(titles.get(our_ep, {}).get("aired") or "", [])
                if len(hits) == 1:
                    titles[our_ep] = {**titles.get(our_ep, {}), **{k: v for k, v in hits[0].items() if v}}
        else:
            pairs = {int(k): v for k, v in (season_map.get("pairs") or {}).items()}
            for our_ep in sorted(nums):
                info = tmdb_eps.get(pairs.get(our_ep, our_ep + tmdb_off))
                if info:
                    titles[our_ep] = {**titles.get(our_ep, {}), **{k: v for k, v in info.items() if v}}
    cur = await conn.execute(
        "SELECT absolute_number, file_path, title FROM episodes WHERE anilist_id=%s AND file_path IS NOT NULL",
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
            # stale-thumb detection: the previous NFO records the still URL
            # its -thumb.jpg came from; if the mapping moved, refetch
            still = info.get("still_url")
            thumb = p.with_name(p.stem + "-thumb.jpg")
            old_nfo = p.with_suffix(".nfo")
            if still and thumb.exists() and old_nfo.exists():
                try:
                    prev = ET.parse(old_nfo).getroot().findtext("thumb")
                except ET.ParseError:
                    prev = None
                if prev != still:
                    thumb.unlink(missing_ok=True)
            write_episode(p, s, e["absolute_number"], title, info.get("overview"),
                          info.get("aired"), info.get("rating"), still)
            if still:
                await _download(http, still, thumb)
    log.info("nfo written", extra={"event": "nfo", "anilist_id": s["anilist_id"], "series": s["title"]})


async def refresh_all(conn, http: httpx.AsyncClient, tmdb_client=None) -> None:
    """Write/update NFOs + artwork for the whole library."""
    rows = await db.monitored_series(conn)
    # one batched fetch covers every entry + franchise root
    ids = list({i for s in rows for i in (s["anilist_id"], s.get("show_key") or s["anilist_id"])})
    await anilist.by_ids(http, ids, conn)
    roots_done: set[int] = set()
    for s in rows:
        await refresh_series(conn, http, tmdb_client, s, roots_done)
