"""KuCoin futures — perp venue «kucoin» (owner 12.09: «добавить фьючи kucoin bitget и gate, lighter»; owner's order after
aster, binance, hyperliquid: kucoin, bitget, gate, lighter). Native perp client of venues.py: perp_instruments / premium /
books / history_since / recent_history. Public endpoints only, no keys, no orders.

Measured 12.09.2026 from the Mac. Tags: [L] verified live, [D] verified in docs (docs-new pages / ccxt 4.5.78), [A] assumption.
Host https://api-futures.kucoin.com; every answer is {"code": "200000", "data": …}, gzip [L].
/api/v1/contracts/active (weight 3 [D]) — all 687 contracts in one answer (1.4 MB plain, 136 KB gzip, ~0.45 s): universe,
  fundingFeeRate = the running estimate for the settlement at nextFundingRateDateTime (fraction per interval, moves about
  once a minute), markPrice, indexPrice, interval, last settled rate lastTimeFundingRate [L]. predictedFundingFeeRate is
  null on every contract and nextFundingRateTime is ms REMAINING — neither is used [L]. It serves perp_instruments
  (hourly), premium (every tick) and recent_history.
/api/v1/allTickers (weight 5) — best bid/ask of every contract. Sizes are in CONTRACTS (× multiplier = base units). `ts` is
  nanoseconds of the last TRADE (20 h old on an illiquid book that was current) — obs is the receipt time [L].
/api/v1/contract/funding-rates {symbol, from, to} (weight 5) — both bounds are required (else HTTP 400 / 400000); returns
  the NEWEST ≤ 100 settlements in [from, to] (inclusive), newest first, so pages go backwards [L]. 30 days = 1 call at 8 h,
  2 calls at 4 h. An unknown symbol answers HTTP 200 with code 404000 [L].
Limits: 2000 weight per 30 s per IP, SHARED with spot api.kucoin.com (kucoin_spot) [L]; headers gw-ratelimit-limit /
  -remaining / -reset (ms to the window end) [L]. HTTP 429 or body 429000 → pause until the reset [D]. The budget is read
  from the headers, so the tick client and the collector's background copies see the same IP-wide counter; a reading
  expires with its window. A tick costs 8 weight, history ≤ 4 calls/s = 20 weight/s (30 % of the pool).
Universe [L]: status Open, type FFWCSX (FFICSX = dated XBTMU26), not isInverse (coin-margined *USDM), quote in PERP_QUOTES,
  then exchanges.keep_best_quote (drops the USDC twins ETHUSDCM, SOLUSDCM, SUIUSDCM, XBTUSDCM, XRPUSDCM). XBT → BTC is the
  only rename. factor comes from the NAME (1000BONK, 10000CAT, 1MBABYDOGE: markPrice is per ONE unit of the named base,
  10000CAT marks 10034× spot CAT); multiplier is the contract size, not the factor.
  Chinese tickers: baseCurrency is pinyin (NIULAI) while Binance/Aster perps, Binance spot and KuCoin spot (name) all use
  牛来 — base = displayBaseCurrency when it is not ASCII (HAJIMI, LONGXIA, NIULAI, WOTAMALAILE) [L].
Intervals [L]: 4 h ×432, 8 h ×245 on 12.09; currentFundingRateGranularity is null on 15 contracts → fundingRateGranularity
  (history confirms 8 h there). Intervals change on the fly (TENCENT 8 h → 4 h at 09-11 12:00; 37 contracts in 30 days) and
  TRUSTUSDTM settles on a 4 h grid offset by 3 h — so kucoin is NOT a FIXED_INTERVAL_H venue and next_ms comes from the
  venue. Stock perps settle 24/7.
Classes [L]: marketStage PRE_MARKET → preipo (ANTHROPIC, OPENAI, BP); PAXG/XAUT → crypto (repo rule; KuCoin says METAL);
  METAL / COMMODITY → commodity (CL, BZ, NATGAS, COPPER, XAG, XPD, XPT); STOCK (US/HK/KR/JP shares and ETFs) → equity;
  CRYPTO → crypto; any other assetClass → «rwa», a class that pairs with nothing [A]. HKD-priced stock perps keep their own
  base (TENCENTHKD 428.78 HKD vs TENCENT 54.71 USD) — never strip «HKD».
Crypto canon: a KuCoin perp under a ticker that config.SPOT_ALIASES maps for kucoin_spot (NEIROCTO → NEIRO, RAY → RAYSOL,
  LUNA → LUNA2, DODO → DODOX, REDSTONE → RED, DATA → DATAIP, PROS → PHAROS) is taken as the same KuCoin asset as the spot
  [A: one exchange, one ticker]; their marks agree with the Binance perp within 0.2 % [L — a hint, not evidence]. Skipped
  when KuCoin lists the canonical ticker as well.
Identity: /api/v1/index/query names EXCHANGES only, never markets (same list as sourceExchanges) [L], and the collector
  collects index legs of native clients only for «dex:COIN» symbols — KuCoin rows stay «no_index» until the collector side
  changes. index_sources() keeps the exchange names (not evidence by itself).
Fees [L]: takerFeeRate 0.0006 / makerFeeRate 0.0002 on all 687 contracts — config.FEES_TAKER.
"""
from __future__ import annotations
import logging, threading, time
from . import config, exchanges
from .client import BudgetExceeded, PermanentHTTPError
from .spot import SpotClient, _book
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

