//! mev-streamer executor: IPC signals → pump.fun tx → Jito bundle → position management.

use std::time::{SystemTime, UNIX_EPOCH};

pub fn log_line(color: &str, tag: &str, msg: &str) {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let secs = now.as_secs();
    println!(
        "\x1b[90m{:02}:{:02}:{:02}.{:03}\x1b[0m {color}{tag:<5}\x1b[0m {msg}",
        (secs / 3600) % 24,
        (secs / 60) % 60,
        secs % 60,
        now.subsec_millis()
    );
}

macro_rules! log_info  { ($($a:tt)*) => { $crate::log_line("\x1b[36m",   "INFO",  &format!($($a)*)) } }
macro_rules! log_ok    { ($($a:tt)*) => { $crate::log_line("\x1b[32;1m", "OK",    &format!($($a)*)) } }
macro_rules! log_warn  { ($($a:tt)*) => { $crate::log_line("\x1b[33m",   "WARN",  &format!($($a)*)) } }
macro_rules! log_error { ($($a:tt)*) => { $crate::log_line("\x1b[31;1m", "ERROR", &format!($($a)*)) } }
macro_rules! log_trade { ($($a:tt)*) => { $crate::log_line("\x1b[35;1m", "TRADE", &format!($($a)*)) } }
macro_rules! log_dim   { ($($a:tt)*) => { $crate::log_line("\x1b[90m",   "POS",   &format!($($a)*)) } }

mod bonding;
mod config;
mod ipc;
mod jito;
mod journal;
mod pda;
mod position;
mod rpc;
mod selfcheck;
mod sender;
mod tx_builder;
mod types;

use bonding::{apply_slippage, BondingCurve, FeeConfig, Global};
use config::Config;
use jito::JitoClient;
use pda::MintAccounts;
use position::{short, Position, PositionManager};
use rpc::Rpc;
use serde_json::json;
use sender::{LandStatus, Submitter};
use solana_sdk::pubkey;
use solana_sdk::pubkey::Pubkey;
use solana_sdk::signature::Keypair;
use solana_sdk::signer::Signer;
use std::str::FromStr;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};
use tx_builder::Budget;
use types::{fmt_sol, fmt_sol_u, Action, ExitReason, Response, Result, Signal};

/// Base fee of a one-signature tx.
const SIG_FEE: u64 = 5_000;
/// Rent of a token ATA (Token-2022 with ImmutableOwner, 170 bytes; SPL is 2_039_280).
/// Only used as an estimate — the real rent is read back from the account.
const ATA_RENT_ESTIMATE: u64 = 2_074_080;
const STATE_REFRESH: Duration = Duration::from_secs(30);
/// Dry runs simulate as a well-funded wallet (sigVerify off) when no key is configured,
/// so the pump program actually runs instead of failing on an empty fee payer.
pub const DEFAULT_SIM_PAYER: Pubkey = pubkey!("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9");

pub struct Executor {
    pub cfg: Config,
    pub keypair: Keypair,
    /// True when WALLET_PRIVATE_KEY was empty (dry run only).
    pub ephemeral: bool,
    pub sim_payer: Pubkey,
    pub rpc: Arc<Rpc>,
    pub submitter: Arc<Submitter>,
    pub positions: PositionManager,
    state: RwLock<(Global, FeeConfig)>,
}

/// Mint `supply` (offset 36) — same for SPL Token and Token-2022.
pub fn mint_supply(data: &[u8]) -> Result<u64> {
    data.get(36..44).map(|b| u64::from_le_bytes(b.try_into().unwrap())).ok_or_else(|| "mint account too short".into())
}

pub async fn fetch_state(rpc: &Rpc) -> Result<(Global, FeeConfig)> {
    let accs = rpc.get_multiple_accounts(&[pda::PUMP_GLOBAL, pda::PUMP_FEE_CONFIG]).await?;
    let g = accs[0].as_ref().ok_or("pump Global account not found")?;
    let f = accs[1].as_ref().ok_or("pump FeeConfig account not found")?;
    if g.owner != pda::PUMP_PROGRAM || f.owner != pda::PUMP_FEES_PROGRAM {
        return Err("Global/FeeConfig owned by unexpected programs".into());
    }
    let global = Global::parse(&g.data)?;
    let fee_config = FeeConfig::parse(&f.data)?;
    if fee_config.fee_tiers.is_empty() {
        return Err("FeeConfig has no fee tiers".into());
    }
    if global.buyback_fee_recipients.is_empty() {
        return Err("Global lists no buyback fee recipients".into());
    }
    Ok((global, fee_config))
}

impl Executor {
    pub fn pump_state(&self) -> (Global, FeeConfig) {
        self.state.read().unwrap().clone()
    }

