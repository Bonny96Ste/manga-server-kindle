# MangaBridge v2

MangaBridge v2 is a self-hosted, multi-profile manga library built around a single deduplicated PDF collection. Each reader gets a personal shelf and independent progress, while trusted profiles can opt into one or more shared library views.

Existing Kindle clients remain compatible. MangaBridge Kindle v1.1.1 adds profile-aware reading-progress sync and automatic next-chapter continuation when paired with this server.

## Highlights

### Profiles

- First-run profile setup replaces browser HTTP Basic authentication.
- Each profile has a username, display name, and optional password.
- Password-free profiles are suitable for a trusted household LAN.
- A signed-in profile can create additional profiles.
- Existing series and legacy reading progress are assigned to the first profile during migration.
- Reading status, last chapter, and last page are stored per profile.

### Personal and shared libraries

- **My library** contains only the series added by the signed-in profile.
- Any profile can create multiple named shared library views.
- A shared view is a union of the accepted members’ personal libraries.
- Invitations remain pending until the invited profile accepts them.
- Cards identify which members added each series.
- Each member can hide a series from a particular shared view without changing anyone’s personal library.
- Hidden shared items can be restored from the shared-view toolbar.

### One physical collection

- Manga folders and PDFs remain global under `data/library`.
- Adding an existing series to another profile creates only a database membership; files are not copied.
- Downloads, monitoring and Kindle delivery use the same physical PDFs.
- Web and Kindle download workers are serialised per physical series and skip chapters that already exist.
- Removing a series from one profile retains the files for other profiles.
- Global deletion requires typing `DELETE` and shows the affected readers.
- Downloaded chapters have a delete action with a shared-file warning.

### Explore

- Popular and trending manga from AniList.
- Recommendations derived from AniList IDs already present in the signed-in profile’s library.
- Covers, banners, scores, status, genres and descriptions.
- “Find source & add” resolves the selected AniList title against the configured manga source only when requested.
- Explore data is cached for six hours to avoid unnecessary API traffic.

### Client and offline downloads

- Every chapter row includes **Download PDF** for saving the original chapter PDF to the current browser/device.
- Chapters that are not yet present on the server are downloaded first and handed to the client automatically when ready.
- The range-download panel includes **Download offline library** alongside the existing server download action.
- Offline-library ZIPs contain the selected original PDFs, `manifest.json`, a README, and a self-contained `index.html` reader.
- After extracting a bundle, `index.html` can be opened without MangaBridge or Internet access. The portable reader provides chapter selection, previous/next navigation, keyboard navigation, last-chapter memory, and direct-PDF fallback.

### Authors and creators

- AniList creator credits are stored with series metadata and displayed on the series page.
- Creator names link to dedicated MangaBridge author pages.
- Author pages include AniList image, biography, occupations, profile facts, and manga credits.
- Works already accessible to the signed-in profile open directly in MangaBridge; other works can use **Find source & add**.
- Existing AniList-linked series automatically backfill creator metadata when first opened after the upgrade.

### Interface

- New manga-inspired visual language with panel cuts, halftone texture, ink shadows and high-contrast controls.
- The page background selects a random AniList banner from the signed-in profile’s library on each request.
- Responsive personal, shared, Explore, series, chapter and reader views.
- The Maintenance panel has been removed.

### Existing capabilities retained

- Search and AniList metadata matching.
- PDF-only chapter storage.
- Individual and range downloads.
- Automatic monitoring and chapter downloads.
- Integrated browser reader.
- Download activity and event history.
- Original-quality Kindle PDF delivery.
- Kindle metadata covers and the existing `/api/kindle/v1/*` contract.

## Storage model

```text
/stack/
├── webapp/                         application source
├── startup.sh
├── docker-compose.yml
├── venv/                           generated, persistent
└── data/
    ├── library/                    one physical folder per manga
    ├── downloads/                  temporary downloader workspace
    └── state/
        ├── accounts-v2.sqlite3     profiles, memberships and progress
        ├── chapter-cache/
        ├── kindle-cache/
        ├── pdf-cache/
        ├── events.json
        └── jobs.json
```

