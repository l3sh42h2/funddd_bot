"""edgeX perpetuals (V2) — perp venue «edgex» (owner 13.09: «добавь биржи backpack, variational, edgex, extended, pacifica,
apex во фьючи» — in that order after lighter_rh). Native perp client of venues.py: perp_instruments / premium / books /
history_since / recent_history, plus the funding_intervals() hook. Public endpoints only: no keys, no accounts, no orders.

Measured 13.09.2026 from the Mac (server time 12.09 21:35-22:15 UTC). Tags: [L] verified live, [D] docs, [A] assumption.
V1 IS DEAD [L]  pro.edgex.exchange/api/v1/public still answers getMetaData (legacy 10000xxx «BTCUSD»), but ticker /
      funding / depth return SUCCESS with data [] — use V2: REST https://edgex-prod-v2.edgex.exchange/api/v2/public,
      WS wss://edgex-quote-prod-v2.edgex.exchange/api/v1/public/ws (the WS path stays v1) [L/D]. 173 contracts
      30000001..30000173, quote USDC. Envelope {code: "SUCCESS", data, msg, requestTime, responseTime}; numbers are
      strings, times ms. An unknown/missing contractId answers SUCCESS + data [] (not an error) [L]. gzip behind the
      TencentEdgeOne CDN, no rate headers [L].
Universe [L]  GET /meta/getMetaData (~365 KB): contractList {contractId, contractName, baseCoinId, quoteCoinId, tickSize,
      stepSize, minOrderSize (BASE qty), enableTrade/enableDisplay/enableOpenPosition, fundingRateIntervalMin ("240" on
      all 173), fundingMaxRate/fundingMinRate (per interval), isStock, isFx, oraclePriceSignedAssetIds}; coinList
      {coinId, coinName}. The base is coinList[baseCoinId].coinName, NOT contractName minus USDC (哈基米USDC → HAJIMI,
      牛来USDC → NIULAI). Hidden contracts (enableDisplay false: ZRO, EUR, JPY on 13.09) are not listed in the app
      (the frontend filters enableDisplay and enableTrade [L bundle]) — excluded here, as are non-openable ones.
Rate [L]  signed fraction PER INTERVAL (4 h on every contract now; per-contract multiple of 60 min anchored at 00:00 UTC
      [D]) — no conversion: not per hour, not per 8 h (the doc's «every 8 hours» is stale [L: 179/179 gaps of 4 h]),
      not annualised. The live predicted rate for the next settlement = forecastFundingRate of getLatestFundingRate =
      fundingRate of the REST and WS tickers (173/173 equal [L]; the docs wrongly call the ticker's field «latest
      settled»). predictedFundingRate is only interestRate/6 on every contract — ignored. getLatestFundingRate.
      fundingRate is the rate FINALIZED at fundingTime (constant through the interval, equal to the settled history
      row) — recent_history() uses it. There is no nextFundingTime in that batch: fundingTime + interval (= the ticker's
      nextFundingTime [L]).
Freshness [L]  getLatestFundingRate is a PER-MINUTE snapshot: fundingTimestamp / markPrice / indexPrice stayed frozen over
      four calls 6 s apart (lag 36 → 56 s) while the ticker's mark moved. So the tick reads the WS channel
      ticker.all.1s (every message = ALL contracts, ~110 KB, ~1 s apart: Snapshot, then dataType «changed» [L]) for
      rate, mark, index, nextFundingTime AND best bid/ask; the REST batch is the degraded path when the stream is not
      fresh (obs = its fundingTimestamp minute — honest, not «arrival»), and books then fall back to the batch's
      impactBidPrice / impactAskPrice (impact notional 100 USD [L]). The channel carries no bid/ask sizes [L] →
      bid_qty = ask_qty = 0.0 (as Lighter / Hyperliquid). Server pings are JSON {"type":"ping","time":…} → we answer
      {"type":"pong","time":<same>} [L/D]. ~10 GB/day of ingress (no permessage-deflate with our minimal client).
History [L]  GET /funding/getFundingRatePage?contractId&size&filterSettlementFundingRate=true&filterBeginTimeInclusive&
      filterEndTimeExclusive&offsetData — without filterSettlementFundingRate=true it returns PER-MINUTE rows (1440/day).
      Newest first; nextPageOffsetData "" = last page; the time window works (1 day → 6 rows, token ""); size 1000 was
      accepted (761 rows = all history) — HISTORY_PAGE 200 covers 30 days (180 rows) in one call. Settled rows:
      isSettlement true, on the 00/04/08/12/16/20 UTC grid. Paid at fundingTime [A]: the Funding Fees page says the rate
      is applied at settlement [D], a FundingRateItem doc line says «used for settlement» one interval later [D,
      contradictory]; private position transactions would be needed to check.
Batch [L]  getLatestFundingRate?contractId=A,B,… — all 173 in one call (115 KB, 0.37 s; comma-joined works); chunked at
      BATCH_IDS so the URL stays short as the list grows [A]. No contractId → []. It holds only the LAST settlement of each
      contract, so each row carries prev_ms = fundingTime − interval: funding.apply_batch moves a leg's cursor through
      the new settlement when the cursor already covers prev_ms (review 13.09 — without it every edgeX leg went to the
      per-leg repair after each 4-hourly settlement: 170 extra calls).
Limits [D/L]  docs give no numbers («Public API: Higher rate limits»; excess → HTTP 429 / RATE_LIMIT_EXCEEDED, exponential
      backoff recommended). 300 calls at 28.7 req/s got 300 × 200 [L]. Own budget [A]: tick 0 calls with a live stream
      (≤ 2 per 10 s degraded), history ≥ 0.3 s apart, hourly universe; 429 / 403 / RATE_LIMIT_EXCEEDED → the host pauses
      60 s (Retry-After honoured) for every client of this process. Codes seen: GATEWAY_INTERNAL_ERROR (500),
      GATEWAY_PARAM_REQUIRED (400) [L].
Classes [L]  isFx → fx; isStock → equity (94 stocks AND ETFs: SPY, QQQ, SOXL, KODEX200 …) — SPCX / CXMT / UNITREE too:
      review 13.09 [L], they are equity on 10+ other venues at one price within 0.3 % (Binance: TRADIFI_PERPETUAL, EQUITY /
      CN_EQUITY); a hard-coded «preipo» cut 70 pairs off. preipo only for OPENAI / ANTHROPIC (private on every venue),
      should edgeX list them; the «Commodities V2» label group of /contract-labels → commodity (XAU XAG CL
      BZ COPPER NATGAS XPD XPT — NOT «Commodities TradeFi», which also lists the stocks JPM and KO; isStock wins);
      everything else → crypto; PAXG / XAUT would be coins (repo rule). FX units [L]: EURUSDC mark 1.169 = USD per EUR
      like the others' EUR; JPYUSDC 0.00649 = USD per JPY, the RECIPROCAL of HL xyz:JPY / Lighter USDJPY / Bitget USDJPY
      (≈153) → own base «JPYUSD» (pairs with nothing).
Identity  no index composition (getIndexPriceConfig → []), no full names, no token contracts (coinList.assetId null).
      Stork oracle; the only declared evidence is the feed id (oraclePriceSignedAssetIds: «1000PEPEUSD», «HAJIMIUSD») —
      carried as oracle_feed. name_hint stays None: a ticker is not a name (identity must not confirm by ticker).
      syntheticAssetId is a legacy ASCII label (GRAMUSDC decodes to «TONUSDC») — never decoded.
Pitfalls  ambiguous tickers B, ON (a coin, not ON Semiconductor), V (Visa, isStock), GS, PENG, LIT (= Lighter), EDGE (=
      edgeX), GRAM (not TON), SKHY (ADR) vs SKHYNIX, SAMSUNG vs SAMSUNGEM. Stocks keep settling on weekends with
      marketOpen false [L]. Rate strings have inconsistent trailing zeros — always float().
"""
from __future__ import annotations
import json, logging, socket, threading, time
from collections import deque
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "edgex"
REST = "https://edgex-prod-v2.edgex.exchange/api/v2/public"
WS_URL = "wss://edgex-quote-prod-v2.edgex.exchange/api/v1/public/ws"
CHANNEL = "ticker.all.1s"
# route /perpetuals/:symbol is in the app bundle; 1000PEPEUSDC, AAPLUSDC and %-encoded 哈基米USDC stay on their market in a
# browser [L]. The old /trade/{symbol} silently redirects to BTCUSDC — not used. Links only from the current list.
PAGE_URL = "https://pro.edgex.exchange/en-US/perpetuals/{symbol}"
QUOTE = "USDC"
PATH_META = "/meta/getMetaData"
PATH_LATEST = "/funding/getLatestFundingRate"
PATH_HIST = "/funding/getFundingRatePage"
PATH_LABELS = "/contract-labels"
OK = "SUCCESS"
THROTTLE_CODES = ("RATE_LIMIT_EXCEEDED",)
H_MS = 3600_000
DEFAULT_IV_H = 4                 # all 173 contracts on 13.09 [L]; used only when a row carries no interval
BATCH_IDS = 100                  # contract ids per getLatestFundingRate call (173 in one call worked [L]) [A]
HISTORY_PAGE = 200               # size=1000 was accepted [L]; 200 rows = 33 days at 4 h → 30 days in one call
HISTORY_MAX_PAGES = 20           # a window that needs more full pages raises instead of silently truncating
HISTORY_GAP_S = 0.3              # between history calls (config.FUNDING_HISTORY_MIN_GAP_S overrides by venue name)
REQ_PER_MIN = 600                # own soft cap of the host per rolling minute [A] (docs give no number)
TICK_CAP = 540                   # tick calls are refused (BudgetExceeded) above this many in the last 60 s
HIST_CAP = 400                   # history waits while the window holds this many: the rest stays for tick / universe
HIST_MAX_WAIT_S = 90.0
AUX_MAX_WAIT_S = 30.0
BAN_DEFAULT_S = 60.0             # 429 / 403 / RATE_LIMIT_EXCEEDED without Retry-After [A]
IDS_TTL_S = 60                   # unknown symbol in history → the universe is refreshed at most this often
LABELS_STALE_OK_S = 86400        # /contract-labels down: a copy up to a day old still classifies commodities
LATEST_TTL_S = 15.0              # books' fallback uses the tick's batch only this young (never a call of its own)
LATEST_REUSE_S = 60.0            # funding_intervals / recent_history reuse a batch this young (it changes per minute)
SPREAD_MAX = 0.05                # [A] a book wider than 5 % of mid is not a price (widest live: 0.64 %, weekend stocks)
WS_FRESH_S = 15.0                # the stream feeds premium / books while its last frame is this young
WS_RECV_TIMEOUT_S = 5.0
WS_SILENT_S = 30.0               # no ticker data on the connection for this long → reconnect (frames come every ~1 s)
WS_BACKOFF_MAX_S = 30.0
WS_FIRST_WAIT_S = 3.0            # the very first premium()/books() waits this long for the snapshot (then never blocks)

