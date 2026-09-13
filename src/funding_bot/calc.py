"""Арифметика таблиц. Всё в долях (0.0001 = 0.01 %), в проценты переводит дашборд.

«Текущий» и ставки в колонке «Фандинг» — В ЧАС (владелец 10.09: «приводи к 1ч»): за родной
интервал часовые биржи (Hyperliquid) проигрывали 8-часовым в 8 раз и тонули в сортировке — лучшая строка
Hyperliquid стояла 398-й из 768, а в пересчёте на час была 3-й. «Период» показывает родной интервал справочно.

futures/futures (пара площадок A|B, A — раньше в config.PERP_VENUES):
- ставка в час = ставка за интервал / интервал; «период» пары = больший из двух интервалов;
- «текущий» = час(A) − час(B); плюс → шорт A / лонг B; знак фандинга как у бирж (плюс — лонги платят шортам);
spot/futures (лонг спот любой спот-площадки + шорт перп):
- «текущий» = предсказанная ставка перпа в час; плюс — платят нам;
общее:
- окно = сумма ФАКТИЧЕСКИ расчитанных ставок в (now − W, now]; пустое окно — None, не 0;
- «Отклонения» нет (владелец 12.09: «откл не нужно»);
- комиссия = круг тейкером на обеих ногах по прайс-листу своих площадок (config.FEES_TAKER); у площадки, где сделка идёт по
  её собственной котировке без комиссии (config.QUOTE_COST_VENUES: Variational RFQ), — плюс полный спред котировки (ревью
  13.09: иначе такие строки выходили самыми дешёвыми); котировки нет — None («—»), как у DEX-ноги;
- курсовой = цена A / цена B − 1 за один токен, по мидам книг (исполнимая), при их отсутствии по маркам.
"""
from __future__ import annotations
from bisect import bisect_right
from . import config

H_MS = 3600_000


def hourly(rate: float | None, interval_h: int | None) -> float | None:
    if rate is None or not interval_h:
        return None
    return rate / float(interval_h)


def window_sums(events: list[tuple[int, float]], now_ms: int) -> dict[int, tuple[float | None, int]]:
    """Для каждого окна конфига: (сумма ставок в (now − W, now], число расчётов); пусто — (None, 0).
    Границы окон — бинарным поиском по времени, сумма — по срезу в том же порядке (результат до бита тот же). Раньше
    каждое окно перебирало всю историю ноги: 8 площадок 12.09 = ~4100 ног × 4 окна × до 720 расчётов раз в минуту."""
    ms = [m for m, _ in events]
    if any(a > b for a, b in zip(ms, ms[1:])):         # db.funding_events_since отдаёт по возрастанию; иначе — сортируем
        events = sorted(events)
        ms = [m for m, _ in events]
    bounds = window_bounds(ms, now_ms)
    hi = bounds[0]
    out = {}
    for w, lo in zip(config.WINDOWS_H, bounds[1:]):
        out[w] = (sum(r for _, r in events[lo:hi]), hi - lo) if hi > lo else (None, 0)
    return out


def window_bounds(ms: list[int], now_ms: int) -> tuple[int, ...]:
    """Границы окон в отсортированном ряду времён: (hi, lo окна 1, lo окна 2, …) — окно w это срез [lo:hi]. Суммы окон
    зависят только от ряда и этих границ: окно событий в памяти (events_window) пересчитывает суммы ноги, лишь когда её
    ряд сменился или граница перешла через расчёт."""
    return (bisect_right(ms, now_ms),) + tuple(bisect_right(ms, now_ms - w * H_MS) for w in config.WINDOWS_H)


def window_anchor(comp: dict | None, now_ms: int) -> int:
    """Правый край окон ноги. Пока последний расчёт по слову биржи ещё не лёг в БД (льгота после расчёта, до
    досбора hh:10), окно кончается перед ним. Иначе вчерашний такой же расчёт уже вышел из суток, а сегодняшнего
    ещё нет: сутки без одного расчёта выдавались за полные, и «отклонение» у 8-часовых ног врало на треть
    текущей ставки (проверка исправлений 11.09)."""
    p = (comp or {}).get("pending_ms")
    return p - 1 if p else now_ms


def mid(book: dict | None) -> float | None:
    if not book or not book.get("bid") or not book.get("ask"):
        return None
    return (book["bid"] + book["ask"]) / 2.0


