## Disclaimer
The entirety of this project (with the important exception of the backend manga downloader) was vibecoded with ChatGPT. I am not a developer, I barely understand the very basics of python, so this is all quite beyond me. The whole project was born by a desire to read manga on an old kindle I had laying around, but I couldn't find any ready-made solutions online, so I decided to test how far the vibes would get me. Quite far turns out. Scary (this literally took no more than 2 days on and off). But the current version is definitely usable, and might be of interest to other people.
Feel free to point out bugs and feature suggestions, or even fork your own for other devices!

# MangaBridge v2
MangaBridge is a self-hosted web app that functions as a self-hosted manga library, downloader and browser reader. It is effectively a web GUI for the excellent [aydinAGF/mangadl-cli](url). On top of that there is also a lightweight plugin for jailbroken amazon kindles (only tested for 4.1.4) that communicates with the server to retrieve chapters to read.

## Features

* **Multi-user profiles** — Individual usernames, display names, optional passwords, and separate libraries and reading progress for each user.
* **Personal libraries** — Each profile manages its own manga collection while sharing the same physical files on the server.
* **Shared libraries** — Create named shared views with other profiles, invite members, combine collections, and independently show or hide titles.
* **Single-copy storage** — Manga PDFs are stored once under `data/library` and reused across profiles, shared libraries, the web reader, downloads, and Kindle clients.
* **Manga search & metadata** — Search the configured manga source and enrich series with AniList covers, banners, descriptions, genres, scores, status, and other metadata.
* **Explore & recommendations** — Browse popular and trending manga and receive recommendations based on titles already in your library.
* **Authors & creators** — View creator credits directly from manga pages and browse dedicated author pages with portraits, biographies, details, and other works.
* **Chapter management** — Download individual chapters or chapter ranges, monitor series for new releases, remove downloaded chapters, and view download activity and history.
* **Integrated web reader** — Read downloaded chapters directly in the browser with scroll and single-page modes, zoom controls, fullscreen support, and saved reading position.
* **Reading progress** — Track chapter and page progress independently for every profile and resume reading across supported clients.
* **Offline reading** — Download individual chapter PDFs or generate portable offline-library bundles containing multiple chapters and a self-contained browser reader.
* **Kindle / KOReader support** — Browse the MangaBridge library from Kindle, download original-quality PDFs, use manga cover metadata, track reading progress, continue reading, and automatically move to or download the next chapter.
* **Kindle progress sync** — Associate a Kindle with a MangaBridge profile and synchronize chapter/page progress between KOReader and the server while retaining offline progress.
* **Automatic downloads** — Monitor selected series and automatically fetch newly available chapters.
* **Shared download handling** — Web and Kindle downloads use the same physical chapter files, avoid duplicate downloads, and serialize concurrent work on the same series.
* **Storage controls** — Delete individual downloaded chapters or globally remove a series, with safeguards when files are shared by multiple profiles.
* **Responsive interface** — Manga-inspired UI for desktop and mobile with library, shared-library, Explore, series, chapter, download, and reader views.
* **AniList integration** — Metadata, creator information, recommendations, popular/trending discovery, and dynamic library artwork with caching to reduce external API traffic.
* **Docker deployment** — Designed for self-hosting with Docker/Portainer and persistent library, database, configuration, and cache storage.
* **Kindle API** — Versioned `/api/kindle/v1/*` API for library browsing, chapter delivery, profile selection, metadata, and reading-progress synchronization.


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
