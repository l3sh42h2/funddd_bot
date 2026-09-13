"""Истина edgeX (V2): публичный REST https://edgex-prod-v2.edgex.exchange/api/v2/public. Свои запросы и свой разбор,
без клиента коллектора edgex.py (тот берёт ставку из WS ticker.all.1s — истина берёт её из другого источника, REST).

Замерено с Мака 13.09.2026 (≈00:40 UTC), поля сверены с edgex-1.gitbook.io (api-v2/public-api/funding-api):
- Конверт {code: "SUCCESS", data, msg, …}; другой code — ошибка. Неизвестный contractId отвечает SUCCESS + [].
- GET /meta/getMetaData → contractList (173: contractId 30000001…, contractName, baseCoinId, quoteCoinId, enableTrade /
  enableDisplay / enableOpenPosition, fundingRateIntervalMin «240» у всех, isStock, isFx) + coinList (coinId, coinName).
  Символ дашборда = contractName ровно как у биржи («BTCUSDC», «哈基米USDC»); база — coinName по baseCoinId (HAJIMI), а не
  contractName без USDC. Квота — USDC у всех. Торгуется = все три флага true; скрытые (enableDisplay false: ZRO, EUR, JPY
  13.09) — в markets с tradable=False.
- GET /contract-labels → группы витрины сайта. Продукт V2 = productCategory «PrepV2»: «Commodities V2» (XAU XAG CL BZ
  COPPER NATGAS XPD XPT) → commodity, «Pre-IPO V2» → preipo (сейчас пусто). Классы: isFx → fx; isStock → equity (акции И
  ETF — SPY, QQQ …); PAXG / XAUT — монеты; прочее — crypto. «Commodities TradeFi» (AppTradFi) не берётся: там и акции JPM,
  KO. Метки не ответили — класс не-акций неизвестен (None), а не «монета по умолчанию».
- Ставка — доля ЗА ИНТЕРВАЛ (4 ч), без перевода: predictedFundingRate = interestRate / частота (док.) = 0.0003 / 6 =
  0.00005 — 6 расчётов в сутки, т. е. за 4 ч; у ETH внутри клампа fundingRate ровно 0.00005. Документация про «каждые
  8 ч» устарела: fundingRateIntervalMin = 240.
- GET /funding/getLatestFundingRate?contractId=A,B,… (через запятую, все в одном вызове; порция BATCH) — поминутный
  снимок: forecastFundingRate («Forecast funding rate» [docs]) = прогноз ближайшего расчёта = fundingRate тикера сайта
  (/quote/getTicker BTC: −0.00005382 = forecast −0.00005382 в ту же минуту); fundingRate строки — последний
  зафиксированный (у расчёта fundingTime); predictedFundingRate — только процентная часть, не берётся. next_ms =
  fundingTime («время расчёта» [docs]) + интервал = nextFundingTime тикера (20:00Z + 4 ч = 00:00Z). Тикер отвечает на
  ОДИН contractId (через запятую → []) — поэтому пакет, а не тикер.
- GET /funding/getFundingRatePage?contractId&size≤100&filterSettlementFundingRate=true&filterBeginTimeInclusive&
  filterEndTimeExclusive[&offsetData] — только строки расчёта (isSettlement true, сетка 00/04/08/12/16/20 UTC), новые
  первыми, nextPageOffsetData «» = конец. Без filterSettlementFundingRate — поминутные строки (1440 в сутки).
- Лимиты в документации без чисел (429 / RATE_LIMIT_EXCEEDED); темп истории HIST_GAP_S.
"""
from __future__ import annotations
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, next_boundary_ms, now_ms, H_MS

REST = "https://edgex-prod-v2.edgex.exchange/api/v2/public"
OK = "SUCCESS"
QUOTE = "USDC"
BATCH = 100                     # contractId в одном getLatestFundingRate (173 в одном вызове проходили)
PAGE = 100                      # size ≤ 100 [docs]
MAX_PAGES = 40
DEFAULT_IV_H = 4
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})
FLAGS = ("enableTrade", "enableDisplay", "enableOpenPosition")


def iv_hours(minutes) -> int | None:
    m = to_int(minutes)
    return max(1, int(round(m / 60.0))) if m and m > 0 else None


