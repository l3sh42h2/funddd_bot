"""Тексты владельцу по связке Solana × Hyperliquid (вариант C: коротко, только важное). Форматтеры и каркас — общие
(views): те же числа, минусы, 🧪 симуляции. Тексты BSC/Aster здесь не строятся и не меняются.

Что видно всегда: сеть и USDC, точный рынок HL (dex:МОНЕТА), фактический маршрут клипа (не политика «auto»),
ожидаемый и минимальный выход, курсовой с издержками, статус соответствия mint ↔ перп, голая нога и её размер.
Ссылки — Solscan по подписи Solana и обозреватель Hyperliquid по хэшу fill (не BscScan)."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from ..trade import tconfig
from . import views as V
from .sender import escape, tx_link

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


def route(path: str | None) -> str:
    return escape(PATH_LABEL.get(str(path or ""), str(path or V.DASH)))


def reason(code: str) -> str:
    c = str(code).split(":", 1)[0]
    return REASON_SHORT.get(c, c)


def short_mint(mint: str | None) -> str:
    """«9cRC…pump» — mint как есть (регистр значим), середина скрыта."""
    m = str(mint or "")
    return m if len(m) <= 12 else f"{m[:4]}…{m[-4:]}"


def links(sig: str | None, hl_hashes: tuple[str, ...] = ()) -> str | None:
    """«Solscan · HL» — подпись свопа и хэши fills HL; нечего показать — None."""
    parts = []
    if sig and not str(sig).startswith("sim-"):
        parts.append(tx_link(sig, "sol", "Solscan"))
    hs = [h for h in hl_hashes if h]
    for i, h in enumerate(hs[:2]):
        parts.append(tx_link(h, "hyperliquid", "HL" if len(hs) == 1 else f"HL {i + 1}"))
    if len(hs) > 2:
        parts.append(f"и ещё {len(hs) - 2}")
    return " · ".join(parts) if parts else None


def _bps(v: Any) -> str:
    d = V._d(v)
    return V.DASH if d is None else V.pct(d / 100, 2, sign=True)


def _usdc(v: Any, sign: bool = False) -> str:
    d = V._d(v)
    return V.DASH if d is None else f"{V.num(d, 2, sign=sign)}{V.NBSP}USDC"


@dataclass(frozen=True)
class SolPlanView:
    intent_id: str
    kind: str                           # entry | exit
    coin: str
    fullcoin: str                       # para:ANSEM — точный рынок HL
    deal_id: str | None = None
    usdc: Decimal | None = None         # вход: бюджет USDC (ExactIn); выход: ожидаемая выручка
    usdc_min: Decimal | None = None     # выход: при минимальном выходе
    tokens: Decimal | None = None       # вход: ожидаемые токены; выход: продаваемые
    tokens_min: Decimal | None = None   # вход: при минимальном выходе
    perp_qty: Decimal | None = None     # вход: шорт; выход: откуп
    perp_px: Decimal | None = None
    path: str | None = None             # маршрут-победитель
    others: tuple[str, ...] = ()        # прочие пути: «OKX: дороже на 0.12 %», «Jupiter Order: только показ»
    basis_bps: Decimal | None = None    # с издержками
    basis_gross_bps: Decimal | None = None
    spot_fee_usd: Decimal | None = None
    perp_fee_usd: Decimal | None = None
    funding_pct_h: Decimal | None = None
    leverage: Any = None
    identity: str | None = None
    notes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()       # чего нет в owner.toml для live (показ — только в симуляции)
    sim: bool = False
    ttl_s: int = tconfig.PLAN_TTL_S
    margin_need: Decimal | None = None  # вход: маржа HL под шорт с излишком и резервом …
    margin_avail: Decimal | None = None     # … и доступно в domain нужного dex


def plan(v: SolPlanView) -> str:
    coin, full = escape(v.coin), escape(v.fullcoin)
    ws = [escape(n) for n in v.notes]
    if v.missing and v.sim:
        ws.append(f"для live не задано: {V._labels(v.missing)}")
    lev = f" · плечо {escape(str(v.leverage))}x" if v.leverage is not None else ""
    if v.kind == "entry":
        head = f"<b>Вход {coin} · {V._leg_num(v.usdc)} USDC</b>"
        body = [f"Спот Solana → ≈ {V.tok(v.tokens)} {coin} (мин. {V.tok(v.tokens_min)}) · {route(v.path)}",
                f"Шорт Hyperliquid {full}: {V.tok(v.perp_qty)} по ≈ {V.px(v.perp_px)}{lev}"]
        if V._d(v.margin_need) is not None and V._d(v.margin_avail) is not None:
            body.append(f"Маржа HL ≈ {V.num(v.margin_need)} из {V.num(v.margin_avail)} USDC")
    else:
        head = f"<b>Выход {coin} · всё</b>"
        body = [f"Продать {V.tok(v.tokens)} {coin} → ≈ {V.num(v.usdc)} USDC (мин. {V.num(v.usdc_min)}) · "
                f"{route(v.path)}", f"Откупить {V.tok(v.perp_qty)} {full} по ≈ {V.px(v.perp_px)}"]
    body.append(f"Курсовой {_bps(v.basis_gross_bps)} · с издержками {_bps(v.basis_bps)}")
    fees = V._sum(v.spot_fee_usd, v.perp_fee_usd)
    body.append(f"Издержки ≈ {V.money(fees)} (спот {V.money(v.spot_fee_usd)} · перп {V.money(v.perp_fee_usd)})")
    if v.others:
        body.append("Прочие маршруты: " + "; ".join(escape(o) for o in v.others))
    if v.identity:
        body.append(escape(v.identity))
    if v.kind == "entry" and v.funding_pct_h is not None:
        body.append(f"Фандинг {V.pct(v.funding_pct_h, 3, sign=True)}/ч")
    body.append(f"⏱ {int(v.ttl_s)} с")
    return V._compose("📝", head, ws, body, v.sim)


@dataclass(frozen=True)
class SolProgressView:
    kind: str                           # entry | exit
    coin: str
    fullcoin: str
    stage: str                          # swap — своп отправлен, жду finalized; hedge — спот получен, иду на перп
    path: str | None = None
    tokens: Decimal | None = None       # получено / продано по чеку
    perp_qty: Decimal | None = None     # сколько шорта продать / откупить
    sim: bool = False


def progress(v: SolProgressView) -> str:
    coin, full = escape(v.coin), escape(v.fullcoin)
    head = f"<b>{'Вход' if v.kind == 'entry' else 'Выход'} {coin}</b>"
    if v.stage == "swap":
        line = f"Своп {route(v.path)} отправлен — жду finalized"
    elif v.kind == "entry":
        line = f"Получено {V.tok(v.tokens)} {coin} — шорт {full} {V.tok(v.perp_qty)}"
    else:
        line = f"Продано {V.tok(v.tokens)} {coin} — откуп {full} {V.tok(v.perp_qty)}"
    return V._compose("⏳", head, [], [line], v.sim)


@dataclass(frozen=True)
class SolFinalView:
    kind: str                           # entry | exit
    coin: str
    fullcoin: str
    deal_id: str
    state: str                          # состояние сделки после
    tokens: Decimal | None = None       # куплено / продано
    usdc: Decimal | None = None         # списано / получено
    path: str | None = None
    perp_qty: Decimal | None = None
    perp_px: Decimal | None = None
    basis_bps: Decimal | None = None
    net_sol: Decimal | None = None      # сеть Solana, SOL (безвозвратно)
    net_usd: Decimal | None = None
    perp_fee_usd: Decimal | None = None
    rest_tokens: Decimal | None = None  # выход: остаток спота сделки
    rest_usd: Decimal | None = None
    dust: bool = False
    hedged: bool | None = None
    sim: bool = False
    warn: tuple[str, ...] = ()          # ⚠️ — только отклонения (голая нога дольше лимита)
    signature: str | None = None        # подпись свопа Solana → Solscan
    hl_hashes: tuple[str, ...] = ()     # хэши fills HL → обозреватель HL
    pnl_usdc: Decimal | None = None     # выход, сделка закрыта: итог сделки (потоки, комиссии, сеть, фандинг)
    pnl_complete: bool = True           # учёт полон (fills и фандинг HL добраны)


def final(v: SolFinalView) -> str:
    coin, full, did = escape(v.coin), escape(v.fullcoin), escape(v.deal_id)
    spx = (V._d(v.usdc) / V._d(v.tokens)) if (V._d(v.usdc) is not None and V._d(v.tokens)) else None
    net = f"Сеть {V.num(v.net_sol, 6)} SOL ≈ {V.money(v.net_usd)} · перп ≈ {V.money(v.perp_fee_usd)}"
    lk = links(v.signature, v.hl_hashes)
    if v.kind == "entry":
        head = f"<b>Вход {coin} выполнен</b>" + (" · ноги ровно ✓" if v.hedged else "")
        body = [f"Спот {V.tok(v.tokens)} {coin} за {V.num(v.usdc)} USDC ({V.px(spx)}) · {route(v.path)}",
                f"Шорт {V.tok(v.perp_qty)} {full} по {V.px(v.perp_px)} · курсовой {_bps(v.basis_bps)}", net, lk,
                f"<code>выход {did}</code>"]
        ws = [escape(x) for x in v.warn]
        return V._compose("⚠️" if ws else "✅", head, ws, body, v.sim)
    closed = v.state == "CLOSED"
    head = f"<b>Выход {coin} · сделка закрыта</b>" if closed else f"<b>Выход {coin}: остаток в сделке</b>"
    body = [f"Продано {V.tok(v.tokens)} {coin} → {V.num(v.usdc)} USDC ({V.px(spx)}) · {route(v.path)}",
            f"Откуплено {V.tok(v.perp_qty)} {full} по {V.px(v.perp_px)}", net]
    if closed and V._d(v.pnl_usdc) is not None:
        body.append(f"Итог сделки {_usdc(v.pnl_usdc, sign=True)}" + ("" if v.pnl_complete else
                                                                     " · учёт HL догружается"))
    if V._d(v.rest_tokens):
        body.append(f"Осталось {V.tok(v.rest_tokens)} {coin} ≈ {V.about(v.rest_usd)}" + (" — пыль" if v.dust else ""))
    body.append(lk)
    if not closed:
        body.append(f"<code>выход {did}</code>")
    ws = [escape(x) for x in v.warn]
    return V._compose("✅" if closed and not ws else "⚠️", head, ws, body, v.sim)


def restart(v: "V.RestartView") -> str:
    """Перезапуск посреди входа/выхода связки: как views.restart, но голая нога — открытая экспозиция и только
    «дохедж»/«выход» («откат» в пилоте выключен)."""
    if v.matched is True and v.hedged is False and v.state not in ("ABORTED", "CLOSED"):
        coin, did = escape(v.coin or v.intent_id), escape(v.deal_id or v.intent_id)
        what = "входа" if v.kind == "entry" else "выхода"
        return V._compose("♻️", f"<b>Перезапуск во время {what} {coin}: открытая экспозиция</b>",
                          [_naked(v.delta, v.delta_usd, coin)], ["Сам не продолжаю", *_fix_cmds(did)], v.sim)
    return V.restart(v)


def restart_check(coin: str, deal_id: str, matched: bool | None, detail: str | None, state: str,
                  sim: bool = False, *, hedged: bool | None = None, delta: Any = None, usd: Any = None,
                  step: Any = None, m: Any = None) -> str:
    """Сверка после перезапуска сделки связки: голая нога — открытая экспозиция без «откат»; прочее — как у BSC."""
    if matched is True and hedged is False and V._d(delta) is not None:
        c, did = escape(coin), escape(deal_id)
        st = escape(V.DEAL_STATE_LABEL.get(str(state), str(state)))
        return V._compose("⚠️", f"<b>{c}</b>: сверено после перезапуска · {st} · открытая экспозиция",
                          [_naked(delta, usd, c)], _fix_cmds(did), sim)
    return V.restart_check(coin, deal_id, matched, detail, state, sim, hedged=hedged, delta=delta, usd=usd, step=step,
                           m=m)


def _naked(delta: Any, usd: Any, coin: str) -> str:
    d, u = V._d(delta), V._d(usd)
    if d is None:
        return "Ноги не ровно"
    side = "спота" if d > 0 else "шорта"
    return f"Без хеджа {V.tok(abs(d))} {side} {coin}" + (f" ≈ {V.about(abs(u))}" if u else "")


def _fix_cmds(did: str) -> list[str]:
    return [f"<code>дохедж {did}</code> — выровнять перп по книге сделки", f"<code>выход {did}</code> — закрыть всё"]


@dataclass(frozen=True)
class SolHaltView:
    kind: str                           # entry | exit | rehedge
    coin: str
    deal_id: str
    intent_id: str
    reason: str
    state: str | None = None
    wallet_tokens: Decimal | None = None
    perp_pos: Decimal | None = None     # позиция HL со знаком; None — не прочитана
    delta: Decimal | None = None        # + спот без хеджа, − шорт без спота (токены); None — книга неизвестна
    delta_usd: Decimal | None = None
    need_qty: Decimal | None = None     # дохедж: сколько продать (+) / откупить (−) на перпе
    sim: bool = False


def halt(v: SolHaltView) -> str:
    coin, did = escape(v.coin), escape(v.deal_id)
    what = {"entry": "Вход", "exit": "Выход"}.get(v.kind, "Дохедж")
    why = V._cap(escape(v.reason))
    if v.state == "ABORTED":
        return V._compose("⛔", f"<b>{what} {coin} не начат</b>", [], [why, "Ничего не куплено — сделка снята"], v.sim)
    legs = (f"Спот {V.tok(v.wallet_tokens, True) if v.wallet_tokens is not None else 'не прочитан'} · шорт "
            f"{V.tok(v.perp_pos, True) if v.perp_pos is not None else 'не прочитан'}")
    d = V._d(v.delta)
    if d is None:
        return V._compose("🛑", f"<b>{coin}: исход неизвестен</b>", [],
                          [f"{what} встал: {escape(v.reason)}", legs,
                           "Новых отправок нет — <code>позиции</code> выяснит"], v.sim)
    if v.state == "HALTED_MISMATCH":
        return V._compose("🛑", f"<b>{coin}: {what.lower()} остановлен, расхождение</b>", [],
                          [why, legs, "Сначала <code>позиции</code>, потом команда"], v.sim)
    if d != 0:
        side = "спота" if d > 0 else "шорта"
        q = V._d(v.need_qty)
        what_fix = (f"шорт ещё {V.tok(q)}" if q is not None and q > 0 else
                    f"откупить {V.tok(abs(q))}" if q is not None and q < 0 else "по книге")
        fix = f"<code>дохедж {did}</code> — {what_fix}"
        return V._compose("🛑", f"<b>{coin}: открытая экспозиция — без хеджа {V.tok(abs(d))} {side} ≈ "
                                f"{V.about(v.delta_usd)}</b>", [],
                          [f"{what} встал: {escape(v.reason)}", legs, fix,
                           f"<code>выход {did}</code> — закрыть всё"], v.sim)
    return V._compose("⏸", f"<b>{coin}: {what.lower()} на паузе</b>", [],
                      [why, "Ноги ровно ✓", f"<code>выход {did}</code>"], v.sim)
