"""Release-name parsing (anitopy) and matching against the library."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import anitopy

VIDEO_EXTS = {".mkv", ".mp4", ".avi"}

# Bundled OVAs/specials. Two things separate them from a normal cour: they hang
# off their TV entry by PARENT rather than PREQUEL (see anilist.franchise), and
# their identity lives entirely in the title *suffix* — "Mushoku Tensei: ... -
# Eris no Goblin Toubatsu" is NOT "Mushoku Tensei". Truncating them at the colon
# makes them collide with the parent show, so the pre-colon shortcut below is
# withheld from these formats.
SIDE_FORMATS = {"SPECIAL", "OVA"}

# Franchise/format filler — never distinguishing on its own.
_FILLER = {
    "the", "a", "an", "of", "and", "no", "wo", "wa", "ga", "ni", "de", "o",
    "part", "cour", "season", "special", "specials", "ova", "oad", "movie", "tv",
}


def single_file(row: dict[str, Any]) -> bool:
    """One release/file *is* the whole entry: a movie, or a one-episode
    OVA/special. Such releases carry no episode number, so both the search
    ranker and the importer have to treat an unnumbered file as episode 1
    instead of rejecting it."""
    return row["format"] == "MOVIE" or (
        row["format"] in SIDE_FORMATS and (row.get("episodes") or 1) == 1
    )


def special_anchors(row: dict[str, Any]) -> set[str]:
    """Words that identify a special in *both* of its canonical names.

    Release groups mix languages — nyaa carries "Mushoku Tensei - Isekai Ittara
    Honki Dasu Part 2 - Eris the Goblin Slayer": romaji franchise, English
    special name. That string equals no single AniList synonym, so exact-name
    matching can never find it. Intersecting the English title with the romaji
    synonym and dropping the franchise name and filler leaves {eris, goblin} —
    present in every naming of this OVA and in no release of the parent show.

    Only the two canonical names are intersected: AniList also lists degenerate
    synonyms ("...Cour 2 Special", or a native title that normalizes to bare
    "2") which share no distinguishing word and would empty the set.
    """
    romaji = (row.get("synonyms") or [row["title"]])[0]
    anchors = set(normalize(row["title"]).split()) & set(normalize(romaji).split())
    anchors -= set(normalize(row.get("show_title") or "").split())
    return {t for t in anchors if t not in _FILLER and not t.isdigit()}

# "2nd Season", "Season 3", "3rd season" — how AniList encodes cours in a title.
_SEASON_RE = re.compile(r"\b(\d+)\s*(?:st|nd|rd|th)?\s+season\b|\bseason\s+(\d+)\b")

# A trailing cour marker AniList spells differently from release groups: "Youjo
# Senki II" is released as "[SubsPlease] Youjo Senki S2 - NN". Strip it (roman
# II–IV, bare "S2", or "2nd") so a sequel query reduces to the franchise base and
# the group's release-name still matches. Runs on normalize()d (lowercase) input.
_SEASON_SUFFIX_RE = re.compile(r"\s+(?:ii|iii|iv|s\d+|\d+(?:st|nd|rd|th))$")


def normalize(title: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for title comparison."""
    title = title.lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return " ".join(title.split())


def _strip_season(name: str) -> str:
    """Drop a trailing 'Season N' / 'Nth Season' / 'II' / 'S2' so sequel titles
    reduce to the base for search."""
    name = _SEASON_RE.sub("", name)
    name = _SEASON_SUFFIX_RE.sub("", name)
    return " ".join(name.split())


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


def _franchise_offset(row: dict[str, Any]) -> int:
    """Episodes aired before this entry across the whole franchise.

    Groups split into two camps for a long-running show: per-cour numbering
    ("4th Season - 12") and franchise-absolute ("- 78"). SubsPlease uses the
    latter, so for a preferred-group release this is the offset that turns the
    release number back into an AniList episode. Computed in SQL (see
    db._WITH_SYNONYMS); 0 when unknown, which disables the whole path.
    """
    return row.get("franchise_offset") or 0


