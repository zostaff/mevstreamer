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

/// Blockhash validity is ~30 s at 200 ms slots; stop waiting after that.
const LAND_TIMEOUT: Duration = Duration::from_secs(30);
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
    if global.buyback_fee_recipients.is_empty() {
        return Err("Global lists no buyback fee recipients".into());
    }
    Ok((global, FeeConfig::parse(&f.data)?))
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

    async fn buy(self: &Arc<Self>, mint: Pubkey, sig: &Signal) -> Result<String> {
        // Claim the slot before any I/O so concurrent signals can't exceed max_positions.
        self.positions.try_reserve(mint)?;
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

    async fn buy_inner(self: &Arc<Self>, mint: Pubkey, sig: &Signal) -> Result<String> {
        let t0 = Instant::now();
        let curve_key = pda::bonding_curve(&mint);

        // One round-trip of latency: accounts ‖ priority fee ‖ (cached) blockhash.
        let keys = [curve_key, mint];
        let (accs, cu_price, blockhash) = tokio::join!(
            self.rpc.get_multiple_accounts(&keys),
            self.cu_price(curve_key),
            self.rpc.latest_blockhash()
        );
        let (accs, blockhash) = (accs?, blockhash?);
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
            return Err(format!("curve is quoted in {}, only SOL curves are supported", curve.quote_mint).into());
        }
        if curve.is_cashback_coin {
            return Err("cashback coin: v3 trades refuse it (CashbackCoinNotSupported)".into());
        }
        if !curve.has_liquidity() {
            return Err("bonding curve has no liquidity".into());
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
        let cost = q.debit + tip_total + tx_builder::priority_fee_lamports(budget);

        if self.cfg.dry_run {
            log_trade!(
                "WOULD BUY ${} {} — {} → ~{} tokens (min {}, fees {} lamports, slip {}%, tips {} lamports, prio {}µL/CU) [state {}ms]",
                sig.ticker, mint, fmt_sol_u(amount_in), fmt_tokens(q.tokens_out), fmt_tokens(min_out),
                q.fees, self.cfg.slippage_pct, tip_total, cu_price, fetch_ms
            );
            let payer = if self.ephemeral { self.sim_payer } else { self.keypair.pubkey() };
            let sim_accounts = MintAccounts::new(mint, &payer, token_program);
            let ixs = tx_builder::buy_instructions(&payer, &sim_accounts, &buyback, amount_in, min_out, budget, &tips);
            self.log_simulation(tx_builder::unsigned(&payer, &ixs, blockhash)?, budget.cu_limit, &sim_accounts).await;
            // Paper position so TP/SL/timeout logic and stats can be exercised.
            self.positions.open(Position {
                mint, ticker: sig.ticker.clone(), entry_price, token_amount: q.tokens_out,
                cost_sol: cost, mint_supply: supply, entry_time: Instant::now(),
                accounts: MintAccounts::new(mint, &self.keypair.pubkey(), token_program),
            });
            return Ok("dry-run".into());
        }

        let user = self.keypair.pubkey();
        let accounts = MintAccounts::new(mint, &user, token_program);
        let ixs = tx_builder::buy_instructions(&user, &accounts, &buyback, amount_in, min_out, budget, &tips);
        let tx = tx_builder::sign(&self.keypair, &ixs, blockhash)?;
        let signature = self.submitter.submit(tx_builder::encode_base64(&tx)?).await?;
        log_trade!(
            "BUY SENT ${} {} — {} min_out {} tips {} lamports prio {}µL/CU sig {} [signal→send {}ms]",
            sig.ticker, short(&mint), fmt_sol_u(amount_in), fmt_tokens(min_out), tip_total,
            cu_price, signature, t0.elapsed().as_millis()
        );

        let me = Arc::clone(self);
        let (ticker, s) = (sig.ticker.clone(), signature.clone());
        tokio::spawn(async move {
            me.confirm_buy(s, accounts, ticker, entry_price, q.tokens_out, cost, supply).await;
        });
        Ok(signature)
    }

    #[allow(clippy::too_many_arguments)]
    async fn confirm_buy(&self, signature: String, accounts: MintAccounts, ticker: String, entry_price: u64, expected: u64, cost: u64, supply: u64) {
        let t0 = Instant::now();
        let status = self.submitter.wait_for_landing(&signature, LAND_TIMEOUT).await;
        // Always check the real balance: a "timed out" tx may still have landed.
        let balance = self.rpc.token_balance(&accounts.user_ata).await;
        let token_amount = match (&status, balance) {
            (LandStatus::Landed { .. }, Ok(b)) | (LandStatus::TimedOut, Ok(b)) if b > 0 => b,
            (LandStatus::Landed { .. }, Err(e)) => {
                log_warn!("buy landed but balance read failed ({e}); tracking expected amount");
                expected
            }
            (s, b) => {
                log_error!("BUY FAILED ${} {} — {:?} (balance {:?})", ticker, short(&accounts.mint), s, b.ok());
                self.positions.release(&accounts.mint);
                return;
            }
        };
        log_ok!("BUY LANDED ${} {} — {} tokens ({:?}, confirmed in {}ms)", ticker, short(&accounts.mint), fmt_tokens(token_amount), status, t0.elapsed().as_millis());
        self.positions.open(Position {
            mint: accounts.mint, ticker, entry_price, token_amount, cost_sol: cost, mint_supply: supply,
            entry_time: Instant::now(), accounts,
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
        let (accs, cu_price, blockhash) = tokio::join!(
            self.rpc.get_multiple_accounts(&keys),
            self.cu_price(a.bonding_curve),
            self.rpc.latest_blockhash()
        );
        let (accs, blockhash) = (accs?, blockhash?);
        let curve = BondingCurve::parse(&accs[0].as_ref().ok_or("bonding curve missing")?.data)?;
        if curve.complete {
            return Err("curve completed between check and sell".into());
        }
        let amount = if self.cfg.dry_run {
            pos.token_amount
        } else {
            accs[1].as_ref().and_then(|acc| acc.token_amount()).unwrap_or(0)
        };
        if amount == 0 {
            log_warn!("${} {} has zero token balance — closing as total loss", pos.ticker, short(&pos.mint));
            self.record_close(pos, reason, 0);
            return Ok("empty".into());
        }

        let (global, fees) = self.pump_state();
        let sol_out = bonding::sell_sol_out(&global, &fees, &curve, pos.mint_supply, amount)?;
        let min_sol_out = apply_slippage(sol_out, self.cfg.slippage_pct);
        let buyback = jito::pick(&global.buyback_fee_recipients);
        let budget = Budget { cu_limit: self.cfg.cu_limit_sell, cu_price_micro_lamports: cu_price };
        let tips = self.tips();
        let tip_total: u64 = tips.iter().map(|t| t.1).sum();
        let proceeds = sol_out as i128 - tip_total as i128 - tx_builder::priority_fee_lamports(budget) as i128;

        if self.cfg.dry_run {
            log_trade!(
                "WOULD SELL ${} {} [{reason}] — {} tokens for ~{} (min {})",
                pos.ticker, short(&pos.mint), fmt_tokens(amount), fmt_sol_u(sol_out), fmt_sol_u(min_sol_out)
            );
            self.record_close(pos, reason, proceeds);
            return Ok("dry-run".into());
        }

        let user = self.keypair.pubkey();
        let ixs = tx_builder::sell_instructions(&user, &a, &buyback, amount, min_sol_out, budget, &tips);
        let tx = tx_builder::sign(&self.keypair, &ixs, blockhash)?;
        let signature = self.submitter.submit(tx_builder::encode_base64(&tx)?).await?;
        log_trade!(
            "SELL SENT ${} {} [{reason}] — {} tokens min {} sig {}",
            pos.ticker, short(&pos.mint), fmt_tokens(amount), fmt_sol_u(min_sol_out), signature
        );

        let me = Arc::clone(self);
        let (pos, s) = (pos.clone(), signature.clone());
        tokio::spawn(async move {
            let status = me.submitter.wait_for_landing(&s, LAND_TIMEOUT).await;
            let remaining = me.rpc.token_balance(&pos.accounts.user_ata).await;
            match (&status, remaining) {
                (_, Ok(0)) | (LandStatus::Landed { .. }, _) => me.record_close(&pos, reason, proceeds),
                (st, r) => {
                    log_error!("SELL NOT LANDED ${} {} — {:?} (balance {:?}); retrying next tick", pos.ticker, short(&pos.mint), st, r.ok());
                    me.positions.abort_close(&pos.mint);
                }
            }
        });
        Ok(signature)
    }

    fn record_close(&self, pos: &Position, reason: ExitReason, proceeds: i128) {
        let pnl = self.positions.finish_close(pos, proceeds);
        let held = pos.entry_time.elapsed().as_secs();
        if pnl > 0 {
            log_ok!("CLOSED ${} {} [{reason}] PnL {} held {held}s", pos.ticker, short(&pos.mint), fmt_sol(pnl));
        } else {
            log_error!("CLOSED ${} {} [{reason}] PnL {} held {held}s", pos.ticker, short(&pos.mint), fmt_sol(pnl));
        }
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

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        log_error!("fatal: {e}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    // 1. Config
    let cfg = Config::from_env()?;
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--selfcheck") {
        let mint = args.get(2).ok_or("usage: mev-streamer-executor --selfcheck <MINT>")?;
        return selfcheck::run(&cfg, mint).await;
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
        "buy {} | jito tip {:?} | sender tip {} lamports | prio {:?} | CU buy/sell {}/{} | TP {}.{:02}x | SL {}.{:02}x | timeout {}s | max {} | slip {}%",
        fmt_sol_u(cfg.buy_lamports), cfg.jito_tip, cfg.sender_tip_lamports, cfg.priority_fee,
        cfg.cu_limit_buy, cfg.cu_limit_sell,
        cfg.take_profit_bps / 10_000, (cfg.take_profit_bps % 10_000) / 100,
        cfg.stop_loss_bps / 10_000, (cfg.stop_loss_bps % 10_000) / 100,
        cfg.timeout_sec, cfg.max_positions, cfg.slippage_pct
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

    let exec = Arc::new(Executor {
        positions: PositionManager::new(cfg.max_positions),
        keypair,
        ephemeral,
        sim_payer,
        rpc,
        submitter,
        state: RwLock::new(state),
        cfg,
    });
    exec.spawn_state_refresher();

    // 3. IPC listener
    let socket_path = exec.cfg.socket_path.clone();
    let listener = ipc::bind(&socket_path)?;
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
