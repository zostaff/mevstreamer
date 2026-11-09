//! VersionedTransaction (v0) construction for pump.fun v3 buy / sell.
//!
//! v3 trades (`buy_exact_quote_in_v3`, `sell_v3`) take 17 accounts instead of the
//! 26–27 of v2: protocol and creator fees stay on the curve, only the buyback
//! slice is paid in the trade. Layout: pump-public-docs `docs/instructions/TRADE_V3.md`.
//!
//! One signed transaction can carry several tips (Jito + Helius Sender, …) so the
//! *same signature* can be fanned out to every relay and still land at most once.

use crate::pda::{self, MintAccounts};
use crate::types::Result;
use base64::Engine;
use solana_sdk::hash::Hash;
use solana_sdk::instruction::{AccountMeta, Instruction};
use solana_sdk::message::{v0, VersionedMessage};
use solana_sdk::pubkey::Pubkey;
use solana_sdk::signature::{Keypair, Signature};
use solana_sdk::signer::Signer;
use solana_sdk::transaction::VersionedTransaction;

/// sha256("global:buy_exact_quote_in_v3")[..8] — from idl/pump.json.
pub const BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR: [u8; 8] = [0xe1, 0xf7, 0x50, 0x1e, 0xd5, 0xb3, 0x84, 0x88];
/// sha256("global:sell_v3")[..8] — from idl/pump.json.
pub const SELL_V3_DISCRIMINATOR: [u8; 8] = [0x1c, 0x92, 0xde, 0x77, 0x26, 0xc4, 0x69, 0xd5];

/// Compute budget for one trade tx. A tight limit raises effective priority (fee/CU);
/// the defaults leave headroom over simulated usage (see `config.rs`, CU_LIMIT_*).
#[derive(Debug, Clone, Copy)]
pub struct Budget {
    pub cu_limit: u32,
    pub cu_price_micro_lamports: u64,
}

pub fn set_compute_unit_limit(units: u32) -> Instruction {
    let mut data = Vec::with_capacity(5);
    data.push(0x02);
    data.extend_from_slice(&units.to_le_bytes());
    Instruction { program_id: pda::COMPUTE_BUDGET_PROGRAM, accounts: vec![], data }
}

pub fn set_compute_unit_price(micro_lamports: u64) -> Instruction {
    let mut data = Vec::with_capacity(9);
    data.push(0x03);
    data.extend_from_slice(&micro_lamports.to_le_bytes());
    Instruction { program_id: pda::COMPUTE_BUDGET_PROGRAM, accounts: vec![], data }
}

/// System program Transfer (instruction index 2).
pub fn transfer(from: &Pubkey, to: &Pubkey, lamports: u64) -> Instruction {
    let mut data = Vec::with_capacity(12);
    data.extend_from_slice(&2u32.to_le_bytes());
    data.extend_from_slice(&lamports.to_le_bytes());
    Instruction {
        program_id: pda::SYSTEM_PROGRAM,
        accounts: vec![AccountMeta::new(*from, true), AccountMeta::new(*to, false)],
        data,
    }
}

/// The 17 accounts shared by `buy_v3`, `buy_exact_quote_in_v3` and `sell_v3` (SOL curve).
fn v3_accounts(a: &MintAccounts, user: &Pubkey, buyback_fee_recipient: &Pubkey) -> Vec<AccountMeta> {
    vec![
        AccountMeta::new_readonly(pda::PUMP_GLOBAL, false),
        AccountMeta::new_readonly(a.mint, false),
        AccountMeta::new_readonly(pda::WSOL_MINT, false),
        AccountMeta::new_readonly(a.token_program, false),
        AccountMeta::new_readonly(pda::TOKEN_PROGRAM, false),
        AccountMeta::new(a.bonding_curve, false),
        AccountMeta::new(a.bonding_curve_ata, false),
        AccountMeta::new(a.bonding_curve_quote_ata, false),
        AccountMeta::new(*user, true),
        AccountMeta::new(a.user_ata, false),
        AccountMeta::new(a.user_quote_ata, false),
        AccountMeta::new(a.user_volume_accumulator, false),
        AccountMeta::new_readonly(pda::PUMP_FEE_CONFIG, false),
        // SOL curve: the buyback wallet itself (token curves would pass its quote ATA).
        AccountMeta::new(*buyback_fee_recipient, false),
        AccountMeta::new_readonly(pda::SYSTEM_PROGRAM, false),
        AccountMeta::new_readonly(pda::PUMP_EVENT_AUTH, false),
        AccountMeta::new_readonly(pda::PUMP_PROGRAM, false),
    ]
}

