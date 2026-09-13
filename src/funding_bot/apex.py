"""ApeX Omni — perp venue «apex» (owner 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica, apex во
фьючи»; the sixth of the six, in the owner's order after pacifica). Native perp client of venues.py: perp_instruments /
premium / books / history_since / recent_history. Public endpoints only — no keys, no accounts, no orders.

Measured 12-13.09.2026 from the Mac. Tags: [L] verified live, [D] api-docs.pro.apex.exchange (= api-docs.omni…),
[A] assumption.
API [L]  Omni v3 only: REST https://omni.apex.exchange/api/v3, WS wss://quote.omni.apex.exchange/realtime_public?v=2&
      timestamp=<ms>. Pro v2 is dead (/v2/ticker → code 2 «internal server error»). Success body {data, timeCost}; error
      body {code, msg} with HTTP 200 (code 3 = bad parameter: «invalid symbol», «invalid get page size», «invalid page
      start»). Unknown path → HTTP 404 JSON. Behind a CDN (x-via «Cdn Cache Server»), no rate headers.
Universe [L]  GET /symbols (~40 KB gzipped): data.contractConfig.perpetualContract (138) + stockContract (47);
      predictionContract (184 event markets) and prelaunchContract (0) are not taken. Kept: enableTrade & enableDisplay &
      enableOpenPosition & settleAssetId USDT & not isPrelaunch → 86 crypto + 39 stock = 125 on 13.09. Tradable-but-hidden
      IO / STBL are out [A: hidden = not offered]. crossSymbolName «BTCUSDT» is our symbol (ticker, depth, WS); symbol
      «BTC-USDT» is the only form /history-funding accepts — remembered for every row, disabled ones too (TON still has
      hourly history).
Rate [L/D]  fr / fundingRate = signed FRACTION PER 1 HOUR = per interval (no conversion); positive = longs pay [D: fee =
      position value × index price × rate]. It is the running estimate of the current hour (moves ~every minute) and equals
      the settled row exactly in the first seconds after the hour (5/5 symbols at 22:00Z 12.09), then resets to the new
      hour's still-noisy estimate. predictedFundingRate is the constant 0.0000125 on every symbol — ignored. Baseline
      0.0000125/h = fundingInterestRate 0.0003 / 24 = 0.01 %/8 h. Caps fundingMaxRate/MinRate per symbol (0.000305 … 0.02),
      unit per hour [A]. Settlement every hour on the UTC hour for every sampled symbol (30 d = 720 rows, no gaps, 8
      symbols) [L/D] — config.FIXED_INTERVAL_H; no funding_intervals() hook.
Tick [L]  REST /ticker takes ONE symbol (none → []) and 125 symbols per 10 s would exceed the 600/min IP limit [D], so the
      WS channel instrumentInfo.all feeds premium(): a FULL snapshot of ~368 rows every ~2 s (prediction and collateral
      rows WBTC/USDC… included — joined on the universe), ts in MICROseconds, fields s, fr, mp (mark), xp (index), ss
      (status "0"; USO shows "5" while tradable — logged, never filtered [A]). fr/mp/xp equal the REST ticker. No bid/ask,
      no next funding time → the next UTC hour. Stream not fresh → REST /ticker round-robin, FALLBACK_PER_TICK symbols a
      tick, every row with its own obs (≈150 calls/min, 30-70 ms each) [A].
Books [L]  WS orderBook25.H.<symbol>, all symbols in one subscribe: snapshot, then deltas {s, b, a, u}. Snapshot sides are
      ASCENDING (best bid = max price, best ask = min — never b[0]); size "0" deletes a level; u is contiguous per symbol.
      A u gap drops the book and re-subscribes it: unsubscribe + subscribe brings a fresh snapshot, a second plain
      subscribe is refused «already subscribed». Quantities are in units of baseTokenId (the unit the price is for:
      1000PEPE). Quiet books stay silent for 20 s+, so a book's obs = the connection's last market-data frame
      (instrumentInfo.all every ~2 s); each new connection rebuilds the books from its own snapshots. Keepalive: server
      {"op":"ping","args":[ms]} every 15 s → {"op":"pong","args":[same]}; client {"op":"ping","args":[ms]} every
      WS_PING_S → {"success":true,"ret_msg":"pong"}; the server drops a client silent for 150 s [D]. One socket carries
      both channels; lighter._WS (minimal RFC 6455, no compression) works unchanged. Ingress ≈ 34 KB/s + ≈ 18 KB/s.
History [L]  GET /history-funding?symbol=<DASHED>&limit≤100&beginTimeInclusive&endTimeExclusive — rows newest first
      {rate (fraction per 1 h), price (the INDEX at settlement, rounded — not the mark → mark None), fundingTime (ms, on
      the hour)}; totalSize is NOT a count (page·limit + n + 1). Page index + window returns nothing past ~page 5 and a deep
      index is slow, so the walk is by time: endTimeExclusive := oldest fundingTime of the last page (exclusive → no
      duplicates); 30 d = 8 calls. A new row appears ≤ 40 s after the hour. No all-symbols endpoint → recent_history() is
      [] and upkeep is the per-leg top-up (125 calls an hour).
Limits [D]  600 requests / 60 s per IP. 403 = IP banned (temporarily or permanently) → long host pause [A]; 429 never
      seen → 60 s pause, Retry-After honoured [A]. Own budget: 0 tick calls while the stream is fresh; history ≥
      HISTORY_GAP_S apart (240/min at most); hourly /symbols. Pause, window and history pace are per host, shared by every
      client of this process (the collector's background copies from make_clients()).
Fees  level-1 taker 0.05 % / maker 0.02 % (fee pages; the account doc example shows takerFeeRate 0.00050 [D]) — no public
      per-symbol fee field [L]; config.FEES_TAKER.
Classes [L]  perpetualContract → crypto (PAXG too, repo rule). stockContract by category: STOCK → equity (SPCX «Space
      Exploration Technologies», CXMT, UNITREE, CBRS included — all four are category STOCK here and equity on 10+ other
      venues at one price within 0.3 %, review 13.09 [L]; preipo only for OPENAI / ANTHROPIC, private on every venue);
      COMMODITY → commodity for known names (XAU XAG CL BZ NATGAS …) but a fund (USO «United States Oil Fund») → equity;
      INDEX or no category → equity when the name is an ETF / fund / trust (SPY QQQ EWY DRAM SOXL); anything else →
      «rwa» (pairs with nothing) + a warning. ORACLE («Oracle Corporation») → base ORCL (ORCL-USDT, the same company, is
      disabled). Factor from the NAME (1000PEPE → PEPE ×1000, 1000000MOG → MOG ×1e6), never from stepSize.
Identity  no index composition, no token contract, no oracle feed (oraclePrice "") [L] → an oracle venue for identity.py:
      names only, never price. name_hint = tokenName unless it merely repeats the ticker (48 of 86 crypto rows: APEX, TAO,
      LIT, PEPE …) — a ticker is not a name. Pitfalls: LIT = Lighter (not Litentry), GRAM = «Gram (prev. Toncoin)»,
      CHIP = USD.AI, SPX = SPX6900 (a coin, not the S&P 500), S = Sonic, NEIROETH ≠ NEIRO.
"""
from __future__ import annotations
import json, logging, math, re, socket, threading, time
from collections import deque
from datetime import datetime
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "apex"
REST = "https://omni.apex.exchange/api/v3"
WS_URL = "wss://quote.omni.apex.exchange/realtime_public?v=2&timestamp={ms}"
PAGE_URL = "https://omni.apex.exchange/trade/{symbol}"   # SPA answers 200 for any path → links only from the live list
QUOTE = "USDT"
HOUR_MS = 3600_000
INTERVAL_H = 1                   # every symbol settles hourly on the UTC hour [L/D]
PATH_SYMBOLS = "/symbols"
PATH_TICKER = "/ticker"
PATH_HIST = "/history-funding"
TOPIC_INFO = "instrumentInfo.all"
TOPIC_BOOK = "orderBook25.H."
GROUPS = ("perpetualContract", "stockContract")          # predictionContract / prelaunchContract are not taken
BAD_PARAM = "3"                  # body code of «invalid symbol / page size / page start» (HTTP 200) [L]
HISTORY_PAGE = 100               # limit ≤ 100; 101 → code 3 [L]
HISTORY_SLACK = 2                # rows over the hours of a short window
HISTORY_MAX_PAGES = 60           # 60 × 100 h ≈ 250 days; a window needing more raises instead of truncating silently
HISTORY_GAP_S = 0.25             # 240 calls/min at most = 40 % of the IP limit (config.FUNDING_HISTORY_MIN_GAP_S overrides)
REQ_PER_MIN = 600                # per IP per rolling minute [D]
TICK_CAP = 540                   # tick / hourly calls are refused or wait above this many in the last 60 s
HIST_CAP = 400                   # history waits while the window holds this many: the rest stays for the tick
HIST_MAX_WAIT_S = 90.0           # history gives up (BudgetExceeded → venue skipped this pass) after waiting this long
AUX_MAX_WAIT_S = 30.0
BAN_DEFAULT_S = 60.0             # 429 without Retry-After [A]
BAN_403_S = 900.0                # 403 = IP banned [D]; how long is not documented [A]
IDS_TTL_S = 60                   # unknown symbol in history → /symbols is refreshed at most this often
FALLBACK_PER_TICK = 26           # REST /ticker calls per tick while the stream is not fresh: 125 symbols in 5 ticks [A]
FALLBACK_BUDGET_S = 4.0          # and never longer than this per tick (config.TICK_DEADLINE_S is 8)
SPREAD_MAX = 0.05                # [A] a book wider than 5 % of mid is not a price (as lighter._book)
WS_FRESH_S = 15.0                # instrumentInfo.all feeds premium() while its last frame is this young (frames ~2 s)
WS_RECV_TIMEOUT_S = 5.0
WS_SILENT_S = 30.0               # no market data on the connection for this long → reconnect
WS_PING_S = 15.0                 # client heartbeat; the server drops a client silent for 150 s [D]
WS_BACKOFF_MAX_S = 30.0
WS_FIRST_WAIT_S = 3.0            # the very first premium()/books() waits this long for data (then never blocks)
BOOK_SUB_CHUNK = 150             # book topics per subscribe message (125 in one message worked [L])
RESUB_MIN_S = 10.0               # a book is re-subscribed at most this often

