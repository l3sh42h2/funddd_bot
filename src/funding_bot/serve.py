"""Веб-процесс дашборда: читает table.json коллектора, ничего больше не знает.

  GET /           страница (данные вшиты, дальше страница сама тянет /data.json раз в 10 с)
  GET /data.json  живая таблица как есть
  GET /status     короткий json: возраст тиков, бэкфилл, заметки шагов
  /cabinet…       личный кабинет владельца только для чтения (cabinet.py: вход по паролю, сделки из trade.db на чтение);
                  в /data.json, / и /status из trade.db не попадает ничего
Слушает только 127.0.0.1: с Мака — ssh-туннель (порт 8792, у lphedge 8791), для всех остальных (владелец 12.09:
«публичный адрес», «чтоб у друга открывался», без пароля) — быстрый туннель Cloudflare (deploy/funding_bot-tunnel.service,
текущая ссылка — runtime/public_url.txt). Входящих портов на сервере нет.
"""
from __future__ import annotations
import gzip, json, os, time, logging, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import cabinet, config, dashboard

log = logging.getLogger(__name__)

_cache: dict[tuple, bytes] = {}
_cache_lock = threading.Lock()
_cache_build_lock = threading.Lock()
POST_DRAIN_MAX = 65536          # тело POST до стольких байт вычитывается даже при отказе — keep-alive не рассинхронизируется


def _cached(kind: str, gz: bool) -> bytes:
    """Страница или /data.json по текущему table.json — собирается и сжимается один раз на снимок (раз в 10 с), а не на
    каждый запрос: по публичной ссылке страницу открывают несколько человек, а сжатие ~6 МБ стоит процессора."""
    try:
        mt = os.stat(config.TABLE_PATH).st_mtime_ns
    except FileNotFoundError:
        mt = None
    key = (kind, gz, mt)
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    # Serialize cache misses only. Concurrent dashboard clients otherwise repeat
    # the same large JSON render and starve the small independent /status route.
    with _cache_build_lock:
        try:
            mt = os.stat(config.TABLE_PATH).st_mtime_ns
        except FileNotFoundError:
            mt = None
        key = (kind, gz, mt)
        with _cache_lock:
            hit = _cache.get(key)
        if hit is not None:
            return hit
        if kind == "data":
            # Collector publishes complete JSON by atomic replace. Serve those
            # exact bytes instead of parsing/encoding a large snapshot under GIL.
            try:
                with open(config.TABLE_PATH, "rb") as source:
                    body = source.read()
            except FileNotFoundError:
                body = json.dumps(load_table(), separators=(",", ":")).encode()
        else:
            body = dashboard.render(load_table()).encode()
        if gz:
            body = gzip.compress(body, compresslevel=5)
        with _cache_lock:
            for k in [k for k in _cache if k[2] != mt]:
                del _cache[k]
            _cache[key] = body
        return body


def load_table(path=None) -> dict:
    path = path or config.TABLE_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"ts": 0, "ff_rows": [], "sf_rows": [], "health": {}, "notes": {"web": "коллектор ещё не записал table.json"},
                "backfill": {}, "n_ff": 0, "n_sf": 0, "windows": list(config.WINDOWS_H)}


def accepts_gzip(header: str | None) -> bool:
    """Accept-Encoding разрешает gzip: токен gzip (или *) с q > 0 («gzip;q=0» — явный отказ)."""
    for part in (header or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() in ("gzip", "x-gzip", "*"):
            q = 1.0
            for p in params.split(";"):
                k, _, v = p.strip().partition("=")
                if k.strip().lower() == "q":
                    try:
                        q = float(v)
                    except ValueError:
                        q = 0.0
            return q > 0
    return False


def _is_cabinet(path: str) -> bool:
    return path == "/cabinet" or path.startswith("/cabinet/")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30                    # медленный или зависший клиент по публичной ссылке не держит поток вечно

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200, gz: bool | None = None, headers=()):
        # 12.09: со спотами Gate/KuCoin/Bitget таблица ~5 МБ, а страница тянет её каждые 10 с через туннель — сжатие
        # (JSON сжимается раз в 10); маленькие ответы (/status) и клиенты без gzip (curl проверки выката) — как есть.
        # gz=True — тело уже сжато (кеш _cached). headers — свои заголовки ответа (кабинет: cookie, CSP, Location…)
        if gz is None:
            gz = len(body) > 4096 and accepts_gzip(self.headers.get("Accept-Encoding"))
            if gz:
                body = gzip.compress(body, compresslevel=5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not any(k.lower() == "cache-control" for k, _ in headers):
            self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        self.wfile.write(body)

    def _cabinet(self, method: str, body: bytes = b""):
        # dict() оставил бы только последний из повторённых заголовков; Cookie по HTTP/2 может прийти несколькими строками
        # (прокси обязан склеить их через «; », но если нет — cookie сессии не терялась бы): все Cookie склеиваются
        hdrs = dict(self.headers.items())
        ck = self.headers.get_all("Cookie") or []
        if len(ck) > 1:
            hdrs = {k: v for k, v in hdrs.items() if k.lower() != "cookie"}
            hdrs["Cookie"] = "; ".join(ck)
        req = cabinet.Req(method, self.path, hdrs, self.client_address[0], body)
        r = cabinet.instance().handle(req)
        self._send(r.body, r.ctype, r.code, headers=r.headers)

    def _body(self) -> bytes | None:
        """Тело POST не больше cabinet.MAX_BODY; None — больше. До POST_DRAIN_MAX байт лишнее вычитывается и выбрасывается
        (иначе остаток тела прочёлся бы как следующий запрос keep-alive), больше или chunked — соединение закрывается."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > POST_DRAIN_MAX:
            self.close_connection = True
            return None
        body = self.rfile.read(n) if n else b""
        return body if n <= cabinet.MAX_BODY else None

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if _is_cabinet(path):
                self._cabinet("GET")
            elif path in ("/data.json", "/", "/index.html"):
                gz = accepts_gzip(self.headers.get("Accept-Encoding"))
                kind = "data" if path == "/data.json" else "page"
                self._send(_cached(kind, gz), "application/json" if kind == "data" else "text/html; charset=utf-8", gz=gz)
            elif path == "/status":
                from .market_snapshot import load_health
                st = load_health()
                self._send(json.dumps(st).encode(), "application/json")
            else:
                self._send(b"not found", "text/plain; charset=utf-8", 404)
        except Exception as e:  # noqa
            log.exception("request %s failed", path)
            self._send(f"error: {type(e).__name__}: {e}".encode(), "text/plain; charset=utf-8", 500)

    def do_POST(self):
        """POST — только вход и выход кабинета; остальное — 404 (дашборд только читает)."""
        path = self.path.split("?")[0]
        try:
            body = self._body()
            if not _is_cabinet(path):
                self._send(b"not found", "text/plain; charset=utf-8", 404)
            elif body is None:
                self._send(b"request too large", "text/plain; charset=utf-8", 413)
            else:
                self._cabinet("POST", body)
        except Exception:  # noqa — текст исключения наружу не отдаём: POST — это вход с паролем
            log.exception("POST %s failed", path)
            self._send(b"error", "text/plain; charset=utf-8", 500)


def serve(port: int = config.WEB_PORT, host: str = config.WEB_HOST):
    srv = ThreadingHTTPServer((host, port), Handler)
    cabinet.instance()              # окружение кабинета читается на старте: в журнале сразу видно, включён ли он
    log.info("дашборд на http://%s:%d (туннель с Мака)", host, port)
    srv.serve_forever()
