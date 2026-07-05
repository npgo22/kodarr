"""The two paths that silently corrupt a library if wrong: matching and layout."""

from pathlib import Path

from kodarr import match, organize

FRIEREN = {
    "anilist_id": 154587,
    "title": "Frieren: Beyond Journey's End",
    "year": 2023,
    "format": "TV",
    "episodes": 28,
    "synonyms": ["Sousou no Frieren", "葬送のフリーレン"],
    "episode_offset": 0,
    "root_path": "/data/media/anime",
    "preferred_group": "SubsPlease",
}
MOVIE = {
    "anilist_id": 142770,
    "title": "Suzume",
    "year": 2022,
    "format": "MOVIE",
    "episodes": 1,
    "synonyms": ["Suzume no Tojimari"],
    "episode_offset": 0,
    "root_path": "/data/media/anime-movies",
    "preferred_group": "SubsPlease",
}
COUR2 = {**FRIEREN, "anilist_id": 999999, "title": "Frieren Season 2", "synonyms": ["Sousou no Frieren"], "episode_offset": 28}


def test_parse_subsplease():
    p = match.parse("[SubsPlease] Sousou no Frieren - 01 (1080p) [ABCD1234].mkv")
    assert p and p.title == "Sousou no Frieren" and p.episode == 1 and p.group == "SubsPlease"


def test_match_by_synonym():
    p = match.parse("[SubsPlease] Sousou no Frieren - 05 (1080p).mkv")
    assert p
    m = match.match(p, [FRIEREN])
    assert m and m[0]["anilist_id"] == 154587 and m[1] == 5


def test_match_offset_routes_to_sequel_entry():
    # ep 30 absolute: out of range for entry 1 (28 eps), maps to ep 2 of cour 2
    p = match.parse("[SubsPlease] Sousou no Frieren - 30 (1080p).mkv")
    assert p
    m = match.match(p, [FRIEREN, COUR2])
    assert m and m[0]["anilist_id"] == 999999 and m[1] == 2


def test_season_tagged_release_routes_to_cour_without_offset():
    # "S2 - 04" is per-cour numbering: must hit the season-2 entry as ep 4,
    # ignoring the absolute episode_offset, and never the season-1 entry.
    p = match.parse("[Erai-raws] Sousou no Frieren S2 - 04 (1080p).mkv")
    assert p and p.season == 2 and p.episode == 4
    m = match.match(p, [FRIEREN, COUR2])
    assert m and m[0]["anilist_id"] == 999999 and m[1] == 4


def test_season_one_release_skips_sequel_entry():
    p = match.parse("[SubsPlease] Sousou no Frieren S1 - 04 (1080p).mkv")
    assert p and p.season == 1
    m = match.match(p, [COUR2, FRIEREN])  # sequel listed first on purpose
    assert m and m[0]["anilist_id"] == 154587


def test_digit_title_not_read_as_season():
    eighty_six = {**FRIEREN, "anilist_id": 116589, "title": "86: Eighty Six", "synonyms": ["86"], "episodes": 11}
    p = match.parse("[SubsPlease] 86 - Eighty Six S1 - 03 (1080p).mkv")
    assert p and p.season == 1
    m = match.match(p, [eighty_six])
    assert m and m[0]["anilist_id"] == 116589 and m[1] == 3


_S = {"format": "TV", "episode_offset": 0, "preferred_group": "SubsPlease"}
SLIME = [  # real AniList entries (2026-07); the show sonarr-anime misrouted
    {**_S, "anilist_id": 101280, "title": "That Time I Got Reincarnated as a Slime", "episodes": 24, "aired": 24, "synonyms": ["Tensei Shitara Slime Datta Ken", "TenSura"]},
    {**_S, "anilist_id": 108511, "title": "That Time I Got Reincarnated as a Slime Season 2", "episodes": 12, "aired": 12, "synonyms": ["Tensei Shitara Slime Datta Ken 2nd Season", "TenSura 2"]},
    {**_S, "anilist_id": 156822, "title": "That Time I Got Reincarnated as a Slime Season 3", "episodes": 24, "aired": 24, "synonyms": ["Tensei Shitara Slime Datta Ken 3rd Season", "Tensura 3"]},
    {**_S, "anilist_id": 182205, "title": "That Time I Got Reincarnated as a Slime Season 4", "episodes": None, "aired": 13, "synonyms": ["Tensei Shitara Slime Datta Ken 4th Season", "Tensura 4"]},
]


