"""Перп-нога Hyperliquid (HIP-3 para:ANSEM): рынок, счёт, подпись агентом, одна отправка IOC, разбор UNKNOWN и учёт —
ТЗ §10/§13, appendices/HYPERLIQUID.md, sol_plan/read_hl.md. Чистые правила — hl_rules.py.

Слои (без ключей → с ключом):
  HlHttp     — POST /info (чтения: повтор сети/5xx, 429 → пауза, свой учёт веса 1200/мин на IP, общий с коллектором)
               и POST /exchange — РОВНО ОДНА попытка: таймаут/обрыв = исход неизвестен, повтора нет.
  HlMarket   — perpDexs/meta → asset id (сырые списки), metaAndAssetCtxs, l2Book, фандинг, история, лимиты OI.
  HlAccount  — всё по адресу ТОРГОВОГО счёта (master или субаккаунт), никогда по адресу агента (H04); ключ не нужен
               (H05): userAbstraction, clearinghouseState(dex=para), spotClearinghouseState, activeAssetData, userFees,
               extraAgents, orderStatus, userFillsByTime, userFunding.
  HlJournal  — SQLite (WAL, synchronous=FULL): nonce агента под BEGIN IMMEDIATE и журнал попыток. DDL — в store.py
               (HL_JOURNAL_SCHEMA: миграция trade.db схемы 2, ворота версии); на отдельной БД — ensure_journal_tables.
  HlSigner   — подпись L1-действия ключом агента: msgpack + nonce + vault + expiresAfter → keccak → EIP-712 phantom
               agent (формула sign_l1_action SDK 0.24.0, сверено золотыми векторами). Ключ сам не читает (keys.py).
  HyperliquidTrade — PerpLeg для движка поверх всего этого; расхождения с протоколом — ниже.

Запись-до (§6, §10): client_id → cloid → nonce (commit) → строка PREPARED с действием, хэшем, expiresAfter (commit)
→ подпись → SIGNED (commit) → on_signed(nonce) движка → POST. Любой сбой до POST — NOT_SENT и исключение; после
подписи строка SIGNED значит «могла уйти». Пока в журнале есть незакрытая заявка счёта/рынка (SIGNED/UNKNOWN),
новая не отправляется (X10): сначала settle_unknown.

UNKNOWN: unknownOid ничего не доказывает, пока не прошёл expiresAfter. «Не выставлена» (NOT_FOUND) — только если
одновременно: заявка ни разу не была найдена (ни в этом разборе, ни раньше — oid в журнале), now > expiresAfter +
запас, unknownOid в ≥ 2 опросах ПОДРЯД после него (сбой чтения рвёт серию), позиция == pos_before, в полной
истории fills с since_ms нет ни нашей заявки, ни чужих (не из known_order_ids). Иначе UNKNOWN → пауза и сверка.
Найденную заявку unknownOid не отменяет: отстающий узел согласованно отдаёт старые orderStatus, позицию и fills.

Расхождения с PerpLeg (types.py) — для интеграции после фазы 1:
  * PerpFill.err_code (int, коды Aster) у HL пуст: вместо него HlPerpFill.err_kind (tick_size / min_notional /
    margin / reduce_only / oracle_bounds / oi_cap / ioc_no_match / nonce / signature_or_agent / rate_limit / other)
    и outcome в терминах ТЗ (FILLED_TERMINAL / PARTIAL_TERMINAL / REJECTED_ZERO_FILL / UNKNOWN). Движок сейчас
    ветвится по −1111/−2022 — для HL нужны категории.
  * «could not immediately match» → EXPIRED (0 исполнено, финал), как EXPIRED у Aster, а не REJECTED.
  * Цена: шага (tick) нет — правило 5 значащих цифр. filters().tick — производная у текущей цены; движку нужен
    quantize_px(symbol, px, side) вместо floor_step(cap, tick). ioc() неверную цену не округляет молча — отказ.
  * fills(symbol, from_id) — NotImplementedError: fromId у HL нет, tid — хэш. Учёт — fills_since(start_ms) с
    перекрытием и признаком полноты; ключ (network, account, coin, time, tid). funding_income() отдаёт строки HL
    с ключом (network, account, coin, time, hash), а не tran_id Aster (store.funding_income не подходит).
  * available_margin() — по режиму счёта (Standard: withdrawable ledger'а para; Unified: spot USDC), режим честно
    в margin(); торговля первой версией — только Standard (решение по Unified — у владельца).
  * setup(): только ISOLATED (noCross), плечо ≤ maxLeverage; при открытой позиции — отказ без явного флага.
  * settle_unknown() ждёт expiresAfter (до ~40 с при EXPIRES_MS 30 с), а не «три −2013 за 5 с».
"""
from __future__ import annotations
import json, logging, re, secrets, sqlite3, threading, time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
import requests
from ..client import BannedError, BudgetExceeded
from . import hl_rules as R
from . import store as _store
from .keys import KeyMismatch, Keys, ModeForbidden, gate as mode_gate, redact_secrets
from .types import Book, Filters, PerpFill, PerpInstrument

log = logging.getLogger(__name__)
D = Decimal
_D0 = Decimal(0)

VENUE = "hyperliquid"
NETWORKS = {"mainnet": "https://api.hyperliquid.xyz", "testnet": "https://api.hyperliquid-testnet.xyz"}
DEFAULT_FULLCOIN = "para:ANSEM"
HOUR_MS = 3_600_000

WEIGHT_LIMIT_MIN = 1200             # вес на IP в минуту (Rate limits) — общий с коллектором на том же IP
SOFT_READ = 0.5                     # обычные чтения — до половины: остальное держим коллектору и пути заявки
SOFT_ORDER = 0.9                    # путь заявки: /exchange, orderStatus, позиция, settle
READ_TIMEOUT = (5, 10)
EXCHANGE_TIMEOUT = (5, 10)          # (connect, read): дольше — исход неизвестен, выясняем по cloid
READ_RETRIES = 2
RULES_TTL_S = 60.0                  # метаданные для отчёта/фильтров
ORDER_IDENTITY_MAX_AGE_S = 10.0     # перед заявкой идентичность asset id сверяется заново (H02)
EXPIRES_MS = 30_000                 # expiresAfter = now + 30 с (ТЗ: 20–30 с); технический параметр, не деньги
CLOCK_SLACK_MS = 5_000              # запас на часы/время блока при проверке «expiresAfter прошёл»
CLOCK_SKEW_MAX_S = 2.0              # live не стартует при большем расхождении часов (exchangeStatus.time)
NOT_FOUND_POLLS = 2                 # unknownOid ПОСЛЕ expiresAfter — не меньше двух опросов
UNKNOWN_POLL_GAP_S = 2.0
FILLS_PAGE = 2000                   # userFillsByTime: до 2000 за ответ, доступны 10 000 последних
FILLS_CAP = 10_000
FUNDING_PAGE = 500
HISTORY_PAGE = 500
MAX_PAGES = 12
FILLS_SLACK_MS = 5_000              # окно fills при разборе UNKNOWN — раньше отправки (часы ±2 с + запас)
NONCE_AHEAD_MAX_MS = 86_400_000 - 600_000   # HL принимает nonce в (T−2 сут, T+1 сут)
_WEIGHTS = {"l2Book": 2, "allMids": 2, "clearinghouseState": 2, "orderStatus": 2, "spotClearinghouseState": 2,
            "exchangeStatus": 2, "userRole": 60}
_HISTORY = frozenset({"userFillsByTime", "userFills", "userFunding", "fundingHistory", "historicalOrders"})
_JSON = {"Content-Type": "application/json"}
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
FINAL = frozenset({"FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"})


# --- ошибки ---------------------------------------------------------------------------------------
class HlError(RuntimeError):
    """Отказ ноги Hyperliquid (формат ответа, несоответствие, запрет по состоянию)."""


class HlApiError(HlError):
    """Площадка ответила отказом: what, http, текст."""

    def __init__(self, what: str, http: int | None, text: str):
        self.what, self.http, self.text = what, http, text
        super().__init__(f"{what}: HTTP {http} {text}"[:300])


class HlNetError(HlError):
    """Ответа нет (таймаут, обрыв, 5xx после повторов). Для /exchange — исход НЕИЗВЕСТЕН."""


def _addr(a: Any, what: str) -> str:
    if not _ADDR_RE.match(str(a or "")):
        raise ValueError(f"Hyperliquid {what}: не адрес 0x…40 hex: {a!r}")
    return str(a).lower()


def _retry_after(r, default: float) -> float:
    try:
        return max(float(r.headers.get("Retry-After") or default), 0.0)
    except (TypeError, ValueError, AttributeError):
        return default


