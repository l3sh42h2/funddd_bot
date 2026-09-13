"""Коллектор: один процесс, один цикл, все такты по времени.

  каждые 10 с   ставки, марки, индексы и книги всех площадок + спот-тикеры Binance/Gate/KuCoin/Bitget → table.json
  каждые 15 мин досбор истории (пакетный, где он есть) + интервалы fundingInfo + проверка полноты → обслуживание
  в hh:10       досбор + проверка полноты сразу после часового расчёта
  каждый час    инструменты всех площадок → пары futures/futures и сделки spot/futures
  каждый час    списки монет спотов (контракты, имена, ввод/вывод, множители токенов акций) → «тот ли актив»
  раз в сутки   состав индекса каждого перпа Binance/Aster (новый перп — сразу) → «тот ли актив» (identity.py)
  каждые 15 мин снимок обеих таблиц в БД (если диск не ниже порога)

Две таблицы (переключатель владельца 10.09): ff — рынок A против рынка той же монеты на B; sf — спот любой
спот-площадки (лонг) + перп любой площадки (шорт). Площадки — config.PERP_VENUES и config.SPOT_VENUES, в порядке,
в котором их добавляет владелец.

Внешнее ревью 11.09 и проверка исправлений — что устроено так и почему:
- Сеть — только в пуле потоков; главный поток ждёт её не дольше TICK_DEADLINE_S за проход (п.6). У площадки два
  слота: тик (ставки и книги) и вспомогательное задание (вселенная, интервалы, пакетный досбор — что из этого пора).
  Раньше вспомогательные шаги шли на главном потоке с тремя попытками по 15 с — одна зависшая точка держала весь
  цикл 51 с; а площадка, чей тик опоздал к дедлайну, считалась «занятой» и её интервалы и досбор не шли вовсе.
  Не успевшее задание применяется в следующем проходе; разбор и запись в БД — на главном потоке.
- У каждой цены есть метка наблюдения obs и номер снимка cyc (п.3, п.4). Книга используется, только если она из того
  же снимка, что и ставка (книга не обновилась — цена по марку, строка это показывает); ставка старше STALE_S —
  строка «устарела». «Не тот актив» с 12.09 — не по цене, а по составу индекса и контрактам (identity.py).
- Обслуживание истории — одно фоновое задание (п.2): сначала добор свежих расчётов всех ног (всегда), потом бэкфилл
  «мелких» ног и ног без курсора непрерывности — не дольше MAINT_DEEP_BUDGET_S за проход. Нога, у которой история
  падает, повторяется с растущей паузой — своей у добора и у глубины; площадка в лимите пропускается до следующего
  прохода — остальные идут.
- После hh:10:30 проверка полноты ждёт пакетный досбор всех площадок (не дольше SETTLE_WAIT_S): иначе каждый свежий
  расчёт уходил бы в посимвольный добор.
- Любая ошибка фонового задания, включая открытие БД, снимает флаг «занято» (п.7).
- В table.json — pid процесса: проверка выката убеждается, что таблицу пишет новый процесс, а не старый.
- Окно событий (13.09, events_window.py): история 30 сут, полнота и суммы окон считаются в своём потоке «events» из
  рядов в памяти (добор по rowid, полная перезагрузка раз в 30 мин и при расхождении числа строк). Главный поток только
  подставляет готовый вид одной подстановкой ссылок и не ждёт пересчёт — кроме самого первого вида процесса (таблица
  без окон показала бы пустые суммы и флаги). Было: перечитывание ~1.5 млн строк на главном потоке, фаза «окна» 11–22 с.
  Состязательная проверка 13.09: первый вид ждём ОДИН раз (при постоянной ошибке иначе каждый тик шла бы полная
  перезагрузка на главном потоке); вид старше EVENTS_STALE_S — заметка «events» (повисший пересчёт не молчит); чистка
  снимков с чекпойнтом WAL — только когда окно свободно (TRUNCATE ждёт транзакцию чтения потока до 30 с).
Веб-процесс читает только table.json — у него нет ни БД, ни бирж.
"""
from __future__ import annotations
import json, os, time, logging, threading, shutil
from concurrent.futures import ThreadPoolExecutor, wait
from . import config, db, exchanges, universe, funding, calc, venues, identity, identity_src, dexleg, events_window
from .client import BudgetExceeded, BannedError, make_clients

log = logging.getLogger(__name__)

EVENTS_S = 60              # окно событий пересчитывается раз в минуту или по флагу «грязно»
# вид окна старше — заметка «events» на странице: обычный возраст ≤ EVENTS_S + пересчёт + тик (~1.5 мин; полная
# перезагрузка на ireland ~20–30 с). Повисший или раз за разом падающий пересчёт иначе держал бы страницу на старом виде молча
EVENTS_STALE_S = 300
AUX_RETRY_S = 120         # упавший шаг площадки (вселенная, интервалы, досбор) — повтор через 2 мин, а не через период
SETTLE_WAIT_S = 120        # после hh:10:30 полнота ждёт пакетный досбор площадок не дольше этого
NOTE = {"universe": "universe", "intervals": "intervals", "incr": "incremental", "coins": "coins", "legs": "index",
        "dex": "okxdex"}
# долгие шаги (список монет с множителями акций ~30 с, пачка составов индекса ~25 с): тик их не ждёт и не шлёт по ним
# «нет ответа» раньше SLOW_AUX_S — иначе каждый такой шаг держал бы тик 8 с и мигал красным чипом
SLOW_KINDS = {"coins", "legs", "dex"}
SLOW_AUX_S = 300


