"""Споты Gate, KuCoin, Bitget (владелец 12.09: «нужны споты gate, kucoin, bitget» — после Binance, в этом порядке).
У каждой свой публичный API, не клон Binance. Коллектор зовёт два метода (через venues.spot_*):
spot_instruments() раз в час и books() каждый тик — лучшие бид/аск всех пар одним вызовом.

Замерено 11–12.09 (Мак и ireland, все три отвечают, gzip), исследование + независимая перепроверка:
Gate    api.gateio.ws/api/v4 (api.gate.com не резолвится). /spot/currency_pairs — все 2236 пар одним ответом,
        /spot/tickers — все пары, ~115 КБ, ~1.9 с. Пара годится: quote USDT, trade_status «tradable» (buyable/sellable —
        только в одну сторону), type «normal» (не premarket). Отбрасываются: st_tag (список на делистинг, спреды 30 %+),
        delisting_time, ETF-токены с плечом (BTC3L, MSTR3S… — 420 пар; в имени «3xLong»). Цифровые суффиксы — другие
        монеты (MANA3 — не Decentraland, PEPE2 — не PEPE): база сравнивается ровно, без срезания цифр.
        В тикере нет размеров и времени: это кэш биржи, обновляется раз в 3–12 с; время снимка = время ответа.
        Лимит 200 запросов / 10 с на точку с IP; 429 → X-Gate-RateLimit-Reset-Timestamp. Поле fee («0.2») — не тариф.
KuCoin  api.kucoin.com. /api/v2/symbols (987 пар, вес 4), /api/v1/market/allTickers (все пары, вес 15, ~99 КБ).
        Пара годится: quote USDT, enableTrading, не аукцион (callauctionIsEnabled), tradingStartTime наступил, не ST
        (st — список риска/делистинга). Тикер монеты — из name, а не из symbol: BCHSV-USDT = BSV, GALAX-USDT = GALA,
        NIULAI-USDT = 牛来 — иначе пара не сойдётся с перпом. Комиссия VIP0 зависит от класса пары: 0.1 % × feeCategory
        (A/B/C = 0.1/0.2/0.3 %; поле takerFeeCoefficient в symbols всегда «1.00» — не класс). Пул IP 2000 веса / 30 с;
        429000 → gw-ratelimit-reset (мс); пул общий со spot и futures API KuCoin. Успех — code «200000». data.time в
        тикерах — время ответа, а не снимка; ответы из нескольких кэшей (цены могут «возвращаться» назад).
Bitget  api.bitget.com. /api/v2/spot/public/symbols (1761), /api/v2/spot/market/tickers (~122 КБ). Пара годится: quote
        USDT, status «online», offTime пуст (непустой = делистинг назначен, а статус ещё online). Пустая сторона книги —
        цена «0». Успех — code «00000»; 429 без Retry-After, FAQ грозит 5 минутами — пауза 60 с. takerFeeRate в symbols
        (0.002 у старых монет) — не тариф VIP0.
Токенизированные акции (12.09, сверка с чужим скринером — «покрываем всю линию»; правила — исследование со скептиком):
Bitget r… (rNVDA; pre… — pre-IPO, не берём), Gate — по классу валюты из /spot/currencies (xstocks …X, ondo-stocks …ON,
gstocks …G; ['stocks'] без подкласса — pre-IPO), KuCoin …X на рынке Stocks, Binance …B с тегом bStocks. Своя база у них
«~ТИКЕР» (с монетами не совпадёт никогда), тикер акции — в alt_base: сделку даёт только перп класса equity, цену за
токен проверяет «не тот актив» (у NFLXX/NFLXON в токене 10 акций, у CRWDX — 4, у TQQQX — 2: такие строки серые).
Все числа — строки. Страницы токена — одностраничные приложения без 404 (Gate молча показывает BTC): ссылка строится
только из символов текущего списка инструментов (config.URLS).
"""
from __future__ import annotations
import re, time, logging
import requests
from . import config
from .client import PermanentHTTPError, BannedError
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _book(bid, ask, bid_qty=None, ask_qty=None) -> dict | None:
    """Годная книга: обе стороны больше нуля и не перекрещены. Пустая сторона у бирж — «0», «» или null."""
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    return dict(bid=b, ask=a, bid_qty=_f(bid_qty) or 0.0, ask_qty=_f(ask_qty) or 0.0)


