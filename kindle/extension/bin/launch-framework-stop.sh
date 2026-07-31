#!/bin/sh
set -u
. /mnt/us/extensions/mangabridge/bin/common.sh

/mnt/us/extensions/mangabridge/bin/install.sh || exit 1
check_koreader || exit 1
mkdir -p "$DATA_DIR"
touch "$DATA_DIR/open-on-start"
log_line "Launching MangaBridge through KOReader (framework-stop fallback)"

cd "$KO_DIR" || exit 1
export KOREADER_DIR="$KO_DIR"
exec "$KO_DIR/koreader.sh" --kual --framework_stop >> "$LAUNCH_LOG" 2>&1