def settle_mark(now_s: float) -> float:
    """Отметка проверки после часового расчёта: hh:10:30 текущего часа."""
    return int(now_s // 3600) * 3600 + config.SETTLE_GRACE_S + 30


def settle_due(now_s: float, last_check_s: float) -> bool:
    """Пора ли проверить полноту после часового расчёта: hh:10:30 прошло, а после этой отметки не проверяли."""
    mark = settle_mark(now_s)
    return now_s >= mark and last_check_s < mark


def _period(kind: str) -> float:
    return {"universe": config.UNIVERSE_S, "intervals": config.INTERVALS_S, "incr": config.FUNDING_INCR_S,
            "coins": config.COINS_S, "legs": config.LEGS_PAUSE_S, "dex": config.OKX_DEX_JOB_S}[kind]


def _stale_s(venue: str | None) -> float:
    """Предел возраста данных площадки: общий STALE_S, у площадки со снимком реже тика — свой (13.09: Variational)."""
    return config.STALE_S_BY_VENUE.get(venue, config.STALE_S) if venue else config.STALE_S


def _fresh(entry: dict | None, now_s: float, venue: str | None = None) -> bool:
    return entry is not None and entry.get("obs") is not None and now_s - entry["obs"] <= _stale_s(venue)


# Имена Lighter (tokenlist, 12.09) идут в проверку как есть — поведение прежних площадок не меняется (живой срез 13.09:
# фильтр ниже снял бы у Lighter 74 имени из 200 и 16 из 57 у Robinhood — «Aave», «dYdX»…, и их «тот по имени» стали бы
# «не проверено»). Решение для Lighter — за владельцем (TZ, раздел «Новые перп-биржи 13.09»).
NAMES_AS_IS = frozenset({"lighter", "lighter_rh"})


def perp_names(instruments: dict[str, dict[str, dict]]) -> dict[str, dict[str, str]]:
    """Заявленные имена рынков у перпов на оракуле площадки (identity.ORACLE_PERPS: Lighter, с 13.09 и Backpack, Variational,
    Extended, ApeX) — их единственное свидетельство «тот ли актив». У новых площадок имя, равное своему тикеру без учёта
    регистра («MKR» у Extended, «Pepe» у 1000PEPE), — не имя: иначе «тот по имени» подтверждался бы тикером
    (identity._tickerish — то же правило, что у монет спотов; клиенты Backpack, Variational и ApeX уже так делают)."""
    return {v: {s: i["name_hint"] for s, i in instruments.get(v, {}).items()
                if i.get("name_hint") and (v in NAMES_AS_IS or not identity._tickerish(i["name_hint"], i.get("base") or s))}
            for v in sorted(identity.ORACLE_PERPS) if v in instruments}


def own_history(cl) -> bool:
    """Площадка без публичной истории фандинга (13.09: Variational): история — собственная книга расчётов клиента с момента
    старта процесса. Глубину 30 сут у неё не добрать ничем — пересборка с пола дала бы только отказ на каждую ногу раз в
    15 мин…4 ч. Её расчёты приходят пакетом recent_history (досбор), глубина копится сама."""
    return bool(getattr(cl, "own_history", False))


class Collector:
    def __init__(self, clients: dict | None = None, con=None, db_path=None, table_path=None,
                 now=time.time, sleep=time.sleep, background: bool = True):
        self._injected = clients is not None
        self.clients = clients or make_clients()
        self.db_path = db_path or config.DB_PATH
        self.con = con or db.connect(self.db_path)
        self.table_path = table_path or config.TABLE_PATH
        self.now, self.sleep = now, sleep
        self.background = background          # False — обслуживание истории в том же потоке (тесты)
        self.venues = [v for v in config.PERP_VENUES if v in self.clients]
        self.spot_venues = [v for v in config.SPOT_VENUES if v in self.clients]
        self.instruments: dict[str, dict[str, dict]] = {ex: {} for ex in self.venues + self.spot_venues}
        self.ff: list[dict] = []
        self.sf: list[dict] = []
        self.sf_dex: list[dict] = []                       # сделки со спотом OKX DEX (dexleg) — после вердиктов identity
        self._legs_known: set[tuple[str, str]] = set()     # ноги, для которых уже назначалась проверка полноты
        self.prem: dict[str, dict[str, dict]] = {}
        self.books: dict[str, dict[str, dict]] = {}
        self.src_ts: dict[str, dict[str, float]] = {}      # площадка -> {"prem"/"books": время последнего успеха}
        self.events: dict[tuple[str, str], list[tuple[int, float]]] = {}
        self.wsums: dict[tuple[str, str], dict[int, tuple[float | None, int]]] = {}
        self.comp: dict[tuple[str, str], dict] = {}
        self.notes: dict[str, str] = {}
        self.last = {"tick": 0.0, "incr": 0.0, "universe": 0.0, "snapshot": 0.0, "events": 0.0,
                     "purge": 0.0, "completeness": 0.0}
        self.backfill_state = {"running": False, "done": 0, "total": 0, "stats": None, "finished_ts": 0}
        self.repair_state = {"running": False, "n_symbols": 0, "n_new": 0, "finished_ts": 0}
        self._maint_running = False
        self._stop = False
        self._events_dirty = True
        self._repair_due = False
        self._rebuild_due = False
        n = len(self.venues) + len(self.spot_venues)
        self._pool = ThreadPoolExecutor(max_workers=2 * n + 1, thread_name_prefix="net")     # +1 — задание OKX DEX
        self._pending: dict[str, tuple] = {}               # тик: площадка -> (Future, время запуска)
        self._aux: dict[str, tuple] = {}                   # задание: площадка -> (Future, время запуска, часы коллектора, шаги)
        self._vlast: dict[tuple[str, str], float] = {}     # (шаг, площадка) -> когда шаг последний раз дал результат
        self._settled: dict[str, float] = {}               # площадка -> отметка hh:10:30, пакет после которой применён
        self._cycle = 0
        self._fail: dict[tuple, tuple[int, float]] = {}    # ((площадка, символ), "repair"|"deep") -> (отказов, когда можно)
        self._lock = threading.Lock()
        # окно событий (events_window): без фона — соединение коллектора и тот же поток, как раньше; в фоне — своё
        # соединение и поток «events». Готовый вид — в ячейку _ev_ready, главный поток подставляет его в refresh_events
        self._evw = events_window.EventWindow(con=None if background else self.con, path=self.db_path)
        self._ev_pool: ThreadPoolExecutor | None = None
        self._ev_job = None                                # идущий пересчёт потока «events» (Future)
        self._ev_ready = None                              # готовый вид, который главный поток ещё не подставил
        self._ev_lock = threading.Lock()                   # только на обмен ячейкой _ev_ready
        self._ev_gen = 0                                   # номер подставленного вида
        self._ev_legs = 0                                  # ног в карте полноты подставленного вида
        self._ev_view_s: float | None = None               # часы коллектора, на которые посчитан подставленный вид
        self._ev_t0 = self.now()                           # отсчёт возраста, пока вида нет
        self._ev_job_t = 0.0                               # когда запущен идущий пересчёт (часы коллектора)
        self._ev_err: str | None = None                    # последняя ошибка пересчёта (снята успехом)
        self._ev_stale = False                             # заметка «events» сейчас — сторож возраста
        self._ev_waited = False                            # первый вид процесса уже ждали (один раз)
        self._ev_warm = False                              # прогрев окна уже запускали (один раз)
        self._started = time.time()
        # «тот ли актив» (identity.py): источники живут в БД — после рестарта вердикты есть сразу, без обхода
        self.id_spots: dict[str, dict] = {}                # спот-площадка -> монеты, рынки, множители акций
        self.id_legs: dict[str, dict[str, dict]] = {}      # перп-площадка -> символ -> состав индекса (HL: описание)
        self.id_ts: dict[str, dict[str, int]] = {}         # перп-площадка -> символ -> когда получен
        self.ident_summary: dict[str, int] = {}
        self._resolve_due = False
        self._splits_seen: set[tuple[str, str, str]] = set()   # universe.class_splits, уже записанные в журнал
        self.dex = dexleg.DexLeg(self.clients.get(dexleg.VENUE))    # без ключа владельца — выключена, запросов нет
        self._load_ident()

    def _load_ident(self):
        try:
            src = db.load_ident_src(self.con)
        except Exception as e:  # noqa — без сохранённых источников вердикты «не проверено» до первого обхода
            log.warning("источники «тот ли актив» из БД: %s: %s", type(e).__name__, e)
            return
        for v, entries in src.items():
            if v in self.spot_venues and "#coins" in entries:
                self.id_spots[v], ts = entries["#coins"]
                recs = list((self.id_spots[v].get("coins") or {}).values())[:50]
                if all("dex" in r for r in recs):         # список без контрактов по сетям DEX (до 12.09) — собрать заново
                    self._vlast[("coins", v)] = ts
            elif v in self.venues:
                self.id_legs[v] = {k: e for k, (e, _ts) in entries.items()}
                self.id_ts[v] = {k: ts for k, (_e, ts) in entries.items()}
            elif v == dexleg.VENUE and "#state" in entries and self.dex.enabled:
                self.dex.restore(entries["#state"][0])

    # --- служебное -----------------------------------------------------------------------
    def intervals(self) -> dict[str, dict[str, int]]:
        return {ex: {s: (i.get("interval_h") or 8) for s, i in self.instruments.get(ex, {}).items()} for ex in self.venues}

    def legs(self) -> list[tuple[str, str]]:
        return funding.legs_of(self.ff, self.sf + self.sf_dex)

    def _set_dex_rows(self, rows: list[dict]):
        """Сделки DEX; если они дали новые ноги (перп Aster с единственным спотом на DEX: BONER, SHROOM…), — проверка
        полноты и бэкфилл глубины сразу, а не у ближайшего досбора через ≤15 мин (владелец 12.09: «!» у новых ног —
        «устранить причину»)."""
        self.sf_dex = rows
        cur = set(self.legs())
        if cur - self._legs_known:
            self._repair_due = True
            self._events_dirty = True            # окна новых ног — не через минуту, а следующим пересчётом
        self._legs_known = cur

    def _bg_clients(self) -> dict:
        """Фоновому обслуживанию истории — свои клиенты: его длинные обходы и паузы 429 не делят сессии с тиком.
        Заголовок веса всё равно общий на IP, так что бюджет оба набора видят один и тот же."""
        return self.clients if self._injected else make_clients()

    def _note(self, name: str, e: BaseException | None):
        """Заметка шага: None — снять. Тип исключения обязателен (урок 24.08: MemoryError с пустым текстом)."""
        if e is None:
            self.notes.pop(name, None)
        elif isinstance(e, (BudgetExceeded, BannedError, TimeoutError)):
            if name not in self.notes:
                log.warning("%s: %s", name, e)
            self.notes[name] = f"{type(e).__name__}: {e}"
        else:
            log.error("%s: %s: %s", name, type(e).__name__, e); self.notes[name] = f"{type(e).__name__}: {str(e)[:200]}"

    def _guard(self, name: str, fn, *a, **kw):
        """Шаг не должен ронять цикл; но и молчать не должен."""
        try:
            self.notes.pop(name, None)      # шаг сам вправе поставить заметку (например, «диск полон»)
            return fn(*a, **kw)
        except Exception as e:  # noqa
            if not isinstance(e, (BudgetExceeded, BannedError)):
                log.exception("%s упал", name)
            self._note(name, e)
        return None

    # --- повтор хронически падающих ног -------------------------------------------------------
    def _retry_ok(self, leg: tuple[str, str], kind: str = "repair") -> bool:
        f = self._fail.get((leg, kind))
        return f is None or self.now() >= f[1]

    def _report(self, leg: tuple[str, str], ok: bool, kind: str = "repair"):
        """Пауза повтора — своя у добора и у глубины. Проверка исправлений 11.09: успешный добор ноги обнулял паузу её
        падающей глубины — длинный запрос повторялся каждые 15 мин вместо 15→30→…→4 ч, а растущая пауза глубины
        заодно держала здоровый добор той же ноги."""
        key = (leg, kind)
        with self._lock:
            if ok:
                self._fail.pop(key, None)
                return
            n = self._fail.get(key, (0, 0.0))[0] + 1
            self._fail[key] = (n, self.now() + min(config.RETRY_BACKOFF_S * 2 ** (n - 1), config.RETRY_BACKOFF_MAX_S))

    # --- задания площадок: вселенная, интервалы, пакетный досбор --------------------------------------------
    def _due(self, kind: str, v: str, t: float) -> bool:
        last = self._vlast.get((kind, v))
        return last is None or t - last >= _period(kind)

    def _submit_aux(self, v: str, kinds: list[str], t: float, todo: list[str] | None = None):
        self._aux[v] = (self._pool.submit(self._aux_job, v, kinds, list(todo or [])), time.time(), t, kinds)

    def _legs_todo(self, v: str) -> list[str]:
        """Символы, чей состав индекса пора (пере)собрать: новые — первыми, дальше самые старые; не больше пачки.
        Hyperliquid — описания крипто-рынков HIP-3 (у основного dex источник — документация оракула, запроса нет)."""
        ts, now = self.id_ts.get(v, {}), self.now()
        ins = self.instruments.get(v, {})
        if hasattr(self.clients[v], "index_legs"):     # Gate / Bitget: состав индекса своим методом
            syms = list(ins)
        elif venues.native(self.clients[v]):
            syms = [s for s, i in ins.items() if ":" in s and (i.get("cls") or "crypto") == "crypto"]
        elif v in config.INDEX_LEGS_PATHS:
            syms = list(ins)
        else:
            return []
        due = sorted((ts.get(s, 0), s) for s in syms if now - ts.get(s, 0) >= config.LEGS_S)
        return [s for _, s in due[:config.LEGS_BATCH]]

    def _schedule_aux(self, t: float):
        """Всё, что площадке пора, — одним заданием; пока её прежнее задание идёт, нового нет."""
        mark = settle_mark(t)
        after = t >= mark and self.last["completeness"] < mark
        for v in self.venues + self.spot_venues:
            if v in self._aux:
                continue
            perp = v in self.venues
            kinds = []
            if self._due("universe", v, t):
                kinds.append("universe")
            elif perp and (not venues.native(self.clients[v]) or hasattr(self.clients[v], "funding_intervals")) \
                    and self._due("intervals", v, t):
                kinds.append("intervals")            # Binance-подобные и Bitget/Gate (интервал меняется на ходу)
            if perp and (self._due("incr", v, t) or (after and self._settled.get(v) != mark)):
                kinds.append("incr")
            # долгие шаги — не в одном задании со вселенной: тик долгие задания не ждёт, и вселенная ждала бы их
            quick = "universe" not in kinds
            if quick and not perp and self._due("coins", v, t):
                kinds.append("coins")
            # обход индексов — не вместе с досбором истории и не пока ждут пакет после часового расчёта (ревью 12.09:
            # пачка до 60 с отодвигала бы досбор hh:10 за SETTLE_WAIT_S — и полнота ушла бы в посимвольный добор)
            settling = after and self._settled.get(v) != mark
            legs_ok = quick and perp and "incr" not in kinds and not settling and self.instruments.get(v)
            todo = self._legs_todo(v) if legs_ok and self._due("legs", v, t) else []
            if todo:
                kinds.append("legs")                 # последним: досбор истории в том же задании не ждёт обхода
            if kinds:
                self._submit_aux(v, kinds, t, todo)
        # OKX DEX: цены пачкой, ликвидность, котировки на клип — своё задание в своём слоте (dexleg); кандидаты — после
        # вердиктов «тот ли актив» (токены только доказанные контрактами)
        dv = dexleg.VENUE
        if self.dex.enabled and self.dex.cand and dv not in self._aux and self._due("dex", dv, t):
            self._aux[dv] = (self._pool.submit(self._dex_job, self.dex.plan(time.time())), time.time(), t, ["dex"])

    def _dex_job(self, plan: dict) -> list[tuple[str, object]]:
        """Работник пула: только сеть OKX DEX; разбор — _take на главном потоке."""
        try:
            return [("dex", self.dex.job(plan))]
        except Exception as e:  # noqa — отдаём главному потоку как результат шага
            return [("dex", e)]

    def _aux_job(self, v: str, kinds: list[str], todo: list[str] | None = None) -> list[tuple[str, object]]:
        """Работник пула: только сеть. Отказ одного шага не мешает остальным шагам задания."""
        cl, out = self.clients[v], []
        for k in kinds:
            try:
                if k == "universe":
                    res = venues.spot_instruments(cl) if v in self.spot_venues else venues.perp_instruments(cl)
                elif k == "intervals":
                    res = venues.funding_intervals(cl)
                elif k == "coins":
                    res = identity_src.coin_blob(cl)
                elif k == "legs":
                    res = (identity_src.hl_annotations(cl, todo or [])
                           if venues.native(cl) and not hasattr(cl, "index_legs")
                           else identity_src.index_legs(cl, todo or []))
                else:
                    res = (int(time.time() * 1000), venues.recent_history(cl))
            except Exception as e:  # noqa — отдаём главному потоку как результат шага
                res = e
            out.append((k, res))
        return out

    def _apply_aux(self, v: str, fut, t_sub: float, kinds: list[str]):
        now = self.now()
        try:
            results = fut.result()
        except Exception as e:  # noqa — работник сам не падает; если всё же упал, повторятся все шаги задания
            results = [(k, e) for k in kinds]
        for kind, res in results:
            name = f"{NOTE[kind]}:{v}"
            try:
                if isinstance(res, Exception):
                    raise res
                self._take(v, kind, res)
                self._note(name, None)
                self._vlast[(kind, v)] = now
            except Exception as e:  # noqa — площадка упала: живём на её прежних данных, повтор через 2 мин
                self._note(name, e)
                self._vlast[(kind, v)] = now - _period(kind) + AUX_RETRY_S
            if kind == "incr" and t_sub >= settle_mark(t_sub):
                self._settled[v] = settle_mark(t_sub)        # и при отказе: полнота не ждёт площадку вечно

    def _take(self, v: str, kind: str, res):
        """Результат шага — в состояние и БД (главный поток: соединение sqlite у каждого потока своё)."""
        if kind == "universe":
            db.upsert_instruments(self.con, res)
            self.instruments[v] = {i["symbol"]: i for i in res}
            self._vlast[("intervals", v)] = self.now()
            self._rebuild_due = True
        elif kind == "intervals":
            if res is None:
                return
            changed = []
            for s, ins in self.instruments.get(v, {}).items():
                new = res.get(s, 8)
                if ins.get("interval_h") != new:
                    changed.append((s, ins.get("interval_h"), new)); ins["interval_h"] = new
            if changed:
                log.info("интервалы %s сменились: %s", v, changed[:10])
                self._events_dirty = True
        elif kind == "coins":
            self._take_coins(v, res)
        elif kind == "legs":
            now_i = int(self.now())
            db.save_ident_src(self.con, v, res, now_i)
            self.id_legs.setdefault(v, {}).update(res)
            self.id_ts.setdefault(v, {}).update({s: now_i for s in res})
            self._resolve_due = True
        elif kind == "dex":
            try:
                self.dex.apply(res)                   # ошибка (429, квота, ключ) — после применения собранного
            finally:
                self._set_dex_rows(self.dex.rows(self.instruments))
                # котировки клипа — в БД: рестарт (выкат) не обнуляет «Комиссию» DEX-строк на ~40 мин полного круга
                db.save_ident_src(self.con, dexleg.VENUE, {"#state": self.dex.snapshot()}, int(self.now()))
        else:
            t0, rows = res
            n = funding.apply_batch(self.con, rows, self.intervals().get(v, {}), t0)
            if n:
                log.info("досбор %s: +%d расчётов", v, n)
            self._events_dirty = True
            self._repair_due = True          # у Hyperliquid пакетного досбора нет — его ведёт добор по полноте
            self.last["incr"] = self.now()

    def _take_coins(self, v: str, blob: dict):
        """Список монет спот-площадки. Множитель токена акции, не пришедший в этот раз, — прежний (сплит объявляют
        заранее, а строка без множителя показала бы ×10 как курсовой разрыв)."""
        prev = self.id_spots.get(v) or {}
        old, now = prev.get("shares") or {}, int(self.now())
        sh = dict(blob.get("shares") or {})
        for s in blob.get("need_n") or []:
            if s not in sh and s in old:
                o = dict(old[s], stale=True)
                o.setdefault("stale_since", now)
                if now - o["stale_since"] <= config.SHARES_STALE_MAX_S:
                    sh[s] = o
        blob["shares"] = sh
        if "alpha" in blob and not blob.get("alpha") and prev.get("alpha"):
            blob["alpha"] = prev["alpha"]            # список Alpha не пришёл — прежний (без него пулы DEX не принимаются)
        db.save_ident_src(self.con, v, {"#coins": blob}, int(self.now()))
        self.id_spots[v] = blob
        # множители акций — прямо в сделки этой площадки. Не пересборкой: пары те же, а пересборка тянет проверку
        # полноты по истории за 30 сут на главном потоке — 4 раза в час тик шёл 11–17 с (выкат 12.09)
        for it in self.sf:
            n = sh.get(it["spot"]) if it.get("spot_ex") == v and it.get("cls") == "equity" else None
            if n:
                it["spot_factor"] = n["n"]
        self._resolve_due = True

    def _with_shares(self, v: str, ins: dict) -> dict:
        n = ((self.id_spots.get(v) or {}).get("shares") or {}).get(ins["symbol"])
        return dict(ins, factor=n["n"]) if n and ins.get("alt_base") else ins

    def _rebuild(self):
        """Пары и сделки заново из инструментов всех площадок, потом вердикты «тот ли актив». Токены акций — с числом
        акций в токене (владелец 12.09: NFLXX — 10 акций, пересчитываем на одну)."""
        perps = {v: list(self.instruments[v].values()) for v in self.venues if self.instruments.get(v)}
        spots = {v: [self._with_shares(v, i) for i in self.instruments[v].values()]
                 for v in self.spot_venues if self.instruments.get(v)}
        self.ff = universe.build_ff(perps)
        self.sf = universe.build_sf(perps, spots)
        self._log_class_splits(perps)
        self._resolve()
        log.info("вселенная: %s; споты %s → перп/перп %d, спот/перп %d; вердикты %s",
                 ", ".join(f"{v} {len(self.instruments.get(v, {}))}" for v in self.venues),
                 ", ".join(f"{v} {len(self.instruments.get(v, {}))}" for v in self.spot_venues), len(self.ff), len(self.sf),
                 self.ident_summary)
        self.last["universe"] = self.now()
        self._repair_due = True
        # ноги могли смениться: карта полноты и суммы новых ног — следующим пересчётом окна (в фоне проверка полноты
        # больше не пересчитывает окно на главном потоке)
        self._events_dirty = True

    def _log_class_splits(self, perps: dict[str, list[dict]]):
        """В журнал — рынки, чей класс расходится с ≥ 2 биржами при той же цене (universe.class_splits): с ними пары не
        строятся. Только новые с прошлой пересборки — раз в час одно и то же не повторяется."""
        try:
            marks = {v: {s: p.get("mark") for s, p in self.prem.get(v, {}).items()} for v in self.venues}
            splits = universe.class_splits(perps, marks)
        except Exception as e:  # noqa — проверка для журнала не должна мешать пересборке
            log.warning("проверка классов: %s: %s", type(e).__name__, e)
            return
        seen = {(x["venue"], x["symbol"], x["cls"]) for x in splits}
        new = [x for x in splits if (x["venue"], x["symbol"], x["cls"]) not in self._splits_seen]
        if new:
            log.warning("класс рынка расходится с другими биржами при той же цене — пар с ними нет: %s",
                        ", ".join(f"{x['venue']}:{x['symbol']} {x['cls']} ≠ {x['major']} ×{x['n_major']}" for x in new[:20]))
        self._splits_seen = seen

    def _resolve(self):
        """Вердикты «тот ли актив» по составу индексов и контрактам (identity.py) — не по цене. Без сети: ~0.5 с."""
        self._resolve_due = False
        before = {it["key"] for it in self.ff + self.sf if it.get("mismatch")}
        try:
            bn = {s: i.get("base_asset") for s, i in self.instruments.get("binance", {}).items()}
            an = {s: i["name_hint"] for s, i in self.instruments.get("aster", {}).items() if i.get("name_hint")}
            hl = {s: (e or {}).get("desc") for s, e in self.id_legs.get("hyperliquid", {}).items()}
            R = identity.Resolver(self.id_spots, self.id_legs, hl, bn, an, perp_names=perp_names(self.instruments))
            self.ident_summary = identity.resolve(self.ff, self.sf, R)
            if self.dex.enabled:                       # токены DEX — только доказанные контрактами этих же вердиктов
                self.dex.set_candidates(R, self.instruments)
                self._set_dex_rows(self.dex.rows(self.instruments))
            self._note("identity", None)
        except Exception as e:  # noqa — ревью 12.09: без вердиктов строки «не проверено» с заметкой, а не «всё чисто»
            self._note("identity", e)
            self.ident_summary = identity.resolve(self.ff, self.sf, None, fallback="resolver_error")
        after = {it["key"] for it in self.ff + self.sf if it.get("mismatch")}
        if after != before:
            log.warning("не тот актив: +%d %s; −%d %s", len(after - before), sorted(after - before)[:12],
                        len(before - after), sorted(before - after)[:12])

    def step_universe(self, timeout: float | None = None):
        """Пересобрать вселенную всех площадок сейчас и подождать ответов не дольше timeout (CLI, тесты).
        Цикл сам планирует её раз в час — в _schedule_aux."""
        t = self.now()
        for v in self.venues + self.spot_venues:
            if v not in self._aux:
                self._submit_aux(v, ["universe"], t)
        self._settle(config.TICK_DEADLINE_S if timeout is None else timeout)

    # --- сеть: ожидание и приём ------------------------------------------------------------------------
    def _settle(self, timeout: float):
        """Подождать сетевые задания не дольше timeout и применить готовые. Задание, висящее с прошлых проходов,
        второй раз не ждём — применится, когда закончится."""
        now_r = time.time()
        futs = [f for f, _ in self._pending.values()] + \
               [a[0] for a in self._aux.values()
                if now_r - a[1] < max(timeout, config.TICK_DEADLINE_S) and not set(a[3]) & SLOW_KINDS]
        if futs:
            wait(futs, timeout=max(0.0, timeout))
        self._collect()

    def _collect(self):
        for v, (f, _t0) in list(self._pending.items()):
            if f.done():
                del self._pending[v]
                self._apply(v, f)
        for v, (f, _t0, t_sub, kinds) in list(self._aux.items()):
            if f.done():
                del self._aux[v]
                self._apply_aux(v, f, t_sub, kinds)
        if self._rebuild_due:
            self._rebuild_due = False
            self._rebuild()
        elif self._resolve_due and (self.ff or self.sf):
            self._resolve()

    # --- тик: параллельно, с дедлайном ----------------------------------------------------------------
    def _fetch(self, v: str) -> dict:
        """Работник пула: ставки и книга одной площадки. Книга упала после ставок — ставки всё равно приходят."""
        cl = self.clients[v]
        with self._lock:
            self._cycle += 1
            cyc = self._cycle
        if v in self.spot_venues:
            t = time.time()
            return dict(books=venues.spot_books(cl), t_books=t, cyc=cyc)
        t = time.time()
        res = dict(prem=venues.premium(cl), t_prem=t, cyc=cyc)
        try:
            tb = time.time()
            res["books"] = venues.books(cl)
            res["t_books"] = tb
        except Exception as e:  # noqa — ставка свежая, книга нет: цена пойдёт по марку
            res["books_err"] = e
        return res

    def _apply(self, v: str, fut):
        try:
            res = fut.result()
        except Exception as e:  # noqa — прежние данные площадки остаются, её строки устаревают по obs
            self._note(f"tick:{v}", e)
            return
        self._note(f"tick:{v}", None)
        src = self.src_ts.setdefault(v, {})
        if "prem" in res:
            for p in res["prem"].values():
                p.setdefault("obs", res["t_prem"]); p["cyc"] = res["cyc"]
            self.prem[v] = res["prem"]; src["prem"] = res["t_prem"]
        if "books" in res:
            for b in res["books"].values():
                b.setdefault("obs", res["t_books"]); b["cyc"] = res["cyc"]
            self.books[v] = res["books"]; src["books"] = res["t_books"]
            self._note(f"books:{v}", None)
        elif "books_err" in res:
            self._note(f"books:{v}", res["books_err"])

    def step_tick(self):
        self._collect()
        for v in self.venues + self.spot_venues:
            if v not in self._pending:
                self._pending[v] = (self._pool.submit(self._fetch, v), time.time())
        self._settle(config.TICK_DEADLINE_S)
        now_r = time.time()
        for v, (_f, t0) in self._pending.items():
            self._note(f"tick:{v}", TimeoutError(f"нет ответа {now_r - t0:.0f} с — тик идёт без неё"))
        for v, (_f, t0, _t, kinds) in self._aux.items():
            if now_r - t0 >= (SLOW_AUX_S if set(kinds) & SLOW_KINDS else config.TICK_DEADLINE_S):
                for k in kinds:
                    self._note(f"{NOTE[k]}:{v}", TimeoutError(f"ответа нет {now_r - t0:.0f} с — цикл идёт без него"))
        self.last["tick"] = self.now()

    # --- окно событий: история, полнота, суммы окон (events_window) ----------------------------------------------------
    def _events_input(self) -> dict:
        """Входы пересчёта окна — снимок на главном потоке: поток окна не читает словари, которые тик меняет на ходу."""
        return dict(now_ms=int(self.now() * 1000), legs=self.legs(), intervals=self.intervals(),
                    next_ms={v: {s: p.get("next_ms") for s, p in self.prem.get(v, {}).items()} for v in self.venues})

    def _run_events(self, inp: dict) -> events_window.EventsView:
        """Пересчёт окна (поток «events», задание обслуживания или главный поток без фона). Готовый вид — в ячейку
        одной подстановкой ссылки: главный поток видит либо прежний вид, либо новый целиком."""
        view = self._evw.refresh(**inp)
        with self._ev_lock:
            if self._ev_ready is None or view.gen > self._ev_ready.gen:
                self._ev_ready = view
        return view

    def _submit_events(self, inp: dict):
        if self._ev_pool is None:
            self._ev_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="events")
        self._ev_job_t = self.now()
        self._ev_job = self._ev_pool.submit(self._run_events, inp)

    def _take_events(self):
        """Готовый вид окна — в состояние тика: три ссылки подставляются на главном потоке, и только он их читает."""
        f = self._ev_job
        if f is not None and f.done():
            self._ev_job = None
            e = f.exception()
            self._note("events", e)             # упал — живём на прежнем виде; повтор через EVENTS_S, окно с нуля
            self._ev_err = self.notes.get("events")
        with self._ev_lock:
            view, self._ev_ready = self._ev_ready, None
        if view is not None and view.gen > self._ev_gen:
            self.events, self.comp, self.wsums = view.events, view.comp, view.wsums
            self._ev_gen, self._ev_legs, self._ev_view_s = view.gen, view.legs, view.now_ms / 1000
        self._events_age()

    def _events_age(self):
        """Сторож свежести (состязательная проверка 13.09): прежний вид остаётся на странице, но не молча. Повисший
        пересчёт (future не завершается) ни ошибки, ни нового вида не даёт — без сторожа страница стояла бы на старых окнах
        и флагах сколько угодно. Вид старше EVENTS_STALE_S — заметка «events» с возрастом, идущим пересчётом и последней
        ошибкой; вид освежился — заметка снята (или вернулась ошибка, если она есть)."""
        age = self.now() - (self._ev_view_s if self._ev_view_s is not None else self._ev_t0)
        stale = self.background and age > EVENTS_STALE_S and bool(self.ff or self.sf or self.sf_dex)
        if stale:
            run = f"; пересчёт идёт {self.now() - self._ev_job_t:.0f} с" if self._ev_job is not None else ""
            err = f"; последняя ошибка — {self._ev_err}" if self._ev_err else ""
            text = f"окно событий не обновлялось {age:.0f} с — страница на прежнем виде{run}{err}"
            if not self._ev_stale:
                log.warning("events: %s", text)
            self.notes["events"] = f"TimeoutError: {text}"
        elif self._ev_stale:
            if self._ev_err:
                self.notes["events"] = self._ev_err
            else:
                self.notes.pop("events", None)
        self._ev_stale = stale

    def _warm_events(self):
        """Первый проход процесса: полная загрузка окна в фоне, пока тик ждёт сеть, — первый вид ждёт только остаток.
        Один раз: при падающем пересчёте прогрев каждый тик гонял бы полную загрузку в фоне раз в 10 с."""
        if self.background and not self._ev_warm and not self._ev_gen and self._ev_job is None and not self._evw.busy():
            self._ev_warm = True
            self._submit_events(dict(now_ms=int(self.now() * 1000), legs=[], intervals={}, next_ms={}))

    def refresh_events(self, force: bool = False):
        """События окна + карта полноты + суммы окон. Раз в минуту или по грязному флагу.

        В фоне (13.09) пересчёт идёт в потоке «events» (events_window: добор по rowid, полнота всех ног из памяти, суммы
        только там, где сдвинулась граница окна); здесь — подстановка готового вида и запуск следующего. Идущий пересчёт
        второй раз не запускается — флаг «грязно» остаётся до следующего тика. force в фоне — только мимо каденции; свежий
        вид для проверки полноты строится в самом задании обслуживания (step_completeness). Ждём пересчёт лишь однажды —
        пока у процесса нет вида с ногами, а строки уже есть: таблица без окон показала бы пустые суммы и флаги. Ждём ОДИН
        раз (состязательная проверка 13.09): если первый пересчёт упал (MemoryError, битая БД), дальше — фоном по каденции;
        иначе каждый тик шла бы полная перезагрузка на главном потоке, пока ошибка не уйдёт.
        Без фона (тесты, CLI-проверки) — в том же потоке, как раньше."""
        self._take_events()
        first = not self._ev_legs and bool(self.ff or self.sf or self.sf_dex)
        wait = first and not self._ev_waited
        if not (force or wait or self._events_dirty or self.now() - self.last["events"] >= EVENTS_S):
            return
        if self.background and not wait:
            if self._ev_job is not None or self._evw.busy():
                return
            inp = self._events_input()
            self._events_dirty = False
            self.last["events"] = self.now()
            self._submit_events(inp)
            return
        inp = self._events_input()
        self._events_dirty = False
        self.last["events"] = self.now()
        self._ev_waited = self._ev_waited or wait
        try:
            self._run_events(inp)                # в фоне: ждёт идущий пересчёт (замок окна), дальше — добор
            self._note("events", None)
        except Exception as e:  # noqa — живём на прежнем виде, заметка на странице
            self._note("events", e)
        self._ev_err = self.notes.get("events")
        self._take_events()

    def _plan(self, comp: dict) -> tuple[dict, list]:
        """Очередь обслуживания по карте полноты: добор свежих расчётов и глубина (мелкие, без курсора, с дырами)."""
        catchup = {k: g for k, g in comp.items()
                   if g.get("needs_repair", g["latest_missing"]) and not g["shallow"] and self._retry_ok(k, "repair")}
        deep = [k for k, g in comp.items() if (g["shallow"] or g["legacy"] or g["hole"]) and self._retry_ok(k, "deep")
                and not own_history(self.clients.get(k[0]))]      # 13.09: у Variational глубину не добрать — копится сама
        deep.sort(key=lambda k: 0 if comp[k]["shallow"] else (1 if comp[k]["hole"] else 2))
        return catchup, deep

    def step_completeness(self):
        """Свежие расчёты — всегда и первыми; глубина (мелкие ноги, ноги без курсора, дыры) — сколько влезет в бюджет.

        Решение ведётся по СВЕЖЕМУ виду окна — после всего, что уже лежит в БД (в hh:10:30 — после пакетного досбора).
        В фоне (13.09) этот пересчёт идёт в самом задании обслуживания, до построения очереди: главный поток его не ждёт,
        а задание, если поток «events» как раз считает, ждёт его замок и берёт снимок БД после. Готовый вид уходит и
        странице (следующий тик). Без фона — здесь же, как раньше."""
        self._repair_due = False
        self.last["completeness"] = self.now()
        if self._maint_running:
            return
        if self.background:
            self.start_maintenance(None, None, fresh=self._events_input())
            return
        self.refresh_events(force=True)
        catchup, deep = self._plan(self.comp)
        if catchup or deep:
            self.start_maintenance(catchup, deep)

    def build_table(self) -> dict:
        self.refresh_events()
        now_ms = int(self.now() * 1000)
        now_s = time.time()
        g = lambda d, v, s: d.get(v, {}).get(s)

        def leg(v, s):
            p, b = g(self.prem, v, s), g(self.books, v, s)
            book_ok = _fresh(b, now_s, v) and p is not None and b.get("cyc") == p.get("cyc")
            return p, (b if book_ok else None), not _fresh(p, now_s, v)

        ff_rows = []
        for it in self.ff:
            pa, ba, sa = leg(it["va"], it["sa"]); pb, bb, sb = leg(it["vb"], it["sb"])
            ff_rows.append(calc.build_ff_row(
                it, g(self.instruments, it["va"], it["sa"]), g(self.instruments, it["vb"], it["sb"]), pa, pb, ba, bb,
                self.wsums.get((it["va"], it["sa"]), {}), self.wsums.get((it["vb"], it["sb"]), {}), now_ms,
                self.comp.get((it["va"], it["sa"])), self.comp.get((it["vb"], it["sb"])), stale=sa or sb))
        sf_rows = []
        for it in self.sf + self.sf_dex:
            p, b, st = leg(it["perp_ex"], it["perp"])
            dx = None
            if it.get("dex"):
                # цена DEX приходит пачкой раз в OKX_DEX_JOB_S — своя свежесть; издержки клипа — из котировок
                spot_b = self.dex.book(it["spot"])
                spot_ok = spot_b is not None and now_s - spot_b["obs"] <= config.OKX_DEX_STALE_S
                dx = self.dex.extra(it, now_s)
            else:
                spot_b = g(self.books, it["spot_ex"], it["spot"])
                spot_ok = _fresh(spot_b, now_s)
            # сделка без свежей книги своего спота не оценивается — строка «устарела», как при старой ставке
            # (проверка исправлений 12.09: молчащая спот-площадка иначе становилась «лучшей комбинацией» монеты)
            row = calc.build_sf_row(
                it, g(self.instruments, it["perp_ex"], it["perp"]), p, b, spot_b if spot_ok else None,
                self.wsums.get((it["perp_ex"], it["perp"]), {}), now_ms, self.comp.get((it["perp_ex"], it["perp"])),
                stale=st or not spot_ok, dex=dx)
            if dx is not None and spot_ok:
                self.dex.track(it["key"], row.get("gap"), spot_b.get("obs"))
            sf_rows.append(row)
        wmax = str(max(config.WINDOWS_H))
        # свой предел свежести площадки — и чипу страницы (renderStatus берёт h.stale_s, как у OKX DEX)
        health = {ex: (dict(cl.health(), stale_s=_stale_s(ex)) if ex in config.STALE_S_BY_VENUE else cl.health())
                  for ex, cl in self.clients.items()}
        src_age = {v: {k: round(now_s - ts, 1) for k, ts in s.items()} for v, s in self.src_ts.items()}
        return dict(ts=int(self.now()), tick_ts=int(self.last["tick"]), universe_ts=int(self.last["universe"]),
                    incr_ts=int(self.last["incr"]), pid=os.getpid(), started_ts=int(self._started),
                    venues=list(self.venues), ff_rows=ff_rows, sf_rows=sf_rows,
                    spot_venues=list(self.spot_venues) + ([dexleg.VENUE] if self.dex.enabled else []),
                    health=health, src_age=src_age, notes=dict(self.notes), backfill=dict(self.backfill_state),
                    repair=dict(self.repair_state), n_ff=len(ff_rows), n_sf=len(sf_rows),
                    n_mismatch_ff=sum(1 for r in ff_rows if r["mismatch"]),
                    n_mismatch_sf=sum(1 for r in sf_rows if r["mismatch"]),
                    n_unknown_ff=sum(1 for r in ff_rows if r.get("ident") == "unknown"),
                    n_unknown_sf=sum(1 for r in sf_rows if r.get("ident") == "unknown"), ident=dict(self.ident_summary),
                    n_stale_ff=sum(1 for r in ff_rows if r["stale"]), n_stale_sf=sum(1 for r in sf_rows if r["stale"]),
                    n_incomplete_ff=sum(1 for r in ff_rows if r["windows"][wmax]["incomplete"]),
                    n_incomplete_sf=sum(1 for r in sf_rows if r["windows"][wmax]["incomplete"]),
                    disk_free_gb=round(self.disk_free_gb(), 2), windows=list(config.WINDOWS_H),
                    window_labels={str(w): config.WINDOW_LABELS.get(w, f"{w} ч") for w in config.WINDOWS_H})

    def write_table(self, table: dict):
        from .market_snapshot import publish_health
        table = dict(table, schema_version=1, snapshot_id=str(time.time_ns()), generated_at=time.time())
        tmp = str(self.table_path) + ".tmp"
        os.makedirs(os.path.dirname(str(self.table_path)), exist_ok=True)
        # dumps целиком, а не dump кусками: dump идёт через iterencode и тысячи мелких write — на 19.5 МБ (8 площадок,
        # 12.09) это 4.6 с тика на ireland; dumps кодирует одним проходом C-кодировщика
        body = json.dumps(table, separators=(",", ":"))
        with open(tmp, "w") as f:
            f.write(body)
        os.replace(tmp, self.table_path)
        publish_health(table, self.table_path)

    def disk_free_gb(self) -> float:
        try:
            return shutil.disk_usage(os.path.dirname(str(self.db_path)) or ".").free / 1e9
        except Exception:  # noqa
            return 0.0

    def step_snapshot(self, table: dict):
        if self.disk_free_gb() < config.DISK_MIN_FREE_GB:
            self.notes["snapshot"] = f"диск: свободно {self.disk_free_gb():.1f} ГБ < {config.DISK_MIN_FREE_GB} — снимки не пишутся"
            return
        db.insert_ff_snapshots(self.con, table["ts"], table["ff_rows"])
        db.insert_sf_snapshots(self.con, table["ts"], table["sf_rows"])
        self.notes.pop("snapshot", None)
        self.last["snapshot"] = self.now()

    def step_health(self):
        rows = [dict(cl.health(), note=self.notes.get(f"tick:{ex}")) for ex, cl in self.clients.items()]
        db.write_health(self.con, rows)

    # --- фоновое обслуживание истории -----------------------------------------------------------------
    def _spawn(self, name: str, fn):
        if not self.background:
            fn()
            return
        try:
            threading.Thread(target=fn, name=name, daemon=True).start()
        except Exception as e:  # noqa — поток не стартовал: задание не должно остаться «занятым»
            self._maint_running = False
            self._note("maintenance", e)

    def _run_backfill(self, con, clients, legs, iv, days, force, deadline=None):
        self.backfill_state.update(running=True, done=0, total=len(legs), stats=None)
        try:
            def prog(i, n, st):
                # только счётчики страницы. Ревью 13.09: флаг «грязно» здесь заставлял КАЖДЫЙ тик прохода перечитывать
                # 30 сут событий из БД (14 площадок: ~1.5 млн строк, 2–4 с на главном потоке). Новые строки видны и так —
                # события перечитываются раз в 60 с (refresh_events), а после прохода флаг ставит _background
                self.backfill_state.update(done=i, total=n, stats=dict(st))
            stop = lambda: self._stop or (deadline is not None and time.time() > deadline)
            st = funding.backfill(con, clients, legs, iv, days, stop=stop, progress=prog, force=force,
                                  report=lambda leg, ok: self._report(leg, ok, "deep"))
            self.backfill_state.update(stats=st)
            log.info("бэкфилл: %d ног в очереди, обработано %d: %s", len(legs), self.backfill_state["done"], st)
        finally:
            self.backfill_state.update(running=False, finished_ts=int(self.now()))

    def _run_repair(self, con, clients, gaps, iv):
        self.repair_state.update(running=True, n_symbols=len(gaps), n_new=0)
        try:
            n = funding.repair(con, clients, gaps, iv, report=lambda leg, ok: self._report(leg, ok, "repair"))
            self.repair_state.update(n_new=n)
            log.info("полнота: у %d ног не хватало свежих расчётов, добрано %d", len(gaps), n)
        finally:
            self.repair_state.update(running=False, finished_ts=int(self.now()))

    def _background(self, name: str, body):
        """Обёртка фонового задания: соединение открывается ВНУТРИ защищённого блока (ревью 11.09, п.7) — любая
        ошибка, включая недоступную БД, снимает флаг «занято» и остаётся заметкой, следующий проход повторит."""
        def run():
            con = None
            try:
                con = db.connect(self.db_path)     # своё соединение: sqlite и потоки
                body(con)
                self.notes.pop(name, None)
            except Exception as e:  # noqa
                log.exception("%s упало", name)
                self._note(name, e)
            finally:
                self._events_dirty = True
                self.repair_state["running"] = False
                self.backfill_state["running"] = False
                self._maint_running = False
                if con is not None:
                    try:
                        con.close()
                    except Exception:  # noqa
                        pass
        return run

    def start_maintenance(self, gaps: dict[tuple[str, str], dict] | None, deep: list[tuple[str, str]] | None,
                          days: int = config.HISTORY_DAYS, fresh: dict | None = None):
        """Одно фоновое задание: добор свежих расчётов всех ног, потом глубина — не дольше бюджета.
        fresh — входы окна (step_completeness в фоне): очередь строится в самом задании по свежему пересчёту окна."""
        if self._maint_running:
            return
        self._maint_running = True
        iv, clients = self.intervals(), self._bg_clients()

        def body(con):
            nonlocal gaps, deep
            if fresh is not None:
                gaps, deep = self._plan(self._run_events(fresh).comp)
            if gaps:
                self._run_repair(con, clients, gaps, iv)
                self._events_dirty = True
            if deep:
                self._run_backfill(con, clients, list(deep), iv, days, force=True,
                                   deadline=time.time() + config.MAINT_DEEP_BUDGET_S)
        self._spawn("maintenance", self._background("maintenance", body))

    def start_backfill(self, days: int = config.HISTORY_DAYS, legs: list[tuple[str, str]] | None = None):
        """Разовый бэкфилл (CLI и тесты); обычный путь — start_maintenance из проверки полноты."""
        if self._maint_running:
            return
        self._maint_running = True
        legs, iv, clients = list(legs if legs is not None else self.legs()), self.intervals(), self._bg_clients()
        legs = [k for k in legs if not own_history(self.clients.get(k[0]))]    # 13.09: Variational — см. own_history
        self._spawn("backfill", self._background("maintenance", lambda con: self._run_backfill(con, clients, legs, iv, days, force=False)))

    # --- цикл -------------------------------------------------------------------------------------------
    def once(self):
        """Один проход всех тактов, чьё время пришло. Возвращает таблицу."""
        t = self.now()
        ph, p0 = {}, [time.perf_counter()]
        self._phases = ph                                      # раскладка медленного тика — в журнал (run)

        def phase_done(name: str):                             # не «mark»: ниже mark = settle_mark(t)
            p1 = time.perf_counter()
            ph[name] = round(p1 - p0[0], 2)
            p0[0] = p1
        self._guard("collect", self._collect)                  # задания прошлых проходов, успевшие закончиться
        self._guard("schedule", self._schedule_aux, t)         # вселенная / интервалы / досбор — кому пора
        self._guard("events_warm", self._warm_events)          # первый проход: окно событий грузится, пока ждём сеть
        phase_done("задания")
        self._guard("tick", self.step_tick)                    # тик + ожидание всей сети не дольше дедлайна
        phase_done("сеть")
        mark = settle_mark(t)
        wait_batch = False
        if t >= mark and self.last["completeness"] < mark:
            self._repair_due = True
            # в hh:10 — сначала пакетный досбор всех площадок (курсоры продлены), потом проверка
            wait_batch = t < mark + SETTLE_WAIT_S and any(self._settled.get(v) != mark for v in self.venues)
        if self._repair_due and (self.ff or self.sf) and not wait_batch:
            self._guard("completeness", self.step_completeness)
        phase_done("полнота")
        self.refresh_events()                   # раз в минуту: события окна из БД, полнота, суммы окон
        phase_done("окна")
        table = self.build_table()
        phase_done("таблица")
        if t - self.last["snapshot"] >= config.SNAPSHOT_S:
            self._guard("snapshot", self.step_snapshot, table)
            self._guard("health", self.step_health)
        # чистка с чекпойнтом WAL(TRUNCATE) ждёт, пока читатели не отпустят WAL (busy timeout — 30 с), а поток «events»
        # держит транзакцию чтения всю полную перезагрузку (ireland ~11–22 с). Раньше чтение шло на этом же потоке и
        # пересечься с чисткой не могло (состязательная проверка 13.09). Окно занято — чистка в следующем тике; пока идёт
        # она, окно не начинает чтение (тот же замок)
        if t - self.last["purge"] >= 86400 and self._evw.lock.acquire(blocking=False):
            try:
                self._guard("purge", db.purge_snapshots, self.con)
            finally:
                self._evw.lock.release()
            self.last["purge"] = t
        phase_done("снимок")
        table["notes"] = dict(self.notes)       # заметки шагов ПОСЛЕ снимка — иначе «диск полон» виден лишь на следующем тике
        self.write_table(table)
        phase_done("запись")
        return table

    def run(self):
        log.info("коллектор стартует: %s + споты %s", ", ".join(self.venues), ", ".join(self.spot_venues))
        try:
            while not self._stop:
                t0 = self.now()
                self.once()
                dt = self.now() - t0
                if dt > config.TICK_S:
                    log.warning("тик занял %.1f с > %d: %s", dt, config.TICK_S, getattr(self, "_phases", {}))
                self.sleep(max(0.5, config.TICK_S - dt))
        finally:
            self._pool.shutdown(wait=False, cancel_futures=True)
            if self._ev_pool is not None:
                self._ev_pool.shutdown(wait=False, cancel_futures=True)

    def stop(self):
        self._stop = True
