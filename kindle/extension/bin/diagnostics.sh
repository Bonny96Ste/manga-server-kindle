#!/bin/sh
set -u
. /mnt/us/extensions/mangabridge/bin/common.sh
mkdir -p "$DATA_DIR"
REPORT="$DATA_DIR/diagnostics.txt"
{
    echo "MangaBridge diagnostics v1.0.5"
    date 2>/dev/null || true
    echo
    echo "System:"
    uname -a 2>/dev/null || true
    for file in /etc/prettyversion.txt /etc/version.txt; do
        if [ -f "$file" ]; then
            echo "--- $file"
            cat "$file"
        fi
    done
    echo
    echo "Storage:"
    df -h /mnt/us 2>/dev/null || df /mnt/us 2>/dev/null || true
    echo
    echo "KOReader files:"
    ls -ld "$KO_DIR" 2>/dev/null || true
    ls -l "$KO_DIR/koreader.sh" "$KO_DIR/reader.lua" "$KO_DIR/libkohelper.sh" 2>/dev/null || true
    if [ -f "$KO_DIR/git-rev" ]; then
        echo "KOReader revision: $(cat "$KO_DIR/git-rev" 2>/dev/null)"
    fi
    echo
    echo "MangaBridge:"
    [ -f "$PLUGIN_DIR/main.lua" ] && echo "Plugin: installed" || echo "Plugin: not installed"
    [ -f "$DATA_DIR/config.lua" ] && echo "Config: present" || echo "Config: not configured"
    [ -f "$DATA_DIR/open-on-start" ] && echo "Startup marker: present" || echo "Startup marker: absent"
    [ -f "$DATA_DIR/cache/library.json" ] && echo "Library cache: present" || echo "Library cache: empty"
    echo
    echo "Downloaded chapters:"
    find "$DATA_DIR/library" -type f -name '*.pdf' 2>/dev/null | wc -l
    echo
    echo "Last installer status:"
    cat "$STATUS_FILE" 2>/dev/null || true
    echo
    echo "Last 120 lines of MangaBridge launch log:"
    tail -n 120 "$LAUNCH_LOG" 2>/dev/null || true
    echo
    echo "Last 160 lines of KOReader crash.log:"
    tail -n 160 "$KO_DIR/crash.log" 2>/dev/null || true
    echo
    echo "Last 120 lines of MangaBridge plugin-error.log:"
    tail -n 120 "$DATA_DIR/plugin-error.log" 2>/dev/null || true
} > "$REPORT"
notify "Diagnostics saved: /mnt/us/mangabridge/diagnostics.txt"
exit 0
