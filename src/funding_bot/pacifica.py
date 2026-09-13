"""Pacifica — perp venue «pacifica» (owner 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica, apex во
фьючи»; the fifth of the six, in the owner's order after lighter_rh). Solana DEX perps: linear, USDC-margined, 1 unit =
1 base token (kBONK = 1000 BONK). Native perp client of venues.py: perp_instruments / premium / books / history_since /
recent_history. Public endpoints only — no keys, no account, no orders.

Measured 12-13.09.2026 from the Mac. Tags: [L] verified live, [D] docs.pacifica.fi (.md pages), [A] assumption.
Hosts [L/D]  REST https://api.pacifica.fi/api/v1 behind CloudFront; envelope {success, data, error, code}. WS
      wss://ws.pacifica.fi/ws — the minimal RFC 6455 client lighter._WS works against it unchanged [L] (imported, not copied).
Universe [L]  GET /info — 77 rows: 76 instrument_type «perpetual» + the spot SOL-USDC (dropped). symbol is CASE-SENSITIVE
      (kBONK), tick_size, lot_size (base units), min_order_size (USD, «10» on all), max_leverage, created_at (ms, listing).
      No status, no class, no full name: name_hint is None; classes come from the static tables below — the docs page
      «Market Specifications» (regenerated from /info, 2026-07-16 [D]) plus later listings cross-checked with Lighter's
      tokenlist and Hyperliquid perpCategories [L/A]. A symbol in no table gets «rwa» (pairs with nothing): Pacifica lists
      pre-markets whose oracle is its OWN mark price [D], so a new ticker must not meet a same-named coin by default.
Rate [L/D]  GET /info/prices — all 77 rows in one call (≈10 credits), one server timestamp for every row. Both rates are
      signed FRACTIONS PER HOUR (interval 1 h, settled on the hour; not per 8 h, not annualised): `funding` = the rate
      paid at the last settlement (fixed for the hour: 0 of 77 moved in 60 s), `next_funding` = the running estimate for
      the NEXT settlement (TWAP of 5 s samples over the hour, applied at its end [D]; 13 of 77 moved in 60 s). premium()
      takes next_funding — the predicted rate, like Binance lastFundingRate. Baseline 0.0000125/h = 0.01 %/8 h ÷ 8 (60 of
      77 markets). Plus = longs pay (public vault payouts: 382/382 and 18/18) [L]. Cap ±4 % per hour [D].
Books [L]  REST /book is per symbol (≈10 credits; 76 per tick would blow the limit), no batch. WS «bbo» per symbol on ONE
      connection: {"channel":"bbo","data":{s,i,li,t,b,B,a,A}} — b/a best bid/ask, B/A sizes in the market's unit (the
      unit the price is for: BTC, or 1 kBONK = 1000 BONK — lot_size 1 there [A for k-markets]). CHANGE-ONLY,
      no snapshot on subscribe: median first message 4.7 s, slowest 40 s, all 76 seen within 50 s; a subscribe to an
      unknown symbol is acked all the same. The «prices» channel (all rows every 3 s) is the liveness heartbeat. A
      client frame at least every 60 s or the server closes [D]: {"method":"ping"} → {"channel":"pong"} [L]; any
      connection is closed after 24 h [D] → reconnect. The cache survives reconnects (there is no snapshot to rebuild
      it); a book's obs = the connection's last market-data frame while the book was updated on that connection, else
      the book's own last update (a change missed during the reconnect gap must not look fresh).
History [L]  GET /funding_rate/history?symbol&limit[&cursor] — rows NEWEST FIRST; limit ≤ 4000 (5000 → 4000 silently);
      cursor pages backwards contiguously (next_cursor, has_more). start_time / end_time are IGNORED. Unknown or
      wrong-case symbol → 200 with data [] — not an error. THE PITFALL: the rate paid at settlement H is
      row(H).next_funding_rate; row(H).funding_rate is the previous hour's (next(H) == funding(H+1) on 798-799 of 799 rows
      per symbol; vault payouts equal next_funding_rate on 156/156 and differ from funding_rate on 42; the first-ever row
      carries the placeholder funding_rate 0.0001). created_at lands 0.3–4.9 s after the hour. Venue-wide skipped
      settlements exist (2026-09-05 and 09-09 00:00Z absent on all 9 sampled markets; the vault was not paid either), so
      «pacifica» must NOT be in config.FIXED_INTERVAL_H — it would call them permanent holes. No all-markets batch →
      recent_history() is [] and upkeep is the per-leg repair (76 legs × 7.5 s ≈ 9.5 min < config.CATCHUP_S).
Limits [L]  «ratelimit-policy: "credits";q=1000;w=60», «ratelimit: "credits";r=<left>;t=<s to reset>». Costs from
      successive r: prices ≈ 10, book ≈ 10, history ≈ 90 PER CALL whatever the limit, info ≈ 0–20; r jumps between values
      (≥ 2 server-side buckets) — budgeted as one. Per minute: tick 6 × 10 + history 8 × 90 (7.5 s apart) = 780 of 1000.
      The limit is per IP: pause, credits and the history pace are per host, shared by every client of it in the process
      (the collector's background copies from make_clients()). 429 was never triggered: Retry-After is honoured when
      present, else the host pauses 60 s [A].
Fees [D/L]  tier 1 taker 0.040 % / maker 0.015 % (/info/fees level 0: taker «0.0004») — config.FEES_TAKER.
Identity  The API gives no names, contracts or oracle feed ids for perps [L]; the only evidence is the docs' oracle table
      (crypto: Binance spot 40 % · Binance futures index 20 % · OKX spot 20 % · Bybit spot 20 %; FARTCOIN / XMR / PIPPIN
      100 % Binance futures index; RWA: trade.xyz 71 % · Lighter 14 % · Bitget 14 %; BP: Backpack spot 100 %) [D]. An
      oracle venue for identity.py (ORACLE_PERPS) — never by price. LIT here is Lighter's token by its oracle (Binance
      futures index, OKX, Bybit), not Binance-spot Litentry [A].
"""
from __future__ import annotations
import json, logging, math, re, socket, threading, time
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .lighter import _WS
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "pacifica"
REST = "https://api.pacifica.fi/api/v1"
WS_URL = "wss://ws.pacifica.fi/ws"
PAGE_URL = "https://app.pacifica.fi/trade/{symbol}"   # app route `/trade/${symbol}` [L]; the SPA never 404s → live symbols only
QUOTE = "USDC"
HOUR_MS = 3600_000
INTERVAL_H = 1                  # every market settles hourly on the hour [D/L]
CAP_PER_H = 0.04                # «funding fees per hour are capped at ±4 %» [D] — fraction per interval
HISTORY_LIMIT = 4000            # max rows per call [D/L]; 30 days = 722 rows → one call per leg
HISTORY_SLACK = 3               # rows over the hours of the window (settlement rows land seconds after the hour)
HISTORY_MAX_PAGES = 5           # 5 × 4000 h ≈ 833 days (BTC's whole history is 3 pages); more raises, never truncates
HISTORY_GAP_S = 7.5             # 90 credits a call → 8 calls/min = 720 of 1000 (config.FUNDING_HISTORY_MIN_GAP_S overrides)
CREDITS_Q = 1000                # window quota when the policy header is missing [L]
TICK_MIN_CREDITS = 20           # the tick is refused (BudgetExceeded, next tick retries) below this many credits left
HIST_MIN_CREDITS = 200          # history waits for the window to reset below this: the tick keeps its credits
AUX_MIN_CREDITS = 50            # hourly universe call waits below this
HIST_MAX_WAIT_S = 90.0          # history gives up (BudgetExceeded → venue skipped this pass) after waiting this long
AUX_MAX_WAIT_S = 30.0
BAN_DEFAULT_S = 60.0            # 429 without Retry-After → host pause [A]
UNIVERSE_TTL_S = 60             # an unknown symbol in history refreshes the market list at most this often
SPREAD_MAX = 0.05               # [A] a book wider than 5 % of mid is not a price (as lighter._book)
OBS_SKEW_S = 120.0              # server timestamp further than this from the local request window → request start is obs
WS_RECV_TIMEOUT_S = 5.0
WS_SILENT_S = 30.0              # no market data (prices every 3 s) on the connection for this long → reconnect
WS_PING_S = 30.0                # client frame well within the server's 60 s idle limit [D]
WS_BACKOFF_MAX_S = 30.0
WS_FIRST_WAIT_S = 3.0           # the very first books() call waits this long for market data (then never blocks)