# --- classes and bases ------------------------------------------------------------------------------------------------
COMMODITY_GROUP = "commodities v2"                 # label group name, lower-case [L]
# members of that group on 13.09 [L] — used only when /contract-labels is down and no copy is cached
COMMODITIES_0 = frozenset({"XAU", "XAG", "CL", "BZ", "COPPER", "NATGAS", "XPD", "XPT"})
# private on every venue (review 13.09: SPCX / CXMT / UNITREE are equity elsewhere at the same price [L] — not here)
PREIPO = frozenset({"OPENAI", "ANTHROPIC"})
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})          # repo rule: gold tokens are coins
FX_AS_OTHERS = frozenset({"EUR", "GBP"})           # quoted USD per unit like HL xyz:EUR / Lighter EURUSD [L for EUR]


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None                   # NaN is not a number here


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def asset_class(coin: str, is_stock, is_fx, commodity: bool) -> str:
    """crypto / equity / commodity / fx / preipo. isStock wins over label groups (JPM, KO sit in «Commodities TradeFi»)."""
    c = str(coin or "").upper()
    if c in GOLD_TOKENS:
        return "crypto"
    if is_fx is True:
        return "fx"
    if is_stock is True:
        return "preipo" if c in PREIPO else "equity"
    if commodity:
        return "commodity"
    return "crypto"


