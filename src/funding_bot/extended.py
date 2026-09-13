"""Extended — perp venue «extended» (owner 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica, apex во
фьючи»; the fourth of the six, in the owner's order after lighter_rh). Starknet DEX perps, USD collateral, linear: one
contract unit = one unit of assetName (1000PEPE = 1000 PEPE, kNOT = 1000 NOT). Native perp client of venues.py:
perp_instruments / premium / books / history_since / recent_history. Public endpoints only — no keys, no account, no orders.

Measured 12-13.09.2026 from the Mac. Tags: [L] verified live, [D] docs (api.docs.extended.exchange, docs.extended.exchange),
[A] assumption.
Host [L/D]  REST https://api.starknet.extended.exchange/api/v1 (api.extended.exchange/api/v1/info/markets → 404). A
      User-Agent is mandatory (empty → 403). Envelope {"status":"OK","data":…}; errors are HTTP 400 {"status":"ERROR",
      "error":{"code","message"}} — 1001 «Market not found», 1006 «startTime is missing» → PermanentHTTPError. A 200 whose
      status is not OK is retried like a 5xx [A].
Universe + tick [L]  GET /info/markets — every market in one array (≈976 KB, not gzipped even when asked; 0.35–0.8 s warm,
      2.6 s cold). Cache-Control max-age=60, yet the body is live (150–249 of 399 markPrices moved between 10 s polls).
      One call per tick feeds premium() AND books() (cached ctx_ttl_s from ARRIVAL, like Hyperliquid / Gate); the hourly
      universe call seeds the same cache. obs = the request start, or the response's Date header when that is more than
      DATE_STALE_S older (a cached copy shows its real age).
      Tradable = type PERPETUAL (three SPOT rows are mixed in) + status ACTIVE + active + visibleOnUi. Out: PRELISTED (12
      RWA, hidden, synthetic ±3 % books), REDUCE_ONLY (MKR, zero bid), DELISTED; DISABLED [D]. isRfq is NOT a filter: it
      is true on 275 of 323 active markets (BTC false), and every active market, RFQ or not, shows a two-sided bid/ask
      (off-hours ones a synthetic band, see Books) [L].
Rate [L/D]  marketStats.fundingRate — signed FRACTION PER 1 HOUR (not 8 h, not annualised): (avgPremium + clamp(interest −
      avgPremium, ±0.05 %)) / 8, applied hourly [D]; the crypto baseline 0.000013 = 0.01 %/8 h ÷ 8 [L]. It is the running
      PREDICTED rate of the current hour (recomputed ~every minute [D]): at the 22:00Z settlement of 12.09 the settled f
      equalled the last pre-hour fundingRate on 12/12 markets [L]. 6 decimals → resolution 1e-6/h. So premium() passes it
      through unchanged with interval_h 1. nextFundingRate is misnamed: the next settlement TIME in ms, one value for
      every perp [L/D].
Books [L]  marketStats.bidPrice / askPrice = the top of the book (BTC bid equalled /info/markets/BTC-USD/orderbook's best
      bid). The list has no sizes → bid_qty / ask_qty 0.0 (as Lighter / Hyperliquid). Off-hours markets (isOffHours: 64–65
      on the weekend — NO_OVERNIGHT, WEEKDAYS and some CONTINUOUS stocks) show mark == index frozen and a synthetic ±3 %
      band (SHOP 124.741 / 132.511 around 128.625); order placement is unavailable while halted [D] → their book is
      dropped and their rate kept (funding still settles hourly at a flat baseline [L]).
History [L]  GET /info/{name}/funding?startTime&endTime (both required) → [{"m","f","T"}] NEWEST FIRST, at most 1000 rows =
      the newest in the window (the documented limit / cursor are ignored: limit=5 and 10000 both gave 720 rows). Older
      windows page backwards with endTime = min(T) − 1 (60 days of 1000PEPE = 1000 + 440 rows, 0 missing hours). T lands
      0.77–1.03 s after the hour (983/984 rows in 41 days; outliers +2–60 s and +559 s) → funding_ms = T floored to the
      hour (funding.py matches settlements by minute), and endTime gets LATE_MS of slack so the late row of the window's
      last hour is not cut off. Every market (crypto, RWA, off-hours, 24/5, even DELISTED) is on an exact 60-min grid →
      fixed 1 h (config.FIXED_INTERVAL_H); delisted markets keep printing flat rows — liveness never comes from history.
      No all-markets batch → recent_history() is [] and upkeep is the per-leg repair (as Hyperliquid, Lighter).
Limits [D/L]  1000 requests / min per IP, 429 beyond [D]; no rate headers, a 40-call burst over 11 s was all 200 [L]. Own
      rolling window: the tick is refused above TICK_CAP, history waits above HIST_CAP; 429 → host pause for Retry-After
      or 60 s (BannedError) [A]. The pause, the window and the history pace are per host, shared by every client of it in
      the process (the collector's background copies from make_clients()). ≈1 MB per tick ≈ 8.4 GB/day at 10 s [L size].
Classes [L data, A rule]  category Crypto → crypto (PAXG / XAUT carry subCategory Commodity — coins by the repo rule);
      RWA: Equity → equity; ETF/Index → equity when referenceMarket is us_equity (DRAM, SOXL, KORU, EWY are ETF shares),
      else index (SPX500m / TECH100m us_index_fut, JP225 jp_equity); Commodity → commodity; FX → fx; Pre-market → preipo;
      anything else → «rwa» (pairs with nothing). 13.09 active mix: crypto 196, equity 113 (4 of them ETF shares),
      commodity 7, index 3, fx 2, preipo 2, rwa 0 [L].
Bases  assetName without the «_24_5» suffix (the 24/5-oracle twin of a stock) → norm_symbol_factor (1000PEPE → PEPE ×1000;
      kNOT, kXEC, kNEIRO … → ×1000 by the lower-case k; KLAC / KORU / KIOXIA stay) → LOCAL_CANON by definition (WTI → CL,
      XBR → BZ, XNG → NATGAS; SPX500m «S&P 500» → SP500 as Lighter US500; TECH100m «Nasdaq-100» and JP225 not mapped) →
      config.PERP_CANON (XCU → COPPER, USDJPY → JPY, STXX → STX). SKHYNIX (Korean share) and SKHY (ADR) stay apart. Two
      tradable markets on one (cls, base, factor) — a stock and its _24_5 twin — keep the higher dailyVolume (none on
      13.09: 32 active twins, their plain stocks are not active [L]).
Fees [D]  taker 0.025 % / maker 0 %, flat (docs «Trading fees and rebates»; /user/fees needs a key) → config.FEES_TAKER.
Links [L, browser]  https://app.extended.exchange/trade/{uiName} — the app routes by uiName, NOT name (kPEPE-USD,
      NVDA-USD, NATGAS-USD open; 1000PEPE-USD and NVDA_24_5-USD silently show BTC-USD; 40 active markets are renamed), so
      the link goes into the instrument («url», calc.url prefers it). SPX500m's uiName is SPX-USD, the SPX6900 coin's is
      SPX6900-USD — no uiName is shared [L]. The app answers 403 to curl — links are never health-checked.
Identity  No index composition (Stork oracle, 5 nodes [D]) and no token contracts for perps (/info/assets carries l1Id only
      on the 5 SPOT assets) [L]. The only evidence is the declared full name (description) → name_hint: an oracle venue
      (identity.ORACLE_PERPS), never by price. Traps [L]: PURR-USD is Hyperliquid Strategies Inc. (equity), QNT Quantinuum,
      BB BlackBerry (the class keeps them off the coins); SPX-USD is the SPX6900 coin, SPX500m the S&P 500; MKR's
      description is just «MKR». tradingConfig.hourlyFundingRateCap mixes units (123 crypto markets «0.006» while ONG
      settled −0.000497/h) → cap = floor = None.
"""
from __future__ import annotations
import logging, re, threading, time
from collections import deque
from email.utils import parsedate_to_datetime
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "extended"
REST = "https://api.starknet.extended.exchange/api/v1"
PAGE_URL = "https://app.extended.exchange/trade/{ui}"   # routes by uiName [L]; the SPA never 404s → live markets only
PATH_MARKETS = "/info/markets"
QUOTE = "USD"
HOUR_MS = 3600_000
INTERVAL_H = 1                  # every market settles hourly on the hour [D/L]
HISTORY_PAGE = 1000             # rows per call = the newest in the window [L]; 30 days = 720 rows → one call per leg
HISTORY_MAX_PAGES = 12          # 12 × 1000 h ≈ 500 days (BTC rows go 300–400 days back); more raises, never truncates
LATE_MS = 600_000               # endTime slack: a settlement row lands up to +559 s after its hour [L]
HISTORY_GAP_S = 0.25            # config.FUNDING_HISTORY_MIN_GAP_S overrides by venue name
REQ_PER_MIN = 1000              # doc: per IP per minute [D]
TICK_CAP = 600                  # own ceiling: tick refused / hourly calls wait above this many requests in the last 60 s
HIST_CAP = 500                  # history waits while the window holds this many: 100 slots stay for the tick (6/min)
HIST_MAX_WAIT_S = 90.0          # history gives up (BudgetExceeded → venue skipped this pass) after waiting this long
AUX_MAX_WAIT_S = 30.0           # the hourly universe call waits at most this long for a slot
BAN_DEFAULT_S = 60.0            # 429 without Retry-After → host pause [A]
SPREAD_MAX = 0.05               # [A] a book wider than 5 % of mid is not a price (as lighter._book); on-hours max 13.09 < 5 %
DATE_STALE_S = 5.0              # Date header older than the request start by more than this → the snapshot is a cached copy
OBS_SKEW_S = 3600.0             # a Date header further off than this is not believed (broken clock / header)
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})                            # gold tokens are coins (repo rule)
CRYPTO_SECTORS = frozenset({"L1", "L2", "INFRA", "DEFI", "MEME", "AI"})   # legacy category values of old listings [L]
LOCAL_CANON = {"commodity": {"WTI": "CL", "XBR": "BZ", "XNG": "NATGAS"},  # WTI Crude Oil / Brent Crude Oil / Natural Gas
               "index": {"SPX500M": "SP500"}}                         # «S&P 500» (Lighter US500 → SP500, HL xyz:SP500)
