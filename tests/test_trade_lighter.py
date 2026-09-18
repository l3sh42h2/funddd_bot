"""trade/lighter_trade.py на фейковом HTTP-транспорте — никаких реальных вызовов к mainnet.zklighter.elliot.ai.

Фикстуры (orderBookDetails/account/nextNonce/apikeys/tx/sendTx) — по «сырым» OpenAPI-схемам apidocs.lighter.xyz
(reference/*.md, читаны 18.09.2026), не по мысленной аналогии с другими биржами; сокращены до используемых полей.

FakeSigner в этом файле — тестовый дублёр интерфейса lighter_trade.SignerProtocol. Он НЕ выполняет настоящую
криптографию Lighter (Schnorr/ECgFp5/Poseidon2 — см. докстроку lighter_trade.py) и не должен использоваться ни
для чего, кроме проверки, что оркестрация (валидация → нонс → вызов подписанта → POST /sendTx → разбор ответа)
работает правильно ВОКРУГ места, где нужна настоящая подпись. Реальный LighterTrade(signer=None) (единственный
безопасный дефолт, используемый везде, кроме этого файла) отказывает раньше, чем дойдёт до сети — см.
test_ioc_without_signer_* / test_cancel_without_signer_raises / test_setup_without_signer_raises.
"""
from __future__ import annotations

import json
from decimal import Decimal as D

import pytest

from funding_bot.trade import lighter_trade as L
from funding_bot.trade.keys import ModeForbidden
from funding_bot.trade.types import Filters, PerpFill, PerpInstrument


