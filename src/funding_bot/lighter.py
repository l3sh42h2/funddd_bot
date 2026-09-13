"""Lighter — two independent venues with one API shape (owner 12.09: «lighter нужны 2 вида: мейн и robinhood, оба спот
и фьюч»). One code path, four clients: perps «lighter» (MAIN) and «lighter_rh» (Robinhood), spots «lighter_spot» and
«lighter_rh_spot». Perp clients implement the native perp interface of venues.py (perp_instruments / premium / books /
history_since / recent_history); spot clients implement spot_instruments / books and coin_blob (identity_src picks it
up by hasattr). Public endpoints only, no keys.

Measured 12.09.2026 from the Mac. Tags: [L] verified live, [D] verified in docs, [A] assumption.
Instances [L]: MAIN REST https://mainnet.zklighter.elliot.ai/api/v1, WS …/stream?readonly=true, quote USDC, assets on
  Ethereum. RH REST https://api.rh.lighter.xyz/api/v1, same paths, quote USDG, assets on Robinhood Chain (4663). Same
  tickers and market ids mean DIFFERENT markets on each instance — everything is keyed by venue name.
/orderBookDetails?filter=perp|spot [L]: universe + mark/index (strings), status, market_config.force_reduce_only,
  price/size decimals, min_quote_amount, created_at (ms), funding_clamp_big (percent per 8 h). No bid/ask, no funding.
  ~300 KB, not gzipped.
/funding-rates [L]: Lighter's own current estimate only in rows with exchange=="lighter" — rows of binance/bybit/
  hyperliquid are mixed in under the SAME market_id. Signed FRACTION PER 8 H: per-hour rate = rate/8 (equalled WS
  current_funding_rate %/h on 217/217 MAIN and 57/57 RH markets).
/fundings [L]: market_id required (no all-markets batch → recent_history() is []), resolution 1h, timestamps in
  SECONDS on the hour, at most 750 rows = the NEWEST in the window (30 d = 720 rows fit one call; older data is paged
  backwards). rate is PERCENT and UNSIGNED — direction long → +, short → −. value = USD per base unit ≈ rate% × index,
  so mark ≈ value / (rate/100). count_back > 0 pulls rows before start, so count_back=0 and rows are filtered.
/tokenlist [L]: class of every market (asset_type CRYPTO/RWA, categories STOCK/ETF/FX/COMMODITIES/PRE_IPO/BONDS/KRW …);
  keyed by backend_symbol when present (kPEPE → 1000PEPE, SPY/USDC → rhSPY/USDC).
/assetDetails [L]: l1_address of every asset (MAIN: Ethereum, RH: Robinhood Chain) and multiplier — contract evidence
  for identity.py without prices.
Books [L]: only the WS channels market_stats/all and spot_market_stats/all carry best bid/ask for all markets. The first
  frame («subscribed/…») is the full snapshot, then «update/…» deltas; both are {market_id: stats}. Empty side = ''.
  Measured on the 10.09 capture: 58 of 233 MAIN perps had gaps > 60 s between updates within 10 min — a delta stream
  is silent for a market that does not change. So a book's obs is the CONNECTION's last market-data frame, not the
  market's own last update; a connection without market data for WS_SILENT_S is dropped and re-opened (a peer that dies
  silently shows up here, and its books go stale by obs). Keepalive: the server closes after 2 min without client
  frames [D] — we send {"type":"ping"} every WS_PING_S. The venv has no websocket library and pyproject is not ours:
  a minimal RFC 6455 client on socket + ssl (+ certifi) lives below.
Limits [D]: standard/unauthenticated 60 requests per rolling minute per IP; 429 (or 405) → firewall cooldown 60 s.
  Each instance documents its own base URL — treated as separate buckets [A]. No rate headers [L]. Budget: tick 2 calls
  per 10 s (12/min) + history ≥ 1.5 s apart and ≤ HIST_CAP in the window (≤ 40/min) + hourly universe. Pacing and the
  429 pause are per host and shared by every client of that host in the process (perp, spot and the collector's
  background copies from make_clients()).
Fees [D]: standard account 0 % maker / 0 % taker on both instances (API taker_fee "0.0000" [L]) — config.FEES_TAKER.
Classes: crypto / equity (stocks AND ETFs — SLV, USO, SGOV are ETF shares, not the metal or oil) / commodity / fx /
  index / preipo; PAXG and XAUT are coins (repo rule); a market missing from the tokenlist falls back on
  funding_premium_multiplier (100 crypto, 1 pre-IPO [D]) and otherwise gets «rwa» — a class that pairs with nothing,
  so an unclassified real-world asset never forms a wrong pair.
Bases aligned with the other venues by definition, not by price: FX pairs → the non-USD currency like Hyperliquid
  (EURUSD → EUR, USDJPY → JPY — HL xyz:JPY is quoted as USDJPY too); KRW stocks SKHYNIXUSD/SAMSUNGUSD/HYUNDAIUSD →
  SKHYNIX/SAMSUNG/HYUNDAI (SKHY is the ADR — a different instrument, kept); WTI → CL (CL is the WTI crude contract);
  US500 → SP500 (S&P 500 index, as HL xyz:SP500). US100 is NOT mapped to HL XYZ100 — not provable by definition [A].
Excluded [A]: status != active and force_reduce_only markets (cannot be opened; 17 MAIN perps on 12.09), hidden ones.
Pitfalls: AI = Artificial Inu, LIT = Lighter (Binance LIT = Litentry), QNT = tokenlist RWA/STOCK (reduce-only anyway).
"""
from __future__ import annotations
import base64, hashlib, json, logging, os, re, socket, ssl, struct, threading, time
from collections import deque
from urllib.parse import urlsplit
import requests
from . import config, identity
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

