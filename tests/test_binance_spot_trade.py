"""binance_spot_trade.py: подпись (общая с binance_trade.py), ворота режима, ордер/отмена/запрос, баланс,
settle_unknown. Фейковый транспорт — тот же принцип, что test_binance_trade.py/test_trade_aster.py: подпись
проверяется независимо от модуля. KEY/SECRET — заведомо фейковые демонстрационные строки."""
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
from funding_bot.trade import binance_spot_trade as bst
from funding_bot.trade.keys import ModeForbidden, redact_secrets

KEY, SECRET = "demo-binance-key-123", "demo-binance-secret-do-not-use-live"
SYM = "AIW3USDT"

EI_ROW = {"symbol": SYM, "baseAsset": "AIW3", "quoteAsset": "USDT", "status": "TRADING",
          "baseAssetPrecision": 8, "quoteAssetPrecision": 8,
          "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                     {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "100000"},
                     {"filterType": "NOTIONAL", "minNotional": "5"}]}


class FakeBinanceSpot(requests.Session):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, str, dict]] = []
        self.script: dict[tuple[str, str], list] = {}
        self.weight = 10
        self.balances_rows = [{"asset": "USDT", "free": "1000", "locked": "0"},
                              {"asset": "AIW3", "free": "0", "locked": "0"}]
        self.fill_cap: D | None = None
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.bad_sig: list[str] = []
        self.next_order_id = 500

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
        if path == "/api/v3/time":
            return self._resp(request, 200, {"serverTime": 1_700_000_000_000})
        if path == "/api/v3/exchangeInfo":
            sym = params.get("symbol")
            rows = [EI_ROW] if sym == SYM else []
            return self._resp(request, 200, {"symbols": rows})
        if path == "/api/v3/account":
            return self._resp(request, 200, {"balances": self.balances_rows})
        if path == "/api/v3/order" and method == "POST":
            return self._order(request, params)
        if path == "/api/v3/order" and method == "GET":
            cid = params.get("origClientOrderId")
            o = self.orders.get(cid)
            if o is None:
                return self._resp(request, 400, {"code": -2013, "msg": "Order does not exist."})
            return self._resp(request, 200, o)
        if path == "/api/v3/order" and method == "DELETE":
            cid = params.get("origClientOrderId")
            o = self.orders.get(cid)
            if o is None:
                return self._resp(request, 400, {"code": -2013, "msg": "Unknown order sent."})
            o = dict(o, status="CANCELED")
            return self._resp(request, 200, o)
        if path == "/api/v3/myTrades":
            return self._resp(request, 200, self.trades)
        raise AssertionError(f"unhandled {method} {path}")

    def _order(self, request, params):
        cid = params["newClientOrderId"]
        qty = D(params["quantity"])
        price = D(params["price"]) if "price" in params else D("0.05")
        oid = self.next_order_id
        self.next_order_id += 1
        filled = qty if self.fill_cap is None else min(qty, self.fill_cap)
        status = "FILLED" if filled == qty else ("EXPIRED" if filled == 0 else "EXPIRED")
        body = {"orderId": oid, "clientOrderId": cid, "status": status, "executedQty": str(filled),
                "cummulativeQuoteQty": str(price * filled)}
        self.orders[cid] = body
        if filled:      # держим баланс как реальная площадка: BUY — база растёт, квота падает (и наоборот)
            base, quote = params["symbol"][:-len("USDT")], "USDT"
            sign = 1 if params["side"] == "BUY" else -1
            self._adjust(base, sign * filled)
            self._adjust(quote, -sign * filled * price)
        return self._resp(request, 200, body)

    def _adjust(self, asset: str, delta: D) -> None:
        row = next((r for r in self.balances_rows if r.get("asset") == asset), None)
        if row is None:
            row = {"asset": asset, "free": "0", "locked": "0"}
            self.balances_rows.append(row)
        row["free"] = str(D(row["free"]) + delta)


@pytest.fixture(autouse=True)
def _clean_redaction():
    from funding_bot.trade import keys as K
    yield
    K._reset_redaction_for_tests()


@pytest.fixture
def fake():
    return FakeBinanceSpot()


def mk(fake, mode="live", paused=False, **kw) -> bst.BinanceSpotTrade:
    st = {"mode": mode, "paused": paused}
    return bst.BinanceSpotTrade(KEY, SECRET, mode_state=lambda: (st["mode"], st["paused"]), session=fake,
                                sleep=lambda s: None, **kw)


# --- подпись (общая функция с binance_trade.py, sign/build_query) ---------------------------------------
def test_signature_matches_independent_hmac_sha256(fake):
    t = mk(fake)
    t.filters(SYM)
    t.balance("USDT")
    assert not fake.bad_sig


def test_wrong_secret_is_caught_by_independent_check(fake):
    t = bst.BinanceSpotTrade(KEY, "wrong-secret", mode_state=lambda: ("live", False), session=fake, sleep=lambda s: None)
    t.balance("USDT")
    assert fake.bad_sig


