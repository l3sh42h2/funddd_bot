"""Нормализация тикеров. `1000PEPE` и `kPEPE` — тысяча PEPE за единицу контракта.

На 10.09.2026 ни одна пара Aster/Binance не различалась префиксом (проверено), так что это
предохранитель на будущие листинги, а не рабочий путь. Множитель участвует в проверке
«тот ли актив»: сравниваются цены за ОДИН токен, а не за единицу контракта.
"""
from __future__ import annotations
import re


def norm_symbol_factor(base: str) -> tuple[str, float]:
    """базовый актив биржи -> (нормализованный тикер, токенов в единице контракта)."""
    s = base.upper(); factor = 1.0
    if base.startswith("k") and re.match(r"^K[A-Z0-9]{3,}$", s):
        s = s[1:]; factor = 1000.0
    m = re.match(r"^1M([A-Z][A-Z0-9]{2,})$", s)       # 1MBABYDOGE — миллион BABYDOGE (перп Binance, 12.09)
    if m:
        return m.group(1), 1_000_000.0
    m = re.match(r"^(1000+)([A-Z].*)$", s)
    if m:
        s = m.group(2); factor = float(m.group(1))
    return s, factor
