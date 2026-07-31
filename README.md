# MangaBridge

MangaBridge is a self-hosted manga library, downloader, browser reader, and lightweight offline Kindle client.

The server runs from a bind-mounted folder using Docker Compose or Portainer. It manages a PDF-only library, retrieves AniList metadata, discovers chapters, downloads selected ranges, monitors series for new releases, and exposes a compact API for older Kindles. The Kindle client is a KOReader plugin launched through KUAL and designed around the physical controls and limited resources of the Kindle 4 Non-Touch.

> **Current working versions:** server `1.0.2`, Kindle client `1.0.5`.

## mangadl-cli
The server downloader was inspired by and adapted from the workflow of aydinAGF/mangadl-cli, distributed under the MIT License.

MIT License
Copyright (c) 2026 aydin

The full upstream license is available at https://github.com/aydinAGF/mangadl-cli/blob/main/LICENSE.

## Features

### Web library

- Library-first dashboard with search and AniList metadata.
- Add search results directly to the local library.
- PDF-only chapter storage.
- Per-chapter download, refresh, and read actions.
- Bulk chapter ranges such as `1-10`, `1,3,7.5`, or `all`.
- Download all missing chapters.
- In-browser reader with server-rendered pages.
- Long-strip and single-page reading modes.
- Previous/next chapter navigation and chapter selector.
- Adjustable page width, fullscreen, and keyboard controls.
- Reading progress, read/unread state, and Continue Reading.
- Pinned series, library statistics, sorting, and activity history.
- Remove a series while archiving its PDFs, or delete everything.
- Restore archived series.
- Monitor a series and automatically download new or missing chapters.
- Persistent download-job history and library JSON export.
- Optional HTTP Basic authentication.

### Kindle client

- Designed for a jailbroken Kindle 4 Non-Touch on firmware 4.1.4.
- Runs as a removable KOReader plugin launched from KUAL.
- Browse the server library and chapter lists.
- Cache library metadata for offline browsing.
- Download one chapter or a range to the Kindle.
- Ask the server to fetch a missing chapter from the Internet, then download and open it automatically.
- Store each chapter as a local PDF for offline reading.
- Continue the last opened MangaBridge chapter.
- Delete an offline chapter without deleting the server copy.
- Interrupted downloads use `.part` files and are validated before use.
- Checks available storage before downloading.
- Uses KOReader for page rendering, zoom, crop, orientation, contrast, refresh behavior, and physical page buttons.
- Writes only below `/mnt/us`; no firmware package or system-partition modification is included.

### Kindle PDF modes

`KINDLE_PDF_MODE=original` is the recommended default. It sends the original server PDF unchanged, preserving resolution, crop boxes, orientation, and page boundaries.

`KINDLE_PDF_MODE=balanced` creates a smaller grayscale fixed-page PDF using the configured width, maximum height, JPEG quality, and cache limit. This saves storage but reduces detail and is not recommended when the original PDF already works well.

## Repository layout

```text
.
├── server/                    Portainer/Docker server
│   ├── docker-compose.yml
│   ├── startup.sh
│   └── webapp/
├── kindle/
│   ├── extension/             KUAL menu and launch scripts
│   ├── plugin/                KOReader MangaBridge plugin
│   └── data/                  Kindle config template
├── scripts/build_releases.py  Builds release ZIPs
├── docs/TROUBLESHOOTING.md
├── .env.example
├── .gitignore
├── LICENSE
└── THIRD_PARTY_NOTICES.md
```

## Requirements

### Server

- Docker Engine with Docker Compose, or Portainer.
- An absolute host folder for the bind-mounted stack.
- LAN access from the Kindle.
- Outbound Internet access for metadata and chapter retrieval.

The runtime image is the stock `python:3.12-slim-bookworm` image. Python dependencies are installed into a persistent bind-mounted virtual environment on startup.

### Kindle

- Kindle 4 Non-Touch, tested with firmware 4.1.4.
- Jailbreak already installed.
- KUAL already installed.
- KOReader installed separately.
- Wi-Fi configured through the normal Kindle interface.

MangaBridge does not install the jailbreak, KUAL, KOReader, or firmware modifications.

## Server installation with Portainer

### 1. Prepare the host folder

Clone or copy the repository to the Docker host. The server folder must be accessible through an absolute path, for example:

```text
/opt/portainer/mangabridge/server
```

Do not place your library or virtual environment in the Git repository. The server creates these bind-mounted paths automatically:

```text
server/data/library
server/data/downloads
server/data/state
server/venv
```

### 2. Configure environment variables

Copy `.env.example` to `.env` outside version control and edit it:

```sh
cp .env.example .env
```

At minimum, set:

```text
MANGADL_STACK_PATH=/opt/portainer/mangabridge/server
MANGADL_PORT=8095
MANGADL_SECRET_KEY=a-long-random-secret
KINDLE_API_TOKEN=a-different-long-random-token
KINDLE_PDF_MODE=original
```

Optional web authentication:

```text
MANGADL_USERNAME=your-user
MANGADL_PASSWORD=your-password
```

The Kindle normally needs only `KINDLE_API_TOKEN`; leave its Basic-auth username and password blank.

### 3. Deploy in Portainer

Create a stack using `server/docker-compose.yml`. Add the values from `.env` to the stack environment, especially the absolute `MANGADL_STACK_PATH`.

Deploy the stack and open:

```text
http://SERVER_IP:MANGADL_PORT
```

The first start creates `server/venv`, installs dependencies, verifies the imports, and starts Gunicorn. Subsequent starts reuse and repair the virtual environment when requirements change.

