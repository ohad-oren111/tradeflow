"""TradeFlow Supabase reachability probe — used by scripts/health_snapshot.sh.

Hits the three tables this project uses (lifecycles, lifecycle_events, halt_acks)
with ``select=*&limit=1`` per §0.5.140 — explicit column names risk 400s when
the migration hasn't been applied yet. Exits 0 if every probe returns HTTP 200.
``halt_acks`` is tolerated as 404 when the PR #12 migration hasn't been applied;
that case is reported but doesn't fail the script.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

TABLES = ("lifecycles", "lifecycle_events", "halt_acks")


async def main() -> int:
    env_file = os.environ.get("ENV_FILE") or "/home/tradeflow/.tradeflow-secrets/.env"
    load_dotenv(env_file)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("[probe_supabase] FAIL — SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing")
        return 1

    base = f"{url.rstrip('/')}/rest/v1"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }

    rc = 0
    async with httpx.AsyncClient(headers=headers, timeout=10.0) as http:
        for table in TABLES:
            r = await http.get(f"{base}/{table}", params={"select": "*", "limit": "1"})
            if r.status_code == 200:
                rows = len(r.json())
                print(f"[probe_supabase] {table} status=200 rows={rows}")
            elif table == "halt_acks" and r.status_code == 404:
                print(
                    f"[probe_supabase] {table} status=404 — migration not applied "
                    "(expected pre-PR-12-migration; not a hard fail)"
                )
            else:
                print(
                    f"[probe_supabase] FAIL {table} status={r.status_code} body={r.text[:200]}",
                    file=sys.stderr,
                )
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
