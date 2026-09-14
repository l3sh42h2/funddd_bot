"""Регрессии по внешнему ревью 11.09 (hyper/reviews/FUNDING_REVIEW_20260911.md): каждый тест — сценарий
ревьюера, но проверяет ИСПРАВЛЕННОЕ поведение. Номера — пункты ревью."""
import re, sqlite3, subprocess, time
from pathlib import Path
from unittest.mock import patch
import pytest
from funding_bot import calc, config, db, funding
from funding_bot.collector import Collector
from fakes import make_world, FakeHL, FakeExchange, H

ABC_FF = "aster:ABCUSDT|binance:ABCUSDT"


def _col(tmp_path, w, name="c"):
    t = [time.time()]
    col = Collector(clients=w, db_path=tmp_path / f"{name}.db", table_path=tmp_path / f"{name}.json",
                    now=lambda: t[0], sleep=lambda s: None, background=False)
    return col, t


def _by(rows):
    return {r["key"]: r for r in rows}


# --- п.1: восстановление после перерыва ------------------------------------------------------------------
def _outage(tmp_path, cursor: bool):
    now = 1789128900000; last = now // H * H; leg = ("hyperliquid", "ABC")
    full = [(last - k * H, 0.0001) for k in range(48)][::-1]
    cl = FakeHL(); cl.history["ABC"] = full
    c = db.connect(tmp_path / "outage.db")
    db.insert_funding_events(c, [dict(exchange=leg[0], symbol=leg[1], funding_ms=ms, rate=r) for ms, r in full if ms <= last - 3 * H])
    db.set_leg_depth(c, *leg, now - 720 * H, synced_to=(last - 3 * H) if cursor else None)
    iv = {"hyperliquid": {"ABC": 1}}; nxt = {"hyperliquid": {"ABC": last + H}}
    comp = lambda: funding.completeness(db.funding_events_since(c, now - 721 * H), [leg], iv, now, 720, nxt,
                                        depth=db.leg_depths(c), sync=db.leg_sync(c))[leg]
    return c, cl, leg, iv, now, last, comp


@pytest.mark.parametrize("cursor", [False, True], ids=["нога прежней версии", "нога с курсором"])
def test_outage_repair_restores_every_missed_hour(tmp_path, cursor):
    """Ревьюер: после простоя в 3 часа добор тянул только последний час — 22 расчёта из 24 выдавались за полные сутки."""
    c, cl, leg, iv, now, last, comp = _outage(tmp_path, cursor)
    g = comp()
    assert g["latest_missing"]
    funding.repair(c, {"hyperliquid": cl}, {leg: g}, iv)
    ws = calc.window_sums(db.funding_events_since(c, now - 721 * H)[leg], now)
    assert ws[24][1] == 24 and abs(ws[24][0] - 0.0024) < 1e-12
    after = comp()
    assert not after["latest_missing"] and not after["hole"]


def test_cursor_catches_up_the_middle_even_if_latest_arrived(tmp_path):
    """Последний расчёт пришёл (например, пакетом), а середины нет: под курсором нога всё равно отстаёт и догоняется."""
    c, cl, leg, iv, now, last, comp = _outage(tmp_path, cursor=True)
    db.insert_funding_events(c, [dict(exchange=leg[0], symbol=leg[1], funding_ms=last, rate=0.0001)])
    g = comp()
    assert g["latest_missing"] and g["since"] == last - 3 * H + 1
    funding.repair(c, {"hyperliquid": cl}, {leg: g}, iv)
    assert calc.window_sums(db.funding_events_since(c, now - 721 * H)[leg], now)[24][1] == 24


def test_hyperliquid_hourly_gap_is_a_hole_without_cursor():
    now = 1000 * H + 30 * 60_000; leg = ("hyperliquid", "X")
    ev = {leg: [(ms, 0.0001) for ms in range(900 * H, 1001 * H, H) if not (990 * H < ms < 994 * H)]}
    g = funding.completeness(ev, [leg], {"hyperliquid": {"X": 1}}, now, 720, None)[leg]
    assert g["hole"] and g["holes"] == [(990 * H, 994 * H)]
    leg2 = ("aster", "X")                      # у площадки с плавающим интервалом 4 часа без расчётов — ещё не дыра
    assert not funding.completeness({leg2: ev[leg]}, [leg2], {"aster": {"X": 1}}, now, 720, None)[leg2]["hole"]