HOUR_MS = 3600_000
ACTIVE = "/api/v1/contracts/active"
TICKERS = "/api/v1/allTickers"
HISTORY = "/api/v1/contract/funding-rates"
PERPETUAL = "FFWCSX"                 # FFICSX = dated futures [L]
HISTORY_PAGE = 100                   # rows per history answer — the newest in the window [L]
HISTORY_MAX_PAGES = 50               # 50 × 100 rows ≈ 200 days of 1 h; a longer request fails loudly, never half-filled
HISTORY_GAP_S = 0.25                 # between history calls (config.FUNDING_HISTORY_MIN_GAP_S overrides by venue name)
FRESH_SETTLE_S = 120                 # recent_history skips a settlement younger than this (stale lastTimeFundingRate risk)
POOL_WINDOW_S = 30.0                 # the weight pool window [L]
RENAME = {"XBT": "BTC"}
GOLD_TOKENS = ("PAXG", "XAUT")
_CLASS = {"CRYPTO": "crypto", "STOCK": "equity", "METAL": "commodity", "COMMODITY": "commodity"}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def eligible(c: dict) -> bool:
    """A linear perpetual that trades now, quoted in a dollar (PERP_QUOTES)."""
    return (c.get("status") == "Open" and c.get("type") == PERPETUAL and not c.get("isInverse")
            and c.get("quoteCurrency") in config.PERP_QUOTES)


def interval_ms(c: dict) -> int | None:
    """Current funding interval; currentFundingRateGranularity is null on some old contracts (DOGE, LINK, …) [L]."""
    for k in ("currentFundingRateGranularity", "fundingRateGranularity"):
        v = _int(c.get(k))
        if v > 0:
            return v
    return None


def asset_class(c: dict) -> str:
    if str(c.get("marketStage") or "").upper() == "PRE_MARKET":
        return "preipo"
    if str(c.get("baseCurrency") or "").upper() in GOLD_TOKENS:
        return "crypto"                                   # gold tokens are coins (repo rule)
    ac = str(c.get("assetClass") or "").upper()
    if not ac:
        return "equity" if str(c.get("marketType") or "").upper() == "NASDAQ" else "crypto"
    return _CLASS.get(ac, "rwa")                          # unknown class: pairs with nothing, never a wrong pair


def ticker(c: dict) -> str:
    """The venue's ticker of the base: XBT → BTC; the Chinese display ticker when baseCurrency is its pinyin."""
    b = str(c.get("baseCurrency") or "")
    d = str(c.get("displayBaseCurrency") or "")
    if d and d != b and not d.isascii():
        return d
    return RENAME.get(b.upper(), b)


