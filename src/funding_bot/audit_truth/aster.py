"""Истина Aster (fapi.asterdex.com — клон API Binance). Точки и ловушки — в _fapi.py.

Класс актива (замер 13.09): underlyingType у Aster всегда COIN, класс — в underlyingSubType (STOCK, ETF, Commodities,
pre-launch) и в channel (nasdaq, hkstock, krstock, astock — акции; forex — валюта, 11 рынков). PAXG/XAUT помечены
Commodities, но это токены золота — монеты (их спот — та же монета).
"""
from __future__ import annotations
from ._fapi import FapiTruth

EQUITY_CHANNELS = {"nasdaq", "hkstock", "krstock", "astock", "usstock"}


class AsterTruth(FapiTruth):
    venue = "aster"
    BASE = "https://fapi.asterdex.com"
    HIST_GAP_S = 0.3

    def asset_class(self, s: dict) -> str | None:
        sub = {str(x).upper() for x in (s.get("underlyingSubType") or [])}
        ch = str(s.get("channel") or "").lower()
        base = (s.get("baseAsset") or "").upper()
        if sub & {"PRE-LAUNCH", "PRE-IPO"}:
            return "preipo"
        if "INDEX" in sub:
            return "index"
        if base in ("PAXG", "XAUT"):
            return "crypto"
        if "COMMODITIES" in sub:
            return "commodity"             # металлы у Aster бывают и в channel forex (XAU, XAG) — сырьё важнее канала
        if ch == "forex":
            return "fx"
        if sub & {"STOCK", "ETF"} or ch in EQUITY_CHANNELS:
            return "equity"
        return "crypto"
