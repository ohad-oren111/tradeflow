# S15 — Corporate spinoff / forced index unwind — UNTESTABLE-HERE

**Verdict: UNTESTABLE-HERE (membership-data gate). Not run.**

Spinoff event dates are derivable from EDGAR Form 10 filings, but the strategy's mechanism
is the FORCED-SELLER leg: index funds dumping the spun-off entity because it is not in the
parent's index. Testing that requires historical index-membership data (was the parent in
the S&P 500 / Russell 1000 at spin date? was the spinco excluded?) plus when-issued /
first-trading-day prices. Index membership history is licensed data (S&P/FTSE); free
reconstructions are unreliable exactly at the small/mid boundary where the effect lives.
Running the event study without the membership split would test "spinoffs drift" — a
different, mushier hypothesis than the pre-registered one — so it is not run.

**Unlock:** historical index-membership/constituent data + a spinoff event list (or a
combined commercial event database).
