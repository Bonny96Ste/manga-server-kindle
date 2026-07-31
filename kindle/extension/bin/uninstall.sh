#!/bin/sh
set -u
. /mnt/us/extensions/mangabridge/bin/common.sh
rm -rf "$PLUGIN_DIR"
rm -f "$DATA_DIR/open-on-start"
notify "MangaBridge plugin removed. Config and downloaded manga were kept."
exit 0
