"""Variational Omni — perp venue «variational» (owner 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica,
apex во фьючи» — in that order after lighter_rh). RFQ perps settled in USDC on Arbitrum, priced off Variational's own
oracle. Native perp client of venues.py: perp_instruments / premium / books / history_since / recent_history, plus the
funding_intervals() hook. Public endpoint only: no keys, no account, no orders.

Measured 12.09.2026 21:30–22:10 UTC from the Mac. Tags: [L] verified live, [D] docs.variational.io, [A] assumption.
Source [D/L]  ONE public endpoint: GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats — every
      listing in one JSON (553 on 12.09, ~281 KB), no params, no pagination. The web app's /api/funding/v2,
      /api/metadata/supported_assets and /api/candles sit behind a Cloudflare JS challenge (403 «Just a moment…») — not
      used, never bypassed. Guessed paths on the API host answer 404 [L]. There is no trading/market-data API yet [D].
Cache [L]  Cloudflare: cache-control public, s-maxage=60, max-age=30; age 49–52 s, cf-cache-status HIT; last-modified =
      the origin's snapshot time (a cache-buster gave MISS with a fresh last-modified and IDENTICAL content over 13 s:
      the origin changes about once a minute). No buster here. obs of every row = the OLDER of last-modified and
      request − age (they agreed within 1 s at 21:35; at 22:23 last-modified was 25 s newer than date − age [L]):
      the snapshot IS up to ~60 s old, and that is what the dashboard must see.
Limits [D/L]  10 requests / 10 s per IP, 1000 / min global [D]; no rate headers [L]. Budget: ONE GET per tick shared by
      premium() and books() (cached CACHE_TTL_S), the hourly universe and the 15-min intervals reuse a snapshot younger
      than AUX_REUSE_S → ≤ 6 requests a minute. 429 and 403 (the challenge) pause the host: Retry-After, else 60 s
      doubling on each consecutive strike up to BAN_MAX_S [A] (the documented strikes/bans are for order placement [D]).
Fields [D/L]  listings[]: ticker, name (full name), mark_price, funding_rate, funding_interval_s, base_spread_bps, volume_24h,
      open_interest{…}, quotes{updated_at (ISO, NANOseconds), base, size_1k, size_100k, size_1m (7 markets)}. All numbers
      are strings. ABSENT: index price, next funding time, status, tick / lot size, min notional, class, contracts.
Rate [D/L]  funding_rate is an ANNUALISED decimal (simple, 365 d): per interval = rate × interval_h / 8760. Evidence:
      0.1095 = 0.00125 %/h interest × 8760 on 327 of 553 (coins inside the ±0.05 % clamp) [D/L]; pre-IPO fixed 0.005 %/8 h
      = 0.05475 on OPENAI / ANTHROPIC [D/L]; BTC 0.056431 → 5.15e-5 /8 h vs Binance 4.86e-5, Bybit 4.75e-5; STORJ −93.62
      (1 h) → −1.07 %/h vs Bybit −1.21 % [L]. Plus = longs pay [D]. It is the PREDICTED rate of the running window,
      recomputed for all listings together every 5 min at :x0/:x5 [L]; cap 2 %/h [D] → cap per interval 0.02 × interval_h.
Interval [D/L]  3600 ×3, 14400 ×305, 28800 ×239, 0 ×6 (swaps) [L]; follows Bybit, else Binance, else 1 h [D] and changes on
      the fly (equities go 1 h on ex-dividend eve [D]) → funding_intervals() from the snapshot; NOT in FIXED_INTERVAL_H.
      next_ms is not published: the UTC epoch grid of the interval (= Bybit nextFundingTime for BTC/WIF/XDC/STORJ [L/A]).
History [D/L]  NONE public (only the user's own CSV export [D]). The settled rate is never published and there is no
      settlement event: across 22:00 UTC the 1 h STORJ estimate went −132.14 → −136.95 → −137.66 without a reset [L].
      So history is our own ledger: per ticker the last estimate (and mark) seen in the running window becomes the
      settlement row of the window's boundary at the first observation after it — an approximation of a number nobody
      publishes [A]. A window whose last estimate is older than EMIT_MAX_AGE_S at the boundary, a skipped window, an
      interval change mid-window or a process restart is a hole, never a guess. history_since() therefore answers only
      for windows the ledger observed continuously and raises NoHistoryError otherwise — an empty answer would let
      funding.sync_leg confirm 30 days of depth that were never fetched. recent_history() is a batch that is complete by
      construction over its window (see there). Everything is in memory, per host, shared by every client instance of
      this process (the collector's background copies from make_clients()); a restart starts from nothing.
Books [D/L]  bid/ask = quotes.size_1k — the RFQ quote for 1 000 USDC notional [D]; quantity = 1000 / price in units of the
      price [A]. Quotes are INDICATIVE [D], refreshed about once a minute per listing and «may be cached up to 600 s» [D];
      51–109 s old at response time on 12.09 (median 70) [L] → with config.STALE_S = 60 most books show as stale unless
      the venue gets its own allowance (config_needed). obs = updated_at. The mark can sit outside the quotes (STORJ mark
      0.04162 vs 1k 0.04221/0.04235) [L]. No quotes on the 6 swaps.
Classes (no class field) [L/D]  in order: interval 0 or «Swap on …» → excluded (USOILP, UKOILP, US500S, US100S, XAUS, XAGS:
      swaps funded at TradFi financing cost [D]); OPENAI / ANTHROPIC → preipo [D]; the doc's TradFi commodity table → commodity
      (PAXG / XAUT stay coins, repo rule); a corporate name (Inc., Corp., plc, Ltd., Holdings, ETF, N.V., «& Co.» …) →
      equity; else crypto. Cross-check, sticky within the process: an exact 0 is the TradFi rate (interest 0), an exact
      0.1095 the crypto one; a coin-named ticker with a TradFi-shaped rate, an equity/commodity with the crypto rate, or both
      seen → «rwa» (pairs with nothing, as lighter.py). 12.09: 103 equity (incl. JPM «JPMorgan Chase & Co.», which a bare
      \\b before «&» misses), 8 commodity, 2 preipo, 6 excluded, 434 crypto. B3 «B3 (Base)» was «rwa» until 13.09 (mark
      10 % off Binance B3USDT) — resolved as the coin, see UNVERIFIED.
Bases [L]  norm_symbol_factor: 1000PEPE → PEPE ×1000, 1000000MOG → MOG ×1e6. Disambiguation suffixes → the common ticker,
      by full name (price only as a unit check, within 0.3 %): 1NEIRO → NEIRO, FF0 → FF, BTR0 → BTR, OPN_OPINION → OPN,
      RE_ETH → RE. Equity canon of config.PERP_CANON (QNTX → QNT, STXX → STX). US500 is the SPY ETF share (mark 765.7),
      not the S&P index — kept as US500 (mapping to SPY is the owner's call).
Identity  the only evidence is the full name → name_hint (None when the name is just the ticker: «1000BONK», «0G», «4»).
      Variational runs its own oracle (weighted multi-exchange feed, composition unpublished [D]) — an oracle venue for
      identity.py. Traps [L]: SPX = SPX6900, CAT = Caterpillar Inc., TA = Trusta AI, US = Talus, LIGHTER = Lighter's token,
      BOT / BNC = equities, SKHY = SK hynix ADR ≠ SKHYNIX, TWT «Trust Wallet» / GIGGLE «Giggle Fund» are coins (no bare
      «Trust» / «Fund» in the equity rule).
Fees [D]  «There are no trading fees on Omni» — revenue is the spread (base_spread_bps: BTC 1.1, alts 7–15 [L]). So the
      cost of a round trip is the quote spread (review 13.09): premium() carries spread_rt = the FULL size_1k spread / mid
      (buy at ask, sell at bid; no SPREAD_MAX cut — a wide quote is a real cost), without a usable 1k quote
      base_spread_bps / 1e4; calc.leg_cost adds it to the «Комиссия» of every row with a Variational leg
      (config.QUOTE_COST_VENUES). 12.09 23:14 GMT [L]: 1k spread median 26.5 bps over 547 listings, p25 16.0, p75 39.5.
"""
from __future__ import annotations
import logging, re, threading, time
from collections import deque
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "variational"
STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
PAGE_URL = "https://omni.variational.io/perpetual/{symbol}"   # route /(app)/perpetual/[asset] [L]; SPA never 404s → live list only
QUOTE = "USDC"
CONTRACT = "variational"
H_MS = 3600_000
YEAR_H = 8760                   # funding_rate is a simple annual rate over 365 days [D/L]
CAP_PER_H = 0.02                # «capped at 2 % per hour» [D]
CRYPTO_APR = 0.1095             # interest 0.00125 %/h × 8760: every coin inside the clamp shows exactly this [D/L]
MAX_IV_H = 8                    # the longest interval seen (28800 s) [L]
CACHE_TTL_S = 9.0               # premium() and books() of one tick share one GET
AUX_REUSE_S = 60.0              # universe / intervals reuse a snapshot this young (the origin changes ~once a minute [L])
WINDOW_S = 10.0                 # doc: 10 requests per 10 s per IP [D]
REQ_PER_WINDOW = 10
TICK_CAP = 5                    # a tick call is refused (BudgetExceeded, next tick retries) with this many in the window
AUX_CAP = 8                     # hourly / 15-min calls wait below this
AUX_MAX_WAIT_S = 30.0
BAN_DEFAULT_S = 60.0            # no Retry-After: 60 s, doubling on consecutive strikes [A]
BAN_MAX_S = 3600.0
OBS_SKEW_S = 120.0              # a last-modified further in the future than this is not a snapshot time
SPREAD_MAX = 0.05               # [A] as lighter.py; widest 1k spread on 12.09 was 153 bps (PTB) [L]
QUOTE_NOTIONAL = 1000.0         # size_1k = the RFQ quote for 1 000 USDC [D]
EMIT_MAX_AGE_S = 900.0          # the last estimate must be at most 3 recomputations (5 min each) old at the boundary
LEDGER_STALE_S = config.SETTLE_GRACE_S   # no observation of a ticker for this long → its recent settlements are unknown
RECENT_SPAN_H = 10              # recent_history() window: longer than MAX_IV_H so funding.apply_batch never «legitimately
                                # skips» a symbol that simply was not in the batch (see recent_history)
