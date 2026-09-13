"""sol_doctor — веха M1, только чтение: doctor / quote-compare / hl-preflight / record на фейках и одна проба на
публичных данных (только при FUNDING_BOT_NET_PROBE=1). Всё синтетическое: адреса не владельца, ключи не настоящие,
числа лимитов — предложения пилота из плана, не решения владельца."""
import base64
import json
import os
import sqlite3
import struct
import time
from decimal import Decimal as D
from urllib.parse import urlsplit

import pytest

from funding_bot import cli, okxdex
from funding_bot import sol_doctor as SD
from funding_bot.trade import keys as K
from funding_bot.trade import owner as O
from funding_bot.trade import spot_router as SR
from funding_bot.trade.fees import network_components
from funding_bot.trade.solana import (ANSEM_MINT, MAINNET_GENESIS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT)
from funding_bot.trade.solana import accounts as A
from funding_bot.trade.solana.b58 import b58decode, b58encode

T0 = 1789318800.0                               # 13.09.2026 17:00Z
WALLET = b58encode(bytes([7] * 32))
MASTER = "0x" + "11" * 20
AGENT = "0x" + "22" * 20
AUTH = b58encode(bytes([5] * 32))
JUP_PD, OKX_PD = b58encode(bytes([8] * 32)), b58encode(bytes([9] * 32))
SECRETS = {"SOLANA_SECRET_B58": b58encode(bytes([9] * 64)), "HL_AGENT_PRIVATE_KEY": "0x" + "ab" * 32,
           "JUPITER_API_KEY": "jupkey-SECRET-123456", "OKX_DEX_API_KEY": "okxkey-SECRET-111",
           "OKX_DEX_SECRET": "okxsecret-SECRET-222", "OKX_DEX_PASSPHRASE": "okxpass-SECRET-333",
           "SOLANA_RPC_URL": "https://primary.example/?api-key=rpcSECRET999",
           "SOLANA_RPC_SECONDARY_URL": "https://secondary.example/"}
LEAKS = ("jupkey-SECRET", "okxkey-SECRET", "okxsecret-SECRET", "okxpass-SECRET", "rpcSECRET999", "ab" * 32,
         SECRETS["SOLANA_SECRET_B58"])


@pytest.fixture(autouse=True)
def _redaction():
    K._reset_redaction_for_tests()
    yield
    K._reset_redaction_for_tests()


# --- owner.toml и реестр ------------------------------------------------------------------------------
def _q(s):
    return '"' + s + '"'


def sections():
    return {
        "": {"schema_version": "2", "mode": _q("live")},
        "telegram": {"owner_id": "42"},
        "profiles.sol_best_hyperliquid": {
            "enabled": "true", "mode": _q("live"), "spot_chain": _q("solana-mainnet"), "spot_policy": _q("auto"),
            "perp_venue": _q("hyperliquid"), "perp_network": _q("mainnet"), "perp_dex": _q("para"),
            "instrument_registry": _q("instruments.json"), "allowed_instruments": '["ansem_sol_para_v1"]',
            "max_active_execution_clips": "1"},
        "wallets.sol_hl": {
            "solana_address": _q(WALLET), "hl_user_address": _q(MASTER), "hl_account_address": _q(MASTER),
            "hl_agent_address": _q(AGENT), "hl_vault_address": '""', "position_ownership_policy": _q("exclusive_market")},
        "spot.solana": {
            "expected_genesis_hash": _q(MAINNET_GENESIS), "transaction_versions": '["legacy", "v0"]',
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
            "max_clip_usdc": _q("150"), "max_operation_usdc": _q("150"), "max_total_position_usdc": _q("150"),
            "max_surplus_tokens": _q("20"), "max_surplus_usdc": _q("3.5"), "max_delta_tokens": _q("150"),
            "max_delta_usdc": _q("25"), "max_unhedged_ms": "60000", "max_spot_slippage_bps": "100",
            "max_price_impact_bps": "150", "max_hedge_slippage_bps": "60", "min_entry_basis_all_in_bps": _q("-150"),
            "normal_exit_policy": _q("command"), "max_network_fee_lamports_per_tx": "200000",
            "max_tip_lamports_per_tx": "0", "max_external_fees_usdc_per_operation": _q("0.5"),
            "min_sol_reserve_lamports": "20000000", "max_rent_locked_lamports": "5000000",
            "min_hl_margin_reserve_usdc": _q("20"), "min_liquidation_distance_bps": "5000",
            "max_dust_tokens": _q("1"), "max_dust_usdc": _q("0.25"), "max_quote_age_ms": "5000",
            "max_book_age_ms": "2000", "max_source_skew_ms": "2000", "max_native_price_age_ms": "60000",
            "max_metadata_age_ms": "600000", "collection_deadline_ms": "3000", "min_blockhash_validity_heights": "60",
            "max_hedge_attempts": "3", "hedge_deadline_ms": "20000", "hl_action_expires_after_ms": "30000"},
        "emergency.sol_best_hyperliquid": {k: "false" for k in (
            "auto_unwind_spot_after_proven_zero_hedge", "auto_rebuy_spot_after_proven_zero_close",
            "auto_sell_surplus_after_resolved_hedge", "auto_correct_known_delta")},
        "observability.sol_best_hyperliquid": {
            "record_candidate_rejections": "true", "record_source_times": "true",
            "record_sanitized_payload_hashes": "true", "expose_signed_payloads": "false",
            "emit_state_transition_events": "true"},
    }


