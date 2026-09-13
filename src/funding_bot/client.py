"""HTTP-клиент для бирж с Binance-подобным API (Binance futures/spot, Aster).

Бюджет веса читается из заголовка ответа (`X-MBX-USED-WEIGHT-1M`, регистр у бирж разный), а не
считается по прайс-листу: у Aster документированные веса не совпадают с фактическими (premiumIndex
по всем символам заявлен как 1, а стоит ~20). Тик пропускается уже при 70 % бюджета — 429 это
поздно, 418 это бан до трёх дней.
"""
from __future__ import annotations
import time, logging, threading
import requests
from . import config

log = logging.getLogger(__name__)


class PermanentHTTPError(RuntimeError):
    """4xx кроме 429/418: повтор не поможет."""


class BannedError(RuntimeError):
    """418: биржа забанила IP; ждать столько, сколько сказала."""


class BudgetExceeded(RuntimeError):
    """Собственный предохранитель: бюджет веса выше мягкого порога, вызов не сделан."""


class BinanceLike:
    def __init__(self, name: str, base: str, weight_limit: int, session: requests.Session | None = None):
        self.name, self.base, self.weight_limit = name, base, weight_limit
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._lock = threading.Lock()
        self.used_weight = 0            # последнее значение из заголовка
        self.used_weight_ts = 0.0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0
        self.banned_until = 0.0
        self._min_gap: dict[str, float] = {}   # путь -> минимальный интервал между вызовами
        self._last_call: dict[str, float] = {}

    # --- бюджет -------------------------------------------------------------------
    def budget_used(self) -> float:
        """Доля минутного бюджета по последнему заголовку; протухает через минуту."""
        if time.time() - self.used_weight_ts > 60:
            return 0.0
        return self.used_weight / float(self.weight_limit)

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self.banned_until and self.budget_used() < soft

    def set_min_gap(self, path: str, gap_s: float):
        self._min_gap[path] = gap_s

    def _read_weight(self, r: requests.Response):
        for k, v in r.headers.items():
            kl = k.lower()
            if kl.startswith("x-mbx-used-weight"):
                try:
                    self.used_weight = int(v); self.used_weight_ts = time.time()
                except ValueError:
                    pass
                break

    # --- вызов ---------------------------------------------------------------------
    def get(self, path: str, params: dict | None = None, retries: int = 3, timeout: int = config.HTTP_TIMEOUT,
            soft: float | None = None):
        if time.time() < self.banned_until:
            raise BannedError(f"{self.name}: бан до {time.strftime('%H:%M:%S', time.gmtime(self.banned_until))}")
        soft = config.WEIGHT_SOFT_LIMIT if soft is None else soft
        if self.budget_used() >= soft:
            raise BudgetExceeded(f"{self.name}: бюджет {self.budget_used():.0%} >= {soft:.0%}, {path} пропущен")
        gap = self._min_gap.get(path)
        if gap:
            with self._lock:
                wait = self._last_call.get(path, 0.0) + gap - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last_call[path] = time.time()
        url = self.base + path
        last = None
        for i in range(retries):
            try:
                r = self._s.get(url, params=params, timeout=timeout)
                self._read_weight(r)
                if r.status_code == 429:
                    self.n_429 += 1
                    ra = float(r.headers.get("Retry-After", 0) or 0)
                    log.warning("%s 429 на %s, used=%s, retry-after=%s", self.name, path, self.used_weight, ra)
                    time.sleep(max(ra, 2.0 * (i + 1)))
                    continue
                if r.status_code == 418:
                    ra = float(r.headers.get("Retry-After", 120) or 120)
                    self.banned_until = time.time() + ra
                    raise BannedError(f"{self.name}: 418 на {path}, retry-after {ra:.0f} с")
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{r.status_code} for {r.url}: {r.text[:200]}")
                r.raise_for_status()
                self.last_ok_ts = time.time()
                return r.json()
            except (PermanentHTTPError, BannedError):
                raise
            except Exception as e:  # noqa: сеть, 5xx, битый JSON — повторяем
                last = e; self.n_err += 1
                time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"GET {url} failed: {last}")

    def bstocks(self) -> set[str]:
        """Символы bStocks (токенизированные акции) спота Binance — по тегу в публичном списке продуктов www.binance.com.
        Только у спотового клиента; у фьючерсных — пусто."""
        if config.EXCHANGES.get(self.name, {}).get("kind") != "spot":
            return set()
        r = self._s.get(config.BINANCE_PRODUCTS_URL, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return {p["s"] for p in (r.json().get("data") or []) if "bStocks" in (p.get("tags") or []) and p.get("q") == config.QUOTE}

    def health(self) -> dict:
        return {"exchange": self.name, "used_weight": self.used_weight, "budget": round(self.budget_used(), 3),
                "last_ok_ts": int(self.last_ok_ts), "n_429": self.n_429, "n_err": self.n_err,
                "banned_until": int(self.banned_until)}


def make_clients() -> dict:
    """Клиенты всех площадок: Binance-подобные по config.EXCHANGES + Hyperliquid (свой POST-клиент)."""
    out = {}
    for name, c in config.EXCHANGES.items():
        cl = BinanceLike(name, c["base"], c["weight_limit"])
        gap = config.FUNDING_HISTORY_MIN_GAP_S.get(name)
        if gap:
            cl.set_min_gap("/fapi/v1/fundingRate", gap)
        out[name] = cl
    if "hyperliquid" in config.PERP_VENUES:
        from .hyperliquid import Hyperliquid      # здесь, а не наверху: hyperliquid импортирует классы ошибок отсюда
        out["hyperliquid"] = Hyperliquid()
    # перпы со своим API (12.09, порядок владельца): KuCoin, Bitget, Gate, Lighter (основной и Robinhood). Конструкторы
    # сети не трогают; поток WebSocket Lighter стартует при первом books() — фоновые копии обслуживания истории
    # (коллектор зовёт make_clients() на каждый проход) его не открывают, а темп и пауза 429 у Lighter общие на хост.
    from .kucoin_fut import KucoinFutures
    from .bitget_fut import BitgetFut
    from .gate_fut import GateFut
    from .lighter import PERP_CLIENTS as LIGHTER_PERPS, SPOT_CLIENTS as LIGHTER_SPOTS
    # 13.09 (порядок владельца): Backpack, Variational, edgeX, Extended, Pacifica, ApeX. Конструкторы тоже без сети; потоки
    # WebSocket (Backpack, edgeX, Pacifica, ApeX) — с первым premium()/books(); темп, пауза 429 и кэши — общие на хост
    # внутри модуля (у Variational там же и собственная история — фоновые копии видят её)
    from .backpack import PERP_CLIENTS as BACKPACK_PERPS
    from .variational import PERP_CLIENTS as VARIATIONAL_PERPS
    from .edgex import PERP_CLIENTS as EDGEX_PERPS
    from .extended import PERP_CLIENTS as EXTENDED_PERPS
    from .pacifica import PERP_CLIENTS as PACIFICA_PERPS
    from .apex import PERP_CLIENTS as APEX_PERPS
    perps = {"kucoin": KucoinFutures, "bitget": BitgetFut, "gate": GateFut, **LIGHTER_PERPS,
             **BACKPACK_PERPS, **VARIATIONAL_PERPS, **EDGEX_PERPS, **EXTENDED_PERPS, **PACIFICA_PERPS, **APEX_PERPS}
    for v in config.PERP_VENUES:
        if v not in out and v in perps:
            out[v] = perps[v]()
    from .spot import SPOT_CLIENTS                # споты со своим API (Gate, KuCoin, Bitget) — тоже импортируют ошибки отсюда
    spots = {**SPOT_CLIENTS, **LIGHTER_SPOTS}
    for v in config.SPOT_VENUES:
        if v not in out and v in spots:
            out[v] = spots[v]()
    from .okxdex import OkxDex                    # спот-нога DEX — только с ключом владельца в .env (иначе её нет вовсе)
    dex = OkxDex()
    if dex.enabled():
        out["okxdex"] = dex
    return out
