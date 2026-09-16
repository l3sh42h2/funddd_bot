"""Чистые форматтеры чисел, времени и меток для владельца — общие для trade (движки, сверка) и tg (Telegram-
шаблоны). Перенесены из tg/views.py и tg/sol_views.py (AC-07, docs/MIGRATION_ACCEPTANCE.md: «core не зависит от
Telegram/presenters» — ревью Codex 16.09 отметило, что engine.py/sol_flow.py всё ещё лениво импортировали
tg.views/tg.sol_views ради этих функций; здесь та же реализация, без зависимости от tg).

Здесь не должно быть ничего специфичного для Telegram: ни HTML-тегов, ни эмодзи-разметки, ни экранирования
(escape()/tx_link() остаются в tg/sender.py). money() принимает явный параметр html, но производит лишь generic
«&lt;» вместо «<» символом (тот же смысл, что «0.00» → «< 0.01») — самих тегов сообщения (<b>, <code>, ...) тут
нет и не будет.

tg/views.py и tg/sol_views.py переиспользуют эти же функции импортом (не копией) — числа, подписи и пороги
показа, которые видит владелец, не меняются ни на символ; правила форматирования — в исходном докстринге
tg/views.py (тысячи неразрывным пробелом, минус «−», проценты фандинга 3 знака, цены 4 значащих и т.д.)."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

NBSP = " "             # неразрывный пробел: число не переносится
MINUS = "−"
DASH = "—"
USD = NBSP + "$"
CENT = Decimal("0.01")

# --- таблицы меток (было в tg/views.py / tg/sol_views.py) ---------------------------------------------------
VENUE_LABEL = {"aster": "Aster", "binance": "Binance", "hyperliquid": "Hyperliquid", "gate": "Gate"}
DEAL_STATE_LABEL = {"DRAFT": "черновик", "ENTERING": "входит", "PAUSED": "на паузе", "OPEN": "открыта",
                    "EXITING": "выходит", "CLOSED": "закрыта", "ABORTED": "снята",
                    "HALTED_MISMATCH": "остановлена: расхождение"}
PATH_LABEL = {"jupiter_build_v2": "Jupiter", "jupiter_order_v2": "Jupiter Order", "okx_solana_v6": "OKX"}
# короткие причины исключения маршрута (коды spot_router); прочие — кодом как есть
REASON_SHORT = {
    "path_disabled": "выключен", "external_signer": "внешний подписант — только показ", "capability": "только показ",
    "not_validated": "не проверен", "not_simulated": "не симулирован", "no_credentials": "нет ключа",
    "deadline": "не успел к сроку", "stale": "котировка устарела", "rate_limited": "лимит запросов",
    "limit_missing": "не задан лимит", "hedge_depth": "стакан HL не покрывает", "margin_insufficient": "мало маржи HL",
    "margin_unknown": "маржа HL неизвестна", "price_impact_over_limit": "удар цены выше допуска",
    "network_fee_over_cap": "сеть дороже лимита", "tip_forbidden": "tip запрещён", "no_route": "нет маршрута",
    "simulation_failed": "симуляция упала", "fee_unknown": "расход неизвестен", "book_stale": "стакан HL устарел",
    "auth": "ключ не принят", "quota": "квота", "http": "сеть", "error": "сбой", "schema": "ответ не по контракту",
    "route_foreign_sol": "SOL уходит на чужой счёт",
}


# --- числа и время (было в tg/views.py) ----------------------------------------------------------------------
def _d(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    try:
        d = Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _signed(body: str, neg: bool, pos: bool, sign: bool) -> str:
    return (MINUS if neg else ("+" if sign and pos else "")) + body


def num(v: Any, places: int = 2, sign: bool = False) -> str:
    """Число с places знаками, тысячи — неразрывным пробелом; None → «—»."""
    d = _d(v)
    if d is None:
        return DASH
    q = d.quantize(Decimal(1).scaleb(-places), ROUND_HALF_EVEN)
    body = format(abs(q), f",.{places}f").replace(",", NBSP)
    return _signed(body, q < 0, q > 0, sign)


def money(v: Any, sign: bool = False, unit: bool = True, html: bool = True) -> str:
    """Издержки, PnL, фандинг: 2 знака и « $». Меньше цента — «< 0.01 $», а не «0.00» (деньги были). html=False —
    для текста, который потом ещё раз пройдёт escape() (отказы и причины паузы из движка)."""
    d = _d(v)
    if d is None:
        return DASH
    tail = USD if unit else ""
    if d != 0 and abs(d) < CENT:
        return _signed(("&lt;" if html else "<") + NBSP + "0.01", d < 0 and sign, d > 0, sign) + tail
    return num(d, 2, sign) + tail


def _leg_num(v: Any) -> str:
    """Размер без «$»: целое — без копеек («200»), иначе 2 знака («199.50»)."""
    d = _d(v)
    if d is None:
        return DASH
    q = d.quantize(CENT, ROUND_HALF_EVEN)
    return num(q, 0 if q == q.to_integral_value() else 2)


def leg(v: Any) -> str:
    """Размер ноги: «200 $», «199.50 $»."""
    s = _leg_num(v)
    return s if s == DASH else s + USD


def tok(v: Any, sign: bool = False, step: Any = None) -> str:
    """Токены: целые с разрядами («4 902»); при шаге перпа меньше 1 — до его знаков; остаток меньше шага (или меньше 1,
    если шаг неизвестен) — 2 значащие цифры («+0.15»)."""
    d = _d(v)
    if d is None:
        return DASH
    st = _d(step)
    unit = st if (st is not None and 0 < st < 1) else Decimal(1)
    if d != 0 and abs(d) < unit:
        return num(d, max(2, 1 - d.adjusted()), sign)
    return num(d, max(0, -unit.normalize().as_tuple().exponent), sign)


def px(v: Any, sig: int = 4) -> str:
    """Цена: sig значащих цифр без экспоненты, нули справа сохраняются (0.04080, 1.235, 64 210)."""
    d = _d(v)
    if d is None:
        return DASH
    if d == 0:
        return "0"
    return num(d, max(0, sig - 1 - d.adjusted()))


def pct(v: Any, places: int = 2, sign: bool = False) -> str:
    """Процент: «0.32 %», со знаком «+0.036 %»/«−0.06 %». Фандинг — places=3."""
    s = num(v, places, sign)
    return s if s == DASH else f"{s}{NBSP}%"


def dur(s: float | None) -> str:
    """47 с · 3 мин · 3 ч 10 мин · 2 д 4 ч."""
    if s is None:
        return DASH
    s = max(0, int(round(float(s))))
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} ч" + (f" {m} мин" if m else "")
    d, h = divmod(s // 3600, 24)
    return f"{d} д" + (f" {h} ч" if h else "")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def contracts(q: Any, m: Any = None, sign: bool = False, step: Any = None, coin: str | None = None) -> str:
    """Количество перпа. m = 1 (или неизвестен) — как токены: «4 902 AIW3» (C: тексты m = 1 прежние); m ≠ 1 —
    «2 контр. (= 2 000 токенов)» (владелец 13.09: только важное; единица названа в скобках, монета не нужна)."""
    k = _d(m)
    if k is None or k == 1:
        return tok(q, sign, step) + (f" {coin}" if coin else "")
    d = _d(q)
    if d is not None and k == 0:            # m не известен (ревью 13.09, M3): контракты без пересчёта в токены
        return f"{tok(d, sign, step)} контр."
    return DASH if d is None else f"{tok(d, sign, step)} контр. (= {tok(d * k, sign)} токенов)"


def m_unknown_text(did: str) -> str:
    """Ревью 13.09, M3: m сделки не известен — дельта ног не считается, «дохедж»/«откат» не предлагаются."""
    return f"множитель контракта не известен — только «выход {did}» целиком"


# --- Solana: маршруты и причины отказа (было в tg/sol_views.py) ----------------------------------------------
def reason(code: str) -> str:
    c = str(code).split(":", 1)[0]
    return REASON_SHORT.get(c, c)


def short_mint(mint: str | None) -> str:
    """«9cRC…pump» — mint как есть (регистр значим), середина скрыта."""
    m = str(mint or "")
    return m if len(m) <= 12 else f"{m[:4]}…{m[-4:]}"
