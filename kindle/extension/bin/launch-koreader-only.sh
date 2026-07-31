#!/bin/sh
set -u
. /mnt/us/extensions/mangabridge/bin/common.sh

check_koreader || exit 1
mkdir -p "$DATA_DIR"
rm -f "$DATA_DIR/open-on-start"
DISABLED_DIR="$DATA_DIR/mangabridge.koplugin.disabled-for-test"
RESTORE_PLUGIN=0

if [ -d "$PLUGIN_DIR" ]; then
    rm -rf "$DISABLED_DIR"
    mv "$PLUGIN_DIR" "$DISABLED_DIR"
    RESTORE_PLUGIN=1
fi

restore_plugin() {
    if [ "$RESTORE_PLUGIN" -eq 1 ] && [ -d "$DISABLED_DIR" ]; then
        mkdir -p "$KO_DIR/plugins"
        rm -rf "$PLUGIN_DIR"
        mv "$DISABLED_DIR" "$PLUGIN_DIR"
    fi
}
trap restore_plugin EXIT HUP INT TERM

log_line "Launching KOReader with MangaBridge temporarily disabled"
cd "$KO_DIR" || exit 1
export KOREADER_DIR="$KO_DIR"
"$KO_DIR/koreader.sh" --kual >> "$LAUNCH_LOG" 2>&1
RC=$?
restore_plugin
trap - EXIT HUP INT TERM
exit "$RC"