fn pump_data(disc: &[u8; 8], a: u64, b: u64) -> Vec<u8> {
    let mut data = Vec::with_capacity(24);
    data.extend_from_slice(disc);
    data.extend_from_slice(&a.to_le_bytes());
    data.extend_from_slice(&b.to_le_bytes());
    // `partial_fill: OptionBool` is optional and only matters for mayhem coins — omitted.
    data
}

pub fn buy_ix(a: &MintAccounts, user: &Pubkey, buyback: &Pubkey, spendable_sol_in: u64, min_tokens_out: u64) -> Instruction {
    Instruction {
        program_id: pda::PUMP_PROGRAM,
        accounts: v3_accounts(a, user, buyback),
        data: pump_data(&BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR, spendable_sol_in, min_tokens_out),
    }
}

pub fn sell_ix(a: &MintAccounts, user: &Pubkey, buyback: &Pubkey, token_amount: u64, min_sol_out: u64) -> Instruction {
    Instruction {
        program_id: pda::PUMP_PROGRAM,
        accounts: v3_accounts(a, user, buyback),
        data: pump_data(&SELL_V3_DISCRIMINATOR, token_amount, min_sol_out),
    }
}

pub fn sign(payer: &Keypair, ixs: &[Instruction], blockhash: Hash) -> Result<VersionedTransaction> {
    let msg = v0::Message::try_compile(&payer.pubkey(), ixs, &[], blockhash)?;
    Ok(VersionedTransaction::try_new(VersionedMessage::V0(msg), &[payer])?)
}

/// Unsigned tx for `simulateTransaction` with `sigVerify: false` — lets a dry run
/// simulate as any funded fee payer.
pub fn unsigned(payer: &Pubkey, ixs: &[Instruction], blockhash: Hash) -> Result<VersionedTransaction> {
    let msg = v0::Message::try_compile(payer, ixs, &[], blockhash)?;
    let n = msg.header.num_required_signatures as usize;
    Ok(VersionedTransaction { signatures: vec![Signature::default(); n], message: VersionedMessage::V0(msg) })
}

/// Compute budget first, then `trade`, then one transfer per non-zero tip.
pub fn with_budget_and_tips(payer: &Pubkey, budget: Budget, trade: Vec<Instruction>, tips: &[(Pubkey, u64)]) -> Vec<Instruction> {
    let mut ixs = Vec::with_capacity(2 + trade.len() + tips.len());
    ixs.push(set_compute_unit_limit(budget.cu_limit));
    if budget.cu_price_micro_lamports > 0 {
        ixs.push(set_compute_unit_price(budget.cu_price_micro_lamports));
    }
    ixs.extend(trade);
    ixs.extend(tips.iter().filter(|(_, l)| *l > 0).map(|(to, l)| transfer(payer, to, *l)));
    ixs
}

/// Compute budget + idempotent base-ATA create (v3 never creates it) + buy + tips.
pub fn buy_instructions(
    user: &Pubkey,
    accounts: &MintAccounts,
    buyback: &Pubkey,
    spendable_sol_in: u64,
    min_tokens_out: u64,
    budget: Budget,
    tips: &[(Pubkey, u64)],
) -> Vec<Instruction> {
    let trade = vec![
        pda::create_ata_idempotent_ix(user, user, &accounts.mint, &accounts.token_program),
        buy_ix(accounts, user, buyback, spendable_sol_in, min_tokens_out),
    ];
    with_budget_and_tips(user, budget, trade, tips)
}

/// SPL Token / Token-2022 `CloseAccount` (index 9): rent goes back to `dest`.
/// Only valid on an empty account — use it after selling the whole balance.
pub fn close_account(token_program: &Pubkey, account: &Pubkey, dest: &Pubkey, owner: &Pubkey) -> Instruction {
    Instruction {
        program_id: *token_program,
        accounts: vec![AccountMeta::new(*account, false), AccountMeta::new(*dest, false), AccountMeta::new_readonly(*owner, true)],
        data: vec![9],
    }
}

/// Compute budget + sell + (optionally) close the now-empty token ATA + tips.
/// Closing reclaims ~0.002 SOL of rent per trade — pass `close_ata` only when
/// `token_amount` is the account's full balance, otherwise the close fails the tx.
#[allow(clippy::too_many_arguments)]
pub fn sell_instructions(
    user: &Pubkey,
    accounts: &MintAccounts,
    buyback: &Pubkey,
    token_amount: u64,
    min_sol_out: u64,
    budget: Budget,
    tips: &[(Pubkey, u64)],
    close_ata: bool,
) -> Vec<Instruction> {
    let mut trade = vec![sell_ix(accounts, user, buyback, token_amount, min_sol_out)];
    if close_ata {
        trade.push(close_account(&accounts.token_program, &accounts.user_ata, user, user));
    }
    with_budget_and_tips(user, budget, trade, tips)
}

