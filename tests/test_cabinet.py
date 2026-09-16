"""Кабинет владельца (только чтение): scrypt-хэш и CLI, выключенный кабинет, вход и выход, флаги cookie, перебор и 429,
CF-Connecting-IP только с 127.0.0.1, CSRF, страница сделок на временной trade.db, экранирование, заголовки, чистота
/data.json и базы (соединение только на чтение). Пароли здесь фейковые."""
from __future__ import annotations
import base64, hashlib, http.client, io, json, os, re, sqlite3, subprocess, threading
from decimal import Decimal as D
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
import pytest
from funding_bot import cabinet, cli, config, serve
from funding_bot.trade import owner, store

JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
LOGIN = "owner-test"
PW = "fake-test-password-9Qx"              # фейковый пароль — только для тестов
HASH = cabinet.make_hash(PW)
T0 = 1_757_700_000.0
TOK_A, TOK_B, TOK_C = "0x" + "a1" * 20, "0x" + "b2" * 20, "0x" + "c3" * 20
E18 = 10 ** 18
XSS_COIN = "<script>alert(1)</script>"


class Clock:
    def __init__(self, t=T0 + 3600):
        self.t = t

    def __call__(self):
        return self.t


def make_cab(tmp_path, env=None, clock=None, sleeps=None, db=None):
    env = {"CABINET_LOGIN": LOGIN, "CABINET_PASS_HASH": HASH} if env is None else env
    return cabinet.Cabinet(environ=env, db_path=db or tmp_path / "trade.db", clock=clock or Clock(),
                           sleep=sleeps.append if sleeps is not None else (lambda s: None))


def rq(method, path, headers=None, peer="127.0.0.1", body=b""):
    return cabinet.Req(method, path, headers or {}, peer, body)


def form(login=LOGIN, password=PW) -> bytes:
    return urlencode({"login": login, "password": password}).encode()


def hdr(resp, name):
    return [v for k, v in resp.headers if k.lower() == name.lower()]


def login_ok(cab, headers=None, peer="127.0.0.1") -> str:
    r = cab.handle(rq("POST", "/cabinet/login", headers, peer, form()))
    assert r.code == 303, r.body
    return hdr(r, "Set-Cookie")[0].split(";")[0]          # «fb_cab=…»


def is_form(r) -> bool:
    return b'action="/cabinet/login"' in r.body and b'id="deals"' not in r.body


# --- временная trade.db: закрытая (симуляция), открытая, на паузе с «грязными» строками, черновик -----------------
def _intent(con, did, kind, iid, t):
    iid, nonce = store.create_intent(con, deal_id=did, kind=kind, spec={}, plan={}, intent_id=iid, now=t)
    assert store.approve_intent(con, iid, nonce, now=t + 1)
    store.set_intent_status(con, iid, "running")
    return iid


def _clip(con, iid, seq, a_in, a_out, t, final="DEX_OK"):
    c = store.create_clip(con, iid, seq, a_in, now=t)
    store.set_clip_state(con, c, "DEX_SENT", now=t)
    if final == "DEX_UNKNOWN":
        store.set_clip_state(con, c, "DEX_UNKNOWN", now=t)
    else:
        store.set_clip_state(con, c, "DEX_OK", dex_in=a_in, dex_out=a_out, now=t)
    return c


def _order(con, did, clip, kind, seq, symbol, side, qty, quote, oid):
    cid = store.client_order_id(did, kind, seq, 1, 1)
    store.perp_order_intent(con, clip_id=clip, client_id=cid, venue="aster", symbol=symbol, side=side,
                            reduce_only=side == "BUY", tif="IOC", price=quote / qty, qty=qty)
    store.perp_order_result(con, cid, "FILLED", order_id=oid, executed_qty=qty, avg_price=quote / qty, cum_quote=quote)


