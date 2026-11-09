//! Minimal Solana JSON-RPC client over reqwest (keep-alive, no QUIC stack).
//!
//! `solana-client` 2.x can't be resolved alongside `solana-sdk` 2.x right now
//! (zeroize / ed25519-dalek pin conflict), and we only need a handful of methods.

use crate::types::{BoxError, Result};
use base64::Engine;
use serde_json::{json, Value};
use solana_sdk::hash::Hash;
use solana_sdk::pubkey::Pubkey;
use std::future::Future;
use std::str::FromStr;
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

/// Since 200 ms slots (SIMD-0525, Oct 2026) a blockhash is valid for ~150 blocks ≈ 30 s.
/// Refresh often and never sign with a hash older than 10 s.
pub const BLOCKHASH_TTL: Duration = Duration::from_secs(10);
pub const BLOCKHASH_REFRESH: Duration = Duration::from_secs(2);
pub const RETRY_DELAY: Duration = Duration::from_millis(500);

pub fn http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .tcp_nodelay(true)
        .pool_idle_timeout(Duration::from_secs(90))
        .pool_max_idle_per_host(8)
        .timeout(Duration::from_secs(5))
        .build()
        .expect("reqwest client")
}

/// Run `f`; on failure log, wait 500ms, try exactly once more.
pub async fn retry_once<T, F, Fut>(what: &str, mut f: F) -> Result<T>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<T>>,
{
    match f().await {
        Ok(v) => Ok(v),
        Err(e) => {
            log_warn!("{what} failed: {e} — retrying in {}ms", RETRY_DELAY.as_millis());
            tokio::time::sleep(RETRY_DELAY).await;
            f().await
        }
    }
}

/// POST a JSON-RPC 2.0 request and return `result`.
pub async fn json_rpc(http: &reqwest::Client, url: &str, method: &str, params: &Value) -> Result<Value> {
    let body = json!({ "jsonrpc": "2.0", "id": 1, "method": method, "params": params });
    let resp = http.post(url).json(&body).send().await?;
    let status = resp.status();
    let v: Value = resp.json().await.map_err(|e| format!("{method}: HTTP {status}, bad body: {e}"))?;
    if let Some(err) = v.get("error") {
        return Err(format!("{method}: {err}").into());
    }
    if !status.is_success() {
        return Err(format!("{method}: HTTP {status}").into());
    }
    v.get("result").cloned().ok_or_else(|| -> BoxError { format!("{method}: missing result").into() })
}

#[derive(Debug, Clone)]
pub struct AccountData {
    pub owner: Pubkey,
    pub lamports: u64,
    pub data: Vec<u8>,
}

impl AccountData {
    /// SPL token account `amount` (offset 64) — same for Token and Token-2022.
    pub fn token_amount(&self) -> Option<u64> {
        self.data.get(64..72).map(|b| u64::from_le_bytes(b.try_into().unwrap()))
    }
}

#[derive(Debug)]
pub struct SimResult {
    pub err: Option<Value>,
    /// State of each requested account after the simulated tx (None if absent).
    pub post_accounts: Vec<Option<AccountData>>,
    pub logs: Vec<String>,
    pub units_consumed: Option<u64>,
}

pub struct Rpc {
    http: reqwest::Client,
    url: String,
    /// (hash, lastValidBlockHeight, fetched at)
    blockhash: RwLock<Option<(Hash, u64, Instant)>>,
}

/// A blockhash plus the last block height at which a tx signed with it can land.
#[derive(Debug, Clone, Copy)]
pub struct Blockhash {
    pub hash: Hash,
    pub last_valid_height: u64,
}

/// Status of a signature as seen by `getSignatureStatuses`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SigStatus {
    pub slot: u64,
    pub err: Option<String>,
    /// confirmed or finalized (not just processed — a processed tx can sit on a dropped fork)
    pub confirmed: bool,
}

