"""Регрессии по проверке исправлений внешнего ревью (11.09, 4 атакующих + скептики): 9 подтверждённых находок.
Каждый тест — сценарий атакующего, проверяет исправленное поведение. Выкат (находки 5-8) — в test_review_regressions."""
import os, time
from funding_bot import calc, config, db, funding
from funding_bot.collector import Collector, settle_mark
from fakes import make_world, perp, FakeHL, H

ABC_FF = "aster:ABCUSDT|binance:ABCUSDT"


def _col(tmp_path, w):
    t = [time.time()]
    col = Collector(clients=w, db_path=tmp_path / "c.db", table_path=tmp_path / "c.json",
                    now=lambda: t[0], sleep=lambda s: None, background=False)
    return col, t


def _by(rows):
    return {r["key"]: r for r in rows}


def _slow(cl, pred, delay):
    orig = cl.get

    def get(path, params=None, *a, **k):
        if pred(path, params or {}):
            time.sleep(delay)
        return orig(path, params, *a, **k)
    cl.get = get
    return orig


# --- находка 2: опоздавший тик больше не делает площадку «занятой» навсегда ---------------------------------------
def test_late_tick_does_not_freeze_venue_steps(tmp_path, monkeypatch):
    """Тик Aster каждый раз чуть дольше дедлайна: раньше её интервалы, досбор и вселенная не шли вовсе — смена
    интервала 1 ч → 4 ч давала часовую ставку вчетверо больше, новый листинг не появлялся."""
    monkeypatch.setattr(config, "TICK_DEADLINE_S", 0.2)
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    a = w["aster"]
    _slow(a, lambda p, q: p == "/fapi/v1/premiumIndex", 0.25)
    a.funding_info["XYZUSDT"] = {"interval_h": 4}
    t[0] += config.INTERVALS_S; col.once(); time.sleep(0.1)
    assert col.instruments["aster"]["XYZUSDT"]["interval_h"] == 4
    a.symbols.append(perp("NEWUSDT")); a.funding_info["NEWUSDT"] = {"interval_h": 8}
    a.premium["NEWUSDT"] = {"rate": 0.001, "mark": 3.0}
    t[0] += config.UNIVERSE_S; col.once(); time.sleep(0.1)
    assert "NEWUSDT" in col.instruments["aster"] and "incremental:aster" not in col.notes


# --- находка 3: зависшая точка вселенной/интервалов/досбора не держит цикл ------------------------------------------
def test_hung_venue_endpoint_does_not_stall_the_loop(tmp_path, monkeypatch):
    """exchangeInfo Aster висит: раньше главный поток ждал 3 × 15 с (51 с), тик и table.json стояли."""
    monkeypatch.setattr(config, "TICK_DEADLINE_S", 0.3)
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    orig = _slow(w["aster"], lambda p, q: p == "/fapi/v1/exchangeInfo", 1.0)
    w["binance"].premium["ABCUSDT"]["rate"] = 0.0009
    t[0] += config.UNIVERSE_S
    t0 = time.time(); tbl = col.once(); dt = time.time() - t0
    assert dt < 0.9 and _by(tbl["ff_rows"])[ABC_FF]["rate_b"] == 0.0009 and tbl["n_ff"] == 13
    assert tbl["pid"] == os.getpid()                                   # проверка выката узнаёт процесс по pid
    time.sleep(0.9); w["aster"].get = orig
    t[0] += config.TICK_S; col.once()
    assert col._vlast[("universe", "aster")] == t[0] and "universe:aster" not in col.notes   # опоздавший ответ принят


