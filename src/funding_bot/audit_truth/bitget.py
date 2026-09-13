"""Истина Bitget фьючерсов (api.bitget.com). Свои запросы и свой разбор, без клиента коллектора bitget_fut.py.

Замерено с Мака 13.09.2026:
- Охват — линейные бессрочные в долларе: productType USDT-FUTURES (787 рынков) и USDC-FUTURES (49: BTCPERP, ETHPERP, …,
  все монеты есть и в USDT-M — аудитор считает их дублями по квоте). COIN-FUTURES (коин-маржинальные) — вне охвата, как
  COIN-M у Binance.
- Список рынков — GET /api/v2/mix/market/contracts {productType} (коллектор берёт v3 — здесь нарочно другая точка):
  symbol ровно как у биржи и дашборда («BTCUSDT», «1000BONKUSDT», «龙虾USDT», «BTCPERP»), baseCoin, symbolType
  perpetual / delivery (поставочные — без фандинга, в список не идут), symbolStatus (normal — торгуется; listed — до
  запуска; maintain, limit_open, restrictedAPI, off — нет), offTime / limitOpenTime («-1» или мс: назначен делистинг,
  хотя статус ещё normal — PKXUSDT), launchTime, fundInterval (ч).
  Торгуется = symbolStatus normal, делистинг не назначен и запуск не в будущем. Рынок с назначенным делистингом — в
  списке с tradable=False и временем в примечании: на дашборде его не ждём.
- Класс — GET /api/v3/market/instruments {category}: symbolType crypto / stock / metal / commodity и isRwa. crypto без
  RWA → crypto; stock → equity; metal / commodity → commodity; PAXG / XAUT (Bitget: metal) — токены золота → crypto
  (правило репозитория). crypto + isRwa YES (11 рынков: EURUSD, USDJPY, GBPUSD, B200, H100, BHP, RIO, VALE, FCX, HPQ,
  KUAISHOU) — биржа сама себе противоречит: класс None (неизвестен); кроме B200 / H100 — «Pre-Market Compute Perpetuals»
  по индексам аренды GPU Silicon Data в USD за GPU-час (анонс Bitget 08–09.09.2026) → index. Биржа не отмечает pre-IPO и
  индексы, всё это у неё stock: частные компании OPENAI, ANTHROPIC, SPCX (SpaceX), MOONSHOT (Moonshot AI) → preipo; индексы
  SP500, NDX100, HSI, JP225, KR200 → index — по смыслу имени и составу индекса (13.09: HSI — ITICK_INDEX 24793, JP225 —
  HYPERLIQUID / INFOWAY_INDEX 64916, KR200 — ITICK_INDEX / HYPERLIQUID / OKX_INDEX 1099: пункты индекса, не цена акции).
  До 13.09 HSI / JP225 / KR200 шли «акцией», и KR200 Bitget был «другим активом» для индекса KR200 у HL.
  v3 не ответила — класс None у всех (присутствие это переживёт).
- Ставка — GET /api/v2/mix/market/current-fund-rate {productType}: fundingRate — доля ЗА ИНТЕРВАЛ, живой прогноз
  ближайшего расчёта (равна fundingRate из /api/v2/mix/market/tickers у всех 787; коллектор берёт тикеры),
  fundingRateInterval (ч, 8 ×412, 4 ×384, 1 ×2 — меняется на ходу), nextUpdate (мс, на сетке UTC). В ответе ещё ~11
  тестовых / нелистингованных символов (RWATESTMEUSDT, BGTESTMEUSDT, …) — ставки берутся только для рынков списка.
- История — GET /api/v2/mix/market/history-fund-rate {symbol, productType, pageSize ≤ 100, pageNo ≤ 100}: НОВЕЙШИЕ
  первыми, startTime / endTime биржа игнорирует — листаем pageNo, пока страница не короче 100 или не дошли до начала
  окна. pageNo > 100 → HTTP 400 code 40808 (конец); незнакомый символ → HTTP 400 code 40034. fundingTime — мс расчёта,
  fundingRate — доля за интервал. Глубина ~90 дней.
- Лимит — 20 запросов / с на IP на каждую точку (x-mbx-used-remain-limit), общий с живым коллектором: темп истории 0.2 с.
"""
from __future__ import annotations
import time
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, now_ms

URL = "https://api.bitget.com"
PRODUCTS = ("USDT-FUTURES", "USDC-FUTURES")
CONTRACTS = "/api/v2/mix/market/contracts"
INSTRUMENTS = "/api/v3/market/instruments"
FUND = "/api/v2/mix/market/current-fund-rate"
HISTORY = "/api/v2/mix/market/history-fund-rate"
PAGE = 100                          # pageSize больше 100 биржа молча режет до 100
MAX_PAGE_NO = 100                   # pageNo > 100 → 40808
END_CODE = "40808"
GOLD = frozenset({"PAXG", "XAUT"})
PREIPO = frozenset({"OPENAI", "ANTHROPIC", "SPCX", "MOONSHOT"})
INDEXES = frozenset({"SP500", "NDX100", "HSI", "JP225", "KR200"})     # у биржи stock: пункты индекса (см. шапку)
COMPUTE = frozenset({"B200", "H100"})           # у биржи crypto + RWA: индексы аренды GPU Silicon Data (см. шапку)
TYPES = {"crypto": "crypto", "stock": "equity", "metal": "commodity", "commodity": "commodity"}


def _t(ms: int) -> str:
    return time.strftime("%m-%d %H:%M UTC", time.gmtime(ms / 1000))


