# S05 — Crypto cash-and-carry basis: BUILD BRIEF (operator go/no-go)

**Status:** research artifact for operator greenlight. NOT a live deployment, NOT a
cross-VPS trading change. Nothing in here is built or deployed autonomously — this is a
go/no-go for a NEW live strategy, which is the operator's call, not a fix.

**Author:** VPS CC, 2026-06-23. Grounds: the Phase-21 S05 gauntlet result
(`tools/eval/phase21_gauntlet/S05_cash_and_carry/`, README + results.json) + a §0.5.97
spec/fee re-verification against Binance source (this session).

---

## 1. The mechanical thesis (why this is an arb, not a bet)

Delta-neutral **cash-and-carry** on Binance crypto:

- **Enter** (≥ ~60 calendar days before a quarterly expiry, when the basis is wide):
  - BUY spot BTC (or ETH).
  - SELL the dated quarterly future (COIN-M, same notional) at its premium to spot.
  - Net position is delta-neutral: you own the coin and are short an equal claim on it.
- **Hold to (near) convergence.** A dated future *must* converge to spot at delivery —
  Binance settles open positions at the index average over 07:30–08:00 UTC on the last
  Friday of Mar/Jun/Sep/Dec (verified against source, §6). The premium you sold at is
  therefore captured **near-deterministically**, independent of where BTC goes: spot up
  → spot leg gains / short future loses equally; spot down → vice-versa; the **basis**
  (futures − spot at entry) is what you keep.
- **Exit** just before the settlement-day stub (the gauntlet's data-integrity fix: close
  on the last full bar before settlement, leaving the final ~0.3% on the table — more
  conservative).

The return is the annualized entry basis minus round-trip fees minus the cost of the
capital/margin tied up. It is the textbook compensation for (a) locking up capital for a
quarter and (b) bearing the liquidation/convexity risk of the inverse short future if
the basis WIDENS before it converges. It is genuine, well-known, capacity-constrained,
and it persists.

## 2. Why the n≥200 gate is the WRONG test here (§0.5.230)

