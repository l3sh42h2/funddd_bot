"""Истина Extended (Starknet): публичный REST https://api.starknet.extended.exchange/api/v1 (User-Agent обязателен —
пустой получает 403; Http его ставит). Свои запросы и свой разбор, без клиента коллектора extended.py.

Замерено с Мака 13.09.2026 (≈00:40 UTC), поля сверены с api.docs.extended.exchange и docs.extended.exchange
(extended-resources/trading/funding-payments):
- Конверт {status: "OK", data}; ошибка — HTTP 400 {status: "ERROR", error{code, message}}.
- GET /info/markets → все рынки одним массивом (399 13.09, ≈1 МБ): name («BTC-USD»), type (PERPETUAL / SPOT), status
  (ACTIVE / PRELISTED / REDUCE_ONLY / DELISTED), active, visibleOnUi, assetName, description, category / subCategory,
  referenceMarket (СТРОКА «null»), marketStats{fundingRate, nextFundingRate, markPrice …}. Символ дашборда = name ровно
  как у биржи. SPOT (3 строки) — не перп, в markets не идёт. Торгуется = ACTIVE + active + visibleOnUi; остальные — в
  markets с tradable=False (DELISTED 60, PRELISTED 12, REDUCE_ONLY 1).
- База = assetName без суффикса «_24_5» (двойник акции на 24/5-оракуле: NVDA_24_5-USD и NVDA-USD — один актив; при двух
  торгуемых коллектор берёт один — аудитор видит в них дубли, quote_twins). kNOT, 1000PEPE — как у биржи.
- Классы (свои, по полям биржи): category Crypto и старые L1 / L2 / Infra / DeFi / Meme / AI → crypto (PAXG / XAUT —
  Crypto/Commodity, монеты); RWA: Equity → equity; ETF/Index → equity при referenceMarket us_equity (доли фондов DRAM,
  SOXL, KORU, EWY), иначе index (SPX500m, TECH100m, JP225); Commodity → commodity; FX → fx; Pre-market → preipo; прочее
  (RWA/TradFi — делистингованные PLACE_JPY) → None.
- marketStats.fundingRate — доля ЗА 1 ЧАС, без перевода: документация — «(средняя премия + clamp(процент − премия,
  ±0.05 %)) / 8», «сайт показывает часовую ставку», «платежи каждый час»; база монет 0.000013 = 0.01 %/8 ч ÷ 8. Считается
  каждую минуту — прогноз текущего часа. marketStats.nextFundingRate — НЕ ставка, а время следующего расчёта в мс
  («Timestamp of the next funding update» [docs]) → next_ms.
- GET /info/{name}/funding?startTime&endTime (оба обязательны) → [{m, f, T}] новые первыми, не больше 1000 за вызов
  (limit / cursor из документации ответ не меняют — замер коллектора, здесь не полагаемся): f — «часовая ставка,
  применённая к платежу» [docs], T — момент расчёта, +0.8…1.0 с после часа (редко до +9 мин). funding_ms = T, опущенное
  к началу часа; окно запроса с запасом LATE_MS сверху. Старше 1000 часов — страница назад с endTime = min(T) − 1.
- Лимит 1000 запросов / мин на IP [docs]; темп истории HIST_GAP_S.
"""
from __future__ import annotations
import time
from decimal import Decimal
from urllib.parse import quote
from .base import Truth, Http, dec, to_int, market, rate, next_boundary_ms, now_ms, H_MS

REST = "https://api.starknet.extended.exchange/api/v1"
PAGE = 1000
MAX_PAGES = 12
LATE_MS = 600_000
TWIN = "_24_5"
CRYPTO_CATS = frozenset({"CRYPTO", "L1", "L2", "INFRA", "DEFI", "MEME", "AI"})


def _ref(m: dict) -> str | None:
    r = str(m.get("referenceMarket") or "").strip().lower()
    return None if r in ("", "null", "none") else r


