//! PumpSwap (pump.fun AMM, `pAMMBay6…`) v2 trading for SOL-quoted pools: state
//! parsers, exact fee-aware quotes and `buy_exact_quote_in_v2` / `sell_v2` builders.
//!
//! Sources: pump-public-docs (`docs/instructions/PUMP_SWAP_TRADE_V2.md`,
//! `docs/PUMP_SWAP_README.md`, `idl/pump_amm.json`, Nov 2026). Quote math and fee
//! selection are ported from pump-rust-client 0.5.0 (MIT, © pump.fun):
//! `math/amm.rs` (`buy_quote_input_v2`, `sell_base_input`, effective / real quote
//! reserves), `math/fees.rs` (`compute_amm_fee_bps`, `exact_in_fee_amounts`) and
//! `sdk/mod.rs` (`fee_tier_supply`), restricted to an untaxed wSOL quote.
//!
//! Supported: canonical (migrated) and permissionless pools quoted in wSOL, Token or
//! Token-2022 base mints without a transfer fee, mayhem pools. Not supported:
//! cashback-coin pools (v2 fails with `CashbackCoinNotSupported` 6079; needs the v1
//! instructions + remaining accounts) and pools quoted in anything but wSOL.

use crate::pda;
use crate::rpc::Rpc;
use crate::tx_builder::{self, Budget};
use crate::types::Result;
use solana_sdk::instruction::{AccountMeta, Instruction};
use solana_sdk::pubkey;
use solana_sdk::pubkey::Pubkey;

pub const PUMP_AMM_PROGRAM: Pubkey = pubkey!("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA");
/// ["global_config"] under pAMM.
pub const AMM_GLOBAL_CONFIG: Pubkey = pubkey!("ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw");
/// pump-fees `FeeConfig` for the AMM: ["fee_config", pAMM] under the fees program.
pub const AMM_FEE_CONFIG: Pubkey = pubkey!("5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx");
/// ["__event_authority"] under pAMM.
pub const AMM_EVENT_AUTH: Pubkey = pubkey!("GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR");
pub const USDC_MINT: Pubkey = pubkey!("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v");
/// Fixed supply the program uses for a mayhem pool's fee tier (`fee_tier_supply`).
pub const MAYHEM_TIER_SUPPLY: u64 = 1_000_000_000_000_000;

pub const POOL_DISCRIMINATOR: [u8; 8] = [241, 154, 109, 4, 17, 177, 109, 188];
pub const GLOBAL_CONFIG_DISCRIMINATOR: [u8; 8] = [149, 8, 156, 202, 160, 252, 176, 217];
pub const FEE_CONFIG_DISCRIMINATOR: [u8; 8] = [143, 52, 146, 187, 219, 123, 76, 155];
/// sha256("global:buy_exact_quote_in_v2")[..8] — idl/pump_amm.json.
pub const BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR: [u8; 8] = [194, 171, 28, 70, 104, 77, 91, 47];
/// sha256("global:sell_v2")[..8] — idl/pump_amm.json.
pub const SELL_V2_DISCRIMINATOR: [u8; 8] = [93, 246, 130, 60, 231, 233, 64, 178];

const BPS: u128 = 10_000;

// ---------------------------------------------------------------- PDAs

/// pump program ["pool-authority", mint]: the `creator` of the pool `migrate` makes.
pub fn pool_authority(mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"pool-authority", mint.as_ref()], &pda::PUMP_PROGRAM).0
}

/// ["pool", index u16 LE, creator, base_mint, quote_mint] under pAMM.
pub fn pool_address(index: u16, creator: &Pubkey, base_mint: &Pubkey, quote_mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(
        &[b"pool", &index.to_le_bytes(), creator.as_ref(), base_mint.as_ref(), quote_mint.as_ref()],
        &PUMP_AMM_PROGRAM,
    )
    .0
}

/// The pool pump creates on migration: index 0, creator = pool-authority PDA of the mint.
pub fn canonical_pool(base_mint: &Pubkey, quote_mint: &Pubkey) -> Pubkey {
    pool_address(0, &pool_authority(base_mint), base_mint, quote_mint)
}

/// ["user_volume_accumulator", user] under pAMM (not the pump program one).
pub fn user_volume_accumulator(user: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"user_volume_accumulator", user.as_ref()], &PUMP_AMM_PROGRAM).0
}

/// Tiered fees apply only to pools whose creator is the mint's pump pool-authority.
pub fn is_pump_pool(base_mint: &Pubkey, pool_creator: &Pubkey) -> bool {
    pool_authority(base_mint) == *pool_creator
}

// ---------------------------------------------------------------- byte helpers

fn rd<const N: usize>(d: &[u8], off: usize) -> [u8; N] {
    // Zero-padded: older, shorter layouts read missing trailing fields as 0.
    let mut out = [0u8; N];
    if off < d.len() {
        let end = (off + N).min(d.len());
        out[..end - off].copy_from_slice(&d[off..end]);
    }
    out
}
fn u64_at(d: &[u8], o: usize) -> u64 {
    u64::from_le_bytes(rd(d, o))
}
fn pk_at(d: &[u8], o: usize) -> Pubkey {
    Pubkey::new_from_array(rd(d, o))
}
fn check_disc(d: &[u8], disc: &[u8; 8], what: &str) -> Result<()> {
    if d.len() < 8 || &d[..8] != disc {
        return Err(format!("{what}: bad account discriminator").into());
    }
    Ok(())
}

// ---------------------------------------------------------------- Pool

