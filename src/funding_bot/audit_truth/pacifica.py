"""Истина Pacifica (Solana): публичный REST https://api.pacifica.fi/api/v1. Свои запросы и свой разбор, без клиента
коллектора pacifica.py.

Замерено с Мака 13.09.2026 (≈00:40 UTC), поля сверены с docs.pacifica.fi (rest-api/markets/get-prices,
get-historical-funding, trading-on-pacifica/contract-specifications/market-specifications):
- Конверт {success, data, error, code}; success=false — ошибка. Неизвестный символ (и неверный регистр) — 200 с data [].
- GET /info → 77 строк: 76 instrument_type «perpetual» + спот SOL-USDC (не перп, в markets не идёт). symbol РЕГИСТРОЗАВИСИМ
  («kBONK») — символ дашборда ровно он; created_at (мс листинга): в будущем — объявлен, но не торгуется (tradable=False).
  Статуса, класса, полного имени нет.
- Классы — таблица документации «Market Specifications» (снимок 16.07): Crypto, Equities (+ ETF URNM → equity), Index,
  FX, Commodities. Листинги позже таблицы (MU, SNDK, DRAM, VVV, KAITO, kSHIB, PONS, USELESS …) — класс неизвестен (None,
  причина в note), без догадки: Pacifica листит премаркеты на СВОЕЙ марк-цене [docs оракула].
- GET /info/prices → все рынки одним вызовом: `funding` — «ставка, выплаченная в прошлую эпоху (час)», `next_funding` —
  «оценка ставки к выплате в следующую эпоху (час)» [docs]. Обе — доля ЗА 1 ЧАС (интервал 1 ч, расчёт на часе), без
  перевода; база монет 0.0000125 = 0.01 %/8 ч ÷ 8. Истина ставки = next_funding (прогноз ближайшего расчёта); next_ms —
  следующий круглый час (в ответе поля нет).
- GET /funding_rate/history?symbol&limit≤4000[&cursor] — новые первыми; start_time / end_time у точки нет [docs] → limit
  по числу часов до start (+HIST_SLACK), назад — cursor (next_cursor, has_more). ЛОВУШКА ИМЁН: строка расчёта часа H
  (created_at = H + 0.3…5 с) несёт funding_rate = «последняя рассчитанная» ДО этого расчёта и next_funding_rate = ставку,
  которая выплачена в H. Своя улика 13.09 23:4xZ: prices.funding («выплачена в прошлую эпоху» = в 23:00Z) = BTC 0.00000531
  = next_funding_rate строки 23:00Z (её funding_rate 0.00000165 = next_funding_rate строки 22:00Z); ETH так же. Поэтому
  история = (created_at, опущенное к часу; next_funding_rate).
- Лимит — кредиты: «ratelimit-policy: "credits";q=1000;w=60»; история ≈ 90 кредитов за вызов при любом limit, цены ≈ 10.
  Темп истории HIST_GAP_S = 8 с (≈ 675 кредитов / мин) и терпеливые повторы на 429 — окно общее с коллектором.
"""
from __future__ import annotations
import math
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, next_boundary_ms, now_ms, H_MS

REST = "https://api.pacifica.fi/api/v1"
HIST_LIMIT = 4000
HIST_SLACK = 3
MAX_PAGES = 5
DOCS_DATE = "16.07.2026"
# docs «Market Specifications» (таблица от DOCS_DATE); PAXG — токен золота = монета, BP — токен Backpack
CLASSES = {
    "crypto": {"BTC", "ETH", "BNB", "DOGE", "HYPE", "SOL", "XRP", "AAVE", "ADA", "ARB", "ASTER", "AVAX", "BCH", "CRV", "ENA",
               "FARTCOIN", "JUP", "LDO", "LINK", "LIT", "LTC", "NEAR", "PAXG", "PUMP", "SUI", "TAO", "TRUMP", "UNI", "XMR",
               "XPL", "ZEC", "kBONK", "kPEPE", "ICP", "PENGU", "STRK", "VIRTUAL", "WIF", "WLD", "WLFI", "ZK", "ZRO", "2Z",
               "CHIP", "MEGA", "MON", "PIPPIN", "BP"},
    "equity": {"CRCL", "GOOGL", "HOOD", "MSTR", "NVDA", "PLTR", "SAMSUNG", "SKHYNIX", "SPCX", "TSLA", "URNM"},
    "index": {"SP500"},
    "fx": {"EURUSD", "USDJPY"},
    "commodity": {"CL", "COPPER", "NATGAS", "PLATINUM", "XAG", "XAU"},
}


