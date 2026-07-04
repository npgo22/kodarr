# kodarr

Anime-only Sonarr replacement: one headless daemon, a CLI, and Postgres. No web
UI, no quality profiles, no season mapping — anime is identified by AniList ID,
release group, and absolute episode number, so that's all kodarr models.

## Features

- **AniList** as the only metadata source; TV and movies, one library entry per AniList ID
- Add anime via CLI or **Seerr requests** (webhook; TVDB/TMDB → AniList via Fribb/anime-lists,
  one request adds every season's AniList entry and processes it immediately)
- **RSS** (SubsPlease) + **autobrr** (webhook) for airing episodes, preferred release group only
- **Prowlarr** backfill for missing episodes — usenet preferred, except preferred-group torrents
- **SeaDex** sweep auto-upgrades finished series to the curated best public release
- **qBittorrent** / **SABnzbd** downloaders; hardlink imports, torrents keep seeding
- Layout: `anime/Title (Year) [anilist-ID]/Title - 001 [Group].mkv` (absolute numbering, per-entry folders)
- **Jellyfin** path refresh after every import
- JSON logs on stdout (VictoriaLogs/Loki-friendly) + Grafana dashboard
- Polite by design: AniList throttling with Retry-After, conditional RSS GETs,
  weekly search backoff, failed-release blocklist, stalled-grab expiry

## Quick start

```sh
cp config.example.toml config.toml    # secrets can come from KODARR_* env vars instead
kodarr add "frieren"                  # search AniList, prints IDs
kodarr add 154587                     # add by ID; --offset N for absolutely-numbered sequel cours
kodarr run --dry-run                  # daemon; dry-run logs grabs without sending them
```

Also: `list`, `remove`, `backfill`, `seadex [--force]`, `import <path> [--seadex]`.

**autobrr**: Webhook action → `POST http://kodarr:7878/webhook/autobrr` with header
`X-Kodarr-Token: <token>` and payload
`{"release_name": "{{ .TorrentName }}", "download_url": "{{ .TorrentUrl }}"}`.

**Seerr**: Settings → Notifications → Webhook: URL
`http://kodarr:7878/webhook/seerr`, Authorization Header = your token,
default JSON payload, enable "Request Approved" + "Request Automatically
Approved". Approved anime requests land in the library and are searched
immediately.

## Deploy

CI publishes `ghcr.io/npgo22/kodarr:latest` (built from `deploy/Dockerfile`).
`deploy/flux/` has example manifests for a Flux + bjw-s app-template cluster:
copy to `kubernetes/apps/downloads/kodarr/`, fill in + sops-encrypt
`secret.sops.yaml`, adjust `config.toml`. State lives entirely in Postgres
(CNPG-style `postgres-init` initContainer included) — no config PVC. The
GrafanaDashboard CR expects a `victoriametrics-logs-datasource` named
`VictoriaLogs`.

## Dev

```sh
uv sync && uv run pytest   # unit + integration (integration needs docker)
```