def make_db(path):
    con = store.connect(path)
    # DOLD — симуляция, вход и выход целиком: спот 100 $ → 8000 (0.0125), продано 8000 за 104 $ (0.013);
    # шорт 8000 за 101.6 $ (0.0127), откуп за 100 $ (0.0125); филлов нет — средние по итогам заявок
    d = store.create_deal(con, coin="AIW3", chain="bsc", token=TOK_A, token_dec=18, perp_venue="aster",
                          symbol="AIW3USDT", leg_usd=D(100), owner_json="{}", sim=True, deal_id="DOLD", now=T0 - 9000)
    i = _intent(con, d, "entry", "EOLD", T0 - 9000)
    store.set_deal_state(con, d, "ENTERING", now=T0 - 8990)
    c = _clip(con, i, 1, 100 * E18, 8000 * E18, T0 - 8980)
    _order(con, d, c, "entry", 1, "AIW3USDT", "SELL", D(8000), D("101.6"), 201)
    store.set_intent_status(con, i, "done")
    store.set_deal_state(con, d, "OPEN", now=T0 - 8900)
    x = _intent(con, d, "exit", "XOLD", T0 - 8000)
    store.set_deal_state(con, d, "EXITING", now=T0 - 7990)
    c = _clip(con, x, 1, 8000 * E18, 104 * E18, T0 - 7980)
    _order(con, d, c, "exit", 2, "AIW3USDT", "BUY", D(8000), D(100), 202)     # e02: вид в client_id — первая буква
    store.set_intent_status(con, x, "done")
    store.set_deal_state(con, d, "CLOSED", now=T0 - 7000)
    store.event(con, "final", deal_id=d, intent_id=x, cost_usd=D("0.5"), state="CLOSED", sim=True, now=T0 - 7000)

    # DAAA — открыта: 2 клипа по 250 $ → 20000 + 19800 токенов (ср. 0.01256); шорт 20000 + 19800; у первой заявки
    # филлы на 253 $ (итог заявки говорит 252) — средняя шорта по userTrades: (253 + 247.5) / 39800 = 0.01258
    d = store.create_deal(con, coin="AIW3", chain="bsc", token=TOK_A, token_dec=18, perp_venue="aster",
                          symbol="AIW3USDT", leg_usd=D(500), owner_json="{}", sim=False, deal_id="DAAA", now=T0)
    i = _intent(con, d, "entry", "EAAA", T0)
    store.set_deal_state(con, d, "ENTERING", now=T0 + 2)
    c1 = _clip(con, i, 1, 250 * E18, 20000 * E18, T0 + 10)
    _order(con, d, c1, "entry", 1, "AIW3USDT", "SELL", D(20000), D(252), 111)
    c2 = _clip(con, i, 2, 250 * E18, 19800 * E18, T0 + 20)
    _order(con, d, c2, "entry", 2, "AIW3USDT", "SELL", D(19800), D("247.5"), 112)
    store.add_perp_fills(con, "aster", [
        dict(trade_id=1, order_id=111, price=D("0.0126"), qty=D(10000), quote_qty=D(126), commission_abs=D("0.05"),
             commission_asset="USDT", maker=False, realized_pnl=D(0), ts=int(T0 * 1000)),
        dict(trade_id=2, order_id=111, price=D("0.0127"), qty=D(10000), quote_qty=D(127), commission_abs=D("0.05"),
             commission_asset="USDT", maker=False, realized_pnl=D(0), ts=int(T0 * 1000))])
    store.set_intent_status(con, i, "done")
    store.set_deal_state(con, d, "OPEN", now=T0 + 60)
    store.event(con, "final", deal_id=d, intent_id=i, cost_usd=D("1.2"), state="OPEN", sim=False, now=T0 + 60)

    # DXSS — на паузе: исход свопа неизвестен; монета и причина — с разметкой (должны экранироваться)
    d = store.create_deal(con, coin=XSS_COIN, chain="bsc", token=TOK_C, token_dec=18, perp_venue="aster",
                          symbol="XSSUSDT", leg_usd=D(50), owner_json="{}", sim=False, deal_id="DXSS", now=T0 + 100)
    i = _intent(con, d, "entry", "EXSS", T0 + 100)
    store.set_deal_state(con, d, "ENTERING", now=T0 + 101)
    _clip(con, i, 1, 50 * E18, None, T0 + 110, final="DEX_UNKNOWN")
    store.set_deal_state(con, d, "PAUSED", reason="сверка <b>вручную</b>", now=T0 + 120)
    store.set_intent_status(con, i, "partial", err="x")
    store.event(con, "paused", deal_id=d, intent_id=i, reason="dex_unknown", text="<img src=x onerror=alert(2)>",
                state="PAUSED", now=T0 + 120)

    # DDRF — черновик (план без «да»): в кабинете не показывается, только счёт
    store.create_deal(con, coin="DRF", chain="bsc", token=TOK_B, token_dec=18, perp_venue="aster", symbol="DRFUSDT",
                      leg_usd=D(10), owner_json="{}", sim=False, deal_id="DDRF", now=T0 + 200)
    con.close()


TICK = T0 + 3600 - 5                       # снимок коллектора свежий: часы кабинета (Clock) — T0 + 3600


def write_table(tmp_path, monkeypatch, tick=TICK, **over):
    row = dict(key=f"okxdex:56:{TOK_A}|aster:AIW3USDT", base="AIW3", spot_ex="okxdex", spot=f"56:{TOK_A}",
               perp_ex="aster", perp="AIW3USDT", spread=0.0001, rate_h=0.0001, gap=0.0012, px_spot=0.01256,
               px_perp=0.012575, stale=False, windows={})
    row.update(over)
    p = tmp_path / "table.json"
    p.write_text(json.dumps(dict(ts=int(tick or 0), tick_ts=tick, pid=7, ff_rows=[], sf_rows=[row], n_ff=0, n_sf=1)))
    monkeypatch.setattr(config, "TABLE_PATH", p)


def cards(page: str) -> dict:
    """id сделки → её карточка на странице."""
    out = {}
    for part in page.split("<article")[1:]:
        m = re.search(r'<span class="m mono">(D[A-Z0-9]+)</span>', part)
        out[m.group(1)] = part
    return out


# --- пароль и CLI ------------------------------------------------------------------------------------------
def test_scrypt_hash_format_and_check():
    parts = HASH.split("$")
    assert parts[:4] == ["scrypt", "16384", "8", "1"] and re.fullmatch(r"[0-9a-f]{32}", parts[4])
    assert re.fullmatch(r"[0-9a-f]{64}", parts[5])
    ph = cabinet.parse_hash(HASH)
    assert cabinet.check_password(PW, ph)
    assert not cabinet.check_password(PW + "x", ph) and not cabinet.check_password("", ph)
    assert cabinet.make_hash(PW) != HASH                                          # соль каждый раз новая
    assert cabinet.make_hash(PW, salt=bytes.fromhex(parts[4])) == HASH            # та же соль — тот же хэш
    assert cabinet.parse_hash(f"'{HASH}'") == ph and cabinet.parse_hash(f' "{HASH}" ') == ph   # кавычки из .env
    assert parts[5] not in repr(ph) and parts[4] not in repr(ph)
    z16, z32 = "00" * 16, "00" * 32
    for bad in ("", "scrypt", HASH.replace("scrypt", "bcrypt", 1), HASH + "$00", f"scrypt$16383$8$1${z16}${z32}",
                f"scrypt$1048576$64$16${z16}${z32}", f"scrypt$16384$8$1$zz${z32}", f"scrypt$16384$8$1$00${z32}",
                f"scrypt$16384$8$1${z16}$00", f"scrypt$16384$0$1${z16}${z32}"):
        assert cabinet.parse_hash(bad) is None, bad
    with pytest.raises(ValueError):
        cabinet.make_hash("")