impl Rpc {
    pub fn new(url: String) -> Self {
        Rpc { http: http_client(), url, blockhash: RwLock::new(None) }
    }

    pub async fn call(&self, method: &str, params: Value) -> Result<Value> {
        retry_once(&format!("rpc {method}"), || json_rpc(&self.http, &self.url, method, &params)).await
    }

    /// Batch account fetch (base64, processed commitment). Order matches `keys`.
    pub async fn get_multiple_accounts(&self, keys: &[Pubkey]) -> Result<Vec<Option<AccountData>>> {
        self.get_multiple_accounts_at(keys, "processed").await
    }

    /// Batch account fetch at an explicit commitment. Use `confirmed` for anything that
    /// decides money (balances after a trade): `processed` can be served by a lagging node.
    pub async fn get_multiple_accounts_at(&self, keys: &[Pubkey], commitment: &str) -> Result<Vec<Option<AccountData>>> {
        let keys_str: Vec<String> = keys.iter().map(|k| k.to_string()).collect();
        let v = self
            .call("getMultipleAccounts", json!([keys_str, { "encoding": "base64", "commitment": commitment }]))
            .await?;
        let arr = v["value"].as_array().ok_or("getMultipleAccounts: missing value")?;
        if arr.len() != keys.len() {
            return Err(format!("getMultipleAccounts: expected {} entries, got {}", keys.len(), arr.len()).into());
        }
        arr.iter().map(parse_account).collect()
    }

    /// Raw token balance of a token account; 0 if the account doesn't exist (processed).
    pub async fn token_balance(&self, token_account: &Pubkey) -> Result<u64> {
        let accs = self.get_multiple_accounts(&[*token_account]).await?;
        Ok(accs.into_iter().next().flatten().and_then(|a| a.token_amount()).unwrap_or(0))
    }

    /// Token balance at `confirmed`: `None` if the account does not exist.
    pub async fn token_balance_confirmed(&self, token_account: &Pubkey) -> Result<Option<u64>> {
        let accs = self.get_multiple_accounts_at(&[*token_account], "confirmed").await?;
        Ok(accs.into_iter().next().flatten().map(|a| a.token_amount().unwrap_or(0)))
    }

    /// SOL balance (lamports) at `confirmed`.
    pub async fn balance(&self, key: &Pubkey) -> Result<u64> {
        let v = self.call("getBalance", json!([key.to_string(), { "commitment": "confirmed" }])).await?;
        v["value"].as_u64().ok_or_else(|| "getBalance: missing value".into())
    }

    /// Current block height (confirmed) — compared with `lastValidBlockHeight` to know
    /// for sure that a tx can no longer land.
    pub async fn block_height(&self) -> Result<u64> {
        let v = self.call("getBlockHeight", json!([{ "commitment": "confirmed" }])).await?;
        v.as_u64().ok_or_else(|| "getBlockHeight: not a number".into())
    }

