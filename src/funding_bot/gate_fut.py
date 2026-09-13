"""Gate USDT perpetuals — perp venue «gate» (owner 12.09: «добавить фьючи kucoin bitget и gate, lighter»; owner's order
after aster, binance, hyperliquid: kucoin, bitget, gate, lighter). Native perp client of venues.py:
perp_instruments / premium / books / history_since / recent_history, plus two hooks the collector does not call yet —
funding_intervals() (live intervals without a universe rebuild) and index_legs() (index composition for identity.py).
Public endpoints only, no keys. Transport = spot.GateSpot's (same host api.gateio.ws — api.gate.com does not resolve).

Measured 12.09.2026 from the Mac. Tags: [L] verified live, [D] verified in docs, [A] assumption.
Universe [L]  GET /futures/usdt/contracts — all 981 contracts in one array (1.26 MB, no gzip). name «BTC_USDT»,
      status «trading», in_delisting, is_pre_market, contract_type ("" 565 / stocks 380 / indices 18 / metals 12 /
      commodities 3 / forex 3), quanto_multiplier (tokens per contract), order_price_round (tick), order_size_min
      (contracts; 0 when enable_decimal), funding_interval (s: 28800 ×597, 14400 ×378, 3600 ×6), funding_rate_limit
      (cap, floor = −cap), mark_price, index_price, launch_time (s). taker_fee_rate «0.00075» on all — NOT the VIP0
      tariff (0.05 % taker [D]: gate.com announcement 36485); config.FEES_TAKER carries the tariff.
Tick [L]  GET /futures/usdt/tickers — all 981 in one call: funding_rate (= funding_rate_indicative), mark_price,
      index_price, highest_bid/highest_size, lowest_ask/lowest_size (sizes in CONTRACTS), quanto_multiplier. No
      timestamp in the body; the x-out-time header (server µs) is the snapshot time. One call feeds premium() AND
      books() (cached ctx_ttl_s from ARRIVAL, like Hyperliquid._ctx). 461 KB uncompressed: 32–40 s total from the Mac
      (ttfb 2–3 s) [L] — measure on ireland before enabling (see config_needed of the build).
Rate [L]  fraction PER INTERVAL, the live PREDICTED rate of the next settlement: 148 of 981 moved within 85 s; at the
      15:00 settlement of 12.09 the ticker at 15:00:20 equalled the settled history row (STORJ −0.019711, IOST
      −0.001334) and history had it within 120 s. Intervals change on the fly (STORJ 4 h → 1 h between 11.09 and
      12.09) — gate must NOT be in config.FIXED_INTERVAL_H. Settlements sit on the UTC grid (981/981).
      next_ms = the grid of the interval, NOT funding_next_apply: at 14:59:19 it already said 16:00 for all six 1 h
      contracts, before the 15:00 settlement [L].
Units [L]  mark/index/bid/ask per ONE unit of the base (PEPE mark 0.000003383 with quanto 10 000 000). MBABYDOGE is a
      million BABYDOGE (mark 0.0003824, its index legs BABYDOGE_USDT at 3.8e-10) — the only digit/letter-prefixed
      unit on Gate; 0G, 1INCH, 2Z, 4, 4STOCK are real names (factor 1).
History [L]  GET /futures/usdt/funding_rate?contract&from&to&limit — rows {"r","t"} NEWEST FIRST, t in SECONDS (the
      real settlement, 0–6 s after the grid). Window [from, to): from inclusive, to exclusive; with both set it returns
      the NEWEST `limit` rows of the range; limit ≤ 1000 (2000 → 400); depth 180 days (older → 400); `from` in ms →
      200 [] silently — always seconds. contract is required (400 MISSING_REQUIRED_PARAM). Weekend rows exist (stocks
      settle Sat/Sun, XAU settles r «0»). No all-contracts batch → recent_history() is [] and upkeep is the per-leg
      top-up by completeness (like Hyperliquid).
Index [L]  GET /futures/usdt/index_constituents/{contract} → {index, time, constituents: [{exchange «Binance», symbols
      [«BTC_USDT»], price, weight}]}; 970 of 981 answer, the 11 pre-market ones 400 INVALID_PARAM_VALUE «invalid index».
      index_legs() rewrites them into the shapes identity.Resolver._parse reads (LEG_EX).
Limits [L/D]  200 requests / 10 s per IP, counted per endpoint (x-gate-ratelimit-requests-remain / -limit) [L];
      429 TOO_MANY_REQUESTS [D] → pause max(10 s, reset − now + 1): the reset header always equals the current second,
      useless alone [L].
"""
from __future__ import annotations
import re, time, logging, threading
from urllib.parse import quote
import requests
from . import config
from .client import PermanentHTTPError, BannedError, BudgetExceeded
from .spot import SpotClient, GateSpot, _book, _f
from .symbols import norm_symbol_factor