def test_cli_cabinet_hash_reads_stdin_and_prints_env_line(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    assert cli.main(["cabinet-hash"]) == 0
    cap = capsys.readouterr()
    line = cap.out.strip()
    assert re.fullmatch(r"CABINET_PASS_HASH='scrypt\$16384\$8\$1\$[0-9a-f]{32}\$[0-9a-f]{64}'", line)
    assert PW not in cap.out and PW not in cap.err
    assert cabinet.check_password(PW, cabinet.parse_hash(line.split("=", 1)[1]))


def test_cli_hash_terminal_asks_twice_and_refuses_empty():
    class Tty(io.StringIO):
        def isatty(self):
            return True
    out, err = io.StringIO(), io.StringIO()
    ans = iter(["first-password-1", "other-password-2"])
    assert cabinet.cli_hash(stdin=Tty(), out=out, err=err, ask=lambda prompt: next(ans)) == 2
    assert out.getvalue() == "" and "first-password-1" not in err.getvalue()
    assert cabinet.cli_hash(stdin=io.StringIO("\n"), out=out, err=err) == 2 and out.getvalue() == ""
    ans = iter(["same-long-password", "same-long-password"])
    assert cabinet.cli_hash(stdin=Tty(), out=out, err=err, ask=lambda prompt: next(ans)) == 0
    assert out.getvalue().startswith("CABINET_PASS_HASH='scrypt$16384$8$1$")


# --- выключенный кабинет ------------------------------------------------------------------------------------
@pytest.mark.parametrize("env", [{}, {"CABINET_LOGIN": LOGIN}, {"CABINET_PASS_HASH": HASH},
                                 {"CABINET_LOGIN": "  ", "CABINET_PASS_HASH": HASH},
                                 {"CABINET_LOGIN": LOGIN, "CABINET_PASS_HASH": "scrypt$16384$8$1$zz$zz"}])
def test_cabinet_disabled_without_env(tmp_path, env):
    cab = make_cab(tmp_path, env=env)
    assert not cab.enabled and cab.why_off and HASH not in cab.why_off
    r = cab.handle(rq("GET", "/cabinet"))
    assert r.code == 503 and "кабинет не настроен" in r.body.decode() and b"<form" not in r.body
    r = cab.handle(rq("POST", "/cabinet/login", body=form()))
    assert r.code == 503 and hdr(r, "Set-Cookie") == []
    assert cab.handle(rq("GET", "/cabinet/deals.json")).code == 503


# --- вход, выход, сессии --------------------------------------------------------------------------------------
def test_login_logout_and_session_expiry(tmp_path):
    clk = Clock()
    cab = make_cab(tmp_path, clock=clk)
    assert is_form(cab.handle(rq("GET", "/cabinet")))                                  # без сессии — форма
    assert cab.handle(rq("GET", "/cabinet/deals.json")).code == 401
    ck = login_ok(cab)
    r = cab.handle(rq("GET", "/cabinet", {"Cookie": "other=1; " + ck}))
    assert r.code == 200 and "сделок пока нет" in r.body.decode() and b'action="/cabinet/logout"' in r.body
    assert not (tmp_path / "trade.db").exists()                                        # базы нет — и не создана
    j = cab.handle(rq("GET", "/cabinet/deals.json", {"Cookie": ck}))
    assert j.code == 200 and "сделок пока нет" in json.loads(j.body)["html"]
    assert is_form(cab.handle(rq("GET", "/cabinet", {"Cookie": "fb_cab=forged-token"})))
    assert cab.handle(rq("GET", "/cabinet/login")).code == 303                         # обновление страницы после POST
    out = cab.handle(rq("POST", "/cabinet/logout", {"Cookie": ck}))
    assert out.code == 303 and hdr(out, "Location") == ["/cabinet"] and "Max-Age=0" in hdr(out, "Set-Cookie")[0]
    assert is_form(cab.handle(rq("GET", "/cabinet", {"Cookie": ck})))                 # выход удалил сессию
    ck2 = login_ok(cab)
    clk.t += cabinet.SESSION_TTL_S - 5
    assert not is_form(cab.handle(rq("GET", "/cabinet", {"Cookie": ck2})))
    clk.t += 10
    assert is_form(cab.handle(rq("GET", "/cabinet", {"Cookie": ck2})))                # 7 суток прошли
    ck3 = login_ok(cab)
    assert is_form(make_cab(tmp_path).handle(rq("GET", "/cabinet", {"Cookie": ck3})))  # рестарт веба = новый вход
    assert cab.handle(rq("POST", "/cabinet", body=b"")).code == 405                    # управления нет


@pytest.mark.parametrize("headers,peer,secure", [
    ({}, "127.0.0.1", False),                                    # ssh-туннель: http://localhost
    ({"X-Forwarded-Proto": "https"}, "127.0.0.1", True),         # cloudflared
    ({"CF-Visitor": '{"scheme":"https"}'}, "127.0.0.1", True),
    ({"X-Forwarded-Proto": "http"}, "127.0.0.1", False),
    ({}, "203.0.113.5", True),                                   # не с localhost
])
def test_cookie_flags(tmp_path, headers, peer, secure):
    r = make_cab(tmp_path).handle(rq("POST", "/cabinet/login", headers, peer, form()))
    assert r.code == 303 and hdr(r, "Location") == ["/cabinet"]
    attrs = [a.strip() for a in hdr(r, "Set-Cookie")[0].split(";")]
    assert attrs[0].startswith("fb_cab=") and len(attrs[0]) > 40
    assert {"HttpOnly", "SameSite=Strict", "Path=/cabinet", f"Max-Age={7 * 86400}"} <= set(attrs)
    assert ("Secure" in attrs) is secure


# --- перебор ------------------------------------------------------------------------------------------------
def test_bruteforce_lock_per_client_then_global(tmp_path):
    clk, sleeps = Clock(), []
    cab = make_cab(tmp_path, clock=clk, sleeps=sleeps)
    A = {"CF-Connecting-IP": "198.51.100.1"}
    bad_login = cab.handle(rq("POST", "/cabinet/login", A, body=form(login="nobody")))
    bad_pw = cab.handle(rq("POST", "/cabinet/login", A, body=form(password="wrong")))
    # неверный логин и неверный пароль неотличимы: код, тело, заголовки; после неудачи — задержка
    assert bad_login.code == bad_pw.code == 401 and bad_login.body == bad_pw.body and bad_login.headers == bad_pw.headers
    assert "неверный логин или пароль" in bad_pw.body.decode() and hdr(bad_pw, "Set-Cookie") == []
    assert sleeps == [cabinet.FAIL_DELAY_S] * 2
    for _ in range(3):
        assert cab.handle(rq("POST", "/cabinet/login", A, body=form(password="wrong"))).code == 401
    r = cab.handle(rq("POST", "/cabinet/login", A, body=form()))            # 6-я попытка — даже с верным паролем
    assert r.code == 429 and hdr(r, "Set-Cookie") == [] and hdr(r, "Retry-After") == ["900"]
    assert len(sleeps) == 5                                                  # на 429 scrypt и задержки нет
    assert login_ok(cab, {"CF-Connecting-IP": "198.51.100.2"})               # другой клиент за тем же cloudflared
    clk.t += cabinet.FAIL_WINDOW_S + 1
    assert login_ok(cab, A)                                                  # окно прошло
    for i in range(cabinet.FAILS_TOTAL):                                     # общий потолок: 30 неудач с разных адресов
        assert cab.handle(rq("POST", "/cabinet/login", {"CF-Connecting-IP": f"203.0.113.{i}"},
                             body=form(password="x"))).code == 401
    r = cab.handle(rq("POST", "/cabinet/login", {"CF-Connecting-IP": "192.0.2.77"}, body=form()))
    assert r.code == 429 and int(hdr(r, "Retry-After")[0]) == 900
    clk.t += cabinet.FAIL_WINDOW_S + 1
    assert login_ok(cab, {"CF-Connecting-IP": "192.0.2.77"})


def test_bruteforce_limit_holds_under_concurrent_requests(tmp_path):
    """Ревью 12.09: проверка лимита и запись неудачи были разными шагами — 40 одновременных запросов с одного адреса все
    доходили до scrypt. Теперь попытка занимается атомарно: до проверки пароля доходят ровно 5 (на клиента) / 30 (всего)."""
    cab = make_cab(tmp_path)
    calls, lock = [], threading.Lock()

    def slow_verify(login, pw):                     # проверка пароля «долгая»: все потоки успевают прийти к лимиту
        with lock:
            calls.append(1)
        threading.Event().wait(0.2)
        return False
    cab._verify = slow_verify

    def burst(n, ip_of):
        codes, gate = [], threading.Barrier(n)

        def one(i):
            gate.wait()
            r = cab.handle(rq("POST", "/cabinet/login", {"CF-Connecting-IP": ip_of(i)}, body=form(password="x")))
            with lock:
                codes.append(r.code)
        ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
        return codes
    codes = burst(40, lambda i: "198.51.100.1")
    assert codes.count(401) == cabinet.FAILS_PER_CLIENT and codes.count(429) == 40 - cabinet.FAILS_PER_CLIENT
    assert len(calls) == cabinet.FAILS_PER_CLIENT
    calls.clear()
    codes = burst(60, lambda i: f"203.0.113.{i}")                           # разные адреса — держит общий потолок
    left = cabinet.FAILS_TOTAL - cabinet.FAILS_PER_CLIENT
    assert codes.count(401) == left and codes.count(429) == 60 - left and len(calls) == left
    # удачный вход не съедает общий счёт: занятая им попытка снимается
    lim = cabinet.Limiter(per_client=5, total=3, clock=Clock())
    for _ in range(5):
        wait, stamp = lim.begin("owner")
        assert wait is None
        lim.success("owner", stamp)
    assert lim.begin("x")[0] is None and lim.begin("y")[0] is None and lim.begin("z")[0] is None
    assert lim.begin("owner")[0] == cabinet.FAIL_WINDOW_S                   # три чужие неудачи — общий потолок


def test_cf_connecting_ip_trusted_only_from_localhost(tmp_path):
    spoof = {"CF-Connecting-IP": "198.51.100.7"}
    assert cabinet.client_of(rq("GET", "/cabinet", spoof, peer="127.0.0.1")) == "198.51.100.7"
    assert cabinet.client_of(rq("GET", "/cabinet", spoof, peer="::1")) == "198.51.100.7"
    assert cabinet.client_of(rq("GET", "/cabinet", spoof, peer="203.0.113.9")) == "203.0.113.9"
    assert cabinet.client_of(rq("GET", "/cabinet", {"CF-Connecting-IP": "x; y"}, peer="127.0.0.1")) == "127.0.0.1"
    assert cabinet.client_of(rq("GET", "/cabinet", peer="127.0.0.1")) == "127.0.0.1"
    cab = make_cab(tmp_path)
    for _ in range(cabinet.FAILS_PER_CLIENT):
        assert cab.handle(rq("POST", "/cabinet/login", spoof, "203.0.113.9", form(password="x"))).code == 401
    # заблокирован адрес сокета, а не подставленный им заголовок: смена заголовка не помогает
    assert cab.handle(rq("POST", "/cabinet/login", {"CF-Connecting-IP": "198.51.100.8"}, "203.0.113.9",
                         form())).code == 429
    assert login_ok(cab, spoof, "127.0.0.1")                                 # настоящий 198.51.100.7 через cloudflared


def test_csrf_origin_and_referer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNTIME", tmp_path)
    cab = make_cab(tmp_path)
    H = "abc-def.trycloudflare.com"
    post = lambda h: cab.handle(rq("POST", "/cabinet/login", h, body=form()))
    r = post({"Host": H, "Origin": "https://evil.example"})
    assert r.code == 403 and hdr(r, "Set-Cookie") == []
    assert post({"Host": H, "Origin": "null"}).code == 403
    assert post({"Host": H, "Referer": "https://evil.example/cabinet"}).code == 403
    assert post({"Host": H, "Origin": f"https://{H}"}).code == 303
    assert post({"Host": H, "Referer": f"https://{H}/cabinet"}).code == 303
    assert post({"Host": "localhost:8792", "Origin": "http://localhost:8792"}).code == 303
    assert post({"Host": "127.0.0.1:8792", "Origin": f"https://{H}"}).code == 403
    (tmp_path / "public_url.txt").write_text(f"https://{H}\n")               # Host подменён — своя ссылка туннеля
    assert post({"Host": "127.0.0.1:8792", "Origin": f"https://{H}"}).code == 303
    ck = login_ok(cab)
    r = cab.handle(rq("POST", "/cabinet/logout", {"Cookie": ck, "Host": H, "Origin": "https://evil.example"}))
    assert r.code == 403 and not is_form(cab.handle(rq("GET", "/cabinet", {"Cookie": ck})))   # сессия жива


def test_cabinet_shows_readonly_trading_profiles_without_configuration_details(tmp_path):
    class Cfg:
        def profile_enabled(self, pid): return pid in (owner.LEGACY_PROFILE, owner.SOL_HL)
        def profile_mode(self, pid): return "live" if pid == owner.LEGACY_PROFILE else "dry"
        def profile_live_blockers(self, pid): return [] if pid == owner.LEGACY_PROFILE else ["secret-looking-detail"]
    profiles = owner.profile_views(Cfg())
    frag = cabinet.venues_fragment({"trading_profiles": profiles})
    assert "OKX DEX · BSC" in frag and "Aster" in frag and "включена" in frag
    assert "Hyperliquid" in frag and "не торгует" in frag and "режим dry" in frag
    assert "secret-looking-detail" not in frag
    # The fragment is part of the authenticated page and the JSON refresh payload.
    cab = make_cab(tmp_path, db=tmp_path / "missing.db", clock=Clock(),
                   env={"CABINET_LOGIN": LOGIN, "CABINET_PASS_HASH": HASH})
    snap = {"now": T0, "deals": [], "drafts": 0, "err": None, "trading_profiles": profiles}
    cab.snapshot_loader = lambda: snap
    ck = login_ok(cab)
    page = cab.handle(rq("GET", "/cabinet", {"Cookie": ck})).body.decode()
    refresh = json.loads(cab.handle(rq("GET", "/cabinet/deals.json", {"Cookie": ck})).body)
    assert "Торговые связки" in page and "включена" in refresh["venues_html"]



# --- страница сделок ------------------------------------------------------------------------------------------
def test_deals_page_on_temp_trade_db(tmp_path, monkeypatch):
    make_db(tmp_path / "trade.db")
    write_table(tmp_path, monkeypatch)
    cab = make_cab(tmp_path)
    ck = login_ok(cab)
    r = cab.handle(rq("GET", "/cabinet", {"Cookie": ck}))
    page = r.body.decode()
    assert r.code == 200
    assert page.index(">DXSS<") < page.index(">DAAA<") < page.index(">DOLD<")          # новые сверху
    assert "DDRF" not in page and "планов без «да»: 1" in page                        # черновик не показан
    assert page.count("<form") == 1 and page.count("<button") == 1 and 'action="/cabinet/logout"' in page
    c = cards(page)
    a = c["DAAA"]
    assert "🟢 открыта" in a and "AIW3" in a and "okx·bsc" in a and "aster" in a and "AIW3USDT" in a
    assert "$500 на ногу" in a
    # правка владельца 13.09: объёмы и средние ног, «закрыта —», «в позиции по журналу», разбивка выхода — «лишняя инфа»
    for gone in ("куплено", "продано", "откуплено", "в позиции", "закрыта", "спот (лонг)", "перп (шорт)", "выход:",
                 "39\u00a0800"):
        assert gone not in a, gone
    assert '<span class="k">открыта</span>' in a and f'data-ts="{int(T0 + 1)}"' in a    # открыта = одобрение входа
    assert 'фандинг 1ч сейчас</span><b class="mono g">+0.010\u00a0%</b>' in a       # 1ч — 3 знака
    assert 'курсовой</span><b class="mono ">+0.12\u00a0%</b>' in a                  # остальные % — 2 знака
    # курсовой — цены ног мелко (как на дашборде); у свежих цифр подписи времени нет («на 12:06 · …» — «лишняя инфа»)
    assert 'курсовой</span><b class="mono ">+0.12\u00a0%</b><span class="s"><span class="mono">$0.01256 | $0.01258</span>' \
        '</span></div>' in a
    assert "data-hm" not in a and "на <time" not in a
    assert 'до ликвидации</span><span class="m">—</span>' in a                         # оценки трейдера ещё нет
    assert "устарело" not in a and '<details class="hist" data-deal="DAAA">' in a and "выплат пока нет" in a
    assert "последнее: вход завершён" in a
    o = c["DOLD"]
    assert "✅ закрыта" in o and "симуляция" in o and "выход завершён — сделка закрыта" in o
    assert f'<span class="k">закрыта</span><time data-ts="{int(T0 - 7000)}">' in o and "PnL итог" in o
    for gone in ("в позиции", "фандинг", "курсовой", "до ликвидации", "история выплат", "куплено"):
        assert gone not in o, gone
    x = c["DXSS"]
    assert "⏸️ пауза" in x and '<div class="why unk">исход свопа выясняется</div>' in x
    assert "пауза: исход свопа выясняется" in x and "история выплат" in x and "пары нет в таблице дашборда" in x
    j = cab.handle(rq("GET", "/cabinet/deals.json", {"Cookie": ck}))
    body = json.loads(j.body)
    assert j.code == 200 and ">DAAA<" in body["html"] and body["summary"].startswith("сделок 3 · открыто 1")


def test_everything_from_db_is_escaped(tmp_path, monkeypatch):
    make_db(tmp_path / "trade.db")
    write_table(tmp_path, monkeypatch)
    cab = make_cab(tmp_path)
    ck = login_ok(cab)
    page = cab.handle(rq("GET", "/cabinet", {"Cookie": ck})).body.decode()
    frag = json.loads(cab.handle(rq("GET", "/cabinet/deals.json", {"Cookie": ck})).body)["html"]
    for s in (page, frag):
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in s and "<script>alert" not in s
        assert "сверка &lt;b&gt;вручную&lt;/b&gt;" in s and "<b>вручную" not in s
        assert "onerror" not in s                    # текст ошибки события на страницу не попадает вовсе
    assert page.count("<script>") == 1               # единственный скрипт — свой


# --- карточка 13.09: живые цифры, свежесть, история выплат --------------------------------------------------------
def _card(tmp_path, clock=None, did="DAAA"):
    cab = make_cab(tmp_path, clock=clock)
    return cards(cabinet.deals_fragment(cab.snapshot()))[did]


def test_live_cells_decimals_source_time_and_staleness(tmp_path, monkeypatch):
    make_db(tmp_path / "trade.db")
    write_table(tmp_path, monkeypatch, spread=0.000338, gap=-0.00731)        # 0.0338 % в час → 0.034
    a = _card(tmp_path)
    assert 'фандинг 1ч сейчас</span><b class="mono g">+0.034\u00a0%</b></div>' in a     # одна цифра, без подписи
    assert 'курсовой</span><b class="mono r">−0.73\u00a0%</b>' in a                   # |курсовой| > 0.5 % — красным
    assert "0.0338" not in a and "устарело" not in a
    write_table(tmp_path, monkeypatch, tick=T0 - 60, spread=0.000338)                  # коллектор молчит час
    a = _card(tmp_path)
    assert 'фандинг 1ч сейчас</span><b class="mono m">+0.034\u00a0%</b>' in a         # серым, не «текущий» цвет
    assert f'на <time data-ts="{int(T0 - 60)}">' in a and a.count("устарело") == 2    # фандинг и курсовой — с датой
    write_table(tmp_path, monkeypatch, stale=True)                                     # снимок свежий, строка устарела
    a = _card(tmp_path)
    assert '<b class="mono m">+0.010\u00a0%</b>' in a and "устарело" in a
    write_table(tmp_path, monkeypatch, tick=None)                                      # время снимка неизвестно
    assert "устарело" in _card(tmp_path)
    write_table(tmp_path, monkeypatch, tick=T0 + 2 * 3600)                             # снимок «из будущего»
    assert '<b class="mono m">+0.010\u00a0%</b>' in _card(tmp_path)
    write_table(tmp_path, monkeypatch, tick=None)                                      # NaN: не свежее и не 500
    p = tmp_path / "table.json"
    p.write_text(p.read_text().replace('"tick_ts": null', '"tick_ts": NaN'))
    a = _card(tmp_path)
    assert '<b class="mono m">+0.010\u00a0%</b>' in a and a.count("устарело") == 2
    write_table(tmp_path, monkeypatch, perp="OTHERUSDT")                               # пары нет в таблице
    a = _card(tmp_path)
    assert "пары нет в таблице дашборда" in a and "+0.010" not in a
    # чужие строки table.json на страницу не попадают как разметка
    write_table(tmp_path, monkeypatch, spread="<b>x</b>", gap="1", px_spot="<img src=x onerror=alert(3)>",
                px_perp=[1])
    a = _card(tmp_path)
    assert "<img" not in a and "onerror" not in a and "<b>x" not in a
    assert 'фандинг 1ч сейчас</span><span class="m">—</span>' in a and "— | —" in a


def test_funding_history_expands_with_running_total(tmp_path, monkeypatch):
    p = tmp_path / "trade.db"
    make_db(p)
    write_table(tmp_path, monkeypatch)
    w = store.connect(p)
    ms, H = int(T0 * 1000), 3_600_000
    store.add_funding_income(w, "aster", [
        dict(tran_id=1, symbol="AIW3USDT", income=D("0.12"), ts=ms + H),
        dict(tran_id=2, symbol="AIW3USDT", income=D("-0.05"), ts=ms + 2 * H),
        dict(tran_id=3, symbol="AIW3USDT", income=D("0.18"), ts=ms + 3 * H),
        dict(tran_id=4, symbol="AIW3USDT", income=D("9"), ts=ms - 1),          # до открытия — прошлая сделка DOLD
        dict(tran_id=5, symbol="XSSUSDT", income=D("-0.5"), ts=ms + H)])       # другой символ — другая сделка
    from funding_bot.trade import marks
    assert marks.journal(w, store.get_deal(w, "DAAA")).funding == D("0.25")   # те же границы, что фандинг в PnL
    w.close()
    a = _card(tmp_path)
    h = a[a.index('<details class="hist" data-deal="DAAA">'):]
    assert "<summary>история выплат</summary>" in h and "<details open" not in a   # свёрнута, раскрывается нажатием
    rows = re.findall(r'<tr><td><time data-ts="(\d+)">[^<]*</time></td><td class="mono (\w*)">([^<]*)</td>'
                      r'<td class="mono">([^<]*)</td></tr>', h)
    assert rows == [(str(int(T0) + 3 * 3600), "g", "+0.1800\u00a0$", "+0.2500\u00a0$"),        # новые сверху, итог с начала
                    (str(int(T0) + 2 * 3600), "r", "−0.0500\u00a0$", "+0.0700\u00a0$"),
                    (str(int(T0) + 3600), "g", "+0.1200\u00a0$", "+0.1200\u00a0$")]
    assert 'всего: <b class="mono g">+0.25\u00a0$</b>' in h and "показаны последние" not in h
    x = _card(tmp_path, did="DXSS")
    assert 'всего: <b class="mono r">−0.50\u00a0$</b>' in x and "+0.1200" not in x
    from funding_bot.core import readmodel
    monkeypatch.setattr(readmodel, "HIST_MAX", 2)                                  # длинная история — последние N
    h = _card(tmp_path)
    assert "показаны последние 2 из 3" in h and h.count("<tr><td>") == 2 and "+0.25\u00a0$" in h
    # история — только за сессией: без cookie ни страница, ни deals.json её не отдают
    cab = make_cab(tmp_path)
    for r in (cab.handle(rq("GET", "/cabinet")), cab.handle(rq("GET", "/cabinet/deals.json"))):
        assert "история выплат".encode() not in r.body and b"0.1800" not in r.body
    assert cab.handle(rq("GET", "/cabinet/deals.json")).code == 401
    ck = login_ok(cab)
    frag = json.loads(cab.handle(rq("GET", "/cabinet/deals.json", {"Cookie": ck})).body)["html"]
    assert '<details class="hist" data-deal="DAAA">' in frag and "+0.1800\u00a0$" in frag


def test_security_headers_and_no_store(tmp_path):
    cab = make_cab(tmp_path)
    ck = login_ok(cab)
    resps = [cab.handle(rq("GET", "/cabinet")), cab.handle(rq("GET", "/cabinet", {"Cookie": ck})),
             cab.handle(rq("GET", "/cabinet/deals.json", {"Cookie": ck})), cab.handle(rq("GET", "/cabinet/deals.json")),
             cab.handle(rq("POST", "/cabinet/login", body=form(password="x"))), cab.handle(rq("GET", "/cabinet/nope")),
             cab.handle(rq("POST", "/cabinet/logout", {"Cookie": ck})), make_cab(tmp_path, env={}).handle(rq("GET", "/cabinet"))]
    for _ in range(cabinet.FAILS_PER_CLIENT):
        cab.handle(rq("POST", "/cabinet/login", body=form(password="x")))
    resps.append(cab.handle(rq("POST", "/cabinet/login", body=form())))
    assert resps[-1].code == 429
    for r in resps:
        assert hdr(r, "Cache-Control") == ["no-store"] and hdr(r, "X-Frame-Options") == ["DENY"]
        csp = hdr(r, "Content-Security-Policy")[0]
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert "http" not in csp and "*" not in csp and "unsafe" not in csp          # внешних источников нет
    page = resps[1].body.decode()
    css = page.split("<style>", 1)[1].split("</style>", 1)[0]
    js = page.split("<script>", 1)[1].split("</script>", 1)[0]
    csp = hdr(resps[1], "Content-Security-Policy")[0]
    for s in (css, js):                                                               # свой стиль и скрипт CSP пропускает
        assert "'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'" in csp


def test_trade_db_opened_read_only(tmp_path):
    p = tmp_path / "trade.db"
    make_db(p)
    con = cabinet.open_ro(p)
    assert con.execute("SELECT count(*) FROM deals").fetchone()[0] == 4
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO flags(k, v) VALUES('x', '1')")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("UPDATE deals SET state = 'CLOSED'")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("CREATE TABLE t(x)")
    con.close()
    missing = tmp_path / "nope" / "trade.db"
    with pytest.raises(FileNotFoundError):
        cabinet.open_ro(missing)
    assert not missing.parent.exists()
    # трейдер держит пишущее соединение — кабинет читает и ничего не меняет
    w = store.connect(p)
    store.event(w, "stop", was="0")
    snap = make_cab(tmp_path).snapshot()
    assert [d["id"] for d in snap["deals"]] == ["DXSS", "DAAA", "DOLD"] and snap["err"] is None
    assert w.execute("SELECT count(*) FROM deals").fetchone()[0] == 4 and store.get_deal(w, "DAAA")["state"] == "OPEN"
    w.close()


# --- через настоящий веб-процесс: маршруты serve.py и чистота публичных ответов ------------------------------------
@pytest.fixture
def web(tmp_path, monkeypatch):
    write_table(tmp_path, monkeypatch)
    make_db(tmp_path / "trade.db")
    monkeypatch.setattr(cabinet, "_inst", make_cab(tmp_path))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict(headers or {})
    if body is not None:
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, data)
    c.close()
    return out


