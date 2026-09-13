"""Вселенная: инструменты всех площадок -> пары futures/futures и сделки spot/futures.

futures/futures: для каждой монеты — каждая пара площадок, где она есть (aster|binance, aster|hyperliquid,
binance|hyperliquid ...). Ключ «A|B:МОНЕТА», A — площадка раньше в config.PERP_VENUES.
spot/futures: спот любой спот-площадки (Binance, Gate, KuCoin, Bitget) + перп любой перп-площадки.
Ключ «спот_площадка:символ|перп_площадка:символ»: у монеты их много.

Ловушка одинаковых тикеров (10.09.2026): MEMEUSDT на Aster — другая монета, чем MEME на Binance, AIUSDT на Aster —
не спот AI. Одинаковое имя не доказывает одинаковый актив. С 12.09 это решает identity.py — по составу индекса перпа
и контрактам монет, а не по цене (владелец: «не курс»); здесь строится только кандидат по тикеру, классу и проверенным
синонимам. Помеченная строка остаётся в таблице серой, чтобы было видно, что её не потеряли, а отбросили.
"""
from __future__ import annotations
import logging
from . import config

log = logging.getLogger(__name__)


def build_ff(perps: dict[str, list[dict]]) -> list[dict]:
    """Все пары рынков одной монеты на РАЗНЫХ площадках.

    Ключ «A:символ|B:символ» (11.09): у монеты на одной площадке бывает несколько рынков — HIP-3 dex Hyperliquid
    (xyz:NET и para:NET — разные перпы с разным фандингом), и каждая комбинация — отдельная сделка. Раньше ключ
    был «A|B:монета» и из нескольких рынков брался один. Дубли по квоте (BTCUSDT и BTCUSDC одной биржи) сюда не
    доходят — их убирает exchanges.keep_best_quote.
    """
    vs = [v for v in config.PERP_VENUES if v in perps]
    by: dict[str, dict[tuple[str, str], list[dict]]] = {}
    for v in vs:
        by[v] = {}
        skip = config.PERP_UNPAIRED.get(v, {})
        for i in perps[v]:
            # пара — только внутри класса актива (12.09): Quant (QNT, монета) и Quantinuum (xyz:QNT, акция) — не пара.
            # «rwa» — класс «не распознан» (клиенты ставят его незнакомому рынку: «пара ни с чем»); 13.09 (повод — Variational
            # B3, позже сверенный как монета): два нераспознанных рынка с одним тикером на разных биржах — тоже не пара.
            # Рынки с чужой единицей цены (config.PERP_UNPAIRED: Extended XIAOMI в HKD, Gate KR200 в долларах) — тоже.
            cls = i.get("cls") or "crypto"
            if cls == "rwa" or i["symbol"] in skip:
                continue
            by[v].setdefault((cls, i["base"]), []).append(i)
    out = []
    for x in range(len(vs)):
        for y in range(x + 1, len(vs)):
            va, vb = vs[x], vs[y]
            for cls, base in sorted(set(by[va]) & set(by[vb])):
                for a in sorted(by[va][(cls, base)], key=lambda i: i["symbol"]):
                    for b in sorted(by[vb][(cls, base)], key=lambda i: i["symbol"]):
                        out.append(dict(key=f"{va}:{a['symbol']}|{vb}:{b['symbol']}", base=base, cls=cls, va=va, vb=vb,
                                        sa=a["symbol"], sb=b["symbol"], fa=a["factor"], fb=b["factor"]))
    out.sort(key=lambda r: (r["base"], vs.index(r["va"]), vs.index(r["vb"]), r["sa"], r["sb"]))
    return out


def class_splits(perps: dict[str, list[dict]], marks: dict[str, dict[str, float | None]], tol: float = 0.01) -> list[dict]:
    """Рынки, чей класс актива расходится с классом той же базы у ≥ 2 других бирж при той же цене за единицу (в пределах
    tol от их медианы). Только для журнала: пары строятся внутри класса, и такой рынок молча выпадает из всех пар с ними.
    Ревью 13.09: SPCX / CXMT / UNITREE / CBRS — акция у 10 бирж, «до IPO» / «rwa» у edgeX, Backpack, ApeX по зашитым
    спискам — 70 пар и 21 сделка пропадали без следа. Цена здесь — только проверка единиц, класс она не решает."""
    from statistics import median
    by: dict[str, list[tuple[str, str, str, float]]] = {}
    for v, ins in perps.items():
        for i in ins:
            m = (marks.get(v) or {}).get(i["symbol"])
            if m and m > 0:
                by.setdefault(i["base"], []).append((v, i["symbol"], i.get("cls") or "crypto", m / (i.get("factor") or 1.0)))
    out = []
    for base in sorted(by):
        xs = by[base]
        nv: dict[str, set[str]] = {}
        for v, _s, c, _p in xs:
            nv.setdefault(c, set()).add(v)
        if len(nv) < 2:
            continue
        major = max(sorted(nv), key=lambda c: len(nv[c]))
        if len(nv[major]) < 2:
            continue
        ps = [p for _v, _s, c, p in xs if c == major]
        ref = median(ps)
        if any(abs(p / ref - 1.0) > tol for p in ps):
            continue            # большинство само расходится в цене (живой 13.09: BP KuCoin 0.490 / Gate 0.595) — единицы неясны
        for v, s, c, p in xs:
            if c != major and len(nv[c]) < len(nv[major]) and abs(p / ref - 1.0) <= tol:
                out.append(dict(base=base, venue=v, symbol=s, cls=c, major=major, n_major=len(nv[major])))
    return out


