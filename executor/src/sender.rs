//! Fan-out submission: one signed transaction → every configured relay in parallel.
//!
//! The same signature can execute only once, so racing Helius Sender, several Jito
//! regions and the RPC is safe — the tx carries one tip per relay that needs it.
//! Landing is tracked with `getSignatureStatuses` (works whichever relay won).

use crate::jito::{self, JitoClient};
use crate::rpc::{http_client, json_rpc, Rpc};
use crate::types::Result;
use serde_json::json;
use solana_sdk::pubkey;
use solana_sdk::pubkey::Pubkey;
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const DEFAULT_SENDER_URL: &str = "https://sender.helius-rpc.com/fast";
/// Sender Max (all paths, priority buffer) needs ≥ 0.001 SOL; below that only the
/// cost-optimized `?swqos_only=true` tier (≥ 0.000005 SOL) is valid.
pub const SENDER_MAX_MIN_TIP: u64 = 1_000_000;
pub const SENDER_SWQOS_MIN_TIP: u64 = 5_000;
const SENDER_PING_EVERY: Duration = Duration::from_secs(4);
/// ~1 slot at 200 ms.
const STATUS_POLL: Duration = Duration::from_millis(200);
const HEIGHT_CHECK_EVERY: Duration = Duration::from_secs(2);
/// Safety net if the RPC never answers; validity is ~150 blocks ≈ 30 s + skips.
const LAND_HARD_CAP: Duration = Duration::from_secs(120);

