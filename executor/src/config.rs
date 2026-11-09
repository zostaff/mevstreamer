//! Configuration from `.env`. All money values are parsed straight from the
//! decimal string into integer lamports / basis points — no f64 involved.

use crate::types::Result;
use solana_sdk::signature::Keypair;
use std::env;

pub const DEFAULT_SOCKET_PATH: &str = "/tmp/mev-streamer.sock";
/// ≈ 1.2–1.3× usage measured by `--selfcheck` on mainnet (2026-11-09): buy_exact_quote_in_v3
/// incl. ATA + volume-accumulator creation 69.7k–73.2k CU, sell_v3 46.2k CU. A tight limit
/// raises effective priority (fee per requested CU) and cuts the priority-fee cost.
pub const DEFAULT_CU_LIMIT_BUY: u32 = 90_000;
pub const DEFAULT_CU_LIMIT_SELL: u32 = 60_000;
/// PumpSwap v2 incl. wSOL wrap/unwrap and ATA creation: buy 81–84k, sell ~58k CU
/// measured by `--selfcheck-amm` (2026-11-09).
pub const DEFAULT_CU_LIMIT_AMM_BUY: u32 = 100_000;
pub const DEFAULT_CU_LIMIT_AMM_SELL: u32 = 75_000;

/// Fixed amount, or derived live and clamped to `[min, max]`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Auto {
    Fixed(u64),
    Auto { min: u64, max: u64 },
}