# --- asset classes (docs «Market Specifications» [D]; listings after 2026-07-16 cross-checked [L/A]) -------------------
EQUITY = frozenset({"CRCL", "GOOGL", "HOOD", "MSTR", "NVDA", "PLTR", "SAMSUNG", "SKHYNIX", "SPCX", "TSLA",
                    "URNM",                                  # ETF shares (docs class «ETF») — equity, as Lighter files ETFs
                    "MU", "SNDK", "DRAM"})                   # later: Lighter STOCK / ETF, HL xyz stocks [L]
INDEX = frozenset({"SP500"})
FX = frozenset({"EURUSD", "USDJPY"})
COMMODITY = frozenset({"CL", "COPPER", "NATGAS", "PLATINUM", "XAG", "XAU"})
CRYPTO = frozenset({"BTC", "ETH", "BNB", "DOGE", "HYPE", "SOL", "XRP", "AAVE", "ADA", "ARB", "ASTER", "AVAX", "BCH",
                    "CRV", "ENA", "FARTCOIN", "JUP", "LDO", "LINK", "LIT", "LTC", "NEAR", "PUMP", "SUI", "TAO", "TRUMP",
                    "UNI", "XMR", "XPL", "ZEC", "kBONK", "kPEPE", "ICP", "PENGU", "STRK", "VIRTUAL", "WIF", "WLD",
                    "WLFI", "ZK", "ZRO", "2Z", "CHIP", "MEGA", "MON", "PIPPIN",          # docs «Crypto perpetuals» [D]
                    "PAXG",                                  # gold token = coin (repo rule); oracle Binance/OKX spot [D]
                    "BP",                                    # Backpack token, oracle Backpack spot 100 % [D]
                    "VVV", "KAITO", "kSHIB", "PONS", "USELESS"})   # later listings: Lighter CRYPTO, docs silent [A]