def half_spread_bps(book: dict | None) -> float | None:
    m = mid(book)
    if not m:
        return None
    return (book["ask"] - book["bid"]) / m / 2.0 * 1e4


def quote_rt(book: dict | None, prem: dict | None) -> float | None:
    """Круг по котировке площадки, доля: полный спред (ask − bid) / мид — купить по ask, продать по bid. Из книги строки
    (коллектор даёт её только свежей и из того же снимка), иначе из «spread_rt», который клиент кладёт в ставку сам
    (Variational: котировка $1k того же снимка, без неё — base_spread_bps). Нет ни того, ни другого — None."""
    m = mid(book)
    if m:
        return (book["ask"] - book["bid"]) / m
    q = (prem or {}).get("spread_rt")
    return float(q) if q is not None and q >= 0 else None


def _costs(legs: list[tuple[str, dict | None, dict | None]]) -> tuple[float | None, float | None, bool]:
    """Ноги (площадка, книга, ставка) → (круг всех ног, его часть по котировкам, есть ли нога-котировка). Круг ноги =
    2 × тейкер (config.FEES_TAKER); у площадки из config.QUOTE_COST_VENUES — плюс quote_rt; котировки нет — (None, None, True)."""
    fee, q_sum, has_q = 0.0, 0.0, False
    for v, bk, pr in legs:
        fee += 2.0 * config.FEES_TAKER[v]
        if v in config.QUOTE_COST_VENUES:
            has_q = True
            q = quote_rt(bk, pr)
            if q is None:
                return None, None, True
            q_sum += q
    return fee + q_sum, (q_sum if has_q else None), has_q


def leg_cost(venue: str, book: dict | None = None, prem: dict | None = None) -> float | None:
    """Круг одной ноги (доля): 2 × тейкер по прайс-листу; у площадки-котировки — плюс полный спред; нет котировки — None."""
    return _costs([(venue, book, prem)])[0]


def side_for(spread: float | None) -> str | None:
    if spread is None or spread == 0:
        return None
    return "short_a" if spread > 0 else "short_b"


def url(venue: str, symbol: str, base: str, own: str | None = None) -> str | None:
    """Страница рынка. own — ссылка, которую положил в инструмент сам клиент (12.09: Bitget/Gate — %-кодировка 龙虾,
    Lighter — kPEPE вместо 1000PEPE и SPY_USDC вместо rhSPY/USDC); без неё — шаблон config.URLS; у площадки без
    страницы рынка (Lighter на Robinhood Chain) — None: имя без ссылки, а не чужой рынок и не KeyError."""
    if own:
        return own
    t = config.URLS.get(venue)
    return t.format(symbol=symbol, base=base) if t else None


def label(venue: str, symbol: str) -> str:
    """Подпись площадки в колонке «Биржа»: у HIP-3 рынков Hyperliquid — с именем dex («hyperliquid·xyz»); у перпа Lighter
    на Robinhood Chain — «lighter·rh» (config.LABELS)."""
    v = config.LABELS.get(venue, venue)
    return f"{v}·{symbol.split(':', 1)[0]}" if ":" in symbol else v


def _incomplete(comps: list[dict | None], w: int, now_ms: int) -> bool:
    lo = now_ms - w * H_MS
    for c in comps:
        if not c:
            continue
        # последний расчёт в плановом досборе (catchup) — не дыра: окно и так кончается перед ним (window_anchor)
        if (c.get("latest_missing") and not c.get("catchup")) or w in (c.get("shallow") or []) \
                or any(b > lo for _, b in (c.get("holes") or [])):
            return True
    return False


def _price(book: dict | None, mark: float | None) -> tuple[float | None, str | None]:
    """Мид книги, если коллектор отдал годную книгу (свежую и из того же снимка, что ставка), иначе марк —
    и откуда цена, чтобы страница могла это показать (ревью 11.09, п.3: старая книга рядом со свежей ставкой)."""
    m = mid(book)
    if m:
        return m, "book"
    return (mark, "mark") if mark else (None, None)


def _rel(row: dict, item: dict):
    """Рейтинг надёжности A/B/C (владелец 13.09, identity) — только у «тот»; ключи только у таких строк: table.json и так
    ~42 МБ, у «?» и «≠» свои пометки. rel_d (пул, имя) — лишь у C."""
    if item.get("ident") == "same" and item.get("rel"):
        row["rel"], row["rel_ev"] = item["rel"], item.get("rel_ev")
        if item.get("rel_d"):
            row["rel_d"] = item["rel_d"]


