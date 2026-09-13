"""Тестировщик, общее ядро: подставные истины бирж (Truth), синтетический table.json и БД. Сети нет."""
import json
import random
from decimal import Decimal
import pytest
from funding_bot import audit, config, db, cli

H = 3600_000
TS = 1_789_254_300            # снимок дашборда: 5 мин после круглого часа (1_789_254_000 — ровно час)
TS_MS = TS * 1000
HOUR = 1_789_254_000_000


def mk_truth(venue, markets, rates, hist=None, note=None, twins=True):
    """Класс-истина с данными в атрибутах: аудитор создаёт его без аргументов, как настоящий."""
    return type(f"Fake_{venue}", (), dict(
        venue=venue, history_note=note, quote_twins=twins, site_hours=None, http=None,
        markets=lambda self: markets, rates=lambda self: rates,
        history=lambda self, s, a, b: None if hist is None else [(ms, r) for ms, r in hist.get(s, []) if a <= ms <= b]))


def M(base, tradable=True, cls="crypto", iv=8, note=None):
    return dict(base=base, tradable=tradable, cls=cls, interval_h=iv, name=None, note=note)


def R(rate, iv=8, nxt=None):
    return dict(rate=Decimal(str(rate)), interval_h=iv, next_ms=nxt, kind="predicted")


def wins(**kw):
    return {w: dict(kw.get(w, {})) for w in ("24", "72", "168")}


def ff(va, sa, vb, sb, rha, rhb, iva=8, ivb=8, na=None, nb=None, wa=None, wb=None, base=None, cls="crypto", stale=False):
    w = {}
    for k in ("24", "72", "168", "720"):
        a, b = (wa or {}).get(k, {}), (wb or {}).get(k, {})
        w[k] = dict(a=a.get("sum"), na=a.get("n", 0), b=b.get("sum"), nb=b.get("n", 0), spread=None,
                    incomplete=bool(a.get("incomplete") or b.get("incomplete")))
    return dict(key=f"{va}:{sa}|{vb}:{sb}", base=base or sa.replace("USDT", ""), cls=cls, va=va, vb=vb, sa=sa, sb=sb,
                rate_h_a=rha, rate_h_b=rhb, iv_a=iva, iv_b=ivb, next_a=na, next_b=nb, windows=w, stale=stale, mismatch=False)


def sf(perp_ex, perp, rh, iv=8, nxt=None, spot_ex="gate_spot", spot=None, w=None, base=None, cls="crypto"):
    ws = {k: dict(spread=(w or {}).get(k, {}).get("sum"), n=(w or {}).get(k, {}).get("n", 0),
                  incomplete=bool((w or {}).get(k, {}).get("incomplete"))) for k in ("24", "72", "168", "720")}
    return dict(key=f"{spot_ex}:{spot}|{perp_ex}:{perp}", base=base or perp.replace("USDT", ""), cls=cls, spot_ex=spot_ex,
                spot=spot or perp, perp_ex=perp_ex, perp=perp, rate_h=rh, period=iv, next_ms=nxt, windows=ws, stale=False,
                mismatch=False)


def write_env(tmp_path, ff_rows=(), sf_rows=(), instruments=(), events=(), cls_col=False):
    table = dict(ts=TS, tick_ts=TS, venues=list(config.PERP_VENUES), ff_rows=list(ff_rows), sf_rows=list(sf_rows))
    tp = tmp_path / "table.json"
    tp.write_text(json.dumps(table))
    dp = tmp_path / "f.db"
    con = db.connect(dp)
    if cls_col:
        con.execute("ALTER TABLE instruments ADD COLUMN cls TEXT")
    for ex, sym, base, *rest in instruments:
        if cls_col:
            con.execute("INSERT INTO instruments(exchange,symbol,base,cls,last_seen) VALUES(?,?,?,?,?)", (ex, sym, base, rest[0] if rest else None, TS))
        else:
            con.execute("INSERT INTO instruments(exchange,symbol,base,last_seen) VALUES(?,?,?,?)", (ex, sym, base, TS))
    con.executemany("INSERT INTO funding_events(exchange,symbol,funding_ms,rate) VALUES(?,?,?,?)", list(events))
    con.commit(); con.close()
    return tp, dp