CLASSES = (("crypto", CRYPTO), ("equity", EQUITY), ("index", INDEX), ("fx", FX), ("commodity", COMMODITY))
_FX = re.compile(r"^(?:([A-Z]{3})USD|USD([A-Z]{3}))$")     # EURUSD → EUR, USDJPY → JPY (as Hyperliquid xyz:EUR, xyz:JPY)
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


def perp_class(symbol: str) -> str:
    """crypto / equity / index / fx / commodity by the static tables (the API has no class); «rwa» for a symbol nobody
    has classified yet — it pairs with nothing (a pre-market on Pacifica's own mark must not meet a same-named coin)."""
    for cls, syms in CLASSES:
        if symbol in syms:
            return cls
    if symbol not in _WARNED:
        _WARNED.add(symbol)
        log.warning("%s: %s is in no class table — «rwa», pairs with nothing until classified", NAME, symbol)
    return "rwa"


def perp_base(symbol: str, cls: str) -> tuple[str, float]:
    """Pacifica symbol → (canonical base, tokens per unit of price): kBONK → BONK ×1000 (only a lower-case k: KAITO stays),
    EURUSD → EUR, USDJPY → JPY, PLATINUM → XPT (config.PERP_CANON); CL, COPPER, SAMSUNG, SKHYNIX are canonical already."""
    base, factor = norm_symbol_factor(symbol)
    if cls == "fx":
        m = _FX.match(base)
        if m:
            base = m.group(1) or m.group(2)
    return config.PERP_CANON.get(cls, {}).get(base, base), factor