def build_ff_row(item: dict, ins_a: dict | None, ins_b: dict | None, prem_a: dict | None, prem_b: dict | None,
                 book_a: dict | None, book_b: dict | None,
                 ws_a: dict[int, tuple[float | None, int]], ws_b: dict[int, tuple[float | None, int]], now_ms: int,
                 comp_a: dict | None = None, comp_b: dict | None = None, stale: bool = False) -> dict:
    """Пара рынков. book_* коллектор передаёт только годные, иначе None — цена по марку (px_src_* = 'mark').
    stale — ставка хотя бы одной ноги устарела: строка показывается, но в сортировке идёт после годных."""
    va, vb = item["va"], item["vb"]
    # интервал из тика, если площадка его несёт (KuCoin/Bitget/Gate/Lighter), иначе из вселенной: смена интервала на
    # ходу иначе ждала бы часовой пересборки, и ставка в час была бы неверна в old/new раз (ревью 12.09)
    iv_a = (prem_a or {}).get("interval_h") or (ins_a or {}).get("interval_h") or 8
    iv_b = (prem_b or {}).get("interval_h") or (ins_b or {}).get("interval_h") or 8
    period = max(iv_a, iv_b)
    rate_a = (prem_a or {}).get("rate"); rate_b = (prem_b or {}).get("rate")
    rh_a, rh_b = hourly(rate_a, iv_a), hourly(rate_b, iv_b)
    spread_h = None if rh_a is None or rh_b is None else rh_a - rh_b
    spread = spread_h                     # в час — см. шапку модуля
    mark_a, mark_b = (prem_a or {}).get("mark"), (prem_b or {}).get("mark")
    (px_a, src_a), (px_b, src_b) = _price(book_a, mark_a), _price(book_b, mark_b)
    fa, fb = item.get("fa") or 1.0, item.get("fb") or 1.0
    gap = ((px_a / fa) / (px_b / fb) - 1.0) if px_a and px_b else None
    windows = {}
    for w in config.WINDOWS_H:
        sa, na = ws_a.get(w, (None, 0)); sb, nb = ws_b.get(w, (None, 0))
        windows[str(w)] = dict(a=sa, na=na, b=sb, nb=nb,
                               spread=None if (na == 0 and nb == 0) else (sa or 0.0) - (sb or 0.0),
                               incomplete=_incomplete([comp_a, comp_b], w, now_ms))
    fee, fee_q, has_q = _costs([(va, book_a, prem_a), (vb, book_b, prem_b)])
    row = dict(
        key=item["key"], base=item["base"], cls=item.get("cls") or "crypto", va=va, vb=vb, sa=item["sa"], sb=item["sb"],
        la=label(va, item["sa"]), lb=label(vb, item["sb"]),
        mismatch=bool(item.get("mismatch")), ident=item.get("ident"), ident_ev=item.get("ident_ev"),
        ident_why=item.get("ident_why") if item.get("ident") != "same" else None, stale=bool(stale),   # подсказка — у ≠ и ?
        urls=[url(va, item["sa"], item["base"], (ins_a or {}).get("url")),
              url(vb, item["sb"], item["base"], (ins_b or {}).get("url"))],
        iv_a=iv_a, iv_b=iv_b, period=period, rate_a=rate_a, rate_b=rate_b, rate_h_a=rh_a, rate_h_b=rh_b,
        spread_h=spread_h, spread=spread, side=side_for(spread),
        fee=fee,
        next_a=(prem_a or {}).get("next_ms"), next_b=(prem_b or {}).get("next_ms"),
        mark_a=mark_a, mark_b=mark_b, px_a=px_a, px_b=px_b, px_src_a=src_a, px_src_b=src_b, gap=gap,
        windows=windows,
    )
    if has_q:
        row["fee_q"] = fee_q          # часть «Комиссии» — спред котировки (подсказка страницы); ключ только у таких строк
    _rel(row, item)
    return row


