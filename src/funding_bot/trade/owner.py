"""Строгий загрузчик runtime/owner.toml — параметров владельца фазы 2 (trade_spec §3).

Правило: пусто = запрещено. Ни одно денежное значение не придумывается кодом: чего владелец не задал, того нет,
и live-команда отказывает, называя недостающий ключ; dry-run всё равно строит план и перечисляет, что
заблокировало бы live.

Почему строго:
- неизвестный ключ или секция — ошибка, а не «проигнорировать»: опечатка `slipage_pct` иначе оставила бы
  настоящий ключ пустым молча, и владелец думал бы, что его 3 % действуют;
- число только числом TOML (без кавычек), дробные разбираются сразу в Decimal (parse_float) — float в
  деньгах не бывает; bool — только true/false (в Python bool — подкласс int, 1 не должно стать «да»);
- адрес со смешанным регистром проверяется по контрольной сумме EIP-55 (опечатка в одной букве кошелька).

Файл перечитывается на КАЖДУЮ команду (load() без кэша), а сделка хранит замороженную JSON-копию
(frozen_json) — отчёт и сверка потом видят именно те лимиты, с которыми сделка шла.

TOML не допускает «ключ =» без значения, поэтому пусто пишется как "" (или ключ отсутствует).

Схема 2 (ТЗ SOL×HL 13.09) — связка «спот Solana × перп Hyperliquid» профилем sol_best_hyperliquid:
  [profiles.sol_best_hyperliquid] [wallets.sol_hl] [spot.solana] [routing.solana] [providers.jupiter]
  [providers.okx] [perp.hyperliquid] (+ ключи связки) [limits.…] [emergency.…] [observability.…].
- старый файл (BSC/OKX + Aster) читается как раньше, те же значения и та же замороженная копия: новые ключи в копию
  не пишутся, пока пусты; [profiles.bsc_okx_aster] enabled не задан = старая связка включена (как было всегда);
- деньги новых секций — строкой Decimal («"30"»), число TOML там — ошибка; бп/мс/лампорты — числами;
- пустой лимит связки = live запрещён (profile_live_missing), никаких значений по умолчанию;
- то, чего первый выпуск не умеет (tx.jup.ag, внешний подписант Jupiter, шорт по confirmed, Unified-счёт HL…),
  загружается, но блокирует live с причиной (profile_unsupported) — это не «поддерживается»;
- значение поля схемы 2 в тексте отказа не печатается — только вид и длина (в api_key_env легко вставить сам ключ, в
  адрес — приватный ключ); короткое слово-опечатку в enum/bool/числе/списке из словаря показываем. Имя переменной,
  похожее на сам секрет (сплошной верхний hex), — отказ. Любой текст OwnerConfigError проходит keys.redact (M08).
"""
from __future__ import annotations
import hashlib, json, re, time, tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from .. import config
from .instruments import MINT_EXTENSIONS_V1, ACCOUNT_EXTENSIONS_V1, SOLANA_MAINNET, NETWORKS, is_sol_address

MODES = ("dry", "readonly", "live")


class OwnerConfigError(ValueError):
    """owner.toml не читается или значение не того вида. Команда отклоняется (и в dry тоже: план по битому
    файлу показал бы не те лимиты).

    Текст маскируется keys.redact при создании: он уходит и в лог, и в Telegram (views.owner_config_error своей
    маскировки не имеет), а в поле файла мог попасть ключ — например 0x+64hex в старом wallets.bsc."""

    def __init__(self, msg: str = ""):
        from .keys import redact                  # keys импортирует owner — здесь только при создании исключения
        super().__init__(redact(msg))


class OwnerMissing(Exception):
    """Нужный ключ пуст. key — первый из недостающих, keys — все (для одной строки отказа)."""

    def __init__(self, *keys: str):
        self.keys = tuple(keys)
        self.key = self.keys[0] if self.keys else ""
        super().__init__(*self.keys)

    def __str__(self) -> str:
        return "в owner.toml не задано: " + ", ".join(self.keys)


class OwnerUnsupported(Exception):
    """Профиль выключен, режим ниже live или выбрано то, чего первый выпуск не умеет. reasons — строки для владельца."""

    def __init__(self, *reasons: str):
        self.reasons = tuple(reasons)
        super().__init__(*self.reasons)

    def __str__(self) -> str:
        return "live запрещён: " + "; ".join(self.reasons)


@dataclass(frozen=True)
class _Spec:
    kind: str                         # num | int | enum | bool | addr | dec | sol | text | list
    lo: Decimal | None = None
    lo_open: bool = False             # True: строго больше lo
    hi: Decimal | None = None
    words: tuple[str, ...] = ()       # enum — допустимые значения; num — допустимые слова вместо числа; list — элементы
    hi_open: bool = False             # True: строго меньше hi
    rx: str | None = None             # text / list без words — формат строки
    hint: str = ""                    # подсказка к отказу (text / list)


_POS = dict(lo=Decimal(0), lo_open=True)          # > 0
_NONNEG = dict(lo=Decimal(0))                     # ≥ 0
_PCT = dict(lo=Decimal(0), lo_open=True, hi=Decimal(100))