def _book(bid, ask, bid_qty=None, ask_qty=None) -> dict | None:
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=abs(_f(bid_qty) or 0.0), ask_qty=abs(_f(ask_qty) or 0.0))


def _obs(ts_ms, t0: float, t1: float) -> float:
    """Moment of observation: the server's snapshot time when plausible (a cached answer shows its real age), capped at
    arrival; otherwise the request start."""
    s = ts_ms / 1000.0 if ts_ms else None
    if s is not None and t0 - OBS_SKEW_S <= s <= t1 + OBS_SKEW_S:
        return min(s, t1)
    return t0


_RL_LEFT = re.compile(r"\br\s*=\s*(\d+)")
_RL_RESET = re.compile(r"\bt\s*=\s*(\d+)")
_RL_QUOTA = re.compile(r"\bq\s*=\s*(\d+)")


# --- per-host state ---------------------------------------------------------------------------------------------------
class _Host:
    """State of one REST host shared by every client of it in this process: the credit window is per IP."""

    def __init__(self):
        self.lock = threading.Lock()
        self.banned_until = 0.0
        self.last_hist = 0.0
        self.left: int | None = None             # credits left in the current window (ratelimit r)
        self.quota = CREDITS_Q                   # ratelimit-policy q
        self.reset_at = 0.0                      # the window resets at (arrival + t)
        self.markets: dict[str, int] = {}        # perp symbol → created_at ms (last universe)
        self.markets_ts = 0.0
        self.seen: list[str] = []                # perp symbols of the last prices answer (books before the universe)

    def credits_left(self, now: float) -> int | None:
        return self.left if self.left is not None and now < self.reset_at else None


_HOSTS: dict[str, _Host] = {}
_HOSTS_LOCK = threading.Lock()


def _host(base: str) -> _Host:
    with _HOSTS_LOCK:
        return _HOSTS.setdefault(base, _Host())


