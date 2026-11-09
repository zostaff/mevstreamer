//! Position tracking, exit rules (TP / SL / trailing stop / timeout), session risk
//! counters, and the monitor loop.
//!
//! Exits are decided on *value*: what selling the whole position would return right
//! now (fee-aware `sell_sol_out` + the ATA rent we get back), against what the buy
//! really cost (payer lamport delta of the landed tx). Spot price alone ignores fees,
//! our own price impact and the fill we actually got.

use crate::bonding::BondingCurve;
use crate::config::{Config, TRAIL_ARM_BPS};
use crate::journal::{unix_now, Journal, Saved};
use crate::pda::MintAccounts;
use crate::types::{fmt_sol, ExitReason};
use crate::Executor;
use solana_sdk::pubkey::Pubkey;
use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Log each open position about every 3 s (the monitor itself ticks faster).
const LOG_EVERY: Duration = Duration::from_secs(3);

#[derive(Debug, Clone)]
pub struct Position {
    pub mint: Pubkey,
    pub ticker: String,
    /// Spot price right after our buy (lamports per whole token × PRICE_SCALE) — logging only.
    pub entry_price: u64,
    pub token_amount: u64,
    /// Lamports the buy really cost: amount in + fees + tips + priority + ATA rent.
    pub cost_sol: u64,
    /// Mint supply at entry (fee tier is chosen by market cap).
    pub mint_supply: u64,
    pub entry_time: Instant,
    pub entry_unix: f64,
    pub accounts: MintAccounts,
    /// Highest value seen (for the trailing stop).
    pub peak_value: u64,
    /// Rent of our token ATA, returned when the sell closes it.
    pub rent_lamports: u64,
    /// Failed sell attempts so far (slippage escalates with each).
    pub exit_attempts: u32,
    pub buy_sig: String,
    /// Paper (DRY_RUN) positions only: quote that our simulated buy put on the curve.
    /// The live curve never saw it, so valuation re-applies it (otherwise selling our
    /// paper tokens runs into the curve's real reserves and the value reads ~0).
    pub paper_net_in: u64,
}

impl Position {
    /// The curve as it would be with this position's own buy on it.
    pub fn effective_curve(&self, live: &BondingCurve) -> BondingCurve {
        if self.paper_net_in > 0 {
            live.after_buy(self.paper_net_in, self.token_amount)
        } else {
            *live
        }
    }

    fn saved(&self, state: &str) -> Saved {
        Saved {
            state: state.into(),
            mint: self.mint.to_string(),
            ticker: self.ticker.clone(),
            token_program: self.accounts.token_program.to_string(),
            token_amount: self.token_amount,
            cost_lamports: self.cost_sol,
            mint_supply: self.mint_supply,
            entry_price: self.entry_price,
            entry_unix: self.entry_unix,
            peak_value: self.peak_value,
            rent_lamports: self.rent_lamports,
            buy_sig: self.buy_sig.clone(),
        }
    }

    /// Rebuild from the journal for `user` (entry time is restored from wall-clock).
    pub fn from_saved(s: &Saved, user: &Pubkey) -> Option<Position> {
        let mint = Pubkey::from_str(&s.mint).ok()?;
        let tp = Pubkey::from_str(&s.token_program).ok()?;
        let age = Duration::from_secs_f64((unix_now() - s.entry_unix).max(0.0));
        Some(Position {
            mint,
            ticker: s.ticker.clone(),
            entry_price: s.entry_price,
            token_amount: s.token_amount,
            cost_sol: s.cost_lamports,
            mint_supply: s.mint_supply,
            entry_time: Instant::now().checked_sub(age).unwrap_or_else(Instant::now),
            entry_unix: s.entry_unix,
            accounts: MintAccounts::new(mint, user, tp),
            peak_value: s.peak_value,
            rent_lamports: s.rent_lamports,
            exit_attempts: 0,
            buy_sig: s.buy_sig.clone(),
            paper_net_in: 0,
        })
    }
}

/// What a pending buy will become if it lands (persisted so a restart can reconcile).
#[derive(Debug, Clone)]
pub struct PendingBuy {
    pub ticker: String,
    pub signature: Option<String>,
    pub expected: Option<Position>,
}

#[derive(Debug)]
enum Slot {
    /// Buy in flight — counts toward max_positions, blocks duplicates.
    Pending(PendingBuy),
    Open(Position),
    /// Sell in flight — excluded from monitoring so we never double-sell.
    Closing(Position),
}