# --- classes and bases ------------------------------------------------------------------------------------------------
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})                 # repo rule: gold tokens are coins
COMMODITIES = frozenset({"XAU", "XAG", "CL", "BZ", "NATGAS", "COPPER", "XCU", "XPT", "XPD", "PLATINUM", "PALLADIUM"})
# private on every venue. Review 13.09 [L]: SPCX / CXMT / UNITREE / CBRS are equity on Aster, Binance, HL, KuCoin, Gate,
# Lighter (both), Variational, Extended, Pacifica at one price — a hard-coded preipo / «rwa» here cut them off every pair
PREIPO = frozenset({"OPENAI", "ANTHROPIC"})
_FUND = re.compile(r"\b(?:ETF|FUND|TRUST)\b", re.I)       # USO, SPY, QQQ, EWY, DRAM, SOXL — shares of a fund
LOCAL_CANON = {"equity": {"ORACLE": "ORCL"}}              # ORACLE-USDT «Oracle Corporation»; ORCL-USDT is disabled [L]
_WARNED: set[str] = set()
_SS_SEEN: dict[str, str] = {}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None                         # NaN is not a number here


def _int(x, default: int | None = 0) -> int | None:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _ts_ms(x) -> int | None:
    """WS ts is in microseconds [L]; seconds and milliseconds tolerated."""
    v = _int(x, None)
    if not v or v <= 0:
        return None
    if v >= 10 ** 14:
        return v // 1000
    return v * 1000 if v < 10 ** 11 else v