def owner_file(tmp_path, over=None, drop=()):
    secs = sections()
    for sec, kv in (over or {}).items():
        for k, v in kv.items():
            if v is None:
                secs.setdefault(sec, {}).pop(k, None)
            else:
                secs.setdefault(sec, {})[k] = v
    lines = [f"{k} = {v}" for k, v in secs.pop("").items()]
    for sec, kv in secs.items():
        if sec not in drop:
            lines += [f"\n[{sec}]"] + [f"{k} = {v}" for k, v in kv.items()]
    p = tmp_path / "owner.toml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def registry_file(tmp_path, ready=True):
    ident = ({"status": "reviewed_override", "evidence": ["chat:owner:20260913"], "reviewed_by": "owner",
              "reviewed_at": "2026-09-13T15:00:00+00:00", "review_reason": "подтверждено владельцем в чате 13.09",
              "expires_at": "2026-10-13T15:00:00+00:00"} if ready else
             {"status": "pending_underlying_evidence", "evidence": ["evidence/ansem_solana_mint.json"]})
    rec = {"instrument_id": "ansem_sol_para_v1", "version": 1, "profile_id": "sol_best_hyperliquid",
           "display_symbol": "ANSEM", "enabled_for_live": ready,
           "spot": {"network": "solana-mainnet", "genesis_hash": MAINNET_GENESIS if ready else None, "mint": ANSEM_MINT,
                    "decimals": 6, "token_program": TOKEN_2022_PROGRAM,
                    "observed_extensions": ["metadataPointer", "tokenMetadata"]},
           "quote": {"network": "solana-mainnet", "mint": USDC_MINT, "decimals": 6, "token_program": TOKEN_PROGRAM},
           "units": {"spot_units_in_base": "1", "perp_units_in_base": "1",
                     "status": "accepted" if ready else "candidate_pending_mapping_acceptance"},
           "perp": {"network": "mainnet", "venue": "hyperliquid", "dex": "para", "fullcoin": "para:ANSEM",
                    "account_id": f"hl:mainnet:{MASTER}:para" if ready else None, "collateral_token_id_observed": 0,
                    "observed_asset_id": 180025, "observed_sz_decimals": 0, "observed_max_leverage": 3,
                    "observed_margin_mode": "noCross"},
           "identity": ident, "snapshot": {"must_refresh_before_live": True}}
    p = tmp_path / "instruments.json"
    p.write_text(json.dumps({"schema_version": 1, "instruments": [rec]}), encoding="utf-8")
    return p


# --- фейковый Hyperliquid -------------------------------------------------------------------------------
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = {}

    def json(self):
        return json.loads(self.content)


class Wall:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _ch(withdrawable="400.0"):
    z = {"accountValue": "0.0", "totalNtlPos": "0.0", "totalRawUsd": "0.0", "totalMarginUsed": "0.0"}
    return {"marginSummary": dict(z), "crossMarginSummary": dict(z), "crossMaintenanceMarginUsed": "0.0",
            "withdrawable": withdrawable, "time": int(T0 * 1000), "assetPositions": []}


