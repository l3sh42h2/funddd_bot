"""Подделки площадок: заранее заданные ответы + управляемые отказы.

FakeExchange — Binance-подобная (Binance, Aster, спот Binance): ответы по путям, заголовок веса, история
постранично по startTime/limit. FakeHL — Hyperliquid со своими методами (perp_instruments/premium/books/
history_since/recent_history), как настоящий клиент hyperliquid.Hyperliquid, включая HIP-3 рынки «dex:МОНЕТА».
"""
from __future__ import annotations
import time
from funding_bot.client import BinanceLike, BudgetExceeded, BannedError, PermanentHTTPError
from funding_bot.hyperliquid import base_of

H = 3600_000


def perp(symbol: str, base: str | None = None, quote="USDT", status="TRADING", ct="PERPETUAL", notional="5", onboard=0,
         ut="COIN", sub=None):
    return {"symbol": symbol, "baseAsset": base or symbol[:-len(quote)], "quoteAsset": quote, "status": status,
            "contractType": ct, "onboardDate": onboard, "underlyingType": ut, "underlyingSubType": sub or [],
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"}, {"filterType": "LOT_SIZE", "stepSize": "1"},
                        {"filterType": "MIN_NOTIONAL", "notional": notional}]}


def spot(symbol: str, base: str | None = None, quote="USDT"):
    return {"symbol": symbol, "baseAsset": base or symbol[:-len(quote)], "quoteAsset": quote, "status": "TRADING",
            "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"}, {"filterType": "LOT_SIZE", "stepSize": "1"},
                        {"filterType": "NOTIONAL", "minNotional": "5"}]}


class FakeExchange(BinanceLike):
    def __init__(self, name: str, kind="perp"):
        super().__init__(name, f"https://{name}.fake", 2400 if kind == "perp" else 6000)
        self.kind = kind
        self.symbols: list[dict] = []
        self.funding_info: dict[str, dict] = {}        # symbol -> {interval_h, cap, floor}
        self.premium: dict[str, dict] = {}             # symbol -> {rate, mark, index?, next_ms}
        self.books: dict[str, dict] = {}               # symbol -> {bid, ask}
        self.history: dict[str, list[tuple[int, float]]] = {}   # symbol -> [(ms, rate)]
        self.calls: list[tuple[str, dict | None]] = []
        self.fake_weight: int | None = None
        self.fail_paths: set[str] = set()
        self.bstock_symbols: set[str] = set()         # bStocks спота Binance (в жизни — тег в списке продуктов)
        self.index: dict[str, list[dict]] = {}        # состав индекса перпа (constituents / indexreferences); нет — 400
        self.coins_blob: dict | None = None           # список монет спота (identity_src.coin_blob); None — из symbols

    def coin_blob(self):
        if "coins" in self.fail_paths:
            raise RuntimeError("список монет недоступен")
        if self.coins_blob is not None:
            return self.coins_blob
        return {"coins": {}, "markets": {s["symbol"]: [s["baseAsset"], True] for s in self.symbols}}

    def bstocks(self):
        if "bstocks" in self.fail_paths:
            raise RuntimeError("список продуктов недоступен")
        return set(self.bstock_symbols)

    # прямой вызов без HTTP: get() бюджет и бан проверяет сам
    def get(self, path, params=None, retries=3, timeout=15, soft=None):
        if time.time() < self.banned_until:
            raise BannedError(f"{self.name}: бан")
        if self.fake_weight is not None:
            self.used_weight, self.used_weight_ts = self.fake_weight, time.time()
        from funding_bot import config
        soft = config.WEIGHT_SOFT_LIMIT if soft is None else soft
        if self.budget_used() >= soft:
            raise BudgetExceeded(f"{self.name}: бюджет {self.budget_used():.0%}")
        self.calls.append((path, dict(params or {})))
        if path in self.fail_paths:
            raise RuntimeError(f"{path} недоступен")
        self.last_ok_ts = time.time()
        p = params or {}
        if path in ("/fapi/v1/exchangeInfo", "/api/v3/exchangeInfo"):
            return {"symbols": self.symbols, "rateLimits": []}
        if path == "/fapi/v1/fundingInfo":
            return [{"symbol": s, "fundingIntervalHours": v["interval_h"], "fundingFeeCap": v.get("cap", 0.02),
                     "fundingFeeFloor": v.get("floor", -0.02)} for s, v in self.funding_info.items()]
        if path == "/fapi/v1/premiumIndex":
            return [{"symbol": s, "markPrice": str(v.get("mark", 1.0)), "indexPrice": str(v.get("index", v.get("mark", 1.0))),
                     "lastFundingRate": str(v["rate"]), "nextFundingTime": v.get("next_ms", 0), "time": int(time.time() * 1000)}
                    for s, v in self.premium.items()]
        if path in ("/fapi/v1/constituents", "/fapi/v3/indexreferences"):
            legs = self.index.get(p.get("symbol"))
            if legs is None:
                raise PermanentHTTPError(f"400 Invalid symbol {p.get('symbol')}")
            return {"symbol": p["symbol"], ("constituents" if "constituents" in path else "references"): legs}
        if path in ("/fapi/v1/ticker/bookTicker", "/api/v3/ticker/bookTicker"):
            return [{"symbol": s, "bidPrice": str(v["bid"]), "askPrice": str(v["ask"]), "bidQty": "1", "askQty": "1"}
                    for s, v in self.books.items()]
        if path == "/fapi/v1/fundingRate":
            limit = int(p.get("limit", 100)); st = p.get("startTime"); en = p.get("endTime")
            if "symbol" in p:
                rows = [(p["symbol"], ms, r) for ms, r in self.history.get(p["symbol"], [])]
            else:
                rows = [(s, ms, r) for s, h in self.history.items() for ms, r in h]
            rows.sort(key=lambda x: x[1])
            if st is not None:
                rows = [x for x in rows if x[1] >= st]
            if en is not None:
                rows = [x for x in rows if x[1] <= en]
            if "symbol" not in p and st is None:
                rows = rows[-limit:]          # без символа биржа отдаёт ПОСЛЕДНИЕ limit
            else:
                rows = rows[:limit]
            return [{"symbol": s, "fundingTime": ms, "fundingRate": str(r), "markPrice": "1"} for s, ms, r in rows]
        raise RuntimeError(f"нет подделки для {path}")


