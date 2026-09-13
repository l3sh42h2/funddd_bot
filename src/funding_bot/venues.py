"""Единый интерфейс площадок поверх двух видов клиентов.

Binance-подобные (Binance, Aster) — клиент BinanceLike + разбор в exchanges.py. Площадки со своим API
(Hyperliquid) — клиент сам умеет perp_instruments/premium/books/history_since/recent_history.
Коллектор и история фандинга зовут только эти функции и не знают, какая площадка перед ними.
Добавить биржу = написать клиент со своими методами и дописать её в config.PERP_VENUES.
"""
from __future__ import annotations
from . import exchanges


def native(cl) -> bool:
    return hasattr(cl, "perp_instruments")


def perp_instruments(cl) -> list[dict]:
    """Перпы в USDT со статусом торгов; у каждого interval_h (объявленный), cap, floor."""
    if native(cl):
        return cl.perp_instruments()
    ins = exchanges.keep_best_quote(exchanges.perp_instruments(cl))
    fi = exchanges.funding_info(cl)
    for i in ins:
        f = fi.get(i["symbol"], {})
        i["interval_h"] = f.get("interval_h", 8); i["cap"] = f.get("cap"); i["floor"] = f.get("floor")
    return ins


def funding_intervals(cl) -> dict[str, int] | None:
    """Объявленные интервалы без пересборки вселенной. None — у площадки интервал фиксирован (Hyperliquid, Lighter: 1 ч)
    или она не умеет отдать их отдельно. Bitget и Gate меняют интервал на ходу и отдают его своим методом (12.09)."""
    if native(cl):
        return cl.funding_intervals() if hasattr(cl, "funding_intervals") else None
    return {s: v["interval_h"] for s, v in exchanges.funding_info(cl).items()}


def premium(cl) -> dict[str, dict]:
    """symbol -> {rate (предсказанная ставка за интервал, доля), mark, index, next_ms}."""
    return cl.premium() if native(cl) else exchanges.premium_index(cl)


def books(cl) -> dict[str, dict]:
    return cl.books() if native(cl) else exchanges.book_tickers(cl)


def spot_native(cl) -> bool:
    return hasattr(cl, "spot_instruments")


def spot_instruments(cl) -> list[dict]:
    """Спот-пары в USDT, торгуемые сейчас. Binance — exchanges.py; Gate/KuCoin/Bitget — свои клиенты (spot.py)."""
    return cl.spot_instruments() if spot_native(cl) else exchanges.spot_instruments(cl)


def spot_books(cl) -> dict[str, dict]:
    """Лучшие бид/аск всех спот-пар одним вызовом."""
    return cl.books() if spot_native(cl) else exchanges.book_tickers(cl)


def history_since(cl, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
    if native(cl):
        return cl.history_since(symbol, start_ms, end_ms)
    return exchanges.funding_history_since(cl, symbol, start_ms, end_ms)


def recent_history(cl) -> list[dict]:
    """Последние расчёты по всем монетам одним вызовом; у кого такого нет — пусто (поддержание добором)."""
    if native(cl):
        return cl.recent_history()
    return exchanges.funding_history(cl, None, limit=1000)
