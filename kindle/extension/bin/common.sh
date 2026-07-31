#!/bin/sh

EXT_DIR="/mnt/us/extensions/mangabridge"
DATA_DIR="/mnt/us/mangabridge"
STATUS_FILE="$DATA_DIR/status.txt"
LAUNCH_LOG="$DATA_DIR/launch.log"
KO_DIR="/mnt/us/koreader"
PLUGIN_DIR="$KO_DIR/plugins/mangabridge.koplugin"
PAYLOAD_DIR="$EXT_DIR/payload/mangabridge.koplugin"

notify() {
    message="$1"
    mkdir -p "$DATA_DIR"
    printf '%s\n' "$message" > "$STATUS_FILE"
    if command -v lipc-set-prop >/dev/null 2>&1; then
        lipc-set-prop com.lab126.system toasterMessage "$message" >/dev/null 2>&1 || true
    fi
}

log_line() {
    mkdir -p "$DATA_DIR"
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown-time)" "$*" >> "$LAUNCH_LOG"
}

check_koreader() {
    if [ ! -d "$KO_DIR" ]; then
        notify "MangaBridge: /mnt/us/koreader is missing"
        return 1
    fi
    if [ ! -x "$KO_DIR/koreader.sh" ]; then
        notify "MangaBridge: koreader.sh is missing or not executable"
        return 1
    fi
    if [ ! -f "$KO_DIR/reader.lua" ]; then
        notify "MangaBridge: KOReader installation is incomplete (reader.lua missing)"
        return 1
    fi
    return 0
}
