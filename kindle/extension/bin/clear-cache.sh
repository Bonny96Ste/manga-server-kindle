#!/bin/sh
set -u
. /mnt/us/extensions/mangabridge/bin/common.sh
rm -rf "$DATA_DIR/cache"
mkdir -p "$DATA_DIR/cache"
notify "MangaBridge cache cleared. Offline PDFs were kept."
exit 0
