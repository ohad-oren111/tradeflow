#!/usr/bin/env bash
# Uninstall the tradeflow watchdog.
#
# Default: strip cron entries only. State and logs are preserved for forensics.
# --purge: also remove ~/tradeflow/.watchdog-venv/ and ~/.tradeflow-watchdog/.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.tradeflow-watchdog"
VENV_DIR="$REPO_DIR/.watchdog-venv"
PURGE=0

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --help|-h)
            echo "Usage: $0 [--purge]"
            echo "  Without --purge: strip cron entries only; keep venv + state for forensics."
            echo "  With --purge: also remove $VENV_DIR and $STATE_DIR."
            exit 0
            ;;
        *)
            echo "[UNINSTALL] guard: unknown argument $arg" >&2
            exit 2
            ;;
    esac
done

if [[ "$EUID" -eq 0 ]]; then
    echo "[UNINSTALL] guard: refusing to run as root — use the tradeflow user" >&2
    exit 1
fi

# Backup current crontab before modification
mkdir -p "$STATE_DIR" 2>/dev/null || true
backup_file="$STATE_DIR/crontab.bak.uninstall.$(date +%s)"
crontab -l 2>/dev/null > "$backup_file" || : > "$backup_file"
echo "[UNINSTALL] crontab_backup: $backup_file"

# Strip watchdog block(s)
tmp_old="$STATE_DIR/crontab.old.$$"
tmp_new="$STATE_DIR/crontab.new.$$"
trap 'rm -f "$tmp_old" "$tmp_new"' EXIT
crontab -l 2>/dev/null > "$tmp_old" || true

awk '
    /^# tradeflow-watchdog/ { skip = 1; next }
    skip && /^[[:space:]]*$/ { skip = 0; next }
    skip { next }
    { print }
' "$tmp_old" > "$tmp_new"

if [[ -s "$tmp_new" ]]; then
    crontab "$tmp_new"
else
    crontab -r 2>/dev/null || true
fi
echo "[UNINSTALL] crontab: tradeflow-watchdog entries stripped"

if [[ "$PURGE" -eq 1 ]]; then
    if [[ -d "$VENV_DIR" ]]; then
        rm -rf "$VENV_DIR"
        echo "[UNINSTALL] purge: removed $VENV_DIR"
    fi
    if [[ -d "$STATE_DIR" ]]; then
        rm -rf "$STATE_DIR"
        echo "[UNINSTALL] purge: removed $STATE_DIR"
    fi
else
    echo "[UNINSTALL] preserved: $VENV_DIR (pass --purge to remove)"
    echo "[UNINSTALL] preserved: $STATE_DIR (pass --purge to remove)"
fi

echo "[UNINSTALL] done"