# --- 1. присутствие ---------------------------------------------------------------------------------------------------
def test_presence_ok_and_error_classification():
    markets = {"AAAUSDT": M("AAA"), "BBBUSDT": M("BBB"), "CCCUSDT": M("CCC"), "AAAUSDC": M("AAA"),
               "EEEUSDT": M("EEE", cls="other"), "FFFUSDT": M("FFF", tradable=False, note="PERPETUAL / USDT / статус SETTLING"),
               "SPOTYUSDT": M("SPOTY"), "STKUSDT": M("STK", cls="equity"), "QNTXUSDT": M("QNTX", cls="equity"),
               "1000PEPEUSDT": M("1000PEPE"), "HHHUSDT": M("HHH", tradable=False, note="статус SETTLING")}
    legs = audit.dashboard_legs(dict(ff_rows=[ff("binance", "AAAUSDT", "hyperliquid", "AAA", 1e-5, 1e-5),
                                              ff("binance", "HHHUSDT", "hyperliquid", "HHH", 1e-5, 1e-5),
                                              ff("binance", "GGGUSDT", "hyperliquid", "GGG", 1e-5, 1e-5)]))
    uni = {("hyperliquid", "BBB"): dict(base="BBB", cls=None), ("hyperliquid", "EEE"): dict(base="EEE", cls=None),
           ("gate_spot", "SPOTY_USDT"): dict(base="SPOTY", cls=None), ("gate_spot", "STK_USDT"): dict(base="STK", cls=None),
           ("hyperliquid", "xyz:QNT"): dict(base="QNT", cls="equity"),               # QNTX — акция, канон QNT (config.PERP_CANON)
           ("hyperliquid", "kPEPE"): dict(base="PEPE", cls="crypto")}
    rows, stale = audit.presence("binance", markets, legs, uni)
    by = {r["symbol"]: r for r in rows}
    assert by["AAAUSDT"]["status"] == "present"
    assert by["BBBUSDT"]["status"] == "missing" and "hyperliquid:BBB" in by["BBBUSDT"]["why"]
    assert "коллектора рынка нет" in by["BBBUSDT"]["why"]
    assert by["CCCUSDT"]["status"] == "ok_alone"                                        # не с чем паровать — OK
    assert by["AAAUSDC"]["status"] == "ok_twin" and "AAAUSDT" in by["AAAUSDC"]["why"]    # дубль по квоте
    assert by["EEEUSDT"]["status"] == "ok_class"                                        # нераспознанный класс пар не строит
    assert by["FFFUSDT"]["status"] == "not_tradable"
    assert by["SPOTYUSDT"]["status"] == "missing" and "gate_spot:SPOTY_USDT" in by["SPOTYUSDT"]["why"]   # спот той же монеты
    assert by["STKUSDT"]["status"] == "ok_alone"                                        # спот той же буквы акции парой не считается
    assert by["QNTXUSDT"]["status"] == "missing"                                        # канон класса: QNTX ↔ xyz:QNT
    assert by["1000PEPEUSDT"]["status"] == "missing"                                    # 1000PEPE ↔ kPEPE по базе PEPE
    st = {x["symbol"]: x["status"] for x in stale}
    assert st == {"GGGUSDT": "absent", "HHHUSDT": "untradable"}                         # делистинг и чужой символ на дашборде


def test_presence_class_rules_and_unpaired():
    # тот же тикер, другой класс в БД — по замыслу не пара; класс пары не записан — «проверить глазами», не ошибка
    uni = {("binance", "BNCUSDT"): dict(base="BNC", cls="equity"), ("aster", "MAXUSDT"): dict(base="MAX", cls=None)}
    rows, _ = audit.presence("hyperliquid", {"BNC": M("BNC", iv=1), "xyz:MAX": M("MAX", cls="equity", iv=1)}, {}, uni)
    by = {r["symbol"]: r for r in rows}
    assert by["BNC"]["status"] == "ok_alone"
    assert by["xyz:MAX"]["status"] == "check"
    # пара уже на дашборде (строка с классом) — это доказанная пара
    idx = {"MAX": [("bitget", "MAXUSDT", "equity")]}
    rows, _ = audit.presence("hyperliquid", {"xyz:MAX": M("MAX", cls="equity", iv=1)}, {}, uni, idx)
    assert rows[0]["status"] == "missing"
    # config.PERP_UNPAIRED: Extended XIAOMI-USD — OK с причиной владельца
    rows, _ = audit.presence("extended", {"XIAOMI-USD": M("XIAOMI", cls="equity", iv=1)}, {},
                             {("bitget", "XIAOMIUSDT"): dict(base="XIAOMI", cls="equity")})
    assert rows[0]["status"] == "ok_unpaired" and "HKD" in rows[0]["why"]
    # у HIP-3 рынки одной монеты — не дубли: xyz:NET на дашборде не извиняет para:NET
    legs = audit.dashboard_legs(dict(ff_rows=[ff("binance", "NETUSDT", "hyperliquid", "xyz:NET", 1e-5, 1e-5, base="NET")]))
    mk = {"xyz:NET": M("NET", cls="equity", iv=1), "para:NET": M("NET", cls="equity", iv=1)}
    uni = {("binance", "NETUSDT"): dict(base="NET", cls="equity")}
    by = {r["symbol"]: r["status"] for r in audit.presence("hyperliquid", mk, legs, uni, quote_twins=False)[0]}
    assert by == {"xyz:NET": "present", "para:NET": "missing"}