/// From https://www.helius.dev/docs/sending-transactions/sender (2026-11-09).
pub const SENDER_TIP_ACCOUNTS: [Pubkey; 10] = [
    pubkey!("4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"),
    pubkey!("D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ"),
    pubkey!("9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta"),
    pubkey!("5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn"),
    pubkey!("2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD"),
    pubkey!("2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ"),
    pubkey!("wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF"),
    pubkey!("3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT"),
    pubkey!("4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey"),
    pubkey!("4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or"),
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Relay {
    Sender,
    Jito,
    Rpc,
}

impl Relay {
    pub fn parse_list(s: &str) -> std::result::Result<Vec<Relay>, String> {
        let mut out = Vec::new();
        for p in s.split(',').map(|p| p.trim().to_ascii_lowercase()).filter(|p| !p.is_empty()) {
            let r = match p.as_str() {
                "sender" | "helius" => Relay::Sender,
                "jito" => Relay::Jito,
                "rpc" => Relay::Rpc,
                other => return Err(format!("unknown relay {other:?} (sender|jito|rpc)")),
            };
            if !out.contains(&r) {
                out.push(r);
            }
        }
        if out.is_empty() {
            return Err("no relays configured".into());
        }
        Ok(out)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LandStatus {
    /// Executed successfully, confirmed or better.
    Landed { slot: u64 },
    /// Executed and failed on-chain (slippage, …): nothing changed except fees.
    Failed(String),
    /// Block height passed the blockhash's lastValidBlockHeight: it can never land.
    Expired,
    /// Gave up waiting (RPC trouble) without proof either way — treat as "may have landed".
    Unknown,
}

impl LandStatus {
    /// True when the tx definitely did not change token balances.
    pub fn definitely_not_landed(&self) -> bool {
        matches!(self, LandStatus::Failed(_) | LandStatus::Expired)
    }
}

pub struct Submitter {
    http: reqwest::Client,
    pub relays: Vec<Relay>,
    /// Full Sender URL incl. `?swqos_only=true` / api-key as configured.
    pub sender_url: String,
    pub sender_tip: u64,
    pub jito: Arc<JitoClient>,
    rpc: Arc<Rpc>,
}

impl Submitter {
    pub fn new(relays: Vec<Relay>, sender_url: String, sender_tip: u64, jito: Arc<JitoClient>, rpc: Arc<Rpc>) -> Self {
        Submitter { http: http_client(), relays, sender_url, sender_tip, jito, rpc }
    }

    pub fn uses(&self, r: Relay) -> bool {
        self.relays.contains(&r)
    }

    /// Tip transfers this tx must carry for the configured relays.
    pub fn tips(&self, jito_tip: u64) -> Vec<(Pubkey, u64)> {
        let mut t = Vec::with_capacity(2);
        if self.uses(Relay::Jito) {
            t.push((jito::random_tip_account(), jito_tip.max(jito::MIN_TIP_LAMPORTS)));
        }
        if self.uses(Relay::Sender) {
            t.push((jito::pick(&SENDER_TIP_ACCOUNTS), self.sender_tip));
        }
        t
    }

    /// Send to every relay concurrently (one task each); Ok(signature) if at least
    /// one accepted it. Returns as soon as the first relay accepts; the rest finish
    /// in the background and are logged.
    pub async fn submit(self: &Arc<Self>, tx_b64: String) -> Result<String> {
        let t0 = Instant::now();
        let tx: Arc<str> = tx_b64.into();
        let (done_tx, mut done_rx) = tokio::sync::mpsc::unbounded_channel::<(String, Result<String>)>();
        let mut n = 0usize;
        let mut spawn = |name: String, kind: Relay, base: Option<String>| {
            let (me, tx, done) = (Arc::clone(self), Arc::clone(&tx), done_tx.clone());
            n += 1;
            tokio::spawn(async move {
                let r = match kind {
                    Relay::Sender => me.send_sender(&tx).await,
                    Relay::Jito => me.jito.send_transaction(base.as_deref().unwrap_or_default(), &tx).await,
                    Relay::Rpc => me.rpc.send_transaction(&tx).await,
                };
                let _ = done.send((name, r));
            });
        };
        if self.uses(Relay::Sender) {
            spawn("sender".into(), Relay::Sender, None);
        }
        if self.uses(Relay::Jito) {
            for base in &self.jito.urls {
                let host = base.trim_start_matches("https://").split('.').next().unwrap_or("jito").to_string();
                spawn(format!("jito:{host}"), Relay::Jito, Some(base.clone()));
            }
        }
        if self.uses(Relay::Rpc) {
            spawn("rpc".into(), Relay::Rpc, None);
        }
        drop(done_tx);

        let mut errors = Vec::new();
        while let Some((name, r)) = done_rx.recv().await {
            match r {
                Ok(sig) => {
                    log_info!("{name} accepted in {}ms ({} relays)", t0.elapsed().as_millis(), n);
                    // Log the stragglers without blocking the caller.
                    tokio::spawn(async move {
                        while let Some((name, r)) = done_rx.recv().await {
                            match r {
                                Ok(_) => log_dim!("  {name} accepted (+{}ms)", t0.elapsed().as_millis()),
                                Err(e) => log_warn!("  {name} rejected: {e}"),
                            }
                        }
                    });
                    for e in errors {
                        log_warn!("  {e}");
                    }
                    return Ok(sig);
                }
                Err(e) => errors.push(format!("{name}: {e}")),
            }
        }
        Err(format!("all relays rejected: {}", errors.join("; ")).into())
    }

    async fn send_sender(&self, tx_b64: &str) -> Result<String> {
        let params = json!([tx_b64, { "encoding": "base64", "skipPreflight": true, "maxRetries": 0 }]);
        let v = json_rpc(&self.http, &self.sender_url, "sendTransaction", &params).await?;
        v.as_str().map(String::from).ok_or_else(|| format!("sender: unexpected result {v}").into())
    }

    /// Poll the signature until it is confirmed (ok or failed) or its blockhash expires.
    ///
    /// Expiry is decided by block height, not wall-clock: skipped slots stretch the
    /// ~150-block validity window, and a tx reported "timed out" that lands later is
    /// exactly how bots end up holding untracked tokens or buying twice.
    pub async fn wait_for_landing(&self, signature: &str, last_valid_height: u64) -> LandStatus {
        let t0 = Instant::now();
        let mut next_height_check = Instant::now() + HEIGHT_CHECK_EVERY;
        let mut expired = false;
        loop {
            tokio::time::sleep(STATUS_POLL).await;
            match self.rpc.signature_status(signature).await {
                Ok(Some(st)) if st.confirmed => {
                    return match st.err {
                        None => LandStatus::Landed { slot: st.slot },
                        Some(e) => LandStatus::Failed(e),
                    };
                }
                Ok(_) => {}
                Err(e) => log_warn!("getSignatureStatuses {}…: {e}", &signature[..8.min(signature.len())]),
            }
            if expired {
                // One more status read after the height passed the limit (it might have
                // landed in the very last valid block).
                return LandStatus::Expired;
            }
            if t0.elapsed() >= LAND_HARD_CAP {
                return LandStatus::Unknown;
            }
            if Instant::now() >= next_height_check {
                next_height_check = Instant::now() + HEIGHT_CHECK_EVERY;
                match self.rpc.block_height().await {
                    Ok(h) if h > last_valid_height => expired = true,
                    Ok(_) => {}
                    Err(e) => log_warn!("getBlockHeight: {e}"),
                }
            }
        }
    }

    /// Keep the TLS connection to Sender warm between sparse signals (Helius: ping if
    /// sends are > 5 s apart).
    pub fn spawn_sender_keepalive(self: &Arc<Self>) {
        if !self.uses(Relay::Sender) {
            return;
        }
        let me = Arc::clone(self);
        tokio::spawn(async move {
            let ping = ping_url(&me.sender_url);
            let mut tick = tokio::time::interval(SENDER_PING_EVERY);
            loop {
                tick.tick().await;
                let _ = me.http.get(&ping).send().await;
            }
        });
    }
}

/// `…/fast?x=y` → `…/ping`.
pub fn ping_url(sender_url: &str) -> String {
    let base = sender_url.split('?').next().unwrap_or(sender_url).trim_end_matches('/');
    let base = base.strip_suffix("/fast").unwrap_or(base);
    format!("{base}/ping")
}

/// Apply the tier rule: below the Sender Max minimum, force `?swqos_only=true`.
pub fn sender_url_for_tip(url: &str, tip: u64) -> String {
    if tip >= SENDER_MAX_MIN_TIP || url.contains("swqos_only") {
        return url.to_string();
    }
    let sep = if url.contains('?') { '&' } else { '?' };
    format!("{url}{sep}swqos_only=true")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relay_list_parsing() {
        assert_eq!(Relay::parse_list("sender, jito").unwrap(), vec![Relay::Sender, Relay::Jito]);
        assert_eq!(Relay::parse_list("jito,jito,rpc").unwrap(), vec![Relay::Jito, Relay::Rpc]);
        assert!(Relay::parse_list("").is_err());
        assert!(Relay::parse_list("bogus").is_err());
    }

    #[test]
    fn sender_tier_and_ping_urls() {
        assert_eq!(sender_url_for_tip(DEFAULT_SENDER_URL, 5_000), "https://sender.helius-rpc.com/fast?swqos_only=true");
        assert_eq!(sender_url_for_tip(DEFAULT_SENDER_URL, 1_000_000), DEFAULT_SENDER_URL);
        assert_eq!(sender_url_for_tip("http://ewr-sender.helius-rpc.com/fast?api-key=k", 10), "http://ewr-sender.helius-rpc.com/fast?api-key=k&swqos_only=true");
        assert_eq!(ping_url("http://ewr-sender.helius-rpc.com/fast?api-key=k"), "http://ewr-sender.helius-rpc.com/ping");
    }
}