log = logging.getLogger(__name__)

NAME = "gate"
H_MS = 3600_000
PATH_CONTRACTS = "/futures/usdt/contracts"
PATH_TICKERS = "/futures/usdt/tickers"
PATH_HIST = "/futures/usdt/funding_rate"
PATH_INDEX = "/futures/usdt/index_constituents/"
PAGE_URL = "https://www.gate.com/futures/USDT/{symbol}"   # SPA behind Cloudflare (403 to curl): links from live symbols only
HISTORY_LIMIT = 1000        # hard cap of the endpoint [L]; 1 h × 30 d = 720 rows → one call per leg
HISTORY_GAP_S = 0.1         # 10 req/s = half of the endpoint's 200 / 10 s; ~981 legs of upkeep ≈ 100 s
HISTORY_MAX_PAGES = 10      # 10 × 1000 rows ≥ 416 days at 1 h; a window that needs more raises, never truncates silently
HISTORY_DEPTH_S = 179 * 86400   # the endpoint refuses from older than 180 days [L]; one day of margin
THROTTLE_MIN_S = 10.0
OBS_SKEW_S = 120.0          # x-out-time further than this from the local request window → local request start is obs
NO_INDEX = ("invalid index", "contract_not_found")   # 400 of a contract without an index [L] / unknown contract [A]

# --- asset class and base (research 12.09 on all 981) ---------------------------------------------------------------
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})        # repo rule: gold tokens are coins (Gate files them under «metals»)
ETF_METALS = frozenset({"IAU", "SLV"})           # iShares ETFs under «metals»: shares, index = IAUON / SLVON spot + vendors
FX_COINS = frozenset({"USDC"})                   # «forex» USDC_USDT: a coin, index = USDC/USDT spot of five exchanges
# the unit is not in the name here: MBABYDOGE = 1 000 000 BABYDOGE [L]
FACTOR_OVERRIDE = {"MBABYDOGE": ("BABYDOGE", 1_000_000.0)}
# Gate's own names → the name the other venues use. Applied BEFORE config.PERP_CANON, only inside the class. Evidence =
# Gate's index constituents [L]: EDGEX = Bitget/Bybit/OKX EDGE (edgeX; Gate's EDGE_USDT is Definitive, its index is
# Gate-only — it keeps base EDGE and the resolver greys its pairs), RON = Binance RONIN, TSTBSC = Binance/Huobi TST,
# BROCCOLI = Binance BROCCOLI714 (consistent with config.SPOT_ALIASES for gate_spot); DFDVX / FUTUON / TQQQX index the
# DFDV / FUTU / TQQQ shares and perps; NG = Binance / HL NATGAS; XCU = Binance / Bitget / HL COPPER.
# NOT renamed: MEMECOIN, AINVDA, GIGGLEMAX — their indices are DEX pools of other tokens.
CANON = {"crypto": {"EDGEX": "EDGE", "RON": "RONIN", "TSTBSC": "TST", "BROCCOLI": "BROCCOLI714"},
         "equity": {"DFDVX": "DFDV", "FUTUON": "FUTU", "TQQQX": "TQQQ"},
         "commodity": {"NG": "NATGAS", "XCU": "COPPER"}}