_TOP = {"mode": _Spec("enum", words=MODES)}
_SECTIONS: dict[str, dict[str, _Spec]] = {
    "telegram": {"owner_id": _Spec("int", lo=Decimal(1))},
    "wallets": {"bsc": _Spec("addr"), "aster_user": _Spec("addr"), "aster_signer": _Spec("addr")},
    "limits": {
        "deal_max_usd_per_leg": _Spec("num", **_POS),
        "max_open_deals": _Spec("int", **_NONNEG),
        # "off" — явное решение владельца «дневного стопа нет» (12.09); пусто — ещё не решено = запрет live
        "daily_loss_stop_usd": _Spec("num", **_POS, words=("off",)),
        "daily_loss_basis": _Spec("enum", words=("realized_costs", "mtm")),
        "min_entry_funding_pct_h": _Spec("num"),        # знак любой: порог, а не размер
        "min_entry_basis_bps": _Spec("num"),
    },
    "dex": {
        "slippage_pct": _Spec("num", **_PCT),
        "clip_slippage_pct": _Spec("num", **_PCT),
        "impact_cap_pct": _Spec("num", **_PCT),
        "approve_policy": _Spec("enum", words=("exact", "unlimited")),
        "broadcast": _Spec("enum", words=("public", "okx_mev")),
        "allow_tax_tokens": _Spec("bool"),
        "native_reserve": _Spec("num", **_NONNEG),
    },
    # "auto" (владелец 12.09) — число подбирает бот по правилу из planner.py и показывает его в плане; для live
    # «auto» считается заданным. clip_max_usd — размер клипа выбирает оптимизатор; clips_max — без потолка n;
    # unhedged_usd_max — plan_cost_drift_pct % клипа; exec_time_max_s — 3 × ожидаемая длительность плана + 60 с
    "exec": {
        "clip_max_usd": _Spec("num", **_POS, words=("auto",)),
        "clips_max": _Spec("int", lo=Decimal(1), words=("auto",)),
        "unhedged_usd_max": _Spec("num", **_POS, words=("auto",)),
        "exec_time_max_s": _Spec("num", **_POS, words=("auto",)),
        "refill_wait_max_s": _Spec("num", **_NONNEG),
        "plan_cost_drift_pct": _Spec("num", **_NONNEG),
        "auto_unwind_naked_after_s": _Spec("num", **_POS),
    },
}
# Post-migration opt-ins.  They must not alter frozen copies of legacy owner files; an old executable must still read
# a deal frozen before resize/SL existed.  Empty extension keys are therefore omitted just like schema-2 keys.
_EXTENSIONS = {
    "resize": {"enabled": _Spec("bool"), "max_increase_usd_per_leg": _Spec("num", **_POS),
               "max_total_usd_per_leg": _Spec("num", **_POS)},
    "stop_loss": {"enabled": _Spec("bool"), "working_type": _Spec("enum", words=("MARK_PRICE", "CONTRACT_PRICE")),
                  "check_interval_s": _Spec("int", lo=Decimal(60))},
}
# [perp.<площадка>] — одна схема на площадку; площадки — только известные коллектору.
# "auto" (владелец 12.09): α/β подбираются под каждый токен по стакану на момент плана и замораживаются в нём;
# liq_alert_pct — тревога при расстоянии до ликвидации меньше половины расстояния на входе
_PERP: dict[str, _Spec] = {
    "leverage": _Spec("int", lo=Decimal(1)),
    "margin_type": _Spec("enum", words=("ISOLATED", "CROSSED")),
    "max_slip_bps": _Spec("num", **_POS, words=("auto",)),
    "touch_frac_max": _Spec("num", lo=Decimal(0), lo_open=True, hi=Decimal(1), words=("auto",)),
    "maker_allowed": _Spec("bool"),
    "liq_alert_pct": _Spec("num", **_PCT, words=("auto",)),
    # ревью 13.09, С1: контракты с множителем (1000BONKUSDT: 1 контракт = 1000 BONK). Пусто или false — отказ во входе,
    # добор и дохедж продажей; выход и откат таких сделок — всегда. Разрешает только владелец
    "allow_contract_multiplier": _Spec("bool"),
}
PERP_VENUES = tuple(config.PERP_VENUES)

# --- схема 2: связка «спот Solana × перп Hyperliquid» (ТЗ SOL×HL 13.09) ---------------------------------------------
SOL_HL = "sol_best_hyperliquid"
SOL_GATE = "sol_best_gate"
SOL_ASTER = "sol_best_aster"
SOL_PROFILES = (SOL_HL, SOL_GATE, SOL_ASTER)
SOL_PROFILE_VENUES = {SOL_HL: "hyperliquid", SOL_GATE: "gate", SOL_ASTER: "aster"}
LEGACY_PROFILE = "bsc_okx_aster"
RH_GATE = "rh_okx_gate"            # спот OKX DEX в сети Robinhood × перп Gate (сделка FATCOIN, владелец 13.09)
PROFILE_IDS = (LEGACY_PROFILE, *SOL_PROFILES, RH_GATE)
# EVM-связки прежнего движка (спот OKX DEX × перп): профиль ↔ (сеть спота, площадка перпа)
EVM_PROFILES = {LEGACY_PROFILE: ("bsc", "aster"), RH_GATE: ("robinhood", "gate")}
EVM_PROFILE_OF = {v: k for k, v in EVM_PROFILES.items()}
# Публичные подписи профилей. Это не перечень рынков коллектора, а единственный
# источник правды о реально разрешённых торговых связках для кабинета владельца.
PROFILE_LEGS = {
    LEGACY_PROFILE: ("OKX DEX · BSC", "Aster"),
    RH_GATE: ("OKX DEX · Robinhood", "Gate"),
    SOL_HL: ("Solana DEX · Jupiter / OKX", "Hyperliquid"),
    SOL_GATE: ("Solana DEX · Jupiter / OKX", "Gate"),
    SOL_ASTER: ("Solana DEX · Jupiter / OKX", "Aster"),
}


def profile_views(cfg) -> list[dict[str, str | bool]]:
    """Безопасная read-only проекция разрешений профилей для владельца.

    Не возвращает адреса, имена переменных или перечень пустых полей: кабинет
    должен отвечать на «какие биржи торгуются», а не раскрывать конфигурацию.
    """
    out = []
    for profile_id in PROFILE_IDS:
        spot, perp = PROFILE_LEGS[profile_id]
        enabled, mode = cfg.profile_enabled(profile_id), cfg.profile_mode(profile_id)
        blockers = cfg.profile_live_blockers(profile_id)
        live = enabled and mode == "live" and not blockers
        if live:
            state, reason = "live", "разрешена"
        elif not enabled:
            state, reason = "off", "выключена владельцем"
        elif mode != "live":
            state, reason = mode, f"режим {mode}"
        elif blockers:
            state, reason = "blocked", "не настроена для live"
        else:  # defensive: never label an unverified state as enabled for trading
            state, reason = "blocked", "статус не подтверждён"
        out.append(dict(id=profile_id, spot=spot, perp=perp, live=live, state=state, reason=reason))
    return out

# кошелёк EVM связки: старая — [wallets] bsc (как было); новые — подсекция схемы 2, чтобы замороженная копия
# настроек сделок BSC не изменилась ни на ключ
EVM_WALLET_KEY = {"bsc": "wallets.bsc", "robinhood": "wallets.rh_gate.evm_address"}
SOL_PATHS = ("jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6")
# ключ OKX общий с коллектором и BSC-трейдером: okxdex.py читает ровно эти имена — второй набор секретов не заводим
OKX_ENV = {"api_key_env": "OKX_DEX_API_KEY", "api_secret_env": "OKX_DEX_SECRET", "api_passphrase_env": "OKX_DEX_PASSPHRASE"}
# имена переменных окружения связки: пусто в owner.toml = это имя
SOL_HL_ENV_DEFAULTS = {
    "spot.solana.rpc_primary_env": "SOLANA_RPC_URL",
    "spot.solana.rpc_secondary_env": "SOLANA_RPC_SECONDARY_URL",
    "spot.solana.rpc_ws_env": "SOLANA_WS_URL",
    "spot.solana.keypair_file_env": "SOLANA_KEYPAIR_FILE",
    "spot.solana.secret_b58_env": "SOLANA_SECRET_B58",
    "providers.jupiter.api_key_env": "JUPITER_API_KEY",
    "perp.hyperliquid.agent_key_env": "HL_AGENT_PRIVATE_KEY",
    **{f"providers.okx.{k}": v for k, v in OKX_ENV.items()},
}
# имена секретов старой связки и прочих ролей — связке Solana их занимать нельзя (ключ одной роли в слоте другой)
_RESERVED_ENV = frozenset({"DEX_EVM_KEY", "ASTER_SIGNER_KEY", "ASTER_SIGNER_ADDRESS", "ASTER_USER", "DEX_EVM_ADDRESS",
                           "TG_BOT_TOKEN", "OKX_DEX_PROJECT"})
