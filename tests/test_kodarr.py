"""The two paths that silently corrupt a library if wrong: matching and layout."""

from pathlib import Path

from kodarr.library import match
from kodarr.library import organize

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


def test_strip_season_reduces_sequel_to_base():
    # groups release "Youjo Senki II" as "Youjo Senki S2 - NN": a backfill query
    # must drop the AniList cour marker or nyaa full-text AND-match finds nothing.
    n = lambda t: match._strip_season(match.normalize(t))
    assert n("Youjo Senki II") == "youjo senki"
    assert n("Mushoku Tensei III") == "mushoku tensei"
    assert n("Some Show S2") == "some show"
    assert n("Some Show 2nd Season") == "some show"
    assert n("Re:ZERO Season 4") == "re zero"
    # must NOT strip a number that is part of the name
    assert n("86") == "86"
    assert n("Mob Psycho 100") == "mob psycho 100"


def test_anidb_synonyms_parse():
    from kodarr.metadata import synonyms
    dump = "\n".join([
        "# <aid>|<type>|<language>|<title>",
        "1234|1|x-jat|Youjo Senki",              # primary romaji -> keep
        "1234|2|en|The Saga of Tanya the Evil",  # english synonym -> keep
        "1234|4|ja|幼女戦記",                     # official native -> keep
        "1234|2|ru|Военная хроника",             # other language -> drop (matcher noise)
        "1234|3|x-jat|YS",                       # short abbreviation -> drop
        "9999|1|x-jat|Unmapped",                 # aid not in the map -> drop
    ])
    got = synonyms._parse(dump, {1234: 42})
    assert got == {42: ["Youjo Senki", "The Saga of Tanya the Evil", "幼女戦記"]}


def test_animelists_parse():
    from kodarr.metadata import animelists
    xml = """<anime-list>
      <anime anidbid="17236" tvdbid="371310" defaulttvdbseason="2">
        <mapping-list><mapping anidbseason="0" tvdbseason="0">;1-2;2-0;</mapping></mapping-list>
      </anime>
      <anime anidbid="18104" tvdbid="371310" defaulttvdbseason="2" episodeoffset="12"/>
      <anime anidbid="9453" tvdbid="102261" defaulttvdbseason="0">
        <mapping-list><mapping anidbseason="1" tvdbseason="0" offset="4" start="1" end="4"/></mapping-list>
      </anime>
      <anime anidbid="6327" tvdbid="102261" defaulttvdbseason="1">
        <mapping-list><mapping anidbseason="0" tvdbseason="1">;1-13;2-14;3-15;</mapping></mapping-list>
      </anime>
      <anime anidbid="99" tvdbid="movie"/>
      <anime tvdbid="777"/>
    </anime-list>"""
    rows = {r[0]: r for r in animelists.parse(xml)}
    assert rows[17236][3] == 0 and rows[17236][4] == {"1": 2, "2": 0}
    assert rows[18104][3] == 12 and rows[18104][4] == {}
    # regular episodes living in TVDB S0 (Nekomonogatari pattern)
    assert rows[9453][5] == {"tvdbseason": 0, "offset": 4, "pairs": {}, "start": 1, "end": 4}
    # AniDB specials counted in-season by TVDB (Bakemonogatari web eps)
    assert rows[6327][4] == {"in_season": {"13": 1, "14": 2, "15": 3}}
    assert rows[99][1] == "movie"
    assert len(rows) == 5  # entry without anidbid dropped


def test_anidb_parse_anime():
    from kodarr.metadata import anidb
    xml = """<anime id="17236"><type>TV Series</type><episodecount>12</episodecount>
      <startdate>2023-07-10</startdate><enddate>2023-09-25</enddate>
      <episodes>
        <episode id="267538"><epno type="1">1</epno><length>25</length><airdate>2023-07-10</airdate>
          <title xml:lang="en">The Brokenhearted Mage</title><title xml:lang="x-jat">Shitsui no Majutsushi</title></episode>
        <episode id="268721"><epno type="2">S1</epno><airdate>2023-07-03</airdate>
          <title xml:lang="en">Guardian Fitz</title></episode>
        <episode id="256846"><epno type="4">T1</epno><title xml:lang="en">Teaser PV</title></episode>
      </episodes></anime>"""
    a = anidb.parse_anime(xml)
    assert a["episodecount"] == 12 and a["enddate"] == "2023-09-25"
    by = {e["epno"]: e for e in a["episodes"]}
    assert by["1"]["type"] == 1 and by["1"]["title_en"] == "The Brokenhearted Mage"
    assert by["S1"]["type"] == 2 and by["S1"]["number"] == 1 and by["S1"]["airdate"] == "2023-07-03"
    assert by["T1"]["type"] == 4  # trailers carried but typed, never counted as episodes