    /// Fee payer's lamport change in a landed tx (post − pre): what a trade really cost
    /// or returned, incl. fees, tips and rent. `None` until the tx is indexed.
    pub async fn payer_delta(&self, sig: &str) -> Result<Option<i128>> {
        let v = json_rpc(&self.http, &self.url, "getTransaction",
            &json!([sig, { "encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 1 }])).await?;
        if v.is_null() {
            return Ok(None);
        }
        let pre = v["meta"]["preBalances"][0].as_u64().ok_or("getTransaction: no preBalances")?;
        let post = v["meta"]["postBalances"][0].as_u64().ok_or("getTransaction: no postBalances")?;
        Ok(Some(post as i128 - pre as i128))
    }

    /// Cached blockhash; refetches inline if the cache is older than BLOCKHASH_TTL.
    pub async fn latest_blockhash(&self) -> Result<Blockhash> {
        if let Some((hash, last_valid_height, at)) = *self.blockhash.read().unwrap() {
            if at.elapsed() < BLOCKHASH_TTL {
                return Ok(Blockhash { hash, last_valid_height });
            }
        }
        self.refresh_blockhash().await
    }

    pub async fn refresh_blockhash(&self) -> Result<Blockhash> {
        let v = self.call("getLatestBlockhash", json!([{ "commitment": "confirmed" }])).await?;
        let s = v["value"]["blockhash"].as_str().ok_or("getLatestBlockhash: missing blockhash")?;
        let hash = Hash::from_str(s).map_err(|e| format!("bad blockhash {s}: {e}"))?;
        let last_valid_height = v["value"]["lastValidBlockHeight"].as_u64().ok_or("getLatestBlockhash: missing lastValidBlockHeight")?;
        *self.blockhash.write().unwrap() = Some((hash, last_valid_height, Instant::now()));
        Ok(Blockhash { hash, last_valid_height })
    }

    pub fn spawn_blockhash_refresher(self: &Arc<Self>) {
        let rpc = Arc::clone(self);
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(BLOCKHASH_REFRESH);
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tick.tick().await;
                if let Err(e) = rpc.refresh_blockhash().await {
                    log_warn!("blockhash refresh failed: {e}");
                }
            }
        });
    }

    /// Wallets holding `mint` after recent successful txs touching `account`
    /// (owner, raw amount) — from `postTokenBalances`. Cheap holder discovery.
    pub async fn recent_holders(&self, account: &Pubkey, mint: &Pubkey, limit: usize) -> Result<Vec<(Pubkey, u64)>> {
        let sigs = self.call("getSignaturesForAddress", json!([account.to_string(), { "limit": limit }])).await?;
        let mut out: Vec<(Pubkey, u64)> = Vec::new();
        for s in sigs.as_array().into_iter().flatten().filter(|s| s["err"].is_null()) {
            let Some(sig) = s["signature"].as_str() else { continue };
            let tx = match self.call("getTransaction", json!([sig, { "encoding": "json", "maxSupportedTransactionVersion": 1 }])).await {
                Ok(t) => t,
                Err(_) => continue,
            };
            for b in tx["meta"]["postTokenBalances"].as_array().into_iter().flatten() {
                if b["mint"].as_str() != Some(&mint.to_string()) {
                    continue;
                }
                let (Some(owner), Some(amt)) = (b["owner"].as_str(), b["uiTokenAmount"]["amount"].as_str()) else { continue };
                if let (Ok(o), Ok(a)) = (Pubkey::from_str(owner), amt.parse::<u64>()) {
                    if a > 0 && !out.iter().any(|x| x.0 == o) {
                        out.push((o, a));
                    }
                }
            }
            if out.len() >= 5 {
                break;
            }
        }
        Ok(out)
    }

    /// Plain RPC submission (an extra path next to the relays). No retries here.
    pub async fn send_transaction(&self, tx_base64: &str) -> Result<String> {
        let params = json!([tx_base64, { "encoding": "base64", "skipPreflight": true, "maxRetries": 0 }]);
        let v = json_rpc(&self.http, &self.url, "sendTransaction", &params).await?;
        v.as_str().map(String::from).ok_or_else(|| format!("sendTransaction: unexpected result {v}").into())
    }

    /// `Some(status)` once the signature is seen at processed or better.
    pub async fn signature_status(&self, sig: &str) -> Result<Option<SigStatus>> {
        let v = json_rpc(&self.http, &self.url, "getSignatureStatuses", &json!([[sig]])).await?;
        Ok(parse_sig_status(&v["value"][0]))
    }

    /// 75th percentile of non-zero recent priority fees (µ-lamports/CU) paid by txs
    /// that write-locked any of `accounts` — the fee needed to compete for that curve.
    pub async fn priority_fee_p75(&self, accounts: &[Pubkey]) -> Result<u64> {
        let keys: Vec<String> = accounts.iter().map(|k| k.to_string()).collect();
        let v = json_rpc(&self.http, &self.url, "getRecentPrioritizationFees", &json!([keys])).await?;
        let mut fees: Vec<u64> = v
            .as_array()
            .map(|a| a.iter().filter_map(|x| x["prioritizationFee"].as_u64()).filter(|f| *f > 0).collect())
            .unwrap_or_default();
        if fees.is_empty() {
            return Ok(0);
        }
        fees.sort_unstable();
        Ok(fees[(fees.len() * 3 / 4).min(fees.len() - 1)])
    }

    /// Simulate without signature checks; returns post-state of `watch` token accounts.
    pub async fn simulate(&self, tx_base64: &str, watch: &[Pubkey]) -> Result<SimResult> {
        let addrs: Vec<String> = watch.iter().map(|k| k.to_string()).collect();
        let v = self
            .call(
                "simulateTransaction",
                json!([tx_base64, {
                    "encoding": "base64",
                    "sigVerify": false,
                    "replaceRecentBlockhash": true,
                    "commitment": "processed",
                    "accounts": { "encoding": "base64", "addresses": addrs }
                }]),
            )
            .await?;
        let val = &v["value"];
        let post_accounts = val["accounts"]
            .as_array()
            .map(|a| a.iter().map(|it| parse_account(it).ok().flatten()).collect())
            .unwrap_or_default();
        Ok(SimResult {
            post_accounts,
            err: val.get("err").filter(|e| !e.is_null()).cloned(),
            logs: val["logs"]
                .as_array()
                .map(|a| a.iter().filter_map(|l| l.as_str().map(String::from)).collect())
                .unwrap_or_default(),
            units_consumed: val["unitsConsumed"].as_u64(),
        })
    }
}

fn parse_sig_status(st: &Value) -> Option<SigStatus> {
    if st.is_null() {
        return None;
    }
    let err = st.get("err").filter(|e| !e.is_null()).map(|e| e.to_string());
    let confirmed = matches!(st["confirmationStatus"].as_str(), Some("confirmed" | "finalized"));
    Some(SigStatus { slot: st["slot"].as_u64().unwrap_or(0), err, confirmed })
}

fn parse_account(item: &Value) -> Result<Option<AccountData>> {
    if item.is_null() {
        return Ok(None);
    }
    let owner = item["owner"].as_str().ok_or("account: missing owner")?;
    let data_b64 = item["data"][0].as_str().ok_or("account: missing data")?;
    Ok(Some(AccountData {
        owner: Pubkey::from_str(owner).map_err(|e| format!("bad owner {owner}: {e}"))?,
        lamports: item["lamports"].as_u64().unwrap_or(0),
        data: base64::engine::general_purpose::STANDARD.decode(data_b64)?,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_account_json() {
        let mut raw = vec![0u8; 72];
        raw[64..72].copy_from_slice(&42u64.to_le_bytes());
        let b64 = base64::engine::general_purpose::STANDARD.encode(&raw);
        let item = json!({ "owner": crate::pda::TOKEN_PROGRAM.to_string(), "lamports": 5, "data": [b64, "base64"] });
        let acc = parse_account(&item).unwrap().unwrap();
        assert_eq!(acc.owner, crate::pda::TOKEN_PROGRAM);
        assert_eq!(acc.token_amount(), Some(42));
        assert!(parse_account(&Value::Null).unwrap().is_none());
    }

    #[test]
    fn parses_signature_status() {
        assert_eq!(parse_sig_status(&Value::Null), None);
        let st = parse_sig_status(&json!({"slot": 7, "err": null, "confirmationStatus": "processed"})).unwrap();
        assert_eq!((st.slot, st.err.is_none(), st.confirmed), (7, true, false));
        let st = parse_sig_status(&json!({"slot": 8, "err": {"InstructionError": [3, {"Custom": 6002}]}, "confirmationStatus": "confirmed"})).unwrap();
        assert!(st.confirmed && st.err.unwrap().contains("6002"));
    }
}