_ENV_RX = r"[A-Z][A-Z0-9_]{0,63}"
_MS = dict(lo=Decimal(0), lo_open=True)
_BPS_CAP = dict(lo=Decimal(0), lo_open=True, hi=Decimal(10000), hi_open=True)   # (0, 10000): 100 % — не допуск

_V1 = "не поддержано первым выпуском"
def _sol_profile(venue: str) -> dict[str, _Spec]:
    """Schema for one explicit Solana-spot/perp profile.

    The profile name is an owner-facing identity; routing is fixed by this
    schema and never inferred from a name prefix.
    """
    out = {
    "enabled": _Spec("bool"),
    "mode": _Spec("enum", words=MODES),
    "spot_chain": _Spec("enum", words=(SOLANA_MAINNET,)),
    "spot_policy": _Spec("enum", words=("auto", "jupiter", "okx")),
    "perp_venue": _Spec("enum", words=(venue,)),
    "instrument_registry": _Spec("text", rx=r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.json",
                                 hint="имя файла *.json в runtime/, без каталогов"),
    "allowed_instruments": _Spec("list", rx=r"[a-z0-9_]{1,64}", hint="instrument_id из instruments.json"),
    "max_active_execution_clips": _Spec("int", lo=Decimal(1), hi=Decimal(1)),       # один активный клип (§6)
    }
    if venue == "hyperliquid":
        out.update({
            "perp_network": _Spec("enum", words=("mainnet",)),
            "perp_dex": _Spec("text", rx=r"[a-z0-9]{1,16}",
                               hint="имя dex HIP-3 строчными, например para"),
        })
    return out


_PROFILE_SOL = _sol_profile("hyperliquid")
_WALLETS_SOL_HL = {
    "solana_address": _Spec("sol"),
    "hl_user_address": _Spec("addr"),         # мастер HL
    "hl_account_address": _Spec("addr"),      # счёт, на котором позиция (мастер или субаккаунт)
    "hl_agent_address": _Spec("addr"),        # API-кошелёк (агент): только подпись, чтения — по счёту
    "hl_vault_address": _Spec("addr"),        # субаккаунт при торговле за него; пусто — мастер
    "position_ownership_policy": _Spec("enum", words=("exclusive_market",)),
}
_WALLETS_SOLANA = {"solana_address": _Spec("sol")}
_SPOT_SOLANA = {
    "rpc_primary_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
    "rpc_secondary_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
    "rpc_ws_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
    "keypair_file_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
    "secret_b58_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
    "expected_genesis_hash": _Spec("sol"),
    "transaction_versions": _Spec("list", words=("legacy", "v0")),
    "allowed_mint_extensions": _Spec("list", words=MINT_EXTENSIONS_V1, hint=_V1),
    "allowed_token_account_extensions": _Spec("list", words=ACCOUNT_EXTENSIONS_V1, hint=_V1),
    "confirmation_trigger": _Spec("enum", words=("confirmed", "finalized")),
    "require_finalized_before_next_clip": _Spec("bool"),
    "rollback_handler_required": _Spec("bool"),
    "direct_landing": _Spec("enum", words=("rpc",)),
    "allow_tx_jup_landing": _Spec("bool"),
    "allow_separate_tip_transaction": _Spec("bool"),
}
_ROUTING_SOLANA = {
    "paths": _Spec("list", words=SOL_PATHS),
    "entry_metric": _Spec("enum", words=("min_effective_spot_buy_cost",)),
    "exit_metric": _Spec("enum", words=("max_net_quote_proceeds",)),
    "require_both_providers_for_entry": _Spec("bool"),
    "allow_degraded_entry": _Spec("bool"),
    "allow_single_provider_risk_reducing_exit": _Spec("bool"),
    "allow_external_signer_managed_routes": _Spec("bool"),
    "require_persisted_recovery_identity": _Spec("bool"),
    "max_collection_rounds": _Spec("int", lo=Decimal(1), hi=Decimal(2)),   # сбор + одна повторная сессия (§5.1 п.7)
    "max_inflight_logical_swaps": _Spec("int", lo=Decimal(1), hi=Decimal(1)),
}
_PROVIDERS = {
    "jupiter": {
        "api_base": _Spec("text", rx=r"https://[^\s?#]+", hint="https://…"),
        "api_key_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
        "integrator_fee_enabled": _Spec("bool"),
        "main_rps": _Spec("num", **_POS),
        "execute_rps": _Spec("num", **_POS),
    },
    "okx": {
        "api_base": _Spec("text", rx=r"https://[^\s?#]+", hint="https://…"),
        "chain_index": _Spec("text", rx=r"[0-9]{1,10}", hint="chainIndex OKX строкой, для Solana \"501\""),
        **{k: _Spec("text", rx=re.escape(v), hint=f"ключ OKX общий с коллектором: только {v} (okxdex.py)")
           for k, v in OKX_ENV.items()},
        "swap_mode": _Spec("enum", words=("exactIn",)),
        "referral_fee_enabled": _Spec("bool"),
        "rps": _Spec("num", **_POS),
        "fee_policy_version": _Spec("text", rx=r"[A-Za-z0-9_.:-]{1,64}", hint="короткая метка"),
    },
}
_PERP_EXTRA = {
    "hyperliquid": {
        "api_base": _Spec("text", rx=r"https://[^\s?#]+", hint="https://…"),
        "ws_url": _Spec("text", rx=r"wss://[^\s?#]+", hint="wss://…"),
        "agent_key_env": _Spec("text", rx=_ENV_RX, hint="имя переменной окружения"),
        # внутренние имена режимов; соответствие строкам userAbstraction — у адаптера HL (фикстура счёта владельца)
        "supported_account_modes": _Spec("list", words=("manual", "standard", "unified", "portfolio_margin")),
        "margin_mode": _Spec("enum", words=("isolated",)),                   # ANSEM: onlyIsolated / noCross
        "builder_fee_enabled": _Spec("bool"),
    },
}
_LIMITS_SOL = {
    # деньги — строкой Decimal
    "max_clip_usdc": _Spec("dec", **_POS),
    "max_operation_usdc": _Spec("dec", **_POS),
    "max_total_position_usdc": _Spec("dec", **_POS),
    "max_surplus_tokens": _Spec("dec", **_NONNEG),
    "max_surplus_usdc": _Spec("dec", **_NONNEG),
    "max_delta_tokens": _Spec("dec", **_NONNEG),
    "max_delta_usdc": _Spec("dec", **_NONNEG),
    "min_entry_basis_all_in_bps": _Spec("dec"),                              # знак любой: порог, не размер
    "normal_exit_policy": _Spec("enum", words=("command", "basis")),          # command — выход только по команде
    "normal_exit_basis_bps": _Spec("dec"),
    "min_exit_pnl_usdc": _Spec("dec"),
    "max_external_fees_usdc_per_operation": _Spec("dec", **_NONNEG),
    "min_hl_margin_reserve_usdc": _Spec("dec", **_NONNEG),
    "max_dust_tokens": _Spec("dec", **_NONNEG),
    "max_dust_usdc": _Spec("dec", **_NONNEG),
    "min_route_improvement_usdc": _Spec("dec", **_NONNEG),                   # только если гистерезис включён
    # б.п. — числом TOML (Decimal); 10000 = 100 % — не допуск
    "max_spot_slippage_bps": _Spec("num", **_BPS_CAP),
    "max_price_impact_bps": _Spec("num", **_BPS_CAP),
    "max_hedge_slippage_bps": _Spec("num", **_BPS_CAP),
    "min_liquidation_distance_bps": _Spec("num", **_BPS_CAP),
    # лампорты — целые
    "max_network_fee_lamports_per_tx": _Spec("int", **_MS),
    "max_tip_lamports_per_tx": _Spec("int", **_NONNEG),
    "min_sol_reserve_lamports": _Spec("int", **_NONNEG),
    "max_rent_locked_lamports": _Spec("int", **_NONNEG),
    # время — целые мс; счётчики
    "max_unhedged_ms": _Spec("int", **_MS),
    "max_quote_age_ms": _Spec("int", **_MS),
    "max_book_age_ms": _Spec("int", **_MS),
    "max_source_skew_ms": _Spec("int", **_MS),
    "max_native_price_age_ms": _Spec("int", **_MS),
    "max_metadata_age_ms": _Spec("int", **_MS),
    "collection_deadline_ms": _Spec("int", **_MS),
    "min_blockhash_validity_heights": _Spec("int", **_MS),
    "max_hedge_attempts": _Spec("int", lo=Decimal(1)),
    "hedge_deadline_ms": _Spec("int", **_MS),
    "hl_action_expires_after_ms": _Spec("int", **_MS),
}
_EMERGENCY_SOL = {
    "auto_unwind_spot_after_proven_zero_hedge": _Spec("bool"),
    "auto_rebuy_spot_after_proven_zero_close": _Spec("bool"),
    "auto_sell_surplus_after_resolved_hedge": _Spec("bool"),
    "auto_correct_known_delta": _Spec("bool"),
    "max_loss_usdc": _Spec("dec", **_POS),
    "max_hedge_slippage_bps": _Spec("num", **_BPS_CAP),
    "max_unwind_slippage_bps": _Spec("num", **_BPS_CAP),
    "deadline_ms": _Spec("int", **_MS),
}
_OBS_SOL = {k: _Spec("bool") for k in ("record_candidate_rejections", "record_source_times",
                                      "record_sanitized_payload_hashes", "expose_signed_payloads",
                                      "emit_state_transition_events")}