#[derive(Debug, Default, Clone)]
pub struct Stats {
    pub trades: u64,
    pub wins: u64,
    pub losses: u64,
    pub pnl_lamports: i128,
    pub total_hold: Duration,
    pub consecutive_losses: u32,
}

pub struct PositionManager {
    slots: Mutex<HashMap<Pubkey, Slot>>,
    stats: Mutex<Stats>,
    /// Mints we already traded this session (REBUY=false blocks them).
    traded: Mutex<HashSet<Pubkey>>,
    max_positions: usize,
    journal: Option<Arc<Journal>>,
}

impl PositionManager {
    pub fn new(max_positions: usize, journal: Option<Arc<Journal>>) -> Self {
        PositionManager {
            slots: Mutex::new(HashMap::new()),
            stats: Mutex::new(Stats::default()),
            traded: Mutex::new(HashSet::new()),
            max_positions,
            journal,
        }
    }

    pub fn journal(&self) -> Option<&Journal> {
        self.journal.as_deref()
    }

    /// Persist the current slots. Called with the slots lock held so snapshots
    /// are written in mutation order.
    fn persist(&self, slots: &HashMap<Pubkey, Slot>) {
        let Some(j) = &self.journal else { return };
        let all: Vec<Saved> = slots
            .iter()
            .filter_map(|(mint, s)| match s {
                Slot::Open(p) => Some(p.saved("open")),
                Slot::Closing(p) => Some(p.saved("closing")),
                Slot::Pending(pb) => pb.expected.as_ref().map(|p| Saved {
                    buy_sig: pb.signature.clone().unwrap_or_default(),
                    ..p.saved("pending")
                }).or_else(|| Some(Saved {
                    state: "pending".into(), mint: mint.to_string(), ticker: pb.ticker.clone(),
                    token_program: String::new(), token_amount: 0, cost_lamports: 0, mint_supply: 0,
                    entry_price: 0, entry_unix: unix_now(), peak_value: 0, rent_lamports: 0,
                    buy_sig: pb.signature.clone().unwrap_or_default(),
                })),
            })
            .collect();
        j.save(&all);
    }

    /// Atomically claim a slot for `mint` before any network I/O.
    pub fn try_reserve(&self, mint: Pubkey, ticker: &str, rebuy: bool) -> Result<(), String> {
        let mut slots = self.slots.lock().unwrap();
        if slots.contains_key(&mint) {
            return Err(format!("already have a position in {mint}"));
        }
        if !rebuy && self.traded.lock().unwrap().contains(&mint) {
            return Err("already traded this mint this session (REBUY=false)".into());
        }
        if slots.len() >= self.max_positions {
            return Err(format!("max positions reached ({})", self.max_positions));
        }
        slots.insert(mint, Slot::Pending(PendingBuy { ticker: ticker.into(), signature: None, expected: None }));
        Ok(())
    }

    /// Record the signed buy (before it is sent) so a crash mid-flight can be reconciled.
    pub fn set_pending(&self, mint: &Pubkey, signature: String, expected: Position) {
        let mut slots = self.slots.lock().unwrap();
        if let Some(Slot::Pending(pb)) = slots.get_mut(mint) {
            pb.signature = Some(signature);
            pb.expected = Some(expected);
        }
        self.persist(&slots);
    }

    /// Drop a slot that never held tokens (buy rejected / failed / expired).
    pub fn release(&self, mint: &Pubkey) {
        let mut slots = self.slots.lock().unwrap();
        slots.remove(mint);
        self.persist(&slots);
    }

    pub fn open(&self, pos: Position) {
        self.traded.lock().unwrap().insert(pos.mint);
        let mut slots = self.slots.lock().unwrap();
        slots.insert(pos.mint, Slot::Open(pos));
        self.persist(&slots);
    }

    pub fn open_positions(&self) -> Vec<Position> {
        self.slots
            .lock()
            .unwrap()
            .values()
            .filter_map(|s| match s {
                Slot::Open(p) => Some(p.clone()),
                _ => None,
            })
            .collect()
    }

    pub fn count(&self) -> usize {
        self.slots.lock().unwrap().len()
    }

    /// Raise the stored peak (in memory; persisted with the next state change).
    pub fn update_peak(&self, mint: &Pubkey, value: u64) {
        if let Some(Slot::Open(p)) = self.slots.lock().unwrap().get_mut(mint) {
            p.peak_value = p.peak_value.max(value);
        }
    }

