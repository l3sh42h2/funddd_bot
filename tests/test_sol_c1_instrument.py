"""Шаг C1 связки SOL×HL: InstrumentSpec schema 2 (types.py) и реестр runtime/instruments.json.

- schema 1 (DQA9Q, okx·bsc × Aster): отпечаток, JSON и читатель прежние — сделка не перепривязывается (M03);
- schema 2: профиль, сеть и genesis, mint base58 с регистром, программа токена, котировка, перп (площадка/сеть/dex/
  fullcoin), scope счёта, Fs/Fp, статус identity; хеш — только по идентичности (ТЗ §2.1, S01/S02, V03);
- deploy/instruments.json.example: ANSEM reviewed_override (решение владельца 13.09, срок 30 дней);
- base58 — один кодек на проект (trade/solana/b58.py)."""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from funding_bot.trade import engine as eng, instruments as I, keys, reconcile, spot_router, store
from funding_bot.trade.solana import b58 as B58
from funding_bot.trade.solana import ANSEM_MINT, MAINNET_GENESIS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT
from funding_bot.trade.types import InstrumentSpec

import test_marks as tm
import test_trade_engine as fx

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "instruments.json.example"
DQA9Q_HASH = "e17075932fe21844"             # inst_hash DQA9Q на коде фазы 1 (до schema 2), снят 13.09
ACCT = "hl:mainnet:0x" + "ab" * 20 + ":para"
T_14 = datetime.fromisoformat("2026-09-14T12:00:00+00:00").timestamp()


def _reg():
    return I.load_registry(EXAMPLE)


def _v2(**kw) -> InstrumentSpec:
    base = I.deal_spec(_reg().get("ansem_sol_para_v1"), account_id=ACCT, now=T_14)
    return replace(base, **kw) if kw else base


