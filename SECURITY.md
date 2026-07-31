# Security

## Do not commit secrets

Never commit `.env`, `/mnt/us/mangabridge/config.lua`, API tokens, Basic-auth passwords, downloaded manga, logs, or persistent application data. The included `.gitignore` excludes the normal locations, but review every commit before pushing.

## Network exposure

MangaBridge is designed primarily for a trusted home LAN. The Kindle 4 client is most compatible with plain HTTP, which does not encrypt the API token in transit. Do not expose the server directly to the public Internet.

For remote access, use a private VPN such as WireGuard or Tailscale on the network rather than publishing the application port publicly. The Kindle model targeted by this project may not support modern TLS configurations reliably.

## Compromised credentials

Rotate `KINDLE_API_TOKEN`, `MANGADL_PASSWORD`, and `MANGADL_SECRET_KEY` immediately if they are shared accidentally. Recreate the container after changing Portainer environment variables.

## Device safety

The provided Kindle package writes only to USB-visible storage under `/mnt/us`. It does not contain a firmware update, modify the root filesystem, replace the stock reader, or write to system partitions. Jailbreaking and third-party software still carry risk; maintain a backup and use at your own responsibility.