HOUR_MS = 3600_000
HISTORY_PAGE = 750               # /fundings returns at most this many rows — the newest in the window [D/L]
HISTORY_MAX_PAGES = 12           # 12 × 750 h ≈ a year; 30 days need one page
HISTORY_GAP_S = 1.5              # between history calls (config.FUNDING_HISTORY_MIN_GAP_S overrides by venue name)
REQ_PER_MIN = 60                 # doc: standard / unauthenticated, per rolling minute per IP [D]
TICK_CAP = 54                    # tick and hourly calls are refused (BudgetExceeded) above this many in the last 60 s
HIST_CAP = 40                    # history waits while the window holds this many: 14 slots stay for the tick
HIST_MAX_WAIT_S = 90.0           # history gives up (BudgetExceeded → venue skipped this pass) after waiting this long
AUX_MAX_WAIT_S = 30.0            # hourly calls (universe, tokenlist, coins) wait at most this long for a slot
BAN_DEFAULT_S = 60.0             # doc: firewall cooldown 60 s; Retry-After is honoured when present
TOKENS_TTL_S = 600               # tokenlist is shared by universe and coin list within this age
TOKENS_STALE_OK_S = 86400        # tokenlist down: a cached copy up to a day old still classifies markets
IDS_TTL_S = 60                   # unknown symbol in history → the market-id map is refreshed at most this often
SPREAD_MAX = 0.05                # [A] a book wider than 5 % of mid is not a price (12.09: ORCL/USDG ask 160804.75)
WS_RECV_TIMEOUT_S = 5.0
WS_SILENT_S = 30.0               # no market data on the connection for this long → reconnect
WS_PING_S = 60.0                 # client frame at least every 2 min or the server closes [D]
WS_BACKOFF_MAX_S = 30.0
WS_FIRST_WAIT_S = 3.0            # the very first books() call waits this long for the snapshot (then never blocks)
WS_MAX_MSG = 16 << 20

INSTANCES = {
    "main": dict(perp="lighter", spot="lighter_spot", rest="https://mainnet.zklighter.elliot.ai/api/v1",
                 ws="wss://mainnet.zklighter.elliot.ai/stream?readonly=true", quote="USDC", chain="eth", contract="main",
                 # route /trade/:symbol is in the app bundle [L]; spot = front symbol with «/» → «_» (bundle) [L];
                 # perp front symbol (kPEPE vs 1000PEPE) [A]. The app never 404s — links only from the current list.
                 url="https://app.lighter.xyz/trade/{symbol}"),
    "rh": dict(perp="lighter_rh", spot="lighter_rh_spot", rest="https://api.rh.lighter.xyz/api/v1",
               ws="wss://api.rh.lighter.xyz/stream?readonly=true", quote="USDG", chain="robinhood", contract="rh",
               url=None),        # no public per-market page found [A]; app.lighter.xyz would show the MAIN market
}

# --- classes and bases ------------------------------------------------------------------------------------------------
_CLS_OVERRIDE = {"USDHKD": "fx",                     # tokenlist tags it CRYPTO/NEW [L]
                 "PAXG": "crypto", "XAUT": "crypto",  # gold tokens are coins (repo rule); tokenlist: RWA/COMMODITIES
                 "US500": "index", "US100": "index"}  # tagged ETF/MAJOR exactly like SPY [L] — they are indices
