"""owner.toml схемы 2: связка sol_best_hyperliquid (ТЗ SOL×HL, CONNECTION §2.1). Приёмка: M01 и M02 (уровень
owner), часть V03/H09 (конфиг). Старый файл сверяется со снимком поведения НЕИЗМЕНЁННОГО кода (legacy_snapshot.json,
снят до правок потока profiles): те же значения, та же замороженная копия, те же тексты отказов."""
import json
from decimal import Decimal as D
from pathlib import Path
import pytest
from funding_bot.trade import instruments as I, keys, owner
from sol_hl_fixtures import DATA, evm_addr, fake_key, sol_toml, write

ROOT = Path(__file__).resolve().parents[1]
SNAP = json.loads((DATA / "legacy_snapshot.json").read_text(encoding="utf-8"))
SOL, LEG = owner.SOL_HL, owner.LEGACY_PROFILE
# случаи снимка, которые были «неизвестный ключ» до схемы 2 и теперь понятны
CHANGED = {
    "[limits.sol_best_hyperliquid]\nmax_clip_usdc = \"30\"\n": "schema_version = 2",
    "[profiles]\nx = 1\n": "неизвестный ключ profiles.x",
    "[perp.hyperliquid]\napi_base = \"https://api.hyperliquid.xyz\"\n": "schema_version = 2",
    "[wallets.sol_hl]\nsolana_address = \"\"\n": "schema_version = 2",
    "schema_version = 2\n": None,
}


def _tz_fixed() -> str:
    """Пример ТЗ с существующими именами OKX (okxdex.py): так его и надо поправить в пакете ТЗ."""
    t = (DATA / "owner.sol-hl.example.toml").read_text(encoding="utf-8")
    return t.replace('"OKX_DEX_API_SECRET"', '"OKX_DEX_SECRET"').replace('"OKX_DEX_API_PASSPHRASE"', '"OKX_DEX_PASSPHRASE"')


# ================================ старая связка — как раньше (M01) ================================
@pytest.mark.parametrize("case", range(len(SNAP["owner"]["ok"])))
def test_legacy_owner_files_unchanged(tmp_path, case):
    c = SNAP["owner"]["ok"][case]
    cfg = owner.load(ROOT / "deploy" / "owner.toml.example" if c["text"] is None else write(tmp_path, c["text"]))
    assert cfg.frozen()["values"] == c["values"]                       # копия сделки — байт-в-байт прежняя
    assert cfg.mode == c["mode"] and cfg.unresolved_auto() == c["unresolved_auto"]
    assert cfg.live_missing("aster") == c["live_missing"]
    assert cfg.live_missing("hyperliquid", "bsc") == c["live_missing_hl"]
    assert set(cfg.frozen()["values"]) == set(owner.LEGACY_SCHEMA) == set(c["keys"])
    assert owner.OwnerCfg.from_frozen(cfg.frozen_json()).values == cfg.values
    # старая связка включена без новых ключей; новая — выключена и ничего не требует от старой
    assert cfg.profile_enabled(LEG) and not cfg.profile_enabled(SOL)
    assert cfg.profile_mode(LEG) == cfg.mode and cfg.profile_live_missing(LEG) == cfg.live_missing("aster", "bsc")
    assert cfg.profile_mode(SOL) in ("dry", "readonly") and cfg.schema_version == 1


@pytest.mark.parametrize("case", range(len(SNAP["owner"]["bad"])))
def test_legacy_owner_refusals_unchanged(tmp_path, case):
    c = SNAP["owner"]["bad"][case]
    p = write(tmp_path, c["text"])
    if c["text"] in CHANGED:
        want = CHANGED[c["text"]]
        if want is None:
            assert owner.load(p).schema_version == 2
        else:
            with pytest.raises(owner.OwnerConfigError, match=want):
                owner.load(p)
        return
    with pytest.raises(owner.OwnerConfigError) as ei:
        owner.load(p)
    assert str(ei.value).replace(str(p), "<path>") == c["err"]


