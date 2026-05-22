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
COPY main.py ./main.py

CMD ["python", "main.py"]
