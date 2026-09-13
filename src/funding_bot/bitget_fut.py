"""Bitget USDT-M perpetuals — perp venue «bitget» (owner 12.09: «добавить фьючи kucoin bitget и gate, lighter»; owner's
order after aster, binance, hyperliquid: kucoin, bitget, gate, lighter). Native perp client of venues.py:
perp_instruments / premium / books / history_since / recent_history, plus two hooks the collector does not call yet —
funding_intervals() (live intervals without a universe rebuild) and index_legs() (index composition for identity.py).
Public endpoints only, no keys. Transport = spot.BitgetSpot's (same host, code «00000», strings, 429 → 60 s pause).

Measured 12.09.2026 from the Mac. Tags: [L] verified live, [D] verified in docs, [A] assumption.
Universe [L]  GET /api/v3/market/instruments?category=USDT-FUTURES — 787 rows. symbolType crypto/stock/metal/commodity,
      isRwa YES/NO, fundInterval "8"/"4"/"1", priceMultiplier = tick, quantityMultiplier = qty step, minOrderAmount
      (USDT), offTime "-1" or ms (ms = delisting scheduled while status is still «online»: PKXUSDT), launchTime.
Tick [L]  GET /api/v2/mix/market/tickers?productType=USDT-FUTURES — all 787 in one call (~76 KB gz, ~0.5 s): bidPr,
      askPr, bidSz, askSz, markPrice, indexPrice, fundingRate, ts (= server time ±4 ms: live, not a stale cache). One
      call feeds premium() AND books() (cached ctx_ttl_s, like Hyperliquid).
      GET /api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES — all symbols (~4.5 KB gz): fundingRate (equal
      to the tickers' one), fundingRateInterval, nextUpdate (ms), min/maxFundingRate = floor/cap. It also lists ~11
      test / pre-listing symbols absent from the universe (RWATESTMEUSDT …) — ignored by joining on the universe.
Rate [L/D]  fraction PER INTERVAL (BTC 0.000074 per 8 h); the live PREDICTED rate for nextUpdate — moves inside the
      interval and equals the settled history row exactly at settlement (checked 15:00 UTC 12.09). The interval is
      DYNAMIC: IOSTUSDT went 8 h → 1 h on 09-10.09 — bitget must NOT be in config.FIXED_INTERVAL_H. Settlements sit on
      the UTC grid of the interval. Stocks / metals / FX: rate 0 while the market is closed (NVDA 179 of 200 rows 0,
      every XAU weekend row 0) — real zero rows, not holes.
Units [L]  mark/index/bid/ask are USDT per ONE unit of baseCoin: PEPEUSDT per PEPE, 1000BONKUSDT per 1000 BONK. The
      factor comes from the NAME (norm_symbol_factor), never from quantityMultiplier (PEPE step 1000, SHIB step 10000,
      both priced per 1 token). Bases: 1000BONK/CAT/RATS/SATS/XEC, 10000NEX, 1000000MOG, 1MBABYDOGE, 1MCHEEMS.
History [L]  GET /api/v2/mix/market/history-fund-rate?symbol&productType&pageSize&pageNo — NEWEST FIRST, pageSize
      silently capped at 100, startTime/endTime IGNORED, ~90 days deep. Past the end: 200 with data [] (re-checked
      12.09 15:5x: pageNo 4..91 of BTC) — the research run also saw 400/40808; both end the paging. Unknown symbol:
      400/40034 → PermanentHTTPError. No mark in history. No all-symbols batch → recent_history() is [] and upkeep is
      the per-leg top-up by completeness (like Hyperliquid).
Index [L]  GET /api/v3/market/index-components?symbol → {componentList: [{exchange, spotPair "BTC/USDT", weight}]}; all
      787 answer. Legs keep the PERP's unit (BINANCE «1000BONK/USDT» although Binance spot is BONKUSDT) and the pair
      base is Bitget's INTERNAL coin code (KLAY for KAIA, NEIRO for NEIROCTO, ALPHA_64 for a Binance Alpha token) —
      index_legs() rewrites them into the shapes identity.Resolver._parse reads (see _leg).
Limits [L/D]  x-mbx-used-remain-limit = calls left this second of 20, per endpoint [L]; 20 req/s/IP per endpoint [D].
      429 → 60 s pause of this client (no Retry-After [A]). Tick = 2 calls per 10 s; history paced history_gap_s.
Fees [D/L]  VIP0 USDT-M taker 0.06 % / maker 0.02 %; takerFeeRate "0.0006" on all 787 (here the API equals VIP0).
"""
from __future__ import annotations
import math, time, logging, threading
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .spot import SpotClient, BitgetSpot, _book, _f
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "bitget"
PRODUCT = "USDT-FUTURES"
H_MS = 3600_000
PATH_INST = "/api/v3/market/instruments"
PATH_TICK = "/api/v2/mix/market/tickers"
PATH_FUND = "/api/v2/mix/market/current-fund-rate"
PATH_HIST = "/api/v2/mix/market/history-fund-rate"
PATH_INDEX = "/api/v3/market/index-components"
PAGE_URL = "https://www.bitget.com/futures/usdt/{symbol}"      # 200 for BTCUSDT / NVDAUSDT; SPA without 404 [L]
HISTORY_PAGE = 100          # pageSize is silently capped at 100 [L]
HISTORY_GAP_S = 0.2         # 5 req/s = a quarter of the endpoint's 20/s; 30-day backfill of ~790 legs ≈ 1180 calls ≈ 4 min
HISTORY_MAX_PAGES = 40      # hard stop; a window that needs more full pages raises instead of silently truncating
ALPHA_TTL_S = 3600
END_CODES = ("40808",)      # «past the last page» seen by the research run (today: 200 + [])
NO_SYMBOL = "40034"         # «Parameter … does not exist»

