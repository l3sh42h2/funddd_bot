"""Окно событий фандинга в памяти (13.09): история за 721 ч по ногам, полнота и суммы окон — без перечитывания БД.

Было: refresh_events раз в минуту перечитывал из БД ВСЕ события 30 сут (14 площадок, ~5 400 ног, ~1.5 млн строк) и
заново считал полноту и суммы — фаза «окна» шла 11–22 с на главном потоке, тик 15–30 с вместо 10.

Теперь:
- ряды ног живут в памяти, отсортированные по времени; каждый пересчёт берёт из БД только строки с rowid больше
  последнего виденного (funding_events только вставляется: новая строка всегда получает rowid больше всех живых) и
  вливает их на своё место — бэкфилл кладёт СТАРЫЕ строки не по порядку. Слева отрезается то, что вышло из окна;
- ряд ноги никогда не меняется на месте: добор и обрезка дают новый список. Прежний вид (события, полнота, суммы) мог
  уже уйти главному потоку — он остаётся целым, пока тот не подставит новый;
- полнота — funding.completeness по рядам в памяти для ВСЕХ ног, каждый раз (замер 13.09 на синтетической БД — в
  PROFILE ниже): почти каждое её поле зависит от «сейчас» — n окна, ожидаемый расчёт по сетке, pending_ms, «мелкая»,
  плановый досбор; пересчёт «только тронутых и тех, у кого что-то сдвинулось со временем» дублировал бы логику
  полноты, и забытое условие молча застыло бы флагом «неполное» на странице. Сама функция — та же, что раньше;
- суммы окон — только у ног, чей ряд сменился или чья граница окна перешла через расчёт (calc.window_bounds): суммы —
  функция ряда и границ, остальным ногам прежний результат подходит до бита;
- полная перезагрузка, как раньше (db.funding_events_since), — при первом пересчёте процесса, раз в FULL_RELOAD_S и
  всегда, когда число строк окна в памяти разошлось с COUNT(*) в БД (удаление, чистка) или сменился хвост по rowid
  (удалили последние строки — SQLite мог отдать их rowid новым строкам, «rowid > последнего» их бы не увидел).
Все чтения одного пересчёта — в одной транзакции чтения (WAL): добор, проверки и курсоры ног из одного снимка БД.
Строка, которую удалили и вставили заново с тем же ключом, но другой ставкой, под тем же rowid и глубже хвоста, —
UPDATE под видом вставки; такого таблица не делает, а от любой такой хитрости страхует полная перезагрузка раз в 30 мин.

PROFILE 13.09 (Mac; синтетика: 5 400 ног, 1 ч ~60 % / 4 ч / 8 ч, 30 сут, дыры, мелкие, без курсора, отложенный последний
расчёт; 1.95 млн строк окна). Было, на главном потоке раз в минуту: перечитывание 1.76 с + полнота 0.22 с + суммы 0.18 с
= 2.16 с (на ireland то же — 11–22 с). Стало, в потоке «events»: тихая минута — чтение 0.017 с, полнота+суммы 0.19 с;
hh:00 (+4 000 расчётов) — 0.05 + 0.45 с; бэкфилл вразброс — 0.02 + 0.20 с; полная (старт, раз в 30 мин, удаление) —
1.76 + 0.45 с. Главному потоку — снимок входов 2 мс на запуск и подстановка трёх ссылок; при 30 % его загрузки счётом
пересчёт в потоке не задерживает его куски дольше 0.1 мс (полная перезагрузка идёт 3 с вместо 2.2 — делит GIL).
"""
from __future__ import annotations
import logging, threading, time
from bisect import bisect_left
from dataclasses import dataclass
from operator import itemgetter
from . import config, db, funding, calc

log = logging.getLogger(__name__)

H_MS = 3600_000
FULL_RELOAD_S = 1800        # полная перезагрузка окна не реже раза в 30 мин (часы коллектора)
TAIL_K = 64                 # сколько последних строк по rowid сверяется целиком
_MS = itemgetter(0)