def test_signing_reuses_shared_binance_hmac(fake):
    """binance_spot_trade делит sign()/build_query() с binance_trade — не второй параллельный HMAC."""
    from funding_bot.trade import binance_trade as bt
    assert bst.sign is bt.sign and bst.build_query is bt.build_query


# --- ворота режима ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode,paused,read_ok,send_ok", [
    ("dry", False, False, False), ("readonly", False, True, False),
    ("live", False, True, True), ("live", True, True, False),
])
def test_mode_gate_blocks_before_signing(fake, mode, paused, read_ok, send_ok):
    t = mk(fake, mode=mode, paused=paused)
    if read_ok:
        t.balance("USDT")
    else:
        with pytest.raises(ModeForbidden):
            t.balance("USDT")
    calls_before = len(fake.calls)
    if send_ok:
        t.order(SYM, "BUY", D(10), "fb-x-e01-c1-a1", price=D("0.05"))
        assert len(fake.calls) > calls_before
    else:
        with pytest.raises(ModeForbidden):
            t.order(SYM, "BUY", D(10), "fb-x-e01-c1-a1", price=D("0.05"))
        assert len(fake.calls) == calls_before


def test_no_keys_forbids_signed_call(fake):
    t = bst.BinanceSpotTrade(mode_state=lambda: ("live", False), session=fake, sleep=lambda s: None)
    with pytest.raises(ModeForbidden):
        t.balance("USDT")


# --- фильтры -----------------------------------------------------------------------------------------------
def test_filters_parsed_with_asset_precision(fake):
    t = mk(fake)
    f = t.filters(SYM)
    assert (f.tick, f.step, f.min_qty, f.min_notional) == (D("0.0001"), D(1), D(1), D(5))
    assert (f.base_asset, f.quote_asset, f.base_decimals, f.quote_decimals) == ("AIW3", "USDT", 8, 8)


# --- баланс: None ≠ 0 ---------------------------------------------------------------------------------------
def test_balance_unknown_is_none_never_zero(fake):
    t = mk(fake)
    assert t.balance("USDT") == D(1000)
    fake.balances_rows = []
    assert t.balance("USDT") is None
    fake.balances_rows = [{"asset": "BTC", "free": "1", "locked": "0"}]
    assert t.balance("USDT") is None
    fake.script[("GET", "/api/v3/account")] = [requests.ReadTimeout("x")]
    assert t.balance("USDT") is None


# --- ордер: MARKET и LIMIT+IOC -------------------------------------------------------------------------------
def test_order_limit_ioc_fill_statuses(fake):
    t = mk(fake)
    f = t.order(SYM, "BUY", D(100), "fb-x-e01-c1-a1", price=D("0.05"))
    assert (f.status, f.base_qty, f.quote_qty, f.avg_px) == ("FILLED", D(100), D(100) * D("0.05"), D("0.05"))
    assert isinstance(f.order_id, int) and f.err_code is None
    fake.fill_cap = D(40)
    f = t.order(SYM, "SELL", D(100), "fb-x-x01-c1-a1", price=D("0.0499"))
    assert (f.status, f.base_qty) == ("PARTIALLY_FILLED", D(40))
    fake.fill_cap = D(0)
    f = t.order(SYM, "SELL", D(100), "fb-x-x01-c2-a1", price=D("0.0499"))
    assert (f.status, f.base_qty) == ("EXPIRED", D(0))


def test_order_market_is_a_client_method_the_adapter_binding_never_calls(fake):
    """market — метод клиента (задача явно просит market/limit IOC): price=None -> MARKET, без ценового потолка."""
    t = mk(fake)
    f = t.order(SYM, "BUY", D(100), "fb-x-e02-c1-a1")   # price=None -> MARKET
    assert f.status == "FILLED"
    _, _, params = fake.calls[-1]
    assert params["type"] == "MARKET" and "price" not in params
    # cex_bindings.py (адаптерный слой) — см. test_binance_cex_adapters.py: quote() требует price_cap и всегда
    # шлёт LIMIT+IOC, никогда MARKET (докстринг binance_spot_trade.py объясняет почему).


def test_order_refuses_bad_input_before_anything(fake):
    t = mk(fake)
    bad = [("BOTH", D(1), D(1), "cid"), ("BUY", D(-1), D(1), "cid"), ("BUY", D(1), D(0), "cid"),
           ("BUY", D(1), D(1), "bad id with spaces")]
    for side, qty, px, cid in bad:
        with pytest.raises((ValueError, TypeError)):
            t.order(SYM, side, qty, cid, price=px)
    assert fake.n("POST", "/api/v3/order") == 0


