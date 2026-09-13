import time
from funding_bot import db, funding, exchanges, universe, venues
from fakes import make_world, H


def _con(tmp_path):
    return db.connect(tmp_path / "t.db")


def _ab(w):
    """Ноги пар aster|binance и объявленные интервалы — как их видит коллектор."""
    perps = {v: venues.perp_instruments(w[v]) for v in ("aster", "binance")}
    iv = {v: {i["symbol"]: i["interval_h"] for i in perps[v]} for v in perps}
    return funding.legs_of(universe.build_ff(perps)), iv


def test_insert_dedup_and_last(tmp_path):
    con = _con(tmp_path)
    rows = [dict(exchange="aster", symbol="ABCUSDT", funding_ms=1000, rate=0.001, mark=1.0),
            dict(exchange="aster", symbol="ABCUSDT", funding_ms=2000, rate=0.002, mark=1.0)]
    assert db.insert_funding_events(con, rows, {"ABCUSDT": 8}) == 2
    assert db.insert_funding_events(con, rows) == 0                       # дубли молча отброшены
    assert db.insert_funding_events(con, rows + [dict(exchange="binance", symbol="ABCUSDT", funding_ms=1000, rate=0.0)]) == 1
    assert db.last_funding_ms(con, "aster", "ABCUSDT") == 2000
    assert db.funding_span(con, "aster", "ABCUSDT") == (1000, 2000)
    assert db.last_funding_ms(con, "aster", "NOPE") is None
    assert db.funding_events_since(con, 1500) == {("aster", "ABCUSDT"): [(2000, 0.002)]}
    assert db.count_funding_events(con) == 3


def test_legs_of_union_without_duplicates():
    ff = [dict(va="aster", sa="ABCUSDT", vb="binance", sb="ABCUSDT"), dict(va="binance", sa="ABCUSDT", vb="hyperliquid", sb="ABC")]
    sf = [dict(perp_ex="binance", perp="ABCUSDT"), dict(perp_ex="binance", perp="BONLYUSDT"), dict(perp_ex="hyperliquid", perp="ABC")]
    # ноги пар совпадают с перпами сделок — история собирается ОДИН раз
    assert funding.legs_of(ff, sf) == [("aster", "ABCUSDT"), ("binance", "ABCUSDT"), ("hyperliquid", "ABC"), ("binance", "BONLYUSDT")]
    assert funding.legs_of([], None) == [] and funding.legs_of([], sf[1:2]) == [("binance", "BONLYUSDT")]


def test_backfill_idempotent_paged_and_records_depth(tmp_path):
    con = _con(tmp_path); w = make_world()
    legs, iv = _ab(w)
    assert len(legs) == 12                                                   # 6 пар aster|binance, включая TradFi и USD1
    # досбор без символа уже положил ПОСЛЕДНИЙ расчёт ABC на Aster — глубины это не даёт, бэкфилл обязан идти
    last_abc = w["aster"].history["ABCUSDT"][-1]
    db.insert_funding_events(con, [dict(exchange="aster", symbol="ABCUSDT", funding_ms=last_abc[0], rate=last_abc[1])])
    t0 = int(time.time() * 1000)
    st = funding.backfill(con, w, legs, iv, days=3)
    n1 = db.count_funding_events(con)
    assert st["new"] == n1 - 1 and n1 > 0 and st["errors"] == 0 and st["skipped"] == 0 and st["verified"] == 12
    assert con.execute("SELECT COUNT(*) FROM funding_events WHERE exchange='aster' AND symbol='ABCUSDT'").fetchone()[0] == 9
    depth = db.leg_depths(con)
    assert set(depth) == set(legs) and all(abs(v - (t0 - 3 * 86400_000)) < 60_000 for v in depth.values())
    # пагинация Binance-подобной истории
    rows = exchanges.funding_history_since(w["aster"], "XYZUSDT", 0, None, page=10)
    assert len(rows) == 72 and rows[0]["funding_ms"] < rows[-1]["funding_ms"]
    calls_before = len(w["aster"].calls) + len(w["binance"].calls)
    st2 = funding.backfill(con, w, legs, iv, days=3)
    assert st2["new"] == 0 and db.count_funding_events(con) == n1            # ни одного дубля
    assert st2["skipped"] == len(legs)                                       # и ни одного лишнего вызова
    assert len(w["aster"].calls) + len(w["binance"].calls) == calls_before