class FakeHL:
    """HL /info по type; /exchange — провал теста (доктор ничего не отправляет)."""

    def __init__(self, wall):
        self.wall = wall
        uni = [{"name": f"para:X{i}", "szDecimals": 0, "maxLeverage": 3, "onlyIsolated": True} for i in range(25)]
        uni.append({"name": "para:ANSEM", "szDecimals": 0, "maxLeverage": 3, "onlyIsolated": True,
                    "marginMode": "noCross", "deployerFeeScale": "0.5"})
        meta = {"universe": uni, "collateralToken": 0}
        ctx = {"funding": "0.0000125", "markPx": "0.1665", "oraclePx": "0.1664", "midPx": "0.1665", "premium": "0.0",
               "openInterest": "9324956", "dayNtlVlm": "100000", "impactPxs": ["0.1663", "0.1667"]}
        hist = [{"coin": "para:ANSEM", "fundingRate": "0.00022", "premium": "0.0",
                 "time": int(T0 * 1000) - h * 3_600_000} for h in range(168, 0, -1)]
        self.fail: dict[str, int] = {}          # type → сколько раз отвечать 500
        self.market = {
            "perpDexs": [None] + [{"name": f"d{i}"} for i in range(1, 8)] + [{"name": "para"}],
            "meta": meta, "metaAndAssetCtxs": [meta, [ctx] * 26], "allMids": {"SOL": "150.25"},
            "l2Book": lambda b: {"coin": "para:ANSEM", "time": int(self.wall() * 1000) - 400, "levels": [
                [{"px": "0.1664", "sz": "5000", "n": 1}, {"px": "0.1663", "sz": "5000", "n": 1}],
                [{"px": "0.1666", "sz": "5000", "n": 1}, {"px": "0.1667", "sz": "5000", "n": 1}]]},
            "perpDexLimits": {"totalOiCap": "50000000.0", "oiSzCapPerPerp": "10000000000.0",
                              "coinToOiCap": [["para:ANSEM", "5000000.0"]]},
            "perpsAtOpenInterestCap": [],
            "fundingHistory": lambda b: [r for r in hist if b["startTime"] <= r["time"] <= b.get("endTime", 10 ** 16)],
        }
        self.users = {
            MASTER: {"userAbstraction": "disabled", "userRole": {"role": "user"}, "extraAgents": [],
                     "clearinghouseState": _ch(), "userFees": {"userCrossRate": "0.00045", "userAddRate": "0.00015"},
                     "spotClearinghouseState": {"balances": [{"coin": "USDC", "token": 0, "total": "12.5",
                                                              "hold": "0.0"}]},
                     "activeAssetData": {"user": MASTER, "coin": "para:ANSEM", "leverage": {"type": "isolated",
                                                                                          "value": 3}},
                     "userRateLimit": {"cumVlm": "0.0", "nRequestsUsed": 10, "nRequestsCap": 10000},
                     "frontendOpenOrders": []},
            AGENT: {"userRole": {"role": "agent", "data": {"user": MASTER}}},
        }
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        payload = json.loads(data)
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        if url.endswith("/exchange"):
            raise AssertionError("доктор не отправляет /exchange")
        typ = payload["type"]
        if self.fail.get(typ):
            self.fail[typ] -= 1
            return Resp(500, {"error": "boom"})
        h = self.users[payload["user"].lower()][typ] if "user" in payload else self.market[typ]
        return Resp(200, h(payload) if callable(h) else h)


# --- фейковый Solana RPC --------------------------------------------------------------------------------
def _coption(key):
    return struct.pack("<I", 0) + bytes(32) if key is None else struct.pack("<I", 1) + b58decode(key)


def _mint_bytes(decimals, mint_auth=None, freeze=None, exts=None):
    b = _coption(mint_auth) + struct.pack("<Q", 10 ** 15) + bytes([decimals, 1]) + _coption(freeze)
    if exts is None:
        return b
    b += bytes(165 - len(b)) + bytes([1])
    return b + b"".join(struct.pack("<HH", t, len(v)) + v for t, v in exts)


def _acc(owner, raw: bytes, parsed):
    return {"b64": {"owner": owner, "lamports": 2_039_280, "space": len(raw), "executable": False,
                    "data": [base64.b64encode(raw).decode(), "base64"]},
            "jsonParsed": {"owner": owner, "lamports": 2_039_280, "space": len(raw), "executable": False,
                           "data": {"program": "spl-token", "space": len(raw), "parsed": parsed}}}


