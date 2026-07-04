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


def test_rank_preferred_group_only_resolution_then_usenet():
    from kodarr.search import rank

    results = [
        {"title": "That Time I Got Reincarnated as a Slime S04E06 1080p CR WEB-DL MULTi AAC2.0 H 264-VARYG (Tensei Shitara Slime Datta Ken 4th Season, Multi-Subs)", "protocol": "torrent", "url": "u1"},
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (720p) [BBBB].mkv", "protocol": "usenet", "url": "u2"},
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (1080p) [AAAA].mkv", "protocol": "torrent", "url": "u3"},
        {"title": "[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (1080p) [AAAA].mkv", "protocol": "usenet", "url": "u4"},
    ]
    ranked = rank(results, SLIME[3], 6)
    # non-preferred group excluded; sub-1080p excluded outright; usenet wins within 1080p
    assert [r["url"] for _, r in ranked] == ["u4", "u3"]
    # blocklisted releases drop out entirely — and nothing below the floor backfills
    ranked = rank(results, SLIME[3], 6, {"[SubsPlease] Tensei Shitara Slime Datta Ken S4 - 06 (1080p) [AAAA].mkv"})
    assert ranked == []


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
    d = organize.dest_path(FRIEREN, 5, "SubsPlease", ".mkv")
    assert d == Path(
        "/data/media/anime/Frieren Beyond Journey's End (2023) [anilist-154587]/"
        "Frieren Beyond Journey's End - 005 [SubsPlease].mkv"
    )


def test_dest_path_movie():
    d = organize.dest_path(MOVIE, None, "SubsPlease", ".mkv")
    assert d == Path("/data/media/anime-movies/Suzume (2022) [anilist-142770]/Suzume (2022) [SubsPlease].mkv")


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