def test_force_backfill_fetches_from_floor_and_marks_depth(tmp_path):
    """Нога, которую полнота считает мелкой, после одного прохода с force перестаёт ею быть — цикла нет.
    Нога без курсора непрерывности (прежняя версия) пересобирается с пола и без force — непрерывность доказывается."""
    con = _con(tmp_path); w = make_world()
    legs = [("binance", "BONLYUSDT")]; iv = {"binance": {"BONLYUSDT": 8}}
    funding.backfill(con, w, legs, iv, days=3)
    assert ("binance", "BONLYUSDT") in db.leg_sync(con)                      # сбор с пола ставит и курсор
    calls = len(w["binance"].calls)
    st = funding.backfill(con, w, legs, iv, days=3)                          # глубокая и курсор свежий → пропуск
    assert st["skipped"] == 1 and len(w["binance"].calls) == calls
    con.execute("UPDATE leg_depth SET synced_to=NULL"); con.commit()
    st = funding.backfill(con, w, legs, iv, days=3)                          # курсора нет → с пола, без дублей
    assert st["verified"] == 1 and st["new"] == 0 and ("binance", "BONLYUSDT") in db.leg_sync(con)
    con.execute("DELETE FROM leg_depth"); con.commit()
    st = funding.backfill(con, w, legs, iv, days=3, force=True)
    assert st["verified"] == 1 and st["new"] == 0 and ("binance", "BONLYUSDT") in db.leg_depths(con)


def test_backfill_hyperliquid_legs_including_hip3(tmp_path):
    con = _con(tmp_path); w = make_world()
    legs = [("hyperliquid", "ABC"), ("hyperliquid", "kPEPE"), ("hyperliquid", "xyz:NATGAS")]
    st = funding.backfill(con, w, legs, {"hyperliquid": {"ABC": 1, "kPEPE": 1, "xyz:NATGAS": 1}}, days=3)
    assert st["new"] == 216 and st["verified"] == 3 and st["errors"] == 0
    assert ("history", "xyz:NATGAS") in w["hyperliquid"].calls
    assert funding.incremental(con, w["hyperliquid"], {}) == 0              # пакетного досбора у Hyperliquid нет


def test_backfill_survives_exchange_error(tmp_path):
    con = _con(tmp_path); w = make_world()
    w["binance"].fail_paths.add("/fapi/v1/fundingRate")
    st = funding.backfill(con, w, [("aster", "ABCUSDT"), ("binance", "ABCUSDT")],
                          {"aster": {"ABCUSDT": 8}, "binance": {"ABCUSDT": 8}}, days=3)
    assert st["errors"] == 1 and st["new"] == 9 and st["verified"] == 1      # Aster собран, Binance — нет, цикл жив
    assert set(db.leg_depths(con)) == {("aster", "ABCUSDT")}                 # упавшая нога глубину НЕ получает


def test_incremental_no_symbol_takes_latest(tmp_path):
    con = _con(tmp_path); w = make_world()
    n = funding.incremental(con, w["aster"], {"ABCUSDT": 8})
    assert n > 0
    path, params = w["aster"].calls[-1]
    assert path == "/fapi/v1/fundingRate" and "symbol" not in params and params["limit"] == 1000
    assert funding.incremental(con, w["aster"], {}) == 0


def test_completeness_shallow_until_depth_verified():
    """Ревью 10.09: история короче окна без подтверждённой глубины — окно неполное, а не «сумма как есть»."""
    now = 1000 * H + 30 * 60_000
    leg = ("binance", "NEWUSDT")
    ev = {leg: [(1000 * H - k * H, 0.0001) for k in range(10)][::-1]}          # 10 часов почасовых расчётов
    iv = {"binance": {"NEWUSDT": 1}}
    g = funding.completeness(ev, [leg], iv, now, 720, None)[leg]
    assert g["shallow"] == [24, 72, 168, 720] and not g["latest_missing"] and g["first_ms"] == 991 * H
    g = funding.completeness(ev, [leg], iv, now, 720, None, depth={leg: now - 30 * 86400_000})[leg]
    assert g["shallow"] == []                                                  # биржу спросили с месяца назад — свежий листинг, не дыра
    g = funding.completeness(ev, [leg], iv, now, 720, None, depth={leg: now - 2 * 86400_000})[leg]
    assert g["shallow"] == [72, 168, 720]                                      # подтверждение позже начала окна окно не прикрывает


