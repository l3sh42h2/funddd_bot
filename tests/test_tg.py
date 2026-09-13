"""Telegram-бот фазы 2: разбор команд, доступ только владельца, CAS кнопки (двойное нажатие), резка на 4096,
HTML-экранирование с откатом, маскировка токена в логах, 429/409, сторож зависшего getUpdates.

Сеть — поддельная сессия (FakeSession): Telegram не вызывается, токен фальшивый."""
import io, json, logging, pickle, re, threading, time
from decimal import Decimal
import pytest, requests
from funding_bot.trade import keys, store, tconfig
from funding_bot.tg import api as tgapi, auth, parse, poller as tgpoller, sender as tgsender, views
from funding_bot.tg.parse import Entry, Exit, Help, Positions, Rehedge, Resume, Start, Status, Stop, Undo, Unknown

TOKEN = "123456789:AAFake-Token_for_tests"
OWNER = 777000111
STRANGER = 555000222
NOW = 1_757_680_000.0


# --- подделки ---------------------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


def ok(result=True):
    return (200, {"ok": True, "result": result})


def err(code, desc, **params):
    body = {"ok": False, "error_code": code, "description": desc}
    if params:
        body["parameters"] = params
    return (code, body)


class FakeSession:
    """Ответы по очереди из script, дальше — default. Элемент: (status, body) | исключение | callable(url, json)."""

    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        self.default = default if default is not None else ok()
        self.posts = []
        self.headers = {}
        self.closed = False

    def post(self, url, json=None, timeout=None):
        self.posts.append({"method": url.rsplit("/", 1)[-1], "url": url, "json": json, "timeout": timeout})
        item = self.script.pop(0) if self.script else self.default
        if callable(item) and not isinstance(item, BaseException):
            item = item(url, json)
        if isinstance(item, BaseException):
            raise item
        return FakeResp(*item)

    def close(self):
        self.closed = True

    def methods(self):
        return [p["method"] for p in self.posts]


def make_api(*sessions):
    it = iter(sessions)
    return tgapi.TgApi(TOKEN, session_factory=lambda: next(it))


class Clock:
    def __init__(self, t=NOW):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


@pytest.fixture(autouse=True)
def _clean_redaction():
    keys._reset_redaction_for_tests()
    yield
    keys._reset_redaction_for_tests()


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "trade.db")
    yield c
    c.close()


def _intent(con, now=NOW, ttl=60):
    return store.create_intent(con, deal_id="DTEST", kind="entry", spec={"coin": "AIW3"}, plan={"n": 1},
                               ttl_s=ttl, now=now)


def html_ok(s: str) -> bool:
    """В тексте только наши теги, парные; ссылки — только tx_link() на bscscan; любой «<» из данных экранирован."""
    return tgsender.html_ok(s)


# ================================ parse ================================
D = Decimal
E = lambda usd, perp="aster", spot="okx·bsc", coin="AIW3": Entry(coin, spot, perp, D(usd))   # noqa: E731


@pytest.mark.parametrize("text,want", [
    ("вход AIW3 okx·bsc aster 500", E("500")),
    ("Вход aiw3 OKX-BSC Aster $500", E("500")),
    ("вход AIW3 okx.bsc aster 500 usdt", E("500")),
    ("вход AIW3 okx bsc aster 500$", E("500")),
    ("вход  AIW3   okx/bsc   aster   500,5", E("500.5")),
    ("вход AIW3 okx·bsc aster $ 500", E("500")),
    ("вход AIW3 okx·bsc aster 500usdt", E("500")),
    ("/вход AIW3 okx_bsc aster 500", E("500")),
    ("вход AIW3 okx•bsc hl 250", E("250", perp="hyperliquid")),
    ("вход AIW3 okxdex:solana aster 10", E("10", spot="okx·sol")),
    ("вход aiw3 okx·bnb aster 0.5", E("0.5")),
    # владелец 12.09: «вход AIW3 okx dex aster 450» — сеть решает движок по таблице (spot «okx»)
    ("вход AIW3 okx dex aster 450", E("450", spot="okx")),
    ("вход AIW3 OKX DEX aster 450", E("450", spot="okx")),
    ("вход AIW3 okxdex aster 450", E("450", spot="okx")),
    ("вход AIW3 okx-dex aster $450", E("450", spot="okx")),
    ("вход AIW3 okx dex bsc aster 450", E("450")),
    ("выход AIW3", Exit("AIW3", None)),
    ("выход D7K2 всё", Exit("D7K2", None)),
    ("выход d7k2 все", Exit("D7K2", None)),
    ("Выход D7K2 200", Exit("D7K2", D(200))),
    ("выход D7K2 200 usdt", Exit("D7K2", D(200))),
    ("выход D7K2 перп", Exit("D7K2", None, True)),
    ("выход D7K2 перп всё", Exit("D7K2", None, True)),
    ("позиции", Positions()), ("/positions", Positions()), ("/status@FundingBot", Status()), ("статус", Status()),
    ("стоп", Stop()), ("/stop", Stop()), ("СТОП!", Stop()), ("стоп всё немедленно", Stop()), ("/стоп", Stop()),
    ("продолжить", Resume()), ("продолжить E7K2", Resume("E7K2")), ("Продолжить e7k2", Resume("E7K2")),
    ("дохедж D7K2", Rehedge("D7K2")), ("откат D7K2", Undo("D7K2")),
    ("помощь", Help()), ("/help", Help()), ("/start", Start()), ("/start@FundingBot", Start()),
    ("ёжик", Unknown("ёжик", "нет такой команды «ежик»")),
])
def test_parse_matrix(text, want):
    assert parse.parse(text) == want


@pytest.mark.parametrize("text", [
    "", "   ", "привет", "/unknown", "вход", "вход AIW3 okx·bsc aster", "вход AIW3 okx·eth aster 500",
    "вход AIW3 uni·bsc aster 500", "вход AIW3 okx·bsc bybit 500", "вход AIW3 okx·bsc aster 1e3",
    "вход AIW3 okx·bsc aster -500", "вход AIW3 okx·bsc aster 0", "вход AIW3 okx·bsc aster 500 600",
    "вход AIW3 okx·bsc aster 500 000", "вход AIW3 okx·bsc aster 500 600 700", "вход AI-W3 okx·bsc aster 500",
    "вход AIW3 okx·bsc aster 1,000.50", "вход AIW3 okx·bsc·x aster 500", "выход", "выход D7K2 перп 100",
    "выход D7K2 abc", "выход D7K2 100 200", "дохедж", "откат D7K2 E7K2", "позиции все", "статус сейчас",
    "продолжить a b",
])
def test_parse_refuses_rather_than_guesses(text):
    cmd = parse.parse(text)
    assert isinstance(cmd, Unknown) and cmd.reason


def test_parse_amount_and_spot_helpers():
    assert parse.parse_amount("$500") == D(500) and parse.parse_amount("500,25") == D("500.25")
    assert parse.parse_amount("1e3") is None and parse.parse_amount("0") is None and parse.parse_amount("") is None
    assert parse.amount_of(["500", "600"]) is None and parse.amount_of(["500", "usdt"]) == D(500)
    assert parse.parse_spot("OKX·BSC") == parse.parse_spot("okx-bsc") == parse.parse_spot("okx:bsc") == "okx·bsc"
    assert parse.parse_spot("okx·rh") == "okx·rh" and parse.parse_spot("okx") is None
    assert parse.parse_spot("okxdex") == parse.parse_spot("okx-dex") == parse.parse_spot("OKX·DEX") == "okx"
    assert parse.parse_spot("dex") is None and parse.parse_spot("uni·dex") is None
    # форма id совпадает с частью монет — различает движок по БД
    # id из store.new_id — префикс + 4 знака без I/O/0/1 («DK7Q2»)
    assert parse.looks_like_id("DK7Q2") and parse.looks_like_id("ek7q2") and not parse.looks_like_id("AIW3")
    assert not parse.looks_like_id("D7K2") and not parse.looks_like_id("DK1Q2")
    assert parse.looks_like_id("DEGEN")                          # монета той же формы — поэтому сверка с БД


def test_callback_data_roundtrip_and_limits():
    s = parse.callback_data("ok", "E7K2", "9f3a1c20")
    assert s == "ok:E7K2:9f3a1c20" and len(s.encode()) <= parse.CALLBACK_MAX_BYTES
    cb = parse.parse_callback(s)
    assert (cb.action, cb.intent_id, cb.nonce) == ("ok", "E7K2", "9f3a1c20")
    for bad in (None, "", "ok:E7K2:9F3A1C20", "ok:E7K2:9f3a1c2", "rm:E7K2:9f3a1c20", "ok:e7k2:9f3a1c20",
                "ok:E7K2:9f3a1c20:x"):
        assert parse.parse_callback(bad) is None
    with pytest.raises(ValueError):
        parse.callback_data("ok", "e7k2", "xyz")


# ================================ auth ================================
def msg(frm=OWNER, chat=None, text="статус", ctype="private", date=NOW, uid=1, is_bot=False, key="message"):
    m = {"message_id": 5, "from": {"id": frm, "is_bot": is_bot}, "chat": {"id": frm if chat is None else chat,
                                                                          "type": ctype},
         "date": None if date is None else int(date)}             # Telegram шлёт date целым unix-временем
    if text is not None:
        m["text"] = text
    return {"update_id": uid, key: m}


def cbq(frm=OWNER, chat="owner", ctype="private", data="ok:E7K2:9f3a1c20", uid=2):
    q = {"id": "cb1", "from": {"id": frm, "is_bot": False}, "data": data}
    if chat == "owner":
        q["message"] = {"message_id": 9, "date": int(NOW) - 10, "chat": {"id": frm, "type": ctype}}
    elif chat is None:
        pass                                           # сообщение недоступно — только from
    else:
        q["message"] = {"message_id": 9, "date": 0, "chat": {"id": chat, "type": ctype}}
    return {"update_id": uid, "callback_query": q}


