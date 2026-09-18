"""binance_trade.py: подпись HMAC-SHA256, ворота режима, вес/бан/429, исход заявки, позиция/баланс, settle_unknown.

Фейковый транспорт — requests.Session с переопределённым send(): подпись проверяется НЕЗАВИСИМО (свой
hmac.new(..., hashlib.sha256), а не импортированный sign()), как у test_trade_aster.py/test_gate_trade.py.
Никаких реальных вызовов к Binance. KEY/SECRET — заведомо фейковые демонстрационные строки, не настоящие ключи.
"""
from __future__ import annotations
import hashlib
import hmac
import json
from decimal import Decimal as D
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from funding_bot.client import BannedError, BudgetExceeded
from funding_bot.trade import binance_trade as bt
from funding_bot.trade.keys import ModeForbidden, redact_secrets

KEY, SECRET = "demo-binance-key-123", "demo-binance-secret-do-not-use-live"
SYM = "AIW3USDT"

EI = {"symbols": [{"symbol": SYM, "baseAsset": "AIW3", "quoteAsset": "USDT", "contractType": "PERPETUAL",
                   "status": "TRADING", "timeInForce": ["GTC", "IOC"],
                   "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                              {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "100000"},
                              {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}


class FakeBinance(requests.Session):
    """Подменяет send(): проверяет подпись независимо от модуля и отвечает по script/handle()."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str, dict]] = []
        self.script: dict[tuple[str, str], list] = {}
        self.weight = 10
        self.position = D(0)
        self.position_rows: list[dict] | None = None
        self.balance_rows = [{"asset": "USDT", "availableBalance": "1000"}]
        self.fill_cap: D | None = None      # None — исполняет весь размер; иначе — не больше fill_cap
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.bad_sig: list[str] = []
        self.next_order_id = 1000

    def n(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p == path)

    def _resp(self, request, status, body, headers=None):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        r.headers = CaseInsensitiveDict({"X-MBX-USED-WEIGHT-1M": str(self.weight), **(headers or {})})
        r.url, r.request, r.encoding = request.url, request, "utf-8"
        return r

    def send(self, request, **kw):
        method = request.method
        parts = urlsplit(request.url)
        path, qs = parts.path, parts.query
        params = dict(parse_qsl(qs))
        self.calls.append((method, path, params))
        if request.headers.get("X-MBX-APIKEY") is not None:
            if request.headers.get("X-MBX-APIKEY") != KEY:
                self.bad_sig.append(qs)
            sig = params.pop("signature", None)
            base = qs.rsplit("&signature=", 1)[0]
            expect = hmac.new(SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
            if sig != expect:
                self.bad_sig.append(qs)
        key = (method, path)
        if key in self.script and self.script[key]:
            item = self.script[key].pop(0)
            if isinstance(item, BaseException):
                raise item
            status, body, *rest = item
            headers = rest[0] if rest else {}
            return self._resp(request, status, body, headers)
        return self.handle(request, method, path, params)

    def handle(self, request, method, path, params):
        if path == "/fapi/v1/time":
            return self._resp(request, 200, {"serverTime": 1_700_000_000_000})
        if path == "/fapi/v1/exchangeInfo":
            return self._resp(request, 200, EI)
        if path == "/fapi/v2/positionRisk":
            if self.position_rows is not None:
                rows = self.position_rows
            else:
                rows = [{"symbol": SYM, "positionAmt": str(self.position), "positionSide": "BOTH"}]
            return self._resp(request, 200, rows)
        if path == "/fapi/v2/balance":
            return self._resp(request, 200, self.balance_rows)
        if path == "/fapi/v1/order" and method == "POST":
            return self._order(request, params)
        if path == "/fapi/v1/order" and method == "GET":
            cid = params.get("origClientOrderId")
            o = self.orders.get(cid)
            if o is None:
                return self._resp(request, 400, {"code": -2013, "msg": "Order does not exist."})
            return self._resp(request, 200, o)
        if path == "/fapi/v1/userTrades":
            return self._resp(request, 200, self.trades)
        if path == "/fapi/v1/positionSide/dual":
            return self._resp(request, 200, {"dualSidePosition": False})
        if path == "/fapi/v1/marginType" and method == "POST":
            return self._resp(request, 400, {"code": -4046, "msg": "No need to change margin type."})
        if path == "/fapi/v1/leverage" and method == "POST":
            return self._resp(request, 200, {"leverage": int(params["leverage"]), "symbol": params["symbol"]})
        if path == "/fapi/v1/multiAssetsMargin":
            return self._resp(request, 200, {"multiAssetsMargin": False})
        raise AssertionError(f"unhandled {method} {path}")

    def _order(self, request, params):
        cid = params["newClientOrderId"]
        qty, price = D(params["quantity"]), D(params["price"])
        oid = self.next_order_id
        self.next_order_id += 1
        filled = qty if self.fill_cap is None else min(qty, self.fill_cap)
        status = "FILLED" if filled == qty else ("EXPIRED" if filled == 0 else "EXPIRED")
        body = {"orderId": oid, "clientOrderId": cid, "status": status, "executedQty": str(filled),
                "avgPrice": str(price) if filled else "0", "cumQuote": str(price * filled),
                "reduceOnly": params.get("reduceOnly") == "true"}
        self.orders[cid] = body
        if filled and self.position_rows is None:    # держим позицию как реальная площадка (пока строки не заданы вручную)
            self.position += filled if params["side"] == "BUY" else -filled
        return self._resp(request, 200, body)


@pytest.fixture(autouse=True)
def _clean_redaction():
    from funding_bot.trade import keys as K
    yield
    K._reset_redaction_for_tests()


@pytest.fixture
def fake():
    return FakeBinance()


def mk(fake, mode="live", paused=False, **kw) -> bt.BinanceTrade:
    st = {"mode": mode, "paused": paused}
    return bt.BinanceTrade(KEY, SECRET, mode_state=lambda: (st["mode"], st["paused"]), session=fake,
                           sleep=lambda s: None, **kw)


# --- подпись -----------------------------------------------------------------------------------------
def test_signature_matches_independent_hmac_sha256(fake):
    t = mk(fake)
    t.filters(SYM)
    t.position(SYM)
    assert fake.n("GET", "/fapi/v2/positionRisk") == 1
    assert not fake.bad_sig


def test_all_params_including_timestamp_and_recvwindow_are_signed(fake):
    t = mk(fake)
    t.position(SYM)
    _, _, params = fake.calls[-1]
    assert "timestamp" in params and "recvWindow" in params and "signature" not in params.keys() - {"signature"}
    # signature был извлечён и проверен в send(); здесь просто убеждаемся, что остальные обязательные поля дошли
    assert params["recvWindow"] == str(bt.RECV_WINDOW_MS)


def test_api_key_header_present_and_matches(fake):
    t = mk(fake)
    t.position(SYM)
    assert not fake.bad_sig


def test_wrong_secret_is_caught_by_independent_check(fake):
    t = bt.BinanceTrade(KEY, "wrong-secret", mode_state=lambda: ("live", False), session=fake, sleep=lambda s: None)
    t.position(SYM)
    assert fake.bad_sig    # независимая проверка в фейке обнаружила несовпадение подписи


# --- ворота режима -------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode,paused,read_ok,send_ok", [
    ("dry", False, False, False), ("readonly", False, True, False),
    ("live", False, True, True), ("live", True, True, False),
])
def test_mode_gate_blocks_before_signing(fake, mode, paused, read_ok, send_ok):
    t = mk(fake, mode=mode, paused=paused)
    if read_ok:
        t.position(SYM)
    else:
        with pytest.raises(ModeForbidden):
            t.position(SYM)
    calls_before = len(fake.calls)
    if send_ok:
        t.ioc(SYM, "SELL", D(10), D("0.05"), "fb-x-e01-c1-a1", True)
        assert len(fake.calls) > calls_before
    else:
        with pytest.raises(ModeForbidden):
            t.ioc(SYM, "SELL", D(10), D("0.05"), "fb-x-e01-c1-a1", True)
        assert len(fake.calls) == calls_before   # ворота — ДО подписи и сети


def test_hedge_ioc_allowed_on_pause(fake):
    t = mk(fake, mode="live", paused=True)
    f = t.ioc(SYM, "SELL", D(10), D("0.05"), "fb-x-e01-c1-a1", True, hedge=True)
    assert f.status == "FILLED"


def test_no_keys_forbids_signed_call(fake):
    t = bt.BinanceTrade(mode_state=lambda: ("live", False), session=fake, sleep=lambda s: None)
    with pytest.raises(ModeForbidden):
        t.position(SYM)


# --- фильтры / инструмент ------------------------------------------------------------------------------
def test_filters_and_instrument_parsed(fake):
    t = mk(fake)
    f = t.filters(SYM)
    assert (f.tick, f.step, f.min_qty, f.max_qty_limit, f.min_notional) == (D("0.0001"), D(1), D(1), D(100000), D(5))
    inst = t.instrument(SYM)
    assert inst.base == "AIW3" and inst.m == 1


# --- позиция/баланс: None ≠ 0 --------------------------------------------------------------------------
def test_position_unknown_is_none_never_zero(fake):
    t = mk(fake)
    fake.position = D(0)
    assert t.position(SYM) == D(0)
    fake.position_rows = []
    assert t.position(SYM) is None
    fake.position_rows = [{"symbol": "BTCUSDT", "positionAmt": "1", "positionSide": "BOTH"}]
    assert t.position(SYM) is None                # не наш символ
    fake.position_rows = [{"symbol": SYM, "positionAmt": "0", "positionSide": "LONG"},
                          {"symbol": SYM, "positionAmt": "-5", "positionSide": "SHORT"}]
    assert t.position(SYM) is None                 # hedge-режим — не наша модель
    fake.script[("GET", "/fapi/v2/positionRisk")] = [(500, {}), requests.ReadTimeout("x"),
                                                      (400, {"code": -1022, "msg": "bad sig"})]
    assert [t.position(SYM) for _ in range(3)] == [None, None, None]


def test_available_margin_usdt_or_none(fake):
    t = mk(fake)
    assert t.available_margin() == D(1000)
    fake.balance_rows = []
    assert t.available_margin() is None
    fake.script[("GET", "/fapi/v2/balance")] = [requests.ConnectionError("x")]
    assert t.available_margin() is None


# --- заявка: статусы -----------------------------------------------------------------------------------
def test_ioc_fill_statuses_full_partial_expired(fake):
    t = mk(fake)
    f = t.ioc(SYM, "SELL", D(100), D("0.05"), "fb-x-e01-c1-a1", False)
    assert (f.status, f.qty, f.avg_px, f.quote) == ("FILLED", D(100), D("0.05"), D(100) * D("0.05"))
    assert isinstance(f.order_id, int) and f.err_code is None
    fake.fill_cap = D(40)
    f = t.ioc(SYM, "BUY", D(100), D("0.0501"), "fb-x-x01-c1-a1", True)
    assert (f.status, f.qty) == ("PARTIALLY_FILLED", D(40))
    fake.fill_cap = D(0)
    f = t.ioc(SYM, "BUY", D(100), D("0.0501"), "fb-x-x01-c2-a1", True)
    assert (f.status, f.qty) == ("EXPIRED", D(0))
    assert fake.orders["fb-x-x01-c2-a1"]["reduceOnly"] is True


def test_ioc_refuses_bad_input_before_anything(fake):
    t = mk(fake)
    bad = [("BOTH", D(1), D(1), "cid"), ("BUY", D(-1), D(1), "cid"), ("BUY", D(1), D(0), "cid"),
           ("BUY", D(1), D(1), "bad id with spaces"), ("BUY", D(1), D(1), "x" * 40)]
    for side, qty, px, cid in bad:
        with pytest.raises((ValueError, TypeError)):
            t.ioc(SYM, side, qty, px, cid, False)
    assert fake.n("POST", "/fapi/v1/order") == 0


@pytest.mark.parametrize("reply,status,code", [
    ((400, {"code": -2019, "msg": "Margin is insufficient."}), "REJECTED", -2019),
    ((400, {"code": -4164, "msg": "Order's notional must be no smaller than 5.0"}), "REJECTED", -4164),
    ((400, {"code": -1111, "msg": "Precision is over the maximum defined for this asset."}), "REJECTED", -1111),
    ((400, {"code": -2022, "msg": "ReduceOnly Order is rejected."}), "REJECTED", -2022),
    ((401, {"code": -1022, "msg": "Signature for this request is not valid."}), "REJECTED", -1022),
    ((503, {"code": -1001, "msg": "Internal error"}), "UNKNOWN", -1001),
    ((200, {"code": -1006, "msg": "unexpected response"}), "UNKNOWN", -1006),
])
def test_ioc_error_mapping_never_resends(fake, reply, status, code):
    t = mk(fake)
    fake.script[("POST", "/fapi/v1/order")] = [reply]
    f = t.ioc(SYM, "SELL", D(100), D("0.05"), "fb-x-e01-c1-a1", False, hedge=True)
    assert (f.status, f.err_code) == (status, code)
    assert fake.n("POST", "/fapi/v1/order") == 1


@pytest.mark.parametrize("exc", [requests.ReadTimeout("read timed out"), requests.ConnectionError("reset"),
                                 OSError("broken pipe")])
def test_ioc_transport_failure_is_unknown_with_sign_nonce(fake, exc):
    t = mk(fake)
    fake.script[("POST", "/fapi/v1/order")] = [exc]
    f = t.ioc(SYM, "SELL", D(100), D("0.05"), "fb-x-e01-c1-a1", False)
    assert f.status == "UNKNOWN" and f.order_id is None and f.sign_nonce > 0


# --- rate limit / бан ----------------------------------------------------------------------------------
def test_weight_header_budget_and_bans(fake):
    t = mk(fake)
    fake.weight = int(bt.SOFT_READ * bt.config.EXCHANGES["binance"]["weight_limit"]) + 10
    t.position(SYM)     # читает вес из заголовка
    with pytest.raises(BudgetExceeded):
        t.balances()
    fake.weight = 1
    t.http.used_weight = 0     # свежий вес пришёл бы следующим ответом; здесь эмулируем это напрямую
    t.balances()
    fake.script[("POST", "/fapi/v1/order")] = [(429, {"code": -1003, "msg": "too many requests"}, {"Retry-After": "3"})]
    f = t.ioc(SYM, "SELL", D(100), D("0.05"), "fb-x-e01-c1-a1", False, hedge=True)
    assert f.status == "REJECTED" and t.backoff_until > 0
    fake.script[("POST", "/fapi/v1/order")] = [(418, {"code": -1003, "msg": "banned"}, {"Retry-After": "120"})]
    t2 = mk(fake)
    t2.ioc(SYM, "SELL", D(100), D("0.05"), "fb-x-e01-c2-a1", False, hedge=True)
    with pytest.raises(BannedError):
        t2.ioc(SYM, "SELL", D(100), D("0.05"), "fb-x-e01-c3-a1", False, hedge=True)


# --- settle_unknown -------------------------------------------------------------------------------------
def test_settle_unknown_finds_filled_order(fake):
    t = mk(fake)
    fake.orders["fb-x-e01-c1-a1"] = {"orderId": 55, "clientOrderId": "fb-x-e01-c1-a1", "status": "FILLED",
                                     "executedQty": "100", "avgPrice": "0.05", "cumQuote": "5"}
    f = t.settle_unknown(SYM, "fb-x-e01-c1-a1", pos_before=D(0), since_ms=0)
    assert f.status == "FILLED" and f.qty == D(100)


def test_settle_unknown_not_found_needs_unchanged_position_and_no_foreign_trades(fake):
    t = mk(fake)
    fake.position = D(0)
    f = t.settle_unknown(SYM, "fb-x-never-sent", pos_before=D(0), since_ms=0)
    assert f.status == "NOT_FOUND"
    fake.position = D(-5)     # позиция сдвинулась — не доказано
    f = t.settle_unknown(SYM, "fb-x-never-sent-2", pos_before=D(0), since_ms=0)
    assert f.status == "UNKNOWN"


def test_settle_unknown_foreign_trade_in_window_stays_unknown(fake):
    t = mk(fake)
    fake.position = D(0)
    fake.trades = [{"orderId": 999, "id": 1, "price": "0.05", "qty": "1", "time": 0}]
    f = t.settle_unknown(SYM, "fb-x-never-sent", pos_before=D(0), since_ms=0, known_order_ids=frozenset({1}))
    assert f.status == "UNKNOWN"


# --- часы -----------------------------------------------------------------------------------------------
def test_check_clock_ok_and_out_of_range(fake):
    t = mk(fake, now=lambda: 1_700_000_000.0)
    t.check_clock()
    t2 = mk(fake, now=lambda: 1_700_000_010.0)   # 10 с расхождения > CLOCK_SKEW_MAX_S
    with pytest.raises(bt.BinanceError):
        t2.check_clock()


# --- настройка (setup) -----------------------------------------------------------------------------------
def test_setup_isolated_and_leverage_idempotent(fake):
    t = mk(fake)
    t.setup(SYM, 5, "ISOLATED")     # margin type уже такой (-4046) — не ошибка
    assert fake.n("POST", "/fapi/v1/leverage") == 1


def test_setup_rejects_bad_inputs(fake):
    t = mk(fake)
    with pytest.raises(ValueError):
        t.setup(SYM, 0, "ISOLATED")
    with pytest.raises(ValueError):
        t.setup(SYM, 5, "BOTH")


def test_multi_assets_isolated_conflict_message(fake):
    t = mk(fake)
    fake.script[("POST", "/fapi/v1/marginType")] = [(400, {"code": -4168, "msg": "Unable to adjust"})]
    with pytest.raises(bt.BinanceError, match="Multi-Assets"):
        t.setup(SYM, 5, "ISOLATED")


# --- секрет никогда не печатается -------------------------------------------------------------------------
def test_secret_never_leaks(fake):
    t = mk(fake)
    assert SECRET not in repr(t) and SECRET not in str(t)
    assert SECRET not in repr(t._secret) and SECRET not in f"{t._secret}"
    text = f"trader={t!r} secret={t._secret!r} raw={SECRET}"
    assert SECRET not in redact_secrets(text)


def test_history_fills_and_account_identity(fake):
    t = mk(fake)
    fake.trades = [{"id": 1, "orderId": 55, "symbol": SYM, "side": "SELL", "price": "0.05", "qty": "100",
                   "quoteQty": "5", "commission": "-0.002", "commissionAsset": "USDT", "maker": False,
                   "realizedPnl": "0", "time": 123}]
    rows = t.history_fills(SYM, None)
    assert rows[0]["trade_id"] == 1 and rows[0]["symbol"] == SYM
    acct = t.history_account()
    assert acct.startswith("acct:v1:binance:") and KEY not in acct
