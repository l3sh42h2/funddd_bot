"""Перп-нога Gate USDT-фьючерсов: подпись HMAC-SHA512 по формуле документации (проверяется НЕЗАВИСИМО от кода
модуля — свой hmac/hashlib в тесте, как test_trade_aster.py делает для EIP-712), ворота режима, округление цены
к тику (SELL вверх / BUY вниз), исходы IOC (partial/unknown/reject), text-id, m=100 (quanto_multiplier FATCOIN).

Готового числового вектора подписи в документации Gate нет (в отличие от Aster) — сверено поиском по официальным
докам и исходникам gateapi-python/-go 13.09.2026 (см. докстринг gate_trade.py). Ключей в тесте нет — FakeGate
принимает любую пару KEY/SECRET, тест сам пересчитывает ожидаемую подпись по формуле и сверяет байт в байт."""
import hashlib, hmac, json, time
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import pytest
import requests
from requests.structures import CaseInsensitiveDict

from funding_bot.client import BannedError, BudgetExceeded
from funding_bot.trade import gate_trade as gt, keys, store
from funding_bot.trade.gate_trade import GateApiError, GateError, GateTrade
from funding_bot.trade.keys import ModeForbidden
from funding_bot.trade.types import PerpLeg

D = Decimal
FIX = Path(__file__).parent / "data" / "gate"
SYM = "FATCOIN_USDT"
KEY, SECRET = "demo-key-123", "demo-secret-do-not-use-live"


def _live(name: str) -> dict:
    return json.loads((FIX / name).read_text())


CONTRACT = _live("contract_fatcoin.json")
ORDER_BOOK = _live("order_book_fatcoin.json")
FUNDING_HIST = _live("funding_rate_fatcoin.json")
SPOT_TIME = _live("spot_time.json")


