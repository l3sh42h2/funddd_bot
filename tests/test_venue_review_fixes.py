"""Регрессии ревью новых площадок 12.09: интервал фандинга из тика раньше вселенной, KR200 Gate в долларах — своя
база, добор истории по площадкам параллельно с общим бюджетом вызовов."""
from __future__ import annotations
import threading, time
from funding_bot import calc, config, db, funding, gate_fut
from funding_bot.client import BudgetExceeded


def _naive_window_sums(ev, now):
    out = {}
    for w in config.WINDOWS_H:
        vals = [r for ms, r in ev if now - w * calc.H_MS < ms <= now]
        out[w] = (sum(vals), len(vals)) if vals else (None, 0)
    return out


def test_window_sums_bisect_equals_full_scan_bit_for_bit():
    """Бинарный поиск границ окон (тик с 8 площадками) даёт ровно то, что полный перебор, включая края окна:
    расчёт ровно на now − W не входит, ровно на now — входит, будущий — не входит."""
    import random
    rnd = random.Random(3)
    now = 1_789_000_000_000
    edges = [(now - w * calc.H_MS, 1e-4) for w in config.WINDOWS_H] + [(now, 2e-4), (now + 1, 3e-4)]
    for n in (0, 1, 5, 800):
        ev = sorted([(now - rnd.randint(-5, 800) * calc.H_MS + rnd.randint(0, 999), rnd.uniform(-1e-3, 1e-3))
                     for _ in range(n)] + edges)
        assert calc.window_sums(ev, now) == _naive_window_sums(ev, now)
        shuffled = ev[:]
        rnd.shuffle(shuffled)                                    # не по порядку — сортируется, суммы те же с точностью
        got, want = calc.window_sums(shuffled, now), _naive_window_sums(ev, now)
        assert {w: c for w, (_, c) in got.items()} == {w: c for w, (_, c) in want.items()}
        assert all(abs((got[w][0] or 0) - (want[w][0] or 0)) < 1e-15 for w in got)
    assert calc.window_sums([], now) == {w: (None, 0) for w in config.WINDOWS_H}


def test_tick_interval_beats_stale_universe_interval():
    """Интервал сменился 8 ч → 1 ч между пересборками вселенной: ставка в час — по интервалу тика; нога без интервала
    в тике — по вселенной, как раньше."""
    item = {"key": "gate|binance:X", "base": "X", "va": "gate", "vb": "binance", "sa": "X_USDT", "sb": "XUSDT"}
    r = calc.build_ff_row(item, {"interval_h": 8}, {"interval_h": 8}, {"rate": 0.001, "interval_h": 1},
                          {"rate": 0.0008}, None, None, {}, {}, now_ms=0)
    assert (r["iv_a"], r["iv_b"], r["period"]) == (1, 8, 8)
    assert abs(r["rate_h_a"] - 0.001) < 1e-12 and abs(r["rate_h_b"] - 0.0001) < 1e-12


def test_gate_kr200_in_usd_is_not_point_kospi():
    """Gate KR200_USDT = KOSPI 200 в долларах (0.80) — не та единица, что KR200 в пунктах у HL / Bitget (1103). С 13.09
    исключение — config.PERP_UNPAIRED (его видит и тестировщик), база — тикер биржи, без своего «KR200USD»."""
    assert "KR200_USDT" in config.PERP_UNPAIRED["gate"]
    assert gate_fut.perp_base("KR200", "index")[0] == "KR200"
    assert gate_fut.perp_base("SPX500", "index")[0] == "SPX500"


class _SlowVenue:
    """Нативный клиент-подделка: каждый запрос истории — 0.2 с (темп площадки)."""

    def __init__(self, name: str, calls: list):
        self.name, self.calls = name, calls

    def perp_instruments(self):
        return []

    def history_since(self, symbol, start_ms, end_ms=None):
        self.calls.append((self.name, symbol, threading.current_thread().name))
        time.sleep(0.2)
        return []


class _Exhausted(_SlowVenue):
    def history_since(self, symbol, start_ms, end_ms=None):
        self.calls.append((self.name, symbol, threading.current_thread().name))
        raise BudgetExceeded("вес 70 %")


def _gaps(venues_, n):
    now = int(time.time() * 1000)
    g = {"needs_repair": True, "latest_missing": True, "missing": [now - 3_600_000], "since": now - 7_200_000}
    return {(ex, f"S{i}"): dict(g) for ex in venues_ for i in range(n)}


def test_repair_runs_venues_in_parallel_with_one_call_budget(tmp_path):
    con = db.connect(tmp_path / "f.db")
    calls, reports = [], []
    clients = {"va": _SlowVenue("va", calls), "vb": _SlowVenue("vb", calls)}
    gaps = _gaps(clients, 3)
    t0 = time.time()
    funding.repair(con, clients, gaps, {}, report=lambda leg, ok: reports.append((leg, ok)))
    dt = time.time() - t0
    assert len(calls) == 6 and sorted(reports) == sorted((k, True) for k in gaps)
    assert dt < 1.0, f"площадки шли по очереди: {dt:.2f} с (по очереди — 1.2 с)"
    assert threading.current_thread().name not in {t for _, _, t in calls}
    calls.clear()
    funding.repair(con, clients, gaps, {}, max_calls=4)      # бюджет вызовов общий на все площадки
    assert len(calls) == 4


def test_repair_skips_only_the_exhausted_venue(tmp_path):
    con = db.connect(tmp_path / "f.db")
    calls, reports = [], []
    clients = {"va": _Exhausted("va", calls), "vb": _SlowVenue("vb", calls)}
    funding.repair(con, clients, _gaps(clients, 3), {}, report=lambda leg, ok: reports.append((leg, ok)))
    assert [c[:2] for c in calls if c[0] == "va"] == [("va", "S0")], "после лимита её ноги до следующего прохода"
    assert len([c for c in calls if c[0] == "vb"]) == 3
    assert (("va", "S0"), False) in reports
