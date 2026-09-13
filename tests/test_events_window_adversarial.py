"""Состязательная проверка окна событий (13.09): попытки развести добор в памяти с полным пересчётом и сломать поток «events».

Равенство до бита — против прежнего пути (db.funding_events_since → funding.completeness → calc.window_sums), как в
test_events_window. Здесь — сценарии, которых там нет по отдельности: бэкфилл целой ноги одним пакетом, новая и
делистнутая нога, смена интервала, часы поперёк края окна и отложенного расчёта без новых строк (плановый досбор
CATCHUP_S), запись другого соединения посреди чтения (снимок WAL), падение посреди добора, уход строк из памяти; и в
коллекторе — зависший и падающий поток, первый вид при постоянной ошибке, чистка с чекпойнтом WAL поперёк чтения
потока, порядок подстановки видов."""
import random, sqlite3, threading, time
import pytest
from funding_bot import calc, collector as collector_mod, config, db, funding
from funding_bot.events_window import EventWindow
from fakes import make_world
from test_client_collector import _collector
from test_events_window import canon, full_view, small_world, table_rows, _settled, _fresh_installed

H = 3600_000
WH = max(config.WINDOWS_H)
NOW0 = 1_789_000_000_000 // H * H + 7 * 60_000


def _same(view, con, now_ms, legs, iv, nxt):
    ev, comp, ws = full_view(con, now_ms, legs, iv, nxt)
    assert canon(view.events) == canon(ev)
    assert canon(view.comp) == canon(comp)
    assert canon(view.wsums) == canon(ws)
    assert canon(table_rows(view.events, view.comp, view.wsums, legs, iv, now_ms)) == \
        canon(table_rows(ev, comp, ws, legs, iv, now_ms))
    assert view.rows == sum(map(len, ev.values()))


