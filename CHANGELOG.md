# Changelog

## 2.3.1

- Hardened series-page metadata handling so malformed or legacy AniList fields cannot crash the page renderer.
- Non-object `metadata.json` values now fall back safely instead of raising during series display.
- Corrupt/stale chapter-cache rows are ignored individually and no longer break a series page.
- Added a regression test for malformed description, genres, tags, creator and URL metadata shapes.

## 2.3.0

- Added profile-aware Kindle requests through the optional `X-MangaBridge-Profile` header while preserving `KINDLE_PROFILE_USERNAME` and first-profile fallback for older clients.
- Added Kindle API reading-progress GET/POST endpoints with monotonic page merging and completion state.
- Added total-page storage and an automatic SQLite migration for existing `reading_progress` tables.
- Added explicit conversion between KOReader's 1-based PDF pages and the web reader's existing 0-based page indexes.
- Kindle library and series payloads now include last chapter/page timestamps and per-chapter progress for Continue Reading.
- Unknown explicitly requested Kindle profiles are rejected instead of silently falling back to another user.
- Existing Kindle API v1 chapter, cover, range and file-delivery routes remain compatible.

## 2.2.0

- Added per-chapter client PDF downloads from the series chapter shelf. Remote chapters are prepared on the server first, then delivered to the browser automatically.
- Added range downloads as portable offline-library ZIPs containing the selected chapter PDFs, a manifest, README, and a self-contained browser reader.
- Added AniList creator credits to series metadata and creator links on series pages.
- Added creator profile pages with image, biography, occupations, profile details, and paginated manga credits.
- Added direct actions from creator bibliographies to open works already accessible in MangaBridge or find a source and add new works.
- Backfills creator metadata for existing AniList-linked series when their series page is opened.
- Added offline bundle and AniList creator/pagination tests.
- Server and Kindle ping version reporting now read from the package VERSION file.

## 2.1.3

- Single-page reader mode uses a scrollable viewport so tall pages can be read top to bottom.
- Reader zoom now works in both scroll and single-page modes from 50% to 300%, with a Fit reset and percentage indicator.
- Continuous mode supports horizontal panning while zoomed.
- Page Up/Page Down and Space/Shift+Space scroll within a single page before crossing chapter pages.
- Added keyboard zoom shortcuts and improved swipe behavior while zoomed.

## 2.0.0

- Replaced web HTTP Basic authentication with multi-profile sessions.
- Added optional per-profile passwords.
- Added personal libraries backed by a single deduplicated physical collection.
- Added multiple invitation-based shared library views.
- Added per-shared-view hide and restore controls.
- Moved reading progress to per-profile SQLite records, including last page.
- Added shared-impact confirmation for chapter and series deletion.
- Added popular, trending and personalised Explore sections via AniList.
- Added rotating AniList library banners and a complete manga-inspired UI redesign.
- Removed the Maintenance panel.
- Preserved the Kindle API v1 contract and existing Kindle client compatibility.
