-- Copy this file to /mnt/us/mangabridge/config.lua and edit it over USB.
-- A plain LAN HTTP URL is the most compatible option for an older Kindle.
return {
    server_url = "http://192.168.1.10:8095",
    api_token = "replace-with-the-KINDLE_API_TOKEN-from-your-server",

    -- Leave these blank unless the web app also uses Basic authentication.
    username = "",
    password = "",

    -- Conservative timeouts for slow Wi-Fi and chapters prepared on demand.
    timeout_seconds = 45,
    download_timeout_seconds = 600,
    poll_attempts = 600,
}