RETAIN_DAYS = config.HISTORY_DAYS + 1

# --- classes and bases -------------------------------------------------------------------------------------------------
PREIPO = frozenset({"OPENAI", "ANTHROPIC"})                                          # the doc's pre-IPO list [D]
COMMODITIES = frozenset({"XAU", "XAG", "XPT", "XPD", "COPPER", "CL", "BZ", "NATGAS"})  # the doc's TradFi table [D]
# Тикеры, чья единица / актив не сверены: класс «rwa», пар нет. B3 снят 13.09 (тестировщик: «B3 — пары aster:B3USDT,
# gate_spot:B3_USDT, на дашборде нет»): «10 % от Binance B3USDT» было сверкой с мёртвым рынком — Binance B3USDT в статусе
# SETTLING с 28.04.2026, марк заморожен на 0.000444. Живой срез 13.09 01:17 UTC [L]: Variational 0.000579, Aster B3USDT
# 0.000584, Bybit 0.000587, Coinbase B3-USD 0.00059, спот Gate / KuCoin 0.00058 — единица та же; имя «B3 (Base)» = Gate
# «B3 Base» (Base, 0xb3b32f9f…b3b3), индекс Aster берёт Gate B3_USDT и KuCoin B3-USDT — монета.
UNVERIFIED: frozenset[str] = frozenset()
# Corporate forms. No bare «Trust» / «Fund»: Trust Wallet (TWT), Trusta AI (TA), Giggle Fund (GIGGLE) are coins [L].
# «& Co.» has no word boundary before «&» — it gets its own branch (JPM «JPMorgan Chase & Co.» [L]).
_EQUITY = re.compile(r"\b(?:Inc|Incorporated|Corp|Corporation|plc|Ltd|Limited|Holdings?|Company|Common Stock|ETF|"
                     r"Trust, Series|Depositary|Oyj|A/S)(?:\.|\b)|&\s*Co\b|\bN\.V\.|\bGroup$")
