-- Copy this file to /mnt/us/mangabridge/config.lua and edit it over USB.
-- A plain LAN HTTP URL is the most compatible option for an older Kindle.
return {
    server_url = "http://192.168.1.10:8095",
    api_token = "replace-with-the-KINDLE_API_TOKEN-from-your-server",

    -- MangaBridge web profile whose library/progress this Kindle should use.
    -- Use the profile username, not the display name.
    profile_username = "reader-one",

    -- Leave these blank unless the web app also uses Basic authentication.
    username = "",
    password = "",

    -- Reading behavior. Progress is always saved locally; this controls server sync.
    progress_sync = true,
    auto_next_chapter = true,

    -- Conservative timeouts for slow Wi-Fi and chapters prepared on demand.
    timeout_seconds = 45,
    download_timeout_seconds = 600,
    poll_attempts = 600,

    -- Show the cached series metadata cover when sleeping during a chapter.
    series_cover_screensaver = true,
}
