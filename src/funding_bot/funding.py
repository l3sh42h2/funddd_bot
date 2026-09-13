"""История фандинга: бэкфилл, досбор, проверка полноты, обслуживание.

Нога = (площадка, символ перпа). У каждой ноги в leg_depth два числа:
  checked_from — «спросили биржу с этого момента, всё, что было, лежит в БД» (подтверждённая глубина);
  synced_to    — курсор непрерывности: на отрезке [checked_from, synced_to] история в БД ПОЛНАЯ.
Курсор двигается только тем сбором, который продолжает отрезок без разрыва: сбором с пола, сбором с synced_to+1 и
пакетом «последние расчёты по всем монетам» (Binance ~7 ч, Aster ~3 ч; проверено 11.09: пакет содержит все
символы, включая TradFi) — для ног, чей курсор уже внутри окна пакета. Пакет «последний расчёт каждого контракта»
(edgeX) несёт у строки prev_ms — прошлый расчёт контракта: курсор, покрывший его, продлевается через новый расчёт (ревью
13.09; окно такого пакета — один интервал на каждый контракт, а не общее). Курсор всегда ставится на «сейчас минус
льгота»: расчёт может появиться в истории с задержкой, и следующий сбор перекрывает льготу.

Ревью 11.09 (внешнее, п.1): раньше добор после перерыва тянул историю только с последнего ожидаемого расчёта —
три пропущенных часа Hyperliquid превращались в один, 22 расчёта из 24 выдавались за полную сутку. Теперь добор
идёт с курсора (или, у ног прежней версии без курсора, с последнего сохранённого расчёта) — весь пропуск целиком.

Полнота (для дашборда и обслуживания):
  latest_missing — последний расчёт по слову биржи (nextFundingTime − интервал) не покрыт курсором → добор;
  hole           — только у ног без курсора: разрыв между соседними расчётами больше max(8 ч, 2×интервал) + льгота,
                   у площадок с неизменным интервалом (Hyperliquid: 1 ч) — больше интервала + льгота;
  shallow        — история начинается позже начала окна, а глубина не подтверждена → бэкфилл;
  legacy         — нога без курсора: один раз пересобирается с пола, чтобы непрерывность стала доказанной.
Допуск на начало окна — самый длинный интервал (8 ч): у Aster ZKCUSDT интервал сменился 4 ч → 1 ч, и по часовому
допуску месяц назад «не хватало» расчётов, которых не было.
"""
from __future__ import annotations
import time, logging, threading
from concurrent.futures import ThreadPoolExecutor
from . import config, db, venues
from .client import BudgetExceeded, BannedError

log = logging.getLogger(__name__)

H_MS = 3600_000
MAX_INTERVAL_H = 8


def _grace_ms() -> int:
    return config.SETTLE_GRACE_S * 1000


def depth_tolerance_ms(iv_h: int) -> int:
    return max(int(iv_h), MAX_INTERVAL_H) * H_MS + _grace_ms()