# ==== schema 1: DQA9Q не перепривязывается ==========================================================================
def test_dqa9q_inst_hash_and_json_unchanged(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    deal = tm.dqa9q(con)
    inst = eng.deal_instrument(con, deal)                      # вердикт миграции на лету (inst_json ещё нет)
    assert inst.schema == 1 and inst.inst_hash() == DQA9Q_HASH
    d = json.loads(inst.to_json())
    assert tuple(sorted(d)) == tuple(sorted(InstrumentSpec.FIELDS_V1)), "JSON schema 1 — ровно поля фазы 1"
    assert InstrumentSpec.from_json(inst.to_json()) == inst and inst.perp_scope is None
    # записанная бэкфиллом копия — тот же отпечаток
    eng.backfill_instruments(con, now=tm.NOW)
    raw = store.get_deal(con, "DQA9Q")["inst_json"]
    assert raw is not None and InstrumentSpec.from_json(raw).inst_hash() == DQA9Q_HASH
    # поля schema 2 в JSON schema 1 читатель не принимает во внимание (как код фазы 1)
    extra = dict(json.loads(raw), profile_id="x", perp_account="y", token_program="z")
    assert InstrumentSpec.from_json(json.dumps(extra)).inst_hash() == DQA9Q_HASH
    con.close()


@pytest.mark.parametrize("schema", [0, 3, True, "1", None])
def test_unknown_schema_refused(schema):
    d = _v2().as_dict()
    d["schema"] = schema
    with pytest.raises(ValueError):
        InstrumentSpec.from_json(json.dumps(d))


# ==== schema 2 ========================================================================================================
def test_v2_fields_roundtrip_and_units():
    s = _v2()
    assert (s.schema, s.profile_id, s.chain, s.network, s.genesis_hash) == (2, "sol_best_hyperliquid", "solana",
                                                                            "solana-mainnet", MAINNET_GENESIS)
    assert (s.token, s.token_dec, s.token_program, s.token_extensions) == (ANSEM_MINT, 6, TOKEN_2022_PROGRAM,
                                                                          ("metadataPointer", "tokenMetadata"))
    assert (s.quote_mint, s.quote_dec, s.quote_program) == (USDC_MINT, 6, TOKEN_PROGRAM)
    assert (s.perp_venue, s.perp_network, s.perp_dex, s.perp_symbol, s.perp_account) == (
        "hyperliquid", "mainnet", "para", "para:ANSEM", ACCT)
    assert s.fs == 1 and s.fp == 1 and s.m == 1 and s.verified and s.identity_status == "reviewed_override"
    assert s.perp_scope == f"hyperliquid|mainnet|{ACCT}|para|para:ANSEM"
    j = s.to_json()
    assert InstrumentSpec.from_json(j) == s and json.loads(j)["token"] == ANSEM_MINT, "mint с регистром"
    assert json.loads(j)["units_per_contract"] == "1" and isinstance(json.loads(j)["token_extensions"], list)


def test_v2_hash_only_by_identity():
    s = _v2()
    h = s.inst_hash()
    other_mint = B58.b58encode(bytes(32 - 1) + b"\x07")
    changed = [dict(token=other_mint), dict(token_dec=9), dict(token_program=TOKEN_PROGRAM),
               dict(token_extensions=("metadataPointer",)), dict(quote_mint=other_mint), dict(quote_dec=9),
               dict(genesis_hash=other_mint), dict(network="solana-devnet"), dict(perp_account=ACCT + "x"),
               dict(perp_dex="xyz", perp_symbol="xyz:ANSEM"), dict(perp_symbol="para:ANSEMX"),
               dict(perp_collateral="token:1"), dict(perp_collateral=None), dict(units_per_contract=D(1000)),
               dict(spot_units_per_token=D(2)), dict(profile_id="sol_other"), dict(instrument_id="ansem_v2"),
               dict(perp_network="testnet"), dict(perp_venue="hyperliquid2")]
    for kw in changed:
        assert replace(s, **kw).inst_hash() != h, kw
    # справка: продление решения владельца, срез меты, версия записи, проверка — отпечаток не меняют
    same = [dict(identity_status="verified_source"), dict(identity_expires_at="2026-12-01T00:00:00+00:00"),
            dict(identity_hash="sha256:" + "0" * 64), dict(perp_asset_id=180026), dict(instrument_version=2),
            dict(source="x"), dict(verified=False), dict(verified_ts=None), dict(why="y"), dict(perp_base_asset=None),
            dict(token_extensions=("tokenMetadata", "metadataPointer")), dict(units_per_contract=D("1.000"))]
    for kw in same:
        assert replace(s, **kw).inst_hash() == h, kw
    assert len(h) == 16 and h != DQA9Q_HASH


def test_v2_mint_case_is_identity():
    """Другой регистр mint — другие байты (S01/S02): либо не base58 32 байта (отказ), либо другой отпечаток."""
    s = _v2()
    low = replace(s, token=ANSEM_MINT.lower())          # тоже 32 байта base58 — но это ДРУГОЙ mint
    assert low.token == ANSEM_MINT.lower() and low.inst_hash() != s.inst_hash() and low.perp_scope == s.perp_scope
    for i, ch in enumerate(ANSEM_MINT):
        if ch.isalpha() and ch.swapcase() in B58.ALPHABET:
            flipped = ANSEM_MINT[:i] + ch.swapcase() + ANSEM_MINT[i + 1:]
            break
    try:
        v = replace(s, token=flipped)
    except ValueError:
        return
    assert v.inst_hash() != s.inst_hash() and v.token != s.token


@pytest.mark.parametrize("kw", [
    dict(token=ANSEM_MINT + "1"), dict(token=" " + ANSEM_MINT), dict(token=ANSEM_MINT[:-1] + "0"),
    dict(token_program="11111111111111111111111111111111"), dict(quote_mint=ANSEM_MINT),
    dict(perp_symbol="ANSEM"), dict(perp_symbol="para:"), dict(perp_dex="", perp_symbol="para:ANSEM"),
    dict(identity_status="same"), dict(identity_status=None), dict(perp_account=None), dict(perp_account=""),
    dict(genesis_hash=None), dict(chain="bsc"), dict(token_dec=True), dict(token_extensions=["metadataPointer"]),
    dict(units_per_contract=D(0)), dict(profile_id=None)])
def test_v2_refuses_broken_spec(kw):
    with pytest.raises(ValueError):
        _v2(**kw)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(units_per_contract=1.0), lambda d: d.update(token_extensions="metadataPointer"),
    lambda d: d.update(quote_dec="6"), lambda d: d.pop("perp_account"), lambda d: d.update(token=d["token"] + "1"),
    lambda d: d.update(perp_asset_id=1.5)])
def test_v2_from_json_refuses(mutate):
    d = _v2().as_dict()
    mutate(d)
    with pytest.raises(ValueError):
        InstrumentSpec.from_json(json.dumps(d))


