"""Истина Lighter — две площадки одного API: «lighter» (основной инстанс) и «lighter_rh» (инстанс Robinhood Chain).
Те же тикеры и market_id на двух инстансах — РАЗНЫЕ рынки (свой стакан, своя ставка), поэтому всё разведено по venue.
Свои запросы и свой разбор, без клиента коллектора lighter.py (его шапку читал только ради ловушек).

Замерено с Мака 12-13.09.2026:
- Хосты: основной REST https://mainnet.zklighter.elliot.ai/api/v1, WS wss://mainnet.zklighter.elliot.ai/stream;
  Robinhood REST https://api.rh.lighter.xyz/api/v1, WS wss://api.rh.lighter.xyz/stream. Пути одни и те же.
- /orderBookDetails?filter=perp → order_book_details: 234 перпа у основного (200 active, 17 active c
  market_config.force_reduce_only, 17 inactive), 57 у Robinhood (все active). Символ рынка — поле symbol ровно как в
  дашборде: «BTC», «1000PEPE» (не kPEPE — это имя в приложении, backend_symbol в tokenlist), «EURUSD», «SAMSUNGUSD».
  Торгуется = status active, не force_reduce_only (открыть нельзя, только закрыть), не market_config.hidden, created_at
  не в будущем; остальные — в markets с tradable=False и причиной.
- /tokenlist → tokens[{symbol, backend_symbol, market PERPS|SPOT, asset_type CRYPTO|RWA, categories, name}]: класс рынка.
  Ключ — backend_symbol, если есть (kPEPE → 1000PEPE). Категории RWA: STOCK, ETF, FX, COMMODITIES, PRE_IPO, BONDS,
  COMPUTE, KRW, MAJOR, NEW. Рынка нет в tokenlist — класс неизвестен (None).
- ТЕКУЩАЯ ставка — WS канал market_stats/all, поле current_funding_rate: «an estimation of the upcoming funding payment»
  (apidocs.lighter.xyz, websocket-reference); REST /funding-rates сам отсылает к этому каналу за живой ставкой. Единицы
  по документации биржи (docs.lighter.xyz/trading/funding): расчёт «at each hour mark», fundingRate = clamp(…)/8 —
  ПРОЦЕНТ ЗА 1 ЧАС. Проверено: соседнее поле funding_rate («the last funding payment») = rate последней строки /fundings
  (там «Funding rate percentage») за тот же час: BTC 0.0012 = 0.0012, STABLE −0.0107 = 0.0107 short. Значит доля за час
  = current_funding_rate / 100, со знаком. Первый кадр «subscribed/market_stats» — полный снимок {market_id: stats}.
  Запасной путь (WS не ответил): REST /funding-rates, строки exchange == «lighter» (под теми же market_id там же лежат
  строки binance/bybit/hyperliquid) — ДОЛЯ ЗА 8 ЧАСОВ: current_funding_rate/100 = rate/8 на 217 из 217 рынков основного и
  57 из 57 Robinhood. Этот путь совпадает с источником коллектора — какой путь сработал, пишется в rates_source.
- Следующий расчёт — ближайший круглый час (правило биржи; в ответах поля «следующий» нет, funding_timestamp — прошлый).
- /fundings {market_id, resolution=1h, start_timestamp, end_timestamp (СЕКУНДЫ), count_back=0}: не больше 750 строк —
  САМЫЕ НОВЫЕ в окне, по возрастанию; старше — постранично назад. timestamp в секундах на круглом часу; rate — ПРОЦЕНТ
  БЕЗ ЗНАКА, direction long (+: платят лонги) / short (−). Строки часа «в процессе» нет (в 23:41 последняя — 23:00).
- Лимит: 60 запросов в скользящую минуту на IP, у каждого base URL свой (apidocs rate-limits); превышение — 429 или
  405 и пауза фаервола 60 с, и она заденет живой коллектор на том же IP (его бюджет до 54 в минуту). Поэтому темп
  тестировщика 10 с между запросами к хосту (6 в минуту: 54 + 6 = 60), а 429/405 — пауза 61 с и повтор.
"""
from __future__ import annotations
import base64, hashlib, json, os, re, socket, ssl, struct, time
from decimal import Decimal
from urllib.parse import urlsplit
import requests
from .base import Truth, Http, dec, to_int, market, rate, next_boundary_ms, now_ms

