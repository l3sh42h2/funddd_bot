"""Учёт и оценка сделки связки Solana × Hyperliquid (ТЗ §13; приёмка H15–H17, G03, G10, G11, S17, V04, V05).

Накопительная книга СДЕЛКИ (не кошелька и не отдельного выхода): каждое событие — ровно один раз.
  спот-поток  = USDC получено на выходах − USDC списано на входах          (фактические суммы finalized-чеков клипов)
  перп-поток  = Σ продаж − Σ покупок                                         (cum_quote исполненных заявок сделки)
  комиссии HL = Σ fee фактических fills по cloid заявок сделки (builderFee уже внутри fee); заявка, чьи fills ещё не
                добраны, — оценкой по применимой ставке, флаг fees_est (факт заменяет оценку, а не прибавляется: H15)
  сеть Solana = fee_events сделки: безвозвратные статьи в SOL (meta.fee чека уже содержит base+priority — оценка
                priority второй раз не прибавляется, S17), не included и не superseded; возвратный rent — отдельно
                (заблокированный капитал, не расход: S18). Неизвестная сумма — None, не 0.
  фандинг     = Σ фактических userFunding счёта по fullcoin в окне сделки (дедуп по ключу API: H16); прогноз сюда не
                попадает
  база        = спот-поток + перп-поток − комиссии HL − сеть·цена SOL + фандинг

PnL при выходе (G11) = база + чистая выручка котировки token→USDC на весь остаток (внешние расходы выхода — по одной
цене SOL; встроенные в выход не вычитаются) − откуп шорта по асксам HL − комиссия откупа по ставке. Стакан мельче шорта
или котировки нет — pnl_exit None с причиной (не последний уровень «до бесконечности»). PnL сейчас — та же база плюс
остаток по цене этой котировки и шорт по миду HL (оценка, флаг px_src).

Денежные единицы — USDC (флаг unit): 1 USDC не объявляется точным USD; SOL переводится в USDC по одной цене с
источником и временем (флаг sol_px). Добор fills/фандинга HL — постранично с перекрытием, отметка курсора — после
записи строк; неполная история — флаг, итог не называется окончательным (V04).
"""
from __future__ import annotations
import json, logging, time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping
from .ledger_flows import spot_quote_flows, filled_orders
from . import store
from .keys import redact

log = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)
FLOW_CLIPS = ("DEX_OK", "PERP_SENT", "BALANCED", "HEDGE_DEFICIT")     # своп состоялся
FILLED = ("FILLED", "PARTIALLY_FILLED")
INGEST_OVERLAP_MS = 15 * 60 * 1000       # перекрытие добора: тот же ключ второй раз не пишется (дедуп по ключу API)
FEE_TOKENS = frozenset({"USDC"})         # комиссия fill в валюте расчётов perp-ledger (иное — не складываем как USDC)
NONREFUNDABLE = frozenset({"network_total", "network_base", "network_priority", "tip", "rent_nonrefundable"})
BOOK_LEVELS = 20


def _dv(x: Any) -> D | None:
    if x is None or (isinstance(x, str) and not x.strip()):
        return None
    try:
        d = x if isinstance(x, D) else D(str(x))
    except (ArithmeticError, ValueError):
        return None
    return d if d.is_finite() else None


