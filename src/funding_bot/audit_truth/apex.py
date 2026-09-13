"""Истина ApeX Omni: публичный REST https://omni.apex.exchange/api/v3. Свои запросы и свой разбор, без клиента
коллектора apex.py (тот берёт ставку из WS instrumentInfo.all — истина берёт её из REST-тикера по символу).

Замерено с Мака 13.09.2026 (≈00:40 UTC), поля сверены с api-docs.pro.apex.exchange (Omni v3):
- Успех {data, timeCost}; ошибка — HTTP 200 {code, msg} (code 3 — «invalid symbol / page size …»). Неизвестный путь —
  404.
- GET /symbols → data.contractConfig: perpetualContract (138), stockContract (47), predictionContract (184 событийных —
  не перпы, не берутся), prelaunchContract (0; будут — в markets с tradable=False). Строка: crossSymbolName «BTCUSDT» —
  символ дашборда и /ticker; symbol «BTC-USDT» — единственная форма, которую принимает /history-funding; baseTokenId —
  база (1000PEPE); tokenName — полное имя; enableTrade / enableDisplay / enableOpenPosition; settleAssetId (USDT у всех);
  isPrelaunch; category. Торгуется = три флага + USDT + не prelaunch; выключенные (50 + 8) и скрытые (IO, STBL) — в
  markets с tradable=False.
- Классы (свои, по группе и category биржи): perpetualContract → crypto; stockContract: STOCK → equity; COMMODITY →
  commodity, но фонд в имени (USO «United States Oil Fund») → equity; INDEX или без category → equity, если это доля фонда
  (ETF / Fund / Trust в имени: SPY, QQQ, EWY, DRAM, SOXL), иначе index / None; OPENAI / ANTHROPIC → preipo; PAXG / XAUT →
  монеты.
- GET /ticker?symbol=BTCUSDT → [{fundingRate, predictedFundingRate, nextFundingTime (ISO «…T00:00:00Z»), …}] — ОДИН
  символ (без символа, через запятую, «BTC-USDT» → []), пакета нет. fundingRate — «текущая часовая ставка» [docs]: доля ЗА
  1 ЧАС = за интервал (расчёт каждый час [docs]), без перевода; оценка текущего часа, меняется в течение часа.
  predictedFundingRate — константа 0.0000125 у всех (процентная часть 0.0003 / 24) — не берётся. Плюс — лонги платят.
  125 символов по одному: темп gap_s (≤ 400 / мин из 600 / мин на IP [docs]).
- GET /history-funding?symbol=BTC-USDT&limit≤100&beginTimeInclusive&endTimeExclusive → data.historyFunds [{rate (доля за
  1 ч), price (индекс), fundingTime (мс, на часе)}] новые первыми; totalSize — не счётчик. Назад — окном:
  endTimeExclusive := самое старое fundingTime страницы (исключающая граница — без дублей).
"""
from __future__ import annotations
import re
from datetime import datetime
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, next_boundary_ms, now_ms

REST = "https://omni.apex.exchange/api/v3"
GROUPS = ("perpetualContract", "stockContract", "prelaunchContract")
FLAGS = ("enableTrade", "enableDisplay", "enableOpenPosition")
QUOTE = "USDT"
PAGE = 100                      # limit ≤ 100; 101 → code 3
MAX_PAGES = 80
FAIL_SHARE = 0.10               # тикеров не ответило больше этой доли — ставок у прогона нет (ошибка), а не «дыры»
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})
PREIPO = frozenset({"OPENAI", "ANTHROPIC"})
_FUND = re.compile(r"\b(?:ETF|FUND|TRUST)\b", re.I)


def asset_class(group: str, token: str, category, name) -> str | None:
    t = str(token or "").upper()
    if group == "perpetualContract" or t in GOLD_TOKENS:
        return "crypto"
    if t in PREIPO:
        return "preipo"
    c, fund = str(category or "").upper(), bool(_FUND.search(str(name or "")))
    if c == "STOCK" or fund:
        return "equity"
    return {"COMMODITY": "commodity", "INDEX": "index"}.get(c)


def iso_ms(s) -> int | None:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