# --- client -----------------------------------------------------------------------------------------------------------
class PacificaPerp:
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
        self._stream: _BboStream | None = None
        self.used_weight = 0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0

    # --- budget and health ------------------------------------------------------------------------------------
    @property
    def banned_until(self) -> float:
        return self._h.banned_until

    def budget_used(self) -> float:
        """Share of the credit window spent, by the last ratelimit header; 0 once that window has reset."""
        h = self._h
        with h.lock:
            left, q = h.credits_left(time.time()), h.quota
        return 0.0 if left is None else max(0.0, 1.0 - left / float(q or CREDITS_Q))

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self._h.banned_until        # credits are guarded inside _get (pacing)

    def health(self) -> dict:
        h = self._h
        with h.lock:
            left, q = h.credits_left(time.time()), h.quota
        self.used_weight = 0 if left is None else max(0, q - left)
        return {"exchange": self.name, "used_weight": self.used_weight, "budget": round(self.budget_used(), 3),
                "last_ok_ts": int(self.last_ok_ts), "n_429": self.n_429, "n_err": self.n_err,
                "banned_until": int(h.banned_until)}

    # --- transport -------------------------------------------------------------------------------------------------
    def _acquire(self, kind: str):
        """Permission to call. tick — now or BudgetExceeded (the next tick retries); hist — waits for its pace and for
        enough credits (until the window resets); aux — waits for a few credits."""
        h = self._h
        need = TICK_MIN_CREDITS if kind == "tick" else HIST_MIN_CREDITS if kind == "hist" else AUX_MIN_CREDITS
        t_end = time.time() + (0.0 if kind == "tick" else HIST_MAX_WAIT_S if kind == "hist" else AUX_MAX_WAIT_S)
        while True:
            now = time.time()
            if now < h.banned_until:
                raise BannedError(f"{self.name}: pause after 429 until {time.strftime('%H:%M:%S', time.gmtime(h.banned_until))}")
            with h.lock:
                left = h.credits_left(now)
                wait = h.reset_at - now if left is not None and left < need else 0.0
                if kind == "hist":
                    wait = max(wait, h.last_hist + self.history_gap_s - now)
                if wait <= 0:
                    if kind == "hist":
                        h.last_hist = now
                    return
            if now + wait > t_end:
                raise BudgetExceeded(f"{self.name}: {left} credits left of {h.quota} for {wait:.0f} s more, {kind} call skipped")
            time.sleep(min(wait, 5.0))

    def _credits(self, r):
        hd = {str(k).lower(): str(v) for k, v in (getattr(r, "headers", None) or {}).items()}
        rl, pol = hd.get("ratelimit") or "", hd.get("ratelimit-policy") or ""
        m_left, m_reset, m_q = _RL_LEFT.search(rl), _RL_RESET.search(rl), _RL_QUOTA.search(pol)
        if not m_left:
            return
        with self._h.lock:
            self._h.left = int(m_left.group(1))
            self._h.reset_at = time.time() + (int(m_reset.group(1)) if m_reset else 60)
            if m_q and int(m_q.group(1)) > 0:
                self._h.quota = int(m_q.group(1))

    def _throttle(self, r, path: str):
        try:
            pause = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            pause = BAN_DEFAULT_S
        pause = max(1.0, pause)
        with self._h.lock:
            self._h.banned_until = max(self._h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s 429 on %s — host paused for %.0f s", self.name, path, pause)
        raise BannedError(f"{self.name}: 429 on {path}, pause {pause:.0f} s")

    def _get(self, path: str, params: dict | None = None, kind: str = "aux", retries: int | None = None,
             timeout: float | None = None) -> dict:
        """One GET → the whole envelope {success, data, …}. 429 → host pause + BannedError; other 4xx → permanent; 5xx,
        network, bad JSON, success=false → retried (the tick: one try — a retry is the next tick)."""
        retries = (config.TICK_RETRIES if kind == "tick" else 3) if retries is None else retries
        timeout = (config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT) if timeout is None else timeout
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            try:
                r = self._s.get(self.rest + path, params=params, timeout=timeout)
                self._credits(r)
                if r.status_code == 429:
                    self._throttle(r, path)
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path}: {r.text[:200]}")
                r.raise_for_status()
                body = r.json()
                if not isinstance(body, dict) or body.get("success") is False or "data" not in body:
                    raise RuntimeError(f"{path}: {str(body)[:200]}")
                self.last_ok_ts = time.time()
                return body
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON, success=false — retried
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    @staticmethod
    def _rows(body: dict) -> list[dict]:
        d = body.get("data")
        return [r for r in d if isinstance(r, dict)] if isinstance(d, list) else []

    # --- native perp interface ----------------------------------------------------------------------------------------
    def perp_instruments(self) -> list[dict]:
        rows = self._rows(self._get("/info", kind="aux"))
        now_ms = int(time.time() * 1000)
        out, markets = [], {}
        for m in rows:
            sym = str(m.get("symbol") or "")
            if not sym or str(m.get("instrument_type") or "").lower() != "perpetual":
                continue                                   # SOL-USDC is a spot market (1x, no funding) [L/D]
            created = _int(m.get("created_at"))
            if created > now_ms:
                continue                                   # listing announced, trading not started [A]
            cls = perp_class(sym)
            base, factor = perp_base(sym, cls)
            out.append(dict(exchange=self.name, symbol=sym, base_asset=sym, base=base, factor=factor,
                            tick_size=_f(m.get("tick_size")), step_size=_f(m.get("lot_size")),
                            min_notional=_f(m.get("min_order_size")), onboard_ms=created, interval_h=INTERVAL_H,
                            cap=CAP_PER_H, floor=-CAP_PER_H, quote=QUOTE, contract="main", cls=cls,
                            name_hint=None,                # the API has no full names [L]
                            url=PAGE_URL.format(symbol=quote(sym, safe=""))))
            markets[sym] = created
        if not out:
            raise RuntimeError(f"{self.name}: /info without perpetuals")
        with self._h.lock:
            self._h.markets, self._h.markets_ts = markets, time.time()
        return out

    def premium(self) -> dict[str, dict]:
        """symbol → {rate (predicted rate of the next hourly settlement, fraction per hour), mark, index (oracle), next_ms,
        ts_ms (server snapshot), obs, interval_h}."""
        t0 = time.time()
        rows = self._rows(self._get("/info/prices", kind="tick"))
        t1 = time.time()
        mk = self._h.markets
        now_ms = int(t1 * 1000)
        nxt = (now_ms // HOUR_MS + 1) * HOUR_MS             # every hour on the hour [D/L]
        out = {}
        for r in rows:
            sym = str(r.get("symbol") or "")
            if not sym or (sym not in mk if mk else "-" in sym):
                continue                                   # the spot SOL-USDC; delisted / not yet in the universe
            rate = _f(r.get("next_funding"))
            if rate is None:
                continue
            ts = _int(r.get("timestamp"), None)
            obs = _obs(ts, t0, t1)
            out[sym] = dict(rate=rate, mark=_f(r.get("mark")), index=_f(r.get("oracle")), next_ms=nxt,
                            ts_ms=int(obs * 1000), obs=obs, interval_h=INTERVAL_H)
        if out:
            self._h.seen = sorted(out)
        return out

    def books(self) -> dict[str, dict]:
        """Best bid / ask of every perp from the WS «bbo» cache; quantities in the market's unit (the unit its price is
        for — kBONK units, i.e. thousands of BONK, on k-markets [A]). A market without a bbo yet
        (quiet since the stream started) has no book this tick — the collector prices it by mark."""
        syms = list(self._h.markets) or list(self._h.seen)
        st = self._ensure_stream(syms)
        return st.books(set(syms) if syms else None)

    def _ensure_stream(self, syms: list[str]) -> "_BboStream":
        if self._stream is None:
            self._stream = _BboStream(self.ws_url, self.name, self._connect)
        if syms:
            self._stream.want(syms)
        self._stream.start()
        self._stream.wait_first(WS_FIRST_WAIT_S)
        return self._stream

    def _onboard(self, symbol: str) -> int:
        h = self._h
        if symbol not in h.markets and time.time() - h.markets_ts >= UNIVERSE_TTL_S:
            self.perp_instruments()                        # a fresh client (background copy) learns the list once per host
        if symbol not in h.markets:
            raise RuntimeError(f"{self.name}: no market {symbol}")
        return h.markets[symbol]

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled hourly rates of one market in [start_ms, end_ms], ascending. The endpoint ignores time filters and
        answers newest first, so the client asks for enough rows to reach start_ms (30 days = one call) and pages back with
        the cursor. rate = row.next_funding_rate — the rate paid at that row's hour (see the header). An empty answer
        for a market that must have settled already raises: the API says [] for unknown symbols too, and an empty
        success would confirm depth that was never fetched."""
        created = self._onboard(symbol)
        start_ms = int(start_ms)
        now_ms = int(time.time() * 1000)
        end_ms = int(end_ms or now_ms)
        if end_ms < start_ms:
            return []
        limit = max(1, min(HISTORY_LIMIT, math.ceil((now_ms - start_ms) / HOUR_MS) + HISTORY_SLACK))
        got: dict[int, dict] = {}
        cursor, oldest, n_rows = None, None, 0
        for _ in range(HISTORY_MAX_PAGES):
            params = {"symbol": symbol, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            body = self._get("/funding_rate/history", params, kind="hist")
            rows = self._rows(body)
            n_rows += len(rows)
            for r in rows:
                ca = _int(r.get("created_at"), None)
                if ca is None:
                    continue
                oldest = ca if oldest is None else min(oldest, ca)
                ms = ca // HOUR_MS * HOUR_MS
                rate = _f(r.get("next_funding_rate"))
                if rate is None or not (start_ms <= ms <= end_ms):
                    continue
                got.setdefault(ms, dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=None))
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not rows or (oldest is not None and oldest <= start_ms):
                break
            if not cursor:
                raise RuntimeError(f"{self.name} {symbol}: has_more without next_cursor at {oldest}")
        else:
            raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {HISTORY_MAX_PAGES} pages")
        if not n_rows:
            first = (created // HOUR_MS + 1) * HOUR_MS if created else 0
            if not created or now_ms > first + config.SETTLE_GRACE_S * 1000:
                raise RuntimeError(f"{self.name} {symbol}: empty history for a market listed at {created} — not confirmed")
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """No «settled rates of all markets» endpoint (history needs symbol [L]). Upkeep is the per-leg top-up by
        completeness. Synthesising the last settlement from prices.funding would invent rows for skipped settlements."""
        return []

    def close(self):
        if self._stream is not None:
            self._stream.stop()


PERP_CLIENTS = {NAME: PacificaPerp}


# --- WebSocket: bbo per symbol + prices heartbeat ----------------------------------------------------------------------
def _sub(source: str, symbol: str | None = None) -> str:
    p = {"source": source}
    if symbol:
        p["symbol"] = symbol
    return json.dumps({"method": "subscribe", "params": p})


class _BboStream:
    """One connection in a daemon thread: «prices» (liveness) + «bbo» for every wanted symbol → cache {symbol: {bid, ask,
    bid_qty, ask_qty, rx, conn, t}}; new symbols are subscribed on the live connection, all of them on a reconnect;
    ping, silence watchdog, reconnect with backoff. The cache is kept across reconnects (no snapshot exists)."""

    def __init__(self, url: str, name: str, connect=None):
        self.url, self.name = url, name
        self._connect = connect or _WS
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._want: set[str] = set()
        self.last_rx = 0.0                                # last market-data frame (bbo or prices) of the latest connection
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
        """One JSON message → cache. True for market data (bbo, prices); acks and pongs are not market data."""
        ch = msg.get("channel")
        d = msg.get("data")
        if ch == "prices" and isinstance(d, list):
            with self._lock:
                self.last_rx = rx
            return True
        if ch != "bbo" or not isinstance(d, dict) or not d.get("s"):
            return False
        sym = str(d["s"])
        with self._lock:
            old = self._data.get(sym)
            t = _int(d.get("t"), None)
            if not (old and t is not None and old.get("t") is not None and t < old["t"]):   # an older frame never wins
                self._data[sym] = dict(bid=d.get("b"), ask=d.get("a"), bid_qty=d.get("B"), ask_qty=d.get("A"), rx=rx,
                                       conn=self.conn_id, t=t)
            self.last_rx = rx
        return True

    def books(self, symbols: set[str] | None = None) -> dict[str, dict]:
        """obs = the latest connection's last market-data frame for books updated on it (a quiet market is unchanged,
        the stream is change-only); a book left from an earlier connection carries its own last update."""
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
            conn.send_text(_sub("prices"))
            subbed: set[str] = set()
            last = sent = time.time()
            while not self._stop.is_set():
                with self._lock:
                    new = sorted(self._want - subbed)
                for s in new:
                    conn.send_text(_sub("bbo", s))
                    subbed.add(s)
                if new:
                    sent = time.time()
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
                if now - sent >= WS_PING_S:
                    conn.send_text('{"method":"ping"}')
                    sent = now
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