class EdgexTruth(Truth):
    venue = "edgex"
    HIST_GAP_S = 0.4

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._ids: dict[str, str] | None = None     # символ → contractId
        self._iv: dict[str, int] = {}

    def _data(self, path: str, params: dict | None = None, gap: float | None = None):
        body = self.http.get(REST + path, params, gap=gap)
        if not isinstance(body, dict) or str(body.get("code")) != OK:
            code = body.get("code") if isinstance(body, dict) else "?"
            raise RuntimeError(f"{path}: code {code}: {str(body)[:200]}")
        return body.get("data")

    def _labels(self) -> dict[str, str] | None:
        """contractId → класс по группам витрины V2; None — метки не ответили."""
        try:
            data = self._data("/contract-labels")
        except Exception:  # noqa — без меток класс не-акций неизвестен (None), присутствие это переживёт
            return None
        out = {}
        for g in data or []:
            if not isinstance(g, dict) or str(g.get("productCategory") or "") != "PrepV2":
                continue
            name = str(g.get("name") or "").strip().lower()
            cls = "commodity" if name.startswith("commodities") else "preipo" if name.startswith("pre-ipo") else None
            for c in g.get("contracts") or [] if cls else []:
                if isinstance(c, dict) and c.get("contractId") is not None:
                    out[str(c["contractId"])] = cls
        return out

    @staticmethod
    def _cls(coin: str, c: dict, labels: dict | None) -> tuple[str | None, str | None]:
        cid = str(c.get("contractId"))
        if coin.upper() in GOLD_TOKENS:
            return "crypto", None
        if c.get("isFx") is True:
            return "fx", None
        if labels and labels.get(cid) == "preipo":
            return "preipo", "группа «Pre-IPO V2»"
        if c.get("isStock") is True:
            return "equity", None
        if labels is None:
            return None, "группы меток не ответили — класс неизвестен"
        if labels.get(cid) == "commodity":
            return "commodity", "группа «Commodities V2»"
        return "crypto", None

    def markets(self) -> dict[str, dict]:
        data = self._data("/meta/getMetaData")
        cl = (data or {}).get("contractList") if isinstance(data, dict) else None
        if not cl:
            raise RuntimeError("getMetaData без contractList")
        coins = {str(c.get("coinId")): str(c.get("coinName") or "") for c in data.get("coinList") or [] if isinstance(c, dict)}
        labels = self._labels()
        out, ids, ivs = {}, {}, {}
        for c in cl:
            if not isinstance(c, dict):
                continue
            cid, sym = str(c.get("contractId") or ""), str(c.get("contractName") or "")
            if not cid or not sym:
                continue
            coin = coins.get(str(c.get("baseCoinId"))) or sym
            quote = coins.get(str(c.get("quoteCoinId"))) or "?"
            off = [f for f in FLAGS if c.get(f) is not True]
            iv = iv_hours(c.get("fundingRateIntervalMin")) or DEFAULT_IV_H
            cls, why = self._cls(coin, c, labels)
            note = "; ".join(s for s in (f"квота {quote}", ("выключено: " + ", ".join(off)) if off else None, why) if s)
            out[sym] = market(coin, not off and quote == QUOTE, cls, iv, None, note)
            ids[sym], ivs[sym] = cid, iv
        self._ids, self._iv = ids, ivs
        return out

    def _ensure(self) -> dict[str, str]:
        if self._ids is None:
            self.markets()
        return self._ids or {}

    def rates(self) -> dict[str, dict]:
        ids = self._ensure()
        by_cid = {cid: sym for sym, cid in ids.items()}
        cids = list(by_cid)
        now = now_ms()
        out = {}
        for k in range(0, len(cids), BATCH):
            chunk = cids[k:k + BATCH]
            rows = [r for r in self._data("/funding/getLatestFundingRate", {"contractId": ",".join(chunk)}) or []
                    if isinstance(r, dict) and str(r.get("contractId")) in by_cid]
            if not rows:
                raise RuntimeError(f"getLatestFundingRate пуст для {len(chunk)} известных контрактов")
            for r in rows:
                sym = by_cid[str(r["contractId"])]
                v, kind = dec(r.get("forecastFundingRate")), "predicted"
                if v is None:
                    v, kind = dec(r.get("fundingRate")), "last"     # минута расчёта: прогноза нет, берём зафиксированный
                iv = iv_hours(r.get("fundingRateIntervalMin")) or self._iv.get(sym) or DEFAULT_IV_H
                ft = to_int(r.get("fundingTime"))
                nxt = ft + iv * H_MS if ft and ft > 0 else None
                if nxt is None or nxt <= now:
                    nxt = next_boundary_ms(now, iv)                 # снимок отстал от расчёта — ближайший по сетке
                out[sym] = rate(v, iv, nxt, kind)
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        cid = self._ensure().get(symbol)
        if cid is None:
            raise RuntimeError(f"нет контракта {symbol} в getMetaData")
        out: dict[int, Decimal] = {}
        token = ""
        for _ in range(MAX_PAGES):
            p = dict(contractId=cid, size=PAGE, filterSettlementFundingRate="true",
                     filterBeginTimeInclusive=int(start_ms), filterEndTimeExclusive=int(end_ms) + 1)
            if token:
                p["offsetData"] = token
            data = self._data("/funding/getFundingRatePage", p, gap=self.HIST_GAP_S)
            data = data if isinstance(data, dict) else {}
            rows = [r for r in data.get("dataList") or [] if isinstance(r, dict)]
            for r in rows:
                if r.get("isSettlement") not in (True, "true"):
                    continue                                        # поминутная строка — не расчёт
                ms, v = to_int(r.get("fundingTime")), dec(r.get("fundingRate"))
                if ms and v is not None and start_ms <= ms <= end_ms:
                    out[ms] = v
            token = str(data.get("nextPageOffsetData") or "")
            if not token or not rows:
                break
        else:
            raise RuntimeError(f"{symbol}: история не кончилась за {MAX_PAGES} страниц")
        return sorted(out.items())