def _iso_ms(s) -> int | None:
    """/ticker nextFundingTime «2026-09-12T23:00:00Z» → ms."""
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _next_hour(next_ms: int | None, now_ms: int) -> int:
    """Next settlement: the venue's time rolled forward by whole hours once passed; without one — the next UTC hour."""
    if not next_ms or next_ms <= 0:
        return (now_ms // HOUR_MS + 1) * HOUR_MS
    if next_ms <= now_ms:
        next_ms += ((now_ms - next_ms) // HOUR_MS + 1) * HOUR_MS
    return next_ms


def _unclassified(token: str, why: str) -> str:
    if token not in _WARNED:
        _WARNED.add(token)
        log.warning("%s: %s is %s — «rwa», pairs with nothing until classified", NAME, token, why)
    return "rwa"


def asset_class(group: str, token: str, category, name) -> str:
    """crypto / equity / commodity / preipo; «rwa» for a real-world asset nobody has classified (pairs with nothing)."""
    t = str(token or "").upper()
    if group == "perpetualContract":
        return "crypto"                                   # PAXG included (repo rule)
    if t in GOLD_TOKENS:
        return "crypto"
    if t in PREIPO:
        return "preipo"
    c = str(category or "").upper()
    fund = bool(_FUND.search(str(name or "")))
    if c == "STOCK":
        return "equity"
    if c == "COMMODITY":
        if t in COMMODITIES:
            return "commodity"
        if fund:
            return "equity"                               # USO — shares of an oil fund, not the oil
        return _unclassified(t, f"COMMODITY «{name}» not in the known list")
    if c in ("INDEX", "", "NONE") and fund:
        return "equity"                                   # SPY QQQ EWY DRAM SOXL — ETF shares
    return _unclassified(t, f"stockContract «{name}» with category {category!r}")


def perp_base(token: str, cls: str) -> tuple[str, float]:
    """baseTokenId → (canonical base, tokens per unit of price): 1000PEPE → PEPE ×1000, ORACLE → ORCL (equity)."""
    base, factor = norm_symbol_factor(str(token or ""))
    base = LOCAL_CANON.get(cls, {}).get(base, base)
    return config.PERP_CANON.get(cls, {}).get(base, base), factor


def _alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def name_hint(name, token: str, base: str) -> str | None:
    """tokenName unless it only repeats the ticker («PEPE» for 1000PEPE, «LIT», «TAO») — identity must not confirm a coin
    by an equal ticker dressed as a name."""
    n = str(name or "").strip()
    w = _alnum(n)
    if not w or w in {_alnum(token), _alnum(base)}:
        return None
    return n


def _book(bid, ask, bid_qty=None, ask_qty=None) -> dict | None:
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=abs(_f(bid_qty) or 0.0), ask_qty=abs(_f(ask_qty) or 0.0))


def _levels(side) -> dict[float, float]:
    out = {}
    for lv in side or []:
        if isinstance(lv, (list, tuple)) and len(lv) >= 2:
            p, s = _f(lv[0]), _f(lv[1])
            if p is not None and s:
                out[p] = s
    return out


def build_universe(data) -> dict:
    """/symbols data → {"ins": instruments, "dashed": {cross symbol: dashed symbol} (every row), "syms": kept symbols}."""
    cc = (data or {}).get("contractConfig") if isinstance(data, dict) else None
    ins, dashed, syms = [], {}, set()
    for group in GROUPS:
        for r in (cc or {}).get(group) or []:
            if not isinstance(r, dict):
                continue
            sym, dash, tok = str(r.get("crossSymbolName") or ""), str(r.get("symbol") or ""), str(r.get("baseTokenId") or "")
            if not sym or not dash or not tok:
                continue
            dashed[sym] = dash                            # disabled rows too: their history stays readable [L]
            if str(r.get("settleAssetId") or "") != QUOTE or r.get("isPrelaunch") is True:
                continue
            if not (r.get("enableTrade") is True and r.get("enableDisplay") is True
                    and r.get("enableOpenPosition") is True):
                continue                                  # disabled (TON, TRUMP …) or hidden (IO, STBL) [L]
            cls = asset_class(group, tok, r.get("category"), r.get("tokenName"))
            base, factor = perp_base(tok, cls)
            ins.append(dict(exchange=NAME, symbol=sym, base_asset=tok, base=base, factor=factor,
                            tick_size=_f(r.get("tickSize")), step_size=_f(r.get("stepSize")),
                            min_notional=None,            # minOrderSize is a BASE quantity [L]
                            min_qty=_f(r.get("minOrderSize")), onboard_ms=0, interval_h=INTERVAL_H,
                            cap=_f(r.get("fundingMaxRate")), floor=_f(r.get("fundingMinRate")), quote=QUOTE,
                            contract="PERPETUAL", cls=cls, market=dash, token_name=r.get("tokenName"),
                            name_hint=name_hint(r.get("tokenName"), tok, base),
                            url=PAGE_URL.format(symbol=quote(sym, safe=""))))
            syms.add(sym)
    return {"ins": ins, "dashed": dashed, "syms": frozenset(syms)}


# --- per-host state ---------------------------------------------------------------------------------------------------
class _Host:
    """State of one REST host shared by every client of it in this process: the limit is per IP, and the universe and the
    REST fallback cache are the same data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()
        self.banned_until = 0.0
        self.last_hist = 0.0
        self.uni: dict | None = None
        self.uni_ts = 0.0
        self.rest_rows: dict[str, dict] = {}     # REST /ticker fallback: symbol → premium row (own obs)
        self.rr = 0                              # round-robin position of the fallback

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
class ApexPerp:
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
        self._stream: _Stream | None = None      # opened by the first premium()/books(), never by the constructor
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
        """A slot in the host's rolling minute. tick — now or BudgetExceeded (the next tick retries); hist — waits for its
        gap and for the window to drop below HIST_CAP; aux — waits for a slot below TICK_CAP."""
        h = self._h
        cap = HIST_CAP if kind == "hist" else TICK_CAP
        t_end = time.time() + (0.0 if kind == "tick" else HIST_MAX_WAIT_S if kind == "hist" else AUX_MAX_WAIT_S)
        while True:
            now = time.time()
            if now < h.banned_until:
                raise BannedError(f"{self.name}: paused until {time.strftime('%H:%M:%S', time.gmtime(h.banned_until))}")
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

    def _throttle(self, r, path: str, default_s: float):
        try:
            pause = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError, AttributeError):
            pause = default_s
        pause = max(1.0, pause)
        with self._h.lock:
            self._h.banned_until = max(self._h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s %d on %s — host paused for %.0f s", self.name, r.status_code, path, pause)
        raise BannedError(f"{self.name}: {r.status_code} on {path}, pause {pause:.0f} s")

    def _get(self, path: str, params: dict | None = None, kind: str = "aux", retries: int | None = None,
             timeout: float | None = None):
        """GET → the body's data. 429 → host pause, 403 (IP ban [D]) → long host pause, both BannedError; other 4xx and
        body code 3 (bad parameter) → PermanentHTTPError; network, 5xx, bad JSON, other body codes → retried (a tick call
        is tried once: the retry is the next tick)."""
        retries = (config.TICK_RETRIES if kind == "tick" else 3) if retries is None else retries
        timeout = (config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT) if timeout is None else timeout
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            try:
                r = self._s.get(self.rest + path, params=params, timeout=timeout)
                if r.status_code == 429:
                    self._throttle(r, path, BAN_DEFAULT_S)
                if r.status_code == 403:
                    self._throttle(r, path, BAN_403_S)
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path}: {str(getattr(r, 'text', ''))[:200]}")
                r.raise_for_status()
                body = r.json()
                if not isinstance(body, dict):
                    raise RuntimeError(f"{path}: body {str(body)[:200]}")
                code = body.get("code")
                if code not in (None, 0, "0"):
                    msg = f"{self.name} {path} code {code}: {str(body.get('msg'))[:200]}"
                    if str(code) == BAD_PARAM:
                        raise PermanentHTTPError(msg)
                    raise RuntimeError(msg)
                if "data" not in body:
                    raise RuntimeError(f"{path}: body without data: {str(body)[:200]}")
                self.last_ok_ts = time.time()
                return body["data"]
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON, foreign body code — retried
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    # --- universe --------------------------------------------------------------------------------------------------
    def _universe(self, kind: str = "aux", fresh: bool = False) -> dict:
        """The host's universe. The tick uses any cached copy (a fresh client fetches it once); perp_instruments forces a
        fresh one; history refreshes it for an unknown symbol (at most every IDS_TTL_S)."""
        h = self._h
        with h.lock:
            uni = h.uni
        if uni is not None and not fresh:
            return uni
        uni = build_universe(self._get(PATH_SYMBOLS, kind=kind))
        if not uni["ins"]:
            raise RuntimeError(f"{self.name}: /symbols without tradable USDT perps")
        with h.lock:
            h.uni, h.uni_ts = uni, time.time()
        return uni

    def perp_instruments(self) -> list[dict]:
        return [dict(i) for i in self._universe("aux", fresh=True)["ins"]]

    # --- tick ------------------------------------------------------------------------------------------------------
    def _ensure_stream(self, syms) -> "_Stream":
        if self._stream is None:
            self._stream = _Stream(self.ws_url, self.name, self._connect)
        self._stream.want(syms)
        self._stream.start()
        self._stream.wait_first(WS_FIRST_WAIT_S)
        return self._stream

    def premium(self) -> dict[str, dict]:
        """symbol → {rate (running estimate of the current hour, fraction PER HOUR = per interval), mark, index, next_ms,
        ts_ms, obs, interval_h}. From instrumentInfo.all (0 REST calls); stream not fresh → REST /ticker round-robin."""
        uni = self._universe("tick")
        info, ts_ms, rx = self._ensure_stream(uni["syms"]).info()
        now = time.time()
        now_ms = int(now * 1000)
        nxt = _next_hour(None, now_ms)
        out = {}
        if info and now - rx <= WS_FRESH_S:
            for sym, r in info.items():
                if sym not in uni["syms"]:
                    continue                              # prediction markets, collateral rows, disabled symbols
                rate = _f(r.get("fr"))
                if rate is None:
                    continue
                ss = str(r.get("ss") if r.get("ss") is not None else "0")
                if _SS_SEEN.get(sym, "0") != ss:
                    _SS_SEEN[sym] = ss
                    log.info("%s %s: symbol status ss=%s (meaning unknown, not filtered)", self.name, sym, ss)
                out[sym] = dict(rate=rate, mark=_f(r.get("mp")), index=_f(r.get("xp")), next_ms=nxt,
                                ts_ms=ts_ms or int(rx * 1000), obs=rx, interval_h=INTERVAL_H)
            if out:
                return out
        return self._rest_premium(uni, now_ms)

    def _rest_premium(self, uni: dict, now_ms: int) -> dict[str, dict]:
        """Degraded tick: at most FALLBACK_PER_TICK /ticker calls (round-robin over the universe, within
        FALLBACK_BUDGET_S; a failed call counts too), cached per host; every row keeps the moment it was fetched as obs,
        so a row that is not refreshed goes stale honestly."""
        h = self._h
        syms = sorted(uni["syms"])
        if not syms:
            return {}
        with h.lock:
            start = h.rr % len(syms)
        t_end = time.time() + FALLBACK_BUDGET_S
        i = n = 0
        stop: Exception | None = None
        while i < len(syms) and n < FALLBACK_PER_TICK and time.time() < t_end:
            sym = syms[(start + i) % len(syms)]
            i += 1
            t0 = time.time()
            try:
                data = self._get(PATH_TICKER, {"symbol": sym}, kind="tick")
            except (BannedError, BudgetExceeded) as e:
                stop = e
                i -= 1                                    # not fetched: the next pass starts here
                break
            except Exception as e:  # noqa — one symbol failing does not stop the pass
                n += 1
                log.warning("%s ticker %s: %s: %s", self.name, sym, type(e).__name__, e)
                continue
            n += 1
            row = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
            rate = _f((row or {}).get("fundingRate"))
            if row is None or rate is None:
                continue
            with h.lock:
                h.rest_rows[sym] = dict(rate=rate, mark=_f(row.get("markPrice")), index=_f(row.get("indexPrice")),
                                        next_ms=_iso_ms(row.get("nextFundingTime")), ts_ms=int(t0 * 1000), obs=t0,
                                        interval_h=INTERVAL_H)
        with h.lock:
            h.rr = (start + i) % len(syms)
            rows = {s: dict(v) for s, v in h.rest_rows.items() if s in uni["syms"]}
        if not rows and stop is not None:
            raise stop
        for v in rows.values():
            v["next_ms"] = _next_hour(v["next_ms"], now_ms)
        return rows

    def books(self) -> dict[str, dict]:
        """Best bid / ask of every symbol from the orderBook25 cache; quantities in baseTokenId units; obs = the
        connection's last market-data frame. A book whose snapshot has not arrived (or was dropped on a u gap) is absent
        — the collector prices that leg by mark."""
        uni = self._universe("tick")
        return self._ensure_stream(uni["syms"]).books(uni["syms"])

    # --- history ---------------------------------------------------------------------------------------------------
    def _market(self, symbol: str) -> str:
        uni = self._universe("hist")
        dash = uni["dashed"].get(symbol)
        if dash is None and time.time() - self._h.uni_ts >= IDS_TTL_S:
            dash = self._universe("hist", fresh=True)["dashed"].get(symbol)
        if dash is None:
            raise RuntimeError(f"{self.name}: no market {symbol}")
        return dash

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled hourly rates of one symbol in [start_ms, end_ms], ascending; rate = fraction per hour (the interval),
        mark None (the row's price is the index). Pages are walked back by endTimeExclusive = the oldest fundingTime of
        the previous page; the limit is sized to the window (a 1-2 h repair asks for 3-4 rows, 30 days for 100)."""
        dash = self._market(symbol)
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        if end_ms < start_ms:
            return []
        span_h = (end_ms - start_ms) / HOUR_MS
        limit = max(1, min(HISTORY_PAGE, int(math.ceil(span_h)) + HISTORY_SLACK))
        got: dict[int, dict] = {}
        hi = end_ms + 1                                   # endTimeExclusive
        for _ in range(HISTORY_MAX_PAGES):
            data = self._get(PATH_HIST, dict(symbol=dash, limit=limit, beginTimeInclusive=start_ms, endTimeExclusive=hi),
                             kind="hist")
            rows = [r for r in (data.get("historyFunds") or []) if isinstance(r, dict)] if isinstance(data, dict) else []
            stamps = []
            for r in rows:
                ms = _int(r.get("fundingTime"), None) or _int(r.get("fundingTimestamp"), None)
                if not ms:
                    continue
                stamps.append(ms)
                rate = _f(r.get("rate"))
                if rate is not None and start_ms <= ms <= end_ms:
                    got[ms] = dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=None)
            if len(rows) < limit or not stamps:
                break
            lo = min(stamps)
            if lo <= start_ms:
                break
            if lo >= hi:
                raise RuntimeError(f"{self.name} {symbol}: history page did not move below {hi}")
            hi = lo
        else:
            # every page full and start_ms still not reached: a silent cut would confirm depth that was never fetched
            raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {HISTORY_MAX_PAGES} pages")
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """No «settled rates of all symbols» endpoint (history needs one symbol [D/L]). Upkeep is the per-leg top-up by
        completeness, like Hyperliquid and Lighter."""
        return []

    def close(self):
        if self._stream is not None:
            self._stream.stop()