    /// Open → Closing. Returns `None` if not open (already closing / unknown).
    pub fn begin_close(&self, mint: &Pubkey) -> Option<Position> {
        let mut slots = self.slots.lock().unwrap();
        match slots.remove(mint) {
            Some(Slot::Open(p)) => {
                slots.insert(*mint, Slot::Closing(p.clone()));
                self.persist(&slots);
                Some(p)
            }
            Some(other) => {
                slots.insert(*mint, other);
                None
            }
            None => None,
        }
    }

    /// Closing → Open (sell didn't land; monitor retries next tick with more slippage).
    pub fn abort_close(&self, mint: &Pubkey) {
        let mut slots = self.slots.lock().unwrap();
        if let Some(Slot::Closing(mut p)) = slots.remove(mint) {
            p.exit_attempts += 1;
            slots.insert(*mint, Slot::Open(p));
        }
        self.persist(&slots);
    }

    /// Remove and record realized PnL. `proceeds` = net lamports the exit returned.
    pub fn finish_close(&self, pos: &Position, proceeds: i128) -> i128 {
        {
            let mut slots = self.slots.lock().unwrap();
            slots.remove(&pos.mint);
            self.persist(&slots);
        }
        let pnl = proceeds - pos.cost_sol as i128;
        let mut st = self.stats.lock().unwrap();
        st.trades += 1;
        if pnl > 0 {
            st.wins += 1;
            st.consecutive_losses = 0;
        } else {
            st.losses += 1;
            st.consecutive_losses += 1;
        }
        st.pnl_lamports += pnl;
        st.total_hold += pos.entry_time.elapsed();
        pnl
    }

    pub fn stats(&self) -> Stats {
        self.stats.lock().unwrap().clone()
    }

    /// Session circuit breakers. `Err(reason)` → refuse new buys.
    pub fn risk_check(&self, cfg: &Config) -> Result<(), String> {
        let st = self.stats.lock().unwrap();
        if cfg.max_session_loss_lamports > 0 && -st.pnl_lamports >= cfg.max_session_loss_lamports as i128 {
            return Err(format!(
                "session loss limit hit ({} ≥ MAX_SESSION_LOSS {}) — buying halted, restart to reset",
                fmt_sol(st.pnl_lamports), fmt_sol(cfg.max_session_loss_lamports as i128)
            ));
        }
        if cfg.max_consecutive_losses > 0 && st.consecutive_losses >= cfg.max_consecutive_losses {
            return Err(format!("{} losses in a row (MAX_CONSECUTIVE_LOSSES) — buying halted, restart to reset", st.consecutive_losses));
        }
        Ok(())
    }
}

/// Pure exit check on position value (lamports we'd get back selling everything now).
/// Multipliers are basis points so comparisons stay in integers.
pub fn check_exit(pos: &Position, value: u64, complete: bool, cfg: &Config, now: Instant) -> Option<ExitReason> {
    if complete {
        return Some(ExitReason::Graduated);
    }
    let v = value as u128 * 10_000;
    let cost = pos.cost_sol as u128;
    if v >= cost * cfg.take_profit_bps as u128 {
        return Some(ExitReason::TakeProfit);
    }
    if v <= cost * cfg.stop_loss_bps as u128 {
        return Some(ExitReason::StopLoss);
    }
    let peak = pos.peak_value.max(value) as u128;
    if cfg.trailing_stop_bps > 0 && peak * 10_000 >= cost * TRAIL_ARM_BPS as u128 && v <= peak * (10_000 - cfg.trailing_stop_bps as u128) {
        return Some(ExitReason::TrailingStop);
    }
    if now.duration_since(pos.entry_time) >= Duration::from_secs(cfg.timeout_sec) {
        return Some(ExitReason::Timeout);
    }
    None
}

/// Slippage for an exit: base for take-profit; for urgent exits and every retry it
/// escalates toward EXIT_SLIPPAGE_PCT so a dump can't trap us with failing sells.
pub fn exit_slippage(cfg: &Config, reason: ExitReason, attempts: u32) -> u64 {
    let base = cfg.slippage_pct;
    let max = cfg.exit_slippage_pct;
    let steps = attempts as u64 + u64::from(reason.urgent());
    // 0 → base, 1 → halfway, ≥2 → max
    match steps {
        0 => base,
        1 => base + (max - base) / 2,
        _ => max,
    }
}

fn spawn_exit(exec: &Arc<Executor>, mint: &Pubkey, reason: ExitReason) {
    if let Some(p) = exec.positions.begin_close(mint) {
        let e = Arc::clone(exec);
        tokio::spawn(async move {
            let _ = e.exit_position(p, reason).await;
        });
    }
}