def asset_class(symbol: str) -> str | None:
    return next((c for c, s in CLASSES.items() if symbol in s), None)


class PacificaTruth(Truth):
    venue = "pacifica"
    HIST_GAP_S = 8.0

    def __init__(self, http: Http | None = None):
        super().__init__(http or Http(self.venue, self.gap_s, retries=6))
        self._perps: set[str] | None = None

    def _body(self, path: str, params: dict | None = None, gap: float | None = None) -> dict:
        body = self.http.get(REST + path, params, gap=gap)
        if not isinstance(body, dict) or body.get("success") is False or not isinstance(body.get("data"), list):
            raise RuntimeError(f"{path}: {str(body)[:200]}")
        return body

    def markets(self) -> dict[str, dict]:
        now = now_ms()
        out = {}
        for m in self._body("/info")["data"]:
            sym = str((m or {}).get("symbol") or "") if isinstance(m, dict) else ""
            if not sym or str(m.get("instrument_type") or "").lower() != "perpetual":
                continue                                   # спот SOL-USDC
            created = to_int(m.get("created_at")) or 0
            cls = asset_class(sym)
            note = ("" if cls else f"класса нет в таблице документации ({DOCS_DATE})") + \
                   (f" / листинг объявлен, торги с {created}" if created > now else "")
            out[sym] = market(str(m.get("base_asset") or sym), created <= now, cls, 1, None, note.strip(" /") or None)
        if not out:
            raise RuntimeError("/info без перпов")
        self._perps = set(out)
        return out

    def rates(self) -> dict[str, dict]:
        nxt = next_boundary_ms(now_ms(), 1)
        out = {}
        for r in self._body("/info/prices")["data"]:
            sym = str((r or {}).get("symbol") or "") if isinstance(r, dict) else ""
            if not sym or (sym not in self._perps if self._perps is not None else "-" in sym):
                continue
            v = dec(r.get("next_funding"))
            if v is not None:
                out[sym] = rate(v, 1, nxt, "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        limit = max(1, min(HIST_LIMIT, math.ceil((now_ms() - int(start_ms)) / H_MS) + HIST_SLACK))
        out: dict[int, Decimal] = {}
        cursor, oldest, n = None, None, 0
        for _ in range(MAX_PAGES):
            params = {"symbol": symbol, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            body = self._body("/funding_rate/history", params, gap=self.HIST_GAP_S)
            rows = [r for r in body["data"] if isinstance(r, dict)]
            n += len(rows)
            for r in rows:
                ca = to_int(r.get("created_at"))
                if ca is None:
                    continue
                oldest = ca if oldest is None else min(oldest, ca)
                ms, v = ca - ca % H_MS, dec(r.get("next_funding_rate"))   # выплачено в H — см. ловушку в шапке
                if v is not None and start_ms <= ms <= end_ms:
                    out.setdefault(ms, v)
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not rows or (oldest is not None and oldest <= start_ms):
                break
            if not cursor:
                raise RuntimeError(f"{symbol}: has_more без next_cursor")
        else:
            raise RuntimeError(f"{symbol}: история не дошла до {start_ms} за {MAX_PAGES} страниц")
        if not n:
            raise RuntimeError(f"{symbol}: история пуста — биржа отвечает [] и на неизвестный символ / чужой регистр")
        return sorted(out.items())
