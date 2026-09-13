"""Истина Hyperliquid: info API по ВСЕМ dex — основной и HIP-3 (xyz, para, io, mkts, …). Сайт — JS-приложение поверх
этого же API и показывает все dex в одном списке. Свои запросы, без клиента коллектора hyperliquid.py.

Замерено с Мака 13.09.2026:
- perpDexs → [null, {name: xyz}, …]: 11 dex, у шести (flx, vntl, hyna, km, abcd, cash) все рынки isDelisted.
- metaAndAssetCtxs {dex} → [meta{universe}, ctxs] построчно; isDelisted → tradable=False. Символ — name ровно как у
  биржи и дашборда: «BTC», «kPEPE» (регистр важен: KPEPE ≠ kPEPE), «xyz:NATGAS».
- ctx.funding — ставка ТЕКУЩЕГО часа (доля за 1 ч, прогноз до расчёта на круглом часу). Сайт показывает её «за 8h» =
  час × 8 (site_hours=8, только для таблицы «как на сайте»). Расчёт каждый час ровно на часе (у HIP-3 тоже):
  next_ms = следующий круглый час — вычислен по правилу биржи, в ответе поля нет.
- perpCategories → [[«xyz:NVDA», «stocks»], …]: stocks/stock, indices, commodities, crypto, fx, preipo, rates.
  Основной dex — только монеты. HIP-3 без категории — класс неизвестен (None), а не «акция по умолчанию».
- fundingHistory {coin, startTime, endTime} — ≤ 500 строк за вызов, time с дрожанием +10…43 мс. Лимит IP 1200 веса в
  минуту общий с коллектором (контексты ~20, история ~20 + 1 на 20 строк) — темп истории 3 с.
"""
from __future__ import annotations
import time
from decimal import Decimal
from .base import Truth, Http, dec, market, rate, page_by_time, next_boundary_ms, now_ms

URL = "https://api.hyperliquid.xyz/info"
PAGE = 500
CATS = {"stocks": "equity", "stock": "equity", "indices": "index", "index": "index", "commodities": "commodity",
        "commodity": "commodity", "crypto": "crypto", "fx": "fx", "forex": "fx", "preipo": "preipo", "rates": "other"}


class HyperliquidTruth(Truth):
    venue = "hyperliquid"
    site_hours = 8
    quote_twins = False                # xyz:NET и para:NET — разные рынки с разным фандингом, не дубли
    HIST_GAP_S = 3.0
    SNAP_TTL_S = 5.0                   # markets() и сразу rates() — один снимок; перепроверка — уже новый

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._snap: tuple[float, list] | None = None

    def _snapshot(self) -> list[tuple[str, dict, dict]]:
        if self._snap and time.time() - self._snap[0] < self.SNAP_TTL_S:
            return self._snap[1]
        dexs = self.http.post(URL, {"type": "perpDexs"}) or []
        names = [""] + [d["name"] for d in dexs if d and d.get("name")]
        rows = []
        for dex in names:
            body = {"type": "metaAndAssetCtxs"}
            if dex:
                body["dex"] = dex
            meta, ctxs = self.http.post(URL, body, gap=0.3)
            uni = (meta or {}).get("universe") or []
            if len(uni) != len(ctxs or []):
                raise RuntimeError(f"dex {dex or 'main'}: universe {len(uni)} ≠ ctxs {len(ctxs or [])}")
            rows += [(dex, a, c) for a, c in zip(uni, ctxs)]
        if not any(not dex for dex, _a, _c in rows):
            raise RuntimeError("основной dex пуст")
        self._snap = (time.time(), rows)
        return rows

    def _categories(self) -> dict[str, str]:
        try:
            return {str(n): str(c).lower() for n, c in self.http.post(URL, {"type": "perpCategories"}) or []}
        except Exception:  # noqa — без категорий класс HIP-3 неизвестен (None), присутствие это переживёт
            return {}

    def markets(self) -> dict[str, dict]:
        cats = self._categories()
        out = {}
        for dex, a, _c in self._snapshot():
            name = a["name"]
            if dex:
                raw = cats.get(name)
                cls = CATS.get(raw) if raw else None
            else:
                raw, cls = None, "crypto"
            note = (f"HIP-3 dex {dex}" + (f", категория {raw}" if raw else ", категории нет") if dex else "основной dex") + \
                   (" / делистинг" if a.get("isDelisted") else "")
            out[name] = market(name.split(":", 1)[-1], not a.get("isDelisted"), cls, 1, None, note)
        return out

    def rates(self) -> dict[str, dict]:
        nxt = next_boundary_ms(now_ms(), 1)
        return {a["name"]: rate(dec(c.get("funding")), 1, nxt, "predicted")
                for _dex, a, c in self._snapshot() if not a.get("isDelisted")}

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        def fetch(cursor):
            rows = self.http.post(URL, {"type": "fundingHistory", "coin": symbol, "startTime": int(cursor),
                                        "endTime": int(end_ms)}, gap=self.HIST_GAP_S) or []
            return [(int(r["time"]), dec(r.get("fundingRate"))) for r in rows if r.get("time") is not None]
        return [(ms, r) for ms, r in page_by_time(fetch, start_ms, end_ms, PAGE) if r is not None]
