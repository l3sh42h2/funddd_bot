"""evm_swap.py на Robinhood Chain (4663): роутер/spender/trim 4663 сняты живыми ответами OKX 13.09 — незнакомый адрес
там теперь «OKX сменила», как у BSC (стоп и отчёт), а не «не подтверждён»; нативная монета сети — своим именем
(BNB/ETH). Всё на чистых функциях/минимальных подделках — без сети."""
from decimal import Decimal
from types import SimpleNamespace
import pytest
from funding_bot import config
from funding_bot.trade.evm_swap import GuardError, OkxEvmSpot, check_approve, check_swap

WALLET = "0x" + "11" * 20
SOME_ROUTER = "0x" + "22" * 20
SOME_SPENDER = "0x" + "33" * 20
TOKEN_IN = "0x" + "44" * 20
TOKEN_OUT = "0x" + "55" * 20


def _w(a: str) -> str:
    return "0" * 24 + a.lower()[2:]


def _min_swap_resp(to: str) -> dict:
    """Минимальный /swap-ответ, которого хватает ровно до проверки router_allowed (гарды до неё — from/value)."""
    return {"routerResult": {}, "tx": {"from": WALLET, "value": "0", "to": to}}


# --- router / spender: «не подтверждён» (4663) против «OKX сменила» (BSC) --------------------------------------
def test_router_unknown_on_robinhood_is_stop_like_bsc():
    with pytest.raises(GuardError) as ei:
        check_swap(_min_swap_resp(SOME_ROUTER), chain="robinhood", wallet=WALLET, token_in=TOKEN_IN,
                   token_out=TOKEN_OUT, amount=1, slippage_pct=Decimal(3), impact_cap_pct=None, allow_tax=False)
    assert ei.value.guard == "router"
    msg = str(ei.value)
    assert "не подтверждён" not in msg and "меняла роутер" in msg


def test_router_message_unchanged_for_bsc():
    """BSC: allowlist непуст, а адрес незнакомый — старый текст «OKX меняла роутер», НЕ «не подтверждён»."""
    with pytest.raises(GuardError) as ei:
        check_swap(_min_swap_resp(SOME_ROUTER), chain="bsc", wallet=WALLET, token_in=TOKEN_IN, token_out=TOKEN_OUT,
                   amount=1, slippage_pct=Decimal(3), impact_cap_pct=None, allow_tax=False)
    assert ei.value.guard == "router"
    msg = str(ei.value)
    assert "не подтверждён" not in msg and "меняла роутер" in msg


def test_spender_unknown_on_robinhood_is_stop_like_bsc():
    amount = 10 ** 6
    resp = {"data": "0x095ea7b3" + _w(SOME_SPENDER) + f"{amount:064x}", "dexContractAddress": SOME_SPENDER,
            "gasLimit": "100000", "gasPrice": "1000000"}
    with pytest.raises(GuardError) as ei:
        check_approve(resp, chain="robinhood", token=TOKEN_IN, amount=amount)
    assert ei.value.guard == "spender"
    msg = str(ei.value)
    assert "не подтверждён" not in msg and "незнакомый spender" in msg


def test_spender_message_unchanged_for_bsc():
    amount = 10 ** 6
    resp = {"data": "0x095ea7b3" + _w(SOME_SPENDER) + f"{amount:064x}", "dexContractAddress": SOME_SPENDER,
            "gasLimit": "100000", "gasPrice": "1000000"}
    with pytest.raises(GuardError) as ei:
        check_approve(resp, chain="bsc", token=TOKEN_IN, amount=amount)
    assert ei.value.guard == "spender"
    msg = str(ei.value)
    assert "не подтверждён" not in msg and "незнакомый spender" in msg


# --- value guard: имя нативной монеты по сети ---------------------------------------------------------------
def test_value_guard_names_eth_on_robinhood():
    resp = {"routerResult": {}, "tx": {"from": WALLET, "value": "5", "to": SOME_ROUTER}}
    with pytest.raises(GuardError) as ei:
        check_swap(resp, chain="robinhood", wallet=WALLET, token_in=TOKEN_IN, token_out=TOKEN_OUT, amount=1,
                   slippage_pct=Decimal(3), impact_cap_pct=None, allow_tax=False)
    assert ei.value.guard == "value" and "ETH" in str(ei.value) and "BNB" not in str(ei.value)


