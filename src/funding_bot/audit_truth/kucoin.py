"""Истина KuCoin Futures (api-futures.kucoin.com). Свои запросы и свой разбор, без клиента коллектора kucoin_fut.py.

Замерено с Мака 13.09.2026:
- GET /api/v1/contracts/active → {"code": "200000", "data": [...]}: все 687 контрактов одним ответом (1.4 МБ). Охват
  тестировщика — линейные бессрочные: type FFWCSX и isInverse false — USDT 677 и USDC 5 (ETHUSDCM, SOLUSDCM, SUIUSDCM,
  XBTUSDCM, XRPUSDCM: дубли по квоте, аудитор их так и считает). Вне охвата, как COIN-M у Binance: обратные
  (коин-маржинальные) XBTUSDM / ETHUSDM / SOLUSDM / XRPUSDM и поставочный XBTMU26 (type FFICSX, фандинга нет).
  Торгуется = status Open. Точка отдаёт только активные: снятый рынок пропадает из списка, и нога дашборда на нём станет
  «рынка у биржи нет».
- Символ — поле symbol ровно как у биржи и дашборда: «XBTUSDTM», «1000BONKUSDTM», «NIULAIUSDTM».
- База — как сайт называет монету: displayBaseCurrency (у китайских тикеров baseCurrency — пиньинь: NIULAI → 牛来,
  LONGXIA → 龙虾, HAJIMI → 哈基米, WOTAMALAILE → 我踏马来了), и XBT → BTC (исторический код биткоина у KuCoin; других
  переименований нет). 1000BONK, 10000CAT, 1MBABYDOGE — как есть: единицу из имени читает аудитор (norm_base).
- Ставка — fundingFeeRate: доля ЗА ИНТЕРВАЛ, живая оценка ближайшего расчёта (равна value из
  /api/v1/funding-rate/{symbol}/current — сверено на XBTUSDTM −0.000023, ETHUSDTM 0.000071, TRUSTUSDTM 0.00005).
  lastTimeFundingRate — прошлый расчёт, не берётся; predictedFundingFeeRate null у всех.
- Интервал — currentFundingRateGranularity (мс); у 14 старых контрактов null → fundingRateGranularity. 4 ч ×432, 8 ч ×240.
- Следующий расчёт — nextFundingRateDateTime (мс, абсолютное время). nextFundingRateTime — мс, ОСТАВШИЕСЯ до расчёта
  (1121581), не берётся. Сетка не обязательно от полуночи: TRUSTUSDTM — 4 ч со сдвигом 3 ч, поэтому время — от биржи.
- Класс — поля биржи: marketStage PRE_MARKET → preipo (ANTHROPIC, OPENAI, BP); assetClass CRYPTO → crypto, STOCK →
  equity, METAL / COMMODITY → commodity (XAG, XPT, XPD, COPPER, CL, BZ, NATGAS); PAXG / XAUT KuCoin числит METAL, но это
  токены золота — монеты (правило репозитория, как у Aster). Незнакомый assetClass — None (класс неизвестен).
- История — GET /api/v1/contract/funding-rates {symbol, from, to}: обе границы обязательны (иначе HTTP 400 / 400000);
  отдаёт НОВЕЙШИЕ ≤ 100 расчётов окна [from, to] по убыванию времени — страницы идут назад (to = самый старый − 1).
  timepoint — мс расчёта (ровно на сетке), fundingRate — доля за интервал. Незнакомый символ — HTTP 200 с code 404000.
- Лимит — 2000 веса / 30 с на IP, общий с живым коллектором и спотом KuCoin (заголовки gw-ratelimit-*). Список
  контрактов — вес 3, история — вес 5: темп истории 0.3 с. Перегрузка — code 429000 в теле: пауза и повтор.
"""
from __future__ import annotations
import time
from decimal import Decimal
from .base import Truth, Http, H_MS, dec, to_int, market, rate

URL = "https://api-futures.kucoin.com"
ACTIVE = "/api/v1/contracts/active"
HISTORY = "/api/v1/contract/funding-rates"
PERP = "FFWCSX"                     # FFICSX — поставочный
PAGE = 100                          # строк в ответе истории — новейшие в окне
MAX_PAGES = 60                      # 60 × 100 расчётов ≥ 250 дней при 1 ч; дальше — ошибка, а не молча обрезанная история
GOLD = frozenset({"PAXG", "XAUT"})
RENAME = {"XBT": "BTC"}
CLASSES = {"CRYPTO": "crypto", "STOCK": "equity", "METAL": "commodity", "COMMODITY": "commodity",
           "FOREX": "fx", "FX": "fx", "INDEX": "index"}