    fn spawn_state_refresher(self: &Arc<Self>) {
        let me = Arc::clone(self);
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(STATE_REFRESH);
            tick.tick().await;
            loop {
                tick.tick().await;
                match fetch_state(&me.rpc).await {
                    Ok(st) => {
                        let mut cur = me.state.write().unwrap();
                        if *cur != st {
                            log_warn!("pump fee schedule changed: {:?}", st.1.fee_tiers.first().map(|t| t.fees));
                        }
                        *cur = st;
                    }
                    Err(e) => log_warn!("pump state refresh failed: {e}"),
                }
            }
        });
    }

    fn jito_tip(&self) -> u64 {
        self.cfg.jito_tip.resolve(self.submitter.jito.tip_floor())
    }

    fn tips(&self) -> Vec<(Pubkey, u64)> {
        self.submitter.tips(self.jito_tip())
    }

    pub async fn handle_signal(self: Arc<Self>, sig: Signal) -> Response {
        let t0 = Instant::now();
        let lag_ms = signal_lag_ms(sig.timestamp);
        log_info!(
            "signal {:?} ${} {} conf={:.2} src={} lag={}ms | {}",
            sig.action, sig.ticker, sig.mint, sig.confidence, sig.source, lag_ms, sig.context
        );
        let mint = match Pubkey::from_str(sig.mint.trim()) {
            Ok(m) => m,
            Err(_) => return Response::error(format!("invalid mint address: {}", sig.mint)),
        };
        let res = match sig.action {
            Action::Buy => self.buy(mint, &sig).await,
            Action::Sell => self.manual_sell(mint).await,
        };
        match res {
            Ok(id) => {
                log_info!("signal handled in {}µs", t0.elapsed().as_micros());
                Response::ok(id)
            }
            Err(e) => {
                log_warn!("signal {} rejected: {e}", short(&mint));
                Response::error(e.to_string())
            }
        }
    }

    /// Signal-level filters + session circuit breakers (cheap, before any I/O).
    fn pre_checks(&self, sig: &Signal) -> Result<()> {
        if sig.confidence < self.cfg.min_confidence {
            return Err(format!("confidence {:.2} < MIN_CONFIDENCE {:.2}", sig.confidence, self.cfg.min_confidence).into());
        }
        let lag = signal_lag_ms(sig.timestamp);
        if self.cfg.max_signal_age_ms > 0 && lag > self.cfg.max_signal_age_ms as i64 {
            return Err(format!("stale signal: {lag}ms old > MAX_SIGNAL_AGE_MS {}", self.cfg.max_signal_age_ms).into());
        }
        self.positions.risk_check(&self.cfg)?;
        Ok(())
    }

    async fn buy(self: &Arc<Self>, mint: Pubkey, sig: &Signal) -> Result<String> {
        self.pre_checks(sig)?;
        // Claim the slot before any I/O so concurrent signals can't exceed max_positions.
        self.positions.try_reserve(mint, &sig.ticker, self.cfg.rebuy)?;
        // Contract: buy_inner only returns Err *before* a tx was signed — after that the
        // confirm task owns the slot, so releasing here can never orphan a landed buy.
        let res = self.buy_inner(mint, sig).await;
        if res.is_err() {
            self.positions.release(&mint);
        }
        res
    }

    /// Priority fee for a tx write-locking `curve`: live p75 (auto) or fixed.
    async fn cu_price(&self, curve: Pubkey) -> u64 {
        match self.cfg.priority_fee {
            config::Auto::Fixed(v) => v,
            auto => match self.rpc.priority_fee_p75(&[curve]).await {
                Ok(live) => auto.resolve(live),
                Err(e) => {
                    log_warn!("priority fee estimate failed ({e}); using minimum");
                    auto.resolve(0)
                }
            },
        }
    }

    /// Wallet SOL balance (live only; dry runs don't need it).
    async fn wallet_balance(&self) -> Result<u64> {
        if self.cfg.dry_run {
            return Ok(u64::MAX);
        }
        self.rpc.balance(&self.keypair.pubkey()).await
    }

    async fn buy_inner(self: &Arc<Self>, mint: Pubkey, sig: &Signal) -> Result<String> {
        let t0 = Instant::now();
        let curve_key = pda::bonding_curve(&mint);

        // One round-trip of latency: accounts ‖ priority fee ‖ (cached) blockhash ‖ balance.
        let keys = [curve_key, mint];
        let (accs, cu_price, blockhash, balance) = tokio::join!(
            self.rpc.get_multiple_accounts(&keys),
            self.cu_price(curve_key),
            self.rpc.latest_blockhash(),
            self.wallet_balance()
        );
        let (accs, blockhash, balance) = (accs?, blockhash?, balance?);
        let fetch_ms = t0.elapsed().as_millis();

        let curve_acc = accs[0].as_ref().ok_or("bonding curve not found (not a pump.fun mint?)")?;
        if curve_acc.owner != pda::PUMP_PROGRAM {
            return Err(format!("bonding curve owned by {}, not pump.fun", curve_acc.owner).into());
        }
        let mint_acc = accs[1].as_ref().ok_or("mint account not found")?;
        let token_program = mint_acc.owner;
        if token_program != pda::TOKEN_PROGRAM && token_program != pda::TOKEN_2022_PROGRAM {
            return Err(format!("mint owned by unknown program {token_program}").into());
        }
        let supply = mint_supply(&mint_acc.data)?;
        let curve = BondingCurve::parse(&curve_acc.data)?;
        if curve.complete {
            return Err("token graduated (bonding curve complete)".into());
        }
        if !curve.is_sol_quoted() {
            // Seen on mainnet (2026-11): such chains end in xStocks (Xs… Token-2022 mints),
            // which no pump route can reach from SOL. `--inspect <MINT>` shows the chain.
            return Err(format!("curve is quoted in {} (not SOL) — not reachable from SOL; see --inspect", curve.quote_mint).into());
        }
        if curve.is_cashback_coin {
            return Err("cashback coin: v3 trades refuse it (CashbackCoinNotSupported)".into());
        }
        // Mayhem coins: verified by --selfcheck round trip (buy → sell all → close) on
        // mainnet 2026-11-09. They get no synthetic migration; a buy larger than what is
        // left fails (NotEnoughTokensToBuy) — fine, nothing is spent.
        if !curve.has_liquidity() {
            return Err("bonding curve has no liquidity".into());
        }
        let mcap = curve.market_cap(supply);
        if self.cfg.max_mcap_lamports > 0 && mcap > self.cfg.max_mcap_lamports {
            return Err(format!("market cap {} > MAX_MCAP_SOL {}", fmt_sol_u(mcap), fmt_sol_u(self.cfg.max_mcap_lamports)).into());
        }

        let (global, fees) = self.pump_state();
        let amount_in = self.cfg.buy_lamports;
        let q = bonding::buy_exact_sol_in(&global, &fees, &curve, supply, amount_in)?;
        let min_out = apply_slippage(q.tokens_out, self.cfg.slippage_pct);
        let entry_price = curve.after_buy(q.net_in, q.tokens_out).price();
        let buyback = jito::pick(&global.buyback_fee_recipients);
        let budget = Budget { cu_limit: self.cfg.cu_limit_buy, cu_price_micro_lamports: cu_price };
        let tips = self.tips();
        let tip_total: u64 = tips.iter().map(|t| t.1).sum();
        let cost = q.debit + tip_total + tx_builder::priority_fee_lamports(budget) + SIG_FEE;
        let need = cost + ATA_RENT_ESTIMATE + self.cfg.min_sol_reserve_lamports;
        if balance < need {
            return Err(format!("wallet balance {} < needed {} (buy + fees + rent + MIN_SOL_RESERVE)", fmt_sol_u(balance), fmt_sol_u(need)).into());
        }
        let expected = Position {
            mint, ticker: sig.ticker.clone(), entry_price, token_amount: q.tokens_out, cost_sol: cost,
            mint_supply: supply, entry_time: Instant::now(), entry_unix: journal::unix_now(),
            accounts: MintAccounts::new(mint, &self.keypair.pubkey(), token_program),
            peak_value: 0, rent_lamports: 0, exit_attempts: 0, buy_sig: String::new(),
            paper_net_in: if self.cfg.dry_run { q.net_in } else { 0 },
        };

        if self.cfg.dry_run {
            log_trade!(
                "WOULD BUY ${} {} — {} → ~{} tokens (min {}, fees {} lamports, slip {}%, tips {} lamports, prio {}µL/CU, mcap {}) [state {}ms]",
                sig.ticker, mint, fmt_sol_u(amount_in), fmt_tokens(q.tokens_out), fmt_tokens(min_out),
                q.fees, self.cfg.slippage_pct, tip_total, cu_price, fmt_sol_u(mcap), fetch_ms
            );
            let payer = if self.ephemeral { self.sim_payer } else { self.keypair.pubkey() };
            let sim_accounts = MintAccounts::new(mint, &payer, token_program);
            let ixs = tx_builder::buy_instructions(&payer, &sim_accounts, &buyback, amount_in, min_out, budget, &tips);
            self.log_simulation(tx_builder::unsigned(&payer, &ixs, blockhash.hash)?, budget.cu_limit, &sim_accounts).await;
            // Paper position so TP/SL/timeout logic and stats can be exercised.
            self.journal_event("buy", json!({"mint": mint.to_string(), "ticker": sig.ticker, "sig": "dry-run",
                "tokens": q.tokens_out, "cost": cost, "mcap": mcap, "source": sig.source, "confidence": sig.confidence,
                "lag_ms": signal_lag_ms(sig.timestamp), "dry_run": true}));
            self.positions.open(expected);
            return Ok("dry-run".into());
        }

        let user = self.keypair.pubkey();
        let accounts = expected.accounts;
        let ixs = tx_builder::buy_instructions(&user, &accounts, &buyback, amount_in, min_out, budget, &tips);
        let tx = tx_builder::sign(&self.keypair, &ixs, blockhash.hash)?;
        let b64 = tx_builder::encode_base64(&tx)?;
        let signature = tx.signatures[0].to_string();
        // ---- point of no return: from here the confirm task owns the slot.
        self.positions.set_pending(&mint, signature.clone(), expected.clone());
        self.journal_event("buy_sent", json!({"mint": mint.to_string(), "ticker": sig.ticker, "sig": signature,
            "amount_in": amount_in, "min_out": min_out, "tips": tip_total, "cu_price": cu_price, "mcap": mcap,
            "source": sig.source, "confidence": sig.confidence, "lag_ms": signal_lag_ms(sig.timestamp)}));
        match self.submitter.submit(b64).await {
            Ok(_) => log_trade!(
                "BUY SENT ${} {} — {} min_out {} tips {} lamports prio {}µL/CU sig {} [signal→send {}ms]",
                sig.ticker, short(&mint), fmt_sol_u(amount_in), fmt_tokens(min_out), tip_total,
                cu_price, signature, t0.elapsed().as_millis()
            ),
            // A relay error (e.g. HTTP timeout) does not prove the tx wasn't forwarded.
            Err(e) => log_warn!("BUY submit uncertain ${} {}: {e} — reconciling on-chain", sig.ticker, short(&mint)),
        }
        let me = Arc::clone(self);
        let s = signature.clone();
        tokio::spawn(async move { me.confirm_buy(s, blockhash.last_valid_height, expected).await });
        Ok(signature)
    }

    /// Token ATA at `confirmed`, retried: (amount, rent lamports) once it holds tokens.
    async fn read_ata(&self, ata: &Pubkey, tries: u32) -> Option<(u64, u64)> {
        for i in 0..tries {
            if i > 0 {
                tokio::time::sleep(Duration::from_millis(400)).await;
            }
            match self.rpc.get_multiple_accounts_at(&[*ata], "confirmed").await {
                Ok(v) => {
                    if let Some(acc) = v.into_iter().next().flatten() {
                        let amt = acc.token_amount().unwrap_or(0);
                        if amt > 0 {
                            return Some((amt, acc.lamports));
                        }
                    }
                }
                Err(e) => log_warn!("ATA read failed: {e}"),
            }
        }
        None
    }

    /// Real lamport delta of a landed tx (retried while the RPC indexes it).
    async fn landed_delta(&self, sig: &str) -> Option<i128> {
        for i in 0..6 {
            if i > 0 {
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
            match self.rpc.payer_delta(sig).await {
                Ok(Some(d)) => return Some(d),
                Ok(None) => {}
                Err(e) => log_warn!("getTransaction {}…: {e}", &sig[..8.min(sig.len())]),
            }
        }
        None
    }

    pub async fn confirm_buy(&self, signature: String, last_valid_height: u64, expected: Position) {
        let t0 = Instant::now();
        let (mint, ticker) = (expected.mint, expected.ticker.clone());
        let status = self.submitter.wait_for_landing(&signature, last_valid_height).await;
        let landed = matches!(status, LandStatus::Landed { .. });
        // Landed → tokens must be there (retry through RPC lag). Otherwise one careful
        // look: an "expired/unknown" tx that did land would show a balance.
        let ata = self.read_ata(&expected.accounts.user_ata, if landed { 6 } else { 2 }).await;
        let (token_amount, rent) = match (landed, ata) {
            (_, Some(x)) => x,
            (true, None) => {
                log_warn!("buy landed but balance not visible yet — tracking expected amount");
                (expected.token_amount, ATA_RENT_ESTIMATE)
            }
            (false, None) => {
                match &status {
                    LandStatus::Unknown => log_error!(
                        "BUY UNKNOWN ${ticker} {} — RPC never confirmed {signature}; no tokens visible, releasing. VERIFY MANUALLY.",
                        short(&mint)
                    ),
                    s => log_error!("BUY FAILED ${ticker} {} — {s:?}", short(&mint)),
                }
                self.journal_event("buy_failed", json!({"mint": mint.to_string(), "ticker": ticker, "sig": signature, "status": format!("{status:?}")}));
                self.positions.release(&mint);
                return;
            }
        };
        let cost = match self.landed_delta(&signature).await {
            Some(d) if d < 0 => (-d) as u64,
            _ => expected.cost_sol + rent,
        };
        log_ok!(
            "BUY LANDED ${ticker} {} — {} tokens for {} ({:?}, confirmed in {}ms)",
            short(&mint), fmt_tokens(token_amount), fmt_sol_u(cost), status, t0.elapsed().as_millis()
        );
        self.journal_event("buy", json!({"mint": mint.to_string(), "ticker": ticker, "sig": signature,
            "tokens": token_amount, "expected_tokens": expected.token_amount, "cost": cost, "rent": rent,
            "confirm_ms": t0.elapsed().as_millis() as u64}));
        self.positions.open(Position {
            token_amount, cost_sol: cost, rent_lamports: rent, entry_time: Instant::now(),
            entry_unix: journal::unix_now(), buy_sig: signature, ..expected
        });
    }

    async fn manual_sell(self: &Arc<Self>, mint: Pubkey) -> Result<String> {
        let pos = self.positions.begin_close(&mint).ok_or("no open position for this mint")?;
        self.exit_position(pos, ExitReason::Manual).await
    }

    /// Caller must have moved the position to Closing via `begin_close`.
    pub async fn exit_position(self: &Arc<Self>, pos: Position, reason: ExitReason) -> Result<String> {
        if reason == ExitReason::Graduated {
            log_error!(
                "${} {} graduated — cannot sell on bonding curve, sell {} tokens on PumpSwap manually. Untracking.",
                pos.ticker, pos.mint, fmt_tokens(pos.token_amount)
            );
            self.journal_event("stranded", json!({"mint": pos.mint.to_string(), "ticker": pos.ticker,
                "tokens": pos.token_amount, "cost": pos.cost_sol, "reason": "graduated"}));
            self.positions.release(&pos.mint);
            return Err("token graduated".into());
        }
        match self.sell(&pos, reason).await {
            Ok(id) => Ok(id),
            Err(e) => {
                log_error!("SELL ERROR ${} {} ({reason}): {e} — will retry", pos.ticker, short(&pos.mint));
                self.positions.abort_close(&pos.mint);
                Err(e)
            }
        }
    }

    async fn sell(self: &Arc<Self>, pos: &Position, reason: ExitReason) -> Result<String> {
        let a = pos.accounts;
        let keys = [a.bonding_curve, a.user_ata];
        // `confirmed`: the token balance decides how much we sell — never trust a lagging node.
        let (accs, cu_price, blockhash) = tokio::join!(
            self.rpc.get_multiple_accounts_at(&keys, "confirmed"),
            self.cu_price(a.bonding_curve),
            self.rpc.latest_blockhash()
        );
        let (accs, blockhash) = (accs?, blockhash?);
        let curve = BondingCurve::parse(&accs[0].as_ref().ok_or("bonding curve missing")?.data)?;
        if curve.complete {
            return Err("curve completed between check and sell".into());
        }
        let (amount, rent_back) = if self.cfg.dry_run {
            (pos.token_amount, 0)
        } else {
            match accs[1].as_ref().map(|acc| (acc.token_amount().unwrap_or(0), acc.lamports)) {
                Some((amt, rent)) if amt > 0 => (amt, rent),
                _ => match self.read_ata(&a.user_ata, 3).await {
                    Some(x) => x,
                    None => {
                        log_warn!(
                            "${} {} has no token balance at confirmed (sold elsewhere / earlier sell landed?) — closing at 0, see journal",
                            pos.ticker, short(&pos.mint)
                        );
                        self.record_close(pos, reason, 0, "empty");
                        return Ok("empty".into());
                    }
                },
            }
        };

        let (global, fees) = self.pump_state();
        let sol_out = bonding::sell_sol_out(&global, &fees, &pos.effective_curve(&curve), pos.mint_supply, amount)?;
        let slip = position::exit_slippage(&self.cfg, reason, pos.exit_attempts);
        let min_sol_out = apply_slippage(sol_out, slip);
        let buyback = jito::pick(&global.buyback_fee_recipients);
        let budget = Budget { cu_limit: self.cfg.cu_limit_sell, cu_price_micro_lamports: cu_price };
        let tips = self.tips();
        let tip_total: u64 = tips.iter().map(|t| t.1).sum();
        let rent_back = if pos.exit_attempts < 2 { rent_back } else { 0 }; // see close_ata below
        let est_proceeds = (sol_out + rent_back) as i128 - (tip_total + tx_builder::priority_fee_lamports(budget) + SIG_FEE) as i128;

        if self.cfg.dry_run {
            log_trade!(
                "WOULD SELL ${} {} [{reason}] — {} tokens for ~{} (min {}, slip {slip}%)",
                pos.ticker, short(&pos.mint), fmt_tokens(amount), fmt_sol_u(sol_out), fmt_sol_u(min_sol_out)
            );
            self.record_close(pos, reason, est_proceeds, "dry-run");
            return Ok("dry-run".into());
        }

        let user = self.keypair.pubkey();
        // Whole balance → close the ATA in the same tx (rent back). If tokens arrive
        // between our read and the sell (dust), the close — and with it the sell — fails;
        // so after two failed attempts sell without closing.
        let close_ata = pos.exit_attempts < 2;
        let ixs = tx_builder::sell_instructions(&user, &a, &buyback, amount, min_sol_out, budget, &tips, close_ata);
        let tx = tx_builder::sign(&self.keypair, &ixs, blockhash.hash)?;
        let b64 = tx_builder::encode_base64(&tx)?;
        let signature = tx.signatures[0].to_string();
        self.journal_event("sell_sent", json!({"mint": pos.mint.to_string(), "ticker": pos.ticker, "sig": signature,
            "reason": reason.to_string(), "tokens": amount, "min_out": min_sol_out, "slippage_pct": slip, "attempt": pos.exit_attempts}));
        match self.submitter.submit(b64).await {
            Ok(_) => log_trade!(
                "SELL SENT ${} {} [{reason}] — {} tokens min {} (slip {slip}%) sig {}",
                pos.ticker, short(&pos.mint), fmt_tokens(amount), fmt_sol_u(min_sol_out), signature
            ),
            Err(e) => log_warn!("SELL submit uncertain ${} {}: {e} — reconciling on-chain", pos.ticker, short(&pos.mint)),
        }

        let me = Arc::clone(self);
        let (pos, s) = (pos.clone(), signature.clone());
        tokio::spawn(async move {
            let status = me.submitter.wait_for_landing(&s, blockhash.last_valid_height).await;
            match &status {
                LandStatus::Landed { .. } => {
                    let proceeds = me.landed_delta(&s).await.unwrap_or(est_proceeds);
                    me.record_close(&pos, reason, proceeds, &s);
                }
                LandStatus::Failed(e) => {
                    log_error!("SELL FAILED ${} {} — {e}; retrying with more slippage", pos.ticker, short(&pos.mint));
                    me.positions.abort_close(&pos.mint);
                }
                LandStatus::Expired | LandStatus::Unknown => {
                    // Only re-sell once we know the tokens are still there.
                    match me.rpc.token_balance_confirmed(&pos.accounts.user_ata).await {
                        Ok(None) | Ok(Some(0)) => me.record_close(&pos, reason, est_proceeds, &s),
                        r => {
                            log_error!("SELL NOT LANDED ${} {} — {:?} (balance {:?}); retrying", pos.ticker, short(&pos.mint), status, r.ok().flatten());
                            me.positions.abort_close(&pos.mint);
                        }
                    }
                }
            }
        });
        Ok(signature)
    }

    fn record_close(&self, pos: &Position, reason: ExitReason, proceeds: i128, sig: &str) {
        let pnl = self.positions.finish_close(pos, proceeds);
        let held = pos.entry_time.elapsed().as_secs();
        self.journal_event("close", json!({"mint": pos.mint.to_string(), "ticker": pos.ticker, "sig": sig,
            "reason": reason.to_string(), "cost": pos.cost_sol, "proceeds": proceeds as i64, "pnl": pnl as i64,
            "held_s": held, "dry_run": self.cfg.dry_run}));
        if pnl > 0 {
            log_ok!("CLOSED ${} {} [{reason}] PnL {} held {held}s", pos.ticker, short(&pos.mint), fmt_sol(pnl));
        } else {
            log_error!("CLOSED ${} {} [{reason}] PnL {} held {held}s", pos.ticker, short(&pos.mint), fmt_sol(pnl));
        }
        if let Err(e) = self.positions.risk_check(&self.cfg) {
            log_error!("RISK: {e}");
        }
    }

    fn journal_event(&self, event: &str, fields: serde_json::Value) {
        if let Some(j) = self.positions.journal() {
            j.event(event, fields);
        }
    }

    /// Live start: reload the journal and reconcile each entry with the wallet's
    /// confirmed token balance. Pending buys get a confirm task (they may still land).
    async fn restore_positions(self: &Arc<Self>) -> Result<()> {
        let Some(j) = self.positions.journal() else { return Ok(()) };
        let saved = j.load()?;
        if saved.is_empty() {
            return Ok(());
        }
        if self.cfg.dry_run {
            log_info!("journal: {} paper position(s) from a previous run discarded", saved.len());
            j.save(&[]);
            return Ok(());
        }
        let user = self.keypair.pubkey();
        let height = self.rpc.block_height().await?;
        for s in saved {
            let Some(mut pos) = Position::from_saved(&s, &user) else {
                log_warn!("journal: unreadable entry for {} ({}) — check the wallet manually", s.mint, s.state);
                continue;
            };
            if pos.cost_sol == 0 {
                pos.cost_sol = self.cfg.buy_lamports;
            }
            match self.rpc.token_balance_confirmed(&pos.accounts.user_ata).await? {
                Some(b) if b > 0 => {
                    log_warn!("journal: restored ${} {} — {} tokens ({} before restart)", pos.ticker, short(&pos.mint), fmt_tokens(b), s.state);
                    self.positions.open(Position { token_amount: b, ..pos });
                }
                _ if s.state == "pending" && !s.buy_sig.is_empty() => {
                    log_warn!("journal: pending buy ${} {} — waiting for it to land or expire", pos.ticker, short(&pos.mint));
                    if self.positions.try_reserve(pos.mint, &pos.ticker, true).is_ok() {
                        let sig = s.buy_sig.clone();
                        self.positions.set_pending(&pos.mint, sig.clone(), pos.clone());
                        let me = Arc::clone(self);
                        // A blockhash is valid ≤150 blocks after it was fetched; we don't
                        // know when, so wait out a full window from now.
                        tokio::spawn(async move { me.confirm_buy(sig, height + 150, pos).await });
                    }
                }
                _ => log_info!("journal: ${} {} ({}) has no tokens on-chain — dropped", pos.ticker, short(&pos.mint), s.state),
            }
        }
        Ok(())
    }

    async fn log_simulation(&self, tx: solana_sdk::transaction::VersionedTransaction, cu_limit: u32, a: &MintAccounts) {
        let b64 = match tx_builder::encode_base64(&tx) {
            Ok(b) => b,
            Err(e) => return log_warn!("simulate: encode failed: {e}"),
        };
        match self.rpc.simulate(&b64, &[a.user_ata]).await {
            Ok(sim) if sim.err.is_none() => {
                let got = sim.post_accounts.first().cloned().flatten().and_then(|acc| acc.token_amount());
                log_ok!(
                    "simulation OK — {} / {} CU{}",
                    sim.units_consumed.unwrap_or(0), cu_limit,
                    got.map(|t| format!(", receives {} tokens", fmt_tokens(t))).unwrap_or_default()
                );
            }
            Ok(sim) => {
                log_warn!("simulation FAILED: {}", sim.err.unwrap_or_default());
                for l in sim.logs.iter().rev().take(8).rev() {
                    log_dim!("  {l}");
                }
            }
            Err(e) => log_warn!("simulation request failed: {e}"),
        }
    }

    fn print_stats(&self) {
        let st = self.positions.stats();
        let avg = if st.trades > 0 { st.total_hold.as_secs() / st.trades } else { 0 };
        println!();
        println!("\x1b[1m══════ Session stats{} ══════\x1b[0m", if self.cfg.dry_run { " (DRY RUN)" } else { "" });
        println!("Trades: {}, Wins: {}, Losses: {}", st.trades, st.wins, st.losses);
        let color = if st.pnl_lamports >= 0 { "\x1b[32;1m" } else { "\x1b[31;1m" };
        println!("Total PnL: {color}{}\x1b[0m", fmt_sol(st.pnl_lamports));
        println!("Avg hold time: {avg}s");
        let open = self.positions.open_positions();
        if !open.is_empty() {
            println!("\x1b[33mStill open ({}), NOT sold:\x1b[0m", open.len());
            for p in open {
                println!("  ${} {} — {} tokens", p.ticker, p.mint, fmt_tokens(p.token_amount));
            }
        }
    }
}

