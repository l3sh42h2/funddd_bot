"""Плановый досбор после часового расчёта — не «дыра» (владелец 12.09: «эти предупреждения тут зачем?»; ревью 12.09:
«неполных» мигало каждый час с hh:10:00 до старта добора Hyperliquid). Добор при этом всё равно назначается."""
from funding_bot import funding, calc, config

H = 3600_000
HH = (1_789_200_000_000 // H) * H          # круглый час
LEG = ("hyperliquid", "X")


def _comp(now_ms, last_ms=HH - H):
    ev = {LEG: [(t, 0.0001) for t in range(last_ms - 48 * H, last_ms + 1, H)]}
    return funding.completeness(ev, [LEG], {"hyperliquid": {"X": 1}}, now_ms, max(config.WINDOWS_H),
                                depth={LEG: last_ms - 800 * H}, sync={LEG: last_ms})[LEG]


def test_latest_settlement_in_catchup_is_not_a_gap_but_is_repaired():
    c = _comp(HH + 15 * 60_000)                                   # hh:15 — Hyperliquid дособирается по одной монете
    assert c["latest_missing"] and c["needs_repair"] and c["catchup"]
    assert not any(calc._incomplete([c], w, HH + 15 * 60_000) for w in config.WINDOWS_H)
    assert c["pending_ms"] == HH and calc.window_anchor(c, HH + 15 * 60_000) == HH - 1     # окно — перед ним


def test_latest_settlement_missing_after_catchup_is_a_gap():
    now = HH + config.CATCHUP_S * 1000 + 60_000                   # досбор давно должен был закончиться
    c = _comp(now)
    assert c["latest_missing"] and not c["catchup"]
    assert all(calc._incomplete([c], w, now) for w in config.WINDOWS_H)


def test_within_grace_nothing_is_missing():
    c = _comp(HH + 5 * 60_000)                                    # льгота: расчёт ещё может появиться сам
    assert not c["latest_missing"] and not c["catchup"]
    assert not calc._incomplete([c], 24, HH + 5 * 60_000)


def test_collected_latest_is_complete():
    c = _comp(HH + 20 * 60_000, last_ms=HH)
    assert not c["latest_missing"] and not c["catchup"]
