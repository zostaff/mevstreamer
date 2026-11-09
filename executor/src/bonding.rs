//! pump.fun on-chain state (BondingCurve, Global, pump-fees FeeConfig) and
//! fee-aware quote math for the v3 trade instructions. Integer only.
//!
//! Layouts and math follow the official IDL (github.com/pump-fun/pump-public-docs,
//! idl/pump.json + idl/pump_fees.json, Oct 2026) and the reference quoting in
//! pump-rust-client 0.5.0 (`math/bonding_curve.rs`, `math/fees.rs`, MIT), restricted
//! to what this bot trades: SOL-quoted curves, no Token-2022 transfer fee on the
//! quote, and no buy that crosses into the synthetic migration.

use crate::types::{Result, TOKEN_DECIMALS};
use solana_sdk::pubkey::Pubkey;

/// Fixed-point scale for `price()`: lamports per whole token × 1e9.
pub const PRICE_SCALE: u128 = 1_000_000_000;

pub const BONDING_CURVE_DISCRIMINATOR: [u8; 8] = [0x17, 0xb7, 0xf8, 0x37, 0x60, 0xd8, 0xac, 0x60];
pub const GLOBAL_DISCRIMINATOR: [u8; 8] = [0xa7, 0xe8, 0xe8, 0xb1, 0xc8, 0x6c, 0x72, 0x7f];
pub const FEE_CONFIG_DISCRIMINATOR: [u8; 8] = [0x8f, 0x34, 0x92, 0xbb, 0xdb, 0x7b, 0x4c, 0x9b];

/// Current BondingCurve size (8 discriminator + fields). Older curves are shorter;
/// like the official decoder, missing trailing fields read as zero.
#[cfg(test)]
pub const BONDING_CURVE_LEN: usize = 166;
const BONDING_CURVE_MIN_LEN: usize = 49; // up to `complete`

const BPS: u128 = 10_000;

// ---------------------------------------------------------------- byte helpers

/// Zero-padded little-endian reader over an account buffer.
struct Reader<'a> {
    data: &'a [u8],
}

impl Reader<'_> {
    fn bytes<const N: usize>(&self, off: usize) -> [u8; N] {
        let mut out = [0u8; N];
        if off < self.data.len() {
            let end = (off + N).min(self.data.len());
            out[..end - off].copy_from_slice(&self.data[off..end]);
        }
        out
    }
    fn u8(&self, off: usize) -> u8 {
        self.bytes::<1>(off)[0]
    }
    fn bool(&self, off: usize) -> bool {
        self.u8(off) != 0
    }
    fn u64(&self, off: usize) -> u64 {
        u64::from_le_bytes(self.bytes(off))
    }
    fn u128(&self, off: usize) -> u128 {
        u128::from_le_bytes(self.bytes(off))
    }
    fn u32(&self, off: usize) -> u32 {
        u32::from_le_bytes(self.bytes(off))
    }
    fn pubkey(&self, off: usize) -> Pubkey {
        Pubkey::new_from_array(self.bytes(off))
    }
}

fn check_disc(data: &[u8], disc: &[u8; 8], what: &str) -> Result<()> {
    if data.len() < 8 || &data[..8] != disc {
        return Err(format!("{what}: bad account discriminator").into());
    }
    Ok(())
}

// ---------------------------------------------------------------- BondingCurve

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct BondingCurve {
    pub virtual_token_reserves: u64,
    /// `virtual_sol_reserves` before quote mints were introduced.
    pub virtual_quote_reserves: u64,
    pub real_token_reserves: u64,
    pub real_quote_reserves: u64,
    pub token_total_supply: u64,
    pub complete: bool,
    pub creator: Pubkey,
    pub is_mayhem_mode: bool,
    pub is_cashback_coin: bool,
    /// `Pubkey::default()` on legacy curves = SOL.
    pub quote_mint: Pubkey,
    pub creator_fee_bps: u64,
}