# Variational's disambiguation suffixes → the ticker every other venue uses (same asset by full name [L])
LOCAL_CANON = {"crypto": {"1NEIRO": "NEIRO", "FF0": "FF", "BTR0": "BTR", "OPN_OPINION": "OPN", "RE_ETH": "RE"}}


class NoHistoryError(RuntimeError):
    """The venue publishes no funding history and our own ledger did not observe the whole requested window."""


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


def interval_h(seconds) -> int | None:
    """funding_interval_s → whole hours; None for 0 (swaps) or garbage."""
    s = _int(seconds)
    if s <= 0:
        return None
    h = s / 3600.0
    if h < 1 or abs(h - round(h)) > 1e-9:
        log.warning("variational: funding_interval_s %s is not whole hours — rounded", s)
    return max(1, int(round(h)))


def per_interval(apr: float, iv_h: int) -> float:
    """Annualised decimal → fraction per interval of iv_h hours (simple, 365 d)."""
    return apr * iv_h / YEAR_H


def asset_class(ticker: str, name, seconds, n_zero: int = 0, n_crypto: int = 0) -> str | None:
    """crypto / equity / commodity / preipo / «rwa» (pairs with nothing); None — not a perp (swap). n_zero / n_crypto —
    how many snapshots showed the rate exactly 0 (TradFi interest) / exactly CRYPTO_APR (crypto interest)."""
    t, nm = str(ticker).upper(), str(name or "").strip()
    if not interval_h(seconds) or nm.startswith("Swap on "):
        return None
    if t in PREIPO:
        return "preipo"                                   # fixed rate 0.05475: the cross-check does not apply
    if t in UNVERIFIED:
        return "rwa"
    cls = "commodity" if t in COMMODITIES else "equity" if _EQUITY.search(nm) else "crypto"
    tradfi, coin = n_zero > 0, n_crypto > 0
    if (tradfi and coin) or (cls == "crypto" and tradfi) or (cls != "crypto" and coin):
        return "rwa"                                      # name and rate disagree: unclassified, pairs with nothing
    return cls