_FX = re.compile(r"^(?:([A-Z]{3})USD|USD([A-Z]{3}))$")              # EURUSD → EUR, USDJPY → JPY (as HL xyz:EUR, xyz:JPY)
_TWIN = re.compile(r"_24_5$")
_WARNED: set[str] = set()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x, default: int = 0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _ref(m: dict) -> str | None:
    r = str(m.get("referenceMarket") or "").strip().lower()
    return None if r in ("", "null", "none") else r              # the API sends the STRING "null" [L]


def tradable(m: dict) -> bool:
    """An open-able perpetual: type PERPETUAL, status ACTIVE, active, not hidden in the app."""
    return (isinstance(m, dict) and m.get("type") == "PERPETUAL" and m.get("status") == "ACTIVE"
            and m.get("active", True) is not False and m.get("visibleOnUi", True) is not False)


def perp_class(m: dict) -> str:
    """crypto / equity / index / commodity / fx / preipo from category + subCategory (+ referenceMarket for ETF/Index);
    «rwa» for a real-world class nobody has mapped yet — it pairs with nothing."""
    asset = _TWIN.sub("", str(m.get("assetName") or "")).upper()
    cat = str(m.get("category") or "").strip().upper()
    sub = str(m.get("subCategory") or "").strip().upper()
    if asset in GOLD_TOKENS or cat == "CRYPTO" or cat in CRYPTO_SECTORS:
        return "crypto"
    if cat == "RWA":
        if sub == "EQUITY":
            return "equity"
        if sub == "ETF/INDEX":
            return "equity" if _ref(m) == "us_equity" else "index"     # DRAM / SOXL shares vs S&P 500, Nikkei 225
        if sub == "COMMODITY":
            return "commodity"
        if sub == "FX":
            return "fx"
        if sub == "PRE-MARKET":
            return "preipo"
    key = f"{cat}/{sub}"
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning("%s: class %s (%s) is not mapped — «rwa», pairs with nothing", NAME, key, m.get("name"))
    return "rwa"