def asset_class(m: dict) -> str | None:
    cat = str(m.get("category") or "").strip().upper()
    sub = str(m.get("subCategory") or "").strip().upper()
    if cat in CRYPTO_CATS:
        return "crypto"
    if cat == "RWA":
        if sub == "EQUITY":
            return "equity"
        if sub == "ETF/INDEX":
            return "equity" if _ref(m) == "us_equity" else "index"
        return {"COMMODITY": "commodity", "FX": "fx", "PRE-MARKET": "preipo"}.get(sub)
    return None


class ExtendedTruth(Truth):
    venue = "extended"
    HIST_GAP_S = 0.3
    SNAP_TTL_S = 5.0                    # markets() и сразу rates() — один ≈1 МБ снимок; перепроверка — уже новый

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._snap: tuple[float, list[dict]] | None = None

    def _data(self, path: str, params: dict | None = None, gap: float | None = None):
        body = self.http.get(REST + path, params, gap=gap)
        if not isinstance(body, dict) or body.get("status") != "OK" or "data" not in body:
            raise RuntimeError(f"{path}: {str(body)[:200]}")
        return body["data"]

    def _perps(self) -> list[dict]:
        if self._snap and time.time() - self._snap[0] < self.SNAP_TTL_S:
            return self._snap[1]
        data = self._data("/info/markets")
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"/info/markets: пусто или не список: {str(data)[:200]}")
        rows = [m for m in data if isinstance(m, dict) and m.get("name") and m.get("type") == "PERPETUAL"]
        self._snap = (time.time(), rows)
        return rows

    def markets(self) -> dict[str, dict]:
        out = {}
        for m in self._perps():
            name = str(m["name"])
            st = str(m.get("status") or "?")
            vis, act = m.get("visibleOnUi") is not False, m.get("active") is not False
            asset = str(m.get("assetName") or name.rsplit("-", 1)[0])
            twin = asset.endswith(TWIN)
            cls = asset_class(m)
            note = f"{m.get('category')}/{m.get('subCategory')}" + (f", {_ref(m)}" if _ref(m) else "") + \
                   (" / двойник 24/5" if twin else "") + ("" if st == "ACTIVE" else f" / статус {st}") + \
                   ("" if vis else " / скрыт в приложении") + ("" if act else " / active=false") + \
                   ("" if cls else " / класс не сопоставлен")
            out[name] = market(asset[:-len(TWIN)] if twin else asset, st == "ACTIVE" and vis and act, cls, 1,
                               str(m.get("description") or "").strip() or None, note)
        return out

    def rates(self) -> dict[str, dict]:
        now = now_ms()
        grid = next_boundary_ms(now, 1)
        out = {}
        for m in self._perps():
            ms = m.get("marketStats") or {}
            v = dec(ms.get("fundingRate"))
            if v is None:
                continue
            nxt = to_int(ms.get("nextFundingRate"))                  # время в мс, несмотря на имя
            if not nxt or not now < nxt <= now + 2 * H_MS:
                nxt = grid
            out[str(m["name"])] = rate(v, 1, nxt, "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        path = f"/info/{quote(symbol, safe='')}/funding"
        got: dict[int, tuple[int, Decimal]] = {}
        hi = int(end_ms) + LATE_MS
        for _ in range(MAX_PAGES):
            rows = self._data(path, {"startTime": int(start_ms), "endTime": hi}, gap=self.HIST_GAP_S)
            if not isinstance(rows, list):
                raise RuntimeError(f"{path}: не список: {str(rows)[:200]}")
            ts = []
            for r in rows:
                t = to_int(r.get("T")) if isinstance(r, dict) else None
                if t is None:
                    continue
                ts.append(t)
                v = dec(r.get("f"))
                if v is None or r.get("m") not in (None, symbol):
                    continue
                ms = t - t % H_MS
                if start_ms <= ms <= end_ms and (ms not in got or t < got[ms][0]):
                    got[ms] = (t, v)
            if len(rows) < PAGE or not ts:
                break
            lo = min(ts)
            if lo - lo % H_MS <= start_ms:
                break
            hi = lo - 1
        else:
            raise RuntimeError(f"{symbol}: история не дошла до {start_ms} за {MAX_PAGES} страниц")
        return [(ms, got[ms][1]) for ms in sorted(got)]