def test_http_routes_and_public_data_has_no_cabinet_data(web):
    st, h, body = call(web, "GET", "/cabinet")
    assert st == 200 and b'action="/cabinet/login"' in body and h["cache-control"] == "no-store"
    assert h["x-frame-options"] == "DENY" and "default-src 'none'" in h["content-security-policy"]
    st, h, _ = call(web, "POST", "/cabinet/login", form(), {"X-Forwarded-Proto": "https"})
    assert st == 303 and h["location"] == "/cabinet" and "Secure" in h["set-cookie"] and "HttpOnly" in h["set-cookie"]
    st, h, _ = call(web, "POST", "/cabinet/login", form())
    assert st == 303 and "Secure" not in h["set-cookie"]
    ck = h["set-cookie"].split(";")[0]
    st, h, body = call(web, "GET", "/cabinet", headers={"Cookie": ck})
    assert st == 200 and b">DAAA<" in body and h["cache-control"] == "no-store"
    st, _, body = call(web, "GET", "/cabinet/deals.json", headers={"Cookie": ck})
    assert st == 200 and ">DAAA<" in json.loads(body)["html"]
    for path in ("/data.json", "/", "/status"):                              # публичные ответы — без trade.db и сессий
        st, h, body = call(web, "GET", path)
        text = body.decode()
        assert st == 200 and "set-cookie" not in h
        for secret in ("DAAA", "DOLD", "DXSS", "alert(1)", "вручную", LOGIN, "fb_cab", "39800"):
            assert secret not in text, (path, secret)
    assert call(web, "POST", "/", b"x=1")[0] == 404                          # дашборд POST не принимает
    assert call(web, "POST", "/cabinet/login", b"x" * 10_000)[0] == 413
    st, h, _ = call(web, "POST", "/cabinet/logout", b"", {"Cookie": ck})
    assert st == 303 and "Max-Age=0" in h["set-cookie"]
    st, _, body = call(web, "GET", "/cabinet", headers={"Cookie": ck})
    assert st == 200 and b'action="/cabinet/login"' in body


