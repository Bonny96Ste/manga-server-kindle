#!/bin/sh
set -u
. /mnt/us/extensions/mangabridge/bin/common.sh

/mnt/us/extensions/mangabridge/bin/install.sh || exit 1
check_koreader || exit 1
mkdir -p "$DATA_DIR"
touch "$DATA_DIR/open-on-start"
log_line "Launching MangaBridge through KOReader (normal KUAL mode)"

# KOReader's Kindle launcher must be told that KUAL started it.  The
# KOREADER_DIR override also avoids launcher path-detection problems.
cd "$KO_DIR" || exit 1
export KOREADER_DIR="$KO_DIR"
exec "$KO_DIR/koreader.sh" --kual >> "$LAUNCH_LOG" 2>&1