LEGACY_SPOT = "binance_spot"      # до 12.09 спот был только Binance, и ключом сделки был перп


def build_sf(perps: dict[str, list[dict]], spots: dict[str, list[dict]]) -> list[dict]:
    """Спот любой спот-площадки (лонг) + перп любой перп-площадки (шорт) по нормализованному базовому активу.

    Строка = сделка (спот-площадка и спот, перп-площадка и перп): у одной монеты их много — это разные сделки с
    разными ставками, книгами и комиссиями. Ключ «спот_площадка:символ|перп_площадка:символ» (12.09: споты Gate,
    KuCoin, Bitget); у сделок со спотом Binance ключ прежней версии («перп_площадка:символ») — в legacy_key. Множители у
    спота и перпа разные (спот PEPE против перпа 1000PEPE / kPEPE; у токена акции NFLXX — 10 акций), хранятся оба.
    """
    sorder = {v: n for n, v in enumerate(config.SPOT_VENUES)}
    porder = {v: n for n, v in enumerate(config.PERP_VENUES)}
    out = []
    for sv in sorted(spots, key=lambda v: sorder.get(v, len(sorder))):
        by: dict[str, list[dict]] = {}
        by_stock: dict[str, list[dict]] = {}
        for s in spots[sv]:
            by.setdefault(s["base"], []).append(s)
            if s.get("alt_base"):
                by_stock.setdefault(s["alt_base"], []).append(s)
        alias = lambda cls, base: [a for a, vs in config.SPOT_ALIASES.get(cls, {}).get(base, []) if vs is None or sv in vs]
        for ex in config.PERP_VENUES:
            skip = config.PERP_UNPAIRED.get(ex, {})
            for i in perps.get(ex, []):
                if i["symbol"] in skip:                          # чужая единица цены (Extended XIAOMI в HKD) — сделки нет
                    continue
                # по классу актива перпа (12.09): монета — та же база и проверенные синонимы (SPORTFUN — это FUN на споте);
                # акция — только токенизированные акции (CRCL ← CRCLX, CRCLON, CRCLG, rCRCL, CRCLB); сырьё — только
                # проверенные токены (золото ← PAXG, XAUT); индексу и pre-IPO спота нет
                cls = i.get("cls") or "crypto"
                if cls == "crypto":
                    cands = list(by.get(i["base"], []))
                    for a in alias(cls, i["base"]):
                        cands += by.get(a, [])
                elif cls == "equity":
                    cands = list(by_stock.get(i["base"], []))
                elif cls == "commodity":
                    cands = [s for a in alias(cls, i["base"]) for s in by.get(a, [])]
                elif cls == "fx":
                    cands = list(by.get(i["base"], []))          # валюта: xyz:EUR ← спот EUR (тестировщик 12.09)
                else:
                    cands = []
                seen: set[str] = set()
                for sp in cands:
                    if sp["symbol"] in seen:
                        continue
                    seen.add(sp["symbol"])
                    it = dict(key=f"{sv}:{sp['symbol']}|{ex}:{i['symbol']}", base=i["base"], cls=cls, spot_ex=sv, spot=sp["symbol"],
                              spot_asset=sp.get("base_asset") or sp["base"], spot_factor=sp["factor"],
                              spot_fee=sp.get("taker_fee"),       # у KuCoin своя у каждой пары (класс A/B/C)
                              spot_url=sp.get("url"),             # страницу знает сам клиент (Lighter: rhSPY → SPY_USDC)
                              spot_tag=None if sp["base"] == i["base"] else (sp.get("base_asset") or sp["base"]),
                              perp_ex=ex, perp=i["symbol"], perp_factor=i["factor"])
                    if sv == LEGACY_SPOT:
                        it["legacy_key"] = f"{ex}:{i['symbol']}"
                    out.append(it)
    out.sort(key=lambda r: (r["base"], sorder.get(r["spot_ex"], len(sorder)), porder[r["perp_ex"]], r["perp"], r["spot"]))
    return out