def test_incremental_batch_advances_cursor_only_where_batch_is_complete(tmp_path):
    con = db.connect(tmp_path / "b.db")
    now = int(time.time() * 1000); top = now // H * H
    ex = FakeExchange("aster")
    ex.history = {"AUSDT": [(top - k * H, 0.0001) for k in range(3)], "ZUSDT": [(top - 2 * H, 0.0002)]}
    start = top - 2 * H + 30 * 60_000
    for s in ("AUSDT", "BUSDT", "CUSDT"):
        db.set_leg_depth(con, "aster", s, now - 30 * 86400_000, synced_to=start)
    funding.incremental(con, ex, {"AUSDT": 1, "BUSDT": 1, "CUSDT": 8})
    sync = db.leg_sync(con)
    assert sync[("aster", "AUSDT")] >= top              # в пакете — продлён как минимум до своего последнего расчёта
    assert sync[("aster", "CUSDT")] > start             # 8-часовому законно нечего было рассчитывать за окно пакета
    assert sync[("aster", "BUSDT")] == start            # часовой, а в пакете его нет — не продлеваем молча


# --- п.2: одна плохая нога не держит остальных ----------------------------------------------------------------
def test_failing_shallow_leg_does_not_starve_fresh_repair(tmp_path):
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    col.con.execute("DELETE FROM funding_events WHERE exchange='aster' AND symbol='AIUSDT'")
    col.con.execute("DELETE FROM leg_depth WHERE exchange='aster' AND symbol='AIUSDT'")
    hl_last = db.last_funding_ms(col.con, "hyperliquid", "ABC")
    col.con.execute("UPDATE leg_depth SET synced_to=? WHERE exchange='hyperliquid' AND symbol='ABC'", (hl_last - H,))
    col.con.execute("DELETE FROM funding_events WHERE exchange='hyperliquid' AND symbol='ABC' AND funding_ms=?", (hl_last,))
    col.con.commit()
    w["aster"].fail_paths.add("/fapi/v1/fundingRate")              # история Aster отвечает ошибкой
    for _ in range(2):
        t[0] += config.FUNDING_INCR_S
        col.step_completeness()
    assert db.last_funding_ms(col.con, "hyperliquid", "ABC") == hl_last   # здоровый Hyperliquid добран
    assert (("aster", "AIUSDT"), "deep") in col._fail                     # падающая нога — в паузе повтора глубины


def test_budget_on_one_venue_skips_only_that_venue(tmp_path):
    con = db.connect(tmp_path / "r.db"); w = make_world()
    w["aster"].fake_weight = 2000                                         # Aster в лимите
    gap = lambda: dict(latest_missing=True, missing=[0], since=None)
    gaps = {("aster", "ABCUSDT"): gap(), ("aster", "XYZUSDT"): gap(), ("hyperliquid", "ABC"): gap()}
    seen = []
    n = funding.repair(con, w, gaps, {"aster": {}, "hyperliquid": {"ABC": 1}}, report=lambda leg, ok: seen.append((leg, ok)))
    assert n > 0 and (("hyperliquid", "ABC"), True) in seen
    assert (("aster", "ABCUSDT"), False) in seen and not any(leg == ("aster", "XYZUSDT") for leg, _ in seen)


def test_failing_leg_backs_off_and_recovers(tmp_path):
    w = make_world(); col, t = _col(tmp_path, w)
    leg = ("aster", "ABCUSDT")
    col._report(leg, False); assert not col._retry_ok(leg)
    t[0] += config.RETRY_BACKOFF_S; assert col._retry_ok(leg)
    col._report(leg, False)
    t[0] += config.RETRY_BACKOFF_S; assert not col._retry_ok(leg)       # второй отказ подряд — пауза вдвое
    t[0] += config.RETRY_BACKOFF_S; assert col._retry_ok(leg)
    col._report(leg, True); assert (leg, "repair") not in col._fail