/// Current Pool size: 8 + 279 (IDL `Pool`). Older pools are shorter.
#[cfg(test)]
pub const POOL_LEN: usize = 287;
const POOL_MIN_LEN: usize = 211; // through `lp_supply`

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Pool {
    pub index: u16,
    pub creator: Pubkey,
    pub base_mint: Pubkey,
    pub quote_mint: Pubkey,
    pub pool_base_token_account: Pubkey,
    pub pool_quote_token_account: Pubkey,
    pub coin_creator: Pubkey,
    pub is_mayhem_mode: bool,
    pub is_cashback_coin: bool,
    /// Boost minus the un-swept v2 fee buckets — may be negative.
    pub virtual_quote_reserves: i128,
    pub creator_fee_bps: u64,
    /// Un-swept protocol / creator fees sitting in the quote vault (v2 trades).
    pub protocol_fees: u64,
    pub creator_fees: u64,
}

impl Pool {
    pub fn parse(d: &[u8]) -> Result<Self> {
        check_disc(d, &POOL_DISCRIMINATOR, "pool")?;
        if d.len() < POOL_MIN_LEN {
            return Err(format!("pool data too short: {} bytes", d.len()).into());
        }
        Ok(Pool {
            index: u16::from_le_bytes(rd(d, 9)),
            creator: pk_at(d, 11),
            base_mint: pk_at(d, 43),
            quote_mint: pk_at(d, 75),
            // lp_mint at 107
            pool_base_token_account: pk_at(d, 139),
            pool_quote_token_account: pk_at(d, 171),
            // lp_supply at 203
            coin_creator: pk_at(d, 211),
            is_mayhem_mode: rd::<1>(d, 243)[0] != 0,
            is_cashback_coin: rd::<1>(d, 244)[0] != 0,
            virtual_quote_reserves: i128::from_le_bytes(rd(d, 245)),
            creator_fee_bps: u64_at(d, 261),
            // can_edit_creator_fee 269, is_holder_reward 270
            protocol_fees: u64_at(d, 271),
            creator_fees: u64_at(d, 279),
        })
    }

    pub fn is_sol_quoted(&self) -> bool {
        self.quote_mint == pda::WSOL_MINT
    }

    /// What prices the pool: vault balance + `virtual_quote_reserves`.
    pub fn effective_quote_reserve(&self, quote_vault_balance: u64) -> Result<u64> {
        u64::try_from(quote_vault_balance as i128 + self.virtual_quote_reserves)
            .map_err(|_| "pool: effective quote reserves out of range".into())
    }

    /// Vault balance that is liquidity (sells are paid from it): balance − fee buckets.
    pub fn real_quote_reserve(&self, quote_vault_balance: u64) -> Result<u64> {
        quote_vault_balance
            .checked_sub(self.protocol_fees.saturating_add(self.creator_fees))
            .ok_or_else(|| "pool: fee buckets exceed quote vault".into())
    }
}

// ---------------------------------------------------------------- GlobalConfig

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GlobalConfig {
    pub disable_flags: u8,
    pub buyback_fee_recipients: Vec<Pubkey>,
    pub buyback_basis_points: u64,
    pub creator_fee_configurable: bool,
}

impl GlobalConfig {
    pub fn parse(d: &[u8]) -> Result<Self> {
        check_disc(d, &GLOBAL_CONFIG_DISCRIMINATOR, "amm global config")?;
        Ok(GlobalConfig {
            disable_flags: rd::<1>(d, 56)[0],
            buyback_fee_recipients: (0..8).map(|i| pk_at(d, 643 + 32 * i)).filter(|p| *p != Pubkey::default()).collect(),
            buyback_basis_points: u64_at(d, 899),
            creator_fee_configurable: rd::<1>(d, 940)[0] != 0,
        })
    }

    /// `disable_flags` bit 3 = buys off, bit 4 = sells off.
    pub fn trading_enabled(&self, is_buy: bool) -> bool {
        self.disable_flags & (1 << if is_buy { 3 } else { 4 }) == 0
    }
}

// ---------------------------------------------------------------- FeeConfig (AMM)

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Fees {
    pub lp_fee_bps: u64,
    pub protocol_fee_bps: u64,
    pub creator_fee_bps: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FeeTier {
    pub market_cap_lamports_threshold: u128,
    pub fees: Fees,
}

/// pump-fees `FeeConfig` at 5PHirr… (the AMM's; includes stable / exotic schedules).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct AmmFeeConfig {
    pub flat_fees: Fees,
    pub fee_tiers: Vec<FeeTier>,
    pub stable_fee_tiers: Vec<FeeTier>,
    pub exotic_flat_fees: Fees,
}

impl AmmFeeConfig {
    pub fn parse(d: &[u8]) -> Result<Self> {
        check_disc(d, &FEE_CONFIG_DISCRIMINATOR, "amm fee config")?;
        let fees = |o: usize| Fees { lp_fee_bps: u64_at(d, o), protocol_fee_bps: u64_at(d, o + 8), creator_fee_bps: u64_at(d, o + 16) };
        let tiers = |off: &mut usize| -> Result<Vec<FeeTier>> {
            let n = u32::from_le_bytes(rd(d, *off)) as usize;
            *off += 4;
            if n > 64 || *off + n * 40 > d.len() {
                return Err(format!("amm fee config: implausible tier count {n}").into());
            }
            let v = (0..n)
                .map(|i| {
                    let o = *off + 40 * i;
                    FeeTier { market_cap_lamports_threshold: u128::from_le_bytes(rd(d, o)), fees: fees(o + 16) }
                })
                .collect();
            *off += n * 40;
            Ok(v)
        };
        let mut off = 8 + 1 + 32; // disc, bump, admin
        let flat_fees = fees(off);
        off += 24;
        let fee_tiers = tiers(&mut off)?;
        let stable_fee_tiers = tiers(&mut off)?;
        Ok(AmmFeeConfig { flat_fees, fee_tiers, stable_fee_tiers, exotic_flat_fees: fees(off) })
    }
}

/// Highest tier with threshold ≤ market cap, else the first; None on an empty table.
fn tier(tiers: &[FeeTier], market_cap: u128) -> Option<&Fees> {
    let first = tiers.first()?;
    Some(tiers.iter().rev().find(|t| market_cap >= t.market_cap_lamports_threshold).map_or(&first.fees, |t| &t.fees))
}

