"""Общие фикстуры тестов связки SOL × HL (поток profiles). Все значения синтетические: это не лимиты владельца, не
его адреса и не его ключи. Ключи Ed25519 — векторы RFC 8032 §7.1 (TEST 1, TEST 2), secp256k1 — скаляры 5, 6, 7."""
import copy
from pathlib import Path
from funding_bot.trade import instruments as I

DATA = Path(__file__).resolve().parent / "data" / "sol_hl"
ANSEM = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"

# RFC 8032 §7.1 — независимые контрольные значения (не вычислены тестируемым кодом)
SEED1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUB1 = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIG1_EMPTY = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065"
                           "224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
SEED2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
PUB2 = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
SIG2_72 = bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                        "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
SOL_ADDR = I.b58encode(PUB1)
SOL_SECRET_B58 = I.b58encode(SEED1 + PUB1)
# формы секрета, которых не должно быть ни в одном тексте ошибки, repr, логе
SECRET_FORMS = (SEED1.hex(), (SEED1 + PUB1).hex(), SOL_SECRET_B58, I.b58encode(SEED1))

HL_AGENT_N, HL_USER_N, OTHER_N = 5, 6, 7


def fake_key(n: int) -> str:
    return "0x" + f"{n:064x}"


def evm_addr(n: int) -> str:
    from eth_account import Account
    return Account.from_key(bytes.fromhex(f"{n:064x}")).address


def _q(s: str) -> str:
    return '"' + s + '"'


def base_sections() -> dict:
    """Полный owner.toml связки (синтетика тестов; числа — предложения плана, не решения владельца)."""
    return {
        "": {"schema_version": "2", "mode": _q("live")},
        "telegram": {"owner_id": "42"},
        "profiles.sol_best_hyperliquid": {
            "enabled": "true", "mode": _q("live"), "spot_chain": _q("solana-mainnet"), "spot_policy": _q("auto"),
            "perp_venue": _q("hyperliquid"), "perp_network": _q("mainnet"), "perp_dex": _q("para"),
            "instrument_registry": _q("instruments.json"), "allowed_instruments": '["ansem_sol_para_v1"]',
            "max_active_execution_clips": "1"},
        "wallets.sol_hl": {
            "solana_address": _q(SOL_ADDR), "hl_user_address": _q(evm_addr(HL_USER_N)),
            "hl_account_address": _q(evm_addr(HL_USER_N)), "hl_agent_address": _q(evm_addr(HL_AGENT_N)),
            "hl_vault_address": '""', "position_ownership_policy": _q("exclusive_market")},
        "spot.solana": {
            "expected_genesis_hash": _q(I.SOLANA_MAINNET_GENESIS), "transaction_versions": '["legacy", "v0"]',
            "allowed_mint_extensions": '["metadataPointer", "tokenMetadata"]',
            "allowed_token_account_extensions": '["immutableOwner"]', "confirmation_trigger": _q("finalized"),
            "require_finalized_before_next_clip": "true", "rollback_handler_required": "true",
            "direct_landing": _q("rpc"), "allow_tx_jup_landing": "false", "allow_separate_tip_transaction": "false"},
        "routing.solana": {
            "paths": '["jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6"]',
            "entry_metric": _q("min_effective_spot_buy_cost"), "exit_metric": _q("max_net_quote_proceeds"),
            "require_both_providers_for_entry": "true", "allow_degraded_entry": "false",
            "allow_single_provider_risk_reducing_exit": "true", "allow_external_signer_managed_routes": "false",
            "require_persisted_recovery_identity": "true", "max_collection_rounds": "2",
            "max_inflight_logical_swaps": "1"},
        "providers.jupiter": {"api_base": _q("https://api.jup.ag/swap/v2"), "integrator_fee_enabled": "false",
                              "main_rps": "1"},
        "providers.okx": {"api_base": _q("https://web3.okx.com/api/v6/dex/aggregator"), "chain_index": _q("501"),
                          "swap_mode": _q("exactIn"), "referral_fee_enabled": "false", "rps": "1"},
        "perp.hyperliquid": {"api_base": _q("https://api.hyperliquid.xyz"), "ws_url": _q("wss://api.hyperliquid.xyz/ws"),
                             "supported_account_modes": '["manual", "standard"]', "margin_mode": _q("isolated"),
                             "builder_fee_enabled": "false", "leverage": "1"},
        "limits.sol_best_hyperliquid": {
            "max_clip_usdc": _q("30"), "max_operation_usdc": _q("30"), "max_total_position_usdc": _q("30"),
            "max_surplus_tokens": _q("20"), "max_surplus_usdc": _q("3.5"), "max_delta_tokens": _q("75"),
            "max_delta_usdc": _q("12"), "max_unhedged_ms": "60000", "max_spot_slippage_bps": "100",
            "max_price_impact_bps": "150", "max_hedge_slippage_bps": "60", "min_entry_basis_all_in_bps": _q("-150"),
            "normal_exit_policy": _q("command"), "max_network_fee_lamports_per_tx": "200000",
            "max_tip_lamports_per_tx": "0", "max_external_fees_usdc_per_operation": _q("0.5"),
            "min_sol_reserve_lamports": "20000000", "max_rent_locked_lamports": "5000000",
            "min_hl_margin_reserve_usdc": _q("20"), "min_liquidation_distance_bps": "5000",
            "max_dust_tokens": _q("1"), "max_dust_usdc": _q("0.25"), "max_quote_age_ms": "5000",
            "max_book_age_ms": "2000", "max_source_skew_ms": "2000", "max_native_price_age_ms": "60000",
            "max_metadata_age_ms": "600000", "collection_deadline_ms": "3000", "min_blockhash_validity_heights": "60",
            "max_hedge_attempts": "3", "hedge_deadline_ms": "20000", "hl_action_expires_after_ms": "30000"},
        "emergency.sol_best_hyperliquid": {
            "auto_unwind_spot_after_proven_zero_hedge": "false", "auto_rebuy_spot_after_proven_zero_close": "false",
            "auto_sell_surplus_after_resolved_hedge": "false", "auto_correct_known_delta": "false"},
        "observability.sol_best_hyperliquid": {
            "record_candidate_rejections": "true", "record_source_times": "true",
            "record_sanitized_payload_hashes": "true", "expose_signed_payloads": "false",
            "emit_state_transition_events": "true"},
    }


