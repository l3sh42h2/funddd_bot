"""Истина Gate фьючерсов USDT (api.gateio.ws/api/v4). Свои запросы и свой разбор, без клиента коллектора gate_fut.py.

Замерено с Мака 13.09.2026:
- Охват — бессрочные с расчётами в USDT (/futures/usdt, 981 контракт, все type direct). BTC-settled (/futures/btc) —
  коин-маржинальные, вне охвата, как COIN-M у Binance.
- Список рынков — GET /futures/usdt/contracts (1.26 МБ, ~3 с): name ровно как у биржи и дашборда («BTC_USDT»,
  «MBABYDOGE_USDT», «龙虾_USDT»), status (trading), in_delisting, is_pre_market, launch_time (с), contract_type,
  funding_interval (с: 28800 ×597, 14400 ×379, 3600 ×5 — меняется на ходу), funding_next_apply (с), funding_rate.
  Торгуется = status trading, не in_delisting и запуск не в будущем; остальные — в списке с tradable=False.
- База — имя до «_USDT». Единственное исключение: MBABYDOGE — это 1 000 000 BABYDOGE (марк 0.000381 против спота
  BABYDOGE_USDT 3.8e-10 у той же биржи), база отдаётся как «1MBABYDOGE», чтобы аудитор прочёл единицу. 0G, 1INCH, 2Z, 4,
  4STOCK — настоящие имена.
- Класс — поля биржи: is_pre_market → preipo (OPENAI, ANTHROPIC, KALSHI, … — акции; BP — монета; все до запуска), кроме
  индексов: B200 / H100 (pre-market, contract_type indices) — индексы аренды GPU Silicon Data в USD за GPU-час, тот же
  продукт и та же цена, что у Bitget B200USDT / H100USDT (5.81 / 5.80, 2.672 / 2.66 — 13.09) → index; contract_type «» → crypto, stocks → equity, indices → index, commodities → commodity (BZ, CL, NG),
  metals → commodity (XAU, XAG, XPT, XPD, XCU, XAL, XNI, XPB), forex → fx (EURUSD, GBPUSD). Поправки по смыслу имени:
  PAXG / XAUT (Gate: metals) — токены золота → crypto (правило репозитория); USDC_USDT (Gate: forex) — стейблкоин →
  crypto; IAU / SLV (Gate: metals) — биржевые фонды iShares → equity. Незнакомый contract_type — None.
- Ставка — funding_rate из GET /futures/usdt/tickers (0.3 с): доля ЗА ИНТЕРВАЛ, живой прогноз ближайшего расчёта
  (= funding_rate_indicative). contracts отдаёт то же поле, но из кэша на секунды старше (ETH 0.000099 против 0.0001 у
  тикеров) — берётся, только если контракта нет в тикерах.
- Следующий расчёт — funding_next_apply × 1000 (сейчас на сетке UTC у 981 из 981). Поле перекатывается раньше расчёта
  (замер коллектора 12.09: в 14:59:19 уже 16:00 у часовых контрактов) и может отставать после него — поэтому время дальше
  одного интервала от «сейчас» сдвигается на интервал назад, а прошедшее — вперёд по сетке интервала.
- История — GET /futures/usdt/funding_rate {contract, from, to, limit ≤ 1000}: строки {"r", "t"} НОВЕЙШИЕ первыми, t — в
  СЕКУНДАХ (расчёт через 0–6 с после сетки: 1789228802), r — доля за интервал; при from и to отдаёт новейшие limit строк
  окна — страницы идут назад (to = самый старый t). from в мс биржа молча отвечает [] — только секунды. Глубина 180
  дней (старше — HTTP 400): начало окна поджимается к 179 дням. Незнакомый контракт — HTTP 400 CONTRACT_NOT_FOUND.
- Лимит — 200 запросов / 10 с на IP на точку (x-gate-ratelimit-*), общий с живым коллектором: темп истории 0.15 с.
"""
from __future__ import annotations
import time
from decimal import Decimal
from .base import Truth, Http, H_MS, dec, to_int, market, rate, now_ms

URL = "https://api.gateio.ws/api/v4"
CONTRACTS = "/futures/usdt/contracts"
TICKERS = "/futures/usdt/tickers"
HISTORY = "/futures/usdt/funding_rate"
LIMIT = 1000                        # потолок точки истории: 1000 расчётов = 41 день при 1 ч
MAX_PAGES = 10
DEPTH_S = 179 * 86400               # глубже 180 дней биржа отвечает 400
GOLD = frozenset({"PAXG", "XAUT"})
STABLE = frozenset({"USDC"})
ETF = frozenset({"IAU", "SLV"})
UNIT = {"MBABYDOGE": "1MBABYDOGE"}
TYPES = {"": "crypto", "stocks": "equity", "indices": "index", "commodities": "commodity", "metals": "commodity",
         "forex": "fx"}


def _t(s: int) -> str:
    return time.strftime("%m-%d %H:%M UTC", time.gmtime(s))


