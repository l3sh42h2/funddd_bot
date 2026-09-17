"""Перп-нога Aster v3: векторы подписи EIP-712, тело формы байт в байт, ворота режима, разбор ошибок и неизвестный
исход — без сети. FakeAster — подделка requests.Session: запрос проходит настоящую подготовку requests (prepare), а
подпись проверяется НЕЗАВИСИМО от кода модуля (свой typed data + recover_message).

Ключ — ПУБЛИЧНЫЙ демонстрационный privateKey из документации Aster (aster-futures-v3.md, стр. 277, «for demonstration
purposes only»): его адрес равен демо-signer из той же таблицы. Средств за ним нет, настоящих ключей тут нет."""
import json
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit
import pytest
import requests
from requests.structures import CaseInsensitiveDict

eth_account = pytest.importorskip("eth_account")
from eth_account import Account                                   # noqa: E402
from eth_account.messages import encode_typed_data                # noqa: E402
from eth_utils import keccak                                      # noqa: E402

from funding_bot.client import BannedError, BudgetExceeded        # noqa: E402
from funding_bot.trade import aster_trade as at, keys, store, tconfig   # noqa: E402
from funding_bot.trade.aster_trade import (AsterApiError, AsterSigner, AsterTrade, MonotonicNonce)   # noqa: E402
from funding_bot.trade.keys import Keys, KeyMismatch, ModeForbidden, SignerKey   # noqa: E402
from funding_bot.trade.types import PerpLeg                       # noqa: E402

D = Decimal
DEMO_KEY = "0x4fd0a42218f3eae43a6ce26d22544e986139a01e5b34a62db53757ffca81bae1"   # публичный демо-ключ документации
DEMO_SIGNER = "0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0"
DEMO_USER = "0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e"
BASE = "https://fapi.asterdex.com"
SYM = "AIW3USDT"

# отчёт aster §1/§3 [LOCAL]: посчитано офлайн eth_account 0.13.7 + hexbytes 1.3.1
DOMSEP = "0xa95d0a7a6f3f17fcebb0c2336645385ce79cde7523f71ab147fdb7f15e9f37f9"
TYPEHASH = "0xc4cfb57eea370ef2a92403a7e865b2de262d7ca272333665b54ad7e6a602ef68"
MSG_A = ("symbol=ASTERUSDT&type=LIMIT&side=BUY&timeInForce=GTC&quantity=20&price=0.5&nonce=1748310859508867"
         "&signer=0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0")
STRUCT_A = "0xfda3c00f7da2bede0ed7f9362bda29d068cf3c41eee370fceb6cde0ec623b8fc"
DIGEST_A = "0xed6fe2804fb7a74d857c73d838ce3722b0f44d2c023c24bca0acb2bdf0d56267"
SIG_A = ("0xd82a6784f5eff00b95ecb88145cf7a5fa6803ea6f67e0217262073da628750a742bb3ba555af418320e55029afe358957808d4f8"
         "10d73ea663d9c91852cd6d941c")
MSG_B = ("symbol=AIW3USDT&side=SELL&type=MARKET&quantity=2443&newClientOrderId=fb-e1-c1&newOrderRespType=RESULT"
         "&nonce=1789217110000000&user=0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e"
         "&signer=0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0")
DIGEST_B = "0xe0dc38b3dc800e9d5e6a2069ab0e4e058cb0887137e618813e78afb3334fdc2b"
SIG_B = ("0xf601504e9d6741190621bed82247473f987de752cd90b0dd5bdcb1245a4435d0534345ac1b73e3bd9e7be2a3aca25dd5d5a3366dfe198"
         "f299619b2a6d7a79f371c")

# строка exchangeInfo AIW3USDT из живого снимка 12.09 (фильтры и TIF — как есть)
AIW3_EI = {"symbol": SYM, "status": "TRADING", "timeInForce": ["GTC", "IOC", "GTX", "HIDDEN"], "filters": [
    {"minPrice": "0.0000100", "maxPrice": "200", "filterType": "PRICE_FILTER", "tickSize": "0.0000100"},
    {"stepSize": "1", "filterType": "LOT_SIZE", "maxQty": "800000", "minQty": "1"},
    {"stepSize": "1", "filterType": "MARKET_LOT_SIZE", "maxQty": "80000", "minQty": "1"},
    {"limit": 200, "filterType": "MAX_NUM_ORDERS"},
    {"notional": "5", "filterType": "MIN_NOTIONAL"},
    {"multiplierDown": "0.9000", "multiplierUp": "1.1000", "filterType": "PERCENT_PRICE"}]}


def _typed(msg: str) -> dict:
    """Независимая копия typed data из документации (не из модуля): проверка не должна повторять ошибку кода."""
    return {"types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                       {"name": "chainId", "type": "uint256"},
                                       {"name": "verifyingContract", "type": "address"}],
                      "Message": [{"name": "msg", "type": "string"}]},
            "primaryType": "Message",
            "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": 1666,
                       "verifyingContract": "0x0000000000000000000000000000000000000000"},
            "message": {"msg": msg}}


def _hx(b) -> str:
    return "0x" + bytes(b).hex()


def _recover(msg: str, sig: str) -> str:
    return Account.recover_message(encode_typed_data(full_message=_typed(msg)), signature=sig)