def test_slime_real_release_forms():
    """Real Nyaa titles for Slime S4 (the show sonarr fetched wrong). Every
    group's naming form must land on the S4 entry, never S1."""
    cases = [
        ("[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 13 (1080p) [C3528385].mkv", 13),
        ("[Erai-raws] Tensei Shitara Slime Datta Ken 4th Season - 13 [1080p CR WEBRip HEVC AAC][MultiSub]", 13),
        ("[ASW] Tensei Shitara Slime Datta Ken S4 - 13 [1080p HEVC x265 10Bit][AAC]", 13),
        ("[DKB] Tensei shitara Slime Datta Ken - S04E13 [1080p][HEVC x265 10bit][Multi-Subs][weekly]", 13),
        # parenthesized alt-title
        ("[Judas] Tensei Shitara Slime Datta Ken (That Time I Got Reincarnated as a Slime) - S04E13 [1080p]", 13),
        # scene form: anitopy returns anime_season as ['4','04'] — must collapse, not drop
        ("That Time I Got Reincarnated as a Slime S04E10 The Master of Greed 1080p CR WEB-DL MULTi AAC2.0 H 264-VARYG (Tensei Shitara Slime Datta Ken 4th Season, Multi-Subs)", 10),
        ("[Yameii] That Time I Got Reincarnated as a Slime - S04E10 [English Dub] [CR WEB-DL 1080p H264 AAC] (Tensei Shitara Slime Datta Ken Season 4 | S4)", 10),
        # trailing season digit in title
        ("[Ironclad] Tensei Shitara Slime Datta Ken 4 - S04E13 [WEB.1080p.AV1] | That Time I Got Reincarnated as a Slime (Multi-Subs)", 13),
    ]
    for name, want_ep in cases:
        p = match.parse(name)
        assert p, name
        m = match.match(p, SLIME)
        assert m and m[0]["anilist_id"] == 182205 and m[1] == want_ep, f"{name} -> {m}"


def test_split_cour_pack_routes_by_offset():
    # Slime S2 = 12+12 across two AniList entries; pack files say S02E13-24.
    # Part 2 carries episode_offset=12 so the overflow lands there as 1-12.
    part1 = {**_S, "anilist_id": 108511, "title": "That Time I Got Reincarnated as a Slime Season 2", "episodes": 12, "aired": 12, "synonyms": ["Tensei Shitara Slime Datta Ken 2nd Season"]}
    part2 = {**part1, "anilist_id": 116742, "episode_offset": 12, "synonyms": ["Tensei Shitara Slime Datta Ken 2nd Season Part 2"]}
    rows = [part1, part2]
    for name, want in [
        ("That.Time.I.Got.Reincarnated.as.a.Slime.S02E05.1080p.BluRay.Remux-CRUCiBLE.mkv", (108511, 5)),
        ("That.Time.I.Got.Reincarnated.as.a.Slime.S02E13.1080p.BluRay.Remux-CRUCiBLE.mkv", (116742, 1)),
        ("That.Time.I.Got.Reincarnated.as.a.Slime.S02E24.1080p.BluRay.Remux-CRUCiBLE.mkv", (116742, 12)),
    ]:
        p = match.parse(name)
        assert p
        m = match.match(p, rows)
        assert m and (m[0]["anilist_id"], m[1]) == want, f"{name} -> {m}"


def test_slime_s1_and_airing_bounds():
    # season-less S1-era name stays on the S1 entry
    p = match.parse("[SubsPlease] Tensei Shitara Slime Datta Ken - 08 (1080p).mkv")
    assert p
    m = match.match(p, SLIME)
    assert m and m[0]["anilist_id"] == 101280 and m[1] == 8
    # RELEASING entry has episodes=None: cap at aired+1 so junk numbering is rejected
    p = match.parse("[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 99 (1080p).mkv")
    assert p
    assert match.match(p, SLIME) is None