def _shares_label(item: dict) -> str:
    """Токен акции с несколькими акциями внутри (владелец 12.09: NFLXX — 10, CRWDX — 4, TQQQX — 2, пересчитываем на
    одну акцию): подпись «gate·NFLXX ×10». Цена в «Курсовом» — за один токен, сравнение — за акцию (spot_factor)."""
    n = item.get("spot_factor") or 1.0
    return f" ×{round(n, 3):g}" if (item.get("cls") == "equity" and abs(n - 1.0) >= 0.01) else ""


def build_sf_row(item: dict, ins: dict | None, prem: dict | None, book: dict | None, spot_book: dict | None,
                 ws: dict[int, tuple[float | None, int]], now_ms: int, comp: dict | None = None,
                 stale: bool = False, dex: dict | None = None) -> dict:
    """Сделка спот (лонг) + перп (шорт). Доход — ставка перпа целиком: плюс = лонги перпа
    платят шортам = платят нам. Минус — платим мы (разворот требует шорта спота, в фазе 1 его нет).

    Курсовой = цена перпа / цена спота − 1 за один токен (базис: плюс — перп дороже, на входе это в нашу пользу).
    Комиссия = круг тейкером: спот своей площадки ×2 + перп ×2. spot_book коллектор передаёт только свежий, иначе None.
    """
    ex = item["perp_ex"]
    sv = item.get("spot_ex") or config.SPOT_VENUES[0]
    iv = (prem or {}).get("interval_h") or (ins or {}).get("interval_h") or 8      # тик — раньше вселенной
    rate = (prem or {}).get("rate"); mark = (prem or {}).get("mark")
    px_perp, src_perp = _price(book, mark)
    px_spot = mid(spot_book)
    pf, sf = item.get("perp_factor") or 1.0, item.get("spot_factor") or 1.0
    gap = ((px_perp / pf) / (px_spot / sf) - 1.0) if px_perp and px_spot else None
    windows = {}
    for w in config.WINDOWS_H:
        s, n = ws.get(w, (None, 0))
        windows[str(w)] = dict(spread=s, n=n, incomplete=_incomplete([comp], w, now_ms))
    rate_h = hourly(rate, iv)
    perp_fee, fee_q, has_q = _costs([(ex, book, prem)])        # перп-нога: тейкер ×2 (+ спред у площадки-котировки)
    if item.get("dex"):
        # DEX-нога (владелец 12.09: «DEX всё-включено, CEX как есть»): круг по цене на клип (пул + удар в обе стороны)
        # + газ входа и выхода + налог токена + 2 × тейкер перпа; котировки ещё нет — «—»
        cost = (dex or {}).get("cost")
        fee = None if cost is None or perp_fee is None else cost + perp_fee
        spot_url = (dex or {}).get("url")
    else:
        fee = None if perp_fee is None else 2.0 * (item.get("spot_fee") or config.FEES_TAKER[sv]) + perp_fee
        spot_url = url(sv, item["spot"], item.get("spot_asset") or item["spot"][:-len(config.QUOTE)], item.get("spot_url"))
    row = dict(
        key=item["key"], base=item["base"], cls=item.get("cls") or "crypto", spot=item["spot"], spot_ex=sv,
        # спот под другим тикером (токенизированная акция, синоним) — подписан своим тикером: «gate·CRCLX», «bitget·rCRCL»
        spot_label=config.LABELS.get(sv, sv) + (f"·{item['spot_tag']}" if item.get("spot_tag") else "") + _shares_label(item),
        perp_ex=ex, perp=item["perp"], perp_label=label(ex, item["perp"]), xfer=item.get("xfer"),
        mismatch=bool(item.get("mismatch")), ident=item.get("ident"), ident_ev=item.get("ident_ev"),
        ident_why=item.get("ident_why") if item.get("ident") != "same" else None, stale=bool(stale),   # подсказка — у ≠ и ?
        urls={"spot": spot_url, "perp": url(ex, item["perp"], item["base"], (ins or {}).get("url"))},
        period=iv, rate=rate, rate_h=rate_h, spread=rate_h, fee=fee, dex=(dex or {}).get("tip") if item.get("dex") else None,
        next_ms=(prem or {}).get("next_ms"), mark=mark, px_spot=px_spot, px_perp=px_perp,
        px_src_perp=src_perp, px_src_spot="book" if px_spot else None, gap=gap,
        half_perp_bps=half_spread_bps(book), half_spot_bps=half_spread_bps(spot_book),
        windows=windows,
    )
    if has_q:
        row["fee_q"] = fee_q
    _rel(row, item)
    return row