# ================================ подделка площадки ================================
class FakeAster(requests.Session):
    """Aster за requests.Session.send: ответы по путям, простая модель IOC, позиция и сделки, скрипт отказов
    (script[(метод, путь)] — очередь ответов (status, body[, headers]) или исключений). Каждая подписанная строка
    проверяется: подпись последней, адрес восстанавливается в signer."""

    def __init__(self):
        super().__init__()
        self.sent: list[requests.PreparedRequest] = []
        self.log: list[tuple[str, str, dict]] = []
        self.script: dict[tuple[str, str], list] = {}
        self.weight = 1
        self.position = D(0)
        self.position_rows = None
        self.balance_rows = [{"asset": "USDT", "balance": "700", "availableBalance": "612.5"},
                             {"asset": "USDF", "balance": "0", "availableBalance": "0"}]
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.income: list[dict] = []
        self.fill_cap: Decimal | None = None      # None — IOC исполняется целиком
        self.lose_reply = None                    # заявка исполнена, а ответ потерян: (status, body) или исключение
        self.hide_queries = 0                     # сколько запросов /order отвечают -2013, хотя заявка есть
        self.dual = True                          # hedge-режим до setup
        self.margin = "CROSSED"
        self.multi = False                        # режим Multi-Assets: ISOLATED в нём отклоняется -4168
        self.lev_reply = None
        self.server_ms = 1_789_217_110_000
        self.clock_ms = 1_789_217_110_000
        self.next_oid = 1000
        self.next_tid = 5000
        self.bad_sig = []

    def n(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.log if m == method and p == path)

    # --- транспорт ---
    def send(self, request, **kw):
        self.sent.append(request)
        u = urlsplit(request.url)
        raw = u.query if request.method == "GET" else (request.body or b"").decode("ascii")
        params = dict(parse_qsl(raw, keep_blank_values=True))
        if "signature" in params:
            msg, _, sig = raw.rpartition("&signature=")
            if "&" in sig or not sig.startswith("0x") or _recover(msg, sig) != DEMO_SIGNER:
                self.bad_sig.append(raw)
        self.log.append((request.method, u.path, params))
        q = self.script.get((request.method, u.path))
        if q:
            item = q.pop(0)
            if isinstance(item, BaseException):
                raise item
            status, body, *hdr = item
            return self._resp(request, status, body, hdr[0] if hdr else {})
        status, body = self.handle(request.method, u.path, params)
        return self._resp(request, status, body, {})

    def _resp(self, request, status, body, headers):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        r.headers = CaseInsensitiveDict({"X-MBX-USED-WEIGHT-1M": str(self.weight), **headers})
        r.url, r.request, r.encoding = request.url, request, "utf-8"
        return r

    # --- модель ---
    def handle(self, method, path, p):
        key = (method, path.removeprefix("/fapi/v3/"))
        if key == ("GET", "time"):
            return 200, {"serverTime": self.server_ms}
        if key == ("GET", "exchangeInfo"):
            return 200, {"symbols": [AIW3_EI]}
        if key == ("GET", "depth"):
            return 200, {"bids": [["0.04093", "62344"], ["0.04092", "1000"]], "asks": [["0.04094", "1000"],
                                                                                     ["0.04095", "5000"]]}
        if key == ("GET", "premiumIndex"):
            return 200, {"symbol": p["symbol"], "markPrice": "0.04093", "lastFundingRate": "0.00049260",
                         "nextFundingTime": 1789218000000}
        if key == ("GET", "positionRisk"):
            rows = self.position_rows if self.position_rows is not None else [
                {"symbol": SYM, "positionAmt": format(self.position, "f"), "positionSide": "BOTH",
                 "liquidationPrice": "0.08"}]
            return 200, rows
        if key == ("GET", "balance"):
            return 200, self.balance_rows
        if key == ("POST", "positionSide/dual"):
            if self.dual is False:
                return 400, {"code": -4059, "msg": "No need to change position side."}
            self.dual = False
            return 200, {"code": 200, "msg": "success"}
        if key == ("GET", "multiAssetsMargin"):
            return 200, {"multiAssetsMargin": self.multi}
        if key == ("POST", "marginType"):
            if self.multi and p["marginType"] == "ISOLATED":
                return 400, {"code": -4168, "msg": "Unable to adjust to isolated-margin mode under the Multi-Assets mode."}
            if self.margin == p["marginType"]:
                return 400, {"code": -4046, "msg": "No need to change margin type."}
            self.margin = p["marginType"]
            return 200, {"code": 200, "msg": "success"}
        if key == ("POST", "leverage"):
            return 200, self.lev_reply or {"leverage": int(p["leverage"]), "maxNotionalValue": "5000", "symbol": p["symbol"]}
        if key == ("POST", "order"):
            return self._order(p)
        if key == ("GET", "order"):
            o = self.orders.get(p["origClientOrderId"])
            if o is None or self.hide_queries > 0:
                self.hide_queries -= 1
                return 400, {"code": -2013, "msg": "Order does not exist."}
            return 200, o
        if key == ("GET", "userTrades"):
            rows = [t for t in self.trades if t["symbol"] == p["symbol"]
                    and ("fromId" not in p or t["id"] >= int(p["fromId"]))
                    and ("startTime" not in p or t["time"] >= int(p["startTime"]))]
            return 200, rows[:int(p.get("limit", 500))]
        if key == ("GET", "income"):
            rows = [r for r in self.income if int(p["startTime"]) <= r["time"] <= int(p["endTime"])]
            return 200, rows[:int(p.get("limit", 100))]
        return 404, {"code": -1, "msg": f"нет пути {method} {path}"}

    def _order(self, p):
        if p.get("type") == "TAKE_PROFIT_MARKET":
            cid = p["newClientOrderId"]
            self.next_oid += 1
            o = {"orderId": self.next_oid, "symbol": p["symbol"], "status": "NEW", "clientOrderId": cid,
                 "price": "0", "avgPrice": "0", "origQty": p["quantity"], "executedQty": "0",
                 "cumQuote": "0", "type": p["type"], "reduceOnly": p["reduceOnly"] == "true",
                 "side": p["side"], "workingType": p["workingType"], "updateTime": self.clock_ms}
            self.orders[cid] = o
            return 200, o
        qty, px, cid = D(p["quantity"]), D(p["price"]), p["newClientOrderId"]
        ex = qty if self.fill_cap is None else min(qty, self.fill_cap)
        self.next_oid += 1
        o = {"orderId": self.next_oid, "symbol": p["symbol"], "status": "FILLED" if ex == qty else "EXPIRED",
             "clientOrderId": cid, "price": p["price"], "avgPrice": format(px if ex else D(0), "f"),
             "origQty": p["quantity"], "executedQty": format(ex, "f"), "cumQuote": format(ex * px, "f"),
             "timeInForce": p["timeInForce"], "type": "LIMIT", "reduceOnly": p["reduceOnly"] == "true",
             "side": p["side"], "positionSide": "BOTH", "updateTime": self.clock_ms}
        self.orders[cid] = o
        if ex:
            self.position += -ex if p["side"] == "SELL" else ex
            self.next_tid += 1
            self.clock_ms += 1
            self.trades.append({"id": self.next_tid, "orderId": self.next_oid, "symbol": p["symbol"], "side": p["side"],
                                "price": p["price"], "qty": format(ex, "f"), "quoteQty": format(ex * px, "f"),
                                "commission": format(-(ex * px * D("0.0004")), "f"), "commissionAsset": "USDT",
                                "realizedPnl": "0", "maker": False, "buyer": p["side"] == "BUY",
                                "time": self.clock_ms})
        if self.lose_reply is not None:
            lr, self.lose_reply = self.lose_reply, None
            if isinstance(lr, BaseException):
                raise lr
            return lr
        return 200, o