# --------------------------------------------------------------------------------------------------------------
# фейковый транспорт (по образцу tests/test_lighter_venue.py::_R/_S для НЕСВЯЗАННОГО read-only модуля lighter.py)
# --------------------------------------------------------------------------------------------------------------
class _R:
    def __init__(self, body, code=200):
        self.status_code = code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class FakeSession:
    """.get/.post маршрутизируются в один handler(method, path, payload) -> _R; непредусмотренный вызов падает
    громко (AssertionError), а не тихо возвращает пустоту."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("/api/v1", 1)[1]
        self.calls.append(("GET", path, dict(params or {})))
        return self.handler("GET", path, dict(params or {}))

    def post(self, url, data=None, timeout=None):
        path = url.split("/api/v1", 1)[1]
        self.calls.append(("POST", path, dict(data or {})))
        return self.handler("POST", path, dict(data or {}))


class Clock:
    """Управляемые часы: __call__() — время, sleep(s) двигает время вперёд (без реального ожидания в тестах)."""

    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _route(script):
    """script: {(method, path): body | _R | Exception | callable(payload)->...}."""

    def handler(method, path, payload):
        key = (method, path)
        if key not in script:
            raise AssertionError(f"unexpected {method} {path} {payload!r}")
        item = script[key]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, _R):
            item = item(payload)
        if isinstance(item, _R):
            return item
        return _R(item)

    return handler


# --------------------------------------------------------------------------------------------------------------
# фикстуры ответов — поля по reference/orderbookdetails.md, account-1.md, nextnonce.md, apikeys.md, tx.md,
# sendtx.md (apidocs.lighter.xyz, 18.09.2026); значения придуманы (тестовые), форма — не придумана.
# --------------------------------------------------------------------------------------------------------------
def _obd(symbol="ETH", market_id=1, status="active", force_reduce_only=False, price_decimals=2, size_decimals=4,
        min_base_amount="0.0001", min_quote_amount="10"):
    return {"order_book_details": [{
        "symbol": symbol, "market_id": market_id, "status": status,
        "market_config": {"force_reduce_only": force_reduce_only},
        "price_decimals": price_decimals, "size_decimals": size_decimals,
        "min_base_amount": min_base_amount, "min_quote_amount": min_quote_amount,
        "taker_fee": "0.0005", "maker_fee": "0.0002",
    }]}


def _account(account_index=6, available_balance="19995", collateral="46342", positions=None):
    return {"code": 200, "total": 1, "next_cursor": "", "accounts": [{
        "code": 200, "account_index": account_index, "index": account_index, "l1_address": "0xabc", "status": 1,
        "available_balance": available_balance, "collateral": collateral,
        "cross_asset_value": available_balance, "positions": positions or [], "assets": [],
    }]}


def _position(symbol="ETH", sign=1, position="3.6956", **extra):
    row = {"market_id": 1, "symbol": symbol, "sign": sign, "position": position, "avg_entry_price": "3024.66",
          "position_value": "3019.92", "unrealized_pnl": "17.5", "realized_pnl": "2.0",
          "liquidation_price": "2500.0", "allocated_margin": "100", "initial_margin_fraction": "20.00",
          "open_order_count": 0, "pending_order_count": 0, "position_tied_order_count": 0, "margin_mode": 1,
          "margin_set_flag": 1, "total_discount": "0"}
    row.update(extra)
    return row


SESSION_KW = {"session": None}   # заменяется per-test


def make_trade(script, *, account_index=6, api_key_index=4, signer=None, clock=None):
    clock = clock or Clock()
    session = FakeSession(_route(script))
    return L.LighterTrade(account_index, api_key_index=api_key_index, signer=signer, session=session,
                          now=clock, sleep=clock.sleep), session, clock


# --------------------------------------------------------------------------------------------------------------
# instrument / filters
# --------------------------------------------------------------------------------------------------------------
def test_instrument_and_filters_from_orderbookdetails():
    t, s, _ = make_trade({("GET", "/orderBookDetails"): _obd()})
    inst = t.instrument("ETH")
    assert inst == PerpInstrument("ETH", "ETH", "ETH", D(1), "USDC", "PERPETUAL")
    filt = t.filters("ETH")
    assert filt.tick == D("0.01") and filt.step == D("0.0001")
    assert filt.min_qty == D("0.0001") and filt.min_notional == D("10")
    assert filt.tifs == frozenset({"IOC"})
    # orderBookDetails не публикует максимальный размер заявки (в отличие от Aster/Gate order_size_max) — не
    # выдумываем число, локального потолка нет.
    assert filt.max_qty_limit == D("Infinity") and filt.max_qty_market == D("Infinity")
    assert len(s.calls) == 1        # filters() переиспользовал кэш orderBookDetails, а не сделал новый запрос


def test_unknown_symbol_raises_lighter_error():
    t, _, _ = make_trade({("GET", "/orderBookDetails"): _obd(symbol="ETH")})
    with pytest.raises(L.LighterError):
        t.instrument("BTC")


# --------------------------------------------------------------------------------------------------------------
# позиция / баланс
# --------------------------------------------------------------------------------------------------------------
def test_position_long_and_short_sign():
    t, _, _ = make_trade({("GET", "/account"): _account(positions=[_position(sign=1, position="2.5")])})
    assert t.position("ETH") == D("2.5")
    t2, _, _ = make_trade({("GET", "/account"): _account(positions=[_position(sign=-1, position="2.5")])})
    assert t2.position("ETH") == D("-2.5")


def test_position_absent_symbol_is_flat_not_unknown():
    t, _, _ = make_trade({("GET", "/account"): _account(positions=[_position(symbol="BTC")])})
    assert t.position("ETH") == D(0)          # отсутствие в DetailedAccount.positions = флэт, не None


def test_available_margin_reads_available_balance():
    t, _, _ = make_trade({("GET", "/account"): _account(available_balance="123.45")})
    assert t.available_margin() == D("123.45")


def test_account_not_found_raises():
    t, _, _ = make_trade({("GET", "/account"): {"code": 200, "total": 0, "accounts": [], "next_cursor": ""}})
    with pytest.raises(L.LighterError):
        t.position("ETH")


def test_next_nonce_and_registered_public_key():
    t, _, _ = make_trade({
        ("GET", "/nextNonce"): {"code": 200, "nonce": 722},
        ("GET", "/apikeys"): {"code": 200, "api_keys": [{"account_index": 6, "api_key_index": 4, "nonce": 722,
                                                         "public_key": "0xpub", "transaction_time": 0}]},
    })
    assert t.next_nonce() == 722
    assert t.registered_public_key() == "0xpub"


# --------------------------------------------------------------------------------------------------------------
# обработка ошибок / rate limit — документация apidocs.lighter.xyz/docs/rate-limits и
# data-structures-constants-and-errors (18.09.2026)
# --------------------------------------------------------------------------------------------------------------
def test_429_bans_and_subsequent_call_is_refused_locally_then_recovers():
    calls = {"n": 0}

    def flaky(_payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return _R({}, code=429)
        return _R(_obd())

    t, s, clock = make_trade({("GET", "/orderBookDetails"): flaky})
    with pytest.raises(L.LighterApiError):
        t.instrument("ETH")
    assert calls["n"] == 1
    with pytest.raises(L.LighterApiError):
        t.instrument("ETH")               # пауза ещё не кончилась — новый HTTP-вызов не делается
    assert calls["n"] == 1                # именно локальный отказ, а не второй сетевой запрос
    clock.sleep(L.BAN_S + 1)              # пауза истекла — следующий вызов реально идёт в сеть и проходит
    t.instrument("ETH")
    assert calls["n"] == 2


def test_405_treated_like_429():
    t, _, _ = make_trade({("GET", "/orderBookDetails"): _R({}, code=405)})
    with pytest.raises(L.LighterApiError):
        t.instrument("ETH")


def test_5xx_retries_then_raises_net_error():
    n = {"c": 0}

    def always_500(_payload):
        n["c"] += 1
        return _R({}, code=503)

    t, _, clock = make_trade({("GET", "/orderBookDetails"): always_500})
    with pytest.raises(L.LighterNetError):
        t.instrument("ETH")
    assert n["c"] == t.http.retries        # ровно retries попыток, не одна и не бесконечно


def test_documented_error_code_raises_api_error_with_classification():
    body = {"code": 21507, "message": "account is below maintenance margin, can't execute transaction"}
    t, _, _ = make_trade({("GET", "/account"): _R(body, code=200)})
    with pytest.raises(L.LighterApiError) as exc:
        t.position("ETH")
    assert exc.value.code == 21507
    assert L.classify_code(21507) == "insufficient_funds_or_margin"


def test_classify_code_unknown_returns_none():
    assert L.classify_code(999999) is None
    assert L.classify_code(None) is None


# --------------------------------------------------------------------------------------------------------------
# client_order_index — наша детерминированная конвенция, не предписание Lighter (см. докстроку lighter_trade.py)
# --------------------------------------------------------------------------------------------------------------
def test_client_order_index_deterministic_in_range_and_distinct():
    a = L._client_order_index("fb-deal1-e01-c1-a1")
    b = L._client_order_index("fb-deal1-e01-c1-a1")
    c = L._client_order_index("fb-deal1-x01-c1-a1")
    assert a == b
    assert a != c
    assert 0 <= a < (1 << 48)


def test_client_order_index_requires_nonempty_string():
    with pytest.raises(L.LighterError):
        L._client_order_index("")


# --------------------------------------------------------------------------------------------------------------
# scale — точное масштабирование в целые base_amount/price по decimals рынка
# --------------------------------------------------------------------------------------------------------------
def test_scale_exact_and_rejects_off_step():
    assert L._scale(D("1.2345"), 4, "quantity") == 12345
    with pytest.raises(L.LighterError):
        L._scale(D("1.23456"), 4, "quantity")     # пятый знак не укладывается в size_decimals=4


# --------------------------------------------------------------------------------------------------------------
# подписанные операции без подписанта — валидация ДО отказа, а не вместо него
# --------------------------------------------------------------------------------------------------------------
def test_ioc_invalid_input_raises_before_signing_gate():
    t, _, _ = make_trade({("GET", "/orderBookDetails"): _obd()})
    with pytest.raises(L.LighterError) as exc:
        t.ioc("ETH", "HOLD", D("1"), D("3000"), "fb-d-e01-c1-a1", False)
    assert not isinstance(exc.value, L.SigningNotAvailable)   # конкретная ошибка входа, не общая «нет подписи»


def test_ioc_force_reduce_only_market_blocks_new_entry_before_signer():
    t, _, _ = make_trade({("GET", "/orderBookDetails"): _obd(force_reduce_only=True)})
    with pytest.raises(L.LighterError) as exc:
        t.ioc("ETH", "BUY", D("1"), D("3000"), "fb-d-e01-c1-a1", False)
    assert not isinstance(exc.value, L.SigningNotAvailable)


def test_ioc_without_signer_raises_signingnotavailable_on_valid_order():
    t, _, _ = make_trade({("GET", "/orderBookDetails"): _obd()})
    with pytest.raises(L.SigningNotAvailable) as exc:
        t.ioc("ETH", "BUY", D("1.0"), D("3000.00"), "fb-d-e01-c1-a1", False)
    assert "ioc" in exc.value.action


def test_cancel_without_signer_raises_signingnotavailable():
    t, _, _ = make_trade({("GET", "/orderBookDetails"): _obd()})
    with pytest.raises(L.SigningNotAvailable):
        t.cancel_order("ETH", 123)


def test_setup_without_signer_raises_signingnotavailable():
    t, _, _ = make_trade({})
    with pytest.raises(L.SigningNotAvailable):
        t.setup("ETH", 1, "ISOLATED")


def test_history_fills_raises_signingnotavailable_authenticated_endpoint():
    t, _, _ = make_trade({})
    with pytest.raises(L.SigningNotAvailable):
        t.history_fills("ETH", 0)
    assert t.history_account() == 6


def test_dry_mode_blocks_ioc_even_with_a_signer_configured():
    """Оборона в глубину: даже если подписанта когда-нибудь подключат, mode=dry не должен пропускать send."""

    class NeverCalledSigner:
        def sign_create_order(self, **kw):
            raise AssertionError("signer must not be reached in dry mode")

        def sign_cancel_order(self, **kw):
            raise AssertionError("signer must not be reached in dry mode")

    t, _, _ = make_trade({("GET", "/orderBookDetails"): _obd()}, signer=NeverCalledSigner())
    t.mode_state = lambda: ("dry", False)
    with pytest.raises(ModeForbidden):      # keys.ModeForbidden — подкласс KeysError, не наш LighterError
        t.ioc("ETH", "BUY", D("1"), D("3000"), "fb-d-e01-c1-a1", False)


# --------------------------------------------------------------------------------------------------------------
# dry-run вход+выход НА ФЕЙКОВОМ подписанте (не настоящая криптография — см. докстроку модуля/файла)
# --------------------------------------------------------------------------------------------------------------
class FakeSigner:
    """Тестовый дублёр SignerProtocol. tx_info — заведомо фейковая строка, никогда не проходящая настоящую
    проверку подписи Lighter; используется только для проверки, что LighterTrade корректно её строит/передаёт."""

    def __init__(self):
        self.create_calls = []
        self.cancel_calls = []

    def sign_create_order(self, **kw):
        self.create_calls.append(kw)
        return 14, json.dumps({"FAKE": True, **{k: v for k, v in kw.items() if isinstance(v, (int, bool, str))}})

    def sign_cancel_order(self, **kw):
        self.cancel_calls.append(kw)
        return 15, json.dumps({"FAKE": True, **kw})


def test_ioc_entry_and_exit_round_trip_on_fakes():
    signer = FakeSigner()
    sent_tx = []

    def send_tx(payload):
        sent_tx.append(payload)
        return _R({"code": 200, "tx_hash": "0xdeadbeef", "predicted_execution_time_ms": 1700000001000,
                  "volume_quota_remaining": 999})

    script = {
        ("GET", "/orderBookDetails"): _obd(),
        ("GET", "/nextNonce"): _R({"code": 200, "nonce": 1}),
        ("POST", "/sendTx"): send_tx,
    }
    t, s, clock = make_trade(script, signer=signer)

    entry = t.ioc("ETH", "BUY", D("1.0000"), D("3000.00"), "fb-d-e01-c1-a1", False)
    assert isinstance(entry, PerpFill) and entry.status == "UNKNOWN" and entry.client_id == "fb-d-e01-c1-a1"
    assert signer.create_calls[-1]["is_ask"] is False
    assert signer.create_calls[-1]["reduce_only"] is False
    assert signer.create_calls[-1]["base_amount"] == 10000       # 1.0000 * 10**4
    assert signer.create_calls[-1]["price"] == 300000            # 3000.00 * 10**2
    # POST /sendTx получил ровно (tx_type, tx_info), что вернул подписант — форма запроса из sendtx.md,
    # 18.09.2026 (x-www-form-urlencoded {tx_type, tx_info}), не JSON-тело.
    assert sent_tx[-1]["tx_type"] == 14
    assert json.loads(sent_tx[-1]["tx_info"])["market_index"] == 1
    assert json.loads(sent_tx[-1]["tx_info"])["FAKE"] is True

    exit_ = t.ioc("ETH", "SELL", D("1.0000"), D("2900.00"), "fb-d-x02-c1-a1", True)
    assert exit_.status == "UNKNOWN"
    assert signer.create_calls[-1]["is_ask"] is True
    assert signer.create_calls[-1]["reduce_only"] is True
    # клиентские индексы двух заявок различаются детерминированно от client_id, не совпадают случайно
    assert signer.create_calls[0]["client_order_index"] != signer.create_calls[1]["client_order_index"]


def test_cancel_order_round_trip_on_fakes():
    signer = FakeSigner()
    script = {
        ("GET", "/orderBookDetails"): _obd(),
        ("GET", "/nextNonce"): _R({"code": 200, "nonce": 5}),
        ("POST", "/sendTx"): _R({"code": 200, "tx_hash": "0xcafe", "predicted_execution_time_ms": 0,
                                "volume_quota_remaining": 1}),
    }
    t, _, _ = make_trade(script, signer=signer)
    result = t.cancel_order("ETH", 777)
    assert isinstance(result, PerpFill) and result.status == "UNKNOWN" and result.order_id == 777
    assert signer.cancel_calls[-1] == {"market_index": 1, "order_index": 777, "nonce": 5, "api_key_index": 4}


# --------------------------------------------------------------------------------------------------------------
# query() — чтение статуса транзакции по публичному /tx (без подписи)
# --------------------------------------------------------------------------------------------------------------
def _tx(status, **extra):
    row = {"code": 200, "hash": "0xabc", "type": 14, "info": "{}", "event_info": "{}", "status": status,
          "transaction_index": 1, "l1_address": "0xabc", "account_index": 6, "nonce": 5, "expire_at": 0,
          "block_height": 1, "queued_at": 0, "executed_at": 0, "sequence_index": 1, "parent_hash": "0x0",
          "api_key_index": 4, "transaction_time": 0, "committed_at": 0, "verified_at": 0}
    row.update(extra)
    return row


def test_query_failed_tx_is_rejected():
    t, _, _ = make_trade({("GET", "/tx"): _tx(L.TX_STATUS_FAILED)})
    fill = t.query("ETH", "0xabc")
    assert fill.status == "REJECTED"


@pytest.mark.parametrize("status", [L.TX_STATUS_PENDING, L.TX_STATUS_EXECUTED, L.TX_STATUS_PENDING_FINAL])
def test_query_pending_or_executed_stays_unknown_economics_not_confirmed(status):
    # Executed(2) тоже 'UNKNOWN': event_info не типизирован в OpenAPI (18.09.2026) — не вытаскиваем qty/price
    # из неподтверждённой схемы, см. докстроку lighter_trade.query().
    t, _, _ = make_trade({("GET", "/tx"): _tx(status)})
    fill = t.query("ETH", "0xabc")
    assert fill.status == "UNKNOWN"


# --------------------------------------------------------------------------------------------------------------
# venue / регистрация в production_registry
# --------------------------------------------------------------------------------------------------------------
def test_venue_and_ioc_partial_terminal_attrs():
    assert L.LighterTrade.venue == "lighter"
    assert L.LighterTrade.ioc_partial_terminal is False


def test_lighter_registered_in_production_registry():
    from funding_bot.trade.adapters.registry import production_registry
    reg = production_registry()
    assert "lighter" in reg._factories
    # регистрация не означает live: FuturesAdapter поверх LighterTrade(signer=None) отказывает на send (доказано
    # в test_ioc_without_signer_raises_signingnotavailable_on_valid_order выше), сама регистрация — не сеть.