# [группа.имя] — подсекции схемы 2; группа может совпадать со старой секцией ([wallets] и [wallets.sol_hl])
_GROUPS: dict[str, dict[str, dict[str, _Spec]]] = {
    "profiles": {**{p: _sol_profile(v) for p, v in ((SOL_HL, "hyperliquid"), (SOL_GATE, "gate"),
                                                       (SOL_ASTER, "aster"))},
                 LEGACY_PROFILE: {"enabled": _Spec("bool")},
                 RH_GATE: {"enabled": _Spec("bool"), "mode": _Spec("enum", words=MODES)}},
    "wallets": {"sol_hl": _WALLETS_SOL_HL, "solana": _WALLETS_SOLANA,
                "rh_gate": {"evm_address": _Spec("addr")}},
    "spot": {"solana": _SPOT_SOLANA},
    "routing": {"solana": _ROUTING_SOLANA},
    "providers": _PROVIDERS,
    "limits": {p: _LIMITS_SOL for p in SOL_PROFILES},
    "emergency": {p: _EMERGENCY_SOL for p in SOL_PROFILES},
    "observability": {p: _OBS_SOL for p in SOL_PROFILES},
}
_TOP_V2 = {"schema_version": _Spec("int", lo=Decimal(1), hi=Decimal(2))}


def _schema() -> dict[str, _Spec]:
    out = dict(_TOP)
    for sec, keys in _SECTIONS.items():
        out.update({f"{sec}.{k}": s for k, s in keys.items()})
    for v in PERP_VENUES:
        out.update({f"perp.{v}.{k}": s for k, s in _PERP.items()})
    return out


def _schema_v2() -> dict[str, _Spec]:
    out = dict(_TOP_V2)
    for grp, subs in _GROUPS.items():
        for sub, keys in subs.items():
            out.update({f"{grp}.{sub}.{k}": s for k, s in keys.items()})
    for v, keys in _PERP_EXTRA.items():
        out.update({f"perp.{v}.{k}": s for k, s in keys.items()})
    return out


def _schema_extensions() -> dict[str, _Spec]:
    return {f"{sec}.{k}": s for sec, keys in _EXTENSIONS.items() for k, s in keys.items()}


LEGACY_SCHEMA: Mapping[str, _Spec] = MappingProxyType(_schema())
EXTENSION_KEYS = frozenset(_schema_extensions())
SOL_V2_KEYS = frozenset(_schema_v2())
# Kept as the public set of post-legacy keys for compatibility tests; extensions do not require schema_version = 2.
V2_KEYS = SOL_V2_KEYS | EXTENSION_KEYS
SCHEMA: Mapping[str, _Spec] = MappingProxyType({**_schema(), **_schema_v2(), **_schema_extensions()})

# Ключи, у которых пусто — осмысленное значение, а не запрет (live их не требует):
#   clip_slippage_pct пусто → шлётся slippage_pct; maker_allowed пусто → только тейкер IOC;
#   auto_unwind_naked_after_s пусто → никогда; пороги входа по фандингу/базису — справка в плане
#   (владелец 12.09: входы только по его команде); daily_loss_basis нужен, только если стоп задан числом;
#   allow_contract_multiplier пусто → контракты с множителем запрещены (как false).
OPTIONAL = frozenset({"mode", "dex.clip_slippage_pct", "exec.auto_unwind_naked_after_s",
                      "limits.min_entry_funding_pct_h", "limits.min_entry_basis_bps", "limits.daily_loss_basis",
                      *(f"perp.{v}.maker_allowed" for v in PERP_VENUES),
                      *(f"perp.{v}.allow_contract_multiplier" for v in PERP_VENUES)})
# Кошельки площадки перпа, без которых live не начинается (Aster v3: user = основной, signer = агент)
_VENUE_WALLETS = {"aster": ("wallets.aster_user", "wallets.aster_signer")}
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DEC_RE = re.compile(r"^-?[0-9]{1,30}(?:\.[0-9]{1,30})?$")