def test_interval_change_4h_to_1h_is_not_shallow():
    """Тестировщик 11.09 (Aster ZKCUSDT, Binance ONGUSDT): месяц назад интервал был 4 ч, теперь 1 ч. По часовому
    допуску в начале окна «не хватало» расчётов, которых не было, — нога навсегда считалась мелкой и блокировала добор."""
    now = 1000 * H + 30 * 60_000
    lo = now - 720 * H                                                         # 280.5 ч
    leg = ("aster", "ZKCUSDT")
    ev = {leg: [(ms, 0.0001) for ms in list(range(284 * H, 900 * H, 4 * H)) + list(range(900 * H, 1001 * H, H))]}
    assert ev[leg][0][0] > lo + 1 * H + 10 * 60_000                            # по старому допуску (1 ч) — «мелкая»
    g = funding.completeness(ev, [leg], {"aster": {"ZKCUSDT": 1}}, now, 720, None)[leg]
    assert g["shallow"] == []


def test_completeness_by_next_funding_time_and_repair(tmp_path):
    con = _con(tmp_path); w = make_world()
    now_ms = int(time.time() * 1000)
    legs = [("aster", "ABCUSDT"), ("binance", "ABCUSDT"), ("aster", "XYZUSDT"), ("binance", "XYZUSDT")]
    iv = {"aster": {"ABCUSDT": 8, "XYZUSDT": 1}, "binance": {"ABCUSDT": 8, "XYZUSDT": 4}}
    funding.backfill(con, w, legs, iv, days=3)
    ev = db.funding_events_since(con, now_ms - 49 * H)
    last = db.last_funding_ms(con, "aster", "XYZUSDT")
    nxt = {"aster": {"XYZUSDT": last + H, "ABCUSDT": db.last_funding_ms(con, "aster", "ABCUSDT") + 8 * H},
           "binance": {"XYZUSDT": db.last_funding_ms(con, "binance", "XYZUSDT") + 4 * H,
                       "ABCUSDT": db.last_funding_ms(con, "binance", "ABCUSDT") + 8 * H}}
    comp = funding.completeness(ev, legs, iv, now_ms, 48, nxt)
    assert all(not g["missing"] and not g["hole"] for g in comp.values())
    assert comp[("aster", "XYZUSDT")]["n"] == 48 and comp[("aster", "ABCUSDT")]["n"] == 6
    t_grace = last + 11 * 60_000                                             # льгота 10 мин после last прошла
    # интервал сменился на ходу (AWE 10.09): расчёты были ежечасные, теперь объявлено 4 ч и next = last + 4ч.
    nxt2 = dict(nxt); nxt2["aster"] = dict(nxt["aster"], XYZUSDT=last + 4 * H)
    iv2 = {"aster": {"ABCUSDT": 8, "XYZUSDT": 4}, "binance": iv["binance"]}
    g = funding.completeness(ev, legs, iv2, t_grace, 48, nxt2)[("aster", "XYZUSDT")]
    assert not g["latest_missing"] and not g["hole"] and g["n"] == 48
    # биржа расчитала новый час, а досбор его не увидел: удалим последний расчёт XYZ на Aster
    con.execute("DELETE FROM funding_events WHERE exchange='aster' AND symbol='XYZUSDT' AND funding_ms=?", (last,)); con.commit()
    ev = db.funding_events_since(con, now_ms - 49 * H)
    g = funding.completeness(ev, legs, iv, t_grace, 48, nxt)[("aster", "XYZUSDT")]
    assert g["latest_missing"] and g["missing"] == [last] and g["n"] == 47
    g = funding.completeness(ev, legs, iv, last + 5 * 60_000, 48, nxt)[("aster", "XYZUSDT")]
    assert not g["latest_missing"]                                           # в льготе — ещё не дыра
    # старая дыра внутри окна: вырежем 12 часов из XYZ на Binance (4ч-сетка → разрыв 16 ч > 8 ч)
    lb = db.last_funding_ms(con, "binance", "XYZUSDT")
    con.execute("DELETE FROM funding_events WHERE exchange='binance' AND symbol='XYZUSDT' AND funding_ms IN (?,?,?)",
                (lb - 4 * H, lb - 8 * H, lb - 12 * H)); con.commit()
    ev = db.funding_events_since(con, now_ms - 49 * H)
    comp = funding.completeness(ev, legs, iv, t_grace, 48, nxt)
    assert comp[("binance", "XYZUSDT")]["hole"] and not comp[("binance", "XYZUSDT")]["latest_missing"]
    # без nextFundingTime — по сетке объявленного интервала, только последняя точка
    g = funding.completeness(ev, legs, iv, t_grace, 48, None)[("aster", "XYZUSDT")]
    assert g["latest_missing"] and g["missing"] == [last]
    # добор идёт только за ПОСЛЕДНИМ расчётом; за старой дырой — нет
    calls = len(w["binance"].calls)
    n = funding.repair(con, w, comp, iv)
    assert n == 1 and db.last_funding_ms(con, "aster", "XYZUSDT") == last
    assert len(w["binance"].calls) == calls
