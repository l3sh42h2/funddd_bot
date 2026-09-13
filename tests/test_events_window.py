"""Окно событий в памяти (13.09, events_window): добор по rowid вместо перечитывания 30 сут, пересчёт в своём потоке.

Главное свойство — равенство до бита: после любой последовательности вставок (новые расчёты, бэкфилл старых строк не по
порядку, дубли), удалений и сдвигов часов вид окна (события, полнота, суммы окон) и строки ff/sf из него — те же, что у
полного пересчёта прежним путём (db.funding_events_since → funding.completeness → calc.window_sums).
Дальше — поток: тик не ждёт пересчёт, вид подставляется целиком, выданный вид не меняется на месте."""
import random, threading, time
import pytest
from funding_bot import calc, config, db, funding
from funding_bot.events_window import EventWindow, FULL_RELOAD_S
from fakes import make_world
from test_client_collector import _collector

H = 3600_000
WH = max(config.WINDOWS_H)


def canon(x):
    """До бита: float — по .hex(), словари — отсортированными парами, списки и кортежи различаются."""
    if isinstance(x, float):
        return ("f", x.hex())
    if isinstance(x, dict):
        return ("d", sorted(((canon(k), canon(v)) for k, v in x.items()), key=repr))
    if isinstance(x, (list, tuple)):
        return (type(x).__name__, [canon(v) for v in x])
    return x


def full_view(con, now_ms, legs, intervals, next_ms):
    """Прежний refresh_events: всё окно из БД, полнота и суммы заново."""
    ev = db.funding_events_since(con, now_ms - (WH + 1) * H)
    comp = funding.completeness(ev, legs, intervals, now_ms, WH, next_ms, depth=db.leg_depths(con), sync=db.leg_sync(con))
    ws = {k: calc.window_sums(v, calc.window_anchor(comp.get(k), now_ms)) for k, v in ev.items()}
    return ev, comp, ws


def table_rows(ev, comp, ws, legs, intervals, now_ms):
    """Строки ff (соседние ноги парами) и sf (каждая нога со спотом) из вида окна — как их строит коллектор."""
    ins = lambda k: {"interval_h": intervals[k[0]][k[1]]}
    out = []
    for a, b in zip(legs, legs[1:]):
        it = dict(key=f"{a}|{b}", base="X", va=a[0], vb=b[0], sa=a[1], sb=b[1])
        out.append(calc.build_ff_row(it, ins(a), ins(b), {"rate": 1e-4, "mark": 1.0}, {"rate": 2e-4, "mark": 1.0}, None, None,
                                     ws.get(a, {}), ws.get(b, {}), now_ms, comp.get(a), comp.get(b)))
    for a in legs:
        it = dict(key=f"s|{a}", base="X", spot="XUSDT", spot_ex="binance_spot", perp_ex=a[0], perp=a[1])
        out.append(calc.build_sf_row(it, ins(a), {"rate": 1e-4, "mark": 1.0}, None, {"bid": 1.0, "ask": 1.001},
                                     ws.get(a, {}), now_ms, comp.get(a)))
    return out


# --- маленький мир: ноги, интервалы, история с глубиной, дырами, без курсора --------------------------------------------
VEN = ("hyperliquid", "aster", "bitget", "gate")         # hyperliquid — неизменный интервал (FIXED_INTERVAL_H)