@pytest.mark.parametrize("u,owner_id,want", [
    (msg(), OWNER, auth.OWNER_MSG),
    (msg(text="/start"), OWNER, auth.OWNER_MSG),
    (msg(chat=-1001, ctype="group"), OWNER, auth.IGNORE),
    (msg(chat=-1002, ctype="supergroup", text="стоп"), OWNER, auth.IGNORE),
    (msg(frm=STRANGER), OWNER, auth.IGNORE),
    (msg(frm=STRANGER, text="вход AIW3 okx·bsc aster 500"), OWNER, auth.IGNORE),
    (msg(frm=STRANGER, text="/start"), OWNER, auth.STRANGER_START),
    (msg(frm=STRANGER, text="/start@FundingBot"), OWNER, auth.STRANGER_START),
    (msg(frm=STRANGER, chat=-1001, ctype="group", text="/start"), OWNER, auth.IGNORE),
    (msg(key="channel_post"), OWNER, auth.IGNORE),
    (msg(key="edited_message", text="вход AIW3 okx·bsc aster 500"), OWNER, auth.IGNORE),
    (msg(text=None), OWNER, auth.IGNORE),                              # стикер/фото владельца
    (msg(is_bot=True), OWNER, auth.IGNORE),
    (msg(chat=STRANGER), OWNER, auth.IGNORE),                          # from ≠ chat
    (msg(date=NOW - 91), OWNER, auth.STALE),
    (msg(date=NOW - 89), OWNER, auth.OWNER_MSG),
    (msg(date=None), OWNER, auth.STALE),
    (msg(), None, auth.IGNORE),                                        # владельца нет — командовать нельзя
    (msg(text="/start"), None, auth.STRANGER_START),                   # …но свой id узнать можно
    (cbq(), OWNER, auth.OWNER_CB),
    (cbq(chat=None), OWNER, auth.OWNER_CB),
    (cbq(frm=STRANGER), OWNER, auth.STRANGER_CB),
    (cbq(chat=-1001, ctype="group"), OWNER, auth.STRANGER_CB),
    (cbq(), None, auth.STRANGER_CB),
    ({}, OWNER, auth.IGNORE),
    ({"update_id": 3, "my_chat_member": {}}, OWNER, auth.IGNORE),
])
def test_auth_matrix(u, owner_id, want):
    d = auth.classify(u, owner_id, now=NOW)
    assert d.verdict == want
    assert d.is_owner == (want in (auth.OWNER_MSG, auth.OWNER_CB))


def test_auth_decision_fields_and_meta():
    d = auth.classify(cbq(), OWNER, now=NOW)
    assert (d.callback_id, d.message_id, d.text, d.user_id) == ("cb1", 9, "ok:E7K2:9f3a1c20", OWNER)
    assert auth.update_meta(msg(text="статус")) == (OWNER, OWNER, "статус")
    assert auth.update_meta(cbq()) == (OWNER, OWNER, "ok:E7K2:9f3a1c20")
    assert auth.update_meta("junk") == (None, None, None)
    assert auth.classify("junk", OWNER, now=NOW).verdict == auth.IGNORE


def test_stranger_start_is_rate_limited_per_chat_and_globally():
    clk = Clock()
    lim = auth.StartLimiter(gap_s=600, global_max=3, clock=clk)
    u = msg(frm=STRANGER, text="/start")
    assert auth.classify(u, OWNER, now=clk(), limiter=lim).verdict == auth.STRANGER_START
    assert auth.classify(u, OWNER, now=clk() + 5, limiter=lim).verdict == auth.STRANGER_LIMITED
    assert lim.allow(2, clk()) and lim.allow(3, clk())
    assert not lim.allow(4, clk())                     # общий потолок: бот не усилитель спама
    clk.t += 600
    assert auth.classify(u, OWNER, now=clk(), limiter=lim).verdict == auth.STRANGER_START
    reply = views.start_reply(STRANGER, STRANGER)
    assert str(STRANGER) in reply and html_ok(reply)


def test_stale_owner_message_reply():
    d = auth.classify(msg(date=NOW - 3600), OWNER, now=NOW)
    assert d.verdict == auth.STALE
    txt = views.stale(d.date)
    assert txt.startswith("⌛") and "устарела" in txt and views.hm(d.date) in txt


# ================================ кнопка: CAS ================================
def test_double_tap_gives_one_submit(con):
    iid, nonce = _intent(con)
    data = parse.callback_data("ok", iid, nonce)
    r1 = auth.press(con, data, now=NOW + 1)
    r2 = auth.press(con, data, now=NOW + 2)
    assert (r1.applied, r1.submit, r1.closed, r1.answer) == (True, True, "ok", views.CB_ACCEPTED)
    assert (r2.applied, r2.submit, r2.answer) == (False, False, views.CB_ALREADY)
    assert store.get_intent(con, iid)["status"] == "approved"
    store.set_intent_status(con, iid, "running")
    assert auth.press(con, data, now=NOW + 3).answer == views.CB_RUNNING
    store.set_intent_status(con, iid, "done")
    assert auth.press(con, data, now=NOW + 4).answer == views.CB_DONE