# --- находка 1: пауза повтора глубины растёт, даже если добор той же ноги успешен -----------------------------------
def test_deep_backoff_grows_while_same_leg_repair_succeeds(tmp_path, monkeypatch):
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    leg = ("hyperliquid", "ABC")
    col.con.execute("UPDATE leg_depth SET synced_to=NULL WHERE exchange=? AND symbol=?", leg); col.con.commit()
    orig = FakeHL.history_since

    def hs(self, symbol, start_ms, end_ms=None):
        if symbol == "ABC" and start_ms < time.time() * 1000 - 50 * H:
            raise RuntimeError("длинный запрос отклонён")                 # короткий добор работает, пересборка с пола — нет
        return orig(self, symbol, start_ms, end_ms)
    monkeypatch.setattr(FakeHL, "history_since", hs)
    # дыра на час раньше последнего расчёта: у Hyperliquid она добирается всегда, а сам последний расчёт первые
    # 10 минут часа ещё в льготе и добора не требует — тест не должен зависеть от времени суток
    gap = db.last_funding_ms(col.con, *leg) - H
    have = lambda: col.con.execute("SELECT 1 FROM funding_events WHERE exchange=? AND symbol=? AND funding_ms=?",
                                   (*leg, gap)).fetchone() is not None
    for n in (1, 2, 3):
        col.con.execute("DELETE FROM funding_events WHERE exchange=? AND symbol=? AND funding_ms=?", (*leg, gap))
        col.con.commit(); col._events_dirty = True
        col.step_completeness()
        assert have()                                                   # добор прошёл
        assert col._fail[(leg, "deep")][0] == n and col._retry_ok(leg, "repair")   # было: всегда 1, повтор каждые 15 мин
        t[0] = col._fail[(leg, "deep")][1]


# --- находка 9: сутки в льготу после расчёта — не без одного расчёта ------------------------------------------------
def test_dev_and_day_window_right_in_grace_after_settlement():
    day0 = 1_789_000_000_000 // (24 * H) * (24 * H); today = day0 + 30 * 24 * H
    now = today + 8 * H + 5 * 60_000                                   # 08:05 — расчёт 08:00 ещё не лёг в БД
    times = list(range(day0, today + 1, 8 * H))
    ev = {("aster", "X"): [(ms, 0.0001) for ms in times], ("binance", "X"): [(ms, 0.0) for ms in times]}
    legs = list(ev); ivs = {"aster": {"X": 8}, "binance": {"X": 8}}
    nxt = {"aster": {"X": today + 16 * H}, "binance": {"X": today + 16 * H}}
    comp = funding.completeness(ev, legs, ivs, now, 720, nxt, sync={k: today for k in legs})
    assert comp[legs[0]]["pending_ms"] == today + 8 * H and not comp[legs[0]]["latest_missing"]
    ws = {k: calc.window_sums(v, calc.window_anchor(comp[k], now)) for k, v in ev.items()}
    item = dict(key="X", base="X", va="aster", vb="binance", sa="X", sb="X")
    row = calc.build_ff_row(item, {"interval_h": 8}, {"interval_h": 8}, {"rate": 0.0001, "mark": 1.0},
                            {"rate": 0.0, "mark": 1.0}, None, None, ws[legs[0]], ws[legs[1]], now, comp[legs[0]], comp[legs[1]])
    assert row["windows"]["24"]["na"] == 3 and abs(row["windows"]["24"]["spread"] - 0.0003) < 1e-15   # было: 2 из 3
    later = funding.completeness(ev, legs, ivs, today + 7 * H, 720, {k[0]: {"X": today + 8 * H} for k in legs},
                                 sync={k: today for k in legs})
    assert later[legs[0]]["pending_ms"] is None                        # середина интервала — окно до «сейчас»


# --- после hh:10:30 проверка полноты ждёт пакетный досбор -----------------------------------------------------------
def test_settle_check_waits_for_the_batch(tmp_path, monkeypatch):
    """Досбор ушёл в пул: без ожидания проверка полноты в hh:10 опережала пакет и слала посимвольный добор по
    каждой ноге площадки."""
    monkeypatch.setattr(config, "TICK_DEADLINE_S", 0.2)
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    mark = settle_mark(t[0]) + 3600
    orig = _slow(w["aster"], lambda p, q: p == "/fapi/v1/fundingRate" and "symbol" not in q, 0.4)
    t[0] = mark + 1
    before = col.last["completeness"]
    col.once()
    assert col.last["completeness"] == before                          # пакет Aster ещё идёт — проверка ждёт
    time.sleep(0.5); w["aster"].get = orig
    t[0] += config.TICK_S; col.once()
    assert col.last["completeness"] >= mark and col._settled["aster"] == mark
