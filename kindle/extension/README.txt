MangaBridge v1.1.1 - Kindle 4 / KUAL / KOReader

This is a safe in-place update for the MangaBridge KOReader plugin. Pair it with
MangaBridge Server v2.3.0 or later to enable profile-aware progress sync.

Reading progress
----------------
MangaBridge records the current PDF page locally in:
/mnt/us/mangabridge/progress.json

Local progress works without Wi-Fi. Progress state is separated by the
profile_username configured in /mnt/us/mangabridge/config.lua.

When Wi-Fi is already connected and progress_sync is enabled, pending progress is
merged with the selected server profile. Opening a chapter restores a farther server
position when necessary. A manual "Sync reading progress now" action is also available.

Continue reading
----------------
Each series menu shows its own latest chapter/page as a Continue chapter action. The MangaBridge menu and library also keep a global Continue latest shortcut for the most recently read series. If that
chapter is no longer on the Kindle, Continue reading asks whether to download it.

Next chapter
------------
Read the final page normally. When you press a forward page-turn button again:
- a downloaded next chapter opens immediately;
- a missing next chapter prompts before MangaBridge downloads it;
- if no newer chapter is known, MangaBridge tells you to refresh when online.

Set auto_next_chapter = false in config.lua or toggle the menu setting to disable this.

Profile setup
-------------
Set the exact MangaBridge profile username in config.lua, for example:

    profile_username = "reader-one",

This is separate from the optional HTTP Basic-auth username/password. Use "Test
connection" to verify which MangaBridge profile the server selected.

High-resolution sleep covers
----------------------------
The v1.0.9 cover revision/cache behavior is retained. Cached covers remain in:
/mnt/us/mangabridge/covers

Your config.lua, chapters and local progress are not included in this update archive.
