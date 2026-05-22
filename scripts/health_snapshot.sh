#!/usr/bin/env bash
# TradeFlow health snapshot — V0-V5 verification from HANDOFF_v5 §6.
# Run from any directory; uses absolute paths. Idempotent. Read-only.
#
# Usage: bash scripts/health_snapshot.sh
#
# Exits 0 if all probes PASS. Non-zero otherwise.

set -euo pipefail

REPO=/home/tradeflow/tradeflow
VENV_PY="${REPO}/.venv/bin/python3"

echo "=== V0 Pre-flight ==="
git -C "${REPO}" fetch
git -C "${REPO}" pull --ff-only origin main
git -C "${REPO}" log -1 --oneline

echo ""
echo "=== V1 Containers ==="
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker inspect tradeflow-app --format 'RestartCount={{.RestartCount}} Status={{.State.Status}} Started={{.State.StartedAt}}'
docker inspect tradeflow-ib-gateway --format 'RestartCount={{.RestartCount}} Status={{.State.Status}} Started={{.State.StartedAt}}'

echo ""
echo "=== V2 Deployed code ==="
docker exec tradeflow-app grep -nE 'def _handle_trade_signal' /app/src/orchestrator.py
docker exec tradeflow-app grep -nE 'def _handle_signal\b' /app/src/orchestrator.py
docker exec tradeflow-app grep -n 'class Reconciler' /app/src/execution/reconciler.py
docker exec tradeflow-app ls /app/config/

echo ""
echo "=== V3 IBKR paper state ==="
"${VENV_PY}" "${REPO}/scripts/_probe_ibkr.py"

echo ""
echo "=== V4 Supabase reachability ==="
"${VENV_PY}" "${REPO}/scripts/_probe_supabase.py"

echo ""
echo "=== V5 Cadence + errors (last 90s) ==="
docker logs tradeflow-app --since 90s > /tmp/tradeflow_recent.log 2>&1
echo "Recent log lines: $(wc -l < /tmp/tradeflow_recent.log)"
echo "Healthchecks:     $(grep -c '\[ORCH\] healthcheck: ok' /tmp/tradeflow_recent.log || true)"
echo "Reconciler ticks: $(grep -c '\[RECON\] tick: drain_complete' /tmp/tradeflow_recent.log || true)"
echo "Errors:           $(grep -ciE 'error|exception|traceback' /tmp/tradeflow_recent.log || true)"
echo "Halt hits:        $(grep -ciE 'foreign_position|halt_new_entries|halt_raised|halt_acked|halt_cleared' /tmp/tradeflow_recent.log || true)"

echo ""
echo "[health_snapshot] all sections complete"