def test_two_devices_pressing_at_once_give_exactly_one_submit(tmp_path):
    for rnd in range(8):
        path = tmp_path / f"r{rnd}.db"
        c0 = store.connect(path)
        iid, nonce = _intent(c0, now=time.time())
        data = parse.callback_data("ok", iid, nonce)
        barrier = threading.Barrier(3)
        results = []

        def tap():
            c = store.connect(path)                    # одно соединение на поток
            try:
                barrier.wait()
                results.append(auth.press(c, data))
            finally:
                c.close()

        ts = [threading.Thread(target=tap) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert len(results) == 3
        assert sum(r.submit for r in results) == 1
        assert sum(r.applied for r in results) == 1
        assert store.get_intent(c0, iid)["status"] == "approved"
        c0.close()


def test_press_no_expired_wrong_nonce_paused_busy(con):
    iid, nonce = _intent(con)
    r = auth.press(con, parse.callback_data("no", iid, nonce), now=NOW + 1)
    assert (r.applied, r.submit, r.closed) == (True, False, "no")
    assert auth.press(con, parse.callback_data("ok", iid, nonce), now=NOW + 2).answer == views.CB_ALREADY_CANCELLED

    # истёкший план: UPDATE его не одобрит, пометим expired и снимем кнопки
    iid2, nonce2 = _intent(con, ttl=60)
    r = auth.press(con, parse.callback_data("ok", iid2, nonce2), now=NOW + 61)
    assert (r.applied, r.submit, r.closed, r.answer) == (False, False, "expired", views.CB_EXPIRED)
    assert store.get_intent(con, iid2)["status"] == "expired"

    # чужой nonce (кнопка старого плана того же id)
    iid3, nonce3 = _intent(con)
    other = "0" * 8 if nonce3 != "0" * 8 else "1" * 8
    assert auth.press(con, parse.callback_data("ok", iid3, other), now=NOW + 1).answer == views.CB_OLD_BUTTON

    # пауза: «да» не принимается, намерение остаётся proposed
    r = auth.press(con, parse.callback_data("ok", iid3, nonce3), paused=True, now=NOW + 1)
    assert (r.applied, r.submit, r.answer) == (False, False, views.CB_PAUSED)
    assert store.get_intent(con, iid3)["status"] == "proposed"
    assert auth.press(con, parse.callback_data("ok", iid3, nonce3), now=NOW + 2).submit

    # другое намерение уже идёт — индекс одного running
    iid4, nonce4 = _intent(con)
    r = auth.press(con, parse.callback_data("ok", iid4, nonce4), now=NOW + 3)
    assert (r.submit, r.answer) == (False, views.CB_BUSY)

    assert auth.press(con, "garbage", now=NOW).answer == views.CB_UNKNOWN
    assert auth.press(con, parse.callback_data("ok", "EZZZZ", "abcdef12"), now=NOW).answer == views.CB_STALE
    assert all(len(getattr(views, n)) <= 200 for n in dir(views) if n.startswith("CB_"))


# ================================ резка и экранирование ================================
def test_split_4096_multibyte_on_line_boundaries():
    lines = [f"строка {i}: спот +2 057 611 AIW3 🧪 ¥ — {'ё' * (i % 50)}" for i in range(400)]
    text = "\n".join(lines)
    assert tgsender.u16len(text) > 3 * tconfig.TG_SPLIT_CHARS
    chunks = tgsender.split_text(text)
    assert len(chunks) >= 3
    assert all(tgsender.u16len(c) <= tconfig.TG_SPLIT_CHARS < 4096 for c in chunks)
    assert "\n".join(chunks) == text
    assert tgsender.u16len("🧪") == 2 and tgsender.u16len("ж") == 1


def test_split_long_line_keeps_emoji_entities_and_tags_whole():
    line = "🧪" * 3000                                     # 6000 единиц UTF-16 одной строкой
    chunks = tgsender.split_text(line)
    assert "".join(chunks) == line and all(tgsender.u16len(c) <= 4000 for c in chunks)
    ent = "x" * 3995 + "&amp;" + "y" * 100
    c = tgsender.split_text(ent)
    assert "".join(c) == ent and not re.search(r"&[a-z]*$", c[0])
    tag = "a" * 3997 + "<b>жирный</b>" + "b" * 50
    c = tgsender.split_text(tag)
    assert "".join(c) == tag and not re.search(r"<[^>]*$", c[0])
    assert tgsender.split_text("") == [] and tgsender.split_text("  \n ") == []
    assert tgsender.split_text("коротко") == ["коротко"]
    assert tgsender.u16len(tgsender.fit_one("ж\n" * 5000)) <= 4000


def test_html_escaping_everywhere():
    nasty = "Aster −2019 <Margin> is insufficient & <script>"
    assert tgsender.escape(nasty) == "Aster −2019 &lt;Margin&gt; is insufficient &amp; &lt;script&gt;"
    assert tgsender.to_plain("<b>Вход</b> &lt;x&gt; &amp; <code>id</code>") == "Вход <x> & id"
    assert tgsender.to_plain("a < b") == "a < b"                   # не тег — остаётся текстом
    evil_tx, good_tx = '0x" onmouseover="alert(1)', "0x" + "ab" * 32
    outs = [
        views.halt(views.HaltView("E7K2", "entry", "AIW3", nasty, clip=1, clips=2, perp_pos=D(-5), wallet_tokens=D(7),
                                  unhedged_qty=D(2), unhedged_usd=D("0.5"))),
        views.halt(views.HaltView("E7K2", "entry", nasty, nasty, unhedged_qty=D(0), state="HALTED_MISMATCH")),
        views.error(nasty), views.refused(nasty), views.requote(nasty), views.owner_config_error(nasty),
        views.plan(views.PlanView("E7K2", "entry", "AIW3", "bsc", "aster", "AIW3USDT", D(500), (D(500),),
                                  notes=(nasty,))),
        views.plan(views.PlanView("X7K2", "exit", nasty, "bsc", "aster", "AIW3USDT", D(500), perp_only=True)),
        views.restart(views.RestartView("E7K2", "entry", details=nasty, matched=False)),
        views.restart_check(nasty, "D7K2", False, nasty, "HALTED_MISMATCH"),
        views.positions([views.PositionView("D7K2", "AIW3", "PAUSED", reason=nasty)], ts=NOW, matched=False,
                        mismatch=nasty),
        views.status(views.StatusView(ts=NOW, mode="live", checks=((nasty, False, nasty),))),
        views.final(views.FinalView("X7K2", "exit", "AIW3", "bsc", "aster", "D7K2", partial=True, partial_reason=nasty)),
        views.final(views.FinalView("E7K2", "entry", "AIW3", "bsc", "aster", "D7K2", tx_hashes=(evil_tx, good_tx))),
        views.fix_plan(views.FixPlanView("E7K2", "rehedge", nasty, "D7K2", D(5), D(5), "SELL")),
        views.fix_done(views.FixDoneView("undo", nasty, "D7K2", "PAUSED", noop=nasty)),
        views.perp_closed(views.PerpClosedView(nasty, "D7K2", D(1), D(1))), views.resume_mismatch("D7K2", nasty),
        views.executor_crash("E7K2", nasty), views.auto_unwind(nasty), views.plan_closed("ok", views.plan_title(
            "entry", nasty, usd=D(1)), NOW),
        views.help_text(), views.unknown(parse.parse("вход <b>")),
    ]
    for o in outs:
        assert html_ok(o), o
        assert "<script>" not in o and "<Margin>" not in o
    linked = next(o for o in outs if "bscscan" in o)
    assert linked.count("<a ") == 1 and f'<a href="https://bscscan.com/tx/{good_tx}">' in linked   # чужое — текстом


def test_tx_link_whitelist_and_split_keeps_tags_whole():
    """Ссылка — только bscscan tx с хэшем ровно 0x + 64 hex; html_ok пропускает только такие; to_plain снимает теги;
    резка на 4096 не рвёт ни тег, ни пару <a …>…</a>."""
    h = "0x" + "4b5e" * 16
    link = tgsender.tx_link(h, "bsc", "tx 0x4b5e…4b5e")
    assert link == f'<a href="https://bscscan.com/tx/{h}">tx 0x4b5e…4b5e</a>' and tgsender.html_ok(link)
    for bad in ('0x" onclick="x', h + "00", "0x" + "zz" * 32, None, "", h[:-1]):
        s = tgsender.tx_link(bad, "bsc")
        assert "<a" not in s and tgsender.html_ok(s), bad
    assert "<a" not in tgsender.tx_link(h, "sol")                            # сеть без белого списка — текстом
    assert tgsender.tx_link(h, "bsc", "<b>") == f'<a href="https://bscscan.com/tx/{h}">&lt;b&gt;</a>'
    for bad in (f'<a href="https://evil.example/tx/{h}">x</a>', '<a href="https://bscscan.com/tx/0x12">x</a>',
                f'<a href="https://bscscan.com/tx/{h}">x', f'x</a>', "<b>x", "x</b>", "<script>", '<a href="x">'):
        assert not tgsender.html_ok(bad), bad
    assert tgsender.to_plain(f"<code>выход D1</code> · {link}") == "выход D1 · tx 0x4b5e…4b5e"
    for line in ("ж" * 3950 + " " + link + " " + "ж" * 200,                 # резка пришлась бы внутрь <a href=…>
                 "ж" * 3700 + tgsender.tx_link(h, "bsc", "т" * 300) + "ж" * 100):   # …или между <a> и </a>
        chunks = tgsender.split_text(line)
        assert "".join(chunks) == line and len(chunks) == 2
        assert all(tgsender.html_ok(c) and tgsender.u16len(c) <= tconfig.TG_SPLIT_CHARS for c in chunks), chunks


# ================================ шаблоны (вариант C владельца 12.09) ================================
def flat(s: str) -> str:
    """Неразрывные пробелы (разряды, «200 $») — обычными: так строки сравниваются с эталоном."""
    return s.replace(views.NBSP, " ")


def _plan(**kw):
    base = dict(intent_id="E7K2", kind="entry", coin="AIW3", chain="bsc", perp_venue="aster", symbol="AIW3USDT",
                leg_usd=D(500), clips_usd=(D(500),), step=D(1), leverage=1, margin_type="ISOLATED",
                impact_usd=D("0.30"), gas_usd_clip=D("0.01"), gas_usd_total=D("0.01"), native_px=D(600),
                total_usd=D("0.57"), exit_cost_usd=D("0.57"), breakeven_h=D("4.56"), funding_pct_h=D("0.05"),
                usd_per_h=D("0.25"), basis_pct=D("0.004"), wallet_stable=D(1000), wallet_native=D("0.1"),
                margin_avail=D(1000), open_deals=0, max_open_deals=1)
    base.update(kw)
    return views.PlanView(**base)


def test_plan_view_and_buttons():
    """Каркас плана: действие, фандинг в $/ч, курсовой, «вход + выход» с окупаемостью, срок. Техника (шаг,
    слиппедж, газ по частям, остатки, лимиты, id плана) не пишется, пока в норме."""
    live = views.plan(_plan())
    assert flat(live).splitlines() == [
        "📝 <b>Вход AIW3 · 500 $ на ногу</b>", "Купить спот OKX DEX·BSC · шорт перп Aster",
        "Фандинг +0.050 %/ч → нам +0.25 $/ч", "Курсовой 0.00 %", "Вход + выход 1.14 $ · окупится за 4.6 ч", "⏱ 60 с"]
    for tech in ("слиппедж", "шаг", "α", "β", "Кошелёк", "Лимит", "E7K2", "газ", "⚠️"):
        assert tech not in live, tech
    assert views.SIM_PREFIX not in live and "Для live не задано" not in live and html_ok(live)
    txt = views.plan(_plan(sim=True, missing_owner_keys=("telegram.owner_id", "dex.impact_cap_pct")))
    assert txt.splitlines()[0] == views.SIM_PREFIX and txt.splitlines()[1].startswith("📝 ")
    assert "⚠️ Для live не задано: 2 · <code>статус</code>" in txt.splitlines() and html_ok(txt)
    neg = flat(views.plan(_plan(funding_pct_h=D("-0.01"), usd_per_h=D("-0.05"), breakeven_h=None,
                                basis_pct=D("-0.2"))))
    assert f"Фандинг {views.MINUS}0.010 %/ч → ⚠️ шорт ПЛАТИТ 0.05 $/ч" in neg          # минус U+2212
    assert "не окупится: фандинг против нас" in neg and f"Курсовой {views.MINUS}0.20 % против нас" in neg
    assert "2 клипа по 250 $" in flat(views.plan(_plan(clips_usd=(D(250), D(250))))).splitlines()
    assert "3 клипа: 250 + 150 + 100 $" in flat(views.plan(_plan(clips_usd=(D(250), D(150), D(100)))))
    kb = views.plan_keyboard("E7K2", "9f3a1c20", views.ok_label("entry", usd=D(200)))
    row = kb["inline_keyboard"][0]
    assert [flat(b["text"]) for b in row] == ["✅ Войти 200 $", "❌ Нет"]
    assert [parse.parse_callback(b["callback_data"]).action for b in row] == ["ok", "no"]
    assert [b["callback_data"] for b in row] == ["ok:E7K2:9f3a1c20", "no:E7K2:9f3a1c20"]     # данные кнопок прежние
    assert flat(views.ok_label("exit", usd=D(200), exit_all=False)) == "✅ Выйти 200 $"
    assert views.ok_label("exit") == "✅ Выйти всё" and views.ok_label("undo") == "✅ Откат"


def test_plan_threshold_lines_appear_only_past_the_threshold():
    """Пороги показа (tconfig.SHOW_*): каждое отклонение — своя строка ⚠️ сразу под заголовком; штатное — ничего."""
    assert "⚠️" not in views.plan(_plan())
    cases = {
        "Газ 0.60 $": dict(gas_usd_total=D("0.5"), approve_gas_usd=D("0.1")),
        "BNB 0.0002 — хватит на 12 свопов": dict(wallet_native=D("0.0002")),          # 0.0002 · 600 / 0.01
        "Плечо 2x ISOLATED": dict(leverage=2),
        "Плечо 1x CROSSED": dict(margin_type="CROSSED"),
        "Маржа Aster не прочитана": dict(margin_avail=None),
        "Баланс USDT не прочитан": dict(wallet_stable=None),
        "USDT на следующую сделку 500 $ не хватит: 300.00": dict(wallet_stable=D(800), max_open_deals=2),
        "Маржи Aster на следующую сделку не хватит: 200.00 из 500.00": dict(margin_avail=D(700), max_open_deals=2),
        "Фандинг ниже порога 0.100 %/ч": dict(min_funding_pct_h=D("0.1")),
        "Курсовой ниже порога 0.05 %": dict(min_basis_bps=D(5)),
    }
    for line, kw in cases.items():
        txt = flat(views.plan(_plan(**kw))).splitlines()
        assert txt[1] == f"⚠️ {line}" and sum(ln.startswith("⚠️") for ln in txt) == 1, (line, txt)
    # следующей сделки лимит не допускает (1 из 1 после входа) — нехватка на неё не пишется
    assert "следующую" not in views.plan(_plan(wallet_stable=D(800), margin_avail=D(700), max_open_deals=1))
    assert "Газ" not in views.plan(_plan(gas_usd_total=D("0.50")))                      # ровно на пороге — штатное
    # заметка движка и проверка вида об одном и том же — одна строка
    one = views.plan(_plan(margin_avail=None, notes=("маржа Aster не прочитана",)))
    assert one.count("Маржа Aster не прочитана") == 1


def test_exit_plan_and_perp_only():
    x = dict(intent_id="X3F7", kind="exit", deal_id="D7K2", clips_usd=(), leg_usd=D("500.4"))
    txt = flat(views.plan(_plan(**x, token_qty=D(2057611), perp_qty=D(2057000))))
    assert txt.splitlines() == ["📝 <b>Выход AIW3 · всё</b>", "Продать спот 2 057 611 · откупить шорт 2 057 000",
                                "Издержки выхода ≈ 0.57 $", "⏱ 60 с"]
    part = flat(views.plan(_plan(**x, token_qty=D(4889), perp_qty=D(4889), exit_all=False, req_usd=D(200),
                                 deal_leg_usd=D(500))))
    assert part.splitlines()[0] == "📝 <b>Выход AIW3 · 200 из 500 $</b>"
    po = flat(views.plan(_plan(**dict(x, leg_usd=D("300.07")), token_qty=D(7333), perp_qty=D(7333), perp_only=True,
                               exit_all=False, total_usd=D("0.19"))))
    assert po.splitlines() == ["📝 <b>Выход AIW3 · только перп</b>", "⚠️ Спот 7 333 AIW3 ≈ 300 $ останется без хеджа",
                               "Откупить шорт 7 333 AIW3 на Aster", "Издержки ≈ 0.19 $", "⏱ 60 с"]


def test_plan_hides_execution_technique():
    """C: α/β, риск голой ноги, предел времени, тревога ликвидации заморожены в плане (engine), но в сообщение не
    идут — у вида плана таких полей больше нет, в тексте их нет."""
    fields = set(views.PlanView.__dataclass_fields__)
    assert not fields & {"alpha", "beta_bps", "ab_auto", "unhedged_risk_usd", "exec_time_s", "liq_alert",
                         "slippage_pct", "cap_usd", "daily_stop", "route"}
    txt = views.plan(_plan())
    assert not any(w in txt for w in ("α", "β", "риск голой ноги", "время ≤", "тревога ликвидации", "маршрут"))


def test_plan_closed_is_one_line():
    t = views.plan_title("entry", "AIW3", usd=D(200))
    assert flat(views.plan_closed("ok", t, 1757703556)) == "⏳ <b>Вход AIW3 · 200 $ на ногу</b> — принят 18:59, исполняю"
    assert flat(views.plan_closed("expired", t)) == "⌛ <b>Вход AIW3 · 200 $ на ногу</b> — истёк, ничего не сделано"
    assert flat(views.plan_closed("no", t)) == "❌ <b>Вход AIW3 · 200 $ на ногу</b> — отменён"
    assert flat(views.plan_closed("requote", t)) == "🔁 <b>Вход AIW3 · 200 $ на ногу</b> — заменён, новый ниже"
    assert views.plan_closed("no", t, sim=True).startswith("🧪 ❌ ") and "\n" not in views.plan_closed("ok", t, NOW)
    assert views.plan_title("exit", "AIW3") == "Выход AIW3 · всё"
    assert flat(views.plan_title("exit", "AIW3", usd=D(200), exit_all=False, deal_usd=D(500))) == \
        "Выход AIW3 · 200 из 500 $"
    assert views.plan_title("exit", "AIW3", perp_only=True) == "Выход AIW3 · только перп"
    assert views.plan_title("rehedge", "<x>") == "Дохедж &lt;x&gt;"


def test_plan_head_from_intent_data_survives_restart(con):
    """Подпись кнопки и строка закрытия строятся из данных намерения и сделки, а не из HTML в памяти процесса."""
    did = store.create_deal(con, coin="AIW3", chain="bsc", token="0x" + "a1" * 20, token_dec=18, perp_venue="aster",
                            symbol="AIW3USDT", leg_usd=D(500), owner_json="{}", sim=True)
    cases = [("entry", {"coin": "AIW3", "usd": D(200)}, "Вход AIW3 · 200 $ на ногу", "✅ Войти 200 $"),
             ("exit", {"coin": "AIW3", "usd": D(200), "all": False}, "Выход AIW3 · 200 из 500 $", "✅ Выйти 200 $"),
             ("exit", {"coin": "AIW3", "usd": None, "all": True}, "Выход AIW3 · всё", "✅ Выйти всё"),
             ("exit", {"coin": "AIW3", "perp_only": True, "all": False}, "Выход AIW3 · только перп",
              "✅ Выйти из перпа"),
             ("rehedge", {"coin": "AIW3"}, "Дохедж AIW3", "✅ Дохедж")]
    for kind, spec, title, ok in cases:
        iid, _ = store.create_intent(con, deal_id=did, kind=kind, spec=spec, plan={}, now=NOW)
        h = views.intent_head(store.get_intent(con, iid), store.get_deal(con, did))
        assert (flat(h.title), flat(h.ok), h.sim) == (title, ok, True)
    assert views.intent_head(None) == views.PlanHead("План", "✅ Да", False)


def test_unknown_values_are_never_zero():
    h = views.halt(views.HaltView("E7K2", "entry", "AIW3", "timeout", clip=1, clips=1))
    assert "баланс ног неизвестен" in h and "Позиция Aster не прочитана" in h and "Кошелёк не прочитан" in h
    assert "Спот не прочитан · шорт не прочитан" in h and " 0 " not in h
    p = views.positions([views.PositionView("D7K2", "AIW3", "OPEN")], ts=NOW, matched=None)
    assert "позиция Aster не прочитана" in p and "кошелёк не прочитан" in p and "сверка ?" in p and " 0 " not in p
    assert views.num(None) == views.tok(None) == views.px(None) == views.usd(None) == views.money(None) == views.DASH
    assert views.pct(None) == views.DASH and views.dur(None) == views.DASH and views.hms(None) == views.DASH
    assert views.hm(None) == views.hours(None) == views.leg(None) == views.about(None) == views.DASH
    f = views.final(views.FinalView("E7K2", "entry", "AIW3", "bsc", "aster", "D7K2"))
    assert "Маржа Aster не прочитана" in f and "Баланс USDT не прочитан" in f and f.startswith("⚠️")


def test_halt_view_naked_leg_lists_owner_choices():
    h = flat(views.halt(views.HaltView("E7K2", "entry", "AIW3", "Aster -2019", deal_id="D7K2", clip=3, clips=5,
                                       perp_pos=D(-822400), wallet_tokens=D(1234567), unhedged_qty=D(412167),
                                       unhedged_usd=D(100), auto_unwind_s=D(600), ts=1757703556, step=D(1))))
    lines = h.splitlines()
    assert lines[0] == "🛑 <b>AIW3: без хеджа 412 167 ≈ 100 $ спота</b>"
    assert lines[1] == "Вход встал на клипе 3/5: Aster -2019" and lines[2] == "Спот +1 234 567 · шорт −822 400"
    assert lines[3:6] == ["<code>дохедж D7K2</code> — шорт ещё раз", "<code>откат D7K2</code> — продать 412 167 на DEX",
                          "<code>выход D7K2</code> — закрыть всё"]
    assert lines[6] == "Без команды откачу сам в 19:09 UTC"
    short = flat(views.halt(views.HaltView("E7K2", "exit", "AIW3", "IOC", deal_id="D7K2", clip=1, clips=1,
                                           perp_pos=D(-1500), wallet_tokens=D(1000), unhedged_qty=D(-500),
                                           unhedged_usd=D(20), step=D(1))))
    assert "без хеджа 500 ≈ 20.00 $ шорта" in short and "откат" not in short
    assert "<code>дохедж D7K2</code> — откупить 500 на перпе" in short
    # ноги ровно после клипа 1 из 2 — пауза, а не ложное «на клипе 2/2»
    b = flat(views.halt(views.HaltView("E7K2", "entry", "AIW3", "стоп владельца", deal_id="D7K2", clip=2, clips=2,
                                       perp_pos=D(-5), wallet_tokens=D(5), unhedged_qty=D(0), clips_done=1,
                                       state="PAUSED")))
    assert b.splitlines() == ["⏸ <b>AIW3: вход на паузе</b> после клипа 1/2", "Стоп владельца", "Ноги ровно ✓",
                              "<code>продолжить E7K2</code> · <code>выход D7K2</code>"]
    nothing = views.halt(views.HaltView("E7K2", "entry", "AIW3", "лимит", state="ABORTED"))
    assert nothing.startswith("⛔ <b>Вход AIW3 не начат</b>") and "Ничего не куплено" in nothing


def test_number_formatting():
    assert flat(views.tok(D(2057611), sign=True)) == "+2 057 611" and flat(views.tok(D("4902.15"))) == "4 902"
    assert views.tok(D("0.15"), sign=True) == "+0.15" and views.tok(D("-1.4")) == "−1"
    assert views.tok(D("0.0023")) == "0.0023" and views.tok(D(0)) == "0"
    assert views.tok(D("1.2345"), step=D("0.001")) == "1.234" and views.tok(D("0.0004"), step=D("0.001")) == "0.00040"
    assert views.px(D("0.040798")) == "0.04080" and views.px(D("0.00024301")) == "0.0002430"
    assert views.px(D("1.23456")) == "1.235" and flat(views.px(D("64210.5"))) == "64 210"
    assert views.usd(D("499.835")) == "499.84" and views.usd(D("-0.004")) == "0.00"
    assert flat(views.money(D("0.004"))) == "&lt; 0.01 $" and flat(views.money(D("0.004"), html=False)) == "< 0.01 $"
    assert flat(views.money(D("-0.85"), sign=True)) == "−0.85 $" and flat(views.money(D("0.07"), sign=True)) == "+0.07 $"
    assert flat(views.money(D(0))) == "0.00 $" and flat(views.money(D("0.004"), sign=True)) == "+&lt; 0.01 $"
    assert flat(views.leg(D(200))) == "200 $" and flat(views.leg(D("199.5"))) == "199.50 $"
    assert flat(views.about(D("250.4"))) == "250 $" and flat(views.about(D(50))) == "50.00 $"
    assert flat(views.pct(D("0.05"), 3, sign=True)) == "+0.050 %" and flat(views.pct(D("0.323"), sign=True)) == "+0.32 %"
    assert views.hours(D("3.88")) == "3.9 ч" and views.hours(D("0.75")) == "45 мин" and views.hours(D(50)) == "2 д 2 ч"
    assert views.dur(47) == "47 с" and views.dur(3 * 3600 + 600) == "3 ч 10 мин" and views.dur(90000) == "1 д 1 ч"
    assert [views.plural(n, "расчёт", "расчёта", "расчётов") for n in (1, 3, 5, 11, 21, 22)] == \
           ["расчёт", "расчёта", "расчётов", "расчётов", "расчёт", "расчёта"]
    assert views.short_addr("0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919") == "0xE4Eb…8919"
    assert views.hm(1757678642) == "12:04" and views.hms(1757678642, seconds=True) == "12:04:02 UTC"


def test_final_progress_positions_status_owner_missing():
    tx = "0x" + "12ab" * 16
    f = views.final(views.FinalView("E7K2", "entry", "AIW3", "bsc", "aster", "D7K2", leg_usd=D(500),
                                    spot_qty=D(2057611), spot_usd=D(500), perp_qty=D(2057000), dust_qty=D(611),
                                    step=D(1000), tx_hashes=(tx,), expected_usd_h=D("0.25"), basis_pct=D("0.12"),
                                    cost_usd=D("0.5"), exit_cost_usd=D("0.57"), breakeven_h=D("4.28"),
                                    wallet_stable=D(500), wallet_native=D("0.1"), margin_avail=D(500)))
    assert flat(f).splitlines() == [
        "✅ <b>AIW3 открыта · 500 $ на ногу</b>", "Спот +2 057 611 · шорт Aster −2 057 000 AIW3",
        "Фандинг +0.25 $/ч · курсовой +0.12 %", "Вход + выход 1.07 $ · окупится за 4.3 ч",
        f'<code>выход D7K2</code> · <a href="https://bscscan.com/tx/{tx}">tx 0x12ab…12ab</a>']
    assert html_ok(f) and "0xE4Eb" not in f and "мейкер" not in f and "Остатки" not in f
    live = dict(wallet_stable=D(1), wallet_native=D(1), margin_avail=D(1))
    x = flat(views.final(views.FinalView("X3F7", "exit", "AIW3", "bsc", "aster", "D7K2", pnl_spot_usd=D("-0.3"),
                                         pnl_perp_usd=D("-0.23"), funding_usd=D("0.75"), pnl_total_usd=D("0.22"),
                                         cost_usd=D("0.4"), **live)))
    assert x.splitlines() == ["✅ <b>AIW3 закрыта · +0.22 $</b>", "Спот −0.30 · перп −0.23 · фандинг +0.75",
                              "Издержки выхода 0.40 $"]
    sx = flat(views.final(views.FinalView("X3F7", "exit", "AIW3", "bsc", "aster", "D7K2", pnl_spot_usd=D("-0.3"),
                                          pnl_perp_usd=D("-0.23"), cost_usd=D("0.4"), sim=True)))
    assert sx.splitlines()[1:3] == ["✅ <b>AIW3 закрыта · −0.53 $</b>", "Без фандинга (симуляция): спот −0.30 · перп −0.23"]
    part = dict(spot_usd=D(200), deal_leg_usd=D(500), partial=True, rest_qty=D(7331), dust_qty=D(0), step=D(1),
                cost_usd=D("0.18"), sim=True)
    pp = flat(views.final(views.FinalView("X3F7", "exit", "AIW3", "bsc", "aster", "DKLQN", exit_all=False, **part)))
    assert pp.splitlines()[1:] == ["✅ <b>AIW3: выход 200 из 500 $</b>", "Остаток 7 331 AIW3 открыт и захеджирован ✓",
                                   "Издержки 0.18 $", "<code>выход DKLQN</code> · <code>позиции</code>"]
    under = flat(views.final(views.FinalView("X3F7", "exit", "AIW3", "bsc", "aster", "DKLQN", exit_all=True, **part)))
    assert under.splitlines()[1:3] == ["⚠️ <b>AIW3: выход 200 из 500 $</b>", "⚠️ Выход не довыполнен"]
    pr = views.progress(views.ProgressView("E7K2", "entry", "AIW3", 3, 5, spot_usd=D(300), imbalance_qty=D(567),
                                           sim=True))
    assert pr.startswith(views.SIM_PREFIX) and "клип 3/5" in pr and "⚠️ Без хеджа +567 AIW3" in pr.splitlines()
    ok = views.progress(views.ProgressView("E7K2", "entry", "AIW3", 2, 2, spot_usd=D(500), spot_qty=D(12222),
                                           perp_qty=D(12222), imbalance_qty=D(0), total_usd=D(500), step=D(1)))
    assert flat(ok).splitlines() == ["⏳ <b>Вход AIW3</b> · клип 2/2 · 500 из 500 $", "Спот +12 222 · шорт −12 222 ✓"]
    ok_deal = views.PositionView("D7K2", "AIW3", "OPEN", perp_qty=D(-5), spot_qty=D(5), delta_qty=D(0), step=D(1),
                                 funding_usd=D("0.75"), funding_n=3, held_s=11400, leg_usd=D(500),
                                 payback_h=D("0.75"))
    assert flat(views.positions([ok_deal], ts=NOW, matched=True)).splitlines() == [
        "📊 <b>1 сделка · фандинг +0.75 $</b> · сверка ✓", "AIW3 <code>D7K2</code> · 500 $ на ногу · 3 ч 10 мин",
        "До окупаемости ~45 мин"]
    bad = views.PositionView("DP9T3", "ZORA", "PAUSED", perp_qty=D(-5), spot_qty=D(7), delta_qty=D(2), step=D(1),
                             liq_dist_pct=D("8.2"), liq_alert_pct=D("24.3"), reason="stop", leg_usd=D(300), sim=True)
    two = flat(views.positions([ok_deal, bad], ts=NOW, matched=False, mismatch="DP9T3: позиция <x>")).splitlines()
    assert two[0] == "📊 <b>2 сделки · фандинг +0.75 $</b> · сверка ✗" and two[1] == "⚠️ Расхождение: DP9T3: позиция &lt;x&gt;"
    assert two[2] == ("⚠️ 🧪 ZORA <code>DP9T3</code>: без хеджа +2 ZORA; до ликвидации <b>8.20 %</b> (порог 24.30 %); "
                      "на паузе — стоп владельца")                              # проблемные — первыми
    near = views.PositionView("D1", "AIW3", "OPEN", perp_qty=D(-5), spot_qty=D(5), delta_qty=D(0), step=D(1),
                              liq_dist_pct=D(28), liq_alert_pct=D(24))            # меньше 1.25 × тревоги, но выше неё
    assert "до ликвидации 28.00 % (порог 24.00 %)" in flat(views.positions([near], ts=NOW, matched=True))
    assert flat(views.positions([], ts=NOW, matched=True)) == "📊 Открытых сделок нет"
    st = flat(views.status(views.StatusView(ts=NOW, mode="readonly", paused=True, running="E7K2", tg_last_ok_ago_s=3,
                                            checks=(("часы Aster", True, "0.3 с"), ("allowance USDT", False, "0")),
                                            daily_stop=None, wallet_stable=D(1), wallet_native=D(1), margin_avail=D(1))))
    assert st.splitlines() == ["🩺 <b>Статус</b> · только чтение · ⚠️ 1 проблема", "✗ allowance USDT: 0",
                               "⏸ пауза · идёт E7K2 · сделок —/— · дн. стоп не задан"]
    good = flat(views.status(views.StatusView(ts=NOW, mode="live", open_deals=0, max_open_deals=1, daily_stop=D(50),
                                              daily_used_usd=D("-1.23"), wallet_stable=D(1), wallet_native=D(1),
                                              margin_avail=D(1))))
    assert good.splitlines() == ["🩺 <b>Статус</b> · live · всё в порядке ✓", "Пауза нет · сделок 0/1 · день −1.23 из 50 $"]
    assert views.status(views.StatusView(ts=NOW, mode="dry")).startswith(views.SIM_PREFIX)
    om = flat(views.owner_missing(["limits.daily_loss_stop_usd"]))
    assert om == "⛔ <b>Вход запрещён</b>: не задано «дневной стоп»\nowner.toml → <code>limits.daily_loss_stop_usd</code>"
    om2 = views.owner_missing(["perp.aster.max_slip_bps", "exec.clips_max"], action="выход")
    assert "⛔ <b>Выход запрещён</b>: не задано 2" in om2 and "предел цены IOC Aster — <code>perp.aster.max_slip_bps</code>" in om2


def test_final_threshold_lines():
    """Итог: ⚠️ по порогам показа — удар ×1.5, издержки +25 %, дельта ≥ шага, ликвидация < 1.25 × тревоги, газ,
    запас BNB, плечо; заголовок тогда ⚠️, а не ✅."""
    base = dict(leg_usd=D(500), spot_qty=D(1000), perp_qty=D(1000), dust_qty=D(0), step=D(1), impact_usd=D("0.30"),
                planned_impact_usd=D("0.30"), cost_usd=D("0.6"), planned_cost_usd=D("0.6"), gas_usd=D("0.02"), swaps=2,
                native_px=D(600), wallet_native=D("0.1"), wallet_stable=D(1000), margin_avail=D(1000), leverage=1,
                margin_type="ISOLATED", liq_dist_pct=D(100), liq_alert_pct=D(20), open_deals=1, max_open_deals=1,
                expected_usd_h=D("0.25"), exit_cost_usd=D("0.6"))
    fin = lambda **kw: flat(views.final(views.FinalView("E1", "entry", "AIW3", "bsc", "aster", "D1", **{**base, **kw})))
    assert fin().startswith("✅") and "⚠️" not in fin()
    cases = {"Удар спота 0.46 $ — план 0.30 $": dict(impact_usd=D("0.46")),
             "Издержки 0.76 $ — план 0.60 $ (+27 %)": dict(cost_usd=D("0.76")),
             "Без хеджа +3 AIW3 ≈ 0.12 $": dict(dust_qty=D(3), dust_usd=D("0.12")),
             "До ликвидации 24.00 % — тревога при 20.00 %": dict(liq_dist_pct=D(24)),
             "Газ 0.52 $": dict(gas_usd=D("0.52")),
             "BNB 0.0003 — хватит на 18 свопов": dict(wallet_native=D("0.0003"), gas_usd=D("0.02"), swaps=2),
             "Плечо 3x ISOLATED": dict(leverage=3)}
    for line, kw in cases.items():
        txt = fin(**kw).splitlines()
        assert txt[0].startswith("⚠️ <b>AIW3 открыта") and txt[1] == f"⚠️ {line}", (line, txt)
    assert "Удар" not in fin(impact_usd=D("0.45")) and "Издержки 0.75" not in fin(cost_usd=D("0.75"))
    assert "До ликвидации" not in fin(liq_dist_pct=D(40))


def test_threshold_boundaries_each_rule():
    """Ревью 12.09: каждый порог показа (tconfig.SHOW_*) срабатывает сразу за границей и молчит на ней."""
    base = dict(leg_usd=D(500), spot_qty=D(1000), perp_qty=D(1000), dust_qty=D(0), step=D(1), impact_usd=D("0.30"),
                planned_impact_usd=D("0.30"), cost_usd=D("0.6"), planned_cost_usd=D("0.6"), gas_usd=D("0.02"), swaps=2,
                native_px=D(600), wallet_native=D("0.1"), wallet_stable=D(1000), margin_avail=D(1000), leverage=1,
                margin_type="ISOLATED", liq_dist_pct=D(100), liq_alert_pct=D(20), open_deals=1, max_open_deals=1,
                expected_usd_h=D("0.25"), exit_cost_usd=D("0.6"))
    fin = lambda **kw: flat(views.final(views.FinalView("E1", "entry", "AIW3", "bsc", "aster", "D1", **{**base, **kw})))
    warns = lambda t: [ln for ln in t.splitlines()[1:] if ln.startswith("⚠️")]
    assert warns(fin()) == []
    # дельта ног: ровно шаг — уже голая; меньше шага — штатная пыль; любой минус (лишний шорт) — голая
    assert warns(fin(dust_qty=D(1))) == ["⚠️ Без хеджа +1 AIW3"]
    assert warns(fin(dust_qty=D("0.99"))) == []
    assert warns(fin(dust_qty=D("-0.15"))) == ["⚠️ Без хеджа −0.15 AIW3"]
    # газ за команду (свопы + approve): 0.50 — штатно, 0.51 — ⚠️
    assert warns(fin(gas_usd=D("0.40"), approve_gas_usd=D("0.10"))) == []
    assert warns(fin(gas_usd=D("0.41"), approve_gas_usd=D("0.10"))) == ["⚠️ Газ 0.51 $"]
    # BNB: ровно на 20 свопов — штатно, на 18 — ⚠️ (своп 0.03 $ при BNB 600 $)
    assert warns(fin(wallet_native=D("0.001"), gas_usd=D("0.06"))) == []
    assert warns(fin(wallet_native=D("0.0009"), gas_usd=D("0.06"))) == ["⚠️ BNB 0.0009 — хватит на 18 свопов"]
    # следующая сделка того же размера: лимит допускает (1 из 2) — нехватка USDT и маржи видна; не допускает — нет
    nxt = dict(open_deals=1, max_open_deals=2, wallet_stable=D(300), margin_avail=D(200))
    assert warns(fin(**nxt)) == ["⚠️ USDT на следующую сделку 500 $ не хватит: 300.00",
                                 "⚠️ Маржи Aster на следующую сделку не хватит: 200.00 из 500.00"]
    assert warns(fin(**dict(nxt, open_deals=2))) == []
    # непрочитанное в live — ⚠️; в симуляции балансы по устройству не читаются — не пишем
    assert warns(fin(margin_avail=None)) == ["⚠️ Маржа Aster не прочитана"]
    assert "не прочитан" not in fin(margin_avail=None, wallet_stable=None, sim=True)
    assert warns(fin(margin_type="CROSSED")) == ["⚠️ Плечо 1x CROSSED"]
    # план: BNB не прочитан — ⚠️
    assert [ln for ln in flat(views.plan(_plan(wallet_native=None))).splitlines() if ln.startswith("⚠️")] == \
        ["⚠️ Баланс BNB не прочитан"]
    # прогресс: газ за порогом — ⚠️, на пороге — нет
    pg = lambda g: flat(views.progress(views.ProgressView("E1", "entry", "AIW3", 1, 2, imbalance_qty=D(0), step=D(1),
                                                          gas_usd=g)))
    assert pg(D("0.51")).splitlines()[1] == "⚠️ Газ 0.51 $" and "⚠️" not in pg(D("0.50"))
    # позиции: ликвидация ровно на 1.25 × тревоги — штатно
    edge = views.PositionView("D1", "AIW3", "OPEN", perp_qty=D(-5), spot_qty=D(5), delta_qty=D(0), step=D(1),
                              liq_dist_pct=D(30), liq_alert_pct=D(24))
    assert "ликвидации" not in views.positions([edge], ts=NOW, matched=True)


def test_review_safety_fixes_20260912():
    """Ревью безопасности C: голая нога в «позициях» — в $; шорт ПЛАТИТ — заголовок итога ⚠️, не ✅; 🧪 у ответов
    «продолжить» и перекотировки; «откат» в остановке продаёт кратное шагу (как propose_fix)."""
    naked = views.PositionView("DP9T3", "ZORA", "OPEN", perp_qty=D(-5), spot_qty=D(7), delta_qty=D(2), step=D(1),
                               delta_usd=D("0.12"))
    assert "⚠️ ZORA <code>DP9T3</code>: без хеджа +2 ZORA ≈ 0.12 $" in \
        flat(views.positions([naked], ts=NOW, matched=True)).splitlines()
    kw = dict(leg_usd=D(500), spot_qty=D(10), perp_qty=D(10), dust_qty=D(0), step=D(1), basis_pct=D("0.1"),
              cost_usd=D("0.3"), exit_cost_usd=D("0.3"), wallet_stable=D(1000), wallet_native=D(1),
              margin_avail=D(1000))
    neg = flat(views.final(views.FinalView("E1", "entry", "AIW3", "bsc", "aster", "D1", expected_usd_h=D("-0.07"),
                                           **kw)))
    assert neg.startswith("⚠️ <b>AIW3 открыта") and "Фандинг: ⚠️ шорт ПЛАТИТ 0.07 $/ч" in neg
    assert views.final(views.FinalView("E1", "entry", "AIW3", "bsc", "aster", "D1", expected_usd_h=D("0.07"),
                                       **kw)).startswith("✅")
    for t in (views.resume_checked("D1", sim=True), views.resume_mismatch("D1", "<x>", sim=True),
              views.requote("<x>", sim=True)):
        assert t.startswith(views.SIM_PREFIX + "\n") and html_ok(t), t
    assert not views.resume_checked("D1").startswith(views.SIM_MARK)
    h = flat(views.halt(views.HaltView("E1", "entry", "AIW3", "IOC", deal_id="D1", perp_pos=D(-10),
                                       wallet_tokens=D("20.7"), unhedged_qty=D("10.7"), unhedged_usd=D(1), step=D(1))))
    assert "<code>откат D1</code> — продать 10 на DEX" in h.splitlines()


def test_review_c_compliance_20260912():
    """Ревью соответствия C 12.09: голая нога — в $ везде (остаток после дохеджа, перезапуск, сверка на старте) и с
    командами; неизвестный шаг — не «ноги ровно ✓»; «фандинг 0.00 $» в «позициях» — штатное, не пишется."""
    fd = flat(views.fix_done(views.FixDoneView("rehedge", "AIW3", "DQ2RL", "PAUSED", D(6000), D("245.40"), "SELL",
                                               delta=D(111), step=D(1))))
    assert fd.splitlines()[:2] == ["⚠️ <b>Дохедж AIW3 выполнен</b>", "⚠️ Без хеджа +111 AIW3 ≈ 4.54 $"]
    rv = dict(intent_id="EK7Q2", kind="entry", deal_id="DK7Q2", clip=1, clips=2, matched=True, coin="AIW3",
              state="PAUSED", step=D(1))
    naked = flat(views.restart(views.RestartView(**rv, hedged=False, delta=D(4902), delta_usd=D("200.1"))))
    assert "⚠️ Без хеджа +4 902 AIW3 ≈ 200 $" in naked.splitlines()
    assert "<code>откат DK7Q2</code> — продать 4 902 на DEX" in naked.splitlines()
    unk = flat(views.restart(views.RestartView(**rv, hedged=None)))
    assert "✓" not in unk and "⚠️ Ровность ног не проверена: шаг перпа не прочитан" in unk.splitlines()
    assert "Ноги ровно ✓" in views.restart(views.RestartView(**rv, hedged=True))
    rc = flat(views.restart_check("AIW3", "DK7Q2", True, "как в журнале", "PAUSED", hedged=False, delta=D(-500),
                                  usd=D("-20.5"), step=D(1)))
    assert rc.splitlines() == ["⚠️ <b>AIW3</b>: сверено после перезапуска · на паузе",
                               "⚠️ Без хеджа −500 AIW3 ≈ 20.50 $", "<code>дохедж DK7Q2</code> — откупить 500 на перпе",
                               "<code>выход DK7Q2</code> — закрыть всё"]
    assert views.restart_check("AIW3", "DK7Q2", True, "", "OPEN", hedged=True).startswith("✅")
    zero = views.PositionView("D7K2", "AIW3", "OPEN", perp_qty=D(-5), spot_qty=D(5), delta_qty=D(0), step=D(1),
                              funding_usd=D(0), leg_usd=D(500))
    assert flat(views.positions([zero], ts=NOW, matched=True)).splitlines()[0] == "📊 <b>1 сделка</b> · сверка ✓"
    for t in (fd, naked, unk, rc):
        assert html_ok(t) and "-" not in re.sub(r"<[^>]+>", "", t).replace("—", ""), t


# ================================ API: ошибки и маскировка токена ================================
def test_api_error_mapping_and_request_shape():
    s = FakeSession([ok([]), err(409, "Conflict: terminated by other getUpdates request"), err(400, "Bad Request"),
                     err(403, "Forbidden: bot was blocked by the user"), err(500, "Internal"),
                     err(429, "Too Many Requests: retry after 7", retry_after=7), (502, None)])
    api = make_api(s)
    assert api.get_updates(42) == []
    p = s.posts[0]
    assert p["timeout"] == (5, 35) and p["json"]["timeout"] == 25 and p["json"]["offset"] == 42
    assert p["json"]["allowed_updates"] == ["message", "callback_query"]
    with pytest.raises(tgapi.TgConflict):
        api.get_updates(42)
    with pytest.raises(tgapi.TgBadRequest):
        api.send_message(1, "x")
    with pytest.raises(tgapi.TgForbidden):
        api.send_message(1, "x")
    with pytest.raises(tgapi.TgError) as ei:
        api.send_message(1, "x")
    assert ei.value.code == 500
    with pytest.raises(tgapi.TgRetryAfter) as ei:
        api.send_message(1, "x")
    assert ei.value.seconds == 7
    with pytest.raises(tgapi.TgNetwork):
        api.send_message(1, "x")
    assert s.posts[2]["json"]["parse_mode"] == "HTML"
    assert s.posts[2]["json"]["link_preview_options"] == {"is_disabled": True}
    body = FakeSession()
    make_api(body).send_message(1, "x", html=False)
    assert "parse_mode" not in body.posts[0]["json"] and "reply_markup" not in body.posts[0]["json"]


def test_token_never_leaks_from_api_errors_repr_or_pickle():
    boom = requests.ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
                                    f"with url: /bot{TOKEN}/getUpdates (Caused by NewConnectionError)")
    api = make_api(FakeSession([boom]))
    with pytest.raises(tgapi.TgNetwork) as ei:
        api.get_updates(0)
    e = ei.value
    assert TOKEN not in str(e) and "bot<redacted>" in str(e)
    assert e.__context__ is None and e.__cause__ is None          # трассировка не потащит исходный текст
    assert TOKEN not in repr(api) and TOKEN not in str(api)
    with pytest.raises(TypeError):
        pickle.dumps(api)
    with pytest.raises(ValueError) as ei:
        tgapi.TgApi("not-a-token-" + TOKEN.split(":")[1])
    assert TOKEN.split(":")[1] not in str(ei.value)


