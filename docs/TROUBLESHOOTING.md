# Troubleshooting

## The Kindle cannot reach the server

From another device on the same LAN:

```sh
curl -i --connect-timeout 5 \
  -H "X-MangaDL-Token: YOUR_TOKEN" \
  http://SERVER_IP:PORT/api/kindle/v1/ping
```

A working response is HTTP `200` with `"ok": true`. A hanging connection normally means the IP address, subnet, port mapping, firewall, or Wi-Fi client isolation is wrong. A `401` means the token does not match.

## The main web page returns 401

That is expected when `MANGADL_USERNAME` is configured. The Kindle API authenticates with `X-MangaDL-Token`; the Kindle's Basic-auth fields can normally remain blank.

## KOReader takes time to start

On a Kindle 4, KOReader may take tens of seconds to stop or yield the Amazon framework and initialize. Avoid repeatedly selecting the KUAL launch item. Use the fallback launcher only when the normal launcher consistently returns to the home screen.

## “The server returned invalid JSON”

Confirm that the server URL points to the correct host and port and that `/api/kindle/v1/library` returns JSON with the API token. An HTML Basic-auth page, reverse-proxy error, or wrong URL will not decode as JSON.

## A server job reports “working outside of application context”

Use server version 1.0.1 or later. Current source uses context-free relative download URLs in background jobs.

## PDF pages overlap or have poor quality

Use:

```text
KINDLE_PDF_MODE=original
```

Recreate the container, delete the affected offline chapter from the Kindle, and download it again. Existing Kindle files are not automatically replaced.

## Diagnostics

In KUAL, run:

```text
MangaBridge → Write diagnostics report
```

The report is written to:

```text
/mnt/us/mangabridge/diagnostics.txt
```

Additional plugin errors are written to `/mnt/us/mangabridge/plugin-error.log` and KOReader maintains its own `crash.log`.