The SQLite database contains no manga pages. Backing up `data/library` and `data/state/accounts-v2.sqlite3` preserves the collection and all profile state.

## Upgrade from MangaBridge v1

### 1. Back up the current stack

At minimum, copy:

```text
data/library/
data/state/
.env
```

The v2 migration is non-destructive, but a backup is strongly recommended.

### 2. Replace application files

Stop or redeploy the container, then replace these bind-mounted files and directories with the v2 package:

```text
webapp/
docker-compose.yml
startup.sh
.env.example
```

Do **not** replace or delete:

```text
data/
venv/
.env
```

The existing virtual environment can be retained. `startup.sh` reconciles the declared dependencies on launch.

### 3. Update Portainer environment variables

Required:

```env
MANGADL_STACK_PATH=/absolute/host/path/to/the/stack
MANGADL_PORT=8095
MANGADL_SECRET_KEY=replace-with-a-long-random-secret
KINDLE_API_TOKEN=replace-with-your-existing-kindle-token
```

Optional:

```env
# Fallback profile for older Kindle clients that do not send a profile header.
# MangaBridge Kindle v1.1.1 can instead choose its profile in config.lua.
# When neither is set, the first profile is used.
KINDLE_PROFILE_USERNAME=reader-one

# Set to 1 only when the web interface is always accessed through HTTPS.
SESSION_COOKIE_SECURE=0
```

`MANGADL_USERNAME` and `MANGADL_PASSWORD` are no longer used for the web login. They remain optional as a legacy Basic-auth fallback for Kindle API requests.

### 4. Redeploy

Deploy the updated stack in Portainer. The container will continue to use the stock `python:3.12-slim-bookworm` image and the bind-mounted startup script.

Open:

```text
http://SERVER_IP:PORT/
```

On first launch, create the initial profile. MangaBridge will:

1. Create `data/state/accounts-v2.sqlite3`.
2. Assign every currently visible v1 series to the new profile.
3. Import legacy read chapters and the most recent chapter from each series metadata file.
4. Leave all PDFs and metadata files in place.

## Fresh Portainer installation

1. Copy this repository to a persistent host directory, for example:

   ```text
   /opt/portainer/mangabridge/server
   ```

2. In Portainer, create a stack from `docker-compose.yml`.
3. Set `MANGADL_STACK_PATH` to that absolute host path.
4. Set a strong `MANGADL_SECRET_KEY` and `KINDLE_API_TOKEN`.
5. Deploy and open the published port.
6. Create the first profile.

## Profile login

The login screen lists all profiles.

- A password-free profile opens after selecting it and pressing **Enter library**.
- A protected profile requires its password.
- Use **Profiles & shared libraries** from the profile chip to edit the current profile or create another one.

Passwords are stored as salted PBKDF2-SHA256 hashes in SQLite.

## Shared library workflow

1. Open **Profiles & shared libraries**.
2. Create a named shared library, such as `Family shelf`.
3. Open the shared library and invite another existing profile.
4. Sign in as the invited profile.
5. Open **Profiles & shared libraries** and accept the pending invitation.
6. The shared tab now displays the union of both personal libraries.

A series may appear once even when several members have it. The card lists all members who added it.

### Hiding a shared series

Press **Hide** on a shared card. This affects only the current profile in that shared view. Use the **Hidden** menu in the shared toolbar to restore it.

## Deletion rules

### Remove a series from one profile

Choose **Remove only from my library**. This deletes the membership and that profile’s progress, but retains all physical files and every other profile’s membership.

### Delete a series globally

Choose **Delete files for all profiles**, review the affected readers, and type `DELETE`. This removes:

- the physical series folder and PDFs;
- all profile memberships for that series;
- all per-profile progress for that series;
- shared-view hide records for that series.

### Delete a chapter

The chapter delete dialog lists readers who have the series. Confirming removes the shared PDF, so every web and Kindle profile loses access to that chapter until it is downloaded again.

## Kindle compatibility

