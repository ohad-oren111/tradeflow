# S13 — Merger arb (small/complex cash deals) — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (deals-data gate). Not run.**

Merger arb needs a historical deal database: announced terms (price/share, cash vs stock),
expected close, actual outcome (closed / broke / re-cut), and timelines. No free clean
source exists. Reconstructing it from EDGAR (DEFM14A / 8-K parsing) would require reliable
outcome labeling — which deal closed, when, at what final terms — and that labeling is
exactly the part EDGAR does not give you cleanly; hand-labeling hundreds of deals is beyond
honest scope on this VPS, and a half-labeled dataset would bias toward deals that closed
(survivorship in the worst possible direction for an arb strategy whose risk IS the break).

**Unlock:** a deals dataset (e.g. SDC/Refinitiv, FactSet, or a curated academic merger
sample with break labels). With it, the eval is straightforward daily-bar work.
