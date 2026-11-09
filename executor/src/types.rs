//! Shared types: IPC messages, errors, exit reasons.

use serde::{Deserialize, Serialize};
use std::fmt;

pub type BoxError = Box<dyn std::error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, BoxError>;

pub const LAMPORTS_PER_SOL: u64 = 1_000_000_000;
/// pump.fun tokens use 6 decimals.
pub const TOKEN_DECIMALS: u32 = 6;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Action {
    Buy,
    /// Manual exit of an open position.
    Sell,
}

/// Incoming signal from the Python process (one JSON object per line).
#[derive(Debug, Clone, Deserialize)]
pub struct Signal {
    pub mint: String,
    #[serde(default)]
    pub ticker: String,
    pub action: Action,
    #[serde(default)]
    pub confidence: f64,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub context: String,
    #[serde(default)]
    pub timestamp: f64,
}

/// Reply written back on the socket (one JSON object per line).
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "status", rename_all = "lowercase")]
pub enum Response {
    Ok { bundle_id: String },
    Error { message: String },
}

impl Response {
    pub fn ok(bundle_id: impl Into<String>) -> Self {
        Response::Ok { bundle_id: bundle_id.into() }
    }
    pub fn error(message: impl Into<String>) -> Self {
        Response::Error { message: message.into() }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExitReason {
    TakeProfit,
    StopLoss,
    /// Value fell TRAILING_STOP below its peak after the trail armed.
    TrailingStop,
    Timeout,
    Manual,
    /// Curve completed (migrated) — can no longer sell on the bonding curve.
    Graduated,
}

impl ExitReason {
    /// Exits where getting out matters more than price: escalate slippage.
    pub fn urgent(self) -> bool {
        matches!(self, ExitReason::StopLoss | ExitReason::TrailingStop | ExitReason::Timeout | ExitReason::Manual)
    }
}

impl fmt::Display for ExitReason {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            ExitReason::TakeProfit => "TAKE_PROFIT",
            ExitReason::StopLoss => "STOP_LOSS",
            ExitReason::TrailingStop => "TRAILING_STOP",
            ExitReason::Timeout => "TIMEOUT",
            ExitReason::Manual => "MANUAL",
            ExitReason::Graduated => "GRADUATED",
        })
    }
}

/// Format signed lamports as SOL without going through f64.
pub fn fmt_sol(lamports: i128) -> String {
    let sign = if lamports < 0 { "-" } else { "+" };
    let abs = lamports.unsigned_abs();
    let whole = abs / LAMPORTS_PER_SOL as u128;
    let frac = (abs % LAMPORTS_PER_SOL as u128) / 100_000; // 4 decimals
    format!("{sign}{whole}.{frac:04} SOL")
}

/// Unsigned variant (no sign prefix).
pub fn fmt_sol_u(lamports: u64) -> String {
    fmt_sol(lamports as i128).trim_start_matches('+').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signal_parses_spec_example() {
        let s: Signal = serde_json::from_str(
            r#"{"mint":"DYdfB51GxXj1SydBJm8uJbndgPBTs8dYwZEg6yF6pump","ticker":"SOMETHING","action":"buy","confidence":0.85,"source":"both","context":"OCR","timestamp":1699999999.123}"#,
        )
        .unwrap();
        assert_eq!(s.action, Action::Buy);
        assert_eq!(s.ticker, "SOMETHING");
    }

    #[test]
    fn response_shape() {
        assert_eq!(
            serde_json::to_string(&Response::ok("abc")).unwrap(),
            r#"{"status":"ok","bundle_id":"abc"}"#
        );
        assert_eq!(
            serde_json::to_string(&Response::error("x")).unwrap(),
            r#"{"status":"error","message":"x"}"#
        );
    }

    #[test]
    fn sol_formatting() {
        assert_eq!(fmt_sol(150_000_000), "+0.1500 SOL");
        assert_eq!(fmt_sol(-1_250_000_000), "-1.2500 SOL");
        assert_eq!(fmt_sol_u(10_000), "0.0000 SOL");
    }
}
