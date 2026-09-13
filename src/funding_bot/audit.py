"""Тестировщик — независимая сверка дашборда с истиной биржи.

Запуск по команде владельца на VPS:  funding_bot audit --venue <площадка>  |  funding_bot audit --all
Владелец 13.09: «после сборки запусти тестировщика, который будет проверять наличие токенов в дашборде, фандинги».

НЕЗАВИСИМОСТЬ — главное свойство. Истину о бирже дают модули audit_truth/<площадка>.py: сырые запросы к публичному API
биржи и свой разбор, без клиентов коллектора (иначе тестировщик повторял бы их ошибки). Аудитор здесь общий для всех
площадок и тоже не пользуется кодом коллектора (universe / calc / venues / symbols): он читает только то, что видит
владелец (runtime/table.json) и что коллектор записал (runtime/funding_bot.db, read-only). Правила из config — это
решения владельца (PERP_UNPAIRED, PERP_CANON, SPOT_ALIASES, CATCHUP_S), а не код коллектора.

Проверки площадки:
  1. присутствие — каждый торгуемый рынок биржи есть на дашборде ногой (ff sa/sb или sf perp); нет — почему:
     не с чем паровать (ни у одной перп- и спот-площадки в БД нет той же базы) — OK; класс пар не строит или рынок
     в config.PERP_UNPAIRED — OK с причиной; дубль по квоте (монета уже на дашборде другим рынком) — OK; иначе ОШИБКА
     «рынок торгуется, пары есть, а на дашборде нет». Нога дашборда, чей рынок на бирже не торгуется, — ОШИБКА;
  2. текущий фандинг — ставка дашборда в час против ставки биржи / интервал; допуск max(2e-6 в час, 10 % от биржи);
     интервал равен; следующий расчёт в пределах 5 мин. Расхождение перепроверяется на следующем тике дашборда (прогнозная
     ставка живёт), в ошибки идут устойчивые. Системный множитель (все ×8, ×24, ×1/8, ставка за интервал вместо часа,
     перевёрнутый знак) называется явно;
  3. окна истории. Сначала КАЖДАЯ нога площадки сверяется с БД коллектора на момент снимка (без запросов к бирже): окно =
     сумме расчётов БД, нет дыры между расчётами, последний расчёт по слову биржи на месте, нет «расчёта» из будущего
     (записанный прогноз — сразу ошибка). Подозрительные ноги (до MAX_ESCALATE) добавляются к выборке (--top верх и низ по
     ставке дашборда, --sample случайных) — и у всех них сумма расчётов биржи за 24/72/168 ч сверяется с окном дашборда с
     тем же якорем, что у дашборда (calc.window_anchor: расчёт, которого ещё нет в БД, в окно не входит); совпасть должны
     сумма (до округления) и число расчётов. «Граница» прощается, только если перестановка расчётов у самых краёв окна
     даёт ровно окно дашборда; причина расхождения называется по расчётам (нет в БД / лишний / другое значение / окно не
     из БД). Окно, которое дашборд сам пометил «!» (у всех строк ноги), — честная неполнота, а не ошибка. Площадка без
     публичной истории: окна дашборда должны сходиться с тем, что коллектор накопил в БД, и быть помечены «!», пока
     накоплено меньше окна. Плюс расчёты биржи против БД (нет в БД / лишние / другие значения) — предупреждения.
  4. нога в разных строках дашборда (разные пары) показана одинаково: ставка, интервал, время расчёта, окна — иначе ОШИБКА.
Только чтение: БД открывается read-only, в table.json ничего не пишется, ордеров нет, ключей нет.
Отчёт — runtime/audit/<площадка>_<время>.md и .json; --all — ещё сводка runtime/audit/ALL_<время>.md.
Код выхода: 0 — ошибок нет, 1 — есть.
"""
from __future__ import annotations
import json, time, sqlite3, random, logging, threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from . import config
from .audit_truth import TRUTHS, TRUTH_ERRORS
from .audit_truth.base import norm_base, window_sum, H_MS

log = logging.getLogger(__name__)

H = H_MS
HISTORY_GRACE_MS = 15 * 60_000        # расчёты моложе этого в БД может ещё не быть — в сверке БД не участвуют
WINDOW_EDGE_MS = 3 * 60_000           # расчёт у самой границы окна — несовпадение суммы объяснимо сдвигом часов
ANCHOR_LAG_MS = 60_000                # суммы окон коллектор пересчитывает раз в минуту, table.json пишет каждый тик
RATE_REL_TOL = 0.10                   # ставка биржи против нашей: 10 % от биржи …
RATE_ABS_TOL = 2e-6                   # … или 2e-6 в час — что больше (ниже — шум между тиком коллектора и проверкой)
RATE_RECHECKS = 2                     # перепроверок расхождения ставки, каждая — на следующем тике дашборда (live_band)
RECHECK_WAIT_S = 40.0                 # ждать тика, снятого ПОСЛЕ прошлого чтения биржи: тик 10 с, а с записью окна бывает 20-30 с
RECHECK_POLL_S = 1.5                  # как часто перечитывать table.json в ожидании тика
OBS_SLACK_S = 2.0                     # ts таблицы — целые секунды, src_age — десятые: «снят после чтения» с таким запасом
NEXT_TOL_MS = 5 * 60_000              # время следующего расчёта
HIST_WINDOWS = (24, 72, 168)
PENDING = "перед несобранным расчётом"   # край окна дашборда перед расчётом, которого ещё нет в БД (calc.window_anchor)
SUM_ABS_TOL = 1e-9                   # суммы расчитанных ставок: совпадают до округления
SUM_REL_TOL = 1e-6
COLLECTED_TOL_S = 600                 # «в последней вселенной площадки» (строки instruments не удаляются)
RATIOS = (8, 24, 4, 2, 3, 12, 100, 1 / 2, 1 / 3, 1 / 4, 1 / 8, 1 / 12, 1 / 24, 1 / 100)
RATIO_TOL = 0.08
SYSTEMATIC_MIN = 3                    # «системно» — не меньше 3 ног и не меньше половины сравнимых
SYSTEMATIC_SHARE = 0.5
MAX_ESCALATE = 40                     # подозрительных ног (сверка с БД) сверх выборки — на сверку с историей биржи
COMMON_HOLE_MIN = 3                   # одна и та же дыра у стольких ног — общая причина, её сверяют представители …
COMMON_HOLE_REPS = 2                  # … не больше стольких на дыру (сначала ноги выборки)
SEEN_LAG_S = 5                        # БД «на момент снимка»: расчёты, записанные не позже снимка таблицы (+ запас)
# Классы, у которых коллектор строит пары (universe: пара — внутри класса; «rwa»/«other» — нераспознанный рынок, пар нет).
PAIRABLE = {"crypto", "equity", "commodity", "fx", "index", "preipo", None}
SPOT_PAIRABLE = {"crypto", "fx", None}   # спот той же базы — пара только монете и валюте (акции — токены, сырьё — синонимы)

STATUS_RU = {
    "present": "в дашборде",
    "missing": "ОШИБКА: рынок торгуется, пары есть, а на дашборде нет",
    "check": "пара по тикеру есть, но её класс не подтверждён — проверить глазами",
    "ok_twin": "дубль по квоте: монета уже на дашборде другим рынком этой биржи — OK",
    "ok_alone": "не с чем паровать: той же базы нет ни у одной перп- и спот-площадки — OK",
    "ok_class": "класс актива пар не строит — OK",
    "ok_unpaired": "исключён владельцем (config.PERP_UNPAIRED) — OK",
    "ok_other": "тот же тикер у других бирж — другой актив или не торгуется (по бирже пары) — OK",
    "not_tradable": "не торгуется на бирже (делистинг / до запуска) — на дашборде не ждём",
}
STALE_RU = {"absent": "ОШИБКА: нога на дашборде, а такого рынка у биржи нет",
            "untradable": "ОШИБКА: нога на дашборде, а рынок у биржи не торгуется"}


def _cls(c):
    """Класс коллектора → словарь истины: «rwa» (нераспознанный рынок у клиентов коллектора) = «other»."""
    if c is None:
        return None
    c = str(c).lower()
    return "other" if c == "rwa" else c


# --- что показывает дашборд -----------------------------------------------------------------------------
LEG_FIELDS = (("rate_h", "ставка в час"), ("iv", "интервал"), ("next_ms", "время расчёта"))