def _checksum_ok(addr: str) -> bool:
    body = addr[2:]
    if body == body.lower() or body == body.upper():
        return True                               # без смешанного регистра контрольной суммы нет
    try:
        from eth_utils import is_checksum_address  # идёт с eth-account (extra trade)
    except ImportError:                           # без extra: сравнения адресов всё равно в нижнем регистре
        return True
    return bool(is_checksum_address(addr))


def _num_text(spec: _Spec) -> str:
    rng = []
    if spec.lo is not None:
        rng.append(("> " if spec.lo_open else "≥ ") + format(spec.lo, "f"))
    if spec.hi is not None:
        rng.append(("< " if spec.hi_open else "≤ ") + format(spec.hi, "f"))
    words = f" или {' / '.join(repr(w) for w in spec.words)}" if spec.words else ""
    return ("целое" if spec.kind == "int" else "число") + (" " + ", ".join(rng) if rng else "") + words


_WORD_RE = re.compile(r"[\w.+-]{1,24}")          # короткое слово-опечатка: «cross», «v1», «50», «авто»
_HEXISH_RE = re.compile(r"[0-9A-Fa-f-]{12,}")    # …но не hex-токен (форма ключа API и секрета)
# имя переменной, похожее на сам секрет: формат [A-Z][A-Z0-9_]* пропускает секрет OKX (32 hex верхним регистром)
_ENV_SECRETISH = re.compile(r"[0-9A-F]{16,}|(?=[A-Z0-9]*[0-9])[A-Z0-9]{20,}")


def _shown(v: Any, key: str = "", word: bool = False) -> str:
    """Значение в тексте отказа. Старые ключи — как было: длинная строка только длиной (тексты отказов старого файла
    те же). Ключи схемы 2 — только вид и длина; word=True (enum, bool, число, список из словаря) — короткое слово."""
    if key in V2_KEYS:
        if isinstance(v, bool):
            return repr(v)
        if isinstance(v, (int, Decimal)):
            return str(v)
        if isinstance(v, str):
            if word and _WORD_RE.fullmatch(v) and not _HEXISH_RE.fullmatch(v):
                return repr(v)
            return f"<строка {len(v)} симв.>"
        if isinstance(v, (list, tuple)):
            return f"<список {len(v)} эл.>"
        return f"<{type(v).__name__}>"
    if isinstance(v, str) and len(v) > 50:
        return f"<строка {len(v)} симв.>"
    return repr(v)


def _in_range(d: Decimal, spec: _Spec) -> bool:
    if spec.lo is not None and (d <= spec.lo if spec.lo_open else d < spec.lo):
        return False
    if spec.hi is not None and (d >= spec.hi if spec.hi_open else d > spec.hi):
        return False
    return True


def _check(key: str, spec: _Spec, v: Any) -> Any:
    """Проверенное значение или None (пусто). Ошибка вида — OwnerConfigError с именем ключа."""
    if isinstance(v, str) and v.strip() == "":
        return None
    if spec.kind == "list" and isinstance(v, (list, tuple)) and not v:
        return None
    if spec.kind == "enum":
        if isinstance(v, str) and v in spec.words:
            return v
        raise OwnerConfigError(f"{key} = {_shown(v, key, word=True)}: допустимо {' | '.join(spec.words)} или \"\"")
    if spec.kind == "bool":
        if isinstance(v, bool):
            return v
        raise OwnerConfigError(f"{key} = {_shown(v, key, word=True)}: нужно true или false (без кавычек) или \"\"")
    if spec.kind == "addr":
        if isinstance(v, str) and _ADDR_RE.match(v.strip()):
            a = v.strip()
            if not _checksum_ok(a):
                raise OwnerConfigError(f"{key} = {a}: неверная контрольная сумма EIP-55 (опечатка в адресе?)")
            return a
        raise OwnerConfigError(f"{key} = {_shown(v, key)}: нужен адрес 0x + 40 hex-символов")
    if spec.kind == "sol":
        if is_sol_address(v):                     # строка как есть: регистр — часть адреса, пробелы — ошибка
            return v
        raise OwnerConfigError(f"{key} = {_shown(v, key)}: нужен адрес Solana — base58, 32 байта, регистр как есть")
    if spec.kind == "text":
        if isinstance(v, str) and re.fullmatch(spec.rx or r".+", v):
            if key.endswith("_env") and _ENV_SECRETISH.fullmatch(v):
                raise OwnerConfigError(f"{key} = {_shown(v, key)}: похоже на сам ключ, а не на имя переменной "
                                       "окружения — в owner.toml только имя, ключ — в .env")
            return v
        raise OwnerConfigError(f"{key} = {_shown(v, key)}: {spec.hint or 'неверный формат'}")
    if spec.kind == "list":
        if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
            raise OwnerConfigError(f"{key} = {_shown(v, key)}: нужен список строк [\"…\", …]")
        for x in v:
            if (x not in spec.words) if spec.words else not re.fullmatch(spec.rx or r".+", x):
                allowed = f" — допустимо: {', '.join(spec.words)}" if spec.words else ""
                raise OwnerConfigError(f"{key}: {_shown(x, key, word=bool(spec.words))}{allowed}"
                                       + (f" ({spec.hint})" if spec.hint else ""))
        if len(set(v)) != len(v):
            raise OwnerConfigError(f"{key}: повтор в списке")
        return tuple(v)
    if spec.kind == "dec":
        if isinstance(v, str) and _DEC_RE.match(v.strip()):
            d = Decimal(v.strip())
            if not _in_range(d, spec):
                raise OwnerConfigError(f"{key} = {v!r}: нужно {_num_text(spec)}")
            return d
        if isinstance(v, (int, Decimal)) and not isinstance(v, bool):
            raise OwnerConfigError(f"{key} = {v}: деньги — строкой Decimal в кавычках, например \"{v}\"")
        raise OwnerConfigError(f"{key} = {_shown(v, key)}: нужно десятичное число строкой, например \"30\"")
    # num / int
    if isinstance(v, str):
        if v.strip() in spec.words:
            return v.strip()
        raise OwnerConfigError(f"{key} = {_shown(v, key, word=True)}: нужно {_num_text(spec)} (число — без кавычек)")
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        raise OwnerConfigError(f"{key} = {_shown(v, key, word=True)}: нужно {_num_text(spec)}")
    if spec.kind == "int" and not isinstance(v, int):
        raise OwnerConfigError(f"{key} = {v}: нужно {_num_text(spec)}")
    d = Decimal(v)
    if not d.is_finite():
        raise OwnerConfigError(f"{key} = {v}: нужно конечное число")
    if not _in_range(d, spec):
        raise OwnerConfigError(f"{key} = {v}: нужно {_num_text(spec)}")
    return v if spec.kind == "int" else d


