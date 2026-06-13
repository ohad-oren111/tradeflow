# S08 — Commodity index-roll spread (GSCI/BCOM window) — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (curve-data gate, shared with S07). Not run.**

Roll-window trades are calendar-spread trades: they need prices for BOTH the front and the
deferred contract month through the GSCI/BCOM roll window (5th–9th business day), for
expired historical months. The same Stage-0 probes that closed S07 close this: Yahoo purges
expired contract months (`CLZ20.NYM` → no data), Stooq is JS-challenge-walled, and no other
free per-month historical source was found. Continuous/front-month series cannot see a
calendar spread by construction.

**Unlock:** identical to S07 — paid per-contract-month historical settles. S08 unblocks
automatically the day S07's data lands.