# --- 2. текущий фандинг ---------------------------------------------------------------------------------------------
def test_rate_tolerance_interval_and_next_time():
    assert audit.compare_rate(1e-5, 1.1e-5) is None                     # 1e-6 ≤ 2e-6 в час
    assert audit.compare_rate(1e-4, 1.09e-4) is None                    # 9 % < 10 %
    assert audit.compare_rate(1e-4, 1.12e-4)["kind"] == "значение"      # 12 % > 10 % и > 2e-6
    assert audit.compare_rate(1e-5, 1.25e-5) is not None                # 2.5e-6 > 2e-6
    assert audit.compare_rate(None, None) is None and audit.compare_rate(1e-5, None)["kind"] == "у нас ставки нет"
    L = dict(rate_h=1e-5, iv=4, next_ms=HOUR + 4 * H, rows=["k"], stale_rows=0)
    assert audit.leg_rate_issue("X", R(8e-5, 8, HOUR + 4 * H), None, L)["problems"] == ["интервал"]   # 8e-5/8 = 1e-5
    x = audit.leg_rate_issue("X", R(4e-5, 4, HOUR + 4 * H + 6 * 60_000), None, L)
    assert x["problems"] == ["время расчёта"]                            # 6 мин > 5 мин
    assert audit.leg_rate_issue("X", R(4e-5, 4, HOUR + 4 * H + 4 * 60_000), None, L) is None


def test_systematic_x8_sign_and_interval_are_named():
    syms = [f"C{i}USDT" for i in range(6)]
    rates = {s: R(8e-5 * (i + 1), 8) for i, s in enumerate(syms)}          # в час 1e-5 × (i+1)
    legs8 = audit.dashboard_legs(dict(sf_rows=[sf("binance", s, 8e-5 * (i + 1)) for i, s in enumerate(syms)]))
    issues, n = audit.check_rates("binance", rates, {}, legs8)
    assert n == 6 and len(issues) == 6 and all(x["kind"].startswith("×8") for x in issues)
    s = audit.systematic(issues, n)
    assert s and s["kind"] == "×8" and "×8" in s["text"] and "6 из 6" in s["text"]
    # ставка за интервал вместо часа при разных интервалах: ×4 и ×8 вперемешку — один системный вид «×интервал»
    rates_iv = {s: R(4e-5, 4 if i % 2 else 8) for i, s in enumerate(syms)}
    legs_iv = audit.dashboard_legs(dict(sf_rows=[sf("binance", s, 4e-5, iv=4 if i % 2 else 8) for i, s in enumerate(syms)]))
    issues, n = audit.check_rates("binance", rates_iv, {}, legs_iv)
    s = audit.systematic(issues, n)
    assert s and s["kind"] == "×интервал" and "не приведена к часу" in s["text"]
    # перевёрнутый знак
    legs_neg = audit.dashboard_legs(dict(sf_rows=[sf("binance", s, -1e-5 * (i + 1)) for i, s in enumerate(syms)]))
    issues, n = audit.check_rates("binance", rates, {}, legs_neg)
    assert audit.systematic(issues, n)["kind"] == "ЗНАК (×−1)"
    # одиночное расхождение — не системно
    legs_one = audit.dashboard_legs(dict(sf_rows=[sf("binance", s, (8e-5 if i == 0 else 1e-5) * (i + 1)) for i, s in enumerate(syms)]))
    issues, n = audit.check_rates("binance", rates, {}, legs_one)
    assert len(issues) == 1 and audit.systematic(issues, n) is None