@pytest.mark.parametrize("reply,status,code", [
    ((400, {"code": -2010, "msg": "Account has insufficient balance for requested action."}), "REJECTED", -2010),
    ((400, {"code": -1013, "msg": "Filter failure: NOTIONAL"}), "REJECTED", -1013),
    ((400, {"code": -1111, "msg": "Precision is over the maximum defined for this asset."}), "REJECTED", -1111),
    ((401, {"code": -1022, "msg": "Signature for this request is not valid."}), "REJECTED", -1022),
    ((503, {"code": -1001, "msg": "Internal error"}), "UNKNOWN", -1001),
])
def test_order_error_mapping_never_resends(fake, reply, status, code):
    t = mk(fake)
    fake.script[("POST", "/api/v3/order")] = [reply]
    f = t.order(SYM, "BUY", D(100), "fb-x-e01-c1-a1", price=D("0.05"))
    assert (f.status, f.err_code) == (status, code)
    assert fake.n("POST", "/api/v3/order") == 1


@pytest.mark.parametrize("exc", [requests.ReadTimeout("read timed out"), requests.ConnectionError("reset"),
                                 OSError("broken pipe")])
def test_order_transport_failure_is_unknown_with_sign_nonce(fake, exc):
    t = mk(fake)
    fake.script[("POST", "/api/v3/order")] = [exc]
    f = t.order(SYM, "BUY", D(100), "fb-x-e01-c1-a1", price=D("0.05"))
    assert f.status == "UNKNOWN" and f.order_id is None and f.sign_nonce > 0


# --- отмена --------------------------------------------------------------------------------------------------
def test_cancel_known_and_unknown_order(fake):
    t = mk(fake)
    t.order(SYM, "BUY", D(100), "fb-x-g01-c1-a1", price=D("0.04"))   # GTC-подобный: у нас всё IOC, но cancel универсален
    f = t.cancel(SYM, "fb-x-g01-c1-a1")
    assert f.status in ("FILLED", "PARTIALLY_FILLED", "EXPIRED")   # order_to_fill по статусу CANCELED+executedQty
    f = t.cancel(SYM, "fb-x-never-existed")
    assert f.status == "NOT_FOUND"


# --- rate limit / бан -----------------------------------------------------------------------------------------
def test_weight_header_budget_and_bans(fake):
    t = mk(fake)
    fake.weight = int(bst.SOFT_READ * bst.config.EXCHANGES["binance_spot"]["weight_limit"]) + 10
    t.balance("USDT")
    with pytest.raises(BudgetExceeded):
        t.balances()
    fake.weight = 1
    t.http.used_weight = 0
    t.balances()
    fake.script[("POST", "/api/v3/order")] = [(429, {"code": -1003, "msg": "too many requests"}, {"Retry-After": "3"})]
    f = t.order(SYM, "BUY", D(100), "fb-x-e01-c1-a1", price=D("0.05"))
    assert f.status == "REJECTED" and t.backoff_until > 0
    fake.script[("POST", "/api/v3/order")] = [(418, {"code": -1003, "msg": "banned"}, {"Retry-After": "120"})]
    t2 = mk(fake)
    t2.order(SYM, "BUY", D(100), "fb-x-e01-c2-a1", price=D("0.05"))
    with pytest.raises(BannedError):
        t2.order(SYM, "BUY", D(100), "fb-x-e01-c3-a1", price=D("0.05"))


# --- settle_unknown --------------------------------------------------------------------------------------------
def test_settle_unknown_finds_filled_order(fake):
    t = mk(fake)
    fake.orders["fb-x-e01-c1-a1"] = {"orderId": 77, "clientOrderId": "fb-x-e01-c1-a1", "status": "FILLED",
                                     "executedQty": "100", "cummulativeQuoteQty": "5"}
    f = t.settle_unknown(SYM, "fb-x-e01-c1-a1", base_before=D(0), since_ms=0)
    assert f.status == "FILLED" and f.base_qty == D(100)


def test_settle_unknown_not_found_needs_unchanged_balance(fake):
    t = mk(fake)
    t.filters(SYM)    # прогреть кэш base_asset для settle_unknown
    f = t.settle_unknown(SYM, "fb-x-never-sent", base_before=D(0), since_ms=0)
    assert f.status == "NOT_FOUND"
    fake.balances_rows = [{"asset": "AIW3", "free": "5", "locked": "0"}]   # баланс сдвинулся — не доказано
    f = t.settle_unknown(SYM, "fb-x-never-sent-2", base_before=D(0), since_ms=0)
    assert f.status == "UNKNOWN"


# --- секрет никогда не печатается -------------------------------------------------------------------------------
def test_secret_never_leaks(fake):
    t = mk(fake)
    assert SECRET not in repr(t) and SECRET not in str(t)
    text = f"trader={t!r} secret={t._secret!r} raw={SECRET}"
    assert SECRET not in redact_secrets(text)


def test_history_fills_and_account_identity(fake):
    t = mk(fake)
    fake.trades = [{"id": 1, "orderId": 77, "symbol": SYM, "isBuyer": True, "price": "0.05", "qty": "100",
                   "quoteQty": "5", "commission": "0.0001", "commissionAsset": "AIW3", "isMaker": False, "time": 123}]
    rows = t.history_fills(SYM, None)
    assert rows[0]["trade_id"] == 1 and rows[0]["side"] == "BUY"
    acct = t.history_account()
    assert acct.startswith("acct:v1:binance_spot:") and KEY not in acct
