# Changelog

## Server 1.0.2 / Kindle client 1.0.5

- Preserve original PDFs by default for correct page geometry and maximum zoom detail.
- Keep optional fixed-page grayscale conversion as `KINDLE_PDF_MODE=balanced`.
- Fix background Kindle jobs using Flask URL generation outside an application context.
- Fix KOReader JSON calls for KOReader v2026.03.
- Improve plugin exception handling and diagnostics.
- Support direct USB installation of the KUAL extension, plugin, and data directory.
- Add offline library caching, bulk ranges, free-space checks, partial-download protection, and local chapter deletion.

## Server 1.0.0 / Kindle client 1.0.0

- Initial MangaDL Ultimate web library and MangaBridge Kindle integration.
