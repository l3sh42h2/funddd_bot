"""Тестировщик: регрессии fault-injection ревью 13.09 — ошибки дашборда, которые аудитор обязан ловить и раньше пропускал.
Синтетика: 30 часовых рынков extended в паре с hyperliquid, выборка истории — только верх/низ по ставке (X29, X28, X01,
X00); ошибка сажается рынку вне выборки (X15), если не сказано иное. Сети нет."""
import ast
import json
import os
import random
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from funding_bot import audit, config, db
from funding_bot.audit_truth.base import window_sum

H = 3600_000
HOUR = 1_789_254_000_000                   # круглый час; расчёты extended — через 0.9 с после часа
TS = HOUR + 300_000                        # снимок дашборда: 5 мин после расчёта
SYMS = [f"X{i:02d}-USD" for i in range(30)]
MID = "X15-USD"
SRC = Path(audit.__file__).resolve().parents[1]


def R(rate, iv=1, nxt=HOUR + H):
    return dict(rate=Decimal(repr(rate)), interval_h=iv, next_ms=nxt, kind="predicted")


def M(base, iv=1, tradable=True, cls="crypto"):
    return dict(base=base, tradable=tradable, cls=cls, interval_h=iv, name=None, note=None)


class Fake:
    def __init__(self, venue, markets, rates, hist, note=None):
        self.venue, self._m, self._r, self._h, self.history_note = venue, markets, rates, hist, note
        self.quote_twins, self.site_hours, self.http = True, None, None
        self.calls = []

    def markets(self):
        return self._m

    def rates(self):
        return self._r

    def history(self, s, a, b):
        self.calls.append(s)
        return [(ms, r) for ms, r in self._h.get(s, []) if a <= ms <= b]


def hist_of(i, n=200, jitter=900):
    return [(HOUR - k * H + jitter, Decimal("0.00001") * ((k + i) % 5 + 1)) for k in range(n)][::-1]


def wins(ev, anchor):
    return {w: window_sum(ev, anchor, w) for w in config.WINDOWS_H}


def row(sym, ws, rh, vb="hyperliquid", sb=None, flip=False):
    base = sym.split("-")[0]
    w = {str(k): dict(a=ws[k][0], na=ws[k][1], b=None, nb=0, spread=ws[k][0], incomplete=False) for k in config.WINDOWS_H}
    return dict(key=f"extended:{sym}|{vb}:{sb or base}", base=base, cls="crypto", va="extended", vb=vb, sa=sym, sb=sb or base,
                rate_a=rh, rate_b=None, rate_h_a=-rh if flip else rh, rate_h_b=None, iv_a=1, iv_b=1, next_a=HOUR + H,
                next_b=None, windows=w, stale=False, mismatch=False)