def asset_class(coin: str, symbol_type: str | None, is_rwa) -> str | None:
    """Класс по полям v3 (symbolType, isRwa); None — биржа класса не говорит или сама себе противоречит."""
    b = str(coin or "").upper()
    st = str(symbol_type or "").lower()
    rwa = str(is_rwa or "").upper() == "YES"
    if b in GOLD:
        return "crypto"
    if st == "crypto":
        if not rwa:
            return "crypto"
        return "index" if b in COMPUTE else None
    if st == "stock":
        return "preipo" if b in PREIPO else ("index" if b in INDEXES else "equity")
    return TYPES.get(st)


class BitgetTruth(Truth):
    venue = "bitget"
    HIST_GAP_S = 0.2
    SNAP_TTL_S = 5.0                   # markets() и сразу rates() — один снимок ставок; перепроверка — уже новый

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._fund: tuple[float, dict[str, dict]] | None = None
        self._product: dict[str, str] = {}
        self._perps: dict[str, dict] | None = None

    def _data(self, path: str, params: dict, gap: float | None = None):
        body = self.http.get(URL + path, params, gap=gap)
        code = str(body.get("code")) if isinstance(body, dict) else "?"
        if code != "00000":
            raise RuntimeError(f"bitget {path}: code {code}: {str(body)[:200]}")
        return body.get("data")

    def _funding(self) -> dict[str, dict]:
        if self._fund and time.time() - self._fund[0] < self.SNAP_TTL_S:
            return self._fund[1]
        out = {}
        for p in PRODUCTS:
            for r in self._data(FUND, {"productType": p}) or []:
                if isinstance(r, dict) and r.get("symbol"):
                    out[r["symbol"]] = r
        if not out:
            raise RuntimeError("bitget current-fund-rate: пустой ответ")
        self._fund = (time.time(), out)
        return out

    def _classes(self, product: str) -> dict[str, dict]:
        try:
            return {r["symbol"]: r for r in self._data(INSTRUMENTS, {"category": product}) or []
                    if isinstance(r, dict) and r.get("symbol")}
        except Exception:  # noqa — без v3 класс неизвестен (None), присутствие это переживёт
            return {}

    def markets(self) -> dict[str, dict]:
        now = now_ms()
        fund = self._funding()
        out = {}
        for p in PRODUCTS:
            rows = self._data(CONTRACTS, {"productType": p}) or []
            v3 = self._classes(p)
            for s in rows:
                if not isinstance(s, dict) or not s.get("symbol"):
                    continue
                if (s.get("symbolType") or "perpetual") != "perpetual":
                    continue                                   # поставочный — без фандинга
                sym, coin = s["symbol"], s.get("baseCoin") or s["symbol"]
                st = s.get("symbolStatus") or "?"
                off, lim, launch = to_int(s.get("offTime")), to_int(s.get("limitOpenTime")), to_int(s.get("launchTime"))
                why = []
                if st != "normal":
                    why.append(f"статус {st}")
                if lim and lim > 0:
                    why.append(f"делистинг назначен: открытие позиций закрывается {_t(lim)}")
                if off and off > 0:
                    why.append(f"делистинг назначен: снятие {_t(off)}")
                if launch and launch > now:
                    why.append(f"запуск {_t(launch)}")
                c3 = v3.get(sym)
                cls = asset_class(coin, c3.get("symbolType"), c3.get("isRwa")) if c3 else None
                kind = f"{c3.get('symbolType')}{'+RWA' if str(c3.get('isRwa')).upper() == 'YES' else ''}" if c3 else "класс v3 нет"
                iv = to_int((fund.get(sym) or {}).get("fundingRateInterval")) or to_int(s.get("fundInterval"))
                out[sym] = market(coin, not why, cls, iv, None, f"{p} / {kind}" + ("" if not why else " / " + "; ".join(why)))
                self._product[sym] = p
        if not out:
            raise RuntimeError("bitget: ни одного бессрочного рынка")
        self._perps = out
        return out

    def rates(self) -> dict[str, dict]:
        keep = self._perps
        out = {}
        for sym, r in self._funding().items():
            if keep is not None and sym not in keep:
                continue                                       # тестовые / нелистингованные символы ставок
            iv = to_int(r.get("fundingRateInterval")) or ((keep or {}).get(sym) or {}).get("interval_h") or 8
            nxt = to_int(r.get("nextUpdate"))
            out[sym] = rate(dec(r.get("fundingRate")), iv, nxt if nxt and nxt > 0 else None, "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        start_ms, end_ms = int(start_ms), int(end_ms)
        p = self._product.get(symbol) or ("USDC-FUTURES" if symbol.endswith("PERP") else "USDT-FUTURES")
        got: dict[int, Decimal] = {}
        for page in range(1, MAX_PAGE_NO + 1):
            try:
                rows = self._data(HISTORY, {"symbol": symbol, "productType": p, "pageSize": PAGE, "pageNo": page},
                                  gap=self.HIST_GAP_S) or []
            except RuntimeError as e:
                if END_CODE in str(e) and page > 1:
                    break                                      # за последней страницей
                raise
            stamps = []
            for r in rows:
                ms = to_int(r.get("fundingTime")) if isinstance(r, dict) else None
                if not ms:
                    continue
                stamps.append(ms)
                v = dec(r.get("fundingRate"))
                if v is not None and start_ms <= ms <= end_ms:
                    got[ms] = v                                # расчёт между двумя страницами сдвигает их: дубль, не дыра
            if len(rows) < PAGE or not stamps or min(stamps) <= start_ms:
                break
        else:
            raise RuntimeError(f"bitget {symbol}: история не дошла до {start_ms} за {MAX_PAGE_NO} страниц")
        return sorted(got.items())