def test_token_redacted_in_logs(caplog, con):
    boom = requests.ConnectionError(f"Max retries exceeded with url: /bot{TOKEN}/getUpdates")
    p = tgpoller.Poller(make_api(FakeSession([boom])), con, lambda u: None, clock=Clock())
    caplog.set_level(logging.DEBUG)
    assert p.step() == 2.0
    assert TOKEN not in caplog.text and "bot<redacted>" in caplog.text
    # любой обработчик через RedactFormatter: и сообщение, и трассировка исключения
    buf = io.StringIO()
    lg = logging.getLogger("test_tg_redact")
    lg.propagate = False
    h = logging.StreamHandler(buf)
    lg.addHandler(h)
    try:
        keys.install_log_redaction(lg)
        lg.error("url https://api.telegram.org/bot%s/sendMessage", TOKEN)
        try:
            raise requests.ConnectionError(f"/bot{TOKEN}/getUpdates")
        except requests.ConnectionError:
            lg.exception("опрос")
    finally:
        lg.removeHandler(h)
    assert TOKEN not in buf.getvalue() and buf.getvalue().count("bot<redacted>") >= 2


# ================================ отправитель ================================
def _sender(*script, **kw):
    s = FakeSession(list(script))
    clk = Clock()
    return tgsender.Sender(make_api(s), clock=clk, sleep=clk.sleep, **kw), s, clk