def window_lo(now_ms: int) -> int:
    """Левый край окна событий: самое длинное окно страницы + час (как было в refresh_events)."""
    return now_ms - (max(config.WINDOWS_H) + 1) * H_MS


@dataclass(frozen=True)
class EventsView:
    """Готовый вид окна: главный поток подставляет его целиком. Словари и ряды внутри после выдачи не меняются."""
    events: dict            # (площадка, символ) -> [(funding_ms, rate), ...] по возрастанию времени
    comp: dict              # карта полноты (funding.completeness)
    wsums: dict             # суммы окон ноги (calc.window_sums)
    now_ms: int
    gen: int                # номер пересчёта: подставляется только вид новее текущего
    legs: int               # ног в карте полноты
    full: str | None        # причина полной перезагрузки; None — добор
    rows: int               # строк окна в памяти
    new: int                # строк, взятых добором
    t_load: float           # чтение БД, с
    t_calc: float           # полнота и суммы, с


def _merge(old: list | None, add: list) -> list:
    """Ряд ноги + новые строки → НОВЫЙ отсортированный ряд. Тот же момент расчёта уже в ряду — берётся строка из БД
    (прежнюю удалили и вставили заново: INSERT OR IGNORE иначе дубля не пустил бы)."""
    add.sort()
    if not old:
        return add
    if add[0][0] > old[-1][0]:                 # обычный случай — новые расчёты позже всех
        return old + add
    d = dict(old)
    d.update(add)
    return sorted(d.items())