def perp_base(ticker: str, cls: str) -> tuple[str, float]:
    t = str(ticker).upper()
    t = LOCAL_CANON.get(cls, {}).get(t, t)
    base, factor = norm_symbol_factor(t)                  # 1000PEPE → PEPE ×1000, 1000000MOG → MOG ×1e6
    return config.PERP_CANON.get(cls, {}).get(base, base), factor


def name_hint(name, ticker: str, base: str) -> str | None:
    """The full name — or None when it is only the ticker again («1000BONK», «0G», «4», RE_ETH «RE»)."""
    nm = str(name or "").strip()
    key = re.sub(r"[^A-Z0-9]", "", nm.upper())
    if not nm or key in {re.sub(r"[^A-Z0-9]", "", str(ticker).upper()), base.upper()}:
        return None
    return nm


def _book(q: dict | None) -> dict | None:
    if not isinstance(q, dict):
        return None
    b, a = _f(q.get("bid")), _f(q.get("ask"))
    if not b or not a or b <= 0 or a <= 0 or b >= a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=QUOTE_NOTIONAL / b, ask_qty=QUOTE_NOTIONAL / a)


def quote_rt(x: dict) -> float | None:
    """Round trip at the RFQ quote as a fraction of mid: the full size_1k spread (buy at ask, sell at bid) — no SPREAD_MAX
    cut, a wide quote is what the trade costs. No usable 1k quote → base_spread_bps / 1e4 (the venue's own full spread:
    BTC 1.08 vs a 1k 1.13 bps [L]); neither → None (the page shows «—»)."""
    q = x.get("quotes") if isinstance(x.get("quotes"), dict) else {}
    k = q.get("size_1k") if isinstance(q.get("size_1k"), dict) else {}
    b, a = _f(k.get("bid")), _f(k.get("ask"))
    if b and a and 0 < b < a:
        return (a - b) / ((a + b) / 2.0)
    s = _f(x.get("base_spread_bps"))
    return s / 1e4 if s is not None and s >= 0 else None


_ISO =re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$")


def iso_ts(v) -> float | None:
    """«2026-09-12T21:33:59.138556019Z» (nanoseconds) → epoch seconds; the fraction is cut to microseconds."""
    m = _ISO.match(str(v or "").strip())
    if not m:
        return None
    tz = m.group(3) or "Z"
    try:
        return datetime.fromisoformat(f"{m.group(1)}.{(m.group(2) or '')[:6].ljust(6, '0')}"
                                      f"{'+00:00' if tz == 'Z' else tz}").timestamp()
    except ValueError:
        return None