def sol_toml(over: dict | None = None, drop: tuple = ()) -> str:
    """over: {секция: {ключ: литерал TOML | None (убрать ключ)}}; drop — убрать секции целиком."""
    secs = copy.deepcopy(base_sections())
    for sec, kv in (over or {}).items():
        dst = secs.setdefault(sec, {})
        for k, v in kv.items():
            if v is None:
                dst.pop(k, None)
            else:
                dst[k] = v
    lines = [f"{k} = {v}" for k, v in secs.pop("").items()]
    for sec, kv in secs.items():
        if sec in drop:
            continue
        lines.append(f"\n[{sec}]")
        lines += [f"{k} = {v}" for k, v in kv.items()]
    return "\n".join(lines) + "\n"


def write(tmp_path, text: str, name: str = "owner.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class SpyEnv(dict):
    """Окружение, которое запоминает любое обращение."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.touched = []

    def __getitem__(self, k):
        self.touched.append(("get", k)); return super().__getitem__(k)

    def get(self, k, d=None):
        self.touched.append(("get", k)); return super().get(k, d)

    def pop(self, k, *d):
        self.touched.append(("pop", k)); return super().pop(k, *d)

    def __contains__(self, k):
        self.touched.append(("in", k)); return super().__contains__(k)

    def keys(self):
        self.touched.append(("keys", None)); return super().keys()

    def items(self):
        self.touched.append(("items", None)); return super().items()

    def names(self) -> set:
        return {k for _, k in self.touched if k}