@pytest.fixture(autouse=True)
def _clean_redaction():
    keys._reset_redaction_for_tests()
    yield
    keys._reset_redaction_for_tests()


@pytest.fixture
def fake():
    return FakeAster()


def _signer(send_user=True, now_us=None) -> AsterSigner:
    return AsterSigner(DEMO_USER, DEMO_SIGNER, Account.from_key(DEMO_KEY), send_user, nonces=MonotonicNonce(now_us))


def mk(fake, mode="live", paused=False, signer="default", **kw) -> AsterTrade:
    st = {"mode": mode, "paused": paused}
    sleeps: list[float] = []
    t = AsterTrade(_signer() if signer == "default" else signer, mode_state=lambda: (st["mode"], st["paused"]),
                   session=fake, sleep=sleeps.append, **kw)
    t.st, t.sleeps = st, sleeps
    return t


# ================================ подпись: векторы ================================
def test_vector_a_domain_digest_signature_and_recovered_signer():
    sm = encode_typed_data(full_message=_typed(MSG_A))
    assert _hx(sm.header) == DOMSEP                                 # domain separator
    assert _hx(keccak(text="Message(string msg)")) == TYPEHASH
    assert _hx(sm.body) == STRUCT_A                                 # hashStruct(Message)
    assert _hx(keccak(b"\x19" + sm.version + sm.header + sm.body)) == DIGEST_A
    signed = Account.from_key(DEMO_KEY).sign_message(sm)
    assert _hx(signed.message_hash) == DIGEST_A and _hx(signed.signature) == SIG_A
    assert _recover(MSG_A, SIG_A) == DEMO_SIGNER
    # то же — через модуль: параметры примера из документации, nonce документации, без user
    s = _signer(send_user=False, now_us=lambda: 1748310859508867)
    qs, n = s.sign_qs({"symbol": "ASTERUSDT", "type": "LIMIT", "side": "BUY", "timeInForce": "GTC",
                       "quantity": D("20"), "price": D("0.50")})
    assert n == 1748310859508867
    assert qs == f"{MSG_A}&signature={SIG_A}"
    assert encode_typed_data(full_message=at.typed_message(MSG_A)) == sm


def test_vector_b_aiw3_order_with_user():
    sm = encode_typed_data(full_message=_typed(MSG_B))
    assert _hx(keccak(b"\x19" + sm.version + sm.header + sm.body)) == DIGEST_B
    assert _recover(MSG_B, SIG_B) == DEMO_SIGNER
    s = _signer(send_user=True, now_us=lambda: 1789217110000000)
    qs, n = s.sign_qs({"symbol": SYM, "side": "SELL", "type": "MARKET", "quantity": D(2443),
                       "newClientOrderId": "fb-e1-c1", "newOrderRespType": "RESULT"})
    assert (qs, n) == (f"{MSG_B}&signature={SIG_B}", 1789217110000000)


def test_nonce_microseconds_strictly_increasing_and_one_generator_per_agent():
    frozen = MonotonicNonce(lambda: 1_789_217_110_000_000)
    got = [frozen.next() for _ in range(5)]
    assert got == list(range(1_789_217_110_000_000, 1_789_217_110_000_005))
    back = iter([200, 100, 50])                                     # часы ушли назад — повтора всё равно нет
    m = MonotonicNonce(lambda: next(back))
    assert [m.next(), m.next(), m.next()] == [200, 201, 202]
    assert len(str(MonotonicNonce().next())) == 16                  # микросекунды, 16 знаков
    a1 = AsterSigner(DEMO_USER, DEMO_SIGNER, Account.from_key(DEMO_KEY), True)
    a2 = AsterSigner(DEMO_USER, DEMO_SIGNER.lower(), Account.from_key(DEMO_KEY), True)
    assert a1.nonces is a2.nonces                                   # один агент — один генератор в процессе
    ns = [a1.sign_qs({})[1], a2.sign_qs({})[1], a1.sign_qs({})[1]]
    assert ns == sorted(set(ns))


def test_signer_refuses_foreign_key_and_master_account():
    acct = Account.from_key(DEMO_KEY)
    with pytest.raises(KeyMismatch):
        AsterSigner(DEMO_USER, DEMO_USER, acct, True)               # ключ не того адреса
    with pytest.raises(KeyMismatch, match="мастер"):
        AsterSigner(DEMO_SIGNER, DEMO_SIGNER, acct, True)           # signer = user: мастер-ключ на сервере
    with pytest.raises(ValueError):
        AsterSigner("0x123", DEMO_SIGNER, acct, True)
    s = AsterSigner(DEMO_USER, DEMO_SIGNER, SignerKey(acct, "aster"), True)   # обёртка keys — тоже подписант
    assert "<key>" not in repr(s) and DEMO_KEY[2:] not in repr(s)
    qs, _ = s.sign_qs({"symbol": SYM})
    msg, _, sig = qs.rpartition("&signature=")
    assert _recover(msg, sig) == DEMO_SIGNER