def test_aligned_regulars_skips_wrapper():
    from kodarr.metadata.anidb import aligned_regulars
    # Zoku pattern: AniDB numbers the combined broadcast alongside its parts
    rows = [
        {"number": 1, "type": 1, "title_en": "Complete Movie", "length_min": 144},
        {"number": 2, "type": 1, "title_en": "Part 1 of 6", "length_min": 25},
        {"number": 3, "type": 1, "title_en": "Part 2 of 6", "length_min": 25},
        {"number": 1, "type": 2, "title_en": "A Special", "length_min": 25},
        {"number": 4, "type": 1, "title_en": "Part 3 of 6", "length_min": None},
    ]
    a = aligned_regulars(rows)
    assert [a[n]["title_en"] for n in sorted(a)] == ["Part 1 of 6", "Part 2 of 6", "Part 3 of 6"]
    # no wrapper: identity mapping
    plain = [{"number": n, "type": 1, "title_en": f"E{n}", "length_min": 25} for n in (1, 2, 3)]
    assert [aligned_regulars(plain)[n]["title_en"] for n in (1, 2, 3)] == ["E1", "E2", "E3"]


_S = {"format": "TV", "episode_offset": 0, "preferred_group": "SubsPlease"}
SLIME = [  # real entries: split-cour franchise with absolute-numbered releases
    {**_S, "anilist_id": 101280, "title": "That Time I Got Reincarnated as a Slime", "episodes": 24, "aired": 24, "synonyms": ["Tensei Shitara Slime Datta Ken", "TenSura"]},
    {**_S, "anilist_id": 108511, "title": "That Time I Got Reincarnated as a Slime Season 2", "episodes": 12, "aired": 12, "synonyms": ["Tensei Shitara Slime Datta Ken 2nd Season", "TenSura 2"]},
    {**_S, "anilist_id": 156822, "title": "That Time I Got Reincarnated as a Slime Season 3", "episodes": 24, "aired": 24, "synonyms": ["Tensei Shitara Slime Datta Ken 3rd Season", "Tensura 3"]},
    {**_S, "anilist_id": 182205, "title": "That Time I Got Reincarnated as a Slime Season 4", "episodes": None, "aired": 13, "synonyms": ["Tensei Shitara Slime Datta Ken 4th Season", "Tensura 4"]},
]


def test_slime_real_release_forms():
    """Real indexer titles, one per release-group naming convention; every
    form must land on the season-4 entry, never season 1."""
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
    # a 12+12 split season: pack files number 13-24 continuously; the second
    # entry carries episode_offset=12 so overflow lands there as 1-12
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
    from kodarr.acquire.backfill import rank

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
    """Cours can chain through an OVA: the walk must hop it, and the OVA
    itself files under Season 00."""
    import asyncio
    import json

    import httpx

    from kodarr.metadata import anilist

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

    from kodarr.metadata import nfo

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
        from kodarr.library import organize
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
    from kodarr.metadata.anilist import _episode_titles

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
    from kodarr.metadata.nfo import season_title

    slime = "That Time I Got Reincarnated as a Slime"
    assert season_title(slime, slime, 1) == "Season 1"
    assert season_title(f"{slime} Season 2 Part 2", slime, 3) == "Season 2 Part 2"
    assert season_title(f"{slime}: Visions of Coleus", slime, 0) == "Visions of Coleus"
    assert season_title("Nisemonogatari", "Monogatari Series", 2) == "Nisemonogatari"
    assert season_title("Mushoku Tensei: Jobless Reincarnation Cour 2", "Mushoku Tensei: Jobless Reincarnation", 2) == "Cour 2"