def _pow10(p) -> float | None:
    try:
        return 10.0 ** -int(p)
    except (TypeError, ValueError):
        return None


class SpotClient:
    """Общий транспорт: одна сессия, темп — дело коллектора (тик раз в 10 с, список раз в час). Отказ «слишком часто»
    ставит паузу площадки (banned_until) — до её конца запросов нет; 4xx — сразу ошибка; сеть/5xx — повтор."""
    name = ""
    base = ""

    def __init__(self, session: requests.Session | None = None):
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self.used_weight = 0
        self.budget = 0.0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0
        self.banned_until = 0.0

    # --- особенности биржи -------------------------------------------------------------------
    def _usage(self, r) -> None:
        """Израсходованная доля лимита — из заголовков ответа (у каждой биржи свои)."""

    def _throttled(self, r, body) -> float | None:
        """Пауза в секундах, если биржа ответила «слишком часто»; иначе None."""
        return 10.0 if r.status_code == 429 else None

    def _payload(self, body, path: str):
        return body

    # --- транспорт ---------------------------------------------------------------------------
    def budget_used(self) -> float:
        return self.budget

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self.banned_until and self.budget < soft

    def health(self) -> dict:
        return {"exchange": self.name, "used_weight": self.used_weight, "budget": round(self.budget, 3),
                "last_ok_ts": int(self.last_ok_ts), "n_429": self.n_429, "n_err": self.n_err,
                "banned_until": int(self.banned_until)}

    def get(self, path: str, params: dict | None = None, retries: int = config.TICK_RETRIES,
            timeout: float = config.TICK_HTTP_TIMEOUT):
        if time.time() < self.banned_until:
            raise BannedError(f"{self.name}: пауза после 429 до {time.strftime('%H:%M:%S', time.gmtime(self.banned_until))}")
        last = None
        for i in range(retries):
            try:
                r = self._s.get(self.base + path, params=params, timeout=timeout)
                self._usage(r)
                try:
                    body = r.json()
                except ValueError:
                    body = None
                pause = self._throttled(r, body)
                if pause is not None:
                    self.n_429 += 1
                    self.banned_until = time.time() + pause
                    log.warning("%s 429 на %s — пауза %.0f с", self.name, path, pause)
                    raise BannedError(f"{self.name}: 429 на {path}, пауза {pause:.0f} с")
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path}: {r.text[:200]}")
                r.raise_for_status()
                if body is None:
                    raise RuntimeError(f"{path}: ответ не JSON: {r.text[:200]}")
                data = self._payload(body, path)
                self.last_ok_ts = time.time()
                return data
            except (PermanentHTTPError, BannedError):
                raise
            except Exception as e:  # noqa: сеть, 5xx, чужой код ответа — повторяем
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    def _instrument(self, symbol: str, base_asset: str, tick=None, step=None, min_notional=None, onboard_ms=0,
                    stock_of: str | None = None, **extra) -> dict:
        """stock_of — тикер акции у токенизированной акции: своя база «~RNVDA» с монетами не совпадёт никогда,
        а тикер акции идёт в alt_base (пару даёт только перп на акцию — universe.build_sf)."""
        if stock_of:
            base, factor = "~" + base_asset.upper(), 1.0
        else:
            base, factor = norm_symbol_factor(base_asset)
        return dict(exchange=self.name, symbol=symbol, base_asset=base_asset, base=base, factor=factor, tick_size=tick,
                    step_size=step, min_notional=min_notional, onboard_ms=int(onboard_ms or 0),
                    alt_base=stock_of.upper() if stock_of else None, **extra)