def test_recheck_drops_live_forecast_noise():
    """MANTRA 11.09: первый снимок разошёлся втрое, через секунды совпали — живая прогнозная ставка, не ошибка."""
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("aster", "MANTRAUSDT", -1.56e-4 / 4, iv=4), sf("aster", "BADUSDT", 8e-4 / 4, iv=4)]))
    issues, _ = audit.check_rates("aster", {"MANTRAUSDT": R(-5.1e-5, 4), "BADUSDT": R(1e-4, 4)}, {}, L1)
    assert {x["symbol"] for x in issues} == {"MANTRAUSDT", "BADUSDT"}
    L2 = audit.dashboard_legs(dict(sf_rows=[sf("aster", "MANTRAUSDT", -2.9e-5 / 4, iv=4), sf("aster", "BADUSDT", 8e-4 / 4, iv=4)]))
    kept, gone = audit.recheck("aster", issues, {"MANTRAUSDT": R(-2.9e-5, 4), "BADUSDT": R(1e-4, 4)}, {}, L2)
    assert gone == 1 and [x["symbol"] for x in kept] == ["BADUSDT"] and kept[0]["kind"].startswith("×8")


def test_recheck_band_forgives_dashboard_one_tick_behind():
    """Прогон 13.09 01:02: у Gate 11 из 11 расхождений дашборд на перепроверке равен ПЕРВОМУ чтению истины (哈基米_USDT
    биржа +0.0115 %/дашборд +0.0101 %, перепроверка +0.0140 %/+0.0115 %) — таблица снята тиком раньше истины. Эксперимент
    «одно мгновение» на всех 7 площадках: в одно мгновение ставки клиента коллектора и истины равны точно."""
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("gate", "哈基米_USDT", 1.0075e-4, iv=4)]))
    issues, _ = audit.check_rates("gate", {"哈基米_USDT": R(1.1475e-4 * 4, 4)}, {}, L1)
    assert len(issues) == 1                                              # первый снимок: 12 % > 10 %
    L2 = audit.dashboard_legs(dict(sf_rows=[sf("gate", "哈基米_USDT", 1.1475e-4, iv=4)]))
    lag = []
    kept, gone = audit.recheck("gate", issues, {"哈基米_USDT": R(1.395e-4 * 4, 4)}, {}, L2, lag)
    assert kept == [] and gone == 1 and [x["symbol"] for x in lag] == ["哈基米_USDT"]
    # HL REZ: истина за 25 с ушла −0.0019 → −0.0028 %, дашборд −0.0016 % — внутри запаса «сдвиг истины между чтениями»
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("hyperliquid", "REZ", -2.3e-5, iv=1)]))
    issues, _ = audit.check_rates("hyperliquid", {"REZ": R(-1.9e-5, 1)}, {}, L1)
    L2 = audit.dashboard_legs(dict(sf_rows=[sf("hyperliquid", "REZ", -1.6e-5, iv=1)]))
    assert audit.recheck("hyperliquid", issues, {"REZ": R(-2.8e-5, 1)}, {}, L2) == ([], 1)
    # ошибка значения, которую коридор не покрывает: истина стоит, дашборд вдвое выше — остаётся
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("gate", "X_USDT", 6e-5, iv=1)]))
    issues, _ = audit.check_rates("gate", {"X_USDT": R(3e-5, 1)}, {}, L1)
    kept, gone = audit.recheck("gate", issues, {"X_USDT": R(3.1e-5, 1)}, {}, L1)
    assert gone == 0 and kept[0]["problems"] == ["ставка"] and kept[0]["reads"] == [(3e-5, 6e-5), (3.1e-5, 6e-5)]