def test_deep_budget_limits_backfill_but_never_fresh_repair(tmp_path, monkeypatch):
    w = make_world(); col, t = _col(tmp_path, w)
    monkeypatch.setattr(config, "MAINT_DEEP_BUDGET_S", 0)
    col.step_universe(); col.step_tick(); col.refresh_events(force=True)
    col.start_maintenance({("hyperliquid", "ABC"): dict(latest_missing=True, missing=[0], since=None)}, col.legs())
    assert db.last_funding_ms(col.con, "hyperliquid", "ABC") is not None       # свежие расчёты добраны
    assert col.backfill_state["done"] == 0 and not col._maint_running          # на глубину бюджета не было — следующий проход


# --- п.3: свежие ставки не смешиваются со старыми книгами -------------------------------------------------------
def test_stale_book_is_not_used_price_falls_back_to_mark(tmp_path):
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    w["aster"].premium["ABCUSDT"].update(mark=20.0, index=20.0)
    w["aster"].fail_paths.add("/fapi/v1/ticker/bookTicker")
    col.step_tick(); tbl = col.build_table()
    row = _by(tbl["ff_rows"])[ABC_FF]
    assert row["mark_a"] == 20.0 and row["px_a"] == 20.0 and row["px_src_a"] == "mark" and abs(row["gap"] - 1.0) < 1e-9
    assert row["px_src_b"] == "book" and not row["stale"] and "books:aster" in col.notes


def test_stale_rates_mark_row_and_snapshot(tmp_path):
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    for p in col.prem["aster"].values():
        p["obs"] -= config.STALE_S + 5                                      # Aster молчит дольше минуты
    tbl = col.build_table()
    ff = _by(tbl["ff_rows"])
    assert ff[ABC_FF]["stale"] and not ff["binance:BONLYUSDT|hyperliquid:BONLY"]["stale"]
    assert tbl["n_stale_ff"] == sum(1 for r in tbl["ff_rows"] if "aster:" in r["key"]) > 0
    sf = _by(tbl["sf_rows"])
    assert sf["binance_spot:ABCUSDT|aster:ABCUSDT"]["stale"] and sf["gate_spot:ABC_USDT|aster:ABCUSDT"]["stale"]
    assert not sf["binance_spot:ABCUSDT|binance:ABCUSDT"]["stale"]
    col.step_snapshot(tbl)
    assert col.con.execute("SELECT stale FROM ff_snapshots WHERE key=?", (ABC_FF,)).fetchone()[0] == 1


# --- п.4 (гистерезис «не тот актив» по новым ценам) снят: с 12.09 вердикт не по цене, а по составу индекса и
# контрактам (владелец: «не курс») — тесты в test_identity.py -------------------------------------------------


# --- п.5 («отклонение» из неполных суток) снят: колонку убрал владелец 12.09 («откл не нужно») ---------------------


# --- п.6: медленная площадка не держит тик -----------------------------------------------------------------------------
def test_slow_venue_does_not_hold_the_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TICK_DEADLINE_S", 0.3)
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    orig = w["aster"].get

    def slow(path, *a, **k):
        if path == "/fapi/v1/premiumIndex":
            time.sleep(1.0)
        return orig(path, *a, **k)
    w["aster"].get = slow
    w["binance"].premium["ABCUSDT"]["rate"] = 0.0009
    t0 = time.time(); t[0] += config.TICK_S; tbl = col.once(); dt = time.time() - t0
    assert dt < 0.9                                                   # ревьюер: было 51 с на площадку
    assert _by(tbl["ff_rows"])[ABC_FF]["rate_b"] == 0.0009            # Binance обновилась, не дожидаясь Aster
    assert "TimeoutError" in tbl["notes"]["tick:aster"]
    col.step_universe()
    # проверка исправлений 11.09: у задания площадки свой слот — вселенная Aster идёт, пока висит её тик
    assert "universe:aster" not in col.notes and col._vlast[("universe", "aster")] == t[0]
    time.sleep(1.1); w["aster"].get = orig
    t[0] += config.TICK_S; tbl = col.once()
    assert "tick:aster" not in tbl["notes"]                           # опоздавший ответ принят, следующий пришёл вовремя


