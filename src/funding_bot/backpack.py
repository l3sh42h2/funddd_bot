"""Backpack Exchange perpetuals — perp venue «backpack» (owner 13.09: «добавь биржи backpack, variational, edgex, extended,
pacifica, apex во фьючи»; the first of the six, right after lighter_rh in the owner's order). Native perp client of
venues.py: perp_instruments / premium / books / history_since / recent_history, plus the funding_intervals() hook. No
index_legs(): the index composition is not public. Public endpoints only — no keys, no account, no orders.

Measured 12-13.09.2026 from the Mac. Tags: [L] verified live, [D] docs (docs.backpack.exchange, support pages),
[A] assumption.
Hosts [L]  REST https://api.backpack.exchange/api/v1 behind CloudFront (numbers are strings, times ms unless noted);
      WS wss://ws.backpack.exchange. The minimal RFC 6455 client lighter._WS works against it unchanged (imported).
Universe [L]  GET /markets?marketType=PERP — 102 rows (5 KB gz, CDN s-maxage=300): symbol «BTC_USDC_PERP», baseSymbol,
      quoteSymbol (USDC on all 102), orderBookState Open | PostOnly | Closed, visible, createdAt (ISO, NO zone = UTC,
      microseconds), fundingInterval (ms; 3600000 on all 102), fundingRateUpperBound / LowerBound («150» / «-150»; BTC,
      ETH, SOL «100») — BASIS POINTS per interval [L-inferred: history plateaus at exactly ±0.015 on bound-150 markets,
      BTC/ETH/SOL never beyond 0.0073], rwaMarketType null | STOCK | INDEX, filters.price.tickSize,
      filters.quantity.stepSize / minQuantity (base units; no notional filter). Kept: Open and visible (12.09: 89).
      Closed (11: absent from /markPrices, /depth 404) and PostOnly (AMZN.US, AMD.US: visible=false, no taker orders)
      are dropped [A — as Lighter's force_reduce_only].
Units [L]  a unit is ONE baseSymbol: kBONK / kPEPE / kSHIB are priced per 1000 tokens (index / Binance spot ≈ 1000.6 /
      1000.5 / 1000.8); upper-case KAITO, KMNO are not prefixed. The factor comes from the NAME (norm_symbol_factor).
      Stocks: baseSymbol «NVDA.US» → NVDA. SKHY.US (mark 187) is the SKHY listing, NOT SKHYNIX (1364) — kept as SKHY.
Classes [L]  rwaMarketType null → crypto (PAXG a coin: repo rule); STOCK → equity (MU SNDK NVDA GOOGL META AMZN TSLA
      CRCL AAPL AMD HOOD INTC SKHY SPCX — «SpaceX» is equity like on 10 other venues at the same price, review 13.09 [L];
      preipo only for OPENAI / ANTHROPIC, private on every venue); INDEX on a «.US» ticker →
      equity: QQQ.US, SPY.US, DRAM.US are ETF SHARES, not indices (Lighter files ETFs as equity) [L names]; INDEX
      without «.US» → index [A]; an unknown rwaMarketType → «rwa» (pairs with nothing) + a warning.
Rates [L]  GET /markPrices — every non-closed perp in one call (2.4 KB gz, CDN s-maxage=1 + stale-while-revalidate=3):
      {symbol, fundingRate, markPrice, indexPrice, nextFundingTimestamp (ms)}, no server time (obs = arrival minus the
      CDN Age header). fundingRate = the PREDICTED rate of the interval in progress, paid at nextFundingTimestamp, a signed
      FRACTION PER INTERVAL = PER 1 HOUR — no conversion: not per 8 h, not annualised (equal to the in-progress history
      row; baseline 0.0000125 = 0.03 %/24 h interest term [D]). Hourly on the hour since 2025-08-20 08:00 UTC [D].
      Stock perps pay a constant +0.00000625/h when there is no premium — real rows, not zeros.
History [L/D]  GET /fundingRates?symbol&limit&offset — symbol required (no all-markets batch → recent_history() is []),
      limit ≤ 10000 [D; 10000 returned L], offset in rows, NO time filter, NEWEST FIRST, 1 h apart (0 gaps / 0 off-grid
      in 30 d on all 91 live markets). Row {symbol, intervalEndTimestamp (ISO, no zone = UTC), fundingRate (9 decimals)};
      no mark. PITFALL 1: the newest row is the interval IN PROGRESS (+≤1 h in the future), a running average that turns
      final only 1-3 min AFTER hh:00 (kBONK 22:00: −0.000060053 at 21:59:40, −0.000059749 at 22:00:30, final −0.000059703
      from 22:02). So rows later than min(end, now − config.SETTLE_GRACE_S) are dropped: otherwise funding.sync_leg would
      move the cursor onto the future row and store a provisional rate as settled. PITFALL 2: an unknown or wrong symbol
      answers 200 [] — a live market answering [] raises instead of confirming depth. CDN caches each exact query string
      60 s (+120 s stale) — a top-up right after hh:00 may see a copy ~3 min old; the 600 s grace covers it. Rows before
      2025-08-20 are 8 h / 4 h apart (irrelevant for 30 days).
Books [L]  no all-markets bid/ask over REST (/tickers has none). WS «bookTicker.<symbol>» for every perp on ONE
      connection ({"method":"SUBSCRIBE","params":[…]}; 91 streams in one message worked): {"stream","data":{e, E µs,
      T µs, s, a ask, A askQty, b bid, B bidQty, u}}. CHANGE-ONLY, no snapshot on subscribe: in 35 s 86/91 spoke; MNT,
      BILL, QQQ.US, AMZN.US, TSLA.US stayed silent (max gap 21 s). Server pings, lighter._WS answers. A market the stream
      has not priced yet is read from REST GET /depth?symbol&limit=5 — at most DEPTH_PER_CALL per books() call, each
      market at most once per REST_BOOK_TTL_S. /depth sorts BOTH sides ASCENDING (best bid = bids[-1], best ask = asks[0]);
      timestamp in MICROseconds; uncached. Quantities are in the market's unit (baseSymbol: kBONK = 1000 BONK).
Limits [L/D]  none documented, no rate headers, no 429 seen (60 depth calls in 23.6 s; 102 history calls in 35.8 s).
      Own budget [A]: tick 1 call + ≤ 5 depth calls, history ≥ 0.5 s apart (config.FUNDING_HISTORY_MIN_GAP_S overrides),
      hourly universe + assets. 429 / 418 → the host pauses (Retry-After, else 60 s) for every client of it in the process
      (the collector's background copies from make_clients()); other 4xx → PermanentHTTPError.
Fees [D]  Tier 1 perps taker 0.050 % / maker 0.020 % — config.FEES_TAKER.
Page [L]  https://backpack.exchange/trade/BTC_USDC_PERP → 308 → /trade/BTC_USD_PERP (200); NVDA.US_USD_PERP 200. The SPA
      answers 200 for nonsense too — links only from the current list.
Identity  index = weighted mean of per-exchange medians, sources NOT disclosed [D], no endpoint [L] → an oracle venue for
      identity.py (ORACLE_PERPS). Names from GET /assets (7258 rows, hourly): name_hint = displayName unless it is just the
      ticker in any case (FARTCOIN, AVAX, S, kBONK → None; «Sui», «Bonk» too — they add nothing to the ticker); 45 of 89
      live perps get a name [L]. A k-market borrows name and coingeckoId of its unprefixed asset (kBONK → coingecko
      «bonk»; the name «Bonk» is the ticker → None; kSHIB → «Shiba Inu» when listed so) [A].
      coingeckoId is carried as coingecko_id (EDGE = «edgex», LIT = «lighter», S = «sonic-3») for a future Resolver
      input; token contracts are not carried (placeholders «bc1», «0x0», «So1» are not contracts).
"""
from __future__ import annotations
import json, logging, math, socket, threading, time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .lighter import _WS
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "backpack"
REST = "https://api.backpack.exchange/api/v1"
WS_URL = "wss://ws.backpack.exchange"
PAGE_URL = "https://backpack.exchange/trade/{front}_USD_PERP"   # the app's canonical route [L]
QUOTE = "USDC"
HOUR_MS = 3600_000
INTERVAL_H = 1                  # every market settles hourly on the hour [D/L]
BOUND_UNIT = 10_000.0           # fundingRateUpperBound / LowerBound are basis points per interval [L-inferred]
HISTORY_PAGE = 1000             # rows per call (limit ≤ 10000 [D]); 30 days = 722 rows → one call per leg
HISTORY_SLACK = 2               # the in-progress row + one spare over the hours of the window
HISTORY_MAX_PAGES = 40          # 40 × 1000 h ≈ 4.5 years; more full pages raise, never truncate silently
HISTORY_GAP_S = 0.5             # between history calls (config.FUNDING_HISTORY_MIN_GAP_S overrides by venue name)
HIST_MAX_WAIT_S = 90.0
MARKETS_TTL_S = 60              # an unknown symbol in history refreshes the market list at most this often
ASSETS_TTL_S = 3600             # /assets (names) with the hourly universe
ASSETS_STALE_OK_S = 86400       # /assets down: a copy up to a day old still names markets; older → no names
BAN_DEFAULT_S = 60.0            # 429 without Retry-After [A]
REQ_REF_PER_MIN = 600           # [A] no documented limit: only scales health()["budget"]
CDN_AGE_MAX_S = 60.0            # a plausible CloudFront Age (s-maxage 1 + stale 3 on /markPrices [L])
OBS_SKEW_S = 120.0              # an exchange timestamp further than this from arrival is ignored
SPREAD_MAX = 0.05               # [A] a book wider than 5 % of mid is not a price (as lighter._book)
DEPTH_PER_CALL = 5              # REST /depth fallbacks per books() call (91 would take ~35 s [L])
DEPTH_BUDGET_S = 3.0            # the fallback stops after this long within one books() call
DEPTH_TIMEOUT_S = 3.0
REST_BOOK_TTL_S = 30.0          # a market is re-read over REST at most this often
WS_RECV_TIMEOUT_S = 5.0
WS_SILENT_S = 30.0              # no bookTicker on the connection for this long → reconnect (89 markets speak constantly)
WS_BACKOFF_MAX_S = 30.0
WS_FIRST_WAIT_S = 3.0           # the very first books() call waits this long for market data (then never blocks)
WS_SUB_CHUNK = 100              # streams per SUBSCRIBE message [A: 91 in one worked L]