def _flatten(doc: dict) -> tuple[dict[str, Any], list[str]]:
    """Документ TOML → {ключ.через.точку: значение}; неизвестное — в список ошибок."""
    flat: dict[str, Any] = {}
    errs: list[str] = []
    for top, val in doc.items():
        if top in _TOP or top in _TOP_V2:
            flat[top] = val
        elif top in _SECTIONS or top in _GROUPS or top in _EXTENSIONS:
            if not isinstance(val, dict):
                errs.append(f"[{top}] должно быть секцией")
                continue
            for k, v in val.items():
                if k in _SECTIONS.get(top, {}) or k in _EXTENSIONS.get(top, {}):
                    flat[f"{top}.{k}"] = v
                elif k in _GROUPS.get(top, {}):
                    if not isinstance(v, dict):
                        errs.append(f"[{top}.{k}] должно быть секцией")
                        continue
                    for kk, vv in v.items():
                        if kk in _GROUPS[top][k]:
                            flat[f"{top}.{k}.{kk}"] = vv
                        else:
                            errs.append(f"неизвестный ключ {top}.{k}.{kk}")
                else:
                    errs.append(f"неизвестный ключ {top}.{k}")
        elif top == "perp":
            if not isinstance(val, dict):
                errs.append("[perp] должно быть секцией")
                continue
            for venue, sec in val.items():
                if venue not in PERP_VENUES or not isinstance(sec, dict):
                    errs.append(f"неизвестная площадка [perp.{venue}] (знаю: {', '.join(PERP_VENUES)})")
                    continue
                extra = _PERP_EXTRA.get(venue, {})
                for k, v in sec.items():
                    if k in _PERP or k in extra:
                        flat[f"perp.{venue}.{k}"] = v
                    else:
                        errs.append(f"неизвестный ключ perp.{venue}.{k}")
        else:
            errs.append(f"неизвестный ключ или секция: {top}")
    return flat, errs