HISTORY_PAGE = 750               # /fundings отдаёт не больше — самые новые в окне
HISTORY_MAX_PAGES = 12
BAN_S = 61.0                     # пауза фаервола 60 с (документация) + секунда
WS_TIMEOUT_S = 20.0
WS_MAX_MSG = 32 << 20            # снимок market_stats/all основного — ~145 КБ
FX_CODES = {"EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "HKD", "KRW", "CNH", "CNY", "SGD", "MXN", "TRY", "INR",
            "BRL", "ZAR", "SEK", "NOK", "TWD", "PLN", "ILS", "THB", "IDR", "PHP"}
INDEX_TICKERS = {"US500", "US100"}   # индексные перпы (S&P 500, Nasdaq 100); tokenlist метит их ETF/MAJOR, как SPY —
                                     # но SPY / QQQ — паи фондов (акции), а US500 / US100 — сами индексы
COIN_TOKENS = {"PAXG", "XAUT"}       # токены золота — монеты (как у истин Aster и остальных бирж), tokenlist: RWA/COMMODITIES


# --- HTTP с правилами Lighter -----------------------------------------------------------------------------------
class LighterHttp(Http):
    """Как base.Http, плюс: 429 И 405 — лимит IP (документация), пауза фаервола 61 с и повтор; тело с code ≠ 200 —
    ошибка биржи, а не данные."""

    def __init__(self, venue: str = "", gap_s: float = 0.0, timeout: float = 30.0, retries: int = 3):
        super().__init__(venue, gap_s, timeout, retries)

    def request(self, method: str, url: str, gap: float | None = None, **kw):
        last = None
        g = self.gap_s if gap is None else gap
        for i in range(self.retries):
            self._pace(url, g)
            self.calls += 1
            try:
                r = self.s.request(method, url, timeout=self.timeout, **kw)
            except requests.RequestException as e:
                last = f"{type(e).__name__}: {e}"; time.sleep(2.0 * (i + 1)); continue
            if r.status_code in (429, 405):
                last = f"HTTP {r.status_code} (лимит IP)"
                time.sleep(BAN_S); continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"; time.sleep(3.0 * (i + 1)); continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {url}: HTTP {r.status_code} {r.text[:200]}")
            try:
                body = r.json()
            except ValueError as e:
                last = f"не JSON: {e}"; time.sleep(1.0 * (i + 1)); continue
            if isinstance(body, dict) and body.get("code") is not None and to_int(body.get("code")) != 200:
                raise RuntimeError(f"{method} {url}: code {body.get('code')} {str(body)[:200]}")
            return body
        raise RuntimeError(f"{method} {url}: {last}")


# --- минимальный WebSocket (свой, не lighter._WS коллектора): один снимок канала ------------------------------------
def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi                                   # системные корни на Маке TLS не проходят
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa
        return ssl.create_default_context()