def _same_num(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= 1e-15 + 1e-9 * max(abs(float(a)), abs(float(b)))


def dashboard_legs(table: dict) -> dict[tuple[str, str], dict]:
    """(площадка, символ) → {rate_h, iv, next_ms, rows, stale_rows, windows{w: {sum, n, incomplete}}, cls, base, conflicts}
    по строкам обеих таблиц. Значения ноги — из первой её строки; у всех строк ноги они обязаны быть одними и теми же
    (коллектор считает ногу один раз). Строка, где нога показана иначе (ставка, интервал, время расчёта, окно), — в
    conflicts: иначе ошибка во второй строке ноги (скажем, знак только на стороне b) проходила бы незамеченной.
    Пометка «!» окна ноги — только если она у ВСЕХ её строк: у строки пары «!» ставится и за чужую ногу, и неполнота
    партнёра не должна извинять расхождение этой ноги."""
    legs: dict[tuple[str, str], dict] = {}

    def add(v, s, rh, iv, nxt, r, wins):
        new = (v, s) not in legs
        L = legs.setdefault((v, s), {"rate_h": rh, "iv": iv, "next_ms": nxt, "rows": [], "stale_rows": 0, "windows": {},
                                     "mismatch": False, "cls": r.get("cls"), "base": r.get("base"), "conflicts": []})
        if not new:
            for f, lab in LEG_FIELDS:
                val = {"rate_h": rh, "iv": iv, "next_ms": nxt}[f]
                if not _same_num(L[f], val):
                    L["conflicts"].append(dict(field=lab, row=r.get("key"), first_row=L["rows"][0], first=L[f], this=val))
        L["rows"].append(r.get("key")); L["mismatch"] |= bool(r.get("mismatch")); L["stale_rows"] += bool(r.get("stale"))
        for w, x in wins.items():
            y = L["windows"].get(w)
            if y is None:
                L["windows"][w] = dict(x)
                continue
            if not _same_num(y.get("sum"), x.get("sum")) or (y.get("n") is not None and x.get("n") is not None
                                                              and int(y["n"]) != int(x["n"])):
                L["conflicts"].append(dict(field=f"окно {w} ч", row=r.get("key"), first_row=L["rows"][0],
                                           first=y.get("sum"), this=x.get("sum")))
            y["incomplete"] = bool(y.get("incomplete")) and bool(x.get("incomplete"))
    for r in table.get("ff_rows", []):
        for side in ("a", "b"):
            wins = {w: dict(sum=x.get(side), n=x.get("n" + side), incomplete=x.get("incomplete"))
                    for w, x in (r.get("windows") or {}).items()}
            add(r["v" + side], r["s" + side], r.get("rate_h_" + side), r.get("iv_" + side), r.get("next_" + side), r, wins)
    for r in table.get("sf_rows", []):
        wins = {w: dict(sum=x.get("spread"), n=x.get("n"), incomplete=x.get("incomplete"))
                for w, x in (r.get("windows") or {}).items()}
        add(r["perp_ex"], r["perp"], r.get("rate_h"), r.get("period"), r.get("next_ms"), r, wins)
    return legs


def dashboard_index(table: dict) -> dict[str, list[tuple[str, str, str | None]]]:
    """База → [(площадка, символ, класс строки)] всех ног дашборда, спотов тоже: пара, которую дашборд уже показывает."""
    idx: dict[str, set] = {}
    for r in table.get("ff_rows", []):
        for side in ("a", "b"):
            idx.setdefault(r.get("base"), set()).add((r["v" + side], r["s" + side], _cls(r.get("cls"))))
    for r in table.get("sf_rows", []):
        idx.setdefault(r.get("base"), set()).add((r["perp_ex"], r["perp"], _cls(r.get("cls"))))
        if r.get("spot_ex") and r.get("spot"):
            idx.setdefault(r.get("base"), set()).add((r["spot_ex"], r["spot"], _cls(r.get("cls"))))
    return {b: sorted(v, key=str) for b, v in idx.items()}


# --- что записал коллектор (read-only) -------------------------------------------------------------------------
def collected_now(con) -> set[tuple[str, str]]:
    """Инструменты последней вселенной каждой площадки (см. COLLECTED_TOL_S)."""
    return set(db_universe(con))


def db_universe(con) -> dict[tuple[str, str], dict]:
    """(площадка, символ) → {base, cls, interval_h} инструментов последней вселенной каждой площадки. Колонки cls в старой
    схеме нет — тогда cls None (класс пары не проверяется по БД, только по строкам дашборда)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(instruments)")}
    pick = lambda c: f"i.{c}" if c in cols else "NULL"
    q = (f"SELECT i.exchange, i.symbol, {pick('base')}, {pick('cls')}, {pick('interval_h')} FROM instruments i "
         "JOIN (SELECT exchange, MAX(last_seen) m FROM instruments GROUP BY exchange) x ON x.exchange = i.exchange "
         "WHERE i.last_seen >= x.m - ?")
    return {(ex, sym): {"base": b, "cls": _cls(c), "interval_h": iv} for ex, sym, b, c, iv in con.execute(q, (COLLECTED_TOL_S,))}


# --- 1. присутствие (чистые функции — их гоняют тесты) -----------------------------------------------------------
def _canon(base: str, cls: str | None) -> str:
    return config.PERP_CANON.get(cls or "", {}).get(base, base)


def presence(venue: str, markets: dict[str, dict], legs: dict, uni: dict, dash_idx: dict | None = None,
             quote_twins: bool = True) -> tuple[list[dict], list[dict]]:
    """Каждому рынку биржи — статус присутствия (STATUS_RU) и причина; плюс ноги дашборда этой площадки без живого рынка.
    uni — db_universe (что коллектор знает), dash_idx — dashboard_index (что дашборд уже показывает)."""
    perp_set, spot_set = set(config.PERP_VENUES), set(config.SPOT_VENUES)
    dash_idx = dash_idx or {}
    by_base: dict[str, list[tuple[str, str, str | None]]] = {}
    for (ex, sym), i in uni.items():
        if i.get("base"):
            by_base.setdefault(i["base"], []).append((ex, sym, i.get("cls")))
    twins: dict[tuple[str, float], list[str]] = {}
    for sym, m in markets.items():
        if (venue, sym) in legs:
            twins.setdefault(norm_base(m.get("base") or sym), []).append(sym)
    rows = []
    for sym, m in markets.items():
        key = (venue, sym)
        dbi = uni.get(key) or {}
        cls = m.get("cls") if m.get("cls") is not None else dbi.get("cls")
        nb, factor = norm_base(m.get("base") or sym)
        bases = {nb, _canon(nb, cls)} | ({dbi["base"]} if dbi.get("base") else set())
        partners, unsure = [], []
        status, why = "present", ""
        if not m.get("tradable"):
            status, why = "not_tradable", m.get("note") or ""
        elif key in legs:
            pass
        elif sym in config.PERP_UNPAIRED.get(venue, {}):
            status, why = "ok_unpaired", config.PERP_UNPAIRED[venue][sym]
        elif cls not in PAIRABLE:
            status, why = "ok_class", f"класс «{cls}»: коллектор не строит пар нераспознанному рынку"
        elif quote_twins and [s for s in twins.get((nb, factor), []) if s != sym]:
            status, why = "ok_twin", "на дашборде: " + ", ".join(s for s in twins[(nb, factor)] if s != sym)[:120]
        else:
            seen = set()

            def put(ex, s, pcls, sure):
                # рынок, который владелец исключил из пар (config.PERP_UNPAIRED: Extended XIAOMI-USD — цена в HKD), ничьей
                # парой не бывает: иначе Gate XIAOMI_USDT без других пар шёл бы «нет на дашборде» (fault-injection 13.09)
                if (ex, s) in seen or s in config.PERP_UNPAIRED.get(ex, {}):
                    return
                seen.add((ex, s)); (partners if sure else unsure).append((ex, s))
            for b in sorted(bases):
                for ex, s, pcls in by_base.get(b, []):
                    if ex == venue:
                        continue
                    if ex in perp_set:
                        if cls is not None and pcls is not None and pcls != cls:
                            continue                               # тот же тикер, другой класс — по замыслу не пара
                        put(ex, s, pcls, pcls is not None or cls in (None, "crypto"))
                    elif ex in spot_set and cls in SPOT_PAIRABLE:
                        put(ex, s, pcls, True)
                for ex, s, pcls in dash_idx.get(b, []):
                    if ex != venue and (cls is None or pcls is None or pcls == cls):
                        put(ex, s, pcls, True)
                # проверенные синонимы «база перпа → база спота» (config.SPOT_ALIASES: золото ← PAXG, SPORTFUN ← FUN)
                for alias, only in config.SPOT_ALIASES.get(cls or "crypto", {}).get(b, []):
                    for ex, s, _pc in by_base.get(alias, []):
                        if ex in spot_set and (only is None or ex in only):
                            put(ex, s, None, True)
            known = "коллектор рынок знает" if dbi else "коллектора рынка нет в последней вселенной (instruments)"
            if partners:
                status = "missing"
                why = f"{known}; пары: " + ", ".join(f"{ex}:{s}" for ex, s in partners[:5]) + \
                      (f" (+{len(partners) - 5})" if len(partners) > 5 else "")
            elif unsure:
                status = "check"
                why = f"{known}; тот же тикер у " + ", ".join(f"{ex}:{s}" for ex, s in unsure[:5]) + \
                      f", класс рынка «{cls}», класс пары в БД не записан"
            else:
                status, why = "ok_alone", "той же базы " + "/".join(sorted(bases)) + " нет ни на одной площадке"
        rows.append(dict(symbol=sym, base=m.get("base"), cls=cls, tradable=bool(m.get("tradable")), status=status, why=why,
                         partners=partners, unsure=unsure, known=bool(dbi), note=m.get("note")))
    stale = []
    for (v, s), L in sorted(legs.items()):
        if v != venue:
            continue
        m = markets.get(s)
        if m is None:
            stale.append(dict(symbol=s, status="absent", why="символа нет в списке рынков биржи", rows=L["rows"][:3]))
        elif not m.get("tradable"):
            stale.append(dict(symbol=s, status="untradable", why=m.get("note") or "", rows=L["rows"][:3]))
    return rows, stale


class PartnerMarkets:
    """Рынки площадок-пар по их же истинам (независимым от коллектора): кэш на прогон, у каждой площадки — один список.
    В --all общий на все площадки (каждая кладёт свой список сама); у одиночного прогона запрашиваются только площадки,
    чьи рынки названы парой у «нет на дашборде». Биржа пары не ответила — None: классификация остаётся как была.
    expect — площадки, которые сверяет этот же прогон (--all): их список ждём от их же прогона, а не второй сессией к той
    же бирже (две сессии не делят темп: у Lighter 8 с между запросами и лимит IP общий с коллектором)."""

    def __init__(self, truths: dict | None = None, expect=(), wait_s: float = 300.0):
        self.truths = truths or {}
        self.wait_s = wait_s
        self._d: dict[str, dict | None] = {}
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._ev = {v: threading.Event() for v in expect}

    def put(self, venue: str, markets: dict | None):
        self._d[venue] = markets
        if venue in self._ev:
            self._ev[venue].set()

    def done(self, venue: str):
        """Прогон площадки кончился (в том числе упал до списка рынков) — ждущие её список больше не ждут."""
        self._d.setdefault(venue, None)
        if venue in self._ev:
            self._ev[venue].set()

    def __call__(self, venue: str) -> dict | None:
        if venue in self._ev:
            self._ev[venue].wait(self.wait_s)
            return self._d.get(venue)
        if venue in self._d:
            return self._d[venue]
        with self._lock:
            lk = self._locks.setdefault(venue, threading.Lock())
        with lk:
            if venue not in self._d:
                t = self.truths.get(venue) or TRUTHS.get(venue)
                try:
                    self._d[venue] = (t() if isinstance(t, type) else t).markets() if t is not None else None
                except Exception as e:  # noqa — без списка пары подтверждения нет, а не падение прогона
                    log.warning("рынки площадки-пары %s: %s", venue, _err(e))
                    self._d[venue] = None
        return self._d[venue]


def confirm_partners(rows: list[dict], markets_of) -> None:
    """«Нет на дашборде, а пара есть» — только если пара и по СВОЕЙ бирже тот же актив и торгуется. В БД коллектора класса
    нет (instruments без cls), и тот же тикер другого класса выглядел парой: QNT (монета Quant) ↔ xyz:QNT / QNT-USD (акция
    Quantinuum), PURR (монета) ↔ PURR (акция), BB, ON, BP (монета ↔ pre-market) — fault-injection 13.09, 5 ложных ошибок
    на чистой таблице. Пара без ответа своей биржи остаётся как была; спот-площадки не проверяются (истин спота нет)."""
    perp = set(config.PERP_VENUES)
    for r in rows:
        if r["status"] not in ("missing", "check"):
            continue
        sure, unsure, dropped = [], [], []
        for lst, is_sure in ((r.get("partners") or [], True), (r.get("unsure") or [], False)):
            for ex, s in lst:
                mk = markets_of(ex) if ex in perp else None
                m2 = None if mk is None else mk.get(s)
                if mk is not None and m2 is None:
                    dropped.append(f"{ex}:{s} — у биржи такого рынка нет")
                elif m2 is not None and not m2.get("tradable"):
                    dropped.append(f"{ex}:{s} — не торгуется")
                elif m2 is not None and r["cls"] is not None and m2.get("cls") is not None and m2["cls"] != r["cls"]:
                    dropped.append(f"{ex}:{s} — другой актив: класс «{m2['cls']}» против «{r['cls']}»")
                elif m2 is not None and m2.get("cls") is not None:
                    sure.append((ex, s))                    # класс пары подтверждён её биржей
                else:
                    (sure if is_sure else unsure).append((ex, s))
        r["partners"], r["unsure"], r["dropped"] = sure, unsure, dropped
        known = "коллектор рынок знает" if r.get("known") else "коллектора рынка нет в последней вселенной (instruments)"
        tail = ("; не пары: " + "; ".join(dropped[:4]) + (f" (+{len(dropped) - 4})" if len(dropped) > 4 else "")) if dropped else ""
        if sure:
            r["status"] = "missing"
            r["why"] = f"{known}; пары: " + ", ".join(f"{ex}:{s}" for ex, s in sure[:5]) + \
                       (f" (+{len(sure) - 5})" if len(sure) > 5 else "") + tail
        elif unsure:
            r["status"] = "check"
            r["why"] = f"{known}; тот же тикер у " + ", ".join(f"{ex}:{s}" for ex, s in unsure[:5]) + \
                       f", класс рынка «{r['cls']}», класс пары не подтверждён" + tail
        else:
            r["status"], r["why"] = "ok_other", "; ".join(dropped[:5])


# --- 2. текущий фандинг ----------------------------------------------------------------------------------------
def truth_hourly(t: dict | None) -> float | None:
    if not t or t.get("rate") is None or not t.get("interval_h"):
        return None
    return float(t["rate"]) / float(t["interval_h"])


def compare_rate(truth_h: float | None, ours_h: float | None) -> dict | None:
    """Расхождение ставки в час; None — сходится (допуск max(RATE_ABS_TOL, RATE_REL_TOL × |биржа|)) или сравнивать нечего."""
    if truth_h is None and ours_h is None:
        return None
    if truth_h is None:
        return {"kind": "у биржи ставки нет", "truth": None, "ours": ours_h, "diff": None}
    if ours_h is None:
        return {"kind": "у нас ставки нет", "truth": truth_h, "ours": None, "diff": None}
    diff = ours_h - truth_h
    if abs(diff) <= max(RATE_ABS_TOL, RATE_REL_TOL * abs(truth_h)):
        return None
    return {"kind": ratio_kind(truth_h, ours_h) or "значение", "truth": truth_h, "ours": ours_h, "diff": diff}


def ratio_kind(truth_h: float, ours_h: float, iv: int | None = None) -> str | None:
    """Отношение наша/биржа, если оно — узнаваемый множитель: «×8», «×1/8», «ЗНАК (×−1)», «ЗНАК и ×8»; интервал ноги
    допишет «= интервал». None — множитель ≈ 1 или ставка биржи ниже шума."""
    if truth_h is None or ours_h is None or abs(truth_h) < RATE_ABS_TOL:
        return None
    r = ours_h / truth_h
    a = abs(r)
    near = lambda x, k: abs(x - k) / k < RATIO_TOL
    if near(a, 1):
        return "ЗНАК (×−1)" if r < 0 else None
    k = next((k for k in RATIOS if near(a, k)), None)
    if k is None:
        return "ЗНАК" if r < 0 else None
    lab = f"×{k:g}" if k >= 1 else f"×1/{1 / k:g}"
    if iv and iv > 1 and near(a, iv):
        lab += f" (= интервал {iv} ч: ставка за интервал вместо часа)"
    elif iv and iv > 1 and near(a, 1 / iv):
        lab += f" (= 1/интервал {iv} ч: поделено на интервал дважды)"
    return f"ЗНАК и {lab}" if r < 0 else lab


def leg_rate_issue(sym: str, t: dict | None, m: dict | None, L: dict) -> dict | None:
    """Ставка, интервал и время расчёта одной ноги; None — всё сходится."""
    th = truth_hourly(t)
    iv_t = (t or {}).get("interval_h") or (m or {}).get("interval_h")
    probs, d = [], compare_rate(th, L.get("rate_h"))
    if d:
        probs.append("ставка")
        if d["truth"] is not None and d["ours"] is not None:
            d["kind"] = ratio_kind(th, L["rate_h"], iv_t) or d["kind"]
    if iv_t and L.get("iv") and int(iv_t) != int(L["iv"]):
        probs.append("интервал")
    nt, no = (t or {}).get("next_ms"), L.get("next_ms")
    if nt and no and abs(int(nt) - int(no)) > NEXT_TOL_MS:
        probs.append("время расчёта")
    if not probs:
        return None
    return dict(symbol=sym, problems=probs, kind=(d or {}).get("kind", "—"), truth_h=th, ours_h=L.get("rate_h"),
                diff=(d or {}).get("diff"), iv_truth=iv_t, iv_ours=L.get("iv"), next_truth=nt, next_ours=no,
                rate_kind=(t or {}).get("kind"), rows=L["rows"][:3], stale=L["stale_rows"] == len(L["rows"]))


def check_rates(venue: str, rates: dict, markets: dict, legs: dict) -> tuple[list[dict], int]:
    """Расхождения по всем ногам площадки на дашборде и число ног, где ставки сравнимы (обе есть и выше шума)."""
    issues, n_cmp = [], 0
    for (v, s), L in legs.items():
        if v != venue:
            continue
        th = truth_hourly(rates.get(s))
        if th is not None and L.get("rate_h") is not None and abs(th) >= RATE_ABS_TOL:
            n_cmp += 1
        x = leg_rate_issue(s, rates.get(s), markets.get(s), L)
        if x:
            issues.append(x)
    issues.sort(key=lambda x: -abs(x["diff"] or 0))
    return issues, n_cmp


def live_band(truths) -> tuple[float, float] | None:
    """Коридор живой ставки биржи по её чтениям (по порядку времени): [min − запас, max + запас], запас = max(допуск
    сравнения, наибольший сдвиг истины между соседними чтениями). Дашборд снят тиком коллектора на 10-30 с раньше, чем
    истина прочитана, а прогнозная ставка (бегущее среднее часа) в первые минуты часа за эти секунды уходит дальше
    допуска. Замер 13.09 «одно мгновение» (клиент коллектора и истина подряд, с Мака): на всех 7 площадках с
    расхождениями прогона 01:02 (hyperliquid, apex, gate, lighter, lighter_rh, backpack, pacifica) в одно мгновение
    ставки совпадают точно, а в прогоне у Gate 11 из 11 и у Lighter 7 из 7 ставка дашборда на перепроверке равна ПЕРВОМУ
    чтению истины — дашборд отстаёт на тик. Ставка дашборда внутри коридора — та, что биржа показывала в эти секунды."""
    xs = [float(x) for x in truths if x is not None]
    if not xs:
        return None
    drift = max((abs(b - a) for a, b in zip(xs, xs[1:])), default=0.0)
    m = max(RATE_ABS_TOL, RATE_REL_TOL * max(abs(x) for x in xs), drift)
    return min(xs) - m, max(xs) + m


def persistent_kind(reads) -> str | None:
    """Множитель или знак, который держится во ВСЕХ снимках (биржа, дашборд) выше шума, — это единицы, а не время:
    коридор live_band его не прощает. Отставание дашборда даёт случайные отношения («×3», потом «×1/2» — HL xyz:QNT
    13.09), ошибка единиц — одно и то же отношение на каждом снимке (×8, «ЗНАК (×−1)», «ЗНАК и ×8»).
    Знак без множителя на каждом снимке — ошибка, только если ставка дашборда не равна НИ ОДНОМУ чтению биржи: ApeX в
    начале часа прыгает между базой +0.0000125 и отрицательной оценкой (замер 13.09 02:01, LDOUSDT: биржа −0.0015 % →
    +0.0013 %, дашборд на тик позже +0.0013 % → −0.0015 %) — знак «перевёрнут» на всех снимках, а дашборд показывает
    прошлое чтение биржи."""
    ks = []
    for t, o in reads:
        if t is None or o is None or min(abs(t), abs(o)) < RATE_ABS_TOL:
            return None
        k = ratio_kind(t, o)
        if k is None:
            return None
        ks.append(kind_base(k))
    if len(set(ks)) == 1 and ks[0] != "ЗНАК":
        return ks[0]
    if all(k.startswith("ЗНАК") for k in ks):
        last = reads[-1][1]
        if any(compare_rate(t, last) is None for t, _o in reads):
            return None                               # дашборд = прошлое чтение биржи: отставание, а не знак
        return "ЗНАК"
    return None


def recheck(venue: str, issues: list[dict], rates2: dict, markets: dict, legs2: dict,
            forgiven: list | None = None) -> tuple[list[dict], int]:
    """Новый снимок биржи и дашборда: остаются только устойчивые расхождения. Возвращает (устойчивые, сколько ушло).
    Ставка уходит из расхождений, если сошлась с новым чтением биржи или легла в коридор живой ставки по ВСЕМ чтениям
    истины этой ноги (live_band) и отношение не держится во всех снимках (persistent_kind). Интервал и время расчёта
    коридор не прощает — они не живут секундами. forgiven — сюда кладутся ноги, прощённые коридором (для отчёта)."""
    kept, gone = [], 0
    for x in issues:
        sym = x["symbol"]
        L2 = legs2.get((venue, sym))
        y = leg_rate_issue(sym, rates2.get(sym), markets.get(sym), L2) if L2 else None
        if not y:
            gone += 1
            continue
        reads = [tuple(r) for r in x.get("reads") or [(x["truth_h"], x["ours_h"])]] + [(y["truth_h"], y["ours_h"])]
        probs = list(y["problems"])
        band = live_band([t for t, _o in reads])
        pk = persistent_kind(reads)
        if "ставка" in probs and y["ours_h"] is not None and band and band[0] <= y["ours_h"] <= band[1] and not pk:
            probs.remove("ставка")
            if forgiven is not None:
                forgiven.append(dict(symbol=sym, band=band, reads=reads))
        if not probs:
            gone += 1
            continue
        drift = max((abs(b[0] - a[0]) for a, b in zip(reads, reads[1:]) if a[0] is not None and b[0] is not None),
                    default=None)
        kept.append(dict(x, problems=probs, reads=reads, drift=drift, persistent=pk,
                         recheck=dict(truth_h=y["truth_h"], ours_h=y["ours_h"], problems=probs)))
    return kept, gone


def dash_obs(table: dict, venue: str) -> float | None:
    """Когда коллектор снял ставки площадки, которые показывает таблица: ts таблицы − возраст ставок площадки (src_age.prem —
    от отправки запроса тика, считается в момент сборки таблицы). obs ноги в строках таблицы нет (calc.build_*_row его не
    копирует) — время одно на площадку; HIP-3 dex HL обновляются не чаще раза в HL_HIP3_TTL_S, их строки старше ещё на
    столько. Без src_age (старая таблица) — время тика."""
    ts = table.get("ts")
    if not ts:
        return None
    age = ((table.get("src_age") or {}).get(venue) or {}).get("prem")
    return float(ts) - float(age) if age is not None else float(table.get("tick_ts") or ts)


def snap_lag(table: dict, venue: str, read: tuple[float, float]) -> dict:
    """Отставание снимка дашборда от чтения биржи (середина запроса ставок, с): сравниваются ставки разного времени —
    это не ошибка, а время; в отчёт отдельной строкой «дашборд отстаёт на N с»."""
    obs = dash_obs(table, venue)
    ts, tick = table.get("ts"), table.get("tick_ts")
    return dict(obs=obs, ts=ts, tick_ts=tick, read=[round(read[0], 1), round(read[1], 1)],
                lag_s=None if obs is None else round((read[0] + read[1]) / 2 - obs, 1),
                write_after_tick_s=None if not ts or not tick else int(ts) - int(tick))


def lag_text(venue: str, lags: list[dict]) -> str | None:
    """«дашборд отстаёт на N с: …» — строка выводов, не ошибка. Первый снимок — тот, на котором родились расхождения
    первого сравнения; перепроверки сверяют снимки, снятые после прошлого чтения биржи."""
    if not lags or lags[0].get("lag_s") is None:
        return None
    x = lags[0]
    fmt = lambda s: _t(int(s * 1000), "%H:%M:%S") if s else "?"
    txt = (f"дашборд отстаёт на {x['lag_s']:.0f} с (не ошибка — время): ставки площадки сняты коллектором {fmt(x['obs'])}, "
           f"таблица записана {fmt(x['ts'])}")
    if x.get("write_after_tick_s") is not None and x["write_after_tick_s"] > config.TICK_S:
        txt += f" — через {x['write_after_tick_s']} с после тика (дольше тика {config.TICK_S} с)"
    txt += f"; биржа прочитана {fmt(x['read'][0])}–{fmt(x['read'][1])} ({x['read'][1] - x['read'][0]:.0f} с)"
    rest = [f"{y['lag_s']:.0f}" for y in lags[1:] if y.get("lag_s") is not None]
    if rest:
        txt += f"; на перепроверках {', '.join(rest)} с"
    if venue == "hyperliquid":
        txt += f"; строки HIP-3 dex старше ещё до {config.HL_HIP3_TTL_S} с (dex обновляются по одному)"
    return txt


def kind_base(kind: str) -> str:
    """Вид без приписки про интервал: «×8 (= интервал 8 ч: …)» → «×8»; «ЗНАК (×−1)» остаётся как есть."""
    return kind.split(" (=")[0]


def systematic(issues: list[dict], n_cmp: int) -> dict | None:
    """Один множитель на большинстве сравнимых ног — это не шум, а единицы: называем его явно."""
    c = Counter(kind_base(x["kind"]) for x in issues if "ставка" in x["problems"] and x["truth_h"] is not None
                and x["ours_h"] is not None and abs(x["truth_h"]) >= RATE_ABS_TOL and x["kind"] not in ("значение", "—"))
    iv_c = sum(1 for x in issues if "интервал вместо часа" in x["kind"])
    if not c or n_cmp <= 0:
        return None
    kind, n = c.most_common(1)[0]
    if iv_c > n:
        kind, n = "×интервал", iv_c
    if n < SYSTEMATIC_MIN or n < SYSTEMATIC_SHARE * n_cmp:
        return None
    text = {"ЗНАК (×−1)": "знак ставки перевёрнут",
            "×интервал": "ставка дашборда = ставка биржи за интервал (не приведена к часу)"}.get(kind, f"ставка дашборда {kind} к бирже")
    return dict(kind=kind, n=n, of=n_cmp, text=f"СИСТЕМНО: {text} — на {n} из {n_cmp} сравнимых ног")


# --- 3. окна истории --------------------------------------------------------------------------------------------
def window_candidates(events, ts_ms: int) -> list[tuple[str, int]]:
    """Возможные правые края окон дашборда: момент снимка, минутой раньше (суммы окон пересчитываются раз в минуту) и —
    если последний расчёт до снимка моложе CATCHUP_S — перед ним (calc.window_anchor: расчёт, которого ещё нет в БД,
    в окно не входит; край — минута расчёта − 1 мс, как у коллектора: nextFundingTime − интервал − 1)."""
    c = [("снимок", ts_ms), ("минутой раньше", ts_ms - ANCHOR_LAG_MS)]
    past = [ms for ms, _ in events if ms <= ts_ms]
    if past:
        p = max(past)
        if ts_ms - p <= config.CATCHUP_S * 1000 + ANCHOR_LAG_MS:
            c.append((PENDING, (p // 60_000) * 60_000 - 1))
    return c


def same_sum(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(SUM_ABS_TOL, SUM_REL_TOL * max(abs(a), abs(b)))


def _n(x) -> int | None:
    try:
        return None if x is None else int(x)
    except (TypeError, ValueError):
        return None


def same_window(ours, n_ours, s, n) -> bool:
    """Окно дашборда = окну истины: сумма до округления и — если дашборд пишет число расчётов — то же число (потерянный
    расчёт с нулевой ставкой сумму не меняет, а число меняет)."""
    return same_sum(ours, s) and (n_ours is None or ours is None or n_ours == n)


def edge_explains(events, anchor: int, w: int, ours, n_ours) -> bool:
    """Расхождение объяснимо только границей, если, переставив расчёты у самых краёв окна (±WINDOW_EDGE_MS: часы биржи и
    коллектора, время расчёта в БД, опущенное к минуте), получаем ровно сумму и число дашборда. Раньше «граница» ставилась
    за ЛЮБОЙ расчёт у края — и в первые минуты после часового расчёта любая ошибка окна часовой площадки проходила «границей»."""
    lo = anchor - w * H
    near = [(ms, float(r)) for ms, r in events if abs(ms - lo) <= WINDOW_EDGE_MS or abs(ms - anchor) <= WINDOW_EDGE_MS]
    if not near or len(near) > 8:
        return False
    s0, n0 = window_sum(events, anchor, w)
    s0 = s0 or 0.0
    for mask in range(1, 1 << len(near)):
        s, n = s0, n0
        for i, (ms, r) in enumerate(near):
            if mask >> i & 1:
                s, n = (s - r, n - 1) if lo < ms <= anchor else (s + r, n + 1)
        if same_window(ours, n_ours, None if n == 0 else s, n):
            return True
    return False


def check_windows(events, dash_windows: dict, ts_ms: int, windows=HIST_WINDOWS, cands=None,
                  fallback: str | None = None) -> dict[str, dict]:
    """Суммы окон: события (биржи, а у площадки без истории — БД) против окон дашборда ноги. Окно, не сошедшееся ни с
    одним краем, описывается (истина, число расчётов, причина) от края `fallback`, если он есть среди краёв, иначе от
    снимка."""
    cands = cands or window_candidates(events, ts_ms)
    out = {}
    for w in windows:
        dw = dash_windows.get(str(w)) or {}
        ours, inc, n_ours = dw.get("sum"), dw.get("incomplete"), _n(dw.get("n"))
        tried = [(lab, a, *window_sum(events, a, w)) for lab, a in cands]
        hit = next((t for t in tried if same_window(ours, n_ours, t[2], t[3])), None)
        lab, a, s, n = hit or next((t for t in tried if t[0] == fallback), tried[0])
        if hit:
            status = "ok"
        else:
            # «граница» — только у краёв по времени снимка (край «перед несобранным расчётом» стоит у расчёта по
            # построению) и только если перестановка расчётов у края даёт ровно окно дашборда
            edge = any(edge_explains(events, a_, w, ours, n_ours) for l_, a_ in cands if l_ != PENDING)
            status = "неполно (!)" if inc else ("граница" if edge else "РАСХОЖДЕНИЕ")
        out[str(w)] = dict(truth=s, n_truth=n, ours=ours, n_ours=n_ours, diff=None if ours is None or s is None else ours - s,
                           incomplete_flag=inc, status=status, anchor=lab if hit else None, anchor_ms=a)
    return out


def window_cause(truth_ev, db_ev, ours, n_ours, anchor: int, w: int) -> str | None:
    """Почему окно не сошлось с биржей — по расчётам, а не только по сумме: каких расчётов биржи нет в БД, какие лишние,
    где другое значение, и равно ли окно дашборда сумме БД (нет — окно посчитано не из БД: ошибка расчёта окна)."""
    lo = anchor - w * H
    T = {ms // 60_000: (ms, float(r)) for ms, r in truth_ev if lo < ms <= anchor}
    D = {ms // 60_000: (ms, float(r)) for ms, r in db_ev if lo < ms <= anchor}
    tl = lambda ks: ", ".join(_t(k * 60_000) for k in sorted(ks)[:4]) + (f" (+{len(ks) - 4})" if len(ks) > 4 else "")
    parts = []
    miss, extra = set(T) - set(D), set(D) - set(T)
    vals = {k for k in set(T) & set(D) if abs(T[k][1] - D[k][1]) > 1e-12 + 1e-9 * abs(T[k][1])}
    if miss:
        parts.append(f"нет в БД расчёта биржи {tl(miss)}")
    if extra:
        parts.append(f"в БД лишний расчёт {tl(extra)} (у биржи его нет)")
    if vals:
        parts.append(f"в БД другое значение {tl(vals)}")
    s_db, n_db = window_sum(db_ev, anchor, w)
    if not same_window(ours, n_ours, s_db, n_db):
        parts.append(f"окно дашборда ({n_ours if n_ours is not None else '?'} расч.) не равно сумме БД ({n_db} расч.) — "
                     "окно посчитано не из собранного")
    if n_ours is not None and n_ours > len(T):
        parts.append(f"в окне дашборда {n_ours} расчётов, у биржи {len(T)} — посчитан расчёт, которого ещё нет")
    return "; ".join(parts) or None


def compare_db(truth_rows, db_rows, cut_ms: int) -> dict:
    """Расчёты биржи против БД коллектора (по минуте расчёта): нет в БД, лишние в БД, другие значения."""
    W = {ms // 60_000: float(r) for ms, r in truth_rows if ms <= cut_ms}
    D = {ms // 60_000: float(r) for ms, r in db_rows if ms <= cut_ms}
    lo = min(W) if W else None
    missing = sorted(k * 60_000 for k in set(W) - set(D))
    extra = sorted(k * 60_000 for k in set(D) - set(W) if lo is None or k >= lo)
    values = [(k * 60_000, W[k], D[k]) for k in sorted(set(W) & set(D)) if abs(W[k] - D[k]) > 1e-12 + 1e-9 * abs(W[k])]
    return dict(n_truth=len(W), n_db=len(D), missing=missing, extra=extra, values=values)


def check_no_history(db_events, dash_windows: dict, ts_ms: int, iv: int | None, next_ms: int | None,
                     windows=HIST_WINDOWS) -> dict[str, dict]:
    """Биржа истории не публикует: окно дашборда = сумма накопленного коллектором в БД, и пока накоплено меньше половины
    окна (по интервалу биржи), окно обязано быть помечено «!»."""
    cands = [("снимок", ts_ms), ("минутой раньше", ts_ms - ANCHOR_LAG_MS)]
    if next_ms and iv:
        cands.append((PENDING, ((int(next_ms) - int(iv) * H) // 60_000) * 60_000 - 1))
    res = check_windows(db_events, dash_windows, ts_ms, windows, cands)
    for w, x in res.items():
        exp = int(w) // int(iv) if iv else None
        x["expected"] = exp
        n = x.get("n_ours") or 0
        if x["status"] != "ok" and not x["incomplete_flag"]:
            x["status"] = "РАСХОЖДЕНИЕ с БД"
        elif x["status"] == "ok" and exp and n < 0.5 * exp and not x["incomplete_flag"]:
            x["status"] = "НЕ ПОМЕЧЕНО"               # накоплено мало, а «!» нет — окно выдаётся за полное
    return res


def screen_leg(db_ev, L: dict, t: dict | None, ts_ms: int, no_history: bool = False,
               windows=HIST_WINDOWS) -> tuple[list[str], list[str]]:
    """Дешёвая сверка ЛЮБОЙ ноги площадки без запросов к бирже → (подозрения, ошибки). Выборка --top/--sample видит
    десятки ног из сотен: ошибка окна у рынка вне выборки раньше не ловилась вовсе. Здесь каждая нога сверяется с тем,
    что лежит в БД на момент снимка (db_ev — (ms, ставка, интервал) с seen_ts не позже снимка):
      - окно дашборда = сумме расчётов БД (у того же края, что у коллектора); нет и «!» нет — подозрение;
      - дыра между расчётами БД больше 1.5 интервала внутри окна без «!» — подозрение (потерян расчёт);
      - нет последнего расчёта по слову биржи (next_ms − интервал) старше досбора, «!» нет — подозрение;
      - у площадки без истории: накоплено меньше половины окна без «!» — подозрение;
      - расчёт в БД не раньше следующего расчёта биржи — ОШИБКА сразу: записан прогноз, а не рассчитанная ставка.
    Подозрение само не ошибка (биржа могла и правда пропустить расчёт): рынок уходит на сверку истории с биржей."""
    reasons, errors = [], []
    ev = sorted((int(e[0]), e[1]) for e in db_ev)
    wins = L.get("windows") or {}
    t = t or {}
    nt, ivt = t.get("next_ms"), t.get("interval_h") or L.get("iv")
    flag = lambda w: bool((wins.get(str(w)) or {}).get("incomplete"))
    cands = window_candidates(ev, ts_ms)
    P = None
    if nt and ivt:
        P = int(nt) - int(ivt) * H
        while P > ts_ms:                                  # ставки сняты позже таблицы: расчёт после снимка не в счёт
            P -= int(ivt) * H
        if ts_ms - P <= config.CATCHUP_S * 1000 + ANCHOR_LAG_MS:
            cands.append((PENDING, (P // 60_000) * 60_000 - 1))
    res = check_windows(ev, wins, ts_ms, windows, cands)
    bad = [w for w, x in res.items() if x["status"] == "РАСХОЖДЕНИЕ"]
    if bad:
        reasons.append("окно " + "/".join(bad) + " ч не равно сумме расчётов в БД")
    ivs = [int(x) for x in (ivt, L.get("iv"), *(e[2] for e in db_ev if len(e) > 2 and e[2])) if x]
    step = max(ivs) if ivs else 8
    times = [ms for ms, _ in ev if ts_ms - (max(windows) + step) * H < ms <= ts_ms]
    holes = []
    for a, b in zip(times, times[1:]):
        if b - a > 1.5 * step * H:
            ws = [w for w in windows if b > ts_ms - w * H and not flag(w)]
            if ws:
                holes.append((a, b))
                reasons.append(f"дыра в БД {_t(a)} → {_t(b)} (интервал {step} ч), окно {'/'.join(map(str, ws))} ч без «!»")
    if P is not None and ts_ms - P > (config.CATCHUP_S + 300) * 1000 and P > ts_ms - 24 * H and not flag(24) \
            and not any(abs(ms - P) <= 5 * 60_000 for ms, _ in ev):
        reasons.append(f"нет последнего расчёта {_t(P)} (по слову биржи), окно 24 ч без «!»")
    if no_history and ivt:
        for w, x in res.items():
            exp = int(w) // int(ivt)
            if exp and (x.get("n_ours") or 0) < 0.5 * exp and not x["incomplete_flag"]:
                reasons.append(f"окно {w} ч: накоплено {x.get('n_ours') or 0} из ~{exp}, «!» нет")
    if nt and int(nt) > ts_ms:
        fut = [ms for ms, _ in ev if ms >= int(nt) - 60_000]
        if fut:
            errors.append(f"в БД расчёт {_t(fut[0])} — не раньше следующего расчёта биржи {_t(int(nt))}: записан прогноз, "
                          "а не рассчитанная ставка (INSERT OR IGNORE его уже не заменит)")
    return reasons, errors, holes


def pick_sample(syms_rates: list[tuple[str, float | None]], top: int, sample: int, rng: random.Random) -> list[str]:
    """Верх и низ по ставке дашборда в час + случайные из остальных."""
    ranked = sorted([x for x in syms_rates if x[1] is not None], key=lambda x: -x[1])
    pick = [s for s, _ in ranked[:top]] + [s for s, _ in ranked[-top:]] if top > 0 else []
    pick = list(dict.fromkeys(pick))
    rest = [s for s, _ in syms_rates if s not in pick]
    rng.shuffle(rest)
    return pick + rest[:max(0, sample)]


# --- прогон ------------------------------------------------------------------------------------------------------
def _read_table(path) -> dict:
    return json.loads(Path(path).read_text())


def _ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def db_events(con, venue: str, start_ms: int, ts_ms: int) -> dict[str, list[tuple[int, float, int | None]]]:
    """Расчёты площадки в БД, какими они были на момент снимка таблицы: символ → [(funding_ms, ставка, интервал)] с
    seen_ts не позже снимка (+SEEN_LAG_S). Записанное позже (досбор после снимка) окна дашборда видеть не могли."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(funding_events)")}
    iv = "interval_h" if "interval_h" in cols else "NULL"
    seen = "seen_ts" if "seen_ts" in cols else "NULL"
    out: dict[str, list] = {}
    lim = ts_ms // 1000 + SEEN_LAG_S
    for sym, ms, r, i, st in con.execute(f"SELECT symbol, funding_ms, rate, {iv}, {seen} FROM funding_events "
                                         "WHERE exchange=? AND funding_ms>=? ORDER BY funding_ms", (venue, start_ms)):
        if st is None or st <= lim:
            out.setdefault(sym, []).append((int(ms), r, i))
    return out