def test_param_formatting_insertion_order_and_reserved_keys():
    s = _signer(send_user=True, now_us=lambda: 7)
    qs, _ = s.sign_qs({"z": "1", "reduceOnly": False, "a": True, "qty": D("1.2E+3"), "px": D("0.04090"),
                       "n": 5, "skip": None, "cid": "fb-X/1:2.3_4"})
    msg = qs.rpartition("&signature=")[0]
    assert msg == ("z=1&reduceOnly=false&a=true&qty=1200&px=0.0409&n=5&cid=fb-X%2F1%3A2.3_4&nonce=7"
                   f"&user={DEMO_USER}&signer={DEMO_SIGNER}")
    with pytest.raises(TypeError):
        s.sign_qs({"price": 0.5})                                   # float — никогда
    for k in ("nonce", "user", "signer", "signature"):
        with pytest.raises(ValueError):
            s.sign_qs({k: "x"})
    assert at.dec_str(D("0E-8")) == "0" and at.dec_str(D("100")) == "100" and at.dec_str(D("1E+2")) == "100"
    assert at.floor_step(D("12216.9"), D(1)) == D(12216) and at.ceil_step(D("0.040901"), D("0.00001")) == D("0.04091")


# ================================ тело формы и query байт в байт ================================
def test_post_form_body_equals_signed_string_byte_for_byte(fake):
    t = mk(fake, signer=_signer(now_us=lambda: 1789217110000000))
    cid = store.client_order_id("D7K2", "entry", 3, 1, 1)
    f = t.ioc(SYM, "SELL", D(12216), D("0.04089"), cid, False, hedge=True)
    assert f.status == "FILLED" and f.sign_nonce == 1789217110000000
    req = next(r for r in fake.sent if r.method == "POST")
    assert req.url == f"{BASE}/fapi/v3/order"
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    expect_msg = urlencode([("symbol", SYM), ("side", "SELL"), ("type", "LIMIT"), ("timeInForce", "IOC"),
                            ("quantity", "12216"), ("price", "0.04089"), ("newClientOrderId", cid),
                            ("reduceOnly", "false"), ("newOrderRespType", "RESULT"), ("nonce", "1789217110000000"),
                            ("user", DEMO_USER), ("signer", DEMO_SIGNER)])
    sig = _hx(Account.from_key(DEMO_KEY).sign_message(encode_typed_data(full_message=_typed(expect_msg))).signature)
    assert isinstance(req.body, bytes)
    assert req.body == f"{expect_msg}&signature={sig}".encode("ascii")
    # и requests, закодировав dict(params, signature=…), дал бы те же байты (отчёт aster §1)
    alt = requests.Request("POST", BASE, data=dict(parse_qsl(expect_msg), signature=sig)).prepare()
    assert (alt.body.encode() if isinstance(alt.body, str) else alt.body) == req.body
    assert not fake.bad_sig


def test_get_query_string_is_signed_string_signature_last(fake):
    t = mk(fake, mode="readonly")
    fake.position = D(-12216)
    assert t.position(SYM) == D(-12216)
    req = fake.sent[-1]
    assert req.method == "GET" and req.body is None
    qs = urlsplit(req.url).query
    assert req.url == f"{BASE}/fapi/v3/positionRisk?{qs}"
    msg, _, sig = qs.rpartition("&signature=")
    assert [k for k, _ in parse_qsl(msg)] == ["symbol", "nonce", "user", "signer"]
    assert _recover(msg, sig) == DEMO_SIGNER and not fake.bad_sig


# ================================ ворота режима ================================
def test_no_signer_only_public_reads_signed_refused_before_network(fake):
    t = AsterTrade(session=fake)                                    # dry: ключей нет вовсе
    assert t.filters(SYM).tick == D("0.00001")
    assert t.book(SYM, 5).bids[0] == (D("0.04093"), D(62344))
    assert t.funding(SYM) == (D("0.04093"), D("0.00049260"), 1789218000000)
    n = len(fake.sent)
    with pytest.raises(ModeForbidden):
        t.position(SYM)
    with pytest.raises(ModeForbidden):
        t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False, hedge=True)
    with pytest.raises(ModeForbidden):
        t.setup(SYM, 1, "ISOLATED")
    assert len(fake.sent) == n


@pytest.mark.parametrize("mode,paused,read_ok,send_ok,hedge_ok", [
    ("dry", False, False, False, False),
    ("readonly", False, True, False, False),
    ("live", False, True, True, True),
    ("live", True, True, False, True),          # «стоп»: новое не шлём, хедж исполненной ноги DEX — да (Q7)
    (None, False, False, False, False),          # пустой mode в owner.toml = dry
])
def test_mode_gate_matrix(fake, mode, paused, read_ok, send_ok, hedge_ok):
    t = mk(fake, mode=mode, paused=paused)

    def attempt(fn):
        before, last = len(fake.sent), t.signer.nonces.last
        try:
            fn()
            return True
        except ModeForbidden:
            assert len(fake.sent) == before and t.signer.nonces.last == last   # до подписи и до сети
            return False

    assert attempt(lambda: t.balances()) is read_ok
    assert attempt(lambda: t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False)) is send_ok
    assert attempt(lambda: t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c2-a1", False, hedge=True)) is hedge_ok
    assert attempt(lambda: t.setup(SYM, 1, "ISOLATED")) is send_ok


def test_mode_state_failure_and_file_mode_lowering_close_the_gate(fake):
    def boom():
        raise OSError("owner.toml не читается")
    t = AsterTrade(_signer(), mode_state=boom, session=fake)
    with pytest.raises(ModeForbidden, match="не прочитаны"):
        t.balances()
    t = mk(fake, mode="live")
    t.st["mode"] = "readonly"                                       # владелец понизил режим в файле
    with pytest.raises(ModeForbidden):
        t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False, hedge=True)
    assert t.balances()
    assert fake.n("POST", "/fapi/v3/order") == 0