class EventWindow:
    """Окно событий в памяти. Один пересчёт за раз (lock): его зовут и поток окна, и задание обслуживания."""

    def __init__(self, con=None, path=None, full_reload_s: float = FULL_RELOAD_S, tail_k: int = TAIL_K):
        # con — чужое соединение, пересчёт в том же потоке (коллектор без фона, тесты); иначе своё по path — открывается
        # при первом пересчёте и живёт, пока не упадёт
        self._con, self._path, self._own = con, path, con is None
        self.full_reload_s, self.tail_k = full_reload_s, tail_k
        self.lock = threading.Lock()
        self.gen = 0
        self._reset()

    def _reset(self):
        self.lists: dict[tuple[str, str], list[tuple[int, float]]] = {}
        self.n = 0                     # строк во всех рядах
        self.rowid: int | None = None  # последний виденный rowid (MAX на момент снимка); None — окна нет
        self.tail: list[tuple] = []
        self.lo: int | None = None
        self.full_ms = 0
        self._ws: dict = {}            # нога -> (ряд, его времена, границы окон, суммы)

    def busy(self) -> bool:
        return self.lock.locked()

    def _connect(self):
        if self._con is None:
            self._con = db.connect(self._path)
        return self._con

    def _drop_con(self):
        if self._own and self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa
                pass
            self._con = None

    # --- чтение БД -----------------------------------------------------------------------------------------------
    def _why_full(self, con, now_ms: int) -> str | None:
        if self.rowid is None:
            return "старт"
        if abs(now_ms - self.full_ms) >= self.full_reload_s * 1000:
            return "30 мин"
        if db.funding_tail(con, self.rowid, self.tail_k) != self.tail:
            return "хвост rowid"
        return None

    def _load(self, con, now_ms: int, lo: int):
        self._reset()
        top = db.funding_rowid_max(con)
        self.lists = db.funding_events_since(con, lo)
        self.n = sum(map(len, self.lists.values()))
        self.rowid, self.tail, self.lo, self.full_ms = top, db.funding_tail(con, top, self.tail_k), lo, now_ms

    def _advance(self, con, lo: int) -> int:
        """Добор: строки с rowid после последнего + (если окно ушло влево) полоса слева; потом обрезка слева."""
        top = db.funding_rowid_max(con)
        rows = db.funding_rows_after(con, self.rowid, top, lo) if top > self.rowid else []
        if lo < self.lo:
            rows = rows + db.funding_rows_band(con, lo, self.lo, self.rowid)
        add: dict[tuple[str, str], list[tuple[int, float]]] = {}
        for ex, sym, ms, rate in rows:
            add.setdefault((ex, sym), []).append((ms, rate))
        for leg, new in add.items():
            old = self.lists.get(leg)
            merged = _merge(old, new)
            self.n += len(merged) - (len(old) if old else 0)
            self.lists[leg] = merged
        if lo > self.lo:
            gone = []
            for leg, lst in self.lists.items():
                if lst[0][0] < lo:
                    i = bisect_left(lst, lo, key=_MS)
                    self.n -= i
                    if i == len(lst):
                        gone.append(leg)
                    else:
                        self.lists[leg] = lst[i:]          # новый список: прежний мог уйти в выданный вид
            for leg in gone:
                del self.lists[leg]
        self.lo = lo
        self.rowid, self.tail = top, db.funding_tail(con, top, self.tail_k)
        return len(rows)

    # --- пересчёт ------------------------------------------------------------------------------------------------
    def _sums(self, events: dict, comp: dict, now_ms: int) -> dict:
        """Суммы окон всех ног ряда; прежний результат ноги — если её ряд тот же объект и границы окон те же."""
        out, cache = {}, {}
        for k, evs in events.items():
            anchor = calc.window_anchor(comp.get(k), now_ms)
            c = self._ws.get(k)
            ms = c[1] if c is not None and c[0] is evs else [m for m, _ in evs]
            b = calc.window_bounds(ms, anchor)
            if c is not None and c[0] is evs and c[2] == b:
                out[k] = c[3]
                cache[k] = c
                continue
            s = calc.window_sums(evs, anchor)
            out[k] = s
            cache[k] = (evs, ms, b, s)
        self._ws = cache
        return out

    def refresh(self, now_ms: int, legs: list[tuple[str, str]], intervals: dict[str, dict[str, int]],
                next_ms: dict[str, dict[str, int]]) -> EventsView:
        """Вид окна на момент now_ms: добор из БД (или полная перезагрузка), полнота всех ног, суммы окон."""
        with self.lock:
            t0 = time.perf_counter()
            lo = window_lo(now_ms)
            try:
                con = self._connect()
                own_tx = not con.in_transaction
                if own_tx:
                    con.execute("BEGIN")               # один снимок БД на все чтения пересчёта
                try:
                    why, n_new = self._why_full(con, now_ms), 0
                    if why is None:
                        n_new = self._advance(con, lo)
                        if self.n != db.count_funding_events_since(con, lo):
                            why = "число строк"
                    if why is not None:
                        self._load(con, now_ms, lo)
                    depth, sync = db.leg_depths(con), db.leg_sync(con)
                finally:
                    if own_tx and con.in_transaction:
                        con.commit()                   # конец транзакции чтения
                t1 = time.perf_counter()
                events = dict(self.lists)
                comp = funding.completeness(events, legs, intervals, now_ms, max(config.WINDOWS_H), next_ms,
                                            depth=depth, sync=sync)
                wsums = self._sums(events, comp, now_ms)
            except BaseException:
                self._reset()                          # состояние могло остаться наполовину — следующий раз с нуля
                self._drop_con()
                raise
            t2 = time.perf_counter()
            self.gen += 1
            view = EventsView(events=events, comp=comp, wsums=wsums, now_ms=now_ms, gen=self.gen, legs=len(comp),
                              full=why, rows=self.n, new=n_new, t_load=round(t1 - t0, 3), t_calc=round(t2 - t1, 3))
            if why is not None:
                log.info("окно событий: полная перезагрузка (%s) — %d строк, %d ног; чтение %.2f с, пересчёт %.2f с",
                         why, self.n, len(events), t1 - t0, t2 - t1)
            return view
