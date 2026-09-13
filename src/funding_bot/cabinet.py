"""Личный кабинет владельца — ТОЛЬКО ЧТЕНИЕ (владелец 12.09: «добавить кнопку справа сверху личного кабинета, где после
авторизации будут мои сделки … без возможности управления, просто их статусы»). Кнопка на дашборде — отдельная задача;
здесь — маршруты /cabinet (их подключает serve.py):

  GET  /cabinet             без сессии — форма входа, с сессией — сделки из runtime/trade.db
  POST /cabinet/login       логин и пароль → cookie сессии → 303 на /cabinet
  POST /cabinet/logout      сессия удаляется → 303 на /cabinet
  GET  /cabinet/deals.json  тот же список для автообновления страницы (раз в 15 с); без сессии — 401

Карточка сделки (правки владельца 13.09): статус, монета, пара, «PnL сейчас» / «PnL при выходе», дата открытия; у
активной — фандинг 1ч сейчас, курсовой (цены ног мелко), до ликвидации — по одной цифре, и «история выплат» по нажатию
(внутри той же страницы с сессией — отдельного маршрута нет); у закрытой — дата закрытия и «PnL итог». Объёмы, средние
ног и строка «на 12:06 · выход: удар DEX …» убраны («лишняя инфа»): свежая цифра — без подписи, устаревшая — серым,
мелко «на <дата время> · устарело» (старое за текущее не выдаётся).

Учётные данные — только из окружения (веб-служба читает свой runtime/cabinet.env, НЕ .env с ключами торговли; runtime/ выкат
не трогает): CABINET_LOGIN и CABINET_PASS_HASH
('scrypt$n$r$p$соль_hex$хэш_hex', строку печатает `funding_bot cabinet-hash`). Нет любой из двух или хэш битый — кабинет
выключен: «кабинет не настроен», вход невозможен. Пароль не хранится и не логируется; в журнал идёт только адрес клиента.

Сессии — в памяти процесса (рестарт веба = новый вход), 7 суток; cookie HttpOnly, SameSite=Strict, Path=/cabinet,
Secure — если запрос пришёл по https (X-Forwarded-Proto / CF-Visitor от cloudflared) или не с localhost (с ssh-туннеля
страница открывается по http, и cookie с Secure браузер там не вернул бы).
Перебор: 5 неудач за 15 мин на клиента и 30 на всех → 429 с Retry-After. Клиент — CF-Connecting-IP, только если запрос
пришёл с 127.0.0.1 (так подключается cloudflared, а заголовок ставит край Cloudflare); с чужого адреса заголовок
подделывается — там клиент = адрес сокета. Ответ на неверный логин и на неверный пароль одинаковый (scrypt считается в
обоих случаях), после неудачи — задержка. CSRF: Origin/Referer на POST (если есть) должен совпасть с хостом страницы;
плюс SameSite=Strict.

trade.db открывается только на чтение ('file:…?mode=ro' + query_only): кабинет не может ни записать, ни создать базу.
Ни одна строка trade.db не попадает в /data.json и на публичную страницу — это отдельные маршруты с сессией.
Ответы кабинета: no-store, X-Frame-Options DENY, CSP без внешних источников (стиль и скрипт — по sha256 содержимого).
"""
from __future__ import annotations
import base64, getpass, hashlib, hmac, html, ipaddress, json, logging, math, os, secrets, sqlite3, sys, threading, time
from collections import Counter, deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from . import config
from .trade import marks as tmarks, tconfig
from .trade.report import dval, fmt_num

log = logging.getLogger(__name__)

ENV_LOGIN, ENV_HASH = "CABINET_LOGIN", "CABINET_PASS_HASH"
# scrypt по заданию владельца: соль 16 байт, n=2**14, r=8, p=1 (~16 МБ памяти и ~50 мс на проверку)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SALT_BYTES, DKLEN = 16, 32
SCRYPT_MEM_CAP = 64 * 1024 * 1024     # хэш с параметрами тяжелее — битый (иначе строка в .env съела бы память веба)
KDF_PARALLEL = 2                      # одновременных scrypt не больше: поток запросов входа не раздует память
SESSION_TTL_S = 7 * 86400
SESSIONS_MAX = 20                     # больше — вытесняется самая старая (входов немного: владелец и его устройства)
COOKIE = "fb_cab"
FAIL_WINDOW_S = 15 * 60
FAILS_PER_CLIENT = 5
FAILS_TOTAL = 30
FAIL_DELAY_S = 0.8
MAX_BODY = 4096                       # форма входа — два коротких поля
DEALS_MAX = 500
REFRESH_S = 15
LOCAL_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
DEX_SPOT = "okxdex"                   # spot_ex строк DEX в table.json (dexleg.VENUE)

D = Decimal
ZERO = D(0)


# --- пароль: scrypt ---------------------------------------------------------------------------------------
def _mem(n: int, r: int, p: int) -> int:
    return 128 * r * (n + p + 2)      # столько просит OpenSSL: V (128·r·(n+2)) + B (128·r·p)


def _scrypt(pw: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_mem(n, r, p) + (1 << 20))


def make_hash(password: str, *, salt: bytes | None = None) -> str:
    """Пароль → 'scrypt$16384$8$1$<соль hex>$<хэш hex>'. Соль — 16 случайных байт на каждый вызов."""
    if not password:
        raise ValueError("пустой пароль")
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    dk = _scrypt(password.encode("utf-8"), salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${dk.hex()}"


@dataclass(frozen=True)
class PassHash:
    n: int
    r: int
    p: int
    salt: bytes
    dk: bytes

    def __repr__(self) -> str:        # в журнал и трассировки хэш не попадает
        return "PassHash(…)"


def parse_hash(raw: str | None) -> PassHash | None:
    """Строка из окружения → параметры; битая — None (кабинет выключен). Кавычки вокруг значения снимаются: .env можно
    писать и как CABINET_PASS_HASH='scrypt$…' (systemd кавычки снимает сам, а загрузчик попроще — нет)."""
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1].strip()
    parts = s.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, dk = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return None
    if n < 2 or n & (n - 1) or not 1 <= r <= 64 or not 1 <= p <= 16 or _mem(n, r, p) > SCRYPT_MEM_CAP:
        return None
    if len(salt) < 8 or len(dk) < 16:
        return None
    return PassHash(n, r, p, salt, dk)


def check_password(password: str, ph: PassHash) -> bool:
    got = _scrypt(password.encode("utf-8"), ph.salt, ph.n, ph.r, ph.p, len(ph.dk))
    return hmac.compare_digest(got, ph.dk)


def cli_hash(stdin=None, out=None, err=None, ask=getpass.getpass) -> int:
    """`funding_bot cabinet-hash`: пароль из stdin (с терминала — без эха и дважды, иначе первая строка) → строка для .env.
    Сам пароль не печатается и не логируется."""
    stdin, out, err = stdin or sys.stdin, out or sys.stdout, err or sys.stderr
    if stdin.isatty():
        pw = ask("пароль кабинета: ")
        if pw != ask("ещё раз: "):
            print("пароли не совпали — ничего не напечатано", file=err)
            return 2
    else:
        pw = stdin.readline()
        pw = pw[:-1] if pw.endswith("\n") else pw
        pw = pw[:-1] if pw.endswith("\r") else pw
    if not pw:
        print("пустой пароль — ничего не напечатано", file=err)
        return 2
    if len(pw) < 12:
        print("предупреждение: пароль короче 12 символов, а форма входа открыта из интернета — лучше длиннее", file=err)
    # одинарные кавычки: в .env «$» внутри них буквальный и для systemd, и для shell
    print(f"{ENV_HASH}='{make_hash(pw)}'", file=out)
    return 0