def _accounts():
    ansem_exts = [(18, bytes(32) + b58decode(ANSEM_MINT)), (19, b"\x00" * 16)]
    ansem = _acc(TOKEN_2022_PROGRAM, _mint_bytes(6, exts=ansem_exts), {"type": "mint", "info": {
        "decimals": 6, "supply": str(10 ** 15), "isInitialized": True, "mintAuthority": None, "freezeAuthority": None,
        "extensions": [{"extension": "metadataPointer", "state": {"authority": None, "metadataAddress": ANSEM_MINT}},
                       {"extension": "tokenMetadata", "state": {"updateAuthority": None, "mint": ANSEM_MINT,
                                                                "name": "The Black Bull", "symbol": "ANSEM"}}]}})
    usdc = _acc(TOKEN_PROGRAM, _mint_bytes(6, AUTH, AUTH), {"type": "mint", "info": {
        "decimals": 6, "supply": str(10 ** 15), "isInitialized": True, "mintAuthority": AUTH, "freezeAuthority": AUTH}})
    ata_usdc = A.ata(WALLET, USDC_MINT, TOKEN_PROGRAM)
    raw = (b58decode(USDC_MINT) + b58decode(WALLET) + struct.pack("<Q", 503_850_000) + _coption(None) + bytes([1])
           + struct.pack("<I", 0) + bytes(8) + struct.pack("<Q", 0) + _coption(None))
    usdc_ata = _acc(TOKEN_PROGRAM, raw, {"type": "account", "info": {
        "mint": USDC_MINT, "owner": WALLET, "tokenAmount": {"amount": "503850000", "decimals": 6},
        "state": "initialized", "isNative": False}})
    prog = lambda pd: {"b64": {"owner": SD.BPF_UPGRADEABLE, "lamports": 1, "space": 36, "executable": True,  # noqa
                               "data": [base64.b64encode(struct.pack("<I", 2) + b58decode(pd)).decode(), "base64"]}}
    pd = {"b64": {"owner": SD.BPF_UPGRADEABLE, "lamports": 1, "space": 45, "executable": False, "data": [
        base64.b64encode(struct.pack("<I", 3) + struct.pack("<Q", 446_000_000) + b"\x01" + b58decode(AUTH)).decode(),
        "base64"]}}
    return {ANSEM_MINT: ansem, USDC_MINT: usdc, ata_usdc: usdc_ata, SD.JUP_PROGRAM: prog(JUP_PD),
            SD.OKX_ROUTER: prog(OKX_PD), JUP_PD: pd}


class FakeRPC:
    def __init__(self):
        self.accounts = _accounts()
        self.calls = []
        self.fail: set[str] = set()
        self.genesis = {}

    def post(self, url, json=None, timeout=None, **kw):
        host, m, p = urlsplit(url).hostname, json["method"], json.get("params") or []
        self.calls.append((host, m))
        if m == "sendTransaction":
            raise AssertionError("доктор не отправляет транзакции")
        if m in self.fail:
            return Resp(503, {})
        return Resp(200, {"jsonrpc": "2.0", "id": json["id"], "result": self.result(host, m, p)})

    def result(self, host, m, p):
        ctx = {"slot": 446_700_000}
        if m == "getGenesisHash":
            return self.genesis.get(host, MAINNET_GENESIS)
        if m == "getHealth":
            return "ok"
        if m == "getSlot":
            return 446_700_000
        if m == "getBlockHeight":
            return 424_750_000
        if m == "getLatestBlockhash":
            return {"context": ctx, "value": {"blockhash": b58encode(bytes([3] * 32)), "lastValidBlockHeight": 424_750_150}}
        if m == "getBalance":
            return {"context": ctx, "value": 749_700_000}
        if m == "getMinimumBalanceForRentExemption":
            return (p[0] + 128) * 6960
        if m == "getAccountInfo":
            a = self.accounts.get(p[0])
            enc = p[1]["encoding"]
            return {"context": ctx, "value": None if a is None else a.get("b64" if enc == "base64" else enc)}
        raise AssertionError(f"неожиданный RPC {m}")


# --- фейковые провайдеры маршрутов ----------------------------------------------------------------------
def cand(path, req, out_raw, *, ready=False, reasons=()):
    base, prio = network_components(n_signatures=1, cu_limit=200_000, cu_price_micro=1_000, payer=req.wallet,
                                    source=path)
    thr = out_raw * (10_000 - req.slippage_bps) // 10_000
    now = time.monotonic()
    return SR.SwapCandidate(
        provider=SR.GROUP_OF[path], path=path, adapter_version="fake/1", request_hash=req.request_hash, side=req.side,
        input_mint=req.input.mint, output_mint=req.output.mint, input_program=req.input.program,
        output_program=req.output.program, input_decimals=req.input.decimals, output_decimals=req.output.decimals,
        amount_in_raw=req.amount_in_raw, expected_out_raw=out_raw, min_out_raw=thr, onchain_min_out_raw=thr,
        fees=(base, prio), price_impact_bps=D("12.5"), received_at=T0, received_mono=now, latency_ms=340,
        simulation_ok=True if ready else None, validated=True if ready else None,
        message_hash="ab" * 32 if ready else None, last_valid_block_height=424_750_150 if ready else None,
        built_mono=now if ready else None, reasons=tuple(reasons))