class GateSpot(SpotClient):
    name = "gate_spot"
    base = "https://api.gateio.ws/api/v4"
    _ETF_BASE = re.compile(r".+\d+[LS]$")
    _ETF_NAME = re.compile(r"\d+x\s*(long|short)", re.I)

    def _usage(self, r):
        rem, lim = r.headers.get("x-gate-ratelimit-requests-remain"), r.headers.get("x-gate-ratelimit-limit")
        try:
            self.used_weight = int(lim) - int(rem)
            self.budget = self.used_weight / float(lim)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _throttled(self, r, body):
        if r.status_code != 429:
            return None
        try:
            return max(1.0, float(r.headers.get("x-gate-ratelimit-reset-timestamp")) - time.time() + 1.0)
        except (TypeError, ValueError):
            return 10.0

    def _payload(self, body, path):
        if not isinstance(body, list):
            raise RuntimeError(f"{path}: ожидался список, пришло {str(body)[:200]}")
        return body

    def spot_instruments(self) -> list[dict]:
        # класс валюты — только в /spot/currencies (category): xstocks / ondo-stocks / gstocks — токенизированные акции,
        # ['stocks'] без подкласса — pre-IPO (SPCX, OPENAI: единицы не равны акции), metals — PAXG/XAUT (монеты)
        cats = {c.get("currency"): set(c.get("category") or [])
                for c in self.get("/spot/currencies", retries=3, timeout=config.HTTP_TIMEOUT)}
        out = []
        for p in self.get("/spot/currency_pairs", retries=3, timeout=config.HTTP_TIMEOUT):
            base_asset = p.get("base") or ""
            if p.get("quote") != config.QUOTE or p.get("trade_status") != "tradable" or (p.get("type") or "normal") != "normal":
                continue
            if p.get("st_tag") or p.get("delisting_time"):
                continue                                  # в списке на делистинг
            if self._ETF_BASE.match(base_asset):
                continue                                  # токен с плечом: BTC3L, MSTR3S, PENGU3L (имя бывает китайским)
            stock = self._stock_underlying(base_asset, cats.get(base_asset, set()))
            if stock == "":
                continue                                  # pre-IPO
            out.append(self._instrument(p["id"], base_asset, _pow10(p.get("precision")), _pow10(p.get("amount_precision")),
                                        _f(p.get("min_quote_amount")), int(p.get("buy_start") or 0) * 1000,
                                        stock_of=stock))
        return out

    @staticmethod
    def _stock_underlying(base: str, cat: set) -> str | None:
        """Тикер акции по классу валюты Gate: CRCLX (xstocks), CRCLON (ondo-stocks), CRCLG (gstocks). По одному суффиксу
        нельзя: X/ON/G на конце у 100+ монет (TRX, RON, PAXG…). «» — pre-IPO, None — не акция."""
        b = base.upper()
        if "stocks" not in cat:
            return None                                   # ONDO — ['ondo-stocks'] без 'stocks': это монета
        if "xstocks" in cat and b.endswith("X"):
            return b[:-1]
        if "ondo-stocks" in cat and b.endswith("ON"):
            return b[:-2]
        if "gstocks" in cat and b.endswith("G"):
            return b[:-1]
        return ""

    def books(self) -> dict[str, dict]:
        out = {}
        for t in self.get("/spot/tickers"):
            sym = t.get("currency_pair") or ""
            if sym.endswith("_" + config.QUOTE):
                b = _book(t.get("highest_bid"), t.get("lowest_ask"))      # размеров в общем тикере нет
                if b:
                    out[sym] = b
        return out