def _hdr(headers, key: str):
    if not headers:
        return None
    v = headers.get(key)
    if v is None:
        v = next((val for k, val in headers.items() if str(k).lower() == key), None)
    return v


def _http_time(v) -> float | None:
    try:
        return parsedate_to_datetime(v).timestamp()
    except Exception:  # noqa — missing or malformed header
        return None


def snapshot_time(headers, t0: float, t1: float) -> float:
    """When the origin produced the body — the OLDER of last-modified and request start − age (CF cache): at 21:35 UTC
    on 12.09 they agreed within 1 s, at 22:23 last-modified was 25 s NEWER than date − age [L]. The older one never
    overstates freshness. Never after arrival."""
    est = t0 - max(0.0, _f(_hdr(headers, "age")) or 0.0)
    lm = _http_time(_hdr(headers, "last-modified"))
    if lm is not None and lm <= t1 + OBS_SKEW_S:
        est = min(est, lm)
    return min(est, t1)


def _iso_ms(ms: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000)) if ms else "—"


# --- per-host state ----------------------------------------------------------------------------------------------------
class _Host:
    """Everything tied to the endpoint, shared by every client of it in the process: the limit is per IP, and the ledger is
    the only history there is (the collector's background copies must see it)."""

    def __init__(self):
        self.lock = threading.Lock()                      # state below
        self.fetch = threading.Lock()                     # one download at a time: a concurrent caller reuses it
        self.calls: deque[float] = deque()
        self.banned_until = 0.0
        self.strikes = 0
        self.snap: tuple[float, float, dict[str, dict]] | None = None   # (arrival, snapshot time s, ticker → listing)
        self.last_obs_ms = 0                              # snapshot time of the last body fed to the ledger
        self.seen: dict[str, int] = {}                    # ticker → snapshot ms of its last observation
        self.pend: dict[str, tuple[int, int, float, float | None, int]] = {}   # ticker → (boundary, iv_h, rate, mark, obs)
        self.rows: dict[str, list[tuple[int, float, float | None, int]]] = {}  # ticker → [(funding_ms, rate, mark, iv_h)]
        self.cov: dict[str, tuple[int, int] | None] = {}  # ticker → (first boundary of the continuous run, its iv_h)
        self.ev: dict[str, list[int]] = {}                # ticker → [n exact 0, n exact CRYPTO_APR]

    def used(self, now: float) -> int:
        with self.lock:
            while self.calls and now - self.calls[0] >= WINDOW_S:
                self.calls.popleft()
            return len(self.calls)


_HOSTS: dict[str, _Host] = {}
_HOSTS_LOCK = threading.Lock()


def _host(url: str) -> _Host:
    with _HOSTS_LOCK:
        return _HOSTS.setdefault(url, _Host())