# --- транспорт ------------------------------------------------------------------------------------
class HlHttp:
    """POST /info и /exchange. Заголовка веса у HL нет — считаем сами по таблице Rate limits (скользящая минута)."""

    def __init__(self, base: str = NETWORKS["mainnet"], session=None, *, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, weight_limit: int = WEIGHT_LIMIT_MIN):
        self.base = base.rstrip("/")
        self._s = session or requests.Session()
        self._now, self._sleep = now, sleep
        self.weight_limit = weight_limit
        self._win: deque[tuple[float, int]] = deque()
        self._lk = threading.Lock()
        self.backoff_until = 0.0
        self.n_429 = 0
        self.n_err = 0
        self.last_ok_ts = 0.0

    def _used(self) -> int:
        with self._lk:
            edge = self._now() - 60.0
            while self._win and self._win[0][0] < edge:
                self._win.popleft()
            return sum(w for _, w in self._win)

    def budget_used(self) -> float:
        return self._used() / self.weight_limit

    def _charge(self, w: int) -> None:
        if w > 0:
            with self._lk:
                self._win.append((self._now(), int(w)))

    def check(self, weight: int, critical: bool, what: str) -> None:
        """До сети (и до подписи): пауза после 429 или исчерпан бюджет — BudgetExceeded, запрос не уходит."""
        now = self._now()
        if now < self.backoff_until:
            raise BudgetExceeded(f"hyperliquid: пауза после 429 ещё {self.backoff_until - now:.1f} с, "
                                 f"{what} не отправлен")
        soft = SOFT_ORDER if critical else SOFT_READ
        if self._used() + weight > self.weight_limit * soft:
            raise BudgetExceeded(f"hyperliquid: вес {self._used()}/{self.weight_limit} ≥ {soft:.0%}, "
                                 f"{what} не отправлен")

    def _post(self, path: str, payload: Any, timeout):
        data = json.dumps(payload, separators=(",", ":")).encode()
        return self._s.post(self.base + path, data=data, headers=_JSON, timeout=timeout)

    @staticmethod
    def _body(r) -> Any:
        return json.loads(r.content, parse_float=Decimal)

    def _on_429(self, r) -> None:
        self.n_429 += 1
        self.backoff_until = self._now() + max(_retry_after(r, 0.0), 2.0)

    def info(self, body: dict, *, critical: bool = False, retries: int = READ_RETRIES) -> Any:
        """Чтение. Сеть/5xx/битый JSON — повтор (чтение повторять безопасно); 429 — пауза и BudgetExceeded;
        4xx — HlApiError. Числа с точкой — Decimal (float в ответе не остаётся)."""
        typ = str(body.get("type"))
        w = _WEIGHTS.get(typ, 20)
        self.check(w, critical, typ)
        last: Any = None
        for i in range(max(1, retries)):
            if i:
                self._sleep(0.5 * i)
            self._charge(w)
            try:
                r = self._post("/info", body, READ_TIMEOUT)
            except Exception as e:      # noqa — сеть: чтение можно повторить
                last, self.n_err = type(e).__name__, self.n_err + 1
                continue
            st = int(r.status_code)
            if st == 429:
                self._on_429(r)
                raise BudgetExceeded(f"hyperliquid: 429 на {typ}, пауза до {self.backoff_until:.0f}")
            if 400 <= st < 500:
                self.n_err += 1
                raise HlApiError(typ, st, (getattr(r, "text", "") or "")[:200])
            if st >= 500:
                last, self.n_err = f"HTTP {st}", self.n_err + 1
                continue
            try:
                data = self._body(r)
            except ValueError:
                last, self.n_err = "битый JSON", self.n_err + 1
                continue
            self.last_ok_ts = self._now()
            if typ in _HISTORY and isinstance(data, list):
                self._charge(len(data) // 20)      # история: + вес за каждые 20 строк ответа
            return data
        raise HlNetError(f"POST /info {typ}: нет ответа ({last})")

    def exchange(self, payload: dict) -> tuple[int, Any]:
        """ОДНА попытка /exchange. Нет ответа — HlNetError (исход неизвестен). Бюджет — check() ДО подписи."""
        self._charge(1)
        try:
            r = self._post("/exchange", payload, EXCHANGE_TIMEOUT)
        except Exception as e:          # noqa — любой обрыв после отправки = исход неизвестен
            self.n_err += 1
            raise HlNetError(f"POST /exchange: нет ответа ({type(e).__name__})") from None
        st = int(r.status_code)
        if st == 429:
            self._on_429(r)
        try:
            body = self._body(r)
        except ValueError:
            body = {"_raw": (getattr(r, "text", "") or "")[:200]}
        if st < 400:
            self.last_ok_ts = self._now()
        else:
            self.n_err += 1
        return st, body

    def health(self) -> dict:
        return {"exchange": VENUE, "used_weight": self._used(), "budget": round(self.budget_used(), 3),
                "last_ok_ts": int(self.last_ok_ts), "n_429": self.n_429, "n_err": self.n_err,
                "backoff_until": int(self.backoff_until)}


# --- рынок ----------------------------------------------------------------------------------------
class HlMarket:
    """Публичные данные одного точного рынка (fullcoin с dex). Всё сопоставление — по сырым ответам."""

    def __init__(self, http: HlHttp, fullcoin: str = DEFAULT_FULLCOIN, *, now: Callable[[], float] = time.time):
        self.http = http
        self.dex, self.fullcoin = R.split_fullcoin(fullcoin)
        self._now = now
        self._ref: tuple[float, R.AssetRef] | None = None
        self._limits: tuple[float, dict] | None = None
        self.last_book_ms: int | None = None

    def _dex_body(self, typ: str) -> dict:
        return {"type": typ, "dex": self.dex} if self.dex else {"type": typ}

    def refresh_identity(self, *, critical: bool = False) -> R.AssetRef:
        dexs = self.http.info({"type": "perpDexs"}, critical=critical)
        meta = self.http.info(self._dex_body("meta"), critical=critical)
        ref = R.resolve_asset(dexs, meta, self.fullcoin)
        self._ref = (self._now(), ref)
        return ref

    def identity(self, max_age_s: float = RULES_TTL_S, *, critical: bool = False) -> R.AssetRef:
        if self._ref is not None and self._now() - self._ref[0] < max_age_s:
            return self._ref[1]
        return self.refresh_identity(critical=critical)

    def ctx(self) -> dict:
        """Контекст рынка из metaAndAssetCtxs: сопоставление по ordinal ВНУТРИ одного ответа, с проверкой имени."""
        got = self.http.info(self._dex_body("metaAndAssetCtxs"))
        if not isinstance(got, list) or len(got) != 2 or not isinstance(got[0], dict):
            raise HlError("metaAndAssetCtxs: не [meta, ctxs]")
        meta, ctxs = got
        uni = meta.get("universe")
        if not isinstance(uni, list) or not isinstance(ctxs, list) or len(uni) != len(ctxs):
            raise HlError("metaAndAssetCtxs: universe и ctxs разной длины")
        hits = [j for j, a in enumerate(uni) if isinstance(a, dict) and a.get("name") == self.fullcoin]
        if len(hits) != 1:
            raise R.HlIdentityChanged(f"metaAndAssetCtxs: {self.fullcoin} найден {len(hits)} раз(а)")
        ref = self.identity()
        if hits[0] != ref.local_index:
            raise R.HlIdentityChanged(f"{self.fullcoin}: позиция в universe {ref.local_index} → {hits[0]}")
        c = ctxs[hits[0]]
        imp = c.get("impactPxs")

        def opt(k):
            return None if c.get(k) is None else R.to_dec(c[k], k)
        return {"funding": R.to_dec(c.get("funding"), "funding"), "mark": R.to_dec(c.get("markPx"), "markPx"),
                "oracle": R.to_dec(c.get("oraclePx"), "oraclePx"), "mid": opt("midPx"), "premium": opt("premium"),
                "open_interest": R.to_dec(c.get("openInterest"), "openInterest"), "day_ntl_vlm": opt("dayNtlVlm"),
                "impact": (R.to_dec(imp[0]), R.to_dec(imp[1])) if isinstance(imp, list) and len(imp) == 2 else None,
                "is_delisted": bool(uni[hits[0]].get("isDelisted")), "ts": self._now()}

    def book(self, limit: int = 20, *, critical: bool = False) -> Book:
        """l2Book (≤ 20 уровней на сторону; глубже ликвидность неизвестна). ts — локальное время получения,
        время биржи — last_book_ms. Ответ по другому рынку — отказ (H01)."""
        b = self.http.info({"type": "l2Book", "coin": self.fullcoin}, critical=critical)
        if not isinstance(b, dict) or b.get("coin") != self.fullcoin:
            raise HlError(f"l2Book: ответ не по {self.fullcoin}: {str(b)[:120]}")
        lv = b.get("levels")
        if not isinstance(lv, list) or len(lv) != 2:
            raise HlError("l2Book: levels не [bids, asks]")
        want = max(1, min(int(limit), 20))

        def side(rows):
            out = tuple((R.to_dec(x["px"], "px"), R.to_dec(x["sz"], "sz")) for x in rows[:want])
            if any(p <= 0 or q <= 0 for p, q in out):
                raise HlError("l2Book: неположительная цена/объём")
            return out
        bids, asks = side(lv[0]), side(lv[1])
        if any(bids[i][0] <= bids[i + 1][0] for i in range(len(bids) - 1)) or \
                any(asks[i][0] >= asks[i + 1][0] for i in range(len(asks) - 1)):
            raise HlError("l2Book: уровни не упорядочены")
        if bids and asks and bids[0][0] >= asks[0][0]:
            raise HlError(f"l2Book: книга пересечена {bids[0][0]} ≥ {asks[0][0]}")
        t = b.get("time")
        self.last_book_ms = t if isinstance(t, int) and not isinstance(t, bool) else None
        return Book(bids=bids, asks=asks, ts=self._now())

    def funding(self) -> tuple[Decimal, Decimal, int]:
        """(марк, ставка текущего часа, следующий круглый час мс). Период HL — 1 ч: не делить на 8, не ×0.6."""
        c = self.ctx()
        now_ms = int(self._now() * 1000)
        return c["mark"], c["funding"], (now_ms // HOUR_MS + 1) * HOUR_MS

    def funding_history(self, start_ms: int, end_ms: int | None = None) -> R.TimePage:
        end = int(end_ms if end_ms is not None else self._now() * 1000)

        def fetch(cur):
            return self.http.info({"type": "fundingHistory", "coin": self.fullcoin, "startTime": cur, "endTime": end})

        def parse(x):
            if x.get("coin") != self.fullcoin:
                raise R.HlRuleError(f"fundingHistory: чужой рынок {x.get('coin')!r}")
            return {"coin": x["coin"], "time": int(x["time"]), "rate": R.to_dec(x["fundingRate"], "fundingRate"),
                    "premium": None if x.get("premium") is None else R.to_dec(x["premium"], "premium")}
        return R.paginate_by_time(fetch, start_ms, page_limit=HISTORY_PAGE, parse=parse,
                                  key=lambda r: (r["coin"], r["time"]), max_pages=MAX_PAGES, collide=("rate",))

    def limits(self, max_age_s: float = RULES_TTL_S) -> dict:
        if self._limits is not None and self._now() - self._limits[0] < max_age_s:
            return self._limits[1]
        lim = self.http.info(self._dex_body("perpDexLimits"))
        if not isinstance(lim, dict):
            raise HlError("perpDexLimits: не объект")
        self._limits = (self._now(), lim)
        return lim

    def oi_state(self) -> dict:
        """Лимит OI рынка (USD), «на лимите» ли он сейчас, текущий OI. Может смениться между проверкой и хеджем —
        ошибка oi_cap на заявке = отказ, а не повтор."""
        lim = self.limits(0)
        caps = {str(n): v for n, v in (lim.get("coinToOiCap") or []) if isinstance(n, str)}
        at_cap = self.http.info(self._dex_body("perpsAtOpenInterestCap"))
        c = self.ctx()
        return {"cap_usd": R.to_dec(caps[self.fullcoin]) if self.fullcoin in caps else None,
                "at_cap": isinstance(at_cap, list) and self.fullcoin in at_cap,
                "open_interest": c["open_interest"], "oi_usd": c["open_interest"] * c["mark"],
                "total_oi_cap": None if lim.get("totalOiCap") is None else R.to_dec(lim["totalOiCap"]),
                "sz_cap_per_perp": None if lim.get("oiSzCapPerPerp") is None else R.to_dec(lim["oiSzCapPerPerp"])}

    def annotation(self) -> dict | None:
        a = self.http.info({"type": "perpAnnotation", "coin": self.fullcoin})
        return a if isinstance(a, dict) else None

    def server_time_ms(self) -> int:
        st = self.http.info({"type": "exchangeStatus"}, critical=True)
        t = st.get("time") if isinstance(st, dict) else None
        if isinstance(t, bool) or not isinstance(t, int):
            raise HlError(f"exchangeStatus: нет time: {str(st)[:120]}")
        return t


# --- счёт -----------------------------------------------------------------------------------------
class HlAccount:
    """Чтения по адресу торгового счёта (master или субаккаунт). Ключ не нужен (H05); адрес агента сюда не
    передаётся никогда: пустой баланс агента ≠ флэт счёта (H04)."""

    def __init__(self, http: HlHttp, account: str, *, fullcoin: str = DEFAULT_FULLCOIN, network: str = "mainnet",
                 now: Callable[[], float] = time.time):
        self.http = http
        self.account = _addr(account, "account")
        self.dex, self.fullcoin = R.split_fullcoin(fullcoin)
        self.network = network
        self._now = now

    def _u(self, typ: str, **kw) -> dict:
        return {"type": typ, "user": self.account, **kw}

    def abstraction(self) -> R.AccountMode:
        return R.parse_abstraction(self.http.info(self._u("userAbstraction")))

    def clearinghouse(self, *, critical: bool = False) -> dict:
        kw = {"dex": self.dex} if self.dex else {}
        ch = self.http.info(self._u("clearinghouseState", **kw), critical=critical)
        if not isinstance(ch, dict) or not isinstance(ch.get("marginSummary"), dict) \
                or not isinstance(ch.get("assetPositions"), list) or ch.get("withdrawable") is None:
            raise HlError(f"clearinghouseState({self.dex or 'main'}): неполный ответ")
        return ch

    def _positions(self, ch: dict) -> list[dict] | None:
        """Строки позиций ТОЛЬКО этого dex; монета другого dex в ответе — ответ не того ledger'а → None (H19)."""
        out = []
        for p in ch["assetPositions"]:
            pos = p.get("position") if isinstance(p, dict) else None
            coin = pos.get("coin") if isinstance(pos, dict) else None
            if not isinstance(coin, str):
                return None
            if self.dex and not coin.startswith(self.dex + ":"):
                return None
            if not self.dex and ":" in coin:
                return None
            out.append(p)
        return out

    def position(self, *, critical: bool = True) -> Decimal | None:
        """Знаковая позиция (< 0 — шорт). None — НЕИЗВЕСТНО: сбой чтения, неполный ответ, монеты чужого dex,
        не one-way. Полный ответ без строки рынка = 0 (clearinghouseState отдаёт только открытые позиции)."""
        try:
            ch = self.clearinghouse(critical=critical)
        except (HlError, BudgetExceeded, BannedError, R.HlRuleError) as e:
            log.warning("hl clearinghouseState %s: %s", self.fullcoin, type(e).__name__)
            return None
        rows = self._positions(ch)
        if rows is None:
            return None
        mine = [p for p in rows if p["position"]["coin"] == self.fullcoin]
        if len(mine) > 1 or any(p.get("type") != "oneWay" for p in mine):
            return None
        try:
            return R.to_dec(mine[0]["position"]["szi"], "szi") if mine else _D0
        except (R.HlRuleError, KeyError):
            return None

    def position_detail(self) -> dict | None:
        """entryPx, liquidationPx, плечо, маржа, cumFunding — для отчёта и дистанции до ликвидации."""
        ch = self.clearinghouse()
        rows = self._positions(ch)
        mine = [p["position"] for p in rows or () if p["position"]["coin"] == self.fullcoin]
        return mine[0] if len(mine) == 1 else None

    def spot_state(self) -> dict:
        sp = self.http.info(self._u("spotClearinghouseState"))
        if not isinstance(sp, dict) or not isinstance(sp.get("balances"), list):
            raise HlError("spotClearinghouseState: неполный ответ")
        return sp

    def margin(self, collateral_token: int | None = 0) -> R.MarginView:
        """Доступная маржа по режиму счёта (честно: режим, источник, поддержан ли для торговли)."""
        try:
            mode = self.abstraction()
        except Exception as e:      # noqa — режим не прочитан: маржа неизвестна
            return R.MarginView("unknown", None, "userAbstraction", False, f"режим не прочитан: {type(e).__name__}")
        ch = sp = None
        try:
            if mode.mode in ("standard", "default"):
                ch = self.clearinghouse()
            elif mode.mode in ("unified", "portfolio"):
                sp = self.spot_state()
        except (HlError, BudgetExceeded, BannedError, R.HlRuleError) as e:
            return R.MarginView(mode.mode, None, "чтение", False, f"не прочитано: {type(e).__name__}")
        return R.margin_view(mode, ch, sp, collateral_token)

    def active_asset_data(self) -> dict:
        a = self.http.info(self._u("activeAssetData", coin=self.fullcoin), critical=True)
        if not isinstance(a, dict) or a.get("coin") != self.fullcoin or not isinstance(a.get("leverage"), dict):
            raise HlError(f"activeAssetData: неполный ответ {str(a)[:120]}")
        if a.get("user") is not None and str(a["user"]).lower() != self.account:
            raise HlError("activeAssetData: ответ по другому адресу")
        lev = a["leverage"]
        v = lev.get("value")
        pair = lambda k: tuple(R.to_dec(x) for x in (a.get(k) or ())) or None     # noqa: E731
        return {"leverage_type": lev.get("type"), "leverage": v if isinstance(v, int) and not isinstance(v, bool)
                else None, "raw_usd": None if lev.get("rawUsd") is None else R.to_dec(lev["rawUsd"]),
                "max_trade_szs": pair("maxTradeSzs"), "available_to_trade": pair("availableToTrade"),
                "mark": None if a.get("markPx") is None else R.to_dec(a["markPx"])}

    def user_fees(self) -> dict:
        f = self.http.info(self._u("userFees"))
        if not isinstance(f, dict) or f.get("userCrossRate") is None:
            raise HlError("userFees: нет userCrossRate")
        st = f.get("activeStakingDiscount") if isinstance(f.get("activeStakingDiscount"), dict) else {}
        return {"cross": R.to_dec(f["userCrossRate"]), "add": R.to_dec(f.get("userAddRate", "0")),
                "referral_discount": R.to_dec(f.get("activeReferralDiscount") or "0"),
                "staking_discount": R.to_dec(st.get("discount") or "0")}

    def user_role(self, address: str | None = None) -> Any:
        return self.http.info({"type": "userRole", "user": _addr(address or self.account, "user")})

    def extra_agents(self, master: str) -> list[dict]:
        rows = self.http.info({"type": "extraAgents", "user": _addr(master, "master")})
        if not isinstance(rows, list):
            raise HlError("extraAgents: не список")
        return [{"name": r.get("name"), "address": str(r.get("address") or "").lower(),
                 "valid_until": r.get("validUntil")} for r in rows if isinstance(r, dict)]

    def agent_status(self, master: str, agent: str) -> dict:
        """Агент в extraAgents мастера и его срок. Не найден — не доказательство «не одобрен»: безымянный агент
        этим запросом не виден (проверка 13.09) — тогда смотреть userRole(agent)."""
        a = _addr(agent, "agent")
        hit = next((r for r in self.extra_agents(master) if r["address"] == a), None)
        now_ms = int(self._now() * 1000)
        vu = hit["valid_until"] if hit else None
        return {"listed": hit is not None, "name": hit["name"] if hit else None, "valid_until": vu,
                "expired": (vu <= now_ms) if isinstance(vu, int) else None}

    def sub_accounts(self, master: str) -> list:
        return self.http.info({"type": "subAccounts", "user": _addr(master, "master")}) or []

    def rate_limit(self) -> dict:
        return self.http.info(self._u("userRateLimit"))

    def open_orders(self) -> list:
        kw = {"dex": self.dex} if self.dex else {}
        return self.http.info(self._u("frontendOpenOrders", **kw)) or []

    def order_status(self, ref: str | int) -> R.OrderStatus:
        """orderStatus по cloid (строка 0x…) или oid (int). Сбой чтения — исключение (не «нет заявки»)."""
        if not (R.is_cloid(ref) or (isinstance(ref, int) and not isinstance(ref, bool))):
            raise ValueError(f"orderStatus: cloid или oid, а не {ref!r}")
        return R.parse_order_status(self.http.info(self._u("orderStatus", oid=ref), critical=True))

    def fills_since(self, start_ms: int, end_ms: int | None = None) -> R.TimePage:
        """Все fills счёта с start_ms (все монеты: чужие не выбрасываем до фиксации полноты страницы)."""
        def fetch(cur):
            b = self._u("userFillsByTime", startTime=int(cur), aggregateByTime=False)
            if end_ms is not None:
                b["endTime"] = int(end_ms)
            return self.http.info(b, critical=True)
        return R.paginate_by_time(fetch, start_ms, page_limit=FILLS_PAGE, max_pages=MAX_PAGES, total_cap=FILLS_CAP,
                                  parse=lambda x: R.fill_row(x, network=self.network, account=self.account),
                                  key=R.fill_key, collide=("oid", "hash", "sz", "px"))

    def funding_since(self, start_ms: int, end_ms: int | None = None) -> R.TimePage:
        def fetch(cur):
            b = self._u("userFunding", startTime=int(cur))
            if end_ms is not None:
                b["endTime"] = int(end_ms)
            return self.http.info(b)
        return R.paginate_by_time(fetch, start_ms, page_limit=FUNDING_PAGE, max_pages=MAX_PAGES,
                                  parse=lambda x: R.funding_row(x, network=self.network, account=self.account),
                                  key=R.funding_key, collide=("usdc", "szi"))


# --- журнал: nonce и попытки -----------------------------------------------------------------------
JOURNAL_SCHEMA = _store.HL_JOURNAL_SCHEMA     # DDL живёт в store.py (одна схема trade.db и её версия)
OPEN_STATES = ("PREPARED", "SIGNED", "UNKNOWN")      # исход не доказан: могла уйти


def connect_journal(path: Path | str) -> sqlite3.Connection:
    """Отдельная БД журнала адаптера (или trade.db — схема аддитивная). WAL + synchronous=FULL: запись-до должна
    пережить сбой ОС. isolation_level=None: одиночная запись фиксируется сразу, группа — BEGIN IMMEDIATE."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        con.close()
        raise HlError(f"журнал HL не перешёл в WAL (journal_mode={mode})")
    con.execute("PRAGMA synchronous=FULL")
    return con


class HlJournal:
    """nonce агента (строго возрастающий, под BEGIN IMMEDIATE — общий для всех процессов с этой БД) и попытки:
    client_id ↔ cloid, nonce, expiresAfter, действие и его хэш, подпись, ответ, исход."""

    def __init__(self, con: sqlite3.Connection, now: Callable[[], float] = time.time):
        self.con = con
        self._now = now
        if con.row_factory is None:
            con.row_factory = sqlite3.Row
        _store.ensure_journal_tables(con, "hl")

    @contextmanager
    def _tx(self):
        if self.con.in_transaction:
            yield self.con
            return
        self.con.execute("BEGIN IMMEDIATE")
        try:
            yield self.con
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    def allocate_nonce(self, network: str, signer: str, now_ms: int) -> int:
        """max(now_ms, прошлый + 1), commit ДО подписи. Часы назад не дают повтора; убежавший вперёд счёт
        (> T+1 сут) HL отвергнет — отказ здесь, а не на бирже."""
        s = _addr(signer, "signer")
        with self._tx() as c:
            row = c.execute("SELECT last_nonce FROM hl_nonces WHERE network=? AND signer=?", (network, s)).fetchone()
            last = int(row[0]) if row else 0
            n = max(int(now_ms), last + 1)
            if n - int(now_ms) > NONCE_AHEAD_MAX_MS:
                raise HlError(f"nonce {n} ушёл вперёд часов больше чем на сутки — HL отвергнет; проверь часы")
            c.execute("INSERT INTO hl_nonces(network, signer, last_nonce, updated) VALUES(?,?,?,?) "
                      "ON CONFLICT(network, signer) DO UPDATE SET last_nonce=excluded.last_nonce, "
                      "updated=excluded.updated", (network, s, n, self._now()))
        return n

    def prepare(self, **row) -> None:
        cols = ("client_id", "cloid", "kind", "network", "master", "account", "vault", "signer", "dex", "fullcoin",
                "asset", "side", "sz", "px", "reduce_only", "nonce", "expires_after", "action_json", "action_hash",
                "deal_id", "intent_id", "clip_id")
        vals = [row.get(k) for k in cols]
        try:
            with self._tx() as c:
                c.execute(f"INSERT INTO hl_order_attempts({', '.join(cols)}, state, created) "
                          f"VALUES({', '.join('?' * len(cols))}, 'PREPARED', ?)", (*vals, self._now()))
        except sqlite3.IntegrityError as e:
            raise HlError(f"попытка {row.get('client_id')}: client_id/cloid/nonce уже в журнале ({e}) — "
                          "одно экономическое действие = один cloid") from None

    def _set(self, client_id: str, sql: str, args: tuple, from_states: tuple | None = None) -> None:
        with self._tx() as c:
            where = "client_id=?" + (f" AND state IN ({','.join('?' * len(from_states))})" if from_states else "")
            cur = c.execute(f"UPDATE hl_order_attempts SET {sql} WHERE {where}",
                            (*args, client_id, *(from_states or ())))
            if cur.rowcount != 1:
                raise HlError(f"журнал HL: попытка {client_id} не обновлена ({sql.split('=')[0]})")

    def signed(self, client_id: str, sig: dict) -> None:
        self._set(client_id, "state='SIGNED', sig_json=?, signed_ts=?", (json.dumps(sig), self._now()), ("PREPARED",))

    def not_sent(self, client_id: str, why: str) -> None:
        """Доказано: POST не начинался (сбой до отправки)."""
        self._set(client_id, "state='NOT_SENT', err=?, resolved_ts=?", (redact_secrets(why)[:300], self._now()),
                  ("PREPARED", "SIGNED"))

    def result(self, client_id: str, state: str, *, http: int | None = None, body: Any = None,
               filled: Decimal | None = None, avg_px: Decimal | None = None, oid: int | None = None,
               err_kind: str | None = None, err: str | None = None) -> None:
        resp = None if body is None else json.dumps(body, default=str)[:4000]
        self._set(client_id, "state=?, http=COALESCE(?, http), response_json=COALESCE(?, response_json), "
                             "filled=?, avg_px=?, oid=COALESCE(?, oid), err_kind=?, err=?, resolved_ts=?",
                  (state, http, resp, None if filled is None else format(filled, "f"),
                   None if avg_px is None else format(avg_px, "f"), oid, err_kind,
                   None if err is None else redact_secrets(err)[:300], self._now()))

    def get(self, client_id: str) -> dict | None:
        r = self.con.execute("SELECT * FROM hl_order_attempts WHERE client_id=?", (client_id,)).fetchone()
        return dict(r) if r else None

    def unresolved(self, network: str, account: str, fullcoin: str, kind: str = "order") -> list[dict]:
        rows = self.con.execute(f"SELECT * FROM hl_order_attempts WHERE network=? AND account=? AND fullcoin=? AND "
                                f"kind=? AND state IN ({','.join('?' * len(OPEN_STATES))}) ORDER BY nonce",
                                (network, account.lower(), fullcoin, kind, *OPEN_STATES)).fetchall()
        return [dict(r) for r in rows]


# --- подпись --------------------------------------------------------------------------------------
class HlSigner:
    """Подписант L1-действий. acct — keys.SignerKey (или LocalAccount в тестах): нужны .address и .sign_message().
    master — владелец агента; account — торговый счёт (субаккаунт → vaultAddress = его адрес)."""

    def __init__(self, acct, *, agent: str, master: str, account: str | None = None, network: str = "mainnet"):
        if network not in NETWORKS:
            raise ValueError(f"сеть HL: {network!r}")
        self.agent = _addr(agent, "agent")
        self.master = _addr(master, "master")
        self.account = _addr(account or master, "account")
        if str(acct.address).lower() != self.agent:
            raise KeyMismatch(f"ключ агента HL даёт адрес {acct.address}, а агент = {agent}")
        if self.agent in (self.master, self.account):
            raise KeyMismatch("агент HL совпадает с мастером/счётом: на сервере был бы ключ основного счёта — нужен "
                              "отдельный API-кошелёк (агент), одобренный мастером")
        from eth_account.messages import encode_typed_data   # здесь: dry/readonly работают и без extra `trade`
        self._encode = encode_typed_data
        self._acct = acct
        self.network = network
        self.mainnet = network == "mainnet"
        self.vault = None if self.account == self.master else self.account

    def __repr__(self) -> str:
        return f"HlSigner(agent={self.agent}, master={self.master}, account={self.account}, net={self.network})"

    def sign(self, action: dict, nonce: int, expires_after: int | None) -> dict:
        h = R.action_hash(action, self.vault, nonce, expires_after)
        s = self._acct.sign_message(self._encode(full_message=R.l1_typed_data(h, self.mainnet)))
        return {"r": hex(s.r), "s": hex(s.s), "v": int(s.v)}      # как to_hex(int) в SDK: без ведущих нулей


# --- нога -----------------------------------------------------------------------------------------
@dataclass
class HlPerpFill(PerpFill):
    """PerpFill + то, чего нет в протоколе: категория отказа, исход в терминах ТЗ, cloid, срок действия."""
    err_kind: str | None = None
    err_text: str | None = None
    cloid: str | None = None
    outcome: str | None = None
    expires_after: int | None = None
    anomaly: bool = False


_OUTCOME = {"FILLED": "FILLED_TERMINAL", "PARTIALLY_FILLED": "PARTIAL_TERMINAL", "EXPIRED": "REJECTED_ZERO_FILL",
            "REJECTED": "REJECTED_ZERO_FILL", "UNKNOWN": "UNKNOWN", "NOT_FOUND": "NOT_SUBMITTED_PROVEN"}


class HyperliquidTrade:
    """PerpLeg для Hyperliquid (один точный рынок, один торговый счёт). Чтения — в любом режиме и без ключей;
    отправки — только live, с подписантом и журналом, через ворота режима (как AsterTrade)."""
    venue = VENUE

    def __init__(self, account: str, *, fullcoin: str = DEFAULT_FULLCOIN, master: str | None = None,
                 network: str = "mainnet", signer: HlSigner | None = None, journal: HlJournal | None = None,
                 keys: Keys | None = None, mode_state: Callable[[], tuple[str | None, bool]] | None = None,
                 session=None, base: str | None = None, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, expires_ms: int = EXPIRES_MS,
                 saved_identity: R.AssetRef | None = None, weight_limit: int = WEIGHT_LIMIT_MIN):
        if network not in NETWORKS:
            raise ValueError(f"сеть HL: {network!r}")
        if isinstance(expires_ms, bool) or not isinstance(expires_ms, int) or not 1_000 <= expires_ms <= 120_000:
            raise ValueError(f"expires_ms — целое 1000…120000, а не {expires_ms!r}")
        self.network = network
        self.dex, self.fullcoin = R.split_fullcoin(fullcoin)
        self.account = _addr(account, "account")
        self.master = _addr(master or account, "master")
        if signer is not None and (signer.account != self.account or signer.master != self.master
                                   or signer.network != network):
            raise KeyMismatch(f"подписант HL для {signer.account}/{signer.master}/{signer.network}, а нога — "
                              f"{self.account}/{self.master}/{network}")
        self._now, self._sleep = now, sleep
        self.http = HlHttp(base or NETWORKS[network], session, now=now, sleep=sleep, weight_limit=weight_limit)
        self.market = HlMarket(self.http, self.fullcoin, now=now)
        self.acct = HlAccount(self.http, self.account, fullcoin=self.fullcoin, network=network, now=now)
        self.signer, self.journal = signer, journal
        self._keys = keys
        self._mode_state = mode_state or (lambda: ("dry", False))
        self.expires_ms = expires_ms
        self.saved_identity = saved_identity
        self.last_error: str | None = None

    def __repr__(self) -> str:
        return f"HyperliquidTrade({self.network}, {self.fullcoin}, account={self.account})"

    # --- служебное --------------------------------------------------------------------------------
    def _gate(self, action: str, hedge: bool) -> None:
        try:
            mode, paused = self._mode_state()
        except Exception as e:      # noqa — не прочитали режим/паузу — закрыто
            raise ModeForbidden(f"режим/пауза не прочитаны ({type(e).__name__}): {action} запрещено") from None
        if self._keys is not None:
            self._keys.gate(mode, action, paused=bool(paused), hedge=hedge)
        else:
            mode_gate(mode, action, paused=bool(paused), hedge=hedge)

    def _now_ms(self) -> int:
        return int(self._now() * 1000)

    def _check_symbol(self, symbol: str) -> None:
        if symbol != self.fullcoin:
            raise ValueError(f"рынок {symbol!r} ≠ {self.fullcoin!r}: только точное имя HIP-3 (H01)")

    def _need_sender(self) -> None:
        if self.signer is None or self.journal is None:
            raise ModeForbidden("нет подписанта/журнала Hyperliquid (readonly): отправка запрещена")

    def _fill(self, client_id: str, status: str, *, qty: Decimal = _D0, avg: Decimal = _D0, oid: int | None = None,
              nonce: int = 0, kind: str | None = None, text: str | None = None, cloid: str | None = None,
              expires: int | None = None, anomaly: bool = False) -> HlPerpFill:
        return HlPerpFill(client_id=client_id, order_id=oid, status=status, qty=qty, avg_px=avg, quote=qty * avg,
                          sign_nonce=nonce, err_code=None, err_kind=kind, err_text=text, cloid=cloid,
                          outcome=_OUTCOME.get(status), expires_after=expires, anomaly=anomaly)

    # --- публичное --------------------------------------------------------------------------------
    def identity(self, *, max_age_s: float = RULES_TTL_S, critical: bool = False) -> R.AssetRef:
        """Свежая привязка рынка; при заданной в плане (saved_identity) — сверка, расхождение = отказ (H02)."""
        fresh = self.market.identity(max_age_s, critical=critical)
        if self.saved_identity is not None:
            R.check_same_identity(self.saved_identity, fresh)
        return fresh

    def instrument(self, symbol: str) -> PerpInstrument:
        """Native unit description for the common perpetual adapter boundary.

        The existing pilot supports USDC collateral (token 0). An unrecognized
        collateral has unknown quote currency, so the generic binding refuses it.
        Frozen market identity remains checked by identity(), including HIP-3 asset IDs.
        """
        self._check_symbol(symbol)
        ref = self.identity()
        if ref.is_delisted or ref.fullcoin != symbol:
            raise HlError('perpetual instrument is delisted or its identity changed')
        base = symbol.split(':', 1)[-1]
        quote = 'USDC' if type(ref.collateral_token) is int and ref.collateral_token == 0 else None
        return PerpInstrument(symbol, base, base, Decimal(1), quote, 'PERPETUAL')

    def filters(self, symbol: str) -> Filters:
        """Для совместимости с движком. tick — ПРОИЗВОДНАЯ правила 5 значащих у текущей середины книги; заявку
        округлять quantize_px(). max_qty — oiSzCapPerPerp из perpDexLimits (отдельного предела заявки HL не даёт)."""
        self._check_symbol(symbol)
        ref = self.identity()
        b = self.market.book(1)
        if not b.bids or not b.asks:
            raise HlError(f"{symbol}: пустая сторона книги — тик у текущей цены не определить")
        mid = (b.bids[0][0] + b.asks[0][0]) / 2
        cap = self.market.limits().get("oiSzCapPerPerp")
        if cap is None:
            raise HlError("perpDexLimits: нет oiSzCapPerPerp")
        step = R.sz_step(ref.sz_decimals)
        return Filters(tick=R.tick_at(mid, ref.sz_decimals), step=step, min_qty=step, max_qty_limit=R.to_dec(cap),
                       max_qty_market=R.to_dec(cap), min_notional=R.MIN_NOTIONAL_USD,
                       tifs=frozenset({"Ioc", "Gtc", "Alo"}))

    def quantize_px(self, symbol: str, px: Decimal, side: str) -> Decimal:
        self._check_symbol(symbol)
        return R.quantize_px(px, self.identity().sz_decimals, side)

    def floor_qty(self, symbol: str, qty: Decimal) -> Decimal:
        self._check_symbol(symbol)
        return R.floor_sz(qty, self.identity().sz_decimals)

    def book(self, symbol: str, limit: int = 20) -> Book:
        self._check_symbol(symbol)
        return self.market.book(limit, critical=True)

    def funding(self, symbol: str) -> tuple[Decimal, Decimal, int]:
        self._check_symbol(symbol)
        return self.market.funding()

    def check_clock(self, max_s: float = CLOCK_SKEW_MAX_S) -> float:
        """Часы − exchangeStatus.time (с поправкой на половину RTT). expiresAfter и доказательство «истекло»
        опираются на часы: live не стартует при расхождении больше max_s."""
        t0 = self._now()
        ms = self.market.server_time_ms()
        t1 = self._now()
        off = ms / 1000.0 - (t0 + t1) / 2.0
        if abs(off) > max_s:
            raise HlError(f"часы расходятся с Hyperliquid на {off:+.2f} с (> {max_s} с): проверь NTP, live не запускаю")
        return off

    # --- счёт -------------------------------------------------------------------------------------
    def position(self, symbol: str) -> Decimal | None:
        self._check_symbol(symbol)
        return self.acct.position(critical=True)

    def margin(self) -> R.MarginView:
        try:
            ct = self.identity().collateral_token
        except Exception as e:      # noqa — без метаданных коллатераль неизвестна
            return R.MarginView("unknown", None, "meta", False, f"метаданные рынка не прочитаны: {type(e).__name__}")
        return self.acct.margin(ct)

    def available_margin(self) -> Decimal | None:
        """Доступная маржа по режиму счёта (None — неизвестно). Можно ли по этому режиму торговать — margin()."""
        return self.margin().available

    def entry_margin_refusals(self, need_usd: Decimal, reserve_usd: Decimal | None) -> list[str]:
        """Проверка маржи ДО свопа Solana (H03): только ledger нужного dex, режим поддержан, резерв задан владельцем
        (нет значения = запрещено). Пустой список — можно."""
        out = []
        if isinstance(need_usd, bool) or not isinstance(need_usd, Decimal) or need_usd <= 0:
            raise ValueError(f"need_usd — положительный Decimal, а не {need_usd!r}")
        if reserve_usd is None:
            out.append("резерв маржи HL не задан владельцем — вход запрещён")
        m = self.margin()
        if not m.trade_supported:
            out.append(f"режим счёта HL «{m.mode}» первой версией не поддержан: {m.reason or ''}".strip())
        if m.available is None:
            out.append(f"маржа HL неизвестна ({m.source}): {m.reason or 'нет данных'}")
        elif reserve_usd is not None and m.available - reserve_usd < need_usd:
            out.append(f"маржи {m.available} USDC ({m.source}) < нужно {need_usd} + резерв {reserve_usd}")
        return out

    # --- отправка: общая часть ----------------------------------------------------------------------
    def _submit(self, kind: str, action: dict, *, client_id: str, cloid: str | None, row: dict,
                on_signed: Callable[[int], None] | None) -> tuple[int, int, int | None, Any]:
        """nonce (commit) → PREPARED (commit) → подпись → SIGNED (commit) → on_signed → ОДИН POST.
        → (nonce, expiresAfter, http | None, тело). Сбой до POST — NOT_SENT и исключение."""
        self.http.check(1, True, kind)                       # бюджет — до nonce и подписи
        now_ms = self._now_ms()
        nonce = self.journal.allocate_nonce(self.network, self.signer.agent, now_ms)
        exp = now_ms + self.expires_ms
        h = R.action_hash(action, self.signer.vault, nonce, exp)
        self.journal.prepare(client_id=client_id, cloid=cloid, kind=kind, network=self.network, master=self.master,
                             account=self.account, vault=self.signer.vault, signer=self.signer.agent, dex=self.dex,
                             fullcoin=self.fullcoin, nonce=nonce, expires_after=exp,
                             action_json=json.dumps(action, separators=(",", ":")), action_hash="0x" + h.hex(), **row)
        try:
            sig = self.signer.sign(action, nonce, exp)
            self.journal.signed(client_id, sig)
            if on_signed is not None:
                on_signed(nonce)
        except BaseException as e:
            try:
                self.journal.not_sent(client_id, f"до отправки: {type(e).__name__}: {e}")
            except Exception:       # noqa — строка останется PREPARED/SIGNED: разбор по expiry, не повтор
                log.error("hl %s: не записан NOT_SENT для %s", kind, client_id)
            raise
        payload = {"action": action, "nonce": nonce, "signature": sig, "vaultAddress": self.signer.vault,
                   "expiresAfter": exp}
        try:
            st, body = self.http.exchange(payload)
        except HlNetError as e:
            self.last_error = str(e)
            return nonce, exp, None, None
        return nonce, exp, st, body

    def _new_client_id(self, kind: str) -> str:
        return f"{kind}:{self.fullcoin}:{self._now_ms()}:{secrets.token_hex(4)}"

    # --- настройка --------------------------------------------------------------------------------
    def setup(self, symbol: str, leverage: int, margin_type: str, *, allow_with_position: bool = False) -> None:
        """Isolated и плечо: отдельное подписанное журналируемое действие с последующей сверкой по activeAssetData.
        CROSSED — отказ до сети (noCross). Уже так — ничего не отправляется. При открытой позиции — отказ без
        явного allow_with_position. Обрыв — HlNetError: повторный setup идемпотентен (сначала читает состояние)."""
        self._check_symbol(symbol)
        if margin_type != "ISOLATED":
            raise ValueError(f"{symbol}: только ISOLATED (marginMode noCross), а не {margin_type!r} — "
                             "ничего не отправлено")
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
            raise ValueError(f"плечо — целое ≥ 1, а не {leverage!r}")
        ref = self.identity(max_age_s=ORDER_IDENTITY_MAX_AGE_S, critical=True)
        if leverage > ref.max_leverage:
            raise ValueError(f"{symbol}: плечо {leverage}x > максимума рынка {ref.max_leverage}x — "
                             "ничего не отправлено")
        self._gate("send", False)
        self._need_sender()
        aad = self.acct.active_asset_data()
        if aad["leverage_type"] == "isolated" and aad["leverage"] == leverage:
            log.info("hl setup %s: уже isolated %sx", symbol, leverage)
            return
        pos = self.acct.position(critical=True)
        if pos is None:
            raise HlError(f"{symbol}: позиция неизвестна — плечо вслепую не меняю")
        if pos != 0 and not allow_with_position:
            raise HlError(f"{symbol}: открыта позиция {pos} — плечо без явной команды не меняю")
        cid = self._new_client_id("lev")
        _n, _e, st, body = self._submit("updateLeverage", R.update_leverage_action(ref.asset, leverage), client_id=cid,
                                        cloid=None, row={"asset": ref.asset}, on_signed=None)
        verdict, text = R.parse_action_response(st, body)
        state = {"ok": "OK", "err": "REJECTED", "unknown": "UNKNOWN"}[verdict]
        self.journal.result(cid, state, http=st, body=body, err=text)
        if verdict == "err":
            raise HlApiError("updateLeverage", st, text or "")
        if verdict == "unknown":
            raise HlNetError(f"updateLeverage: исход неизвестен ({text}) — setup можно повторить, он сверит чтением")
        got = self.acct.active_asset_data()
        if not (got["leverage_type"] == "isolated" and got["leverage"] == leverage):
            raise HlError(f"updateLeverage ok, но activeAssetData: {got['leverage_type']} {got['leverage']}x ≠ "
                          f"isolated {leverage}x — настройку успешной не считаю")
        log.info("hl setup %s: isolated %sx", symbol, leverage)

    def agent_refusals(self) -> list[str]:
        """Подписант — действующий агент мастера счёта (userRole агента = agent этого мастера; срок в extraAgents не
        истёк). Только чтения, ничего не подписывается; не прочитано — отказ, а не «годен». Пусто — можно слать."""
        if self.signer is None:
            return ["ключа HL нет (readonly)"]
        agent = self.signer.agent                        # ≠ мастеру и счёту (HlSigner это требует)
        short = f"{agent[:6]}…{agent[-4:]}"
        try:
            body = self.acct.user_role(agent)
        except Exception as e:      # noqa — не прочитано: годность агента не доказана
            return [f"агент HL {short} не проверен: userRole не прочитан ({type(e).__name__})"]
        d = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
        role = str(body.get("role")) if isinstance(body, dict) else str(body)[:30]
        who = str(d.get("user") or d.get("master") or "").lower() or None
        if role != "agent" or who != self.master:
            return [f"агент HL {short} не действует (userRole {role}) — одобрите API-кошелёк в HL"]
        try:
            st = self.acct.agent_status(self.master, agent)
        except Exception as e:      # noqa
            return [f"срок агента HL {short} не прочитан (extraAgents: {type(e).__name__})"]
        if st["expired"]:
            return [f"срок агента HL {short} истёк — одобрите API-кошелёк заново"]
        return []

    def noop(self) -> tuple[str, str | None]:
        """Подписанное действие, которое только сжигает nonce: проверка агента/vaultAddress без торговли (живая
        микропроверка — отдельным разрешением владельца)."""
        self._gate("send", False)
        self._need_sender()
        cid = self._new_client_id("noop")
        _n, _e, st, body = self._submit("noop", R.noop_action(), client_id=cid, cloid=None, row={}, on_signed=None)
        verdict, text = R.parse_action_response(st, body)
        self.journal.result(cid, {"ok": "OK", "err": "REJECTED", "unknown": "UNKNOWN"}[verdict], http=st, body=body,
                            err=text)
        return verdict, text

    # --- заявки -----------------------------------------------------------------------------------
    def ioc(self, symbol: str, side: str, qty: Decimal, px_cap: Decimal, client_id: str, reduce_only: bool,
            *, hedge: bool = False, on_signed: Callable[[int], None] | None = None,
            links: dict | None = None) -> HlPerpFill:
        """Limit IOC с ценой-ограничителем. Не отправлено (ворота, бюджет, параметры, незакрытая заявка, смена
        метаданных, сбой записи-до) — исключение. Отправлено — HlPerpFill:
          FILLED / PARTIALLY_FILLED (финал, остаток отменён) / EXPIRED (0, «could not immediately match»);
          REJECTED + err_kind — отказ ордера или всего действия (status err);
          UNKNOWN — нет ответа / 5xx / битый JSON / resting у IOC: НЕ ПОВТОРЯТЬ, звать settle_unknown()."""
        self._gate("send", hedge)
        self._need_sender()
        self._check_symbol(symbol)
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side BUY|SELL, а не {side!r}")
        for name, v in (("qty", qty), ("px_cap", px_cap)):
            if isinstance(v, bool) or not isinstance(v, Decimal) or not v.is_finite() or v <= 0:
                raise ValueError(f"{name} — положительный Decimal, а не {v!r}")
        if not isinstance(reduce_only, bool) or not isinstance(client_id, str) or not client_id:
            raise ValueError("reduce_only — bool, client_id — непустая строка")
        if self.journal.get(client_id) is not None:
            raise HlError(f"client_id {client_id} уже отправлялся — новая попытка = новый client_id")
        open_ = self.journal.unresolved(self.network, self.account, self.fullcoin)
        if open_:
            raise HlError(f"есть заявка с неизвестным исходом ({open_[0]['client_id']}, {open_[0]['state']}) — "
                          "сначала settle_unknown, новую не отправляю")
        ref = self.identity(max_age_s=ORDER_IDENTITY_MAX_AGE_S, critical=True)
        if not R.valid_sz(qty, ref.sz_decimals):
            raise ValueError(f"{symbol}: qty {qty} не кратно 10^-{ref.sz_decimals} — округляй floor_qty() до вызова")
        if not R.valid_px(px_cap, ref.sz_decimals):
            raise ValueError(f"{symbol}: цена {px_cap} вне правил HL (≤5 значащих, ≤{6 - ref.sz_decimals} знаков) — "
                             "quantize_px() до вызова")
        if not reduce_only and qty * px_cap < R.MIN_NOTIONAL_USD:
            raise ValueError(f"{symbol}: {qty}×{px_cap} < ${R.MIN_NOTIONAL_USD} (MinTradeNtl) — не отправляю")
        if reduce_only:
            pos = self.acct.position(critical=True)
            ok = pos is not None and ((side == "BUY" and pos < 0 and qty <= -pos) or
                                      (side == "SELL" and pos > 0 and qty <= pos))
            if not ok:
                raise HlError(f"reduceOnly {side} {qty}: позиция {pos} — сверка, заявка не отправлена (H14)")
        cloid = R.derive_cloid(self.network, self.account, client_id)
        action = R.order_action(ref.asset, side == "BUY", px_cap, qty, reduce_only, cloid, sz_decimals=ref.sz_decimals)
        self.last_error = None
        # прямые связи попытки со сделкой/намерением/клипом движка (H13): пишутся в той же строке запись-до
        ln = {k: (links or {}).get(k) for k in ("deal_id", "intent_id", "clip_id") if (links or {}).get(k) is not None}
        nonce, exp, st, body = self._submit("order", action, client_id=client_id, cloid=cloid, on_signed=on_signed,
                                            row={"asset": ref.asset, "side": side, "sz": format(qty, "f"),
                                                 "px": format(px_cap, "f"), "reduce_only": int(reduce_only), **ln})
        out = R.parse_order_response(st, body, req_sz=qty, cloid=cloid)
        if out.status in ("REJECTED", "UNKNOWN", "EXPIRED") and out.err_text:
            self.last_error = out.err_text if st is not None else (self.last_error or out.err_text)
        try:
            self.journal.result(client_id, out.status, http=st, body=body, filled=out.filled, avg_px=out.avg_px,
                                oid=out.oid, err_kind=out.err_kind, err=out.err_text)
        except Exception as e:      # noqa — ответ уже есть: строка SIGNED разберётся settle_unknown по cloid
            log.error("hl ioc %s: итог не записан в журнал (%s)", client_id, type(e).__name__)
        log.info("hl ioc %s %s %s@%s → %s %s avg %s %s", client_id, side, qty, px_cap, out.status, out.filled,
                 out.avg_px, out.err_kind or "")
        return self._fill(client_id, out.status, qty=out.filled, avg=out.avg_px, oid=out.oid, nonce=nonce,
                          kind=out.err_kind, text=out.err_text, cloid=cloid, expires=exp, anomaly=out.anomaly)

    def _saved_cloid(self, client_id: str) -> tuple[dict | None, str | None]:
        """Строка журнала и СОХРАНЁННЫЙ cloid (H11/H13). Несовпадение с выводом из client_id — чужой scope."""
        row = self.journal.get(client_id) if self.journal is not None else None
        if row is None or row.get("kind") != "order" or not R.is_cloid(row.get("cloid")):
            return None, f"нет сохранённой заявки/cloid для {client_id}"
        if row["cloid"] != R.derive_cloid(row["network"], row["account"], client_id) or row["account"] != self.account \
                or row["network"] != self.network or row["fullcoin"] != self.fullcoin:
            return None, f"cloid {client_id} из другого счёта/сети/рынка"
        return row, None

    def _status(self, cloid: str) -> R.OrderStatus | None:
        try:
            return self.acct.order_status(cloid)
        except (HlError, BudgetExceeded, BannedError, R.HlRuleError, ValueError) as e:
            self.last_error = f"orderStatus: {type(e).__name__}: {e}"[:200]
            return None

    def _from_status(self, row: dict, st: R.OrderStatus, since_ms: int) -> HlPerpFill:
        """Найденная заявка → итог. Исполненное — по origSz − sz, цена — по fills этого oid; расхождение fills с
        исполненным (fills ещё не догнали) — UNKNOWN, а не «примерно»."""
        cid, cloid, exp = row["client_id"], row["cloid"], row["expires_after"]
        req = D(row["sz"])
        n = int(row["nonce"])
        if st.cloid not in (None, cloid) or st.coin != self.fullcoin or st.orig_sz != req:
            return self._fill(cid, "UNKNOWN", oid=st.oid, nonce=n, cloid=cloid, expires=exp, anomaly=True,
                              text=f"orderStatus не той заявки: {st.coin} {st.cloid} origSz {st.orig_sz}")
        if not st.terminal:
            return self._fill(cid, "UNKNOWN", oid=st.oid, nonce=n, cloid=cloid, expires=exp,
                              anomaly=st.status == "open", text=f"статус {st.status} — не финал")
        ex = st.executed
        if ex is None or ex < 0 or ex > req:
            return self._fill(cid, "UNKNOWN", oid=st.oid, nonce=n, cloid=cloid, expires=exp, anomaly=True,
                              text=f"исполнено {ex} при заявке {req}")
        if ex == 0:
            status, kind = R.status_zero_fill(st)
            return self._fill(cid, status, oid=st.oid, nonce=n, kind=kind, text=st.status, cloid=cloid, expires=exp)
        try:
            page = self.acct.fills_since(max(0, int(since_ms) - FILLS_SLACK_MS))
        except (HlError, BudgetExceeded, BannedError, R.HlRuleError) as e:
            return self._fill(cid, "UNKNOWN", oid=st.oid, nonce=n, cloid=cloid, expires=exp,
                              text=f"fills не прочитаны: {type(e).__name__}")
        mine = [r for r in page.rows if r["oid"] == st.oid and r["coin"] == self.fullcoin]
        tot = sum((r["sz"] for r in mine), _D0)
        if tot != ex:
            return self._fill(cid, "UNKNOWN", oid=st.oid, nonce=n, cloid=cloid, expires=exp,
                              text=f"fills по oid {st.oid}: {tot} ≠ исполнено {ex}")
        avg = sum((r["px"] * r["sz"] for r in mine), _D0) / tot
        return self._fill(cid, "FILLED" if ex == req else "PARTIALLY_FILLED", qty=ex, avg=avg, oid=st.oid, nonce=n,
                          cloid=cloid, expires=exp)

    def query(self, symbol: str, client_id: str) -> HlPerpFill:
        """orderStatus по СОХРАНЁННОМУ cloid. unknownOid → NOT_FOUND, но это ОДНО наблюдение, не доказательство:
        доказательство даёт только settle_unknown() после expiresAfter. sign_nonce — nonce самой заявки."""
        self._check_symbol(symbol)
        row, why = self._saved_cloid(client_id)
        if row is None:
            self.last_error = why
            return self._fill(client_id, "UNKNOWN", text=why)
        st = self._status(row["cloid"])
        if st is None:
            return self._fill(client_id, "UNKNOWN", nonce=int(row["nonce"]), cloid=row["cloid"],
                              expires=row["expires_after"], text=self.last_error)
        if not st.found:
            return self._fill(client_id, "NOT_FOUND", nonce=int(row["nonce"]), cloid=row["cloid"],
                              expires=row["expires_after"], text="unknownOid (одно наблюдение)")
        return self._from_status(row, st, int((row.get("created") or self._now()) * 1000))

    def submission_absent(self, symbol: str, client_id: str, account_id: str, *, proof_con) -> bool:
        """Local proof used only by the sole executor after common admission.

        Every POST requires this native journal's committed SIGNED row. No row,
        or an explicit NOT_SENT row, proves no POST for this client ID. PREPARED
        and SIGNED remain unresolved; absence of a common signing nonce is not
        evidence. The caller must own execution and verify its common attempt.
        """
        from .runtime import hl_account_id
        self._check_symbol(symbol)
        if account_id != hl_account_id(self.network, self.master, self.account, self.dex):
            raise HlError('common attempt account differs from native account')
        if self.journal is None or not proof_con.in_transaction:
            return False
        # The caller holds BEGIN IMMEDIATE. Native prepare/sign must use this
        # same database, and on_signed must observe the resulting common state
        # before POST. A different journal cannot provide this atomic proof.
        if self.journal.con is not proof_con:
            from pathlib import Path
            def main_path(con):
                return next((r[2] for r in con.execute('PRAGMA database_list') if r[1] == 'main'), '')
            own, common = main_path(self.journal.con), main_path(proof_con)
            if not own or not common or Path(own).resolve() != Path(common).resolve():
                return False
            if self.journal.con.in_transaction:
                return False
        row = proof_con.execute('SELECT * FROM hl_order_attempts WHERE client_id=?', (client_id,)).fetchone()
        if row is None:
            return True
        row = dict(row)
        saved, why = self._saved_cloid(client_id)
        return saved is not None and saved['state'] == 'NOT_SENT'

    def settle_unknown(self, symbol: str, client_id: str, *, pos_before: Decimal | None, since_ms: int,
                       known_order_ids=frozenset(), wait: bool = True) -> HlPerpFill:
        """Исход заявки без повторной отправки (см. шапку). Ждёт expiresAfter + запас, опрашивая orderStatus;
        найдена с финалом → её итог (запись в журнал); иначе NOT_FOUND только с полным набором доказательств;
        иначе UNKNOWN. wait=False — один опрос (для сверки при старте)."""
        self._check_symbol(symbol)
        row, why = self._saved_cloid(client_id)
        if row is None:
            self.last_error = why
            return self._fill(client_id, "UNKNOWN", text=why)
        cloid, exp, n = row["cloid"], row["expires_after"], int(row["nonce"])
        if row["state"] in FINAL or row["state"] in ("NOT_FOUND", "NOT_SENT"):
            if row["state"] == "NOT_SENT":
                return self._fill(client_id, "NOT_FOUND", nonce=n, cloid=cloid, expires=exp, text="не отправлялась")
            if row["state"] in FINAL:
                q, a = D(row["filled"] or "0"), D(row["avg_px"] or "0")
                return self._fill(client_id, row["state"], qty=q, avg=a, oid=row["oid"], nonce=n, kind=row["err_kind"],
                                  text=row["err"], cloid=cloid, expires=exp)
            return self._fill(client_id, "NOT_FOUND", nonce=n, cloid=cloid, expires=exp, text=row["err"])
        gap = UNKNOWN_POLL_GAP_S
        horizon = (exp + CLOCK_SLACK_MS) / 1000.0 if exp is not None else self._now()
        deadline = max(self._now(), horizon) + (NOT_FOUND_POLLS + 1) * gap
        # найдена хоть раз (здесь или раньше: oid в журнале) — NOT_FOUND больше нет, только её финал или UNKNOWN
        seen = row.get("oid") is not None
        after, last = 0, None
        while True:
            st = self._status(cloid)
            if st is None:
                after = 0                               # сбой чтения рвёт серию «подряд»
            elif st.found:
                seen, after = True, 0
                f = self._from_status(row, st, since_ms)
                last = f
                if f.status in FINAL:
                    self._journal_result(client_id, f)
                    return f
            elif exp is not None and self._now_ms() > exp + CLOCK_SLACK_MS:
                after += 1
                if after >= NOT_FOUND_POLLS and not seen:
                    break
            if not wait or self._now() >= deadline:
                break
            self._sleep(gap)
        if seen and last is None:
            last = self._fill(client_id, "UNKNOWN", oid=row["oid"], nonce=n, cloid=cloid, expires=exp,
                              text=f"заявка уже найдена (oid {row['oid']}), unknownOid ×{after} её не отменяет")
        unknown = last if last is not None else self._fill(client_id, "UNKNOWN", nonce=n, cloid=cloid, expires=exp,
                                                           text=self.last_error or "исход не доказан")
        if seen or after < NOT_FOUND_POLLS:
            self._journal_result(client_id, unknown)
            return unknown
        pos = self.acct.position(critical=True) if pos_before is not None else None
        if pos is None or pos != pos_before:
            unknown = self._fill(client_id, "UNKNOWN", nonce=n, cloid=cloid, expires=exp,
                                 text=f"unknownOid после срока, но позиция {pos} ≠ {pos_before}")
            self._journal_result(client_id, unknown)
            return unknown
        try:
            page = self.acct.fills_since(max(0, int(since_ms) - FILLS_SLACK_MS))
        except (HlError, BudgetExceeded, BannedError, R.HlRuleError) as e:
            page, why = None, type(e).__name__
        if page is None or not page.complete:
            unknown = self._fill(client_id, "UNKNOWN", nonce=n, cloid=cloid, expires=exp,
                                 text=f"история fills неполная ({page.gap if page else why})")
            self._journal_result(client_id, unknown)
            return unknown
        known = {int(x) for x in known_order_ids}
        new = [r for r in page.rows if r["coin"] == self.fullcoin and (r["cloid"] == cloid or r["oid"] not in known)]
        if new:
            unknown = self._fill(client_id, "UNKNOWN", nonce=n, cloid=cloid, expires=exp,
                                 text=f"unknownOid после срока, но в fills {len(new)} новых сделок {self.fullcoin}")
            self._journal_result(client_id, unknown)
            return unknown
        f = self._fill(client_id, "NOT_FOUND", nonce=n, cloid=cloid, expires=exp,
                       text=f"unknownOid ×{after} после expiresAfter, позиция и fills неизменны — не выставлена")
        self._journal_result(client_id, f)
        return f

    def _journal_result(self, client_id: str, f: HlPerpFill) -> None:
        try:
            self.journal.result(client_id, f.status, filled=f.qty, avg_px=f.avg_px, oid=f.order_id, err_kind=f.err_kind,
                                err=f.err_text)
        except Exception as e:      # noqa — журнал не обновлён: следующая сверка повторит разбор
            log.error("hl %s: итог разбора не записан (%s)", client_id, type(e).__name__)

    def pending(self) -> list[dict]:
        """Незакрытые попытки этого счёта/рынка — разбирать ДО новых команд (после рестарта, X10/M10)."""
        return [] if self.journal is None else self.journal.unresolved(self.network, self.account, self.fullcoin)

    # --- учёт -------------------------------------------------------------------------------------
    def fills(self, symbol: str, from_id: int | None) -> list[dict]:
        raise NotImplementedError("Hyperliquid: курсора fromId нет (tid — хэш, не счётчик) — учёт через "
                                  "fills_since(symbol, start_ms) с перекрытием и признаком полноты")

    def fills_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> R.TimePage:
        """Fills рынка с start_ms: полнота считается по ВСЕМ монетам счёта, потом фильтр по точному fullcoin."""
        self._check_symbol(symbol)
        p = self.acct.fills_since(start_ms, end_ms)
        return R.TimePage(rows=[r for r in p.rows if r["coin"] == self.fullcoin], complete=p.complete, gap=p.gap,
                          pages=p.pages, last_time=p.last_time)

    def funding_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> R.TimePage:
        self._check_symbol(symbol)
        p = self.acct.funding_since(start_ms, end_ms)
        return R.TimePage(rows=[r for r in p.rows if r["coin"] == self.fullcoin], complete=p.complete, gap=p.gap,
                          pages=p.pages, last_time=p.last_time)

    def funding_income(self, symbol: str, start_ms: int) -> list[dict]:
        """Фактические начисления userFunding с start_ms (строки HL, ключ network/account/coin/time/hash).
        Неполная история — исключение: неполный funding не выдаётся за итог."""
        p = self.funding_since(symbol, start_ms)
        if not p.complete:
            raise HlError(f"userFunding {symbol}: история неполная ({p.gap})")
        return p.rows

    def health(self) -> dict:
        h = self.http.health()
        h.update(fullcoin=self.fullcoin, account=self.account, network=self.network,
                 signer=self.signer.agent if self.signer else None, vault=self.signer.vault if self.signer else None,
                 pending=len(self.pending()), last_book_ms=self.market.last_book_ms)
        return h