def test_rank_preferred_group_resolution_then_seeders():
    from kodarr.search import rank

    results = [
        {"title": "That Time I Got Reincarnated as a Slime S04E06 1080p CR WEB-DL MULTi AAC2.0 H 264-VARYG (Tensei Shitara Slime Datta Ken 4th Season, Multi-Subs)", "seeders": 900, "url": "u1"},
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (720p) [BBBB].mkv", "seeders": 500, "url": "u2"},
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (1080p) [AAAA].mkv", "seeders": 100, "url": "u3"},
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (1080p) [CCCC].mkv", "seeders": 400, "url": "u4"},
    ]
    ranked = rank(results, SLIME[3], 6)
    # non-preferred group excluded; sub-1080p excluded; seeders break the resolution tie
    assert [r["url"] for _, r in ranked] == ["u4", "u3"]


def test_jellyfin_path_translation():
    import asyncio
    import json

    import httpx

    from kodarr.clients import Jellyfin

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read())["Updates"][0]["Path"])
        return httpx.Response(204)

    async def main():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        jf = Jellyfin(http, "http://jf", "key", "/data", "/media")
        await jf.notify("/data/media/anime/Show [anilist-1]")

    asyncio.run(main())
    assert seen == ["/media/media/anime/Show [anilist-1]"]


def test_no_match():
    p = match.parse("[SubsPlease] Some Other Show - 01 (1080p).mkv")
    assert p
    assert match.match(p, [FRIEREN]) is None


def test_dest_path_episode():
    # no franchise fields -> entry is its own show, season 1
    d = organize.dest_path(FRIEREN, 5, "SubsPlease", ".mkv")
    assert d == Path(
        "/data/media/anime/Frieren Beyond Journey's End (2023) [anilist-154587]/"
        "Season 01/Frieren Beyond Journey's End S01E005 [SubsPlease].mkv"
    )


def test_dest_path_franchise_season():
    cour2 = {**FRIEREN, "anilist_id": 182255, "title": "Frieren Season 2",
             "show_key": 154587, "show_title": "Frieren: Beyond Journey's End", "show_year": 2023, "season": 2}
    d = organize.dest_path(cour2, 4, "SubsPlease", ".mkv")
    assert d == Path(
        "/data/media/anime/Frieren Beyond Journey's End (2023) [anilist-154587]/"
        "Season 02/Frieren Beyond Journey's End S02E004 [SubsPlease].mkv"
    )
    # specials entries go to Season 00, not Season 01
    ova = {**cour2, "format": "OVA", "season": 0}
    d = organize.dest_path(ova, 2, "Mehul", ".mkv")
    assert d.parent.name == "Season 00" and d.name.startswith("Frieren Beyond Journey's End S00E002")