def test_sender_429_waits_retry_after_and_resends_same_message():
    snd, s, clk = _sender(err(429, "Too Many Requests: retry after 7", retry_after=7), ok({"message_id": 11}))
    got = []
    assert snd.send(1, "отчёт", on_done=got.append)
    snd.drain()
    assert s.methods() == ["sendMessage", "sendMessage"]
    assert s.posts[0]["json"]["text"] == s.posts[1]["json"]["text"] == "отчёт"
    assert 8 in clk.sleeps and snd.retries_429 == 1 and snd.sent == 1 and snd.dropped == 0
    assert got == [{"message_id": 11}]
    snd2, s2, clk2 = _sender((429, None), ok())                  # 429 без JSON — ждём 5+1 по умолчанию
    snd2.send(1, "x")
    snd2.drain()
    assert 6 in clk2.sleeps and len(s2.posts) == 2


def test_sender_paces_at_1_05_s():
    snd, s, clk = _sender()
    snd.send(1, "a")
    snd.send(1, "b")
    snd.send(1, "c")
    snd.drain()
    assert len(s.posts) == 3 and clk.sleeps == [pytest.approx(1.05)] * 2


def test_sender_html_400_falls_back_to_plain_text():
    snd, s, _ = _sender(err(400, "Bad Request: can't parse entities: Unsupported start tag"), ok({"message_id": 3}))
    snd.send(1, "<b>Вход</b> &lt;x&gt;")
    snd.drain()
    assert s.posts[0]["json"]["parse_mode"] == "HTML"
    assert "parse_mode" not in s.posts[1]["json"] and s.posts[1]["json"]["text"] == "Вход <x>"
    assert snd.sent == 1 and snd.fails == 0


