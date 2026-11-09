//! Jito block engine: low-latency `sendTransaction` (bundleOnly) and the live tip floor.
//!
//! Docs: https://docs.jito.wtf/lowlatencytxnsend/ — `sendTransaction` with
//! `?bundleOnly=true` is sent as a single-tx bundle (revert protection); the tip
//! is a plain SOL transfer to a tip account inside the same transaction.

use crate::rpc::{http_client, json_rpc};
use crate::types::Result;
use serde_json::{json, Value};
use solana_sdk::pubkey;
use solana_sdk::pubkey::Pubkey;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const DEFAULT_BLOCK_ENGINE_URL: &str = "https://mainnet.block-engine.jito.wtf";
pub const TIP_FLOOR_URL: &str = "https://bundles.jito.wtf/api/v1/bundles/tip_floor";
/// Jito's minimum tip.
pub const MIN_TIP_LAMPORTS: u64 = 1_000;
const TIP_FLOOR_REFRESH: Duration = Duration::from_secs(2);

/// Matches `getTipAccounts` on 2026-11-09.
pub const TIP_ACCOUNTS: [Pubkey; 8] = [
    pubkey!("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5"),
    pubkey!("HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe"),
    pubkey!("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY"),
    pubkey!("ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49"),
    pubkey!("DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh"),
    pubkey!("ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt"),
    pubkey!("DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL"),
    pubkey!("3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"),
];

/// Non-cryptographic pick; only spreads write-lock contention across tip accounts.
pub fn pick<T: Copy>(items: &[T]) -> T {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0) as usize;
    items[(nanos ^ (nanos >> 7)) % items.len()]
}

pub fn random_tip_account() -> Pubkey {
    pick(&TIP_ACCOUNTS)
}

/// `…/api/v1/bundles` (old JITO_URL style) or a bare host → block engine base URL.
pub fn base_url(url: &str) -> String {
    let u = url.trim().trim_end_matches('/');
    u.split("/api/v1").next().unwrap_or(u).to_string()
}

pub struct JitoClient {
    http: reqwest::Client,
    /// Block engine base URLs (regional engines can be listed side by side).
    pub urls: Vec<String>,
    /// Latest landed-tip percentile in lamports (0 = unknown yet).
    tip_floor: AtomicU64,
}

impl JitoClient {
    pub fn new(urls: Vec<String>) -> Self {
        JitoClient { http: http_client(), urls, tip_floor: AtomicU64::new(0) }
    }

    /// Send one signed tx (base64) to one engine. Returns the signature.
    pub async fn send_transaction(&self, base: &str, tx_b64: &str) -> Result<String> {
        let url = format!("{base}/api/v1/transactions?bundleOnly=true");
        let v = json_rpc(&self.http, &url, "sendTransaction", &json!([tx_b64, { "encoding": "base64" }])).await?;
        v.as_str().map(String::from).ok_or_else(|| format!("jito sendTransaction: unexpected result {v}").into())
    }

    pub fn tip_floor(&self) -> u64 {
        self.tip_floor.load(Ordering::Relaxed)
    }

    async fn fetch_tip_floor(&self, percentile_field: &str) -> Result<u64> {
        let v: Value = self.http.get(TIP_FLOOR_URL).send().await?.error_for_status()?.json().await?;
        let sol = v[0][percentile_field].as_f64().ok_or("tip_floor: missing field")?;
        Ok((sol * 1e9).round() as u64)
    }

    /// Background refresh of the landed-tip percentile used by `JITO_TIP=auto`.
    pub fn spawn_tip_floor_refresher(self: &Arc<Self>, percentile_field: &'static str) {
        let me = Arc::clone(self);
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(TIP_FLOOR_REFRESH);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            let mut warned = false;
            loop {
                tick.tick().await;
                match me.fetch_tip_floor(percentile_field).await {
                    Ok(l) => {
                        me.tip_floor.store(l, Ordering::Relaxed);
                        warned = false;
                    }
                    Err(e) if !warned => {
                        log_warn!("jito tip_floor fetch failed: {e}");
                        warned = true;
                    }
                    Err(_) => {}
                }
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tip_account_is_from_list() {
        for _ in 0..50 {
            assert!(TIP_ACCOUNTS.contains(&random_tip_account()));
        }
    }

    #[test]
    fn base_url_accepts_old_and_new_forms() {
        assert_eq!(base_url("https://mainnet.block-engine.jito.wtf/api/v1/bundles"), DEFAULT_BLOCK_ENGINE_URL);
        assert_eq!(base_url("https://ny.mainnet.block-engine.jito.wtf/"), "https://ny.mainnet.block-engine.jito.wtf");
    }
}