# --- asset class (isRwa × symbolType + overrides; research 12.09 on all 787) -------------------------------------
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})                    # repo rule: gold tokens are coins
FX = frozenset({"EURUSD", "GBPUSD", "USDJPY"})               # labelled crypto+RWA by Bitget
INDEXES = frozenset({"SP500", "NDX100", "HSI", "JP225", "KR200",
                     "B200", "H100"})                        # B200/H100 = GPU rental price indices (self-priced)
PREIPO = frozenset({"OPENAI", "ANTHROPIC", "MOONSHOT", "SPCX"})
RWA_EQUITY = frozenset({"BHP", "FCX", "HPQ", "KUAISHOU", "RIO", "VALE"})   # shares Bitget labels crypto+RWA
# Bitget's own names of shares → the exchange ticker (their index legs are QNT/USD, NOK/USD … at INTRINIO / MASSIVE [L]).
# Applied after config.PERP_CANON, only inside class equity. HKD lines (TENCENTHKD 429.2 HKD vs TENCENT 54.73 USD) and
# SKHY (190) vs SKHYNIX (1364.96) are different units — deliberately NOT canonicalised.
CANON = {"equity": {"QNTSTOCK": "QNT", "NOKSTOCK": "NOK", "BBSTOCK": "BB", "DIASTOCK": "DIA", "RTXSTOCK": "RTX",
                    "STXSTOCK": "STX"}}
# spot market id of each venue the Resolver knows by market id (identity.OURX); other venues are read by base only
LEG_SEP = {"binance": "", "binance_cross": "", "binance_cross2": "", "binance_cross3": "", "bitget": "",
           "gateio": "_", "kucoin": "-"}