def test_sender_403_is_logged_and_bot_keeps_running():
    snd, s, _ = _sender(err(403, "Forbidden: bot was blocked by the user"), ok())
    got = []
    snd.send(1, "x", on_done=got.append)
    snd.drain()
    assert got == [None] and snd.dropped == 1 and snd.fails == 1
    snd.send(1, "y")
    snd.drain()
    assert snd.fails == 0 and snd.sent == 1


def test_sender_network_retries_then_gives_up():
    boom = requests.ConnectionError(f"/bot{TOKEN}/sendMessage")
    snd, s, clk = _sender(boom, requests.Timeout("read timeout"), ok())
    snd.send(1, "x")
    snd.drain()
    assert len(s.posts) == 3 and snd.sent == 1 and 2 in clk.sleeps and 4 in clk.sleeps
    snd2, s2, _ = _sender(boom, boom, boom)
    snd2.send(1, "x")
    snd2.drain()
    assert len(s2.posts) == tgsender.NET_RETRIES and snd2.dropped == 1


def test_sender_splits_long_message_buttons_on_last_chunk():
    snd, s, _ = _sender(ok({"message_id": 1}), ok({"message_id": 2}), ok({"message_id": 3}))
    text = "\n".join(f"строка {i} {'ж' * 80}" for i in range(120))
    kb = views.plan_keyboard("E7K2", "9f3a1c20")
    got = []
    snd.send(1, text, reply_markup=kb, on_done=got.append)
    snd.drain()
    assert len(s.posts) >= 3
    assert all("reply_markup" not in p["json"] for p in s.posts[:-1]) and s.posts[-1]["json"]["reply_markup"] == kb
    assert "\n".join(p["json"]["text"] for p in s.posts) == text
    assert got == [{"message_id": len(s.posts)}]


