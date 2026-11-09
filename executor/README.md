# Module 5: Rust Executor

Listens on `/tmp/mev-streamer.sock` for newline-delimited JSON signals, builds pump.fun
**v3** trades (`buy_exact_quote_in_v3` / `sell_v3`, 17 accounts, per the official IDL of
2026-10), fans one signed transaction out to Helius Sender + Jito + RPC, and manages exits
(TP / SL / timeout).

```bash
cp ../.env.example ../.env      # or run from the repo root; dotenvy finds ../.env
cargo build --release
./target/release/mev-streamer-executor                    # serve signals
./target/release/mev-streamer-executor --selfcheck MINT   # verify buy/sell + quotes vs mainnet simulation
./target/release/mev-streamer-executor --probe-relays     # relay endpoints accept the format (spends nothing)
./target/release/mev-streamer-executor --inspect MINT     # quote chain of a curve
./target/release/mev-streamer-executor --new-wallet FILE  # fresh hot wallet (secret → FILE, mode 600)
```

Signal (one line):
`{"mint":"...pump","ticker":"X","action":"buy","confidence":0.85,"source":"both","context":"...","timestamp":1699999999.1}`
`action` may also be `"sell"` to force-exit an open position.

Reply: `{"status":"ok","bundle_id":"<signature>"}` or `{"status":"error","message":"..."}` (`"dry-run"` in dry-run mode).

**DRY_RUN=true:** builds the real transaction and simulates it on mainnet (`sigVerify:false`;
without a key it simulates as `SIM_PAYER`, a funded system wallet), logs CU used and tokens
received, opens a *paper* position and runs the same TP/SL/timeout logic.

**Landing & reconciliation.** A tx is "not landed" only once the block height passes its
blockhash's `lastValidBlockHeight` (never on a wall-clock timeout); status counts at
`confirmed`; a relay error after signing does not release the slot. Cost and proceeds are
the payer's real lamport delta from `getTransaction`.

**Exits** on position value (fee-aware sell quote + ATA rent back) vs real cost: TP, SL,
trailing stop (armed at 1.2×), timeout; sells the whole balance and closes the ATA; slippage
escalates to `EXIT_SLIPPAGE_PCT` for urgent exits and retries.

**Risk limits** before every buy: signal age, confidence, session loss cap, consecutive
losses, SOL reserve, market cap, no re-buy of a traded mint.

**State** in `STATE_DIR` (`logs/`): `positions.json` (atomic snapshot, reconciled with
on-chain balances on a live start; pending buys are awaited) and `trades.jsonl` (events).
Dry runs use `*.dry.*`. A second executor on the same socket refuses to start.

**Verified 2026-11-09 on mainnet:** buy quote == simulated tokens received (exact); one
simulated tx buy → sell all → close ATA: payer delta == quote (exact, 3 mayhem coins);
buy 70–75k CU, sell 46k CU; Sender, Jito and RPC accept the submission format; journal
restore + single-instance guard with an unfunded live key. A funded live trade has not been run.

Not supported (rejected with a clear message): non-SOL-quoted curves (observed chains all
end in xStocks, unreachable from SOL), cashback coins, buys that would cross into the
synthetic migration, sells beyond the curve's real reserves, graduated curves.
