"""Настоящий клиент Hyperliquid на подставной HTTP-сессии (внешнее ревью 11.09: в остальных тестах его заменяет FakeHL)."""
import json, time, types
import pytest
from funding_bot import config
from funding_bot.hyperliquid import Hyperliquid
from funding_bot.client import BannedError

H = 3600_000


class _R:
    def __init__(self, code=200, body=None, headers=None):
        self.status_code, self._b, self.headers, self.text = code, body, headers or {}, json.dumps(body)

    def json(self): return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return self.handler(json)


def _meta(names):
    uni = [{"name": n, "szDecimals": 2} for n in names]
    ctx = [{"funding": "0.0000125", "markPx": "10", "oraclePx": "10", "midPx": "10", "impactPxs": ["9.9", "10.1"]} for _ in names]
    return [{"universe": uni}, ctx]


def _clock(monkeypatch, start=1000.0):
    clock = [start]
    fake = types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None, gmtime=time.gmtime, strftime=time.strftime)
    monkeypatch.setattr("funding_bot.hyperliquid.time", fake)
    return clock


def test_history_pagination_500_plus_220():
    full = [dict(coin="BTC", time=1786500000000 + i * H, fundingRate="0.0000125") for i in range(720)]
    s = _S(lambda b: _R(body=[r for r in full if b["startTime"] <= r["time"] <= b["endTime"]][:500]))
    cl = Hyperliquid(session=s, history_gap_s=0, short_gap_s=0)
    rows = cl.history_since("BTC", full[0]["time"], full[-1]["time"])
    assert len(rows) == 720 and len({r["funding_ms"] for r in rows}) == 720 and len(s.calls) == 2
    assert s.calls[1]["startTime"] == full[499]["time"] + 1 and all(r["rate"] == 0.0000125 for r in rows)


def test_malformed_context_raises():
    s = _S(lambda b: _R(body=[{"universe": [{"name": "BTC"}, {"name": "ETH"}]}, [{"funding": "0"}]]))
    with pytest.raises(RuntimeError, match="universe 2 != ctxs 1"):
        Hyperliquid(session=s)._fetch("")


def test_429_retries_then_pauses_for_a_minute(monkeypatch):
    clock = _clock(monkeypatch)
    s = _S(lambda b: _R(429, body={}, headers={"Retry-After": "1"}))
    cl = Hyperliquid(session=s)
    with pytest.raises(RuntimeError):
        cl.post({"type": "metaAndAssetCtxs"})
    assert cl.n_429 == 3 and cl.banned_until >= clock[0] + 59
    with pytest.raises(BannedError):
        cl.post({"type": "metaAndAssetCtxs"})
    assert len(s.calls) == 3                                           # во время паузы запросов к бирже нет
    s2 = _S(lambda b: _R(429, body={}) if len(s2.calls) == 1 else _R(body=_meta(["BTC"])))
    cl2 = Hyperliquid(session=s2)
    assert cl2.post({"type": "metaAndAssetCtxs"})[0]["universe"][0]["name"] == "BTC" and cl2.n_429 == 1


def test_tick_refreshes_main_dex_and_every_stale_hip3_dex(monkeypatch):
    clock = _clock(monkeypatch)

    def h(b):
        if b["type"] == "perpDexs":
            return _R(body=[None, {"name": "xyz"}, {"name": "para"}, {"name": "dead"}])
        return _R(body=_meta({"": ["BTC"], "xyz": ["xyz:NATGAS"], "para": ["para:NET"], "dead": []}[b.get("dex", "")]))
    s = _S(h)
    cl = Hyperliquid(session=s)
    ins = cl.perp_instruments()
    assert {i["symbol"]: i["base"] for i in ins} == {"BTC": "BTC", "xyz:NATGAS": "NATGAS", "para:NET": "NET"}
    n0 = len(s.calls)
    clock[0] += 10; cl.premium()
    assert [c.get("dex", "") for c in s.calls[n0:]] == [""]           # HIP-3 ещё свежие (< 40 с) — только основной
    clock[0] += 35; n1 = len(s.calls); p = cl.premium()
    assert [c.get("dex", "") for c in s.calls[n1:]] == ["", "para", "xyz"]   # основной + КАЖДЫЙ устаревший живой HIP-3
    assert p["para:NET"]["obs"] == clock[0] and p["xyz:NATGAS"]["obs"] == clock[0] and p["BTC"]["obs"] == clock[0]
    clock[0] += 10; n2 = len(s.calls); p = cl.premium()
    assert [c.get("dex", "") for c in s.calls[n2:]] == [""]
    # метка наблюдения — время снимка своего dex, а не «сейчас»: гистерезис не примет кэш за новое наблюдение
    assert p["xyz:NATGAS"]["obs"] == clock[0] - 10 and p["BTC"]["obs"] == clock[0]
    assert cl.books()["xyz:NATGAS"]["obs"] == clock[0] - 10


def test_many_hip3_dexes_never_older_than_stale_limit(monkeypatch):
    """Проверка исправлений 11.09: по одному dex за тик при 8 dex хвост старел до ~90 с > STALE_S — строки бледнели,
    «не тот актив» не подтверждался никогда."""
    clock = _clock(monkeypatch)
    names = [f"d{k}" for k in range(8)]

    def h(b):
        if b["type"] == "perpDexs":
            return _R(body=[None] + [{"name": n} for n in names])
        d = b.get("dex", "")
        return _R(body=_meta([f"{d}:C{d[1:]}"] if d else ["BTC"]))
    cl = Hyperliquid(session=_S(h))
    cl.perp_instruments()
    worst = 0.0
    for _ in range(30):
        clock[0] += config.TICK_S
        cl.premium()
        worst = max(worst, max(clock[0] - ts for ts, _u, _c in cl._ctx.values()))
    assert worst <= config.HL_HIP3_TTL_S + config.TICK_S < config.STALE_S