_FPM_CLASS = {100: "crypto", 1: "preipo"}             # funding_premium_multiplier: 1 crypto, ½ RWA, 1/100 pre-IPO [D]
_FX = re.compile(r"^(?:USD([A-Z]{3})|([A-Z]{3})USD)$")
LOCAL_CANON = {"commodity": {"WTI": "CL"}, "index": {"US500": "SP500"}}


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


def _pow10(p):
    try:
        return 10.0 ** -int(p)
    except (TypeError, ValueError):
        return None


def _cats(token: dict | None) -> set[str]:
    return {str(c).upper() for c in (token or {}).get("categories") or []}


def _tmap(tokens: list[dict], market: str) -> dict[str, dict]:
    """tokenlist → {API symbol: token}. The API symbol is backend_symbol when present (kPEPE → 1000PEPE)."""
    return {str(t.get("backend_symbol") or t.get("symbol")): t for t in tokens
            if isinstance(t, dict) and t.get("market") == market and (t.get("backend_symbol") or t.get("symbol"))}


def perp_class(symbol: str, token: dict | None, fpm=None) -> str:
    s = symbol.upper()
    if s in _CLS_OVERRIDE:
        return _CLS_OVERRIDE[s]
    if token is None:
        try:
            return _FPM_CLASS.get(int(float(fpm)), "rwa")
        except (TypeError, ValueError):
            return "rwa"
    if str(token.get("asset_type") or "").upper() != "RWA":
        return "crypto"
    c = _cats(token)
    if "PRE_IPO" in c:
        return "preipo"
    if "FX" in c:
        return "fx"
    if "BONDS" in c:
        return "index"
    if "COMMODITIES" in c and not c & {"ETF", "STOCK"}:
        return "commodity"                               # SLV / USO are ETF shares → equity below
    if c & {"STOCK", "ETF"}:
        return "equity"
    if "COMPUTE" in c:
        return "index"                                   # H100 — GPU rental price index
    return "equity"                                      # RWA tagged only NEW (AAOI) — a stock


def perp_base(symbol: str, cls: str, token: dict | None = None) -> tuple[str, float]:
    base, factor = norm_symbol_factor(symbol)            # 1000PEPE → PEPE ×1000
    if cls == "fx":
        m = _FX.match(base)
        if m:
            base = m.group(1) or m.group(2)              # EURUSD → EUR, USDJPY → JPY (as Hyperliquid xyz:EUR, xyz:JPY)
    elif cls == "equity" and "KRW" in _cats(token) and base.endswith("USD") and len(base) > 3:
        base = base[:-3]                                 # SKHYNIXUSD → SKHYNIX
    base = LOCAL_CANON.get(cls, {}).get(base, base)
    return config.PERP_CANON.get(cls, {}).get(base, base), factor


def spot_stock(base_asset: str, token: dict | None) -> str | None:
    """Ticker of the share behind a tokenized-stock spot; '' — pre-IPO (not taken); None — a coin. By tokenlist class
    (RWA + STOCK/ETF), never by price. XAUT/USDC is RWA/NEW without STOCK — a coin."""
    if token is not None:
        c = _cats(token)
        if str(token.get("asset_type") or "").upper() != "RWA" or not c & {"STOCK", "ETF"}:
            return None
        if "PRE_IPO" in c:
            return ""
        front = str(token.get("symbol") or "").split("/")[0]          # SPY/USDC for rhSPY/USDC; AAPL/USDG
        return (front or base_asset).upper()
    m = re.match(r"^rh([A-Z][A-Z0-9.]*)$", base_asset)                # rhSPY without a tokenlist entry [A]
    return m.group(1) if m else None


def _book(bid, ask) -> dict | None:
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=0.0, ask_qty=0.0)


def _ts_ms(x) -> int | None:
    try:
        v = int(float(x))
    except (TypeError, ValueError):
        return None
    return v * 1000 if v < 10 ** 11 else v                              # seconds [L]; milliseconds tolerated


def _settled(r: dict) -> tuple[float, float | None] | None:
    """/fundings row → (signed fraction per hour, mark at settlement). rate is unsigned percent, direction is the sign."""
    rate = _f(r.get("rate"))
    if rate is None:
        return None
    d = str(r.get("direction") or "").lower()
    if d == "short":
        sign = -1.0
    elif d == "long" or rate == 0:
        sign = 1.0
    else:
        return None                                      # a non-zero rate without a side cannot be signed
    frac = sign * abs(rate) / 100.0
    val = _f(r.get("value"))
    mark = abs(val / (rate / 100.0)) if val and rate else None
    return frac, mark