impl BondingCurve {
    pub fn parse(data: &[u8]) -> Result<Self> {
        if data.len() < BONDING_CURVE_MIN_LEN {
            return Err(format!("bonding curve data too short: {} bytes", data.len()).into());
        }
        check_disc(data, &BONDING_CURVE_DISCRIMINATOR, "bonding curve")?;
        let r = Reader { data };
        Ok(BondingCurve {
            virtual_token_reserves: r.u64(8),
            virtual_quote_reserves: r.u64(16),
            real_token_reserves: r.u64(24),
            real_quote_reserves: r.u64(32),
            token_total_supply: r.u64(40),
            complete: r.bool(48),
            creator: r.pubkey(49),
            is_mayhem_mode: r.bool(81),
            is_cashback_coin: r.bool(82),
            quote_mint: r.pubkey(83),
            creator_fee_bps: r.u64(115),
        })
    }

    pub fn is_sol_quoted(&self) -> bool {
        is_sol_like_quote_mint(&self.quote_mint)
    }

    pub fn has_liquidity(&self) -> bool {
        self.virtual_quote_reserves > 0 && self.virtual_token_reserves > 0 && self.real_token_reserves > 0
    }

    /// Spot price: lamports per whole token, scaled by `PRICE_SCALE`.
    pub fn price(&self) -> u64 {
        if self.virtual_token_reserves == 0 {
            return 0;
        }
        let token_unit = 10u128.pow(TOKEN_DECIMALS);
        let p = self.virtual_quote_reserves as u128 * token_unit * PRICE_SCALE / self.virtual_token_reserves as u128;
        p.min(u64::MAX as u128) as u64
    }

    /// Curve state after a buy that put `net_in` lamports on the curve (fees excluded).
    pub fn after_buy(&self, net_in: u64, tokens_out: u64) -> Self {
        BondingCurve {
            virtual_quote_reserves: self.virtual_quote_reserves.saturating_add(net_in),
            virtual_token_reserves: self.virtual_token_reserves.saturating_sub(tokens_out),
            real_quote_reserves: self.real_quote_reserves.saturating_add(net_in),
            real_token_reserves: self.real_token_reserves.saturating_sub(tokens_out),
            ..*self
        }
    }
}

/// SOL-like quotes: zero key (legacy curves), wSOL, Token-2022 native mint.
pub fn is_sol_like_quote_mint(m: &Pubkey) -> bool {
    *m == Pubkey::default() || *m == crate::pda::WSOL_MINT || *m == crate::pda::WSOL_2022_MINT
}

// ---------------------------------------------------------------- Global

/// The `Global` fields that affect a v3 trade.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Global {
    pub creator_fee_configurable: bool,
    pub buyback_basis_points: u64,
    pub buyback_fee_recipients: Vec<Pubkey>,
}

impl Global {
    pub fn parse(data: &[u8]) -> Result<Self> {
        check_disc(data, &GLOBAL_DISCRIMINATOR, "global")?;
        let r = Reader { data };
        let recipients: Vec<Pubkey> =
            (0..8).map(|i| r.pubkey(741 + 32 * i)).filter(|p| *p != Pubkey::default()).collect();
        Ok(Global {
            buyback_fee_recipients: recipients,
            buyback_basis_points: r.u64(997),
            creator_fee_configurable: r.bool(1045),
        })
    }
}

// ---------------------------------------------------------------- FeeConfig

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

/// pump-fees `FeeConfig` (PDA ["fee_config", pump program] under the fees program).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FeeConfig {
    pub flat_fees: Fees,
    pub fee_tiers: Vec<FeeTier>,
}

