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

/// Buyback wallets listed in pump-public-docs `docs/FEE_RECIPIENTS.md` (Nov 2026).
const DOCS_BUYBACK_WALLETS: [&str; 8] = [
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
];

/// `--selfcheck-amm <MINT>`: verify the PumpSwap v2 quotes and builders on the mint's
/// canonical wSOL pool by simulation as SIM_PAYER (nothing is signed or sent):
/// - BUY: buy-only tx — tokens received and payer lamport delta vs the quote;
/// - EXIT: one tx buy → sell all → close both ATAs — payer delta vs buy debit + sell quote.
///
/// The pool can trade between our fetch and the simulation; the vault balances after
/// the simulation reveal the state the program priced on, and a stale run is retried.
pub async fn run_amm(cfg: &Config, mint: &str) -> Result<()> {
    use crate::pumpswap::{self, AmmAccounts, AmmMarket};
    use crate::rpc::AccountData;

    let mint = Pubkey::from_str(mint.trim()).map_err(|e| format!("bad mint: {e}"))?;
    let rpc = Rpc::new(cfg.rpc_url.clone());
    let (gc, fc) = pumpswap::fetch_configs(&rpc).await?;
    let docs: Vec<Pubkey> = DOCS_BUYBACK_WALLETS.iter().map(|s| Pubkey::from_str(s).unwrap()).collect();
    println!(
        "amm config: buyback {} bps, {} buyback wallets ({}), disable_flags {:#04x}, creator_fee_configurable {}, sol tiers {}",
        gc.buyback_basis_points, gc.buyback_fee_recipients.len(),
        if gc.buyback_fee_recipients == docs { "= docs/FEE_RECIPIENTS.md" } else { "DIFFER from docs/FEE_RECIPIENTS.md" },
        gc.disable_flags, gc.creator_fee_configurable, fc.fee_tiers.len()
    );
    let curve = rpc.get_multiple_accounts(&[pda::bonding_curve(&mint)]).await?.remove(0);
    if let Some(c) = curve.as_ref().and_then(|c| BondingCurve::parse(&c.data).ok()) {
        println!("curve: complete={} (pump bonding curve)", c.complete);
    }
    let m0 = pumpswap::fetch_market(&rpc, &mint).await?;
    let show = |m: &AmmMarket| -> Result<()> {
        let [lp, p, c] = pumpswap::fee_bps(&gc, &fc, m)?;
        println!(
            "pool {}: index {} pump_pool={} sol={} mayhem={} cashback={} coin_creator={} base_program={}",
            m.pool_key, m.pool.index, pumpswap::is_pump_pool(&m.pool.base_mint, &m.pool.creator), m.pool.is_sol_quoted(),
            m.pool.is_mayhem_mode, m.pool.is_cashback_coin, m.pool.coin_creator, m.base_token_program
        );
        println!(
            "reserves: base {} | quote vault {} virtual {} buckets {}+{} → effective {} real {} | mcap {:.1} SOL | fees lp/protocol/creator {lp}/{p}/{c} bps",
            m.base_reserve, m.quote_vault, m.pool.virtual_quote_reserves, m.pool.protocol_fees, m.pool.creator_fees,
            m.effective_quote_reserve()?, m.pool.real_quote_reserve(m.quote_vault)?, m.market_cap()? as f64 / 1e9
        );
        Ok(())
    };
    show(&m0)?;
    m0.check_supported()?;

    let payer = std::env::var("SIM_PAYER").ok().and_then(|s| Pubkey::from_str(s.trim()).ok()).unwrap_or(crate::DEFAULT_SIM_PAYER);
    let bb = pumpswap::buyback_fee_recipient(&gc, 0);
    let budget = Budget { cu_limit: 400_000, cu_price_micro_lamports: 0 };
    let a = AmmAccounts::new(&m0, &payer);
    // payer, volume accumulator, base ATA, wSOL ATA, pool, base vault, quote vault
    let watch = [payer, a.user_volume_accumulator, a.user_base_ata, a.user_wsol_ata, m0.pool_key, m0.pool.pool_base_token_account, m0.pool.pool_quote_token_account];
    let lam = |v: &[Option<AccountData>], i: usize| v.get(i).and_then(|x| x.as_ref()).map_or(0i128, |x| x.lamports as i128);
    let amt = |v: &[Option<AccountData>], i: usize| v.get(i).and_then(|x| x.as_ref()).and_then(|x| x.token_amount()).unwrap_or(0);
    let mut ok = true;
    if let Some(w) = rpc.get_multiple_accounts(&[a.user_wsol_ata]).await?.remove(0) {
        println!("note: SIM_PAYER already has a wSOL ATA ({} lamports); the trade closes it, Δ below excludes that refund", w.lamports);
    }

    // ---- BUY only (base ATA left open: its rent is part of the cost)
    for attempt in 1..=3 {
        let pre = rpc.get_multiple_accounts(&watch[..5]).await?;
        if pre[0].is_none() {
            return Err("SIM_PAYER not found".into());
        }
        if pre[2].is_some() {
            println!("BUY  skipped: SIM_PAYER already holds this token (ATA exists) — set SIM_PAYER to another funded wallet");
            return Err("selfcheck FAILED".into());
        }
        let m = pumpswap::fetch_market(&rpc, &mint).await?;
        let q = pumpswap::buy_exact_quote_in(&gc, &fc, &m, cfg.buy_lamports)?;
        let blockhash = rpc.refresh_blockhash().await?.hash;
        let ixs = pumpswap::buy_instructions(&payer, &a, &bb, cfg.buy_lamports, 1, budget, &[]);
        let sim = rpc.simulate(&tx_builder::encode_base64(&tx_builder::unsigned(&payer, &ixs, blockhash)?)?, &watch).await?;
        if let Some(e) = &sim.err {
            ok = false;
            println!("BUY  FAILED: {e}");
            sim.logs.iter().rev().take(12).rev().for_each(|l| println!("     {l}"));
            break;
        }
        let post = &sim.post_accounts;
        let got = amt(post, 2);
        let base_ata_rent = lam(post, 2);
        let (d_acc, d_pool) = (lam(post, 1) - lam(&pre, 1), lam(post, 4) - lam(&pre, 4));
        // payer: −debit − fee − base ATA rent − accumulator/pool rent; a pre-existing wSOL ATA is closed back to it.
        let expected = lam(&pre, 0) - q.debit as i128 - SIG_FEE as i128 - base_ata_rent - d_acc - d_pool + lam(&pre, 3);
        let got_payer = lam(post, 0);
        let seen = (amt(post, 5) + got, amt(post, 6).wrapping_sub(q.vault_in()));
        let m_ok = got == q.tokens_out && got_payer == expected;
        if !m_ok && seen != (m.base_reserve, m.quote_vault) && attempt < 3 {
            println!("BUY  retry {attempt}: pool traded between fetch and simulation");
            continue;
        }
        ok &= m_ok;
        println!(
            "BUY  {}: {} lamports in → quote {} tokens, simulated {} | payer Δ expected {} got {} (debit {}, fees lp/prot/creator {}/{}/{}, buyback {}, base ATA rent {base_ata_rent}, accumulator rent {d_acc}, pool top-up {d_pool}) ({} CU)",
            if m_ok { "OK " } else { "MISMATCH" }, cfg.buy_lamports, q.tokens_out, got,
            expected - lam(&pre, 0) - lam(&pre, 3), got_payer - lam(&pre, 0) - lam(&pre, 3), q.debit, q.lp_fee, q.protocol_fee, q.creator_fee, q.buyback_fee,
            sim.units_consumed.unwrap_or(0)
        );
        break;
    }

    // ---- EXIT: buy → sell everything → close wSOL + base ATA, one tx
    for attempt in 1..=3 {
        let pre = rpc.get_multiple_accounts(&watch[..5]).await?;
        let m = pumpswap::fetch_market(&rpc, &mint).await?;
        let q = pumpswap::buy_exact_quote_in(&gc, &fc, &m, cfg.buy_lamports)?;
        let after = m.after_buy(&q);
        let s = pumpswap::sell(&gc, &fc, &after, q.tokens_out)?;
        let mut trade = pumpswap::buy_trade_ixs(&a, &payer, &bb, cfg.buy_lamports, 1, false);
        trade.extend(pumpswap::sell_trade_ixs(&a, &payer, &bb, q.tokens_out, 1, true));
        let ixs = tx_builder::with_budget_and_tips(&payer, budget, trade, &[]);
        let blockhash = rpc.refresh_blockhash().await?.hash;
        let sim = rpc.simulate(&tx_builder::encode_base64(&tx_builder::unsigned(&payer, &ixs, blockhash)?)?, &watch).await?;
        if let Some(e) = &sim.err {
            // A trade landing between our fetch and the simulation shrinks the buy, and
            // selling the quoted amount then fails with insufficient funds.
            if attempt < 3 && sim.logs.iter().any(|l| l.contains("insufficient funds")) {
                println!("EXIT retry {attempt}: {e} — pool traded between fetch and simulation");
                continue;
            }
            ok = false;
            println!("EXIT FAILED (buy → sell all → close): {e}");
            sim.logs.iter().rev().take(12).rev().for_each(|l| println!("     {l}"));
            break;
        }
        let post = &sim.post_accounts;
        let closed = lam(post, 2) == 0 && lam(post, 3) == 0;
        let (d_acc, d_pool) = (lam(post, 1) - lam(&pre, 1), lam(post, 4) - lam(&pre, 4));
        let expected = lam(&pre, 0) - q.debit as i128 + s.sol_out as i128 - SIG_FEE as i128 - d_acc - d_pool + lam(&pre, 3);
        let got_payer = lam(post, 0);
        let vault_ok = amt(post, 5) == m.base_reserve
            && amt(post, 6) == (m.quote_vault + q.vault_in()).wrapping_sub(s.sol_out + s.buyback_fee);
        let m_ok = got_payer == expected && closed;
        if !m_ok && !vault_ok && attempt < 3 {
            println!("EXIT retry {attempt}: pool traded between fetch and simulation");
            continue;
        }
        ok &= m_ok;
        let cost = q.debit as i128 - s.sol_out as i128;
        println!(
            "EXIT {}: buy {} → {} tokens → sell all: quote {} lamports back, payer Δ expected {} got {} ({} CU, ATAs closed: {closed}, vaults as modelled: {vault_ok})",
            if m_ok { "OK " } else { "MISMATCH" }, cfg.buy_lamports, q.tokens_out, s.sol_out,
            expected - lam(&pre, 0) - lam(&pre, 3), got_payer - lam(&pre, 0) - lam(&pre, 3), sim.units_consumed.unwrap_or(0)
        );
        println!(
            "     round-trip cost {cost} lamports = {:.3}% of {} (excl. {SIG_FEE} base fee{})",
            cost as f64 * 100.0 / cfg.buy_lamports as f64, cfg.buy_lamports,
            if d_acc + d_pool != 0 { format!(", one-time rent {}", d_acc + d_pool) } else { String::new() }
        );
        break;
    }
    if ok {
        println!("selfcheck-amm PASSED");
        Ok(())
    } else {
        Err("selfcheck-amm FAILED".into())
    }
}