def _entry_total(row: dict[str, Any]) -> int:
    """Episode count for the entry — while airing AniList leaves episodes NULL,
    so fall back to what has aired (+1 for the one dropping right now)."""
    return row.get("episodes") or (row.get("aired") or 0) + 1


def _lands_in_entry(episode: int | None, row: dict[str, Any], offset: int) -> bool:
    """Does this release number, read as offset-based, fall inside the entry?"""
    if not offset or episode is None:
        return False
    return 1 <= episode - offset <= _entry_total(row)


@dataclass
class ParsedRelease:
    title: str
    group: str | None
    episode: int | None  # None for movies / batches
    season: int | None = None  # release-named cour ("S4", "4th Season"); None if absent
    resolution: int | None = None  # vertical pixels (1080, 720, ...); None if unnamed
    raw: str = ""  # the whole release name; specials are identified from it


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
        raw=release_name,
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
        # release groups truncate titles at the colon; accept pre-colon forms.
        # Never for a special/OVA: "Cour 2 - Eris no Goblin Toubatsu" truncates
        # to the bare franchise name, so the parent's own episode 1 would match
        # the special (both "Mushoku Tensei", both episode 1) and get imported
        # over it.
        short = (
            set()
            if row.get("format") in SIDE_FORMATS
            else {normalize(n.split(":")[0]) for n in [row["title"], *row["synonyms"]] if ":" in n}
        )
        base_names = names | short | {_strip_season(n) for n in names | short}
        if not (wants & base_names):
            # A special is usually named across two languages at once, matching
            # no single synonym exactly; fall back to its anchor words, which
            # no release of the parent show carries. Scanned over the whole
            # release name because groups file specials as "<Show> - S00E01 -
            # <Special Name>", leaving anitopy's anime_title as just the show.
            anchors = special_anchors(row) if row.get("format") in SIDE_FORMATS else set()
            if not (anchors and anchors <= set(normalize(parsed.raw or parsed.title).split())):
                continue
        if row.get("format") not in SIDE_FORMATS:
            # Cour routing. Skipped for specials: they are not a cour, and
            # _entry_season misreads them anyway — a native-language synonym
            # normalizes down to a bare "2" ("...第2クール..."), which would
            # both fake a cour number and reject the special's own (unseasoned)
            # release as "season-1-era". The full-title gate above is what
            # identifies a special.
            entry_season = _entry_season(names)
            if parsed.season is not None and parsed.season != entry_season:
                continue  # release names a different cour than this entry
            if parsed.season is None and entry_season > 1 and row["episode_offset"] == 0:
                # unseasoned release + later-season entry with no absolute-numbering
                # offset: this is a season-1-era file, not ours — unless the
                # number is franchise-absolute. SubsPlease numbers long-running
                # shows straight through ("Re Zero ... - 78" is S4E12), so the
                # preferred group's own releases would otherwise never match.
                if not _lands_in_entry(parsed.episode, row, _franchise_offset(row)):
                    continue
        elif parsed.season:
            # a cour-tagged release is the parent show, never the special —
            # except S00, which *is* the specials season
            continue
        ep = None
        if parsed.episode is not None:
            # while airing, episodes is NULL on AniList — cap at aired+1
            total = _entry_total(row)
            if parsed.season is not None:
                # season-tagged releases number per cour, except split-cour
                # packs that number the whole season continuously
                candidates = [parsed.episode, parsed.episode - row["episode_offset"]]
            else:
                # untagged: either the entry's own (AniDB cour) numbering, or
                # franchise-absolute as SubsPlease writes it. Cour numbering is
                # tried first so a season-1-era file can never be re-read as a
                # later cour's episode.
                candidates = [parsed.episode - row["episode_offset"],
                              parsed.episode - _franchise_offset(row)]
            ep = next((c for c in candidates if 1 <= c <= total), None)
            if ep is None:
                continue  # right title, wrong entry (e.g. sequel cour)
        return row, ep
    return None