// ---------------------------------------------------------------- market snapshot

/// Everything a quote needs: pool + its vault balances + base mint supply.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AmmMarket {
    pub pool_key: Pubkey,
    pub pool: Pool,
    pub base_token_program: Pubkey,
    /// `pool_base_token_account.amount`.
    pub base_reserve: u64,
    /// `pool_quote_token_account.amount` (raw, incl. un-swept fees).
    pub quote_vault: u64,
    pub mint_supply: u64,
}

impl AmmMarket {
    pub fn effective_quote_reserve(&self) -> Result<u64> {
        self.pool.effective_quote_reserve(self.quote_vault)
    }

    /// Spot market cap in lamports (what selects the fee tier).
    pub fn market_cap(&self) -> Result<u128> {
        if self.base_reserve == 0 {
            return Err("pool has no base reserves".into());
        }
        let supply = if self.pool.is_mayhem_mode { MAYHEM_TIER_SUPPLY } else { self.mint_supply };
        Ok(self.effective_quote_reserve()? as u128 * supply as u128 / self.base_reserve as u128)
    }

    /// Error unless a v2 trade on this pool is something we support.
    pub fn check_supported(&self) -> Result<()> {
        if !self.pool.is_sol_quoted() {
            return Err(format!("pool quoted in {} (only wSOL pools supported)", self.pool.quote_mint).into());
        }
        if self.pool.is_cashback_coin {
            return Err("cashback-coin pool: v2 trades unsupported (6079), needs v1".into());
        }
        Ok(())
    }

    /// Market state after a v2 buy (what lands where, per pump-amm `BuyLeg`).
    pub fn after_buy(&self, q: &AmmBuyQuote) -> Self {
        let kept = q.protocol_fee - q.buyback_fee + q.creator_fee; // fee buckets
        let mut pool = self.pool;
        pool.protocol_fees += q.protocol_fee - q.buyback_fee;
        pool.creator_fees += q.creator_fee;
        pool.virtual_quote_reserves -= kept as i128;
        AmmMarket {
            pool,
            base_reserve: self.base_reserve - q.tokens_out,
            quote_vault: self.quote_vault + q.net_in + q.lp_fee + kept,
            ..*self
        }
    }
}

impl AmmBuyQuote {
    /// Lamports that land in the pool's quote vault (everything but the buyback slice).
    pub fn vault_in(&self) -> u64 {
        self.debit - self.buyback_fee
    }
}

/// `[lp, protocol, creator]` bps charged on this pool right now (`compute_amm_fee_bps`;
/// the creator slice is 0 when the pool has no coin creator).
pub fn fee_bps(gc: &GlobalConfig, fc: &AmmFeeConfig, m: &AmmMarket) -> Result<[u64; 3]> {
    let mcap = m.market_cap()?;
    let q = &m.pool.quote_mint;
    let fees = if !is_pump_pool(&m.pool.base_mint, &m.pool.creator) {
        &fc.flat_fees
    } else if crate::bonding::is_sol_like_quote_mint(q) {
        tier(&fc.fee_tiers, mcap).ok_or("amm fee config: empty fee tiers")?
    } else if *q == USDC_MINT {
        tier(&fc.stable_fee_tiers, mcap).ok_or("amm fee config: empty stable fee tiers")?
    } else if fc.exotic_flat_fees == Fees::default() {
        &fc.flat_fees
    } else {
        &fc.exotic_flat_fees
    };
    let creator = if m.pool.coin_creator == Pubkey::default() {
        0
    } else if gc.creator_fee_configurable && m.pool.creator_fee_bps != 0 {
        m.pool.creator_fee_bps
    } else {
        fees.creator_fee_bps
    };
    Ok([fees.lp_fee_bps, fees.protocol_fee_bps, creator])
}

fn fee_amount(amount: u128, bps: u64) -> u128 {
    (amount * bps as u128).div_ceil(BPS)
}

// ---------------------------------------------------------------- quotes

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AmmBuyQuote {
    pub tokens_out: u64,
    /// Lamports entering the price-moving reserves.
    pub net_in: u64,
    pub lp_fee: u64,
    pub protocol_fee: u64,
    pub creator_fee: u64,
    /// Part of `protocol_fee` paid out in the trade to the buyback ATA.
    pub buyback_fee: u64,
    /// Lamports leaving the user's wSOL account (≤ budget).
    pub debit: u64,
}