def scenario(tmp_path, ts=TS, drop=None, dash=None, db_extra=(), pending=False, rows_extra=(), note=None, sample=0):
    """drop {рынок: ms} — расчёт потерян и в БД, и в окне (честно из БД), «!» нет; dash {рынок: (убрать ms | None, [(ms,
    ставка)] добавить)} — подлог только окна дашборда; pending — снимок в досбор: расчёта HOUR в БД ещё нет, окна до него."""
    hist = {s: hist_of(i) for i, s in enumerate(SYMS)}
    rates = {s: R(1e-5 * (i + 1)) for i, s in enumerate(SYMS)}
    markets = {s: M(s.split("-")[0]) for s in SYMS}
    rows, events = [], []
    anchor = HOUR - 1 if pending else ts
    for i, s in enumerate(SYMS):
        ev = [(ms, r) for ms, r in hist[s] if ms <= ts and not (pending and ms // 60_000 == HOUR // 60_000)]
        if drop and s in drop:
            ev = [x for x in ev if x[0] != drop[s]]
        events += [("extended", s, ms, float(r), 1, ms // 1000 + 30) for ms, r in ev]
        rm, add = (dash or {}).get(s, (None, []))
        ws = wins([x for x in ev if x[0] != rm], anchor)
        if add:                                            # подлог: расчёт вписан в окна мимо края (он позже якоря)
            extra = sum(float(r) for _ms, r in add)
            ws = {w: ((s_ or 0.0) + extra, n + len(add)) for w, (s_, n) in ws.items()}
        rows.append(row(s, ws, 1e-5 * (i + 1)))
    rows += list(rows_extra)
    table = dict(ts=ts // 1000, tick_ts=ts // 1000, venues=list(config.PERP_VENUES), ff_rows=rows, sf_rows=[])
    tp = tmp_path / "table.json"
    tp.write_text(json.dumps(table))
    dp = tmp_path / "f.db"
    con = db.connect(dp)
    for s in SYMS:
        b = s.split("-")[0]
        con.execute("INSERT INTO instruments(exchange,symbol,base,last_seen) VALUES(?,?,?,?)", ("extended", s, b, ts // 1000))
        con.execute("INSERT INTO instruments(exchange,symbol,base,last_seen) VALUES(?,?,?,?)", ("hyperliquid", b, b, ts // 1000))
    con.executemany("INSERT INTO funding_events(exchange,symbol,funding_ms,rate,interval_h,seen_ts) VALUES(?,?,?,?,?,?)",
                    events + list(db_extra))
    con.commit(); con.close()
    T = Fake("extended", markets, rates, hist, note)
    rep = audit.audit_venue("extended", top=2, sample=sample, days=7, table_path=tp, db_path=dp, truth=T, recheck_wait_s=0,
                            rng=random.Random(1))
    return rep, T


def named(rep, sym):
    """Где отчёт называет рынок ошибкой (часть → причины)."""
    out = []
    h, rr = rep["history"], rep["rates"]
    for x in h["items"]:
        if x["symbol"] == sym:
            out += [f"окно {w}: {y['status']} {y.get('cause') or ''}" for w, y in (x.get("windows") or {}).items()
                    if y["status"] in ("РАСХОЖДЕНИЕ", "РАСХОЖДЕНИЕ с БД", "НЕ ПОМЕЧЕНО")]
    out += [x["why"] for x in h.get("db_errors", []) + h.get("conflicts", []) + rr.get("conflicts", []) if x["symbol"] == sym]
    out += [", ".join(x["problems"]) for x in rr["issues"] if x["symbol"] == sym]
    return out


def test_clean_synthetic_is_ok_and_screens_every_leg(tmp_path):
    rep, T = scenario(tmp_path)
    assert rep["errors"] == 0, rep["verdict"]
    assert rep["history"]["screened"] == 30 and rep["history"]["suspects"] == {} and rep["history"]["escalated"] == []
    assert sorted(T.calls) == sorted(["X29-USD", "X28-USD", "X01-USD", "X00-USD"])     # только выборка — лишних запросов нет


def test_lost_settlement_outside_the_sample_is_escalated_and_named(tmp_path):
    """Расчёт потерян в БД (окно честно из БД, «!» нет) у рынка вне выборки: раньше не проверялся вовсе."""
    lost = HOUR - 10 * H + 900
    rep, T = scenario(tmp_path, drop={MID: lost})
    assert MID in rep["history"]["escalated"] and "дыра в БД" in rep["history"]["suspects"][MID][0]
    why = named(rep, MID)
    assert any("окно 24: РАСХОЖДЕНИЕ" in x and "нет в БД расчёта биржи" in x and audit._t(lost) in x for x in why), why
    assert rep["error_parts"]["история"] >= 1 and "**Итог: ОШИБОК" in audit.render_md(rep, 2)
    assert "Почему окна не сошлись с биржей" in audit.render_md(rep, 2)


def test_window_not_computed_from_db_is_escalated(tmp_path):
    """БД полная, а окно дашборда без одного расчёта (ошибка сборки окна): расхождение с БД → сверка с биржей."""
    rep, _ = scenario(tmp_path, dash={MID: (HOUR - 5 * H + 900, [])})
    assert "не равно сумме расчётов в БД" in rep["history"]["suspects"][MID][0]
    assert any("не равно сумме БД" in x for x in named(rep, MID))


def test_window_with_an_upcoming_settlement_is_caught(tmp_path):
    """Окно включает ещё не наступивший расчёт (следующий час) по прогнозу: больше расчётов, чем у биржи."""
    rep, _ = scenario(tmp_path, dash={MID: (None, [(HOUR + H - 1, Decimal("0.00016"))])})
    why = named(rep, MID)
    assert any("РАСХОЖДЕНИЕ" in x and "расчёт, которого ещё нет" in x for x in why), why


def test_forecast_written_as_settlement_is_an_error(tmp_path):
    rep, _ = scenario(tmp_path, db_extra=[("extended", MID, HOUR + H, 1.6e-4, 1, TS // 1000 - 60)])
    assert [x["symbol"] for x in rep["history"]["db_errors"]] == [MID]
    assert "записан прогноз" in rep["history"]["db_errors"][0]["why"] and rep["error_parts"]["история"] >= 1


def test_sign_flipped_in_one_row_of_the_leg_is_caught(tmp_path):
    """Нога в двух строках, знак перевёрнут только во второй (другая пара): раньше смотрелась лишь первая строка."""
    ws = wins([(ms, r) for ms, r in hist_of(15) if ms <= TS], TS)
    rep, _ = scenario(tmp_path, rows_extra=[row(MID, ws, 1.6e-4, vb="pacifica", sb="X15", flip=True)])
    assert [x["symbol"] for x in rep["rates"]["conflicts"]] == [MID]
    assert "ставка в час" in rep["rates"]["conflicts"][0]["why"] and rep["error_parts"]["ставки"] == 1


def test_window_differs_between_rows_of_one_leg(tmp_path):
    ev = [(ms, r) for ms, r in hist_of(15) if ms <= TS]
    bad = wins(ev[:-3], TS)
    rep, _ = scenario(tmp_path, rows_extra=[row(MID, bad, 1.6e-4, vb="pacifica", sb="X15")])
    assert [x["symbol"] for x in rep["history"]["conflicts"]] == [MID] and "окно" in rep["history"]["conflicts"][0]["why"]


def test_partner_incomplete_flag_does_not_excuse_the_leg():
    """«!» строки пары ставится и за чужую ногу: у ноги «!» — только если он у всех её строк."""
    w_inc = {"24": dict(a=1e-4, na=24, b=None, nb=0, spread=None, incomplete=True)}
    w_ok = {"24": dict(spread=1e-4, n=24, incomplete=False)}
    t = dict(ff_rows=[dict(key="k1", va="extended", vb="edgex", sa="A-USD", sb="A", rate_h_a=1e-5, rate_h_b=1e-5, iv_a=1,
                           iv_b=1, next_a=None, next_b=None, windows=w_inc)],
             sf_rows=[dict(key="k2", perp_ex="extended", perp="A-USD", rate_h=1e-5, period=1, next_ms=None, windows=w_ok,
                           spot_ex="gate_spot", spot="A_USDT")])
    L = audit.dashboard_legs(t)[("extended", "A-USD")]
    assert L["windows"]["24"]["incomplete"] is False and L["conflicts"] == []
    assert audit.dashboard_legs(dict(ff_rows=t["ff_rows"]))[("extended", "A-USD")]["windows"]["24"]["incomplete"] is True


def test_snapshot_right_after_settlement_no_longer_hides_errors(tmp_path):
    """Снимок через 90 с после часового расчёта: раньше любая ошибка окна часовой площадки проходила «границей»."""
    ts = HOUR + 90_000
    rep, _ = scenario(tmp_path, ts=ts, drop={"X29-USD": HOUR - 6 * H + 900})
    assert rep["history"]["items"][0]["symbol"] == "X29-USD"
    assert rep["history"]["items"][0]["windows"]["24"]["status"] == "РАСХОЖДЕНИЕ"
    sub = tmp_path / "clean"
    sub.mkdir()
    clean, _ = scenario(sub, ts=ts)
    assert clean["errors"] == 0, clean["verdict"]


def test_edge_is_still_forgiven_when_it_explains_the_sum():
    """Честная граница: БД хранит расчёт на минуте, биржа — через 0.9 с; снимок ровно на часе."""
    truth = [(HOUR - k * H + 900, Decimal("0.00001") * (k % 5 + 1)) for k in range(60)][::-1]
    dbev = [(ms - 900, r) for ms, r in truth]
    s, n = window_sum(dbev, HOUR, 24)
    res = audit.check_windows(truth, {"24": dict(sum=s, n=n, incomplete=False)}, HOUR, windows=(24,))
    assert res["24"]["status"] == "граница"
    # а сумма, которую граница не объясняет, — расхождение, хоть расчёт у края и есть
    res = audit.check_windows(truth, {"24": dict(sum=s + 3e-5, n=n, incomplete=False)}, HOUR, windows=(24,))
    assert res["24"]["status"] == "РАСХОЖДЕНИЕ"


def test_lost_zero_rate_settlement_is_caught_by_the_count():
    truth = [(HOUR - k * H + 900, Decimal("0") if k == 7 else Decimal("0.00001")) for k in range(60)][::-1]
    s, n = window_sum(truth, TS, 24)
    res = audit.check_windows(truth, {"24": dict(sum=s, n=n - 1, incomplete=False)}, TS, windows=(24,))
    assert res["24"]["status"] == "РАСХОЖДЕНИЕ"
    assert audit.check_windows(truth, {"24": dict(sum=s, n=n, incomplete=False)}, TS, windows=(24,))["24"]["status"] == "ok"


def test_pending_settlement_counted_by_forecast_is_caught(tmp_path):
    """Досбор: расчёта HOUR в БД ещё нет, окна обязаны кончаться перед ним. Окно, куда он вошёл по прогнозной ставке, —
    ошибка (раньше — «граница»: расчёт у самого края снимка)."""
    ts = HOUR + 120_000
    clean, _ = scenario(tmp_path, ts=ts, pending=True)
    assert clean["errors"] == 0, clean["verdict"]
    sub = tmp_path / "bad"
    sub.mkdir()
    rep, _ = scenario(sub, ts=ts, pending=True, dash={"X29-USD": (None, [(HOUR + 900, Decimal("0.0003"))])})
    why = named(rep, "X29-USD")
    assert any("РАСХОЖДЕНИЕ" in x and "не равно сумме БД" in x for x in why), why


def test_escalation_is_capped_and_the_rest_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_ESCALATE", 2)
    # у каждой ноги своя дыра (общая дыра у многих ног сверяется представителями — см. тест ниже)
    rep, T = scenario(tmp_path, drop={s: HOUR - (10 + k) * H + 900
                                      for k, s in enumerate(("X10-USD", "X11-USD", "X12-USD", "X13-USD"))})
    assert rep["history"]["escalated"] == ["X10-USD", "X11-USD"] and rep["history"]["unchecked"] == ["X12-USD", "X13-USD"]
    assert any(v.startswith("НЕ СВЕРЕНО с биржей: ещё 2") for v in rep["verdict"])
    assert len(T.calls) == 6


def test_no_history_venue_screens_every_leg_against_the_db(tmp_path):
    """Площадка без истории: окно рынка вне выборки не равно БД — ошибка (раньше проверялась лишь выборка)."""
    rep, T = scenario(tmp_path, dash={MID: (HOUR - 5 * H + 900, [])}, note="истории нет")
    T.history = lambda s, a, b: None                               # биржа истории не публикует
    rep = audit.audit_venue("extended", top=2, sample=0, days=7, table_path=tmp_path / "table.json",
                            db_path=tmp_path / "f.db", truth=T, recheck_wait_s=0, rng=random.Random(1))
    assert MID in rep["history"]["escalated"]
    assert any("РАСХОЖДЕНИЕ с БД" in x for x in named(rep, MID)), named(rep, MID)
    assert all(x["symbol"] == MID for x in rep["history"]["items"] if x.get("escalated"))


def test_hole_common_to_the_venue_is_checked_on_representatives(tmp_path, monkeypatch):
    """Pacifica 09.09 00:00: биржа не рассчитала час у всех рынков разом. Дыра у всех ног — одна причина: сверяются
    представители (выборка), остальные ноги не гонятся на сверку поштучно (у Pacifica это 8 с на рынок)."""
    lost = HOUR - 10 * H + 900
    orig = hist_of
    monkeypatch.setitem(globals(), "hist_of", lambda i, **kw: [x for x in orig(i, **kw) if x[0] != lost])
    rep, T = scenario(tmp_path, drop={s: lost for s in SYMS})
    assert rep["errors"] == 0, rep["verdict"]
    assert len(T.calls) == 4 and rep["history"]["escalated"] == []            # только выборка
    ch = rep["history"]["common_holes"]
    assert len(ch) == 1 and ch[0]["n"] == 30 and ch[0]["confirmed"] is True
    assert any("у биржи тоже нет расчёта" in v for v in rep["verdict"])
    # коллектор стоял час, а у биржи расчёт есть: ошибки у представителей и строка про остальные ноги
    monkeypatch.setitem(globals(), "hist_of", orig)
    sub = tmp_path / "outage"
    sub.mkdir()
    rep, T = scenario(sub, drop={s: lost for s in SYMS})
    ch = rep["history"]["common_holes"]
    assert ch[0]["confirmed"] is False and rep["error_parts"]["история"] >= 4
    assert any(v.startswith("ОКНА: та же дыра") and "ещё у 26 ног" in v for v in rep["verdict"]), rep["verdict"]


def test_same_ticker_of_another_asset_is_not_a_partner(tmp_path):
    """В БД коллектора класса нет: монета QNT (gate) и акция Quantinuum (extended QNT-USD, hyperliquid xyz:QNT) выглядели
    парой — «нет на дашборде» на чистой таблице. Класс пары берётся у её биржи; без ответа биржи — как было."""
    markets = {"QNT_USDT": M("QNT", iv=8), "MON_USDT": M("MON", iv=8), "ZZZ_USDT": M("ZZZ", iv=8)}
    uni = {("extended", "QNT-USD"): dict(base="QNT", cls=None), ("hyperliquid", "xyz:QNT"): dict(base="QNT", cls=None),
           ("extended", "MON-USD"): dict(base="MON", cls=None), ("pacifica", "ZZZ"): dict(base="ZZZ", cls=None)}
    other = {"extended": {"QNT-USD": M("QNT", cls="equity"), "MON-USD": M("MON")},
             "hyperliquid": {"xyz:QNT": M("QNT", cls="equity")}, "pacifica": None}
    rows, _ = audit.presence("gate", markets, {}, uni)
    assert {r["symbol"]: r["status"] for r in rows} == {"QNT_USDT": "missing", "MON_USDT": "missing", "ZZZ_USDT": "missing"}
    audit.confirm_partners(rows, other.get)
    by = {r["symbol"]: r for r in rows}
    assert by["QNT_USDT"]["status"] == "ok_other" and "другой актив" in by["QNT_USDT"]["why"]
    assert by["MON_USDT"]["status"] == "missing" and "extended:MON-USD" in by["MON_USDT"]["why"]   # настоящая пара
    assert by["ZZZ_USDT"]["status"] == "missing"                    # биржа пары не ответила — ошибка остаётся
    # делистинг у пары — не пара
    rows, _ = audit.presence("gate", {"OLD_USDT": M("OLD", iv=8)}, {}, {("extended", "OLD-USD"): dict(base="OLD", cls=None)})
    audit.confirm_partners(rows, {"extended": {"OLD-USD": M("OLD", tradable=False)}}.get)
    assert rows[0]["status"] == "ok_other" and "не торгуется" in rows[0]["why"]


def test_owner_unpaired_market_is_nobodys_partner():
    """Extended XIAOMI-USD исключён владельцем из пар (цена в HKD): Gate XIAOMI_USDT без других пар — «не с чем паровать»."""
    uni = {("extended", "XIAOMI-USD"): dict(base="XIAOMI", cls=None)}
    rows, _ = audit.presence("gate", {"XIAOMI_USDT": M("XIAOMI", iv=8, cls="equity")}, {}, uni)
    assert rows[0]["status"] == "ok_alone"
    uni[("bitget", "XIAOMIUSDT")] = dict(base="XIAOMI", cls=None)
    rows, _ = audit.presence("gate", {"XIAOMI_USDT": M("XIAOMI", iv=8, cls="equity")}, {}, uni)
    assert rows[0]["status"] == "check" and "bitget:XIAOMIUSDT" in rows[0]["why"] and "extended" not in rows[0]["why"]


def test_partner_markets_cache_fetches_each_venue_once():
    n = []

    class T:
        def __init__(self):
            n.append(1)

        def markets(self):
            return {"A": M("A")}
    pm = audit.PartnerMarkets({"extended": T})
    pm.put("gate", {"G": M("G")})
    assert pm("extended") == {"A": M("A")} and pm("extended") is pm("extended") and len(n) == 1
    assert pm("gate") == {"G": M("G")}


def test_partner_markets_waits_for_a_venue_audited_by_the_same_run():
    """--all: список площадки, которую сверяет этот же прогон, берётся у её прогона — второй сессии к бирже (и второго
    темпа мимо её лимита) нет; упавший прогон площадки отпускает ждущих."""
    import threading
    made = []

    class T:
        def __init__(self):
            made.append(1)

        def markets(self):
            return {"X": M("X")}
    pm = audit.PartnerMarkets({"lighter": T, "gate": T}, expect=["lighter", "gate"], wait_s=5)
    got = {}
    th = threading.Thread(target=lambda: got.setdefault("v", pm("lighter")))
    th.start()
    pm.put("lighter", {"L": M("L")})
    th.join(2)
    assert got["v"] == {"L": M("L")} and made == []
    pm.done("gate")                                          # прогон gate упал до списка рынков
    assert pm("gate") is None and made == []


# --- ставки: дашборд отстаёт от живой биржи на чтение, ошибки коллектора сажаются поверх ------------------------------------
# Истина движется между чтениями, как в первые минуты часа (13.09: Backpack 16 % рынков за 30 с вне допуска, HL 14.5 % за
# 60 с): треть ног — на 40 % за чтение, X03 прыгает между базой +1.25e-5 и отрицательной оценкой (ApeX LDOUSDT 02:01),
# остальные — на 1 %. Коллектор к следующему тику показывает то, что биржа отдала в прошлом чтении (13.09: у Gate 11 из 11
# и у Lighter 7 из 7 расхождений дашборд на перепроверке равен первому чтению истины).
FAST = {s for i, s in enumerate(SYMS) if i % 3 == 0 and s != "X03-USD"}
SLOW_BAD = "X14-USD"                               # медленная нога: множитель узнаётся на каждом снимке
FAST_BAD = "X15-USD"


def live_rate(s, k):
    """Ставка биржи в час у ноги s на чтении k (k = −1 — что коллектор снял до первого чтения)."""
    i = SYMS.index(s)
    if s == "X03-USD":
        return 1.25e-5 if k % 2 else -1.5e-5
    return 1e-5 * (i + 1) * (1.4 if s in FAST else 1.01) ** k


class LiveTruth(Fake):
    """rates() чтения k — ставки чтения k; тут же коллектор пишет тик со ставками этого чтения (дашборд отстаёт на
    чтение), obs тика — момент чтения."""

    def __init__(self, write, iv, **kw):
        super().__init__(**kw)
        self.k, self.write, self.iv = 0, write, iv

    def rates(self):
        t, k = time.time(), self.k
        self.k += 1
        self.write(k, t)
        return {s: R(live_rate(s, k) * self.iv, iv=self.iv) for s in SYMS}


def ok_leg(s, rh, iv):
    return rh, iv, HOUR + H


def rate_scenario(tmp_path, monkeypatch, fault=ok_leg, iv=1, wait=5.0, ticks=True):
    """fault(рынок, ставка в час, интервал) → (ставка дашборда в час, интервал, след. расчёт) — ошибка коллектора на
    КАЖДОМ тике; ticks=False — коллектор стоит (новых тиков нет). История та же, что в scenario(): чистая."""
    monkeypatch.setattr(audit, "RECHECK_POLL_S", 0.01)
    hist = {s: hist_of(i) for i, s in enumerate(SYMS)}
    ev = {s: [(ms, r) for ms, r in hist[s] if ms <= TS] for s in SYMS}
    tp = tmp_path / "table.json"

    def rows_at(k):
        out = []
        for s in SYMS:
            rh, iv_, nxt = fault(s, live_rate(s, k - 1), iv)
            r = row(s, wins(ev[s], TS), rh)
            r.update(rate_a=None if rh is None else rh * iv_, iv_a=iv_, next_a=nxt)
            out.append(r)
        return out

    def write(k, t_read):
        """Тик после чтения k: ставки чтения k; ts растёт с тиком, src_age — от ts до чтения (obs = момент чтения)."""
        if not ticks:
            return
        ts = int(time.time()) + 2 + k
        tp.write_text(json.dumps(dict(ts=ts, tick_ts=ts, venues=list(config.PERP_VENUES), ff_rows=rows_at(k + 1),
                                      sf_rows=[], src_age={"extended": {"prem": round(ts - t_read, 1)}})))

    # снимок 0 — ставки, снятые коллектором за 5 с до сверки; ts синтетический (якорь окон), src_age — от него
    tp.write_text(json.dumps(dict(ts=TS // 1000, tick_ts=TS // 1000, venues=list(config.PERP_VENUES), ff_rows=rows_at(0),
                                  sf_rows=[], src_age={"extended": {"prem": round(TS / 1000 - (time.time() - 5), 1)}})))
    dp = tmp_path / "f.db"
    con = db.connect(dp)
    for s in SYMS:
        b = s.split("-")[0]
        con.execute("INSERT INTO instruments(exchange,symbol,base,last_seen) VALUES(?,?,?,?)", ("extended", s, b, TS // 1000))
        con.execute("INSERT INTO instruments(exchange,symbol,base,last_seen) VALUES(?,?,?,?)", ("hyperliquid", b, b, TS // 1000))
    con.executemany("INSERT INTO funding_events(exchange,symbol,funding_ms,rate,interval_h,seen_ts) VALUES(?,?,?,?,?,?)",
                    [("extended", s, ms, float(r), 1, ms // 1000 + 30) for s in SYMS for ms, r in ev[s]])
    con.commit(); con.close()
    T = LiveTruth(write, iv, venue="extended", markets={s: M(s.split("-")[0], iv=iv) for s in SYMS}, rates=None, hist=hist)
    return audit.audit_venue("extended", top=2, sample=0, days=7, table_path=tp, db_path=dp, truth=T, recheck_wait_s=wait,
                             rng=random.Random(1))


def test_lagged_live_table_is_clean_and_the_lag_is_a_separate_line(tmp_path, monkeypatch):
    rep = rate_scenario(tmp_path, monkeypatch)
    rr = rep["rates"]
    assert rep["errors"] == 0, rep["verdict"]
    assert rr["issues"] == [] and rr["transient"] >= len(FAST) + 1 and rr["lagged"] >= len(FAST) + 1
    assert rr["rechecks"] >= 1 and not any("нет тика" in w for w in rep["warnings"])
    assert 4 <= rr["lags"][0]["lag_s"] <= 9                         # коллектор снял снимок 0 за 5 с до чтения
    line = next(v for v in rep["verdict"] if v.startswith("дашборд отстаёт на"))
    assert "не ошибка" in line and "на перепроверках" in line
    md = audit.render_md(rep, 2)
    assert "**Итог: OK" in md and "Дашборд отстаёт на" in md


def test_lagged_live_table_was_a_false_error_without_the_live_band(tmp_path, monkeypatch):
    """Прежняя перепроверка — только против последнего чтения: чистая таблица, отстающая на тик, давала ошибки (01:02 13.09:
    66 на 10 площадках, из них ставок — 60 на 7)."""
    monkeypatch.setattr(audit, "live_band", lambda xs: None)
    rep = rate_scenario(tmp_path, monkeypatch)
    assert {x["symbol"] for x in rep["rates"]["issues"]} == FAST | {"X03-USD"}


def _one(sym, k):
    return lambda s, rh, iv: ((k(rh) if s == sym else rh), iv, HOUR + H)


def test_unit_faults_on_one_leg_are_caught_through_the_lag(tmp_path, monkeypatch):
    """×8, ×24, знак — у медленной ноги называются видом из всех снимков; у ноги, где биржа за чтение уходит на 40 %, —
    остаются ошибкой (вид может быть «значение»: множитель к прошлому чтению)."""
    cases = [(SLOW_BAD, lambda r: 8 * r, "×8"), (SLOW_BAD, lambda r: 24 * r, "×24"), (SLOW_BAD, lambda r: -r, "ЗНАК (×−1)"),
             (FAST_BAD, lambda r: 8 * r, None), (FAST_BAD, lambda r: -r, None), (FAST_BAD, lambda r: 24 * r, None)]
    for n, (sym, k, kind) in enumerate(cases):
        sub = tmp_path / f"c{n}"
        sub.mkdir()
        rep = rate_scenario(sub, monkeypatch, fault=_one(sym, k))
        rr = rep["rates"]
        assert [x["symbol"] for x in rr["issues"]] == [sym], (n, rr["issues"])
        assert rr["issues"][0]["problems"] == ["ставка"] and rep["error_parts"]["ставки"] == 1
        if kind:
            assert rr["issues"][0]["persistent"] == kind, (n, rr["issues"][0])
        assert rr["systematic"] is None


def test_systematic_unit_faults_are_named_through_the_lag(tmp_path, monkeypatch):
    cases = [("×8", 1, lambda s, rh, iv: (8 * rh, iv, HOUR + H)),
             ("ЗНАК (×−1)", 1, lambda s, rh, iv: (-rh, iv, HOUR + H)),
             ("×8", 8, lambda s, rh, iv: (rh * iv, iv, HOUR + H)),           # ставка за интервал 8 ч вместо часа
             ("×1/8", 8, lambda s, rh, iv: (rh / iv, iv, HOUR + H))]         # поделено на интервал дважды
    for n, (kind, iv, f) in enumerate(cases):
        sub = tmp_path / f"s{n}"
        sub.mkdir()
        rr = rate_scenario(sub, monkeypatch, fault=f, iv=iv)["rates"]
        assert rr["systematic"] and rr["systematic"]["kind"] == kind, (n, rr["systematic"], rr["kinds"])
        # все ноги — ошибка; исключение одно — X03 при знаке и ×1/8: биржа сама прыгает +1.25e-5 ↔ −1.5e-5, перевёрнутый знак
        # отстающего дашборда = прошлое чтение биржи (ApeX LDOUSDT), а 1/8 (±1.9e-6, ниже шума) — внутри её же скачка:
        # у одной такой ноги ошибку от отставания не отличить, системный вид назван по остальным
        missed = set(SYMS) - {x["symbol"] for x in rr["issues"]}
        assert missed <= ({"X03-USD"} if kind in ("ЗНАК (×−1)", "×1/8") else set()), (n, missed)
        if iv == 8 and kind == "×8":
            assert any("интервал 8 ч: ставка за интервал вместо часа" in x["kind"] for x in rr["issues"])


def test_interval_and_next_time_are_never_forgiven_by_the_band(tmp_path, monkeypatch):
    for n, (f, prob) in enumerate([(lambda s, rh, iv: (rh, 4 if s == FAST_BAD else iv, HOUR + H), "интервал"),
                                   (lambda s, rh, iv: (rh, iv, HOUR + (3 if s == SLOW_BAD else 1) * H), "время расчёта")]):
        sub = tmp_path / f"i{n}"
        sub.mkdir()
        rr = rate_scenario(sub, monkeypatch, fault=f)["rates"]
        assert len(rr["issues"]) == 1 and rr["issues"][0]["problems"] == [prob], (n, rr["issues"])


def test_no_fresh_tick_is_said_in_the_warnings(tmp_path, monkeypatch):
    """Коллектор стоит: тика после чтения биржи нет — перепроверка идёт по последнему снимку, и это сказано."""
    rep = rate_scenario(tmp_path, monkeypatch, wait=0.1, ticks=False)
    assert rep["rates"]["rechecks"] >= 1
    assert any(w.startswith("перепроверка 1: за 0 с нет тика дашборда") for w in rep["warnings"]), rep["warnings"]


def test_run_all_runs_every_venue_at_once(tmp_path, monkeypatch):
    """Все площадки — в своих потоках разом: иначе PartnerMarkets ждал бы площадку, стоящую в очереди."""
    import threading
    seen, lock = set(), threading.Lock()
    barrier = threading.Barrier(len(config.PERP_VENUES), timeout=10)

    def fake(v, *a, **kw):
        barrier.wait()                                       # упадёт по таймауту, если потоков меньше, чем площадок
        with lock:
            seen.add(v)
        return audit._finish(dict(venue=v, ts=0, fatal="подставной прогон", warnings=[]), 0, None)
    monkeypatch.setattr(audit, "audit_venue", fake)
    path, total = audit.run_all(out_dir=tmp_path)
    assert seen == set(config.PERP_VENUES) and total == len(config.PERP_VENUES) and path.name.startswith("ALL_")


# --- независимость: граф импорта -------------------------------------------------------------------------------------------
COLLECTOR = {"hyperliquid", "kucoin_fut", "bitget_fut", "gate_fut", "lighter", "backpack", "variational", "edgex", "extended",
             "pacifica", "apex", "exchanges", "client", "venues", "collector", "universe", "calc", "symbols", "funding",
             "identity", "identity_src", "dashboard", "spot", "dexleg", "okxdex", "db", "serve", "cabinet", "trade", "tg"}


def test_import_graph_of_the_auditor_holds_no_collector_code():
    """Живой граф импорта в чистом процессе: тестировщик и все истины грузят только config, audit и audit_truth.*."""
    code = ("import sys, funding_bot.audit, funding_bot.audit_truth as t; "
            "print(len(t.TRUTHS), len(t.TRUTH_ERRORS)); print('\\n'.join(sorted(m for m in sys.modules if m.startswith('funding_bot'))))")
    env = dict(os.environ, PYTHONPATH=str(SRC))
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, env=env, check=True,
                         timeout=120).stdout.split()
    n_truths, n_err, mods = int(out[0]), int(out[1]), out[2:]
    assert n_err == 0 and n_truths == len(config.PERP_VENUES)          # все 14 истин загружены — граф полный
    allowed = {"funding_bot", "funding_bot.config", "funding_bot.audit"}
    bad = [m for m in mods if m not in allowed and not m.startswith("funding_bot.audit_truth")]
    assert not bad, f"тестировщик тянет код коллектора: {bad}"
    assert not {m.rsplit('.', 1)[-1] for m in mods if m.startswith("funding_bot.audit_truth.")} & (COLLECTOR - {"base"} - set(config.PERP_VENUES) - {"_fapi"})


def test_auditor_module_imports_only_config_and_truths():
    tree = ast.parse(Path(audit.__file__).read_text())
    rel = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level:
            rel |= {f"{n.module or ''}:{a.name}" for a in n.names}
        elif isinstance(n, ast.Import):
            assert not any(a.name.startswith("funding_bot") for a in n.names)
    mods = {r.split(":")[0] or r.split(":")[1] for r in rel}
    assert mods <= {"config", "audit_truth", "audit_truth.base"}, rel
