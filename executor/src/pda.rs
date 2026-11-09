//! Program IDs and PDA / ATA derivation.

use solana_sdk::instruction::{AccountMeta, Instruction};
use solana_sdk::pubkey;
use solana_sdk::pubkey::Pubkey;

pub const PUMP_PROGRAM: Pubkey = pubkey!("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P");
pub const PUMP_GLOBAL: Pubkey = pubkey!("4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf");
pub const PUMP_EVENT_AUTH: Pubkey = pubkey!("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1");
/// pump-fees program and its `FeeConfig` PDA for the pump program (seeds ["fee_config", pump]).
pub const PUMP_FEES_PROGRAM: Pubkey = pubkey!("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ");
pub const PUMP_FEE_CONFIG: Pubkey = pubkey!("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt");
pub const WSOL_MINT: Pubkey = pubkey!("So11111111111111111111111111111111111111112");
pub const WSOL_2022_MINT: Pubkey = pubkey!("9pan9bMn5HatX4EJdBwg9VgCa7Uz5HL8N1m5D3NdXejP");

pub const SYSTEM_PROGRAM: Pubkey = pubkey!("11111111111111111111111111111111");
pub const TOKEN_PROGRAM: Pubkey = pubkey!("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
pub const TOKEN_2022_PROGRAM: Pubkey = pubkey!("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
pub const ATA_PROGRAM: Pubkey = pubkey!("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL");
pub const COMPUTE_BUDGET_PROGRAM: Pubkey = pubkey!("ComputeBudget111111111111111111111111111111");

/// bonding curve PDA: seeds ["bonding-curve", mint].
pub fn bonding_curve(mint: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"bonding-curve", mint.as_ref()], &PUMP_PROGRAM).0
}

/// ["user_volume_accumulator", user] under pump — passed to every v3 trade.
pub fn user_volume_accumulator(user: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[b"user_volume_accumulator", user.as_ref()], &PUMP_PROGRAM).0
}

/// Associated token account: seeds [owner, token_program, mint] under the ATA program.
pub fn ata(owner: &Pubkey, mint: &Pubkey, token_program: &Pubkey) -> Pubkey {
    Pubkey::find_program_address(&[owner.as_ref(), token_program.as_ref(), mint.as_ref()], &ATA_PROGRAM).0
}

/// `CreateIdempotent` (instruction index 1) — succeeds even if the ATA was
/// created between our existence check and landing.
pub fn create_ata_idempotent_ix(payer: &Pubkey, owner: &Pubkey, mint: &Pubkey, token_program: &Pubkey) -> Instruction {
    Instruction {
        program_id: ATA_PROGRAM,
        accounts: vec![
            AccountMeta::new(*payer, true),
            AccountMeta::new(ata(owner, mint, token_program), false),
            AccountMeta::new_readonly(*owner, false),
            AccountMeta::new_readonly(*mint, false),
            AccountMeta::new_readonly(SYSTEM_PROGRAM, false),
            AccountMeta::new_readonly(*token_program, false),
        ],
        data: vec![1],
    }
}

/// All addresses a v3 trade of one SOL-quoted mint needs, derived once and reused.
/// For SOL curves the quote ATAs are passed but never touched (lamports move directly).
#[derive(Debug, Clone, Copy)]
pub struct MintAccounts {
    pub mint: Pubkey,
    pub token_program: Pubkey,
    pub bonding_curve: Pubkey,
    pub bonding_curve_ata: Pubkey,
    pub bonding_curve_quote_ata: Pubkey,
    pub user_ata: Pubkey,
    pub user_quote_ata: Pubkey,
    pub user_volume_accumulator: Pubkey,
}

impl MintAccounts {
    pub fn new(mint: Pubkey, user: &Pubkey, token_program: Pubkey) -> Self {
        let bonding_curve = bonding_curve(&mint);
        MintAccounts {
            mint,
            token_program,
            bonding_curve,
            bonding_curve_ata: ata(&bonding_curve, &mint, &token_program),
            bonding_curve_quote_ata: ata(&bonding_curve, &WSOL_MINT, &TOKEN_PROGRAM),
            user_ata: ata(user, &mint, &token_program),
            user_quote_ata: ata(user, &WSOL_MINT, &TOKEN_PROGRAM),
            user_volume_accumulator: user_volume_accumulator(user),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::str::FromStr;

    #[test]
    fn ata_derivation_properties() {
        let owner = Pubkey::from_str("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM").unwrap();
        let mint = Pubkey::from_str("So11111111111111111111111111111111111111112").unwrap();
        let derived = ata(&owner, &mint, &TOKEN_PROGRAM);
        // Determinism + off-curve property (a valid PDA is never on the ed25519 curve).
        assert_eq!(derived, ata(&owner, &mint, &TOKEN_PROGRAM));
        assert!(!derived.is_on_curve());
        assert_ne!(derived, ata(&owner, &mint, &TOKEN_2022_PROGRAM));
    }

    /// Hard-coded addresses must equal their seed derivations (IDL `pda` entries).
    #[test]
    fn constants_match_seed_derivations() {
        let pda = |seeds: &[&[u8]], prog: &Pubkey| Pubkey::find_program_address(seeds, prog).0;
        assert_eq!(pda(&[b"global"], &PUMP_PROGRAM), PUMP_GLOBAL);
        assert_eq!(pda(&[b"__event_authority"], &PUMP_PROGRAM), PUMP_EVENT_AUTH);
        assert_eq!(pda(&[b"fee_config", PUMP_PROGRAM.as_ref()], &PUMP_FEES_PROGRAM), PUMP_FEE_CONFIG);
    }

    #[test]
    fn bonding_curve_is_pda() {
        let mint = Pubkey::new_unique();
        assert!(!bonding_curve(&mint).is_on_curve());
    }
}
