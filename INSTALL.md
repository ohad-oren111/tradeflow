# TradeFlow Install Guide

Paper-mode-first setup. No live trading until a minimum of **30 paper days** and **50 paper trades** have completed.

## 1. Prerequisites

- Python 3.11+ (3.11.x recommended; pyproject pins `>=3.11,<3.13`)
- Docker (for IB Gateway in Phase 1+)
- An Interactive Brokers paper account with futures permissions (CME / MNQ)
- Linux host (Ubuntu 22.04 LTS tested) or macOS for local dev

## 2. Clone and install

```bash
git clone https://github.com/ohad-oren111/tradeflow.git
cd tradeflow
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

## 3. Secrets

Secrets live **outside** the repo:

```bash
mkdir -p /home/$USER/.tradeflow-secrets
cp .env.example /home/$USER/.tradeflow-secrets/.env
# edit /home/$USER/.tradeflow-secrets/.env with your IBKR + Supabase + Telegram values
```

Symlink or `export ENV_FILE=...` from the project root as needed.

## 4. IB Gateway (paper mode first)

Run IB Gateway in **paper mode** — port `4002`. Never point at the live port (`4001`) until the paper graduation criteria are met.

## 5. Verify

```bash
pytest -q
```

All Phase 0 config tests should pass.

## 6. Run

```bash
python main.py --paper
```

In Phase 0 this only confirms the scaffold parses; the live trading loop lands in Phase 3 PR 6+.

## Notes

- No historical price data is included in this repo. Backtests (Phase 6) require your own data source.
- The Supabase `SUPABASE_SERVICE_ROLE_KEY` bypasses Row-Level Security — keep it strictly out of git and out of logs.
