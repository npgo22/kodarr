"""Postgres access: async pool + idempotent schema apply."""

from __future__ import annotations

from importlib.resources import files

from psycopg import AsyncConnection
from psycopg.rows import dict_row


async def connect(dsn: str) -> AsyncConnection:
    # ponytail: single connection, not a pool — one daemon, low query rate.
    # Swap to psycopg_pool.AsyncConnectionPool if contention ever shows up.
    conn = await AsyncConnection.connect(dsn, autocommit=True, row_factory=dict_row)
    schema = files("kodarr").joinpath("schema.sql").read_text()
    await conn.execute(schema)
    return conn


async def add_series(conn: AsyncConnection, media: dict, root_path: str) -> None:
    # franchise fields are set at add time; metadata refreshes omit them and
    # must not clobber (COALESCE keeps the stored value)
    params = {"show_key": None, "show_title": None, "show_year": None, "season": None,
              **media, "root_path": root_path}
    params.pop("relations", None)
    await conn.execute(
        """INSERT INTO series (anilist_id, title, year, format, episodes, aired, status, synonyms, root_path,
                               show_key, show_title, show_year, season)
           VALUES (%(anilist_id)s, %(title)s, %(year)s, %(format)s, %(episodes)s, %(aired)s, %(status)s, %(synonyms)s, %(root_path)s,
                   %(show_key)s, %(show_title)s, %(show_year)s, %(season)s)
           ON CONFLICT (anilist_id) DO UPDATE SET
             title = EXCLUDED.title, year = EXCLUDED.year, episodes = EXCLUDED.episodes,
             aired = EXCLUDED.aired, status = EXCLUDED.status, synonyms = EXCLUDED.synonyms,
             show_key = COALESCE(EXCLUDED.show_key, series.show_key),
             show_title = COALESCE(EXCLUDED.show_title, series.show_title),
             show_year = COALESCE(EXCLUDED.show_year, series.show_year),
             season = COALESCE(EXCLUDED.season, series.season)""",
        params,
    )


async def monitored_series(conn: AsyncConnection) -> list[dict]:
    cur = await conn.execute("SELECT * FROM series WHERE monitored")
    return await cur.fetchall()


async def get_series(conn: AsyncConnection, anilist_id: int) -> dict | None:
    cur = await conn.execute("SELECT * FROM series WHERE anilist_id = %s", (anilist_id,))
    return await cur.fetchone()


async def get_episode(conn: AsyncConnection, anilist_id: int, absolute_number: int) -> dict | None:
    cur = await conn.execute(
        "SELECT * FROM episodes WHERE anilist_id = %s AND absolute_number = %s",
        (anilist_id, absolute_number),
    )
    return await cur.fetchone()


async def upsert_episode(
    conn: AsyncConnection, anilist_id: int, absolute_number: int,
    file_path: str, release_group: str | None, from_seadex: bool,
    source_name: str | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO episodes (anilist_id, absolute_number, file_path, release_group, from_seadex, source_name, imported_at)
           VALUES (%s, %s, %s, %s, %s, %s, now())
           ON CONFLICT (anilist_id, absolute_number) DO UPDATE SET
             file_path = EXCLUDED.file_path, release_group = EXCLUDED.release_group,
             from_seadex = EXCLUDED.from_seadex,
             source_name = COALESCE(EXCLUDED.source_name, episodes.source_name),
             imported_at = now()""",
        (anilist_id, absolute_number, file_path, release_group, from_seadex, source_name),
    )


async def active_grab(conn: AsyncConnection, anilist_id: int, absolute_number: int | None) -> dict | None:
    cur = await conn.execute(
        """SELECT * FROM grabs WHERE anilist_id = %s
           AND (absolute_number = %s OR (absolute_number IS NULL AND %s::int IS NULL))
           AND status IN ('queued', 'downloading', 'completed')""",
        (anilist_id, absolute_number, absolute_number),
    )
    return await cur.fetchone()


async def insert_grab(
    conn: AsyncConnection, anilist_id: int, absolute_number: int | None,
    source: str, client: str, client_id: str | None, release_name: str,
) -> None:
    await conn.execute(
        """INSERT INTO grabs (anilist_id, absolute_number, source, client, client_id, release_name)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (anilist_id, absolute_number, source, client, client_id, release_name),
    )


async def grabs_in_flight(conn: AsyncConnection) -> list[dict]:
    cur = await conn.execute("SELECT * FROM grabs WHERE status IN ('queued', 'downloading')")
    return await cur.fetchall()


async def set_grab_status(conn: AsyncConnection, grab_id: int, status: str) -> None:
    await conn.execute("UPDATE grabs SET status = %s, updated_at = now() WHERE id = %s", (status, grab_id))


async def get_id_map(conn: AsyncConnection, anilist_id: int) -> dict | None:
    cur = await conn.execute("SELECT * FROM id_map WHERE anilist_id = %s", (anilist_id,))
    return await cur.fetchone()


async def failed_release_names(conn: AsyncConnection, anilist_id: int) -> set[str]:
    """Blocklist: releases that failed before are never grabbed again."""
    cur = await conn.execute(
        "SELECT release_name FROM grabs WHERE anilist_id = %s AND status = 'failed'", (anilist_id,)
    )
    return {r["release_name"] for r in await cur.fetchall()}


async def expire_stale_grabs(conn: AsyncConnection, days: int = 3) -> list[dict]:
    """Fail in-flight grabs that never completed so they stop blocking retries."""
    cur = await conn.execute(
        """UPDATE grabs SET status = 'failed', updated_at = now()
           WHERE status IN ('queued', 'downloading') AND created_at < now() - make_interval(days => %s)
           RETURNING release_name""",
        (days,),
    )
    return await cur.fetchall()


async def searched_recently(conn: AsyncConnection, anilist_id: int, days: int = 7) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM series WHERE anilist_id = %s AND last_search > now() - make_interval(days => %s)",
        (anilist_id, days),
    )
    return await cur.fetchone() is not None


async def mark_searched(conn: AsyncConnection, anilist_id: int) -> None:
    await conn.execute("UPDATE series SET last_search = now() WHERE anilist_id = %s", (anilist_id,))