class MiniWS:
    """RFC 6455 ровно настолько, чтобы взять один снимок: рукопожатие, маскированный кадр наружу, внутрь — сборка
    фрагментов, ответ на ping, close — ошибка. Расширений не предлагаем — сервер не сжимает."""
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, sock, buf: bytes = b""):
        self.sock, self.buf = sock, buf

    @classmethod
    def connect(cls, url: str, timeout: float) -> "MiniWS":
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
                          f"User-Agent: funding_bot audit\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("рукопожатие: соединение закрыто")
                buf += chunk
                if len(buf) > 65536:
                    raise ConnectionError("рукопожатие: слишком длинный заголовок")
            head, rest = buf.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            status = lines[0].split()
            if len(status) < 2 or status[1] != "101":
                raise ConnectionError(f"рукопожатие: {lines[0][:100]}")
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
            want = base64.b64encode(hashlib.sha1((key + cls.GUID).encode()).digest()).decode()
            if hdrs.get("sec-websocket-accept") != want:
                raise ConnectionError("рукопожатие: неверный Sec-WebSocket-Accept")
        except Exception:
            sock.close()
            raise
        return cls(sock, rest)

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

    def _need(self, n: int):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("поток закрыт сервером")
            self.buf += chunk

    def frame(self) -> tuple[bool, int, bytes]:
        self._need(2)
        b0, b1 = self.buf[0], self.buf[1]
        fin, op, masked, n, i = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F, 2
        if n == 126:
            self._need(4); n, i = struct.unpack(">H", self.buf[2:4])[0], 4
        elif n == 127:
            self._need(10); n, i = struct.unpack(">Q", self.buf[2:10])[0], 10
        if n > WS_MAX_MSG:
            raise ConnectionError(f"кадр {n} байт")
        mask = None
        if masked:
            self._need(i + 4); mask, i = self.buf[i:i + 4], i + 4
        self._need(i + n)
        p, self.buf = self.buf[i:i + n], self.buf[i + n:]
        if mask:
            p = bytes(x ^ mask[k & 3] for k, x in enumerate(p))
        return fin, op, bytes(p)

    def message(self) -> bytes:
        frag: bytearray | None = None
        while True:
            fin, op, p = self.frame()
            if op == 0x9:
                self.send(0xA, p); continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise ConnectionError("сервер закрыл соединение")
            if op in (0x1, 0x2):
                if fin:
                    return p
                frag = bytearray(p); continue
            if op == 0x0:
                if frag is None:
                    raise ConnectionError("продолжение без первого фрагмента")
                frag += p
                if len(frag) > WS_MAX_MSG:
                    raise ConnectionError("сообщение слишком большое")
                if fin:
                    return bytes(frag)
                continue
            raise ConnectionError(f"неизвестный opcode {op}")

    def close(self):
        try:
            self.send(0x8, struct.pack(">H", 1000))
        except Exception:  # noqa
            pass
        try:
            self.sock.close()
        except Exception:  # noqa
            pass


def ws_snapshot(url: str, channel: str, key: str, timeout: float = WS_TIMEOUT_S, connect=None) -> dict:
    """Подключиться, подписаться на channel, дождаться первого «subscribed/…» (полный снимок), вернуть msg[key]."""
    ws = (connect or MiniWS.connect)(url, timeout)
    try:
        ws.send(0x1, json.dumps({"type": "subscribe", "channel": channel}).encode())
        t_end = time.time() + timeout
        while time.time() < t_end:
            try:
                msg = json.loads(ws.message())
            except ValueError:
                continue
            if isinstance(msg, dict) and str(msg.get("type") or "").startswith("subscribed/") \
                    and isinstance(msg.get(key), dict):
                return msg[key]
            if isinstance(msg, dict) and msg.get("error"):
                raise RuntimeError(f"WS {channel}: {str(msg)[:200]}")
        raise TimeoutError(f"WS {channel}: снимка нет за {timeout:.0f} с")
    finally:
        ws.close()


# --- класс и база рынка ------------------------------------------------------------------------------------------
def fx_pair(symbol: str) -> str | None:
    """EURUSD → EUR, USDJPY → JPY (валютная пара в записи символа), иначе None."""
    s = symbol.upper()
    if len(s) == 6 and s[:3] == "USD" and s[3:] in FX_CODES:
        return s[3:]
    if len(s) == 6 and s[3:] == "USD" and s[:3] in FX_CODES:
        return s[:3]
    return None


def asset_class(symbol: str, tok: dict | None) -> tuple[str | None, str]:
    """(класс, откуда он) по tokenlist биржи; своя таблица, не perp_class коллектора."""
    s = symbol.upper()
    if s in COIN_TOKENS:
        return "crypto", "токен золота — монета"
    if fx_pair(s):
        return "fx", "валютная пара в символе"         # USDHKD tokenlist метит CRYPTO/NEW — пара всё равно валютная
    if tok is None:
        return None, "нет в tokenlist — класс неизвестен"
    at = str(tok.get("asset_type") or "").upper()
    cats = {str(c).upper() for c in tok.get("categories") or []}
    tag = f"{at}/{','.join(sorted(cats)) or '—'}"
    if at != "RWA":
        return "crypto", tag
    name = str(tok.get("name") or "").lower()
    if "PRE_IPO" in cats:
        return "preipo", tag
    if "FX" in cats:
        return "fx", tag
    if s in INDEX_TICKERS or ("index" in name and not re.search(r"\b(etf|trust|fund)\b", name)):
        return "index", tag + " (индекс)"               # KRCOMP «Korea Composite Stock Price Index»
    if "BONDS" in cats:
        return "index", tag + " (доходность облигаций)"   # US10Y
    if "COMMODITIES" in cats and not cats & {"STOCK", "ETF"}:
        return "commodity", tag                         # SLV / USO — паи фондов (ETF) → акции ниже
    if cats & {"STOCK", "ETF"}:
        return "equity", tag
    if "COMPUTE" in cats:
        return "index", tag + " (ценовой индекс аренды GPU)"   # H100
    return "equity", tag + " (RWA только с NEW — по имени акции: AAOI, ARM, QCOM)"


