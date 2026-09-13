"""OKX DEX — спот-нога на DEX (владелец 12.09: «в спот добавить okx dex»). Агрегатор ончейн-свопов, API v6.

Решения владельца: сети Solana, BSC, Robinhood; только токены, у которых есть фьючи на наших перп-биржах
(config.PERP_VENUES — список расширяется его командой); ликвидность токена от $50k; мостовые версии — с пометкой «мост»;
«Комиссия» DEX-строки — всё включено. Ключ — ТОЛЬКО из .env (OKX_DEX_API_KEY / OKX_DEX_SECRET / OKX_DEX_PASSPHRASE,
необязательно OKX_DEX_PROJECT), никогда не печатается; без ключа площадка выключена — запросов нет.

Исследование 12.09 (документация v6 + пробы без ключа с Мака и с VPS; ключом не проверено):
- без ключа всё отвечает 401 {"code":"50103"}; с VPS — JSON через Cloudflare (ZRH), не HTML-заглушка;
- подпись: OK-ACCESS-SIGN = Base64(HMAC-SHA256(secret, timestamp + METHOD + path[?query] + body)), timestamp — ISO UTC
  с миллисекундами, не дальше 30 с от часов сервера; заголовки OK-ACCESS-KEY / -SIGN / -TIMESTAMP / -PASSPHRASE;
- цены пачкой: POST /api/v6/dex/market/price (до 100 токенов, Basic: 100K бесплатных вызовов в месяц, дальше платно —
  поэтому пачка раз в несколько минут, а не каждый тик); /dex/index/current-price бесплатна, но это индекс из
  «сторонних источников», не цена DEX — для курсового не годится;
- исполнимая цена: GET /api/v6/dex/aggregator/quote на клип (amount в минимальных единицах токена): toTokenAmount,
  priceImpactPercent, tradeFee (сеть, USD — это газ, НЕ комиссия), estimateGasFee (единицы газа, не wei), taxRate,
  isHoneyPot. Комиссии агрегатора нет; на триальном ключе OKX оставляет себе положительное проскальзывание;
- лимит триального ключа — 1 запрос в секунду (60 дней); 429 / code 50011 — «слишком часто»; 402 — квота Market API
  кончилась; 82000 — нет ликвидности на сумму; 82104 / 82105 — токен / сеть не поддерживаются.

Фаза 2 (trade_spec §1, §4): swap() и approve_tx() только СОБИРАЮТ транзакцию (подпись, гарды и отправка — в
trade/evm_swap.py). Лимит ключа 1 запрос/с один на коллектор и трейдер, поэтому темп общий между процессами:
слот запроса занимается под flock в runtime/okxdex.pace (иначе два процесса по 0.7 запроса/с = 429 на ключе).
"""
from __future__ import annotations
import base64, datetime, fcntl, hashlib, hmac, json, os, threading, time, logging
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
import requests
from . import config
from .client import PermanentHTTPError, BannedError

log = logging.getLogger(__name__)

BASE = "https://web3.okx.com"
BATCH = 100
SWAP_PATH = "/api/v6/dex/aggregator/swap"
APPROVE_PATH = "/api/v6/dex/aggregator/approve-transaction"
# Общий темп ключа между процессами (= trade.tconfig.OKX_PACE_LOCK; здесь без импорта trade — коллектору он не нужен)
PACE_PATH = config.RUNTIME / "okxdex.pace"
PACE_AHEAD_MAX_S = 60.0     # слот в файле дальше этого — мусор или часы ушли назад: не ждать часами


class QuotaExhausted(PermanentHTTPError):
    """402: бесплатная квота Market API на месяц кончилась — отдельная красная заметка, а не «серые строки»."""


class NoLiquidity(RuntimeError):
    """82000: на эту сумму ликвидности нет — строка «нет ликвидности на клип»."""


