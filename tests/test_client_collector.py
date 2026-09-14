import json, os, time, subprocess
import pytest
from funding_bot import config, db, dashboard
from funding_bot.client import BinanceLike, BudgetExceeded, BannedError
from funding_bot.collector import Collector, settle_due
from fakes import make_world, perp, spot, grid, H
from test_universe_identity import FF_KEYS, SF_KEYS, MEME_FF, AI_SF, MEME_SF, BN

JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
ABC_FF = "aster:ABCUSDT|binance:ABCUSDT"
XYZ_FF = "aster:XYZUSDT|binance:XYZUSDT"
BONLY_FF = "binance:BONLYUSDT|hyperliquid:BONLY"
PEPE_FF = "aster:1000PEPEUSDT|hyperliquid:kPEPE"
NATGAS_FF = "binance:NATGASUSDT|hyperliquid:xyz:NATGAS"
GPRO_FF = "aster:GPROUSD1|binance:GPROUSDT"


class _Resp:
    def __init__(self, code=200, headers=None, body="[]"):
        self.status_code, self.headers, self.text = code, headers or {}, body
        self.url = "u"

    def json(self): return json.loads(self.text)
    def raise_for_status(self):
        if self.status_code >= 500: raise RuntimeError(str(self.status_code))


class _Session:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params)); return self.responses.pop(0)


def test_weight_header_any_case_and_soft_limit():
    s = _Session([_Resp(headers={"x-mbx-used-weight-1m": "100"}), _Resp(headers={"X-MBX-USED-WEIGHT-1M": "1700"}), _Resp()])
    cl = BinanceLike("binance", "https://x", 2400, session=s)
    cl.get("/p"); assert cl.used_weight == 100 and cl.budget_ok()
    cl.get("/p"); assert cl.used_weight == 1700 and not cl.budget_ok()
    with pytest.raises(BudgetExceeded):
        cl.get("/p")                                        # без HTTP-вызова
    assert len(s.calls) == 2
    cl.used_weight_ts -= 61                                 # заголовок протух — бюджет считается свободным
    assert cl.budget_ok()