def test_sender_progress_edits_are_throttled_and_latest_wins():
    snd, s, clk = _sender()
    snd.edit(1, 5, "p1", throttle=True)
    snd.drain(force_edits=False)
    assert [p["json"]["text"] for p in s.posts] == ["p1"]
    snd.edit(1, 5, "p2", throttle=True)
    snd.edit(1, 5, "p3", throttle=True)
    snd.drain(force_edits=False)
    assert len(s.posts) == 1                                     # 5 с ещё не прошло
    clk.t += tconfig.TG_EDIT_MIN_S
    snd.drain(force_edits=False)
    assert [p["json"]["text"] for p in s.posts] == ["p1", "p3"]
    snd.edit(1, 5, "p4", throttle=True)
    snd.edit(1, 5, "закрыто", reply_markup=None)                 # обычная правка вытесняет ждущий прогресс
    snd.drain(force_edits=True)
    assert [p["json"]["text"] for p in s.posts] == ["p1", "p3", "закрыто"]
    assert "reply_markup" not in s.posts[-1]["json"]             # кнопки сняты


def test_sender_edit_not_modified_is_success():
    snd, s, _ = _sender(err(400, "Bad Request: message is not modified"))
    got = []
    snd.edit(1, 5, "same", on_done=got.append)
    snd.drain()
    assert got == [{}] and snd.fails == 0 and snd.dropped == 0 and len(s.posts) == 1


