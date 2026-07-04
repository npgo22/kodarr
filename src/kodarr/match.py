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
        if n.isdigit():
            return int(n)
    return 1


@dataclass
class ParsedRelease:
    title: str
    group: str | None
    episode: int | None  # None for movies / batches
    season: int | None = None  # release-named cour ("S4", "4th Season"); None if absent


def parse(release_name: str) -> ParsedRelease | None:
    parsed = anitopy.parse(release_name)
    if not parsed or "anime_title" not in parsed:
        return None
    ep = parsed.get("episode_number")
    if isinstance(ep, list):  # batch ranges like ['01','12'] — not a single episode
        ep = None
    season = parsed.get("anime_season")
    if isinstance(season, list):  # multi-season batch — no single cour
        season = None
    return ParsedRelease(
        title=parsed["anime_title"],
        group=parsed.get("release_group"),
        episode=int(ep) if ep is not None else None,
        season=int(season) if season is not None and str(season).isdigit() else None,
    )


def match(parsed: ParsedRelease, series_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], int | None] | None:
    """Match a parsed release against series rows (needs title, synonyms,
    episode_offset keys). Returns (series, anilist_episode) or None.

    anitopy strips the cour into ``season`` and leaves ``title`` as the base name,
    while AniList keeps the season *in* the title ("... 4th Season"). So compare
    against season-stripped synonyms, and when the release names a season, only
    match the AniList entry for that season — otherwise "S4 - 04" collides with
    the season-1 entry (same base title, episode in range).

    A release with no season falls back to ``episode_offset`` routing, so groups
    that number absolutely across cours still map to the right entry.
    """
    want = normalize(parsed.title)
    for row in series_rows:
        names = {normalize(n) for n in [row["title"], *row["synonyms"]]}
        base_names = names | {_strip_season(n) for n in names}
        if want not in base_names:
            continue
        if parsed.season is not None and parsed.season != _entry_season(names):
            continue  # release names a different cour than this entry
        ep = None
        if parsed.episode is not None:
            ep = parsed.episode - row["episode_offset"]
            total = row.get("episodes")
            if ep < 1 or (total and ep > total):
                continue  # right title, wrong entry (e.g. sequel cour)
        return row, ep
    return None