def test_418_bans_and_429_retries(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    s = _Session([_Resp(418, {"Retry-After": "300"})])
    cl = BinanceLike("aster", "https://x", 2400, session=s)
    with pytest.raises(BannedError):
        cl.get("/p")
    assert cl.banned_until > time.time() + 200
    with pytest.raises(BannedError):
        cl.get("/p")                                        # до конца бана вызовов нет
    assert len(s.calls) == 1
    s2 = _Session([_Resp(429, {"Retry-After": "1"}), _Resp(body="[1]")])
    cl2 = BinanceLike("aster", "https://x", 2400, session=s2)
    assert cl2.get("/p") == [1] and cl2.n_429 == 1


def test_min_gap_paces_funding_history(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    s = _Session([_Resp(), _Resp()])
    cl = BinanceLike("binance", "https://x", 2400, session=s); cl.set_min_gap("/fapi/v1/fundingRate", 0.7)
    cl.get("/fapi/v1/fundingRate"); cl.get("/fapi/v1/fundingRate")
    assert slept and 0 < slept[-1] <= 0.7


def _collector(tmp_path, world, now=None, background=False):
    t = [now or time.time()]
    col = Collector(clients=world, db_path=tmp_path / "c.db", table_path=tmp_path / "table.json",
                    now=lambda: t[0], sleep=lambda s: None, background=background)
    return col, t


def _ticks(col, t, n):
    tbl = None
    for _ in range(n):
        t[0] += config.TICK_S; tbl = col.once()
    return tbl


def _by(rows):
    return {r["key"]: r for r in rows}


def _ident(col, t):
    """Списки монет и составы индексов — долгие шаги, тик их не ждёт: дождаться заданий и пересобрать таблицу."""
    for _ in range(3):
        t[0] += config.TICK_S; col.once()
        for f, *_rest in list(col._aux.values()):
            f.result(timeout=10)
    t[0] += config.TICK_S
    return col.once()


def test_tables_three_venues_flags_windows_and_snapshots(tmp_path):
    w = make_world(); col, t = _collector(tmp_path, w)
    tbl = col.once()
    assert tbl["venues"] == ["aster", "binance", "hyperliquid"]
    assert tbl["n_ff"] == 13 and tbl["n_sf"] == 16 and tbl["n_mismatch_ff"] == 0 and tbl["n_mismatch_sf"] == 0   # один тик — не флаг
    assert tbl["spot_venues"] == ["binance_spot", "gate_spot"]
    assert col.con.execute("SELECT COUNT(*) FROM ff_snapshots").fetchone()[0] == 13
    abc = col.con.execute("SELECT px_a, px_b, gap FROM ff_snapshots WHERE key=?", (ABC_FF,)).fetchone()
    assert None not in abc and abs(abc[0] - 10.0) < 1e-9                      # ревью 10.09: цены в снимке больше не NULL
    assert col.con.execute("SELECT COUNT(*) FROM sf_snapshots WHERE mismatch IS NOT NULL").fetchone()[0] == 16
    assert col.con.execute("SELECT COUNT(*) FROM sf_snapshots WHERE spot_ex='gate_spot'").fetchone()[0] == 5
    tbl = _ident(col, t)
    ff, sf = _by(tbl["ff_rows"]), _by(tbl["sf_rows"])
    assert set(ff) == FF_KEYS and set(sf) == SF_KEYS
    # «не тот актив» — по контрактам и составу индекса (владелец 12.09: «не курс»)
    assert {k for k, r in ff.items() if r["mismatch"]} == {MEME_FF}
    assert {k for k, r in sf.items() if r["mismatch"]} == {AI_SF, MEME_SF}
    assert sf[AI_SF]["ident_ev"] == "contract_conflict" and "Artificial Inu" in sf[AI_SF]["ident_why"]
    assert sf[BN + "ABCUSDT|binance:ABCUSDT"]["ident"] == "same" and sf["gate_spot:ABC_USDT|aster:ABCUSDT"]["ident"] == "same"
    assert sf[BN + "PEPEUSDT|binance:1000PEPEUSDT"]["ident"] == "same"            # спот ×1000 в индексе = множитель перпа
    assert sf[BN + "PEPEUSDT|hyperliquid:kPEPE"]["ident_ev"] == "hl_oracle_market"
    assert tbl["n_unknown_sf"] == sum(1 for r in sf.values() if r["ident"] == "unknown") > 0     # XYZ, BONLY: индекса нет
    src = db.load_ident_src(col.con)
    assert {"aster", "binance", "binance_spot", "gate_spot"} <= set(src) and "ABCUSDT" in src["binance"]
    # 12.09: спот Gate — своя цена, своя комиссия, своя подпись и страница токена
    gx = sf["gate_spot:XYZ_USDT|binance:XYZUSDT"]
    assert gx["spot_label"] == "gate" and abs(gx["px_spot"] - 1.995) < 1e-12 and abs(gx["gap"] - (2.0 / 1.995 - 1)) < 1e-12
    assert abs(gx["fee"] - 2 * (config.FEES_TAKER["gate_spot"] + 0.0005)) < 1e-12 and "XYZ" in gx["urls"]["spot"]
    xyz = ff[XYZ_FF]
    assert (xyz["iv_a"], xyz["iv_b"], xyz["period"]) == (1, 4, 4) and xyz["side"] == "short_b"
    assert abs(xyz["spread"] - (-0.0001 - 0.0001)) < 1e-12                     # час A − час B («приводи к 1ч»)
    bo = ff[BONLY_FF]
    assert (bo["iv_a"], bo["iv_b"], bo["period"]) == (8, 1, 8) and bo["side"] == "short_a"
    assert abs(bo["spread"] - (0.0006 / 8 - 0.00005)) < 1e-12
    # владелец 10.09 «не вижу хайпера»: часовая ставка Hyperliquid 0.005 %/ч выше 8-часовой 0.02 %/8ч Aster
    assert sf[BN + "BONLYUSDT|hyperliquid:BONLY"]["spread"] > sf[BN + "ABCUSDT|aster:ABCUSDT"]["spread"]
    assert abs(bo["fee"] - 2 * (0.0005 + 0.00045)) < 1e-12 and bo["urls"][1].endswith("/trade/BONLY")
    assert abs(ff[PEPE_FF]["gap"]) < 1e-6                                     # 1000PEPE против kPEPE
    hp = sf[BN + "PEPEUSDT|hyperliquid:kPEPE"]
    assert hp["spot"] == "PEPEUSDT" and abs(hp["gap"]) < 1e-6 and abs(hp["fee"] - 2 * (0.001 + 0.00045)) < 1e-12
    # 11.09: HIP-3 рынок Hyperliquid против TradFi-перпа Binance; USD1-перп Aster против TradFi Binance
    ng = ff[NATGAS_FF]
    assert ng["lb"] == "hyperliquid·xyz" and ng["la"] == "binance" and ng["urls"][1].endswith("/trade/xyz:NATGAS")
    assert (ng["iv_a"], ng["iv_b"]) == (4, 1) and abs(ng["spread"] - (0.0038 / 4 - 0.00062)) < 1e-12
    assert ff[GPRO_FF]["urls"][0].endswith("/futures/GPROUSD1") and not ff[GPRO_FF]["mismatch"]
    # бэкфилл прошёл в первом проходе: окна полные, у Hyperliquid 24 часовых расчёта за сутки
    ha = sf[BN + "ABCUSDT|hyperliquid:ABC"]
    assert ha["windows"]["24"]["n"] == 24 and not ha["windows"]["720"]["incomplete"]
    assert not ff[ABC_FF]["windows"]["720"]["incomplete"] and tbl["n_incomplete_ff"] == 0
    assert ff[NATGAS_FF]["windows"]["24"]["nb"] == 24
    bs = sf[BN + "BONLYUSDT|binance:BONLYUSDT"]
    assert bs["windows"]["24"]["n"] == 3 and abs(bs["windows"]["24"]["spread"] - 0.0018) < 1e-12
    saved = json.loads((tmp_path / "table.json").read_text())
    assert saved["n_ff"] == 13 and saved["n_sf"] == 16


def test_ff_snapshot_spread_unit_migrated_once(tmp_path):
    """Снимки, записанные «за период», пересчитываются в час один раз — ряд не смешивает единицы."""
    import sqlite3
    p = tmp_path / "m.db"
    raw = sqlite3.connect(p); raw.executescript(db.SCHEMA)
    raw.execute("INSERT INTO ff_snapshots(ts,key,base,va,vb,iv_a,iv_b,spread) VALUES(1,'aster|binance:X','X','aster','binance',8,4,0.0008)")
    raw.execute("INSERT INTO ff_snapshots(ts,key,base,va,vb,iv_a,iv_b,spread) VALUES(1,'aster|binance:Y','Y','aster','binance',1,1,NULL)")
    raw.commit(); raw.close()
    c = db.connect(p)
    assert abs(c.execute("SELECT spread FROM ff_snapshots WHERE key='aster|binance:X'").fetchone()[0] - 0.0001) < 1e-15
    assert c.execute("SELECT spread FROM ff_snapshots WHERE key='aster|binance:Y'").fetchone()[0] is None
    c.close(); c = db.connect(p)                                              # второе подключение не делит повторно
    assert abs(c.execute("SELECT spread FROM ff_snapshots WHERE key='aster|binance:X'").fetchone()[0] - 0.0001) < 1e-15


def test_identity_verdicts_survive_restart_without_refetch(tmp_path):
    """Ревью 10.09: после рестарта AI на Aster висел наверху как настоящая сделка. С 12.09 источники вердиктов (списки
    монет, составы индексов) лежат в БД: после рестарта вердикты есть в первом же проходе, без повторного обхода."""
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once(); _ident(col, t)
    w2 = make_world()
    col2, _ = _collector(tmp_path, w2)
    tbl = col2.once()
    sf, ff = _by(tbl["sf_rows"]), _by(tbl["ff_rows"])
    assert sf[AI_SF]["mismatch"] and sf[MEME_SF]["mismatch"] and ff[MEME_FF]["mismatch"]
    assert not any(p in ("/fapi/v1/constituents", "/fapi/v3/indexreferences") for p, _ in w2["binance"].calls + w2["aster"].calls)
    row = col2.con.execute("SELECT mismatch FROM sf_snapshots WHERE key=? ORDER BY ts DESC LIMIT 1", (AI_SF,)).fetchone()
    assert row[0] == 1


def test_identity_never_uses_price(tmp_path):
    """Владелец 12.09: «не курс». Базис 5 % у той же монеты — не флаг; спот ABC в 13 раз дороже перпа, но рынок спота в
    индексе перпа — та же монета (разрыв — это курсовой, его видно в своей колонке)."""
    w = make_world()
    w["binance"].premium["BONLYUSDT"].update(mark=5.25, index=5.0)
    w["binance"].books["BONLYUSDT"] = {"bid": 5.24, "ask": 5.26}
    w["binance_spot"].books["ABCUSDT"] = {"bid": 129.9, "ask": 130.1}
    col, t = _collector(tmp_path, w)
    col.once(); tbl = _ident(col, t)
    sf = _by(tbl["sf_rows"])
    bo = sf[BN + "BONLYUSDT|binance:BONLYUSDT"]
    assert not bo["mismatch"] and bo["gap"] > 0.04
    ab = sf[BN + "ABCUSDT|binance:ABCUSDT"]
    assert not ab["mismatch"] and ab["ident"] == "same" and ab["gap"] < -0.9
    assert not _by(tbl["ff_rows"])[BONLY_FF]["mismatch"]


def test_venue_failure_is_isolated(tmp_path):
    w = make_world(); w["hyperliquid"].fail.add("meta")
    col, t = _collector(tmp_path, w)
    tbl = col.once()
    assert "universe:hyperliquid" in tbl["notes"] and "tick:hyperliquid" in tbl["notes"]
    assert tbl["n_ff"] == 6 and tbl["n_sf"] == 12                           # две площадки живут без третьей (8 Binance + 4 Gate)
    w["hyperliquid"].fail.clear()
    t[0] += 130; tbl = col.once()                                           # упавшая площадка — повтор через 2 мин, не через час
    assert tbl["n_ff"] == 13 and tbl["n_sf"] == 16
    assert not any(k.endswith(":hyperliquid") for k in tbl["notes"])
    w["aster"].fail_paths.add("/fapi/v1/premiumIndex")
    tbl = _ticks(col, t, 1)
    assert "RuntimeError" in tbl["notes"]["tick:aster"] and tbl["n_ff"] == 13  # таблица живёт на прежних данных Aster
    w["aster"].fail_paths.clear()
    assert "tick:aster" not in _ticks(col, t, 1)["notes"]
    w["gate_spot"].fail.add("books")                                         # спот Gate молчит — его сделки устаревают, остальные живут
    for _ in range(7):
        t[0] += config.TICK_S; tbl = col.once()
    for p in col.books["gate_spot"].values():
        p["obs"] -= config.STALE_S + 1
    tbl = col.build_table()
    assert "tick:gate_spot" in tbl["notes"] and tbl["n_sf"] == 16
    gx = _by(tbl["sf_rows"])["gate_spot:XYZ_USDT|binance:XYZUSDT"]
    assert gx["px_spot"] is None and gx["gap"] is None                     # старая книга Gate не выдаётся за курсовой
    assert gx["stale"] and tbl["n_stale_sf"] == 5                           # все 5 сделок Gate «устарели» — не лучшие в группе
    assert _by(tbl["sf_rows"])[BN + "ABCUSDT|binance:ABCUSDT"]["gap"] is not None


def test_budget_exceeded_skips_venue_tick_and_is_visible(tmp_path):
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once()
    w["aster"].fake_weight = 2000
    tbl = _ticks(col, t, 1)
    assert "BudgetExceeded" in tbl["notes"].get("tick:aster", "")
    assert tbl["health"]["aster"]["budget"] > config.WEIGHT_SOFT_LIMIT


def test_disk_low_stops_snapshots_not_history(tmp_path, monkeypatch):
    w = make_world(); col, t = _collector(tmp_path, w)
    monkeypatch.setattr(col, "disk_free_gb", lambda: 0.5)
    tbl = col.once()
    assert col.con.execute("SELECT COUNT(*) FROM ff_snapshots").fetchone()[0] == 0
    assert col.con.execute("SELECT COUNT(*) FROM sf_snapshots").fetchone()[0] == 0
    assert "снимки не пишутся" in tbl["notes"]["snapshot"]
    assert db.count_funding_events(col.con) > 0


def test_schedule_respects_cadences(tmp_path, monkeypatch):
    # Isolate periodic scheduling from the host clock and settlement boundaries.
    now = 1_700_001_234.0
    w = make_world(int(now * 1000)); col, t = _collector(tmp_path, w, now=now)
    monkeypatch.setattr(time, 'time', lambda: t[0])
    cnt = lambda path, nosym=False: sum(1 for p, q in w["aster"].calls if p == path and (not nosym or "symbol" not in q))
    col.once()
    assert (cnt("/fapi/v1/exchangeInfo"), cnt("/fapi/v1/fundingInfo"), cnt("/fapi/v1/fundingRate", True)) == (1, 1, 1)
    t[0] += config.TICK_S; col.once()
    assert (cnt("/fapi/v1/exchangeInfo"), cnt("/fapi/v1/fundingInfo"), cnt("/fapi/v1/fundingRate", True)) == (1, 1, 1)
    t[0] += config.FUNDING_INCR_S; col.once()                                # интервалы и досбор — раз в 15 мин
    assert (cnt("/fapi/v1/exchangeInfo"), cnt("/fapi/v1/fundingInfo"), cnt("/fapi/v1/fundingRate", True)) == (1, 2, 2)
    t[0] += config.UNIVERSE_S; col.once()                                    # вселенная — раз в час
    assert cnt("/fapi/v1/exchangeInfo") == 2 and cnt("/fapi/v1/fundingInfo") == 3


def test_cli_top_prints_both_modes(tmp_path, monkeypatch, capsys):
    """`funding_bot top` падал на убранном «Отклонении» (проверка исправлений 12.09) — тестов на него не было."""
    from funding_bot import cli
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once()
    monkeypatch.setattr(config, "TABLE_PATH", tmp_path / "table.json")
    assert cli.main(["top", "--mode", "sf", "-n", "5"]) == 0 and cli.main(["top", "--mode", "ff", "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert "gate|binance" in out and "aster|binance" in out and "откл" not in out


def test_settle_due_fires_once_after_each_hour():
    top = 1000 * 3600
    mark = top + config.SETTLE_GRACE_S + 30
    assert not settle_due(mark - 1, top - 100)              # до hh:10:30 — рано
    assert settle_due(mark, top - 100)                      # прошло, а после отметки не проверяли
    assert not settle_due(mark + 5, mark)                   # уже проверили после этой отметки
    assert settle_due(mark + 3600, mark + 5)                # следующий час


def test_interval_change_seen_within_15_min(tmp_path):
    """Ревью 10.09: Aster меняет интервал на ходу, а интервалы обновлялись раз в час."""
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once()
    w["aster"].funding_info["XYZUSDT"] = {"interval_h": 4}
    t[0] += config.INTERVALS_S; tbl = col.once()
    xyz = _by(tbl["ff_rows"])[XYZ_FF]
    assert (xyz["iv_a"], xyz["iv_b"], xyz["period"]) == (4, 4, 4)


def test_new_leg_from_hourly_rebuild_is_backfilled(tmp_path):
    """Ревью 10.09: нога, появившаяся при часовой пересборке, раньше никогда не получала глубины."""
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once()
    b, s = w["binance"], w["binance_spot"]
    b.symbols.append(perp("NEWUSDT")); s.symbols.append(spot("NEWUSDT"))
    b.funding_info["NEWUSDT"] = {"interval_h": 8}; b.premium["NEWUSDT"] = {"rate": 0.0002, "mark": 2.0}
    b.books["NEWUSDT"] = {"bid": 1.99, "ask": 2.01}; s.books["NEWUSDT"] = {"bid": 1.99, "ask": 2.0}
    b.history["NEWUSDT"] = grid(int(t[0] * 1000), 8, 0.0002, days=2)          # листинг двое суток назад
    # пересборка вселенной без сдвига часов коллектора: сдвиг на час вперёд разводил его «сейчас» с настоящим временем
    # внутри funding (курсор ноги ставится по time.time()), и с hh:35 до границы 8 ч тест падал по времени суток
    col._vlast = {k: v for k, v in col._vlast.items() if k[0] != "universe"}
    tbl = col.once()
    assert ("binance", "NEWUSDT") in db.leg_depths(col.con)
    nw = _by(tbl["sf_rows"])[BN + "NEWUSDT|binance:NEWUSDT"]
    assert nw["windows"]["720"]["n"] == 6 and not nw["windows"]["720"]["incomplete"]   # честно короткая история — не «!»


def test_failed_backfill_leg_is_retried_and_flagged_meanwhile(tmp_path):
    w = make_world(); w["hyperliquid"].fail.add("history")
    col, t = _collector(tmp_path, w)
    tbl = col.once()
    ha = _by(tbl["sf_rows"])[BN + "ABCUSDT|hyperliquid:ABC"]
    assert ha["windows"]["720"]["incomplete"] and ha["windows"]["24"]["spread"] is None      # не выдаём пустоту за истину
    w["hyperliquid"].fail.clear()
    t[0] += config.FUNDING_INCR_S; tbl = col.once()
    ha = _by(tbl["sf_rows"])[BN + "ABCUSDT|hyperliquid:ABC"]
    now_ms = int(t[0] * 1000)                                                 # часы коллектора ушли на 15 мин вперёд
    expected = sum(1 for ms, _ in w["hyperliquid"].history["ABC"] if now_ms - 24 * H < ms <= now_ms)
    assert not ha["windows"]["720"]["incomplete"] and ha["windows"]["24"]["n"] == expected >= 23
    assert ("hyperliquid", "ABC") in db.leg_depths(col.con)


def test_shallow_leg_does_not_block_latest_repair(tmp_path):
    """Тестировщик 11.09: пока в карте полноты была хоть одна «мелкая» нога, шаг уходил в бэкфилл и не делал добор —
    99 монет Hyperliquid жили без последнего часа. Теперь одно задание делает и то, и другое."""
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once()
    last = db.last_funding_ms(col.con, "hyperliquid", "ABC")
    # простой коллектора: курсор HL ABC остался на три часа назад, последних расчётов в БД нет
    col.con.execute("UPDATE leg_depth SET synced_to=? WHERE exchange='hyperliquid' AND symbol='ABC'", (last - 3 * H,))
    col.con.execute("DELETE FROM funding_events WHERE exchange='hyperliquid' AND symbol='ABC' AND funding_ms > ?", (last - 3 * H,))
    col.con.execute("DELETE FROM leg_depth WHERE exchange='binance' AND symbol='BONLYUSDT'")
    col.con.execute("DELETE FROM funding_events WHERE exchange='binance' AND symbol='BONLYUSDT' AND funding_ms < ?", (last - 24 * H,))
    col.con.commit(); col._events_dirty = True
    t[0] += config.FUNDING_INCR_S; col.once()
    assert db.last_funding_ms(col.con, "hyperliquid", "ABC") == last          # добор прошёл — весь пропуск, не только последний час
    n = col.con.execute("SELECT COUNT(*) FROM funding_events WHERE exchange='hyperliquid' AND symbol='ABC' AND funding_ms > ?",
                        (last - 3 * H,)).fetchone()[0]
    assert n == 3
    assert ("binance", "BONLYUSDT") in db.leg_depths(col.con)                  # и мелкая нога добрана с пола
    assert col.repair_state["n_symbols"] >= 1 and col.repair_state["n_new"] >= 3


def test_first_tick_is_not_blocked_by_history(tmp_path):
    """Пустая БД на старте: тик не ждёт посимвольных обходов — они в фоне, таблица пишется сразу."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    w = make_world()
    entered, release = Event(), Event()
    history = w['hyperliquid'].history_since

    def blocked_history(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'test history was never released'
        return history(*args, **kwargs)
    w['hyperliquid'].history_since = blocked_history
    col, t = _collector(tmp_path, w, background=True)
    # Prove independence while history is still blocked. A 500 ms wall-clock
    # assertion measured the shared runner's load, not the dependency itself.
    with ThreadPoolExecutor(max_workers=1) as pool:
        tick = pool.submit(col.once)
        try:
            assert entered.wait(5)
            tbl = tick.result(timeout=5)
            assert tbl['n_ff'] == 13 and not release.is_set()
        finally:
            release.set()
    for _ in range(300):
        if not col._maint_running and col.backfill_state["finished_ts"]:
            break
        time.sleep(0.02)
    assert col.backfill_state["finished_ts"] and col.backfill_state["stats"]["errors"] == 0
    assert ("hyperliquid", "BONLY") in db.leg_depths(col.con) and ("hyperliquid", "xyz:NATGAS") in db.leg_depths(col.con)


@pytest.mark.skipif(not os.path.exists(JSC), reason="JavaScriptCore есть только на Маке")
def test_page_script_signed_sort_nulls_last_and_modes(tmp_path):
    """Скрипт страницы в настоящем JS-движке: сортировка со знаком (владелец 10.09), пустые в конце, оба режима."""
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once(); tbl = _ident(col, t)
    ff = _by(tbl["ff_rows"])
    ff[ABC_FF]["spread"] = None                                               # строка без ставки
    html = dashboard.render(tbl)
    assert 'data-m="sf">spot/futures' in html and 'data-m="ff">futures/futures' in html
    assert "со спотом" not in html and '"hyperliquid", "Hyperliquid"' in html
    stub = """
var __els = {};
function __el(){ return {innerHTML:'', textContent:'', hidden:false, value:'', checked:false, dataset:{},
                         classList:{toggle(){}, add(){}, remove(){}}}; }
var document = {hidden:false, querySelector(s){ if(!__els[s]) __els[s] = __el(); return __els[s]; }, querySelectorAll(s){ return []; },
                addEventListener(){}};
var localStorage = {getItem(){ return null; }, setItem(){}};
function setInterval(){}
function setTimeout(){ return 0; }            // плановый опрос — цепочка setTimeout; jsc ждал бы её до конца теста
function clearTimeout(){}
var console = {error(){}, log(){}};
"""
    checks = """
var out = {};
GAP_HIDE = Infinity;          // проверки порядка и «скрыть ≠» — без правила 7 % (AI, MEME ×13…×120); его проверяет test_dashboard_rating
out.min1dDef = ui.min1d; ui.min1d = '';           // по умолчанию «1d > 0.5 %»; проверки порядка — на всех строках
const S = r => r.mismatch ? 'M' : (nz(r.spread) ? 'N' : r.spread);
ui.mode = 'ff';
ui.ff = {sort:'spread', dir:-1}; out.desc = filtered().map(S);
ui.ff = {sort:'spread', dir:1};  out.asc = filtered().map(S);
ui.ff = {sort:'w4', dir:1};      out.w4asc = filtered().map(r => r.mismatch ? 'M' : (nz((r.windows['4']||{}).spread) ? 'N' : r.windows['4'].spread));
ui.ff = {sort:'base', dir:-1};   out.base = filtered().filter(r => !r.mismatch).map(r => r.base);
ui.ff = {sort:'spread', dir:-1}; render();
out.ffShown = __els['#shown'].textContent; out.ffRows = __els['#rows'].innerHTML; out.ffHead = __els['#head'].innerHTML;
const TR = s => (s.match(/<tr/g) || []).length;
out.ffTr = TR(out.ffRows);
ui.open.ff = ['ABC']; render(); out.ffOpen = __els['#rows'].innerHTML; out.ffOpenTr = TR(out.ffOpen);
ui.mode = 'sf'; ui.sf = {sort:'spread', dir:-1}; render();
out.sfShown = __els['#shown'].textContent; out.sfTop = filtered()[0].key; out.sfRows = __els['#rows'].innerHTML;
out.sfTr = TR(out.sfRows);
setPick('sp', '*', false); setPick('sp', 'gate_spot', true); out.sfGate = filtered().map(r => r.spot_ex); setPick('sp', '*', true);
// сортировка по Монете / Периоду / Комиссии: среди комбинаций группы с тем же значением ключа, что у первой, первой
// стоит лучшая по текущему (у Монеты ключ равен всегда), а не первая по порядку бирж
const lead = s => grouped(filtered()).every(g => {
  const k = KEYS[s], top = g.filter(r => rank(r) === rank(g[0]) && k(r) === k(g[0]) && !nz(r.spread));
  return !top.length || (!nz(g[0].spread) && g[0].spread === Math.max(...top.map(r => r.spread))); });
out.leads = [];
for(const m of ['sf', 'ff']) for(const s of ['base', 'period', 'fee']) for(const d of [-1, 1]){ ui.mode = m; ui[m] = {sort:s, dir:d}; out.leads.push(lead(s)); }
ui.mode = 'sf'; ui.sf = {sort:'spread', dir:-1};
// владелец 12.09 вечер: «1d >» вместо «Текущий ≥»; биржи галочками вместо «периода»; строка состояния — только при неполадке
ui.min1d = '0.1'; out.d1 = filtered().map(r => [r.base, (r.windows[D1] || {}).spread]); ui.min1d = '';
ui.mode = 'ff'; ui.min1d = '0.1'; out.d1ff = filtered().map(r => (r.windows[D1] || {}).spread); ui.min1d = ''; ui.mode = 'sf';
setPick('sp', '*', false); setPick('sp', 'binance_spot', true); setPick('pp', '*', false); setPick('pp', 'binance', true);
out.exSf = filtered().map(r => [r.spot_ex, r.perp_ex]); out.exBtn1 = __els['#spBtn'].textContent + '|' + __els['#ppBtn'].textContent;
ui.mode = 'ff'; setPick('pp', 'aster', true); out.exFf = filtered().map(r => [r.va, r.vb]); out.exBtn2 = __els['#ppBtn'].textContent;
setPick('sp', '*', true); setPick('pp', '*', true); out.exAll = [ui.spSel, ui.ppSel];
out.exMenu = __els['#spMenu'].innerHTML + __els['#ppMenu'].innerHTML; out.exBtn3 = __els['#spBtn'].textContent; ui.mode = 'sf';
renderStatus(Date.now()); out.status = __els['#status'].innerHTML; out.statusHidden = __els['#status'].hidden; out.misN = __els['#misN'].textContent;
T.n_incomplete_sf = 5; renderStatus(Date.now()); out.statusInc = __els['#status'].innerHTML; T.n_incomplete_sf = 0;
// ревью 12.09: фильтр «1d» по умолчанию включён — он не должен молча выкидывать «не тот актив» (это делает переключатель)
ui.min1d = '0.1'; ui.hideMis = false; out.d1mis = filtered().filter(r => r.mismatch).length;
ui.hideMis = true; out.d1hide = filtered().filter(r => r.mismatch).length; ui.hideMis = false; ui.min1d = '';
out.pageErr = pageErr;
print(JSON.stringify(out));
"""
    src = tmp_path / "page.js"
    src.write_text(stub + dashboard.page_script(tbl) + checks)
    res = subprocess.run([JSC, str(src)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr + res.stdout
    out = json.loads(res.stdout.strip().splitlines()[-1])

    def ordered(seq, desc):
        nums = [x for x in seq if x not in ("N", "M")]
        tail = [x for x in seq if x in ("N", "M")]
        assert seq == nums + tail, seq                                        # числа, потом пустые, потом «не тот актив»
        assert tail == sorted(tail, key=lambda x: x == "M"), seq
        assert nums == sorted(nums, reverse=desc), seq
    ordered(out["desc"], True)                                                # первый клик — плюс сверху
    ordered(out["asc"], False)                                                # второй — минус сверху, пустые всё равно внизу
    ordered(out["w4asc"], False)
    assert out["desc"].count("N") == 1 and out["desc"][-1] == "M"
    assert out["base"] == sorted(out["base"])
    # владелец 12.09: комбинации монеты — группой; видна лучшая, «N спредов» раскрывает остальные
    assert out["ffShown"] == "7 монет · 13 из 13" and out["sfShown"] == "6 монет · 16 из 16"
    assert out["ffTr"] == 7 and "3 спреда ⌄" in out["ffRows"] and "rowspan" not in out["ffRows"]
    assert out["ffOpenTr"] == 9 and 'rowspan="3"' in out["ffOpen"] and "3 спреда ⌃" in out["ffOpen"]
    assert out["sfTr"] == 6 and "6 спредов ⌄" in out["sfRows"]              # ABC: 2 спота × 3 перпа
    assert "Текущий 1ч ↓" in out["ffHead"] and ">spot<" not in out["ffRows"]  # спота во futures/futures нет
    assert "Отклонение" not in out["ffHead"]                                  # владелец 12.09: «откл не нужно»
    assert "hyperliquid·xyz" in out["ffRows"]                                 # HIP-3 рынок подписан своим dex
    assert out["sfTop"] == "gate_spot:XYZ_USDT|binance:XYZUSDT" and ">gate</a> <span class=\"up\">▲" in out["sfRows"]
    assert set(out["sfGate"]) == {"gate_spot"} and len(out["sfGate"]) == 5
    assert all(out["leads"]) and len(out["leads"]) == 12
    assert '"gate_spot", "Gate"' in html
    # «Курсовой»: проценты крупно сверху, цены мелко под ними (владелец 10.09: «проценты важнее»)
    assert '%</b><span class="s mono">' in out["ffRows"] and '%</b><span class="s mono">' in out["sfRows"]
    # владелец 12.09 вечер: «1d > 0.5 %» по умолчанию; фильтр — по сумме за сутки, в futures/futures по модулю
    assert out["min1dDef"] == "0.5" and 'id="min1d"' in html and 'id="ivP"' not in html and 'id="ivF"' not in html
    assert out["d1"] and all(s * 100 > 0.1 for _, s in out["d1"]) and {"BONLY", "XYZ"} <= {b for b, _ in out["d1"]}
    assert out["d1ff"] and all(abs(s) * 100 > 0.1 for s in out["d1ff"])
    # биржи галочками: сделка видна, если все её площадки выбраны (спот и перп Binance — одна биржа)
    assert out["exSf"] and all(p == ["binance_spot", "binance"] for p in out["exSf"]) and out["exBtn1"] == "Binance ⌄|Binance ⌄"
    assert out["exFf"] and all(set(p) <= {"aster", "binance"} for p in out["exFf"]) and out["exBtn2"] == "Aster, Binance ⌄"
    assert out["exAll"] == [None, None] and out["exBtn3"] == "все ⌄" and "> Все</label>" in out["exMenu"]
    assert "Hyperliquid" in out["exMenu"] and "Gate" in out["exMenu"]
    assert 'id="spBtn"' in html and 'id="ppMenu" hidden' in html and 'id="spotEx"' not in html and 'id="exBtn"' not in html
    # строка состояния: всё в порядке — пусто; плановый досбор Hyperliquid — не «неполных»; дыра без досбора — «неполных»
    assert out["status"] == "" and out["statusHidden"] is True and out["misN"] == " (2)"
    assert "неполных: 5" in out["statusInc"]                                 # плановый досбор коллектор не считает сам
    assert out["d1mis"] >= 1 and out["d1hide"] == 0                          # «скрыть «не тот актив»» работает и с «1d»
    assert out["pageErr"] == ""
