"""Hyperliquid — третья биржа (решение владельца 10.09). Публичный info-API.

Рынки = основной dex + все HIP-3 dex (xyz, para, io, mkts, …): сайт показывает их в одном списке «All», и 11.09
тестировщик нашёл, что 14 из 20 рынков с самым жирным фандингом — HIP-3 (xyz:NATGAS, xyz:BOT, …), которых мы не
собирали. Имена HIP-3 уже с префиксом dex («xyz:NATGAS»), так их понимают и fundingHistory, и страница токена.

Замерено 10-11.09.2026:
- `perpDexs` — список dex (первый элемент null = основной); у части dex (flx, vntl, hyna, km, …) живых рынков нет.
- `metaAndAssetCtxs` [+ dex] — вселенная и контексты: `funding` (ставка ТЕКУЩЕГО часа, доля), `markPx`, `oraclePx`
  (индекс), `midPx`, `impactPxs` [бид, аск] по импакт-объёму. Лучшего бида/аска одним вызовом нет — берём impactPxs.
- Фандинг раз в час на круглом часу, у HIP-3 тоже (xyz:NATGAS, para:NET, io:GPRO — шаг ровно 1 ч).
- `fundingHistory` {coin, startTime[, endTime]} — не больше 500 строк за вызов. Пакетного «все монеты» нет —
  поддержание добором по полноте.
- k-монеты (kPEPE, kSHIB, …) — ×1000. Имя регистрозависимое: KPEPE ≠ kPEPE.
- Заголовка веса нет. Лимит IP — 1200 веса в минуту; контексты ~20 за вызов, история ~20 + 1 на 20 строк.
  Поэтому: основной dex — каждый тик, HIP-3 — каждый, чей снимок старше 40 с (4 dex × 20 веса раз в 40 с), длинные
  страницы истории — раз в 4 с, короткий добор — раз в 1.5 с.
"""
from __future__ import annotations
import time, logging, threading
import requests
from . import config
from .client import PermanentHTTPError, BannedError
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

HOUR_MS = 3600_000
HISTORY_PAGE = 500
MIN_ORDER_USD = 10.0
DEXES_TTL_S = 3600


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def base_of(name: str) -> tuple[str, float]:
    """'xyz:NATGAS' → ('NATGAS', 1); 'kPEPE' → ('PEPE', 1000)."""
    return norm_symbol_factor(name.split(":", 1)[-1])