# KR200_USDT — KOSPI 200, переведённый в доллары (индекс Gate = KOSPI200_KRW → USD, марк 0.80 против 1100 у HL xyz:KR200 и
# Bitget KR200 в пунктах; ревью 12.09): другая единица. До 13.09 здесь стояла своя база «KR200USD»; с 13.09 исключение —
# config.PERP_UNPAIRED["gate"] (одно на вселенную и тестировщика), база — тикер биржи. JPN225 / HK50 Gate — тоже в
# долларах, но их имена и так не совпадают с JP225 / HSI других площадок: приводить их нельзя.
_FX = re.compile(r"^(?:([A-Z]{3})USD|USD([A-Z]{3}))$")   # EURUSD → EUR (as Hyperliquid xyz:EUR and lighter.py)

# Gate constituent exchange (lower-case, spaces dropped) → (leg exchange as identity.py spells it, symbol separator).
# Our spot venues (identity.OURX) get the venue's own market id: Binance/Bitget BTCUSDT, Gate BTC_USDT, KuCoin BTC-USDT;
# DEX pools «MEME-WETH» (identity.dex_leg); Binance perps «NATGASUSDT» (kind bnperp). Names identity does not know yet
# (huobi, cryptocom, *_futures, *_index, infoway, gvol, ondo, gate_tradfi, pancakeswapv4) parse as «unknown_ex» —
# no evidence — until the integrator adds them to OTHX / PERPISH / VENDORS / DEXF (config_needed of the build).
LEG_EX = {"gate": ("gateio", "_"), "binance": ("binance", ""), "kucoin": ("kucoin", "-"), "bitget": ("bitget", ""),
          "okx": ("okx", "_"), "mexc": ("mexc", "_"), "bybit": ("bybit", "_"), "coinbase": ("coinbase", "_"),
          "huobi": ("huobi", "_"), "crypto.com": ("cryptocom", "_"),
          "binancealpha": ("binance_alpha", ""), "binancefutures": ("binance_future", ""),
          "gatefutures": ("gateio_futures", "_"), "bitgetfutures": ("bitget_futures", "_"),
          "bybitfutures": ("bybit_futures", "_"), "okxfutures": ("okx_futures", "_"),
          "hyperliquidfutures": ("hyperliquid_futures", "_"), "hyperliquid:xyz": ("hyperliquid", "_"),
          "binanceindex": ("binance_index", "_"), "okxindex": ("okx_index", "_"), "bitgetindex": ("bitget_index", "_"),
          "bybitindex": ("bybit_index", "_"), "gatetradfi": ("gate_tradfi", "_"), "massive": ("massive", "_"),
          "itick": ("itick", "_"), "infoway": ("infoway", "_"), "gvol": ("gvol", "_"), "ondo": ("ondo", "_"),
          "uniswapv3": ("uniswapv3", "-"), "uniswapv4": ("uniswapv4", "-"), "pancakev2": ("pancakeswapv2", "-"),
          "pancakev3": ("pancakeswapv3", "-"), "pancakev4": ("pancakeswapv4", "-")}
# legs priced per token of a spot market: the perp's unit goes there as «*N» (Binance style «PEPEUSDT*1000»)
SPOT_LEGS = frozenset({"gateio", "binance", "kucoin", "bitget", "okx", "mexc", "bybit", "coinbase", "huobi",
                       "cryptocom", "binance_alpha"})


