#!/usr/bin/env bash
# Idempotent installer for the tradeflow watchdog.
# Safe to run multiple times. Backs up existing crontab before modifying.
#
# Steps:
#   1. Create ~/.tradeflow-watchdog/ (mode 700) if missing
#   2. Create ~/tradeflow/.watchdog-venv/ if missing OR python version < 3.11
#   3. pip install -r requirements-watchdog.txt
#   4. Backup current crontab to ~/.tradeflow-watchdog/crontab.bak.<epoch>
#   5. Strip any prior tradeflow-watchdog entries (idempotent)
#   6. Append new entries from scripts/watchdog_crontab.template
#   7. Run --mode=self-test and exit with its return code
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="$HOME/.tradeflow-watchdog"
VENV_DIR="$REPO_DIR/.watchdog-venv"
REQ_FILE="$REPO_DIR/requirements-watchdog.txt"
TEMPLATE="$REPO_DIR/scripts/watchdog_crontab.template"
WATCHDOG_PY="$REPO_DIR/scripts/tradeflow_watchdog.py"

if [[ "$EUID" -eq 0 ]]; then
    echo "[INSTALL] guard: refusing to run as root — use the tradeflow user" >&2
    exit 1
fi

echo "[INSTALL] start: repo=$REPO_DIR state_dir=$STATE_DIR venv=$VENV_DIR"

# 1. State dir
if [[ ! -d "$STATE_DIR" ]]; then
    mkdir -m 700 "$STATE_DIR"
    echo "[INSTALL] state_dir: created $STATE_DIR"
else
    chmod 700 "$STATE_DIR"
    echo "[INSTALL] state_dir: exists"
fi

# 2. Venv
needs_venv=0
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    needs_venv=1
else
    py_version=$("$VENV_DIR/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    case "$py_version" in
        3.11|3.12|3.13|3.14) ;;
        *) needs_venv=1 ;;
    esac
fi

# Detect an explicit python3.11+ binary that has ensurepip + venv. Avoid the
# unversioned `python3`: on Ubuntu 22.04 that points to python3.10 which is
# missing `ensurepip` from the system venv module, so `python3 -m venv` fails.
# Order is descending preference — newer interpreter wins when present.
detect_venv_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import ensurepip, venv' >/dev/null 2>&1; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if [[ "$needs_venv" -eq 1 ]]; then
    if ! venv_python=$(detect_venv_python); then
        echo "[INSTALL] guard: no python3.11+ with ensurepip+venv found." >&2
        echo "[INSTALL] guard: install with 'sudo apt install python3.11-venv'" >&2
        exit 1
    fi
    rm -rf "$VENV_DIR"
    echo "[INSTALL] venv: creating with $venv_python -m venv $VENV_DIR"
    "$venv_python" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    echo "[INSTALL] venv: created at $VENV_DIR (python=$("$VENV_DIR/bin/python" --version))"
else
    echo "[INSTALL] venv: exists (python=$("$VENV_DIR/bin/python" --version))"
fi

# 3. Install deps
"$VENV_DIR/bin/pip" install --quiet --upgrade -r "$REQ_FILE"
echo "[INSTALL] deps: installed from $REQ_FILE"

# 4. Backup crontab
backup_file="$STATE_DIR/crontab.bak.$(date +%s)"
if crontab -l 2>/dev/null > "$backup_file"; then
    backup_size=$(stat -c '%s' "$backup_file")
    echo "[INSTALL] crontab_backup: saved $backup_file (${backup_size} bytes)"
else
    : > "$backup_file"
    echo "[INSTALL] crontab_backup: no existing crontab; empty backup at $backup_file"
fi

# 5. Strip prior watchdog entries — remove anything between the marker comment
#    and the next blank line OR end of file. Idempotent.
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

# 6. Append rendered template
echo "" >> "$tmp_new"
sed -e "s|{{REPO_DIR}}|$REPO_DIR|g" \
    -e "s|{{HOME}}|$HOME|g" \
    "$TEMPLATE" >> "$tmp_new"

crontab "$tmp_new"
echo "[INSTALL] crontab: installed tradeflow-watchdog entries"

# 7. Self-test
echo "[INSTALL] self_test: invoking $WATCHDOG_PY --mode=self-test"
if "$VENV_DIR/bin/python" "$WATCHDOG_PY" --mode=self-test; then
    echo "[INSTALL] self_test: passed — Telegram message should arrive shortly"
    echo "[INSTALL] done"
    exit 0
else
    rc=$?
    echo "[INSTALL] self_test: FAILED (rc=$rc) — check $STATE_DIR/watchdog.log" >&2
    echo "[INSTALL] crontab entries were still installed. To remove: $REPO_DIR/scripts/uninstall_watchdog.sh"
    exit "$rc"
fi