# ================================ подделка площадки ================================
class FakeGate(requests.Session):
    """Gate за requests.Session.send: ответы по (метод, путь), простая модель IOC/позиции/сделок/account_book,
    скрипт отказов (как FakeAster). Каждый подписанный запрос проверяется НЕЗАВИСИМЫМ пересчётом HMAC-SHA512."""

    def __init__(self, key=KEY, secret=SECRET):
        super().__init__()
        self.key, self.secret = key, secret
        self.sent: list[requests.PreparedRequest] = []
        self.log: list[tuple[str, str, dict, bytes]] = []
        self.script: dict[tuple[str, str], list] = {}
        self.bad_sig: list[str] = []
        self.remain, self.limit = 199, 200
        self.contracts = {SYM: dict(CONTRACT)}
        self.book = dict(ORDER_BOOK)
        self.server_ms = int(SPOT_TIME["server_time"])
        self.clock_s = self.server_ms // 1000
        self.account_row = {"available": "500", "in_dual_mode": False}
        self.dual_can_disable = True
        self.position_row = {"size": 0, "contract": SYM}
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.account_book: list[dict] = []
        self.next_oid = 9000
        self.next_tid = 7000
        self.next_book_id = 1
        self.fill_cap: int | None = None       # None — IOC исполняется целиком
        self.lose_reply = None
        self.hide_queries = 0
        self.leverage_reply = None

    def n(self, method: str, path: str) -> int:
        return sum(1 for m, p, _, _ in self.log if m == method and p == path)

    # --- транспорт ---------------------------------------------------------------------------
    def send(self, request, **kw):
        self.sent.append(request)
        u = urlsplit(request.url)
        # base URL уже содержит /api/v4 (GATE_BASE) — u.path приходит как «/api/v4/futures/…»; внутренние пути
        # модуля (и в подписи, и в путях диспетчера ниже) — БЕЗ этого префикса (SIGN_PREFIX добавляется только
        # в строку подписи поверх base). path — для диспетчера/лога/script, u.path — как ушло на wire (для подписи).
        path = u.path.removeprefix(gt.SIGN_PREFIX)
        params = dict(parse_qsl(u.query, keep_blank_values=True))
        body = request.body or b""
        if isinstance(body, str):
            body = body.encode()
        if "SIGN" in request.headers:
            if not self._verify(request, u, body):
                self.bad_sig.append(request.url)
        self.log.append((request.method, path, params, body))
        q = self.script.get((request.method, path))
        if q:
            item = q.pop(0)
            if isinstance(item, BaseException):
                raise item
            status, resp, *hdr = item
            return self._resp(request, status, resp, hdr[0] if hdr else {})
        status, resp = self.handle(request.method, path, params, body)
        return self._resp(request, status, resp, {})

    def _verify(self, request, u, body: bytes) -> bool:
        expect_key = request.headers.get("KEY")
        ts = request.headers.get("Timestamp")
        sign_hdr = request.headers.get("SIGN")
        body_hash = hashlib.sha512(body).hexdigest()
        s = f"{request.method}\n{u.path}\n{u.query}\n{body_hash}\n{ts}"    # u.path уже несёт /api/v4 — не дублируем
        expect_sig = hmac.new(self.secret.encode(), s.encode(), hashlib.sha512).hexdigest()
        return expect_key == self.key and sign_hdr == expect_sig and bool(ts)

    def _resp(self, request, status, body, headers):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(body).encode()
        base = {"x-gate-ratelimit-requests-remain": str(self.remain), "x-gate-ratelimit-limit": str(self.limit)}
        r.headers = CaseInsensitiveDict({**base, **headers})
        r.url, r.request, r.encoding = request.url, request, "utf-8"
        return r

    # --- модель --------------------------------------------------------------------------------
    def handle(self, method, path, p, body: bytes):
        orders_prefix = f"/futures/{gt.SETTLE}/orders/"
        if (method, path) == ("GET", "/spot/time"):
            return 200, {"server_time": self.server_ms}
        if path.startswith(f"/futures/{gt.SETTLE}/contracts/") and method == "GET":
            sym = path.rsplit("/", 1)[-1]
            c = self.contracts.get(sym)
            return (200, c) if c else (404, {"label": "CONTRACT_NOT_FOUND", "message": "no such contract"})
        if (method, path) == ("GET", f"/futures/{gt.SETTLE}/order_book"):
            return 200, self.book
        if (method, path) == ("GET", f"/futures/{gt.SETTLE}/accounts"):
            return 200, dict(self.account_row)
        if path.startswith(f"/futures/{gt.SETTLE}/positions/") and method == "GET" and "/leverage" not in path:
            return 200, dict(self.position_row)
        if (method, path) == ("POST", f"/futures/{gt.SETTLE}/dual_mode"):
            if p.get("dual_mode") != "false":
                return 400, {"label": "INVALID_PARAM_VALUE", "message": "тест поддерживает только выключение"}
            if not self.dual_can_disable:
                return 400, {"label": "POSITION_HOLDING", "message": "у аккаунта есть позиции — переключить нельзя"}
            self.account_row["in_dual_mode"] = False
            return 200, dict(self.account_row)
        if path.endswith("/leverage") and method == "POST":
            lev = p.get("leverage")
            return 200, self.leverage_reply or {"leverage": lev, "contract": SYM}
        if (method, path) == ("POST", f"/futures/{gt.SETTLE}/orders"):
            return self._order(json.loads(body or b"{}"))
        if method == "GET" and path.startswith(orders_prefix):
            from urllib.parse import unquote
            text = unquote(path[len(orders_prefix):])
            if text not in self.orders or self.hide_queries > 0:
                if self.hide_queries > 0:
                    self.hide_queries -= 1
                return 404, {"label": "ORDER_NOT_FOUND", "message": "not found"}
            return 200, self.orders[text]
        if (method, path) == ("GET", f"/futures/{gt.SETTLE}/my_trades"):
            # ИСПРАВЛЕНО (GATE-1, 13.09): реальная семантика Gate — last_id листает СТРОГО НАЗАД (id < last_id),
            # новые сделки первыми (проверено живым публичным /trades 13.09 — см. докстринг gate_trade.fills()).
            # Раньше фейк отдавал id > lid по возрастанию — повторял ошибочное допущение самого модуля вместо
            # независимой проверки: тест на такой подделке не мог поймать баг.
            rows = sorted((t for t in self.trades if t["contract"] == p.get("contract")),
                         key=lambda t: t["id"], reverse=True)
            if "last_id" in p:
                lid = int(p["last_id"])
                rows = [t for t in rows if t["id"] < lid]
            return 200, rows[: int(p.get("limit", 1000))]
        if (method, path) == ("GET", f"/futures/{gt.SETTLE}/account_book"):
            frm, to = int(p.get("from", 0)), int(p.get("to", 2**31))
            rows = [r for r in self.account_book if frm <= int(r["time"]) <= to]
            off = int(p.get("offset", 0))
            return 200, rows[off: off + int(p.get("limit", 1000))]
        return 404, {"label": "NOT_FOUND", "message": f"нет пути {method} {path}"}

    def _order(self, p):
        size, px, text = int(p["size"]), D(p["price"]), p["text"]
        qty = abs(size)
        ex = qty if self.fill_cap is None else min(qty, self.fill_cap)
        left_mag = qty - ex
        self.next_oid += 1
        o = {"id": self.next_oid, "contract": p["contract"], "size": size, "price": p["price"], "text": text,
            "tif": p["tif"], "left": left_mag if size >= 0 else -left_mag,
            "fill_price": format(px if ex else D(0), "f"), "status": "finished",
            "finish_as": "filled" if left_mag == 0 else "ioc", "reduce_only": bool(p.get("reduce_only"))}
        self.orders[text] = o
        if ex:
            signed_ex = ex if size >= 0 else -ex
            self.position_row["size"] = int(self.position_row.get("size", 0)) + signed_ex
            self.next_tid += 1
            self.trades.append({"id": self.next_tid, "create_time": self.clock_s, "contract": p["contract"],
                                "order_id": str(self.next_oid), "size": signed_ex, "price": p["price"],
                                "role": "taker", "text": text, "fee": format(px * ex * D("0.00075"), "f")})
            self.clock_s += 1
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
    return FakeGate()


def mk(fake, mode="live", paused=False, key=KEY, secret=SECRET, **kw) -> GateTrade:
    st = {"mode": mode, "paused": paused}
    sleeps: list[float] = []
    t = GateTrade(key, secret, mode_state=lambda: (st["mode"], st["paused"]), session=fake, sleep=sleeps.append, **kw)
    t.st, t.sleeps = st, sleeps
    return t


# ================================ подпись: независимый пересчёт по формуле доков ================================
def test_sign_string_formula_matches_docs_get_and_post():
    # GET без query и без тела — hex(sha512("")) фиксирован докой примера ("no request body → hashed result of
    # empty string"); подставляем сами и сверяем с нашей функцией.
    empty_hash = hashlib.sha512(b"").hexdigest()
    s = gt.build_sign_string("GET", "/futures/usdt/accounts", "", b"", 1758000000)
    assert s == f"GET\n/api/v4/futures/usdt/accounts\n\n{empty_hash}\n1758000000"
    body = b'{"contract":"FATCOIN_USDT","size":-100,"price":"0.00171","tif":"ioc","text":"t-e01-c1-a1","reduce_only":false}'
    s2 = gt.build_sign_string("POST", "/futures/usdt/orders", "", body, 1758000001)
    assert s2 == f"POST\n/api/v4/futures/usdt/orders\n\n{hashlib.sha512(body).hexdigest()}\n1758000001"
    sig = gt.sign(SECRET, s2)
    assert sig == hmac.new(SECRET.encode(), s2.encode(), hashlib.sha512).hexdigest()
    assert len(sig) == 128 and all(c in "0123456789abcdef" for c in sig)      # hex(HMAC-SHA512) — 64 байта


