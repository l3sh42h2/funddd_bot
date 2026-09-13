"""Общая истина Binance-подобного fapi (Binance USDⓈ-M и Aster — клон его API: те же пути и поля).

Замерено с Мака 13.09.2026 (свои запросы, без клиента коллектора exchanges.py):
- /fapi/v1/exchangeInfo — список рынков сайта. Перп = contractType PERPETUAL / TRADIFI_PERPETUAL / «» (у Aster пустой
  тип бывает у рынков до запуска); CURRENT_QUARTER / NEXT_QUARTER — поставочные без фандинга, в список не идут.
  Торгуется = status TRADING; SETTLING (делистинг: Binance 130, Aster 15) и PENDING_TRADING (до запуска) — в markets с
  tradable=False. Квоты у Binance USDT/USDC/USD1/U/BTC (BTC-квота — ETHBTC), у Aster USDT/USD1/U.
- /fapi/v1/premiumIndex — lastFundingRate: ставка БЛИЖАЙШЕГО расчёта (сайт показывает её как «Funding Rate» с
  обратным отсчётом до nextFundingTime), доля за интервал. В ответе есть символы, которых нет в торгах (Binance 138
  делистингованных, Aster 150 символов вне exchangeInfo) — ставки берутся только для рынков exchangeInfo.
- /fapi/v1/fundingInfo — fundingIntervalHours; символа нет в ответе — 8 ч (умолчание обеих бирж по документации).
- /fapi/v1/fundingRate {symbol, startTime, endTime, limit ≤ 1000} — рассчитанные ставки по возрастанию времени;
  fundingTime с дрожанием в миллисекунды (1789171200001). Binance: 500 вызовов / 5 мин на IP — общий с коллектором.
"""
from __future__ import annotations
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, page_by_time

PERP_TYPES = ("PERPETUAL", "TRADIFI_PERPETUAL", "")


class FapiTruth(Truth):
    venue = ""                          # у подкласса — binance / aster
    BASE = ""
    HIST_GAP_S = 0.7
    history_note = None

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._perps: dict[str, dict] | None = None

    # класс актива — у подкласса (поля бирж разные)
    def asset_class(self, s: dict) -> str | None:
        raise NotImplementedError

    def _intervals(self) -> dict[str, int]:
        out = {}
        for r in self.http.get(self.BASE + "/fapi/v1/fundingInfo") or []:
            iv = to_int(r.get("fundingIntervalHours"))
            if r.get("symbol") and iv:
                out[r["symbol"]] = iv
        return out

    def markets(self) -> dict[str, dict]:
        info = self.http.get(self.BASE + "/fapi/v1/exchangeInfo")
        syms = (info or {}).get("symbols") or []
        if not syms:
            raise RuntimeError("exchangeInfo: пустой список рынков")
        iv = self._intervals()
        out = {}
        for s in syms:
            ct = s.get("contractType") or ""
            if ct not in PERP_TYPES:
                continue
            st = s.get("status") or "?"
            sym = s["symbol"]
            note = f"{ct or 'тип пуст'} / {s.get('quoteAsset')}" + ("" if st == "TRADING" else f" / статус {st}")
            out[sym] = market(s.get("baseAsset") or sym, st == "TRADING", self.asset_class(s), iv.get(sym, 8),
                              s.get("name") or None, note)
        self._perps = out
        return out

    def rates(self) -> dict[str, dict]:
        prem = self.http.get(self.BASE + "/fapi/v1/premiumIndex") or []
        iv = self._intervals()
        keep = self._perps
        out = {}
        for p in prem:
            sym = p.get("symbol")
            if not sym or (keep is not None and sym not in keep):
                continue
            nxt = to_int(p.get("nextFundingTime"))
            out[sym] = rate(dec(p.get("lastFundingRate")), iv.get(sym, 8), nxt or None, "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        def fetch(cursor):
            rows = self.http.get(self.BASE + "/fapi/v1/fundingRate",
                                 {"symbol": symbol, "startTime": int(cursor), "endTime": int(end_ms), "limit": 1000},
                                 gap=self.HIST_GAP_S) or []
            return [(int(r["fundingTime"]), dec(r.get("fundingRate"))) for r in rows if r.get("fundingTime") is not None]
        return [(ms, r) for ms, r in page_by_time(fetch, start_ms, end_ms, 1000) if r is not None]