def test_http_split_cookie_headers_keep_session(web):
    """Cookie несколькими строками (HTTP/2 → HTTP/1.1 без склейки): сессия не теряется, даже если fb_cab не в последней."""
    _, h, _ = call(web, "POST", "/cabinet/login", form())
    ck = h["set-cookie"].split(";")[0]
    c = http.client.HTTPConnection("127.0.0.1", web, timeout=10)
    c.putrequest("GET", "/cabinet")
    c.putheader("Cookie", ck)
    c.putheader("Cookie", "__cf_bm=zzz")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    assert r.status == 200 and b">DAAA<" in body
    for meth in ("HEAD", "PUT", "DELETE", "OPTIONS", "PATCH"):              # других методов нет: 501 без данных
        st, h, body = call(web, meth, "/cabinet", headers={"Cookie": ck})
        assert st == 501 and b"DAAA" not in body and "set-cookie" not in h
    for path in ("/Cabinet", "/CABINET/deals.json", "/cabinet/../cabinet/deals.json", "/%63abinet/deals.json",
                 "/cabinet/deals.json/x.css", "//cabinet/deals.json", "/cabinet/deals.json?x=1", "/cabinet?a=b"):
        st, h, body = call(web, "GET", path)                                  # без cookie: ни одной сделки
        assert b"DAAA" not in body and b"39800" not in body and st in (200, 401, 404), (path, st)
        assert h.get("cache-control") == "no-store", path