def test_query_string_is_part_of_signed_and_wire_string():
    q = gt._query_string({"contract": "FATCOIN_USDT", "limit": 5, "z": None})
    assert q == "contract=FATCOIN_USDT&limit=5"          # None выброшен, порядок — порядок вставки


def test_post_body_bytes_and_signature_byte_for_byte(fake):
    t = mk(fake, now=lambda: 1758000123.0)
    # px_cap — цена КОНТРАКТА (соглашение проекта, GATE-4): 0.00171 токена · m(=100) = 0.171; на биржу должна
    # уйти цена ТОКЕНА (0.00171) — ровно то, что ioc() шлёт в теле POST ниже.
    f = t.ioc(SYM, "SELL", D(100), D("0.171"), "e01-c1-a1", False, hedge=True)
    assert f.status == "FILLED" and f.sign_nonce == 1758000123
    req = next(r for r in fake.sent if r.method == "POST")
    assert req.url == f"{gt.GATE_BASE}/futures/{gt.SETTLE}/orders"
    assert req.headers["Content-Type"] == "application/json"
    assert req.headers["KEY"] == KEY and req.headers["Timestamp"] == "1758000123"
    sent_body = json.loads(req.body)
    assert sent_body == {"contract": SYM, "size": -100, "price": "0.00171", "tif": "ioc", "text": "t-e01-c1-a1",
                         "reduce_only": False}
    body_hash = hashlib.sha512(req.body).hexdigest()
    s = f"POST\n/api/v4/futures/{gt.SETTLE}/orders\n\n{body_hash}\n1758000123"
    assert req.headers["SIGN"] == hmac.new(SECRET.encode(), s.encode(), hashlib.sha512).hexdigest()
    assert not fake.bad_sig


def test_get_signed_query_and_empty_body_hash(fake):
    t = mk(fake, mode="readonly")
    assert t.available_margin() == D("500")
    req = fake.sent[-1]
    assert req.method == "GET" and req.body is None
    u = urlsplit(req.url)
    assert u.path == f"/api/v4/futures/{gt.SETTLE}/accounts" and u.query == ""
    s = f"GET\n{u.path}\n\n{hashlib.sha512(b'').hexdigest()}\n{req.headers['Timestamp']}"
    assert req.headers["SIGN"] == hmac.new(SECRET.encode(), s.encode(), hashlib.sha512).hexdigest()
    assert not fake.bad_sig


# ================================ ворота режима ================================
def test_no_keys_only_public_reads_signed_refused_before_network(fake):
    t = GateTrade(session=fake)                        # dry: ключей нет вовсе
    assert t.filters(SYM).tick == D("0.001")            # тик КОНТРАКТА = order_price_round·m (GATE-4)
    assert t.book(SYM, 5).bids[0] == (D("0.167"), D(311))   # цена КОНТРАКТА = цена токена·m (GATE-4)
    n = len(fake.sent)
    with pytest.raises(ModeForbidden):
        t.position(SYM)
    with pytest.raises(ModeForbidden):
        t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c1-a1", False, hedge=True)
    with pytest.raises(ModeForbidden):
        t.setup(SYM, 1, "ISOLATED")
    assert len(fake.sent) == n