/// SPL Token `SyncNative` (index 17): sets a wrapped-SOL account's token amount to
/// its lamports above rent — needed after transferring SOL into it.
pub fn sync_native(account: &Pubkey) -> Instruction {
    Instruction { program_id: pda::TOKEN_PROGRAM, accounts: vec![AccountMeta::new(*account, false)], data: vec![17] }
}

/// Priority fee in lamports for a budget: ceil(price × limit / 10⁶).
pub fn priority_fee_lamports(b: Budget) -> u64 {
    ((b.cu_price_micro_lamports as u128 * b.cu_limit as u128).div_ceil(1_000_000)) as u64
}

pub fn encode_base64(tx: &VersionedTransaction) -> Result<String> {
    Ok(base64::engine::general_purpose::STANDARD.encode(bincode::serialize(tx)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    const B: Budget = Budget { cu_limit: 90_000, cu_price_micro_lamports: 100_000 };

    #[test]
    fn compute_budget_data() {
        assert_eq!(set_compute_unit_limit(200_000).data, vec![0x02, 0x40, 0x0d, 0x03, 0x00]);
        assert_eq!(set_compute_unit_price(100_000).data, vec![0x03, 0xa0, 0x86, 0x01, 0, 0, 0, 0, 0]);
    }

    #[test]
    fn discriminators_are_anchor_sighashes() {
        use solana_sdk::hash::hash;
        assert_eq!(hash(b"global:buy_exact_quote_in_v3").to_bytes()[..8], BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR);
        assert_eq!(hash(b"global:sell_v3").to_bytes()[..8], SELL_V3_DISCRIMINATOR);
    }

    #[test]
    fn buy_ix_layout_matches_idl() {
        let user = Pubkey::new_unique();
        let bb = Pubkey::new_unique();
        let a = MintAccounts::new(Pubkey::new_unique(), &user, pda::TOKEN_2022_PROGRAM);
        let ix = buy_ix(&a, &user, &bb, 7, 9);
        assert_eq!(ix.accounts.len(), 17);
        assert_eq!(ix.data.len(), 24);
        assert_eq!(&ix.data[..8], &BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR);
        assert_eq!(u64::from_le_bytes(ix.data[8..16].try_into().unwrap()), 7);
        assert_eq!(u64::from_le_bytes(ix.data[16..24].try_into().unwrap()), 9);
        // writable/signer flags from idl/pump.json
        let w: Vec<bool> = ix.accounts.iter().map(|m| m.is_writable).collect();
        assert_eq!(w, [false, false, false, false, false, true, true, true, true, true, true, true, false, true, false, false, false]);
        assert!(ix.accounts[8].is_signer && ix.accounts.iter().filter(|m| m.is_signer).count() == 1);
        assert_eq!(ix.accounts[13].pubkey, bb);
        assert_eq!(ix.accounts[16].pubkey, pda::PUMP_PROGRAM);
    }

    #[test]
    fn full_txs_sign_and_fit() {
        let kp = Keypair::new();
        let user = kp.pubkey();
        let a = MintAccounts::new(Pubkey::new_unique(), &user, pda::TOKEN_PROGRAM);
        let tips = [(crate::jito::TIP_ACCOUNTS[0], 10_000), (Pubkey::new_unique(), 5_000)];
        let ixs = buy_instructions(&user, &a, &Pubkey::new_unique(), 100_000_000, 1, B, &tips);
        assert_eq!(ixs.len(), 6); // limit + price + ata + buy + 2 tips
        let tx = sign(&kp, &ixs, Hash::new_unique()).unwrap();
        assert!(tx.verify_with_results().iter().all(|ok| *ok));
        let bytes = bincode::serialize(&tx).unwrap();
        assert!(bytes.len() <= 1232, "tx too large: {}", bytes.len());
        let u = unsigned(&user, &ixs, Hash::new_unique()).unwrap();
        assert_eq!(u.signatures.len(), 1);
        assert_eq!(sell_instructions(&user, &a, &Pubkey::new_unique(), 5, 1, B, &tips[..1], false).len(), 4);
        let close = sell_instructions(&user, &a, &Pubkey::new_unique(), 5, 1, B, &tips, true);
        assert_eq!(close.len(), 6);
        assert_eq!((close[3].program_id, close[3].data.clone()), (pda::TOKEN_PROGRAM, vec![9]));
        assert!(bincode::serialize(&sign(&kp, &close, Hash::new_unique()).unwrap()).unwrap().len() <= 1232);
        assert_eq!(priority_fee_lamports(B), 9_000);
    }
}