def test_recheck_band_never_forgives_persistent_multiplier_or_sign():
    """×8 и знак держатся во всех снимках — единицы, а не время: даже когда истина скачет (широкий коридор), ошибка
    остаётся и называется видом из всех снимков."""
    # ×8: истина 1e-5 → 9e-5 в час, дашборд 8e-5 → 7.2e-4 — ×8 оба раза
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("binance", "AUSDT", 8e-5, iv=8)]))
    issues, _ = audit.check_rates("binance", {"AUSDT": R(1e-5 * 8, 8)}, {}, L1)
    L2 = audit.dashboard_legs(dict(sf_rows=[sf("binance", "AUSDT", 7.2e-4, iv=8)]))
    kept, _ = audit.recheck("binance", issues, {"AUSDT": R(9e-5 * 8, 8)}, {}, L2)
    assert len(kept) == 1 and kept[0]["persistent"] == "×8"
    # ×8 ВНУТРИ коридора: истина упала 8e-5 → 1e-5 в час (коридор [−6e-5, 1.5e-4]), дашборд 6.4e-4 → 8e-5 — ×8 на
    # обоих снимках, и 8e-5 равен старой истине: без проверки «множитель во всех снимках» коридор простил бы ×8
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("binance", "BUSDT", 6.4e-4, iv=8)]))
    issues, _ = audit.check_rates("binance", {"BUSDT": R(8e-5 * 8, 8)}, {}, L1)
    L2 = audit.dashboard_legs(dict(sf_rows=[sf("binance", "BUSDT", 8e-5, iv=8)]))
    band = audit.live_band([8e-5, 1e-5])
    assert band[0] <= 8e-5 <= band[1]
    kept, _ = audit.recheck("binance", issues, {"BUSDT": R(1e-5 * 8, 8)}, {}, L2)
    assert len(kept) == 1 and kept[0]["persistent"] == "×8"
    # знак: истина +1e-5 → +3e-5 (сдвиг 2e-5, коридор [−1e-5, 5e-5]), дашборд −1e-5 → −8e-6 — знак на обоих снимках
    L1 = audit.dashboard_legs(dict(sf_rows=[sf("apex", "STXUSDT", -1e-5, iv=1)]))
    issues, _ = audit.check_rates("apex", {"STXUSDT": R(1e-5, 1)}, {}, L1)
    L2 = audit.dashboard_legs(dict(sf_rows=[sf("apex", "STXUSDT", -8e-6, iv=1)]))
    kept, _ = audit.recheck("apex", issues, {"STXUSDT": R(3e-5, 1)}, {}, L2)
    assert len(kept) == 1 and kept[0]["persistent"].startswith("ЗНАК")
    # ApeX 13.09 02:01, LDOUSDT: биржа −0.0015 % → +0.0013 % → +0.0013 %, дашборд на тик позже +0.0013 % → −0.0015 % →
    # −0.0015 % — знак «перевёрнут» на всех снимках, но дашборд показывает прошлое чтение биржи: отставание, прощается
    reads = [(-1.5e-5, 1.3e-5), (1.3e-5, -1.5e-5), (1.3e-5, -1.5e-5)]
    assert audit.persistent_kind(reads) is None
    L = lambda o: audit.dashboard_legs(dict(sf_rows=[sf("apex", "LDOUSDT", o, iv=1)]))
    issues, _ = audit.check_rates("apex", {"LDOUSDT": R(-1.5e-5, 1)}, {}, L(1.3e-5))
    kept, gone = audit.recheck("apex", issues, {"LDOUSDT": R(1.3e-5, 1)}, {}, L(-1.5e-5))
    assert kept == [] and gone == 1                                      # дашборд = первое чтение биржи
    # тот же рисунок, но дашборд не равен ни одному чтению биржи — знак остаётся ошибкой
    assert audit.persistent_kind([(1e-5, -2.5e-5), (3e-5, -2.2e-5)]) == "ЗНАК"
    # случайные отношения отставания (HL xyz:QNT: «ЗНАК и ×1/3», потом значение) — не единицы
    assert audit.persistent_kind([(-1.77e-5, 6.25e-6), (-3.41e-5, -2.38e-5)]) is None
    assert audit.persistent_kind([(3.57e-5, 1.69e-5), (4.80e-5, 3.99e-5)]) is None
    assert audit.live_band([]) is None and audit.live_band([None, 1e-5]) == pytest.approx((8e-6, 1.2e-5))


# --- 3. окна истории ----------------------------------------------------------------------------------------------------
def _hourly(n=200, jitter=43):
    return [(HOUR - k * H + jitter, Decimal("0.00001") * (k % 7 + 1)) for k in range(n)][::-1]


def _dash(events, anchor, w, n_drop=0, incomplete=False):
    xs = [float(r) for ms, r in events if anchor - w * H < ms <= anchor]
    xs = xs[n_drop:]
    return dict(sum=sum(xs) if xs else None, n=len(xs), incomplete=incomplete)


