# kodarr handoff — replace sonarr-anime, prove Slime backfill

## Goal (from user)
Deploy `kodarr` (this repo: anime-only Sonarr replacement, in `../arr/kodarr`) into
the homelab-k8s cluster (`../homelab-k8s`) to **replace `sonarr-anime`** (NOT the main
`sonarr`). Then exercise it by re-fetching the **entire latest season of "That Time I
Got Reincarnated as a Slime"** — deliberately chosen because it has many non-SubsPlease
releases (mixed release-group numbering). Fix any kodarr bugs found along the way;
editing the kodarr codebase is explicitly allowed.

## Key facts established
- kodarr **replaces `sonarr-anime`** only. Both manage `/data/media/anime`. The main
  `sonarr` handles non-anime TV at `/data/media/tv` and stays.
- kodarr model: AniList ID + release group + **absolute** episode number. Postgres
  state (CNPG `postgres-init` initContainer, no config PVC). Prowlarr backfill,
  SeaDex sweep, qbit/SABnzbd, Jellyfin refresh. Deploy manifests ready in
  `deploy/flux/` (copy to `kubernetes/apps/downloads/kodarr/`).
- Image `ghcr.io/npgo22/kodarr:latest` builds via `.github/workflows` on push to main.
  Repo pushed to origin/main at commit 5d18ce1 (before my change below).
- homelab env: `KUBECONFIG=<homelab-k8s>/kubeconfig`; kubectl at
  `~/.local/share/mise/installs/kubectl/1.36.2/kubectl`; SOPS-age key at
  `~/.config/sops/age/keys.txt`; Prowlarr current API key `0976a2dfa429b33fb322fd296bacbe2f`.

## Bug I found and (partially) fixed — VERIFY BEFORE TRUSTING
**Root cause the Slime test would expose:** `match.parse()` (anitopy) strips the cour
into `anime_season` and leaves `title` as the bare base name. AniList keeps the season
IN the title ("...4th Season"). So `[SubsPlease] ...Slime Datta Ken S4 - 04` parsed to
`title="Tensei Shitara Slime Datta Ken", episode=4` with **season dropped**, and matched
the **season-1** AniList entry (id 101280, whose romaji normalizes to the bare title) —
grabbing **S4E04 as S1E04**. This is the primary RSS path, not just backfill. Confirmed
empirically (S1=101280, S2=108511, S3=156822, S4=182205; S4 has no bare-title synonym).

**My fix (in `src/kodarr/match.py`, UNCOMMITTED, UNTESTED):** keep anitopy's
`anime_season` on `ParsedRelease`; compare release title against season-stripped
synonyms; when a release names a season, only match the AniList entry whose season
(derived from "Nth Season"/bare-number synonym) equals it. Releases with no season
still fall back to `episode_offset` routing (preserves `test_match_offset_routes_to_sequel_entry`).

## NEXT STEPS for whoever picks this up
1. **Run the tests** — I never did: `cd ../arr/kodarr && uv run pytest -q`. My match.py
   edit needs the existing suite to pass AND new cases for Slime (S1 vs S4 disambiguation,
   Erai-raws "4th Season - 13" absolute-vs-season, the VARYG "S04E03" English form).
   NOTE: `_entry_season` derives season from synonyms heuristically — verify against the
   real AniList entries above; the bare-number-synonym assumption may be brittle.
2. Sanity-check the other numbering forms actually route right: Erai-raws numbers Slime
   S4 **absolutely** ("4th Season - 13" = anitopy season=4 ep=13) — with my season-gate
   that requires the S4 entry's episode range to include 13; confirm episode_offset math
   still lands it. This interaction (season-named AND absolute-numbered) is the risky one.
3. Only after matching is proven: deploy. Copy `deploy/flux/` →
   `kubernetes/apps/downloads/kodarr/`, fill+sops-encrypt `secret.sops.yaml` (DB DSN,
   Prowlarr/Jellyfin/SAB keys, qbit creds, webhook token), add kodarr DB to CNPG
   `postgres` cluster, add the ks to `kubernetes/apps/downloads/kustomization.yaml`.
   Run **alongside** sonarr-anime first (don't delete it yet).
4. `kodarr add 182205` (Slime S4), then `kodarr backfill 182205 --dry-run` to watch the
   grab decisions on real Prowlarr results BEFORE sending anything. Only cut over
   sonarr-anime once the dry-run picks correct releases.

## What I changed (uncommitted)
- `src/kodarr/match.py` — season-aware matching (see above). Not tested, not committed.
- `HANDOFF.md` — this file.

The Pyright "anitopy could not be resolved" warning is spurious (editor running from the
homelab-k8s dir, not kodarr's `.venv`).
