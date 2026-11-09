//! Position tracking, TP / SL / timeout evaluation, and the 3-second monitor loop.

use crate::bonding::BondingCurve;
use crate::config::Config;
use crate::pda::MintAccounts;
use crate::types::{fmt_sol, ExitReason};
use crate::Executor;
use solana_sdk::pubkey::Pubkey;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const POLL_INTERVAL: Duration = Duration::from_secs(3);

#[derive(Debug, Clone)]
pub struct Position {
    pub mint: Pubkey,
    pub ticker: String,
    /// Spot price right after our buy: lamports per whole token × `bonding::PRICE_SCALE`.
    pub entry_price: u64,
    pub token_amount: u64,
    /// Lamports spent: amount_in + Jito tip.
    pub cost_sol: u64,
    /// Mint supply at entry (fee tier is chosen by market cap).
    pub mint_supply: u64,
    pub entry_time: Instant,
    pub accounts: MintAccounts,
}

#[derive(Debug)]
enum Slot {
    /// Buy sent, not yet confirmed — counts toward max_positions.
    Pending,
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
}

pub struct PositionManager {
    slots: Mutex<HashMap<Pubkey, Slot>>,
    stats: Mutex<Stats>,
    max_positions: usize,
}

impl PositionManager {
    pub fn new(max_positions: usize) -> Self {
        PositionManager { slots: Mutex::new(HashMap::new()), stats: Mutex::new(Stats::default()), max_positions }
    }

    /// Atomically claim a slot for `mint` before any network I/O.
    pub fn try_reserve(&self, mint: Pubkey) -> Result<(), String> {
        let mut slots = self.slots.lock().unwrap();
        if slots.contains_key(&mint) {
            return Err(format!("already have a position in {mint}"));
        }
        if slots.len() >= self.max_positions {
            return Err(format!("max positions reached ({})", self.max_positions));
        }
        slots.insert(mint, Slot::Pending);
        Ok(())
    }

    pub fn release(&self, mint: &Pubkey) {
        self.slots.lock().unwrap().remove(mint);
    }