class FakeProvider:
    def __init__(self, group, paths, out, *, ready=False, unavailable=None):
        self.group, self.paths, self.out, self.ready, self.unavailable = group, paths, out, ready, unavailable
        self.requests = []

    def candidates(self, req):
        self.requests.append(req)
        if self.unavailable:
            return [SR.Unavailable(self.group, p, self.unavailable, "фейк", time.monotonic()) for p in self.paths]
        k = 1 if req.side == "entry" else D("0.1665")         # продажа: токены → USDC по цене около марка
        res = []
        for p in self.paths:
            raw = int(D(req.amount_in_raw) * self.out[p] / 150 * k) if req.side == "exit" else self.out[p]
            res.append(cand(p, req, raw, ready=self.ready and p != "jupiter_order_v2",
                            reasons=("capability:order_preview_only",) if p == "jupiter_order_v2" else ()))
        return res


def providers(ready=False, okx_unavailable=None):
    return [FakeProvider("jupiter", ("jupiter_order_v2", "jupiter_build_v2"),
                         {"jupiter_order_v2": 901_000_000, "jupiter_build_v2": 900_900_000}, ready=ready),
            FakeProvider("okx", ("okx_solana_v6",), {"okx_solana_v6": 899_000_000}, ready=ready,
                         unavailable=okx_unavailable)]


def setup(tmp_path, *, over=None, drop=(), ready_registry=True, prov=None, env=None):
    wall = Wall()
    hl, rpc = FakeHL(wall), FakeRPC()
    deps = SD.Deps(hl_session=hl, rpc_session=rpc, providers=prov, registry_path=registry_file(tmp_path, ready_registry),
                   wall=wall, sleep=wall.sleep, okx_pace=False, jup_gate=SR.RateGate(0))
    return owner_file(tmp_path, over, drop), dict(SECRETS) if env is None else env, deps, hl, rpc


def run(argv, owner, env, deps):
    out = []
    code = SD.main([argv[0], "--owner", str(owner), *argv[1:]], environ=env, deps=deps, out=out.append)
    return code, "\n".join(out)


# --- тесты ------------------------------------------------------------------------------------------------
def test_doctor_readonly_pilot_not_ready_no_keys_no_sends(tmp_path, monkeypatch):
    """Профиль выключен/readonly, лимитов нет: live_ready=false с причинами; приватные ключи не загружены и стёрты;
    ни /exchange, ни sendTransaction; секреты в выводе не видны."""
    monkeypatch.setattr(K.SolanaKey, "__init__", lambda *a, **k: pytest.fail("ключ Solana загружен"))
    monkeypatch.setattr(K, "_load_hl_agent", lambda *a, **k: pytest.fail("ключ агента HL загружен"))
    owner, env, deps, hl, rpc = setup(tmp_path, over={"profiles.sol_best_hyperliquid": {
        "enabled": "false", "mode": _q("readonly")}}, drop=("limits.sol_best_hyperliquid",), prov=providers())
    code, text = run(["doctor"], owner, env, deps)
    assert code == 1
    assert "live_ready=false" in text
    assert "SOLANA_SECRET_B58" not in env and "HL_AGENT_PRIVATE_KEY" not in env
    for leak in LEAKS:
        assert leak not in text
    assert not [c for c in hl.calls if c[0] == "exchange"] and ("primary.example", "sendTransaction") not in rpc.calls
    assert "связка sol_best_hyperliquid выключена" in text and "[limits.sol_best_hyperliquid]" in text
    assert "✓ primary@primary.example: genesis 5eyk…2N9d совпал" in text
    assert "✓ secondary@secondary.example" in text
    assert "✓ asset 180025 (dex para #8, позиция 25) · szDecimals 0 · maxLeverage 3 · только isolated" in text
    assert "✓ режим счёта disabled → Standard: поддержан" in text
    assert f"✓ агент {AGENT[:4]}…{AGENT[-4:]}: userRole agent мастера" in text
    assert "? котировки: не задан limits.sol_best_hyperliquid.max_spot_slippage_bps" in text


def test_doctor_all_green_live_ready_true(tmp_path):
    owner, env, deps, *_ = setup(tmp_path, prov=providers(ready=True))
    code, text = run(["doctor"], owner, env, deps)
    assert "✗" not in text and "\n? " not in text, text
    assert text.endswith("live_ready=true — состояние может измениться: проверка повторяется перед каждой операцией")
    assert code == 0
    assert "✓ identity reviewed_override (owner: подтверждено владельцем в чате 13.09) · до 2026-10-13, осталось 29 д" in text
    assert "✓ маржа 400.00 USDC (clearinghouseState(dex).withdrawable) · нужно ≥ 170.00" in text
    assert "✓ USDC 503.85 (ATA" in text and "ANSEM: ATA" in text and "создастся первой покупкой" in text
    assert "✓ выбор complete: jupiter_build_v2" in text
    assert "Jupiter v6 JUP6…TaV4: последний деплой — слот 446000000" in text
    assert "OKX router 6m2C…kBma: programdata узлом не отдаётся" in text