def test_windows_follow_dashboard_anchor():
    ev = _hourly()
    pend = HOUR - 1                                               # calc.window_anchor: nextFundingTime − интервал − 1
    dash = {"24": _dash(ev, pend, 24), "72": _dash(ev, TS_MS, 72), "168": _dash(ev, TS_MS, 168, n_drop=1)}
    res = audit.check_windows(ev, dash, TS_MS)
    assert res["24"]["status"] == "ok" and res["24"]["anchor"] == "перед несобранным расчётом" and res["24"]["n_truth"] == 24
    assert res["72"]["status"] == "ok" and res["72"]["anchor"] == "снимок"
    assert res["168"]["status"] == "РАСХОЖДЕНИЕ"                # один расчёт потерян, пометки «!» нет
    dash["168"]["incomplete"] = True
    assert audit.check_windows(ev, dash, TS_MS)["168"]["status"] == "неполно (!)"
    # край «перед несобранным» — только пока расчёт свежий: через 40 мин без него и без «!» — ошибка
    late = TS_MS + 35 * 60_000
    assert audit.check_windows(ev, {"24": _dash(ev, pend, 24)}, late, windows=(24,))["24"]["status"] == "РАСХОЖДЕНИЕ"
    # коллектор пересчитывает окна раз в минуту: край минутой раньше снимка — тоже его край
    ts_edge = HOUR + 30_000                                       # снимок через 30 с после расчёта, окна ещё до него
    d = {"24": _dash(ev, HOUR - 30_000, 24)}
    assert audit.check_windows(ev, d, ts_edge, windows=(24,))["24"]["status"] == "ok"


def test_unmatched_window_is_explained_from_the_dashboard_edge_not_the_snapshot():
    """READYUSDTM 13.09: снимок 01:02:28, расчёт 01:00 ляжет в БД досбором 01:10 — край дашборда перед ним, а в БД нет и
    настоящей потери 00:00. Причина окна — только потерянный расчёт; несобранный по плану в «нет в БД» не идёт."""
    ev = _hourly()                                                  # последний расчёт HOUR + 43 мс, снимок через 5 мин
    lost = HOUR - H + 43
    snap = [(ms, r) for ms, r in ev if ms not in (lost, HOUR + 43)]  # в БД на момент снимка: без 00:00 и без 01:00
    dash = {w: _dash(snap, HOUR - 1, int(w)) for w in ("24", "72", "168")}
    cands = audit.window_candidates(ev, TS_MS)
    res = audit.check_windows(ev, dash, TS_MS, cands=cands, fallback=audit.PENDING)
    for w, x in res.items():
        assert x["status"] == "РАСХОЖДЕНИЕ" and x["anchor"] is None and x["anchor_ms"] == HOUR - 1
        assert (x["n_truth"], x["n_ours"]) == (int(w), int(w) - 1)
        assert audit.window_cause(ev, snap, x["ours"], x["n_ours"], x["anchor_ms"], int(w)) == \
            f"нет в БД расчёта биржи {audit._t(HOUR - H)}"
    # от снимка (как было) расчёт 01:00, которого по плану ещё нет, тоже шёл в «нет в БД»
    old = audit.check_windows(ev, dash, TS_MS)["24"]
    assert old["anchor_ms"] == TS_MS and audit.window_cause(ev, snap, old["ours"], old["n_ours"], old["anchor_ms"], 24) \
        .startswith(f"нет в БД расчёта биржи {audit._t(HOUR - H)}, {audit._t(HOUR)}")
    # край без такого кандидата (расчёт уже старше CATCHUP_S) — как раньше, от снимка
    late = TS_MS + 40 * 60_000
    assert audit.check_windows(ev, dash, late, fallback=audit.PENDING)["24"]["anchor_ms"] == late


def test_no_history_venue_windows_accumulate_honestly():
    db_ev = [(HOUR - k * H, 0.00001) for k in range(10)][::-1]       # коллектор накопил 10 часов из 24
    ok = {"24": dict(sum=0.0001, n=10, incomplete=True)}
    r = audit.check_no_history(db_ev, ok, TS_MS, 1, None, windows=(24,))
    assert r["24"]["status"] == "ok" and r["24"]["expected"] == 24
    bad = {"24": dict(sum=0.0001, n=10, incomplete=False)}             # 10 из 24 — а окно выдано за полное
    assert audit.check_no_history(db_ev, bad, TS_MS, 1, None, windows=(24,))["24"]["status"] == "НЕ ПОМЕЧЕНО"
    wrong = {"24": dict(sum=0.0005, n=10, incomplete=False)}
    assert audit.check_no_history(db_ev, wrong, TS_MS, 1, None, windows=(24,))["24"]["status"] == "РАСХОЖДЕНИЕ с БД"


def test_pick_sample_top_bottom_random():
    xs = [(f"S{i}", i * 1e-6) for i in range(30)] + [("NONE", None)]
    p = audit.pick_sample(xs, 3, 4, random.Random(1))
    assert p[:3] == ["S29", "S28", "S27"] and p[3:6] == ["S2", "S1", "S0"] and len(p) == 10 and len(set(p)) == 10