def test_keys_load_mode_caps_owner_file_mode(fake):
    acct = Account.from_key(DEMO_KEY)
    k = Keys(mode="readonly", evm_address="0x" + "1" * 40, aster_user=DEMO_USER, aster_signer=DEMO_SIGNER,
             aster=SignerKey(acct, "aster"))
    t = AsterTrade.from_keys(k, lambda: ("live", False), send_user=True, session=fake)
    assert t.balances()                                             # подписанное чтение — да
    with pytest.raises(ModeForbidden):                              # загружено readonly: live файла не поднимает
        t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False, hedge=True)
    assert fake.n("POST", "/fapi/v3/order") == 0 and t.signer.user == DEMO_USER


def test_on_signed_runs_before_send_and_its_failure_blocks_send(fake):
    t = mk(fake)
    seen = []

    def note(n):
        seen.append((n, len(fake.sent)))
    f = t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False, on_signed=note)
    assert seen == [(f.sign_nonce, 0)]                              # nonce записан ДО отправки

    def db_down(n):
        raise RuntimeError("trade.db недоступна")
    with pytest.raises(RuntimeError):
        t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c2-a1", False, on_signed=db_down)
    assert fake.n("POST", "/fapi/v3/order") == 1


def test_ioc_refuses_bad_input_before_anything(fake):
    t = mk(fake)
    t.filters(SYM)                                                  # фильтры в кэше
    bad = [dict(side="SHORT"), dict(cid="fb bad id"), dict(qty=100.0), dict(qty=D("100.5")), dict(px=D("0.040905")),
           dict(qty=D(0)), dict(qty=D(900000))]
    for b in bad:
        with pytest.raises((ValueError, TypeError)):
            t.ioc(SYM, b.get("side", "SELL"), b.get("qty", D(100)), b.get("px", D("0.04")),
                  b.get("cid", "fb-X-e01-c1-a1"), False)
    assert fake.n("POST", "/fapi/v3/order") == 0


# ================================ исходы IOC ================================
@pytest.mark.parametrize("reply,status,code", [
    ((400, {"code": -2019, "msg": "Margin is insufficient."}), "REJECTED", -2019),
    ((400, {"code": -4164, "msg": "Order's notional must be no smaller than 5.0 (unless you choose reduce only)"}),
     "REJECTED", -4164),
    ((400, {"code": -1111, "msg": "Precision is over the maximum defined for this asset."}), "REJECTED", -1111),
    ((400, {"code": -2022, "msg": "ReduceOnly Order is rejected."}), "REJECTED", -2022),
    ((401, {"code": -1022, "msg": "Signature for this request is not valid."}), "REJECTED", -1022),
    ((503, {"code": -1001, "msg": "Internal error"}), "UNKNOWN", -1001),
    ((503, {}), "UNKNOWN", None),
    ((502, {"_": "bad gateway"}), "UNKNOWN", None),
    ((400, {"code": -1007, "msg": "Timeout waiting for response from backend server."}), "UNKNOWN", -1007),
    ((500, {"code": -1006, "msg": "Unexpected response"}), "UNKNOWN", -1006),
    ((200, {"msg": "no order id"}), "UNKNOWN", None),
])
def test_ioc_error_mapping_never_resends(fake, reply, status, code):
    t = mk(fake)
    fake.script[("POST", "/fapi/v3/order")] = [reply]
    f = t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False, hedge=True)
    assert (f.status, f.err_code) == (status, code)
    assert f.sign_nonce > 0 and f.qty == 0
    assert fake.n("POST", "/fapi/v3/order") == 1                    # ни одной повторной отправки
    if status == "REJECTED":
        assert str(code) in t.last_error


@pytest.mark.parametrize("exc", [requests.ReadTimeout("read timed out"), requests.ConnectionError("reset"),
                                 OSError("broken pipe")])
def test_ioc_transport_failure_is_unknown_with_sign_nonce(fake, exc):
    t = mk(fake)
    fake.script[("POST", "/fapi/v3/order")] = [exc]
    f = t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False)
    assert f.status == "UNKNOWN" and f.sign_nonce == t.signer.nonces.last and f.order_id is None
    assert fake.n("POST", "/fapi/v3/order") == 1


def test_ioc_fill_statuses_full_partial_expired_and_not_final(fake):
    t = mk(fake)
    f = t.ioc(SYM, "SELL", D(12216), D("0.04089"), "fb-X-e01-c1-a1", False)
    assert (f.status, f.qty, f.avg_px, f.quote) == ("FILLED", D(12216), D("0.04089"), D(12216) * D("0.04089"))
    assert isinstance(f.qty, Decimal) and isinstance(f.order_id, int)
    fake.fill_cap = D(41)
    f = t.ioc(SYM, "BUY", D(100), D("0.04094"), "fb-X-x01-c1-a1", True)
    assert (f.status, f.qty) == ("PARTIALLY_FILLED", D(41))         # остаток IOC истёк — это финал
    fake.fill_cap = D(0)
    f = t.ioc(SYM, "BUY", D(100), D("0.04094"), "fb-X-x01-c2-a1", True)
    assert (f.status, f.qty) == ("EXPIRED", D(0))
    assert fake.orders["fb-X-x01-c2-a1"]["reduceOnly"] is True
    fake.script[("POST", "/fapi/v3/order")] = [(200, {"orderId": 77, "clientOrderId": "fb-X-e02-c1-a1", "status": "NEW",
                                                     "executedQty": "0", "avgPrice": "0", "cumQuote": "0"})]
    f = t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e02-c1-a1", False)
    assert (f.status, f.order_id) == ("UNKNOWN", 77)                # не финал: спросить, а не считать
    fake.script[("POST", "/fapi/v3/order")] = [(200, {"orderId": 78, "clientOrderId": "someone-else", "status": "FILLED",
                                                     "executedQty": "100", "avgPrice": "0.04", "cumQuote": "4"})]
    assert t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e02-c2-a1", False).status == "UNKNOWN"