def test_doctor_unified_account_not_ready(tmp_path):
    owner, env, deps, hl, _ = setup(tmp_path, prov=providers(ready=True))
    hl.users[MASTER]["userAbstraction"] = "unifiedAccount"
    code, text = run(["doctor"], owner, env, deps)
    assert code == 1
    assert "✗ режим счёта unifiedAccount — v1 торгует только Standard: переключите счёт в Standard" in text
    assert "live_ready=false — причин" in text


def test_doctor_pending_identity_and_registry_blockers(tmp_path):
    owner, env, deps, *_ = setup(tmp_path, ready_registry=False, prov=providers(ready=True))
    code, text = run(["doctor"], owner, env, deps)
    assert code == 1
    assert "✗ identity pending_underlying_evidence — для live нужен verified_source или reviewed_override" in text
    assert "✗ запись выключена для live" in text and "✗ единицы: candidate_pending_mapping_acceptance" in text


def test_doctor_dry_reads_no_environment(tmp_path):
    """dry: окружение не читается вовсе; RPC — публичный; Jupiter без ключа; OKX без ключа — без запросов."""
    class NoEnv(dict):
        def _boom(self, *a, **k):
            raise AssertionError("в dry прочитано окружение")
        get = pop = __getitem__ = __contains__ = _boom

    class JupSession:
        def __init__(self):
            self.headers = []

        def get(self, url, params=None, headers=None, timeout=None):
            self.headers.append(headers)
            return Resp(429, {"error": "rate"})

    class NoOkx:
        headers = {}

        def request(self, *a, **k):
            raise AssertionError("OKX без ключа не вызывается")

    owner, _, deps, _, rpc = setup(tmp_path, over={"profiles.sol_best_hyperliquid": {"mode": _q("dry")}})
    jup = JupSession()
    deps.jup_session, deps.okx_session = jup, NoOkx()
    code, text = run(["doctor"], owner, NoEnv(), deps)
    assert code == 1
    assert "режим dry: окружение не читается" in text and "окружение не читалось (режим dry)" in text
    assert "public@api.mainnet-beta.solana.com: genesis" in text
    assert {h for h, _ in rpc.calls} == {"api.mainnet-beta.solana.com"}
    assert jup.headers and all("x-api-key" not in h for h in jup.headers)
    assert "okx_solana_v6: недоступен — no_credentials" in text
    assert "jupiter_build_v2: недоступен — rate_limited" in text


def test_unknown_is_never_zero(tmp_path):
    """Сбой чтения маржи и SOL — «?» с причиной, а не 0; live_ready=false."""
    owner, env, deps, hl, rpc = setup(tmp_path, prov=providers(ready=True))
    hl.fail["clearinghouseState"] = 10
    rpc.fail.add("getBalance")
    code, text = run(["doctor"], owner, env, deps)
    assert code == 1
    assert "? маржа (standard): неизвестна — не прочитано: HlNetError" in text
    assert "? SOL не прочитан — RpcUnavailable" in text
    assert "маржа 0" not in text and "SOL 0 " not in text


def test_genesis_mismatch_secondary(tmp_path):
    owner, env, deps, _, rpc = setup(tmp_path, prov=providers(ready=True))
    rpc.genesis["secondary.example"] = b58encode(bytes([1] * 32))
    code, text = run(["doctor", "--no-quotes"], owner, env, deps)
    assert code == 1
    assert "✗ secondary@secondary.example: secondary@secondary.example: genesis" in text
    assert "чужая сеть" in text
    assert "? котировки не запрашивались (--no-quotes)" in text


def test_doctor_readonly_without_addresses(tmp_path):
    """readonly, адресов нет: ключи не загружены — это видно; ключ в окружении не выдаётся за «нет ключа»;
    чтения через публичный RPC; приватные переменные всё равно стёрты."""
    owner, env, deps, _, rpc = setup(tmp_path, over={"wallets.sol_hl": {
        "solana_address": '""', "hl_user_address": '""', "hl_account_address": '""'}}, prov=providers())
    code, text = run(["doctor"], owner, env, deps)
    assert code == 1
    assert "✗ ключи и адреса связки (readonly): в owner.toml не задано: wallets.sol_hl.solana_address" in text
    assert "✗ RPC владельца не загружены (SOLANA_RPC_URL, SOLANA_RPC_SECONDARY_URL)" in text
    assert "Jupiter: ключи связки не загружены" in text and "? OKX: ключи связки не загружены" in text
    assert "OKX: ключа нет" not in text
    assert {h for h, _ in rpc.calls} == {"api.mainnet-beta.solana.com"}
    assert "✗ wallets.sol_hl.hl_account_address не задан" in text
    assert "SOLANA_SECRET_B58" not in env and "HL_AGENT_PRIVATE_KEY" not in env
    for leak in LEAKS:
        assert leak not in text