def test_sender_never_blocks_queue_full_drops_new():
    snd, s, _ = _sender(qmax=2)
    assert snd.send(1, "a") and snd.send(1, "b")
    t0 = time.monotonic()
    assert snd.send(1, "c") is False and time.monotonic() - t0 < 0.5
    assert snd.dropped == 1 and snd.qsize == 2


def test_sender_alarm_dedup_by_kind():
    snd, s, clk = _sender()
    assert snd.alarm(1, "⚠️ опрос: 503", "poll 503")
    assert snd.alarm(1, "⚠️ опрос: 504", "poll 504") is False     # цифры в виде не различают
    assert snd.alarm(1, "⚠️ другое", "rpc") is True
    snd.drain()
    assert len(s.posts) == 2
    clk.t += tgsender.ALARM_WINDOW_S
    snd.drain()
    assert len(s.posts) == 3 and s.posts[-1]["json"]["text"] == "⚠️ poll # — ещё 1 раз за 5 мин"
    snd2, s2, clk2 = _sender()                                   # вид по-русски, число с согласованием
    for _ in range(4):
        snd2.alarm(1, views.conflict_alarm(), "conflict")
    clk2.t += tgsender.ALARM_WINDOW_S
    snd2.drain()
    assert s2.posts[-1]["json"]["text"] == "⚠️ Telegram 409 — ещё 3 раза за 5 мин"


def test_sender_thread_delivers_and_closes():
    s = FakeSession()
    snd = tgsender.Sender(make_api(s), gap_s=0.0).start()
    assert snd.alive()
    snd.send(1, "a")
    snd.send(1, "⏹ служба останавливается")
    assert snd.close(timeout=5) == 0
    assert [p["json"]["text"] for p in s.posts] == ["a", "⏹ служба останавливается"]
    assert snd.send(1, "после закрытия") in (True, False)


# ================================ опрос ================================
def upd(uid, text="статус", frm=OWNER):
    return msg(frm=frm, text=text, uid=uid)


def test_poll_claims_before_offset_and_skips_redelivered(con):
    s = FakeSession([ok([upd(10), upd(11)]), ok([upd(11), upd(12)])])
    handled = []
    p = tgpoller.Poller(make_api(s), con, lambda u: handled.append(u["update_id"]) or "owner_msg", clock=Clock())
    assert p.step() == 0.0
    assert store.tg_offset(con) == 12 and handled == [10, 11]
    con.execute("DELETE FROM flags WHERE k=?", (store.FLAG_TG_OFFSET,))   # «упали до сохранения смещения»
    assert p.step() == 0.0
    assert handled == [10, 11, 12] and p.skipped == 1 and store.tg_offset(con) == 13
    assert s.posts[1]["json"]["offset"] == 0
    row = con.execute("SELECT verdict, text, user_id FROM tg_updates WHERE update_id=12").fetchone()
    assert tuple(row) == ("owner_msg", "статус", OWNER)
    assert p.last_ok == NOW


def test_poll_handler_crash_still_advances_offset(con, caplog):
    def bad(u):
        raise RuntimeError(f"упал /bot{TOKEN}/x")

    p = tgpoller.Poller(make_api(FakeSession([ok([upd(20)])])), con, bad, clock=Clock())
    caplog.set_level(logging.ERROR)
    assert p.step() == 0.0
    assert store.tg_offset(con) == 21
    assert con.execute("SELECT verdict FROM tg_updates WHERE update_id=20").fetchone()[0] == "error:RuntimeError"
    assert TOKEN not in caplog.text


def test_poll_409_alarms_once_and_backs_off(con):
    c409 = err(409, "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running")
    alarms = []
    p = tgpoller.Poller(make_api(FakeSession([c409, c409, ok([]), c409])), con, lambda u: None,
                        on_alarm=lambda k, t: alarms.append((k, t)), clock=Clock())
    assert p.step() == 30.0 and p.step() == 60.0
    assert len(alarms) == 1 and alarms[0][0] == "conflict" and "409" in alarms[0][1]
    assert p.step() == 0.0 and p.conflicts == 0
    assert p.step() == 30.0 and len(alarms) == 2               # после успешного опроса — снова тревога


def test_poll_429_and_backoff(con):
    boom = requests.ConnectionError("down")
    p = tgpoller.Poller(make_api(FakeSession([err(429, "slow", retry_after=3), boom, boom, boom, boom, boom, boom])),
                        con, lambda u: None, clock=Clock())
    assert p.step() == 4.0
    assert [p.step() for _ in range(6)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_steady_409_is_contact_so_watchdog_neither_renews_nor_exits(con):
    """Токен опрашивает другой процесс: 409 — это ответ Telegram. Новая сессия его не лечит, а выход с кодом 3 крутил
    бы рестарты каждые ~20 мин. Остаётся тревога о конфликте (раз в час, в Poller)."""
    clk = Clock()
    c409 = err(409, "Conflict: terminated by other getUpdates request")
    api = make_api(FakeSession([c409] * 40))
    p = tgpoller.Poller(api, con, lambda u: None, clock=clk)
    wd = tgpoller.Watchdog(api, p, executor_busy=lambda: False, clock=clk)
    for _ in range(30):                                          # ~53 мин сплошного 409
        clk.t += 106
        p.step()
        assert wd.check() is None
    assert api.renewals == 0 and p.conflicts == 30


def test_startup_refuses_webhook_and_never_deletes_it():
    s = FakeSession([ok({"id": 1, "username": "fb_bot"}),
                     ok({"url": "https://hook.example.com/secret-path/xyz", "pending_update_count": 3})])
    alarms = []
    with pytest.raises(tgpoller.WebhookSet) as ei:
        tgpoller.startup_check(make_api(s), on_alarm=lambda k, t: alarms.append(t))
    assert ei.value.host == "https://hook.example.com"
    assert "secret-path" not in alarms[0] and "hook.example.com" in alarms[0]
    assert s.methods() == ["getMe", "getWebhookInfo"]             # deleteWebhook не вызывается
    s2 = FakeSession([ok({"id": 1, "username": "fb_bot"}), ok({"url": "", "pending_update_count": 0})])
    assert tgpoller.startup_check(make_api(s2))["username"] == "fb_bot"


def test_watchdog_renews_deaf_poller_and_exits_only_when_idle(tmp_path):
    clk = Clock()
    api = make_api(*[FakeSession() for _ in range(40)])
    p = tgpoller.Poller(api, None, lambda u: None, clock=clk)
    p.last_ok = clk()
    busy = {"v": True}
    alarms = []
    wd = tgpoller.Watchdog(api, p, executor_busy=lambda: busy["v"], on_alarm=lambda k, t: alarms.append(k),
                           clock=clk, health_path=tmp_path / "tg_health.json")
    clk.t += 100
    assert wd.check() is None and api.renewals == 0
    clk.t += 6                                                   # 106 с без успешного опроса
    assert wd.check() == "renewed" and api.renewals == 1
    clk.t += 44
    assert wd.check() is None                                    # после обновления ждём снова 105 с
    clk.t += 62
    assert wd.check() == "renewed" and api.renewals == 2
    health = json.loads((tmp_path / "tg_health.json").read_text())
    assert health["reconnects_24h"] == 2 and health["last_ok"] == NOW and "pid" in health
    for _ in range(9):                                           # 11 обновлений за час
        clk.t += 106
        r = wd.check()
    assert r == "renewed" and alarms == ["tg_watchdog"]          # исполнитель занят — не выходим, только тревога
    busy["v"] = False
    clk.t += 106
    assert wd.check() == "exit"
    p.last_ok = clk()                                            # опрос ожил — обновлений больше нет
    clk.t += 10
    assert wd.check() in (None, "exit")


def test_watchdog_restarts_dead_poller_thread():
    clk = Clock()
    api = make_api(FakeSession())
    p = tgpoller.Poller(api, None, lambda u: None, clock=clk)
    p.last_ok = clk()
    restarted = []
    wd = tgpoller.Watchdog(api, p, poller_alive=lambda: False, restart_poller=lambda: restarted.append(1), clock=clk)
    assert wd.check() == "restarted" and restarted == [1]
    p.stop()                                                     # остановленный намеренно — не воскрешаем
    assert wd.check() is None and restarted == [1]


def test_hung_getupdates_is_renewed_and_polling_resumes(con):
    release = threading.Event()

    class HangSession(FakeSession):
        def post(self, url, json=None, timeout=None):
            self.posts.append({"method": url.rsplit("/", 1)[-1], "json": json, "timeout": timeout})
            release.wait(5)                                      # полуоткрытый сокет: висит до закрытия сессии
            raise requests.ConnectionError("connection aborted")

        def close(self):
            self.closed = True
            release.set()

    def slow_ok(url, body):
        time.sleep(0.01)
        return ok([])

    s1, s2 = HangSession(), FakeSession(default=slow_ok)
    api = make_api(s1, s2)
    clk = Clock()
    p = tgpoller.Poller(api, con, lambda u: None, clock=clk, sleep=lambda s: time.sleep(0.01))
    t = threading.Thread(target=p.run, daemon=True)
    t.start()
    try:
        deadline = time.time() + 5
        while not s1.posts and time.time() < deadline:
            time.sleep(0.01)
        assert s1.posts and p.last_ok is None
        wd = tgpoller.Watchdog(api, p, clock=clk)
        clk.t += tconfig.TG_RENEW_AFTER_S + 1
        assert wd.check() == "renewed" and s1.closed
        deadline = time.time() + 5
        while p.last_ok is None and time.time() < deadline:
            time.sleep(0.01)
        assert p.last_ok is not None and s2.posts and api.renewals == 1
    finally:
        p.stop()
        release.set()
        t.join(5)
    assert not t.is_alive()


def test_write_health_is_atomic(tmp_path):
    path = tmp_path / "rt" / "tg_health.json"
    assert tgpoller.write_health(path, {"last_ok": 1.5, "pid": 7})
    assert json.loads(path.read_text()) == {"last_ok": 1.5, "pid": 7}
    assert not (tmp_path / "rt" / "tg_health.json.tmp").exists()