# ==== реестр: пример для runtime/instruments.json (на VPS не кладём) ===================================================
def test_deploy_registry_example_owner_decision_13_09():
    reg = _reg()
    assert reg.sha256 is not None and [s.instrument_id for s in reg.specs] == ["ansem_sol_para_v1"]
    s = reg.get("ansem_sol_para_v1")
    i = s.identity
    assert (i.status, i.reviewed_by, i.review_reason) == ("reviewed_override", "owner",
                                                          "подтверждено владельцем в чате 13.09")
    assert I.ts_epoch(i.expires_at) - I.ts_epoch(i.reviewed_at) == timedelta(days=30).total_seconds()
    assert any(e.startswith("owner_chat_20260913") for e in i.evidence)
    assert (s.spot.mint, s.spot.genesis_hash, s.perp.fullcoin, s.units.status) == (ANSEM_MINT, MAINNET_GENESIS,
                                                                                  "para:ANSEM", "accepted")
    # live закрыт до шагов владельца: выключенная запись и не заданный счёт — честные причины, identity — нет
    assert I.entry_blockers(s, now=T_14) == ["запись выключена для live (enabled_for_live = false)",
                                             "perp: не задан account_id (scope счёта)"]
    late = I.entry_blockers(s, now=I.ts_epoch(i.expires_at) + 1)
    assert any("срок истёк" in x for x in late), "после 30 дней вход закрыт"
    assert I.protective_blockers(s) == [], "защитное действие по открытой позиции срок не блокирует (G05)"
    # тот же пример, но включённый и со счётом — запись допускает live (остальные ворота — отдельно)
    on = I.parse_instrument(dict(json.loads(EXAMPLE.read_text())["instruments"][0], enabled_for_live=True,
                                 perp=dict(json.loads(EXAMPLE.read_text())["instruments"][0]["perp"], account_id=ACCT)))
    assert I.entry_blockers(on, now=T_14) == []


def test_registry_lookup_is_case_exact_and_deal_spec_needs_account():
    reg = _reg()
    assert [s.instrument_id for s in reg.by_asset("sol", ANSEM_MINT)] == ["ansem_sol_para_v1"]
    assert reg.by_asset("solana", ANSEM_MINT.lower()) == () and reg.by_asset("solana", ANSEM_MINT.upper()) == ()
    assert reg.resolve("sol_best_hyperliquid", "para:ANSEM").instrument_id == "ansem_sol_para_v1"
    with pytest.raises(I.RegistryError, match="другим регистром"):
        reg.resolve("sol_best_hyperliquid", "para:ansem")
    s = reg.get("ansem_sol_para_v1")
    with pytest.raises(I.RegistryError, match="account_id"):
        I.deal_spec(s)
    with_acct = I.parse_instrument(dict(json.loads(EXAMPLE.read_text())["instruments"][0],
                                        perp=dict(json.loads(EXAMPLE.read_text())["instruments"][0]["perp"],
                                                  account_id=ACCT)))
    assert I.deal_spec(with_acct).perp_account == ACCT
    with pytest.raises(I.RegistryError, match="≠ account_id"):
        I.deal_spec(with_acct, account_id=ACCT + "x")
    # identity-поля записи → тот же отпечаток спецификации сделки, что и у записи с тем же счётом
    assert I.deal_spec(with_acct).inst_hash() == I.deal_spec(s, account_id=ACCT).inst_hash()


def test_stream1_pending_example_cannot_build_deal_spec():
    """Пример потока 1 (identity pending, genesis не задан) — спецификацию сделки не построить: сети нет."""
    reg = I.load_registry(ROOT / "tests" / "data" / "sol_hl" / "instruments.example.json")
    with pytest.raises(I.RegistryError, match="genesis_hash"):
        I.deal_spec(reg.get("ansem_sol_para_v1"), account_id=ACCT)


# ==== base58: один кодек ================================================================================================
def test_single_base58_codec():
    assert spot_router.b58encode is B58.b58encode and I.b58encode is B58.b58encode
    assert spot_router.B58_ALPHABET == I.B58_ALPHABET == B58.ALPHABET
    raw = bytes(range(32))
    assert spot_router.b58decode(B58.b58encode(raw)) == raw == I.b58decode(B58.b58encode(raw))
    for bad in ("", " 1", "0OIl", "abc!"):
        with pytest.raises(spot_router.PayloadError):
            spot_router.b58decode(bad)
    # кроме trade/solana/b58.py алфавит base58 нигде не определён заново
    src = ROOT / "src" / "funding_bot"
    hits = [p.relative_to(src).as_posix() for p in src.rglob("*.py") if B58.ALPHABET in p.read_text(encoding="utf-8")]
    assert hits == ["trade/solana/b58.py"], hits
    assert keys.b58encode is B58.b58encode