### Docker Compose without Portainer

From the repository root:

```sh
docker compose --env-file .env -f server/docker-compose.yml up -d
```

View logs:

```sh
docker compose --env-file .env -f server/docker-compose.yml logs -f
```

### Verify the Kindle API

```sh
curl -i \
  -H "X-MangaDL-Token: YOUR_TOKEN" \
  http://SERVER_IP:PORT/api/kindle/v1/ping
```

A working response has HTTP `200` and includes:

```json
{
  "ok": true,
  "api_version": 1,
  "bridge_version": "1.0.2"
}
```

Test the library endpoint:

```sh
curl -i \
  -H "X-MangaDL-Token: YOUR_TOKEN" \
  http://SERVER_IP:PORT/api/kindle/v1/library
```

## Kindle installation

### Recommended: use a release ZIP

Build the release package locally or download `mangabridge-kindle-usb-v1.0.5.zip` from the repository's GitHub Releases page.

Extract it directly into the Kindle USB root. After extraction, these paths must exist:

```text
/mnt/us/extensions/mangabridge
/mnt/us/koreader/plugins/mangabridge.koplugin
/mnt/us/mangabridge
```

Edit this file over USB:

```text
/mnt/us/mangabridge/config.lua
```

Example:

```lua
return {
    server_url = "http://192.168.1.50:8095",
    api_token = "replace-with-your-KINDLE_API_TOKEN",

    username = "",
    password = "",

    timeout_seconds = 45,
    download_timeout_seconds = 600,
    poll_attempts = 600,
}
```

Use the server's real LAN IP address. Older Kindles are most compatible with plain HTTP on a trusted home network.

Safely eject the Kindle, close and reopen KUAL, then select:

```text
MangaBridge → Launch MangaBridge
```

KOReader can take several seconds to start on this hardware. Avoid selecting the launcher repeatedly.

### Kindle controls

In MangaBridge lists:

- Page-turn buttons move through menu pages.
- The five-way controller highlights and opens items.
- Holding/selecting an offline chapter offers local deletion.

While reading:

- The physical page buttons turn pages through KOReader.
- The Kindle Menu/Options button opens KOReader reader controls.
- KOReader provides zoom, crop, orientation, contrast, page fitting, refresh settings, and navigation.

## Offline behavior

The client stores downloaded PDFs below:

```text
/mnt/us/mangabridge/library
```

Cached library and series metadata are stored below:

```text
/mnt/us/mangabridge/cache
```

Once a chapter has been downloaded, it can be read without the server or Wi-Fi. Refreshing the library, fetching uncached chapter lists, and preparing missing chapters require network access.

## Monitoring and automatic downloads

Open a series in the web interface and enable monitoring.

Two modes are available:

- **Only chapters released after monitoring starts** records a baseline and downloads later releases.
- **Download every missing chapter** fills all gaps in the local server library.

The global watcher wakes at `WATCH_SCAN_SECONDS`; each series also has its own minimum interval. Automatic downloads are stored as PDFs in the server library and become available to the Kindle client.

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `MANGADL_STACK_PATH` | `/opt/portainer/mangabridge/server` | Absolute host bind-mount path |
| `MANGADL_PORT` | `8095` | Host and container web port |
| `MANGADL_SECRET_KEY` | insecure placeholder | Flask session secret; replace it |
| `MANGADL_USERNAME` | empty | Optional web Basic-auth username |
| `MANGADL_PASSWORD` | empty | Optional web Basic-auth password |
| `KINDLE_API_TOKEN` | empty | Token accepted by `/api/kindle/*` |
| `WATCH_SCAN_SECONDS` | `300` | Background watch scan frequency; minimum 300 |
| `PDF_RENDER_SCALE` | `1.6` | Browser-reader page-render scale |
| `KINDLE_PDF_MODE` | `original` | `original` or `balanced` |
| `KINDLE_RENDER_WIDTH` | `1200` | Balanced-mode source raster width |
| `KINDLE_RENDER_MAX_HEIGHT` | `1600` | Balanced-mode source raster maximum height |
| `KINDLE_JPEG_QUALITY` | `86` | Balanced-mode JPEG quality |
| `KINDLE_CACHE_MAX_MB` | `2048` | Server cache limit for balanced Kindle PDFs |

## Updating

### Server

Back up `server/data`, replace the tracked source files, and recreate the container. Do not remove `server/data` or `server/venv` unless troubleshooting a damaged environment.

### Kindle

Extract the new Kindle release ZIP into the USB root and replace existing extension/plugin files. Preserve:

```text
/mnt/us/mangabridge/config.lua
/mnt/us/mangabridge/library
/mnt/us/mangabridge/cache
```

A plugin-only update can replace `/mnt/us/koreader/plugins/mangabridge.koplugin` without touching downloads.

## Security and privacy

MangaBridge is intended for a trusted LAN. Plain HTTP exposes the Kindle API token to devices capable of observing that network. Do not publish the application port directly to the Internet. See [SECURITY.md](SECURITY.md).

If a token or password is exposed, rotate it and recreate the container. Never place real credentials in screenshots, issues, example files, commits, or release archives.

## Legal notice

MangaBridge does not include manga content. Use it only with material you are legally permitted to access and retain. Website integrations can change or stop working without notice.

The downloader workflow is inspired by the MIT-licensed [`mangadl-cli`](https://github.com/aydinAGF/mangadl-cli). The Kindle client requires the separately installed AGPL-licensed [KOReader](https://github.com/koreader/koreader) and KUAL. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MangaBridge source in this repository is released under the MIT License. See [LICENSE](LICENSE).
