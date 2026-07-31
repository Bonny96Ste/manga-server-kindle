#!/bin/sh

EXT_DIR="/mnt/us/extensions/mangabridge"
DATA_DIR="/mnt/us/mangabridge"
KO_DIR="/mnt/us/koreader"
PLUGIN_DIR="$KO_DIR/plugins/mangabridge.koplugin"
PAYLOAD_DIR="$EXT_DIR/payload/mangabridge.koplugin"
STATUS_FILE="$DATA_DIR/status.txt"
INSTALL_LOG="$DATA_DIR/install.log"

mkdir -p "$DATA_DIR" "$DATA_DIR/library" "$DATA_DIR/cache" 2>/dev/null

# Capture all installer output where it can be read over USB.
exec >> "$INSTALL_LOG" 2>&1
printf '\n=== MangaBridge installer %s ===\n' "$(date 2>/dev/null || echo unknown-time)"
printf 'Extension: %s\nData: %s\nKOReader: %s\n' "$EXT_DIR" "$DATA_DIR" "$KO_DIR"

notify() {
    message="$1"
    printf '%s\n' "$message" > "$STATUS_FILE"
    if command -v lipc-set-prop >/dev/null 2>&1; then
        lipc-set-prop com.lab126.system toasterMessage "$message" >/dev/null 2>&1 || true
    fi
    printf '%s\n' "$message"
}

if [ ! -d "$EXT_DIR" ]; then
    notify "MangaBridge: extension folder is missing"
    exit 1
fi

# Repair permissions that may be stripped by Windows/USB ZIP extraction.
chmod 755 "$EXT_DIR"/bin/*.sh 2>/dev/null || true

if [ ! -d "$KO_DIR" ] || [ ! -f "$KO_DIR/koreader.sh" ]; then
    notify "MangaBridge: KOReader is not installed in /mnt/us/koreader"
    exit 1
fi

if [ ! -f "$PAYLOAD_DIR/main.lua" ] || [ ! -f "$PAYLOAD_DIR/_meta.lua" ]; then
    notify "MangaBridge: plugin payload is missing"
    exit 1
fi

mkdir -p "$KO_DIR/plugins" || {
    notify "MangaBridge: cannot create KOReader plugins folder"
    exit 1
}

rm -rf "$PLUGIN_DIR"
cp -R "$PAYLOAD_DIR" "$PLUGIN_DIR" || {
    notify "MangaBridge: failed to copy the KOReader plugin"
    exit 1
}

if [ ! -f "$DATA_DIR/config.example.lua" ]; then
    cp "$EXT_DIR/config.example.lua" "$DATA_DIR/config.example.lua" || true
fi

# Preserve an existing user config. Seed a clearly editable copy when absent.
if [ ! -f "$DATA_DIR/config.lua" ] && [ -f "$DATA_DIR/config.example.lua" ]; then
    cp "$DATA_DIR/config.example.lua" "$DATA_DIR/config.lua" || true
fi

if [ -f "$PLUGIN_DIR/main.lua" ]; then
    notify "MangaBridge installed. Edit /mnt/us/mangabridge/config.lua, then launch it."
    exit 0
fi

notify "MangaBridge: install verification failed"
exit 1