The Phase-21 harness returned **NONE** for S05 — but for a *structural*, not an economic,
reason, and §0.5.230 names exactly this case ("the n≥200 gate is the WRONG test for
contractual/mechanical arbs (S05-class); don't fail a real mechanical edge on sample
size alone"):

- Crypto quarterlies expire **4×/year**. All of history holds only ~21 expired BTC + ~21
  expired ETH contracts → **n = 14–26 per variant**, structurally far below 200. You
  cannot manufacture more independent episodes; the universe is the universe.
- The n≥200 and Deflated-Sharpe gates are built to catch an *overfit signal strategy*
  fishing a large sample. A mechanical convergence arb has no signal to overfit — the
  edge is a contractual identity (the future converges), evidenced by **rho = 1.0
  monotonicity** (PnL scales with entry basis) and **zero losing episodes in six years**
  for the threshold variants. Sample size is the wrong axis to judge it on.
- The correct test for a mechanical arb is: *does the entry basis reliably exceed the
  all-in cost, with margin, including in the adverse regime?* S05 answers yes on every
  count (§3).

## 3. The gauntlet evidence (what's already proven)

From the frozen S05 result (champion `roll60`, cost-derived, no tuning):

| | TRAIN (expiry <2024) | HOLDOUT (expiry ≥2024, post-spot-ETF) |
|---|---|---|
| n | 26 | 20 |
| PF | 163.2 | 34.2 |
| net | +$30,989 | +$18,580 |
| win rate | 92% | 90% |
| median annualized net carry | 3.16% | **6.69%** |

All five kill tests **PASS**: K1 the carry **survived** the 2024+ spot-ETF basis
compression (unlike Phase-20c funding carry, which inverted); K2 strip COVID / May-2021
delever / FTX and normal-regime carry still clears costs (+$968/episode); K3 monotonic
rho 1.0; **K4 holdout PF stays ≥1.3 out to 7× costs** (enormous fee margin); K5
convergence integrity confirmed (max |F−S|/S = 0.75%). Threshold variants thr5/10/15
posted **100% win, 0 losers in 6 years**.

This is the cleanest mechanical result of the entire P15–21 gauntlet. The "NONE" is the
harness honestly refusing to certify on 20 holdout observations — not evidence against
the edge.

## 4. Execution venue, instruments, mechanics

- **Venue:** the Binance VPS (separate from this MNQ/IBKR box). No change to the IBKR
  account, the MNQ bot, or any existing live system. S05 is a self-contained new book.
- **Instruments:** Binance **COIN-M dated quarterly** futures (BTCUSD_<YYMMDD>,
  ETHUSD_<YYMMDD>) short leg + **spot** BTC/ETH long leg. COIN-M (coin-margined) matches
  the gauntlet; the short future is an inverse contract → **convexity** (labeled-ignored
  in the backtest; must be managed live, see §5).
- **Sizing:** equal notional per leg, delta-neutral. The gauntlet modeled $100k notional
  / contract; live should start far smaller (§7).
- **Entry rule (champion roll60):** on the last bar ≥60 cal-days before expiry, enter IF
  the first-day annualized basis beats the cost hurdle. Pure, cost-derived, no tuning.
  Optionally the thr10/thr15 variants (only enter if annualized basis ≥10/15%) trade
  less often but with a fatter margin (100% historical win rate).

## 5. Capital & risk (the real exposures, stated plainly)

- **Liquidation risk on the short inverse future (the #1 live risk).** If the basis
  WIDENS after entry (future runs further above spot — a delever/squeeze), the short
  future shows an unrealized loss and can be liquidated *before* the convergence that
  would make you whole. The spot long offsets the economic loss but is on a **different
  margin pool** — Binance will not auto-net spot against a COIN-M future. Mitigation:
  hold the short with **low leverage (≤2×)** and a large coin-margin buffer; size so a
  +30–40% transient basis blowout cannot liquidate. This is the convexity the backtest
  labeled-ignored and is the binding live constraint.
- **Capital lockup.** Capital is tied for up to a quarter per episode; the 3–7%
  annualized net carry is the *compensation* for that, not free money. Returns are on
  deployed-and-locked capital, not annualized-on-paper.
- **Counterparty / venue risk.** Funds sit on Binance for the hold. Cap exposure;
  this is uninsured exchange risk.
- **Capacity.** Crowded trade; the basis IS the crowd's clearing price. Small size only.
- **Roll/settlement mechanics.** Must exit on the last full bar before the 08:00 UTC
  settlement stub (data-integrity fix) or eat the partial-bar artifact.

## 6. Fee/spec re-verification against source (§0.5.97)

Verified against Binance support this session (2026-06):
- Quarterly cycle **Mar/Jun/Sep/Dec**, settled at the **index average over 07:30–08:00
  UTC** (1,800 1-sec samples) on the delivery date. **Settlement fee = the taker fee.**
- COIN-M futures fees (VIP0) ≈ **0.02% maker / 0.05% taker**; spot taker ≈ **0.10%**
  (≈0.075% with BNB). Round trip across both legs (buy spot + sell future at entry; sell
  spot + settle future at exit) ≈ **~30 bp**, consistent with the gauntlet's $280/RT at
  $100k (28 bp). K4 showed survival to **7× this** — fees are not the binding constraint.

## 7. Concrete go/no-go BUILD PLAN (phased, kill-gated)

If greenlit, build in strict phases — never jump to size:

1. **Phase 0 — paper/observation (2–4 weeks, no capital):** stand up a read-only basis
   monitor on the Binance VPS that logs, per live quarterly contract, the annualized
   basis vs the cost hurdle and would-be roll60/thr10 entries. Confirm the live basis
   matches the backtest's distribution. Deliverable: a daily basis log + a "would-have-
   entered" ledger. **Gate to Phase 1:** live basis ≥ backtest median for ≥1 contract.
2. **Phase 1 — one minimum-size episode (real, tiny):** a single delta-neutral pair at
   the smallest viable notional, ≤2× short leverage, full margin buffer. Instrument the
   liquidation distance continuously. **Gate to Phase 2:** clean convergence capture
   within fee tolerance, no margin scare.
3. **Phase 2 — scale to a few concurrent contracts (BTC+ETH, 2 expiries):** add a
   **kill condition**: auto-flatten BOTH legs if short-leg margin ratio crosses a
   pre-set threshold (basis-blowout circuit-breaker), and a hard per-venue capital cap.
4. **Standing guards:** never paper→live without operator sign-off; never exceed the
   capital cap; never raise leverage above the convexity-safe bound; a basis-blowout
   flatten is always allowed to lose the fee, never the principal.

## Go/no-go recommendation

**GO to Phase 0 (paper monitor)** is low-risk and high-information: it's read-only, costs
nothing, and either confirms the live basis matches the backtest (justifying tiny real
size) or reveals a live/backtest gap before any capital is exposed. **A real-capital GO
(Phase 1+) is the operator's call** and should wait on a clean Phase-0 confirmation — the
backtest is clean but the one thing it labeled-ignored (live inverse-future liquidation
under a basis blowout) is exactly the risk that only live observation retires.

The honest summary: **S05 is the most credible mechanical edge the gauntlet surfaced; it
fails the harness only on a gate (n) that §0.5.230 says doesn't apply to it. It deserves
a paper-monitor greenlight, not a NONE-and-forget.**

## What I got wrong / uncertain
- The backtest **labels-ignores convexity** (the inverse-future second-order term) and
  models margin as if the two legs net — they do not on Binance. The live binding risk
  (short-leg liquidation under a transient basis widening) is therefore *under-*modeled
  in the +6.7% carry figure; Phase 0/1 exist specifically to retire it. I did not build
  the monitor or fetch live current-quarter basis — that is Phase 0 work, post-greenlight.