def expected_settlements(interval_h: int, now_ms: int, window_h: float, grace_s: int = config.SETTLE_GRACE_S) -> list[int]:
    """Моменты расчёта в окне (now − window, now − grace], кратные интервалу от эпохи UTC.
    Все площадки считают на круглых часах (проверено: у всех 740 символов Aster минуты = 0)."""
    step = int(interval_h) * H_MS
    if step <= 0:
        return []
    hi = now_ms - grace_s * 1000
    lo = now_ms - int(window_h * H_MS)
    first = (lo // step + 1) * step        # строго больше lo
    return list(range(first, hi + 1, step))


def legs_of(ff: list[dict], sf: list[dict] | None = None) -> list[tuple[str, str]]:
    """Все перпы, которым нужна история: обе ноги каждой пары + перп каждой сделки.
    Без дублей и в стабильном порядке (так бэкфилл после рестарта идёт тем же путём)."""
    seen: set[tuple[str, str]] = set(); out = []

    def add(leg):
        if leg[1] and leg not in seen:
            seen.add(leg); out.append(leg)
    for it in ff:
        add((it["va"], it["sa"])); add((it["vb"], it["sb"]))
    for it in sf or []:
        add((it["perp_ex"], it["perp"]))
    return out


def sync_leg(con, cl, ex: str, sym: str, since: int, now_ms: int, iv_map: dict | None, floor_ms: int,
             synced: int | None) -> int:
    """Один сбор истории ноги с `since` до сейчас. Двигает подтверждения: сбор с пола → глубина = пол и курсор;
    сбор, продолжающий курсор без разрыва, → только курсор. Возвращает число новых строк."""
    rows = venues.history_since(cl, sym, since, now_ms)
    n = db.insert_funding_events(con, rows, iv_map)
    # биржа отдаёт историю по порядку: всё, что раньше последнего полученного расчёта, уже опубликовано;
    # позже — только то, что старше льготы (свежий расчёт может лечь в историю с задержкой)
    cursor = max([now_ms - _grace_ms()] + [r["funding_ms"] for r in rows])
    if since <= floor_ms:
        db.set_leg_depth(con, ex, sym, floor_ms, synced_to=cursor)
    elif synced is not None and since <= synced + 1:
        db.advance_sync(con, ex, sym, cursor)
    return n


def backfill(con, clients: dict, legs: list[tuple[str, str]], intervals: dict[str, dict[str, int]],
             days: int = config.HISTORY_DAYS, stop=None, progress=None, force: bool = False, report=None) -> dict:
    """Посимвольный сбор глубины `days`. Идемпотентен: повторный запуск не вставляет дублей.

    force=False: пропускает ноги с подтверждённой глубиной и свежим курсором; ноги без курсора собирает с пола.
    force=True (зовёт обслуживание для «мелких» ног и ног без курсора): всегда с пола — после одного успешного
    прохода нога перестаёт быть мелкой, цикла не бывает.
    Площадка упёрлась в лимит — её остальные ноги пропускаются до следующего прохода, другие площадки идут дальше.
    report(leg, ok) — для задержки повтора у хронически падающих ног.
    """
    now_ms = int(time.time() * 1000)
    floor_ms = now_ms - days * 86400_000
    stats = {"calls": 0, "new": 0, "errors": 0, "skipped": 0, "verified": 0}
    depth, sync = db.leg_depths(con), db.leg_sync(con)
    skip: set[str] = set()
    todo = list(legs)
    for i, (ex, sym) in enumerate(todo):
        if stop is not None and stop():
            break
        if ex in skip:
            stats["skipped"] += 1
            continue
        first, _last = db.funding_span(con, ex, sym)
        iv_h = intervals.get(ex, {}).get(sym, 8)
        tol = depth_tolerance_ms(iv_h)
        chk, synced = depth.get((ex, sym)), sync.get((ex, sym))
        deep = (first is not None and first <= floor_ms + tol) or (chk is not None and chk <= floor_ms + tol)
        fresh = synced is not None and synced >= now_ms - iv_h * H_MS - _grace_ms()
        if not force and deep and fresh:
            stats["skipped"] += 1
            if progress:
                progress(i + 1, len(todo), stats)
            continue
        since = floor_ms if (force or not deep or synced is None) else synced + 1
        try:
            stats["new"] += sync_leg(con, clients[ex], ex, sym, since, now_ms, intervals.get(ex), floor_ms, synced)
            stats["calls"] += 1
            stats["verified"] += since <= floor_ms
            if report:
                report((ex, sym), True)
        except (BudgetExceeded, BannedError) as e:
            log.warning("бэкфилл %s %s: %s — площадка пропущена до следующего прохода", ex, sym, e)
            stats["errors"] += 1; skip.add(ex)
            if report:
                report((ex, sym), False)
        except Exception as e:  # noqa
            log.warning("бэкфилл %s %s: %s: %s", ex, sym, type(e).__name__, e); stats["errors"] += 1
            if report:
                report((ex, sym), False)
        if progress:
            progress(i + 1, len(todo), stats)
    return stats


def incremental(con, cl, intervals: dict[str, int]) -> int:
    """Пакет «последние расчёты по всем монетам» + продление курсоров ног, у которых он уже внутри окна пакета.

    Продлеваются только символы, попавшие в пакет, и символы с интервалом длиннее окна пакета (им законно нечего
    было рассчитывать). Символ с коротким интервалом, которого в пакете нет, не продлевается: если пакет почему-то
    неполон, его догонит посимвольный добор, а не молчаливая дыра."""
    t0 = int(time.time() * 1000)
    return apply_batch(con, venues.recent_history(cl), intervals, t0)


def apply_batch(con, rows: list[dict], intervals: dict[str, int], t0: int) -> int:
    """Запись пакета и продление курсоров. Отдельно от сети: коллектор берёт пакет в пуле потоков, а пишет в БД
    на главном потоке. t0 — момент запроса пакета.

    Строка {symbol, hold: True} без funding_ms — «контракт есть, но в пакет не вошёл намеренно» (KuCoin: свежий расчёт,
    смена интервала, прошлый расчёт внутри окна): строки нет, и правило «длинный интервал — законно нечего было
    рассчитывать» к нему не применяется — его ведёт посимвольный добор. 13.09 READYUSDTM (4 ч → 1 ч с 01:00): пропущен
    пакетом, у коллектора ещё 4 ч, курсор перепрыгнул расчёт 00:00, и полнота его уже не видела."""
    hold = {r["symbol"] for r in rows if r.get("hold")}
    rows = [r for r in rows if not r.get("hold")]
    n = db.insert_funding_events(con, rows, intervals)
    if rows:
        ex = rows[0]["exchange"]
        cover_from = (min(r["funding_ms"] for r in rows) // 60_000 + 1) * 60_000
        window_ms = t0 - cover_from
        base = t0 - _grace_ms()
        targets: dict[str, int] = {}
        own: dict[int, dict[str, int]] = {}              # строки со своим prev_ms: с какого курсора → {символ: цель}
        for r in rows:
            s, ms, p = r["symbol"], r["funding_ms"], r.get("prev_ms")
            if p is not None and p < ms:
                # пакет «последний расчёт каждого контракта» (edgeX, ревью 13.09): между прошлым расчётом контракта prev_ms
                # и этим других нет — курсор, покрывший prev_ms, идёт через ms, но не дальше следующего расчёта. Интервал
                # коллектора короче — строже он (интервал сменился на ходу). Без этого окно пакета открывалось на ms+1 мин,
                # и ни один курсор не переходил расчёт: все ноги уходили в посимвольный добор после каждого расчёта
                iv_ms = int(intervals.get(s) or 0) * H_MS
                p_eff = max(p, ms - iv_ms) if iv_ms else p
                tgt = min(max(base, ms), 2 * ms - p_eff - 1)
                own.setdefault(p_eff, {})[s] = max(own.get(p_eff, {}).get(s, tgt), tgt)
            else:                                        # попавшим в пакет — до их последнего расчёта, если он позже
                targets[s] = max(targets.get(s, base), ms)
        mine = {s for g in own.values() for s in g}
        for s, iv in intervals.items():                  # длинный интервал: законно нечего было рассчитывать
            if int(iv) * H_MS > window_ms and s not in mine and s not in hold:
                targets.setdefault(s, base)
        db.advance_sync_batch(con, ex, targets, cover_from)
        for since, tg in own.items():
            db.advance_sync_batch(con, ex, tg, since)
    return n


def completeness(events: dict[tuple[str, str], list[tuple[int, float]]], legs: list[tuple[str, str]],
                 intervals: dict[str, dict[str, int]], now_ms: int, window_h: float,
                 next_ms: dict[str, dict[str, int]] | None = None,
                 grace_s: int = config.SETTLE_GRACE_S,
                 depth: dict[tuple[str, str], int] | None = None,
                 sync: dict[tuple[str, str], int] | None = None) -> dict[tuple[str, str], dict]:
    """(площадка, символ) -> {n, missing, latest_missing, hole, holes, first_ms, shallow, legacy, since, pending_ms, catchup}.

    Объявленный интервал (`fundingInfo`) — не сетка: Aster меняет его на ходу. Поэтому последний расчёт ждём по
    слову самой биржи: prev = nextFundingTime − интервал; без nextFundingTime — по сетке объявленного интервала.
    since — откуда добирать: с курсора, у ног без курсора — с последнего сохранённого расчёта.
    """
    out = {}
    grace_ms = grace_s * 1000
    lo_max = now_ms - int(window_h * H_MS)
    depth, sync = depth or {}, sync or {}
    for ex, sym in legs:
        iv = intervals.get(ex, {}).get(sym, 8)
        iv_ms = iv * H_MS
        tol = depth_tolerance_ms(iv)
        evs = events.get((ex, sym), [])
        times = [ms for ms, _ in evs]
        in_win = [ms for ms in times if lo_max < ms <= now_ms]
        have_min = {ms // 60_000 for ms in times}
        synced = sync.get((ex, sym))
        nxt = (next_ms or {}).get(ex, {}).get(sym)
        if nxt:
            prev_raw = nxt - iv_ms
            prev = prev_raw if now_ms > prev_raw + grace_ms else None
        else:
            exp = expected_settlements(iv, now_ms, window_h, grace_s)
            prev = exp[-1] if exp else None
            exp0 = expected_settlements(iv, now_ms, window_h, 0)
            prev_raw = exp0[-1] if exp0 else None
        # последний расчёт уже был, но в БД его ещё нет (в том числе в льготу): окна ноги кончаются перед ним
        pending_ms = prev_raw if (prev_raw is not None and lo_max < prev_raw <= now_ms
                                  and prev_raw // 60_000 not in have_min) else None
        fixed = config.FIXED_INTERVAL_H.get(ex)
        missing: list[int] = []
        if prev is not None and prev > lo_max:
            if synced is not None:
                # под курсором — по курсору; у площадки с неизменным интервалом сам расчёт ещё и обязан лежать в БД
                # (защита от дыры, которую курсор «не видит»: у Hyperliquid расчёт есть каждый час, всегда)
                if synced < prev or (fixed and prev // 60_000 not in have_min):
                    missing = [prev]
            elif prev // 60_000 not in have_min:
                missing = [prev]
        if fixed:
            holes = [(a, b) for a, b in zip(times, times[1:]) if b - a > fixed * H_MS + grace_ms]
        elif synced is None:
            holes = [(a, b) for a, b in zip(times, times[1:]) if b - a > max(MAX_INTERVAL_H, 2 * iv) * H_MS + grace_ms]
        else:
            holes = []                       # внутри подтверждённого отрезка разрывов нет по построению
        first = times[0] if times else None
        last = times[-1] if times else None
        chk = depth.get((ex, sym))
        shallow = []
        for w in config.WINDOWS_H:
            lo = now_ms - w * H_MS
            covered = (first is not None and first <= lo + tol) or (chk is not None and chk <= lo + tol)
            if not covered:
                shallow.append(w)
        since = synced + 1 if synced is not None else (last + 1 if last is not None else (min(missing) - 1 if missing else None))
        if fixed and missing and last is not None and prev // 60_000 not in have_min:
            since = last + 1 if since is None else min(since, last + 1)
        if holes and (fixed or synced is not None):
            since = holes[0][0] + 1 if since is None else min(since, holes[0][0] + 1)
        # добор: нет последнего расчёта или дыра, которую можно закрыть одним запросом (площадка с неизменным интервалом);
        # дыры ног с плавающим интервалом без курсора закрывает пересборка с пола (очередь глубины)
        needs_repair = bool(missing) or (bool(holes) and bool(fixed))
        # плановый досбор: нет только последнего расчёта, и он моложе CATCHUP_S — добор идёт (needs_repair), но для
        # страницы это не дыра: окна ноги и так кончаются перед ним (pending_ms), суммы не занижены
        catchup = bool(missing) and now_ms - missing[-1] <= config.CATCHUP_S * 1000
        out[(ex, sym)] = dict(n=len(in_win), missing=missing, latest_missing=bool(missing), hole=bool(holes),
                              holes=holes, first_ms=first, shallow=shallow, legacy=synced is None, since=since,
                              needs_repair=needs_repair, pending_ms=pending_ms, catchup=catchup)
    return out


def repair(con, clients: dict, gaps: dict[tuple[str, str], dict], intervals: dict[str, dict[str, int]],
           max_calls: int = config.REPAIR_MAX_CALLS, days: int = config.HISTORY_DAYS, report=None) -> int:
    """Добор свежих расчётов: весь пропуск от курсора (или от последнего сохранённого расчёта / начала дыры) до
    сейчас. Глубже пола не ходит — это дело бэкфилла. Площадка упёрлась в лимит — пропускаются только её ноги."""
    now_ms = int(time.time() * 1000)
    floor_ms = now_ms - days * 86400_000
    sync = db.leg_sync(con)
    todo: dict[str, list[tuple[str, int]]] = {}
    for (ex, sym), g in gaps.items():
        if not g.get("needs_repair", g["latest_missing"]):
            continue
        since = g.get("since")
        if since is None:
            since = min(g["missing"]) - 1
        todo.setdefault(ex, []).append((sym, max(int(since), floor_ms)))
    # Площадки — параллельно, ноги одной площадки — по очереди (темп у каждого клиента свой). Один общий цикл по ~4100
    # ногам 8 площадок шёл ~13 мин и почти съедал CATCHUP_S (ревью 12.09); теперь время = самая медленная площадка.
    # У каждого потока своё соединение с файлом БД; у БД в памяти (тесты) потоков нет — одно соединение, по очереди.
    path = con.execute("PRAGMA database_list").fetchone()[2]
    lock, left = threading.Lock(), [max_calls]

    def run(ex: str, legs: list[tuple[str, int]], c) -> int:
        n = 0
        for sym, since in legs:
            with lock:
                if left[0] <= 0:
                    break
                left[0] -= 1
            try:
                n += sync_leg(c, clients[ex], ex, sym, since, now_ms, intervals.get(ex), floor_ms, sync.get((ex, sym)))
                ok = True
            except (BudgetExceeded, BannedError) as e:
                log.warning("добор %s %s: %s — площадка пропущена до следующего прохода", ex, sym, e)
                ok = None
            except Exception as e:  # noqa
                log.warning("добор %s %s: %s: %s", ex, sym, type(e).__name__, e)
                ok = False
            if report:
                with lock:
                    report((ex, sym), bool(ok))
            if ok is None:
                break
        return n

    def own(ex: str, legs: list[tuple[str, int]]) -> int:
        c = db.connect(path)
        try:
            return run(ex, legs, c)
        finally:
            c.close()

    if not path or len(todo) < 2:
        return sum(run(ex, legs, con) for ex, legs in todo.items())
    with ThreadPoolExecutor(max_workers=len(todo), thread_name_prefix="repair") as pool:
        return sum(pool.map(lambda kv: own(*kv), todo.items()))