class FakeHL:
    """Hyperliquid: основной dex и HIP-3 («xyz:NATGAS»), k-монеты, делистнутые, часовой фандинг,
    история без пакетного вызова."""
    name = "hyperliquid"

    def __init__(self):
        self.coins: dict[str, dict] = {}                 # name -> {rate, mark, index?, bid, ask, delisted?}
        self.history: dict[str, list[tuple[int, float]]] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.fail: set[str] = set()                      # "meta" — контексты/вселенная, "history" — история
        self.delay = 0.0                                 # задержка вызова истории (проверка фоновых обходов)
        self.cls: dict[str, str] = {"xyz:NATGAS": "commodity"}   # как perpCategories: класс рынка HIP-3
        self.ann: dict[str, str] = {}                    # описание рынка HIP-3 (perpAnnotation)
        self.used_weight = 0; self.last_ok_ts = 0.0; self.n_429 = 0; self.n_err = 0; self.banned_until = 0.0

    def budget_used(self): return 0.0

    def annotations(self, coins):
        self._call("annotations")
        return {c: {"desc": self.ann.get(c)} for c in coins}

    def health(self):
        return {"exchange": self.name, "used_weight": 0, "budget": 0.0, "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": 0}

    def _call(self, what: str, arg: str | None = None):
        self.calls.append((what, arg))
        if what in self.fail:
            raise RuntimeError(f"hyperliquid {what} недоступен")
        self.last_ok_ts = time.time()

    def _live(self):
        return {n: c for n, c in self.coins.items() if not c.get("delisted")}

    def perp_instruments(self):
        self._call("meta")
        out = []
        for n in self._live():
            base, factor = base_of(n)
            cls = self.cls.get(n) or ("equity" if ":" in n else "crypto")
            out.append(dict(exchange=self.name, symbol=n, base_asset=n, base=base, factor=factor, tick_size=None,
                            step_size=1.0, min_notional=10.0, onboard_ms=0, interval_h=1, cap=None, floor=None,
                            quote="USDC", contract=n.split(":", 1)[0] if ":" in n else "main", cls=cls))
        return out

    def premium(self):
        self._call("meta")
        now_ms = int(time.time() * 1000); nxt = (now_ms // H + 1) * H
        return {n: dict(rate=c["rate"], mark=c["mark"], index=c.get("index", c["mark"]), next_ms=nxt, ts_ms=now_ms)
                for n, c in self._live().items()}

    def books(self):
        return {n: dict(bid=c["bid"], ask=c["ask"], bid_qty=0.0, ask_qty=0.0) for n, c in self._live().items()}

    def history_since(self, symbol, start_ms, end_ms=None):
        if self.delay:
            time.sleep(self.delay)
        self._call("history", symbol)
        return [dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=r, mark=None)
                for ms, r in self.history.get(symbol, []) if ms >= start_ms and (end_ms is None or ms <= end_ms)]

    def recent_history(self):
        return []