class KucoinSpot(SpotClient):
    name = "kucoin_spot"
    base = "https://api.kucoin.com"

    def _usage(self, r):
        rem, lim = r.headers.get("gw-ratelimit-remaining"), r.headers.get("gw-ratelimit-limit")
        try:
            self.used_weight = int(lim) - int(rem)
            self.budget = self.used_weight / float(lim)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def _throttled(self, r, body):
        code = str(body.get("code")) if isinstance(body, dict) else ""
        if r.status_code != 429 and code != "429000":
            return None
        try:
            return max(1.0, float(r.headers.get("gw-ratelimit-reset")) / 1000.0)
        except (TypeError, ValueError):
            return 30.0                                   # перегрузка шлюза: 429000 без заголовков лимита

    def _payload(self, body, path):
        if not isinstance(body, dict) or str(body.get("code")) != "200000":
            raise RuntimeError(f"{path}: код {body.get('code') if isinstance(body, dict) else '?'}: {str(body)[:200]}")
        return body.get("data")

    def spot_instruments(self) -> list[dict]:
        now_ms = int(time.time() * 1000)
        out = []
        for s in self.get("/api/v2/symbols", retries=3, timeout=config.HTTP_TIMEOUT) or []:
            if s.get("quoteCurrency") != config.QUOTE or not s.get("enableTrading") or s.get("callauctionIsEnabled"):
                continue
            start = s.get("tradingStartTime")
            if start and int(start) > now_ms:
                continue                                  # листинг объявлен, торги ещё не начались
            if s.get("st"):
                continue                                  # ST: список риска / делистинга
            ticker = (s.get("name") or s["symbol"]).split("-")[0]      # BCHSV-USDT торгуется как BSV
            cat = int(s.get("feeCategory") or 1)
            stock = s.get("market") == "Stocks" and ticker.upper().endswith("X")   # xStocks: TSLAX, CRCLX
            out.append(self._instrument(s["symbol"], ticker, _f(s.get("priceIncrement")), _f(s.get("baseIncrement")),
                                        _f(s.get("minFunds") or s.get("quoteMinSize")), start or 0,
                                        stock_of=ticker.upper()[:-1] if stock else None,
                                        taker_fee=config.FEES_TAKER[self.name] * cat, fee_class=cat))
        return out

    def books(self) -> dict[str, dict]:
        # data.time — время ответа сервера, а не снимка (перепроверка 12.09: всегда x-in-time + 1–4 мс), и ответы идут из
        # нескольких кэшей разного возраста — время наблюдения остаётся временем получения
        data = self.get("/api/v1/market/allTickers") or {}
        out = {}
        for t in data.get("ticker") or []:
            sym = t.get("symbol") or ""
            if sym.endswith("-" + config.QUOTE):
                b = _book(t.get("buy"), t.get("sell"), t.get("bestBidSize"), t.get("bestAskSize"))
                if b:
                    out[sym] = b
        return out


class BitgetSpot(SpotClient):
    name = "bitget_spot"
    base = "https://api.bitget.com"
    _STOCK = re.compile(r"^(r|pre)[A-Z0-9]+$")          # rNVDA, rTSLA, preSPCX — токенизированные акции

    def _usage(self, r):
        rem = r.headers.get("x-mbx-used-remain-limit")   # осталось вызовов этой точки в текущей секунде (из 20)
        try:
            self.used_weight = 20 - int(rem)
            self.budget = max(0.0, self.used_weight / 20.0)
        except (TypeError, ValueError):
            pass

    def _throttled(self, r, body):
        code = str(body.get("code")) if isinstance(body, dict) else ""
        return 60.0 if r.status_code == 429 or code == "429" else None

    def _payload(self, body, path):
        if not isinstance(body, dict) or str(body.get("code")) != "00000":
            raise RuntimeError(f"{path}: код {body.get('code') if isinstance(body, dict) else '?'}: {str(body)[:200]}")
        return body.get("data")

    def spot_instruments(self) -> list[dict]:
        out = []
        for s in self.get("/api/v2/spot/public/symbols", retries=3, timeout=config.HTTP_TIMEOUT) or []:
            if s.get("quoteCoin") != config.QUOTE or s.get("status") != "online":
                continue
            if s.get("offTime") not in (None, "", "0", 0):
                continue                                  # делистинг назначен, статус ещё online
            coin = s.get("baseCoin") or ""
            # токенизированная акция rNVDA → NVDA (владелец 12.09: «покрываем всю линию», у чужого скринера CRCL 16 спредов);
            # pre-IPO preSPCX / preOPAI — единицы не равны акции, в сделки не идут. Регистр важен: RAY, RENDER — монеты.
            m = self._STOCK.match(coin)
            if m and m.group(1) == "pre":
                continue
            out.append(self._instrument(s["symbol"], coin, _pow10(s.get("pricePrecision")), _pow10(s.get("quantityPrecision")),
                                        _f(s.get("minTradeUSDT")), stock_of=coin[1:] if m else None))
        return out

    def books(self) -> dict[str, dict]:
        out = {}
        for t in self.get("/api/v2/spot/market/tickers") or []:
            sym = t.get("symbol") or ""
            if sym.endswith(config.QUOTE):
                b = _book(t.get("bidPr"), t.get("askPr"), t.get("bidSz"), t.get("askSz"))
                if b:
                    out[sym] = b
        return out


SPOT_CLIENTS = {"gate_spot": GateSpot, "kucoin_spot": KucoinSpot, "bitget_spot": BitgetSpot}
