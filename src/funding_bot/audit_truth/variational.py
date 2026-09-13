"""Истина Variational Omni: один публичный снимок /metadata/stats (все листинги одним JSON, за кэшем Cloudflare до 60 с).
Свои запросы и свой разбор, без клиента коллектора variational.py.

Замерено с Мака 13.09.2026 (≈00:40 UTC) и сверено с docs.variational.io (omni/trading/funding-rates, pre-ipo-perpetuals,
tradfi-perpetuals):
- GET https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats → {listings: [...]}: 553 строки — ticker,
  name (полное имя), funding_rate, funding_interval_s, mark_price, quotes… Числа — строки. Нет статуса, класса, времени
  следующего расчёта. Символ дашборда = ticker ровно как у биржи («BTC», «1000PEPE», «OPN_OPINION»).
- funding_interval_s: 3600 ×3, 14400 ×305, 28800 ×239, 0 ×6. Ноль — «Swap on …» (USOILP, UKOILP, XAUS, XAGS, US500S,
  US100S): отдельный инструмент со своим финансированием [docs TradFi], не перп → в markets с tradable=False, без ставки.
- funding_rate — ГОДОВАЯ простая ставка (365 дн.), не «за интервал». Документация единицу не называет; доказательство
  своё, из самих чисел: процент по документации 0.00125 %/ч × 8760 = 0.1095 — ровно это число стоит у всех монет внутри
  клампа; pre-IPO по документации «0.005 % каждые 8 ч» × 1095 = 0.05475 — ровно столько у OPENAI / ANTHROPIC.
  Перевод: ставка за интервал = funding_rate × interval_h / 8760 (BTC 0.050542 × 8 / 8760 = 4.6e-5 за 8 ч).
  Это ставка ТЕКУЩЕГО окна (прогноз: премия раз в 60 с, среднее по окну [docs]); плюс — лонги платят [docs].
- Окно — как у Bybit, иначе как у Binance, иначе 1 ч [docs]. Расчёт на сетке интервала от эпохи UTC; поля в ответе нет —
  next_ms вычислен по сетке.
- Истории нет: ни публичного API, ни страницы (только CSV своих сделок) → history() = None. Окна дашборда — накопленное
  коллектором; аудитор сверяет их с БД и требует «!», пока накоплено меньше половины окна.
- Класса в API нет. Своё правило по документации и полному имени: OPENAI / ANTHROPIC — preipo (стр. Pre-IPO / TradFi);
  BZ CL COPPER NATGAS XAG XAU XPD XPT — commodity (таблица TradFi); корпоративная форма или фонд в имени (Inc., Corp., plc,
  Ltd., Holdings, N.V., «& Co.», ETF, «Trust, Series», Depositary …) — equity (US500 — «SPDR S&P 500 ETF Trust», доля
  фонда, не индекс); PAXG / XAUT — монеты; прочее — crypto. Перекрёстная улика — процентная часть ставки: у TradFi она 0
  [docs], у монет 0.1095. Имя монеты при ставке ровно 0 или «акция» при ставке ровно 0.1095 — класс неизвестен (None,
  причина в note), а не догадка. «Trust Wallet» (TWT), «Giggle Fund» — монеты: голых «Trust» / «Fund» в правиле нет.
- Лимит 10 запросов / 10 с на IP [docs]: markets() и rates() одного прогона делят один снимок (SNAP_TTL_S).
"""
from __future__ import annotations
import re, time
from decimal import Decimal
from .base import Truth, Http, dec, to_int, market, rate, next_boundary_ms, now_ms

URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
YEAR_H = 8760                                   # простая годовая ставка на 365 дней (см. шапку)
CRYPTO_APR = Decimal("0.1095")                  # процент монет 0.00125 %/ч × 8760 [docs]
PREIPO = frozenset({"OPENAI", "ANTHROPIC"})     # docs: Pre-IPO / таблица TradFi
COMMODITY = frozenset({"BZ", "CL", "COPPER", "NATGAS", "XAG", "XAU", "XPD", "XPT"})   # docs: таблица TradFi
GOLD_TOKENS = frozenset({"PAXG", "XAUT"})       # токены золота — монеты (правило репозитория)
_CORP = re.compile(r"\b(?:Inc|Incorporated|Corp|Corporation|plc|Ltd|Limited|Holdings?|Company|N\.V|S\.A|A/S|Oyj|ETF|"
                   r"Common Stock|Depositary|Trust, Series)(?:\.|\b)|&\s*Co\b|\bGroup$")


def interval_h(seconds) -> tuple[int | None, str | None]:
    """funding_interval_s → целые часы; 0 / мусор → None (своп). Нецелые часы округляются с пометкой."""
    s = to_int(seconds)
    if not s or s <= 0:
        return None, None
    h = s / 3600.0
    if abs(h - round(h)) > 1e-9 or h < 1:
        return max(1, int(round(h))), f"интервал {s} с — не целые часы, округлён"
    return int(round(h)), None


def asset_class(ticker: str, name: str | None, apr: Decimal | None) -> tuple[str | None, str | None]:
    """(класс, пояснение). Своп сюда не попадает (его отсекает интервал 0)."""
    t, nm = str(ticker).upper(), str(name or "").strip()
    if t in PREIPO:
        return "preipo", "pre-IPO по документации (фикс. 0.005 %/8 ч)"
    if t in GOLD_TOKENS:
        return "crypto", None
    if t in COMMODITY:
        cls = "commodity"
    elif _CORP.search(nm):
        cls = "equity"
    else:
        cls = "crypto"
    if apr is not None:
        if cls == "crypto" and apr == 0:
            return None, "имя монеты, а процентная часть ставки 0 (как у TradFi) — класс неизвестен"
        if cls != "crypto" and apr == CRYPTO_APR:
            return None, f"по имени {cls}, а ставка = процент монет 0.1095 — класс неизвестен"
    return cls, None


class VariationalTruth(Truth):
    venue = "variational"
    history_note = ("Variational не публикует историю фандинга (ни API, ни страницы — только CSV своих сделок): окна "
                    "дашборда — накопленное коллектором, сверяются с БД")
    SNAP_TTL_S = 8.0                    # markets() и сразу rates() — один снимок (он и так до 60 с старый за CF)

    def __init__(self, http: Http | None = None):
        super().__init__(http)
        self._snap: tuple[float, list[dict]] | None = None

    def _listings(self) -> list[dict]:
        if self._snap and time.time() - self._snap[0] < self.SNAP_TTL_S:
            return self._snap[1]
        body = self.http.get(URL)
        rows = body.get("listings") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"metadata/stats без listings: {str(body)[:200]}")
        rows = [x for x in rows if isinstance(x, dict) and x.get("ticker")]
        self._snap = (time.time(), rows)
        return rows

    def markets(self) -> dict[str, dict]:
        out = {}
        for x in self._listings():
            t, nm = str(x["ticker"]), (str(x.get("name") or "").strip() or None)
            iv, iv_note = interval_h(x.get("funding_interval_s"))
            if iv is None or (nm or "").startswith("Swap on "):
                out[t] = market(t, False, "other", None, nm, "своп («Swap on …», интервал 0): не перп, фандинга нет")
                continue
            cls, why = asset_class(t, nm, dec(x.get("funding_rate")))
            note = "; ".join(s for s in (why, iv_note) if s) or None
            out[t] = market(t, True, cls, iv, nm, note)
        return out

    def rates(self) -> dict[str, dict]:
        now = now_ms()
        out = {}
        for x in self._listings():
            iv, _n = interval_h(x.get("funding_interval_s"))
            apr = dec(x.get("funding_rate"))
            if iv is None or apr is None or str(x.get("name") or "").startswith("Swap on "):
                continue
            out[str(x["ticker"])] = rate(apr * iv / YEAR_H, iv, next_boundary_ms(now, iv), "predicted")
        return out

    def history(self, symbol: str, start_ms: int, end_ms: int) -> None:
        return None