def test_value_guard_names_bnb_on_bsc_unchanged():
    resp = {"routerResult": {}, "tx": {"from": WALLET, "value": "5", "to": SOME_ROUTER}}
    with pytest.raises(GuardError) as ei:
        check_swap(resp, chain="bsc", wallet=WALLET, token_in=TOKEN_IN, token_out=TOKEN_OUT, amount=1,
                   slippage_pct=Decimal(3), impact_cap_pct=None, allow_tax=False)
    assert ei.value.guard == "value" and "BNB" in str(ei.value)


# --- OkxEvmSpot: проводка chain/chain_id/стейбл для 4663 ------------------------------------------------------
def test_okx_evm_spot_wires_robinhood_chain_id_and_stable():
    spot = OkxEvmSpot(okx=None, rpc=SimpleNamespace(), owner_cfg=lambda: None, chain="robinhood", wallet=WALLET)
    assert spot.chain == "robinhood" and spot.ci == "4663" and spot.chain_id == 4663
    assert spot.stable == config.OKX_DEX_STABLES["4663"][0] and spot.stable_dec == 6
    assert spot._native_symbol() == "ETH"


def test_okx_evm_spot_bsc_unaffected():
    spot = OkxEvmSpot(okx=None, rpc=SimpleNamespace(), owner_cfg=lambda: None, chain="bsc", wallet=WALLET)
    assert spot.chain_id == 56 and spot._native_symbol() == "BNB"


def test_check_native_guard_message_native_symbol():
    spot_rh = OkxEvmSpot(okx=None, rpc=SimpleNamespace(native_balance=lambda addr: 0), owner_cfg=lambda: None,
                         chain="robinhood", wallet=WALLET)
    with pytest.raises(GuardError) as ei:
        spot_rh._check_native(100_000, 1_000_000, SimpleNamespace(get=lambda k: None))
    assert ei.value.guard == "native" and "ETH" in str(ei.value) and "BNB" not in str(ei.value)

    spot_bsc = OkxEvmSpot(okx=None, rpc=SimpleNamespace(native_balance=lambda addr: 0), owner_cfg=lambda: None,
                          chain="bsc", wallet=WALLET)
    with pytest.raises(GuardError) as ei2:
        spot_bsc._check_native(100_000, 1_000_000, SimpleNamespace(get=lambda k: None))
    assert ei2.value.guard == "native" and "BNB" in str(ei2.value)


# --- trim receiver: та же логика «не подтверждён», для полноты (обычно не достигается — router отказывает раньше) --
def test_trim_receiver_unknown_on_robinhood_is_stop():
    from funding_bot.trade.evm_swap import _check_trim
    tail = bytes.fromhex("777777771111") + b"\x80" + (10).to_bytes(25, "big") + bytes.fromhex("777777771111") \
        + (0).to_bytes(6, "big") + bytes.fromhex("aa" * 20)
    with pytest.raises(GuardError) as ei:
        _check_trim(tail, "robinhood", 1)
    assert ei.value.guard == "calldata_tail" and "не подтверждён" not in str(ei.value)


def test_trim_tail_live_4663_passes():
    """Хвост живого /swap 4663 (покупка FATCOIN за USDG, VPS 13.09) проходит: получатель и доля — как у BSC; trim
    с ожидаемым выходом ниже котировки — стоп."""
    from funding_bot.trade.evm_swap import _check_trim
    tail = bytes.fromhex("7777777711118000000000000000000000000000000001546629665f52dd985f"
                         "777777771111000000000064fa00a9ed787f3793db668bff3e6e6e7db0f92a1b")
    expect = int.from_bytes(tail[7:32], "big")
    _check_trim(tail, "robinhood", expect)
    with pytest.raises(GuardError):
        _check_trim(tail, "robinhood", expect + 1)