# --- прогон целиком: отчёт, коды выхода ---------------------------------------------------------------------------------
def _env_binance(tmp_path, delisted_leg=False, window_ok=True):
    ev = _hourly(200)
    hist = {"AAAUSDT": [(ms, r) for ms, r in ev if (ms - 43) % (8 * H) == 0]}    # 8-часовые расчёты
    a_ev = hist["AAAUSDT"]
    wa = {"24": _dash(a_ev, TS_MS, 24), "72": _dash(a_ev, TS_MS, 72), "168": _dash(a_ev, TS_MS, 168, n_drop=0 if window_ok else 1)}
    rows = [ff("binance", "AAAUSDT", "hyperliquid", "AAA", 1e-5, 1e-5, na=HOUR + 8 * H, wa=wa)]
    markets = {"AAAUSDT": M("AAA"), "CCCUSDT": M("CCC")}
    if delisted_leg:
        rows.append(ff("binance", "OLDUSDT", "hyperliquid", "OLD", 1e-5, 1e-5, base="OLD"))
        markets["OLDUSDT"] = M("OLD", tradable=False, note="PERPETUAL / USDT / статус SETTLING")
    tp, dp = write_env(tmp_path, ff_rows=rows, instruments=[("binance", "AAAUSDT", "AAA"), ("hyperliquid", "AAA", "AAA")],
                       events=[("binance", "AAAUSDT", ms, float(r)) for ms, r in a_ev])
    T = mk_truth("binance", markets, {"AAAUSDT": R(8e-5, 8, HOUR + 8 * H), "OLDUSDT": R(8e-5, 8)}, hist)
    return tp, dp, T


def test_run_ok_report_and_delisted_leg_error(tmp_path):
    tp, dp, T = _env_binance(tmp_path)
    md, errors = audit.run("binance", top=2, sample=2, table_path=tp, db_path=dp, out_dir=tmp_path / "audit",
                           truth=T(), recheck_wait_s=0)
    text = md.read_text()
    assert errors == 0 and md.name.startswith("binance_") and "**Итог: OK" in text
    rep = json.loads(md.with_suffix(".json").read_text())
    assert rep["presence"]["counts"] == {"present": 1, "ok_alone": 1}
    assert rep["history"]["items"][0]["windows"]["168"]["status"] == "ok"
    sub = tmp_path / "bad"
    sub.mkdir()
    tp, dp, T = _env_binance(sub, delisted_leg=True, window_ok=False)
    md, errors = audit.run("binance", top=2, sample=0, table_path=tp, db_path=dp, out_dir=tmp_path / "audit2",
                           truth=T(), recheck_wait_s=0)
    rep = json.loads(md.with_suffix(".json").read_text())
    assert rep["presence"]["stale"][0]["status"] == "untradable"
    assert rep["error_parts"]["присутствие"] == 1 and rep["error_parts"]["история"] == 1 and errors >= 2
    assert "**Итог: ОШИБОК" in md.read_text()


def test_run_explains_a_lost_settlement_next_to_a_not_yet_collected_one(tmp_path):
    """Прогон целиком (13.09, READYUSDTM): на момент снимка в БД нет ни потерянного расчёта, ни свежего, который ляжет
    досбором hh:10, — край дашборда перед свежим; в причину окна идёт только потерянный, число расчётов биржи — без свежего."""
    ev = _hourly(200)
    lost = HOUR - H + 43
    db_ev = [(ms, r) for ms, r in ev if ms not in (lost, HOUR + 43)]
    wa = {w: _dash(db_ev, HOUR - 1, int(w)) for w in ("24", "72", "168")}
    rows = [ff("binance", "AAAUSDT", "hyperliquid", "AAA", 1e-5, 1e-5, iva=1, na=HOUR + H, wa=wa)]
    tp, dp = write_env(tmp_path, ff_rows=rows, instruments=[("binance", "AAAUSDT", "AAA"), ("hyperliquid", "AAA", "AAA")],
                       events=[("binance", "AAAUSDT", ms, float(r)) for ms, r in db_ev])
    T = mk_truth("binance", {"AAAUSDT": M("AAA", iv=1)}, {"AAAUSDT": R(1e-5, 1, HOUR + H)}, {"AAAUSDT": ev})
    md, errors = audit.run("binance", top=2, sample=0, table_path=tp, db_path=dp, out_dir=tmp_path / "audit",
                           truth=T(), recheck_wait_s=0)
    item = next(x for x in json.loads(md.with_suffix(".json").read_text())["history"]["items"] if x["symbol"] == "AAAUSDT")
    for w in ("24", "72", "168"):
        x = item["windows"][w]
        assert x["status"] == "РАСХОЖДЕНИЕ" and (x["n_truth"], x["n_ours"]) == (int(w), int(w) - 1)
        assert x["cause"] == f"нет в БД расчёта биржи {audit._t(HOUR - H)}"
    assert errors >= 1


