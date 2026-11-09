//! `mev-streamer-executor --selfcheck <MINT>`: verify the v3 buy and sell builders and
//! the fee-aware quotes against mainnet by simulation (nothing is signed or sent).
//!
//! - buy: simulated as a funded wallet (SIM_PAYER), quote vs tokens received;
//! - sell: simulated as a real holder of the token, quote vs lamports received.
//!
//! Run it after any pump.fun program upgrade before trading live.

use crate::bonding::{self, BondingCurve};
use crate::config::Config;
use crate::pda::{self, MintAccounts};
use crate::rpc::Rpc;
use crate::tx_builder::{self, Budget};
use crate::types::Result;
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;

/// Simulation-only: payer fee = 5000/sig + priority (0 here).
const SIG_FEE: u64 = 5_000;

pub async fn run(cfg: &Config, mint: &str) -> Result<()> {
    let mint = Pubkey::from_str(mint.trim()).map_err(|e| format!("bad mint: {e}"))?;
    let rpc = Rpc::new(cfg.rpc_url.clone());
    let (global, fees) = crate::fetch_state(&rpc).await?;
    let blockhash = rpc.refresh_blockhash().await?.hash;
    let curve_key = pda::bonding_curve(&mint);
    let accs = rpc.get_multiple_accounts(&[curve_key, mint]).await?;
    let curve = BondingCurve::parse(&accs[0].as_ref().ok_or("bonding curve not found")?.data)?;
    let mint_acc = accs[1].as_ref().ok_or("mint not found")?;
    let (token_program, supply) = (mint_acc.owner, crate::mint_supply(&mint_acc.data)?);
    println!("curve: complete={} sol_quoted={} cashback={} mayhem={} creator_fee_bps={} token_program={}",
        curve.complete, curve.is_sol_quoted(), curve.is_cashback_coin, curve.is_mayhem_mode, curve.creator_fee_bps, token_program);
    println!("fees:  {:?} (protocol, creator bps)", bonding::curve_fee_bps(&global, &fees, &curve, supply)?);
    let buyback = global.buyback_fee_recipients[0];
    let budget = Budget { cu_limit: 400_000, cu_price_micro_lamports: 0 };
    let mut ok = true;

    // ---- buy
    let payer = std::env::var("SIM_PAYER").ok().and_then(|s| Pubkey::from_str(s.trim()).ok()).unwrap_or(crate::DEFAULT_SIM_PAYER);
    let q = bonding::buy_exact_sol_in(&global, &fees, &curve, supply, cfg.buy_lamports)?;
    let a = MintAccounts::new(mint, &payer, token_program);
    let pre = rpc.token_balance(&a.user_ata).await.unwrap_or(0);
    let ixs = tx_builder::buy_instructions(&payer, &a, &buyback, cfg.buy_lamports, 1, budget, &[]);
    let sim = rpc.simulate(&tx_builder::encode_base64(&tx_builder::unsigned(&payer, &ixs, blockhash)?)?, &[a.user_ata]).await?;
    match &sim.err {
        None => {
            let got = sim.post_accounts[0].as_ref().and_then(|x| x.token_amount()).unwrap_or(0).saturating_sub(pre);
            let m = got == q.tokens_out;
            ok &= m;
            println!("BUY  {}: quote {} tokens, simulated {} ({} CU) {}", if m { "OK " } else { "MISMATCH" },
                q.tokens_out, got, sim.units_consumed.unwrap_or(0), if m { "" } else { "<- quote math is off" });
        }
        Some(e) => {
            ok = false;
            println!("BUY  FAILED: {e}");
            sim.logs.iter().rev().take(10).rev().for_each(|l| println!("     {l}"));
        }
    }

    // ---- round trip: buy → sell everything bought → close the ATA, in ONE simulated
    // tx as SIM_PAYER. Tests our sell quote on the exact post-buy curve (what live
    // trading does) without depending on finding a holder through a rate-limited RPC.
    let curve = fresh_curve(&rpc, &curve_key).await?;
    let q = bonding::buy_exact_sol_in(&global, &fees, &curve, supply, cfg.buy_lamports)?;
    let after = curve.after_buy(q.net_in, q.tokens_out);
    let sell_quote = bonding::sell_sol_out(&global, &fees, &after, supply, q.tokens_out)?;
    let a = MintAccounts::new(mint, &payer, token_program);
    let pre = rpc.get_multiple_accounts(&[payer, a.user_volume_accumulator, a.user_ata]).await?;
    let pre_payer = pre[0].as_ref().map(|x| x.lamports).ok_or("SIM_PAYER not found")?;
    let pre_acc = pre[1].as_ref().map(|x| x.lamports).unwrap_or(0);
    if pre[2].is_some() {
        println!("EXIT skipped: SIM_PAYER already holds this token (ATA exists) — set SIM_PAYER to another funded wallet");
    } else {
        let mut ixs = tx_builder::buy_instructions(&payer, &a, &buyback, cfg.buy_lamports, 1, budget, &[]);
        ixs.push(tx_builder::sell_ix(&a, &payer, &buyback, q.tokens_out, 1));
        ixs.push(tx_builder::close_account(&token_program, &a.user_ata, &payer, &payer));
        let sim = rpc.simulate(&tx_builder::encode_base64(&tx_builder::unsigned(&payer, &ixs, blockhash)?)?, &[payer, a.user_volume_accumulator, a.user_ata]).await?;
        match &sim.err {
            None => {
                let post_payer = sim.post_accounts[0].as_ref().map(|x| x.lamports).unwrap_or(0);
                let post_acc = sim.post_accounts[1].as_ref().map(|x| x.lamports).unwrap_or(0);
                let closed = sim.post_accounts[2].as_ref().map_or(true, |x| x.lamports == 0);
                // payer: −debit + sell proceeds − base fee − volume-accumulator rent (if created).
                let expected = pre_payer as i128 - q.debit as i128 + sell_quote as i128 - SIG_FEE as i128 - (post_acc as i128 - pre_acc as i128);
                let got = post_payer as i128;
                let m = got == expected && closed;
                ok &= m;
                println!(
                    "EXIT {}: buy {} → {} tokens → sell all: quote {} lamports back, payer Δ expected {} got {} ({} CU, ATA closed: {closed})",
                    if m { "OK " } else { "MISMATCH" }, cfg.buy_lamports, q.tokens_out, sell_quote,
                    expected - pre_payer as i128, got - pre_payer as i128, sim.units_consumed.unwrap_or(0)
                );
            }
            Some(e) => {
                ok = false;
                println!("EXIT FAILED (buy → sell all → close): {e}");
                sim.logs.iter().rev().take(10).rev().for_each(|l| println!("     {l}"));
            }
        }
    }
    if ok {
        println!("selfcheck PASSED");
        Ok(())
    } else {
        Err("selfcheck FAILED".into())
    }
}