def _int(x, default: int | None = None) -> int | None:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def asset_class(base_asset: str, contract_type: str | None, pre_market) -> str:
    """crypto / equity / commodity / index / fx / preipo; «rwa» for a contract_type nobody has classified yet (pairs
    with nothing — a new class must not land among coins by default)."""
    b = str(base_asset or "").upper()
    ct = str(contract_type or "").strip().lower()
    if pre_market:
        # pre-market — фаза торгов, а не класс (13.09): B200 / H100 — индексы аренды GPU (Silicon Data, USD за GPU-час), тот
        # же продукт и та же единица, что Bitget B200USDT / H100USDT (у Bitget тоже pre-market; марк 5.81 / 5.80, 2.672 /
        # 2.66) и Lighter H100 → «index». Монета BP и акции до IPO (OPENAI …) — «preipo», как раньше. Ставка 0, кап 1e-6.
        return "index" if ct == "indices" else "preipo"
    if b in GOLD_TOKENS or b in FX_COINS:
        return "crypto"
    if ct == "":
        return "crypto"                          # 4STOCK is a coin (contract_type "")
    if ct == "stocks":
        return "equity"
    if ct == "indices":
        return "index"
    if ct == "commodities":
        return "commodity"                       # BZ, CL, NG
    if ct == "metals":
        return "equity" if b in ETF_METALS else "commodity"
    if ct == "forex":
        return "fx"                              # EURUSD, GBPUSD
    log.warning("gate: %s has contract_type %r — unclassified, pairs with nothing", b, contract_type)
    return "rwa"


def perp_base(base_asset: str, cls: str) -> tuple[str, float]:
    """Gate base asset → (canonical base, tokens per unit of price)."""
    b, factor = FACTOR_OVERRIDE.get(str(base_asset).upper()) or norm_symbol_factor(base_asset)
    if cls == "fx":
        m = _FX.match(b)
        if m:
            b = m.group(1) or m.group(2)
    b = CANON.get(cls, {}).get(b, b)
    return config.PERP_CANON.get(cls, {}).get(b, b), factor


def interval_h(seconds) -> int:
    s = _int(seconds)
    if not s or s <= 0:
        return 8
    h = s / 3600.0
    if h < 1 or abs(h - round(h)) > 1e-9:
        log.warning("gate: funding_interval %s s is not whole hours — rounded", s)
    return max(1, int(round(h)))


def _unit(m: float) -> str:
    """«*1000000», never «*1e+06» (identity reads the multiplier with \\*(\\d+(\\.\\d+)?))."""
    return f"*{int(m)}" if float(m).is_integer() else f"*{m:.12f}".rstrip("0")


