"""Истина биржи для тестировщика: интерфейс Truth и общие помощники (свой HTTP с темпом, разбор Decimal, окна времени).

НЕЗАВИСИМОСТЬ (главное свойство тестировщика): модули этого пакета НЕ импортируют клиентов коллектора (hyperliquid.py,
kucoin_fut.py, bitget_fut.py, gate_fut.py, lighter.py, backpack.py, variational.py, edgex.py, extended.py, pacifica.py,
apex.py, exchanges.py, client.py, venues.py), его universe / calc / symbols. Каждая истина ходит в публичный API биржи
сырыми запросами и разбирает ответ сама — иначе тестировщик повторял бы ошибки клиентов. Читать шапки клиентов (точки,
ловушки) можно; импортировать — нет. Из config берутся только адрес/темп-независимые вещи (USER_AGENT).

Интерфейс (фиксирован; аудитор audit.py зовёт только его):

    class Truth:
        venue: str                        # как в config.PERP_VENUES
        def markets(self) -> dict[str, dict]
            символ рынка ровно как в дашборде (instruments.symbol, sa/sb/perp в table.json) →
            {"base": сырая база, как её называет биржа, "tradable": bool,
             "cls": "crypto"|"equity"|"commodity"|"fx"|"index"|"other"|None, "interval_h": int|None,
             "name": str|None, "note": str|None}
            Неторгуемые (делистинг, до запуска) — тоже, с tradable=False.
        def rates(self) -> dict[str, dict]
            символ → {"rate": Decimal (доля за интервал — ТА ЖЕ ставка, что биржа показывает текущей/следующей),
                      "interval_h": int, "next_ms": int|None, "kind": "predicted"|"last"|"unknown"}
        def history(self, symbol, start_ms, end_ms) -> list[tuple[int, Decimal]] | None
            рассчитанный фандинг в [start, end]: (funding_ms, ставка за интервал). None — биржа истории не публикует
            (почему — в history_note).
        history_note: str | None = None

Единицы — истина биржи: если биржа публикует годовую ставку или «за 8 ч», истина переводит её явно и пишет, из какого
поля. Необязательные атрибуты (расширение, у аудитора есть умолчания):
    site_hours: int | None  — сайт показывает ставку «за N часов» (Hyperliquid — 8h): только для таблицы «как на сайте»;
    quote_twins: bool       — несколько рынков одной монеты на площадке — дубли по квоте, коллектор берёт один
                              (Binance BTCUSDT/BTCUSDC). False — это разные рынки (HIP-3: xyz:NET и para:NET);
    cls может быть и «preipo» (перп до IPO / до запуска: пары у коллектора строятся, это не «other»).
"""
from __future__ import annotations
import math, time, threading
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
import re
import requests
from .. import config

H_MS = 3600_000
CLASSES = ("crypto", "equity", "commodity", "fx", "index", "preipo", "other")
KINDS = ("predicted", "last", "unknown")


class Http:
    """Своя сессия тестировщика: темп на хост (делим лимиты IP с живым коллектором), 429/5xx/сеть — пауза и повтор,
    прочие 4xx — сразу ошибка с кодом и началом тела. Ключей нет и не бывает: только публичные точки."""

    def __init__(self, venue: str = "", gap_s: float = 0.0, timeout: float = 20.0, retries: int = 4):
        self.venue = venue
        self.gap_s = gap_s
        self.timeout = timeout
        self.retries = retries
        self.s = requests.Session()
        self.s.headers["user-agent"] = config.USER_AGENT + " audit"
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def _pace(self, url: str, gap: float):
        host = urlsplit(url).netloc
        with self._lock:
            wait = self._last.get(host, 0.0) + gap - time.time()
            self._last[host] = time.time() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, url: str, gap: float | None = None, **kw):
        last = None
        g = self.gap_s if gap is None else gap
        for i in range(self.retries):
            self._pace(url, g)
            self.calls += 1
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"; time.sleep(2.0 * (i + 1)); continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                ra = r.headers.get("Retry-After")
                try:
                    pause = float(ra) if ra else 3.0 * (i + 1)
                except ValueError:
                    pause = 3.0 * (i + 1)
                time.sleep(min(pause, 30.0)); continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {url}: HTTP {r.status_code} {r.text[:200]}")
            try:
                return r.json()
            except ValueError as e:
                last = f"не JSON: {e}"; time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{method} {url}: {last}")

    def get(self, url: str, params: dict | None = None, gap: float | None = None):
        return self.request("GET", url, gap, params=params)

    def post(self, url: str, body=None, gap: float | None = None):
        return self.request("POST", url, gap, json=body)