def test_mushoku_short_title_matching():
    """Release groups truncate titles at the colon; the full catalog title
    never appears in release names."""
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
    """Requesting any entry must enumerate the whole chain: TV cours, OVA
    hops, and movies."""
    import asyncio
    import json

    import httpx

    from kodarr.metadata import anilist

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
    """A movie search must not match a same-titled TV batch (both parse
    with episode=None)."""
    from kodarr.acquire.backfill import rank

    movie = {"anilist_id": 139498, "title": "That Time I Got Reincarnated as a Slime the Movie: Scarlet Bond",
             "format": "MOVIE", "episodes": 1, "aired": 1, "episode_offset": 0,
             "preferred_group": "SubsPlease", "synonyms": ["Tensei Shitara Slime Datta Ken Movie: Guren no Kizuna-hen"]}
    results = [
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken (01-48) (1080p) [Batch]", "seeders": 500, "url": "u1"},
    ]
    assert rank(results, movie, None) == []


def test_pick_best_falls_back_to_public_alt():
    """When every 'best' is private-tracker-only, the entry's public alt
    must be picked instead of nothing."""
    from types import SimpleNamespace

    from kodarr.acquire.seadex import pick_best

    def rec(best, public, infohash, group):
        tracker = SimpleNamespace(is_public=lambda p=public: p)
        return SimpleNamespace(is_best=best, tracker=tracker, infohash=infohash, release_group=group)

    ab_best = rec(True, False, None, "-ZR-")
    ab_alt = rec(False, False, None, "Metal")
    nyaa_alt = rec(False, True, "abc123", "Metal")
    assert pick_best((ab_best, ab_alt, nyaa_alt)).release_group == "Metal"
    # public best still wins over public alt
    nyaa_best = rec(True, True, "def456", "PMR")
    assert pick_best((nyaa_alt, nyaa_best)).release_group == "PMR"
    assert pick_best((ab_best, ab_alt)) is None


def test_movie_search_query_has_no_episode_number():
    # movies store as "episode 1" but releases carry no number: "Suzume 01"
    # finds nothing on nyaa; the bare title must be searched.
    from kodarr.acquire.backfill import search_query
    assert search_query("Suzume no Tojimari", 1, "MOVIE", 0) == "Suzume no Tojimari"
    # series still get the zero-padded episode
    assert search_query("Sousou no Frieren", 5, "TV", 0) == "Sousou no Frieren 05"
    assert search_query("Sousou no Frieren", 2, "TV", 28) == "Sousou no Frieren 30"


# --- bundled OVAs/specials: must not collide with their parent show ----------

ERIS = {  # real AniList 141534: the OVA bundled with Mushoku Tensei cour 2
    "anilist_id": 141534,
    "title": "Mushoku Tensei: Jobless Reincarnation Cour 2 - Eris the Goblin Slayer",
    "year": 2022,
    "format": "SPECIAL",
    "episodes": 1,
    "aired": 1,
    "synonyms": [
        "Mushoku Tensei: Isekai Ittara Honki Dasu Part 2 - Eris no Goblin Toubatsu",
        "Mushoku Tensei: Jobless Reincarnation Cour 2 - Eris the Goblin Slayer",
        # Load-bearing: normalize() strips the kana/kanji and leaves the bare
        # "2", which _entry_season reads as season 2 — that is what let a
        # "Mushoku Tensei S2" release clear the cour gate for this entry.
        "無職転生 ～異世界行ったら本気だす～ 第2クール エリスのゴブリン討伐",
        "Mushoku Tensei: Jobless Reincarnation Cour 2 Special",
    ],
    "episode_offset": 0,
    "root_path": "/data/media/anime",
    "preferred_group": "SubsPlease",
    "show_title": "Mushoku Tensei: Jobless Reincarnation",
}


def test_special_does_not_match_parent_episode():
    """Regression: the special's title truncated at the colon is just
    "Mushoku Tensei", and it has exactly one episode — so the parent show's
    S2E01 matched it and got grabbed as the OVA. The pre-colon shortcut must
    not apply to SPECIAL/OVA entries."""
    p = match.parse("[SubsPlease] Mushoku Tensei S2 - 01 (1080p) [EC64C8B1].mkv")
    assert p and p.episode == 1 and p.season == 2
    assert match.match(p, [ERIS]) is None


def test_special_still_matches_its_own_release():
    p = match.parse(
        "[SubsPlease] Mushoku Tensei Isekai Ittara Honki Dasu Part 2 - Eris no Goblin Toubatsu (1080p) [A1B2C3D4].mkv"
    )
    assert p
    m = match.match(p, [ERIS])
    assert m and m[0]["anilist_id"] == 141534