PERP_CLIENTS = {NAME: ApexPerp}


# --- WebSocket: instrumentInfo.all + orderBook25 per symbol on one connection ------------------------------------------
def _default_connect(url: str, timeout: float):
    from .lighter import _WS                             # minimal RFC 6455 client (no websocket library in the venv)
    return _WS(url, timeout)


def _topic(sym: str) -> str:
    return TOPIC_BOOK + sym


class _Stream:
    """One connection in a daemon thread: instrumentInfo.all (rates, liveness) + orderBook25.H.<symbol> for every wanted
    symbol → caches; new symbols are subscribed on the live connection, all of them on a reconnect; a u gap re-subscribes
    that book; server ping answered, client ping every WS_PING_S, silence watchdog, reconnect with backoff."""

    def __init__(self, url: str, name: str, connect=None):
        self.url, self.name = url, name
        self._connect = connect or _default_connect
        self._lock = threading.Lock()
        self._info: dict[str, dict] = {}
        self._info_ts: int | None = None                  # server ts of the last instrumentInfo.all frame (ms)
        self._info_rx = 0.0                               # its arrival
        self._books: dict[str, dict] = {}                 # symbol → {"b": {px: qty}, "a": {px: qty}, "u": seq}
        self._want: set[str] = set()
        self._resub: set[str] = set()
        self._resub_ts: dict[str, float] = {}
        self.last_rx = 0.0                                # last market-data frame of the current connection
        self.synced = threading.Event()                   # instrumentInfo.all arrived on the current connection
        self.n_conn = 0
        self.n_ping = 0
        self.n_gap = 0
        self.err: str | None = None
        self.backoff0 = 1.0
        self._waited = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def want(self, symbols):
        s = {str(x) for x in symbols or () if x}
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

    def info(self) -> tuple[dict[str, dict], int | None, float]:
        with self._lock:
            return dict(self._info), self._info_ts, self._info_rx

    def books(self, symbols=None) -> dict[str, dict]:
        out = {}
        with self._lock:
            last = self.last_rx
            for sym, bk in self._books.items():
                if symbols is not None and sym not in symbols:
                    continue
                if not bk["b"] or not bk["a"]:
                    continue
                bp, ap = max(bk["b"]), min(bk["a"])       # snapshot sides are ascending: best bid is the LAST [L]
                b = _book(bp, ap, bk["b"][bp], bk["a"][ap])
                if b is not None:
                    b["obs"] = last
                    out[sym] = b
        return out

    def _ask_resub(self, sym: str, rx: float):
        if rx - self._resub_ts.get(sym, -1e18) >= RESUB_MIN_S:
            self._resub_ts[sym] = rx
            self._resub.add(sym)

    def apply(self, msg: dict, rx: float) -> bool:
        """One JSON message → caches. True for market data. instrumentInfo.all frames are full snapshots [L] and replace
        the rate cache; books: snapshot replaces, delta merges (size 0 deletes), a u gap drops the book and asks for a
        re-subscription."""
        top = msg.get("topic")
        if not isinstance(top, str):
            return False
        d = msg.get("data")
        if top == TOPIC_INFO:
            if not isinstance(d, list):
                return False
            rows = {str(r["s"]): r for r in d if isinstance(r, dict) and r.get("s")}
            with self._lock:
                if str(msg.get("type") or "snapshot") == "snapshot":
                    self._info = rows
                else:
                    self._info = {**self._info, **rows}   # a «delta» was never seen [A]: merge per symbol
                self._info_ts = _ts_ms(msg.get("ts"))
                self._info_rx = self.last_rx = rx
            self.synced.set()
            return True
        if not top.startswith(TOPIC_BOOK) or not isinstance(d, dict):
            return False
        sym = str(d.get("s") or top[len(TOPIC_BOOK):])
        typ = str(msg.get("type") or "")
        u = _int(d.get("u"), None)
        with self._lock:
            if typ == "snapshot":
                self._books[sym] = {"b": _levels(d.get("b")), "a": _levels(d.get("a")), "u": u}
            elif typ == "delta":
                bk = self._books.get(sym)
                if bk is None:
                    self._ask_resub(sym, rx)              # a delta without its snapshot: ask for one (rate-limited)
                elif u is not None and bk["u"] is not None and u != bk["u"] + 1:
                    if u > bk["u"]:
                        del self._books[sym]              # a hole in the sequence: the book is no longer the venue's
                        self.n_gap += 1
                        self._ask_resub(sym, rx)
                        log.warning("%s book %s: u %s → %s — re-subscribing", self.name, sym, bk["u"], u)
                else:
                    for side in ("b", "a"):
                        lv = bk[side]
                        for x in d.get(side) or []:
                            if not isinstance(x, (list, tuple)) or len(x) < 2:
                                continue
                            p, s = _f(x[0]), _f(x[1])
                            if p is None or s is None:
                                continue
                            if s == 0:
                                lv.pop(p, None)
                            else:
                                lv[p] = s
                    bk["u"] = u if u is not None else bk["u"]
            else:
                return False
            self.last_rx = rx
        return True

    def _send(self, conn, op: str, topics: list[str]):
        for k in range(0, len(topics), BOOK_SUB_CHUNK):
            conn.send_text(json.dumps({"op": op, "args": topics[k:k + BOOK_SUB_CHUNK]}))

    def _session_once(self) -> bool:
        """One connection until it fails. True if instrumentInfo.all arrived (a healthy session resets the backoff)."""
        conn, healthy = None, False
        try:
            conn = self._connect(self.url.format(ms=int(time.time() * 1000)), WS_RECV_TIMEOUT_S)
            with self._lock:
                self.n_conn += 1
                self._books = {}                          # rebuilt from this connection's snapshots
                self._resub.clear()
                self._resub_ts.clear()
            conn.send_text(json.dumps({"op": "subscribe", "args": [TOPIC_INFO]}))
            subbed: set[str] = set()
            last = pinged = time.time()
            while not self._stop.is_set():
                with self._lock:
                    new = sorted(self._want - subbed)
                    resub = sorted(self._resub & subbed)
                    self._resub.clear()
                if new:
                    self._send(conn, "subscribe", [_topic(s) for s in new])
                    subbed.update(new)
                if resub:
                    self._send(conn, "unsubscribe", [_topic(s) for s in resub])   # a plain re-subscribe is refused [L]
                    self._send(conn, "subscribe", [_topic(s) for s in resub])
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
                    if isinstance(msg, dict):
                        if msg.get("op") == "ping":
                            conn.send_text(json.dumps({"op": "pong", "args": msg.get("args")}))
                            self.n_ping += 1
                        elif msg.get("success") is False:
                            log.warning("%s ws: %s", self.name, str(msg.get("ret_msg"))[:200])
                        elif self.apply(msg, now):
                            last = now
                            healthy = healthy or self.synced.is_set()
                if now - last > WS_SILENT_S:
                    raise TimeoutError(f"no market data for {now - last:.0f} s")
                if now - pinged >= WS_PING_S:
                    conn.send_text(json.dumps({"op": "ping", "args": [str(int(now * 1000))]}))
                    pinged = now
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
