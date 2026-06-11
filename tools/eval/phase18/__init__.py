"""Phase-18 — TF foundational edge audit.

Holds TF's ACTUAL gated strategy (the live SMA100-bounce long entry + 30m-EMA200
regime gate + trailing-ratchet exit) to the SAME pre-registered honest bar that
returned NONE for every Phase-15/16/17 candidate. Drives the REAL prod entry/exit
code (``tools.eval.engine`` over ``tools.eval.data``), scores it with the Phase-16
cost model + metrics + gates, and decomposes gated vs ungated to isolate what the
gate actually adds. Pure research — READ-ONLY, no ``src/`` change, no deploy.
"""
