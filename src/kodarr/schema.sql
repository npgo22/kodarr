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
    client          text NOT NULL,              -- qbittorrent | sabnzbd
    client_id       text,                       -- infohash or SAB nzo_id
    release_name    text NOT NULL,
    status          text NOT NULL DEFAULT 'queued', -- queued|downloading|completed|imported|failed
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS grabs_active_idx ON grabs (status) WHERE status IN ('queued', 'downloading', 'completed');