def test_schema_keys_only_grew():
    assert set(SNAP["schema_keys"]) == set(owner.LEGACY_SCHEMA) and set(owner.LEGACY_SCHEMA) < set(owner.SCHEMA)
    assert owner.V2_KEYS == set(owner.SCHEMA) - set(owner.LEGACY_SCHEMA)


# ================================ пример ТЗ ================================
def test_tz_example_requires_existing_okx_env_names(tmp_path):
    with pytest.raises(owner.OwnerConfigError) as ei:
        owner.load(DATA / "owner.sol-hl.example.toml")
    msg = str(ei.value)
    assert "providers.okx.api_secret_env" in msg and "OKX_DEX_SECRET" in msg and "okxdex.py" in msg
    assert "providers.okx.api_passphrase_env" in msg
    cfg = owner.load(write(tmp_path, _tz_fixed()))
    assert cfg.schema_version == 2 and not cfg.profile_enabled(SOL)
    assert cfg.profile_mode(SOL) == "dry"                              # общий mode пуст → dry
    assert cfg.get("routing.solana.paths") == ("jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6")
    assert cfg.get("profiles.sol_best_hyperliquid.allowed_instruments") == ("ansem_sol_para_v1",)
    assert cfg.env_name("providers.okx.api_secret_env") == "OKX_DEX_SECRET"
    assert cfg.env_name("spot.solana.rpc_primary_env") == "SOLANA_RPC_URL"
    assert cfg.env_name("spot.solana.secret_b58_env") == "SOLANA_SECRET_B58"      # не задано — имя по умолчанию
    miss = cfg.profile_live_missing(SOL)
    for k in ("telegram.owner_id", "wallets.sol_hl.solana_address", "wallets.sol_hl.hl_agent_address",
              "spot.solana.expected_genesis_hash", "perp.hyperliquid.leverage", "providers.jupiter.main_rps",
              "providers.okx.rps", "limits.sol_best_hyperliquid.max_clip_usdc",
              "limits.sol_best_hyperliquid.normal_exit_policy", "limits.sol_best_hyperliquid.max_dust_usdc"):
        assert k in miss, k
    # M02: связка Solana не требует ничего от Aster/BSC
    assert not [k for k in miss if k.startswith(("wallets.aster", "wallets.bsc", "dex.", "exec.", "perp.aster",
                                                 "limits.deal_", "limits.daily"))]
    for k in ("wallets.sol_hl.hl_vault_address", "limits.sol_best_hyperliquid.min_route_improvement_usdc",
              "providers.jupiter.execute_rps", "providers.okx.fee_policy_version", "spot.solana.rpc_primary_env"):
        assert k not in miss, k
    bl = " | ".join(cfg.profile_live_blockers(SOL))
    assert "выключена" in bl and "режим dry" in bl and "не задано" in bl
    with pytest.raises(owner.OwnerMissing):
        cfg.require_profile_live(SOL)
    with pytest.raises(owner.OwnerMissing) as ei:
        cfg.profile_limit(SOL, "max_clip_usdc")                        # нет значения = запрещено
    assert ei.value.key == "limits.sol_best_hyperliquid.max_clip_usdc"
    assert owner.OwnerCfg.from_frozen(cfg.frozen_json()).values == cfg.values