class FakeSpot:
    """Спот-площадка со своим API (как spot.GateSpot/KucoinSpot/BitgetSpot): spot_instruments() и books()."""

    def __init__(self, name: str, sep: str = "_"):
        self.name, self.sep = name, sep
        self.pairs: dict[str, dict] = {}                 # base_asset -> {bid, ask}
        self.fail: set[str] = set()                      # "instruments", "books", "coins"
        self.calls: list[str] = []
        self.coins_blob: dict | None = None              # список монет (identity_src.coin_blob); None — из pairs
        self.used_weight = 0; self.last_ok_ts = 0.0; self.n_429 = 0; self.n_err = 0; self.banned_until = 0.0

    def budget_used(self): return 0.0

    def health(self):
        return {"exchange": self.name, "used_weight": 0, "budget": 0.0, "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": 0}

    def _call(self, what):
        self.calls.append(what)
        if what in self.fail:
            raise RuntimeError(f"{self.name} {what} недоступен")
        self.last_ok_ts = time.time()

    def spot_instruments(self):
        from funding_bot.symbols import norm_symbol_factor
        self._call("instruments")
        out = []
        for a in self.pairs:
            base, factor = norm_symbol_factor(a)
            out.append(dict(exchange=self.name, symbol=f"{a}{self.sep}USDT", base_asset=a, base=base, factor=factor,
                            tick_size=None, step_size=None, min_notional=None, onboard_ms=0))
        return out

    def books(self):
        self._call("books")
        return {f"{a}{self.sep}USDT": dict(bid=v["bid"], ask=v["ask"], bid_qty=1.0, ask_qty=1.0) for a, v in self.pairs.items()}

    def coin_blob(self):
        self._call("coins")
        if self.coins_blob is not None:
            return self.coins_blob
        return {"coins": {}, "markets": {f"{a}{self.sep}USDT": [a, True] for a in self.pairs}}