fn fmt_tokens(raw: u64) -> String {
    let unit = 10u64.pow(types::TOKEN_DECIMALS);
    format!("{}.{:02}", raw / unit, (raw % unit) / (unit / 100))
}

fn signal_lag_ms(ts: f64) -> i64 {
    if ts <= 0.0 {
        return 0;
    }
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis() as i64;
    now - (ts * 1000.0) as i64
}

/// `--new-wallet <FILE>`: a fresh hot wallet for the bot (keep only trading funds on
/// it). Writes the base58 secret to FILE (mode 600, never overwrites), prints the address.
fn new_wallet(path: &str) -> Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let kp = Keypair::new();
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)
        .map_err(|e| format!("{path}: {e}"))?;
    writeln!(f, "{}", bs58::encode(kp.to_bytes()).into_string())?;
    println!("new wallet {} — secret key written to {path} (chmod 600).", kp.pubkey());
    println!("Put it in .env as WALLET_PRIVATE_KEY=<contents of {path}> and fund the address with trading SOL only.");
    Ok(())
}

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        log_error!("fatal: {e}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--new-wallet") {
        return new_wallet(args.get(2).ok_or("usage: mev-streamer-executor --new-wallet <FILE>")?);
    }
    // 1. Config
    let cfg = Config::from_env()?;
    if args.get(1).map(String::as_str) == Some("--selfcheck") {
        let mint = args.get(2).ok_or("usage: mev-streamer-executor --selfcheck <MINT>")?;
        return selfcheck::run(&cfg, mint).await;
    }
    if args.get(1).map(String::as_str) == Some("--inspect") {
        let mint = args.get(2).ok_or("usage: mev-streamer-executor --inspect <MINT>")?;
        return selfcheck::inspect(&cfg, mint).await;
    }
    if args.get(1).map(String::as_str) == Some("--probe-relays") {
        return selfcheck::probe_relays(&cfg).await;
    }

    // 2. Wallet
    let (keypair, ephemeral) = match &cfg.wallet_private_key {
        Some(k) => (config::load_keypair(k)?, false),
        None => {
            log_warn!("WALLET_PRIVATE_KEY not set — using ephemeral keypair (dry run only)");
            (Keypair::new(), true)
        }
    };
    let sim_payer = match std::env::var("SIM_PAYER").ok().filter(|s| !s.trim().is_empty()) {
        Some(s) => Pubkey::from_str(s.trim()).map_err(|e| format!("SIM_PAYER: {e}"))?,
        None => DEFAULT_SIM_PAYER,
    };

    log_info!("wallet       {}{}", keypair.pubkey(), if ephemeral { format!(" (ephemeral; simulating as {sim_payer})") } else { String::new() });
    log_info!("rpc          {}", cfg.rpc_url);
    log_info!("relays       {:?} | jito {:?}", cfg.relays, cfg.jito_urls);
    log_info!(
        "buy {} | jito tip {:?} | sender tip {} lamports | prio {:?} | CU buy/sell {}/{}",
        fmt_sol_u(cfg.buy_lamports), cfg.jito_tip, cfg.sender_tip_lamports, cfg.priority_fee,
        cfg.cu_limit_buy, cfg.cu_limit_sell,
    );
    log_info!(
        "exits        TP {}.{:02}x | SL {}.{:02}x | trail {}% | timeout {}s | slip {}% → {}% urgent | poll {}ms",
        cfg.take_profit_bps / 10_000, (cfg.take_profit_bps % 10_000) / 100,
        cfg.stop_loss_bps / 10_000, (cfg.stop_loss_bps % 10_000) / 100,
        cfg.trailing_stop_bps / 100, cfg.timeout_sec, cfg.slippage_pct, cfg.exit_slippage_pct, cfg.monitor_interval_ms
    );
    log_info!(
        "risk         max {} positions | session loss cap {} | {} losses in a row | reserve {} | max age {}ms | min conf {:.2} | max mcap {} | rebuy {}",
        cfg.max_positions, fmt_sol_u(cfg.max_session_loss_lamports), cfg.max_consecutive_losses,
        fmt_sol_u(cfg.min_sol_reserve_lamports), cfg.max_signal_age_ms, cfg.min_confidence,
        if cfg.max_mcap_lamports > 0 { fmt_sol_u(cfg.max_mcap_lamports) } else { "off".into() }, cfg.rebuy
    );
    if cfg.dry_run {
        log_warn!("DRY_RUN=true — nothing will be sent");
    } else {
        log_error!("DRY_RUN=false — LIVE TRADING with real funds");
    }

    let rpc = Arc::new(Rpc::new(cfg.rpc_url.clone()));
    // Warm the blockhash cache, HTTP connection and pump state before accepting signals.
    let (bh, state) = tokio::join!(rpc.refresh_blockhash(), fetch_state(&rpc));
    bh.map_err(|e| format!("initial blockhash fetch: {e}"))?;
    let state = state.map_err(|e| format!("pump state: {e}"))?;
    log_info!(
        "pump fees    protocol {} bps, creator {} bps, buyback {}% of protocol ({} tiers)",
        state.1.fee_tiers[0].fees.protocol_fee_bps, state.1.fee_tiers[0].fees.creator_fee_bps,
        state.0.buyback_basis_points / 100, state.1.fee_tiers.len()
    );
    rpc.spawn_blockhash_refresher();

    let jito = Arc::new(JitoClient::new(cfg.jito_urls.clone()));
    if matches!(cfg.jito_tip, config::Auto::Auto { .. }) && cfg.relays.contains(&sender::Relay::Jito) {
        jito.spawn_tip_floor_refresher("landed_tips_75th_percentile");
    }
    let sender_url = sender::sender_url_for_tip(&cfg.sender_url, cfg.sender_tip_lamports);
    let submitter = Arc::new(Submitter::new(cfg.relays.clone(), sender_url, cfg.sender_tip_lamports, Arc::clone(&jito), Arc::clone(&rpc)));
    submitter.spawn_sender_keepalive();

    // Bind first: the single-instance check must run before this process touches
    // the journal, or a second executor could rewrite the first one's positions.
    let socket_path = cfg.socket_path.clone();
    let listener = ipc::bind(&socket_path)?;
    let journal = Arc::new(journal::Journal::open(&cfg.state_dir, cfg.dry_run)?);
    let (snap, trades) = journal.paths();
    log_info!("journal      {snap} | {trades}");
    let exec = Arc::new(Executor {
        positions: PositionManager::new(cfg.max_positions, Some(journal)),
        keypair,
        ephemeral,
        sim_payer,
        rpc,
        submitter,
        state: RwLock::new(state),
        cfg,
    });
    exec.spawn_state_refresher();
    exec.restore_positions().await.map_err(|e| format!("restoring positions from journal: {e}"))?;

    // 3. IPC listener (bound earlier, before touching the journal)
    log_ok!("listening on {socket_path}");
    tokio::spawn(ipc::serve(listener, Arc::clone(&exec)));

    // 4. Position monitor
    tokio::spawn(position::run_monitor(Arc::clone(&exec)));

    // 5. SIGINT
    tokio::signal::ctrl_c().await?;
    log_warn!("SIGINT — shutting down ({} tracked)", exec.positions.count());
    let _ = std::fs::remove_file(&socket_path);
    exec.print_stats();
    Ok(())
}