def test_special_search_uses_distinguishing_suffix():
    """The bare franchise name only finds the parent show; the suffix is the
    only part that identifies the special."""
    from kodarr.acquire.backfill import search_query, search_titles

    titles = search_titles(ERIS)
    assert "mushoku tensei" not in titles, titles
    assert any("eris no goblin toubatsu" in t for t in titles), titles
    # one-episode specials are named like movies — no episode number appended
    assert search_query(titles[0], 1, "SPECIAL", 0) == titles[0]


def test_special_franchise_root_follows_parent_edge():
    """A bundled OVA has no PREQUEL — only PARENT. Without that hop it becomes
    its own franchise root and Jellyfin shows a duplicate series."""
    import asyncio
    import json

    import httpx

    from kodarr.metadata import anilist

    def rel(type_, id_, fmt):
        return {"relationType": type_, "node": {"id": id_, "format": fmt, "title": {"romaji": f"n{id_}", "english": None}, "startDate": {"year": 2021}}}

    fmts = {108465: "TV", 127720: "TV", 141534: "SPECIAL"}
    graph = {
        108465: [],
        127720: [rel("PREQUEL", 108465, "TV")],
        141534: [rel("PARENT", 127720, "TV")],  # no PREQUEL edge at all
    }

    def handler(request: httpx.Request) -> httpx.Response:
        aid = json.loads(request.read())["variables"]["id"]
        media = {"id": aid, "format": fmts[aid], "status": "FINISHED", "episodes": 12,
                 "startDate": {"year": 2021}, "title": {"romaji": f"n{aid}", "english": None, "native": None},
                 "synonyms": [], "nextAiringEpisode": None,
                 "relations": {"edges": graph[aid]}}
        return httpx.Response(200, json={"data": {"Media": media}})

    async def main():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        special = await anilist.by_id(http, 141534)
        fr = await anilist.franchise(http, special)
        assert fr["show_key"] == 108465 and fr["season"] == 0, fr

    asyncio.run(main())


def test_special_anchor_words():
    # shared by the English and romaji names, absent from the franchise name
    assert match.special_anchors(ERIS) == {"eris", "goblin"}


def test_special_matches_real_mixed_language_release():
    """Real nyaa naming: romaji franchise + English special name. It equals no
    single AniList synonym, so only the anchor words can identify it."""
    for name in [
        "[Erai-raws] Mushoku Tensei - Isekai Ittara Honki Dasu Part 2 - Eris the Goblin Slayer [1080p].mkv",
        "[Lia] Mushoku Tensei - S00E01 - Eris The Goblin Slayer [WEB-DL 1080p AAC].mkv",
    ]:
        p = match.parse(name)
        assert p, name
        m = match.match(p, [ERIS])
        assert m and m[0]["anilist_id"] == 141534, name


def test_special_anchors_still_reject_parent_releases():
    for name in [
        "[SubsPlease] Mushoku Tensei S2 - 01 (1080p) [EC64C8B1].mkv",
        "[SubsPlease] Mushoku Tensei - 12 (1080p) [ABCD1234].mkv",
    ]:
        p = match.parse(name)
        assert p and match.match(p, [ERIS]) is None, name


def test_special_search_leads_with_franchise_plus_anchors():
    """Nyaa is a substring search: the full AniList title finds nothing, but
    "mushoku tensei eris goblin" finds the real releases."""
    from kodarr.acquire.backfill import search_titles

    assert search_titles(ERIS)[0] == "mushoku tensei eris goblin"


def test_special_rank_accepts_non_preferred_group():
    """SubsPlease never releases a disc-only special; hard-filtering the group
    means it could never be found. Preference, not a gate — and the preferred
    group still sorts first."""
    from kodarr.acquire.backfill import rank

    erai = {"title": "[Erai-raws] Mushoku Tensei - Isekai Ittara Honki Dasu Part 2 - Eris the Goblin Slayer [1080p].mkv", "seeders": 5}
    ranked = rank([erai], ERIS, 1)
    assert [r[1] for r in ranked] == [erai]
    # a parent-show release is still rejected no matter how well seeded
    parent = {"title": "[SubsPlease] Mushoku Tensei S2 - 01 (1080p) [EC64C8B1].mkv", "seeders": 999}
    assert rank([parent], ERIS, 1) == []