def _fmt_field(field: str, x) -> str:
    if x is None:
        return "—"
    if field == "интервал":
        return f"{x} ч"
    if field == "время расчёта":
        return _t(x)
    return _p(float(x), 5)


def leg_conflicts(venue: str, legs: dict, windows: bool) -> list[dict]:
    """Ноги площадки, показанные в разных строках дашборда по-разному: windows=False — ставка / интервал / время расчёта,
    True — окна. Коллектор считает ногу один раз — разница между строками всегда ошибка сборки строки."""
    out = []
    for (v, s), L in sorted(legs.items()):
        if v != venue:
            continue
        cs = [c for c in L.get("conflicts") or [] if c["field"].startswith("окно") == windows]
        if cs:
            out.append(dict(symbol=s, n=len(cs), rows=sorted({c["row"] for c in cs})[:3], why="нога показана по-разному в строках: " +
                            "; ".join(f"{c['field']} {_fmt_field(c['field'], c['first'])} в {c['first_row']} против "
                                      f"{_fmt_field(c['field'], c['this'])} в {c['row']}" for c in cs[:3])))
    return out


def audit_venue(venue: str, top: int = 20, sample: int = 10, days: int = 7, table_path=None, db_path=None,
                truth=None, recheck_wait_s: float = RECHECK_WAIT_S, rng: random.Random | None = None, markets_of=None) -> dict:
    """Одна площадка → отчёт (dict). Сеть — только через истины бирж, файлы — только на чтение. markets_of(площадка) →
    рынки площадки-пары по её истине (PartnerMarkets) — подтвердить «нет на дашборде»; без него и с подставной истиной
    (тесты) пары не подтверждаются по их биржам."""
    t0 = time.time()
    rng = rng or random.Random()
    table_path = Path(table_path or config.TABLE_PATH)
    db_path = Path(db_path or config.DB_PATH)
    rep: dict = dict(venue=venue, ts=int(t0), fatal=None, warnings=[], presence=None, rates=None, history=None,
                     errors=0, error_parts={}, verdict=[])
    cls = type(truth) if truth is not None else TRUTHS.get(venue)
    if cls is None:
        rep["fatal"] = "нет тестировщика площадки: " + (TRUTH_ERRORS.get(venue) or f"модуль audit_truth/{venue}.py не найден")
        return _finish(rep, t0, None)
    T = truth or cls()
    rep["history_note"] = getattr(T, "history_note", None)
    try:
        markets = T.markets()
    except Exception as e:  # noqa — биржа не ответила: сверять не с чем, это ошибка прогона, а не «OK»
        rep["fatal"] = f"биржа не отдала список рынков: {_err(e)}"
        return _finish(rep, t0, T)
    table = _read_table(table_path)                 # сразу перед ставками: сравниваем в пределах пары секунд
    rates, rates_err = {}, None
    read = (time.time(), time.time())
    try:
        rates = T.rates()
        read = (read[0], time.time())
    except Exception as e:  # noqa
        rates_err = f"биржа не отдала ставки: {_err(e)}"
    legs = dashboard_legs(table)
    ts_ms = int(table.get("ts") or 0) * 1000
    rep.update(table_ts=table.get("ts"), tick_ts=table.get("tick_ts"))
    if venue not in (table.get("venues") or [venue]):
        rep["warnings"].append(f"площадки нет в table.json venues — коллектор её не собирает")
    con = None
    try:
        con = _ro(db_path)
        uni = db_universe(con)
    except Exception as e:  # noqa — без БД присутствие судит по строкам дашборда
        uni = {}
        rep["warnings"].append(f"БД не открылась ({_err(e)}): пары ищутся только по строкам дашборда")

    # 1. присутствие
    prow, stale = presence(venue, markets, legs, uni, dashboard_index(table), getattr(T, "quote_twins", True))
    if markets_of is None and truth is None:
        markets_of = PartnerMarkets()
    if markets_of is not None:
        if hasattr(markets_of, "put"):
            markets_of.put(venue, markets)
        confirm_partners(prow, markets_of)
    site_h = getattr(T, "site_hours", None)
    for r in prow:
        t = rates.get(r["symbol"])
        th = truth_hourly(t)
        r["rate_h"] = th
        r["site"] = None if th is None else (th * site_h if site_h else float(t["rate"]))
        r["site_label"] = f"{site_h}h" if site_h else (f"{t['interval_h']}h" if t else "")
    counts = Counter(r["status"] for r in prow)
    rep["presence"] = dict(counts=dict(counts), rows=prow, stale=stale, n_markets=len(markets),
                           n_tradable=sum(1 for r in prow if r["tradable"]), n_legs=sum(1 for v, _s in legs if v == venue))

    # 2. текущий фандинг — с перепроверками на следующих тиках дашборда. Таблица снята тиком коллектора раньше, чем
    # истина прочитана (13.09: у Gate 11 из 11 расхождений дашборд на перепроверке равен ПЕРВОМУ чтению истины), поэтому
    # первое сравнение — только отбор. Перепроверка сравнивает подобное с подобным: ждёт тик, чьи ставки сняты ПОСЛЕ
    # начала прошлого чтения биржи (dash_obs), — его ставка лежит между прошлым и новым чтением, и сверяется с коридором по
    # всем чтениям истины (live_band), а не с последним. Отставание дашборда — отдельной строкой, не ошибкой
    issues, n_cmp = check_rates(venue, rates, markets, legs) if not rates_err else ([], 0)
    transient, lagged, rounds = 0, [], 0
    lags = [snap_lag(table, venue, read)] if not rates_err else []
    table2 = table
    while issues and recheck_wait_s > 0 and rounds < RATE_RECHECKS:
        rounds += 1
        t_first = table2.get("tick_ts", 0)
        need = read[0] - OBS_SLACK_S
        deadline = time.time() + recheck_wait_s
        fresh = False
        while time.time() < deadline:
            time.sleep(RECHECK_POLL_S)
            try:
                table2 = _read_table(table_path)
            except Exception:  # noqa — файл мог подменяться в этот момент
                continue
            o = dash_obs(table2, venue)
            if table2.get("tick_ts", 0) > t_first and (o is None or o >= need):
                fresh = True
                break
        if not fresh:
            rep["warnings"].append(f"перепроверка {rounds}: за {recheck_wait_s:.0f} с нет тика дашборда со ставками, снятыми "
                                   f"после чтения биржи — сверено с последним снимком (тик {_t(int(table2.get('tick_ts') or 0) * 1000, '%H:%M:%S')})")
        try:
            read = (time.time(), time.time())
            rates2 = T.rates()
            read = (read[0], time.time())
            lags.append(snap_lag(table2, venue, read))
            issues, gone = recheck(venue, issues, rates2, markets, dashboard_legs(table2), lagged)
            transient += gone
        except Exception as e:  # noqa — перепроверка не удалась: остаются расхождения прошлого снимка
            rep["warnings"].append(f"перепроверка ставок не удалась: {_err(e)}")
            break
    rep["rates"] = dict(error=rates_err, n_truth=len(rates), n_compared=n_cmp, issues=issues, transient=transient,
                        lagged=len(lagged), rechecks=rounds, lags=lags, lag_text=lag_text(venue, lags),
                        systematic=systematic(issues, n_cmp),
                        kinds=dict(Counter(kind_base(x["kind"]) for x in issues)),
                        stale_issues=sum(1 for x in issues if x["stale"]))

    rep["rates"]["conflicts"] = leg_conflicts(venue, legs, windows=False)

    # 3. окна истории: сначала КАЖДАЯ нога площадки против БД (без запросов к бирже), затем выборка --top/--sample и
    # подозрительные ноги — против истории биржи
    mine = [(s, L.get("rate_h")) for (v, s), L in legs.items() if v == venue and s in markets]
    picks = pick_sample(mine, top, sample, rng)
    start = ts_ms - max(days * 24, max(HIST_WINDOWS) + 2) * H
    now = int(time.time() * 1000)
    cut = now - HISTORY_GRACE_MS
    dbev = db_events(con, venue, start, ts_ms) if con else {}
    screen, db_errors, leg_holes = {}, [], {}
    no_hist = getattr(T, "history_note", None) is not None
    for (v, s), L in legs.items():
        if v != venue or s not in markets:
            continue
        reasons, errs, hl = screen_leg(dbev.get(s, []), L, rates.get(s), ts_ms, no_hist)
        if reasons:
            screen[s] = reasons
        if hl:
            leg_holes[s] = [(a // 60_000, b // 60_000) for a, b in hl]
        db_errors += [dict(symbol=s, why=e) for e in errs]
    # дыра у многих ног разом — одна причина (биржа не рассчитала час у всех рынков — Pacifica 09.09 00:00 — или коллектор
    # стоял): её сверяют представители (сначала ноги выборки), ноги с этой и только этой дырой поштучно не сверяются
    by_hole: dict[tuple[int, int], list[str]] = {}
    for s, hs in leg_holes.items():
        for k in hs:
            by_hole.setdefault(k, []).append(s)
    common = {k: sorted(v) for k, v in by_hole.items() if len(v) >= COMMON_HOLE_MIN}
    reps: set[str] = set()
    for v_ in common.values():
        r_ = [s for s in v_ if s in picks][:COMMON_HOLE_REPS]
        reps |= set(r_ + [s for s in v_ if s not in r_][:COMMON_HOLE_REPS - len(r_)])
    only_common = {s for s, hs in leg_holes.items() if len(screen.get(s, [])) == len(hs) and all(k in common for k in hs)}
    sus = sorted((s for s in screen if s not in picks and (s not in only_common or s in reps)),
                 key=lambda s: (0 if "не равно" in screen[s][0] else 1, s))
    escalate, unchecked = sus[:MAX_ESCALATE], sus[MAX_ESCALATE:]
    items = []
    for s in picks + escalate:
        L = legs[(venue, s)]
        db_rows = con.execute("SELECT funding_ms, rate FROM funding_events WHERE exchange=? AND symbol=? AND funding_ms>=? "
                              "ORDER BY funding_ms", (venue, s, start)).fetchall() if con else []
        first_db = con.execute("SELECT MIN(funding_ms) FROM funding_events WHERE exchange=? AND symbol=?",
                               (venue, s)).fetchone()[0] if con else None
        why = dict(screen=screen.get(s), escalated=s in escalate)
        try:
            ev = T.history(s, start, now)
        except Exception as e:  # noqa
            items.append(dict(symbol=s, rate_h=L.get("rate_h"), error=_err(e), **why)); continue
        if ev is None:
            t = rates.get(s) or {}
            wins = check_no_history(db_rows, L["windows"], ts_ms, t.get("interval_h") or L.get("iv"), L.get("next_ms"))
            items.append(dict(symbol=s, rate_h=L.get("rate_h"), no_history=True, windows=wins, n_db=len(db_rows),
                              first_db_ms=first_db, **why))
            continue
        ev = sorted((int(ms), r) for ms, r in ev)
        snap = [(ms, r) for ms, r, *_ in dbev.get(s, [])]
        # последнего расчёта биржи на момент снимка в БД ещё не было — край дашборда стоит перед ним (calc.window_anchor):
        # не сошедшееся окно объясняем от этого края. От снимка несобранный по плану расчёт шёл в «нет в БД» (13.09:
        # READYUSDTM 01:00 при снимке 01:02:28 и досборе 01:10 — рядом с настоящей потерей 00:00)
        cands = window_candidates(ev, ts_ms)
        pend = next((a for lab, a in cands if lab == PENDING), None)
        have = {ms // 60_000 for ms, _ in snap}
        wins = check_windows(ev, L["windows"], ts_ms, cands=cands,
                             fallback=PENDING if pend is not None and (pend + 1) // 60_000 not in have else None)
        for w, x in wins.items():
            if x["status"] == "РАСХОЖДЕНИЕ":
                x["cause"] = window_cause(ev, snap, x["ours"], x["n_ours"], x["anchor_ms"], int(w))
        items.append(dict(symbol=s, rate_h=L.get("rate_h"), first_db_ms=first_db, windows=wins,
                          **compare_db(ev, db_rows, cut), **why))
    bad_st = {"РАСХОЖДЕНИЕ", "РАСХОЖДЕНИЕ с БД", "НЕ ПОМЕЧЕНО"}
    checked = {x["symbol"]: x for x in items}
    common_holes = []
    for (ka, kb), v_ in sorted(common.items()):
        got = [checked[s] for s in v_ if s in checked and "error" not in checked[s]]
        bad = [x["symbol"] for x in got if any(w["status"] in bad_st for w in (x.get("windows") or {}).values())]
        others = [s for s in v_ if s not in checked]
        common_holes.append(dict(a=ka * 60_000, b=kb * 60_000, n=len(v_), checked=[x["symbol"] for x in got], bad=bad,
                                 confirmed=None if not got else not bad, n_others=len(others), others=others[:30]))
    rep["history"] = dict(items=items, note=rep["history_note"], windows=list(HIST_WINDOWS), days=days,
                          screened=len(mine), suspects={s: screen[s] for s in sorted(screen)}, escalated=escalate,
                          unchecked=unchecked, db_errors=db_errors, conflicts=leg_conflicts(venue, legs, windows=True),
                          common_holes=common_holes)
    if con:
        con.close()
    return _finish(rep, t0, T)


def _count_errors(rep: dict) -> dict[str, int]:
    parts = {}
    if rep.get("fatal"):
        parts["прогон"] = 1
    p = rep.get("presence")
    if p:
        parts["присутствие"] = p["counts"].get("missing", 0) + len(p["stale"])
    r = rep.get("rates")
    if r:
        parts["ставки"] = (1 if r.get("error") else 0) + sum(1 for x in r["issues"] if not x["stale"]) + \
            len(r.get("conflicts") or [])
    h = rep.get("history")
    if h:
        bad = {"РАСХОЖДЕНИЕ", "РАСХОЖДЕНИЕ с БД", "НЕ ПОМЕЧЕНО"}
        parts["история"] = sum(1 for x in h["items"] if "error" in x) + \
            sum(1 for x in h["items"] if "windows" in x for w in x["windows"].values() if w["status"] in bad) + \
            len(h.get("db_errors") or []) + len(h.get("conflicts") or [])
    return {k: v for k, v in parts.items() if v}


def _finish(rep: dict, t0: float, T) -> dict:
    http = getattr(T, "http", None)
    rep["calls"] = getattr(http, "calls", None)
    rep["elapsed_s"] = round(time.time() - t0, 1)
    rep["error_parts"] = _count_errors(rep)
    rep["errors"] = sum(rep["error_parts"].values())
    rep["verdict"] = verdict(rep)
    return rep


def verdict(r: dict) -> list[str]:
    """Выводы одной строкой на пункт — то, что читается первым."""
    out = []
    if r.get("fatal"):
        out.append(f"НЕ ПРОВЕРЕНО: {r['fatal']}")
    p = r.get("presence")
    if p:
        c = p["counts"]
        if c.get("missing"):
            out.append(f"ПРИСУТСТВИЕ: {c['missing']} торгуемых рынков с парой нет на дашборде")
        by = Counter(x["status"] for x in p["stale"])
        if by.get("absent"):
            out.append(f"ПРИСУТСТВИЕ: {by['absent']} ног дашборда — рынка у биржи нет")
        if by.get("untradable"):
            out.append(f"ПРИСУТСТВИЕ: {by['untradable']} ног дашборда — рынок у биржи не торгуется")
        if c.get("check"):
            out.append(f"проверить глазами: {c['check']} рынков — пара по тикеру, класс пары не подтверждён")
        if p["n_legs"] == 0 and p["n_tradable"]:
            out.append("на дашборде нет ни одной ноги площадки — коллектор её не собирает?")
    rr = r.get("rates")
    if rr:
        if rr.get("error"):
            out.append(f"СТАВКИ: {rr['error']}")
        if rr.get("systematic"):
            out.append(rr["systematic"]["text"])
        real = [x for x in rr["issues"] if not x["stale"]]
        if real:
            kinds = Counter(p_ for x in real for p_ in x["problems"])
            out.append(f"СТАВКИ: {len(real)} устойчивых расхождений ({', '.join(f'{k} {n}' for k, n in kinds.most_common())})")
        if rr.get("stale_issues"):
            out.append(f"ставки: {rr['stale_issues']} расхождений у ног, которые дашборд сам показывает устаревшими (серыми)")
        if rr.get("conflicts"):
            out.append(f"СТАВКИ: {len(rr['conflicts'])} ног показаны в разных строках дашборда по-разному (ставка / интервал / "
                       "время расчёта): " + ", ".join(x["symbol"] for x in rr["conflicts"][:8]))
        if rr.get("lag_text"):
            out.append(rr["lag_text"])                 # время, а не ошибка: в _count_errors не входит
    h = r.get("history")
    if h:
        its = h["items"]
        if h.get("db_errors"):
            out.append(f"ИСТОРИЯ: {len(h['db_errors'])} ног — в БД расчёт не раньше следующего расчёта биржи (записан прогноз): "
                       + ", ".join(x["symbol"] for x in h["db_errors"][:8]))
        if h.get("conflicts"):
            out.append(f"ОКНА: {len(h['conflicts'])} ног — окно ноги разное в разных строках дашборда: "
                       + ", ".join(x["symbol"] for x in h["conflicts"][:8]))
        if h.get("suspects") is not None:
            out.append(f"сверка всех {h.get('screened', 0)} ног площадки с БД: подозрений {len(h['suspects'])}, "
                       f"из них сверено с историей биржи сверх выборки {len(h.get('escalated') or [])}")
        if h.get("unchecked"):
            out.append(f"НЕ СВЕРЕНО с биржей: ещё {len(h['unchecked'])} подозрительных ног сверх лимита {MAX_ESCALATE}: "
                       + ", ".join(h["unchecked"][:10]))
        for ch in h.get("common_holes") or []:
            span = f"{_t(ch['a'])} → {_t(ch['b'])}"
            if ch["confirmed"]:
                out.append(f"дыра {span} у {ch['n']} ног разом — у биржи тоже нет расчёта (сверено на "
                           f"{', '.join(ch['checked'][:3])}) — OK")
            elif ch["confirmed"] is False:
                out.append(f"ОКНА: та же дыра {span}, что у {', '.join(ch['bad'][:3])} (у биржи расчёт есть), ещё у "
                           f"{ch['n_others']} ног — поштучно не сверены: {', '.join(ch['others'][:8])}")
            else:
                out.append(f"дыра {span} у {ch['n']} ног разом — сверить с биржей не удалось")
        if h.get("note") and any(x.get("no_history") for x in its):
            out.append(f"истории у биржи нет: {h['note']}")
        bad = Counter(w["status"] for x in its if "windows" in x for w in x["windows"].values())
        for st in ("РАСХОЖДЕНИЕ", "РАСХОЖДЕНИЕ с БД", "НЕ ПОМЕЧЕНО"):
            if bad.get(st):
                what = {"РАСХОЖДЕНИЕ": "сумма не сходится с биржей и без пометки «!» — дашборд выдаёт неверную сумму за верную",
                        "РАСХОЖДЕНИЕ с БД": "сумма не сходится с накопленным в БД",
                        "НЕ ПОМЕЧЕНО": "накоплено меньше половины окна, а пометки «!» нет"}[st]
                out.append(f"ОКНА: {bad[st]} — {what}")
        if bad.get("неполно (!)"):
            out.append(f"окна: {bad['неполно (!)']} сумм неполны и честно помечены «!»")
        withdb = [x for x in its if "missing" in x]
        ms = sum(len(x["missing"]) for x in withdb); ex = sum(len(x["extra"]) for x in withdb); vs = sum(len(x["values"]) for x in withdb)
        if ms or ex or vs:
            out.append(f"история в БД (предупреждение): нет в БД {ms}, лишних {ex}, другие значения {vs} (по {len(withdb)} рынкам)")
        errs = [x for x in its if "error" in x]
        if errs:
            out.append(f"НЕ ПРОВЕРЕНО: запрос истории упал у {len(errs)} рынков")
    out += r.get("warnings") or []
    return out or ["расхождений не найдено"]


# --- отчёт -------------------------------------------------------------------------------------------------------
def _p(x, d=4):
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def _t(ms, fmt="%m-%d %H:%M"):
    return time.strftime(fmt, time.gmtime(ms / 1000)) if ms else "—"


def summary_line(r: dict) -> str:
    if not r["errors"]:
        return "**Итог: OK — ошибок нет**"
    return f"**Итог: ОШИБОК {r['errors']}** — " + ", ".join(f"{k} {n}" for k, n in r["error_parts"].items())


def render_md(r: dict, top: int) -> str:
    v = r["venue"]
    L = [f"# Тестировщик: {v} — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r['ts']))}", "", summary_line(r), ""]
    p = r.get("presence")
    if p:
        L.append(f"Биржа: {p['n_tradable']} рынков в торгах (+{p['n_markets'] - p['n_tradable']} не торгуются), на дашборде "
                 f"{p['n_legs']} ног площадки. Дашборд снят {_t((r.get('table_ts') or 0) * 1000, '%m-%d %H:%M:%S')} UTC. "
                 f"Запросов к бирже: {r.get('calls')}, {r['elapsed_s']} с.")
    L += ["", "## Выводы", ""] + [f"- {x}" for x in r["verdict"]]
    if r.get("fatal"):
        return "\n".join(L + [""])
    rows = p["rows"]; c = p["counts"]
    L += ["", "## 1. Присутствие в дашборде", ""] + [f"- {STATUS_RU[k]}: **{c.get(k, 0)}**" for k in STATUS_RU] + \
         [f"- {STALE_RU[k]}: **{sum(1 for x in p['stale'] if x['status'] == k)}**" for k in STALE_RU] + [""]
    for st in ("missing", "check"):
        lst = [m for m in rows if m["status"] == st]
        if lst:
            L += [f"### {STATUS_RU[st]} ({len(lst)})", ""] + \
                 [f"- {m['symbol']} ({m['cls'] or 'класс ?'}): {m['why']}" for m in lst[:60]] + \
                 ([f"- … ещё {len(lst) - 60}"] if len(lst) > 60 else []) + [""]
    if p["stale"]:
        L += ["### Ноги дашборда без живого рынка", ""] + \
             [f"- {x['symbol']}: {STALE_RU[x['status']]} ({x['why']}); строки: {', '.join(map(str, x['rows']))}" for x in p["stale"][:60]] + [""]
    for st in ("ok_class", "ok_unpaired", "ok_other"):
        lst = [m for m in rows if m["status"] == st]
        if lst:
            L += [f"{STATUS_RU[st]}: " + ", ".join(f"{m['symbol']} ({m['why']})" for m in lst[:30]), ""]
    trad = sorted([m for m in rows if m["tradable"] and m["site"] is not None], key=lambda m: -m["site"])
    lab = trad[0]["site_label"] if trad and len({m["site_label"] for m in trad}) == 1 else "за интервал"
    L += [f"### Как на сайте: фандинг по убыванию — первые {top} и последние {top}", "",
          f"| # | рынок | фандинг ({lab}, как на сайте) | в час | статус |", "|---|---|---|---|---|"]
    show = trad[:top] + ([None] if len(trad) > 2 * top else []) + (trad[-top:] if len(trad) > top else [])
    seen = set()
    for m in show:
        if m is None:
            L.append("| … | | | | |"); continue
        if m["symbol"] in seen:
            continue
        seen.add(m["symbol"])
        st = STATUS_RU[m["status"]] + (": " + m["why"] if m["why"] and m["status"] != "present" else "")
        L.append(f"| {trad.index(m) + 1} | {m['symbol']} | {_p(m['site'])} {m['site_label']} | {_p(m['rate_h'])} | {st} |")
    rr = r["rates"]
    L += ["", "## 2. Текущий фандинг: биржа против дашборда (в час, устойчивые после перепроверки)", ""]
    if rr.get("error"):
        L.append(f"**{rr['error']}**")
    if rr.get("systematic"):
        L += [f"**{rr['systematic']['text']}**", ""]
    L.append(f"Сравнимых ног: {rr['n_compared']}. Допуск: {RATE_ABS_TOL * 100:.4f} %/ч или {int(RATE_REL_TOL * 100)} % от биржи; "
             f"интервал — равен; следующий расчёт — ±{NEXT_TOL_MS // 60_000} мин.")
    if rr.get("lag_text"):
        L += ["", rr["lag_text"][0].upper() + rr["lag_text"][1:] + ". Первое сравнение — только отбор; перепроверка ждёт тик, "
              "снятый после прошлого чтения биржи, и сверяет его с коридором всех чтений."]
    if rr["issues"]:
        L += ["", "| рынок | биржа | дашборд | что | вид | интервал биржа/наш | след. расчёт биржа/наш | перепроверка биржа/наш "
              "| сдвиг биржи между чтениями |", "|---|---|---|---|---|---|---|---|---|"]
        for x in rr["issues"][:50]:
            rc = x.get("recheck") or {}
            L.append(f"| {x['symbol']}{' (серая)' if x['stale'] else ''} | {_p(x['truth_h'])} | {_p(x['ours_h'])} | "
                     f"{', '.join(x['problems'])} | {x.get('persistent') or x['kind']} | {x['iv_truth']}/{x['iv_ours']} | "
                     f"{_t(x['next_truth'])}/{_t(x['next_ours'])} | "
                     f"{_p(rc.get('truth_h'))} / {_p(rc.get('ours_h'))} | {_p(x.get('drift'))} |")
    else:
        L.append("Устойчивых расхождений нет.")
    if rr.get("transient"):
        L.append(f"Разошлись в первом снимке и ушли на перепроверках ({rr.get('rechecks') or 1}): {rr['transient']}, из них "
                 f"{rr.get('lagged', 0)} — ставка дашборда в коридоре живой ставки биржи по всем её чтениям (дашборд снят тиком "
                 "коллектора раньше истины; множитель или знак во всех снимках коридор не прощает).")
    if rr.get("conflicts"):
        L += ["", f"### Нога показана в разных строках по-разному ({len(rr['conflicts'])})", ""] + \
             [f"- {x['symbol']}: ОШИБКА — {x['why']}" for x in rr["conflicts"][:40]]
    h = r["history"]
    L += ["", f"## 3. История: окна {'/'.join(str(w) for w in h['windows'])} ч против биржи ({len(h['items'])} рынков)", ""]
    if h.get("suspects") is not None:
        L += [f"Сначала все {h.get('screened', 0)} ног площадки на дашборде сверены с БД коллектора (без запросов к бирже): "
              f"окно = сумме расчётов в БД, дыр между расчётами нет, последний расчёт на месте. Подозрений: "
              f"{len(h['suspects'])}; сверх выборки (верх/низ по ставке и случайные) с историей биржи сверено "
              f"{len(h.get('escalated') or [])}.", ""]
    if h.get("db_errors"):
        L += [f"- {x['symbol']}: ОШИБКА — {x['why']}" for x in h["db_errors"][:40]] + [""]
    if h.get("conflicts"):
        L += [f"- {x['symbol']}: ОШИБКА — {x['why']}" for x in h["conflicts"][:40]] + [""]
    if h.get("unchecked"):
        L += [f"Не сверено с биржей (лимит {MAX_ESCALATE}): " + "; ".join(f"{s}: {h['suspects'][s][0]}" for s in h["unchecked"][:30]), ""]
    if h.get("note"):
        L += [f"Истории у биржи нет: {h['note']}. Окна сверяются с накопленным в БД и с пометкой «!».", ""]
    wl = {"24": "1 день", "72": "3 дня", "168": "1 неделя"}
    L += ["| рынок | в час | расч. биржа/БД | нет в БД | лишние | значения | " + " | ".join(wl[str(w)] for w in h["windows"]) +
          " | первый в БД | почему в сверке |", "|---|---|---|---|---|---|" + "---|" * len(h["windows"]) + "---|---|"]
    causes = []
    for x in h["items"]:
        whyp = ("подозрение: " + "; ".join(x["screen"])) if x.get("escalated") else ("выборка" + (
            " + подозрение: " + "; ".join(x["screen"]) if x.get("screen") else ""))
        for w, y in (x.get("windows") or {}).items():
            if y.get("cause"):
                causes.append(f"- {x['symbol']}, окно {w} ч: {y['cause']}")
        if "error" in x:
            L.append(f"| {x['symbol']} | {_p(x.get('rate_h'))} | ошибка: {x['error']} |" + " |" * (4 + len(h["windows"])) +
                     f" {whyp} |"); continue
        ws = x["windows"]

        def cell(w):
            y = ws[str(w)]
            if y["status"] == "ok":
                return "ok" + (" (без несобранного)" if y.get("anchor") == PENDING else "")
            extra = f", {y.get('n_ours')} из ~{y['expected']}" if y.get("expected") else \
                f", расч. {y.get('n_ours') if y.get('n_ours') is not None else '?'}/{y.get('n_truth')}"
            return f"{y['status']} ({_p(y['ours'], 3)} vs {_p(y['truth'], 3)}{extra})"
        if x.get("no_history"):
            L.append(f"| {x['symbol']} | {_p(x.get('rate_h'))} | нет/{x['n_db']} | — | — | — | " +
                     " | ".join(cell(w) for w in h["windows"]) + f" | {_t(x.get('first_db_ms'))} | {whyp} |")
        else:
            L.append(f"| {x['symbol']} | {_p(x.get('rate_h'))} | {x['n_truth']}/{x['n_db']} | {len(x['missing'])} | {len(x['extra'])} | "
                     f"{len(x['values'])} | " + " | ".join(cell(w) for w in h["windows"]) + f" | {_t(x.get('first_db_ms'))} | {whyp} |")
    if causes:
        L += ["", "### Почему окна не сошлись с биржей", ""] + causes[:60]
    return "\n".join(L + [""])


def _js(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    return str(o)


def _stamp(t: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(t or time.time()))


def write_report(rep: dict, top: int, out_dir=None, stamp: str | None = None) -> Path:
    out_dir = Path(out_dir or (config.RUNTIME / "audit"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or _stamp(rep["ts"])
    (out_dir / f"{rep['venue']}_{stamp}.json").write_text(json.dumps(rep, ensure_ascii=False, default=_js))
    md = out_dir / f"{rep['venue']}_{stamp}.md"
    md.write_text(render_md(rep, top))
    return md


def run(venue: str, top: int = 20, sample: int = 10, days: int = 7, table_path=None, db_path=None, out_dir=None,
        truth=None, recheck_wait_s: float = RECHECK_WAIT_S, stamp: str | None = None) -> tuple[Path, int]:
    """Одна площадка: отчёт .md/.json → (путь к .md, число ошибок)."""
    rep = audit_venue(venue, top, sample, days, table_path, db_path, truth, recheck_wait_s)
    return write_report(rep, top, out_dir, stamp), rep["errors"]


def run_all(top: int = 20, sample: int = 10, days: int = 7, table_path=None, db_path=None, out_dir=None,
            truths: dict | None = None, recheck_wait_s: float = RECHECK_WAIT_S, workers: int | None = None) -> tuple[Path, int]:
    """Все площадки config.PERP_VENUES разом, каждая в своём потоке (разные хосты, у каждой свой темп) + сводка
    ALL_<время>.md. Раньше 6 потоков: самые долгие (Pacifica, Lighter, Lighter RH — по 7 мин из-за темпа 8-10 с) ждали
    в очереди, и прогон с Мака шёл 523 с вместо ~430. Списки рынков площадок-пар — один на прогон (PartnerMarkets): ждём
    его от прогона самой площадки, второй сессии к бирже нет — поэтому и нужны все потоки сразу (иначе ожидание в очереди)."""
    stamp = _stamp()
    out_dir = Path(out_dir or (config.RUNTIME / "audit"))
    truths = truths or {}
    venues = list(config.PERP_VENUES)
    workers = len(venues) if workers is None else max(1, workers)
    pm = PartnerMarkets(truths, expect=venues if workers >= len(venues) else ())

    def one(v):
        try:
            rep = audit_venue(v, top, sample, days, table_path, db_path, truths.get(v), recheck_wait_s, markets_of=pm)
        except Exception as e:  # noqa — упавший прогон одной площадки не роняет сводку
            rep = _finish(dict(venue=v, ts=int(time.time()), fatal=f"прогон упал: {_err(e)}", warnings=[]), time.time(), None)
        finally:
            pm.done(v)
        return rep, write_report(rep, top, out_dir, stamp)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(one, venues))
    total = sum(rep["errors"] for rep, _ in res)
    L = [f"# Тестировщик: все площадки — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", "",
         "**Итог: OK — ошибок нет**" if not total else f"**Итог: ОШИБОК {total}** — на "
         f"{sum(1 for rep, _ in res if rep['errors'])} площадках из {len(res)}", "",
         "| площадка | итог | присутствие | ставки | история | рынков / ног | отчёт |", "|---|---|---|---|---|---|---|"]
    for rep, md in res:
        ep, p = rep["error_parts"], rep.get("presence") or {}
        itog = "OK" if not rep["errors"] else ("НЕ ПРОВЕРЕНО" if rep.get("fatal") else f"ОШИБОК {rep['errors']}")
        L.append(f"| {rep['venue']} | {itog} | {ep.get('присутствие', 0)} | {ep.get('ставки', 0)} | {ep.get('история', 0)} | "
                 f"{p.get('n_tradable', '—')} / {p.get('n_legs', '—')} | {md.name} |")
    L += ["", "## Выводы по площадкам", ""]
    for rep, _md in res:
        L += [f"### {rep['venue']}", ""] + [f"- {x}" for x in rep["verdict"][:8]] + [""]
    path = out_dir / f"ALL_{stamp}.md"
    path.write_text("\n".join(L))
    (out_dir / f"ALL_{stamp}.json").write_text(json.dumps(
        {rep["venue"]: dict(errors=rep["errors"], error_parts=rep["error_parts"], verdict=rep["verdict"], report=md.name)
         for rep, md in res}, ensure_ascii=False, default=_js))
    return path, total