PREIPO = frozenset({"OPENAI", "ANTHROPIC"})    # private on every venue; SPCX is equity elsewhere (review 13.09) [L]
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_WARNED: set[str] = set()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x, default: int | None = 0) -> int | None:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _warn_once(key: str, msg: str, *args):
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(msg, *args)


def iso_ms(x) -> int | None:
    """«2026-09-12T22:00:00» / «2025-01-21T06:34:54.691858» (no zone = UTC [L]) → epoch ms, exactly."""
    try:
        d = datetime.fromisoformat(str(x).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - _EPOCH) // timedelta(milliseconds=1)


def _ticker(base_symbol: str) -> str:
    b = str(base_symbol or "")
    return b[:-3] if b.upper().endswith(".US") and len(b) > 3 else b


def perp_class(base_symbol: str, rwa) -> str:
    """crypto / equity / index / preipo by rwaMarketType; «rwa» for a real-world type nobody has classified (pairs with
    nothing — a new RWA kind must not land among coins or shares by default)."""
    if rwa is None or str(rwa).strip() == "":
        return "crypto"
    t = str(rwa).strip().upper()
    share = str(base_symbol or "").upper().endswith(".US")
    if t == "STOCK":
        return "preipo" if _ticker(base_symbol).upper() in PREIPO else "equity"
    if t == "INDEX":
        return "equity" if share else "index"        # QQQ.US / SPY.US / DRAM.US are ETF shares [L names]
    _warn_once(f"cls:{base_symbol}:{t}", "%s: %s has rwaMarketType %r — unclassified, pairs with nothing", NAME,
               base_symbol, rwa)
    return "rwa"