class ApexTruth(Truth):
    venue = "apex"
    gap_s = 0.15
    HIST_GAP_S = 0.3

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._dash: dict[str, str] | None = None        # crossSymbolName → «BTC-USDT» (для истории)
        self._trad: list[str] = []
        self.rate_errors: dict[str, str] = {}

    def _data(self, path: str, params: dict | None = None, gap: float | None = None):
        body = self.http.get(REST + path, params, gap=gap)
        if not isinstance(body, dict):
            raise RuntimeError(f"{path}: {str(body)[:200]}")
        if body.get("code") not in (None, 0, "0"):
            raise RuntimeError(f"{path} code {body.get('code')}: {str(body.get('msg'))[:200]}")
        if "data" not in body:
            raise RuntimeError(f"{path}: без data: {str(body)[:200]}")
        return body["data"]

    def markets(self) -> dict[str, dict]:
        data = self._data("/symbols")
        cc = (data or {}).get("contractConfig") if isinstance(data, dict) else None
        if not isinstance(cc, dict):
            raise RuntimeError("/symbols без contractConfig")
        out, dash = {}, {}
        for group in GROUPS:
            for r in cc.get(group) or []:
                if not isinstance(r, dict) or not r.get("crossSymbolName"):
                    continue
                sym, tok = str(r["crossSymbolName"]), str(r.get("baseTokenId") or r["crossSymbolName"])
                off = [f for f in FLAGS if r.get(f) is not True]
                settle = str(r.get("settleAssetId") or "?")
                pre = r.get("isPrelaunch") is True or group == "prelaunchContract"
                trad = not off and not pre and settle == QUOTE
                cls = asset_class(group, tok, r.get("category"), r.get("tokenName"))
                note = "; ".join(s for s in (f"{group}, category {r.get('category')}", f"расчёт {settle}",
                                             ("выключено: " + ", ".join(off)) if off else None,
                                             "prelaunch" if pre else None,
                                             None if cls else "класс не сопоставлен") if s)
                out[sym] = market(tok, trad, cls, 1, str(r.get("tokenName") or "").strip() or None, note)
                if r.get("symbol"):
                    dash[sym] = str(r["symbol"])
        if not out:
            raise RuntimeError("/symbols: рынков нет")
        self._dash = dash
        self._trad = sorted(s for s, m in out.items() if m["tradable"])
        return out

    def _ensure(self):
        if self._dash is None:
            self.markets()

    def rates(self) -> dict[str, dict]:
        self._ensure()
        now = now_ms()
        out, errs = {}, {}
        for sym in self._trad:
            try:
                rows = self._data("/ticker", {"symbol": sym})
            except Exception as e:  # noqa — один символ не роняет прогон (порог FAIL_SHARE ниже)
                errs[sym] = f"{type(e).__name__}: {e}"[:200]
                continue
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
            v = dec((row or {}).get("fundingRate"))
            if v is None:
                errs[sym] = "тикер без fundingRate"
                continue
            nxt = iso_ms(row.get("nextFundingTime"))
            if not nxt or nxt <= now:
                nxt = next_boundary_ms(now, 1)
            out[sym] = rate(v, 1, nxt, "predicted")
        self.rate_errors = errs
        if self._trad and len(errs) > FAIL_SHARE * len(self._trad):
            raise RuntimeError(f"/ticker не ответил по {len(errs)} из {len(self._trad)}: "
                               + "; ".join(f"{k}: {v}" for k, v in list(errs.items())[:3]))
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        self._ensure()
        dash = (self._dash or {}).get(symbol)
        if not dash:
            raise RuntimeError(f"нет рынка {symbol} в /symbols")
        out: dict[int, Decimal] = {}
        hi = int(end_ms) + 1
        for _ in range(MAX_PAGES):
            data = self._data("/history-funding", dict(symbol=dash, limit=PAGE, beginTimeInclusive=int(start_ms),
                                                       endTimeExclusive=hi), gap=self.HIST_GAP_S)
            rows = [r for r in (data or {}).get("historyFunds") or [] if isinstance(r, dict)] if isinstance(data, dict) else []
            ts = []
            for r in rows:
                ms = to_int(r.get("fundingTime")) or to_int(r.get("fundingTimestamp"))
                if not ms:
                    continue
                ts.append(ms)
                v = dec(r.get("rate"))
                if v is not None and start_ms <= ms <= end_ms:
                    out[ms] = v
            if len(rows) < PAGE or not ts:
                break
            lo = min(ts)
            if lo <= start_ms:
                break
            if lo >= hi:
                raise RuntimeError(f"{symbol}: страница истории не сдвинулась ниже {hi}")
            hi = lo
        else:
            raise RuntimeError(f"{symbol}: история не дошла до {start_ms} за {MAX_PAGES} страниц")
        return sorted(out.items())