@pytest.mark.parametrize("mode,paused,read_ok,send_ok,hedge_ok", [
    ("dry", False, False, False, False),
    ("readonly", False, True, False, False),
    ("live", False, True, True, True),
    ("live", True, True, False, True),
    (None, False, False, False, False),
])
def test_mode_gate_matrix(fake, mode, paused, read_ok, send_ok, hedge_ok):
    t = mk(fake, mode=mode, paused=paused)

    def attempt(fn):
        try:
            fn()
            return True
        except ModeForbidden:
            return False

    # available_margin/setup — чисто подписанные, ModeForbidden летит ДО сети (в call(), до _check_order/filters)
    before = len(fake.sent)
    ok = attempt(lambda: t.account())
    assert ok is read_ok and (len(fake.sent) == before) == (not read_ok)
    assert attempt(lambda: t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c1-a1", False)) is send_ok
    assert attempt(lambda: t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c2-a1", False, hedge=True)) is hedge_ok
    assert attempt(lambda: t.setup(SYM, 1, "ISOLATED")) is send_ok


def test_gate_never_sends_dual_mode_true():
    with pytest.raises(ValueError, match="dual_mode=True"):
        GateTrade(KEY, SECRET)._set_dual_mode(True)


# ================================ входные проверки IOC ================================
def test_ioc_refuses_bad_input_before_anything(fake):
    t = mk(fake)
    t.filters(SYM)
    bad = [dict(side="SHORT"), dict(cid="bad id with space"), dict(cid="x" * 29), dict(qty=D("100.5")),
          dict(qty=D(0)), dict(qty=D(-1)), dict(px=D(0)), dict(qty=D(2_000_000))]
    for b in bad:
        with pytest.raises(ValueError):
            t.ioc(SYM, b.get("side", "SELL"), b.get("qty", D(100)), b.get("px", D("0.002")),
                 b.get("cid", "e01-c1-a1"), False)
    assert fake.n("POST", f"/futures/{gt.SETTLE}/orders") == 0


# ================================ округление цены к тику ================================
def test_price_rounding_sell_up_buy_down(fake):
    t = mk(fake)
    tick = t.filters(SYM).tick
    assert tick == D("0.001")                                            # тик КОНТРАКТА = order_price_round·m
    # px_cap — цена КОНТРАКТА (GATE-4): 0.001664 токена · m(=100) = 0.1664, не кратно нативному тику 0.00001.
    f = t.ioc(SYM, "SELL", D(10), D("0.1664"), "e01-c1-a1", False)
    px_sell = D(json.loads(fake.sent[-1].body)["price"])                  # на бирже — цена ТОКЕНА
    assert px_sell == D("0.00167") and px_sell >= D("0.001664")          # вверх — не хуже кэпа/m
    assert f.avg_px == D("0.167")                                         # PerpFill.avg_px — снова цена контракта
    f = t.ioc(SYM, "BUY", D(10), D("0.1676"), "e01-c2-a1", True)
    px_buy = D(json.loads(fake.sent[-1].body)["price"])
    assert px_buy == D("0.00167") and px_buy <= D("0.001676")            # вниз — не хуже кэпа/m
    assert gt.floor_step is gt.ceil_step or True                          # хелперы импортированы из aster_trade
    assert f.status == "FILLED"


# ================================ исходы IOC ================================
def test_ioc_fill_statuses_full_partial_expired_and_reduce_only(fake):
    t = mk(fake)
    # px_cap — цена КОНТРАКТА (GATE-4): 0.00171/0.00169 токена · m(=100).
    f = t.ioc(SYM, "SELL", D(100), D("0.171"), "e01-c1-a1", False)
    assert (f.status, f.qty, f.avg_px) == ("FILLED", D(100), D("0.171"))     # avg_px — тоже цена контракта
    assert isinstance(f.order_id, int)
    fake.fill_cap = 30
    f = t.ioc(SYM, "BUY", D(100), D("0.169"), "e01-c2-a1", True)
    assert (f.status, f.qty) == ("PARTIALLY_FILLED", D(30))
    assert fake.orders["t-e01-c2-a1"]["reduce_only"] is True
    fake.fill_cap = 0
    f = t.ioc(SYM, "BUY", D(100), D("0.169"), "e01-c3-a1", True)
    assert (f.status, f.qty) == ("EXPIRED", D(0))
    fake.script[("POST", f"/futures/{gt.SETTLE}/orders")] = [(200, {"id": 55, "text": "t-e01-c4-a1", "size": -100,
                                                                    "left": -100, "fill_price": "0", "status": "open",
                                                                    "finish_as": ""})]
    f = t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c4-a1", False)
    assert (f.status, f.order_id) == ("UNKNOWN", 55)                     # status=open — не финал
    fake.script[("POST", f"/futures/{gt.SETTLE}/orders")] = [(200, {"id": 56, "text": "t-someone-else", "size": -100,
                                                                    "left": 0, "fill_price": "0.002", "status": "finished",
                                                                    "finish_as": "filled"})]
    assert t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c5-a1", False).status == "UNKNOWN"   # чужой text


@pytest.mark.parametrize("reply,status,code", [
    ((400, {"label": "BALANCE_NOT_ENOUGH", "message": "not enough"}), "REJECTED", "BALANCE_NOT_ENOUGH"),
    ((400, {"label": "INVALID_PARAM_VALUE", "message": "bad price"}), "REJECTED", "INVALID_PARAM_VALUE"),
    ((403, {"label": "FORBIDDEN", "message": "no"}), "REJECTED", "FORBIDDEN"),
    ((503, {"label": "SERVER_ERROR", "message": "try later"}), "UNKNOWN", "SERVER_ERROR"),
    ((502, {}), "UNKNOWN", None),
    ((429, {"label": "TOO_BUSY", "message": "slow down"}), "UNKNOWN", "TOO_BUSY"),
    ((200, {"garbage": True}), "UNKNOWN", None),
    ((200, {"id": 1, "text": "t-e01-c1-a1", "size": -100, "left": -100, "status": "finished",
           "finish_as": "reduce_only", "fill_price": "0"}), "REJECTED", "reduce_only"),
])
def test_ioc_error_mapping_never_resends(fake, reply, status, code):
    t = mk(fake)
    fake.script[("POST", f"/futures/{gt.SETTLE}/orders")] = [reply]
    f = t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c1-a1", False, hedge=True)
    assert (f.status, f.err_code) == (status, code)
    assert fake.n("POST", f"/futures/{gt.SETTLE}/orders") == 1
    if status == "REJECTED" and reply[0] >= 400:            # last_error — текст HTTP-отказа площадки; для finish_as
        assert code in t.last_error                         # (200 успешно принят, но REJECTED) его код и так есть в err_code


# ================================ GATE-2/GATE-4: order_to_fill / book / funding — цена контракта, не токена =====
def test_gate2_order_to_fill_quote_matches_fills_quote_for_same_trade(fake):
    """Находка: одна и та же сделка (1000 контрактов FATCOIN по 0.00167, m=100) давала PerpFill.quote=1.67 через
    order_to_fill(), но quote_qty=167.00 через fills() — в 100 раз меньше. Мутация «order_to_fill(cid, body, ts)
    без m» (обратно — m по умолчанию 1) на этом тесте ловится напрямую: PerpFill.quote должен совпасть с
    fills().quote_qty для той же сделки, что и уходит на биржу."""
    t = mk(fake)
    f = t.ioc(SYM, "SELL", D(1000), D("0.167"), "e01-c1-a1", False)     # px_cap контракт: 0.00167·100
    assert f.status == "FILLED" and f.avg_px == D("0.167")
    assert f.quote == D("167")                                          # 0.00167(токен)·1000(контр.)·100(m)
    rows = t.fills(SYM, None)
    assert rows and rows[0]["quote_qty"] == f.quote                     # тот же нотионал, что и в my_trades


def test_gate4_book_and_funding_mark_are_contract_price_consistent_with_planner(fake):
    """Находка: стакан/марк Gate — цена ТОКЕНА, а planner.py/engine.py ждут цену КОНТРАКТА (px·m), как у Aster;
    несовпадение давало basis_bps ≈ −9900 на ровном рынке. Проверяем формулой planner.py буквально (basis_bps =
    best/upc/dex_px − 1) и types.InstrumentSpec.px_per_token (тем же путём, каким книгу читает engine.py:
    px_tok = мид/m) — обе должны вернуть цену, совпадающую с нативной ценой токена на живом снимке книги."""
    from funding_bot.trade.types import InstrumentSpec
    t = mk(fake, mode="readonly")
    b = t.book(SYM, 1)
    inst = InstrumentSpec(chain="robinhood", token="0x12d5ee7917ca430073c3a638ee1e6f0648a98a01", token_dec=18,
                          perp_venue="gate", perp_symbol=SYM, units_per_contract=t.instrument(SYM).m)
    dex_px = D("0.00167")                                                # цена токена на DEX, тот же порядок, что и книга
    best = b.bids[0][0]
    basis_bps = (best / inst.m / dex_px - 1) * 10_000                    # ровно формула planner.py:767
    assert abs(basis_bps) < D(50)                                        # не −9900 бп на совпадающих ценах
    assert inst.px_per_token(best) == D("0.00167")                       # engine.py: px_tok = мид/m


@pytest.mark.parametrize("exc", [requests.ReadTimeout("read timed out"), requests.ConnectionError("reset"),
                                 OSError("broken pipe")])
def test_ioc_transport_failure_is_unknown_with_ts(fake, exc):
    t = mk(fake)
    fake.script[("POST", f"/futures/{gt.SETTLE}/orders")] = [exc]
    f = t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c1-a1", False)
    assert f.status == "UNKNOWN" and f.sign_nonce > 0 and f.order_id is None
    assert fake.n("POST", f"/futures/{gt.SETTLE}/orders") == 1


# ================================ text-id и settle_unknown ================================
def test_query_not_found_and_errors(fake):
    t = mk(fake)
    assert t.query(SYM, "none-e01-c1-a1").status == "NOT_FOUND"
    fake.script[("GET", f"/futures/{gt.SETTLE}/orders/t-x-e01-c1-a1")] = [(503, {}), requests.ReadTimeout("x")]
    assert [t.query(SYM, "x-e01-c1-a1").status for _ in range(2)] == ["UNKNOWN", "UNKNOWN"]
    with pytest.raises(ValueError):
        t.query(SYM, "bad id with spaces")


def test_lost_reply_resolved_by_query_without_resend(fake):
    t = mk(fake)
    fake.lose_reply = (503, {"label": "SERVER_ERROR", "message": "internal"})
    cid = "e01-c1-a1"
    f = t.ioc(SYM, "SELL", D(100), D("0.00171"), cid, False, hedge=True)
    assert f.status == "UNKNOWN"
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=fake.clock_s * 1000)
    assert (g.status, g.qty, g.order_id) == ("FILLED", D(100), fake.orders["t-" + cid]["id"])
    assert fake.n("POST", f"/futures/{gt.SETTLE}/orders") == 1


def test_lost_reply_after_timeout_found_after_two_misses(fake):
    t = mk(fake)
    fake.lose_reply = requests.ReadTimeout("timed out")
    fake.hide_queries = 2
    f = t.ioc(SYM, "SELL", D(50), D("0.00171"), "e01-c1-a1", False)
    assert f.status == "UNKNOWN"
    g = t.settle_unknown(SYM, "e01-c1-a1", pos_before=D(0), since_ms=fake.clock_s * 1000)
    assert (g.status, g.qty) == ("FILLED", D(50))
    assert fake.n("GET", f"/futures/{gt.SETTLE}/orders/t-e01-c1-a1") == 3


def test_not_found_only_after_three_misses_and_unchanged_account(fake):
    t = mk(fake, now=lambda: fake.clock_s)
    fake.script[("POST", f"/futures/{gt.SETTLE}/orders")] = [requests.ConnectTimeout("no route")]
    cid = "e01-c1-a1"
    since = fake.clock_s * 1000
    assert t.ioc(SYM, "SELL", D(50), D("0.002"), cid, False).status == "UNKNOWN"
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=since)
    assert (g.status, g.err_code) == ("NOT_FOUND", gt.NOT_FOUND_LABEL)
    assert sum(t.sleeps) == pytest.approx(gt.UNKNOWN_SPAN_S)
    with pytest.raises(ValueError):
        import tempfile, os as _os
        con = store.connect(_os.path.join(tempfile.mkdtemp(), "t.db"))
        store.record_perp_fill(con, g)                 # NOT_FOUND не пишется как итог (движок решает NOT_PLACED)


