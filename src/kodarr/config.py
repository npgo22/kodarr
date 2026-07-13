"""Config: TOML file + env-var overrides for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    db_dsn: str
    anime_root: str
    movie_root: str
    downloads_dir: str
    jellyfin_url: str
    jellyfin_api_key: str
    jellyfin_path_from: str  # rewrite library paths for jellyfin's mount, e.g. /data -> /media
    jellyfin_path_to: str
    nyaa_url: str
    qbit_url: str
    qbit_user: str
    qbit_pass: str
    qbit_category: str
    tmdb_api_key: str = ""
    anidb_cache: str = "/data/media/.kodarr-cache"  # Shoko Anime_HTTP.zip lives here (media PVC)
    anidb_client: str = ""  # registered AniDB HTTP-API client name; empty = cache-only
    rss_feeds: list[str] = field(default_factory=list)
    rss_interval: int = 600
    preferred_groups: list[str] = field(default_factory=lambda: ["SubsPlease"])
    webhook_port: int = 8096
    webhook_token: str = ""
    dry_run: bool = False


def _env(name: str, file_value: str) -> str:
    return os.environ.get(f"KODARR_{name}", file_value)


def load(path: str | Path = "config.toml") -> Config:
    data: dict = {}
    p = Path(path)
    if p.exists():
        data = tomllib.loads(p.read_text())

    def get(section: str, key: str, default: object = ""):
        return data.get(section, {}).get(key, default)

    return Config(
        db_dsn=_env("DB_DSN", get("db", "dsn")),
        anime_root=get("paths", "anime_root", "/data/media/anime"),
        movie_root=get("paths", "movie_root", "/data/media/anime-movies"),
        downloads_dir=get("paths", "downloads", "/data/downloads"),
        jellyfin_url=get("jellyfin", "url"),
        jellyfin_api_key=_env("JELLYFIN_API_KEY", get("jellyfin", "api_key")),
        jellyfin_path_from=get("jellyfin", "path_from"),
        jellyfin_path_to=get("jellyfin", "path_to"),
        nyaa_url=get("nyaa", "url", "https://nyaa.si"),
        qbit_url=get("qbittorrent", "url"),
        qbit_user=_env("QBIT_USER", get("qbittorrent", "user")),
        qbit_pass=_env("QBIT_PASS", get("qbittorrent", "pass")),
        qbit_category=get("qbittorrent", "category", "kodarr"),
        tmdb_api_key=_env("TMDB_API_KEY", get("tmdb", "api_key")),
        anidb_cache=get("anidb", "cache_dir", "/data/media/.kodarr-cache"),
        anidb_client=get("anidb", "client", ""),
        rss_feeds=get("rss", "feeds", ["https://subsplease.org/rss/?r=1080"]),
        rss_interval=int(get("rss", "interval", 600)),
        preferred_groups=get("groups", "preferred", ["SubsPlease"]),
        webhook_port=int(get("webhook", "port", 7878)),
        webhook_token=_env("WEBHOOK_TOKEN", get("webhook", "token")),
    )