def small_world(con, rnd, now_ms, n=24):
    legs, iv, nxt = [], {}, {}
    for i in range(n):
        v = VEN[i % len(VEN)]
        h = 1 if v in config.FIXED_INTERVAL_H else rnd.choice([1, 1, 4, 8])
        legs.append((v, f"C{i}")); iv.setdefault(v, {})[f"C{i}"] = h
        nxt.setdefault(v, {})[f"C{i}"] = None
    rows = []
    for v, s in legs:
        step = iv[v][s] * H
        start = now_ms - rnd.choice([33 * 24, WH, WH - 3, 240, 48, 6]) * H
        top = now_ms - rnd.choice([0, 0, 1, 3]) * step
        times = list(range((start // step + 1) * step, top + 1, step))
        if len(times) > 12 and rnd.random() < 0.3:
            a = rnd.randrange(1, len(times) - 6); del times[a:a + rnd.randint(1, 4)]
        rows += [dict(exchange=v, symbol=s, funding_ms=t, rate=rnd.uniform(-1e-3, 1e-3)) for t in times]
        if rnd.random() < 0.8:
            db.set_leg_depth(con, v, s, start, synced_to=None if rnd.random() < 0.2 else (times[-1] if times else start))
    db.insert_funding_events(con, rows)
    return legs, iv, nxt


def mutate(con, rnd, legs, iv, nxt, now_ms) -> str:
    """Одно случайное изменение БД (или часов коллектора — возвращает сдвиг в имени шага)."""
    op = rnd.choice(["settle", "settle", "backfill", "dup", "delete", "tail", "tail_same", "depth", "next", "purge",
                     "vacuum", "idle"])
    one = lambda: rnd.choice(legs)
    if op == "settle":                                    # новые расчёты по сетке у части ног, часть отстаёт
        rows = []
        for v, s in legs:
            step = iv[v][s] * H
            for t in (now_ms // step * step, now_ms // step * step - step):
                if rnd.random() < 0.6:
                    rows.append(dict(exchange=v, symbol=s, funding_ms=t, rate=rnd.uniform(-1e-3, 1e-3)))
        rnd.shuffle(rows); db.insert_funding_events(con, rows)
    elif op == "backfill":                                # старые строки не по порядку, в том числе у края окна и за ним
        v, s = one(); step = iv[v][s] * H
        lo = now_ms - rnd.choice([35 * 24, WH + 2, WH, 100]) * H
        times = list(range((lo // step + 1) * step, now_ms - rnd.randint(0, 50) * H, step))
        rnd.shuffle(times)
        db.insert_funding_events(con, [dict(exchange=v, symbol=s, funding_ms=t, rate=rnd.uniform(-1e-3, 1e-3))
                                       for t in times[:rnd.randint(1, 200)]])
    elif op == "dup":                                     # дубли ключей с другой ставкой — INSERT OR IGNORE их не пускает
        got = con.execute("SELECT exchange,symbol,funding_ms FROM funding_events ORDER BY RANDOM() LIMIT 20").fetchall()
        db.insert_funding_events(con, [dict(exchange=e, symbol=s, funding_ms=m, rate=0.5) for e, s, m in got])
    elif op == "delete":                                  # удаление случайных строк (в окне и вне)
        con.execute("DELETE FROM funding_events WHERE rowid IN (SELECT rowid FROM funding_events ORDER BY RANDOM() LIMIT ?)",
                    (rnd.randint(1, 8),)); con.commit()
    elif op in ("tail", "tail_same"):                     # удалены последние по rowid; новые строки займут их rowid
        k = rnd.randint(1, 5)
        got = con.execute("SELECT exchange,symbol,funding_ms,rate FROM funding_events ORDER BY rowid DESC LIMIT ?", (k,)).fetchall()
        con.execute("DELETE FROM funding_events WHERE rowid IN (SELECT rowid FROM funding_events ORDER BY rowid DESC LIMIT ?)",
                    (k,)); con.commit()
        if op == "tail_same":                             # те же ключи в том же порядке — с другой ставкой
            db.insert_funding_events(con, [dict(exchange=e, symbol=s, funding_ms=m, rate=r + 1e-6) for e, s, m, r in reversed(got)])
        else:                                             # другие ключи на те же rowid (число строк то же)
            v, s = one(); step = iv[v][s] * H
            db.insert_funding_events(con, [dict(exchange=v, symbol=s, funding_ms=now_ms // step * step - j * step,
                                                rate=rnd.uniform(-1e-3, 1e-3)) for j in range(k)])
    elif op == "depth":                                   # курсоры и глубина ног
        for _ in range(3):
            v, s = one(); r = rnd.random()
            if r < 0.4:
                db.set_leg_depth(con, v, s, now_ms - rnd.randint(1, 800) * H, synced_to=now_ms - rnd.randint(0, 30) * H)
            elif r < 0.7:
                db.advance_sync(con, v, s, now_ms - rnd.randint(0, 3) * H)
            else:
                con.execute("DELETE FROM leg_depth WHERE exchange=? AND symbol=?", (v, s)); con.commit()
    elif op == "next":                                    # слово биржи о следующем расчёте появилось / пропало
        v, s = one(); step = iv[v][s] * H
        nxt[v][s] = None if nxt[v][s] else (now_ms // step + 1) * step
    elif op == "purge":                                   # чистка старых строк поперёк края окна
        con.execute("DELETE FROM funding_events WHERE funding_ms < ?", (now_ms - rnd.randint(700, 725) * H,)); con.commit()
    elif op == "vacuum":                                  # VACUUM перенумеровывает rowid таблицы без INTEGER PRIMARY KEY
        con.execute("VACUUM")
    return op


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_incremental_view_equals_full_recompute_bit_for_bit(tmp_path, seed):
    rnd = random.Random(seed)
    con = db.connect(tmp_path / "w.db")
    now_ms = 1_789_000_000_000 // H * H + 7 * 60_000
    legs, iv, nxt = small_world(con, rnd, now_ms)
    evw = EventWindow(con=con)
    why, prev, prev_c = [], None, None
    for step in range(90):
        op = mutate(con, rnd, legs, iv, nxt, now_ms)
        now_ms += rnd.choice([0, 60_000, 60_000, 7 * 60_000, 11 * 60_000, H, 3 * H, 13 * H, -5 * 60_000])
        view = evw.refresh(now_ms, legs, iv, nxt)
        why.append(view.full)
        ev, comp, ws = full_view(con, now_ms, legs, iv, nxt)
        assert canon(view.events) == canon(ev), (seed, step, op, view.full)
        assert canon(view.comp) == canon(comp), (seed, step, op, view.full)
        assert canon(view.wsums) == canon(ws), (seed, step, op, view.full)
        assert canon(table_rows(view.events, view.comp, view.wsums, legs, iv, now_ms)) == \
            canon(table_rows(ev, comp, ws, legs, iv, now_ms)), (seed, step, op)
        assert view.rows == sum(map(len, ev.values())) and view.legs == len(legs)
        if prev is not None:                              # выданный раньше вид не менялся на месте
            assert canon((prev.events, prev.comp, prev.wsums)) == prev_c, (seed, step, op)
        prev, prev_c = view, canon((view.events, view.comp, view.wsums))
    assert why.count(None) >= 30                          # путь добора действительно шёл
    assert "число строк" in why and "хвост rowid" in why


def test_full_reload_on_count_mismatch_tail_reuse_and_every_30_min(tmp_path):
    con = db.connect(tmp_path / "f.db")
    rnd = random.Random(9)
    now = 1_789_000_000_000 // H * H + 20 * 60_000
    legs, iv, nxt = small_world(con, rnd, now, n=8)
    evw = EventWindow(con=con)
    check = lambda v: canon((v.events, v.comp, v.wsums)) == canon(full_view(con, v.now_ms, legs, iv, nxt))
    v = evw.refresh(now, legs, iv, nxt)
    assert v.full == "старт" and check(v)
    db.insert_funding_events(con, [dict(exchange=l[0], symbol=l[1], funding_ms=now - 3 * 60_000, rate=1e-4) for l in legs[:3]])
    v = evw.refresh(now + 60_000, legs, iv, nxt)
    assert v.full is None and v.new == 3 and check(v)
    # удаление строки в глубине (не в хвосте по rowid): число строк окна разошлось → полная перезагрузка
    con.execute("DELETE FROM funding_events WHERE rowid = (SELECT MIN(rowid) FROM funding_events WHERE funding_ms >= ?)",
                (now - 100 * H,)); con.commit()
    v = evw.refresh(now + 120_000, legs, iv, nxt)
    assert v.full == "число строк" and check(v)
    # чистка поперёк края окна
    con.execute("DELETE FROM funding_events WHERE funding_ms < ?", (now - 700 * H,)); con.commit()
    v = evw.refresh(now + 180_000, legs, iv, nxt)
    assert v.full == "число строк" and check(v)
    # удалена последняя строка, новая заняла её rowid: число строк то же — без сверки хвоста расхождение прошло бы молча
    n0 = db.count_funding_events_since(con, 0); top = db.funding_rowid_max(con)
    con.execute("DELETE FROM funding_events WHERE rowid=?", (top,)); con.commit()
    db.insert_funding_events(con, [dict(exchange=legs[5][0], symbol=legs[5][1], funding_ms=now - 11 * 60_000, rate=3e-4)])
    assert db.funding_rowid_max(con) == top and db.count_funding_events_since(con, 0) == n0
    v = evw.refresh(now + 240_000, legs, iv, nxt)
    assert v.full == "хвост rowid" and check(v)
    # раз в 30 мин по часам коллектора — даже без изменений; раньше — добор
    t_full = now + 240_000
    v = evw.refresh(t_full + FULL_RELOAD_S * 1000 - 60_000, legs, iv, nxt)
    assert v.full is None and check(v)
    v = evw.refresh(t_full + FULL_RELOAD_S * 1000, legs, iv, nxt)
    assert v.full == "30 мин" and check(v)
    # новый процесс — новое окно: с нуля
    assert EventWindow(con=con).refresh(t_full + FULL_RELOAD_S * 1000, legs, iv, nxt).full == "старт"


def test_window_sums_recomputed_only_where_rows_or_bounds_moved(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "s.db")
    now = 1_789_000_000_000 // H * H + 30 * 60_000             # середина часа: минута вперёд границ не двигает
    legs, iv, nxt = small_world(con, random.Random(4), now, n=12)
    evw = EventWindow(con=con)
    evw.refresh(now, legs, iv, nxt)
    calls, orig = [], calc.window_sums
    monkeypatch.setattr(calc, "window_sums", lambda evs, a: calls.append(1) or orig(evs, a))
    v = evw.refresh(now + 60_000, legs, iv, nxt)
    assert v.full is None and not calls                        # ни ряд, ни граница не сдвинулись — суммы прежние
    db.insert_funding_events(con, [dict(exchange=legs[0][0], symbol=legs[0][1], funding_ms=now - 60_000, rate=1e-4)])
    v = evw.refresh(now + 120_000, legs, iv, nxt)
    assert len(calls) == 1                                     # только тронутая нога
    assert canon(v.wsums) == canon(full_view(con, now + 120_000, legs, iv, nxt)[2])


# --- коллектор: вид из окна = полный пересчёт, и строки таблицы те же -------------------------------------------------
def test_collector_view_and_table_equal_full_recompute(tmp_path):
    w = make_world(); col, t = _collector(tmp_path, w)
    col.once()
    rnd = random.Random(11)
    legs = col.legs()
    for step in range(30):
        r = rnd.random()
        if r < 0.25:
            t[0] += rnd.choice([10, 600, 3600]); col.once()          # настоящий проход: досбор, полнота, добор
        elif r < 0.5:
            rows = []
            for v, s in rnd.sample(legs, 5):
                base = int(t[0] * 1000) // H * H - rnd.randint(0, 40 * 24) * H
                rows += [dict(exchange=v, symbol=s, funding_ms=base - j * H, rate=rnd.uniform(-1e-3, 1e-3)) for j in range(3)]
            rnd.shuffle(rows); db.insert_funding_events(col.con, rows)
        elif r < 0.7:
            col.con.execute("DELETE FROM funding_events WHERE rowid IN (SELECT rowid FROM funding_events ORDER BY RANDOM() LIMIT 3)")
            col.con.commit()
        else:
            t[0] += rnd.choice([30, 61, 900])
        col._events_dirty = True
        col.refresh_events()
        ev, comp, ws = full_view(col.con, **col._events_input())
        assert canon((col.events, col.comp, col.wsums)) == canon((ev, comp, ws)), step
        tbl = col.build_table()
        mine = (col.events, col.comp, col.wsums)
        col.events, col.comp, col.wsums = ev, comp, ws
        ref = col.build_table()
        col.events, col.comp, col.wsums = mine
        assert canon(tbl["ff_rows"]) == canon(ref["ff_rows"]) and canon(tbl["sf_rows"]) == canon(ref["sf_rows"]), step
        assert tbl["n_incomplete_ff"] == ref["n_incomplete_ff"] and tbl["n_incomplete_sf"] == ref["n_incomplete_sf"]


# --- поток «events» -----------------------------------------------------------------------------------------------------
def _settled(col, timeout=20.0):
    """Дождаться фонового обслуживания и пересчёта окна; подставить готовый вид."""
    end = time.time() + timeout
    while time.time() < end:
        col._take_events()
        if not col._maint_running and col._ev_job is None and not col._evw.busy() and col._ev_ready is None:
            return
        time.sleep(0.02)
    raise AssertionError("фон не закончил")


def _fresh_installed(col, t):
    """Вид окна на текущий момент — пересчёт потоком «events» и подстановка."""
    t[0] += 61; col._events_dirty = True
    col.refresh_events()
    col._ev_job.result(timeout=10)
    col.refresh_events()


def test_tick_never_waits_for_events_worker(tmp_path):
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once()                                                  # первый вид процесса — единственное ожидание
    assert col._ev_legs == len(col.legs())
    _settled(col)
    gen0, comp0 = col._ev_gen, col.comp
    orig, calls, release = col._evw.refresh, [], threading.Event()

    def slow(**kw):
        calls.append(threading.current_thread().name)
        release.wait(10)
        return orig(**kw)
    col._evw.refresh = slow
    t0 = time.perf_counter()
    col._events_dirty = True
    col.refresh_events()
    job = col._ev_job
    assert job is not None and not job.done()
    for _ in range(5):                                          # пересчёт уже идёт: второй не запускается, тик не ждёт
        t[0] += 61; col._events_dirty = True
        col.refresh_events()
        tbl = col.once()
    dt = time.perf_counter() - t0
    assert dt < 1.5 and not job.done(), dt
    assert col._ev_job is job and [c for c in calls if c.startswith("events")] == ["events_0"]
    assert col._ev_gen == gen0 and col.comp is comp0 and tbl["n_ff"] == 13     # таблица идёт на прежнем виде
    assert col._events_dirty                                    # «грязно» ждёт следующего пересчёта
    release.set()
    job.result(timeout=10)
    col.refresh_events()
    assert col._ev_gen > gen0 and col.comp is not comp0
    col._evw.refresh = orig
    _settled(col)


def test_view_swap_is_atomic_and_old_view_is_untouched(tmp_path, monkeypatch):
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once(); _settled(col); _fresh_installed(col, t)
    old = (col.events, col.comp, col.wsums)
    saved = canon(old)
    now_ms = int(t[0] * 1000)
    legs = col.legs()
    rows = [dict(exchange=v, symbol=s, funding_ms=now_ms // H * H + H, rate=2e-4) for v, s in legs]        # новые расчёты
    rows += [dict(exchange=v, symbol=s, funding_ms=now_ms - 700 * H + 1, rate=-3e-4) for v, s in legs[:4]]   # бэкфилл
    db.insert_funding_events(col.con, rows)
    gate, orig = threading.Event(), funding.completeness

    def slow(*a, **k):
        gate.wait(10)
        return orig(*a, **k)
    monkeypatch.setattr(funding, "completeness", slow)
    t[0] += 3 * 3600; col._events_dirty = True
    col.refresh_events()
    seen = set()
    for _ in range(40):                                         # вид собирается — главный поток видит только прежний
        col._take_events()
        seen.add((id(col.events), id(col.comp), id(col.wsums)))
        assert col._ev_ready is None
        time.sleep(0.005)
    assert seen == {tuple(map(id, old))}
    gate.set()
    col._ev_job.result(timeout=10)
    col.refresh_events()
    new = (col.events, col.comp, col.wsums)
    assert all(a is not b for a, b in zip(old, new))
    assert canon(old) == saved                                  # прежние ряды и словари не тронуты
    assert set(new[0]) == set(new[2]) and set(new[1]) == set(legs)
    assert canon(new) == canon(full_view(col.con, **col._events_input()))
    _settled(col)


def test_background_completeness_decides_on_fresh_view(tmp_path):
    """Проверка полноты в фоне строит очередь по окну, пересчитанному в самом задании: подставленный вид устарел (удаление
    после него), а добор всё равно идёт."""
    w = make_world(); col, t = _collector(tmp_path, w, background=True)
    col.once(); _settled(col); _fresh_installed(col, t)
    leg = ("hyperliquid", "ABC")
    hl_last = db.last_funding_ms(col.con, *leg)
    col.con.execute("DELETE FROM funding_events WHERE exchange=? AND symbol=? AND funding_ms IN (?, ?)", (*leg, hl_last, hl_last - 5 * H))
    col.con.commit()
    assert not col.comp[leg]["hole"] and not col.comp[leg]["latest_missing"]      # вид на странице об удалении не знает
    t[0] += config.FUNDING_INCR_S
    t0 = time.perf_counter()
    col.step_completeness()
    assert time.perf_counter() - t0 < 0.5                       # главный поток не ждёт ни окно, ни добор
    _settled(col)
    have = {ms for (ms,) in col.con.execute("SELECT funding_ms FROM funding_events WHERE exchange=? AND symbol=?", leg)}
    assert hl_last in have and hl_last - 5 * H in have


def test_first_view_of_process_has_windows(tmp_path):
    """Рестарт на готовой БД: первая же таблица нового процесса — с окнами (первый вид ждём), дальше тик не ждёт."""
    w = make_world(); pre, t = _collector(tmp_path, w)
    pre.once()
    col, t2 = _collector(tmp_path, make_world(), now=t[0], background=True)
    tbl = col.once()
    assert col._ev_legs == len(col.legs())
    assert any(r["windows"]["24"]["n"] > 0 for r in tbl["sf_rows"])
    _settled(col)