The current Kindle application continues to use API version 1 endpoints:

```text
GET  /api/kindle/v1/ping
GET  /api/kindle/v1/library
GET  /api/kindle/v1/series/<series-id>
GET  /api/kindle/v1/series/<series-id>/chapter/<chapter-id>/progress
POST /api/kindle/v1/series/<series-id>/chapter/<chapter-id>/progress
POST /api/kindle/v1/series/<series-id>/chapter/<chapter-id>/prepare
POST /api/kindle/v1/series/<series-id>/bulk
GET  /api/kindle/v1/jobs/<job-id>
GET  /api/kindle/v1/series/<series-id>/chapter/<chapter-id>/file
GET  /api/kindle/v1/series/<series-id>/cover
```

MangaBridge Kindle v1.1.1 sends `X-MangaBridge-Profile` with the configured `profile_username`; that profile controls library visibility and progress. An explicitly requested unknown profile returns an error rather than falling back. Clients that do not send the header continue to use `KINDLE_PROFILE_USERNAME`, then the first profile as a final fallback.

Progress is stored in the same per-profile `reading_progress` table used by the browser. The Kindle API translates KOReader's 1-based PDF page numbers to the browser reader's 0-based page indexes, so the same physical page resumes correctly across devices. Progress merges monotonically: an older device cannot reduce the furthest saved page.

Test it with:

```bash
curl -H "X-MangaDL-Token: YOUR_TOKEN" \
  http://SERVER_IP:PORT/api/kindle/v1/ping
```

The response retains `api_version: 1` and reports the selected profile.

## Explore data

MangaBridge uses AniList’s public GraphQL API for discovery and metadata. No AniList login is required. The server caches popular and recommendation responses for six hours and resolves a downloadable source only when a user presses **Find source & add**.

Recommendations are based on up to four AniList-linked series from the current profile and are deduplicated against that profile’s library.

## Security notes

- Use a long random `MANGADL_SECRET_KEY`.
- Give password-free profiles only to users on a trusted network.
- Put the service behind HTTPS, a VPN, or an authenticated reverse proxy before exposing it publicly.
- Set `SESSION_COOKIE_SECURE=1` only behind HTTPS.
- Keep `KINDLE_API_TOKEN` private and rotate it if disclosed.
- The web session is separate from the Kindle token.
- Web state-changing requests are protected with per-session CSRF tokens; Kindle API requests remain token-authenticated and are exempt.
- Destructive global actions require explicit confirmation but are available to any profile that owns the series.

## Backup and restore

Back up:

```text
data/library/
data/state/accounts-v2.sqlite3
data/state/jobs.json
data/state/events.json
```

SQLite uses WAL mode. For a fully consistent live backup, stop the container first or include the adjacent `accounts-v2.sqlite3-wal` and `accounts-v2.sqlite3-shm` files.

## Troubleshooting

### The first profile does not contain the old library

Confirm the series folders existed under `data/library` before the first profile was created. You can add a missing global series to a profile through search without downloading its PDFs again.

### A shared library is empty

Both profiles must have accepted membership. Pending invitations are shown under **Profiles & shared libraries**.

### Explore is temporarily empty

Check container Internet access and AniList availability. Existing cached suggestions remain available when possible. Use **Refresh suggestions** after connectivity returns.

### The Kindle shows the wrong profile

For MangaBridge Kindle v1.1.1 or later, set the exact profile username in `/mnt/us/mangabridge/config.lua`:

```lua
profile_username = "the-profile-username",
```

Then use **Test connection** on the Kindle and confirm the selected profile. For older Kindle clients, set `KINDLE_PROFILE_USERNAME` on the server and recreate the container.

### A deleted chapter still appears in the browser reader

Reload the series page. Rendered page images may remain in the server cache until it is pruned, but the chapter endpoint will no longer expose them without the source PDF.

## Version

- Web/server: **2.3.1**
- Kindle API: **1** (backwards compatible)
- Recommended Kindle client: **1.1.1** for progress sync; older API v1 clients remain compatible