# --- скрипт страницы в настоящем JS-движке -------------------------------------------------------------------------
@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_page_script_refresh_in_jsc(tmp_path):
    stub = r"""
var __log = [], __ms = 0, __tick = null, __reloads = 0, __status = 200;
var __times = [{ts: '1757700000', textContent: '', getAttribute: function(){ return this.ts; }}];
function __det(id, open, top){ var tw = {scrollTop: top};
  return {open: open, tw: tw, getAttribute: function(a){ return a === 'data-deal' ? id : null; },
          querySelector: function(sel){ return sel === '.tw' ? tw : null; }}; }
// «история выплат» DAAA раскрыта владельцем и прокручена на 240 px; у __proto__ — чужой id не ломает словарь
var __details = [__det('DAAA', true, 240), __det('DXSS', false, 0), __det('__proto__', false, 0)];
function __el(id){ return {_h: '', textContent: '', set innerHTML(v){ this._h = v; __log.push(id + '=' + v);
    if(id === 'deals') __details = [__det('DXSS', false, 0), __det('DAAA', false, 0), __det('__proto__', false, 0)]; },
    // новый HTML — всё свёрнуто, прокрутка сброшена
  get innerHTML(){ return this._h; },
  querySelectorAll: function(sel){ return sel.indexOf('details') === 0 ? __details : __times; }}; }
var __els = {deals: __el('deals'), sum: __el('sum'), upd: __el('upd')};
var document = {hidden: false, getElementById: function(id){ return __els[id]; }, querySelectorAll: function(){ return __times; }};
function setInterval(f, ms){ __tick = f; __ms = ms; }
var location = {reload: function(){ __reloads++; }};
function fetch(url, opts){
  __log.push('fetch ' + url + ' ' + opts.cache + ' ' + opts.credentials);
  return Promise.resolve({status: __status, ok: __status === 200, json: function(){
    return Promise.resolve({html: '<article>x</article>', summary: 'сделок 1', upd: '<time data-ts="1">t</time>', ts: 1}); }});
}
function later(n, f){ return n ? Promise.resolve().then(function(){ return later(n - 1, f); }) : Promise.resolve().then(f); }
"""
    tail = r"""
var __t0 = __times[0].textContent;
__tick();
later(20, function(){
  __status = 401; __tick();
  later(20, function(){
    print(JSON.stringify({log: __log, reloads: __reloads, ms: __ms, t0: __t0, sum: __els.sum.textContent,
                          open: __details.map(function(d){ return d.getAttribute('data-deal') + ':' + d.open + ':' +
                                                                  d.tw.scrollTop; })}));
  });
});
"""
    f = tmp_path / "cab.js"
    f.write_text(stub + cabinet.JS + tail)
    p = subprocess.run([JSC, str(f)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert re.fullmatch(r"\d\d\.\d\d \d\d:\d\d", out["t0"])                   # UTC → местное время браузера
    assert out["ms"] == cabinet.REFRESH_S * 1000
    assert out["log"][0] == "fetch /cabinet/deals.json no-store same-origin"
    assert "deals=<article>x</article>" in out["log"] and out["sum"] == "сделок 1"
    assert out["reloads"] == 1                                               # сессия пропала — перезагрузка на форму
    # раскрытая история не свернулась и не прыгнула наверх; остальные — как пришли с сервера
    assert out["open"] == ["DXSS:false:0", "DAAA:true:240", "__proto__:false:0"]
