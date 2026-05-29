# TradeFlow Phase 1 — orchestrator app image.
#
# Python 3.11-slim base. Installs runtime deps from pyproject.toml. Runs main.py
# which wires IBClient + SupabaseClient into the orchestrator loop.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY src ./src
COPY config ./config
COPY comms ./comms
COPY dashboard ./dashboard
# W-S14.2 Track 6e — ship scripts/ so inspect_decisions.py (and siblings) run
# in-container instead of host-only.
COPY scripts ./scripts
COPY main.py ./main.py

EXPOSE 8080

# PR #12 — PID-1 liveness probe so `docker ps` reports (healthy)/(unhealthy)
# instead of just `Up <time>`. The orchestrator runs as PID 1 (no init wrapper),
# so a Python kernel + a /proc/1 cmdline check is sufficient to catch the
# process-died case. procps/pgrep is not in python:3.11-slim by default; we
# stay slim by using stdlib only. Stronger liveness (asyncio-loop heartbeat,
# IB-Gateway round-trip) is a PR #13+ follow-up.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import sys, pathlib; sys.exit(0 if 'main.py' in pathlib.Path('/proc/1/cmdline').read_text() else 1)" \
    || exit 1

CMD ["python", "main.py"]
