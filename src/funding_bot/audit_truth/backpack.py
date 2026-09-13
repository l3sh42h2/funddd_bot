"""Истина Backpack Exchange (перпы). Свои запросы и свой разбор, без клиента коллектора backpack.py.

Замерено с Мака 12-13.09.2026 (REST https://api.backpack.exchange/api/v1, за CloudFront; числа — строки):
- GET /markets?marketType=PERP → 102 рынка: symbol «BTC_USDC_PERP» — ровно ключ дашборда; baseSymbol «BTC», «kBONK»
  (цена за 1000 BONK), «NVDA.US» (акция на бирже США); orderBookState Open | PostOnly | Closed; visible; createdAt (ISO
  без зоны = UTC, микросекунды); fundingInterval — МИЛЛИСЕКУНДЫ (3600000 у всех = 1 ч); rwaMarketType null | STOCK |
  INDEX. Торгуется = Open, visible не false, createdAt не в будущем. Closed (11: делистинг) и PostOnly (AMZN.US, AMD.US:
  visible=false, только мейкер) — в markets с tradable=False и причиной.
- Класс: rwaMarketType null → монета; STOCK → акция (OPENAI / ANTHROPIC — частные компании → preipo); INDEX на тикере
  «.US» → акция: это паи фондов на бирже США (QQQ.US «Invesco QQQ Trust», SPY.US «SPDR S&P 500 ETF Trust», DRAM.US
  «Roundhill Memory ETF» — имена из GET /assets), INDEX без «.US» → индекс; другой тип → other.
  База: baseSymbol, у акций без суффикса биржи «.US» (NVDA.US → NVDA); kBONK — как есть (множитель снимает norm_base).
- ТЕКУЩАЯ ставка — GET /markPrices (все неторгуемые-не-закрытые перпы одним вызовом): fundingRate — «The current funding
  rate» (docs.backpack.exchange), ДОЛЯ ЗА ИНТЕРВАЛ (= за 1 ч): совпадает со строкой «в процессе» истории (BTC 0.0000125
  и 0.0000125 в 23:42 за интервал до 00:00), база 0.0000125/ч = 0.03 % в сутки; потолки fundingRateUpperBound «150» —
  базисные пункты (история упирается ровно в ±0.015). Прогноз: платится в nextFundingTimestamp (мс). Перевода нет.
- GET /fundingRates {symbol, limit ≤ 10000, offset}: фильтра по времени НЕТ, САМЫЕ НОВЫЕ ПЕРВЫМИ, строки через 1 ч
  {symbol, intervalEndTimestamp (ISO без зоны = UTC), fundingRate}. ЛОВУШКА 1: первая строка — интервал В ПРОЦЕССЕ
  (время в будущем, бегущее среднее), и расчёт за только что прошедший час окончательным становится не сразу: замер
  13.09 00:00 — строка kBONK за 00:00 −0.000053741 за 40 с до часа, −0.000054708 уже с +20 с и без изменений до +400 с
  (в шапке клиента коллектора 12.09 22:00 — 1-3 мин) — строки новее now − FINAL_LAG_MS (5 мин) не отдаются. Ровно в
  hh:00 /markPrices переходит на следующий интервал, и его бегущее среднее в первые минуты часа скачет (BTC 0.0000119 на
  +20 с → 0.0000125 на +80 с) — расхождение ставки в начале часа перепроверяет аудитор. fundingRate там — до 28 знаков. ЛОВУШКА 2: неизвестный символ отвечает 200 [] — пустой ответ по
  рынку, который уже должен был рассчитаться, — ошибка, а не «истории нет». CDN держит копию точной строки запроса 60 с.
- Лимитов в документации нет, 429 не видели: темп истории 0.5 с.
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, now_ms, H_MS

REST = "https://api.backpack.exchange/api/v1"
PAGE_MAX = 1000                  # строк за вызов (биржа позволяет до 10000); 7 суток = 170 строк
PAGE_SLACK = 3                   # строка «в процессе» + запас сверх часов окна
HISTORY_MAX_PAGES = 40
FINAL_LAG_MS = 5 * 60_000        # расчёт за hh:00 окончательный через 1-3 мин (замер) — берём с запасом
FRESH_MS = 2 * H_MS              # рынок моложе — пустая история законна
PREIPO = {"OPENAI", "ANTHROPIC"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def iso_ms(x) -> int | None:
    """«2026-09-12T22:00:00» / «2025-01-21T06:34:54.691858» (без зоны = UTC) → мс эпохи, точно."""
    try:
        d = datetime.fromisoformat(str(x).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - _EPOCH) // timedelta(milliseconds=1)


def ticker(base_symbol: str) -> str:
    b = str(base_symbol or "")
    return b[:-3] if b.upper().endswith(".US") and len(b) > 3 else b


def asset_class(base_symbol: str, rwa) -> tuple[str, str]:
    """(класс, откуда он) по rwaMarketType биржи."""
    if rwa is None or str(rwa).strip() == "":
        return "crypto", "rwaMarketType пуст — монета"
    t = str(rwa).strip().upper()
    listed = str(base_symbol or "").upper().endswith(".US")
    if t == "STOCK":
        if ticker(base_symbol).upper() in PREIPO:
            return "preipo", "STOCK частной компании"
        return "equity", "STOCK"
    if t == "INDEX":
        return ("equity", "INDEX на тикере .US — пай фонда (ETF)") if listed else ("index", "INDEX")
    return "other", f"неизвестный rwaMarketType {rwa!r}"


class BackpackTruth(Truth):
    venue = "backpack"
    gap_s = 0.2
    HIST_GAP_S = 0.5

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._iv: dict[str, int] = {}
        self._created: dict[str, int] = {}

    def markets(self) -> dict[str, dict]:
        rows = self.http.get(REST + "/markets", {"marketType": "PERP"})
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"/markets: пустой ответ {str(rows)[:200]}")
        now = now_ms()
        out = {}
        for m in rows:
            if not isinstance(m, dict) or m.get("marketType") != "PERP" or not m.get("symbol"):
                continue
            sym, bs = str(m["symbol"]), str(m.get("baseSymbol") or "")
            cls, src = asset_class(bs, m.get("rwaMarketType"))
            ms = to_int(m.get("fundingInterval"))
            iv = ms // H_MS if ms and ms > 0 and ms % H_MS == 0 else None
            created = iso_ms(m.get("createdAt")) or 0
            why = []
            if m.get("orderBookState") != "Open":
                why.append(f"orderBookState {m.get('orderBookState')}")
            if m.get("visible") is False:
                why.append("скрыт (visible=false)")
            if created > now:
                why.append("до запуска (createdAt в будущем)")
            if iv is None:
                why.append(f"fundingInterval {m.get('fundingInterval')!r} — не целые часы")
            note = f"{bs}/{m.get('quoteSymbol')}, {src}" + ("; " + "; ".join(why) if why else "")
            base = ticker(bs) if cls != "crypto" else bs
            out[sym] = market(base or sym, not why, cls, iv, None, note)
            if iv:
                self._iv[sym] = iv
            self._created[sym] = created
        return out

    def rates(self) -> dict[str, dict]:
        rows = self.http.get(REST + "/markPrices")
        if not isinstance(rows, list):
            raise RuntimeError(f"/markPrices: не список {str(rows)[:200]}")
        if not self._iv:
            self.markets()                        # интервал рынка — из /markets
        out = {}
        for x in rows:
            if not isinstance(x, dict) or not x.get("symbol"):
                continue
            v = dec(x.get("fundingRate"))
            iv = self._iv.get(str(x["symbol"]))
            if v is None or not iv:
                continue
            out[str(x["symbol"])] = rate(v, iv, to_int(x.get("nextFundingTimestamp")) or None, "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        now = now_ms()
        start_ms = int(start_ms)
        hi = min(int(end_ms), now - FINAL_LAG_MS)
        size = max(10, min(PAGE_MAX, int(math.ceil(max(0, now - start_ms) / H_MS)) + PAGE_SLACK))
        got: dict[int, Decimal] = {}
        oldest, n_rows = None, 0
        for page in range(HISTORY_MAX_PAGES):
            rows = self.http.get(REST + "/fundingRates", {"symbol": symbol, "limit": size, "offset": page * size},
                                 gap=self.HIST_GAP_S)
            if not isinstance(rows, list):
                raise RuntimeError(f"/fundingRates {symbol}: не список {str(rows)[:200]}")
            n_rows += len(rows)
            for r in rows:
                if not isinstance(r, dict) or r.get("symbol") not in (None, symbol):
                    continue
                ms = iso_ms(r.get("intervalEndTimestamp"))
                if ms is None:
                    continue
                oldest = ms if oldest is None else min(oldest, ms)
                v = dec(r.get("fundingRate"))
                if v is not None and start_ms <= ms <= hi:
                    got.setdefault(ms, v)             # стык страниц (расчёт между вызовами) — дубль, не дыра
            if len(rows) < size or (oldest is not None and oldest <= start_ms):
                break
        else:
            raise RuntimeError(f"/fundingRates {symbol}: за {HISTORY_MAX_PAGES} страниц по {size} не дошли до начала окна")
        if not n_rows:
            created = self._created.get(symbol)
            if created is None or now - created > FRESH_MS:
                raise RuntimeError(f"/fundingRates {symbol}: пустой ответ (неизвестный символ тоже отвечает 200 [])")
        return sorted(got.items())