# --- п.7: фоновое задание не залипает ----------------------------------------------------------------------------------
def test_maintenance_never_stuck_after_db_error(tmp_path):
    w = make_world(); col, t = _col(tmp_path, w)
    gap = {("hyperliquid", "ABC"): dict(latest_missing=True, missing=[0], since=None)}
    with patch("funding_bot.db.connect", side_effect=sqlite3.OperationalError("unable to open database file")):
        col.start_maintenance(gap, [])
    assert not col._maint_running and "OperationalError" in col.notes["maintenance"]
    col.start_maintenance(gap, [])                                    # причина ушла — следующий проход работает
    assert not col._maint_running and "maintenance" not in col.notes
    assert db.last_funding_ms(col.con, "hyperliquid", "ABC") is not None


# --- п.8: выкат --------------------------------------------------------------------------------------------------------------
def test_deploy_tests_before_switch_and_strict_checks():
    root = Path(__file__).resolve().parents[1] / "deploy"
    for f in root.glob("*.sh"):
        assert subprocess.run(["bash", "-n", str(f)]).returncode == 0, f
        assert "set -euo pipefail" in f.read_text(), f
    d = (root / "deploy.sh").read_text()
    assert all(f"{name})" in d for name in ("build", "inspect-base", "install", "status"))
    assert "systemd-run" in d and "TimeoutStartSec=infinity" in d and "expected-base.json" in d
    server = (root / "migration" / "server_job.py").read_text()
    install = server[server.index("class Job:"):server.index("\ndef inspect(paths):")]
    order = ["af.require_base(current_identity(p), expected)", "wait_drain(client", "systemctl', 'stop', 'funding_bot-core.service",
             "af.backup_database(db_path, backup)", "switch_link(p, release)", "wait_core_ready(self.client_factory()", "end_drain"]
    assert [install.index(s) for s in order] == sorted(install.index(s) for s in order)
    assert "TimeoutStopSec=infinity" in (root / "migration/funding_bot-core.service").read_text()
    for name in ("remote_test.sh", "remote_switch.sh", "remote_verify.sh", "remote_rollback.sh"):
        result = subprocess.run(["bash", str(root / name)], capture_output=True, text=True)
        assert result.returncode == 64 and "RETIRED" in result.stderr


# --- добавлено после прогона проб ревьюера на новом коде --------------------------------------------------------------
def test_hyperliquid_missing_hour_repaired_even_under_cursor(tmp_path):
    """Проба ревьюера (п.2) удаляла расчёт позади курсора: курсор «верил» базе. У Hyperliquid расчёт есть каждый час —
    отсутствие прошлого часа или разрыв больше часа добирается и под курсором."""
    w = make_world(); col, t = _col(tmp_path, w); col.once()
    hl_last = db.last_funding_ms(col.con, "hyperliquid", "ABC")
    col.con.execute("DELETE FROM funding_events WHERE exchange='hyperliquid' AND symbol='ABC' AND funding_ms IN (?, ?)",
                    (hl_last, hl_last - 5 * H))
    col.con.commit()
    assert ("hyperliquid", "ABC") in db.leg_sync(col.con)                     # курсор на месте и говорит «всё есть»
    t[0] += config.FUNDING_INCR_S
    col.step_completeness()
    have = {ms for (ms,) in col.con.execute("SELECT funding_ms FROM funding_events WHERE exchange='hyperliquid' AND symbol='ABC'")}
    assert hl_last in have and hl_last - 5 * H in have                        # и последний час, и дыра внутри суток


def test_tick_requests_fail_fast(tmp_path):
    """Тиковые запросы — одна попытка с коротким таймаутом: повтор — это следующий тик (ревью, п.6)."""
    w = make_world(); col, t = _col(tmp_path, w)
    seen = []
    orig = w["aster"].get

    def spy(path, params=None, retries=3, timeout=15, soft=None):
        seen.append((path, retries, timeout))
        return orig(path, params, retries, timeout, soft)
    w["aster"].get = spy
    col.step_universe(); col.step_tick()
    tick = [(r, to) for p, r, to in seen if p in ("/fapi/v1/premiumIndex", "/fapi/v1/ticker/bookTicker")]
    assert tick and all(r == config.TICK_RETRIES and to == config.TICK_HTTP_TIMEOUT for r, to in tick)
    assert any(p == "/fapi/v1/exchangeInfo" and r == 3 for p, r, _ in seen)  # вселенная и история — с повторами, как раньше
