"""Числа для сообщений фазы 2: план, прогресс, итог сделки, позиции (trade_spec §6 п.11, шаблоны отчёта telegram §9).

Только арифметика Decimal и форматирование чисел; тексты и HTML собирает tg/views.py. Источники — строки trade.db
(суммы там TEXT: сырые int единицы токена или Decimal строкой), чеки сети и ответы биржи.

Правила, выученные на прошлых ботах:
- неизвестное остаётся None и показывается «—», никогда не 0 (пустое чтение, принятое за «флэт», чуть не закрыло
  все позиции; «сверено ✓» при неизвестной стороне — ложь);
- средняя цена перпа — Σ quoteQty / Σ qty из userTrades (как платит биржа), не из ответа заявки;
- газ = gasUsed·effectiveGasPrice по чеку; апрувы отдельно от свопов (владелец видит, что стоил вход сам по себе);
- комиссия — абсолютная величина (знак в userTrades у Aster не гарантирован), не-долларовые активы — отдельно.
"""
from __future__ import annotations
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Mapping
from .types import Plan

D = Decimal
ZERO = D(0)
ONE = D(1)
BPS = D(10_000)
WEI = D(10) ** 18
NBSP = "\u00a0"            # разделитель разрядов: число не рвётся переносом строки в Telegram
MINUS = "−"           # «−» как в шаблонах сообщений
STABLE_ASSETS = frozenset({"USDT", "USDC", "USD1", "USD"})
SWAP_KINDS = frozenset({"swap", "bump", "cancel"})    # всё, что платит газ за своп, в т.ч. замена на том же nonce