pub async fn run_monitor(exec: Arc<Executor>) {
    let mut tick = tokio::time::interval(Duration::from_millis(exec.cfg.monitor_interval_ms));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut last_log = Instant::now();
    loop {
        tick.tick().await;
        let open = exec.positions.open_positions();
        if open.is_empty() {
            continue;
        }
        let now = Instant::now();
        // One round-trip for every tracked curve.
        let keys: Vec<Pubkey> = open.iter().map(|p| p.accounts.bonding_curve).collect();
        let accounts = match exec.rpc.get_multiple_accounts(&keys).await {
            Ok(a) => a,
            Err(e) => {
                log_warn!("monitor: curve fetch failed: {e}");
                // Without prices we can still honour the timeout.
                for p in open.iter().filter(|p| now.duration_since(p.entry_time) >= Duration::from_secs(exec.cfg.timeout_sec)) {
                    spawn_exit(&exec, &p.mint, ExitReason::Timeout);
                }
                continue;
            }
        };
        let log_now = last_log.elapsed() >= LOG_EVERY;
        if log_now {
            last_log = now;
        }
        let (global, fees) = exec.pump_state();
        for (pos, acc) in open.into_iter().zip(accounts) {
            let curve = match acc.map(|a| BondingCurve::parse(&a.data)) {
                Some(Ok(c)) => c,
                Some(Err(e)) => {
                    log_warn!("monitor: {} bad curve data: {e}", pos.mint);
                    continue;
                }
                None => {
                    log_warn!("monitor: {} curve account missing", pos.mint);
                    continue;
                }
            };
            let value = match crate::bonding::sell_sol_out(&global, &fees, &pos.effective_curve(&curve), pos.mint_supply, pos.token_amount) {
                Ok(v) => v + pos.rent_lamports,
                Err(e) => {
                    // Never read "can't quote" as "worth 0" — that would fire the stop-loss.
                    if log_now {
                        log_warn!("  {} ${} cannot value position: {e}", short(&pos.mint), pos.ticker);
                    }
                    if now.duration_since(pos.entry_time) >= Duration::from_secs(exec.cfg.timeout_sec) || curve.complete {
                        spawn_exit(&exec, &pos.mint, if curve.complete { ExitReason::Graduated } else { ExitReason::Timeout });
                    }
                    continue;
                }
            };
            exec.positions.update_peak(&pos.mint, value);
            if log_now {
                let bps = change_bps(pos.cost_sol, value);
                log_dim!(
                    "  {} ${} value {:+}.{:02}% (peak {:+}%) held {}s PnL {}",
                    short(&pos.mint),
                    pos.ticker,
                    bps / 100,
                    (bps % 100).abs(),
                    change_bps(pos.cost_sol, pos.peak_value.max(value)) / 100,
                    now.duration_since(pos.entry_time).as_secs(),
                    fmt_sol(value as i128 - pos.cost_sol as i128)
                );
            }
            if let Some(reason) = check_exit(&pos, value, curve.complete, &exec.cfg, now) {
                spawn_exit(&exec, &pos.mint, reason);
            }
        }
    }
}

/// Change vs base in basis points (for logging).
fn change_bps(base: u64, now: u64) -> i128 {
    if base == 0 {
        return 0;
    }
    (now as i128 - base as i128) * 10_000 / base as i128
}