    pub fn open(&self, pos: Position) {
        self.slots.lock().unwrap().insert(pos.mint, Slot::Open(pos));
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

    /// Open → Closing. Returns `None` if not open (already closing / unknown).
    pub fn begin_close(&self, mint: &Pubkey) -> Option<Position> {
        let mut slots = self.slots.lock().unwrap();
        match slots.remove(mint) {
            Some(Slot::Open(p)) => {
                slots.insert(*mint, Slot::Closing(p.clone()));
                Some(p)
            }
            Some(other) => {
                slots.insert(*mint, other);
                None
            }
            None => None,
        }
    }

    /// Closing → Open (sell didn't land; monitor will retry next tick).
    pub fn abort_close(&self, mint: &Pubkey) {
        let mut slots = self.slots.lock().unwrap();
        if let Some(Slot::Closing(p)) = slots.remove(mint) {
            slots.insert(*mint, Slot::Open(p));
        }
    }

    /// Remove and record realized PnL. `proceeds` = SOL received minus exit tip.
    pub fn finish_close(&self, pos: &Position, proceeds: i128) -> i128 {
        self.slots.lock().unwrap().remove(&pos.mint);
        let pnl = proceeds - pos.cost_sol as i128;
        let mut st = self.stats.lock().unwrap();
        st.trades += 1;
        if pnl > 0 {
            st.wins += 1;
        } else {
            st.losses += 1;
        }
        st.pnl_lamports += pnl;
        st.total_hold += pos.entry_time.elapsed();
        pnl
    }

    pub fn stats(&self) -> Stats {
        self.stats.lock().unwrap().clone()
    }
}

/// Pure exit check: price ≥ entry×TP, price ≤ entry×SL, or held ≥ timeout.
/// Multipliers are basis points so the comparison stays in integers.
pub fn check_exit(pos: &Position, curve: &BondingCurve, cfg: &Config, now: Instant) -> Option<ExitReason> {
    if curve.complete {
        return Some(ExitReason::Graduated);
    }
    let price = curve.price() as u128 * 10_000;
    let entry = pos.entry_price as u128;
    if price >= entry * cfg.take_profit_bps as u128 {
        return Some(ExitReason::TakeProfit);
    }
    if price <= entry * cfg.stop_loss_bps as u128 {
        return Some(ExitReason::StopLoss);
    }
    if now.duration_since(pos.entry_time) >= Duration::from_secs(cfg.timeout_sec) {
        return Some(ExitReason::Timeout);
    }
    None
}

/// Price change vs entry in basis points (for logging).
fn change_bps(entry: u64, now: u64) -> i128 {
    if entry == 0 {
        return 0;
    }
    (now as i128 - entry as i128) * 10_000 / entry as i128
}

pub async fn run_monitor(exec: Arc<Executor>) {
    let mut tick = tokio::time::interval(POLL_INTERVAL);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        tick.tick().await;
        let open = exec.positions.open_positions();
        if open.is_empty() {
            continue;
        }
        // One round-trip for every tracked curve.
        let keys: Vec<Pubkey> = open.iter().map(|p| p.accounts.bonding_curve).collect();
        let accounts = match exec.rpc.get_multiple_accounts(&keys).await {
            Ok(a) => a,
            Err(e) => {
                log_warn!("monitor: curve fetch failed, skipping tick: {e}");
                continue;
            }
        };
        let now = Instant::now();
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
            let bps = change_bps(pos.entry_price, curve.price());
            let (global, fees) = exec.pump_state();
            let est = crate::bonding::sell_sol_out(&global, &fees, &curve, pos.mint_supply, pos.token_amount)
                .unwrap_or(0) as i128 - pos.cost_sol as i128;
            log_dim!(
                "  {} ${} {:+}.{:02}% held {}s est.PnL {}",
                short(&pos.mint),
                pos.ticker,
                bps / 100,
                (bps % 100).abs(),
                now.duration_since(pos.entry_time).as_secs(),
                fmt_sol(est)
            );
            if let Some(reason) = check_exit(&pos, &curve, &exec.cfg, now) {
                if let Some(p) = exec.positions.begin_close(&pos.mint) {
                    let e = Arc::clone(&exec);
                    tokio::spawn(async move {
                        let _ = e.exit_position(p, reason).await;
                    });
                }
            }
        }
    }
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

    fn curve(vsol: u64) -> BondingCurve {
        BondingCurve { virtual_token_reserves: 1_000_000_000_000_000, virtual_quote_reserves: vsol, real_token_reserves: 1, ..Default::default() }
    }

    fn pos(entry_price: u64, at: Instant) -> Position {
        let mint = Pubkey::new_unique();
        Position {
            mint,
            ticker: "T".into(),
            entry_price,
            token_amount: 1,
            cost_sol: 100,
            mint_supply: 1_000_000_000_000_000,
            entry_time: at,
            accounts: MintAccounts::new(mint, &Pubkey::new_unique(), pda::TOKEN_PROGRAM),
        }
    }

    #[test]
    fn exit_rules() {
        let now = Instant::now();
        let base = curve(30_000_000_000);
        let p = pos(base.price(), now);
        assert_eq!(check_exit(&p, &base, &cfg(), now), None);
        assert_eq!(check_exit(&p, &curve(60_000_000_000), &cfg(), now), Some(ExitReason::TakeProfit));
        assert_eq!(check_exit(&p, &curve(15_000_000_000), &cfg(), now), Some(ExitReason::StopLoss));
        assert_eq!(check_exit(&p, &base, &cfg(), now + Duration::from_secs(120)), Some(ExitReason::Timeout));
        assert_eq!(check_exit(&p, &BondingCurve { complete: true, ..base }, &cfg(), now), Some(ExitReason::Graduated));
    }

    #[test]
    fn slot_lifecycle_and_limits() {
        let pm = PositionManager::new(2);
        let p = pos(1, Instant::now());
        pm.try_reserve(p.mint).unwrap();
        assert!(pm.try_reserve(p.mint).is_err(), "duplicate mint");
        pm.try_reserve(Pubkey::new_unique()).unwrap();
        assert!(pm.try_reserve(Pubkey::new_unique()).is_err(), "max positions");
        assert!(pm.begin_close(&p.mint).is_none(), "pending can't close");
        pm.open(p.clone());
        assert_eq!(pm.open_positions().len(), 1);
        assert!(pm.begin_close(&p.mint).is_some());
        assert!(pm.begin_close(&p.mint).is_none(), "no double close");
        assert!(pm.open_positions().is_empty());
        pm.abort_close(&p.mint);
        assert_eq!(pm.open_positions().len(), 1);
        pm.begin_close(&p.mint);
        assert_eq!(pm.finish_close(&p, 250), 150);
        let st = pm.stats();
        assert_eq!((st.trades, st.wins, st.losses, st.pnl_lamports), (1, 1, 0, 150));
        assert_eq!(pm.count(), 1);
    }
}