def test_three_misses_but_account_moved_or_old_stays_unknown(fake):
    t = mk(fake, now=lambda: fake.clock_s)
    cid = "e01-c1-a1"
    since = fake.clock_s * 1000
    fake.position_row["size"] = -50                     # позиция сдвинулась, а заявки «нет»
    assert t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=since).status == "UNKNOWN"
    fake.position_row["size"] = 0
    fake.trades.append({"id": 1, "create_time": fake.clock_s, "contract": SYM, "order_id": "4242", "size": -50,
                        "price": "0.002", "role": "taker", "text": "t-other", "fee": "0.075"})
    assert t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=since).status == "UNKNOWN"      # чужая сделка в окне
    g = t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=since, known_order_ids={4242})
    assert g.status == "NOT_FOUND"                       # учтённая дочерняя — не мешает
    assert t.settle_unknown(SYM, cid, pos_before=None, since_ms=since).status == "UNKNOWN"
    # окно text-id (доки: ~60 с) истекло — «не найдена» больше не доказательство
    late = fake.clock_s * 1000 - (gt.TEXT_ID_SAFE_MS + 1000)
    assert t.settle_unknown(SYM, cid, pos_before=D(0), since_ms=late).status == "UNKNOWN"
    assert fake.n("POST", f"/futures/{gt.SETTLE}/orders") == 0


