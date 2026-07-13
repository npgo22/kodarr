CREATE TABLE IF NOT EXISTS series (
    anilist_id      integer PRIMARY KEY,
    title           text NOT NULL,
    year            integer,
    format          text NOT NULL,              -- 'TV' | 'MOVIE' (anything AniList returns)
    episodes        integer,                    -- NULL while airing count unknown
    aired           integer NOT NULL DEFAULT 0, -- episodes aired so far (= episodes when FINISHED)
    status          text,                       -- RELEASING | FINISHED | ...
    synonyms        text[] NOT NULL DEFAULT '{}',
    root_path       text NOT NULL,
    episode_offset  integer NOT NULL DEFAULT 0, -- abs number in releases = anilist ep + offset
    preferred_group text NOT NULL DEFAULT 'SubsPlease',
    monitored       boolean NOT NULL DEFAULT true,
    last_search     timestamptz,                -- backfill backoff; cleared when new eps air
    added_at        timestamptz NOT NULL DEFAULT now()
);

-- franchise grouping (AniList prequel-chain walk): one Jellyfin show folder
-- per show_key, one "Season NN" folder per entry
ALTER TABLE series ADD COLUMN IF NOT EXISTS show_key    integer;
ALTER TABLE series ADD COLUMN IF NOT EXISTS show_title  text;
ALTER TABLE series ADD COLUMN IF NOT EXISTS show_year   integer;
ALTER TABLE series ADD COLUMN IF NOT EXISTS season      integer;

CREATE TABLE IF NOT EXISTS episodes (
    anilist_id      integer NOT NULL REFERENCES series ON DELETE CASCADE,
    absolute_number integer NOT NULL,           -- movies use 1
    title           text,
    air_date        date,
    file_path       text,
    release_group   text,
    from_seadex     boolean NOT NULL DEFAULT false,
    imported_at     timestamptz,
    PRIMARY KEY (anilist_id, absolute_number)
);

CREATE TABLE IF NOT EXISTS grabs (
    id              serial PRIMARY KEY,
    anilist_id      integer NOT NULL REFERENCES series ON DELETE CASCADE,
    absolute_number integer,                    -- NULL = batch / movie / seadex pack
    source          text NOT NULL,              -- rss | autobrr | seadex | search
    client          text NOT NULL,              -- always 'qbittorrent' (usenet support removed)
    client_id       text,                       -- torrent infohash when known
    release_name    text NOT NULL,
    status          text NOT NULL DEFAULT 'queued', -- queued|downloading|completed|imported|failed
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS grabs_active_idx ON grabs (status) WHERE status IN ('queued', 'downloading', 'completed');

-- TVDB/TMDB -> AniList mapping (Fribb/anime-lists), used by the Jellyseerr webhook
CREATE TABLE IF NOT EXISTS id_map (
    anilist_id      integer PRIMARY KEY,
    tvdb_id         integer,
    tmdb_movie_id   integer,
    tvdb_season     integer,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS id_map_tvdb_idx ON id_map (tvdb_id);
ALTER TABLE id_map ADD COLUMN IF NOT EXISTS tmdb_tv_id  integer;
ALTER TABLE id_map ADD COLUMN IF NOT EXISTS tmdb_season integer;
ALTER TABLE id_map ADD COLUMN IF NOT EXISTS anidb_id    integer;  -- keys the AniDB title-synonyms feed
CREATE INDEX IF NOT EXISTS id_map_tmdb_idx ON id_map (tmdb_movie_id);

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS source_name text;  -- original release filename (what was downloaded)

-- manual id_map corrections (e.g. TMDB's Monogatari seasons don't line up with
-- AniList); re-applied after every Fribb refresh so they survive the rewrite
CREATE TABLE IF NOT EXISTS id_map_overrides (
    anilist_id  integer PRIMARY KEY,
    tmdb_season integer                        -- NULL = disable TMDB episode enrichment
);
-- entries whose Fribb row is missing entirely still need a tv id to enrich from
ALTER TABLE id_map_overrides ADD COLUMN IF NOT EXISTS tmdb_tv_id integer;
-- absolute episode offset into the TMDB season; overrides the sibling-cour SUM
-- (TMDB "Specials" seasons interleave recaps, arithmetic placement can't land)
ALTER TABLE id_map_overrides ADD COLUMN IF NOT EXISTS tmdb_offset integer;

-- extra title aliases from the AniDB anime-titles dump (every language +
-- official + synonym AniList doesn't list), keyed to AniList via id_map.anidb_id.
-- Merged into series.synonyms at read time so release matching accepts alternate
-- namings. Refreshed weekly after the Fribb id map.
CREATE TABLE IF NOT EXISTS title_synonyms (
    anilist_id integer PRIMARY KEY,
    synonyms   text[] NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT now()
);
DROP TABLE IF EXISTS manami_synonyms;  -- superseded (manami-project archived 2026-07)

-- AniList response cache: FINISHED media is immutable (30d TTL), airing 6h.
-- This is what keeps franchise walks + nfo passes from hammering the API.
CREATE TABLE IF NOT EXISTS anilist_cache (
    anilist_id  integer PRIMARY KEY,
    payload     jsonb NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

-- === AniDB episode identity (the mapping spine) ===
-- Per-episode ground truth from AniDB (via Shoko's Anime_HTTP cache zip, live
-- HTTP API fallback). Regular/special typing, real titles and airdates. series
-- rows with an anidb_id but anidb_mapped IS NULL form the background mapping
-- queue: they work with AniList-guessed numbers until the anidb pass resolves
-- them, then everything (episode counts, offsets, specials) is derived data.
ALTER TABLE series ADD COLUMN IF NOT EXISTS anidb_mapped timestamptz;

CREATE TABLE IF NOT EXISTS anidb_episodes (
    anidb_id    integer NOT NULL,   -- AniDB anime id
    epno        text    NOT NULL,   -- '1', 'S1' (special), 'T1' (trailer), 'C1' (credits)...
    type        integer NOT NULL,   -- 1 regular, 2 special, 3 credits, 4 trailer, 5 parody, 6 other
    number      integer NOT NULL,   -- numeric part of epno
    title_en    text,
    title_romaji text,
    airdate     date,
    length_min  integer,
    PRIMARY KEY (anidb_id, epno)
);

-- Cross-source identity from Anime-Lists/anime-lists (the Shoko/Sonarr-anime
-- community map): AniDB anime -> TVDB/TMDB season + episode offset + where its
-- specials land in TVDB season 0. episode_offset here replaces every manual
-- series.episode_offset: tvdb/release episode N = anidb episode N - offset.
CREATE TABLE IF NOT EXISTS anidb_map (
    anidb_id            integer PRIMARY KEY,
    tvdb_id             text,               -- numeric, or 'movie'/'OVA'/'unknown'
    default_tvdb_season text,
    episode_offset      integer NOT NULL DEFAULT 0,
    special_map         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- anidb special num -> tvdb S0 episode num
    updated_at          timestamptz NOT NULL DEFAULT now()
);
-- where an entry's REGULAR episodes live when they don't follow
-- defaulttvdbseason + episodeoffset (e.g. Nekomonogatari -> TVDB S0 E5-8)
ALTER TABLE anidb_map ADD COLUMN IF NOT EXISTS season_map jsonb NOT NULL DEFAULT '{}'::jsonb;
-- Fribb id-map gaps (e.g. Zoku Owarimonogatari lacks anidb_id): manual, durable
ALTER TABLE id_map_overrides ADD COLUMN IF NOT EXISTS anidb_id integer;