def base_of(symbol: str, cls: str | None, tok: dict | None) -> str:
    """Сырая база: символ, кроме случаев, когда сама запись символа — пара к доллару: EURUSD → EUR; акции Кореи в
    долларах SAMSUNGUSD → SAMSUNG (tokenlist: STOCK+KRW, то же имя, что у SAMSUNG). 1000PEPE оставлен — множитель
    снимает общая norm_base аудитора."""
    fx = fx_pair(symbol)
    if fx:
        return fx
    cats = {str(c).upper() for c in (tok or {}).get("categories") or []}
    if cls == "equity" and "KRW" in cats and symbol.upper().endswith("USD") and len(symbol) > 3:
        return symbol[:-3]
    return symbol


def signed_rate(r: dict) -> Decimal | None:
    """Строка /fundings → доля за час со знаком: rate — процент без знака, direction long → +, short → −."""
    v = dec(r.get("rate"))
    if v is None:
        return None
    d = str(r.get("direction") or "").lower()
    if v == 0:
        return Decimal(0)
    if d == "long":
        return abs(v) / 100
    if d == "short":
        return -abs(v) / 100
    raise RuntimeError(f"строка /fundings без стороны: {r}")


# --- истина --------------------------------------------------------------------------------------------------------
class LighterTruth(Truth):
    venue = "lighter"
    REST = "https://mainnet.zklighter.elliot.ai/api/v1"
    WS = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
    # 6 запросов в минуту: коллектор на том же IP держит до 54 из 60 (lighter.TICK_CAP) — вместе ровно лимит. Было 8 с
    # (7.5 в минуту): 54 + 7.5 = 61.5 > 60, и 405 с паузой фаервола 60 с задел бы живой коллектор (ревью 13.09)
    gap_s = 10.0
    HIST_GAP_S = 10.0

    def __init__(self, http: Http | None = None, ws=None):
        super().__init__(http or LighterHttp(self.venue, self.gap_s))
        self.ws = ws or ws_snapshot
        self._ids: dict[str, int] | None = None     # символ → market_id (активный, если символ повторяется)
        self._sym_by_id: dict[int, str] = {}
        self.rates_source: str | None = None

    def _details(self) -> list[dict]:
        body = self.http.get(self.REST + "/orderBookDetails", {"filter": "perp"}) or {}
        det = [m for m in body.get("order_book_details") or [] if isinstance(m, dict) and m.get("symbol")]
        if not det:
            raise RuntimeError("orderBookDetails: перпов нет")
        ids: dict[str, int] = {}
        for m in det:
            mid = to_int(m.get("market_id"))
            if mid is None:
                continue
            sym = str(m["symbol"])
            self._sym_by_id[mid] = sym
            if sym not in ids or m.get("status") == "active":
                ids[sym] = mid
        self._ids = ids
        return det

    def _tokens(self) -> dict[str, dict]:
        try:
            toks = (self.http.get(self.REST + "/tokenlist") or {}).get("tokens") or []
        except Exception:  # noqa — без tokenlist классы неизвестны (None), присутствие это переживёт
            return {}
        return {str(t.get("backend_symbol") or t.get("symbol")): t for t in toks
                if isinstance(t, dict) and t.get("market") == "PERPS" and (t.get("backend_symbol") or t.get("symbol"))}

    def markets(self) -> dict[str, dict]:
        det = self._details()
        tmap = self._tokens()
        now = now_ms()
        out = {}
        for m in det:
            if str(m.get("market_type") or "perp") != "perp":
                continue
            sym = str(m["symbol"])
            mc = m.get("market_config") or {}
            why = []
            if m.get("status") != "active":
                why.append(f"статус {m.get('status')}")
            if mc.get("force_reduce_only"):
                why.append("только закрытие позиций (force_reduce_only)")
            if mc.get("hidden"):
                why.append("скрыт в приложении (hidden)")
            created = to_int(m.get("created_at")) or 0
            if created > now:
                why.append("до запуска (created_at в будущем)")
            tok = tmap.get(sym)
            cls, src = asset_class(sym, tok)
            note = f"market_id {m.get('market_id')}, {src}" + ("; " + "; ".join(why) if why else "")
            if sym in out and out[sym]["tradable"] and why:
                continue                               # символ повторяется — побеждает торгуемый
            out[sym] = market(base_of(sym, cls, tok), not why, cls, 1, (tok or {}).get("name"), note)
        return out

    def rates(self) -> dict[str, dict]:
        nxt = next_boundary_ms(now_ms(), 1)
        err = None
        try:
            stats = self.ws(self.WS, "market_stats/all", "market_stats", WS_TIMEOUT_S)
        except Exception as e:  # noqa — WS не ответил: запасной REST, источник пишется в rates_source
            stats, err = None, f"{type(e).__name__}: {e}"
        out = {}
        if stats:
            items = [stats] if "market_id" in stats else [v for v in stats.values() if isinstance(v, dict)]
            for v in items:
                sym = v.get("symbol") or self._sym_by_id.get(to_int(v.get("market_id")))
                r = dec(v.get("current_funding_rate"))
                if sym and r is not None:
                    out[str(sym)] = rate(r / 100, 1, nxt, "predicted")      # процент за час → доля за час
            if out:
                self.rates_source = "WS market_stats/all: current_funding_rate (% за 1 ч) / 100"
                return out
            err = err or "снимок без ставок"
        body = self.http.get(self.REST + "/funding-rates") or {}
        if not self._sym_by_id:
            self._details()
        for r in body.get("funding_rates") or []:
            if not isinstance(r, dict) or r.get("exchange") != "lighter":
                continue                               # под теми же market_id — ставки binance / bybit / hyperliquid
            sym = self._sym_by_id.get(to_int(r.get("market_id"))) or r.get("symbol")
            v = dec(r.get("rate"))
            if sym and v is not None:
                out[str(sym)] = rate(v / 8, 1, nxt, "predicted")            # доля за 8 ч → доля за час
        self.rates_source = f"REST /funding-rates (доля за 8 ч) / 8 — WS не дал снимок: {err}"
        return out

    def _market_id(self, symbol: str) -> int:
        if self._ids is None or symbol not in self._ids:
            self._details()
        mid = (self._ids or {}).get(symbol)
        if mid is None:
            raise RuntimeError(f"рынка {symbol} нет в orderBookDetails")
        return mid

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        mid = self._market_id(symbol)
        start_ms, end_ms = int(start_ms), int(end_ms)
        got: dict[int, Decimal] = {}
        hi = end_ms
        for _ in range(HISTORY_MAX_PAGES):
            body = self.http.get(self.REST + "/fundings",
                                 {"market_id": mid, "resolution": "1h", "start_timestamp": start_ms // 1000,
                                  "end_timestamp": hi // 1000, "count_back": 0}, gap=self.HIST_GAP_S) or {}
            rows = [r for r in body.get("fundings") or [] if isinstance(r, dict)]
            stamps = []
            for r in rows:
                ts = to_int(r.get("timestamp"))
                if ts is None:
                    continue
                ms = ts * 1000 if ts < 10 ** 11 else ts          # секунды (замер); миллисекунды тоже поймём
                stamps.append(ms)
                v = signed_rate(r)
                if v is not None and start_ms <= ms <= end_ms:
                    got[ms] = v
            if len(rows) < HISTORY_PAGE or not stamps:
                break
            first = min(stamps)
            if first <= start_ms:
                break
            hi = first - 1000                                    # страница полна — окно старше самой ранней строки
        return sorted(got.items())


class LighterRhTruth(LighterTruth):
    """Инстанс Robinhood Chain: свой хост и свои рынки (квота USDG); тикеры те же, что у основного, — рынки другие."""
    venue = "lighter_rh"
    REST = "https://api.rh.lighter.xyz/api/v1"
    WS = "wss://api.rh.lighter.xyz/stream?readonly=true"