class FakeNative:
    """Перп-площадка со своим API (12.09: как kucoin_fut / bitget_fut / gate_fut / lighter): рынки заданы словарём,
    ссылка на страницу рынка — в самом инструменте («url»; None — страницы нет, как у Lighter на Robinhood Chain)."""

    def __init__(self, name: str, iv_h: int = 8):
        self.name, self.iv_h = name, iv_h
        self.markets: dict[str, dict] = {}               # symbol -> {base, factor?, cls?, rate, mark, bid, ask, url?}
        self.history: dict[str, list[tuple[int, float]]] = {}
        self.calls: list[tuple[str, str | None]] = []
        self.used_weight = 0; self.last_ok_ts = 0.0; self.n_429 = 0; self.n_err = 0; self.banned_until = 0.0

    def budget_used(self): return 0.0

    def health(self):
        return {"exchange": self.name, "used_weight": 0, "budget": 0.0, "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": 0}

    def perp_instruments(self):
        self.calls.append(("instruments", None)); self.last_ok_ts = time.time()
        return [dict(exchange=self.name, symbol=s, base_asset=m.get("base_asset", m["base"]), base=m["base"],
                     factor=m.get("factor", 1.0), tick_size=None, step_size=None, min_notional=None, onboard_ms=0,
                     interval_h=m.get("interval_h", self.iv_h), cap=None, floor=None, quote="USDT", contract="PERPETUAL",
                     cls=m.get("cls", "crypto"), url=m.get("url")) for s, m in self.markets.items()]

    def premium(self):
        now_ms = int(time.time() * 1000)
        out = {}
        for s, m in self.markets.items():
            step = m.get("interval_h", self.iv_h) * H
            out[s] = dict(rate=m["rate"], mark=m["mark"], index=m["mark"], next_ms=(now_ms // step + 1) * step, ts_ms=now_ms)
        return out

    def books(self):
        return {s: dict(bid=m["bid"], ask=m["ask"], bid_qty=1.0, ask_qty=1.0) for s, m in self.markets.items()}

    def history_since(self, symbol, start_ms, end_ms=None):
        self.calls.append(("history", symbol))
        return [dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=r, mark=None)
                for ms, r in self.history.get(symbol, []) if ms >= start_ms and (end_ms is None or ms <= end_ms)]

    def recent_history(self):
        return []


def grid(now_ms: int, iv_h: int, rate: float, days: float = 3):
    """Ровная сетка расчётов за `days` суток, последний — на ближайшей границе интервала не позже now."""
    step = iv_h * H; last = (now_ms // step) * step
    return [(last - k * step, rate) for k in range(int(days * 24 / iv_h))][::-1]


def make_world(now_ms: int | None = None):
    """Три площадки + спот.

    Aster/Binance: ABC (обе 8ч), XYZ (Aster 1ч, Binance 4ч), MEME (не тот актив), 1000PEPE, ONLYA (только Aster),
    AIUSDT на Aster — другой токен, в 13 раз дороже спота AI (ловушка 10.09); BONLY — перп только на Binance.
    11.09: NATGAS (Aster USDT, Binance TradFi, Hyperliquid HIP-3 xyz), GPRO (Aster USD1, Binance TradFi),
    ABCUSDC — дубль по квоте, ETHBTC — не долларовая квота.
    Hyperliquid: ABC, kPEPE (×1000), BONLY, xyz:NATGAS (HIP-3), DEAD (делистнута).
    12.09: второй спот — Gate (свой API): ABC и XYZ (у XYZ спота Binance нет — сделки только через Gate).
    """
    now_ms = now_ms or int(time.time() * 1000)
    a, b, s = FakeExchange("aster"), FakeExchange("binance"), FakeExchange("binance_spot", kind="spot")
    h = FakeHL()
    g = FakeSpot("gate_spot")
    g.pairs = {"ABC": dict(bid=9.98, ask=10.0), "XYZ": dict(bid=1.99, ask=2.0)}
    # 11.09: TradFi-перп Binance (NATGAS, GPRO), USD1-перп Aster (GPROUSD1 — единственный GPRO на Aster),
    # USDC-дубль Binance (ABCUSDC — у ABC есть USDT-перп, берётся он), HIP-3 рынок Hyperliquid (xyz:NATGAS).
    # классы как у бирж (12.09): Aster — underlyingSubType (Commodities, STOCK), Binance — TRADIFI + underlyingType
    a.symbols = [perp("ABCUSDT"), perp("XYZUSDT"), perp("MEMEUSDT"), perp("ONLYAUSDT"), perp("1000PEPEUSDT"),
                 perp("AIUSDT"), perp("NATGASUSDT", sub=["Commodities"]), perp("GPROUSD1", base="GPRO", quote="USD1", sub=["STOCK"]),
                 perp("DEADUSDT", status="SETTLING"), perp("USDQUOTE", base="USDQ", quote="USD")]
    b.symbols = [perp("ABCUSDT"), perp("XYZUSDT"), perp("MEMEUSDT"), perp("1000PEPEUSDT"), perp("BONLYUSDT"),
                 perp("NATGASUSDT", ct="TRADIFI_PERPETUAL", ut="COMMODITY", sub=["TradFi"]),
                 perp("GPROUSDT", ct="TRADIFI_PERPETUAL", ut="EQUITY", sub=["TradFi"]),
                 perp("ABCUSDC", base="ABC", quote="USDC"), perp("ETHBTC", base="ETH", quote="BTC"),
                 perp("ABCUSDT_260101", base="ABC", ct="CURRENT_QUARTER")]
    s.symbols = [spot("ABCUSDT"), spot("MEMEUSDT"), spot("PEPEUSDT"), spot("BONLYUSDT"), spot("AIUSDT")]
    a.funding_info = {"ABCUSDT": {"interval_h": 8}, "XYZUSDT": {"interval_h": 1}, "MEMEUSDT": {"interval_h": 4},
                      "1000PEPEUSDT": {"interval_h": 4}, "AIUSDT": {"interval_h": 8}, "NATGASUSDT": {"interval_h": 4},
                      "GPROUSD1": {"interval_h": 8}}
    b.funding_info = {"ABCUSDT": {"interval_h": 8}, "XYZUSDT": {"interval_h": 4}, "MEMEUSDT": {"interval_h": 4},
                      "1000PEPEUSDT": {"interval_h": 4}, "BONLYUSDT": {"interval_h": 8}, "NATGASUSDT": {"interval_h": 4},
                      "GPROUSDT": {"interval_h": 8}, "ABCUSDC": {"interval_h": 8}}
    a.premium = {"ABCUSDT": {"rate": 0.0002, "mark": 10.0}, "XYZUSDT": {"rate": -0.0001, "mark": 2.0},
                 "MEMEUSDT": {"rate": 0.0005, "mark": 120.0}, "1000PEPEUSDT": {"rate": 0.0001, "mark": 0.01},
                 "AIUSDT": {"rate": 0.0003, "mark": 13.0}, "NATGASUSDT": {"rate": 0.0036, "mark": 2.93},
                 "GPROUSD1": {"rate": 0.0017, "mark": 1.40}}
    b.premium = {"ABCUSDT": {"rate": 0.0001, "mark": 10.0}, "XYZUSDT": {"rate": 0.0004, "mark": 2.0},
                 "MEMEUSDT": {"rate": 0.0001, "mark": 1.0}, "1000PEPEUSDT": {"rate": 0.0001, "mark": 0.01},
                 "BONLYUSDT": {"rate": 0.0006, "mark": 5.0}, "NATGASUSDT": {"rate": 0.0038, "mark": 2.93},
                 "GPROUSDT": {"rate": 0.0015, "mark": 1.40}, "ABCUSDC": {"rate": 0.0001, "mark": 10.0}}
    a.books = {"ABCUSDT": {"bid": 9.99, "ask": 10.01}, "XYZUSDT": {"bid": 1.99, "ask": 2.01},
               "MEMEUSDT": {"bid": 119.0, "ask": 121.0}, "1000PEPEUSDT": {"bid": 0.0099, "ask": 0.0101},
               "AIUSDT": {"bid": 12.9, "ask": 13.1}, "NATGASUSDT": {"bid": 2.92, "ask": 2.94},
               "GPROUSD1": {"bid": 1.39, "ask": 1.41}}
    b.books = {"ABCUSDT": {"bid": 9.995, "ask": 10.005}, "XYZUSDT": {"bid": 1.995, "ask": 2.005},
               "MEMEUSDT": {"bid": 0.999, "ask": 1.001}, "1000PEPEUSDT": {"bid": 0.0099, "ask": 0.0101},
               "BONLYUSDT": {"bid": 4.99, "ask": 5.01}, "NATGASUSDT": {"bid": 2.92, "ask": 2.94},
               "GPROUSDT": {"bid": 1.39, "ask": 1.41}, "ABCUSDC": {"bid": 9.99, "ask": 10.01}}
    s.books = {"ABCUSDT": {"bid": 9.99, "ask": 10.0}, "MEMEUSDT": {"bid": 1.0, "ask": 1.0},
               "PEPEUSDT": {"bid": 0.0000099, "ask": 0.0000101}, "BONLYUSDT": {"bid": 4.98, "ask": 5.0},
               "AIUSDT": {"bid": 0.99, "ask": 1.01}}
    a.history = {"ABCUSDT": grid(now_ms, 8, 0.0002), "XYZUSDT": grid(now_ms, 1, -0.0001), "MEMEUSDT": grid(now_ms, 4, 0.0005),
                 "1000PEPEUSDT": grid(now_ms, 4, 0.0001), "AIUSDT": grid(now_ms, 8, 0.0003),
                 "NATGASUSDT": grid(now_ms, 4, 0.0036), "GPROUSD1": grid(now_ms, 8, 0.0017)}
    b.history = {"ABCUSDT": grid(now_ms, 8, 0.0001), "XYZUSDT": grid(now_ms, 4, 0.0004), "MEMEUSDT": grid(now_ms, 4, 0.0001),
                 "1000PEPEUSDT": grid(now_ms, 4, 0.0001), "BONLYUSDT": grid(now_ms, 8, 0.0006),
                 "NATGASUSDT": grid(now_ms, 4, 0.0038), "GPROUSDT": grid(now_ms, 8, 0.0015), "ABCUSDC": grid(now_ms, 8, 0.0001)}
    h.coins = {"ABC": dict(rate=0.00003, mark=10.0, bid=9.99, ask=10.01),
               "kPEPE": dict(rate=-0.00001, mark=0.01, bid=0.0099, ask=0.0101),
               "BONLY": dict(rate=0.00005, mark=5.0, bid=4.99, ask=5.01),
               "xyz:NATGAS": dict(rate=0.00062, mark=2.93, bid=2.92, ask=2.94),
               "DEAD": dict(rate=0.0, mark=1.0, bid=1.0, ask=1.0, delisted=True)}
    h.history = {"ABC": grid(now_ms, 1, 0.00003), "kPEPE": grid(now_ms, 1, -0.00001), "BONLY": grid(now_ms, 1, 0.00005),
                 "xyz:NATGAS": grid(now_ms, 1, 0.00062)}
    # 12.09: «тот ли актив» — по составу индекса и контрактам (identity.py), не по цене. ABC — спот Binance и Gate в
    # индексах обоих перпов; 1000PEPE — спот PEPE ×1000; MEME и AI на Aster — токены Alpha (A Meme Coin, Artificial Inu)
    # с другим контрактом, чем спот Binance (Memecoin, Sleepless AI) — ловушка 10.09
    from funding_bot.identity import coin_record
    rec = lambda name, addr: coin_record(name, [("ETH", addr, True, True)], "")
    leg = lambda ex, sym, wt=0.5: {"exchange": ex, "symbol": sym, "weight": str(wt)}
    a.index = {"ABCUSDT": [leg("binance", "ABCUSDT"), leg("gateio", "ABC_USDT")],
               "MEMEUSDT": [leg("binance_alpha", "MEMEUSDT", 1)], "AIUSDT": [leg("binance_alpha", "AIUSDT", 1)]}
    b.index = {"ABCUSDT": [leg("binance", "ABCUSDT"), leg("gateio", "ABC_USDT")], "MEMEUSDT": [leg("binance", "MEMEUSDT", 1)],
               "1000PEPEUSDT": [leg("binance", "PEPEUSDT*1000", 1)]}
    s.coins_blob = {"coins": {"ABC": rec("Abc Coin", "0x" + "ab" * 20), "MEME": rec("Memecoin", "0x" + "b1" * 20),
                              "AI": rec("Sleepless AI", "0x" + "bd" * 20), "PEPE": rec("Pepe", "0x" + "69" * 20),
                              "BONLY": rec("Bonly", "0x" + "b0" * 20)},
                    "markets": {x["symbol"]: [x["baseAsset"], True] for x in s.symbols},
                    "alpha": [{"s": "MEME", "n": "A Meme Coin", "a": "0x" + "38" * 20, "c": "4663", "id": "ALPHA_1"},
                              {"s": "AI", "n": "Artificial Inu", "a": "0x" + "2e" * 20, "c": "4663", "id": "ALPHA_2"}]}
    g.coins_blob = {"coins": {"ABC": rec("Abc Coin", "0x" + "ab" * 20), "XYZ": rec("Xyz", "0x" + "99" * 20)},
                    "markets": {"ABC_USDT": ["ABC", True], "XYZ_USDT": ["XYZ", True]}}
    return {"aster": a, "binance": b, "binance_spot": s, "hyperliquid": h, "gate_spot": g}