def perp_base(asset_name: str, cls: str) -> tuple[str, float]:
    """assetName → (canonical base, tokens per contract unit): NVDA_24_5 → NVDA, 1000PEPE → PEPE ×1000, kNOT → NOT ×1000,
    SPX500m → SP500 (index), XNG → NATGAS, USDJPY → JPY, STXX → STX."""
    base, factor = norm_symbol_factor(_TWIN.sub("", str(asset_name or "")))
    if cls == "fx":
        mm = _FX.match(base)
        if mm:
            base = mm.group(1) or mm.group(2)
    base = LOCAL_CANON.get(cls, {}).get(base, base)
    return config.PERP_CANON.get(cls, {}).get(base, base), factor


def _book(bid, ask) -> dict | None:
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=0.0, ask_qty=0.0)


def _obs(date_hdr, t0: float) -> float:
    """Moment of observation: the request start, unless the server's Date says the body is clearly older (a cached copy
    under Cache-Control max-age=60). Date has 1 s resolution — a few seconds of it (or of clock skew) are ignored."""
    try:
        s = parsedate_to_datetime(date_hdr).timestamp() if date_hdr else None
    except (TypeError, ValueError, IndexError, OverflowError):
        s = None
    if s is not None and t0 - OBS_SKEW_S <= s < t0 - DATE_STALE_S:
        return s
    return t0