def dval(v: Any) -> D | None:
    """TEXT/int/float/Decimal → Decimal; None и "" → None (неизвестно, не ноль)."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if isinstance(v, bool):
        raise TypeError("bool не число")
    if isinstance(v, D):
        return v
    if isinstance(v, int):
        return D(v)
    if isinstance(v, float):
        return D(repr(v))
    return D(str(v).strip())


def _sum(vals: Iterable[D | None]) -> D:
    return sum((v for v in vals if v is not None), ZERO)


# --- форматирование чисел -----------------------------------------------------------------------------
def fmt_num(x: D | None, places: int = 2, sign: bool = False) -> str:
    """1234567.891 → «1 234 567.89» (разряды неразрывным пробелом), минус — «−»; None → «—»."""
    if x is None:
        return "—"
    q = D(1).scaleb(-places) if places > 0 else D(1)
    v = x.quantize(q, rounding=ROUND_HALF_UP)
    neg = v < 0
    body = format(abs(v), "f")
    whole, _, frac = body.partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    s = NBSP.join(groups) + (("." + frac) if frac else "")
    if neg and v != 0:
        return MINUS + s
    return ("+" + s) if (sign and v > 0) else s


def fmt_qty(x: D | None, sign: bool = False) -> str:
    """Количество токенов: целое — без дробной части, иначе все значащие знаки (0.5 AIW3 не станет «1»)."""
    if x is None:
        return "—"
    n = x.normalize()
    places = max(-n.as_tuple().exponent, 0) if n != n.to_integral_value() else 0
    return fmt_num(x, places, sign)


def fmt_pct(frac: D | None, places: int = 3, sign: bool = True) -> str:
    """Доля → проценты: 0.0005 → «+0.050 %»."""
    return "—" if frac is None else fmt_num(frac * 100, places, sign) + NBSP + "%"


def short_addr(a: str | None) -> str:
    return "—" if not a else f"{a[:6]}…{a[-4:]}"


def short_hash(h: str | None) -> str:
    return "—" if not h else f"{h[:6]}…"


# --- окупаемость ------------------------------------------------------------------------------------------
def payback_h(entry_usd: D | None, exit_usd: D | None, usd_per_h: D | None, received_usd: D | None = None) -> D | None:
    """ОДНА формула окупаемости для всех сообщений (план, итог, «позиции»): (вход + выход − уже получено фандинга) /
    фандинг в $/ч. Выход — оценка: пока это издержки входа по плану (честный расчёт выхода — отдельная задача).
    Фандинг не в нашу пользу или что-то неизвестно — None (не «0 ч»). Уже окупилось — 0."""
    if entry_usd is None or exit_usd is None or usd_per_h is None or usd_per_h <= 0:
        return None
    return max(entry_usd + exit_usd - (received_usd or ZERO), ZERO) / usd_per_h


# --- план ---------------------------------------------------------------------------------------------
def plan_numbers(plan: Plan) -> dict:
    """Числа сообщения «План входа/выхода»: издержки по статьям в $ и в долях размера, фандинг, окупаемость."""
    e, inp = plan.est, plan.inputs
    S = inp["size_usd"]
    dex_cost = e["dex_fee_usd"] + e["dex_impact_usd"]
    fh = inp.get("funding_h")
    usd_h = fh * S if fh is not None else None                 # плюс — шорт получает
    total = e["total_usd"]
    exit_est = total if plan.kind == "entry" else None         # оценка выхода = издержки входа по плану
    return {
        "kind": plan.kind, "coin": plan.coin, "symbol": plan.symbol, "leg_usd": plan.leg_usd, "size_usd": S,
        "n": e["n"], "clip_usd": e["clip_usd"], "children": e["children"], "m": e["m"], "tokens": e["tokens"],
        "dex_px": e["dex_px"], "dex_cost_usd": dex_cost, "dex_cost_frac": dex_cost / S if S else None,
        "gas_usd": e["gas_usd"], "gas_per_swap_usd": e["gas_per_swap_usd"], "approve_usd": e["approve_usd"],
        "perp_spread_usd": e["perp_spread_usd"], "perp_spread_frac": e["perp_spread_usd"] / S if S else None,
        "perp_fee_usd": e["perp_fee_usd"], "fee_taker": inp.get("fee_taker"),
        "perp_no_refill_usd": e["perp_no_refill_usd"], "pace_usd": e["pace_usd"],
        "total_usd": total, "total_pct": e["total_pct"], "notional_usd": e["notional_usd"],
        "funding_h": fh, "usd_per_h": usd_h, "exit_est_usd": exit_est,
        "breakeven_h": payback_h(total, exit_est, usd_h),
        "basis_bps": e["basis_bps"], "unhedged_risk_usd": e["unhedged_risk_usd"],
        "missing": list(plan.missing_owner_keys), "expires": plan.expires,
    }


# --- ноги по строкам trade.db ----------------------------------------------------------------------------
def dex_leg(clips: Iterable[Mapping], kind: str, dec_token: int, dec_stable: int) -> dict:
    """Итог DEX-ноги по клипам (dex_in/dex_out — сырые единицы из чека). entry: стейбл → токен; exit: наоборот.
    avg_px — $ за токен, уже с комиссией пула и ударом."""
    tok = usd = ZERO
    n = 0
    for c in clips:
        din, dout = dval(c.get("dex_in")), dval(c.get("dex_out"))
        if din is None or dout is None:
            continue                                            # клип без чека — не в сумме (не ноль!)
        if kind == "entry":
            usd += din / D(10) ** dec_stable
            tok += dout / D(10) ** dec_token
        else:
            tok += din / D(10) ** dec_token
            usd += dout / D(10) ** dec_stable
        n += 1
    return {"tokens": tok, "usd": usd, "avg_px": (usd / tok) if tok > 0 else None, "clips": n}


def gas_totals(txs: Iterable[Mapping], native_px: D | None) -> dict:
    """Газ по чекам: свопы (swap/bump/cancel) и апрувы отдельно; в нативной монете и в $ (None — цена неизвестна)."""
    acc = {"swap": ZERO, "approve": ZERO}
    cnt = 0
    for t in txs:
        used, price = dval(t.get("gas_used")), dval(t.get("eff_gas_price"))
        if used is None or price is None:
            continue
        kind = "approve" if t.get("kind") == "approve" else ("swap" if t.get("kind") in SWAP_KINDS else None)
        if kind is None:
            continue
        acc[kind] += used * price / WEI
        cnt += 1
    usd = (lambda x: x * native_px if native_px is not None else None)
    return {"swap_native": acc["swap"], "approve_native": acc["approve"], "native": acc["swap"] + acc["approve"],
            "swap_usd": usd(acc["swap"]), "approve_usd": usd(acc["approve"]),
            "usd": usd(acc["swap"] + acc["approve"]), "txs": cnt}


def perp_leg(fills: Iterable[Mapping]) -> dict:
    """Перп-нога по userTrades: количество, VWAP = Σ quote / Σ qty, комиссия (в стейблах и прочих активах отдельно),
    доля мейкера по обороту, реализованный PnL."""
    qty = quote = comm = maker_q = taker_q = pnl = ZERO
    other: dict[str, D] = {}
    n = 0
    for f in fills:
        q, qq = dval(f.get("qty")), dval(f.get("quote_qty"))
        if q is None:
            continue
        if qq is None:
            px = dval(f.get("price"))
            qq = q * px if px is not None else ZERO
        qty += q
        quote += qq
        c = abs(dval(f.get("commission_abs")) or ZERO)
        asset = (f.get("commission_asset") or "USDT").upper()
        if asset in STABLE_ASSETS:
            comm += c
        elif c:
            other[asset] = other.get(asset, ZERO) + c
        if f.get("maker"):
            maker_q += qq
        else:
            taker_q += qq
        pnl += dval(f.get("realized_pnl")) or ZERO
        n += 1
    return {"qty": qty, "quote": quote, "vwap": (quote / qty) if qty > 0 else None, "commission_usd": comm,
            "commission_other": other, "maker_quote": maker_q, "taker_quote": taker_q,
            "maker_share": (maker_q / quote) if quote > 0 else None, "realized_pnl": pnl, "fills": n}


def perp_from_clips(clips: Iterable[Mapping]) -> tuple[D, D]:
    """Σ perp_qty, Σ perp_quote по клипам — для прогресса до добора userTrades."""
    cl = list(clips)
    return _sum(dval(c.get("perp_qty")) for c in cl), _sum(dval(c.get("perp_quote")) for c in cl)


# --- прогресс -----------------------------------------------------------------------------------------
def progress_numbers(*, kind: str, clips: list[Mapping], dec_token: int, dec_stable: int, n_planned: int,
                     txs: Iterable[Mapping] = (), native_px: D | None = None, fills: Iterable[Mapping] = (),
                     fee_taker: D | None = None, m: D = ONE) -> dict:
    """«⏳ Вход · клип i/n»: сколько куплено/продано на DEX, сколько захеджировано на перпе, дисбаланс ног (токены и
    $), газ и комиссии. Комиссия до добора userTrades — оценка по тарифу (commission_est=True).
    m — токенов в контракте: перп в контрактах, дисбаланс — в токенах (токены − контракты·m)."""
    dex = dex_leg(clips, kind, dec_token, dec_stable)
    pq, pquote = perp_from_clips(clips)
    fl = list(fills)
    if fl:
        comm, est = perp_leg(fl)["commission_usd"], False
    else:
        comm, est = (pquote * fee_taker if fee_taker is not None else None), True
    imb = dex["tokens"] - pq * m
    gas = gas_totals(txs, native_px)
    return {"kind": kind, "clip": dex["clips"], "n_planned": n_planned, "dex_tokens": dex["tokens"],
            "dex_usd": dex["usd"], "dex_avg_px": dex["avg_px"], "perp_qty": pq, "perp_quote": pquote,
            "perp_avg_px": (pquote / pq) if pq > 0 else None, "imbalance": imb,
            "imbalance_usd": imb * dex["avg_px"] if dex["avg_px"] is not None else None,
            "gas_usd": gas["usd"], "gas_native": gas["native"], "commission_usd": comm, "commission_est": est}


# --- итог сделки -------------------------------------------------------------------------------------
def final_numbers(*, kind: str, clips: list[Mapping], txs: Iterable[Mapping], fills: Iterable[Mapping],
                  dec_token: int, dec_stable: int, native_px: D | None, ref_px: D | None, perp_mid_ref: D | None,
                  plan_total_usd: D | None = None, est_exit_usd: D | None = None, funding_h: D | None = None,
                  next_funding_ms: int | None = None, position: Mapping | None = None,
                  started: float | None = None, finished: float | None = None, m: D = ONE,
                  perp_summary: Mapping | None = None, gas_complete: bool = True) -> dict:
    """«✅ Вход завершён»: ноги, дисбаланс (пыль), базис, издержки факт против плана, окупаемость, ликвидация.

    Издержки считаются против опорных цен ПЛАНА (ref_px — DEX, perp_mid_ref — мид перпа): удар спота, проскальзывание
    перпа, комиссия, газ. Неизвестная статья не превращается в 0 — она в unknown, а итог помечен неполным.
    basis_bps = (VWAP перпа за токен / средняя DEX − 1)·1e4: на входе плюс — шорт продан дороже купленного спота.
    m — токенов в контракте: пыль в токенах (токены − контракты·m), VWAP контракта / m — цена токена.
    """
    tx_list = list(txs)
    dex = dex_leg(clips, kind, dec_token, dec_stable)
    gas = (gas_totals(tx_list, native_px) if gas_complete else
           dict.fromkeys(('swap_native', 'approve_native', 'native', 'swap_usd', 'approve_usd', 'usd', 'txs')))
    perp = perp_leg(fills) if perp_summary is None else dict(perp_summary)
    dust = dex["tokens"] - perp["qty"] * m if perp["qty"] is not None else None
    basis = ((perp["vwap"] / m / dex["avg_px"] - 1) * BPS) if (perp["vwap"] and dex["avg_px"]) else None
    impact = None
    if ref_px is not None and dex["tokens"] > 0:
        impact = dex["usd"] - dex["tokens"] * ref_px if kind == "entry" else dex["tokens"] * ref_px - dex["usd"]
    slip = None
    if perp_mid_ref is not None and perp["qty"] is not None and perp["qty"] > 0 and perp["quote"] is not None:
        slip = (perp_mid_ref * perp["qty"] - perp["quote"]) if kind == "entry" else (perp["quote"] - perp_mid_ref * perp["qty"])
    parts = {"impact_usd": impact, "perp_slip_usd": slip, "commission_usd": perp["commission_usd"],
             "gas_usd": gas["usd"]}
    unknown = [k for k, v in parts.items() if v is None]
    total = _sum(parts.values())
    leg = dex["usd"]
    usd_h = funding_h * perp["quote"] if (funding_h is not None and perp["quote"] is not None and perp["quote"] > 0) else None
    breakeven = None
    if kind == "entry" and not unknown:
        breakeven = payback_h(total, est_exit_usd if est_exit_usd is not None else ZERO, usd_h)
    liq = mark = liq_dist = lev = margin = None
    if position:
        liq, mark = dval(position.get("liquidationPrice")), dval(position.get("markPrice"))
        lev = dval(position.get("leverage"))
        margin = dval(position.get("isolatedMargin") or position.get("isolatedWallet"))
        if liq is not None and liq > 0 and mark:
            liq_dist = abs(mark - liq) / mark
    return {
        "kind": kind, "dex": dex, "gas": gas, "perp": perp, "dust": dust, "basis_bps": basis, **parts,
        "total_usd": total, "total_complete": not unknown, "unknown": unknown,
        "total_pct": (total / (2 * leg) * 100) if leg > 0 else None,
        "plan_total_usd": plan_total_usd,
        "vs_plan_usd": (total - plan_total_usd) if (plan_total_usd is not None and not unknown) else None,
        "est_exit_usd": est_exit_usd, "funding_h": funding_h, "usd_per_h": usd_h, "breakeven_h": breakeven,
        "next_funding_ms": next_funding_ms, "liq_px": liq, "mark_px": mark, "liq_dist_frac": liq_dist,
        "leverage": lev, "margin": margin,
        "duration_s": (finished - started) if (started is not None and finished is not None) else None,
        "tx_hashes": [t.get("tx_hash") for t in tx_list if t.get("tx_hash") and t.get("kind") in SWAP_KINDS
                      and t.get("status") == 1],
    }


# --- позиции ------------------------------------------------------------------------------------------
def positions_numbers(*, spot_units: int | None, dec_token: int, spot_px: D | None, position_amt: D | None,
                      mark: D | None, unrealized: D | None, income_rows: Iterable[Mapping], created: float,
                      now: float, book_tokens: D | None = None, book_short: D | None = None,
                      tol: D = ZERO, m: D = ONE) -> dict:
    """«📊 Позиции» по ПРАВДЕ (balanceOf и positionRisk), сверка с журналом сделки.
    position_amt — знаковая позиция биржи (шорт < 0), None = не прочиталась. matches: True/False, None — сверять
    не с чем (любая сторона неизвестна): «совпадает ✓» при неизвестной стороне было бы ложью.
    m — токенов в контракте: шорт в контрактах (× марк контракта — $), дельта ног — в токенах."""
    spot = D(spot_units) / D(10) ** dec_token if spot_units is not None else None
    short = -position_amt if position_amt is not None else None
    delta = (spot - short * m) if (spot is not None and short is not None) else None
    inc = [dval(r.get("income")) for r in income_rows]
    matches = None
    if None not in (spot, short, book_tokens, book_short):
        matches = abs(spot - book_tokens) <= tol and abs(short - book_short) <= tol
    return {"spot_tokens": spot, "spot_usd": (spot * spot_px) if (spot is not None and spot_px is not None) else None,
            "short_qty": short, "short_usd": (short * mark) if (short is not None and mark is not None) else None,
            "pnl_usd": unrealized, "delta": delta,
            "delta_usd": (delta * spot_px) if (delta is not None and spot_px is not None) else None,
            "funding_usd": _sum(inc), "funding_count": sum(1 for v in inc if v is not None),
            "hold_h": D(str(max(now - created, 0.0))) / 3600, "matches": matches}