class Truth:
    """База истин. Подкласс задаёт venue и три метода; http — своя сессия (подставляется в тестах)."""
    venue: str = ""
    history_note: str | None = None
    site_hours: int | None = None
    quote_twins: bool = True
    gap_s: float = 0.0                 # темп запросов к бирже по умолчанию

    def __init__(self, http: Http | None = None):
        self.http = http or Http(self.venue, self.gap_s)

    def markets(self) -> dict[str, dict]:
        raise NotImplementedError

    def rates(self) -> dict[str, dict]:
        raise NotImplementedError

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]] | None:
        raise NotImplementedError


# --- разбор -------------------------------------------------------------------------------------------------
def dec(x) -> Decimal | None:
    """Число биржи → Decimal без потери знаков строки. None/''/NaN/inf/bool → None. float — через repr (0.1 → 0.1)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        if isinstance(x, float):
            if not math.isfinite(x):
                return None
            d = Decimal(repr(x))
        elif isinstance(x, Decimal):
            d = x
        else:
            s = str(x).strip()
            if not s:
                return None
            d = Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def to_int(x) -> int | None:
    try:
        v = int(float(x))
    except (TypeError, ValueError):
        return None
    return v


def market(base: str, tradable: bool = True, cls: str | None = None, interval_h: int | None = None,
           name: str | None = None, note: str | None = None) -> dict:
    return {"base": base, "tradable": bool(tradable), "cls": cls, "interval_h": interval_h, "name": name, "note": note}


def rate(r: Decimal | None, interval_h: int, next_ms: int | None = None, kind: str = "predicted") -> dict:
    return {"rate": r, "interval_h": int(interval_h), "next_ms": next_ms, "kind": kind}


def norm_base(name: str) -> tuple[str, float]:
    """Своя нормализация базы (не symbols.py коллектора): 'xyz:NATGAS' → NATGAS; 1000PEPE / kPEPE → PEPE ×1000;
    1MBABYDOGE → BABYDOGE ×1e6. 1INCH остаётся 1INCH."""
    s = str(name).split(":", 1)[-1]
    factor = 1.0
    if re.match(r"^k[A-Z0-9]{3,}$", s):
        s, factor = s[1:], 1000.0
    m = re.match(r"^1M([A-Za-z][A-Za-z0-9]{2,})$", s)
    if m:
        return m.group(1).upper(), 1_000_000.0
    m = re.match(r"^(1000+)([A-Za-z].*)$", s)
    if m:
        s, factor = m.group(2), float(m.group(1))
    return s.upper(), factor


# --- время ------------------------------------------------------------------------------------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def next_boundary_ms(t_ms: int, interval_h: int) -> int:
    """Ближайший расчёт строго позже t на сетке интервала от эпохи UTC (Hyperliquid — каждый час на часе)."""
    step = int(interval_h) * H_MS
    return (t_ms // step + 1) * step


def window_sum(events, anchor_ms: int, hours: float) -> tuple[float | None, int]:
    """Сумма ставок в (anchor − W, anchor] и число расчётов; пусто — (None, 0), как у дашборда (calc.window_sums)."""
    lo = anchor_ms - int(hours * H_MS)
    xs = [float(r) for ms, r in events if lo < ms <= anchor_ms]
    return (sum(xs), len(xs)) if xs else (None, 0)


def page_by_time(fetch, start_ms: int, end_ms: int, page: int, max_pages: int = 60) -> list[tuple[int, Decimal]]:
    """Постраничная история «по возрастанию времени»: fetch(cursor) → [(ms, Decimal)]; страница короче page — конец.
    Отдаёт [start, end] без дублей по времени."""
    out: dict[int, Decimal] = {}
    cursor = int(start_ms)
    for _ in range(max_pages):
        rows = fetch(cursor)
        for ms, r in rows:
            if start_ms <= ms <= end_ms and r is not None:
                out[ms] = r
        if len(rows) < page or not rows:
            break
        nxt = max(ms for ms, _ in rows) + 1
        if nxt <= cursor or nxt > end_ms:
            break
        cursor = nxt
    return sorted(out.items())
