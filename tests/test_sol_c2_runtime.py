"""Шаг C2: выбор ног по сделке и профилю (runtime) — связка сделки из её замороженной спецификации, реестр ног с
ленивой сборкой и изоляцией (сбой SOL/HL не роняет BSC), прежний legs(sim) — ровно старая связка, фабрика SOL/HL в
dry не читает окружение (M02), ключи связки грузятся один раз (load_sol_hl стирает приватные переменные)."""
import json
import pytest
from funding_bot.trade import keys as K, owner, runtime as RT
from funding_bot.trade.owner import LEGACY_PROFILE, SOL_HL
import sol_hl_fixtures as F

V2 = json.dumps({"schema": 2, "profile_id": SOL_HL, "chain": "solana"})
V1 = json.dumps({"schema": 1, "chain": "bsc"})


@pytest.mark.parametrize("row,prof", [
    ({"chain": "bsc", "inst_json": None, "sim": 0}, LEGACY_PROFILE),
    ({"chain": "bsc", "inst_json": V1, "sim": 0}, LEGACY_PROFILE),          # DQA9Q: schema 1
    ({"chain": "solana", "inst_json": V2, "sim": 0}, SOL_HL),
    ({"chain": "solana", "inst_json": None, "sim": 0}, SOL_HL),             # Solana без schema 2 — не BSC-ноги
    ({"chain": "bsc", "inst_json": "{битый", "sim": 0}, LEGACY_PROFILE),
])
def test_profile_of_deal(row, prof):
    assert RT.profile_of_deal(row) == prof and RT.is_sol_deal(row) == (prof == SOL_HL)


def test_registry_legacy_call_is_old_contract_and_sol_failure_is_isolated():
    seen = []

    def boom(sim):
        seen.append(sim)
        raise RuntimeError("нет RPC: https://rpc.example/?api-key=supersecretvalue123")
    reg = RT.RuntimeRegistry(lambda sim: ("bsc", sim), {SOL_HL: boom})
    assert reg(True) == ("bsc", True) and reg(False) == ("bsc", False)
    assert reg.for_deal({"chain": "bsc", "inst_json": None, "sim": 1}) == ("bsc", True)
    with pytest.raises(RT.ProfileDown) as e:
        reg.for_deal({"chain": "solana", "inst_json": V2, "sim": 0})
    assert "supersecretvalue123" not in str(e.value) and "supersecretvalue123" not in reg.last_error[SOL_HL]
    with pytest.raises(RT.ProfileDown):                                # повтор — снова пробует (не кэширует сбой)
        reg.for_profile(SOL_HL, False)
    assert seen == [False, False] and reg(False) == ("bsc", False)
    assert reg.health()[SOL_HL] is not None and reg.health()[LEGACY_PROFILE] is None


def test_registry_builds_lazily_once():
    made = []
    reg = RT.RuntimeRegistry(None, {SOL_HL: lambda sim: made.append(sim) or object()})
    assert made == []
    a = reg.for_profile(SOL_HL, True)
    assert reg.for_profile(SOL_HL, True) is a and made == [True]
    with pytest.raises(RT.ProfileDown):
        reg.for_profile(LEGACY_PROFILE, True)
    assert reg(True) is None


def test_legs_of_with_old_legs_callable():
    legs = lambda sim: ("bsc", sim)                                   # noqa: E731 — прежний legs(sim) без реестра
    assert RT.legs_of(legs, {"chain": "bsc", "inst_json": V1, "sim": 0}) == ("bsc", False)
    with pytest.raises(RT.ProfileDown):                                # SOL-сделке ноги BSC не отдаются
        RT.legs_of(legs, {"chain": "solana", "inst_json": V2, "sim": 0})


def test_sol_factory_dry_reads_no_environment(tmp_path):
    p = F.write(tmp_path, F.sol_toml({"": {"mode": '"dry"'}}))
    cfg = owner.load(p)
    env = F.SpyEnv({"SOLANA_SECRET_B58": F.SOL_SECRET_B58, "HL_AGENT_PRIVATE_KEY": F.fake_key(5)})
    f = RT.SolFactory(lambda: cfg, None, keys_mode="live", environ=env)
    assert f.keys(cfg) is None and f(False) is None and env.names() == set()


def test_sol_factory_loads_profile_keys_once(tmp_path, monkeypatch):
    p = F.write(tmp_path, F.sol_toml({"": {"mode": '"readonly"'}}))
    cfg = owner.load(p)
    calls = []
    monkeypatch.setattr(K, "load_sol_hl", lambda c, m, e=None: calls.append(m) or object())
    f = RT.SolFactory(lambda: cfg, None, keys_mode="readonly", environ={})
    k1, k2 = f.keys(cfg), f.keys(cfg)
    assert k1 is k2 and calls == ["readonly"]


def test_hl_account_id_scope():
    a = RT.hl_account_id("mainnet", "0xABCDEF0000000000000000000000000000000001",
                         "0xABCDEF0000000000000000000000000000000002", "para")
    assert a == ("hyperliquid:mainnet:0xabcdef0000000000000000000000000000000001:"
                 "0xabcdef0000000000000000000000000000000002:para")