def _int(x, default: int | None = None) -> int | None:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def asset_class(base_coin: str, symbol_type: str | None, is_rwa) -> str:
    """crypto / equity / commodity / index / fx / preipo; «rwa» for a real-world asset nobody has classified yet (pairs
    with nothing — a new crypto+RWA symbol must not land among coins or shares by default)."""
    b = str(base_coin or "").upper()
    st = str(symbol_type or "").lower()
    rwa = str(is_rwa or "").upper() == "YES"
    if b in GOLD_TOKENS:
        return "crypto"
    if not rwa and st in ("crypto", ""):
        return "crypto"
    if b in PREIPO:
        return "preipo"
    if b in FX:
        return "fx"
    if b in INDEXES:
        return "index"
    if st in ("metal", "commodity"):
        return "commodity"                               # XAU XAG XPT XPD COPPER; CL (WTI), BZ (Brent), NATGAS
    if st == "stock" or b in RWA_EQUITY:
        return "equity"
    log.warning("bitget: %s is RWA with symbolType %r — unclassified, pairs with nothing", b, symbol_type)
    return "rwa"


def _roll(next_ms: int | None, iv_h: int, now_ms: int) -> int:
    """Next settlement: the venue's nextUpdate, rolled forward by whole intervals once passed (the funding endpoint
    missed a tick); without one — the UTC grid of the interval."""
    step = max(1, int(iv_h or 8)) * H_MS
    if not next_ms:
        return (now_ms // step + 1) * step
    if next_ms <= now_ms:
        next_ms += ((now_ms - next_ms) // step + 1) * step
    return next_ms


class BitgetFut(SpotClient):
    name = NAME
    base = "https://api.bitget.com"
    # transport quirks are BitgetSpot's: remaining calls of this second, 429 / code "429" → 60 s pause, code "00000"
    _usage = BitgetSpot._usage
    _throttled = BitgetSpot._throttled
    _payload = BitgetSpot._payload

    def __init__(self, session: requests.Session | None = None, history_gap_s: float | None = None,
                 ctx_ttl_s: float = 5.0, fund_ttl_s: float = 5.0, legs_gap_s: float | None = None):
        super().__init__(session)
        self.history_gap_s = (config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, HISTORY_GAP_S)
                              if history_gap_s is None else history_gap_s)
        self.legs_gap_s = config.LEGS_GAP_S if legs_gap_s is None else legs_gap_s
        self.ctx_ttl_s, self.fund_ttl_s = ctx_ttl_s, fund_ttl_s
        self._lock = threading.Lock()
        self._last_hist = 0.0
        self._last_legs = 0.0
        self._tick: tuple[float, dict[str, dict]] = (0.0, {})       # (arrival time, symbol → tickers row)
        self._fund: tuple[float, dict[str, dict]] = (0.0, {})       # (arrival time, symbol → {iv, next_ms, cap, floor})
        self._uni: dict[str, int] = {}                              # last universe: symbol → interval_h
        self._alpha: tuple[float, dict[str, str]] = (0.0, {})       # Binance Alpha id → token symbol

    # --- shared snapshots ----------------------------------------------------------------------------------------
    def _tickers(self) -> tuple[float, dict[str, dict]]:
        with self._lock:
            ts, rows = self._tick
        if rows and time.time() - ts < self.ctx_ttl_s:
            return ts, rows
        data = self.get(PATH_TICK, {"productType": PRODUCT})           # tick call: one try, short timeout
        t = time.time()
        rows = {r["symbol"]: r for r in data or [] if isinstance(r, dict) and r.get("symbol")}
        if not rows:
            raise RuntimeError(f"{self.name}: tickers without symbols")
        with self._lock:
            self._tick = (t, rows)
        return t, rows

    def _funding(self, fresh: bool = False, **kw) -> dict[str, dict]:
        with self._lock:
            ts, rows = self._fund
        if not fresh and rows and time.time() - ts < self.fund_ttl_s:
            return rows
        out = {}
        for r in self.get(PATH_FUND, {"productType": PRODUCT}, **kw) or []:
            if isinstance(r, dict) and r.get("symbol"):
                out[r["symbol"]] = dict(iv=_int(r.get("fundingRateInterval")), next_ms=_int(r.get("nextUpdate")),
                                        cap=_f(r.get("maxFundingRate")), floor=_f(r.get("minFundingRate")))
        if not out:
            raise RuntimeError(f"{self.name}: current-fund-rate without symbols")
        with self._lock:
            self._fund = (time.time(), out)
        return out

    def _funding_soft(self, **kw) -> dict[str, dict]:
        """Funding snapshot for the tick: a failure keeps the previous one — the rate itself comes from the tickers,
        and next_ms rolls forward on the interval grid (_roll)."""
        try:
            return self._funding(**kw)
        except Exception as e:  # noqa — a missed funding call must not drop the venue's rates
            log.warning("%s current-fund-rate: %s: %s — previous snapshot kept", self.name, type(e).__name__, e)
            with self._lock:
                return self._fund[1]

    def _mine(self, sym: str) -> bool:
        uni = self._uni
        return sym in uni if uni else sym.endswith(config.QUOTE)

    # --- native perp interface ----------------------------------------------------------------------------------------
    def perp_instruments(self) -> list[dict]:
        raw = self.get(PATH_INST, {"category": PRODUCT}, retries=3, timeout=config.HTTP_TIMEOUT) or []
        fund = self._funding_soft(fresh=True, retries=3, timeout=config.HTTP_TIMEOUT)
        now_ms = int(time.time() * 1000)
        out, uni = [], {}
        for s in raw:
            if not isinstance(s, dict):
                continue
            sym, coin = s.get("symbol") or "", s.get("baseCoin") or ""
            if not sym or not coin or s.get("quoteCoin") != config.QUOTE or s.get("status") != "online" \
                    or (s.get("type") or "perpetual") != "perpetual":
                continue
            if str(s.get("offTime") or "-1") not in ("-1", "0"):
                continue                                  # delisting scheduled, status still «online» (PKXUSDT)
            launch = _int(s.get("launchTime"), 0) or 0
            if launch > now_ms:
                continue                                  # listing announced, trading not started [A]
            cls = asset_class(coin, s.get("symbolType"), s.get("isRwa"))
            b, factor = norm_symbol_factor(coin)          # the unit is in the NAME, not in quantityMultiplier
            base = config.PERP_CANON.get(cls, {}).get(b) or CANON.get(cls, {}).get(b, b)
            fr = fund.get(sym) or {}
            iv = fr.get("iv") or _int(s.get("fundInterval")) or 8
            out.append(dict(exchange=self.name, symbol=sym, base_asset=coin, base=base, factor=factor,
                            tick_size=_f(s.get("priceMultiplier")), step_size=_f(s.get("quantityMultiplier")),
                            min_notional=_f(s.get("minOrderAmount")), onboard_ms=launch, interval_h=int(iv),
                            cap=fr.get("cap"), floor=fr.get("floor"), quote=config.QUOTE, contract="PERPETUAL",
                            cls=cls, url=PAGE_URL.format(symbol=quote(sym, safe=""))))   # 龙虾USDT → %E9%BE%99…
            uni[sym] = int(iv)
        if not out:
            raise RuntimeError(f"{self.name}: instruments without tradable USDT perps")
        with self._lock:
            self._uni = uni
        return out

    def premium(self) -> dict[str, dict]:
        """symbol → {rate (predicted, fraction per interval), mark, index, next_ms, ts_ms, obs}. obs = arrival of the
        tickers snapshot: a cached snapshot is not a new observation."""
        t, rows = self._tickers()
        fund = self._funding_soft()
        now_ms = int(time.time() * 1000)
        out = {}
        for sym, r in rows.items():
            if not self._mine(sym):
                continue
            rate = _f(r.get("fundingRate"))
            if rate is None:
                continue
            f = fund.get(sym) or {}
            iv = f.get("iv") or self._uni.get(sym) or 8
            out[sym] = dict(rate=rate, mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice")),
                            next_ms=_roll(f.get("next_ms"), iv, now_ms), ts_ms=_int(r.get("ts")) or int(t * 1000), obs=t,
                            interval_h=int(iv))      # calc берёт интервал тика раньше вселенной (смена на ходу)
        return out

    def books(self) -> dict[str, dict]:
        t, rows = self._tickers()
        out = {}
        for sym, r in rows.items():
            if self._mine(sym):
                b = _book(r.get("bidPr"), r.get("askPr"), r.get("bidSz"), r.get("askSz"))   # drops empty / crossed
                if b:
                    b["obs"] = t
                    out[sym] = b
        return out

    def funding_intervals(self) -> dict[str, int]:
        """Live intervals of the universe from the funding endpoint (IOST 8 h → 1 h switches between universe
        rebuilds). Hook for venues.funding_intervals(); not called by the collector until it is wired."""
        fund = self._funding(fresh=True, retries=3, timeout=config.HTTP_TIMEOUT)
        uni = dict(self._uni)
        if not uni:
            return {s: f["iv"] for s, f in fund.items() if f.get("iv")}
        out = {s: int((fund.get(s) or {}).get("iv") or iv) for s, iv in uni.items()}
        with self._lock:
            self._uni = out
        return dict(out)

    def _pace(self, attr: str, gap: float):
        with self._lock:
            wait = getattr(self, attr) + gap - time.time()
            setattr(self, attr, time.time() + max(0.0, wait))
        if wait > 0:
            time.sleep(wait)

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled rates of one symbol in [start_ms, end_ms], ascending. Pages are newest-first and ignore the time
        window, so paging goes backwards until a page is short or reaches start_ms. pageSize is sized to the window at
        the shortest interval (1 h): a short repair pulls 2-3 rows, not 100. A settlement landing between two page
        calls shifts pages by one row — a duplicate at the boundary (dropped here), never a gap."""
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        span_h = max(0, end_ms - start_ms) / H_MS
        size = max(1, min(HISTORY_PAGE, int(math.ceil(span_h)) + 2))
        pages = min(HISTORY_MAX_PAGES, int(math.ceil((span_h + 2) / size)) + 2)
        got: dict[int, dict] = {}
        for page in range(1, pages + 1):
            self._pace("_last_hist", self.history_gap_s)
            try:
                rows = self.get(PATH_HIST, dict(symbol=symbol, productType=PRODUCT, pageSize=size, pageNo=page),
                                retries=3, timeout=config.HTTP_TIMEOUT) or []
            except PermanentHTTPError as e:
                if any(c in str(e) for c in END_CODES):
                    return [got[k] for k in sorted(got)]       # past the last page
                raise                                          # 40034: no such symbol
            stamps = []
            for r in rows:
                ms = _int(r.get("fundingTime")) if isinstance(r, dict) else None
                if ms is None:
                    continue
                stamps.append(ms)
                rate = _f(r.get("fundingRate"))
                if rate is not None and start_ms <= ms <= end_ms:
                    got[ms] = dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=None)
            if len(rows) < size or not stamps or min(stamps) <= start_ms:
                return [got[k] for k in sorted(got)]
        # every page full and start_ms still not reached: a silent cut would confirm depth that was never fetched
        raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {pages} pages of {size}")

    def recent_history(self) -> list[dict]:
        """No «settled rates of all symbols» endpoint (current-fund-rate carries only the predicted rate) [L]."""
        return []

    # --- index composition (identity.py) ------------------------------------------------------------------------------
    def _alpha_ids(self) -> dict[str, str]:
        """Binance Alpha id (ALPHA_64) → token symbol, from the public Alpha list the Resolver also uses. Failure →
        {} (alpha legs become «binance_alpha_id»: no evidence rather than a guess by the word «ALPHA»)."""
        with self._lock:
            ts, m = self._alpha
        if m and time.time() - ts < ALPHA_TTL_S:
            return m
        try:
            r = self._s.get(config.BINANCE_ALPHA_URL, timeout=config.HTTP_TIMEOUT)
            r.raise_for_status()
            body = r.json()
            rows = body.get("data") if isinstance(body, dict) else body
            m = {str(t["alphaId"]).upper(): str(t["symbol"]).upper() for t in rows or []
                 if isinstance(t, dict) and t.get("alphaId") and t.get("symbol")}
        except Exception as e:  # noqa
            log.warning("%s Binance Alpha list: %s: %s", self.name, type(e).__name__, e)
            m = {}
        if m:
            with self._lock:
                self._alpha = (time.time(), m)
        return m

    @staticmethod
    def _leg(c: dict, alpha: dict[str, str]) -> dict | None:
        """One component → a leg in the shape identity.Resolver._parse reads ({exchange, symbol, weight}):
        - exchange lower-case (BINANCE → binance);
        - our spot venues (identity.OURX) — the venue's own market id: BTCUSDT (Binance, Bitget), BTC_USDT (Gate),
          BTC-USDT (KuCoin); other venues and vendors — BASE_QUOTE (split_market reads the base);
        - the perp's unit moved into «*N» (Binance style, as /fapi/v1/constituents «PEPEUSDT*1000»):
          BINANCE «1000BONK/USDT» → «BONKUSDT*1000»;
        - BINANCE_ALPHA «ALPHA_64/USDT» → the token symbol from the Alpha list; unknown id → «binance_alpha_id».
        The raw pair stays in «pair» for the audit trail."""
        ex = str(c.get("exchange") or "").strip().lower()
        pair = str(c.get("spotPair") or "").strip()
        if not ex or not pair:
            return None
        b, _, q = pair.partition("/")
        b, q = b.strip(), (q.strip().upper() or config.QUOTE)
        w = str(c.get("weight") or "0")
        if ex == "binance_alpha":
            tok = alpha.get(b.upper())
            if tok:
                return {"exchange": ex, "symbol": f"{tok}{q}", "weight": w, "pair": pair}
            return {"exchange": "binance_alpha_id", "symbol": b.upper(), "weight": w, "pair": pair}
        base, f = norm_symbol_factor(b)
        sym = f"{base}{LEG_SEP.get(ex, '_')}{q}" + (f"*{f:g}" if f != 1.0 else "")
        return {"exchange": ex, "symbol": sym, "weight": w, "pair": pair}

    def index_legs(self, symbols: list[str], deadline_s: float | None = None) -> dict[str, dict]:
        """Symbol → {"legs": [...] | None (no index), "dex": {}} — same contract as identity_src.index_legs (which
        dispatches here by hasattr). No DEX pools in Bitget indices [L]. One symbol failing is skipped (retried next
        job); 429 / a foreign 4xx / the deadline returns what was collected."""
        out, errs = {}, 0
        t_end = time.time() + (config.LEGS_DEADLINE_S if deadline_s is None else deadline_s)
        alpha: dict[str, str] | None = None
        for s in symbols:
            if time.time() > t_end:
                break
            self._pace("_last_legs", self.legs_gap_s)
            try:
                d = self.get(PATH_INDEX, {"symbol": s}, retries=2, timeout=config.HTTP_TIMEOUT)
                comps = d.get("componentList") if isinstance(d, dict) else None
            except PermanentHTTPError as e:
                if NO_SYMBOL not in str(e):
                    if out:
                        log.warning("%s index components: %s — batch cut", self.name, e)
                        break
                    raise
                comps = None                              # no such symbol → no index
            except (BannedError, BudgetExceeded):
                if out:
                    break
                raise
            except Exception as e:  # noqa — network / 5xx / broken body of one symbol: the rest go on
                errs += 1
                log.warning("%s index components %s: %s: %s", self.name, s, type(e).__name__, e)
                continue
            legs = None
            if isinstance(comps, list) and comps:
                if alpha is None and any(str((c or {}).get("exchange") or "").lower() == "binance_alpha" for c in comps):
                    alpha = self._alpha_ids()
                legs = [x for x in (self._leg(c, alpha or {}) for c in comps if isinstance(c, dict)) if x] or None
            out[s] = {"legs": legs, "dex": {}}
        if not out and errs:
            raise RuntimeError(f"{self.name}: index components failed for all {errs} symbols of the batch")
        return out
