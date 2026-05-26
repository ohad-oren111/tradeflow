#!/usr/bin/env bash
# TradeFlow disk/log hygiene. Conservative: explicit ALLOWED_PATHS whitelist;
# refuses to touch anything else. Supports --dry-run.
#
# Schedule: daily 03:00 UTC via cron (installed by scripts/install_watchdog.sh).
# Log format: [CLEANUP] component: action — detail
set -euo pipefail

ALLOWED_PATHS=("/tmp" "$HOME/.tradeflow-watchdog")
DRY_RUN=0

if [[ "$EUID" -eq 0 ]]; then
    echo "[CLEANUP] guard: refusing to run as root — use the tradeflow user" >&2
    exit 1
fi

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --help|-h)
            echo "Usage: $0 [--dry-run]"
            exit 0
            ;;
        *)
            echo "[CLEANUP] guard: unknown argument $arg" >&2
            exit 2
            ;;
    esac
done

path_is_allowed() {
    local candidate="$1"
    local allowed
    for allowed in "${ALLOWED_PATHS[@]}"; do
        if [[ "$candidate" == "$allowed" || "$candidate" == "$allowed"/* ]]; then
            return 0
        fi
    done
    return 1
}

run_cmd() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "[CLEANUP] dry-run: would run: $*"
    else
        "$@"
    fi
}

echo "[CLEANUP] start: ts=$(date -u +%FT%TZ) dry_run=$DRY_RUN"

# 1. /tmp files older than 7 days
if path_is_allowed "/tmp"; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
        count=$(find /tmp -mindepth 1 -mtime +7 2>/dev/null | wc -l)
        echo "[CLEANUP] tmp: dry-run — would delete $count files in /tmp older than 7 days"
    else
        deleted=$(find /tmp -mindepth 1 -mtime +7 -print -delete 2>/dev/null | wc -l)
        echo "[CLEANUP] tmp: deleted $deleted files in /tmp older than 7 days"
    fi
else
    echo "[CLEANUP] tmp: skipped — /tmp not in ALLOWED_PATHS"
fi

# 2. Rotate watchdog logs (>50MB OR >7 days old)
LOG_DIR="$HOME/.tradeflow-watchdog"
LOG_FILE="$LOG_DIR/watchdog.log"
if path_is_allowed "$LOG_DIR"; then
    if [[ -f "$LOG_FILE" ]]; then
        size_bytes=$(stat -c '%s' "$LOG_FILE")
        size_mb=$((size_bytes / 1048576))
        mtime_days=$(( ( $(date +%s) - $(stat -c '%Y' "$LOG_FILE") ) / 86400 ))
        if (( size_mb > 50 || mtime_days > 7 )); then
            rotated="$LOG_DIR/watchdog.log.$(date -u +%F)"
            run_cmd mv "$LOG_FILE" "$rotated"
            run_cmd touch "$LOG_FILE"
            echo "[CLEANUP] log_rotate: rotated watchdog.log → $(basename "$rotated") (size=${size_mb}MB age=${mtime_days}d)"
        else
            echo "[CLEANUP] log_rotate: no rotation needed (size=${size_mb}MB age=${mtime_days}d)"
        fi
    else
        echo "[CLEANUP] log_rotate: no watchdog.log to rotate"
    fi

    # Gzip prior-week rotated logs that aren't yet compressed
    for log in "$LOG_DIR"/watchdog.log.*; do
        [[ ! -f "$log" ]] && continue
        case "$log" in
            *.gz) continue ;;
        esac
        # gzip files older than 1 day to give the rotation time to settle
        if [[ $(( ( $(date +%s) - $(stat -c '%Y' "$log") ) / 86400 )) -ge 1 ]]; then
            run_cmd gzip "$log"
            echo "[CLEANUP] log_rotate: gzipped $(basename "$log")"
        fi
    done

    # Delete rotated logs older than 30 days
    if [[ "$DRY_RUN" -eq 1 ]]; then
        old_count=$(find "$LOG_DIR" -maxdepth 1 -name 'watchdog.log.*' -mtime +30 2>/dev/null | wc -l)
        echo "[CLEANUP] log_rotate: dry-run — would delete $old_count rotated logs older than 30 days"
    else
        old_deleted=$(find "$LOG_DIR" -maxdepth 1 -name 'watchdog.log.*' -mtime +30 -print -delete 2>/dev/null | wc -l)
        echo "[CLEANUP] log_rotate: deleted $old_deleted rotated logs older than 30 days"
    fi
else
    echo "[CLEANUP] log_rotate: skipped — $LOG_DIR not in ALLOWED_PATHS"
fi

# 3. Docker hygiene — narrow, conservative pruning only
echo "[CLEANUP] docker_image_prune: dangling + unused older than 7 days"
run_cmd docker image prune -af --filter "until=168h"

echo "[CLEANUP] docker_builder_prune: build cache older than 7 days"
run_cmd docker builder prune -af --filter "until=168h"

# NOTE: deliberately NOT running `docker system prune -af` — too broad; can hit
# images the running containers depend on if a tag points to "latest".

echo "[CLEANUP] complete: ts=$(date -u +%FT%TZ)"