def perp_base(base_symbol: str, cls: str) -> tuple[str, float]:
    """baseSymbol → (canonical base, tokens per unit): kBONK → BONK ×1000 (lower-case k only: KAITO, KMNO stay),
    NVDA.US → NVDA (shares only — a «.US» coin would stay unpaired), then config.PERP_CANON of the class."""
    b = str(base_symbol or "")
    if cls != "crypto":
        b = _ticker(b)
    base, factor = norm_symbol_factor(b)
    return config.PERP_CANON.get(cls, {}).get(base, base), factor


def _name(rec: dict | None, *tickers) -> str | None:
    """A declared name, not a ticker: «Bitcoin» yes; «FARTCOIN», «AVAX», «kBONK» no (identity must not confirm by ticker)."""
    n = str((rec or {}).get("name") or "").strip()
    if not n or n.upper() in {str(t).upper() for t in tickers if t}:
        return None
    return n


def _book(bid, ask, bid_qty=None, ask_qty=None) -> dict | None:
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=abs(_f(bid_qty) or 0.0), ask_qty=abs(_f(ask_qty) or 0.0))


def depth_book(body, rx: float) -> dict | None:
    """/depth answer → best bid / ask with sizes. Both sides come sorted ASCENDING [L] — taken by price, not position.
    obs = the answer's timestamp (MICROseconds [L]) when plausible, capped at arrival; else arrival."""
    if not isinstance(body, dict):
        return None

    def side(rows):
        out = []
        for r in rows or []:
            if isinstance(r, (list, tuple)) and len(r) >= 2:
                p, q = _f(r[0]), _f(r[1])
                if p and p > 0 and q is not None and q > 0:
                    out.append((p, q))
        return out
    bids, asks = side(body.get("bids")), side(body.get("asks"))
    if not bids or not asks:
        return None
    bb, ba = max(bids), min(asks)
    b = _book(bb[0], ba[0], bb[1], ba[1])
    if b is None:
        return None
    ts = _int(body.get("timestamp"), None)
    s = ts / 1e6 if ts else None
    b["obs"] = min(s, rx) if s is not None and abs(s - rx) <= OBS_SKEW_S else rx
    return b


