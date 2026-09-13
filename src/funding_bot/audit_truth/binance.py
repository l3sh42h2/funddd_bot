"""Истина Binance USDⓈ-M (fapi.binance.com). Точки и ловушки — в _fapi.py.

Класс актива — по полям самой биржи (замер 13.09: underlyingType COIN 703, EQUITY 156, HK_EQUITY 15, KR_EQUITY 8,
CN_EQUITY 2, COMMODITY 8, INDEX 3, PREMARKET 2): INDEX — индекс (BTCDOM, ALL, DEFI); PREMARKET — до IPO/запуска;
COMMODITY — сырьё; *EQUITY — акции (TRADIFI_PERPETUAL); COIN — монета. Незнакомый тип у TRADIFI — None (неизвестно),
а не «акция»: присутствие тогда судит только по наличию пары.
"""
from __future__ import annotations
from ._fapi import FapiTruth


class BinanceTruth(FapiTruth):
    venue = "binance"
    BASE = "https://fapi.binance.com"
    HIST_GAP_S = 0.7                   # 500 вызовов fundingRate / 5 мин на IP — общий с коллектором

    def asset_class(self, s: dict) -> str | None:
        ut = (s.get("underlyingType") or "").upper()
        if ut == "INDEX":
            return "index"
        if ut == "PREMARKET":
            return "preipo"
        if ut == "COMMODITY":
            return "commodity"
        if ut.endswith("EQUITY"):
            return "equity"
        if ut == "COIN":
            return "crypto"
        return None