def test_order_to_fill_fills_in_avg_from_quote():
    f = at.order_to_fill("c", {"orderId": 1, "status": "FILLED", "executedQty": "10", "avgPrice": "0",
                               "cumQuote": "0.41"}, 5)
    assert f.avg_px == D("0.041") and f.sign_nonce == 5


# ================================ неизвестный исход ================================
def test_lost_reply_resolved_by_query_without_resend(fake):
    t = mk(fake)
    fake.lose_reply = (503, {"code": -1001, "msg": "Internal error; unable to process your request."})
    cid = "fb-X-e01-c1-a1"
    f = t.ioc(SYM, "SELL", D(12216), D("0.04089"), cid, False, hedge=True)
    assert f.status == "UNKNOWN"
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_ms)
    assert (g.status, g.qty, g.order_id) == ("FILLED", D(12216), fake.orders[cid]["orderId"])
    assert g.sign_nonce == 0                                        # nonce запроса не затирает nonce заявки
    assert fake.n("POST", "/fapi/v3/order") == 1


def test_lost_reply_after_timeout_found_after_two_misses(fake):
    t = mk(fake)
    fake.lose_reply = requests.ReadTimeout("timed out")
    fake.hide_queries = 2                                           # заявка ещё не видна запросу
    f = t.ioc(SYM, "SELL", D(500), D("0.04089"), "fb-X-e01-c1-a1", False)
    assert f.status == "UNKNOWN"
    g = t.settle_unknown(SYM, "fb-X-e01-c1-a1", pos_before=D(0), since_ms=fake.clock_ms)
    assert (g.status, g.qty) == ("FILLED", D(500))
    assert fake.n("GET", "/fapi/v3/order") == 3 and fake.n("POST", "/fapi/v3/order") == 1


def test_not_placed_only_after_three_misses_and_unchanged_account(fake, tmp_path):
    t = mk(fake)
    fake.script[("POST", "/fapi/v3/order")] = [requests.ConnectTimeout("no route")]
    cid = "fb-X-e01-c1-a1"
    assert t.ioc(SYM, "SELL", D(500), D("0.04"), cid, False).status == "UNKNOWN"
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_ms)
    assert (g.status, g.err_code) == ("NOT_FOUND", -2013)
    assert fake.n("GET", "/fapi/v3/order") == tconfig.ASTER_UNKNOWN_QUERIES
    assert sum(t.sleeps) == pytest.approx(tconfig.ASTER_UNKNOWN_SPAN_S)   # три запроса за ~5 с
    assert fake.n("GET", "/fapi/v3/positionRisk") == 1 and fake.n("GET", "/fapi/v3/userTrades") == 1
    with pytest.raises(ValueError):
        store.record_perp_fill(store.connect(tmp_path / "t.db"), g)   # NOT_PLACED ставит движок, не одна запись
    assert fake.n("POST", "/fapi/v3/order") == 1                    # единственная (потерянная) отправка


def test_three_misses_but_account_moved_stays_unknown(fake):
    t = mk(fake)
    cid = "fb-X-e01-c1-a1"
    fake.position = D(-500)                                         # позиция сдвинулась, а заявки «нет»
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_ms)
    assert g.status == "UNKNOWN"
    fake.position = D(0)
    fake.trades.append({"id": 9, "orderId": 4242, "symbol": SYM, "side": "SELL", "price": "0.04", "qty": "500",
                        "quoteQty": "20", "commission": "-0.008", "commissionAsset": "USDT", "realizedPnl": "0",
                        "maker": False, "time": fake.clock_ms + 10})
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_ms)
    assert g.status == "UNKNOWN"                                    # чужая сделка в окне — не доказано
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_ms, known_order_ids={4242})
    assert g.status == "NOT_FOUND"                                  # сделка учтённой дочерней — не мешает
    assert t.settle_unknown(SYM, cid, pos_before=None, since_ms=fake.clock_ms).status == "UNKNOWN"
    fake.position_rows = []
    assert t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_ms).status == "UNKNOWN"
    assert fake.n("POST", "/fapi/v3/order") == 0


def test_query_errors_are_unknown_and_single_miss_is_not_found(fake):
    t = mk(fake)
    assert t.query(SYM, "fb-none-e01-c1-a1").status == "NOT_FOUND"
    fake.script[("GET", "/fapi/v3/order")] = [(503, {}), requests.ReadTimeout("x"), (400, {"code": -1121, "msg": "bad"})]
    assert [t.query(SYM, "fb-none-e01-c1-a1").status for _ in range(3)] == ["UNKNOWN"] * 3
    with pytest.raises(ValueError):
        t.query(SYM, "bad id with spaces")


# ================================ чтения ================================
def test_filters_from_live_snapshot_cached(fake):
    t = mk(fake)
    f = t.filters(SYM)
    assert (f.tick, f.step, f.min_qty, f.max_qty_limit, f.max_qty_market, f.min_notional) == (
        D("0.00001"), D(1), D(1), D(800000), D(80000), D(5))
    assert f.tifs == frozenset({"GTC", "IOC", "GTX", "HIDDEN"}) and "FOK" not in f.tifs
    assert t.status(SYM) == "TRADING"
    t.filters(SYM)
    assert fake.n("GET", "/fapi/v3/exchangeInfo") == 1              # кэш
    with pytest.raises(at.AsterError):
        t.filters("NOPEUSDT")


def test_book_decimal_and_valid_limit(fake):
    t = mk(fake)
    b = t.book(SYM, 2)
    assert b.bids == ((D("0.04093"), D(62344)), (D("0.04092"), D(1000)))
    assert b.asks[0] == (D("0.04094"), D(1000)) and b.ts > 0
    assert fake.log[-1][2]["limit"] == "5"                          # допустимые 5/10/20/50
    t.book(SYM, 500)
    assert fake.log[-1][2]["limit"] == "50"                         # больше 50 не берём (вес 2)