# --- перебор и сессии -----------------------------------------------------------------------------------
class Limiter:
    """Неудачные входы за скользящее окно: на клиента и на всех вместе. Удачный вход чистит счёт только своего клиента.
    Попытка записывается неудачей ЗАРАНЕЕ — в той же блокировке, что и проверка (begin), и снимается только удачным
    входом (success). Ревью 12.09: при «проверить → scrypt → записать неудачу» 40 одновременных запросов с одного адреса
    все проходили проверку до первой записанной неудачи и все 40 доходили до scrypt — потолки 5 и 30 не держали."""

    def __init__(self, per_client: int = FAILS_PER_CLIENT, total: int = FAILS_TOTAL, window_s: float = FAIL_WINDOW_S,
                 clock=time.time):
        self.per_client, self.total, self.window_s, self.clock = per_client, total, window_s, clock
        self._by: dict[str, deque] = {}
        self._all: deque = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        edge = now - self.window_s
        while self._all and self._all[0] <= edge:
            self._all.popleft()
        for k in list(self._by):
            q = self._by[k]
            while q and q[0] <= edge:
                q.popleft()
            if not q:
                del self._by[k]

    def _wait(self, client: str, now: float) -> int | None:
        waits = []
        if len(self._all) >= self.total:
            waits.append(self._all[len(self._all) - self.total] + self.window_s - now)
        q = self._by.get(client)
        if q and len(q) >= self.per_client:
            waits.append(q[len(q) - self.per_client] + self.window_s - now)
        return max(1, int(-(-max(waits) // 1))) if waits else None

    def retry_after(self, client: str) -> int | None:
        """Секунд до следующей разрешённой попытки; None — пробовать можно (только взгляд, попытку не занимает)."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            return self._wait(client, now)

    def begin(self, client: str) -> tuple[int | None, float | None]:
        """Занять попытку: (секунд ждать, None) — отказ; (None, отметка) — можно проверять пароль, попытка уже записана
        неудачей. Отметку отдать в success() при верном пароле; при неверном — ничего не делать (уже посчитана)."""
        with self._lock:
            now = self.clock()
            self._prune(now)
            wait = self._wait(client, now)
            if wait:
                return wait, None
            self._all.append(now)
            self._by.setdefault(client, deque()).append(now)
            return None, now

    def success(self, client: str, stamp: float | None = None) -> None:
        """Верный пароль: счёт клиента обнуляется, его занятая попытка из общего счёта снимается."""
        with self._lock:
            self._by.pop(client, None)
            if stamp is not None:
                try:
                    self._all.remove(stamp)
                except ValueError:          # уже вычищена окном
                    pass


class Sessions:
    """Сессии в памяти процесса: ключ — sha256 токена (сам токен живёт только в cookie браузера)."""

    def __init__(self, ttl_s: float = SESSION_TTL_S, max_n: int = SESSIONS_MAX, clock=time.time):
        self.ttl_s, self.max_n, self.clock = ttl_s, max_n, clock
        self._s: dict[bytes, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8", "replace")).digest()

    def new(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            for k in [k for k, exp in self._s.items() if exp <= now]:
                del self._s[k]
            while len(self._s) >= self.max_n:
                del self._s[min(self._s, key=self._s.get)]
            self._s[self._key(tok)] = now + self.ttl_s
        return tok

    def valid(self, token: str | None) -> bool:
        if not token or len(token) > 128:
            return False
        k = self._key(token)
        with self._lock:
            exp = self._s.get(k)
            if exp is None:
                return False
            if exp <= self.clock():
                del self._s[k]
                return False
            return True

    def drop(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._s.pop(self._key(token), None)


# --- запрос и ответ (без привязки к http.server: тесты зовут handle() напрямую) -------------------------
@dataclass
class Req:
    method: str
    path: str
    headers: dict
    peer: str
    body: bytes = b""

    def __post_init__(self):
        self.headers = {str(k).lower(): str(v) for k, v in (self.headers or {}).items()}

    def h(self, name: str) -> str:
        return (self.headers.get(name.lower()) or "").strip()


@dataclass
class Resp:
    code: int
    body: bytes
    ctype: str = "text/html; charset=utf-8"
    headers: list = field(default_factory=list)


def client_of(req: Req) -> str:
    """Кто пробует войти: за cloudflared (сокет 127.0.0.1) — CF-Connecting-IP, иначе адрес сокета."""
    if req.peer in LOCAL_PEERS:
        cf = req.h("CF-Connecting-IP")
        if cf:
            try:
                return str(ipaddress.ip_address(cf))
            except ValueError:
                pass
    return req.peer


def via_https(req: Req) -> bool:
    if req.h("X-Forwarded-Proto").split(",")[0].strip().lower() == "https":
        return True
    v = req.h("CF-Visitor")
    if v:
        try:
            return str(json.loads(v).get("scheme", "")).lower() == "https"
        except (ValueError, AttributeError):
            return False
    return False


def cookie_secure(req: Req) -> bool:
    return via_https(req) or req.peer not in LOCAL_PEERS


def session_token(req: Req) -> str | None:
    for part in req.h("Cookie").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and v.strip():
            return v.strip().strip('"')
    return None


def _host(x: str) -> str:
    x = x.strip().lower()
    for port in (":443", ":80"):
        if x.endswith(port):
            return x[:-len(port)]
    return x


def same_origin(req: Req, extra_hosts=()) -> bool:
    """CSRF: Origin (или Referer) POST-запроса — с хоста самой страницы. Нет обоих — пропускаем (curl, старый клиент):
    чужой сайт cookie всё равно не пришлёт — SameSite=Strict. «null» (песочница, file://) — отказ."""
    src = req.h("Origin") or req.h("Referer")
    if not src:
        return True
    if src.lower() == "null":
        return False
    try:
        host = _host(urlsplit(src).netloc)
    except ValueError:
        return False
    if not host:
        return False
    allowed = {_host(x) for x in [req.h("Host"), *req.h("X-Forwarded-Host").split(","), *extra_hosts] if x and x.strip()}
    return host in allowed


# --- trade.db только на чтение ---------------------------------------------------------------------------
def open_ro(path) -> sqlite3.Connection:
    """Соединение, которое не может писать: URI mode=ro (базу не создаст и не изменит) + query_only."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _dv(v) -> D | None:
    """TEXT из БД → Decimal; битое или бесконечное — None (страница не падает из-за одной строки)."""
    try:
        x = dval(v)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return x if x is None or x.is_finite() else None


def _row_key(d: dict):
    """Ключ строки пары в table.json: спот DEX «<chainIndex>:<токен>», перп, символ (как dexleg строит строку). EVM-адрес —
    нижним регистром (как раньше); mint Solana (501) — base58 как есть: регистр значим (S01)."""
    try:
        ci = tconfig.chain_index(d["chain"])
    except (KeyError, TypeError):
        return None
    spot = f"{ci}:{d['token']}"
    return (spot if _sol_spot(spot) else spot.lower(), d["perp_venue"], d["symbol"])


def _sol_spot(spot: str) -> bool:
    return str(spot).startswith("501:")


def _is_sol(d: dict) -> bool:
    from .trade.runtime import is_sol_deal
    try:
        return is_sol_deal(d)
    except Exception:                           # noqa — битая строка сделки: карточка BSC-вида, а не падение страницы
        return False


SOL_ROUTE = {"jupiter_build_v2": "Jupiter", "jupiter_order_v2": "Jupiter Order", "okx_solana_v6": "OKX"}
IDENT_TEXT = {"reviewed_override": "подтверждено владельцем", "verified_source": "подтверждено источником"}


def _sol_view(con: sqlite3.Connection, d: dict) -> dict:
    """Связка Solana × Hyperliquid: сеть, mint (как есть), фактические маршруты клипов, точный рынок HL и основание
    соответствия — из замороженной спецификации сделки и журнала (не из политики «auto»)."""
    from .trade import sol_ledger
    inst = sol_ledger.inst_of(d)
    try:
        routes = sol_ledger.ledger(con, d, fee_rate=None).routes
    except (sqlite3.Error, ArithmeticError, ValueError, TypeError, KeyError):
        routes = ()
    return {"mint": d.get("token"), "fullcoin": inst.get("perp_symbol") or d.get("symbol"),
            "routes": tuple(dict.fromkeys(SOL_ROUTE.get(r, r) for r in routes)),
            "identity": IDENT_TEXT.get(inst.get("identity_status")), "ident_to": str(inst.get("identity_expires_at")
                                                                                    or "")[:10]}


_TERMINAL = ("CLOSED", "ABORTED")
_DEX_UNKNOWN = ("DEX_SENT", "DEX_UNKNOWN")        # как engine.deal_book: исход свопа не выяснен
_PERP_OPEN = ("INTENT", "SENT", "UNKNOWN")        # исход заявки не выяснен


def load_deals(con: sqlite3.Connection, limit: int = DEALS_MAX, now: float | None = None) -> dict:
    """Все сделки (кроме черновиков — планов без «да»), новые сверху: статус, PnL (оценка трейдера или итог), у активной
    — «до ликвидации» (positionRisk из оценки трейдера) и история выплат фандинга (funding_income).
    Объёмы и средние ног карточка больше не показывает (правка владельца 13.09: «лишняя инфа»); остались только пометки
    «исход свопа / заявки выясняется» — клип в DEX_SENT/DEX_UNKNOWN, заявка сделки (префикс client_id fb-<сделка>-, как
    engine.deal_book) в INTENT/SENT/UNKNOWN."""
    deals = [dict(r) for r in con.execute("SELECT * FROM deals WHERE state != 'DRAFT' ORDER BY created DESC, id DESC "
                                          "LIMIT ?", (int(limit),))]
    drafts = con.execute("SELECT count(*) FROM deals WHERE state = 'DRAFT'").fetchone()[0]
    if not deals:
        return {"deals": [], "drafts": drafts}
    opened = {r[0]: r[1] for r in con.execute("SELECT deal_id, MIN(approved) FROM intents WHERE kind = 'entry' "
                                               "AND approved IS NOT NULL GROUP BY deal_id")}
    qs = lambda xs: ",".join("?" * len(xs))                                        # noqa: E731
    spot_unknown = {r[0] for r in con.execute(f"SELECT DISTINCT i.deal_id FROM clips c JOIN intents i ON i.id = c.intent_id "
                                              f"WHERE c.state IN ({qs(_DEX_UNKNOWN)})", _DEX_UNKNOWN)}
    perp_unknown = set()
    for (cid,) in con.execute(f"SELECT client_id FROM perp_orders WHERE client_id LIKE 'fb-%' "
                              f"AND state IN ({qs(_PERP_OPEN)})", _PERP_OPEN):
        parts = str(cid).split("-")
        if len(parts) >= 3:
            perp_unknown.add(parts[1])
    now = time.time() if now is None else now
    out = []
    for d in deals:
        last = con.execute("SELECT e.ts, e.kind, e.json, i.kind AS ikind FROM exec_events e LEFT JOIN intents i "
                           "ON i.id = e.intent_id WHERE e.deal_id = ? ORDER BY e.ts DESC, e.rowid DESC LIMIT 1",
                           (d["id"],)).fetchone()
        v = _deal_view(d, opened.get(d["id"]), d["id"] in spot_unknown, d["id"] in perp_unknown, last)
        if _is_sol(d):
            v["sol"], v["unit"] = _sol_view(con, d), "USDC"
        v["pnl"] = _pnl_view(con, d, now)
        if d.get("state") not in _TERMINAL:
            v["liq"] = None if v["sim"] else _liq_view(con, d, now)
            v["hist"] = None if v["sim"] else funding_history(con, d)
        out.append(v)
    return {"deals": out, "drafts": drafts}


def _liq_view(con: sqlite3.Connection, d: dict, now: float) -> dict | None:
    """«До ликвидации» — последняя оценка трейдера С flags.liq (positionRisk; trade/marks.py): строка, где чтение не
    удалось, прошлое не стирает — показывается прошлое со своим временем, старше MARK_STALE_S — «устарело»."""
    try:
        r = con.execute("SELECT flags_json FROM deal_marks WHERE deal_id = ? AND CASE WHEN json_valid(flags_json) "
                        "THEN json_type(flags_json, '$.liq') END = 'object' ORDER BY ts DESC, rowid DESC LIMIT 1",
                        (d["id"],)).fetchone()
    except sqlite3.OperationalError:            # трейдер старой версии: deal_marks ещё нет
        return None
    if r is None:
        return None
    try:
        lq = json.loads(r[0]).get("liq")
        ts = float(lq.get("ts"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(ts):
        return None
    price, mark = _dv(lq.get("price")), _dv(lq.get("mark"))
    return {"price": price, "mark": mark, "ts": ts, "dist": tmarks.liq_distance(price, mark),
            "stale": now - ts > tconfig.MARK_STALE_S}


HIST_MAX = 200                          # строк истории выплат в карточке (всего — по всем)


def funding_history(con: sqlite3.Connection, d: dict) -> dict:
    """Выплаты фандинга сделки: funding_income площадки и символа сделки с её открытия — те же границы, что у фандинга в
    PnL (marks.journal). rows — (время с, сумма $, итог с начала), новые сверху, не больше HIST_MAX; total — по всем.
    Связка Solana × Hyperliquid — фактические userFunding счёта сделки в её окне (sol_ledger), USDC."""
    if _is_sol(d):
        from .trade import sol_ledger
        try:
            rows = [(int(t * 1000), x) for t, x in sol_ledger.funding_rows(con, d)]
        except (sqlite3.Error, TypeError, ValueError):
            rows = []
    else:
        try:
            start = int(float(d["created"]) * 1000)
            rows = con.execute("SELECT ts, income FROM funding_income WHERE venue = ? AND symbol = ? AND ts >= ? "
                               "ORDER BY ts, tran_id", (d["perp_venue"], d["symbol"], start)).fetchall()
        except (sqlite3.OperationalError, TypeError, ValueError):
            return {"rows": [], "n": 0, "total": ZERO}
    acc, out = ZERO, []
    for ts, inc in rows:
        x = _dv(inc)
        if x is None or ts is None:
            continue
        acc += x
        out.append((int(ts) / 1000, x, acc))
    return {"rows": out[::-1][:HIST_MAX], "n": len(out), "total": acc}


def _pnl_view(con: sqlite3.Connection, d: dict, now: float) -> dict | None:
    """PnL для карточки. Активная — последняя оценка трейдера из deal_marks (trade/marks.py; кабинет в биржи не ходит).
    Закрытая — итог: строка final трейдера (с газом в $), без неё — по журналу здесь же (engine._deal_pnl без газа:
    цены BNB у кабинета нет — так и подписано). Отменённая — ничего."""
    st = d.get("state")
    if st == "ABORTED":
        return None
    try:
        mk = tmarks.decode(con.execute("SELECT * FROM deal_marks WHERE deal_id = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
                                       (d["id"],)).fetchone())
    except sqlite3.OperationalError:            # трейдер старой версии ещё не создал deal_marks — расчёта нет
        mk = None
    if st == "CLOSED":
        if mk is not None and mk["flags"].get("final") and mk["pnl_now"] is not None:
            out = {"final": True, "total": mk["pnl_now"], "no_gas": False}
            if mk["flags"].get("accounting_complete") is False:
                out["incomplete"] = True
            return out
        if _is_sol(d):
            from .trade import sol_ledger
            return sol_ledger.realized_view(con, d)
        try:
            j = tmarks.journal(con, d, until_ms=int(float(d.get("updated") or now) * 1000))
        except (sqlite3.Error, ArithmeticError, ValueError, TypeError, KeyError) as e:
            log.warning("кабинет: итог %s не посчитан: %s", d.get("id"), type(e).__name__)
            return None
        gas = j.gas_usd(None)                   # газа не было — 0; был — в $ не перевести
        return {"final": True, "total": j.spot_flow + j.perp_flow - j.fees + (j.funding or ZERO) - (gas or ZERO),
                "no_gas": gas is None}
    if mk is None or mk["flags"].get("final"):
        return {"final": False, "mark": None}
    return {"final": False, "mark": mk, "stale": now - mk["ts"] > tconfig.MARK_STALE_S}


def _deal_view(d: dict, opened, spot_unknown: bool, perp_unknown: bool, last) -> dict:
    ev = None
    if last is not None:
        try:
            data = json.loads(last["json"]) if last["json"] else {}
        except ValueError:
            data = {}
        ev = (event_text(last["kind"], data if isinstance(data, dict) else {}, last["ikind"]), last["ts"])
    return {
        "id": d["id"], "coin": d.get("coin"), "chain": d.get("chain"), "venue": d.get("perp_venue"),
        "symbol": d.get("symbol"), "leg_usd": _dv(d.get("leg_usd")), "state": d.get("state"), "reason": d.get("reason"),
        "sim": bool(d.get("sim")), "opened": opened or d.get("created"),
        "closed": d.get("updated") if d.get("state") in _TERMINAL else None,
        "spot_unknown": spot_unknown, "perp_unknown": perp_unknown,
        "last": ev, "row_key": _row_key(d), "live": None, "liq": None, "hist": None,
    }


# --- тексты статусов и событий ---------------------------------------------------------------------------
STATE_VIEW = {"OPEN": ("🟢", "открыта", "st-open"), "ENTERING": ("🔄", "входит", "st-run"),
              "EXITING": ("🔄", "выходит", "st-run"), "PAUSED": ("⏸️", "пауза", "st-pause"),   # FE0F — эмодзи, а не «II»
              "HALTED_MISMATCH": ("🛑", "остановлена: расхождение", "st-halt"), "CLOSED": ("✅", "закрыта", "st-done"),
              "ABORTED": ("⚪", "отменена", "st-done"), "DRAFT": ("📝", "черновик", "st-done")}

# причины паузы движка (Pause(reason, …) в trade/engine.py, reconcile) → человеческий текст без технических деталей
REASON_TEXT = {
    "error": "сбой исполнителя", "stop": "стоп владельца", "terminate": "служба останавливалась",
    "owner": "owner.toml не прочитан", "owner_missing": "в owner.toml не хватает параметров",
    "mode": "режим запрещает отправки", "native": "мало BNB на газ", "limit": "сработал лимит владельца",
    "funding": "фандинг не прочитан", "funding_sign": "фандинг сменил знак", "exec_time": "исполнение дольше допустимого",
    "plan_ab": "план устарел — нужен свежий", "book_unknown": "журнал сделки неполон — нужна сверка",
    "hedge_deficit": "ноги не сбалансированы", "position_unknown": "позиция на бирже не прочитана",
    "position_mismatch": "позиция не совпадает с журналом", "setup": "настройка перпа не удалась",
    "approve_unknown": "исход разрешения токена неизвестен", "approve_refused": "разрешение токена не отправлено",
    "approve_failed": "разрешение токена не прошло", "dex_unknown": "исход свопа выясняется",
    "dex_refused": "своп не отправлен", "dex_revert": "своп откатился дважды",
    "perp_refused": "заявка на перпе не отправлена", "perp_unknown": "исход заявки на перпе выясняется",
    "perp_rejected": "заявка на перпе нулевая после округления", "reduce_only_reject": "биржа отклонила откуп шорта",
    "busy": "токен или символ заняты другой сделкой", "state": "состояние сделки изменилось",
    "replan": "остаток не планируется", "changed": "дельта ног сменила знак", "wallet_unknown": "баланс кошелька не прочитан",
    "restart": "перезапуск во время исполнения", "entry": "вход не завершён", "exit": "выход не завершён",
    "rehedge": "дохедж не завершён", "undo": "откат не завершён", "inst_unverified": "инструмент сделки не подтверждён",
}


def reason_text(reason) -> str | None:
    """Код причины → текст; русская причина (их пишет сам движок: «сверено владельцем», «перп закрыт, спот остался…») —
    как есть; незнакомый латинский код и детали после «restart:» — не показываются."""
    if not reason:
        return None
    r = str(reason).strip()
    if r in REASON_TEXT:
        return REASON_TEXT[r]
    if r.startswith("restart"):
        return "после перезапуска ноги не сошлись — нужна сверка"
    if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in r):
        return r[:200]
    return None


_START = {"entry": "вход начат", "exit": "выход начат", "rehedge": "дохедж начат", "undo": "откат начат"}
_FIXED = {"rehedge": "дохедж выполнен", "undo": "откат выполнен"}
_PLAN = {"entry": "вход", "exit": "выход", "rehedge": "дохедж", "undo": "откат"}


def event_text(kind: str, data: dict, ikind: str | None = None) -> str:
    """Последнее событие exec_events → короткий текст для владельца (без хэшей, чисел заявок и текстов ошибок)."""
    k = data.get("intent_kind") or ikind
    st = data.get("state")
    if kind == "proposed":
        return "предложен план: " + _PLAN.get(k, "исполнение")
    if kind == "start":
        return _START.get(k, "исполнение начато")
    if kind == "requote":
        return "перекотировка не прошла — план отменён"
    if kind == "requote_ok":
        return "котировка обновлена, исполнение идёт"
    if kind == "paused":
        head = {"HALTED_MISMATCH": "остановка", "ABORTED": "вход отменён"}.get(st, "пауза")
        why = reason_text(data.get("reason"))
        return f"{head}: {why}" if why else head
    if kind == "auto_unwind":
        return "автооткат голой ноги"
    if kind == "approve":
        return "разрешение токена выдано" if data.get("status") == "ok" else "разрешение токена не прошло"
    if kind == "dex_not_sent":
        return "своп не отправлен"
    if kind == "dex":
        return {"ok": "своп на DEX выполнен", "reverted": "своп на DEX откатился"}.get(data.get("status"),
                                                                                      "исход свопа выясняется")
    if kind == "perp_unknown":
        return "исход заявки на перпе выясняется"
    if kind == "replan":
        return "остаток перепланирован"
    if kind == "fixed":
        return _FIXED.get(k, "исполнение завершено")
    if kind == "final":
        if st == "CLOSED":
            return "выход завершён — сделка закрыта"
        if st == "OPEN":
            return "частичный выход завершён" if k == "exit" else "вход завершён"
        return "исполнение завершено"
    if kind in ("reconcile_clip", "reconcile_order"):
        return "сверка после перезапуска"
    if kind == "restart_check":
        m = data.get("matched")
        return "проверка после перезапуска: " + ("ноги сходятся" if m is True else "расхождение" if m is False else "идёт")
    return "событие исполнения"


# --- числа ------------------------------------------------------------------------------------------------
def _trim(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _price(x: D | None) -> str:
    if x is None:
        return "—"
    a = abs(x)
    if a == 0:
        return "$0"
    places = 1 if a >= 1000 else 2 if a >= 100 else 4 if a >= 1 else min(12, 3 - a.adjusted())   # 4 значащих
    return "$" + fmt_num(x, places)


def _usd(x: D | None) -> str:
    return "—" if x is None else "$" + fmt_num(x, 0 if x == x.to_integral_value() else 2)


def _pct(x, places: int, cls: str | None = None) -> str:
    """Доля → «+0.034 %» крупно (правка владельца 13.09: текущий фандинг 1ч — 3 знака, остальные проценты — 2).
    cls None — цвет по знаку; иначе заданный («m» — устарело, «» — без цвета). Нет числа — «—»."""
    if isinstance(x, D):
        x = float(x) if x.is_finite() else None
    if not isinstance(x, (int, float)) or isinstance(x, bool) or x != x or abs(x) == float("inf"):
        return '<span class="m">—</span>'
    sign = "+" if x > 0 else ("−" if x < 0 else "")
    if cls is None:
        cls = "g" if x > 0 else ("r" if x < 0 else "")
    return f'<b class="mono {cls}">{sign}{abs(x) * 100:.{places}f}\u00a0%</b>'       # «%» не отрывается от числа


def _t(ts) -> str:
    """Время: сервер пишет UTC, скрипт страницы переводит в местное время браузера."""
    if not ts:
        return "—"
    return f'<time data-ts="{int(ts)}">{time.strftime("%d.%m %H:%M", time.gmtime(ts))} UTC</time>'


def _stale(ts) -> str:
    """Подпись устаревшей живой цифры: «на <дата время> · устарело» (старое не выдаётся за текущее). Свежей подписи нет:
    владелец 13.09 назвал строку «на 12:06 · выход: …» «лишняя инфа». Время источника неизвестно — просто «устарело»."""
    return f"на {_t(ts)} · устарело" if ts else "устарело"


def _pnl_usd(x: D | None, unit: str = "$") -> str:
    return "—" if x is None else fmt_num(x, 2, sign=True) + "\u00a0" + unit


def pnl_block(v: dict) -> str:
    """Две крупные строки «PnL сейчас» и «PnL при выходе» (цвет по знаку; расчёт старше MARK_STALE_S — серым, мелко «на
    <дата время> · устарело»), мелко — только пометки о неполадках: строку «на 12:06 · выход: удар DEX …» владелец 13.09
    назвал «лишняя инфа» — у свежего расчёта подписи нет. Закрытая — «PnL итог». Текст из БД экранируется."""
    p = v.get("pnl")
    if not p:
        return ""
    sol = bool(v.get("sol"))
    unit = v.get("unit") or "$"                 # связка Solana × Hyperliquid — USDC (не объявляется точным USD)
    e = lambda x: html.escape("" if x is None else str(x))
    cls = lambda x, gray=False: "m" if (gray or x is None) else ("g" if x > 0 else "r" if x < 0 else "")
    big = lambda label, x, gray=False: (f'<div class="pl">{e(label)} <b class="big mono {cls(x, gray)}">'
                                        f'{e(_pnl_usd(x, unit))}</b></div>')
    if p["final"]:
        notes = []
        if p.get("no_gas"):
            notes.append("сеть Solana не учтена: цены SOL у кабинета нет" if sol else
                         "газ в $ не учтён: цены BNB у кабинета нет")
        if p.get("incomplete"):
            notes.append("учёт Hyperliquid догружается — итог не окончательный")
        note = f'<div class="pn m">{" · ".join(notes)}</div>' if notes else ""
        return f'<div class="pnl">{big("PnL итог", p["total"])}{note}</div>'
    mk = p.get("mark")
    if mk is None:
        return '<div class="pnl"><div class="pl m">PnL: считается…</div></div>'
    stale = bool(p.get("stale"))
    fl = mk["flags"]
    note = [_stale(mk["ts"])] if stale else []
    if fl.get("uncovered"):
        note.append('<span class="unk">стакан HL мельче шорта — выход не оценён</span>' if sol else
                    '<span class="unk">стакан не покрывает шорт — остаток по худшей цене</span>')
    if sol and (fl.get("funding_incomplete") or fl.get("fills_incomplete") or fl.get("fees_est")):
        note.append("учёт Hyperliquid догружается")
    if fl.get("approve") == "unknown":
        note.append("allowance не прочитан — газ approve заложен")
    if fl.get("position") is not None:
        note.append('<span class="unk">позиция биржи не совпадает с журналом</span>')
    errs = fl.get("errors")
    if isinstance(errs, list) and errs:
        note.append(f'<span class="unk">не всё прочитано: {e(errs[0])}</span>')
    pn = f'<div class="pn m">{" · ".join(note)}</div>' if note else ""
    return (f'<div class="pnl">{big("PnL сейчас", mk["pnl_now"], stale)}{big("PnL при выходе", mk["pnl_exit"], stale)}'
            f'{pn}</div>')


# --- страница -------------------------------------------------------------------------------------------
# Палитра и основа — те же, что у дашборда (dashboard.CSS), но своей копией: общий лист тянул бы в кабинет чужие классы
# (у дашборда .cab — кнопка «Кабинет» с nowrap — растягивала страницу кабинета шире экрана телефона)
BASE_CSS = """
:root{--bg:#f2f4f1;--card:#fbfcfa;--card2:#e9ece7;--line:#d3d8cf;--fg:#1b201c;--mut:#616b62;--acc:#8a6d2f;
 --good:#1e7a4f;--bad:#a83b3b;--goodbg:#dfeee5;--badbg:#f6e2e0;--warnbg:#f4ecd8;--warn:#8a6d2f;--infobg:#e4e9ee;--info:#3a5b78;--link:#2f6ea8;}
@media (prefers-color-scheme: dark){:root{--bg:#12140f;--card:#191c16;--card2:#22261e;--line:#31362a;--fg:#e6e9df;--mut:#8f9787;
 --acc:#d9b45f;--good:#5cc48d;--bad:#e07a72;--goodbg:#18301f;--badbg:#33201d;--warnbg:#33290f;--warn:#d9b45f;--infobg:#1b2530;--info:#8fb2cc;--link:#8fb2cc;}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 "IBM Plex Sans",-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:15px;font-weight:600;margin:0}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.g{color:var(--good)}.r{color:var(--bad)}.m{color:var(--mut)}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.chip{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10.5px;font-weight:600;letter-spacing:.03em;margin-right:4px;white-space:nowrap}
.c-info{background:var(--infobg);color:var(--info)}
.up{color:var(--good)}.dn{color:var(--bad)}
"""
CAB_CSS = """
.wrap{max-width:760px;margin:0 auto;padding:16px 16px 24px}
.hdr{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px}
.acts{display:flex;gap:8px;align-items:center}
.acts form{margin:0}
.btn{display:inline-block;font:inherit;font-size:12px;font-weight:600;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer;text-decoration:none}
.btn:hover{background:var(--card2);text-decoration:none}
.sum{color:var(--mut);font-size:12px;margin:2px 0 12px}
.deal{background:var(--card);border:1px solid var(--line);border-left-width:4px;border-radius:8px;padding:12px 14px;margin:0 0 10px}
.st-open{border-left-color:var(--good)}.st-run{border-left-color:var(--info)}.st-pause{border-left-color:var(--warn)}
.st-halt{border-left-color:var(--bad)}.st-done{border-left-color:var(--line)}
.deal .hd{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.deal .stt{font-size:18px;font-weight:700}
.deal .cn{font-size:15px;font-weight:600}
.deal .why{color:var(--warn);font-size:12px;margin-top:3px}
.deal .pair{margin:6px 0 8px;color:var(--mut)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:6px 16px}
.kv .k{display:block;color:var(--mut);font-size:11px}
.unk{color:var(--warn);font-weight:600}
.ev{margin-top:8px;padding-top:6px;border-top:1px solid var(--line);font-size:12px;color:var(--mut)}
.empty{padding:28px 16px;text-align:center;color:var(--mut);background:var(--card);border:1px solid var(--line);border-radius:8px}
.login{display:flex;flex-direction:column;gap:12px;max-width:340px;margin:18px 0}
.login label{display:flex;flex-direction:column;gap:4px;color:var(--mut);font-size:12px}
.login input{font:inherit;font-size:16px;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:9px 10px}
.login .go{font-size:14px;padding:9px 12px;background:var(--fg);color:var(--bg);border-color:var(--fg)}
.login .err{color:var(--bad);min-height:1.2em}
.pnl{margin:2px 0 8px}
.pnl .pl{font-size:13px;line-height:1.6}
.pnl .big{font-size:18px;font-weight:700;margin-left:6px}
.pnl .pn{font-size:12px;margin-top:2px}
.kv .s{display:block;font-size:11px;color:var(--mut)}
.hist{margin-top:8px;font-size:12px}
.hist summary{cursor:pointer;color:var(--link);width:max-content}
.hist .tw{max-height:320px;overflow:auto;margin-top:6px}
.hist table{border-collapse:collapse}
.hist th,.hist td{padding:2px 14px 2px 0;text-align:right;white-space:nowrap}
.hist th:first-child,.hist td:first-child{text-align:left}
.hist th{color:var(--mut);font-weight:400;font-size:11px}
.hist .tot{margin-top:4px}
"""
STYLE = BASE_CSS + CAB_CSS

# Автообновление: раз в 15 с тот же список с сервера (HTML собран и экранирован на сервере), время — в местное время
# браузера. 401 (сессия истекла или веб перезапущен) — перезагрузка: покажется форма входа. Раскрытая «история выплат»
# (<details data-deal>) после обновления остаётся раскрытой и прокрученной туда же (новый HTML — новый блок прокрутки:
# без этого список раз в 15 с прыгал бы наверх).
JS = r"""
(function(){
  var p2 = function(n){ return (n < 10 ? '0' : '') + n; };
  function times(root){
    var els = root.querySelectorAll('time[data-ts]');
    for(var i = 0; i < els.length; i++){
      var d = new Date(Number(els[i].getAttribute('data-ts')) * 1000);
      if(isNaN(d.getTime())) continue;
      els[i].textContent = p2(d.getDate()) + '.' + p2(d.getMonth() + 1) + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
    }
  }
  function refresh(){
    if(document.hidden) return;
    fetch('/cabinet/deals.json', {cache: 'no-store', credentials: 'same-origin'}).then(function(r){
      if(r.status === 401){ location.reload(); return null; }
      return r.ok ? r.json() : null;
    }).then(function(j){
      if(!j) return;
      var box = document.getElementById('deals'), open = {}, ds = box.querySelectorAll('details[data-deal]'), k, tw, id;
      for(k = 0; k < ds.length; k++) if(ds[k].open){
        tw = ds[k].querySelector('.tw');
        open['d' + ds[k].getAttribute('data-deal')] = tw ? tw.scrollTop : 0;
      }
      box.innerHTML = j.html; times(box);
      ds = box.querySelectorAll('details[data-deal]');
      for(k = 0; k < ds.length; k++){
        id = 'd' + ds[k].getAttribute('data-deal');
        if(!open.hasOwnProperty(id)) continue;
        ds[k].open = true;
        tw = ds[k].querySelector('.tw');
        if(tw) tw.scrollTop = open[id];
      }
      document.getElementById('sum').textContent = j.summary;
      var u = document.getElementById('upd'); u.innerHTML = j.upd; times(u);
    }).catch(function(){});
  }
  times(document);
  setInterval(refresh, %d);
})();
""" % (REFRESH_S * 1000)


def _sha(s: str) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode()


CSP = ("default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; connect-src 'self'; "
       f"img-src 'self' data:; style-src '{_sha(STYLE)}'; script-src '{_sha(JS)}'")
SEC_HEADERS = (("Cache-Control", "no-store"), ("Pragma", "no-cache"), ("X-Frame-Options", "DENY"),
               ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
               ("Referrer-Policy", "same-origin"), ("X-Robots-Tag", "noindex, nofollow"))
# Referrer-Policy same-origin, а не no-referrer: при no-referrer браузер шлёт на POST «Origin: null», и проверка CSRF
# отказала бы самому владельцу


def _doc(body: str, script: bool = False) -> str:
    js = f"<script>{JS}</script>" if script else ""
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>funding_bot · кабинет</title>'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">'
            f'<style>{STYLE}</style></head><body><div class="wrap">{body}</div>{js}</body></html>')


def _html(code: int, doc: str, extra=()) -> Resp:
    return Resp(code, doc.encode("utf-8"), "text/html; charset=utf-8", [*SEC_HEADERS, *extra])


def _json(code: int, obj: dict) -> Resp:
    return Resp(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                list(SEC_HEADERS))


def _redirect(loc: str, extra=()) -> Resp:
    return Resp(303, b"", "text/plain; charset=utf-8", [*SEC_HEADERS, ("Location", loc), *extra])


_HEAD = '<div class="hdr"><h1>funding_bot · кабинет</h1><div class="acts"><a class="btn" href="/">дашборд</a>{}</div></div>'


def login_page(code: int = 200, msg: str = "", extra=()) -> Resp:
    body = (_HEAD.format("") +
            '<form class="login" method="post" action="/cabinet/login">'
            '<label>логин<input name="login" autocomplete="username" autocapitalize="none" spellcheck="false" '
            'maxlength="256" required></label>'
            '<label>пароль<input name="password" type="password" autocomplete="current-password" maxlength="1024" '
            'required></label>'
            '<button class="btn go" type="submit">войти</button>'
            f'<div class="err">{html.escape(msg)}</div></form>'
            '<p class="m">Только просмотр: статусы сделок, PnL и фандинг. Управления здесь нет.</p>')
    return _html(code, _doc(body), extra)


def off_page() -> Resp:
    return _html(503, _doc(_HEAD.format("") + '<div class="empty">кабинет не настроен</div>'))


def _kv(k: str, v: str) -> str:
    return f'<div><span class="k">{html.escape(k)}</span>{v}</div>'


GAP_RED = 0.005                         # |курсовой| больше — красным (как на дашборде)


def _src(ts, stale: bool, extra: str = "") -> str:
    """Мелкая строка под живой цифрой: extra (цены ног у курсового) и у устаревшей — «на <дата время> · устарело».
    Свежая без extra — ничего (одна цифра в ячейке)."""
    s = " · ".join(x for x in (extra, _stale(ts) if stale else "") if x)
    return f'<span class="s">{s}</span>' if s else ""


def live_cells(v: dict) -> list[str]:
    """Живые цифры активной сделки (правка владельца 13.09: «фандинг текущий, расстояние до ликвидации, текущий
    курсовой спред») — по одной цифре. Фандинг 1ч (3 знака) и курсовой (2 знака, цены спота | перпа мелко — как на
    дашборде) — из table.json коллектора; до ликвидации (2 знака) — из positionRisk оценки трейдера. Устаревшая — серым,
    мелко «на <дата время> · устарело» (старый фандинг за текущий не выдаётся). Симуляция — «—»."""
    e = lambda x: html.escape("" if x is None else str(x))            # noqa: E731
    dash = '<span class="m">—</span>'
    out = []
    lv = v.get("live")
    if lv:
        st = bool(lv.get("stale"))
        out.append(_kv("фандинг 1ч сейчас", _pct(lv.get("rate_h"), 3, "m" if st else None) + _src(lv.get("ts"), st)))
        gap = lv.get("gap")
        red = isinstance(gap, (int, float)) and not isinstance(gap, bool) and abs(gap) > GAP_RED
        px = f'<span class="mono">{e(_price(_dv(lv.get("px_spot"))))} | {e(_price(_dv(lv.get("px_perp"))))}</span>'
        out.append(_kv("курсовой", _pct(gap, 2, "m" if st else ("r" if red else "")) + _src(lv.get("ts"), st, px)))
    else:
        out += [_kv("фандинг 1ч сейчас", dash + '<span class="s">пары нет в таблице дашборда</span>'),
                _kv("курсовой", dash)]
    lq = v.get("liq")
    if v.get("sim"):
        out.append(_kv("до ликвидации", dash + '<span class="s">симуляция</span>'))
    elif not lq:
        out.append(_kv("до ликвидации", dash))
    else:
        st, dist = bool(lq.get("stale")), lq.get("dist")
        cls = "m" if st else ("r" if dist is not None and dist <= 0 else "")
        out.append(_kv("до ликвидации", _pct(dist, 2, cls) + _src(lq.get("ts"), st)))
    return out


def history_block(v: dict) -> str:
    """«история выплат» по нажатию (<details>, работает и без скрипта): время (местное — скрипт страницы), выплата $ со
    знаком, итог с начала сделки; новые сверху; внизу «всего». Только у активной сделки не в симуляции."""
    h = v.get("hist")
    if h is None:
        return ""
    e = lambda x: html.escape("" if x is None else str(x))            # noqa: E731
    sgn = lambda x: "g" if x > 0 else ("r" if x < 0 else "")          # noqa: E731
    unit = v.get("unit") or "$"
    usd = lambda x: fmt_num(x, 4, sign=True) + "\u00a0" + unit             # noqa: E731
    if h["n"]:
        rows = "".join(f'<tr><td>{_t(ts)}</td><td class="mono {sgn(x)}">{e(usd(x))}</td><td class="mono">{e(usd(acc))}'
                       f'</td></tr>' for ts, x, acc in h["rows"])
        more = (f'<div class="m">показаны последние {len(h["rows"])} из {h["n"]}</div>'
                if h["n"] > len(h["rows"]) else "")
        body = f'<div class="tw"><table><tr><th>время</th><th>выплата</th><th>итог</th></tr>{rows}</table></div>{more}'
    else:
        body = '<div class="m">выплат пока нет</div>'
    tot = h["total"]
    return (f'<details class="hist" data-deal="{e(v["id"])}"><summary>история выплат</summary>{body}'
            f'<div class="tot">всего: <b class="mono {sgn(tot)}">{e(_pnl_usd(tot, unit))}</b></div></details>')


def deal_card(v: dict) -> str:
    e = lambda x: html.escape("" if x is None else str(x))
    emo, label, cls = STATE_VIEW.get(v["state"], ("❔", str(v["state"]), "st-done"))
    sim = ' <span class="chip c-info">симуляция</span>' if v["sim"] else ""
    out = [f'<article class="deal {cls}"><div class="hd"><span class="stt">{emo} {e(label)}</span>'
           f'<span class="cn">{e(v["coin"])}</span><span class="m mono">{e(v["id"])}</span>{sim}</div>']
    why = reason_text(v["reason"]) if v["state"] in ("PAUSED", "HALTED_MISMATCH", "ABORTED") else None
    if why:
        out.append(f'<div class="why">{e(why)}</div>')
    for flag, text in (("spot_unknown", "исход свопа выясняется"), ("perp_unknown", "исход заявки выясняется")):
        if v.get(flag):
            out.append(f'<div class="why unk">{text}</div>')
    sv = v.get("sol")
    if sv:                                      # связка Solana × Hyperliquid: фактический маршрут, а не «okx»
        rt = " · ".join(sv["routes"]) if sv["routes"] else "Jupiter / OKX — лучший"
        out.append(f'<div class="pair">спот Solana · {e(rt)} <span class="up">▲</span> | перп Hyperliquid '
                   f'<span class="dn">▼</span> <span class="mono">{e(sv["fullcoin"])}</span> · '
                   f'{e(fmt_num(v["leg_usd"], 0) if v["leg_usd"] is not None else "—")} USDC</div>')
        mint = str(sv["mint"] or "")
        ident = f' · соответствие {e(sv["identity"])} до {e(sv["ident_to"])}' if sv["identity"] else ""
        out.append(f'<div class="pair">mint <span class="mono" title="{e(mint)}">{e(mint[:4] + "…" + mint[-4:])}'
                   f'</span>{ident}</div>')
    else:
        out.append(f'<div class="pair">спот okx·{e(v["chain"])} <span class="up">▲</span> | перп {e(v["venue"])} '
                   f'<span class="dn">▼</span> <span class="mono">{e(v["symbol"])}</span> · {e(_usd(v["leg_usd"]))} на ногу</div>')
    out.append(pnl_block(v))
    kv = [_kv("открыта", _t(v["opened"]))]
    if v["state"] in _TERMINAL:
        kv.append(_kv("отменена" if v["state"] == "ABORTED" else "закрыта", _t(v["closed"])))
    else:
        kv += live_cells(v)
    out.append(f'<div class="kv">{"".join(kv)}</div>')
    out.append(history_block(v))
    if v["last"]:
        out.append(f'<div class="ev">последнее: {e(v["last"][0])} · {_t(v["last"][1])}</div>')
    out.append("</article>")
    return "".join(out)


def summary_text(snap: dict) -> str:
    ds = snap["deals"]
    c = Counter(d["state"] for d in ds)
    work = sum(c[s] for s in ("ENTERING", "EXITING", "PAUSED", "HALTED_MISMATCH"))
    s = f"сделок {len(ds)} · открыто {c['OPEN']} · в работе и на паузе {work} · закрыто {c['CLOSED']}"
    if c["ABORTED"]:
        s += f" · отменено {c['ABORTED']}"
    if snap.get("drafts"):
        s += f" · планов без «да»: {snap['drafts']} (не показаны)"
    return s


def deals_fragment(snap: dict) -> str:
    if snap.get("err"):
        return f'<div class="empty r">{html.escape(snap["err"])}</div>'
    if not snap["deals"]:
        return '<div class="empty">сделок пока нет</div>'
    return "".join(deal_card(v) for v in snap["deals"])


def deals_page(snap: dict) -> Resp:
    body = (_HEAD.format('<form method="post" action="/cabinet/logout"><button class="btn" type="submit">выйти</button>'
                         '</form>') +
            f'<div class="sum"><span id="sum">{html.escape(summary_text(snap))}</span> · только просмотр · '
            f'обновлено <span id="upd">{_t(snap["now"])}</span></div><div id="deals">{deals_fragment(snap)}</div>')
    return _html(200, _doc(body, script=True))


# --- кабинет ----------------------------------------------------------------------------------------------
class Cabinet:
    def __init__(self, environ=None, db_path=None, *, clock=time.time, sleep=time.sleep,
                 fail_delay_s: float = FAIL_DELAY_S):
        env = os.environ if environ is None else environ
        login, raw = (env.get(ENV_LOGIN) or "").strip(), (env.get(ENV_HASH) or "").strip()
        self._hash = parse_hash(raw) if raw else None
        self._login = login if (login and self._hash is not None) else None
        if not login or not raw:
            self.why_off = f"нет {ENV_LOGIN} или {ENV_HASH} в окружении"
        elif self._hash is None:
            self.why_off = f"{ENV_HASH} не в формате scrypt$n$r$p$соль$хэш"
        else:
            self.why_off = None
        self.db_path = Path(db_path) if db_path else tconfig.TRADE_DB_PATH
        self.clock, self.sleep, self.fail_delay_s = clock, sleep, fail_delay_s
        self.sessions = Sessions(clock=clock)
        self.limiter = Limiter(clock=clock)
        self._kdf = threading.BoundedSemaphore(KDF_PARALLEL)
        self._tbl: tuple = (None, {}, None)         # (mtime table.json, строки DEX-пар, tick_ts)
        self._tbl_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._login is not None

    # --- маршруты ---
    def handle(self, req: Req) -> Resp:
        try:
            return self._route(req)
        except Exception:                      # noqa — текст исключения наружу не отдаём (пути, SQL)
            log.exception("кабинет: %s %s", req.method, req.path)
            return _html(500, _doc(_HEAD.format("") + '<div class="empty r">внутренняя ошибка кабинета</div>'))

    def _route(self, req: Req) -> Resp:
        path = req.path.split("?")[0].rstrip("/") or "/"
        if req.method == "GET":
            if path == "/cabinet":
                return self.page(req)
            if path == "/cabinet/deals.json":
                return self.deals_json(req)
            if path in ("/cabinet/login", "/cabinet/logout"):
                return _redirect("/cabinet")
        elif req.method == "POST":
            if path == "/cabinet/login":
                return self.login(req)
            if path == "/cabinet/logout":
                return self.logout(req)
            if path in ("/cabinet", "/cabinet/deals.json"):
                return _html(405, _doc(_HEAD.format("") + '<div class="empty">только просмотр</div>'),
                             [("Allow", "GET")])
        return _html(404, _doc(_HEAD.format("") + '<div class="empty">нет такой страницы</div>'))

    def page(self, req: Req) -> Resp:
        if not self.enabled:
            return off_page()
        if not self.sessions.valid(session_token(req)):
            return login_page()
        return deals_page(self.snapshot())

    def deals_json(self, req: Req) -> Resp:
        if not self.enabled:
            return _json(503, {"error": "off"})
        if not self.sessions.valid(session_token(req)):
            return _json(401, {"error": "login"})
        snap = self.snapshot()
        return _json(200, {"ts": int(snap["now"]), "html": deals_fragment(snap), "summary": summary_text(snap),
                           "upd": _t(snap["now"])})

    def login(self, req: Req) -> Resp:
        if not self.enabled:
            return off_page()
        if not same_origin(req, self._public_hosts()):
            return login_page(403, "запрос пришёл с чужой страницы — откройте кабинет заново и войдите")
        client = client_of(req)
        wait, stamp = self.limiter.begin(client)       # проверка и запись попытки — одним шагом (см. Limiter)
        if wait:
            log.debug("кабинет: вход с %s отложен на %d с", client, wait)
            return login_page(429, f"слишком много неудачных попыток — повторите через {max(1, -(-wait // 60))} мин",
                              [("Retry-After", str(wait))])
        try:
            form = parse_qs(req.body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=10)
        except ValueError:
            form = {}
        login = (form.get("login") or [""])[0]
        pw = (form.get("password") or [""])[0]
        if self._verify(login, pw):
            self.limiter.success(client, stamp)
            log.info("кабинет: вход с %s", client)
            return _redirect("/cabinet", [("Set-Cookie", self._cookie(req, self.sessions.new(), SESSION_TTL_S))])
        log.warning("кабинет: неудачный вход с %s", client)       # что введено — не пишем (пароль в поле логина)
        self.sleep(self.fail_delay_s)
        return login_page(401, "неверный логин или пароль")

    def logout(self, req: Req) -> Resp:
        if not same_origin(req, self._public_hosts()):
            return _html(403, _doc(_HEAD.format("") + '<div class="empty">запрос пришёл с чужой страницы</div>'))
        self.sessions.drop(session_token(req))
        return _redirect("/cabinet", [("Set-Cookie", self._cookie(req, "", 0))])

    # --- проверка входа ---
    def _verify(self, login: str, pw: str) -> bool:
        """scrypt считается всегда — и при чужом логине: время ответа не выдаёт, что именно неверно."""
        if len(login) > 256 or len(pw) > 1024:
            login, pw = "", ""
        with self._kdf:
            pw_ok = check_password(pw, self._hash)
        login_ok = hmac.compare_digest(hashlib.sha256(login.encode("utf-8", "replace")).digest(),
                                       hashlib.sha256(self._login.encode("utf-8")).digest())
        return pw_ok and login_ok

    def _cookie(self, req: Req, value: str, max_age: int) -> str:
        parts = [f"{COOKIE}={value}", "Path=/cabinet", f"Max-Age={int(max_age)}", "HttpOnly", "SameSite=Strict"]
        if cookie_secure(req):
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def _public_hosts() -> list[str]:
        """Текущая публичная ссылка туннеля (runtime/public_url.txt) — запасной «свой» хост для проверки Origin."""
        try:
            u = (config.RUNTIME / "public_url.txt").read_text().strip()
        except OSError:
            return []
        return [urlsplit(u).netloc] if u else []

    # --- данные ---
    def snapshot(self) -> dict:
        now = self.clock()
        try:
            con = open_ro(self.db_path)
        except FileNotFoundError:
            return {"now": now, "deals": [], "drafts": 0, "err": None}     # сделок ещё не было — базы нет
        except sqlite3.Error as e:
            log.warning("кабинет: trade.db не открылась: %s", e)
            return {"now": now, "deals": [], "drafts": 0, "err": "журнал сделок не открылся"}
        try:
            data = load_deals(con, now=now)
        except sqlite3.Error as e:
            log.warning("кабинет: trade.db не прочитана: %s", e)
            return {"now": now, "deals": [], "drafts": 0, "err": "журнал сделок не прочитан"}
        finally:
            con.close()
        if any(d["state"] not in _TERMINAL for d in data["deals"]):
            live, tick = self._live_rows()
            # свежесть — по возрасту снимка коллектора СЕЙЧАС (растёт и без нового файла): старше STALE_S (как чип
            # дашборда), время из будущего или строка сама «устарела» — серым «устарело»
            old = tick is None or abs(now - tick) > config.STALE_S
            for d in data["deals"]:
                row = live.get(d["row_key"]) if d["state"] not in _TERMINAL and d["row_key"] is not None else None
                if row is not None:
                    d["live"] = dict(row, ts=tick, stale=row["stale"] or old)
        return {"now": now, **data, "err": None}

    def _live_rows(self) -> tuple[dict, float | None]:
        """Строки DEX-пар из table.json (ставка 1ч, курсовой и цены ног для активных сделок) и время снимка коллектора
        (tick_ts); разбирается раз на снимок коллектора."""
        path = config.TABLE_PATH
        try:
            mt = os.stat(path).st_mtime_ns
        except OSError:
            return {}, None
        with self._tbl_lock:
            if self._tbl[0] == mt:
                return self._tbl[1], self._tbl[2]
        try:
            with open(path) as f:
                t = json.load(f)
        except (OSError, ValueError):
            return {}, None
        if not isinstance(t, dict):
            return {}, None
        try:
            tick = float(t.get("tick_ts") or t.get("ts") or 0) or None
        except (TypeError, ValueError):
            tick = None
        if tick is not None and not math.isfinite(tick):      # NaN «моложе» любого порога — за свежее не выдать
            tick = None
        idx = {}
        for r in t.get("sf_rows") or []:
            if isinstance(r, dict) and r.get("spot_ex") == DEX_SPOT:
                spot = str(r.get("spot") or "")
                idx[(spot if _sol_spot(spot) else spot.lower(), r.get("perp_ex"), r.get("perp"))] = {
                    "rate_h": r.get("spread"), "gap": r.get("gap"), "px_spot": r.get("px_spot"),
                    "px_perp": r.get("px_perp"), "stale": bool(r.get("stale"))}
        with self._tbl_lock:
            self._tbl = (mt, idx, tick)
        return idx, tick


_inst: Cabinet | None = None
_inst_lock = threading.Lock()


def instance() -> Cabinet:
    """Один кабинет на процесс веба: окружение читается при первом обращении (serve() делает его на старте)."""
    global _inst
    with _inst_lock:
        if _inst is None:
            _inst = Cabinet()
            if _inst.enabled:
                log.info("кабинет: включён (/cabinet)")
            else:
                log.info("кабинет выключен: %s", _inst.why_off)
        return _inst