/// `buy_exact_quote_in_v2` spending `sol_in` lamports (incl. all fees). Exact port of
/// `buy_quote_input_v2` for an untaxed quote: net = floor(in·10⁴/(10⁴+Σbps)), each
/// fee ceil(net·bps/10⁴), overshoot shaved off net, tokens = swap_exact_input(net − 1).
pub fn buy_exact_quote_in(gc: &GlobalConfig, fc: &AmmFeeConfig, m: &AmmMarket, sol_in: u64) -> Result<AmmBuyQuote> {
    if !gc.trading_enabled(true) {
        return Err("pumpswap buys disabled".into());
    }
    let quote_reserve = m.effective_quote_reserve()?;
    if m.base_reserve == 0 || quote_reserve == 0 {
        return Err("pool has empty reserves".into());
    }
    let bps = fee_bps(gc, fc, m)?;
    let input = sol_in as u128;
    let mut net = input * BPS / (BPS + bps.iter().map(|b| *b as u128).sum::<u128>());
    if net == 0 {
        return Err("buy amount too small".into());
    }
    let [lp, protocol, creator] = bps.map(|b| fee_amount(net, b));
    if net + lp + protocol + creator > input {
        net = net.saturating_sub(net + lp + protocol + creator - input);
    }
    if net <= 1 {
        return Err("buy amount too small to cover fees".into());
    }
    let tokens = (net - 1) * m.base_reserve as u128 / (quote_reserve as u128 + net - 1);
    if tokens == 0 {
        return Err("quote returned 0 tokens".into());
    }
    let buyback = if m.pool.is_mayhem_mode { 0 } else { protocol * gc.buyback_basis_points as u128 / BPS };
    Ok(AmmBuyQuote {
        tokens_out: tokens as u64,
        net_in: net as u64,
        lp_fee: lp as u64,
        protocol_fee: protocol as u64,
        creator_fee: creator as u64,
        buyback_fee: buyback as u64,
        debit: (net + lp + protocol + creator) as u64,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AmmSellQuote {
    /// Lamports (wSOL) the seller receives after all fees.
    pub sol_out: u64,
    /// Constant-product output before fees.
    pub gross: u64,
    /// lp + protocol + creator.
    pub fees: u64,
    /// Part of the protocol fee paid out of the quote vault to the buyback ATA.
    pub buyback_fee: u64,
}

/// `sell_v2` of `tokens`: floor(tokens·Q/(B+tokens)) minus lp + protocol + creator fees
/// (each ceil). Port of `sell_base_input`, incl. its real-reserve payout check.
pub fn sell(gc: &GlobalConfig, fc: &AmmFeeConfig, m: &AmmMarket, tokens: u64) -> Result<AmmSellQuote> {
    if !gc.trading_enabled(false) {
        return Err("pumpswap sells disabled".into());
    }
    let quote_reserve = m.effective_quote_reserve()?;
    if m.base_reserve == 0 || quote_reserve == 0 {
        return Err("pool has empty reserves".into());
    }
    if tokens == 0 {
        return Ok(AmmSellQuote { sol_out: 0, gross: 0, fees: 0, buyback_fee: 0 });
    }
    let gross = tokens as u128 * quote_reserve as u128 / (m.base_reserve as u128 + tokens as u128);
    let [lp, protocol, creator] = fee_bps(gc, fc, m)?.map(|b| fee_amount(gross, b));
    let fees = lp + protocol + creator;
    if gross < fees {
        return Err("sell fees exceed output".into());
    }
    if (m.pool.real_quote_reserve(m.quote_vault)? as u128) < gross - lp {
        return Err("sell exceeds the pool's real quote reserves".into());
    }
    let buyback = if m.pool.is_mayhem_mode { 0 } else { protocol * gc.buyback_basis_points as u128 / BPS };
    Ok(AmmSellQuote { sol_out: (gross - fees) as u64, gross: gross as u64, fees: fees as u64, buyback_fee: buyback as u64 })
}

// ---------------------------------------------------------------- fetching

/// Fetch the AMM GlobalConfig and FeeConfig (owners checked).
pub async fn fetch_configs(rpc: &Rpc) -> Result<(GlobalConfig, AmmFeeConfig)> {
    let accs = rpc.get_multiple_accounts(&[AMM_GLOBAL_CONFIG, AMM_FEE_CONFIG]).await?;
    let g = accs[0].as_ref().ok_or("pumpswap GlobalConfig not found")?;
    let f = accs[1].as_ref().ok_or("pumpswap FeeConfig not found")?;
    if g.owner != PUMP_AMM_PROGRAM || f.owner != pda::PUMP_FEES_PROGRAM {
        return Err("pumpswap GlobalConfig/FeeConfig owned by unexpected programs".into());
    }
    let gc = GlobalConfig::parse(&g.data)?;
    if gc.buyback_fee_recipients.is_empty() {
        return Err("pumpswap GlobalConfig has no buyback fee recipients".into());
    }
    Ok((gc, AmmFeeConfig::parse(&f.data)?))
}

/// Fetch the canonical (mint, wSOL) pool, its vault balances and the mint (2 RPC round trips).
pub async fn fetch_market(rpc: &Rpc, mint: &Pubkey) -> Result<AmmMarket> {
    fetch_market_at(rpc, &canonical_pool(mint, &pda::WSOL_MINT)).await
}

/// Canonical (index-0, wSOL) pool and the addresses its vaults normally have (ATAs of
/// the pool) for both possible base token programs — lets a caller read curve, mint,
/// pool and vaults in ONE `getMultipleAccounts` before knowing which venue trades.
/// Returns [pool, base vault (Token-2022), base vault (SPL), quote vault (wSOL)].
pub fn canonical_keys(mint: &Pubkey) -> [Pubkey; 4] {
    let pool = canonical_pool(mint, &pda::WSOL_MINT);
    [
        pool,
        pda::ata(&pool, mint, &pda::TOKEN_2022_PROGRAM),
        pda::ata(&pool, mint, &pda::TOKEN_PROGRAM),
        pda::ata(&pool, &pda::WSOL_MINT, &pda::TOKEN_PROGRAM),
    ]
}

impl AmmMarket {
    /// Build from already-fetched accounts. `vaults` are (key, account) candidates;
    /// the ones matching the pool's recorded vault addresses are used. `None` when a
    /// vault is not among them (caller falls back to `fetch_market_at`).
    pub fn from_accounts(
        pool_key: Pubkey,
        pool_acc: &crate::rpc::AccountData,
        vaults: &[(Pubkey, Option<&crate::rpc::AccountData>)],
        base_token_program: Pubkey,
        mint_supply: u64,
    ) -> Result<Option<Self>> {
        if pool_acc.owner != PUMP_AMM_PROGRAM {
            return Err(format!("{pool_key} is not a pumpswap account").into());
        }
        let pool = Pool::parse(&pool_acc.data)?;
        let find = |k: &Pubkey| vaults.iter().find(|(key, _)| key == k).and_then(|(_, a)| *a).and_then(|a| a.token_amount());
        let (Some(base_reserve), Some(quote_vault)) = (find(&pool.pool_base_token_account), find(&pool.pool_quote_token_account)) else {
            return Ok(None);
        };
        Ok(Some(AmmMarket { pool_key, pool, base_token_program, base_reserve, quote_vault, mint_supply }))
    }
}

/// Same for any pool address.
pub async fn fetch_market_at(rpc: &Rpc, pool_key: &Pubkey) -> Result<AmmMarket> {
    let a = rpc.get_multiple_accounts(&[*pool_key]).await?;
    let acc = a[0].as_ref().ok_or_else(|| format!("pumpswap pool {pool_key} not found"))?;
    if acc.owner != PUMP_AMM_PROGRAM {
        return Err(format!("{pool_key} is not a pumpswap account").into());
    }
    let pool = Pool::parse(&acc.data)?;
    let b = rpc.get_multiple_accounts(&[pool.base_mint, pool.pool_base_token_account, pool.pool_quote_token_account]).await?;
    let mint = b[0].as_ref().ok_or("base mint not found")?;
    let amount = |i: usize, what: &str| -> Result<u64> {
        b[i].as_ref().and_then(|x| x.token_amount()).ok_or_else(|| format!("pool {what} vault missing").into())
    };
    Ok(AmmMarket {
        pool_key: *pool_key,
        pool,
        base_token_program: mint.owner,
        base_reserve: amount(1, "base")?,
        quote_vault: amount(2, "quote")?,
        mint_supply: crate::mint_supply(&mint.data)?,
    })
}

// ---------------------------------------------------------------- instructions

/// Addresses a v2 trade on one SOL pool needs for one user.
#[derive(Debug, Clone, Copy)]
pub struct AmmAccounts {
    pub pool: Pubkey,
    pub base_mint: Pubkey,
    pub base_token_program: Pubkey,
    pub pool_base_token_account: Pubkey,
    pub pool_quote_token_account: Pubkey,
    pub user_base_ata: Pubkey,
    /// User's wSOL ATA (created, funded and closed inside each trade tx).
    pub user_wsol_ata: Pubkey,
    pub user_volume_accumulator: Pubkey,
}

impl AmmAccounts {
    pub fn new(m: &AmmMarket, user: &Pubkey) -> Self {
        AmmAccounts {
            pool: m.pool_key,
            base_mint: m.pool.base_mint,
            base_token_program: m.base_token_program,
            pool_base_token_account: m.pool.pool_base_token_account,
            pool_quote_token_account: m.pool.pool_quote_token_account,
            user_base_ata: pda::ata(user, &m.pool.base_mint, &m.base_token_program),
            user_wsol_ata: pda::ata(user, &pda::WSOL_MINT, &pda::TOKEN_PROGRAM),
            user_volume_accumulator: user_volume_accumulator(user),
        }
    }
}

/// wSOL ATA of a buyback wallet (`GlobalConfig.buyback_fee_recipients[i]`, the same 8
/// wallets as docs/FEE_RECIPIENTS.md) — the v2 `buyback_fee_recipient` account.
pub fn buyback_fee_recipient(gc: &GlobalConfig, i: usize) -> Pubkey {
    let wallet = gc.buyback_fee_recipients[i % gc.buyback_fee_recipients.len()];
    pda::ata(&wallet, &pda::WSOL_MINT, &pda::TOKEN_PROGRAM)
}

/// The 17 accounts of `buy_v2` / `buy_exact_quote_in_v2` / `sell_v2` (wSOL quote).
fn v2_accounts(a: &AmmAccounts, user: &Pubkey, buyback_ata: &Pubkey) -> Vec<AccountMeta> {
    vec![
        AccountMeta::new(a.pool, false),
        AccountMeta::new(*user, true),
        AccountMeta::new_readonly(AMM_GLOBAL_CONFIG, false),
        AccountMeta::new_readonly(a.base_mint, false),
        AccountMeta::new_readonly(pda::WSOL_MINT, false),
        AccountMeta::new(a.user_base_ata, false),
        AccountMeta::new(a.user_wsol_ata, false),
        AccountMeta::new(a.pool_base_token_account, false),
        AccountMeta::new(a.pool_quote_token_account, false),
        AccountMeta::new_readonly(a.base_token_program, false),
        AccountMeta::new_readonly(pda::TOKEN_PROGRAM, false),
        AccountMeta::new_readonly(pda::SYSTEM_PROGRAM, false),
        AccountMeta::new(a.user_volume_accumulator, false),
        AccountMeta::new_readonly(AMM_FEE_CONFIG, false),
        AccountMeta::new(*buyback_ata, false),
        AccountMeta::new_readonly(AMM_EVENT_AUTH, false),
        AccountMeta::new_readonly(PUMP_AMM_PROGRAM, false),
    ]
}

fn ix_data(disc: &[u8; 8], a: u64, b: u64) -> Vec<u8> {
    let mut d = Vec::with_capacity(24);
    d.extend_from_slice(disc);
    d.extend_from_slice(&a.to_le_bytes());
    d.extend_from_slice(&b.to_le_bytes());
    d
}

pub fn buy_exact_quote_in_ix(a: &AmmAccounts, user: &Pubkey, buyback_ata: &Pubkey, spendable_sol_in: u64, min_tokens_out: u64) -> Instruction {
    Instruction {
        program_id: PUMP_AMM_PROGRAM,
        accounts: v2_accounts(a, user, buyback_ata),
        data: ix_data(&BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR, spendable_sol_in, min_tokens_out),
    }
}

pub fn sell_ix(a: &AmmAccounts, user: &Pubkey, buyback_ata: &Pubkey, token_amount: u64, min_sol_out: u64) -> Instruction {
    Instruction {
        program_id: PUMP_AMM_PROGRAM,
        accounts: v2_accounts(a, user, buyback_ata),
        data: ix_data(&SELL_V2_DISCRIMINATOR, token_amount, min_sol_out),
    }
}

fn create_wsol_ata(user: &Pubkey) -> Instruction {
    pda::create_ata_idempotent_ix(user, user, &pda::WSOL_MINT, &pda::TOKEN_PROGRAM)
}

/// Buy legs without budget/tips: create base ATA + create wSOL ATA + wrap `sol_in` +
/// buy_exact_quote_in_v2 (+ close the wSOL ATA back to `user` when `close_wsol`).
pub fn buy_trade_ixs(a: &AmmAccounts, user: &Pubkey, buyback_ata: &Pubkey, sol_in: u64, min_tokens_out: u64, close_wsol: bool) -> Vec<Instruction> {
    let mut v = vec![
        pda::create_ata_idempotent_ix(user, user, &a.base_mint, &a.base_token_program),
        create_wsol_ata(user),
        tx_builder::transfer(user, &a.user_wsol_ata, sol_in),
        tx_builder::sync_native(&a.user_wsol_ata),
        buy_exact_quote_in_ix(a, user, buyback_ata, sol_in, min_tokens_out),
    ];
    if close_wsol {
        v.push(tx_builder::close_account(&pda::TOKEN_PROGRAM, &a.user_wsol_ata, user, user));
    }
    v
}

/// Sell legs without budget/tips: create wSOL ATA + sell_v2 + close wSOL (proceeds and
/// rent → `user` as SOL) (+ close the base ATA when `close_base_ata`, i.e. full balance sold).
pub fn sell_trade_ixs(a: &AmmAccounts, user: &Pubkey, buyback_ata: &Pubkey, token_amount: u64, min_sol_out: u64, close_base_ata: bool) -> Vec<Instruction> {
    let mut v = vec![
        create_wsol_ata(user),
        sell_ix(a, user, buyback_ata, token_amount, min_sol_out),
        tx_builder::close_account(&pda::TOKEN_PROGRAM, &a.user_wsol_ata, user, user),
    ];
    if close_base_ata {
        v.push(tx_builder::close_account(&a.base_token_program, &a.user_base_ata, user, user));
    }
    v
}

/// Full buy tx instruction list: compute budget + buy legs (wSOL closed) + tips.
#[allow(clippy::too_many_arguments)]
pub fn buy_instructions(
    user: &Pubkey,
    a: &AmmAccounts,
    buyback_ata: &Pubkey,
    sol_in: u64,
    min_tokens_out: u64,
    budget: Budget,
    tips: &[(Pubkey, u64)],
) -> Vec<Instruction> {
    tx_builder::with_budget_and_tips(user, budget, buy_trade_ixs(a, user, buyback_ata, sol_in, min_tokens_out, true), tips)
}

/// Full sell tx instruction list: compute budget + sell legs + tips.
#[allow(clippy::too_many_arguments, dead_code)] // called once main.rs trades AMM pools
pub fn sell_instructions(
    user: &Pubkey,
    a: &AmmAccounts,
    buyback_ata: &Pubkey,
    token_amount: u64,
    min_sol_out: u64,
    budget: Budget,
    tips: &[(Pubkey, u64)],
    close_base_ata: bool,
) -> Vec<Instruction> {
    tx_builder::with_budget_and_tips(user, budget, sell_trade_ixs(a, user, buyback_ata, token_amount, min_sol_out, close_base_ata), tips)
}

#[cfg(test)]
mod tests {
    use super::*;
    use solana_sdk::hash::{hash, Hash};
    use solana_sdk::signature::Keypair;
    use solana_sdk::signer::Signer;

    #[test]
    fn discriminators_are_anchor_sighashes() {
        assert_eq!(hash(b"global:buy_exact_quote_in_v2").to_bytes()[..8], BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR);
        assert_eq!(hash(b"global:sell_v2").to_bytes()[..8], SELL_V2_DISCRIMINATOR);
        assert_eq!(hash(b"account:Pool").to_bytes()[..8], POOL_DISCRIMINATOR);
        assert_eq!(hash(b"account:GlobalConfig").to_bytes()[..8], GLOBAL_CONFIG_DISCRIMINATOR);
        assert_eq!(hash(b"account:FeeConfig").to_bytes()[..8], FEE_CONFIG_DISCRIMINATOR);
    }

    #[test]
    fn constants_match_seed_derivations() {
        let p = |seeds: &[&[u8]], prog: &Pubkey| Pubkey::find_program_address(seeds, prog).0;
        assert_eq!(p(&[b"global_config"], &PUMP_AMM_PROGRAM), AMM_GLOBAL_CONFIG);
        assert_eq!(p(&[b"__event_authority"], &PUMP_AMM_PROGRAM), AMM_EVENT_AUTH);
        assert_eq!(p(&[b"fee_config", PUMP_AMM_PROGRAM.as_ref()], &pda::PUMP_FEES_PROGRAM), AMM_FEE_CONFIG);
    }

    /// README example pool GseMAnND… (index 0, creator 9XDYTf…, base 7LSs…pump, wSOL).
    #[test]
    fn pool_pda_matches_docs_example() {
        let creator = pubkey!("9XDYTfQKwW8sHPqnFdUreMmtmffmkHVPGTNV2e3LKxNW");
        let base = pubkey!("7LSsEoJGhLeZzGvDofTdNg7M3JttxQqGWNLo6vWMpump");
        assert_eq!(pool_address(0, &creator, &base, &pda::WSOL_MINT), pubkey!("GseMAnNDvntR5uFePZ51yZBXzNSn7GdFPkfHwfr6d77J"));
    }

    fn encode_pool(p: &Pool) -> Vec<u8> {
        let mut d = vec![0u8; POOL_LEN];
        d[..8].copy_from_slice(&POOL_DISCRIMINATOR);
        d[8] = 254;
        d[9..11].copy_from_slice(&p.index.to_le_bytes());
        for (o, k) in [(11, p.creator), (43, p.base_mint), (75, p.quote_mint), (139, p.pool_base_token_account), (171, p.pool_quote_token_account), (211, p.coin_creator)] {
            d[o..o + 32].copy_from_slice(k.as_ref());
        }
        d[243] = p.is_mayhem_mode as u8;
        d[244] = p.is_cashback_coin as u8;
        d[245..261].copy_from_slice(&p.virtual_quote_reserves.to_le_bytes());
        d[261..269].copy_from_slice(&p.creator_fee_bps.to_le_bytes());
        d[271..279].copy_from_slice(&p.protocol_fees.to_le_bytes());
        d[279..287].copy_from_slice(&p.creator_fees.to_le_bytes());
        d
    }

    fn sample_pool() -> Pool {
        let base_mint = Pubkey::new_unique();
        Pool {
            index: 0,
            creator: pool_authority(&base_mint),
            base_mint,
            quote_mint: pda::WSOL_MINT,
            pool_base_token_account: Pubkey::new_unique(),
            pool_quote_token_account: Pubkey::new_unique(),
            coin_creator: Pubkey::new_unique(),
            is_mayhem_mode: false,
            is_cashback_coin: false,
            virtual_quote_reserves: -1_234,
            creator_fee_bps: 0,
            protocol_fees: 1_000,
            creator_fees: 234,
        }
    }

    #[test]
    fn pool_parse_roundtrip_and_short_layout() {
        let p = Pool { index: 3, is_mayhem_mode: true, creator_fee_bps: 77, ..sample_pool() };
        let d = encode_pool(&p);
        assert_eq!(Pool::parse(&d).unwrap(), p);
        // Pool written before the appended fields (ends after is_cashback_coin, 245 bytes).
        let old = Pool::parse(&d[..245]).unwrap();
        assert_eq!((old.virtual_quote_reserves, old.protocol_fees, old.creator_fees), (0, 0, 0));
        assert!(Pool::parse(&d[..100]).is_err());
        let mut bad = d.clone();
        bad[0] ^= 1;
        assert!(Pool::parse(&bad).is_err());
        assert_eq!(p.effective_quote_reserve(10_000).unwrap(), 10_000 - 1_234);
        assert_eq!(p.real_quote_reserve(10_000).unwrap(), 10_000 - 1_234);
    }

    #[test]
    fn global_config_offsets() {
        let mut d = vec![0u8; 949];
        d[..8].copy_from_slice(&GLOBAL_CONFIG_DISCRIMINATOR);
        d[56] = 1 << 4; // sells disabled
        let bb = Pubkey::new_unique();
        d[643 + 32..643 + 64].copy_from_slice(bb.as_ref());
        d[899..907].copy_from_slice(&2_500u64.to_le_bytes());
        d[940] = 1;
        let gc = GlobalConfig::parse(&d).unwrap();
        assert_eq!(gc.buyback_fee_recipients, vec![bb]);
        assert_eq!((gc.buyback_basis_points, gc.creator_fee_configurable), (2_500, true));
        assert!(gc.trading_enabled(true) && !gc.trading_enabled(false));
    }

    fn fees(lp: u64, p: u64, c: u64) -> Fees {
        Fees { lp_fee_bps: lp, protocol_fee_bps: p, creator_fee_bps: c }
    }

    #[test]
    fn fee_config_parse_all_schedules() {
        let mut d = FEE_CONFIG_DISCRIMINATOR.to_vec();
        d.push(255);
        d.extend_from_slice(&[9u8; 32]);
        let put_fees = |d: &mut Vec<u8>, f: Fees| [f.lp_fee_bps, f.protocol_fee_bps, f.creator_fee_bps].iter().for_each(|v| d.extend_from_slice(&v.to_le_bytes()));
        put_fees(&mut d, fees(25, 5, 0));
        d.extend_from_slice(&2u32.to_le_bytes());
        for (t, f) in [(0u128, fees(2, 93, 30)), (420_000_000_000u128, fees(20, 5, 95))] {
            d.extend_from_slice(&t.to_le_bytes());
            put_fees(&mut d, f);
        }
        d.extend_from_slice(&1u32.to_le_bytes());
        d.extend_from_slice(&0u128.to_le_bytes());
        put_fees(&mut d, fees(3, 70, 10));
        put_fees(&mut d, fees(5, 300, 25));
        let fc = AmmFeeConfig::parse(&d).unwrap();
        assert_eq!(fc.flat_fees, fees(25, 5, 0));
        assert_eq!(fc.fee_tiers.len(), 2);
        assert_eq!(fc.fee_tiers[1].market_cap_lamports_threshold, 420_000_000_000);
        assert_eq!(fc.fee_tiers[1].fees, fees(20, 5, 95));
        assert_eq!(fc.stable_fee_tiers[0].fees, fees(3, 70, 10));
        assert_eq!(fc.exotic_flat_fees, fees(5, 300, 25));
        assert!(AmmFeeConfig::parse(&d[..70]).is_err());
    }

    fn market() -> (GlobalConfig, AmmFeeConfig, AmmMarket) {
        let gc = GlobalConfig { buyback_basis_points: 5_000, creator_fee_configurable: true, buyback_fee_recipients: vec![Pubkey::new_unique()], ..Default::default() };
        let tiers = vec![
            FeeTier { market_cap_lamports_threshold: 0, fees: fees(2, 93, 30) },
            FeeTier { market_cap_lamports_threshold: 1_000_000_000_000, fees: fees(20, 5, 5) },
        ];
        let fc = AmmFeeConfig { flat_fees: fees(25, 5, 0), fee_tiers: tiers.clone(), stable_fee_tiers: tiers, exotic_flat_fees: Fees::default() };
        let pool = Pool { virtual_quote_reserves: 0, protocol_fees: 0, creator_fees: 0, ..sample_pool() };
        let m = AmmMarket { pool_key: Pubkey::new_unique(), pool, base_token_program: pda::TOKEN_PROGRAM, base_reserve: 200_000_000_000_000, quote_vault: 400_000_000_000, mint_supply: 1_000_000_000_000_000 };
        (gc, fc, m)
    }

    #[test]
    fn fee_selection_rules() {
        let (gc, fc, m) = market();
        // mcap = 400 SOL * 1e15 / 2e14 = 2000 SOL ≥ 1000 SOL threshold → second tier.
        assert_eq!(fee_bps(&gc, &fc, &m).unwrap(), [20, 5, 5]);
        let low = AmmMarket { quote_vault: 100_000_000_000, ..m };
        assert_eq!(fee_bps(&gc, &fc, &low).unwrap(), [2, 93, 30]);
        let third_party = AmmMarket { pool: Pool { creator: Pubkey::new_unique(), ..m.pool }, ..m };
        assert_eq!(fee_bps(&gc, &fc, &third_party).unwrap(), [25, 5, 0]);
        let no_creator = AmmMarket { pool: Pool { coin_creator: Pubkey::default(), ..m.pool }, ..m };
        assert_eq!(fee_bps(&gc, &fc, &no_creator).unwrap(), [20, 5, 0]);
        let custom = AmmMarket { pool: Pool { creator_fee_bps: 50, ..m.pool }, ..m };
        assert_eq!(fee_bps(&gc, &fc, &custom).unwrap(), [20, 5, 50]);
    }

    #[test]
    fn buy_quote_fits_budget_and_round_trip_loses_fees() {
        let (gc, fc, m) = market();
        let q = buy_exact_quote_in(&gc, &fc, &m, 1_000_000_000).unwrap();
        assert!(q.debit <= 1_000_000_000 && q.debit >= 1_000_000_000 - 3);
        // net = floor(1e9 * 1e4 / 10030)
        assert_eq!(q.net_in, 997_008_972); // floor(1e9·1e4/10030) = …973, ceil fees overshoot by 1 → shaved
        assert_eq!(q.buyback_fee, q.protocol_fee / 2);
        let no_fee = 999_999_999u128 * m.base_reserve as u128 / (m.quote_vault as u128 + 999_999_999);
        assert!((q.tokens_out as u128) < no_fee);
        let after = m.after_buy(&q);
        // fee buckets stay in the vault but out of the pricing reserves
        assert_eq!(after.effective_quote_reserve().unwrap(), m.quote_vault + q.net_in + q.lp_fee);
        assert_eq!(after.pool.real_quote_reserve(after.quote_vault).unwrap(), m.quote_vault + q.net_in + q.lp_fee);
        let s = sell(&gc, &fc, &after, q.tokens_out).unwrap();
        assert!(s.sol_out < 1_000_000_000 && s.sol_out > 993_000_000, "{s:?}");
        assert!(buy_exact_quote_in(&gc, &fc, &m, 1).is_err());
        let sells_off = GlobalConfig { disable_flags: 1 << 4, ..gc.clone() };
        assert!(sell(&sells_off, &fc, &m, 1_000).is_err() && buy_exact_quote_in(&sells_off, &fc, &m, 1_000_000).is_ok());
    }

    #[test]
    fn mayhem_pool_has_no_buyback_and_fixed_tier_supply() {
        let (gc, fc, m) = market();
        let mm = AmmMarket { pool: Pool { is_mayhem_mode: true, ..m.pool }, mint_supply: 1, ..m };
        assert_eq!(mm.market_cap().unwrap(), m.market_cap().unwrap());
        assert_eq!(buy_exact_quote_in(&gc, &fc, &mm, 1_000_000_000).unwrap().buyback_fee, 0);
    }

    #[test]
    fn ix_layout_matches_idl() {
        let (gc, _, m) = market();
        let user = Pubkey::new_unique();
        let a = AmmAccounts::new(&m, &user);
        let bb = buyback_fee_recipient(&gc, 0);
        for (ix, disc) in [(buy_exact_quote_in_ix(&a, &user, &bb, 7, 9), BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR), (sell_ix(&a, &user, &bb, 7, 9), SELL_V2_DISCRIMINATOR)] {
            assert_eq!(ix.accounts.len(), 17);
            assert_eq!(ix.data, ix_data(&disc, 7, 9));
            let w: Vec<bool> = ix.accounts.iter().map(|m| m.is_writable).collect();
            assert_eq!(w, [true, true, false, false, false, true, true, true, true, false, false, false, true, false, true, false, false]);
            assert!(ix.accounts[1].is_signer && ix.accounts.iter().filter(|m| m.is_signer).count() == 1);
            assert_eq!(ix.accounts[14].pubkey, pda::ata(&gc.buyback_fee_recipients[0], &pda::WSOL_MINT, &pda::TOKEN_PROGRAM));
            assert_eq!(ix.accounts[12].pubkey, user_volume_accumulator(&user));
        }
    }

    #[test]
    fn full_txs_sign_and_fit() {
        let (gc, _, m) = market();
        let kp = Keypair::new();
        let user = kp.pubkey();
        let m = AmmMarket { base_token_program: pda::TOKEN_2022_PROGRAM, ..m };
        let a = AmmAccounts::new(&m, &user);
        let bb = buyback_fee_recipient(&gc, 3);
        let budget = Budget { cu_limit: 150_000, cu_price_micro_lamports: 100_000 };
        let tips = [(crate::jito::TIP_ACCOUNTS[0], 10_000), (Pubkey::new_unique(), 5_000)];
        let buy = buy_instructions(&user, &a, &bb, 100_000_000, 1, budget, &tips);
        assert_eq!(buy.len(), 2 + 6 + 2);
        let sell = sell_instructions(&user, &a, &bb, 5, 1, budget, &tips, true);
        assert_eq!(sell.len(), 2 + 4 + 2);
        assert_eq!((sell[5].program_id, sell[5].data.clone()), (pda::TOKEN_2022_PROGRAM, vec![9]));
        for ixs in [buy, sell] {
            let tx = tx_builder::sign(&kp, &ixs, Hash::new_unique()).unwrap();
            let n = bincode::serialize(&tx).unwrap().len();
            assert!(n <= 1232, "tx too large: {n}");
        }
    }
}