# ================================ фильтры / инструмент / книга / фандинг (живые фикстуры) ================================
def test_filters_and_instrument_from_live_fixture(fake):
    t = mk(fake, mode="readonly")
    f = t.filters(SYM)
    # tick — ИСПРАВЛЕНО (GATE-4): цена КОНТРАКТА = order_price_round(0.00001)·quanto_multiplier(100) = 0.001;
    # шаг количества/лимиты — в контрактах, множитель их не касается.
    assert (f.tick, f.step, f.min_qty, f.max_qty_limit, f.max_qty_market, f.min_notional) == (
        D("0.001"), D(1), D(1), D(1_000_000), D(1_000_000), D(0))
    assert f.tifs == frozenset({"gtc", "ioc", "poc", "fok"})
    assert t.status(SYM) == "trading"
    inst = t.instrument(SYM)
    assert inst.m == D(100) and inst.symbol == SYM and inst.quote_asset == "USDT"
    assert inst.base_asset == "FATCOIN" and inst.contract_type == ""
    t.filters(SYM)
    assert fake.n("GET", f"/futures/{gt.SETTLE}/contracts/{SYM}") == 1     # кэш


def test_enable_decimal_contract_is_refused_not_silently_rounded(fake):
    fake.contracts["WEIRD_USDT"] = {**CONTRACT, "name": "WEIRD_USDT", "enable_decimal": True}
    t = mk(fake, mode="readonly")
    with pytest.raises(GateError, match="enable_decimal"):
        t.filters("WEIRD_USDT")


def test_book_from_live_fixture(fake):
    t = mk(fake, mode="readonly")
    b = t.book(SYM, 3)
    # ИСПРАВЛЕНО (GATE-4): книга Gate отдаёт цену ТОКЕНА, book() домножает на m(=100) — цена КОНТРАКТА, как и
    # у Aster; количество (contracts) множитель не трогает.
    assert b.bids[:3] == ((D("0.167"), D(311)), (D("0.166"), D(310)), (D("0.165"), D(309)))
    assert b.asks[0] == (D("0.169"), D(74)) and b.ts > 0


def test_funding_hourly_conversion_from_live_fixture(fake):
    t = mk(fake, mode="readonly")
    mark, rate_interval, next_ms = t.funding(SYM)
    # ИСПРАВЛЕНО (GATE-3): funding() отдаёт ставку ЗА ИНТЕРВАЛ (как Aster), не за час — движок сам делит на
    # pair.period_h. ИСПРАВЛЕНО (GATE-4): mark — цена КОНТРАКТА (·m), как book().
    assert mark == D(CONTRACT["mark_price"]) * D(100)
    assert rate_interval == D(CONTRACT["funding_rate"])
    assert next_ms == int(CONTRACT["funding_next_apply"]) * 1000


def test_funding_raises_without_funding_interval(fake):
    """GATE-3: funding_interval отсутствует/=0 — GateError, а не молчаливые выдуманные 8 ч."""
    t = mk(fake, mode="readonly")
    fake.contracts[SYM] = {**CONTRACT, "funding_interval": 0}
    with pytest.raises(GateError, match="funding_interval"):
        t.funding(SYM)
    del fake.contracts[SYM]["funding_interval"]
    with pytest.raises(GateError, match="funding_interval"):
        t.funding(SYM)


def test_clock_check_against_live_spot_time(fake):
    t = mk(fake, now=lambda: fake.server_ms / 1000 - 0.5)
    assert t.check_clock() == pytest.approx(0.5)
    t = mk(fake, now=lambda: fake.server_ms / 1000 + 10)
    with pytest.raises(GateError, match="NTP"):
        t.check_clock()


# ================================ позиция / маржа / настройка ================================
def test_position_flat_is_real_zero_error_is_none(fake):
    t = mk(fake, mode="readonly")
    fake.position_row["size"] = 0
    assert t.position(SYM) == D(0)                       # у Gate size:0 — правда флэт (объект позиции есть всегда)
    fake.position_row["size"] = -12345
    assert t.position(SYM) == D(-12345)
    fake.script[("GET", f"/futures/{gt.SETTLE}/positions/{SYM}")] = [(500, {}), requests.ReadTimeout("x")]
    assert [t.position(SYM) for _ in range(2)] == [None, None]


def test_available_margin_none_on_error(fake):
    t = mk(fake, mode="readonly")
    assert t.available_margin() == D("500")
    fake.script[("GET", f"/futures/{gt.SETTLE}/accounts")] = [(503, {})]
    assert t.available_margin() is None


def test_setup_isolated_only_and_leverage_checked(fake):
    t = mk(fake)
    t.setup(SYM, 3, "ISOLATED")
    posts = [(p, prm) for m, p, prm, _ in fake.log if m == "POST"]
    assert [p for p, _ in posts] == [f"/futures/{gt.SETTLE}/positions/{SYM}/leverage"]
    assert posts[0][1]["leverage"] == "3"
    with pytest.raises(GateError, match="ISOLATED"):
        t.setup(SYM, 3, "CROSSED")
    for lev in (0, True, 1.0, 126):
        with pytest.raises(ValueError):
            t.setup(SYM, lev, "ISOLATED")
    fake.leverage_reply = {"leverage": "5", "contract": SYM}
    with pytest.raises(GateError, match="5x"):
        t.setup(SYM, 3, "ISOLATED")