class Hyperliquid:
    name = "hyperliquid"

    def __init__(self, base: str = "https://api.hyperliquid.xyz", session: requests.Session | None = None,
                 history_gap_s: float | None = None, short_gap_s: float | None = None,
                 ctx_ttl_s: float = 5.0, hip3_ttl_s: float | None = None):
        self.base = base
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self.history_gap_s = config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, 4.0) if history_gap_s is None else history_gap_s
        self.short_gap_s = config.HL_SHORT_HISTORY_GAP_S if short_gap_s is None else short_gap_s
        self.ctx_ttl_s = ctx_ttl_s
        self.hip3_ttl_s = config.HL_HIP3_TTL_S if hip3_ttl_s is None else hip3_ttl_s
        self._lock = threading.Lock()
        self._last_hist = 0.0
        self._dexes: list[str] | None = None          # "" = основной
        self._dexes_ts = 0.0
        self._ctx: dict[str, tuple[float, list, list]] = {}
        self.used_weight = 0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0
        self.banned_until = 0.0

    # --- транспорт ------------------------------------------------------------------------
    def budget_used(self) -> float:
        return 0.0          # заголовка веса у Hyperliquid нет; защита — темп вызовов истории и кэш контекстов

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self.banned_until

    def post(self, body: dict, retries: int = 3, timeout: int = config.HTTP_TIMEOUT):
        if time.time() < self.banned_until:
            raise BannedError(f"{self.name}: пауза после 429 до {time.strftime('%H:%M:%S', time.gmtime(self.banned_until))}")
        last = None
        for i in range(retries):
            try:
                r = self._s.post(self.base + "/info", json=body, timeout=timeout)
                if r.status_code == 429:
                    self.n_429 += 1
                    wait = float(r.headers.get("Retry-After", 0) or 0) or 5.0 * (i + 1)
                    log.warning("%s 429 на %s, жду %.0f с", self.name, body.get("type"), wait)
                    last = "429"
                    time.sleep(wait)
                    continue
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{r.status_code} {body.get('type')}: {r.text[:200]}")
                r.raise_for_status()
                self.last_ok_ts = time.time()
                return r.json()
            except PermanentHTTPError:
                raise
            except Exception as e:  # noqa: сеть, 5xx, битый JSON — повторяем
                last = e; self.n_err += 1
                time.sleep(1.0 * (i + 1))
        if last == "429":
            self.banned_until = time.time() + 60          # три 429 подряд — минута тишины, а не лавина
        raise RuntimeError(f"POST {self.base}/info {body.get('type')} failed: {last}")

    def health(self) -> dict:
        return {"exchange": self.name, "used_weight": self.used_weight, "budget": 0.0, "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": int(self.banned_until)}

    # --- контексты по dex -----------------------------------------------------------------------
    def dexes(self) -> list[str]:
        if self._dexes is None or time.time() - self._dexes_ts > DEXES_TTL_S:
            raw = self.post({"type": "perpDexs"})
            self._dexes = [""] + [d["name"] for d in raw if d]
            self._dexes_ts = time.time()
        return self._dexes

    def _fetch(self, dex: str, retries: int = 3, timeout: int = config.HTTP_TIMEOUT):
        body = {"type": "metaAndAssetCtxs"}
        if dex:
            body["dex"] = dex
        meta, ctxs = self.post(body, retries=retries, timeout=timeout)
        uni = meta.get("universe", [])
        if len(uni) != len(ctxs):
            raise RuntimeError(f"{self.name}{':' + dex if dex else ''}: universe {len(uni)} != ctxs {len(ctxs)}")
        with self._lock:
            self._ctx[dex] = (time.time(), uni, ctxs)

    def _live_dexes(self) -> list[str]:
        return [d for d, (_, uni, _c) in self._ctx.items() if d and any(not a.get("isDelisted") for a in uni)]

    def refresh_all(self):
        """Все dex разом — раз в час, при пересборке вселенной (~11 вызовов)."""
        for dex in self.dexes():
            try:
                self._fetch(dex)
            except Exception as e:  # noqa — упавший HIP-3 dex не должен ронять основной
                if not dex:
                    raise
                log.warning("%s dex %s: %s: %s", self.name, dex, type(e).__name__, e)

    def refresh_tick(self):
        """Тик: основной dex, если устарел, и КАЖДЫЙ живой HIP-3 dex, чей снимок старше hip3_ttl_s. Раньше — один за
        тик: при 5+ dex хвост очереди старел дольше STALE_S (после часовой пересборки все dex получали одно время
        снимка), его строки бледнели, а гистерезис «не тот актив» так и не набирал трёх наблюдений подряд
        (проверка исправлений 11.09). Каждый dex теперь не старше hip3_ttl_s + тик."""
        now = time.time()
        main = self._ctx.get("")
        fast = dict(retries=config.TICK_RETRIES, timeout=config.TICK_HTTP_TIMEOUT)    # повтор — это следующий тик
        if not main or now - main[0] >= self.ctx_ttl_s:
            self._fetch("", **fast)
        for _ts, dex in sorted((self._ctx[d][0], d) for d in self._live_dexes() if now - self._ctx[d][0] >= self.hip3_ttl_s):
            try:
                self._fetch(dex, **fast)
            except Exception as e:  # noqa — упавший HIP-3 dex не держит остальных; его строки устареют по obs
                log.warning("%s dex %s: %s: %s", self.name, dex, type(e).__name__, e)

    def _live(self):
        """(dex, актив, контекст, время снимка dex). Время снимка — момент наблюдения: HIP-3 dex обновляются по одному
        за тик, и у их строк оно старше, чем у основного dex (ревью 11.09: гистерезис не должен считать кэш новым)."""
        with self._lock:
            snap = list(self._ctx.items())
        for dex, (ts, uni, ctxs) in snap:
            for a, c in zip(uni, ctxs):
                if not a.get("isDelisted"):
                    yield dex, a, c, ts

    # --- данные ----------------------------------------------------------------------------------
    def categories(self) -> dict[str, str]:
        """Класс рынков HIP-3 из perpCategories: [[«xyz:NVDA», «stocks»], …]; значения бывают «stocks»/«stock», «FX»."""
        try:
            return {str(n): str(c).lower() for n, c in self.post({"type": "perpCategories"}) or []}
        except Exception as e:  # noqa — без категорий HIP-3 считается акциями (так и есть у большинства)
            log.warning("%s perpCategories: %s: %s", self.name, type(e).__name__, e)
            return {}

    def annotations(self, coins: list[str]) -> dict[str, dict]:
        """Описание рынка HIP-3 (perpAnnotation) → {"desc": текст}: у крипто-рынков в нём имя монеты («price of Bitcoin
        (BTC)»). Для «того же актива» (identity.py); зовётся раз в сутки на ~7 рынков, с паузой."""
        out = {}
        for c in coins:
            try:
                a = self.post({"type": "perpAnnotation", "coin": c}, retries=2)
            except PermanentHTTPError:
                a = None
            except Exception as e:  # noqa — сбой одного рынка: остальные идут, этот дособерётся следующим заданием
                log.warning("%s perpAnnotation %s: %s: %s", self.name, c, type(e).__name__, e)
                continue
            out[c] = {"desc": a.get("description") if isinstance(a, dict) else None}
            time.sleep(0.3)
        return out

    @staticmethod
    def _cls(dex: str, category: str | None) -> str:
        if not dex:
            return "crypto"                               # основной dex — только монеты
        c = (category or "").lower()
        if c.startswith("commod"):
            return "commodity"
        if c.startswith("ind"):
            return "index"
        if c.startswith("crypto"):
            return "crypto"
        if c in ("fx", "forex"):
            return "fx"
        return "equity"

    def perp_instruments(self) -> list[dict]:
        self.refresh_all()
        cats = self.categories()
        out = []
        for dex, a, _c, _ts in self._live():
            base, factor = base_of(a["name"])
            dec = a.get("szDecimals")
            cls = self._cls(dex, cats.get(a["name"]))
            out.append(dict(exchange=self.name, symbol=a["name"], base_asset=a["name"],
                            base=config.PERP_CANON.get(cls, {}).get(base, base), factor=factor,
                            tick_size=None, step_size=(10.0 ** -int(dec)) if dec is not None else None,
                            min_notional=MIN_ORDER_USD, onboard_ms=0, interval_h=1, cap=None, floor=None,
                            quote="USDC", contract=dex or "main", cls=cls))
        return out

    def premium(self) -> dict[str, dict]:
        self.refresh_tick()
        now_ms = int(time.time() * 1000)
        nxt = (now_ms // HOUR_MS + 1) * HOUR_MS
        return {a["name"]: dict(rate=_f(c.get("funding")), mark=_f(c.get("markPx")), index=_f(c.get("oraclePx")),
                                next_ms=nxt, ts_ms=now_ms, obs=ts) for _, a, c, ts in self._live()}

    def books(self) -> dict[str, dict]:
        out = {}
        for _, a, c, ts in self._live():
            imp = c.get("impactPxs") or []
            bid, ask = (_f(imp[0], 0.0), _f(imp[1], 0.0)) if len(imp) == 2 else (0.0, 0.0)
            if not (bid and ask):
                m = _f(c.get("midPx"), 0.0)
                bid = ask = m
            if bid and ask:
                out[a["name"]] = dict(bid=bid, ask=ask, bid_qty=0.0, ask_qty=0.0, obs=ts)
        return out

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Расчёты монеты с start_ms постранично (по 500). Длинные страницы — раз в history_gap_s, короткий добор —
        раз в short_gap_s (ответ в пару строк весит в разы меньше)."""
        end_ms = end_ms or int(time.time() * 1000)
        gap = self.history_gap_s if (end_ms - start_ms) / HOUR_MS > 50 else self.short_gap_s
        out, cursor = [], int(start_ms)
        for _ in range(20):
            with self._lock:
                wait = self._last_hist + gap - time.time()
                self._last_hist = time.time() + max(0.0, wait)
            if wait > 0:
                time.sleep(wait)
            rows = self.post({"type": "fundingHistory", "coin": symbol, "startTime": cursor, "endTime": int(end_ms)})
            for r in rows:
                rate = _f(r.get("fundingRate"))
                if rate is None:
                    continue
                out.append(dict(exchange=self.name, symbol=symbol, funding_ms=int(r["time"]), rate=rate, mark=None))
            if len(rows) < HISTORY_PAGE:
                break
            cursor = int(rows[-1]["time"]) + 1
        return out

    def recent_history(self) -> list[dict]:
        """Пакетного «последние расчёты по всем монетам» нет. Поддержание — добор по полноте."""
        return []