def test_position_unknown_is_none_never_zero(fake):
    t = mk(fake, mode="readonly")
    fake.position = D(0)
    assert t.position(SYM) == D(0)                                  # строка с нулём — флэт
    fake.position_rows = []
    assert t.position(SYM) is None                                  # пустой ответ — НЕ флэт
    fake.position_rows = [{"symbol": "BTCUSDT", "positionAmt": "1", "positionSide": "BOTH"}]
    assert t.position(SYM) is None
    fake.position_rows = [{"symbol": SYM, "positionAmt": "0", "positionSide": "LONG"},
                          {"symbol": SYM, "positionAmt": "-5", "positionSide": "SHORT"}]
    assert t.position(SYM) is None                                  # hedge-режим — не наша модель
    fake.position_rows = None
    fake.script[("GET", "/fapi/v3/positionRisk")] = [(500, {}), requests.ReadTimeout("x"),
                                                     (400, {"code": -1022, "msg": "sig"})]
    assert [t.position(SYM) for _ in range(3)] == [None, None, None]


def test_available_margin_usdt_or_none(fake):
    t = mk(fake, mode="readonly")
    assert t.available_margin() == D("612.5")
    fake.balance_rows = [{"asset": "USDF", "availableBalance": "10"}]
    assert t.available_margin() is None
    fake.script[("GET", "/fapi/v3/balance")] = [(503, {})]
    assert t.available_margin() is None


def test_setup_idempotent_and_checks_leverage(fake):
    t = mk(fake)
    t.setup(SYM, 1, "ISOLATED")
    assert (fake.dual, fake.margin) == (False, "ISOLATED")
    t.setup(SYM, 1, "ISOLATED")                                     # -4059/-4046 — «уже так», это успех
    posts = [(p, prm) for m, p, prm in fake.log if m == "POST"]
    assert [p for p, _ in posts] == ["/fapi/v3/positionSide/dual", "/fapi/v3/marginType", "/fapi/v3/leverage"] * 2
    assert posts[0][1]["dualSidePosition"] == "false" and posts[2][1]["leverage"] == "1"
    fake.lev_reply = {"leverage": 2, "symbol": SYM}
    with pytest.raises(at.AsterError, match="2x"):
        t.setup(SYM, 1, "ISOLATED")
    fake.lev_reply = None
    fake.script[("POST", "/fapi/v3/marginType")] = [(400, {"code": -4048, "msg": "Margin type cannot be changed if there exists position."})]
    with pytest.raises(AsterApiError) as ei:
        t.setup(SYM, 1, "CROSSED")
    assert ei.value.code == -4048
    for lev, mt in ((0, "ISOLATED"), (True, "ISOLATED"), (1.0, "ISOLATED"), (1, "isolated")):
        with pytest.raises(ValueError):
            t.setup(SYM, lev, mt)


def test_multi_assets_mode_is_read_and_isolated_refusal_is_explained(fake):
    """Живой случай 12.09: аккаунт в Multi-Assets — ISOLATED отклоняется -4168. Режим читается (предпроверка), а
    отказ настройки объясняет владельцу, что переключить; ни плечо, ни заявки после него не шлются."""
    t = mk(fake)
    assert t.multi_assets() is False
    fake.multi = True
    assert t.multi_assets() is True
    with pytest.raises(at.AsterError, match="Single-Asset") as ei:
        t.setup(SYM, 1, "ISOLATED")
    assert not isinstance(ei.value, AsterApiError) and "Ничего не отправлено" in str(ei.value)
    assert fake.n("POST", "/fapi/v3/leverage") == 0
    t.setup(SYM, 1, "CROSSED")                                      # cross в Multi-Assets разрешён
    fake.script[("GET", "/fapi/v3/multiAssetsMargin")] = [(200, {"multiAssetsMargin": "maybe"})]
    with pytest.raises(at.AsterError, match="неожиданный"):
        t.multi_assets()


def test_fills_pagination_and_absolute_commission(fake, monkeypatch):
    t = mk(fake, mode="readonly")
    monkeypatch.setattr(at, "PAGE_LIMIT", 2)
    for i in range(5):
        fake.trades.append({"id": 100 + i, "orderId": 7, "symbol": SYM, "side": "SELL", "price": "0.0409",
                            "qty": "10", "quoteQty": "0.409", "commission": "-0.0001636", "commissionAsset": "USDT",
                            "realizedPnl": "0", "maker": i == 0, "time": 1000 + i})
    rows = t.fills(SYM, 101)
    assert [r["trade_id"] for r in rows] == [101, 102, 103, 104]
    assert rows[0]["commission_abs"] == D("0.0001636") and rows[0]["price"] == D("0.0409")
    assert fake.n("GET", "/fapi/v3/userTrades") == 3                # полная страница → ещё запрос, пустой = конец
    assert len(t.fills(SYM, None)) == 2                              # без fromId — одна (последняя) страница


def test_funding_income_windows_dedup(fake):
    now_ms = 1_789_217_110_000
    t = mk(fake, mode="readonly", now=lambda: now_ms / 1000)
    start = now_ms - 10 * 24 * 3600 * 1000                          # 10 суток: два окна по 7
    fake.income = [{"symbol": SYM, "incomeType": "FUNDING_FEE", "income": "0.0123", "asset": "USDT",
                    "time": start + h * 3600_000, "tranId": 900 + h, "info": ""} for h in range(0, 240, 24)]
    fake.income.append({"symbol": SYM, "incomeType": "COMMISSION", "income": "-1", "asset": "USDT",
                        "time": start + 5, "tranId": 1, "info": ""})
    rows = t.funding_income(SYM, start)
    assert [r["tran_id"] for r in rows] == [900 + h for h in range(0, 240, 24)]
    assert rows[0]["income"] == D("0.0123")
    calls = [p for m, p2, p in fake.log if p2 == "/fapi/v3/income"]
    assert len(calls) == 2 and all(int(c["endTime"]) - int(c["startTime"]) < at.WEEK_MS for c in calls)
    assert calls[0]["incomeType"] == "FUNDING_FEE"


