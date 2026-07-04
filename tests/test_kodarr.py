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