# --- client ------------------------------------------------------------------------------------------------------------
class Variational:
    name = NAME
    own_history = True                                    # hook for the integrator: history is our own ledger

    def __init__(self, session: requests.Session | None = None, url: str = STATS_URL, cache_ttl_s: float = CACHE_TTL_S):
        self.url = url
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._s.headers["accept"] = "application/json"
        self._h = _host(url)
        self.cache_ttl_s = cache_ttl_s
        self.used_weight = 0
        self.last_ok_ts = 0.0
        self.n_429 = 0
        self.n_err = 0

    # --- budget and health ------------------------------------------------------------------------------------------
    @property
    def banned_until(self) -> float:
        return self._h.banned_until

    def budget_used(self) -> float:
        return self._h.used(time.time()) / float(REQ_PER_WINDOW)

    def budget_ok(self, soft: float = config.WEIGHT_SOFT_LIMIT) -> bool:
        return time.time() >= self._h.banned_until        # the window itself is guarded inside _acquire

    def health(self) -> dict:
        self.used_weight = self._h.used(time.time())
        return {"exchange": self.name, "used_weight": self.used_weight,
                "budget": round(self.used_weight / REQ_PER_WINDOW, 3), "last_ok_ts": int(self.last_ok_ts),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": int(self._h.banned_until)}

    # --- transport --------------------------------------------------------------------------------------------------
    def _acquire(self, kind: str):
        """A slot in the host's rolling 10 s. tick — now or BudgetExceeded; aux — waits up to AUX_MAX_WAIT_S."""
        h = self._h
        cap = TICK_CAP if kind == "tick" else AUX_CAP
        t_end = time.time() + (0.0 if kind == "tick" else AUX_MAX_WAIT_S)
        while True:
            now = time.time()
            if now < h.banned_until:
                raise BannedError(f"{self.name}: paused until {time.strftime('%H:%M:%S', time.gmtime(h.banned_until))}")
            with h.lock:
                while h.calls and now - h.calls[0] >= WINDOW_S:
                    h.calls.popleft()
                n = len(h.calls)
                wait = h.calls[n - cap] + WINDOW_S - now if n >= cap else 0.0
                if wait <= 0:
                    h.calls.append(now)
                    return
            if now + wait > t_end:
                raise BudgetExceeded(f"{self.name}: {n} requests in the last {WINDOW_S:.0f} s, {kind} call skipped")
            time.sleep(min(wait, 5.0))

    def _throttle(self, r):
        try:
            ra = float(_hdr(r.headers, "retry-after"))
        except (TypeError, ValueError):
            ra = 0.0
        h = self._h
        with h.lock:
            h.strikes += 1
            pause = max(1.0, ra) if ra > 0 else min(BAN_MAX_S, BAN_DEFAULT_S * 2 ** (h.strikes - 1))
            h.banned_until = max(h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s %d on /metadata/stats — host paused for %.0f s", self.name, r.status_code, pause)
        raise BannedError(f"{self.name}: {r.status_code}, pause {pause:.0f} s")

    def _download(self, kind: str) -> tuple[dict[str, dict], float]:
        retries = config.TICK_RETRIES if kind == "tick" else 3
        timeout = config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            t0 = time.time()
            try:
                r = self._s.get(self.url, timeout=timeout)
                if r.status_code in (429, 403):           # 403 = the Cloudflare challenge: back off, never bypass
                    self._throttle(r)
                if 400 <= r.status_code < 500:
                    raise PermanentHTTPError(f"{self.name} {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                body = r.json()
                rows = body.get("listings") if isinstance(body, dict) else None
                if not isinstance(rows, list) or not rows:
                    raise RuntimeError(f"stats without listings: {str(body)[:200]}")
                t1 = time.time()
                with self._h.lock:
                    self._h.strikes = 0
                self.last_ok_ts = t1
                return ({str(x["ticker"]): x for x in rows if isinstance(x, dict) and x.get("ticker")},
                        snapshot_time(r.headers, t0, t1))
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON / challenge page with 200 — retried (aux only)
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET /metadata/stats: {type(last).__name__}: {last}")

    def _snapshot(self, kind: str = "tick", max_age_s: float | None = None) -> tuple[float, dict[str, dict]]:
        """(snapshot time s, ticker → listing). Cached from ARRIVAL for max_age_s (default cache_ttl_s): premium() and
        books() of one tick make one request; a concurrent caller waits for the running download and reuses it."""
        h = self._h
        ttl = self.cache_ttl_s if max_age_s is None else max_age_s
        with h.lock:
            snap = h.snap
        if snap and time.time() - snap[0] < ttl:
            return snap[1], snap[2]
        with h.fetch:
            with h.lock:
                snap = h.snap
            if snap and time.time() - snap[0] < ttl:
                return snap[1], snap[2]
            rows, data_s = self._download(kind)
            with h.lock:
                h.snap = (time.time(), data_s, rows)
            self._observe(rows, data_s)
            return data_s, rows

    # --- the ledger ---------------------------------------------------------------------------------------------------
    def _observe(self, rows: dict[str, dict], data_s: float):
        """One new snapshot → evidence counters and the settlement ledger. A snapshot not newer than the last one (the
        same CF copy, another PoP's older copy) is not observed twice."""
        h = self._h
        ms = int(data_s * 1000)
        with h.lock:
            if ms <= h.last_obs_ms:
                return
            h.last_obs_ms = ms
            floor = ms - RETAIN_DAYS * 86400_000
            for t, x in rows.items():
                iv, apr = interval_h(x.get("funding_interval_s")), _f(x.get("funding_rate"))
                if not iv or apr is None:
                    continue
                ev = h.ev.setdefault(t, [0, 0])
                if apr == 0.0:
                    ev[0] += 1
                elif abs(apr - CRYPTO_APR) < 1e-12:
                    ev[1] += 1
                self._roll(t, ms, iv, per_interval(apr, iv), _f(x.get("mark_price")), floor)
                h.seen[t] = ms

    def _roll(self, t: str, ms: int, iv: int, rate: float, mark: float | None, floor: int):
        """Called under the host lock. The window of an observation at ms ends at the next grid boundary b. When an
        observation lands at or after the pending window's boundary, that window's last estimate becomes its settlement
        row — if it is fresh enough; the continuous run (coverage) breaks on a stale estimate, a skipped window or an
        interval change inside a window."""
        h = self._h
        step = iv * H_MS
        b = (ms // step + 1) * step
        p = h.pend.get(t)
        if p is not None:
            pb, piv, prate, pmark, pobs = p
            if ms >= pb:
                if pb - pobs <= EMIT_MAX_AGE_S * 1000:
                    rows = h.rows.setdefault(t, [])
                    if not rows or rows[-1][0] < pb:
                        rows.append((pb, prate, pmark, piv))
                    if h.cov.get(t) is None:
                        h.cov[t] = (pb, piv)
                    while rows and rows[0][0] < floor:
                        rows.pop(0)
                    if rows and h.cov[t] and h.cov[t][0] < rows[0][0]:
                        h.cov[t] = (rows[0][0], rows[0][3])          # pruned: vouch only for what is still held
                    if b != pb + step:
                        h.cov[t] = None                              # windows after pb were never observed
                else:
                    log.info("%s %s: estimate %.0f s old at %s — no settlement row", self.name, t, (pb - pobs) / 1000,
                             _iso_ms(pb))
                    h.cov[t] = None
            elif pb != b:
                log.info("%s %s: interval %s h → %s h inside the window — its estimate is dropped", self.name, t, piv, iv)
                h.cov[t] = None
        h.pend[t] = (b, iv, rate, mark, ms)

    def coverage_ms(self, symbol: str) -> int | None:
        """First settlement of the ledger's continuous run for this ticker (None — nothing vouched for yet). Hook for the
        integrator: completeness of an own-history venue cannot be older than this."""
        with self._h.lock:
            c = self._h.cov.get(symbol)
        return c[0] if c else None

    # --- native perp interface ------------------------------------------------------------------------------------------
    def perp_instruments(self) -> list[dict]:
        _data_s, rows = self._snapshot("aux", AUX_REUSE_S)
        with self._h.lock:
            ev = {t: tuple(v) for t, v in self._h.ev.items()}
        out = []
        for t, x in rows.items():
            name = x.get("name")
            cls = asset_class(t, name, x.get("funding_interval_s"), *ev.get(t, (0, 0)))
            if cls is None:
                continue
            iv = interval_h(x.get("funding_interval_s"))
            base, factor = perp_base(t, cls)
            cap = CAP_PER_H * iv
            out.append(dict(exchange=self.name, symbol=t, base_asset=t, base=base, factor=factor, tick_size=None,
                            step_size=None, min_notional=None, onboard_ms=0, interval_h=iv, cap=cap, floor=-cap,
                            quote=QUOTE, contract=CONTRACT, cls=cls, name_hint=name_hint(name, t, base),
                            url=PAGE_URL.format(symbol=quote(t, safe=""))))
        if not out:
            raise RuntimeError(f"{self.name}: stats without perps")
        return out

    def premium(self) -> dict[str, dict]:
        """ticker → {rate (predicted, fraction per interval), mark, index (None: the oracle is not public), next_ms (UTC
        grid of the interval), ts_ms / obs (the snapshot's time, up to ~60 s old behind the CF cache), interval_h,
        spread_rt (round-trip cost at the quote, quote_rt — the venue charges no fee)}."""
        data_s, rows = self._snapshot("tick")
        now_ms = int(time.time() * 1000)
        out = {}
        for t, x in rows.items():
            iv, apr = interval_h(x.get("funding_interval_s")), _f(x.get("funding_rate"))
            if not iv or apr is None:
                continue
            step = iv * H_MS
            out[t] = dict(rate=per_interval(apr, iv), mark=_f(x.get("mark_price")), index=None,
                          next_ms=(now_ms // step + 1) * step, ts_ms=int(data_s * 1000), obs=data_s, interval_h=iv,
                          spread_rt=quote_rt(x))
        return out

    def books(self) -> dict[str, dict]:
        """ticker → the 1 000 USDC RFQ quote (indicative), quantity in units of the price; obs = quotes.updated_at."""
        data_s, rows = self._snapshot("tick")
        out = {}
        for t, x in rows.items():
            q = x.get("quotes") if isinstance(x.get("quotes"), dict) else {}
            b = _book(q.get("size_1k"))
            if b is None:
                continue
            obs = iso_ts(q.get("updated_at"))
            b["obs"] = min(obs, data_s) if obs is not None else data_s
            out[t] = b
        return out

    def funding_intervals(self) -> dict[str, int]:
        """Live intervals (they change on the fly [D]) from a snapshot at most AUX_REUSE_S old — no extra request while
        the tick runs."""
        _data_s, rows = self._snapshot("aux", AUX_REUSE_S)
        out = {t: iv for t, x in rows.items() if (iv := interval_h(x.get("funding_interval_s")))}
        if not out:
            raise RuntimeError(f"{self.name}: stats without intervals")
        return out

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settlement rows of one ticker in [start_ms, end_ms] from our own ledger (the venue publishes no history). No
        network. Raises NoHistoryError when the ledger cannot vouch for the whole window: the ticker was never observed,
        the window starts before the continuous run (restart, gap, 30-day backfill), or the ticker was not observed for
        LEDGER_STALE_S (settlements after that are unknown). Rows are OUR approximation: the last predicted rate seen
        before the boundary (≤ EMIT_MAX_AGE_S old) and the mark at that time."""
        h = self._h
        now_ms = int(time.time() * 1000)
        start_ms, end_ms = int(start_ms), int(end_ms or now_ms)
        if end_ms < start_ms:
            return []
        with h.lock:
            seen, cov = h.seen.get(symbol), h.cov.get(symbol)
            rows = list(h.rows.get(symbol, ()))
        if seen is None:
            raise NoHistoryError(f"{self.name} {symbol}: not observed by this process; the venue has no public history")
        if seen < min(end_ms, now_ms) - LEDGER_STALE_S * 1000:
            raise NoHistoryError(f"{self.name} {symbol}: last observed {(now_ms - seen) / 1000:.0f} s ago — later "
                                 f"settlements are unknown")
        if cov is None or start_ms <= cov[0] - cov[1] * H_MS:
            raise NoHistoryError(f"{self.name} {symbol}: own ledger is continuous from {_iso_ms(cov[0] if cov else None)} "
                                 f"only, asked from {_iso_ms(start_ms)}")
        return [dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=r, mark=m)
                for ms, r, m, _iv in rows if start_ms <= ms <= end_ms]

    def recent_history(self) -> list[dict]:
        """The ledger's settlements of the last RECENT_SPAN_H, as a batch that is COMPLETE over its window — the promise
        funding.apply_batch relies on when it advances cursors. Not drained (INSERT OR IGNORE makes a repeat harmless).
        A ticker is in the batch only if it is being observed now and its continuous run started before the window; a
        ticker with a recent hole stays out (a cursor from before the hole must not jump over it) until its run covers
        the window. [] when the batch would span less than the longest interval (apply_batch would then wave through
        every longer-interval symbol that is not in it)."""
        h = self._h
        now_ms = int(time.time() * 1000)
        lo, fresh = now_ms - RECENT_SPAN_H * H_MS, now_ms - LEDGER_STALE_S * 1000
        out = []
        with h.lock:
            for t, rows in h.rows.items():
                cov = h.cov.get(t)
                if h.seen.get(t, 0) < fresh or cov is None or cov[0] - cov[1] * H_MS > lo:
                    continue
                out.extend(dict(exchange=self.name, symbol=t, funding_ms=ms, rate=r, mark=m)
                           for ms, r, m, _iv in rows if ms > lo)
        if not out:
            return []
        cover_from = (min(r["funding_ms"] for r in out) // 60_000 + 1) * 60_000      # as funding.apply_batch computes it
        if now_ms - cover_from <= MAX_IV_H * H_MS + 300_000:
            return []
        out.sort(key=lambda r: (r["funding_ms"], r["symbol"]))
        return out


PERP_CLIENTS = {NAME: Variational}