# ================================ полная связка ================================
def test_full_config_is_live_ready_and_typed(tmp_path):
    cfg = owner.load(write(tmp_path, sol_toml()))
    assert cfg.profile_live_missing(SOL) == [] and cfg.profile_unsupported(SOL) == []
    assert cfg.profile_live_blockers(SOL) == [] and cfg.profile_mode(SOL) == "live"
    cfg.require_profile_live(SOL)
    lim = lambda n: cfg.profile_limit(SOL, n)                          # noqa: E731
    assert lim("max_clip_usdc") == D("30") and isinstance(lim("max_clip_usdc"), D)
    assert lim("min_entry_basis_all_in_bps") == D("-150") and lim("max_surplus_usdc") == D("3.5")
    assert lim("max_spot_slippage_bps") == D(100) and isinstance(lim("max_spot_slippage_bps"), D)
    assert lim("max_tip_lamports_per_tx") == 0 and type(lim("min_sol_reserve_lamports")) is int
    assert cfg.get("wallets.sol_hl.solana_address") == I.b58encode(bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"))          # регистр как есть
    assert cfg.get("wallets.sol_hl.hl_vault_address") is None
    fj = cfg.frozen_json()
    assert '"limits.sol_best_hyperliquid.max_clip_usdc":"30"' in fj                 # деньги строкой, без float
    back = owner.OwnerCfg.from_frozen(fj)
    assert back.values == cfg.values and isinstance(back.profile_limit(SOL, "max_clip_usdc"), D)
    # старая связка в этом файле не описана — по-старому считается включённой; выключается явно (SOL-only, M02)
    assert cfg.profile_enabled(LEG)
    only = owner.load(write(tmp_path, sol_toml({"profiles.bsc_okx_aster": {"enabled": "false"}})))
    assert not only.profile_enabled(LEG) and "выключена" in " ".join(only.profile_live_blockers(LEG))
    assert only.profile_live_blockers(SOL) == []


def test_mode_is_the_lower_of_file_and_profile(tmp_path):
    def mode(top, prof, enabled="true"):
        return owner.load(write(tmp_path, sol_toml({"": {"mode": f'"{top}"'}, "profiles.sol_best_hyperliquid": {
            "mode": f'"{prof}"', "enabled": enabled}}))).profile_mode(SOL)
    assert mode("live", "live") == "live" and mode("readonly", "live") == "readonly"
    assert mode("live", "readonly") == "readonly" and mode("dry", "live") == "dry"
    assert mode("live", "live", "false") == "readonly"                 # выключенная — не выше readonly


def test_requirements_follow_routing_and_policies(tmp_path):
    jup_only = owner.load(write(tmp_path, sol_toml({"routing.solana": {"paths": '["jupiter_build_v2"]'}},
                                                   drop=("providers.okx",))))
    assert jup_only.profile_live_missing(SOL) == []
    okx_only = owner.load(write(tmp_path, sol_toml({"routing.solana": {"paths": '["okx_solana_v6"]'}},
                                                   drop=("providers.jupiter",))))
    assert okx_only.profile_live_missing(SOL) == []
    basis = owner.load(write(tmp_path, sol_toml({"limits.sol_best_hyperliquid": {"normal_exit_policy": '"basis"'}})))
    assert basis.profile_live_missing(SOL) == ["limits.sol_best_hyperliquid.normal_exit_basis_bps",
                                               "limits.sol_best_hyperliquid.min_exit_pnl_usdc"]
    auto = owner.load(write(tmp_path, sol_toml({"emergency.sol_best_hyperliquid": {"auto_correct_known_delta": "true"}})))
    assert auto.profile_live_missing(SOL) == [f"emergency.sol_best_hyperliquid.{k}" for k in (
        "max_loss_usdc", "max_hedge_slippage_bps", "max_unwind_slippage_bps", "deadline_ms")]
    empty = owner.load(write(tmp_path, sol_toml({"limits.sol_best_hyperliquid": {"max_dust_usdc": '""'}})))
    assert empty.profile_live_missing(SOL) == ["limits.sol_best_hyperliquid.max_dust_usdc"]


@pytest.mark.parametrize("over, needle", [
    ({"limits.sol_best_hyperliquid": {"max_clip_usd": '"30"'}}, "неизвестный ключ limits.sol_best_hyperliquid.max_clip_usd"),
    ({"profiles.foo": {"enabled": "true"}}, "неизвестный ключ profiles.foo"),
    ({"spot.solana": {"rpc_url": '"x"'}}, "неизвестный ключ spot.solana.rpc_url"),
    ({"perp.hyperliquid": {"levrage": "1"}}, "неизвестный ключ perp.hyperliquid.levrage"),
    ({"limits.sol_best_hyperliquid": {"max_clip_usdc": "30"}}, "строкой Decimal"),
    ({"limits.sol_best_hyperliquid": {"max_clip_usdc": '"1e3"'}}, "строкой"),
    ({"limits.sol_best_hyperliquid": {"max_clip_usdc": '"-1"'}}, "max_clip_usdc"),
    ({"limits.sol_best_hyperliquid": {"max_clip_usdc": '"31"'}}, "больше max_operation_usdc"),
    ({"limits.sol_best_hyperliquid": {"max_total_position_usdc": '"20"', "max_operation_usdc": '"25"',
                                      "max_clip_usdc": '"20"'}}, "больше max_total_position_usdc"),
    ({"limits.sol_best_hyperliquid": {"max_spot_slippage_bps": "10000"}}, "< 10000"),
    ({"limits.sol_best_hyperliquid": {"max_spot_slippage_bps": '"50"'}}, "без кавычек"),
    ({"limits.sol_best_hyperliquid": {"max_unhedged_ms": "1.5"}}, "целое"),
    ({"limits.sol_best_hyperliquid": {"max_dust_tokens": '"NaN"'}}, "max_dust_tokens"),
    ({"limits.sol_best_hyperliquid": {"normal_exit_policy": '"auto"'}}, "command | basis"),
    ({"profiles.sol_best_hyperliquid": {"max_active_execution_clips": "2"}}, "max_active_execution_clips"),
    ({"profiles.sol_best_hyperliquid": {"spot_chain": '"solana-devnet"'}}, "spot_chain"),
    ({"profiles.sol_best_hyperliquid": {"allowed_instruments": '["ANSEM"]'}}, "allowed_instruments"),
    ({"profiles.sol_best_hyperliquid": {"instrument_registry": '"../instruments.json"'}}, "без каталогов"),
    ({"routing.solana": {"max_collection_rounds": "3"}}, "max_collection_rounds"),
    ({"routing.solana": {"max_inflight_logical_swaps": "2"}}, "max_inflight_logical_swaps"),
    ({"routing.solana": {"paths": '["jupiter_ultra"]'}}, "jupiter_ultra"),
    ({"routing.solana": {"paths": '["okx_solana_v6", "okx_solana_v6"]'}}, "повтор"),
    ({"spot.solana": {"transaction_versions": '["v1"]'}}, "v1"),
    ({"spot.solana": {"allowed_mint_extensions": '["transferHook"]'}}, "не поддержано первым выпуском"),
    ({"spot.solana": {"allowed_token_account_extensions": '["cpiGuard"]'}}, "не поддержано первым выпуском"),
    ({"spot.solana": {"expected_genesis_hash": f'"{I.TOKEN_PROGRAM}"'}}, "не genesis"),
    ({"spot.solana": {"confirmation_trigger": '"confirmed"', "rollback_handler_required": "false"}}, "отката"),
    ({"spot.solana": {"rpc_secondary_env": '"SOLANA_RPC_URL"'}}, "то же имя"),
    ({"spot.solana": {"keypair_file_env": '"DEX_EVM_KEY"'}}, "занято другой ролью"),
    ({"perp.hyperliquid": {"agent_key_env": '"OKX_DEX_SECRET"'}}, "занято другой ролью"),
    ({"spot.solana": {"rpc_primary_env": '"solana_rpc"'}}, "имя переменной"),
    ({"wallets.sol_hl": {"solana_address": '"0' + I.TOKEN_PROGRAM[1:] + '"'}}, "base58"),
    ({"wallets.sol_hl": {"hl_agent_address": f'"{evm_addr(6)}"'}}, "мастер-ключ"),
    ({"wallets.sol_hl": {"hl_account_address": f'"{evm_addr(7)}"'}}, "vaultAddress"),
    ({"wallets.sol_hl": {"hl_vault_address": f'"{evm_addr(7)}"'}}, "hl_vault_address ≠ hl_account_address"),
    ({"wallets.sol_hl": {"hl_user_address": '"0xe57bFE9F44b819898F47BF37E5AF72a0783e1141"'}}, "EIP-55"),
    ({"providers.okx": {"chain_index": '"56"'}}, "501"),
    ({"providers.okx": {"api_key_env": '"MY_OKX_KEY"'}}, "OKX_DEX_API_KEY"),
    ({"providers.jupiter": {"api_base": '"http://api.jup.ag"'}}, "https"),
    ({"perp.hyperliquid": {"margin_mode": '"cross"'}}, "isolated"),
    ({"perp.hyperliquid": {"margin_type": '"CROSSED"'}}, "CROSSED против"),
    ({"perp.hyperliquid": {"supported_account_modes": '["dexAbstraction"]'}}, "dexAbstraction"),
    ({"": {"schema_version": "3"}}, "schema_version"),
    ({"": {"schema_version": None}}, "требует schema_version = 2"),
])
def test_sol_schema_is_strict(tmp_path, over, needle):
    with pytest.raises(owner.OwnerConfigError) as ei:
        owner.load(write(tmp_path, sol_toml(over)))
    assert needle in str(ei.value)


def test_secret_pasted_into_address_is_not_echoed(tmp_path):
    secret = I.b58encode(bytes(range(64)))
    with pytest.raises(owner.OwnerConfigError) as ei:
        owner.load(write(tmp_path, sol_toml({"wallets.sol_hl": {"solana_address": f'"{secret}"'}})))
    assert secret not in str(ei.value) and "86 симв" in str(ei.value)


def test_unsupported_features_load_but_block_live(tmp_path):
    cfg = owner.load(write(tmp_path, sol_toml({
        "spot.solana": {"allow_tx_jup_landing": "true", "allow_separate_tip_transaction": "true",
                        "confirmation_trigger": '"confirmed"', "require_finalized_before_next_clip": "false"},
        "routing.solana": {"allow_external_signer_managed_routes": "true", "require_persisted_recovery_identity": "false"},
        "providers.jupiter": {"integrator_fee_enabled": "true"}, "providers.okx": {"referral_fee_enabled": "true"},
        "perp.hyperliquid": {"builder_fee_enabled": "true", "supported_account_modes": '["standard", "unified"]'},
        "observability.sol_best_hyperliquid": {"expose_signed_payloads": "true"}})))
    un = " | ".join(cfg.profile_unsupported(SOL))
    for k in ("allow_tx_jup_landing", "allow_separate_tip_transaction", "confirmation_trigger",
              "require_finalized_before_next_clip", "allow_external_signer_managed_routes",
              "require_persisted_recovery_identity", "integrator_fee_enabled", "referral_fee_enabled",
              "builder_fee_enabled", "expose_signed_payloads", "unified"):
        assert k in un, k
    assert cfg.profile_live_missing(SOL) == []
    with pytest.raises(owner.OwnerUnsupported) as ei:
        cfg.require_profile_live(SOL)
    assert "live запрещён" in str(ei.value)
    assert cfg.profile_unsupported(LEG) == []


# ================================ секрет в поле owner.toml (PF-2, M08) ================================
UUID = "3f2a9c1e-7b4d-4e8a-9c21-5d6e7f8a9b0c"               # форма ключа Jupiter / OKX api_key / Helius
OKX_SEC = "22582BD0CFF14C41EDBF1AB98506286D"                # форма секрета OKX: 32 hex верхним регистром


class _Rec:
    def __init__(self):
        self.sent = []

    def send(self, chat, text, **kw):
        self.sent.append(text)
        return True


def _tg_text(err) -> str:
    """Что уйдёт владельцу в Telegram при битом owner.toml: Bot._command → views.owner_config_error (без redact)."""
    from types import SimpleNamespace
    from funding_bot.tg.bot import Bot
    rec = _Rec()
    Bot._command(SimpleNamespace(sender=rec, mode="dry"), SimpleNamespace(text="позиции", chat_id=1), err)
    assert len(rec.sent) == 1
    return rec.sent[0]


@pytest.mark.parametrize("sec, key, lit, secret", [
    ("providers.jupiter", "api_key_env", f'"{UUID}"', UUID),
    ("providers.okx", "api_key_env", f'"{UUID}"', UUID),
    ("providers.okx", "api_secret_env", f'"{OKX_SEC}"', OKX_SEC),
    ("providers.okx", "api_passphrase_env", '"MyOkxPassphrase9"', "MyOkxPassphrase9"),
    ("spot.solana", "rpc_primary_env", '"https://x.example/' + "k" * 20 + '"', "k" * 20),
    # формат имени [A-Z][A-Z0-9_]* пропускал секрет с буквы: сплошной верхний hex и длинное без «_» — отказ
    ("spot.solana", "secret_b58_env", f'"A{OKX_SEC}"', f"A{OKX_SEC}"),
    ("perp.hyperliquid", "agent_key_env", '"JBSWY3DPEHPK3PXPJBSWY3DP"', "JBSWY3DPEHPK3PXPJBSWY3DP"),
    ("wallets.sol_hl", "hl_agent_address", f'"{fake_key(0xBEEF)}"', fake_key(0xBEEF)[2:]),
    ("wallets.sol_hl", "hl_user_address", f'"{UUID}"', UUID),
    ("providers.jupiter", "api_base", f'"https://api.jup.ag/swap/v2?api-key={UUID}"', UUID),
    ("profiles.sol_best_hyperliquid", "allowed_instruments", f'["{UUID}"]', UUID),
    ("profiles.sol_best_hyperliquid", "spot_policy", f'"{UUID}"', UUID),                  # enum
    ("routing.solana", "allow_degraded_entry", f'"{OKX_SEC}"', OKX_SEC),                   # bool
    ("limits.sol_best_hyperliquid", "max_unhedged_ms", f'"{OKX_SEC}"', OKX_SEC),           # целое
    ("limits.sol_best_hyperliquid", "max_clip_usdc", f'"{UUID}"', UUID),                   # деньги строкой
])
def test_secret_in_new_field_is_not_echoed_anywhere(tmp_path, sec, key, lit, secret):
    with pytest.raises(owner.OwnerConfigError) as ei:
        owner.load(write(tmp_path, sol_toml({sec: {key: lit}})))
    msg = str(ei.value)
    assert f"{sec}.{key}" in msg and "симв." in msg and secret not in msg, msg
    tg = _tg_text(ei.value)
    assert f"{sec}.{key}" in tg and secret not in tg


def test_typo_words_are_still_shown_and_real_names_pass(tmp_path):
    """Короткая опечатка в enum/числе/списке из словаря видна владельцу; обычные имена переменных принимаются."""
    for over, needle in (({"perp.hyperliquid": {"margin_mode": '"cross"'}}, "'cross'"),
                         ({"limits.sol_best_hyperliquid": {"max_spot_slippage_bps": '"50"'}}, "'50'"),
                         ({"spot.solana": {"transaction_versions": '["v1"]'}}, "'v1'")):
        with pytest.raises(owner.OwnerConfigError) as ei:
            owner.load(write(tmp_path, sol_toml(over)))
        assert needle in str(ei.value)
    cfg = owner.load(write(tmp_path, sol_toml({"spot.solana": {"rpc_primary_env": '"HELIUS_RPC_URL"'},
                                               "providers.jupiter": {"api_key_env": '"JUP_KEY_2"'}})))
    assert cfg.env_name("spot.solana.rpc_primary_env") == "HELIUS_RPC_URL"
    assert cfg.env_name("providers.jupiter.api_key_env") == "JUP_KEY_2"


def test_legacy_private_key_in_address_is_masked_everywhere(tmp_path):
    """Старое поле wallets.bsc печатало значение целиком ({v!r}); теперь — длиной (правило старых ключей, тексты снимка
    те же), и в TG тоже. Второй слой — маскировка в самом исключении: 64-hex не проходит ни из какой ветки."""
    pk = fake_key(0xBEEF)
    for val in (pk, pk[2:]):
        with pytest.raises(owner.OwnerConfigError) as ei:
            owner.load(write(tmp_path, f'[wallets]\nbsc = "{val}"\n'))
        msg = str(ei.value)
        assert "wallets.bsc" in msg and "40 hex" in msg and f"{len(val)} симв." in msg and pk[2:] not in msg
        assert pk[2:] not in _tg_text(ei.value)
    e = owner.OwnerConfigError(f"wallets.bsc = {pk!r}: нужен адрес")   # любая будущая ветка с сырым значением
    assert pk[2:] not in str(e) and "<hex64>" in str(e) and pk[2:] not in _tg_text(e)
    assert keys.redact(str(e)) == str(e)                               # маскировка уже внутри