def _roll(next_ms: int | None, iv_h: int, now_ms: int) -> int:
    """Next settlement: the venue's nextFundingTimestamp, rolled forward by whole intervals once passed (a CDN copy from
    before hh:00); without one — the UTC grid of the interval."""
    step = max(1, int(iv_h or INTERVAL_H)) * HOUR_MS
    if not next_ms:
        return (now_ms // step + 1) * step
    if next_ms <= now_ms:
        next_ms += ((now_ms - next_ms) // step + 1) * step
    return next_ms


# --- per-host state ---------------------------------------------------------------------------------------------------
class _Host:
    """State of one REST host shared by every client of it in this process: a limit (if any) is per IP."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()
        self.banned_until = 0.0
        self.last_hist = 0.0
        self.markets: dict[str, dict] = {}       # live perp symbol → {onboard_ms, interval_h}
        self.markets_ts = 0.0
        self.seen: list[str] = []                # perp symbols of the last /markPrices (books before the universe)
        self.assets: dict[str, dict] | None = None   # asset symbol → {name, cg}
        self.assets_ts = 0.0

    def used(self, now: float) -> int:
        with self.lock:
            while self.calls and now - self.calls[0] >= 60.0:
                self.calls.popleft()
            return len(self.calls)


_HOSTS: dict[str, _Host] = {}
_HOSTS_LOCK = threading.Lock()


def _host(base: str) -> _Host:
    with _HOSTS_LOCK:
        return _HOSTS.setdefault(base, _Host())


# --- client -----------------------------------------------------------------------------------------------------------
class BackpackFut:
    name = NAME

    def __init__(self, session: requests.Session | None = None, history_gap_s: float | None = None, connect=None,
                 rest: str = REST, ws_url: str = WS_URL):
        self.rest, self.ws_url = rest, ws_url
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._h = _host(rest)
        self.history_gap_s = (config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, HISTORY_GAP_S)
                              if history_gap_s is None else history_gap_s)
        self._connect = connect
        self._stream: _BookStream | None = None
        self._lock = threading.Lock()
        self._rest_books: dict[str, dict] = {}   # symbol → {"t": last REST attempt, "book": dict | None}
        self.used_weight = 0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0

    # --- budget and health ------------------------------------------------------------------------------------
    @property
    def banned_until(self) -> float:
        return self._h.banned_until

    def budget_used(self) -> float:
        return self._h.used(time.time()) / float(REQ_REF_PER_MIN)

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self._h.banned_until

    def health(self) -> dict:
        self.used_weight = self._h.used(time.time())
        return {"exchange": self.name, "used_weight": self.used_weight,
                "budget": round(self.used_weight / REQ_REF_PER_MIN, 3), "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": int(self._h.banned_until)}

    # --- transport -------------------------------------------------------------------------------------------------
    def _acquire(self, kind: str):
        """Permission to call: never during a 429 pause; history also keeps its pace (shared by the host's clients)."""
        h = self._h
        t_end = time.time() + HIST_MAX_WAIT_S
        while True:
            now = time.time()
            if now < h.banned_until:
                raise BannedError(f"{self.name}: pause after 429 until {time.strftime('%H:%M:%S', time.gmtime(h.banned_until))}")
            with h.lock:
                wait = h.last_hist + self.history_gap_s - now if kind == "hist" else 0.0
                if wait <= 0:
                    if kind == "hist":
                        h.last_hist = now
                    h.calls.append(now)
                    return
            if now + wait > t_end:
                raise BudgetExceeded(f"{self.name}: history pace, call skipped")
            time.sleep(min(wait, 5.0))

    def _throttle(self, r, path: str):
        try:
            pause = float((r.headers or {}).get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            pause = BAN_DEFAULT_S
        pause = max(1.0, pause)
        with self._h.lock:
            self._h.banned_until = max(self._h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s %d on %s — host paused for %.0f s", self.name, r.status_code, path, pause)
        raise BannedError(f"{self.name}: {r.status_code} on {path}, pause {pause:.0f} s")

    def _request(self, path: str, params: dict | None = None, kind: str = "aux", retries: int | None = None,
                 timeout: float | None = None):
        """One GET → (JSON body, response). 429/418 → host pause + BannedError; other 4xx → permanent (error bodies are
        {code, message} or plain text [L]); 5xx, network, bad JSON → retried (the tick: one try — a retry is the next
        tick)."""
        retries = (config.TICK_RETRIES if kind == "tick" else 3) if retries is None else retries
        timeout = (config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT) if timeout is None else timeout
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            try:
                r = self._s.get(self.rest + path, params=params, timeout=timeout)
                if r.status_code in (429, 418):
                    self._throttle(r, path)
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path}: {str(r.text)[:200]}")
                r.raise_for_status()
                body = r.json()
                self.last_ok_ts = time.time()
                return body, r
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON — retried
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    def _get(self, path: str, params: dict | None = None, **kw):
        return self._request(path, params, **kw)[0]

    @staticmethod
    def _rows(body, path: str) -> list[dict]:
        if not isinstance(body, list):
            raise RuntimeError(f"{NAME} {path}: not a list: {str(body)[:200]}")
        return [r for r in body if isinstance(r, dict)]

    # --- shared data -----------------------------------------------------------------------------------------------
    @staticmethod
    def _live(m: dict) -> bool:
        return (str(m.get("marketType") or "") == "PERP" and m.get("orderBookState") == "Open"
                and m.get("visible") is not False and str(m.get("quoteSymbol") or "") == QUOTE
                and bool(m.get("symbol")) and bool(m.get("baseSymbol")))

    @staticmethod
    def _interval_h(m: dict) -> int:
        ms = _int(m.get("fundingInterval"), None)
        if not ms or ms <= 0 or ms % HOUR_MS:
            _warn_once(f"iv:{m.get('symbol')}:{ms}", "%s %s: fundingInterval %r is not whole hours — 1 h assumed", NAME,
                       m.get("symbol"), m.get("fundingInterval"))
            return INTERVAL_H
        iv = ms // HOUR_MS
        if iv != INTERVAL_H:
            _warn_once(f"iv:{m.get('symbol')}:{ms}", "%s %s: fundingInterval %d h ≠ 1 h — not verified live (and "
                       "config.FIXED_INTERVAL_H says 1 h)", NAME, m.get("symbol"), iv)
        return int(iv)

    def _markets(self, kind: str = "aux") -> list[dict]:
        """GET /markets?marketType=PERP → live perp rows (Open, visible, USDC); remembers {symbol → onboard, interval}
        for the host (history of a background copy, premium filtering, books)."""
        rows = [m for m in self._rows(self._get("/markets", {"marketType": "PERP"}, kind=kind), "/markets")
                if str(m.get("marketType") or "") == "PERP"]
        if not rows:
            raise RuntimeError(f"{self.name}: /markets without perps")
        now_ms = int(time.time() * 1000)
        live = [m for m in rows if self._live(m) and (iso_ms(m.get("createdAt")) or 0) <= now_ms]
        for m in rows:
            if str(m.get("quoteSymbol") or "") != QUOTE:
                _warn_once(f"q:{m.get('symbol')}", "%s %s: quote %r — only USDC perps are taken", NAME, m.get("symbol"),
                           m.get("quoteSymbol"))
        mk = {str(m["symbol"]): dict(onboard_ms=iso_ms(m.get("createdAt")) or 0, interval_h=self._interval_h(m))
              for m in live}
        if not mk:
            raise RuntimeError(f"{self.name}: /markets without open perps")
        with self._h.lock:
            self._h.markets, self._h.markets_ts = mk, time.time()
        return live

    def _assets(self) -> dict[str, dict]:
        """GET /assets → {symbol: {name, cg}} (displayName, coingeckoId). Names are optional: a failure keeps a copy up to
        a day old, else {} — the universe never fails for want of names."""
        h, now = self._h, time.time()
        if h.assets is not None and now - h.assets_ts < ASSETS_TTL_S:
            return h.assets
        try:
            rows = self._rows(self._get("/assets", kind="aux"), "/assets")
            m = {str(a["symbol"]): dict(name=a.get("displayName"), cg=a.get("coingeckoId"))
                 for a in rows if a.get("symbol")}
            if not m:
                raise RuntimeError("/assets: empty")
        except (BannedError, BudgetExceeded):
            raise
        except Exception as e:  # noqa — names are evidence, not a precondition
            if h.assets is not None and now - h.assets_ts < ASSETS_STALE_OK_S:
                log.warning("%s /assets: %s: %s — names from %.0f min ago", self.name, type(e).__name__, e,
                            (now - h.assets_ts) / 60)
                return h.assets
            log.warning("%s /assets: %s: %s — markets without names", self.name, type(e).__name__, e)
            return {}
        h.assets, h.assets_ts = m, now
        return m

    # --- native perp interface ----------------------------------------------------------------------------------------
    def perp_instruments(self) -> list[dict]:
        live = self._markets("aux")
        assets = self._assets()
        out = []
        for m in live:
            sym, bs = str(m["symbol"]), str(m["baseSymbol"])
            cls = perp_class(bs, m.get("rwaMarketType"))
            base, factor = perp_base(bs, cls)
            iv = self._interval_h(m)
            up, lo = _f(m.get("fundingRateUpperBound")), _f(m.get("fundingRateLowerBound"))
            flt = m.get("filters") or {}
            px, qty = flt.get("price") or {}, flt.get("quantity") or {}
            rec = assets.get(bs)
            name = _name(rec, bs, _ticker(bs), base)
            cg = (rec or {}).get("cg")
            if factor != 1.0 and base in assets:          # kBONK → the asset BONK («Bonk», coingecko «bonk») [A]
                name = name or _name(assets[base], base, bs)
                cg = cg or assets[base].get("cg")
            front = sym[:-len("_USDC_PERP")] if sym.endswith("_USDC_PERP") else bs
            out.append(dict(exchange=self.name, symbol=sym, base_asset=bs, base=base, factor=factor,
                            tick_size=_f(px.get("tickSize")), step_size=_f(qty.get("stepSize")),
                            min_notional=None,             # minQuantity is in base units; no notional filter [L]
                            onboard_ms=iso_ms(m.get("createdAt")) or 0, interval_h=iv,
                            cap=up / BOUND_UNIT if up is not None else None,
                            floor=lo / BOUND_UNIT if lo is not None else None,
                            quote=QUOTE, contract="PERPETUAL", cls=cls, name_hint=name, coingecko_id=cg,
                            url=PAGE_URL.format(front=quote(front, safe="."))))
        return out

    def funding_intervals(self) -> dict[str, int]:
        """Declared intervals of the live perps without names (hook of venues.funding_intervals). 1 h on all [L]."""
        self._markets("aux")
        return {s: v["interval_h"] for s, v in self._h.markets.items()}

    def premium(self) -> dict[str, dict]:
        """symbol → {rate (predicted rate of the interval in progress, fraction per interval = per hour), mark, index,
        next_ms, ts_ms, obs, interval_h}. obs = arrival minus the CDN Age: a cached copy is not a new observation."""
        body, r = self._request("/markPrices", kind="tick")
        t = time.time()
        rows = self._rows(body, "/markPrices")
        hd = {str(k).lower(): v for k, v in (getattr(r, "headers", None) or {}).items()}
        age = _f(hd.get("age"))
        obs = t - age if age is not None and 0 <= age <= CDN_AGE_MAX_S else t
        mk = self._h.markets
        now_ms = int(t * 1000)
        out = {}
        for x in rows:
            sym = str(x.get("symbol") or "")
            if not sym or (sym not in mk if mk else not sym.endswith("_PERP")):
                continue                                   # PostOnly / closed / not yet in the universe
            rate = _f(x.get("fundingRate"))
            if rate is None:
                continue
            iv = (mk.get(sym) or {}).get("interval_h") or INTERVAL_H
            out[sym] = dict(rate=rate, mark=_f(x.get("markPrice")), index=_f(x.get("indexPrice")),
                            next_ms=_roll(_int(x.get("nextFundingTimestamp"), None), iv, now_ms),
                            ts_ms=int(obs * 1000), obs=obs, interval_h=iv)
        if out:
            self._h.seen = sorted(out)
        return out

    def books(self) -> dict[str, dict]:
        """Best bid / ask of every live perp: the WS bookTicker cache, and for a market the change-only stream has not
        priced yet — REST /depth (≤ DEPTH_PER_CALL per call, each market ≤ once per REST_BOOK_TTL_S). Quantities in the
        market's unit. A market with neither has no book this tick — the collector prices it by mark."""
        syms = list(self._h.markets) or list(self._h.seen)
        ws = self._ensure_stream(syms).books(set(syms) if syms else None)
        missing = [s for s in syms if s not in ws]
        self._fill_rest(missing)
        with self._lock:
            rest = {s: dict(e["book"]) for s, e in self._rest_books.items() if s in missing and e.get("book")}
        return {**rest, **ws}

    def _fill_rest(self, missing: list[str]):
        if not missing:
            return
        t0 = time.time()
        with self._lock:
            have = {s: self._rest_books.get(s, {}).get("t", 0.0) for s in missing}
        due = sorted((t, s) for s, t in have.items() if t0 - t >= REST_BOOK_TTL_S)[:DEPTH_PER_CALL]
        for _, s in due:
            if time.time() - t0 > DEPTH_BUDGET_S:
                break
            try:
                body = self._get("/depth", {"symbol": s, "limit": 5}, kind="tick", timeout=DEPTH_TIMEOUT_S)
                b = depth_book(body, time.time())
            except (BannedError, BudgetExceeded):
                break
            except PermanentHTTPError as e:
                log.warning("%s depth %s: %s", self.name, s, e)
                b = None
            except Exception as e:  # noqa — network: the rest would wait too; the next call retries
                log.warning("%s depth %s: %s: %s", self.name, s, type(e).__name__, e)
                with self._lock:
                    self._rest_books[s] = dict(self._rest_books.get(s) or {}, t=time.time())
                break
            with self._lock:
                self._rest_books[s] = dict(t=time.time(), book=b)

    def _ensure_stream(self, syms: list[str]) -> "_BookStream":
        if self._stream is None:
            self._stream = _BookStream(self.ws_url, self.name, self._connect)
        if syms:
            self._stream.want(syms)
        self._stream.start()
        self._stream.wait_first(WS_FIRST_WAIT_S)
        return self._stream

    def _market(self, symbol: str) -> dict:
        h = self._h
        if symbol not in h.markets and time.time() - h.markets_ts >= MARKETS_TTL_S:
            self._markets("aux")                           # a fresh client (background copy) learns the list once per host
        if symbol not in h.markets:
            raise RuntimeError(f"{self.name}: no market {symbol}")
        return h.markets[symbol]

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled rates of one market in [start_ms, min(end_ms, now − SETTLE_GRACE_S)], ascending. The endpoint has no
        time filter and answers newest first, so the page size is the hours back to start_ms (+ the in-progress row and a
        spare: a 2-hour top-up asks for 4 rows, 30 days for 722 — one call) and paging goes on by offset until a short
        page or start_ms. A settlement landing between two page calls shifts offsets by one row — a duplicate at the
        boundary (dropped), never a gap. Rows newer than the grace are the in-progress / not-yet-final interval (header,
        PITFALL 1). An empty answer for a market that must have settled raises (PITFALL 2)."""
        info = self._market(symbol)
        now_ms = int(time.time() * 1000)
        start_ms = int(start_ms)
        end_ms = int(end_ms or now_ms)
        hi = min(end_ms, now_ms - config.SETTLE_GRACE_S * 1000)
        size = max(1, min(HISTORY_PAGE, int(math.ceil(max(0, now_ms - start_ms) / HOUR_MS)) + HISTORY_SLACK))
        got: dict[int, dict] = {}
        oldest, n_rows = None, 0
        for page in range(HISTORY_MAX_PAGES):
            rows = self._rows(self._get("/fundingRates", {"symbol": symbol, "limit": size, "offset": page * size},
                                        kind="hist"), "/fundingRates")
            n_rows += len(rows)
            for r in rows:
                if r.get("symbol") not in (None, symbol):
                    continue
                ms = iso_ms(r.get("intervalEndTimestamp"))
                if ms is None:
                    continue
                oldest = ms if oldest is None else min(oldest, ms)
                rate = _f(r.get("fundingRate"))
                if rate is None or not (start_ms <= ms <= hi):
                    continue
                got.setdefault(ms, dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=None))
            if len(rows) < size or (oldest is not None and oldest <= start_ms):
                break
        else:
            raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {HISTORY_MAX_PAGES} pages of {size}")
        if not n_rows:
            created = int(info.get("onboard_ms") or 0)
            first = (created // HOUR_MS + 1) * HOUR_MS if created else 0
            if not created or now_ms > first + config.SETTLE_GRACE_S * 1000:
                raise RuntimeError(f"{self.name} {symbol}: empty history for a market listed at {created} — not confirmed")
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """No «settled rates of all markets» endpoint (/fundingRates needs symbol [L]). Upkeep is the per-leg top-up by
        completeness (89 legs × 0.5 s ≈ 45 s after each hour)."""
        return []

    def close(self):
        if self._stream is not None:
            self._stream.stop()


PERP_CLIENTS = {NAME: BackpackFut}


# --- WebSocket: bookTicker per symbol on one connection ----------------------------------------------------------------
def _subscribe(symbols) -> str:
    return json.dumps({"method": "SUBSCRIBE", "params": [f"bookTicker.{s}" for s in symbols]})


class _BookStream:
    """One connection in a daemon thread: bookTicker.<symbol> for every wanted market → cache {symbol: {bid, ask, bid_qty,
    ask_qty, rx, conn, T}}; new markets are subscribed on the live connection, all of them after a reconnect; silence
    watchdog; reconnect with backoff. The server pings and lighter._WS answers — no client keepalive. The cache is kept
    across reconnects (change-only, there is no snapshot to rebuild it)."""

    def __init__(self, url: str, name: str, connect=None):
        self.url, self.name = url, name
        self._connect = connect or _WS
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._want: set[str] = set()
        self.last_rx = 0.0                                # last bookTicker frame of the latest connection
        self.conn_id = 0                                  # number of the latest connection (0 = none yet)
        self.synced = threading.Event()                   # market data arrived on the current connection
        self.n_conn = 0
        self.err: str | None = None
        self.backoff0 = 1.0
        self._waited = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def want(self, symbols):
        s = {str(x) for x in symbols if x}
        if s:
            with self._lock:
                self._want = s

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"ws-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def wait_first(self, timeout: float):
        if not self._waited:
            self._waited = True
            self.synced.wait(timeout)

    def apply(self, msg: dict, rx: float) -> bool:
        """One JSON message → cache. True for a bookTicker frame; acks and errors are not market data. A frame with an
        older engine time T than the cached one never wins."""
        d = msg.get("data")
        if not str(msg.get("stream") or "").startswith("bookTicker.") or not isinstance(d, dict) or not d.get("s"):
            if msg.get("error"):
                log.warning("%s ws: %s", self.name, str(msg)[:200])
            return False
        sym = str(d["s"])
        t = _int(d.get("T"), None)
        with self._lock:
            old = self._data.get(sym)
            if not (old and t is not None and old.get("T") is not None and t < old["T"]):
                self._data[sym] = dict(bid=d.get("b"), ask=d.get("a"), bid_qty=d.get("B"), ask_qty=d.get("A"), rx=rx,
                                       conn=self.conn_id, T=t)
            self.last_rx = rx
        return True

    def books(self, symbols: set[str] | None = None) -> dict[str, dict]:
        """obs = the latest connection's last frame for books updated on it (a quiet market is unchanged — the stream is
        change-only); a book left from an earlier connection carries its own last update (a change missed during the
        reconnect gap must not look fresh)."""
        with self._lock:
            data, last, cur = dict(self._data), self.last_rx, self.conn_id
        out = {}
        for sym, e in data.items():
            if symbols is not None and sym not in symbols:
                continue
            b = _book(e.get("bid"), e.get("ask"), e.get("bid_qty"), e.get("ask_qty"))
            if b is not None:
                b["obs"] = last if e.get("conn") == cur else e.get("rx", 0.0)
                out[sym] = b
        return out

    def _session_once(self) -> bool:
        """One connection until it fails. True if market data arrived (a healthy session resets the backoff)."""
        conn, healthy = None, False
        try:
            conn = self._connect(self.url, WS_RECV_TIMEOUT_S)
            with self._lock:
                self.n_conn += 1
                self.conn_id = self.n_conn
            subbed: set[str] = set()
            last = time.time()
            while not self._stop.is_set():
                with self._lock:
                    new = sorted(self._want - subbed)
                for i in range(0, len(new), WS_SUB_CHUNK):
                    conn.send_text(_subscribe(new[i:i + WS_SUB_CHUNK]))
                subbed.update(new)
                try:
                    op, payload = conn.recv()
                except socket.timeout:
                    op, payload = None, b""
                now = time.time()
                if op == 0x8:
                    raise ConnectionError("closed by server")
                if op == 0x1:
                    try:
                        msg = json.loads(payload)
                    except ValueError:
                        msg = None
                    if isinstance(msg, dict) and self.apply(msg, now):
                        last = now
                        if not healthy:
                            healthy = True
                            self.synced.set()
                if now - last > WS_SILENT_S:
                    raise TimeoutError(f"no market data for {now - last:.0f} s")
        except Exception as e:  # noqa — any failure: reconnect
            self.err = f"{type(e).__name__}: {e}"
            log.warning("%s ws: %s — reconnecting", self.name, self.err)
        finally:
            self.synced.clear()
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa
                    pass
        return healthy

    def _run(self, max_sessions: int | None = None):
        backoff, n = self.backoff0, 0
        while not self._stop.is_set():
            healthy = self._session_once()
            n += 1
            if max_sessions is not None and n >= max_sessions:
                break
            if healthy:
                backoff = self.backoff0
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2 or self.backoff0, WS_BACKOFF_MAX_S)