def _validate(flat: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {k: None for k in SCHEMA}
    errs: list[str] = []
    for k, v in flat.items():
        try:
            values[k] = _check(k, SCHEMA[k], v)
        except OwnerConfigError as e:
            errs.append(str(e))
    # сквозные проверки: потолок проскальзывания владельца — жёсткий максимум; мастер ≠ агент
    clip, cap = values["dex.clip_slippage_pct"], values["dex.slippage_pct"]
    if isinstance(clip, Decimal) and isinstance(cap, Decimal) and clip > cap:
        errs.append(f"dex.clip_slippage_pct = {clip} больше dex.slippage_pct = {cap} (потолок владельца)")
    u, s = values["wallets.aster_user"], values["wallets.aster_signer"]
    if u and s and u.lower() == s.lower():
        errs.append("wallets.aster_signer совпадает с aster_user: подписант должен быть отдельным API-кошельком "
                    "(агентом), иначе на сервере лежал бы мастер-ключ")
    _validate_v2(flat, values, errs)
    return values, errs


def _validate_v2(flat: dict[str, Any], values: dict[str, Any], errs: list[str]) -> None:
    """Сквозные проверки схемы 2. Работают только по заданным ключам: пустое ловит profile_live_missing."""
    used = [k for k in flat if k in SOL_V2_KEYS and k != "schema_version"]
    if used and flat.get("schema_version") != 2:
        errs.append(f"ключ {used[0]} (связка Solana × Hyperliquid) требует schema_version = 2 в начале файла")
    lim = f"limits.{SOL_HL}."
    a, b, c = (values[lim + k] for k in ("max_clip_usdc", "max_operation_usdc", "max_total_position_usdc"))
    if a is not None and b is not None and a > b:
        errs.append(f"{lim}max_clip_usdc = {a} больше max_operation_usdc = {b}")
    if b is not None and c is not None and b > c:
        errs.append(f"{lim}max_operation_usdc = {b} больше max_total_position_usdc = {c}")
    w = "wallets.sol_hl."
    user, acct, agent, vault = (values[w + k] for k in ("hl_user_address", "hl_account_address", "hl_agent_address",
                                                        "hl_vault_address"))
    low = lambda x: x.lower() if x else None     # noqa: E731 — EVM-адреса HL сравниваются без регистра
    if agent and low(agent) in {low(user), low(acct)} - {None}:
        errs.append(f"{w}hl_agent_address совпадает с мастером/счётом: агент — отдельный API-кошелёк, иначе на "
                    "сервере лежал бы мастер-ключ")
    if vault and acct and low(vault) != low(acct):
        errs.append(f"{w}hl_vault_address ≠ hl_account_address: за субаккаунт подписываем тем же адресом, что читаем")
    if vault and user and low(vault) == low(user):
        errs.append(f"{w}hl_vault_address = мастеру: для мастера vault пустой")
    if acct and user and not vault and low(acct) != low(user):
        errs.append(f"{w}hl_account_address ≠ hl_user_address без hl_vault_address: торговля за субаккаунт требует "
                    "vaultAddress")
    g = values["spot.solana.expected_genesis_hash"]
    if g is not None and g != NETWORKS[SOLANA_MAINNET].genesis_hash:
        errs.append(f"spot.solana.expected_genesis_hash = {g}: не genesis {SOLANA_MAINNET} "
                    f"({NETWORKS[SOLANA_MAINNET].genesis_hash})")
    if values["spot.solana.confirmation_trigger"] == "confirmed" and values["spot.solana.rollback_handler_required"] is False:
        errs.append("spot.solana.confirmation_trigger = \"confirmed\" без rollback_handler_required: хедж по confirmed "
                    "только с обработкой отката (ТЗ §9.3)")
    ci = values["providers.okx.chain_index"]
    if ci is not None:
        from .tconfig import chain_index
        if ci != chain_index("solana"):
            errs.append(f"providers.okx.chain_index = {ci!r}: для Solana у OKX \"{chain_index('solana')}\"")
    mt, mm = values["perp.hyperliquid.margin_type"], values["perp.hyperliquid.margin_mode"]
    if mt == "CROSSED" and mm == "isolated":
        errs.append("perp.hyperliquid.margin_type = CROSSED против margin_mode = isolated")
    # имена переменных окружения: у каждой роли своё, чужие секреты не занимать (ключ одной роли в слоте другой)
    names: dict[str, str] = {}
    for key, default in SOL_HL_ENV_DEFAULTS.items():
        n = values[key] or default
        if key.startswith("providers.okx."):
            continue
        if n in _RESERVED_ENV or n in OKX_ENV.values():
            errs.append(f"{key} = {n!r}: это имя занято другой ролью")
        elif n in names:
            errs.append(f"{key} = {n!r}: то же имя, что у {names[n]} (основной и резервный RPC, ключ и файл — разные "
                        "переменные)")
        else:
            names[n] = key


def _jsonable(v: Any) -> Any:
    return format(v, "f") if isinstance(v, Decimal) else v


@dataclass(frozen=True)
class OwnerCfg:
    """Снимок owner.toml на момент команды. values — ВСЕ известные ключи (None = пусто)."""
    values: Mapping[str, Any]
    path: str
    sha256: str | None                # None — файла нет (всё пусто, режим dry)
    loaded: float

    # --- доступ ---
    def _known(self, key: str) -> None:
        if key not in self.values:     # опечатка в коде — ошибка программиста, а не «пусто»
            raise KeyError(f"нет такого ключа owner.toml: {key}")

    def get(self, key: str, default: Any = None) -> Any:
        self._known(key)
        v = self.values[key]
        return default if v is None else v

    def is_set(self, key: str) -> bool:
        self._known(key)
        return self.values[key] is not None

    def missing(self, *keys: str) -> list[str]:
        return [k for k in keys if not self.is_set(k)]

    def require(self, *keys: str) -> tuple:
        """Значения ключей по порядку; хоть один пуст — OwnerMissing со всеми пустыми."""
        miss = self.missing(*keys)
        if miss:
            raise OwnerMissing(*miss)
        return tuple(self.values[k] for k in keys)

    @property
    def mode(self) -> str:
        return self.values.get("mode") or "dry"

    @property
    def owner_id(self) -> int | None:
        return self.values.get("telegram.owner_id")

    # --- что нужно для live ---
    def live_required(self, perp_venue: str = "aster", chain: str = "bsc") -> tuple[str, ...]:
        """Ключи, без которых live-вход/выход отказывает. dry-run показывает live_missing() в плане."""
        if perp_venue not in PERP_VENUES:
            raise KeyError(f"неизвестная площадка перпа: {perp_venue}")
        wallet = EVM_WALLET_KEY.get(chain, f"wallets.{chain}")
        self._known(wallet)
        keys = ["telegram.owner_id", wallet, *_VENUE_WALLETS.get(perp_venue, ()),
                "limits.deal_max_usd_per_leg", "limits.daily_loss_stop_usd",   # max_open_deals — не обязателен
                # (владелец 13.09: «количество сделок устанавливаю я»): нет ключа — лимита числа сделок нет
                "dex.slippage_pct", "dex.impact_cap_pct", "dex.approve_policy", "dex.broadcast",
                "dex.allow_tax_tokens", "dex.native_reserve",
                *(f"perp.{perp_venue}.{k}" for k in _PERP if f"perp.{perp_venue}.{k}" not in OPTIONAL),
                "exec.clip_max_usd", "exec.clips_max", "exec.unhedged_usd_max", "exec.exec_time_max_s",
                "exec.refill_wait_max_s", "exec.plan_cost_drift_pct"]
        if isinstance(self.values.get("limits.daily_loss_stop_usd"), Decimal):
            keys.append("limits.daily_loss_basis")     # стоп задан числом — нужно и его определение
        return tuple(keys)

    def unresolved_auto(self) -> list[str]:
        """«auto», которое нечем разрешить, — для live это то же, что пусто. unhedged_usd_max = "auto" считается как
        plan_cost_drift_pct % клипа: без допуска владельца числа нет (в dry план строится без этого предела)."""
        out = []
        if self.values.get("exec.unhedged_usd_max") == "auto" and self.values.get("exec.plan_cost_drift_pct") is None:
            out.append("exec.unhedged_usd_max")
        return out

    def live_missing(self, perp_venue: str = "aster", chain: str = "bsc") -> list[str]:
        """Пустые ключи из live_required плюс неразрешимые «auto»; порядок — как в live_required."""
        req = self.live_required(perp_venue, chain)
        bad = set(self.missing(*req)) | set(self.unresolved_auto())
        return [k for k in req if k in bad]

    def require_live(self, perp_venue: str = "aster", chain: str = "bsc") -> None:
        """Отказ live, если чего-то не хватает (пусто или неразрешимое «auto»): OwnerMissing со всеми ключами."""
        miss = self.live_missing(perp_venue, chain)
        if miss:
            raise OwnerMissing(*miss)

    # --- профили (схема 2) ---
    @property
    def schema_version(self) -> int:
        return self.values.get("schema_version") or 1

    def _profile(self, profile_id: str) -> None:
        if profile_id not in PROFILE_IDS:
            raise KeyError(f"неизвестный профиль: {profile_id}")

    def profile_enabled(self, profile_id: str) -> bool:
        """Старая связка включена, пока владелец не написал enabled = false (так было всегда); новая — только true."""
        self._profile(profile_id)
        v = self.values.get(f"profiles.{profile_id}.enabled")
        return v is not False if profile_id == LEGACY_PROFILE else v is True

    def profile_mode(self, profile_id: str) -> str:
        """Режим связки = меньший из общего `mode` и `profiles.<id>.mode`; выключенная связка — не выше readonly
        (doctor читает публичное, отправок нет). Старая связка — общий `mode`, как раньше."""
        self._profile(profile_id)
        if profile_id == LEGACY_PROFILE:
            return self.mode
        rank = {m: i for i, m in enumerate(MODES)}
        m = min(self.mode, self.values.get(f"profiles.{profile_id}.mode") or "dry", key=rank.__getitem__)
        if not self.profile_enabled(profile_id):
            m = min(m, "readonly", key=rank.__getitem__)
        return m

    def env_name(self, key: str) -> str:
        """Имя переменной окружения роли связки (пусто в файле = имя по умолчанию)."""
        self._known(key)
        return self.values[key] or SOL_HL_ENV_DEFAULTS[key]

    def _sol_required(self, pid: str) -> list[str]:
        venue = SOL_PROFILE_VENUES[pid]
        keys = ["telegram.owner_id"]
        opt = {"wallets.sol_hl.hl_vault_address", f"limits.{pid}.min_route_improvement_usdc",
               f"limits.{pid}.normal_exit_basis_bps", f"limits.{pid}.min_exit_pnl_usdc",
               "providers.jupiter.execute_rps", "providers.okx.fee_policy_version",
               *SOL_HL_ENV_DEFAULTS, *(f"emergency.{pid}.{k}" for k in ("max_loss_usdc", "max_hedge_slippage_bps",
                                                                         "max_unwind_slippage_bps", "deadline_ms"))}
        # настройки провайдера нужны, только если его путь выбран в routing.solana.paths (пусто — нужны оба)
        paths = self.values.get("routing.solana.paths") or SOL_PATHS
        used = [p for p, pre in (("jupiter", "jupiter_"), ("okx", "okx_")) if any(x.startswith(pre) for x in paths)]
        wallet = "sol_hl" if pid == SOL_HL else "solana"
        for grp, sub in (("profiles", pid), ("wallets", wallet), ("spot", "solana"), ("routing", "solana"),
                         *(("providers", p) for p in used), ("limits", pid), ("emergency", pid)):
            keys += [f"{grp}.{sub}.{k}" for k in _GROUPS[grp][sub] if f"{grp}.{sub}.{k}" not in opt]
        if venue in _PERP_EXTRA:
            keys += [f"perp.{venue}.{k}" for k in _PERP_EXTRA[venue] if f"perp.{venue}.{k}" not in opt]
        keys.append(f"perp.{venue}.leverage")
        if self.values.get(f"limits.{pid}.normal_exit_policy") == "basis":
            keys += [f"limits.{pid}.normal_exit_basis_bps", f"limits.{pid}.min_exit_pnl_usdc"]
        e = f"emergency.{pid}."
        if any(self.values.get(e + k) is True for k in _EMERGENCY_SOL if k.startswith("auto_")):
            keys += [e + k for k in ("max_loss_usdc", "max_hedge_slippage_bps", "max_unwind_slippage_bps",
                                      "deadline_ms")]
        return keys

    def profile_live_missing(self, profile_id: str) -> list[str]:
        """Пустые ключи, без которых связка не идёт в live (порядок — как в схеме)."""
        self._profile(profile_id)
        if profile_id in EVM_PROFILES:             # EVM-связки: прежний список ключей по (площадка, сеть)
            chain, venue = EVM_PROFILES[profile_id]
            return self.live_missing(venue, chain)
        return self.missing(*self._sol_required(profile_id))

    def profile_unsupported(self, profile_id: str) -> list[str]:
        """Заданное, но не поддержанное первым выпуском — live запрещён с этой причиной (не «поддерживается»)."""
        self._profile(profile_id)
        if profile_id in EVM_PROFILES:
            return []
        if profile_id != SOL_HL:
            return []
        v, out = self.values, []

        def on(key: str, why: str, bad: Any = True) -> None:
            if v.get(key) is bad:
                out.append(f"{key}: {why}")

        on("spot.solana.allow_tx_jup_landing", "отправка через tx.jup.ag — отдельный провайдер посадки, не реализован")
        on("spot.solana.allow_separate_tip_transaction", "отдельная tip-транзакция — многотранзакционный цикл не реализован")
        on("spot.solana.require_finalized_before_next_clip", "следующий клип только после finalized (ТЗ §9.3)", False)
        if v.get("spot.solana.confirmation_trigger") == "confirmed":
            out.append("spot.solana.confirmation_trigger: хедж по confirmed требует обработки отката — не реализована")
        on("routing.solana.allow_external_signer_managed_routes",
           "внешний подписант Jupiter: восстановление по requestId не проверено (external_signer_recovery=false)")
        on("routing.solana.require_persisted_recovery_identity", "без записи идентичности попытки восстановление "
                                                                  "невозможно", False)
        on("providers.jupiter.integrator_fee_enabled", "комиссия интегратора выключена в первом выпуске")
        on("providers.okx.referral_fee_enabled", "реферальная комиссия выключена в первом выпуске")
        on("perp.hyperliquid.builder_fee_enabled", "builder fee выключен в первом выпуске")
        on(f"observability.{SOL_HL}.expose_signed_payloads", "подписанные байты наружу не выдаются (ТЗ §9.2)")
        modes = v.get("perp.hyperliquid.supported_account_modes") or ()
        extra = [m for m in modes if m not in ("manual", "standard")]
        if extra:
            out.append(f"perp.hyperliquid.supported_account_modes: {', '.join(extra)} — только чтение и честный отказ до "
                       "свопа; торговля в первом выпуске — manual/standard")
        return out

    def profile_live_blockers(self, profile_id: str) -> list[str]:
        """Всё, что мешает live связки, одной строкой на причину: выключена, режим, пустые ключи, неподдержанное."""
        out = []
        if not self.profile_enabled(profile_id):
            out.append(f"связка {profile_id} выключена (profiles.{profile_id}.enabled)")
        if self.profile_mode(profile_id) != "live":
            out.append(f"режим {self.profile_mode(profile_id)} (нужен live и в mode, и в profiles.{profile_id}.mode)")
        miss = self.profile_live_missing(profile_id)
        if miss:
            out.append("в owner.toml не задано: " + ", ".join(miss))
        return out + self.profile_unsupported(profile_id)

    def require_profile_live(self, profile_id: str) -> None:
        """Отказ live связки: OwnerMissing со всеми пустыми ключами, затем OwnerUnsupported с прочими причинами."""
        miss = self.profile_live_missing(profile_id)
        if miss:
            raise OwnerMissing(*miss)
        rest = [b for b in self.profile_live_blockers(profile_id) if not b.startswith("в owner.toml не задано")]
        if rest:
            raise OwnerUnsupported(*rest)

    def profile_limit(self, profile_id: str, name: str) -> Any:
        """Лимит связки; пусто — OwnerMissing (нет значения = запрещено, умолчаний нет)."""
        self._profile(profile_id)
        key = f"limits.{profile_id}.{name}"
        self._known(key)
        if self.values[key] is None:
            raise OwnerMissing(key)
        return self.values[key]

    # --- замороженная копия для сделки ---
    def frozen(self) -> dict:
        """Пустые ключи схемы 2 в копию не пишутся: копия старого файла байт-в-байт прежняя (её читает и старый код)."""
        return {"path": self.path, "sha256": self.sha256, "loaded": self.loaded,
                "values": {k: _jsonable(v) for k, v in self.values.items()
                           if not (v is None and k in V2_KEYS | EXTENSION_KEYS)}}

    def frozen_json(self) -> str:
        return json.dumps(self.frozen(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_frozen(cls, s: str) -> "OwnerCfg":
        """Обратно из deals.owner_json: числа снова Decimal, проверки те же (битая копия — ошибка, а не пусто)."""
        d = json.loads(s)
        flat = {}
        for k, v in (d.get("values") or {}).items():
            if k not in SCHEMA:
                raise OwnerConfigError(f"в копии неизвестный ключ {k}")
            if v is None:
                continue
            spec = SCHEMA[k]
            if spec.kind == "num" and isinstance(v, str) and v not in spec.words:
                try:
                    v = Decimal(v)
                except InvalidOperation:
                    raise OwnerConfigError(f"{k} = {v!r}: в копии не число") from None
            flat[k] = v
        values, errs = _validate(flat)
        if errs:
            raise OwnerConfigError("копия owner.toml не проходит проверку: " + "; ".join(errs))
        return cls(values=MappingProxyType(values), path=d.get("path") or "", sha256=d.get("sha256"),
                   loaded=float(d.get("loaded") or 0))


def load(path: Path | str | None = None) -> OwnerCfg:
    """Свежее чтение файла — без кэша (правка владельца действует со следующей команды). Нет файла — всё пусто:
    режим dry, live запрещён."""
    p = Path(path) if path else config.OWNER_PATH
    now = time.time()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return OwnerCfg(values=MappingProxyType({k: None for k in SCHEMA}), path=str(p), sha256=None, loaded=now)
    try:
        doc = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except UnicodeDecodeError:
        raise OwnerConfigError(f"{p}: не UTF-8") from None
    except tomllib.TOMLDecodeError as e:
        raise OwnerConfigError(f"{p}: ошибка TOML — {e} (пустое значение пишется как \"\")") from None
    flat, errs = _flatten(doc)
    values, verrs = _validate(flat)
    errs += verrs
    if errs:
        raise OwnerConfigError(f"{p}: " + "; ".join(errs))
    return OwnerCfg(values=MappingProxyType(values), path=str(p), sha256=hashlib.sha256(raw).hexdigest(), loaded=now)