def test_franchise_walk_through_ova():
    """Slime's real chain: S2's prequel is the Coleus OVA, whose prequel is S1.
    The walk must hop the OVA; the OVA itself is Season 00."""
    import asyncio
    import json

    import httpx

    from kodarr import anilist

    def rel(type_, id_, fmt):
        return {"relationType": type_, "node": {"id": id_, "format": fmt, "title": {"romaji": f"n{id_}", "english": None}, "startDate": {"year": 2018}}}

    graph = {
        101280: [],
        161802: [rel("PREQUEL", 101280, "TV")],
        108511: [rel("PREQUEL", 161802, "OVA")],
        116742: [rel("PREQUEL", 108511, "TV")],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        aid = json.loads(request.read())["variables"]["id"]
        media = {"id": aid, "format": "OVA" if aid == 161802 else "TV", "status": "FINISHED", "episodes": 12,
                 "startDate": {"year": 2018}, "title": {"romaji": f"n{aid}", "english": None, "native": None},
                 "synonyms": [], "nextAiringEpisode": None,
                 "relations": {"edges": graph[aid]}}
        return httpx.Response(200, json={"data": {"Media": media}})

    async def main():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s2p2 = await anilist.by_id(http, 116742)
        fr = await anilist.franchise(http, s2p2)
        assert fr["show_key"] == 101280 and fr["season"] == 3, fr  # S1, S2, S2P2
        ova = await anilist.by_id(http, 161802)
        fr = await anilist.franchise(http, ova)
        assert fr["show_key"] == 101280 and fr["season"] == 0, fr

    asyncio.run(main())


def test_dest_path_movie():
    d = organize.dest_path(MOVIE, None, "SubsPlease", ".mkv")
    assert d == Path("/data/media/anime-movies/Suzume (2022) [anilist-142770]/Suzume (2022) [SubsPlease].mkv")


def test_nfo_writes(tmp_path):
    import asyncio
    import xml.etree.ElementTree as ET

    import httpx

    from kodarr import nfo

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"jpegbytes")

    media = {"anilist_id": 154587, "title": "Frieren: Beyond Journey's End", "year": 2023,
             "description": "Elf & friends.", "score": 89, "genres": ["Adventure", "Fantasy"],
             "studio": "Madhouse", "cover_url": "https://s4.anilist.co/cover.jpg",
             "banner_url": "https://s4.anilist.co/banner.jpg", "episode_titles": {1: "The Journey's End"}}
    series = {**FRIEREN, "root_path": str(tmp_path), "show_title": media["title"],
              "show_key": 154587, "show_year": 2023, "season": 1}

    async def main():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        from kodarr import organize
        await nfo.write_show(http, organize.series_dir(series).parent, media,
                             {"tvdb_id": 424536, "tmdb_tv_id": 209867, "tmdb_movie_id": None})
        await nfo.write_season(http, series, media)
        vid = organize.series_dir(series) / "x S01E001.mkv"
        vid.write_bytes(b"v")
        nfo.write_episode(vid, series, 1, media["episode_titles"][1])

    asyncio.run(main())
    show_dir = tmp_path / "Frieren Beyond Journey's End (2023) [anilist-154587]"
    tv = ET.parse(show_dir / "tvshow.nfo").getroot()
    assert tv.findtext("title") == "Frieren: Beyond Journey's End"
    assert tv.findtext("rating") == "8.9"
    uids = {u.get("type"): u.text for u in tv.findall("uniqueid")}
    assert uids == {"anilist": "154587", "tvdb": "424536", "tmdb": "209867"}
    assert (show_dir / "poster.jpg").read_bytes() == b"jpegbytes"
    season = ET.parse(show_dir / "Season 01" / "season.nfo").getroot()
    assert season.findtext("seasonnumber") == "1"
    ep = ET.parse(show_dir / "Season 01" / "x S01E001.nfo").getroot()
    assert ep.findtext("title") == "The Journey's End" and ep.findtext("episode") == "1"


def test_episode_title_parsing():
    from kodarr.anilist import _episode_titles

    titles = _episode_titles([
        {"title": "Episode 5 - The Master of Greed", "thumbnail": "x"},
        {"title": "Episode 5.5 - Recap", "thumbnail": "x"},
        {"title": "Some Movie Title", "thumbnail": "x"},
    ])
    assert titles == {5: "The Master of Greed"}


def test_import_hardlink_and_replace(tmp_path):
    src = tmp_path / "dl" / "ep.mkv"
    src.parent.mkdir()
    src.write_bytes(b"video")
    old = tmp_path / "lib" / "old.mkv"
    old.parent.mkdir()
    old.write_bytes(b"worse video")
    dest = tmp_path / "lib" / "new.mkv"

    organize.import_file(src, dest, replace=old)

    assert dest.read_bytes() == b"video"
    assert dest.stat().st_ino == src.stat().st_ino  # hardlinked, still seeds
    assert not old.exists()
    assert src.exists()


def test_season_title():
    from kodarr.nfo import season_title

    slime = "That Time I Got Reincarnated as a Slime"
    assert season_title(slime, slime, 1) == "Season 1"
    assert season_title(f"{slime} Season 2 Part 2", slime, 3) == "Season 2 Part 2"
    assert season_title(f"{slime}: Visions of Coleus", slime, 0) == "Visions of Coleus"
    assert season_title("Nisemonogatari", "Monogatari Series", 2) == "Nisemonogatari"
    assert season_title("Mushoku Tensei: Jobless Reincarnation Cour 2", "Mushoku Tensei: Jobless Reincarnation", 2) == "Cour 2"