class Unsupported(RuntimeError):
    """82104 / 82105: токен или сеть агрегатор не поддерживает — токен выбывает до пересборки вселенной."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class OkxDex:
    name = "okxdex"

    def __init__(self, session: requests.Session | None = None, key: str | None = None, secret: str | None = None,
                 passphrase: str | None = None, project: str | None = None, rps: float | None = None,
                 clock=time.time, pace_path: Path | str | bool | None = None):
        self.key = _env("OKX_DEX_API_KEY") if key is None else key
        self.secret = _env("OKX_DEX_SECRET") if secret is None else secret
        self.passphrase = _env("OKX_DEX_PASSPHRASE") if passphrase is None else passphrase
        self.project = _env("OKX_DEX_PROJECT") if project is None else project
        self._s = session or requests.Session()
        self._s.headers["user-agent"] = config.USER_AGENT
        self._clock = clock
        # темп: лимит ключа × мягкая доля бюджета (как у бирж: 70 %) — при 1 RPS запрос раз в ~1.43 с
        self.min_gap = 1.0 / ((rps or config.OKX_DEX_RPS) * config.WEIGHT_SOFT_LIMIT)
        self._lock = threading.Lock()
        self._last = 0.0
        # общий слот между процессами: None — runtime/okxdex.pace, False — только внутри процесса. Каталога нет —
        # общий темп выключен (библиотека каталоги не создаёт), темп процесса остаётся как был
        self.pace_path: Path | None = None if pace_path is False else Path(pace_path or PACE_PATH)
        if self.pace_path is not None and not self.pace_path.parent.is_dir():
            self.pace_path = None
        self.used_weight = 0; self.last_ok_ts = 0.0; self.n_429 = 0; self.n_err = 0; self.banned_until = 0.0
        self.started = time.time()          # до первого ответа возраст площадки считается от старта, а не от 1970 года
        self.calls = 0

    # --- служебное -------------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.key and self.secret and self.passphrase)

    def budget_used(self) -> float:
        return 0.0

    def health(self) -> dict:
        # stale_s: цены DEX приходят пачкой раз в OKX_DEX_JOB_S — «нет данных» на странице только после OKX_DEX_STALE_S
        return {"exchange": self.name, "used_weight": self.calls, "budget": 0.0, "last_ok_ts": int(self.last_ok_ts or self.started),
                "n_429": self.n_429, "n_err": self.n_err, "banned_until": int(self.banned_until), "enabled": self.enabled(),
                "stale_s": config.OKX_DEX_STALE_S}

    def _timestamp(self) -> str:
        return datetime.datetime.fromtimestamp(self._clock(), datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def sign(self, ts: str, method: str, path: str, body: str) -> str:
        mac = hmac.new(self.secret.encode(), (ts + method.upper() + path + body).encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _shared_slot(self, now: float, slot: float) -> float:
        """Слот под flock общего файла: не раньше последнего занятого ЛЮБЫМ процессом + свой интервал. Файл
        открывается на каждый запрос (1 в секунду — дёшево) и закрывается — это же снимает блокировку, так что
        упавший процесс замок не держит."""
        fd = os.open(self.pace_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 64).decode("ascii", "replace").strip()
            try:
                shared = float(raw) if raw else 0.0
            except ValueError:
                shared = 0.0
            if not shared <= now + PACE_AHEAD_MAX_S:      # и NaN тоже
                shared = 0.0
            slot = max(slot, shared + self.min_gap)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, repr(slot).encode())
            return slot
        finally:
            os.close(fd)

    def _reserve(self, now: float) -> float:
        """Занять ближайший слот запроса и вернуть, сколько ждать. Внутри процесса — как раньше (_last + интервал);
        поверх — общий файл runtime/okxdex.pace. Сбой файла — предупреждение и темп только своего процесса."""
        with self._lock:
            slot = max(now, self._last + self.min_gap)
            if self.pace_path is not None:
                try:
                    slot = self._shared_slot(now, slot)
                except OSError as e:
                    log.warning("okxdex: общий темп (%s) недоступен: %s — дальше темп только этого процесса",
                                self.pace_path, e)
                    self.pace_path = None
            self._last = slot
        return slot - now

    def _pace(self):
        wait = self._reserve(time.time())
        if wait > 0:
            time.sleep(wait)

    # --- запрос -----------------------------------------------------------------------------
    def request(self, method: str, path: str, params: dict | None = None, body=None,
                timeout: float = config.TICK_HTTP_TIMEOUT):
        if not self.enabled():
            raise PermanentHTTPError("okxdex: ключа нет в .env (OKX_DEX_API_KEY / OKX_DEX_SECRET / OKX_DEX_PASSPHRASE)")
        if time.time() < self.banned_until:
            raise BannedError(f"okxdex: пауза после 429 до {time.strftime('%H:%M:%S', time.gmtime(self.banned_until))}")
        self._pace()
        path_q = path + ("?" + urlencode(params) if params else "")
        body_s = json.dumps(body, separators=(",", ":")) if body is not None else ""
        ts = self._timestamp()
        headers = {"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": self.sign(ts, method, path_q, body_s),
                   "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase, "Content-Type": "application/json"}
        if self.project:
            headers["OK-ACCESS-PROJECT"] = self.project
        self.calls += 1
        try:
            r = self._s.request(method.upper(), BASE + path_q, data=body_s or None, headers=headers, timeout=timeout)
        except Exception as e:  # noqa — сеть: повтор — следующий цикл
            self.n_err += 1
            raise RuntimeError(f"okxdex {path}: {type(e).__name__}: {e}")
        try:
            data = r.json()
        except ValueError:
            self.n_err += 1
            raise RuntimeError(f"okxdex {path}: HTTP {r.status_code}, ответ не JSON: {r.text[:200]}")
        code = str(data.get("code")) if isinstance(data, dict) else ""
        if r.status_code == 429 or code == "50011":
            self.n_429 += 1
            self.banned_until = time.time() + 5.0
            raise BannedError(f"okxdex: 429 на {path} — пауза 5 с")
        if r.status_code == 402:
            raise QuotaExhausted(f"okxdex: квота Market API исчерпана ({path})")
        if r.status_code in (401, 403):
            raise PermanentHTTPError(f"okxdex: {r.status_code} code {code}: {data.get('msg') if isinstance(data, dict) else ''}")
        if code == "82000":
            raise NoLiquidity(f"okxdex {path}: нет ликвидности на сумму")
        if code in ("82104", "82105"):
            raise Unsupported(f"okxdex {path}: code {code} — токен/сеть не поддерживается")
        if r.status_code >= 400 or code != "0":
            self.n_err += 1
            raise RuntimeError(f"okxdex {path}: HTTP {r.status_code} code {code}: {str(data)[:200]}")
        self.last_ok_ts = time.time()
        return data.get("data")

    # --- данные -----------------------------------------------------------------------------
    def market_prices(self, tokens: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[float, int]]:
        """(chainIndex, адрес) -> (цена USD, время мс) пачками по 100 (Basic tier). EVM-адреса — в нижнем регистре;
        адреса Solana регистрозависимы — не трогаем."""
        out: dict[tuple[str, str], tuple[float, int]] = {}
        for i in range(0, len(tokens), BATCH):
            chunk = tokens[i:i + BATCH]
            body = [{"chainIndex": str(c), "tokenContractAddress": a} for c, a in chunk]
            for row in self.request("POST", "/api/v6/dex/market/price", body=body) or []:
                try:
                    out[(str(row["chainIndex"]), row["tokenContractAddress"])] = (float(row["price"]), int(row.get("time") or 0))
                except (KeyError, TypeError, ValueError):
                    continue
        return out

    def price_info(self, tokens: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """(chainIndex, адрес) -> {liq, price, vol24} пачками по 100 (Premium tier: 100K бесплатных в месяц — раз в час)."""
        out: dict[tuple[str, str], dict] = {}
        for i in range(0, len(tokens), BATCH):
            body = [{"chainIndex": str(c), "tokenContractAddress": a} for c, a in tokens[i:i + BATCH]]
            for row in self.request("POST", "/api/v6/dex/market/price-info", body=body) or []:
                try:
                    out[(str(row["chainIndex"]), row["tokenContractAddress"])] = dict(
                        liq=float(row.get("liquidity") or 0), price=float(row.get("price") or 0),
                        vol24=float(row.get("volume24H") or 0))
                except (KeyError, TypeError, ValueError):
                    continue
        return out

    def basic_info(self, tokens: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
        """(chainIndex, адрес) -> {name, symbol} — для подписи строки и признака моста в имени (Binance-Peg, Wormhole…)."""
        out: dict[tuple[str, str], dict] = {}
        for i in range(0, len(tokens), BATCH):
            body = [{"chainIndex": str(c), "tokenContractAddress": a} for c, a in tokens[i:i + BATCH]]
            for row in self.request("POST", "/api/v6/dex/market/token/basic-info", body=body) or []:
                try:
                    out[(str(row["chainIndex"]), row["tokenContractAddress"])] = dict(
                        name=row.get("tokenName") or row.get("name"), symbol=row.get("tokenSymbol") or row.get("symbol"))
                except (KeyError, TypeError):
                    continue
        return out

    def quote(self, chain: str, from_addr: str, to_addr: str, amount_units: int) -> dict:
        """Исполнимая котировка на сумму (минимальные единицы from-токена). tradeFee — газ маршрута в USD."""
        data = self.request("GET", "/api/v6/dex/aggregator/quote",
                            params={"chainIndex": str(chain), "amount": str(int(amount_units)),
                                    "fromTokenAddress": from_addr, "toTokenAddress": to_addr})
        q = (data or [{}])[0]
        f = lambda x: float(x) if x not in (None, "") else None
        tok = lambda k: q.get(k) or {}
        return dict(from_amount=int(q.get("fromTokenAmount") or 0), to_amount=int(q.get("toTokenAmount") or 0),
                    price_impact=f(q.get("priceImpactPercent")), gas_usd=f(q.get("tradeFee")),
                    from_decimals=int(tok("fromToken").get("decimal") or 0), to_decimals=int(tok("toToken").get("decimal") or 0),
                    buy_tax=f(tok("toToken").get("taxRate")), sell_tax=f(tok("fromToken").get("taxRate")),
                    honeypot=bool(tok("toToken").get("isHoneyPot") or tok("fromToken").get("isHoneyPot")),
                    t=time.time())

    # --- фаза 2: сборка транзакций (гарды, подпись и отправка — trade/evm_swap.py) --------------------
    @staticmethod
    def _pct(x) -> str:
        """Процент строкой для v6 ("3" = 3 %, не доля). float не принимаем: пороги владельца — Decimal."""
        if isinstance(x, (bool, float)):
            raise TypeError(f"процент должен быть Decimal/int/str, а не {type(x).__name__}")
        d = x if isinstance(x, Decimal) else Decimal(str(x).strip())
        if not d.is_finite() or d <= 0 or d > 100:
            raise ValueError(f"процент вне (0, 100]: {x}")
        return format(d, "f")

    @staticmethod
    def _units(x) -> str:
        if isinstance(x, bool) or not isinstance(x, int) or x <= 0:
            raise ValueError(f"сумма в минимальных единицах — целое > 0, а не {x!r}")
        return str(x)

    def swap(self, chain: str, token_in: str, token_out: str, amount_units: int, slippage_pct,
             impact_cap_pct, wallet: str) -> dict:
        """GET /aggregator/swap (v6) → data[0] как есть (routerResult + tx): гарды evm_swap проверяют именно сырой
        ответ. slippagePercent — имя v6 (процент строкой; v5-шный `slippage` долей НЕ шлём). impact_cap_pct None —
        параметр не шлётся (у API тогда 90 % = защиты нет; в live evm_swap требует значение владельца).
        НИКОГДА не шлёт swapReceiverAddress (получатель = userWalletAddress), feePercent, dexIds и approveTransaction:
        approve — отдельной транзакцией (approve_tx)."""
        params = {"chainIndex": str(chain), "amount": self._units(amount_units), "fromTokenAddress": token_in,
                  "toTokenAddress": token_out, "slippagePercent": self._pct(slippage_pct),
                  "userWalletAddress": wallet, "gasLevel": "average"}
        if impact_cap_pct is not None:
            params["priceImpactProtectionPercent"] = self._pct(impact_cap_pct)
        return self._first(self.request("GET", SWAP_PATH, params=params), SWAP_PATH)

    def approve_tx(self, chain: str, token: str, amount_units: int) -> dict:
        """GET /aggregator/approve-transaction → data[0]: data (calldata approve), dexContractAddress (это SPENDER, не
        адрес транзакции: to — контракт токена), gasLimit, gasPrice."""
        params = {"chainIndex": str(chain), "tokenContractAddress": token, "approveAmount": self._units(amount_units)}
        return self._first(self.request("GET", APPROVE_PATH, params=params), APPROVE_PATH)

    @staticmethod
    def _first(data, path: str) -> dict:
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise RuntimeError(f"okxdex {path}: пустой ответ ({str(data)[:120]})")
        return data[0]