pub fn short(k: &Pubkey) -> String {
    let s = k.to_string();
    format!("{}…{}", &s[..4], &s[s.len() - 4..])
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pda;

    fn cfg() -> Config {
        Config::test_default()
    }

    fn pos(cost: u64, at: Instant) -> Position {
        let mint = Pubkey::new_unique();
        Position {
            mint,
            ticker: "T".into(),
            entry_price: 1,
            token_amount: 1,
            cost_sol: cost,
            mint_supply: 1_000_000_000_000_000,
            entry_time: at,
            entry_unix: unix_now(),
            accounts: MintAccounts::new(mint, &Pubkey::new_unique(), pda::TOKEN_PROGRAM),
            peak_value: 0,
            rent_lamports: 0,
            exit_attempts: 0,
            buy_sig: String::new(),
            paper_net_in: 0,
        }
    }

    #[test]
    fn exit_rules_on_value() {
        let now = Instant::now();
        let c = cfg(); // TP 2.0, SL 0.5, trail 25 % armed at 1.2x, timeout 120 s
        let p = pos(1_000, now);
        assert_eq!(check_exit(&p, 1_000, false, &c, now), None);
        assert_eq!(check_exit(&p, 2_000, false, &c, now), Some(ExitReason::TakeProfit));
        assert_eq!(check_exit(&p, 500, false, &c, now), Some(ExitReason::StopLoss));
        assert_eq!(check_exit(&p, 1_000, false, &c, now + Duration::from_secs(120)), Some(ExitReason::Timeout));
        assert_eq!(check_exit(&p, 1_000, true, &c, now), Some(ExitReason::Graduated));
        // trailing: peak 1.8x, now 1.3x (−28 % from peak) → exit; 1.4x (−22 %) → hold
        let p = Position { peak_value: 1_800, ..pos(1_000, now) };
        assert_eq!(check_exit(&p, 1_300, false, &c, now), Some(ExitReason::TrailingStop));
        assert_eq!(check_exit(&p, 1_400, false, &c, now), None);
        // not armed: peak 1.1x, drop to 0.8x is SL territory only
        let p = Position { peak_value: 1_100, ..pos(1_000, now) };
        assert_eq!(check_exit(&p, 800, false, &c, now), None);
        let off = Config { trailing_stop_bps: 0, ..cfg() };
        let p = Position { peak_value: 1_800, ..pos(1_000, now) };
        assert_eq!(check_exit(&p, 1_300, false, &off, now), None);
    }

    #[test]
    fn exit_slippage_escalates() {
        let c = Config { slippage_pct: 10, exit_slippage_pct: 40, ..cfg() };
        assert_eq!(exit_slippage(&c, ExitReason::TakeProfit, 0), 10);
        assert_eq!(exit_slippage(&c, ExitReason::StopLoss, 0), 25);
        assert_eq!(exit_slippage(&c, ExitReason::TakeProfit, 1), 25);
        assert_eq!(exit_slippage(&c, ExitReason::Timeout, 1), 40);
        assert_eq!(exit_slippage(&c, ExitReason::TakeProfit, 5), 40);
    }

    #[test]
    fn slot_lifecycle_limits_and_risk() {
        let pm = PositionManager::new(2, None);
        let p = pos(100, Instant::now());
        pm.try_reserve(p.mint, "T", false).unwrap();
        assert!(pm.try_reserve(p.mint, "T", false).is_err(), "duplicate mint");
        let other = Pubkey::new_unique();
        pm.try_reserve(other, "U", false).unwrap();
        assert!(pm.try_reserve(Pubkey::new_unique(), "V", false).is_err(), "max positions");
        pm.release(&other);
        assert!(pm.begin_close(&p.mint).is_none(), "pending can't close");
        pm.open(p.clone());
        assert_eq!(pm.open_positions().len(), 1);
        assert!(pm.begin_close(&p.mint).is_some());
        assert!(pm.begin_close(&p.mint).is_none(), "no double close");
        assert!(pm.open_positions().is_empty());
        pm.abort_close(&p.mint);
        assert_eq!(pm.open_positions()[0].exit_attempts, 1);
        pm.begin_close(&p.mint);
        assert_eq!(pm.finish_close(&p, 250), 150);
        let st = pm.stats();
        assert_eq!((st.trades, st.wins, st.losses, st.pnl_lamports), (1, 1, 0, 150));
        assert_eq!(pm.count(), 0);
        assert!(pm.try_reserve(p.mint, "T", false).is_err(), "no rebuy");
        assert!(pm.try_reserve(p.mint, "T", true).is_ok(), "rebuy allowed");

        // circuit breakers
        let c = Config { max_session_loss_lamports: 100, max_consecutive_losses: 2, ..cfg() };
        let pm = PositionManager::new(5, None);
        let a = pos(100, Instant::now());
        pm.open(a.clone());
        pm.begin_close(&a.mint);
        pm.finish_close(&a, 60); // −40
        assert!(pm.risk_check(&c).is_ok());
        let b = pos(100, Instant::now());
        pm.open(b.clone());
        pm.begin_close(&b.mint);
        pm.finish_close(&b, 90); // −10, 2 in a row
        assert!(pm.risk_check(&c).unwrap_err().contains("in a row"));
        let c2 = Config { max_session_loss_lamports: 50, max_consecutive_losses: 0, ..cfg() };
        assert!(pm.risk_check(&c2).unwrap_err().contains("loss limit"));
    }

    #[test]
    fn saved_roundtrip() {
        let user = Pubkey::new_unique();
        let mut p = pos(123, Instant::now());
        p.accounts = MintAccounts::new(p.mint, &user, pda::TOKEN_2022_PROGRAM);
        let back = Position::from_saved(&p.saved("open"), &user).unwrap();
        assert_eq!((back.mint, back.cost_sol, back.accounts.user_ata), (p.mint, 123, p.accounts.user_ata));
    }
}