impl Auto {
    pub fn resolve(self, live: u64) -> u64 {
        match self {
            Auto::Fixed(v) => v,
            Auto::Auto { min, max } => live.clamp(min, max),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Config {
    pub rpc_url: String,
    pub wallet_private_key: Option<String>,
    pub buy_lamports: u64,
    /// JITO_TIP: SOL amount or `auto` (live tip_floor p75, clamped to [1000 lamports, JITO_TIP_MAX]).
    pub jito_tip: Auto,
    /// Take-profit multiplier in basis points (2.0x → 20_000).
    pub take_profit_bps: u64,
    /// Stop-loss multiplier in basis points (0.5x → 5_000).
    pub stop_loss_bps: u64,
    pub timeout_sec: u64,
    pub max_positions: usize,
    pub slippage_pct: u64,
    pub dry_run: bool,
    pub jito_urls: Vec<String>,
    pub relays: Vec<crate::sender::Relay>,
    pub sender_url: String,
    pub sender_tip_lamports: u64,
    /// PRIORITY_FEE: µ-lamports/CU or `auto` (p75 of recent fees on the curve, clamped).
    pub priority_fee: Auto,
    pub cu_limit_buy: u32,
    pub cu_limit_sell: u32,
    pub cu_limit_amm_buy: u32,
    pub cu_limit_amm_sell: u32,
    pub socket_path: String,

    // ---- exits
    /// Trailing stop: sell when value falls this far (bps of peak) below its peak,
    /// once the peak is ≥ TRAIL_ARM_BPS of cost. 0 = off.
    pub trailing_stop_bps: u64,
    /// Max slippage for urgent exits (stop-loss / timeout / retries): the sell's
    /// slippage escalates from SLIPPAGE_PCT up to this.
    pub exit_slippage_pct: u64,
    pub monitor_interval_ms: u64,

    // ---- risk limits (checked before every buy)
    pub min_confidence: f64,
    /// Reject signals older than this (ms since the evidence was captured). 0 = off.
    pub max_signal_age_ms: u64,
    /// Stop buying once realized session loss reaches this. 0 = off.
    pub max_session_loss_lamports: u64,
    /// Stop buying after this many losing trades in a row. 0 = off.
    pub max_consecutive_losses: u32,
    /// SOL that must stay in the wallet after a buy (exit fees, ATA rent). Live only.
    pub min_sol_reserve_lamports: u64,
    /// Skip curves whose market cap (in SOL) is already above this. 0 = off.
    pub max_mcap_lamports: u64,
    /// Allow buying a mint again after its position closed this session.
    pub rebuy: bool,
    /// Directory for positions*.json / trades*.jsonl.
    pub state_dir: String,
}

/// The trailing stop arms once value ≥ 1.2× cost, so noise right after entry
/// doesn't stop us out (the hard STOP_LOSS covers that).
pub const TRAIL_ARM_BPS: u64 = 12_000;

impl Config {
    pub fn from_env() -> Result<Self> {
        // Missing .env is fine — variables may come from the environment.
        let _ = dotenvy::dotenv();

        let cfg = Config {
            rpc_url: required("RPC_URL")?,
            wallet_private_key: env::var("WALLET_PRIVATE_KEY").ok().filter(|s| !s.trim().is_empty()),
            buy_lamports: parse_fixed(&required("BUY_SOL")?, 9).map_err(|e| format!("BUY_SOL: {e}"))?,
            jito_tip: parse_auto(
                &optional("JITO_TIP", "auto"), 9,
                Auto::Auto { min: crate::jito::MIN_TIP_LAMPORTS, max: parse_fixed(&optional("JITO_TIP_MAX", "0.001"), 9).map_err(|e| format!("JITO_TIP_MAX: {e}"))? },
            ).map_err(|e| format!("JITO_TIP: {e}"))?,
            take_profit_bps: parse_fixed(&optional("TAKE_PROFIT", "2.0"), 4).map_err(|e| format!("TAKE_PROFIT: {e}"))?,
            stop_loss_bps: parse_fixed(&optional("STOP_LOSS", "0.5"), 4).map_err(|e| format!("STOP_LOSS: {e}"))?,
            timeout_sec: optional("TIMEOUT_SEC", "120").parse().map_err(|e| format!("TIMEOUT_SEC: {e}"))?,
            max_positions: optional("MAX_POSITIONS", "3").parse().map_err(|e| format!("MAX_POSITIONS: {e}"))?,
            slippage_pct: optional("SLIPPAGE_PCT", "15").parse().map_err(|e| format!("SLIPPAGE_PCT: {e}"))?,
            dry_run: parse_bool(&optional("DRY_RUN", "true")).map_err(|e| format!("DRY_RUN: {e}"))?,
            jito_urls: optional("JITO_URLS", &optional("JITO_URL", crate::jito::DEFAULT_BLOCK_ENGINE_URL))
                .split(',').map(crate::jito::base_url).filter(|u| !u.is_empty()).collect(),
            relays: crate::sender::Relay::parse_list(&optional("RELAYS", "sender,jito")).map_err(|e| format!("RELAYS: {e}"))?,
            sender_url: optional("HELIUS_SENDER_URL", crate::sender::DEFAULT_SENDER_URL),
            sender_tip_lamports: parse_fixed(&optional("SENDER_TIP", "0.000005"), 9).map_err(|e| format!("SENDER_TIP: {e}"))?,
            priority_fee: parse_auto(
                &optional("PRIORITY_FEE", "auto"), 0,
                Auto::Auto {
                    min: optional("PRIORITY_FEE_MIN", "50000").parse().map_err(|e| format!("PRIORITY_FEE_MIN: {e}"))?,
                    max: optional("PRIORITY_FEE_MAX", "3000000").parse().map_err(|e| format!("PRIORITY_FEE_MAX: {e}"))?,
                },
            ).map_err(|e| format!("PRIORITY_FEE: {e}"))?,
            cu_limit_buy: optional("CU_LIMIT_BUY", &DEFAULT_CU_LIMIT_BUY.to_string()).parse().map_err(|e| format!("CU_LIMIT_BUY: {e}"))?,
            cu_limit_sell: optional("CU_LIMIT_SELL", &DEFAULT_CU_LIMIT_SELL.to_string()).parse().map_err(|e| format!("CU_LIMIT_SELL: {e}"))?,
            cu_limit_amm_buy: optional("CU_LIMIT_AMM_BUY", &DEFAULT_CU_LIMIT_AMM_BUY.to_string()).parse().map_err(|e| format!("CU_LIMIT_AMM_BUY: {e}"))?,
            cu_limit_amm_sell: optional("CU_LIMIT_AMM_SELL", &DEFAULT_CU_LIMIT_AMM_SELL.to_string()).parse().map_err(|e| format!("CU_LIMIT_AMM_SELL: {e}"))?,
            socket_path: optional("SOCKET_PATH", DEFAULT_SOCKET_PATH),
            trailing_stop_bps: parse_fixed(&optional("TRAILING_STOP", "0.25"), 4).map_err(|e| format!("TRAILING_STOP: {e}"))?,
            exit_slippage_pct: optional("EXIT_SLIPPAGE_PCT", "40").parse().map_err(|e| format!("EXIT_SLIPPAGE_PCT: {e}"))?,
            monitor_interval_ms: optional("MONITOR_INTERVAL_MS", "400").parse().map_err(|e| format!("MONITOR_INTERVAL_MS: {e}"))?,
            min_confidence: optional("MIN_CONFIDENCE", "0").parse().map_err(|e| format!("MIN_CONFIDENCE: {e}"))?,
            max_signal_age_ms: optional("MAX_SIGNAL_AGE_MS", "6000").parse().map_err(|e| format!("MAX_SIGNAL_AGE_MS: {e}"))?,
            max_session_loss_lamports: 0, // set below (default depends on BUY_SOL)
            max_consecutive_losses: optional("MAX_CONSECUTIVE_LOSSES", "5").parse().map_err(|e| format!("MAX_CONSECUTIVE_LOSSES: {e}"))?,
            min_sol_reserve_lamports: parse_fixed(&optional("MIN_SOL_RESERVE", "0.01"), 9).map_err(|e| format!("MIN_SOL_RESERVE: {e}"))?,
            max_mcap_lamports: parse_fixed(&optional("MAX_MCAP_SOL", "0"), 9).map_err(|e| format!("MAX_MCAP_SOL: {e}"))?,
            rebuy: parse_bool(&optional("REBUY", "false")).map_err(|e| format!("REBUY: {e}"))?,
            state_dir: optional("STATE_DIR", "logs"),
        };
        let mut cfg = cfg;
        // Default loss cap: 3 positions' worth. Explicit 0 disables it.
        cfg.max_session_loss_lamports = match env::var("MAX_SESSION_LOSS").ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty()) {
            Some(v) => parse_fixed(&v, 9).map_err(|e| format!("MAX_SESSION_LOSS: {e}"))?,
            None => cfg.buy_lamports.saturating_mul(3),
        };
        cfg.validate()?;
        Ok(cfg)
    }

    fn validate(&self) -> Result<()> {
        if self.buy_lamports == 0 {
            return Err("BUY_SOL must be > 0".into());
        }
        if self.take_profit_bps <= 10_000 {
            return Err("TAKE_PROFIT must be > 1.0".into());
        }
        if self.stop_loss_bps >= 10_000 {
            return Err("STOP_LOSS must be < 1.0".into());
        }
        if self.slippage_pct >= 100 {
            return Err("SLIPPAGE_PCT must be < 100".into());
        }
        if self.max_positions == 0 {
            return Err("MAX_POSITIONS must be >= 1".into());
        }
        if self.jito_urls.is_empty() && self.relays.contains(&crate::sender::Relay::Jito) {
            return Err("JITO_URLS is empty".into());
        }
        if self.relays.contains(&crate::sender::Relay::Sender) && self.sender_tip_lamports < crate::sender::SENDER_SWQOS_MIN_TIP {
            return Err("SENDER_TIP must be >= 0.000005 SOL (Helius Sender minimum)".into());
        }
        if !(10_000..=1_400_000).contains(&self.cu_limit_buy) || !(10_000..=1_400_000).contains(&self.cu_limit_sell) {
            return Err("CU_LIMIT_BUY / CU_LIMIT_SELL must be within 10000..=1400000".into());
        }
        if self.trailing_stop_bps >= 10_000 {
            return Err("TRAILING_STOP must be < 1.0 (fraction below the peak), 0 = off".into());
        }
        if self.exit_slippage_pct >= 100 || self.exit_slippage_pct < self.slippage_pct {
            return Err("EXIT_SLIPPAGE_PCT must be in [SLIPPAGE_PCT, 100)".into());
        }
        if !(100..=10_000).contains(&self.monitor_interval_ms) {
            return Err("MONITOR_INTERVAL_MS must be within 100..=10000".into());
        }
        // Helius Sender rejects txs without a compute-unit price.
        if self.relays.contains(&crate::sender::Relay::Sender) && self.priority_fee == Auto::Fixed(0) {
            return Err("PRIORITY_FEE=0 is rejected by Helius Sender (needs a compute-unit price)".into());
        }
        if !self.dry_run && self.wallet_private_key.is_none() {
            return Err("WALLET_PRIVATE_KEY is required when DRY_RUN=false".into());
        }
        Ok(())
    }
}

/// `auto` → `auto_default`; otherwise a fixed decimal scaled by 10^decimals.
fn parse_auto(s: &str, decimals: u32, auto_default: Auto) -> std::result::Result<Auto, String> {
    if s.eq_ignore_ascii_case("auto") {
        Ok(auto_default)
    } else {
        parse_fixed(s, decimals).map(Auto::Fixed)
    }
}

#[cfg(test)]
impl Config {
    pub fn test_default() -> Self {
        Config {
            rpc_url: String::new(),
            wallet_private_key: None,
            buy_lamports: 1,
            jito_tip: Auto::Fixed(1_000),
            take_profit_bps: 20_000,
            stop_loss_bps: 5_000,
            timeout_sec: 120,
            max_positions: 2,
            slippage_pct: 5,
            dry_run: true,
            jito_urls: vec![crate::jito::DEFAULT_BLOCK_ENGINE_URL.into()],
            relays: vec![crate::sender::Relay::Jito],
            sender_url: crate::sender::DEFAULT_SENDER_URL.into(),
            sender_tip_lamports: 5_000,
            priority_fee: Auto::Fixed(0),
            cu_limit_buy: DEFAULT_CU_LIMIT_BUY,
            cu_limit_sell: DEFAULT_CU_LIMIT_SELL,
            cu_limit_amm_buy: DEFAULT_CU_LIMIT_AMM_BUY,
            cu_limit_amm_sell: DEFAULT_CU_LIMIT_AMM_SELL,
            socket_path: String::new(),
            trailing_stop_bps: 2_500,
            exit_slippage_pct: 40,
            monitor_interval_ms: 400,
            min_confidence: 0.0,
            max_signal_age_ms: 0,
            max_session_loss_lamports: 0,
            max_consecutive_losses: 0,
            min_sol_reserve_lamports: 0,
            max_mcap_lamports: 0,
            rebuy: false,
            state_dir: String::new(),
        }
    }
}

/// Decode a base58-encoded 64-byte secret key.
pub fn load_keypair(b58: &str) -> Result<Keypair> {
    let bytes = bs58::decode(b58.trim())
        .into_vec()
        .map_err(|e| format!("WALLET_PRIVATE_KEY: invalid base58: {e}"))?;
    if bytes.len() != 64 {
        return Err(format!("WALLET_PRIVATE_KEY: expected 64 bytes, got {}", bytes.len()).into());
    }
    Keypair::try_from(bytes.as_slice()).map_err(|e| format!("WALLET_PRIVATE_KEY: {e}").into())
}

fn required(key: &str) -> Result<String> {
    env::var(key)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .ok_or_else(|| format!("missing required env var {key}").into())
}

fn optional(key: &str, default: &str) -> String {
    env::var(key)
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn parse_bool(s: &str) -> std::result::Result<bool, String> {
    match s.to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Ok(true),
        "0" | "false" | "no" | "off" => Ok(false),
        other => Err(format!("expected true/false, got {other:?}")),
    }
}

/// Parse a non-negative decimal string into an integer scaled by 10^decimals.
/// `parse_fixed("0.05", 9) == 50_000_000`. Rejects more fractional digits than
/// `decimals` rather than silently rounding.
pub fn parse_fixed(s: &str, decimals: u32) -> std::result::Result<u64, String> {
    let s = s.trim();
    let (int_part, frac_part) = match s.split_once('.') {
        Some((i, f)) => (i, f),
        None => (s, ""),
    };
    if int_part.is_empty() && frac_part.is_empty() {
        return Err("empty number".into());
    }
    if !int_part.chars().all(|c| c.is_ascii_digit()) || !frac_part.chars().all(|c| c.is_ascii_digit()) {
        return Err(format!("not a non-negative decimal: {s:?}"));
    }
    if frac_part.len() > decimals as usize {
        return Err(format!("{s:?} has more than {decimals} decimal places"));
    }
    let scale = 10u64.pow(decimals);
    let int: u64 = if int_part.is_empty() { 0 } else { int_part.parse().map_err(|e| format!("{e}"))? };
    let frac: u64 = if frac_part.is_empty() {
        0
    } else {
        let padded = format!("{frac_part:0<width$}", width = decimals as usize);
        padded.parse().map_err(|e| format!("{e}"))?
    };
    int.checked_mul(scale)
        .and_then(|v| v.checked_add(frac))
        .ok_or_else(|| format!("{s:?} overflows"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_point_parsing() {
        assert_eq!(parse_fixed("0.1", 9).unwrap(), 100_000_000);
        assert_eq!(parse_fixed("1", 9).unwrap(), 1_000_000_000);
        assert_eq!(parse_fixed("0.000001", 9).unwrap(), 1_000);
        assert_eq!(parse_fixed(".5", 4).unwrap(), 5_000);
        assert_eq!(parse_fixed("2.0", 4).unwrap(), 20_000);
        assert!(parse_fixed("0.0000000001", 9).is_err());
        assert!(parse_fixed("-1", 9).is_err());
        assert!(parse_fixed("abc", 9).is_err());
        assert!(parse_fixed("", 9).is_err());
    }

    #[test]
    fn auto_settings() {
        let d = Auto::Auto { min: 10, max: 100 };
        assert_eq!(parse_auto("auto", 9, d).unwrap(), d);
        assert_eq!(parse_auto("0.001", 9, d).unwrap(), Auto::Fixed(1_000_000));
        assert_eq!(d.resolve(5), 10);
        assert_eq!(d.resolve(500), 100);
        assert_eq!(Auto::Fixed(7).resolve(500), 7);
    }

    #[test]
    fn keypair_roundtrip() {
        use solana_sdk::signer::Signer;
        let kp = Keypair::new();
        let b58 = bs58::encode(kp.to_bytes()).into_string();
        assert_eq!(load_keypair(&b58).unwrap().pubkey(), kp.pubkey());
        assert!(load_keypair("abc").is_err());
    }
}