impl FeeConfig {
    pub fn parse(data: &[u8]) -> Result<Self> {
        check_disc(data, &FEE_CONFIG_DISCRIMINATOR, "fee config")?;
        let r = Reader { data };
        let fees = |o: usize| Fees { lp_fee_bps: r.u64(o), protocol_fee_bps: r.u64(o + 8), creator_fee_bps: r.u64(o + 16) };
        let mut off = 8 + 1 + 32; // discriminator, bump, admin
        let flat_fees = fees(off);
        off += 24;
        let n = r.u32(off) as usize;
        off += 4;
        if n > 64 || off + n * 40 > data.len() {
            return Err(format!("fee config: implausible fee_tiers length {n}").into());
        }
        let fee_tiers = (0..n)
            .map(|i| {
                let o = off + i * 40;
                FeeTier { market_cap_lamports_threshold: r.u128(o), fees: fees(o + 16) }
            })
            .collect();
        Ok(FeeConfig { flat_fees, fee_tiers })
    }

    /// Highest tier with threshold ≤ market cap, else the first tier.
    fn tier(&self, market_cap: u128) -> Result<&Fees> {
        let first = self.fee_tiers.first().ok_or("fee config: empty fee tiers")?;
        if market_cap < first.market_cap_lamports_threshold {
            return Ok(&first.fees);
        }
        Ok(self
            .fee_tiers
            .iter()
            .rev()
            .find(|t| market_cap >= t.market_cap_lamports_threshold)
            .map_or(&first.fees, |t| &t.fees))
    }
}

// ---------------------------------------------------------------- quoting

/// `[protocol_bps, creator_bps]` charged on this curve right now.
pub fn curve_fee_bps(global: &Global, fc: &FeeConfig, curve: &BondingCurve, mint_supply: u64) -> Result<[u64; 2]> {
    if curve.virtual_token_reserves == 0 {
        return Err("curve has no virtual token reserves".into());
    }
    let supply = if curve.is_mayhem_mode { curve.token_total_supply } else { mint_supply };
    let market_cap = curve.virtual_quote_reserves as u128 * supply as u128 / curve.virtual_token_reserves as u128;
    let fees = fc.tier(market_cap)?;
    let creator = if curve.creator == Pubkey::default() {
        0
    } else if global.creator_fee_configurable && curve.creator_fee_bps != 0 {
        curve.creator_fee_bps
    } else {
        fees.creator_fee_bps
    };
    Ok([fees.protocol_fee_bps, creator])
}

fn fee_amount(amount: u128, bps: u64) -> u128 {
    (amount * bps as u128).div_ceil(BPS)
}