def test_quote_compare_three_paths(tmp_path):
    owner, env, deps, *_ = setup(tmp_path, prov=providers(okx_unavailable="no_credentials"))
    code, text = run(["quote-compare", "--side", "buy", "--amount-raw", "150000000"], owner, env, deps)
    assert code == 0, text
    assert "покупка ANSEM: вход 150 USDC (150000000 raw)" in text and "slippage 100 бп" in text
    assert "· jupiter_order_v2 (только показ) · выход 901 ANSEM" in text
    assert "· jupiter_build_v2 · выход 900.9 ANSEM · мин. 891.891" in text
    assert "USDC за ANSEM" in text and "расходы 0.000781 USDC" in text and "impact 12.5 бп" in text
    assert "базис к шорту" in text and "возраст" in text and "ответ 340 мс" in text
    assert "для live: только показ" in text and "не симулирован" in text
    assert "✗ okx_solana_v6: недоступен — no_credentials (фейк)" in text
    assert "победитель показа: jupiter_order_v2 — в live не исполним (no_eligible_route)" in text
    assert "шорт HL: 901 контр., VWAP 0.166400" in text
    assert "SOL 150.25 USDC (HL allMids SOL)" in text


def test_quote_compare_same_amount_and_sell_side(tmp_path):
    prov = providers()
    owner, env, deps, *_ = setup(tmp_path, prov=prov)
    code, text = run(["quote-compare", "--side", "sell", "--amount-raw", "900000000", "--slippage-bps", "50"],
                     owner, env, deps)
    assert code == 0, text
    reqs = [r for p in prov for r in p.requests]
    assert {r.amount_in_raw for r in reqs} == {900_000_000} and {r.side for r in reqs} == {"exit"}
    assert {r.input.mint for r in reqs} == {ANSEM_MINT} and {r.output.mint for r in reqs} == {USDC_MINT}
    assert {r.slippage_bps for r in reqs} == {50} and {r.purpose for r in reqs} == {"mark"}
    assert "продажа ANSEM: вход 900 ANSEM" in text and "нетто" in text


def test_quote_compare_refuses_without_slippage(tmp_path):
    owner, env, deps, *_ = setup(tmp_path, over={"limits.sol_best_hyperliquid": {"max_spot_slippage_bps": None}},
                                 prov=providers())
    code, text = run(["quote-compare", "--side", "buy", "--amount-raw", "1000000"], owner, env, deps)
    assert code == 2
    assert "✗ не задан limits.sol_best_hyperliquid.max_spot_slippage_bps" in text


def test_hl_preflight_agent_not_approved(tmp_path):
    owner, env, deps, hl, _ = setup(tmp_path)
    hl.users[AGENT]["userRole"] = {"role": "user"}
    code, text = run(["hl-preflight"], owner, env, deps)
    assert code == 1
    assert "✗ агент 0x22…2222: userRole user — не агент этого мастера: одобрите API-кошелёк в HL" in text
    assert text.endswith("hl-preflight: не готово — причин 1 (✗ и ? выше)")
    assert "фандинг сейчас +0.0012%/ч · за 7 д в среднем +0.0220%/ч (168 ч)" in text
    assert "стакан bid 0.1664 / ask 0.1666" in text and "шорт на 150 USDC: 901 контр." in text


def test_hl_preflight_ready(tmp_path):
    owner, env, deps, *_ = setup(tmp_path)
    code, text = run(["hl-preflight"], owner, env, deps)
    assert code == 0, text
    assert "✓ OI $1.55M из лимита $5.00M (31 %)" in text
    assert "тейкер ≈0.0675% (ставка счёта 0.0450% × 1.5 HIP-3)" in text