/// `--inspect <MINT>`: walk the quote chain (B → its quote A → … → SOL/other) and
/// print where each level trades — the information a multi-hop route needs.
pub async fn inspect(cfg: &Config, mint: &str) -> Result<()> {
    let rpc = Rpc::new(cfg.rpc_url.clone());
    let mut cur = Pubkey::from_str(mint.trim()).map_err(|e| format!("bad mint: {e}"))?;
    for depth in 0..4 {
        let curve_key = pda::bonding_curve(&cur);
        let accs = rpc.get_multiple_accounts(&[curve_key, cur]).await?;
        let mint_owner = accs[1].as_ref().map(|a| a.owner.to_string()).unwrap_or_else(|| "missing".into());
        let Some(acc) = accs[0].as_ref().filter(|a| a.owner == pda::PUMP_PROGRAM) else {
            println!("{depth}: {cur} — no pump bonding curve (mint program {mint_owner})");
            break;
        };
        let c = BondingCurve::parse(&acc.data)?;
        println!(
            "{depth}: {cur} curve complete={} quote={} mayhem={} cashback={} real_quote={} mint_program={mint_owner}",
            c.complete, if c.is_sol_quoted() { "SOL".to_string() } else { c.quote_mint.to_string() },
            c.is_mayhem_mode, c.is_cashback_coin, c.real_quote_reserves
        );
        if c.is_sol_quoted() {
            break;
        }
        cur = c.quote_mint;
    }
    Ok(())
}

async fn fresh_curve(rpc: &Rpc, key: &Pubkey) -> Result<BondingCurve> {
    let acc = rpc.get_multiple_accounts(&[*key]).await?.into_iter().next().flatten().ok_or("bonding curve not found")?;
    BondingCurve::parse(&acc.data)
}

/// `--probe-relays`: send one tx that can never land (fresh unfunded key: compute budget
/// + the tip transfers) to every relay and print their answers. Checks endpoints, auth
/// and request format without spending anything.
pub async fn probe_relays(cfg: &Config) -> Result<()> {
    use crate::jito::JitoClient;
    use crate::sender::{self, Relay, Submitter};
    use solana_sdk::signature::Keypair;
    use solana_sdk::signer::Signer;
    use std::sync::Arc;

    let rpc = Arc::new(Rpc::new(cfg.rpc_url.clone()));
    let blockhash = rpc.refresh_blockhash().await?.hash;
    let jito = Arc::new(JitoClient::new(cfg.jito_urls.clone()));
    let url = sender::sender_url_for_tip(&cfg.sender_url, cfg.sender_tip_lamports);
    let sub = Arc::new(Submitter::new(cfg.relays.clone(), url.clone(), cfg.sender_tip_lamports, Arc::clone(&jito), Arc::clone(&rpc)));
    let kp = Keypair::new();
    let tips = sub.tips(crate::jito::MIN_TIP_LAMPORTS);
    let ixs: Vec<_> = [tx_builder::set_compute_unit_limit(10_000), tx_builder::set_compute_unit_price(100_000)]
        .into_iter()
        .chain(tips.iter().map(|(to, l)| tx_builder::transfer(&kp.pubkey(), to, *l)))
        .collect();
    let b64 = tx_builder::encode_base64(&tx_builder::sign(&kp, &ixs, blockhash)?)?;
    println!("probe tx from unfunded {} ({} tips) — it cannot land", kp.pubkey(), tips.len());
    if sub.uses(Relay::Sender) {
        println!("sender  {url}");
    }
    for u in &cfg.jito_urls {
        println!("jito    {u}/api/v1/transactions?bundleOnly=true");
    }
    match sub.submit(b64).await {
        Ok(sig) => println!("accepted by at least one relay: {sig}"),
        Err(e) => println!("no relay accepted: {e}"),
    }
    tokio::time::sleep(std::time::Duration::from_millis(1500)).await; // let straggler logs print
    Ok(())
}