/// floor(amount * out / (in + amount))
fn swap_exact_input(amount: u128, in_reserve: u128, out_reserve: u128) -> u128 {
    amount * out_reserve / (in_reserve + amount)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BuyQuote {
    /// Tokens the program will send for this budget.
    pub tokens_out: u64,
    /// Lamports that land on the curve (price-moving part).
    pub net_in: u64,
    /// Protocol + creator fee.
    pub fees: u64,
    /// Total lamports debited from the wallet (≤ budget).
    pub debit: u64,
}

/// `buy_exact_quote_in_v3` spending `sol_in` lamports (pump `exact_quote_in_fees`):
/// net = floor(in·10⁴/(10⁴+Σbps)), each fee = ceil(net·bps/10⁴), excess shaved off
/// net, tokens = swap_exact_input(net − 1). A budget that would buy past the curve's
/// remaining supply is refused rather than quoted through the synthetic migration.
pub fn buy_exact_sol_in(global: &Global, fc: &FeeConfig, curve: &BondingCurve, mint_supply: u64, sol_in: u64) -> Result<BuyQuote> {
    if curve.complete {
        return Err("bonding curve complete".into());
    }
    if curve.virtual_quote_reserves == 0 || sol_in == 0 {
        return Err("nothing to quote".into());
    }
    let [p, c] = curve_fee_bps(global, fc, curve, mint_supply)?;
    let input = sol_in as u128;
    let mut net = input * BPS / (BPS + p as u128 + c as u128);
    let (fp, fcr) = (fee_amount(net, p), fee_amount(net, c));
    if net + fp + fcr > input {
        net = net.saturating_sub(net + fp + fcr - input);
    }
    if net <= 1 {
        return Err("buy amount too small to cover fees".into());
    }
    let tokens = swap_exact_input(net - 1, curve.virtual_quote_reserves as u128, curve.virtual_token_reserves as u128);
    if tokens == 0 {
        return Err("quote returned 0 tokens".into());
    }
    if tokens > curve.real_token_reserves as u128 {
        return Err("buy would complete the curve (synthetic migration not supported)".into());
    }
    Ok(BuyQuote { tokens_out: tokens as u64, net_in: net as u64, fees: (fp + fcr) as u64, debit: (net + fp + fcr) as u64 })
}

/// Lamports received for selling `tokens` (v2/v3): curve output minus protocol and creator fee.
pub fn sell_sol_out(global: &Global, fc: &FeeConfig, curve: &BondingCurve, mint_supply: u64, tokens: u64) -> Result<u64> {
    if tokens == 0 || curve.virtual_token_reserves == 0 {
        return Ok(0);
    }
    let gross = swap_exact_input(tokens as u128, curve.virtual_token_reserves as u128, curve.virtual_quote_reserves as u128);
    let [p, c] = curve_fee_bps(global, fc, curve, mint_supply)?;
    let fee = fee_amount(gross, p) + fee_amount(gross, c);
    Ok(gross.saturating_sub(fee) as u64)
}

/// out × (100 − pct) / 100
pub fn apply_slippage(expected_out: u64, slippage_pct: u64) -> u64 {
    let pct = slippage_pct.min(100) as u128;
    (expected_out as u128 * (100 - pct) / 100) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Fresh pump.fun curve with a creator.
    pub fn fresh() -> BondingCurve {
        BondingCurve {
            virtual_token_reserves: 1_073_000_000_000_000,
            virtual_quote_reserves: 30_000_000_000,
            real_token_reserves: 793_100_000_000_000,
            real_quote_reserves: 0,
            token_total_supply: 1_000_000_000_000_000,
            creator: Pubkey::new_from_array([7; 32]),
            ..Default::default()
        }
    }

    /// Mainnet values on 2026-11-09: one tier, protocol 95 bps, creator 30 bps, buyback 50%.
    pub fn mainnet_fees() -> (Global, FeeConfig) {
        let fees = Fees { lp_fee_bps: 0, protocol_fee_bps: 95, creator_fee_bps: 30 };
        (
            Global { creator_fee_configurable: true, buyback_basis_points: 5_000, buyback_fee_recipients: vec![Pubkey::new_unique()] },
            FeeConfig { flat_fees: fees, fee_tiers: vec![FeeTier { market_cap_lamports_threshold: 0, fees }] },
        )
    }

    fn encode(c: &BondingCurve) -> Vec<u8> {
        let mut d = vec![0u8; BONDING_CURVE_LEN];
        d[..8].copy_from_slice(&BONDING_CURVE_DISCRIMINATOR);
        for (i, v) in [c.virtual_token_reserves, c.virtual_quote_reserves, c.real_token_reserves, c.real_quote_reserves, c.token_total_supply]
            .iter()
            .enumerate()
        {
            d[8 + 8 * i..16 + 8 * i].copy_from_slice(&v.to_le_bytes());
        }
        d[48] = c.complete as u8;
        d[49..81].copy_from_slice(c.creator.as_ref());
        d[81] = c.is_mayhem_mode as u8;
        d[82] = c.is_cashback_coin as u8;
        d[83..115].copy_from_slice(c.quote_mint.as_ref());
        d[115..123].copy_from_slice(&c.creator_fee_bps.to_le_bytes());
        d
    }

    #[test]
    fn parse_full_and_short_layouts() {
        let c = BondingCurve { is_cashback_coin: true, quote_mint: crate::pda::WSOL_MINT, creator_fee_bps: 42, ..fresh() };
        let data = encode(&c);
        assert_eq!(BondingCurve::parse(&data).unwrap(), c);
        // A legacy 81-byte curve: quote mint reads as default (= SOL).
        let old = BondingCurve::parse(&data[..81]).unwrap();
        assert_eq!(old.quote_mint, Pubkey::default());
        assert!(old.is_sol_quoted() && !old.is_cashback_coin);
        assert!(BondingCurve::parse(&data[..40]).is_err());
        let mut bad = data.clone();
        bad[0] ^= 1;
        assert!(BondingCurve::parse(&bad).is_err());
    }

    #[test]
    fn buy_quote_is_fee_aware_and_fits_budget() {
        let (g, fc) = mainnet_fees();
        let q = buy_exact_sol_in(&g, &fc, &fresh(), 1_000_000_000_000_000, 1_000_000_000).unwrap();
        assert!(q.debit <= 1_000_000_000);
        // net ≈ 1e9 / 1.0125
        assert_eq!(q.net_in, 987_654_320);
        assert_eq!(q.fees, (987_654_320u128 * 95).div_ceil(10_000) as u64 + (987_654_320u128 * 30).div_ceil(10_000) as u64);
        let no_fee = swap_exact_input(1_000_000_000 - 1, 30_000_000_000, 1_073_000_000_000_000) as u64;
        assert!(q.tokens_out < no_fee);
    }

    #[test]
    fn no_creator_means_no_creator_fee() {
        let (g, fc) = mainnet_fees();
        let c = BondingCurve { creator: Pubkey::default(), ..fresh() };
        assert_eq!(curve_fee_bps(&g, &fc, &c, 1).unwrap(), [95, 0]);
        let custom = BondingCurve { creator_fee_bps: 10, ..fresh() };
        assert_eq!(curve_fee_bps(&g, &fc, &custom, 1).unwrap(), [95, 10]);
    }

    #[test]
    fn fee_tier_selection() {
        let f = |p| Fees { lp_fee_bps: 0, protocol_fee_bps: p, creator_fee_bps: 0 };
        let fc = FeeConfig {
            flat_fees: f(1),
            fee_tiers: vec![
                FeeTier { market_cap_lamports_threshold: 100, fees: f(10) },
                FeeTier { market_cap_lamports_threshold: 1_000, fees: f(20) },
            ],
        };
        assert_eq!(fc.tier(5).unwrap().protocol_fee_bps, 10);
        assert_eq!(fc.tier(999).unwrap().protocol_fee_bps, 10);
        assert_eq!(fc.tier(1_000).unwrap().protocol_fee_bps, 20);
    }

    #[test]
    fn refuses_buy_past_remaining_supply() {
        let (g, fc) = mainnet_fees();
        let c = BondingCurve { real_token_reserves: 1_000, ..fresh() };
        assert!(buy_exact_sol_in(&g, &fc, &c, 1_000_000_000_000_000, 1_000_000_000).is_err());
    }

    #[test]
    fn sell_after_buy_loses_roughly_two_fees() {
        let (g, fc) = mainnet_fees();
        let c = fresh();
        let q = buy_exact_sol_in(&g, &fc, &c, 1_000_000_000_000_000, 500_000_000).unwrap();
        let back = sell_sol_out(&g, &fc, &c.after_buy(q.net_in, q.tokens_out), 1_000_000_000_000_000, q.tokens_out).unwrap();
        // ~1.25% in + ~1.25% out
        assert!(back < 500_000_000 && back > 485_000_000, "back={back}");
    }

    #[test]
    fn price_and_slippage() {
        let c = fresh();
        assert_eq!(c.price(), 27_958_993_476);
        assert!(c.after_buy(1_000_000_000, 30_000_000_000_000).price() > c.price());
        assert_eq!(apply_slippage(1_000, 5), 950);
        assert_eq!(apply_slippage(1_000, 100), 0);
    }
}