def asset_class(base_asset: str, contract_type: str | None, pre_market) -> str | None:
    b = str(base_asset or "").upper()
    ct = str(contract_type or "").strip().lower()
    if pre_market:
        return "index" if ct == "indices" else "preipo"     # pre-market — фаза торгов; у индекса единица та же (см. шапку)
    if b in GOLD or b in STABLE:
        return "crypto"
    if ct == "metals" and b in ETF:
        return "equity"
    return TYPES.get(ct)


def interval_h(c: dict) -> int | None:
    s = to_int(c.get("funding_interval"))
    return max(1, round(s / 3600)) if s and s > 0 else None


def next_ms(c: dict, iv: int | None, now: int) -> int | None:
    """funding_next_apply (с) → мс; перекатившееся раньше расчёта — на интервал назад, отставшее — вперёд по сетке."""
    n = to_int(c.get("funding_next_apply"))
    if not n or n <= 0:
        return None
    n *= 1000
    if not iv:
        return n
    step = iv * H_MS
    if n - now > step:
        n -= ((n - now - 1) // step) * step
    if n <= now:
        n += ((now - n) // step + 1) * step
    return n


class GateTruth(Truth):
    venue = "gate"
    HIST_GAP_S = 0.15
    SNAP_TTL_S = 5.0                   # markets() и сразу rates() — один список контрактов; перепроверка — уже новый

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._snap: tuple[float, dict[str, dict]] | None = None

    def _contracts(self) -> dict[str, dict]:
        if self._snap and time.time() - self._snap[0] < self.SNAP_TTL_S:
            return self._snap[1]
        data = self.http.get(URL + CONTRACTS)
        rows = {c["name"]: c for c in data if isinstance(c, dict) and c.get("name")} if isinstance(data, list) else {}
        if not rows:
            raise RuntimeError(f"gate {CONTRACTS}: пустой список контрактов: {str(data)[:200]}")
        self._snap = (time.time(), rows)
        return rows

    def markets(self) -> dict[str, dict]:
        now_s = now_ms() / 1000
        out = {}
        for name, c in self._contracts().items():
            ba = name.rsplit("_", 1)[0]
            st = c.get("status") or "?"
            launch = to_int(c.get("launch_time")) or 0
            why = []
            if st != "trading":
                why.append(f"статус {st}")
            if c.get("in_delisting"):
                why.append("делистинг (in_delisting)")
            if launch > now_s:
                why.append(f"запуск {_t(launch)}")
            ct = c.get("contract_type") or ""
            note = (ct or "монета") + (" / pre-market" if c.get("is_pre_market") else "") + \
                   (f" / тип {c.get('type')}" if (c.get("type") or "direct") != "direct" else "") + \
                   (f" / {ba} = 1 000 000 BABYDOGE" if ba.upper() in UNIT else "") + \
                   ("" if not why else " / " + "; ".join(why))
            out[name] = market(UNIT.get(ba.upper(), ba), not why, asset_class(ba, ct, c.get("is_pre_market")),
                               interval_h(c), None, note)
        return out

    def rates(self) -> dict[str, dict]:
        cons = self._contracts()
        try:
            tick = {t["contract"]: t for t in self.http.get(URL + TICKERS) or [] if isinstance(t, dict) and t.get("contract")}
        except Exception:  # noqa — без тикеров ставка из contracts (то же поле, на секунды старше)
            tick = {}
        now = now_ms()
        out = {}
        for name, c in cons.items():
            iv = interval_h(c) or 8
            r = dec((tick.get(name) or {}).get("funding_rate"))
            if r is None:
                r = dec(c.get("funding_rate"))
            out[name] = rate(r, iv, next_ms(c, iv, now), "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        start_ms, end_ms = int(start_ms), int(end_ms)
        lo = max(start_ms // 1000, int(now_ms() / 1000) - DEPTH_S)
        to = end_ms // 1000 + 1                               # to не включается: расчёт ровно в end_ms попадёт
        got: dict[int, Decimal] = {}
        for _ in range(MAX_PAGES):
            if to <= lo:
                break
            rows = self.http.get(URL + HISTORY, {"contract": symbol, "from": lo, "to": to, "limit": LIMIT},
                                 gap=self.HIST_GAP_S)
            if not isinstance(rows, list):
                raise RuntimeError(f"gate {symbol}: история не списком: {str(rows)[:200]}")
            stamps = []
            for r in rows:
                t = to_int(r.get("t")) if isinstance(r, dict) else None
                if t is None:
                    continue
                stamps.append(t)
                v, ms = dec(r.get("r")), t * 1000
                if v is not None and start_ms <= ms <= end_ms:
                    got[ms] = v
            if len(rows) < LIMIT or not stamps:
                break
            to = min(stamps)
        else:
            raise RuntimeError(f"gate {symbol}: история не дошла до {start_ms} за {MAX_PAGES} страниц")
        return sorted(got.items())
