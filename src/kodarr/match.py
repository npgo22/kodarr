"""Release-name parsing (anitopy) and matching against the library."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import anitopy

VIDEO_EXTS = {".mkv", ".mp4", ".avi"}


def normalize(title: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for title comparison."""
    title = title.lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return " ".join(title.split())


@dataclass
class ParsedRelease:
    title: str
    group: str | None
    episode: int | None  # None for movies / batches


def parse(release_name: str) -> ParsedRelease | None:
    parsed = anitopy.parse(release_name)
    if not parsed or "anime_title" not in parsed:
        return None
    ep = parsed.get("episode_number")
    if isinstance(ep, list):  # batch ranges like ['01','12'] — not a single episode
        ep = None
    return ParsedRelease(
        title=parsed["anime_title"],
        group=parsed.get("release_group"),
        episode=int(ep) if ep is not None else None,
    )


def match(parsed: ParsedRelease, series_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], int | None] | None:
    """Match a parsed release against series rows (needs title, synonyms,
    episode_offset keys). Returns (series, anilist_episode) or None.

    anilist_episode = release absolute number - episode_offset, so continuing
    cours numbered absolutely by release groups map back to the AniList entry.
    """
    want = normalize(parsed.title)
    for row in series_rows:
        names = {normalize(n) for n in [row["title"], *row["synonyms"]]}
        if want in names:
            ep = None
            if parsed.episode is not None:
                ep = parsed.episode - row["episode_offset"]
                total = row.get("episodes")
                if ep < 1 or (total and ep > total):
                    continue  # right title, wrong entry (e.g. sequel cour)
            return row, ep
    return None