def crypto_aliases() -> dict[str, str]:
    """KuCoin ticker → canonical base, from the aliases verified for kucoin_spot (None = every spot venue)."""
    out: dict[str, str] = {}
    for canon, lst in (config.SPOT_ALIASES.get("crypto") or {}).items():
        for alias, vs in lst:
            if vs is None or "kucoin_spot" in vs:
                out.setdefault(alias.upper(), canon)
    return out


class KucoinFutures(SpotClient):
    """Transport of SpotClient (one session, 429 → venue pause, 4xx → PermanentHTTPError, network/5xx → retry). Not a
    subclass of spot.KucoinSpot: that one has spot_instruments, and venues.spot_native() would treat this as a spot."""
    name = "kucoin"
    base = "https://api-futures.kucoin.com"

    def __init__(self, session=None, history_gap_s: float | None = None):
        super().__init__(session)
        self.history_gap_s = (config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, HISTORY_GAP_S)
                              if history_gap_s is None else history_gap_s)
        self._lock = threading.Lock()
        self._last_hist = 0.0
        self._mult: dict[str, float] = {}
        self._sources: dict[str, list[str]] = {}
        self.budget_ts = 0.0
        self.budget_reset_s = POOL_WINDOW_S

    # --- venue specifics of the transport ------------------------------------------------------------------------------
    def _usage(self, r):
        h = r.headers
        try:
            lim, rem = float(h.get("gw-ratelimit-limit")), float(h.get("gw-ratelimit-remaining"))
        except (TypeError, ValueError):
            return
        if lim <= 0:
            return
        self.used_weight = int(lim - rem)
        self.budget = max(0.0, (lim - rem) / lim)
        self.budget_ts = time.time()
        try:
            self.budget_reset_s = min(POOL_WINDOW_S, max(0.0, float(h.get("gw-ratelimit-reset")) / 1000.0))
        except (TypeError, ValueError):
            self.budget_reset_s = POOL_WINDOW_S

    def _throttled(self, r, body):
        code = str(body.get("code")) if isinstance(body, dict) else ""
        if r.status_code != 429 and code != "429000":
            return None
        try:
            return max(1.0, float(r.headers.get("gw-ratelimit-reset")) / 1000.0)
        except (TypeError, ValueError):
            return POOL_WINDOW_S                          # gateway overload: 429000 without limit headers

    def _payload(self, body, path):
        code = str(body.get("code")) if isinstance(body, dict) else "?"
        if code == "200000":
            return body.get("data")
        msg = f"{self.name} {path}: code {code}: {str(body)[:200]}"
        if code.startswith("4"):
            raise PermanentHTTPError(msg)                 # 404000 «contract does not exist» comes with HTTP 200 [L]
        raise RuntimeError(msg)

    def budget_used(self) -> float:
        """Share of the IP pool used, by the last header; a reading older than its window's reset has expired."""
        if time.time() - self.budget_ts > self.budget_reset_s:
            return 0.0
        return self.budget

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self.banned_until and self.budget_used() < soft

    def health(self) -> dict:
        h = super().health()
        h["budget"] = round(self.budget_used(), 3)
        return h

    # --- contracts --------------------------------------------------------------------------------------------------
    def _active(self, kind: str) -> list[dict]:
        fast = kind == "tick"
        data = self.get(ACTIVE, retries=config.TICK_RETRIES if fast else 3,
                        timeout=config.TICK_HTTP_TIMEOUT if fast else config.HTTP_TIMEOUT)
        raw = [c for c in data or [] if isinstance(c, dict) and c.get("symbol")] if isinstance(data, list) else []
        if not raw:
            raise RuntimeError(f"{self.name}: contracts/active without contracts: {str(data)[:200]}")
        mult = {}
        for c in raw:
            m = _f(c.get("multiplier"))
            if m and m > 0:
                mult[c["symbol"]] = m
        with self._lock:
            self._mult = mult
            self._sources = {c["symbol"]: [str(x) for x in c.get("sourceExchanges") or []] for c in raw}
        return raw

    def _instruments(self, raw: list[dict]) -> list[dict]:
        rows = []
        for c in raw:
            if eligible(c):
                cls = asset_class(c)
                base, factor = norm_symbol_factor(ticker(c))
                rows.append((c, cls, base, factor))
        own = {(cls, base) for _, cls, base, _ in rows}
        alias = crypto_aliases()
        out = []
        for c, cls, base, factor in rows:
            if cls == "crypto" and base in alias:
                if ("crypto", alias[base]) in own:
                    log.warning("%s %s: KuCoin also lists %s — alias %s → %s not applied", self.name, c["symbol"],
                                alias[base], base, alias[base])
                else:
                    base = alias[base]
            base = config.PERP_CANON.get(cls, {}).get(base, base)
            iv = interval_ms(c)
            mult = _f(c.get("multiplier"))
            out.append(dict(exchange=self.name, symbol=str(c["symbol"]), base_asset=str(c.get("baseCurrency") or ""),
                            base=base, factor=factor, tick_size=_f(c.get("tickSize")),
                            step_size=mult * (_f(c.get("lotSize")) or 1.0) if mult else None,
                            min_notional=None,                    # the minimum order is 1 contract = multiplier × mark
                            onboard_ms=_int(c.get("firstOpenDate")),
                            interval_h=max(1, round(iv / HOUR_MS)) if iv else 8,
                            cap=_f(c.get("fundingRateCap")), floor=_f(c.get("fundingRateFloor")),
                            quote=c.get("quoteCurrency"), contract="PERPETUAL", cls=cls, contract_size=mult))
        return exchanges.keep_best_quote(out)

    def perp_instruments(self) -> list[dict]:
        return self._instruments(self._active("aux"))

    def premium(self) -> dict[str, dict]:
        """symbol → {rate (running estimate of the next settlement, fraction per interval), mark, index, next_ms}."""
        raw = self._active("tick")
        t = time.time()
        now_ms = int(t * 1000)
        out = {}
        for c in raw:
            rate = _f(c.get("fundingFeeRate"))
            if rate is None or not eligible(c):
                continue
            ivm = interval_ms(c)                  # в том же ответе каждого тика: смена интервала видна сразу
            out[str(c["symbol"])] = dict(rate=rate, mark=_f(c.get("markPrice")), index=_f(c.get("indexPrice")),
                                         next_ms=_int(c.get("nextFundingRateDateTime")), ts_ms=now_ms, obs=t,
                                         interval_h=max(1, round(ivm / 3_600_000)) if ivm else None)
        return out

    def books(self) -> dict[str, dict]:
        data = self.get(TICKERS)
        if not isinstance(data, list):
            raise RuntimeError(f"{self.name}: allTickers is not a list: {str(data)[:200]}")
        t = time.time()
        with self._lock:
            mult = dict(self._mult)
        out = {}
        for x in data:
            sym = str((x or {}).get("symbol") or "")
            m = mult.get(sym)
            qty = lambda v: (_f(v) or 0.0) * m if m else 0.0      # contracts → base units; unknown size → 0
            b = _book(x.get("bestBidPrice"), x.get("bestAskPrice"), qty(x.get("bestBidSize")), qty(x.get("bestAskSize")))
            if sym and b:
                b["obs"] = t                              # receipt time: ts is the last trade, not the book [L]
                out[sym] = b
        return out

    def index_sources(self) -> dict[str, list[str]]:
        """Exchanges named in each contract's index (sourceExchanges of the last contracts/active): exchange NAMES only,
        never market symbols [L] — not identity evidence by itself. 0 calls."""
        with self._lock:
            return {k: list(v) for k, v in self._sources.items()}

    # --- history ------------------------------------------------------------------------------------------------------
    def _hist_slot(self):
        if self.budget_used() >= config.WEIGHT_SOFT_LIMIT:
            raise BudgetExceeded(f"{self.name}: pool {self.budget_used():.0%} used (shared with kucoin_spot), history skipped")
        with self._lock:
            wait = self._last_hist + self.history_gap_s - time.time()
            self._last_hist = time.time() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settlements of one contract in [start_ms, end_ms], ascending. Each answer holds the newest ≤ 100 of the window,
        so pages go backwards from the oldest row received."""
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        if end_ms < start_ms:
            return []
        got: dict[int, dict] = {}
        hi = end_ms
        for _ in range(HISTORY_MAX_PAGES):
            self._hist_slot()
            rows = self.get(HISTORY, {"symbol": symbol, "from": start_ms, "to": hi}, retries=3, timeout=config.HTTP_TIMEOUT)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise RuntimeError(f"{self.name} {symbol}: funding-rates is not a list: {str(rows)[:200]}")
            stamps = []
            for r in rows:
                ms = _int((r or {}).get("timepoint"))
                if ms <= 0:
                    continue
                stamps.append(ms)
                rate = _f(r.get("fundingRate"))
                if rate is not None and start_ms <= ms <= end_ms:
                    got[ms] = dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=None)
            if len(rows) < HISTORY_PAGE or not stamps:
                break
            lo = min(stamps)
            if lo <= start_ms:
                break
            hi = lo - 1
        else:
            # never hand back a history with a silent hole at its old end: sync_leg would confirm the depth
            raise RuntimeError(f"{self.name} {symbol}: more than {HISTORY_MAX_PAGES} pages since {start_ms}")
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """Last settlement of every contract, from contracts/active (no batch history endpoint: funding-rates needs a
        symbol [L]): funding_ms = nextFundingRateDateTime − interval, rate = lastTimeFundingRate (equal to the history row
        on 24 of 24 contracts [L]).

        funding.apply_batch treats the batch as complete from cover = min(funding_ms) rounded up to the minute and moves
        every cursor that is already inside that window. So a contract is emitted only if its PREVIOUS settlement is before
        cover — otherwise the cursor would jump over a settlement that is not in the batch (TRUSTUSDTM, 4 h offset by 3 h:
        at 07:30 cover is 00:01 and its 03:00 would be lost). Also skipped: a settlement younger than FRESH_SETTLE_S (a
        just-rolled next time with a not-yet-updated rate would be frozen by INSERT OR IGNORE) and one before the current
        interval cycle began (after an interval change next − interval does not date the last settlement). Skipped
        contracts are served by the per-symbol repair.

        Каждый пропущенный контракт вселенной приходит меткой {exchange, symbol, hold: True} без funding_ms:
        funding.apply_batch строки не пишет и не продлевает его курсор правилом «интервал длиннее окна пакета — законно
        нечего было рассчитывать» — пропуск здесь не значит «расчёта не было». 13.09 READYUSDTM (4 ч → 1 ч с 01:00): с 00:00
        до 01:00 его пропускала проверка цикла, окно пакета 23:01…00:10 открывал 23:00 TRUST, у коллектора интервал из
        часовой вселенной (4 ч > окна) — курсор уходил за 00:00, а полнота, сравнивающая курсор с последним расчётом по
        слову биржи, дыру под курсором уже не видит."""
        raw = self._active("aux")
        now_ms = int(time.time() * 1000)
        keep = {i["symbol"] for i in self._instruments(raw)}
        cands, held = [], []
        for c in raw:
            sym = str(c["symbol"])
            if sym not in keep:
                continue
            iv, nxt, rate = interval_ms(c), _int(c.get("nextFundingRateDateTime")), _f(c.get("lastTimeFundingRate"))
            ms = nxt - iv if iv and nxt > 0 else 0
            cycle = _int(c.get("effectiveFundingRateCycleStartTime"))
            if not ms or rate is None or ms > now_ms - FRESH_SETTLE_S * 1000 or (cycle and ms < cycle):
                held.append(sym)
                continue
            cands.append((sym, ms, iv, rate))
        if not cands:
            return []
        cover = (min(ms for _, ms, _, _ in cands) // 60_000 + 1) * 60_000       # exactly as funding.apply_batch
        out = []
        for sym, ms, iv, rate in cands:
            if ms - iv < cover:
                out.append(dict(exchange=self.name, symbol=sym, funding_ms=ms, rate=rate, mark=None))
            else:
                held.append(sym)
        return out + [dict(exchange=self.name, symbol=sym, hold=True) for sym in held]