def inst_of(deal: Mapping) -> dict:
    """Замороженная спецификация сделки (deals.inst_json schema 2) словарём; нет или битая — {}."""
    try:
        d = json.loads(dict(deal).get("inst_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def scope_of(deal: Mapping) -> tuple[str, str, str] | None:
    """(сеть HL, счёт — нижним регистром, fullcoin) из perp_account спецификации
    «hyperliquid:<сеть>:<мастер>:<счёт>:<dex>». Нет — None (учёт HL сделки не читается)."""
    inst = inst_of(deal)
    parts = str(inst.get("perp_account") or "").split(":")
    if len(parts) != 5 or parts[0] != "hyperliquid" or not parts[3]:
        return None
    return parts[1], parts[3].lower(), str(inst.get("perp_symbol") or dict(deal).get("symbol"))


def window_ms(deal: Mapping) -> tuple[int, int | None]:
    """Окно сделки на счёте: от создания до закрытия (одна незакрытая сделка на scope — фандинг окна её)."""
    d = dict(deal)
    start = int(float(d["created"]) * 1000)
    end = int(float(d["updated"]) * 1000) if d.get("state") in ("CLOSED", "ABORTED") and d.get("updated") else None
    return start, end


def _cursor_scope(sc: tuple[str, str, str], kind: str) -> str:
    return f"hl:{sc[0]}:{sc[1]}:{sc[2]}:{kind}"


def ingest(con, deal: Mapping, perp, *, now: float | None = None) -> dict:
    """Добор фактических fills и userFunding счёта по fullcoin сделки (только чтение HL). Возвращает
    {"fills": полно?, "funding": полно?, "errors": [...]}; сбой — текстом, не исключением (учёт догрузится)."""
    out: dict = {"fills": None, "funding": None, "errors": []}
    sc = scope_of(deal)
    if sc is None:
        out["errors"].append("scope счёта HL сделки неизвестен")
        return out
    start, _end = window_ms(deal)
    asked_ms = int((time.time() if now is None else now) * 1000)     # полная страница = история полна до момента запроса
    for kind, fn_name, add in (("fills", "fills_since", store.add_hl_fills),
                               ("funding", "funding_since", store.add_hl_funding)):
        fn = getattr(perp, fn_name, None)
        if fn is None:
            out["errors"].append(f"{kind}: нога HL без истории")
            continue
        scope = _cursor_scope(sc, kind)
        cur = store.get_cursor(con, scope)
        since = start
        if cur is not None and cur["watermark_ms"] is not None:
            since = max(start, int(cur["watermark_ms"]) - INGEST_OVERLAP_MS)
        try:
            page = fn(sc[2], since)
            add(con, page.rows, now=now)                     # сначала строки, потом отметка курсора
            wm = max(page.last_time or 0, asked_ms) if page.complete else None
            store.set_cursor(con, scope, watermark_ms=wm if wm is not None else (None if cur is None else
                                                                                 cur["watermark_ms"]),
                             complete=bool(page.complete), gap=page.gap, now=now)
            out[kind] = bool(page.complete)
        except Exception as e:           # noqa — учёт не роняет сверку и оценку
            out["errors"].append(f"{kind}: {type(e).__name__}: {redact(e)[:120]}")
            out[kind] = False
    return out


def our_cloids(con, sc: tuple[str, str, str] | None, deal_id: str | None = None) -> dict[str, str | None]:
    """cloid → deal_id наших заявок (журнал адаптера HL): scope счёта/fullcoin; deal_id — только этой сделки."""
    if sc is None:
        return {}
    sql = "SELECT cloid, deal_id, client_id FROM hl_order_attempts WHERE network=? AND lower(account)=? AND fullcoin=? " \
          "AND cloid IS NOT NULL"
    rows = con.execute(sql, sc).fetchall()
    out = {}
    for cloid, did, cid in rows:
        d = did or (str(cid).split("-")[1] if str(cid).startswith("fb-") and str(cid).count("-") >= 2 else None)
        if deal_id is None or d == deal_id:
            out[str(cloid)] = d
    return out


# --- книга сделки ----------------------------------------------------------------------------------------------
@dataclass
class Ledger:
    quote_dec: int
    spot_in: D = ZERO
    spot_out: D = ZERO
    perp_sell: D = ZERO
    perp_buy: D = ZERO
    perp_fee: D | None = ZERO            # факт fills + оценка недобранного; None — ставка неизвестна
    fees_est: bool = False
    funding: D | None = None             # None — симуляция (фандинг не начисляется)
    funding_n: int = 0
    funding_complete: bool | None = None
    fills_complete: bool | None = None
    network_lamports: int | None = 0     # None — есть статья с неизвестной суммой
    rent_locked_lamports: int | None = 0
    spot_ext_usdc: D = ZERO              # внешние расходы спота в USDC (не в потоках токенов)
    other_unknown: tuple = ()            # статьи в активе, который не переводится (оценка неизвестна)
    foreign_fills: tuple = ()            # fills на fullcoin сделки в её окне не нашими заявками (ручные/чужие)
    routes: tuple = ()                   # фактический маршрут каждого клипа (путь провайдера)
    sim: bool = False

    @property
    def spot_flow(self) -> D:
        return self.spot_out - self.spot_in

    @property
    def perp_flow(self) -> D:
        return self.perp_sell - self.perp_buy

    def network_usdc(self, sol_px: D | None) -> D | None:
        if self.network_lamports is None:
            return None
        if self.network_lamports == 0:
            return ZERO
        return None if sol_px is None else D(self.network_lamports) / D(10) ** 9 * sol_px

    def base(self, sol_px: D | None) -> D | None:
        """Реализованная часть: потоки − комиссии HL − сеть + фандинг. Неизвестная статья — None (не 0)."""
        net = self.network_usdc(sol_px)
        if net is None or self.perp_fee is None or self.other_unknown:
            return None
        return self.spot_flow + self.perp_flow - self.perp_fee - net - self.spot_ext_usdc + (self.funding or ZERO)

    @property
    def complete(self) -> bool:
        """Учёт окончателен: fills и фандинг добраны полностью, оценок нет (симуляция — по своему журналу)."""
        if self.sim:
            return self.perp_fee is not None
        return bool(self.fills_complete) and bool(self.funding_complete) and not self.fees_est


def ledger(con, deal: Mapping, *, fee_rate: D | None) -> Ledger:
    """Книга сделки по trade.db (только чтение): годится и трейдеру, и кабинету (соединение read-only)."""
    d = dict(deal)
    did = d["id"]
    inst = inst_of(d)
    qdec = int(inst.get("quote_dec") or 6)
    wallet = None
    L = Ledger(quote_dec=qdec, sim=bool(d.get("sim")))
    flows = spot_quote_flows(con, did, qdec, exit_kinds=("exit",))
    L.spot_in, L.spot_out = flows.debit, flows.credit
    try:
        L.routes = tuple(p for (p,) in con.execute(
            "SELECT a.path FROM sol_tx_attempts a JOIN clips c ON a.clip_ref = CAST(c.id AS TEXT) JOIN intents i ON "
            "i.id = c.intent_id WHERE i.deal_id=? AND a.state IN ('FINALIZED_OK', 'FINALIZED_ERR') ORDER BY a.created",
            (did,)))
    except Exception:                    # noqa — журнала Solana нет (прежняя БД/кабинет старой схемы)
        L.routes = ()
    sc = scope_of(d)
    cloid_of: dict[str, str] = {}
    try:
        cloid_of = {str(cid): str(cl) for cid, cl in con.execute(
            "SELECT client_id, cloid FROM hl_order_attempts WHERE deal_id=? AND cloid IS NOT NULL", (did,))}
        if not cloid_of:
            prefix = f"fb-{did}-"
            cloid_of = {str(cid): str(cl) for cid, cl in con.execute(
                "SELECT client_id, cloid FROM hl_order_attempts WHERE substr(client_id, 1, ?)=? AND cloid IS NOT NULL",
                (len(prefix), prefix))}
    except Exception:                    # noqa
        cloid_of = {}
    fills: dict[str, list] = {}
    if sc is not None and cloid_of and not L.sim:
        try:
            marks = ",".join("?" * len(cloid_of))
            for cl, px, sz, fee, tok in con.execute(
                    f"SELECT cloid, px, sz, fee, fee_token FROM hl_fills WHERE network=? AND account=? AND coin=? AND "
                    f"cloid IN ({marks})", (*sc, *cloid_of.values())):
                fills.setdefault(str(cl), []).append((_dv(px), _dv(sz), _dv(fee), tok))
        except Exception:                # noqa — таблиц учёта нет (кабинет на старой БД)
            fills = {}
    prefix = f"fb-{did}-"
    fee_total: D | None = ZERO
    for o in filled_orders(con, did):
        q = _dv(o["cum_quote"]) or ZERO
        if o["side"] == "SELL":
            L.perp_sell += q
        else:
            L.perp_buy += q
        rows = fills.get(cloid_of.get(str(o["client_id"]), ""), [])
        covered, paid = ZERO, ZERO
        for px, sz, fee, tok in rows:
            if None in (px, sz, fee) or (tok or "").upper() not in FEE_TOKENS:
                covered = None
                break
            covered += px * sz
            paid += abs(fee)
        if covered is None:
            rest = q
            paid = ZERO
        else:
            rest = q - covered
        if rest > D("0.000001"):
            L.fees_est = True
            if fee_rate is None or fee_total is None:
                fee_total = None
            else:
                fee_total += paid + rest * fee_rate
        elif fee_total is not None:
            fee_total += paid
    L.perp_fee = fee_total
    # сеть и rent — fee_events сделки (чеки finalized; статья одного чека — один раз, G03)
    net: int | None = 0
    rent: int | None = 0
    other = []
    try:
        fee_rows = store.fee_events(con, deal_id=did)
    except Exception:                    # noqa
        fee_rows = []
    for f in fee_rows:
        if f["included"] or f["superseded"]:
            continue
        amt = None if f["amount_raw"] is None else int(f["amount_raw"])
        if f["refundable"]:              # депозит/возврат rent — заблокированный капитал, не расход (S18)
            if amt is None or rent is None:
                rent = None
            else:
                rent += amt if f["kind"] == "rent_deposit" else -amt
            continue
        if f["asset"] == "native:solana" and f["kind"] in NONREFUNDABLE:
            net = None if (amt is None or net is None) else net + amt
        elif f["asset"] == inst.get("quote_mint"):
            if amt is None:
                other.append(f["kind"])
            else:
                L.spot_ext_usdc += D(amt) / D(10) ** int(f["decimals"])
        else:
            other.append(f"{f['kind']}:{f['asset']}")
    L.network_lamports, L.rent_locked_lamports, L.other_unknown = net, rent, tuple(other)
    if not L.sim and sc is not None:
        start, end = window_ms(d)
        try:
            sql = "SELECT usdc FROM hl_funding WHERE network=? AND account=? AND coin=? AND time>=?"
            args: list = [*sc, start]
            if end is not None:
                sql += " AND time<=?"
                args.append(end)
            vals = [_dv(r[0]) for r in con.execute(sql, args)]
            L.funding = sum((v for v in vals if v is not None), ZERO)
            L.funding_n = sum(1 for v in vals if v is not None)
            for kind in ("fills", "funding"):
                c = store.get_cursor(con, _cursor_scope(sc, kind))
                ok = None if c is None else bool(c["complete"]) and (
                    end is None or c["watermark_ms"] is None or int(c["watermark_ms"]) >= end)
                setattr(L, f"{kind}_complete", ok)
            ours = set(our_cloids(con, sc))
            sql = "SELECT tid, cloid FROM hl_fills WHERE network=? AND account=? AND coin=? AND time>=?"
            args = [*sc, start]
            if end is not None:
                sql += " AND time<=?"
                args.append(end)
            L.foreign_fills = tuple(int(t) for t, cl in con.execute(sql, args) if not cl or str(cl) not in ours)
        except Exception:                # noqa — таблиц учёта нет: фандинг неизвестен (не 0)
            L.funding, L.funding_complete, L.fills_complete = None, None, None
    return L


# --- оценка ----------------------------------------------------------------------------------------------------
def pnl_exit_of(base: D, liq_credit: D, close_cost: D, close_fee: D) -> D:
    """PnL при выходе (ТЗ §13, G11). base по потокам = реализованное − стоимость остатка спота + нотионал открытия
    остатка шорта (все комиссии и фандинг уже внутри один раз), поэтому
    pnl_exit = base + чистая выручка продажи остатка (внешний расход выхода уже вычтен) − откуп шорта − комиссия откупа."""
    return base + liq_credit - close_cost - close_fee


def sol_price(legs) -> tuple[D | None, dict | None]:
    """Цена SOL в USDC — одна на оценку, с источником и временем (V05). Нет — None (сеть в USDC неизвестна, не 0)."""
    fn = getattr(legs, "native_obs", None)
    try:
        obs = fn() if fn is not None else None
    except Exception as e:               # noqa
        log.warning("sol: цена SOL: %s", type(e).__name__)
        obs = None
    if obs is None:
        return None, None
    return obs.price, {"price": obs.price, "source": obs.source, "unit": "USDC"}


def exit_request(deal: Mapping, legs, units: int, cfg) -> Any:
    """ExactIn-запрос продажи ВСЕХ токенов сделки (как план выхода): те же mint/программа/кошелёк/допуск."""
    from .owner import SOL_HL
    from .spot_router import AssetRef, QuoteRequest
    from .solana.accounts import ata
    inst = inst_of(deal)
    slip = cfg.get(f"limits.{SOL_HL}.max_spot_slippage_bps") if cfg is not None else None
    if slip is None:
        raise ValueError(f"не задан limits.{SOL_HL}.max_spot_slippage_bps — котировку выхода не запрашиваю")
    dl = cfg.get(f"limits.{SOL_HL}.collection_deadline_ms")
    tok = AssetRef(inst["token"], inst["token_program"], int(inst["token_dec"]), str(inst.get("perp_base_asset") or ""))
    q = AssetRef(inst["quote_mint"], inst["quote_program"], int(inst["quote_dec"]), "USDC")
    w = legs.wallet
    rent = []
    for a in (q, tok):
        try:
            rent.append((a.mint, legs.spot.account_rent(a.mint, a.program, tuple(inst.get("token_extensions") or ())
                                                        if a is tok else ())))
        except Exception:                # noqa
            rent.append((a.mint, None))
    span = float(dl) / 1000 if dl is not None else 10.0
    return QuoteRequest(side="exit", input=tok, output=q, amount_in_raw=int(units), wallet=w, slippage_bps=int(slip),
                        genesis_hash=inst["genesis_hash"], deadline_mono=legs.router.clock() + span, purpose="mark",
                        input_account=ata(w, tok.mint, tok.program), output_account=ata(w, q.mint, q.program),
                        account_rent=tuple(rent))


def exit_quote(legs, req, prices: Mapping) -> tuple[tuple[D, D, str] | None, list[str]]:
    """Лучшая чистая выручка продажи req.amount_in_raw токенов среди котировок провайдеров (лёгкий запрос: OKX
    /quote, Jupiter Order-превью). Годится только ответ на ТОТ ЖЕ запрос (R01); внешний расход неизвестен — кандидат
    не годится. Возвращает ((валовый выход USDC, чистый USDC, путь) | None, причины)."""
    from .fees import value_external
    from .spot_router import SwapCandidate
    best, why = None, []
    for p in getattr(legs.router, "providers", ()):
        fn = getattr(p, "quote", None) or getattr(p, "order", None) or getattr(p, "candidates", None)
        if fn is None:
            continue
        try:
            got = fn(req)
        except Exception as e:           # noqa — провайдер не ответил: причина, а не ноль
            why.append(f"{getattr(p, 'group', '?')}: {type(e).__name__}")
            continue
        for c in (got if isinstance(got, (list, tuple)) else [got]):
            if not isinstance(c, SwapCandidate):
                why.append(f"{getattr(c, 'path', '?')}: {getattr(c, 'reason', 'недоступен')}")
                continue
            if (c.side, c.request_hash, int(c.amount_in_raw), c.input_mint, c.output_mint) != (
                    "exit", req.request_hash, int(req.amount_in_raw), req.input.mint, req.output.mint) \
                    or not c.expected_out_raw or c.expected_out_raw <= 0:
                why.append(f"{c.path}: ответ не на этот запрос")
                continue
            v = value_external(c.fees, wallet=req.wallet, unit=req.output.mint, prices=prices,
                               now_mono=legs.router.clock(), max_price_age_ms=None)
            if v.total is None:
                why.append(f"{c.path}: расход выхода неизвестен ({', '.join(v.unknown)})")
                continue
            gross = D(int(c.expected_out_raw)) / D(10) ** int(req.output.decimals)
            net = gross - v.total
            if best is None or net > best[1]:
                best = (gross, net, c.path)
    return best, why


def mark(con, deal: Mapping, legs, *, now: float, cfg=None, busy: Callable[[], bool] | None = None,
         fetch_history: bool = True):
    """Оценка сделки связки (только чтение сети) → marks.Mark той же схемы, что у BSC (deal_marks)."""
    from . import marks as M
    from .engine import deal_book
    from .planner import mid, walk
    gate = lambda: M._gate(busy)                # noqa: E731
    d = dict(deal)
    sim = bool(d["sim"])
    m = M.Mark(d["id"], float(now), flags={"unit": "USDC", "profile": "sol_best_hyperliquid"})
    if sim:
        m.flags["sim"] = True
    if legs is None:
        m.err("ноги связки не собраны — не посчитано")
        return m
    bk = deal_book(con, d["id"])
    if not bk.known:
        m.err(f"книга сделки неизвестна: {bk.why}")
        return m
    dec = int(d["token_dec"])
    q_tok, q_short = bk.tokens(dec), bk.short
    m.flags.update(q_tok=q_tok, q_short=q_short)
    if not sim and fetch_history:
        gate()
        got = ingest(con, d, legs.perp, now=now)
        for e in got["errors"]:
            m.err(e)
    rate = None
    try:
        rate = legs.fee_rate()
    except Exception:                   # noqa
        rate = None
    L = ledger(con, d, fee_rate=rate)
    gate()
    sol_px, src = sol_price(legs)
    if src is not None:
        m.flags["sol_px"] = {**src, "ts": float(now)}
    m.fees = L.perp_fee
    m.gas = L.network_usdc(sol_px)
    m.funding = L.funding if L.funding is not None else ZERO
    if L.fees_est:
        m.flags["fees_est"] = True
    if m.gas is None:
        m.err("сеть Solana в USDC неизвестна (цена SOL или сумма статьи)")
    if L.other_unknown:
        m.err("расход в неучтённом активе: " + ", ".join(L.other_unknown))
    if not sim:
        if not L.funding_complete:
            m.flags["funding_incomplete"] = True
        if not L.fills_complete:
            m.flags["fills_incomplete"] = True
        if L.foreign_fills:
            m.flags["foreign_fills"] = len(L.foreign_fills)
    if L.rent_locked_lamports:
        m.flags["rent_locked_lamports"] = L.rent_locked_lamports
    m.flags["accounting_complete"] = bool(L.complete and m.gas is not None)
    base = L.base(sol_px)
    # котировка выхода на весь остаток (одна на проход) и асксы HL на весь шорт
    exit_q = None
    units = int(bk.tokens_raw)
    if units > 0:
        gate()
        try:
            req = exit_request(d, legs, units, cfg)
            prices = {}
            obs = legs.native_obs() if getattr(legs, "native_obs", None) else None
            if obs is not None:
                prices["native:solana"] = obs
            exit_q, why = exit_quote(legs, req, prices)
            if exit_q is None:
                m.err("котировки продажи остатка нет: " + ("; ".join(why)[:160] or "—"))
        except Exception as e:           # noqa
            m.err(f"котировка выхода: {type(e).__name__}: {redact(e)[:120]}")
    elif units == 0:
        exit_q = (ZERO, ZERO, None)
    gate()
    book = None
    try:
        book = legs.perp.book(d["symbol"], BOOK_LEVELS)
    except Exception as e:               # noqa
        m.err(f"стакан HL: {type(e).__name__}: {redact(e)[:120]}")
    m.px_perp = mid(book) if book is not None else None
    close = None
    if q_short == 0:
        close = (ZERO, ZERO)
    elif book is not None:
        got, quote, _last = walk(book.asks, q_short)
        if got < q_short:
            m.flags["uncovered"] = q_short - got
            m.err(f"асков HL {got} меньше шорта {q_short} — выход не оценён")
        elif rate is None:
            m.err("ставка HL неизвестна — комиссия откупа не оценена")
        else:                            # цена HL — за единицу размера перпа: сумма уже в USDC
            close = (quote, quote * rate)
    if exit_q is not None and units > 0:
        m.px_dex = exit_q[0] / q_tok
        m.flags["px_src"] = "exit_quote"
        if exit_q[2]:
            m.flags["exit_path"] = exit_q[2]
    spot_val = ZERO if units == 0 else (exit_q[0] if exit_q is not None else None)
    perp_val = ZERO if q_short == 0 else (q_short * m.px_perp if m.px_perp is not None else None)
    if base is not None and spot_val is not None and perp_val is not None:
        m.pnl_now = base + spot_val - perp_val
    if base is not None and exit_q is not None and close is not None:
        m.pnl_exit = pnl_exit_of(base, exit_q[1], close[0], close[1])
        if m.pnl_now is not None:
            m.exit_cost = m.pnl_now - m.pnl_exit
            m.flags["exit"] = {"dex": exit_q[0] - exit_q[1], "book": close[0] - (perp_val or ZERO), "fee": close[1]}
    if not sim:
        gate()
        try:
            pos = legs.perp.position(d["symbol"])
        except Exception:               # noqa
            pos = None
        if pos is None:
            m.err("позиция HL не прочитана")
        elif pos != -q_short:
            m.flags["position"] = pos
        if q_short != 0:
            try:
                det = legs.perp.acct.position_detail() if getattr(legs.perp, "acct", None) else None
                mk = legs.perp.funding(d["symbol"])[0]
                liq = _dv((det or {}).get("liquidationPx"))
                m.flags["liq"] = {"price": liq if liq is not None and liq > 0 else None,
                                  "mark": mk if mk is not None and mk > 0 else None, "ts": m.ts}
            except Exception as e:      # noqa
                m.err(f"ликвидация HL: {type(e).__name__}")
        else:
            m.flags["liq"] = {"price": None, "mark": None, "ts": m.ts}
    return m


def final_mark(con, deal: Mapping, legs, *, now: float, fetch_history: bool = True):
    """Итог закрытой сделки: база по журналу (потоки, комиссии HL, сеть, фандинг) — строка с flags.final; учёт
    неполон — flags.accounting_complete=False (следующий проход пересчитает с новой ревизией)."""
    from . import marks as M
    d = dict(deal)
    m = M.Mark(d["id"], float(now), flags={"final": True, "unit": "USDC", "profile": "sol_best_hyperliquid"})
    if d["sim"]:
        m.flags["sim"] = True
    if legs is not None and not d["sim"] and fetch_history:
        got = ingest(con, d, legs.perp, now=now)
        for e in got["errors"]:
            m.err(e)
    rate = None
    if legs is not None:
        try:
            rate = legs.fee_rate()
        except Exception:               # noqa
            rate = None
    L = ledger(con, d, fee_rate=rate)
    sol_px, src = sol_price(legs) if legs is not None else (None, None)
    if src is not None:
        m.flags["sol_px"] = {**src, "ts": float(now)}
    m.fees, m.funding = L.perp_fee, (L.funding if L.funding is not None else ZERO)
    m.gas = L.network_usdc(sol_px)
    if L.fees_est:
        m.flags["fees_est"] = True
    m.flags["accounting_complete"] = bool(L.complete and m.gas is not None)
    m.pnl_now = L.base(sol_px)
    return m


def realized_view(con, deal: Mapping) -> dict | None:
    """Итог закрытой сделки для кабинета без сети: потоки − комиссии HL (факт/оценка) + фандинг; сеть Solana в
    лампортах отдельно (цены SOL у кабинета нет — так и подписано)."""
    try:
        L = ledger(con, deal, fee_rate=None)
    except Exception as e:               # noqa
        log.warning("кабинет: итог %s: %s", dict(deal).get("id"), type(e).__name__)
        return None
    if L.perp_fee is None:
        return {"final": True, "total": None, "no_gas": True, "unit": "USDC"}
    total = L.spot_flow + L.perp_flow - L.perp_fee - L.spot_ext_usdc + (L.funding or ZERO)
    return {"final": True, "total": total, "no_gas": bool(L.network_lamports), "unit": "USDC",
            "incomplete": not L.complete}


def funding_rows(con, deal: Mapping) -> list[tuple[float, D]]:
    """Выплаты фандинга HL сделки (факт userFunding) — (время с, сумма USDC) по возрастанию."""
    sc = scope_of(deal)
    if sc is None:
        return []
    start, end = window_ms(deal)
    sql = "SELECT time, usdc FROM hl_funding WHERE network=? AND account=? AND coin=? AND time>=?"
    args: list = [*sc, start]
    if end is not None:
        sql += " AND time<=?"
        args.append(end)
    try:
        rows = con.execute(sql + " ORDER BY time, hash", args).fetchall()
    except Exception:                    # noqa — таблицы нет (старая схема)
        return []
    return [(int(t) / 1000, v) for t, u in rows if (v := _dv(u)) is not None]
