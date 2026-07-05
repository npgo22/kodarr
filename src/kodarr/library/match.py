"""Release-name parsing (anitopy) and matching against the library."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import anitopy

VIDEO_EXTS = {".mkv", ".mp4", ".avi"}

# "2nd Season", "Season 3", "3rd season" — how AniList encodes cours in a title.
_SEASON_RE = re.compile(r"\b(\d+)\s*(?:st|nd|rd|th)?\s+season\b|\bseason\s+(\d+)\b")


def normalize(title: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for title comparison."""
    title = title.lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return " ".join(title.split())


def _strip_season(name: str) -> str:
    """Drop a trailing 'Season N' / 'Nth Season' so sequel titles reduce to the base."""
    return " ".join(_SEASON_RE.sub("", name).split())


def _entry_season(names: set[str]) -> int:
    """Best-guess season number of an AniList entry from its titles/synonyms.

    Sequels carry 'Nth Season'/'Season N' in a title; AniList also lists the bare
    season number ('2','3','4') as a synonym. First cours / single-season shows
    have neither → season 1.
    """
    for n in names:
        m = _SEASON_RE.search(n)
        if m:
            return int(m.group(1) or m.group(2))
    for n in names:
        # cap so shows titled with a bare number don't read as a season
        if n.isdigit() and 1 <= int(n) <= 20:
            return int(n)
    return 1


@dataclass
class ParsedRelease:
    title: str
    group: str | None
    episode: int | None  # None for movies / batches
    season: int | None = None  # release-named cour ("S4", "4th Season"); None if absent
    resolution: int | None = None  # vertical pixels (1080, 720, ...); None if unnamed


def _collapse(value) -> int | None:
    """anitopy returns a list when a number appears twice in a name. The same
    value repeated collapses to that value; different values (a range) don't."""
    if isinstance(value, list):
        ints = {int(v) for v in value if str(v).isdigit()}
        return ints.pop() if len(ints) == 1 else None
    return int(value) if value is not None and str(value).isdigit() else None


def _resolution(value) -> int | None:
    """anitopy video_resolution: '1080p', '1920x1080', or a list of those."""
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    m = re.search(r"(\d+)p?$", str(value).lower())
    return int(m.group(1)) if m else None


_BATCH_RE = re.compile(r"\(\s*\d{1,3}\s*[-~]\s*\d{1,3}\s*\)|\bbatch\b", re.IGNORECASE)


def is_batch(release_name: str) -> bool:
    """Multi-episode batch: '(01-48)' ranges or an explicit Batch tag."""
    return bool(_BATCH_RE.search(release_name))


def parse(release_name: str) -> ParsedRelease | None:
    parsed = anitopy.parse(release_name)
    if not parsed or "anime_title" not in parsed:
        return None
    # drop embedded alt-titles: "Title (English Title)" -> "Title"
    title = " ".join(re.sub(r"\([^)]*\)", " ", parsed["anime_title"]).split())
    return ParsedRelease(
        title=title or parsed["anime_title"],
        group=parsed.get("release_group"),
        episode=_collapse(parsed.get("episode_number")),
        season=_collapse(parsed.get("anime_season")),
        resolution=_resolution(parsed.get("video_resolution")),
    )


def match(parsed: ParsedRelease, series_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], int | None] | None:
    """Match a parsed release against series rows (needs title, synonyms,
    episode_offset keys). Returns (series, anilist_episode) or None.

    anitopy strips the cour marker into ``season``; AniList keeps it in the
    title. Titles are therefore compared season-stripped, and a season-tagged
    release only matches the entry for that season. Season-less releases route
    by ``episode_offset`` (absolute numbering across cours).
    """
    want = normalize(parsed.title)
    wants = {want}
    if parsed.season is not None and want.endswith(f" {parsed.season}"):
        # a trailing digit in the title sometimes restates the season
        wants.add(want.removesuffix(f" {parsed.season}").strip())
    for row in series_rows:
        names = {normalize(n) for n in [row["title"], *row["synonyms"]]}
        # release groups truncate titles at the colon; accept pre-colon forms
        short = {normalize(n.split(":")[0]) for n in [row["title"], *row["synonyms"]] if ":" in n}
        base_names = names | short | {_strip_season(n) for n in names | short}
        if not (wants & base_names):
            continue
        entry_season = _entry_season(names)
        if parsed.season is not None and parsed.season != entry_season:
            continue  # release names a different cour than this entry
        if parsed.season is None and entry_season > 1 and row["episode_offset"] == 0:
            # unseasoned release + later-season entry with no absolute-numbering
            # offset: this is a season-1-era file, not ours
            continue
        ep = None
        if parsed.episode is not None:
            # while airing, episodes is NULL on AniList — cap at aired+1
            total = row.get("episodes") or (row.get("aired") or 0) + 1
            if parsed.season is not None:
                # season-tagged releases number per cour, except split-cour
                # packs that number the whole season continuously
                candidates = [parsed.episode, parsed.episode - row["episode_offset"]]
            else:
                candidates = [parsed.episode - row["episode_offset"]]
            ep = next((c for c in candidates if 1 <= c <= total), None)
            if ep is None:
                continue  # right title, wrong entry (e.g. sequel cour)
        return row, ep
    return None