def _err_text(r) -> str:
    try:
        e = (r.json() or {}).get("error") or {}
        if isinstance(e, dict) and (e.get("code") is not None or e.get("message")):
            return f"code {e.get('code')}: {e.get('message')}"
    except Exception:  # noqa — not JSON: raw text below
        pass
    return str(getattr(r, "text", ""))[:200]


# --- per-host state ---------------------------------------------------------------------------------------------------
class _Host:
    """State of one REST host shared by every client of it in this process: the limit is per IP."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()
        self.banned_until = 0.0
        self.last_hist = 0.0

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
class ExtendedPerp:
    name = NAME

    def __init__(self, session: requests.Session | None = None, history_gap_s: float | None = None,
                 ctx_ttl_s: float = 5.0, rest: str = REST):
        self.rest = rest
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT          # mandatory: an empty UA gets 403 [L]
        self._h = _host(rest)
        self.history_gap_s = (config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, HISTORY_GAP_S)
                              if history_gap_s is None else history_gap_s)
        self.ctx_ttl_s = ctx_ttl_s
        self._lock = threading.Lock()
        self._tl = threading.local()                                # Date header of the last response on this thread
        self._snap: tuple[float, float, dict[str, dict]] = (0.0, 0.0, {})   # (arrival, obs, name → market)
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
                raise BudgetExceeded(f"{self.name}: {n} requests in the last 60 s (own cap {cap}), {kind} call skipped")
            time.sleep(min(wait, 5.0))

    def _throttle(self, r, path: str):
        try:
            pause = float((r.headers or {}).get("Retry-After"))
        except (TypeError, ValueError):
            pause = BAN_DEFAULT_S
        pause = max(1.0, pause)
        with self._h.lock:
            self._h.banned_until = max(self._h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s 429 on %s — host paused for %.0f s", self.name, path, pause)
        raise BannedError(f"{self.name}: 429 on {path}, pause {pause:.0f} s")

    def _get(self, path: str, params: dict | None = None, kind: str = "aux", retries: int | None = None,
             timeout: float | None = None):
        """GET → body["data"]. 429 → host pause (BannedError); other 4xx → PermanentHTTPError with the API's error code;
        5xx, network, bad JSON and a 200 whose status is not OK are retried."""
        retries = (config.TICK_RETRIES if kind == "tick" else 3) if retries is None else retries
        timeout = (config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT) if timeout is None else timeout
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            try:
                r = self._s.get(self.rest + path, params=params, timeout=timeout)
                if r.status_code == 429:
                    self._throttle(r, path)
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path}: {_err_text(r)}")
                r.raise_for_status()
                body = r.json()
                if not isinstance(body, dict) or body.get("status") != "OK" or "data" not in body:
                    raise RuntimeError(f"{path}: status {body.get('status') if isinstance(body, dict) else '?'}: "
                                       f"{str(body)[:200]}")
                hdr = r.headers or {}
                self._tl.date = hdr.get("Date") or hdr.get("date")
                self.last_ok_ts = time.time()
                return body["data"]
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON, status ERROR on a 200 — retried
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    # --- the markets snapshot (universe + tick) -------------------------------------------------------------------
    def _fetch(self, kind: str) -> tuple[float, dict[str, dict]]:
        self._tl.date = None
        t0 = time.time()
        data = self._get(PATH_MARKETS, kind=kind)
        t1 = time.time()
        if not isinstance(data, list):
            raise RuntimeError(f"{self.name} {PATH_MARKETS}: expected a list, got {str(data)[:200]}")
        rows = {str(m["name"]): m for m in data if isinstance(m, dict) and m.get("name")}
        if not rows:
            raise RuntimeError(f"{self.name}: markets list is empty")
        obs = _obs(getattr(self._tl, "date", None), t0)
        with self._lock:
            self._snap = (t1, obs, rows)
        return obs, rows

    def _snapshot(self) -> tuple[float, dict[str, dict]]:
        """(obs, name → market). TTL counts from ARRIVAL: premium() and books() of one tick share one ≈1 MB download."""
        with self._lock:
            arr, obs, rows = self._snap
        if rows and time.time() - arr < self.ctx_ttl_s:
            return obs, rows
        return self._fetch("tick")

    # --- native perp interface ----------------------------------------------------------------------------------------
    def perp_instruments(self) -> list[dict]:
        _obs_s, rows = self._fetch("aux")
        cands = []
        for name, m in rows.items():
            if not tradable(m):
                continue
            cls = perp_class(m)
            asset = str(m.get("assetName") or name.rsplit("-", 1)[0])
            base, factor = perp_base(asset, cls)
            tc, ms = m.get("tradingConfig") or {}, m.get("marketStats") or {}
            mn, mark = _f(tc.get("minOrderSize")), _f(ms.get("markPrice"))
            ui = str(m.get("uiName") or "").strip()
            cands.append(dict(exchange=self.name, symbol=name, base_asset=asset, base=base, factor=factor,
                              tick_size=_f(tc.get("minPriceChange")), step_size=_f(tc.get("minOrderSizeChange")),
                              # minOrderSize is in contract units (BTC 0.0001, 1000PEPE 1000) [L] → USD at the mark [A]
                              min_notional=mn * mark if mn and mark else None, onboard_ms=_int(m.get("createdAt")),
                              interval_h=INTERVAL_H, cap=None, floor=None, quote=QUOTE, contract="PERPETUAL", cls=cls,
                              name_hint=str(m.get("description") or "").strip() or None,
                              url=PAGE_URL.format(ui=quote(ui, safe="")) if ui else None,
                              volume=_f(ms.get("dailyVolume")) or 0.0))
        out = self._dedup(cands)
        if not out:
            raise RuntimeError(f"{self.name}: markets list without tradable perpetuals")
        for i in out:
            i.pop("volume", None)
        return out

    def _dedup(self, cands: list[dict]) -> list[dict]:
        """One market per (cls, base, factor): a stock and its _24_5 twin are the same share — the higher dailyVolume
        stays (as exchanges.keep_best_quote keeps one market per coin)."""
        best: dict[tuple, dict] = {}
        for i in cands:
            k = (i["cls"], i["base"], i["factor"])
            o = best.get(k)
            if o is None:
                best[k] = i
                continue
            keep, drop = (i, o) if (i["volume"], i["symbol"]) > (o["volume"], o["symbol"]) else (o, i)
            best[k] = keep
            log.warning("%s: %s and %s are one %s market (%s ×%g) — kept %s (daily volume %.0f vs %.0f)", self.name,
                        keep["symbol"], drop["symbol"], k[0], k[1], k[2], keep["symbol"], keep["volume"], drop["volume"])
        keep_ids = {id(v) for v in best.values()}
        return [i for i in cands if id(i) in keep_ids]

    def premium(self) -> dict[str, dict]:
        """name → {rate (predicted rate of the current hour, fraction per 1 h — passed through), mark, index, next_ms,
        ts_ms, obs, interval_h}. Off-hours markets are kept: their funding still settles."""
        obs, rows = self._snapshot()
        now_ms = int(time.time() * 1000)
        grid_next = (now_ms // HOUR_MS + 1) * HOUR_MS
        out = {}
        for name, m in rows.items():
            if not tradable(m):
                continue
            ms = m.get("marketStats") or {}
            rate = _f(ms.get("fundingRate"))
            if rate is None:
                continue
            nxt = _int(ms.get("nextFundingRate"))                   # the next settlement TIME in ms, despite the name
            if not now_ms < nxt <= now_ms + 2 * HOUR_MS:
                nxt = grid_next
            out[name] = dict(rate=rate, mark=_f(ms.get("markPrice")), index=_f(ms.get("indexPrice")), next_ms=nxt,
                             ts_ms=int(obs * 1000), obs=obs, interval_h=INTERVAL_H)
        return out

    def books(self) -> dict[str, dict]:
        """Top of the book from the same snapshot; sizes are not in the list (0.0). Off-hours books (synthetic ±3 % band,
        no trading), empty, crossed and wider than SPREAD_MAX books are dropped."""
        obs, rows = self._snapshot()
        out = {}
        for name, m in rows.items():
            if not tradable(m) or m.get("isOffHours"):
                continue
            ms = m.get("marketStats") or {}
            b = _book(ms.get("bidPrice"), ms.get("askPrice"))
            if b is not None:
                b["obs"] = obs
                out[name] = b
        return out

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled hourly rates of one market with funding_ms in [start_ms, end_ms], ascending. funding_ms = T floored to
        the hour. One call = the newest 1000 rows of the window (30 days fit one call); an older window pages backwards
        with endTime = min(T) − 1. A window that needs more than HISTORY_MAX_PAGES raises — never a silent cut."""
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        if end_ms < start_ms:
            return []
        path = f"/info/{quote(symbol, safe='')}/funding"
        got: dict[int, tuple[int, dict]] = {}
        hi = end_ms + LATE_MS
        for _ in range(HISTORY_MAX_PAGES):
            rows = self._get(path, {"startTime": start_ms, "endTime": hi}, kind="hist")
            if not isinstance(rows, list):
                raise RuntimeError(f"{self.name} {path}: expected a list, got {str(rows)[:200]}")
            stamps = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                t = _int(r.get("T"), -1)
                if t < 0:
                    continue
                stamps.append(t)
                rate = _f(r.get("f"))
                if rate is None or (r.get("m") and r.get("m") != symbol):
                    continue
                fms = t - t % HOUR_MS
                if start_ms <= fms <= end_ms and (fms not in got or t < got[fms][0]):
                    got[fms] = (t, dict(exchange=self.name, symbol=symbol, funding_ms=fms, rate=rate, mark=None))
            if len(rows) < HISTORY_PAGE or not stamps:
                break
            oldest = min(stamps)
            if oldest - oldest % HOUR_MS <= start_ms:
                break
            hi = oldest - 1
        else:
            raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {HISTORY_MAX_PAGES} pages")
        return [got[k][1] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """No «settled rates of all markets» endpoint: /info/{market}/funding is per market [L]. Upkeep is the per-leg
        top-up by completeness."""
        return []


PERP_CLIENTS = {NAME: ExtendedPerp}