def test_run_no_history_venue_and_fatal(tmp_path):
    db_ev = [("variational", "BTC", HOUR - k * H, 0.00001) for k in range(10)]
    rows = [ff("binance", "BTCUSDT", "variational", "BTC", 1e-5, 1e-5, ivb=1, base="BTC",
               wb={"24": dict(sum=0.0001, n=10, incomplete=True), "72": dict(sum=0.0001, n=10, incomplete=True),
                   "168": dict(sum=0.0001, n=10, incomplete=True)})]
    tp, dp = write_env(tmp_path, ff_rows=rows, events=db_ev)
    T = mk_truth("variational", {"BTC": M("BTC", iv=1)}, {"BTC": R(1e-5, 1)}, None, note="у Variational публичной истории нет")
    md, errors = audit.run("variational", top=1, sample=0, table_path=tp, db_path=dp, out_dir=tmp_path, truth=T(), recheck_wait_s=0)
    assert errors == 0 and "истории у биржи нет" in md.read_text()
    # биржа не ответила — это не OK
    class Down:
        venue = "binance"; http = None
        def markets(self): raise RuntimeError("HTTP 503")
        def rates(self): return {}
        def history(self, *a): return []
    md, errors = audit.run("binance", table_path=tp, db_path=dp, out_dir=tmp_path, truth=Down(), recheck_wait_s=0)
    assert errors == 1 and "НЕ ПРОВЕРЕНО" in md.read_text()


def test_cli_exit_codes_venue_and_all(tmp_path, monkeypatch):
    tp, dp, T = _env_binance(tmp_path)
    monkeypatch.setattr(config, "TABLE_PATH", tp); monkeypatch.setattr(config, "DB_PATH", dp)
    monkeypatch.setattr(config, "RUNTIME", tmp_path)
    monkeypatch.setattr(audit, "TRUTHS", {"binance": T})
    assert cli.main(["audit", "--venue", "binance", "--top", "1", "--sample", "0"]) == 0
    sub = tmp_path / "x"
    sub.mkdir()
    tp2, dp2, T2 = _env_binance(sub, delisted_leg=True)
    monkeypatch.setattr(config, "TABLE_PATH", tp2); monkeypatch.setattr(config, "DB_PATH", dp2)
    monkeypatch.setattr(audit, "TRUTHS", {"binance": T2})
    assert cli.main(["audit", "--venue", "binance", "--top", "1", "--sample", "0"]) == 1
    # --all: у остальных площадок истины нет → «НЕ ПРОВЕРЕНО», код 1, сводка ALL_*.md
    monkeypatch.setattr(config, "TABLE_PATH", tp); monkeypatch.setattr(config, "DB_PATH", dp)
    monkeypatch.setattr(audit, "TRUTHS", {"binance": T})
    assert cli.main(["audit", "--all", "--top", "1", "--sample", "0"]) == 1
    alls = list((tmp_path / "audit").glob("ALL_*.md"))
    assert len(alls) == 1 and "НЕ ПРОВЕРЕНО" in alls[0].read_text() and "| binance | OK |" in alls[0].read_text()
    # пустая истина Hyperliquid при ноге hyperliquid:AAA на дашборде — честная ошибка (рынка нет, ставки нет)
    monkeypatch.setattr(audit, "TRUTHS", {v: (T if v == "binance" else mk_truth(v, {}, {}, {})) for v in config.PERP_VENUES})
    assert cli.main(["audit", "--all", "--top", "1", "--sample", "0"]) == 1
    hl = mk_truth("hyperliquid", {"AAA": M("AAA")}, {"AAA": R(8e-5, 8)}, {})
    monkeypatch.setattr(audit, "TRUTHS", {v: {"binance": T, "hyperliquid": hl}.get(v) or mk_truth(v, {}, {}, {})
                                          for v in config.PERP_VENUES})
    assert cli.main(["audit", "--all", "--top", "1", "--sample", "0"]) == 0
    with pytest.raises(SystemExit):
        cli.main(["audit", "--venue", "nosuch"])