# --- окно событий: сценарии ---------------------------------------------------------------------------------------------
def test_burst_backfill_new_leg_delisting_and_interval_change(tmp_path):
    con = db.connect(tmp_path / "b.db")
    rnd = random.Random(21)
    now = NOW0
    legs, iv, nxt = small_world(con, rnd, now, n=12)
    evw = EventWindow(con=con, full_reload_s=10**9)                    # только добор: 30 мин не маскирует расхождение
    _same(evw.refresh(now, legs, iv, nxt), con, now, legs, iv, nxt)
    # 1) новая нога: 30 сут + 3 сут за краем окна одним пакетом, вразброс; курсор и глубина — сразу
    new = ("aster", "NEWUSDT"); iv["aster"]["NEWUSDT"] = 1; nxt["aster"]["NEWUSDT"] = None
    times = list(range((now - 33 * 24 * H) // H * H, now - 10 * 60_000, H)); rnd.shuffle(times)
    db.insert_funding_events(con, [dict(exchange=new[0], symbol=new[1], funding_ms=t, rate=rnd.uniform(-1e-3, 1e-3))
                                   for t in times])
    db.set_leg_depth(con, *new, now - 30 * 86400_000, synced_to=now - 11 * 60_000)
    legs2 = legs + [new]
    now += 60_000
    v = evw.refresh(now, legs2, iv, nxt)
    # весь пакет — добором, без полной перезагрузки; строки старше края окна добор не берёт
    assert v.full is None and v.new == sum(1 for x in times if x >= now - (WH + 1) * H)
    _same(v, con, now, legs2, iv, nxt)
    # 2) мелкая нога добирается с пола целиком (старые строки перед имеющимися, вразброс)
    shallow = min(legs, key=lambda k: len(v.events.get(k, [])) or 10**9)
    step = iv[shallow[0]][shallow[1]] * H
    times = list(range((now - 30 * 86400_000) // step * step, now - 50 * H, step)); rnd.shuffle(times)
    db.insert_funding_events(con, [dict(exchange=shallow[0], symbol=shallow[1], funding_ms=t, rate=rnd.uniform(-1e-3, 1e-3))
                                   for t in times])
    now += 60_000
    v = evw.refresh(now, legs2, iv, nxt)
    assert v.full is None
    _same(v, con, now, legs2, iv, nxt)
    # 3) делистинг: нога ушла из вселенной, её строки остаются в окне, пока не состарятся (как прежде)
    gone = legs2[0]
    legs3 = legs2[1:]
    now += 60_000
    v = evw.refresh(now, legs3, iv, nxt)
    assert gone not in v.comp and gone in v.events
    _same(v, con, now, legs3, iv, nxt)
    # 4) смена интервала 4 ч → 1 ч на ходу: часовые расчёты после смены, объявленный интервал сменился
    ch = next(k for k in legs3 if k[0] != "hyperliquid" and iv[k[0]][k[1]] != 1)
    iv[ch[0]][ch[1]] = 1
    top = now // H * H
    db.insert_funding_events(con, [dict(exchange=ch[0], symbol=ch[1], funding_ms=top - j * H, rate=1e-4) for j in range(3)])
    for dt in (60_000, 11 * 60_000, H):
        now += dt
        v = evw.refresh(now, legs3, iv, nxt)
        assert v.full is None
        _same(v, con, now, legs3, iv, nxt)


def test_clock_sweep_across_window_edges_pending_and_catchup_without_new_rows(tmp_path, monkeypatch):
    """Строк не прибавляется: часы идут по минуте поперёк часовых расчётов (слово биржи next_ms двигается, как на бирже),
    краёв окон, льготы, CATCHUP_S. Полнота и суммы обязаны меняться только от часов — и совпадать с полным пересчётом."""
    con = db.connect(tmp_path / "c.db")
    rnd = random.Random(5)
    now = NOW0 - 7 * 60_000 + 50 * 60_000                             # hh:50
    legs, iv, nxt = small_world(con, rnd, now, n=16)
    evw = EventWindow(con=con, full_reload_s=10**9)                    # только добор: полная перезагрузка не маскирует
    calls, orig = [], calc.window_sums
    monkeypatch.setattr(calc, "window_sums", lambda evs, a: calls.append(1) or orig(evs, a))
    seen = set()
    for i in range(160):
        now += rnd.choice([60_000, 60_000, 60_000, 3 * 60_000, 7 * 60_000])
        for v, s in legs:                                              # слово биржи: следующий расчёт по сетке
            step = iv[v][s] * H
            nxt[v][s] = (now // step + 1) * step if (hash((v, s)) % 3) else None
        n0 = len(calls)
        view = evw.refresh(now, legs, iv, nxt)
        n_sums = len(calls) - n0                                       # до _same: полный пересчёт зовёт window_sums сам
        assert view.full in (None, "старт")
        _same(view, con, now, legs, iv, nxt)
        for g in view.comp.values():
            seen.add(("pending", g["pending_ms"] is not None))
            seen.add(("catchup", g["latest_missing"], g["catchup"]))
        seen.add(("sums", "reused" if n_sums < len(view.events) else "all"))
        seen.add(("sums", "some" if n_sums else "none"))
    # сценарий действительно прошёл: отложенный расчёт, плановый досбор и его истечение, суммы и из кэша, и заново
    assert {("pending", True), ("pending", False), ("catchup", True, True), ("catchup", True, False),
            ("sums", "reused"), ("sums", "some")} <= seen, seen


def test_write_from_other_connection_mid_read_sees_one_snapshot(tmp_path, monkeypatch):
    """Чтения пересчёта — один снимок WAL. Запись другого соединения посреди загрузки/добора (новый расчёт + продление
    курсора той же ноги) не должна попасть в вид наполовину: вид = полный пересчёт по БД ДО записи, следующий — ПОСЛЕ."""
    p = tmp_path / "s.db"
    w = db.connect(p)
    rnd = random.Random(8)
    now = NOW0
    legs, iv, nxt = small_world(w, rnd, now, n=10)
    evw = EventWindow(path=p)                                          # своё соединение, как у потока «events»
    leg = next(k for k in legs if iv[k[0]][k[1]] == 1)
    other = next(k for k in legs if k != leg)
    k_ins = [0]

    def write():
        k_ins[0] += 1
        db.insert_funding_events(w, [dict(exchange=leg[0], symbol=leg[1], funding_ms=now // H * H + k_ins[0] * H, rate=7e-4)])
        db.set_leg_depth(w, *leg, now - 40 * 86400_000, synced_to=now + k_ins[0] * H)

    for target in ("funding_events_since", "funding_rows_after", "count_funding_events_since"):
        orig, fired = getattr(db, target), []
        # у добора должно быть что брать: иначе rows_after не зовётся вовсе
        db.insert_funding_events(w, [dict(exchange=other[0], symbol=other[1], funding_ms=now - 17 * 60_000 - k_ins[0], rate=1e-5)])

        def hooked(*a, _o=orig, **k):
            if not fired:
                fired.append(1); write()
            return _o(*a, **k)
        ref = full_view(w, now, legs, iv, nxt)                         # БД до записи
        monkeypatch.setattr(db, target, hooked)
        v = evw.refresh(now, legs, iv, nxt)
        monkeypatch.setattr(db, target, orig)
        assert fired, target
        assert canon((v.events, v.comp, v.wsums)) == canon(ref), target
        if target != "funding_events_since":
            assert v.full is None, (target, v.full)                    # снимок: число строк сошлось, лишней перезагрузки нет
        now += 60_000
        v = evw.refresh(now, legs, iv, nxt)                            # следующий — видит запись целиком
        _same(v, w, now, legs, iv, nxt)
        now += H
        if target == "funding_events_since":
            evw.full_reload_s = 10**9


def test_failure_mid_advance_resets_and_next_view_is_exact(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "e.db")
    rnd = random.Random(3)
    now = NOW0
    legs, iv, nxt = small_world(con, rnd, now, n=10)
    evw = EventWindow(con=con)
    evw.refresh(now, legs, iv, nxt)
    db.insert_funding_events(con, [dict(exchange=v, symbol=s, funding_ms=now // H * H + H, rate=1e-4) for v, s in legs])
    orig = db.count_funding_events_since
    monkeypatch.setattr(db, "count_funding_events_since", lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    with pytest.raises(sqlite3.OperationalError):
        evw.refresh(now + 60_000, legs, iv, nxt)                       # ряды уже влиты (добор), проверка числа упала
    assert evw.rowid is None and not evw.lists
    monkeypatch.setattr(db, "count_funding_events_since", orig)
    v = evw.refresh(now + 60_000, legs, iv, nxt)
    assert v.full == "старт"
    _same(v, con, now + 60_000, legs, iv, nxt)
    assert not con.in_transaction                                      # транзакция чтения закрыта и при падении


def test_memory_rows_leave_window_and_dead_legs_vanish(tmp_path):
    """34 сут часов по часу: живые ноги получают расчёты, часть ног делистнута на 3-и сутки. Строки, вышедшие за край,
    уходят из памяти; нога без строк в окне исчезает из рядов и из кэша сумм; память = COUNT(*) окна."""
    con = db.connect(tmp_path / "m.db")
    now = NOW0
    legs = [("aster", f"M{i}") for i in range(8)]
    iv = {"aster": {s: 1 for _, s in legs}}
    nxt = {"aster": {s: None for _, s in legs}}
    evw = EventWindow(con=con, full_reload_s=10**9)
    longest = 0
    for h in range(34 * 24):
        now += H
        live = legs if h < 48 else legs[:5]
        db.insert_funding_events(con, [dict(exchange=v, symbol=s, funding_ms=now // H * H, rate=1e-5 * h) for v, s in live])
        view = evw.refresh(now, legs, iv, nxt)
        assert view.full in (None, "старт")
        assert evw.n == db.count_funding_events_since(con, now - (WH + 1) * H)
        assert set(evw._ws) == set(evw.lists)
        longest = max(longest, max(map(len, evw.lists.values())))
        if h % 97 == 0:
            _same(view, con, now, legs, iv, nxt)
    assert longest <= WH + 1
    assert set(evw.lists) == set(legs[:5])                             # делистнутые ушли целиком
    _same(view, con, now, legs, iv, nxt)


# --- коллектор: поток «events» ------------------------------------------------------------------------------------------
def test_hung_worker_is_noted_as_stale_not_silent(tmp_path):
    """Пересчёт повис: тик идёт на прежнем виде, но не молча — после EVENTS_STALE_S на странице заметка «events» с возрастом
    вида; пересчёт закончился — вид подставлен, заметка снята."""
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once(); _settled(col); _fresh_installed(col, t); _settled(col)
    comp0 = col.comp
    orig, gate = col._evw.refresh, threading.Event()

    def hung(**kw):
        gate.wait(30)
        return orig(**kw)
    col._evw.refresh = hung
    try:
        tbl = None
        for _ in range(14):                                            # 14 × 30 с = 7 мин по часам коллектора
            t[0] += 30; col._events_dirty = True
            tbl = col.once()
        assert col.comp is comp0                                       # последний годный вид — на месте
        note = tbl["notes"].get("events")
        assert note and "не обновлял" in note and note.split(":")[0] == "TimeoutError", tbl["notes"]
    finally:
        gate.set()
    col._evw.refresh = orig
    _settled(col)
    t[0] += 61; col._events_dirty = True
    col.refresh_events(); col._ev_job and col._ev_job.result(timeout=10)
    tbl = col.once()
    assert "events" not in tbl["notes"], tbl["notes"]
    _settled(col)


def test_worker_exception_keeps_last_view_notes_error_and_recovers(tmp_path):
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once(); _settled(col); _fresh_installed(col, t); _settled(col)
    comp0, ws0 = col.comp, col.wsums
    orig = col._evw.refresh

    def bad(**kw):
        raise MemoryError()                                            # урок 24.08: пустой текст — тип обязателен
    col._evw.refresh = bad
    t[0] += 61; col._events_dirty = True
    col.refresh_events(); col._ev_job.exception(timeout=10)
    tbl = col.once()
    assert col.comp is comp0 and col.wsums is ws0
    assert tbl["notes"].get("events", "").startswith("MemoryError"), tbl["notes"]
    assert any(r["windows"]["24"]["n"] for r in tbl["sf_rows"])        # таблица — на прежнем виде, не пустая
    col._evw.refresh = orig
    _settled(col)
    _fresh_installed(col, t)
    assert "events" not in col.notes
    assert canon((col.events, col.comp, col.wsums)) == canon(full_view(col.con, **col._events_input()))
    _settled(col)


def test_first_view_failure_does_not_block_every_tick(tmp_path):
    """Рестарт на готовой БД, а пересчёт падает (MemoryError на полной загрузке, битая БД): ждать первый вид на главном
    потоке — один раз. Дальше тик не ждёт: иначе КАЖДЫЙ тик шла бы полная перезагрузка на главном потоке (на ireland
    11–22 с), а не раз в минуту в фоне."""
    w = make_world(); pre, t = _collector(tmp_path, w)
    pre.once()
    col, t2 = _collector(tmp_path, make_world(), now=t[0], background=True)
    where = []

    def failing(**kw):
        where.append(threading.current_thread().name)
        time.sleep(0.3)
        raise RuntimeError("битая БД")
    col._evw.refresh = failing
    col.once()
    slow = []
    for _ in range(6):
        t2[0] += config.TICK_S
        a = time.perf_counter(); tbl = col.once(); slow.append(time.perf_counter() - a)
    assert where.count("MainThread") <= 1, where
    assert max(slow) < 0.25, slow
    assert tbl["notes"].get("events", "").startswith("RuntimeError"), tbl["notes"]
    col._evw.refresh = lambda **kw: (_ for _ in ()).throw(RuntimeError("стоп"))
    _settled(col)


def test_purge_checkpoint_does_not_stall_tick_behind_worker_read(tmp_path, monkeypatch):
    """Чистка снимков раз в сутки — с чекпойнтом WAL(TRUNCATE) на главном потоке. Он ждёт (busy timeout, в бою 30 с), пока
    читатели не отпустят WAL, а поток «events» держит транзакцию чтения всю полную перезагрузку (на ireland 11–22 с).
    Раньше чтение шло на том же потоке и пересечься с чисткой не могло. Тик не должен вставать: чистка — когда окно
    свободно, иначе в следующем тике."""
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once(); _settled(col); _fresh_installed(col, t); _settled(col)
    col.con.execute("PRAGMA busy_timeout=1500")                        # в бою 30 000
    col._evw.full_reload_s = 0                                         # следующий пересчёт — полная загрузка
    orig, entered, gate = db.funding_events_since, threading.Event(), threading.Event()

    def held(con, since):
        out = orig(con, since)
        entered.set(); gate.wait(10)                                   # транзакция чтения ещё открыта
        return out
    monkeypatch.setattr(db, "funding_events_since", held)
    t[0] += 61; col._events_dirty = True
    col.refresh_events()
    assert entered.wait(5)
    db.insert_funding_events(col.con, [dict(exchange="aster", symbol="ABCUSDT", funding_ms=int(t[0] * 1000) - 5, rate=1e-4)])
    col.last["purge"] = t[0] - 2 * 86400
    t[0] += config.TICK_S
    a = time.perf_counter(); col.once(); dt = time.perf_counter() - a
    gate.set()
    try:
        assert dt < 1.0, dt
        assert col.last["purge"] < t[0] - 86400                        # отложена, а не пропущена
    finally:
        monkeypatch.setattr(db, "funding_events_since", orig)
        _settled(col)
    t[0] += config.TICK_S
    col.once()
    assert col.last["purge"] == t[0]                                   # окно свободно — чистка прошла
    _settled(col)


def test_older_view_never_replaces_newer(tmp_path):
    """Порядок подстановки: вид с меньшим номером (задание обслуживания досчитало позже потока) не затирает новый."""
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once(); _settled(col)
    inp = col._events_input()
    v1 = col._evw.refresh(**inp)
    t[0] += 60; inp2 = col._events_input()
    v2 = col._evw.refresh(**inp2)
    with col._ev_lock:
        col._ev_ready = v2
    col._take_events()
    assert col._ev_gen == v2.gen and col.comp is v2.comp
    with col._ev_lock:
        col._ev_ready = v1                                             # опоздавший старый
    col._take_events()
    assert col._ev_gen == v2.gen and col.comp is v2.comp
    _settled(col)