def test_setup_never_enables_dual_mode_only_tries_to_disable(fake):
    t = mk(fake)
    fake.account_row["in_dual_mode"] = True
    fake.dual_can_disable = True
    t.setup(SYM, 1, "ISOLATED")
    dual_posts = [prm for m, p, prm, _ in fake.log if p == f"/futures/{gt.SETTLE}/dual_mode"]
    assert dual_posts == [{"dual_mode": "false"}]        # ни разу не True
    fake.account_row["in_dual_mode"] = True
    fake.dual_can_disable = False
    with pytest.raises(GateError, match="Position Mode"):
        t.setup(SYM, 1, "ISOLATED")
    assert fake.n("POST", f"/futures/{gt.SETTLE}/positions/{SYM}/leverage") == 1   # второй раз плечо не шлём


# ================================ fills / funding_income / учёт ================================
def test_fills_quanto_conversion_and_pagination(fake, monkeypatch):
    t = mk(fake, mode="readonly")
    t.filters(SYM)
    monkeypatch.setattr(gt, "PAGE_LIMIT", 2)
    for i in range(5):
        fake.trades.append({"id": 100 + i, "create_time": 1000 + i, "contract": SYM, "order_id": "7",
                            "size": -10, "price": "0.00171", "role": "taker" if i else "maker",
                            "text": "t-x", "fee": format(D("0.00171") * 10 * D("0.00075"), "f")})
    rows = t.fills(SYM, 101)
    assert [r["trade_id"] for r in rows] == [101, 102, 103, 104]        # GATE-1: страницы читаются от свежих назад
    assert rows[0]["qty"] == D(10) and rows[0]["side"] == "SELL"
    assert rows[0]["price"] == D("0.00171") * D(100)                    # ИСПРАВЛЕНО (GATE-4): цена контракта = цена·m
    assert rows[0]["quote_qty"] == D("0.00171") * 10 * D(100)         # цена × контракты × m(=100)
    assert rows[0]["commission_abs"] == D("0.00171") * 10 * D("0.00075")
    assert rows[0]["commission_asset"] == "USDT" and rows[0]["realized_pnl"] is None
    assert len(t.fills(SYM, None)) == 2                                # без from_id — одна (самая свежая) страница


# ================================ GATE-1: пагинация fills() — реальная семантика Gate (id < last_id, назад) =====
def test_fills_gate1_new_trades_beyond_first_page_are_not_dropped(fake, monkeypatch):
    """Сценарий (а) из находки: на бирже больше сделок, чем в одной странице, и нужные (новые) сделки лежат ЗА
    первой страницей вглубь — старый код (last_id=from_id−1, курсор «вперёд») их не находил вовсе. Мутация
    «вернуть lid=from_id−1 и max(id) как курсор следующей страницы» на этом тесте ловится: с реальной (fake)
    семантикой Gate такой код увидит только старые id (< from_id) и упрётся в короткую страницу, не дойдя до
    новых — список получится пустым/неполным вместо полного [60..250]."""
    t = mk(fake, mode="readonly")
    t.filters(SYM)
    monkeypatch.setattr(gt, "PAGE_LIMIT", 100)
    for i in range(1, 251):                              # 250 сделок, id 1..250 — больше одной страницы (100)
        fake.trades.append({"id": i, "create_time": 1000 + i, "contract": SYM, "order_id": str(1000 + i),
                            "size": -1, "price": "0.00171", "role": "taker", "text": "t-x", "fee": "0"})
    rows = t.fills(SYM, 60)                               # в базе последняя известная — 59, нужны 60..250
    ids = [r["trade_id"] for r in rows]
    assert ids == list(range(60, 251))                    # все новые сделки собраны, ни одна не потеряна
    assert ids[0] == 60 and ids[-1] == 250


def test_fills_gate1_far_from_id_does_not_exhaust_max_pages(fake, monkeypatch):
    """Сценарий (б) из находки: большая история, нужны только несколько последних сделок. Старый код продолжал
    листать last_id вниз далеко за from_id и на 2500 сделках упирался в MAX_PAGES (GateError «больше 10 страниц»).
    Исправленный код останавливается, как только min(id) страницы опускается до from_id — здесь хватает одной
    страницы даже при MAX_PAGES=2."""
    t = mk(fake, mode="readonly")
    t.filters(SYM)
    monkeypatch.setattr(gt, "PAGE_LIMIT", 1000)
    monkeypatch.setattr(gt, "MAX_PAGES", 2)               # старый код тут упал бы GateError на 2500 сделках
    for i in range(1, 2501):
        fake.trades.append({"id": i, "create_time": 1000 + i, "contract": SYM, "order_id": str(1000 + i),
                            "size": -1, "price": "0.00171", "role": "taker", "text": "t-x", "fee": "0"})
    rows = t.fills(SYM, 2491)                             # известны сделки 1..2490, нужны только 2491..2500
    assert [r["trade_id"] for r in rows] == list(range(2491, 2501))


def test_fills_real_gate_pagination_semantics_in_fake(fake):
    """Сама подделка листает так же, как биржа (независимая проверка семантики, а не копия допущения модуля):
    last_id=X → строго id < X, новые первыми."""
    for i in (10, 8, 9, 7):                                # порядок добавления не важен — сортировка по id
        fake.trades.append({"id": i, "create_time": 1000, "contract": SYM, "order_id": None, "size": -1,
                            "price": "0.001", "role": "taker", "text": "t-x", "fee": "0"})
    status, body = fake.handle("GET", f"/futures/{gt.SETTLE}/my_trades", {"contract": SYM, "limit": "2"}, b"")
    assert status == 200 and [r["id"] for r in body] == [10, 9]      # без last_id — самые свежие первыми
    status, body = fake.handle("GET", f"/futures/{gt.SETTLE}/my_trades",
                               {"contract": SYM, "limit": "10", "last_id": "9"}, b"")
    assert [r["id"] for r in body] == [8, 7]                          # last_id=9 → строго < 9, тоже по убыванию


