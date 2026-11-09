//! On-disk state so a crash or restart never strands tokens without TP/SL/timeout.
//!
//! - `positions[.dry].json`: snapshot of every tracked position (pending / open /
//!   closing), rewritten atomically (tmp + rename) on each state change;
//! - `trades[.dry].jsonl`: append-only event log (buy, sell, close, failures) — the
//!   data for judging a strategy from paper runs.
//!
//! On a live start the executor reloads the snapshot and reconciles each entry with
//! the wallet's on-chain token balance (see `main.rs::restore_positions`).

use crate::types::Result;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub fn unix_now() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64()
}

/// Serializable form of one tracked position.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Saved {
    /// "pending" | "open" | "closing"
    pub state: String,
    pub mint: String,
    pub ticker: String,
    pub token_program: String,
    pub token_amount: u64,
    pub cost_lamports: u64,
    pub mint_supply: u64,
    pub entry_price: u64,
    pub entry_unix: f64,
    pub peak_value: u64,
    pub rent_lamports: u64,
    pub buy_sig: String,
    /// "curve" | "amm:<pool>:<base vault>:<quote vault>" (missing in old journals = curve)
    #[serde(default)]
    pub venue: String,
}

pub struct Journal {
    snapshot: PathBuf,
    trades: PathBuf,
    lock: Mutex<()>,
}

impl Journal {
    pub fn open(dir: &str, dry_run: bool) -> Result<Self> {
        let dir = PathBuf::from(dir);
        fs::create_dir_all(&dir).map_err(|e| format!("STATE_DIR {}: {e}", dir.display()))?;
        let sfx = if dry_run { ".dry" } else { "" };
        Ok(Journal {
            snapshot: dir.join(format!("positions{sfx}.json")),
            trades: dir.join(format!("trades{sfx}.jsonl")),
            lock: Mutex::new(()),
        })
    }

    pub fn paths(&self) -> (String, String) {
        (self.snapshot.display().to_string(), self.trades.display().to_string())
    }

    pub fn load(&self) -> Result<Vec<Saved>> {
        match fs::read_to_string(&self.snapshot) {
            Ok(s) if s.trim().is_empty() => Ok(vec![]),
            Ok(s) => Ok(serde_json::from_str(&s).map_err(|e| format!("{}: {e}", self.snapshot.display()))?),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(vec![]),
            Err(e) => Err(format!("{}: {e}", self.snapshot.display()).into()),
        }
    }

    /// Atomic rewrite. Errors are logged, never fatal: trading must not stop
    /// because the disk is full, but the operator must know.
    pub fn save(&self, all: &[Saved]) {
        let _g = self.lock.lock().unwrap();
        let tmp = self.snapshot.with_extension("json.tmp");
        let res = serde_json::to_vec_pretty(all)
            .map_err(|e| e.to_string())
            .and_then(|b| fs::write(&tmp, b).map_err(|e| e.to_string()))
            .and_then(|_| fs::rename(&tmp, &self.snapshot).map_err(|e| e.to_string()));
        if let Err(e) = res {
            log_error!("journal: cannot write {}: {e}", self.snapshot.display());
        }
    }

    /// Append one event line `{"ts":…, "event":…, …fields}`.
    pub fn event(&self, event: &str, mut fields: Value) {
        if let Value::Object(m) = &mut fields {
            m.insert("ts".into(), Value::from(unix_now()));
            m.insert("event".into(), Value::from(event));
        }
        let _g = self.lock.lock().unwrap();
        let res = OpenOptions::new().create(true).append(true).open(&self.trades).and_then(|mut f| {
            let mut line = serde_json::to_vec(&fields).unwrap_or_default();
            line.push(b'\n');
            f.write_all(&line)
        });
        if let Err(e) = res {
            log_error!("journal: cannot append {}: {e}", self.trades.display());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_roundtrip_and_events() {
        let dir = std::env::temp_dir().join(format!("mevs-journal-{}", std::process::id()));
        let j = Journal::open(dir.to_str().unwrap(), true).unwrap();
        assert!(j.load().unwrap().is_empty());
        let s = Saved {
            state: "open".into(), mint: "m".into(), ticker: "T".into(), token_program: "p".into(),
            token_amount: 1, cost_lamports: 2, mint_supply: 3, entry_price: 4, entry_unix: 5.0,
            peak_value: 6, rent_lamports: 7, buy_sig: "s".into(), venue: String::new(),
        };
        j.save(std::slice::from_ref(&s));
        assert_eq!(j.load().unwrap(), vec![s]);
        j.event("buy", serde_json::json!({"mint": "m"}));
        j.event("close", serde_json::json!({"mint": "m", "pnl": -1}));
        let log = fs::read_to_string(dir.join("trades.dry.jsonl")).unwrap();
        assert_eq!(log.lines().count(), 2);
        assert!(log.contains(r#""event":"close""#));
        let _ = fs::remove_dir_all(dir);
    }
}