def test_record_writes_own_sqlite_and_failures_as_reasons(tmp_path):
    owner, env, deps, hl, rpc = setup(tmp_path, prov=providers())
    hl.fail["l2Book"] = 2                          # первый замер стакана — сбой (две попытки HlHttp)
    db = tmp_path / "series.sqlite"
    code, text = run(["record", "--minutes", "1", "--db", str(db), "--hl-every", "15", "--routes-every", "30",
                      "--rpc-every", "20"], owner, env, deps)
    assert code == 0, text
    con = sqlite3.connect(db)
    hl_rows = con.execute("SELECT bid, sell_vwap, error FROM hl_samples ORDER BY id").fetchall()
    assert len(hl_rows) == 4
    assert hl_rows[0][0] is None and hl_rows[0][1] is None and hl_rows[0][2].startswith("book ")
    assert all(r[0] == "0.1664" and r[2] is None for r in hl_rows[1:])
    rpc_rows = con.execute("SELECT endpoint, ok, blockhash_margin FROM rpc_samples").fetchall()
    assert len(rpc_rows) == 6 and {r[2] for r in rpc_rows} == {150}
    routes = con.execute("SELECT cycle, side, path, status, amount_in_raw FROM route_samples").fetchall()
    assert {r[0] for r in routes} == {1, 2}
    assert ("buy", "jupiter_build_v2", "preview") in {(r[1], r[2], r[3]) for r in routes}
    sell_amounts = {r[4] for r in routes if r[1] == "sell"}
    assert sell_amounts == {"901000000"}           # продажа — объём лучшего пути покупки, одинаковый по путям
    assert "стакан HL: 4 замеров, с ошибкой 1" in text and "мин. запас высот blockhash 150" in text
    assert not [c for c in hl.calls if c[0] == "exchange"]


def test_record_refuses_trader_and_collector_db(tmp_path):
    owner, env, deps, *_ = setup(tmp_path, prov=providers())
    for name in ("trade.db", "funding_bot.db"):
        code, text = run(["record", "--minutes", "1", "--db", str(tmp_path / name)], owner, env, deps)
        assert code == 2 and "отдельный SQLite" in text
    assert SD.record_path(None) == SD.config.RUNTIME / "sol_hl_record.sqlite"


def test_okx_uses_shared_pace_file(tmp_path, monkeypatch):
    pace = tmp_path / "okxdex.pace"
    monkeypatch.setattr(okxdex, "PACE_PATH", pace)
    owner, env, deps, *_ = setup(tmp_path)
    deps.okx_pace = None                            # как в бою: общий файл темпа
    ctx = SD.Ctx(O.load(owner), env, deps)
    jup, okx = ctx.providers()
    assert okx.okx.pace_path == pace and okx.okx.enabled() and jup.has_key()
    assert "jupkey" not in repr(jup) and "okxkey" not in repr(ctx.creds)


def test_book_depth_math():
    from funding_bot.trade.types import Book
    b = Book(bids=((D("0.2"), D("100")), (D("0.1"), D("1000"))), asks=((D("0.3"), D("10")),), ts=T0)
    d = SD.book_depth(b, D("30"), 0)
    q, vwap, slip = d["sell"]
    assert q == 150 and vwap == (D("20") + D("5")) / 150 and slip.quantize(D("0.1")) == D("1666.7")
    assert d["buy"] is None                       # на 100 контрактов асков 10 — глубины нет, не «бесконечно»


def test_cli_registration(monkeypatch):
    seen = []
    monkeypatch.setattr(SD, "main", lambda argv: seen.append(argv) or 7)
    assert cli.main(["sol-hl", "doctor", "--no-quotes", "--owner", "x.toml"]) == 7
    assert cli.main(["sol-hl", "record", "--minutes", "5"]) == 7
    assert seen == [["doctor", "--no-quotes", "--owner", "x.toml"], ["record", "--minutes", "5"]]
    with pytest.raises(SystemExit):
        cli.main(["sol-hl", "send"])


@pytest.mark.skipif(os.environ.get("FUNDING_BOT_NET_PROBE") != "1",
                    reason="сеть: только по FUNDING_BOT_NET_PROBE=1 (публичные HL /info и Solana RPC, только чтение)")
def test_public_probe(tmp_path):
    cfg = O.load(tmp_path / "нет.toml")            # файла нет: режим dry, окружение не читается
    ctx = SD.Ctx(cfg, {}, SD.Deps(registry_path=tmp_path / "нет.json"))
    rep = SD.doctor(ctx, quotes=False)
    text = rep.render()
    print(text)
    ref = ctx.ref()
    assert (ref.fullcoin, ref.asset, ref.sz_decimals, ref.only_isolated) == ("para:ANSEM", 180025, 0, True)
    assert "✓ public@api.mainnet-beta.solana.com: genesis 5eyk…2N9d совпал" in text
    mi = A.read_mint(ctx.rpc(), ANSEM_MINT)
    assert (mi.program, mi.decimals, sorted(mi.ext_names)) == (TOKEN_2022_PROGRAM, 6, ["metadataPointer",
                                                                                         "tokenMetadata"])
    assert "live_ready=false" in text