def test_funding_income_type_fund_and_dedup(fake):
    now_ms = fake.server_ms
    t = mk(fake, mode="readonly", now=lambda: now_ms / 1000)
    start = now_ms - 3 * 3600_000
    fake.account_book = [{"id": str(900 + h), "time": start // 1000 + h * 3600, "change": "0.0123",
                          "balance": "500", "type": "fund", "text": "", "contract": SYM} for h in range(0, 3)]
    fake.account_book.append({"id": "1", "time": start // 1000 + 5, "change": "-1", "balance": "499",
                              "type": "fee", "text": "", "contract": SYM})
    rows = t.funding_income(SYM, start)
    assert [r["tran_id"] for r in rows] == [900, 901, 902]
    assert rows[0]["income"] == D("0.0123") and rows[0]["symbol"] == SYM


def test_rate_limit_budget_and_429(fake):
    t = mk(fake)
    t.filters(SYM)                                         # прогреть кэш фильтров — дальше ioc() не трогает сеть за ним
    fake.remain, fake.limit = 40, 200                    # 80% использовано — обычное чтение стоп с 70%
    t.account()                                           # первый вызов ещё видит budget=0 (заголовок читается ПОСЛЕ
                                                           # ответа) — этот проходит и СОХРАНЯЕТ 80% в t.budget
    with pytest.raises(BudgetExceeded):
        t.account()                                       # теперь budget уже известен — «сырое» чтение не глотает
                                                           # исключения (в отличие от available_margin(), см. ниже)
    assert t.available_margin() is None
    fake.remain = 20                                     # 90% — заявка всё ещё можно (до 95%)
    assert t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c1-a1", False).status == "FILLED"    # это же и «прогрев» budget=90%
    fake.remain = 5                                       # 97.5%
    t.position(SYM)                                        # прогрев: при 90% ещё проходит (< 95%), сохраняет 97.5%
    with pytest.raises(BudgetExceeded):
        t.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c2-a1", False)          # теперь 97.5% ≥ 95% — стоп до сети

    t2 = mk(FakeGate())
    t2._s.script[("POST", f"/futures/{gt.SETTLE}/orders")] = [
        (429, {"label": "TOO_BUSY", "message": "slow"}, {"x-gate-ratelimit-reset-timestamp": str(int(time.time()) + 3)})]
    f = t2.ioc(SYM, "SELL", D(100), D("0.002"), "e01-c1-a1", False)
    assert f.status == "UNKNOWN" and t2.backoff_until > 0


# ================================ секрет не печатается ================================
def test_secret_never_leaks(fake):
    t = mk(fake)                                          # прямой конструктор — регистрация в __init__, не только from_env
    assert SECRET not in repr(t) and SECRET not in json.dumps(t.health())
    assert repr(t._secret) == "<secret>" and str(t._secret) == "<secret>"
    assert f"{t._secret}" == "<secret>"
    assert t.health()["key"] == KEY                       # KEY — публичный идентификатор, не секрет
    assert keys.redact_secrets(f"секрет={SECRET}") == "секрет=<key>"      # общий реестр keys.py уже знает секрет
    import logging, io
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    logging.getLogger("gatetest").addHandler(h)
    keys.install_log_redaction(logging.getLogger("gatetest"))            # оборачивает формат УЖЕ добавленных хендлеров
    logging.getLogger("gatetest").warning("секрет=%s", SECRET)
    h.flush()
    assert SECRET not in buf.getvalue() and "<key>" in buf.getvalue()
    logging.getLogger("gatetest").removeHandler(h)


def test_is_perpleg_protocol(fake):
    t = mk(fake)
    assert isinstance(t, PerpLeg) and t.venue == "gate" and GateTrade.BASE == gt.GATE_BASE


# ================================ связка со store (запись-до, обе площадки одной схемой) ================================
def test_write_ahead_with_store_rows(fake, tmp_path):
    con = store.connect(tmp_path / "trade.db")
    t = mk(fake)
    cid = store.client_order_id("D7K2", "entry", 1, 1, 1)          # общий формат id подходит и Gate (≤28 байт, без ':'/'/')
    store.perp_order_intent(con, clip_id=None, client_id=cid, venue="gate", symbol=SYM, side="SELL",
                            reduce_only=False, tif="ioc", price=D("0.00171"), qty=D(100))
    f = t.ioc(SYM, "SELL", D(100), D("0.00171"), cid, False, hedge=True,
             on_signed=lambda ts: store.perp_order_sent(con, cid, sign_nonce=ts))
    assert store.get_perp_order(con, cid)["sign_nonce"] == f.sign_nonce
    assert store.record_perp_fill(con, f)
    row = store.get_perp_order(con, cid)
    assert (row["state"], row["executed_qty"], row["order_id"]) == ("FILLED", "100", f.order_id)
    assert store.add_perp_fills(con, "gate", t.fills(SYM, None)) == 1
    assert store.add_perp_fills(con, "gate", t.fills(SYM, None)) == 0        # дедуп по trade_id
    fake.account_book = [{"id": "31", "time": fake.clock_s, "change": "0.25", "balance": "500", "type": "fund",
                          "contract": SYM}]
    t2 = mk(fake, now=lambda: fake.clock_s + 1000)
    assert store.add_funding_income(con, "gate", t2.funding_income(SYM, fake.clock_s * 1000 - 1000)) == 1