class GateFut(SpotClient):
    name = NAME
    base = GateSpot.base
    _payload_list = GateSpot._payload

    def __init__(self, session: requests.Session | None = None, history_gap_s: float | None = None,
                 ctx_ttl_s: float = 5.0, legs_gap_s: float | None = None):
        super().__init__(session)
        self.history_gap_s = (config.FUNDING_HISTORY_MIN_GAP_S.get(self.name, HISTORY_GAP_S)
                              if history_gap_s is None else history_gap_s)
        self.legs_gap_s = config.LEGS_GAP_S if legs_gap_s is None else legs_gap_s
        self.ctx_ttl_s = ctx_ttl_s
        self._lock = threading.Lock()
        self._tl = threading.local()                 # x-out-time of the last response on this thread
        self._last_hist = 0.0
        self._last_legs = 0.0
        self._tick: tuple[float, float, dict[str, dict]] = (0.0, 0.0, {})   # (arrival, obs, contract → tickers row)
        self._iv: dict[str, int] = {}                # last universe: contract → interval_h
        self._fac: dict[str, float] = {}             # last universe: contract → factor

    # --- transport quirks (GateSpot's, plus the server time of the snapshot) -------------------------------------
    def _usage(self, r):
        GateSpot._usage(self, r)
        try:
            self._tl.out_s = int(r.headers.get("x-out-time")) / 1e6
        except (TypeError, ValueError, AttributeError):
            self._tl.out_s = None

    def _throttled(self, r, body):
        if r.status_code != 429:
            return None
        try:
            reset = float(r.headers.get("x-gate-ratelimit-reset-timestamp")) - time.time() + 1.0
        except (TypeError, ValueError):
            reset = 0.0
        return max(THROTTLE_MIN_S, reset)

    def _payload(self, body, path):
        if not isinstance(body, (list, dict)):
            raise RuntimeError(f"{path}: expected JSON list/object, got {str(body)[:200]}")
        return body

    def _list(self, path: str, params: dict | None = None, **kw) -> list:
        data = self.get(path, params, **kw)
        if not isinstance(data, list):
            raise RuntimeError(f"{self.name} {path}: expected a list, got {str(data)[:200]}")
        return data

    def _pace(self, attr: str, gap: float):
        with self._lock:
            wait = getattr(self, attr) + gap - time.time()
            setattr(self, attr, time.time() + max(0.0, wait))
        if wait > 0:
            time.sleep(wait)

    # --- shared tick snapshot ----------------------------------------------------------------------------------------
    def _tickers(self) -> tuple[float, dict[str, dict]]:
        """(obs, contract → row). TTL counts from ARRIVAL: a 35 s download must not be fetched twice by premium() and
        books(). obs = the server's x-out-time (the snapshot is that old, not «arrival»), else the request start."""
        with self._lock:
            arr, obs, rows = self._tick
        if rows and time.time() - arr < self.ctx_ttl_s:
            return obs, rows
        self._tl.out_s = None
        t0 = time.time()
        data = self._list(PATH_TICKERS)                       # tick call: one try, short timeout (SpotClient.get)
        t1 = time.time()
        rows = {r["contract"]: r for r in data if isinstance(r, dict) and r.get("contract")}
        if not rows:
            raise RuntimeError(f"{self.name}: tickers without contracts")
        out_s = getattr(self._tl, "out_s", None)
        obs = min(out_s, t1) if out_s is not None and t0 - OBS_SKEW_S <= out_s <= t1 + OBS_SKEW_S else t0
        with self._lock:
            self._tick = (t1, obs, rows)
        return obs, rows

    def _mine(self, sym: str) -> bool:
        iv = self._iv
        return sym in iv if iv else sym.endswith("_" + config.QUOTE)

    # --- native perp interface ----------------------------------------------------------------------------------------
    def perp_instruments(self) -> list[dict]:
        raw = self._list(PATH_CONTRACTS, retries=3, timeout=config.HTTP_TIMEOUT)
        now_s = time.time()
        out, ivs, facs = [], {}, {}
        for c in raw:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            if not name.endswith("_" + config.QUOTE) or c.get("status") != "trading" or c.get("in_delisting"):
                continue
            if (c.get("type") or "direct") != "direct":
                continue                                       # all 981 are «direct» [L]; anything else prices differently [A]
            launch = _int(c.get("launch_time"), 0) or _int(c.get("create_time"), 0) or 0
            if launch > now_s:
                continue                                       # listing announced, trading not started [A]
            ba = name.rsplit("_", 1)[0]
            cls = asset_class(ba, c.get("contract_type"), c.get("is_pre_market"))
            base, factor = perp_base(ba, cls)
            iv = interval_h(c.get("funding_interval"))
            q, mark, cap = _f(c.get("quanto_multiplier")), _f(c.get("mark_price")), _f(c.get("funding_rate_limit"))
            # min order = order_size_min contracts (0 with enable_decimal → one contract [A]) × tokens × mark
            mn = max(_f(c.get("order_size_min")) or 0.0, 1.0) * q * mark if q and mark else None
            out.append(dict(exchange=self.name, symbol=name, base_asset=ba, base=base, factor=factor,
                            tick_size=_f(c.get("order_price_round")), step_size=q, min_notional=mn,
                            onboard_ms=launch * 1000, interval_h=iv, cap=cap, floor=-cap if cap is not None else None,
                            quote=config.QUOTE, contract="PERPETUAL", cls=cls,
                            url=PAGE_URL.format(symbol=quote(name, safe=""))))      # 龙虾_USDT → %E9%BE%99…
            ivs[name], facs[name] = iv, factor
        if not out:
            raise RuntimeError(f"{self.name}: contracts without tradable USDT perps")
        with self._lock:
            self._iv, self._fac = ivs, facs
        return out

    def premium(self) -> dict[str, dict]:
        """contract → {rate (predicted, fraction per interval), mark, index, next_ms, ts_ms, obs}."""
        obs, rows = self._tickers()
        now_ms = int(time.time() * 1000)
        ivs = self._iv
        out = {}
        for sym, r in rows.items():
            if not self._mine(sym):
                continue
            rate = _f(r.get("funding_rate"))
            if rate is None:
                continue
            iv = ivs.get(sym) or 8
            step = iv * H_MS
            out[sym] = dict(rate=rate, mark=_f(r.get("mark_price")), index=_f(r.get("index_price")),
                            next_ms=(now_ms // step + 1) * step, ts_ms=int(obs * 1000), obs=obs, interval_h=iv)
        return out

    def books(self) -> dict[str, dict]:
        """Best bid / ask; quantities in units of the price (sizes are contracts × quanto_multiplier tokens) [A]."""
        obs, rows = self._tickers()
        out = {}
        for sym, r in rows.items():
            if not self._mine(sym):
                continue
            q = _f(r.get("quanto_multiplier")) or 1.0
            b = _book(r.get("highest_bid"), r.get("lowest_ask"),
                      abs(_f(r.get("highest_size")) or 0.0) * q, abs(_f(r.get("lowest_size")) or 0.0) * q)
            if b:                                              # drops empty / crossed books
                b["obs"] = obs
                out[sym] = b
        return out

    def funding_intervals(self) -> dict[str, int]:
        """Live intervals of the universe (STORJ 4 h → 1 h switches between universe rebuilds). Hook for
        venues.funding_intervals(); not called by the collector until it is wired. Costs one contracts call (1.26 MB)."""
        raw = self._list(PATH_CONTRACTS, retries=3, timeout=config.HTTP_TIMEOUT)
        live = {c["name"]: interval_h(c.get("funding_interval")) for c in raw if isinstance(c, dict) and c.get("name")}
        if not live:
            raise RuntimeError(f"{self.name}: contracts without names")
        with self._lock:
            uni = self._iv
            out = ({s: live.get(s, iv) for s, iv in uni.items()} if uni
                   else {s: iv for s, iv in live.items() if s.endswith("_" + config.QUOTE)})
            self._iv = out
        return dict(out)

    def history_since(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Settled rates of one contract in [start_ms, end_ms], ascending. One call covers 1000 rows (30 days at 1 h);
        a longer window pages backwards with to = the oldest t (to is exclusive, so no row repeats)."""
        start_ms = int(start_ms)
        end_ms = int(end_ms or time.time() * 1000)
        if end_ms < start_ms:
            return []
        lo = max(start_ms // 1000, int(time.time()) - HISTORY_DEPTH_S)   # older than 180 d does not exist at Gate
        to = end_ms // 1000 + 1
        got: dict[int, dict] = {}
        for _ in range(HISTORY_MAX_PAGES):
            if to <= lo:
                break
            self._pace("_last_hist", self.history_gap_s)
            rows = self._list(PATH_HIST, dict(contract=symbol, **{"from": lo}, to=to, limit=HISTORY_LIMIT),
                              retries=3, timeout=config.HTTP_TIMEOUT)
            stamps = []
            for r in rows:
                t = _int(r.get("t")) if isinstance(r, dict) else None
                if t is None:
                    continue
                stamps.append(t)
                rate, ms = _f(r.get("r")), t * 1000
                if rate is not None and start_ms <= ms <= end_ms:
                    got[ms] = dict(exchange=self.name, symbol=symbol, funding_ms=ms, rate=rate, mark=None)
            if len(rows) < HISTORY_LIMIT or not stamps:
                break
            to = min(stamps)
        else:
            # every page full and `from` still not reached: a silent cut would confirm depth that was never fetched
            raise RuntimeError(f"{self.name} {symbol}: history did not reach {start_ms} in {HISTORY_MAX_PAGES} pages")
        return [got[k] for k in sorted(got)]

    def recent_history(self) -> list[dict]:
        """No «settled rates of all contracts» endpoint: funding_rate requires contract [L]."""
        return []

    # --- index composition (identity.py) ------------------------------------------------------------------------------
    @staticmethod
    def legs_of(constituents: list, factor: float = 1.0) -> list[dict] | None:
        """Gate constituents → legs {exchange, symbol, weight, raw} in the shapes identity.Resolver._parse reads.
        Zero-weight reference legs are dropped when a weighted leg exists. The perp's unit (MBABYDOGE = 1e6 BABYDOGE)
        goes onto spot legs as «*N», as Binance's /fapi/v1/constituents writes «PEPEUSDT*1000»."""
        legs = []
        for c in constituents or []:
            if not isinstance(c, dict):
                continue
            ex_raw = str(c.get("exchange") or "").strip()
            syms = [str(s).strip() for s in (c.get("symbols") or []) if str(s).strip()]
            if not ex_raw or not syms:
                continue
            ex, sep = LEG_EX.get(ex_raw.lower().replace(" ", ""),
                                 (re.sub(r"[^a-z0-9]+", "_", ex_raw.lower()).strip("_") or "unknown", "_"))
            w = _f(c.get("weight")) or 0.0
            for s in syms:
                b, _, q = s.rpartition("_") if "_" in s else (s, "", "")
                sym = f"{b}{sep}{q}" if q else b
                if ex == "binance_future":
                    sym = sym.upper()
                if ex in SPOT_LEGS and factor != 1.0:
                    _lb, lf = norm_symbol_factor(b)
                    m = factor / (lf or 1.0)
                    if m != 1.0:
                        sym += _unit(m)
                legs.append({"exchange": ex, "symbol": sym, "weight": f"{w / len(syms):g}", "raw": f"{ex_raw} {s}"})
        if any(_f(x["weight"]) for x in legs):
            legs = [x for x in legs if _f(x["weight"])]
        return legs or None

    def _dex_search(self, q: str):
        from . import identity_src
        return identity_src._get_json(config.DEXSCREENER_SEARCH_URL + quote(q))

    def index_legs(self, symbols: list[str], deadline_s: float | None = None) -> dict[str, dict]:
        """Contract → {"legs": [...] | None (no index), "dex": {query: [pools]}} — the contract of
        identity_src.index_legs (which dispatches here by hasattr). DEX-only indices (MEMECOIN, MICRODUCK) get the same
        DexScreener search. One contract failing is skipped (retried next job); 429 / a foreign 4xx / the deadline
        returns what was collected."""
        from . import identity, identity_src
        out, errs = {}, 0
        t_end = time.time() + (config.LEGS_DEADLINE_S if deadline_s is None else deadline_s)
        for s in symbols:
            if time.time() > t_end:
                break
            self._pace("_last_legs", self.legs_gap_s)
            if time.time() > t_end:
                break                                          # the pacing pause itself ran past the deadline
            try:
                d = self.get(PATH_INDEX + quote(s, safe=""), retries=2, timeout=config.HTTP_TIMEOUT)
                comps = d.get("constituents") if isinstance(d, dict) else None
            except PermanentHTTPError as e:
                if not any(k in str(e).lower() for k in NO_INDEX):
                    if out:
                        log.warning("%s index constituents: %s — batch cut", self.name, e)
                        break
                    raise
                comps = None                                   # pre-market: 400 «invalid index»
            except (BannedError, BudgetExceeded):
                if out:
                    break
                raise
            except Exception as e:  # noqa — network / 5xx / broken body of one contract: the rest go on
                errs += 1
                log.warning("%s index constituents %s: %s: %s", self.name, s, type(e).__name__, e)
                continue
            legs = self.legs_of(comps, self._fac.get(s, 1.0)) if isinstance(comps, list) else None
            e = {"legs": legs, "dex": {}}
            try:
                qs = identity_src._dex_queries(legs or [], s)
            except Exception as ex:  # noqa — a leg of a strange shape: no pool search
                log.warning("%s index constituents %s: legs not parsed: %s", self.name, s, ex)
                qs = []
            for q in qs:
                try:
                    e["dex"][q] = identity.dex_pairs(self._dex_search(q))
                except Exception as ex:  # noqa — the pool search failed: this row's verdict stays «unchecked»
                    log.warning("DexScreener %r: %s: %s", q, type(ex).__name__, ex)
                time.sleep(0.25)
            out[s] = e
        if not out and errs:
            raise RuntimeError(f"{self.name}: index constituents failed for all {errs} contracts of the batch")
        return out