def test_mushoku_short_title_matching():
    """SubsPlease truncates at the colon: 'Mushoku Tensei S3 - 01'. The full
    AniList title never appears in the release name."""
    s1 = {**_S, "anilist_id": 108465, "title": "Mushoku Tensei: Jobless Reincarnation", "episodes": 11, "aired": 11, "synonyms": ["Mushoku Tensei: Isekai Ittara Honki Dasu"]}
    s3 = {**_S, "anilist_id": 178789, "title": "Mushoku Tensei: Jobless Reincarnation Season 3", "episodes": 14, "aired": 2, "synonyms": ["Mushoku Tensei: Isekai Ittara Honki Dasu Season 3"]}
    p = match.parse("[SubsPlease] Mushoku Tensei S3 - 01 (1080p) [C3A7F258].mkv")
    assert p and p.season == 3 and p.episode == 1
    m = match.match(p, [s1, s3])
    assert m and m[0]["anilist_id"] == 178789 and m[1] == 1
    # unseasoned S1-era name must stay on S1, never leak into S3
    p = match.parse("[SubsPlease] Mushoku Tensei - 05 (1080p).mkv")
    assert p and p.season is None
    m = match.match(p, [s3, s1])  # s3 listed first on purpose
    assert m and m[0]["anilist_id"] == 108465


def test_franchise_members_includes_movies_and_specials():
    """Requesting any entry must enumerate the whole chain: TV cours, the OVA
    hop, and movies (TVDB's season shape drops those — the Monogatari lesson)."""
    import asyncio
    import json

    import httpx

    from kodarr import anilist

    def rel(type_, id_, fmt):
        return {"relationType": type_, "node": {"id": id_, "format": fmt, "title": {"romaji": f"n{id_}", "english": None}, "startDate": {"year": 2020}}}

    graph = {  # 1 (TV root) -> 2 (movie) -> 3 (TV); root also sequels to 4 (OVA)
        1: {"fmt": "TV", "rels": [rel("SEQUEL", 2, "MOVIE"), rel("SEQUEL", 4, "OVA")]},
        2: {"fmt": "MOVIE", "rels": [rel("PREQUEL", 1, "TV"), rel("SEQUEL", 3, "TV")]},
        3: {"fmt": "TV", "rels": [rel("PREQUEL", 2, "MOVIE")]},
        4: {"fmt": "OVA", "rels": [rel("PREQUEL", 1, "TV")]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        aid = json.loads(request.read())["variables"]["id"]
        g = graph[aid]
        media = {"id": aid, "format": g["fmt"], "status": "FINISHED", "episodes": 12,
                 "startDate": {"year": 2020}, "title": {"romaji": f"n{aid}", "english": None, "native": None},
                 "synonyms": [], "nextAiringEpisode": None, "relations": {"edges": g["rels"]}}
        return httpx.Response(200, json={"data": {"Media": media}})

    async def main():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        s3 = await anilist.by_id(http, 3)
        members = await anilist.franchise_members(http, s3, None)
        assert sorted(m["anilist_id"] for m in members) == [1, 2, 3, 4]

    asyncio.run(main())


def test_movie_backfill_rejects_tv_batch():
    """Slime movie searches matched the (01-48) TV batch: same short title,
    episode=None on both sides slipped every gate."""
    from kodarr.search import rank

    movie = {"anilist_id": 139498, "title": "That Time I Got Reincarnated as a Slime the Movie: Scarlet Bond",
             "format": "MOVIE", "episodes": 1, "aired": 1, "episode_offset": 0,
             "preferred_group": "SubsPlease", "synonyms": ["Tensei Shitara Slime Datta Ken Movie: Guren no Kizuna-hen"]}
    results = [
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken (01-48) (1080p) [Batch]", "seeders": 500, "url": "u1"},
    ]
    assert rank(results, movie, None) == []