def test_weight_header_budget_and_bans(fake):
    t = mk(fake)
    fake.weight = 1800                                              # 75 % от 2400
    t.balances()
    assert t.http.used_weight == 1800
    with pytest.raises(BudgetExceeded):
        t.balances()                                                # обычное чтение — стоп с 70 %
    assert t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False).status == "FILLED"   # заявка — до 95 %
    fake.weight = 2300
    t.position(SYM)
    with pytest.raises(BudgetExceeded):
        t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c2-a1", False)
    assert fake.n("POST", "/fapi/v3/order") == 1

    t = mk(FakeAster())
    t._s.script[("POST", "/fapi/v3/order")] = [(429, {"code": -1003, "msg": "Too many requests"}, {"Retry-After": "3"})]
    f = t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False)
    assert (f.status, f.err_code) == ("REJECTED", -1003) and t.backoff_until > 0
    with pytest.raises(BudgetExceeded, match="429"):
        t.balances()
    t = mk(FakeAster())                                             # Retry-After датой — не исключение после отправки
    t._s.script[("POST", "/fapi/v3/order")] = [(429, {}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})]
    assert t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False).status == "REJECTED"
    t = mk(FakeAster())
    t._s.script[("POST", "/fapi/v3/order")] = [(418, {"code": -1003, "msg": "banned"}, {"Retry-After": "120"})]
    assert t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c1-a1", False).status == "REJECTED"
    with pytest.raises(BannedError):
        t.ioc(SYM, "SELL", D(100), D("0.04"), "fb-X-e01-c2-a1", False, hedge=True)


def test_clock_offset_check(fake):
    t = mk(fake, now=lambda: fake.server_ms / 1000 - 0.5)
    assert t.check_clock() == pytest.approx(0.5)
    t = mk(fake, now=lambda: fake.server_ms / 1000 + 3)
    with pytest.raises(at.AsterError, match="NTP"):
        t.check_clock()


def test_is_perpleg_and_repr_hides_key(fake):
    t = mk(fake)
    assert isinstance(t, PerpLeg) and t.venue == "aster" and AsterTrade.BASE == tconfig.ASTER_BASE
    assert DEMO_KEY[2:] not in repr(t.signer) and DEMO_KEY[2:] not in json.dumps(t.health())


# ================================ связка со store (запись-до) ================================
def test_write_ahead_with_store_rows(fake, tmp_path):
    con = store.connect(tmp_path / "trade.db")
    t = mk(fake)
    cid = store.client_order_id("D7K2", "entry", 1, 1, 1)
    store.perp_order_intent(con, clip_id=None, client_id=cid, venue="aster", symbol=SYM, side="SELL",
                            reduce_only=False, tif="IOC", price=D("0.04089"), qty=D(12216))
    f = t.ioc(SYM, "SELL", D(12216), D("0.04089"), cid, False, hedge=True,
              on_signed=lambda n: store.perp_order_sent(con, cid, sign_nonce=n))
    assert store.get_perp_order(con, cid)["sign_nonce"] == f.sign_nonce
    assert store.record_perp_fill(con, f)
    row = store.get_perp_order(con, cid)
    assert (row["state"], row["executed_qty"], row["order_id"]) == ("FILLED", "12216", f.order_id)
    assert store.add_perp_fills(con, "aster", t.fills(SYM, None)) == 1
    assert store.add_perp_fills(con, "aster", t.fills(SYM, None)) == 0          # дедуп по trade_id
    fr = con.execute("SELECT commission_abs FROM perp_fills").fetchone()[0]
    assert not fr.startswith("-")
    fake.income = [{"symbol": SYM, "incomeType": "FUNDING_FEE", "income": "0.25", "asset": "USDT",
                    "time": fake.clock_ms, "tranId": 31, "info": ""}]
    t2 = mk(fake, now=lambda: (fake.clock_ms + 1000) / 1000)
    assert store.add_funding_income(con, "aster", t2.funding_income(SYM, fake.clock_ms - 1000)) == 1


def test_native_stop_is_buy_take_profit_reduce_only_and_new_is_proven_open(fake):
    t = mk(fake)
    cid = "fb-D7K2-s00-c1-a1"
    seen = []
    f = t.take_profit_on_fall(SYM, D(100), D("0.03"), cid, working_type="MARK_PRICE",
                              on_signed=seen.append)
    assert f.status == "OPEN" and f.order_id is not None and seen == [f.sign_nonce]
    p = [p for m, path, p in fake.log if m == "POST" and path == "/fapi/v3/order"][-1]
    assert (p["side"], p["type"], p["reduceOnly"], p["workingType"], p["stopPrice"]) == (
        "BUY", "TAKE_PROFIT_MARKET", "true", "MARK_PRICE", "0.03")
    assert t.query_conditional(SYM, cid).status == "OPEN"


def test_native_stop_is_durable_and_raises_reader_gate(tmp_path):
    con = store.connect(tmp_path / "trade.db")
    did = store.create_deal(con, coin="AIW3", chain="bsc", token="0x" + "a1" * 20, token_dec=18,
                            perp_venue="aster", symbol=SYM, leg_usd=D(10), owner_json="{}", sim=True)
    cid = store.client_order_id(did, "stop", 0, 1, 1)
    store.perp_order_intent(con, clip_id=None, client_id=cid, venue="aster", symbol=SYM, side="BUY",
                            reduce_only=True, tif="TAKE_PROFIT_MARKET", price=D("0.03"), qty=D(100))
    store.create_native_stop(con, deal_id=did, client_id=cid, venue="aster", symbol=SYM, trigger_price=D("0.03"),
                             working_type="MARK_PRICE", qty=D(100), now=1)
    assert store.get_native_stop(con, did)["state"] == "INTENT"
    assert store.schema_info(con)["min_reader"] == 6