# --- per-host state ---------------------------------------------------------------------------------------------------
class _Host:
    """State of one REST host shared by every client of it in this process: the limit is per IP."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()
        self.banned_until = 0.0
        self.last_hist = 0.0
        self.tokens: list[dict] | None = None
        self.tokens_ts = 0.0
        self.perp_ids: dict[str, int] = {}
        self.perp_syms: dict[int, str] = {}
        self.perp_ids_ts = 0.0

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


# --- clients ----------------------------------------------------------------------------------------------------------
class _Lighter:
    instance = "main"
    kind = ""
    CHANNEL = ""
    WS_KEY = ""

    def __init__(self, session: requests.Session | None = None, history_gap_s: float | None = None, connect=None):
        cfg = INSTANCES[self.instance]
        self._cfg = cfg
        self.name = cfg[self.kind]
        self.rest, self.ws_url, self.quote = cfg["rest"], cfg["ws"], cfg["quote"]
        self.chain, self.contract = cfg["chain"], cfg["contract"]
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._h = _host(self.rest)
        self.history_gap_s = (config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, HISTORY_GAP_S)
                              if history_gap_s is None else history_gap_s)
        self._connect = connect
        self._stream: _Stream | None = None
        self.used_weight = 0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0

    # --- budget and health ------------------------------------------------------------------------------------
    @property
    def banned_until(self) -> float:
        return self._h.banned_until

    def budget_used(self) -> float:
        return self._h.used(time.time()) / float(REQ_PER_MIN)

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self._h.banned_until        # the window itself is guarded inside _get (pacing)

    def health(self) -> dict:
        self.used_weight = self._h.used(time.time())
        return {"exchange": self.name, "used_weight": self.used_weight, "budget": round(self.used_weight / REQ_PER_MIN, 3),
                "last_ok_ts": int(self.last_ok_ts), "n_429": self.n_429, "n_err": self.n_err,
                "banned_until": int(self._h.banned_until)}

    # --- transport -------------------------------------------------------------------------------------------------
    def _acquire(self, kind: str):
        """A slot in the host's rolling minute. tick — now or BudgetExceeded (the next tick retries); hist — waits for
        its gap and for the window to drop below HIST_CAP; aux — waits for a slot below TICK_CAP."""
        h = self._h
        cap = HIST_CAP if kind == "hist" else TICK_CAP
        t_end = time.time() + (0.0 if kind == "tick" else HIST_MAX_WAIT_S if kind == "hist" else AUX_MAX_WAIT_S)
        while True:
            now = time.time()
            if now < h.banned_until:
                raise BannedError(f"{self.name}: pause after 429 until {time.strftime('%H:%M:%S', time.gmtime(h.banned_until))}")
            with h.lock:
                while h.calls and now - h.calls[0] >= 60.0:
                    h.calls.popleft()
                n = len(h.calls)
                wait = h.calls[n - cap] + 60.0 - now if n >= cap else 0.0
                if kind == "hist":
                    wait = max(wait, h.last_hist + self.history_gap_s - now)
                if wait <= 0:
                    h.calls.append(now)
                    if kind == "hist":
                        h.last_hist = now
                    return
            if now + wait > t_end:
                raise BudgetExceeded(f"{self.name}: {n} requests in the last 60 s (limit {REQ_PER_MIN}), {kind} call skipped")
            time.sleep(min(wait, 5.0))

    def _throttle(self, r, path: str):
        try:
            pause = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError):
            pause = BAN_DEFAULT_S
        pause = max(1.0, pause)
        with self._h.lock:
            self._h.banned_until = max(self._h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s %d on %s — host paused for %.0f s", self.name, r.status_code, path, pause)
        raise BannedError(f"{self.name}: {r.status_code} on {path}, pause {pause:.0f} s")

    def _get(self, path: str, params: dict | None = None, kind: str = "aux", retries: int | None = None,
             timeout: float | None = None) -> dict:
        retries = (config.TICK_RETRIES if kind == "tick" else 3) if retries is None else retries
        timeout = (config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT) if timeout is None else timeout
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            try:
                r = self._s.get(self.rest + path, params=params, timeout=timeout)
                if r.status_code in (429, 405):           # doc: rate limit answers 429 or 405
                    self._throttle(r, path)
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path}: {r.text[:200]}")
                r.raise_for_status()
                body = r.json()
                if not isinstance(body, dict) or _int(body.get("code", 200)) != 200:
                    raise RuntimeError(f"{path}: body code {body.get('code') if isinstance(body, dict) else '?'}: "
                                       f"{str(body)[:200]}")
                self.last_ok_ts = time.time()
                return body
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON, foreign body code — retried
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    # --- shared data -----------------------------------------------------------------------------------------------
    def _tokens(self) -> list[dict]:
        h, now = self._h, time.time()
        if h.tokens is not None and now - h.tokens_ts < TOKENS_TTL_S:
            return h.tokens
        try:
            toks = [t for t in (self._get("/tokenlist").get("tokens") or []) if isinstance(t, dict)]
            if not toks:
                raise RuntimeError("tokenlist: empty")
        except Exception as e:  # noqa — a recent copy still classifies markets; none — the universe step fails
            if h.tokens is not None and now - h.tokens_ts < TOKENS_STALE_OK_S:
                log.warning("%s tokenlist: %s: %s — using the copy from %.0f min ago", self.name, type(e).__name__, e,
                            (now - h.tokens_ts) / 60)
                return h.tokens
            raise
        h.tokens, h.tokens_ts = toks, now
        return toks

    def _url(self, token: dict | None, symbol: str) -> str | None:
        tmpl = self._cfg["url"]
        if not tmpl:
            return None
        return tmpl.format(symbol=str((token or {}).get("symbol") or symbol).replace("/", "_"))

    # --- books from the stream -----------------------------------------------------------------------------------------
    def _ensure_stream(self) -> "_Stream":
        if self._stream is None:
            self._stream = _Stream(self.ws_url, self.CHANNEL, self.WS_KEY, self.name, self._connect)
        self._stream.start()
        self._stream.wait_first(WS_FIRST_WAIT_S)
        return self._stream

    def _stream_books(self, ids: dict[int, str]) -> dict[str, dict]:
        """Best bid/ask of every market from the WS cache. obs = the connection's last market-data frame (see header)."""
        data, last = self._ensure_stream().snapshot()
        out = {}
        for mid, e in data.items():
            sym = ids.get(mid) if ids else e.get("symbol")
            if not sym:
                continue
            b = _book(e.get("bid"), e.get("ask"))
            if b is not None:
                b["obs"] = last
                out[sym] = b
        return out

    def close(self):
        if self._stream is not None:
            self._stream.stop()