def perp_base(coin: str, cls: str) -> tuple[str, float]:
    """edgeX coin name → (canonical base, tokens per unit of price). 1000PEPE → PEPE ×1000. FX: EUR stays EUR (USD per EUR,
    as elsewhere); JPY is USD per JPY here — the reciprocal of everyone's USDJPY — so it becomes «JPYUSD»."""
    b, factor = norm_symbol_factor(str(coin or ""))
    if cls == "fx" and b not in FX_AS_OTHERS:
        b = b + "USD"
    return config.PERP_CANON.get(cls, {}).get(b, b), factor


def interval_h(minutes) -> int | None:
    """fundingRateIntervalMin → whole hours; None when absent."""
    m = _int(minutes)
    if m <= 0:
        return None
    if m % 60:
        log.warning("edgex: funding interval %s min is not whole hours — rounded", m)
    return max(1, int(round(m / 60.0)))


def _roll(next_ms: int | None, iv_h: int, now_ms: int) -> int:
    """Next settlement: the venue's time, rolled forward by whole intervals once passed; without one — the UTC grid."""
    step = max(1, int(iv_h or DEFAULT_IV_H)) * H_MS
    if not next_ms or next_ms <= 0:
        return (now_ms // step + 1) * step
    if next_ms <= now_ms:
        next_ms += ((now_ms - next_ms) // step + 1) * step
    return next_ms


def _book(bid, ask) -> dict | None:
    b, a = _f(bid), _f(ask)
    if not b or not a or b <= 0 or a <= 0 or b > a:
        return None
    if (a - b) / ((a + b) / 2.0) > SPREAD_MAX:
        return None
    return dict(bid=b, ask=a, bid_qty=0.0, ask_qty=0.0)            # the channel carries no sizes [L]


# --- host state -------------------------------------------------------------------------------------------------------
class _Host:
    """State shared by every edgeX client of this process (the tick client and the collector's background copies from
    make_clients()): the limit is per IP, and the universe / batch are the same data."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: deque[float] = deque()
        self.banned_until = 0.0
        self.last_hist = 0.0
        self.uni: dict | None = None               # {"ins": [...], "ids": {sym: cid}, "syms": {cid: sym}, "iv": {sym: h}}
        self.uni_ts = 0.0
        self.labels: frozenset[str] | None = None  # contract ids of the commodity group
        self.labels_ts = 0.0
        self.latest: tuple[float, dict[str, dict]] = (0.0, {})     # (arrival, cid → getLatestFundingRate row)

    def used(self, now: float) -> int:
        with self.lock:
            while self.calls and now - self.calls[0] >= 60.0:
                self.calls.popleft()
            return len(self.calls)


_HOST = _Host()


def _host() -> _Host:
    return _HOST


# --- client -----------------------------------------------------------------------------------------------------------
class EdgexPerp:
    name = NAME

    def __init__(self, session: requests.Session | None = None, history_gap_s: float | None = None, connect=None):
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._h = _host()
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
        """A slot in the host's rolling minute. tick — now or BudgetExceeded (the next tick retries); hist — waits for its
        gap and for the window to drop below HIST_CAP; aux — waits for a slot below TICK_CAP."""
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

    def _throttle(self, r, path: str, why: str):
        try:
            pause = float(r.headers.get("Retry-After"))
        except (TypeError, ValueError):
            pause = BAN_DEFAULT_S
        pause = max(1.0, pause)
        with self._h.lock:
            self._h.banned_until = max(self._h.banned_until, time.time() + pause)
        self.n_429 += 1
        log.warning("%s %s on %s — host paused for %.0f s", self.name, why, path, pause)
        raise BannedError(f"{self.name}: {why} on {path}, pause {pause:.0f} s")

    def _get(self, path: str, params: dict | None = None, kind: str = "aux", retries: int | None = None,
             timeout: float | None = None):
        """GET → the envelope's data. 429 / 403 / RATE_LIMIT_EXCEEDED → host pause (BannedError); other 4xx and
        *PARAM* codes → PermanentHTTPError; network, 5xx, other codes → retried (a tick call is tried once)."""
        retries = (config.TICK_RETRIES if kind == "tick" else 3) if retries is None else retries
        timeout = (config.TICK_HTTP_TIMEOUT if kind == "tick" else config.HTTP_TIMEOUT) if timeout is None else timeout
        last = None
        for i in range(max(1, retries)):
            self._acquire(kind)
            try:
                r = self._s.get(REST + path, params=params, timeout=timeout)
                if r.status_code in (429, 403):            # 403 = CDN / WAF refusal, treated as a rate answer [A]
                    self._throttle(r, path, str(r.status_code))
                body = None
                try:
                    body = r.json()
                except Exception:  # noqa — a 4xx without JSON is still permanent, a 5xx is retried below
                    pass
                code = str(body.get("code")) if isinstance(body, dict) else ""
                if code in THROTTLE_CODES:
                    self._throttle(r, path, code)
                if 400 <= r.status_code < 500 or "PARAM" in code:
                    raise PermanentHTTPError(f"{self.name} {r.status_code} {path} {code}: {str(getattr(r, 'text', ''))[:200]}")
                r.raise_for_status()
                if not isinstance(body, dict) or code != OK:
                    raise RuntimeError(f"{path}: body code {code or '?'}: {str(body)[:200]}")
                self.last_ok_ts = time.time()
                return body.get("data")
            except (PermanentHTTPError, BannedError, BudgetExceeded):
                raise
            except Exception as e:  # noqa: network, 5xx, bad JSON, foreign body code — retried
                last = e; self.n_err += 1
                if i + 1 < retries:
                    time.sleep(1.0 * (i + 1))
        raise RuntimeError(f"{self.name} GET {path}: {type(last).__name__}: {last}")

    # --- universe --------------------------------------------------------------------------------------------------
    def _commodity_ids(self, kind: str) -> frozenset[str] | None:
        """Contract ids of the «Commodities V2» label group. Down → a copy up to a day old; none → None (the caller
        falls back on COMMODITIES_0 by coin name)."""
        h, now = self._h, time.time()
        try:
            data = self._get(PATH_LABELS, kind=kind)
            ids = None
            for g in data or []:
                if isinstance(g, dict) and str(g.get("name") or "").strip().lower() == COMMODITY_GROUP:
                    ids = frozenset(str(c.get("contractId")) for c in g.get("contracts") or []
                                    if isinstance(c, dict) and c.get("contractId") is not None)
            if not ids:
                raise RuntimeError(f"contract-labels without the «{COMMODITY_GROUP}» group")
        except (BannedError, BudgetExceeded):
            raise
        except Exception as e:  # noqa — classes must not fail the universe; a recent copy or the known list stands in
            with h.lock:
                cached, ts = h.labels, h.labels_ts
            if cached is not None and now - ts < LABELS_STALE_OK_S:
                log.warning("%s contract-labels: %s: %s — using the copy from %.0f min ago", self.name,
                            type(e).__name__, e, (now - ts) / 60)
                return cached
            log.warning("%s contract-labels: %s: %s — commodities by the 13.09 list", self.name, type(e).__name__, e)
            return None
        with h.lock:
            h.labels, h.labels_ts = ids, now
        return ids

    def _build(self, data: dict, commodity_ids: frozenset[str] | None) -> dict:
        coins = {str(c.get("coinId")): str(c.get("coinName") or "") for c in data.get("coinList") or [] if isinstance(c, dict)}
        ins, ids, syms, ivs = [], {}, {}, {}
        for c in data.get("contractList") or []:
            if not isinstance(c, dict):
                continue
            cid, sym = str(c.get("contractId") or ""), str(c.get("contractName") or "")
            coin = coins.get(str(c.get("baseCoinId")), "")
            if not cid or not sym or not coin or coins.get(str(c.get("quoteCoinId"))) != QUOTE:
                continue
            if not (c.get("enableTrade") is True and c.get("enableDisplay") is True and c.get("enableOpenPosition") is True):
                continue                                   # hidden (ZRO, EUR, JPY) or not openable [L]
            com = cid in commodity_ids if commodity_ids is not None else coin.upper() in COMMODITIES_0
            cls = asset_class(coin, c.get("isStock"), c.get("isFx"), com)
            base, factor = perp_base(coin, cls)
            iv = interval_h(c.get("fundingRateIntervalMin")) or DEFAULT_IV_H
            feeds = sorted({str(x) for x in c.get("oraclePriceSignedAssetIds") or [] if x})
            ins.append(dict(exchange=self.name, symbol=sym, base_asset=coin, base=base, factor=factor,
                            tick_size=_f(c.get("tickSize")), step_size=_f(c.get("stepSize")),
                            min_notional=None,                  # minOrderSize is a BASE quantity, not USD [L]
                            onboard_ms=0, interval_h=iv, cap=_f(c.get("fundingMaxRate")),
                            floor=_f(c.get("fundingMinRate")), quote=QUOTE, contract="v2", cls=cls, contract_id=cid,
                            min_qty=_f(c.get("minOrderSize")), oracle_feed=feeds[0] if len(feeds) == 1 else None,
                            name_hint=None, url=PAGE_URL.format(symbol=quote(sym, safe=""))))
            ids[sym], syms[cid], ivs[sym] = cid, sym, iv
        return {"ins": ins, "ids": ids, "syms": syms, "iv": ivs}

    def _universe(self, kind: str = "aux", fresh: bool = False) -> dict:
        """The host's universe. The tick uses any cached copy (a fresh client fetches it once); perp_instruments forces
        a fresh one; history refreshes it for an unknown symbol (at most every IDS_TTL_S)."""
        h = self._h
        with h.lock:
            uni = h.uni
        if uni is not None and not fresh:
            return uni
        data = self._get(PATH_META, kind=kind)
        if not isinstance(data, dict) or not data.get("contractList"):
            raise RuntimeError(f"{self.name}: getMetaData without contracts")
        uni = self._build(data, self._commodity_ids(kind))
        if not uni["ins"]:
            raise RuntimeError(f"{self.name}: getMetaData without tradable USDC perps")
        with h.lock:
            h.uni, h.uni_ts = uni, time.time()             # declared intervals; funding_intervals() refreshes them live
        return uni

    def perp_instruments(self) -> list[dict]:
        return [dict(i) for i in self._universe("aux", fresh=True)["ins"]]

    # --- REST batch (degraded tick, intervals, recent history) -------------------------------------------------
    def _latest(self, kind: str) -> tuple[float, dict[str, dict]]:
        """getLatestFundingRate for every contract of the universe, chunked. A chunk answering [] for known ids is a
        failure (an unknown id is answered SUCCESS + [] [L])."""
        uni = self._universe(kind)
        cids = list(uni["syms"])
        rows: dict[str, dict] = {}
        for k in range(0, len(cids), BATCH_IDS):
            chunk = cids[k:k + BATCH_IDS]
            data = self._get(PATH_LATEST, {"contractId": ",".join(chunk)}, kind=kind)
            got = [r for r in data or [] if isinstance(r, dict) and str(r.get("contractId")) in uni["syms"]]
            if not got:
                raise RuntimeError(f"{self.name}: getLatestFundingRate empty for {len(chunk)} known contracts")
            for r in got:
                rows[str(r["contractId"])] = r
        t = time.time()
        with self._h.lock:
            self._h.latest = (t, rows)
        return t, rows

    def _latest_cached(self, max_age: float, kind: str) -> tuple[float, dict[str, dict]]:
        with self._h.lock:
            t, rows = self._h.latest
        if rows and time.time() - t <= max_age:
            return t, rows
        return self._latest(kind)

    # --- tick ------------------------------------------------------------------------------------------------------
    def _ensure_stream(self) -> "_Stream":
        if self._stream is None:
            self._stream = _Stream(WS_URL, self.name, self._connect)
        self._stream.start()
        self._stream.wait_first(WS_FIRST_WAIT_S)
        return self._stream

    def _live(self) -> tuple[dict[str, dict], float] | None:
        """(cid → ticker entry, time of the last frame) while the stream is fresh; None otherwise."""
        data, last = self._ensure_stream().snapshot()
        if data and time.time() - last <= WS_FRESH_S:
            return data, last
        return None

    def premium(self) -> dict[str, dict]:
        """symbol → {rate (predicted, fraction PER INTERVAL), mark, index, next_ms, ts_ms, obs, interval_h}. From the live
        stream (0 REST calls); stream not fresh → the REST batch (per-minute snapshot, obs = its minute)."""
        uni = self._universe("tick")
        live = self._live()
        now_ms = int(time.time() * 1000)
        out = {}
        if live is not None:
            data, last = live
            for cid, e in data.items():
                sym = uni["syms"].get(cid)
                rate = _f(e.get("fundingRate"))            # = forecastFundingRate on 173/173 [L]
                if not sym or rate is None:
                    continue
                iv = uni["iv"].get(sym) or DEFAULT_IV_H
                out[sym] = dict(rate=rate, mark=_f(e.get("markPrice")), index=_f(e.get("indexPrice")),
                                next_ms=_roll(_int(e.get("nextFundingTime")), iv, now_ms), ts_ms=int(last * 1000),
                                obs=last, interval_h=iv)
            if out:
                return out
        t, rows = self._latest("tick")
        for cid, r in rows.items():
            sym = uni["syms"].get(cid)
            if not sym:
                continue
            rate = _f(r.get("forecastFundingRate"))
            if rate is None and r.get("isSettlement") is True:
                rate = _f(r.get("fundingRate"))            # the settlement minute: the forecast is the finalized rate [A]
            if rate is None:
                continue
            iv = interval_h(r.get("fundingRateIntervalMin")) or uni["iv"].get(sym) or DEFAULT_IV_H
            ft = _int(r.get("fundingTime"))
            ts = _int(r.get("fundingTimestamp")) or int(t * 1000)
            out[sym] = dict(rate=rate, mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice")),
                            next_ms=_roll(ft + iv * H_MS if ft > 0 else None, iv, now_ms), ts_ms=ts,
                            obs=min(ts / 1000.0, t), interval_h=iv)
        return out

    def books(self) -> dict[str, dict]:
        """Best bid / ask of every contract from the stream (obs = the connection's last frame). Stream not fresh → the
        impact prices of the tick's REST batch (fetched by premium() in the same tick; books never call on their own)."""
        uni = self._universe("tick")
        live = self._live()
        out = {}
        if live is not None:
            data, last = live
            for cid, e in data.items():
                sym = uni["syms"].get(cid)
                b = _book(e.get("bestBidPrice"), e.get("bestAskPrice")) if sym else None
                if b is not None:
                    b["obs"] = last
                    out[sym] = b
            return out
        with self._h.lock:
            t, rows = self._h.latest
        if not rows or time.time() - t > LATEST_TTL_S:
            return out
        for cid, r in rows.items():
            sym = uni["syms"].get(cid)
            b = _book(r.get("impactBidPrice"), r.get("impactAskPrice")) if sym else None
            if b is not None:
                ts = _int(r.get("premiumIndexTimestamp")) or _int(r.get("fundingTimestamp"))
                b["obs"] = min(ts / 1000.0, t) if ts else t
                out[sym] = b
        return out

    def funding_intervals(self) -> dict[str, int]:
        """Live intervals of EVERY symbol of the universe (the collector sets a missing one to 8) from the batch's
        fundingRateIntervalMin — the batch is reused when younger than LATEST_REUSE_S."""
        uni = self._universe("aux")
        _t, rows = self._latest_cached(LATEST_REUSE_S, "aux")
        live = {uni["syms"][cid]: interval_h(r.get("fundingRateIntervalMin"))
                for cid, r in rows.items() if cid in uni["syms"]}
        out = {s: int(live.get(s) or iv) for s, iv in uni["iv"].items()}
        with self._h.lock:
            if self._h.uni is uni:
                uni["iv"] = dict(out)
        return out

    # --- history ---------------------------------------------------------------------------------------------------
    def _contract_id(self, symbol: str) -> str:
        uni = self._universe("hist")
        cid = uni["ids"].get(symbol)
        if cid is None and time.time() - self._h.uni_ts >= IDS_TTL_S:
            cid = self._universe("hist", fresh=True)["ids"].get(symbol)
        if cid is None:
            raise RuntimeError(f"{self.name}: no market {symbol}")
        return cid

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled rates of one contract in [start_ms, end_ms], ascending. The server filters the window and pages newest
        first (nextPageOffsetData "" = the end); 30 days fit one page."""
        cid = self._contract_id(symbol)
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        if end_ms < start_ms:
            return []
        got: dict[int, dict] = {}
        token = ""
        for _ in range(HISTORY_MAX_PAGES):
            p = dict(contractId=cid, size=HISTORY_PAGE, filterSettlementFundingRate="true",
                     filterBeginTimeInclusive=start_ms, filterEndTimeExclusive=end_ms + 1)
            if token:
                p["offsetData"] = token
            data = self._get(PATH_HIST, p, kind="hist")
            rows = [r for r in (data or {}).get("dataList") or [] if isinstance(r, dict)] if isinstance(data, dict) else []
            stamps = []
            for r in rows:
                ms = _int(r.get("fundingTime"))
                if ms <= 0:
                    continue
                stamps.append(ms)
                if r.get("isSettlement") is False:
                    continue                               # a per-minute row: the filter should have removed it [L]
                rate = _f(r.get("fundingRate"))
                if rate is not None and start_ms <= ms <= end_ms:
                    got[ms] = dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=_f(r.get("markPrice")))
            token = str((data or {}).get("nextPageOffsetData") or "") if isinstance(data, dict) else ""
            if not token or not rows or (stamps and min(stamps) <= start_ms):
                break
        else:
            # every page full and start_ms still not reached: a silent cut would confirm depth that was never fetched
            raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {HISTORY_MAX_PAGES} pages")
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """The last settlement of every contract from the batch (fundingTime, fundingRate = the finalized rate [L]).
        funding.apply_batch treats a batch as complete from its OLDEST row, and this batch holds only the LAST settlement
        per contract — so only rows of the newest settlement time are returned (a contract still showing an older one
        would stretch the window over settlements the batch does not contain), and nothing when that settlement is older
        than one interval + SETTLE_GRACE_S (a stuck batch). mark = None: the batch's mark is not the settlement's.
        prev_ms = the contract's previous settlement (fundingTime − the SHORTER of the universe's and the row's own
        interval): nothing settles in between, so apply_batch may move a cursor that already covers prev_ms through this
        row (review 13.09: the batch-wide window alone never let a cursor cross a settlement)."""
        uni = self._universe("aux")
        _t, rows = self._latest_cached(LATEST_REUSE_S, "aux")
        now_ms = int(time.time() * 1000)
        cand = []
        for cid, r in rows.items():
            sym = uni["syms"].get(cid)
            ms, rate = _int(r.get("fundingTime")), _f(r.get("fundingRate"))
            if sym and rate is not None and 0 < ms <= now_ms + 60_000:
                ivs = [x for x in (uni["iv"].get(sym), interval_h(r.get("fundingRateIntervalMin"))) if x]
                cand.append((ms, sym, rate, min(ivs) if ivs else DEFAULT_IV_H))
        if not cand:
            return []
        top = max(c[0] for c in cand)
        out = []
        for ms, sym, rate, iv in cand:
            if ms == top and top > now_ms - iv * H_MS - config.SETTLE_GRACE_S * 1000:
                out.append(dict(exchange=self.name, symbol=sym, funding_ms=ms, rate=rate, mark=None,
                                prev_ms=ms - iv * H_MS))
        return out

    def close(self):
        if self._stream is not None:
            self._stream.stop()


PERP_CLIENTS = {NAME: EdgexPerp}


# --- WebSocket ---------------------------------------------------------------------------------------------------------
_FIELDS = ("contractName", "bestBidPrice", "bestAskPrice", "markPrice", "indexPrice", "fundingRate", "fundingTime",
           "nextFundingTime", "marketOpen")


def _default_connect(url: str, timeout: float):
    from .lighter import _WS                        # minimal RFC 6455 client (no websocket library in the venv)
    return _WS(url, timeout)


class _Stream:
    """ticker.all.1s in a daemon thread → cache {contractId: fields}; JSON ping answered with pong, reconnect with backoff,
    silence watchdog. Every frame carries all contracts [L]: Snapshot replaces the cache, «changed» merges per contract
    (a subset would keep the others)."""

    def __init__(self, url: str, name: str, connect=None):
        self.url, self.name = url, name
        self._connect = connect or _default_connect
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self.last_rx = 0.0
        self.synced = threading.Event()
        self.n_conn = 0
        self.n_ping = 0
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

    def snapshot(self) -> tuple[dict[str, dict], float]:
        with self._lock:
            return dict(self._data), self.last_rx

    def apply(self, msg: dict, rx: float) -> bool:
        if msg.get("type") != "quote-event":
            return False
        c = msg.get("content")
        if not isinstance(c, dict) or str(c.get("channel") or msg.get("channel") or "") != CHANNEL:
            return False
        rows = c.get("data")
        if not isinstance(rows, list):
            return False
        snap = str(c.get("dataType") or "").lower() == "snapshot"
        with self._lock:
            data = {} if snap else self._data
            for r in rows:
                if not isinstance(r, dict) or r.get("contractId") in (None, ""):
                    continue
                cid = str(r["contractId"])                 # a string on the wire (docs: integer) [L]
                e = dict(data.get(cid) or {})
                for k in _FIELDS:
                    if k in r:
                        e[k] = r[k]
                data[cid] = e
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
            conn.send_text(json.dumps({"type": "subscribe", "channel": CHANNEL}))
            last = time.time()
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
                    if isinstance(msg, dict):
                        typ = msg.get("type")
                        if typ == "ping":
                            conn.send_text(json.dumps({"type": "pong", "time": msg.get("time")}))
                            self.n_ping += 1
                        elif typ == "error":
                            raise ConnectionError(f"server error: {str(msg)[:200]}")
                        elif self.apply(msg, now):
                            last = now
                            healthy = healthy or self.synced.is_set()
                if now - last > WS_SILENT_S:
                    raise TimeoutError(f"no ticker data for {now - last:.0f} s")
        except Exception as e:  # noqa — any failure: reconnect
            self.err = f"{type(e).__name__}: {e}"
            log.warning("%s ws %s: %s — reconnecting", self.name, CHANNEL, self.err)
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