class KucoinTruth(Truth):
    venue = "kucoin"
    HIST_GAP_S = 0.3
    SNAP_TTL_S = 5.0                   # markets() и сразу rates() — один снимок; перепроверка — уже новый
    THROTTLE_S = 5.0                   # пауза на code 429000 (перегрузка шлюза без HTTP 429)

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._snap: tuple[float, list[dict]] | None = None

    def _data(self, path: str, params: dict | None = None, gap: float | None = None):
        """Тело KuCoin {code, data}: 200000 — данные; 429000 — пауза и повтор; прочее — ошибка с кодом."""
        for i in range(3):
            body = self.http.get(URL + path, params, gap=gap)
            code = str(body.get("code")) if isinstance(body, dict) else "?"
            if code == "200000":
                return body.get("data")
            if code == "429000" and i < 2:
                time.sleep(self.THROTTLE_S * (i + 1))
                continue
            raise RuntimeError(f"kucoin {path}: code {code}: {str(body)[:200]}")

    def _contracts(self) -> list[dict]:
        if self._snap and time.time() - self._snap[0] < self.SNAP_TTL_S:
            return self._snap[1]
        data = self._data(ACTIVE)
        rows = [c for c in data if isinstance(c, dict) and c.get("symbol")] if isinstance(data, list) else []
        if not rows:
            raise RuntimeError(f"kucoin {ACTIVE}: пустой список контрактов: {str(data)[:200]}")
        self._snap = (time.time(), rows)
        return rows

    # --- разбор контракта ----------------------------------------------------------------------------------------
    @staticmethod
    def in_scope(c: dict) -> bool:
        """Линейный бессрочный (USDT / USDC). Обратные и поставочные — вне охвата."""
        return c.get("type") == PERP and not c.get("isInverse")

    @staticmethod
    def interval_h(c: dict) -> int | None:
        for k in ("currentFundingRateGranularity", "fundingRateGranularity"):
            v = to_int(c.get(k))
            if v and v > 0:
                return max(1, round(v / H_MS))
        return None

    @staticmethod
    def base_of(c: dict) -> str:
        b = str(c.get("displayBaseCurrency") or c.get("baseCurrency") or c.get("symbol"))
        return RENAME.get(b.upper(), b)

    @staticmethod
    def asset_class(c: dict) -> str | None:
        if str(c.get("marketStage") or "").upper() == "PRE_MARKET":
            return "preipo"
        if str(c.get("baseCurrency") or "").upper() in GOLD:
            return "crypto"
        ac = str(c.get("assetClass") or "").upper()
        if ac:
            return CLASSES.get(ac)
        mt = str(c.get("marketType") or "").upper()          # старые контракты без assetClass
        return {"CRYPTO": "crypto", "NASDAQ": "equity"}.get(mt)

    # --- интерфейс -------------------------------------------------------------------------------------------------
    def markets(self) -> dict[str, dict]:
        out = {}
        for c in self._contracts():
            if not self.in_scope(c):
                continue
            sym, st = str(c["symbol"]), str(c.get("status") or "?")
            b = self.base_of(c)
            note = f"{c.get('quoteCurrency')}-маржа" + ("" if st == "Open" else f" / статус {st}")
            stage = str(c.get("marketStage") or "")
            if stage and stage != "NORMAL":
                note += f" / {stage}"
            if b != c.get("baseCurrency"):
                note += f" / baseCurrency {c.get('baseCurrency')}"
            if c.get("assetClass"):
                note += f" / {c['assetClass']}"
            out[sym] = market(b, st == "Open", self.asset_class(c), self.interval_h(c), c.get("displaySymbol") or None, note)
        if not out:
            raise RuntimeError("kucoin: ни одного линейного бессрочного контракта")
        return out

    def rates(self) -> dict[str, dict]:
        out = {}
        for c in self._contracts():
            iv = self.interval_h(c)
            if not self.in_scope(c) or not iv:
                continue
            nxt = to_int(c.get("nextFundingRateDateTime"))
            out[str(c["symbol"])] = rate(dec(c.get("fundingFeeRate")), iv, nxt if nxt and nxt > 0 else None, "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        start_ms, end_ms = int(start_ms), int(end_ms)
        got: dict[int, Decimal] = {}
        hi = end_ms
        for _ in range(MAX_PAGES):
            if hi < start_ms:
                break
            rows = self._data(HISTORY, {"symbol": symbol, "from": start_ms, "to": hi}, gap=self.HIST_GAP_S)
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise RuntimeError(f"kucoin {symbol}: история не списком: {str(rows)[:200]}")
            stamps = []
            for r in rows:
                ms = to_int((r or {}).get("timepoint"))
                if not ms or ms <= 0:
                    continue
                stamps.append(ms)
                v = dec(r.get("fundingRate"))
                if v is not None and start_ms <= ms <= end_ms:
                    got[ms] = v
            if len(rows) < PAGE or not stamps:
                break
            hi = min(stamps) - 1
        else:
            raise RuntimeError(f"kucoin {symbol}: история не дошла до {start_ms} за {MAX_PAGES} страниц")
        return sorted(got.items())
