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
```

Signal (one line):
`{"mint":"...pump","ticker":"X","action":"buy","confidence":0.85,"source":"both","context":"...","timestamp":1699999999.1}`
`action` may also be `"sell"` to force-exit an open position.

Reply: `{"status":"ok","bundle_id":"<signature>"}` or `{"status":"error","message":"..."}` (`"dry-run"` in dry-run mode).

**DRY_RUN=true:** builds the real transaction and simulates it on mainnet (`sigVerify:false`;
without a key it simulates as `SIM_PAYER`, a funded system wallet), logs CU used and tokens
received, opens a *paper* position and runs the same TP/SL/timeout logic.

**Verified 2026-11-09 on mainnet:** buy quote == simulated tokens received (exact), sell quote
== simulated lamports received (exact); buy 69.7k–73.2k CU, sell 46.2k CU; Sender, Jito
(global + NY) and RPC accept the submission format. A funded live trade has not been run.

Not supported (rejected with a clear message): non-SOL-quoted curves, cashback coins, buys
that would cross into the synthetic migration, graduated curves.