class _LighterPerp(_Lighter):
    kind = "perp"
    CHANNEL = "market_stats/all"
    WS_KEY = "market_stats"

    def _remember_ids(self, details: list[dict]):
        ids, syms = {}, {}
        for m in details:
            try:
                mid, sym = int(m["market_id"]), str(m["symbol"])
            except (KeyError, TypeError, ValueError):
                continue
            syms[mid] = sym
            if sym not in ids or m.get("status") == "active":
                ids[sym] = mid
        if ids:
            h = self._h
            with h.lock:
                h.perp_ids, h.perp_syms, h.perp_ids_ts = ids, syms, time.time()

    def _details(self, kind: str) -> list[dict]:
        ob = self._get("/orderBookDetails", {"filter": "perp"}, kind=kind)
        det = [m for m in ob.get("order_book_details") or [] if isinstance(m, dict)]
        if not det:
            raise RuntimeError(f"{self.name}: orderBookDetails without perps")
        self._remember_ids(det)
        return det

    def perp_instruments(self) -> list[dict]:
        det = self._details("aux")
        tmap = _tmap(self._tokens(), "PERPS")
        out = []
        for m in det:
            sym = str(m.get("symbol") or "")
            mc = m.get("market_config") or {}
            if not sym or m.get("status") != "active" or str(m.get("market_type") or "perp") != "perp":
                continue
            if mc.get("force_reduce_only") or mc.get("hidden"):
                continue                                  # cannot be opened / not listed in the app [A]
            tok = tmap.get(sym)
            cls = perp_class(sym, tok, m.get("funding_premium_multiplier"))
            base, factor = perp_base(sym, cls, tok)
            big = _f(m.get("funding_clamp_big"))          # percent per 8 h [D] → fraction per hour
            cap = big / 100.0 / 8.0 if big else None
            mult = _f(m.get("multiplier"))
            if mult is not None and abs(mult - 1.0) > 1e-9:
                log.warning("%s %s: multiplier %s ≠ 1 — unit not verified", self.name, sym, m.get("multiplier"))
            out.append(dict(exchange=self.name, symbol=sym, base_asset=sym, base=base, factor=factor,
                            tick_size=_pow10(m.get("price_decimals")), step_size=_pow10(m.get("size_decimals")),
                            min_notional=_f(m.get("min_quote_amount")), onboard_ms=_int(m.get("created_at")),
                            interval_h=1, cap=cap, floor=-cap if cap else None, quote=self.quote,
                            contract=self.contract, cls=cls, market_id=int(m["market_id"]),
                            name_hint=(tok or {}).get("name"), url=self._url(tok, sym)))
        return out

    def premium(self) -> dict[str, dict]:
        fr = self._get("/funding-rates", kind="tick")
        t = time.time()
        det = self._details("tick")
        rates: dict[int, float] = {}
        for r in fr.get("funding_rates") or []:
            if isinstance(r, dict) and r.get("exchange") == "lighter":
                v = _f(r.get("rate"))
                try:
                    if v is not None:
                        rates[int(r["market_id"])] = v
                except (KeyError, TypeError, ValueError):
                    pass
        now_ms = int(t * 1000)
        nxt = (now_ms // HOUR_MS + 1) * HOUR_MS           # settlement every hour on the hour [D/L]
        out = {}
        for m in det:
            if m.get("status") != "active":
                continue
            v = rates.get(_int(m.get("market_id")))
            if v is None:
                continue
            out[str(m["symbol"])] = dict(rate=v / 8.0, mark=_f(m.get("mark_price")), index=_f(m.get("index_price")),
                                         next_ms=nxt, ts_ms=now_ms, obs=t, interval_h=1)
        return out

    def books(self) -> dict[str, dict]:
        return self._stream_books(dict(self._h.perp_syms))

    def _market_id(self, symbol: str) -> int:
        h = self._h
        mid = h.perp_ids.get(symbol)
        if mid is None and time.time() - h.perp_ids_ts >= IDS_TTL_S:
            self._details("hist")                         # a fresh client (background copy) learns ids once per host
            mid = h.perp_ids.get(symbol)
        if mid is None:
            raise RuntimeError(f"{self.name}: no market {symbol}")
        return mid

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled hourly rates of one market in [start_ms, end_ms], ascending. One call covers 750 h; older windows
        are paged backwards from the oldest row received."""
        mid = self._market_id(symbol)
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        got: dict[int, dict] = {}
        hi = end_ms
        for _ in range(HISTORY_MAX_PAGES):
            body = self._get("/fundings", dict(market_id=mid, resolution="1h", start_timestamp=start_ms // 1000,
                                               end_timestamp=hi // 1000, count_back=0), kind="hist")
            rows = [r for r in body.get("fundings") or [] if isinstance(r, dict)]
            stamps = []
            for r in rows:
                ms = _ts_ms(r.get("timestamp"))
                if ms is None:
                    continue
                stamps.append(ms)
                if not (start_ms <= ms <= end_ms):
                    continue
                ev = _settled(r)
                if ev is not None:
                    got[ms] = dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=ev[0], mark=ev[1])
            if len(rows) < HISTORY_PAGE or not stamps:
                break
            first = min(stamps)
            if first <= start_ms:
                break
            hi = first - 1000
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """No all-markets batch (/fundings needs market_id [L]). Upkeep is the per-leg top-up by completeness."""
        return []


class _LighterSpot(_Lighter):
    kind = "spot"
    CHANNEL = "spot_market_stats/all"
    WS_KEY = "spot_market_stats"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._spot_ids: dict[int, str] = {}

    def _details(self) -> list[dict]:
        ob = self._get("/orderBookDetails", {"filter": "spot"})
        det = [m for m in ob.get("spot_order_book_details") or [] if isinstance(m, dict)]
        if not det:
            raise RuntimeError(f"{self.name}: orderBookDetails without spot markets")
        return det

    def spot_instruments(self) -> list[dict]:
        det = self._details()
        tmap = _tmap(self._tokens(), "SPOT")
        out, ids = [], {}
        for m in det:
            sym = str(m.get("symbol") or "")
            if "/" not in sym or m.get("status") != "active":
                continue
            base_asset, quote = sym.split("/", 1)
            if quote.upper() != self.quote:
                continue
            tok = tmap.get(sym)
            stock = spot_stock(base_asset, tok)
            if stock == "":
                continue                                  # pre-IPO: units are not shares
            if stock:
                # own base «~AAPL» never meets a coin; the share ticker goes to alt_base (only an equity perp pairs).
                # multiplier (AAPL 1.000566, SGOV 1.0051 [L]) is carried as shares per token [A]
                base, factor, alt = "~" + base_asset.upper(), _f(m.get("multiplier")) or 1.0, stock
            else:
                (base, factor), alt = norm_symbol_factor(base_asset), None
            mid = _int(m.get("market_id"))
            ids[mid] = sym
            out.append(dict(exchange=self.name, symbol=sym, base_asset=base_asset, base=base, factor=factor,
                            tick_size=_pow10(m.get("price_decimals")), step_size=_pow10(m.get("size_decimals")),
                            min_notional=_f(m.get("min_quote_amount")), onboard_ms=_int(m.get("created_at")),
                            alt_base=alt, quote=quote, market_id=mid, contract=self.contract,
                            name_hint=(tok or {}).get("name"), url=self._url(tok, sym)))
        self._spot_ids = ids
        return out

    def books(self) -> dict[str, dict]:
        return self._stream_books(dict(self._spot_ids))

    def coin_blob(self) -> dict:
        """identity_src.coin_blob contract: {"coins": {code: record}, "markets": {symbol: [code, trading]}, "shares",
        "need_n"}. Contracts from /assetDetails (MAIN: Ethereum, RH: Robinhood Chain), names from the tokenlist."""
        ad = self._get("/assetDetails")
        det = self._details()
        tmap = _tmap(self._tokens(), "SPOT")
        names: dict[str, str] = {}
        for key, t in tmap.items():
            code = key.split("/")[0]
            if t.get("name") and ("/" in key or code not in names):
                names[code] = t["name"]                   # the pair's name wins: LINK/USDC → «Chainlink»
        coins, mult = {}, {}
        for a in ad.get("asset_details") or []:
            code = str((a or {}).get("symbol") or "")
            if not code:
                continue
            addr = str(a.get("l1_address") or "")
            if re.fullmatch(r"0x0+", addr):
                addr = ""                                 # the chain's native coin (ETH)
            coins[code] = identity.coin_record(names.get(code), [(self.chain, addr, True, True)], code)
            mult[code] = _f(a.get("multiplier"))
        markets, shares, need = {}, {}, []
        for m in det:
            sym = str(m.get("symbol") or "")
            if "/" not in sym:
                continue
            code = sym.split("/", 1)[0]
            markets[sym] = [code, m.get("status") == "active"]
            if spot_stock(code, tmap.get(sym)):
                need.append(sym)
                n = _f(m.get("multiplier")) or mult.get(code)
                if n and n > 0:
                    shares[sym] = {"n": n, "src": f"Lighter {self.contract}"}
        if not markets:
            raise RuntimeError(f"{self.name}: empty spot list — previous data stays")
        return dict(coins=coins, markets=markets, shares=shares, need_n=sorted(need))


class LighterPerp(_LighterPerp):
    instance, name = "main", "lighter"


class LighterRhPerp(_LighterPerp):
    instance, name = "rh", "lighter_rh"


class LighterSpot(_LighterSpot):
    instance, name = "main", "lighter_spot"


class LighterRhSpot(_LighterSpot):
    instance, name = "rh", "lighter_rh_spot"


PERP_CLIENTS = {"lighter": LighterPerp, "lighter_rh": LighterRhPerp}
SPOT_CLIENTS = {"lighter_spot": LighterSpot, "lighter_rh_spot": LighterRhSpot}


# --- WebSocket ---------------------------------------------------------------------------------------------------------
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi                                   # a dependency of requests; system roots fail TLS on the Mac
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa
        return ssl.create_default_context()


class _WS:
    """Minimal RFC 6455 client: masked text frames out; in — fragments reassembled, ping answered, close reported.
    No extensions are offered, so the server does not compress [L]. A recv timeout leaves the buffer intact."""

    def __init__(self, url: str, timeout: float):
        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if u.scheme == "wss":
                sock = _ssl_ctx().wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                          f"User-Agent: {config.USER_AGENT}\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("handshake: connection closed")
                buf += chunk
                if len(buf) > 65536:
                    raise ConnectionError("handshake: header too long")
            head, rest = buf.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            status = lines[0].split()
            if len(status) < 2 or status[1] != "101":
                raise ConnectionError(f"handshake: {lines[0][:100]}")
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
            if hdrs.get("sec-websocket-accept") != want:
                raise ConnectionError("handshake: bad Sec-WebSocket-Accept")
        except Exception:
            sock.close()
            raise
        self._init(sock, rest)

    @classmethod
    def from_socket(cls, sock, buf: bytes = b"") -> "_WS":
        self = cls.__new__(cls)
        self._init(sock, buf)
        return self

    def _init(self, sock, buf: bytes):
        self.sock = sock
        self._buf = buf
        self._frag = bytearray()
        self._frag_op: int | None = None

    def send(self, op: int, payload: bytes = b""):
        n = len(payload)
        hdr = bytearray([0x80 | op])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(hdr) + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def send_text(self, s: str):
        self.send(0x1, s.encode())

    def _parse(self):
        b = self._buf
        if len(b) < 2:
            return None
        fin, op, masked, n, i = bool(b[0] & 0x80), b[0] & 0x0F, bool(b[1] & 0x80), b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        if n > WS_MAX_MSG:
            raise ConnectionError(f"frame of {n} bytes")
        mask = None
        if masked:
            if len(b) < i + 4:
                return None
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        p = b[i:i + n]
        if mask:
            p = bytes(x ^ mask[k & 3] for k, x in enumerate(p))
        self._buf = b[i + n:]
        return fin, op, bytes(p)

    def _frame(self):
        while True:
            f = self._parse()
            if f is not None:
                return f
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("stream closed by peer")
            self._buf += chunk

    def recv(self) -> tuple[int, bytes]:
        """Next message (opcode, payload): 0x1 text / 0x2 binary (reassembled), 0x8 close."""
        while True:
            fin, op, p = self._frame()
            if op == 0x9:
                self.send(0xA, p)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                return 0x8, p
            if op in (0x1, 0x2):
                if fin:
                    return op, p
                self._frag_op, self._frag = op, bytearray(p)
                continue
            if op == 0x0:
                if self._frag_op is None:
                    raise ConnectionError("continuation without a first fragment")
                self._frag += p
                if len(self._frag) > WS_MAX_MSG:
                    raise ConnectionError("message too large")
                if fin:
                    op, self._frag_op = self._frag_op, None
                    return op, bytes(self._frag)
                continue
            raise ConnectionError(f"unknown opcode {op}")

    def close(self):
        try:
            self.send(0x8, struct.pack(">H", 1000))
        except Exception:  # noqa
            pass
        try:
            self.sock.close()
        except Exception:  # noqa
            pass


class _Stream:
    """One subscription («market_stats/all» or «spot_market_stats/all») in a daemon thread: snapshot + deltas → cache
    {market_id: {symbol, bid, ask}}, reconnect with backoff, ping, silence watchdog."""

    def __init__(self, url: str, channel: str, key: str, name: str, connect=None):
        self.url, self.channel, self.key, self.name = url, channel, key, name
        self._connect = connect or _WS
        self._lock = threading.Lock()
        self._data: dict[int, dict] = {}
        self.last_rx = 0.0                                # last market-data frame on any connection
        self.synced = threading.Event()                   # a snapshot arrived on the current connection
        self.n_conn = 0
        self.err: str | None = None
        self.backoff0 = 1.0
        self._waited = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

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

    def snapshot(self) -> tuple[dict[int, dict], float]:
        with self._lock:
            return dict(self._data), self.last_rx

    def apply(self, msg: dict, rx: float) -> bool:
        """One JSON message → cache. «subscribed/…» is a full snapshot and REPLACES the cache (vanished markets drop
        out); «update/…» merges per market (a missing field keeps its value). Both are {market_id: stats} [L]."""
        body = msg.get(self.key)
        if not isinstance(body, dict):
            return False
        items = [body] if "market_id" in body else [v for v in body.values() if isinstance(v, dict)]
        snap = str(msg.get("type") or "").startswith("subscribed/")
        with self._lock:
            data = {} if snap else self._data
            for m in items:
                try:
                    mid = int(m["market_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                e = dict(data.get(mid) or {})
                for src, dst in (("symbol", "symbol"), ("best_bid_price", "bid"), ("best_ask_price", "ask")):
                    if src in m:
                        e[dst] = m[src]
                data[mid] = e
            self._data = data
            self.last_rx = rx
        if snap:
            self.synced.set()
        return True

    def _session_once(self) -> bool:
        """One connection until it fails. True if a snapshot arrived (a healthy session resets the backoff)."""
        conn, healthy = None, False
        try:
            conn = self._connect(self.url, WS_RECV_TIMEOUT_S)
            self.n_conn += 1
            conn.send_text(json.dumps({"type": "subscribe", "channel": self.channel}))
            last = sent = time.time()
            while not self._stop.is_set():
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
                        healthy = healthy or str(msg.get("type") or "").startswith("subscribed/")
                if now - last > WS_SILENT_S:
                    raise TimeoutError(f"no market data for {now - last:.0f} s")
                if now - sent >= WS_PING_S:
                    conn.send_text('{"type":"ping"}')
                    sent = now
        except Exception as e:  # noqa — any failure: reconnect
            self.err = f"{type(e).__name__}: {e}"
            log.warning("%s ws %s: %s — reconnecting", self.name, self.channel, self.err)
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
