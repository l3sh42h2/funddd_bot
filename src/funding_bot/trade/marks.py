"""Оценка сделки для владельца: «PnL сейчас» и «PnL при выходе» (кабинет /cabinet и «позиции» в Telegram).

Кабинет — публичный веб-процесс и в биржи не ходит, поэтому считает трейдер (tg/bot.py, поток заданий, раз в
tconfig.MARK_S; во время исполнения — пропуск) и пишет строку в trade.db → deal_marks; кабинет её только читает.
Сеть здесь только ЧИТАЕТСЯ: котировка OKX, стакан и positionRisk перпа, начисления фандинга, allowance токена. Ни одной
отправки; в БД — своя строка оценки и добор начислений фандинга (как engine._deal_pnl).

Формулы (Decimal, $ = USDT; потоки — по журналу ВСЕХ намерений сделки):
  спот-поток = USDT получено на выходах/откатах − USDT потрачено на входах      (чеки клипов, как engine._deal_pnl)
  перп-поток = Σ продаж − Σ покупок                                             (cum_quote исполненных заявок сделки)
  комиссии   = userTrades (commission_abs в стейблах); оборот заявки, не покрытый ими (userTrades не добраны или
               комиссия не в стейблах — скидка BNB), — × тариф (как orders_as_fills), флаг fees_est
  газ        = Σ gas_used × effectiveGasPrice транзакций сделки × цена BNB     (report.gas_totals по engine.intent_txs)
  фандинг    = Σ income FUNDING_FEE с открытия сделки (плюс — получили)
  база       = спот-поток + перп-поток − комиссии − газ + фандинг

  PnL сейчас     = база + Q_tok·P_dex − Q_short·P_perp
                   P_dex — середина пула: (покупка spot.pool_price + продажа) / 2 по малым котировкам ~OKX_DEX_QUOTE_USD
                   (pool_price — цена ПОКУПКИ, комиссия пула сверху: длинный спот по ней завышен на комиссию),
                   P_perp — мид стакана перпа
  PnL при выходе = база + (USDT по ОДНОЙ котировке OKX на продажу всех Q_tok − газ свопа из котировки − газ approve,
                           если allowance токена к spender OKX меньше Q_tok)
                        − (откуп Q_short проходом по асксам стакана + комиссия тейкера на эту сумму)
  цена выхода    = PnL сейчас − PnL при выходе = удар DEX + стакан + комиссия выхода + газ выхода (разбивка — во flags)

  до ликвидации  = liquidationPrice / markPrice − 1 для шорта (ликвидация ВЫШЕ марка; плюс — запас) — кабинет считает
                   по flags.liq = {price, mark, ts} из positionRisk того же прохода (правка владельца 13.09)

Q_tok и Q_short — книга сделки по журналу (engine.deal_book), не кошелёк: свои токены владельца сделку не касаются.
Стакан мельче шорта — остаток по худшему уровню и флаг uncovered. Неизвестное остаётся None («—»), никогда не 0; ошибка
чтения — текст во flags (redact), трейдер не падает. Симуляция — те же формулы на ногах симуляции (публичные данные);
фандинг там не начисляется и газ в журнал не пишется — флаг sim.
"""
from __future__ import annotations
import json, logging
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from .. import config
from . import report, store, tconfig
from .keys import redact

log = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)
WEI = D(10) ** 18
FLOW_CLIPS = ("DEX_OK", "PERP_SENT", "BALANCED", "HEDGE_DEFICIT")     # своп состоялся (как engine._deal_pnl)
FILLED = ("FILLED", "PARTIALLY_FILLED")
FEE_TOL = D("0.01")                     # оборот заявки, не покрытый userTrades, меньше цента — округление, не оценка
COLS = store.MARK_COLS


def _dv(x: Any) -> D | None:
    """TEXT/float/Decimal → Decimal; пусто, битое и бесконечное — None."""
    if x is None or isinstance(x, bool) or (isinstance(x, str) and not x.strip()):
        return None
    try:
        d = x if isinstance(x, D) else D(repr(x) if isinstance(x, float) else str(x))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def stable_of(chain: str) -> tuple[str, int]:
    s, dec = config.OKX_DEX_STABLES[tconfig.chain_index(chain)]
    return s, int(dec)


# --- потоки по журналу (и трейдер, и кабинет: только чтение БД) -----------------------------------------------------
@dataclass
class Journal:
    spot_in: D                  # USDT потрачено на входах
    spot_out: D                 # USDT получено на выходах и откатах
    perp_sell: D                # Σ cum_quote продаж перпа
    perp_buy: D                 # Σ cum_quote покупок перпа
    fees: D                     # комиссии перпа, $
    fees_est: bool              # хоть одна заявка — оценкой по тарифу (userTrades не добраны)
    funding: D | None           # фандинг: + получили, − заплатили; None — симуляция (не начисляется)
    gas_native: D               # газ всех транзакций сделки в нативной монете

    @property
    def spot_flow(self) -> D:
        return self.spot_out - self.spot_in

    @property
    def perp_flow(self) -> D:
        return self.perp_sell - self.perp_buy

    def gas_usd(self, native_px: D | None) -> D | None:
        """Газ в $: газа не было — 0; был, а цена BNB неизвестна — None (не 0)."""
        if self.gas_native == 0:
            return ZERO
        return None if native_px is None else self.gas_native * native_px


def deal_txs(con, deal_id: str) -> list[dict]:
    """Транзакции сделки: engine.intent_txs по всем её намерениям (свопы клипов и approve по событию)."""
    from .engine import intent_txs
    seen: dict[int, dict] = {}
    for (iid,) in con.execute("SELECT id FROM intents WHERE deal_id=?", (deal_id,)).fetchall():
        for t in intent_txs(con, iid):
            seen[int(t["id"])] = t
    return [seen[k] for k in sorted(seen)]


def journal(con, deal: Mapping, *, until_ms: int | None = None) -> Journal:
    """Денежные потоки сделки по trade.db. until_ms — верхняя граница начислений фандинга (закрытая сделка: следующая
    сделка на том же символе не должна дописать свой фандинг в её итог)."""
    did = deal["id"]
    _stable, sdec = stable_of(deal["chain"])
    s_in = s_out = ZERO
    qs = ",".join("?" * len(FLOW_CLIPS))
    for r in con.execute(f"SELECT c.dex_in, c.dex_out, i.kind FROM clips c JOIN intents i ON c.intent_id = i.id "
                         f"WHERE i.deal_id=? AND c.state IN ({qs})", (did, *FLOW_CLIPS)):
        if r["kind"] == "entry":
            s_in += D(int(r["dex_in"] or 0)) / D(10) ** sdec
        elif r["kind"] in ("exit", "undo"):
            s_out += D(int(r["dex_out"] or 0)) / D(10) ** sdec
    prefix = f"fb-{did}-"
    # userTrades по заявке: [комиссия в стейблах, оборот, который она покрывает, оборот сделки неизвестен]. Комиссия не
    # в стейблах (скидка BNB) в $ не переводится, недобранные userTrades дают неполную сумму — непокрытый оборот ниже
    # оценивается по тарифу (fees_est). Раньше такая комиссия молча становилась 0 и PnL завышался.
    comm: dict[tuple, list] = {}
    for f in con.execute("SELECT f.venue, f.order_id, f.price, f.qty, f.quote_qty, f.commission_abs, f.commission_asset "
                         "FROM perp_fills f JOIN perp_orders o ON f.venue = o.venue AND f.order_id = o.order_id "
                         "WHERE substr(o.client_id, 1, ?)=?", (len(prefix), prefix)):
        if (f["commission_asset"] or "USDT").upper() not in report.STABLE_ASSETS:
            continue
        acc = comm.setdefault((f["venue"], int(f["order_id"])), [ZERO, ZERO, False])
        acc[0] += abs(_dv(f["commission_abs"]) or ZERO)
        qq = _dv(f["quote_qty"])
        if qq is None:
            px, qty = _dv(f["price"]), _dv(f["qty"])
            qq = px * qty if (px is not None and qty is not None) else None
        if qq is None:
            acc[2] = True                       # оборот сделки неизвестен — комиссии заявки верим как есть
        else:
            acc[1] += qq
    rate = D(str(config.FEES_TAKER[deal["perp_venue"]]))
    sell = buy = fees = ZERO
    est = False
    for o in con.execute("SELECT venue, order_id, side, cum_quote FROM perp_orders WHERE substr(client_id, 1, ?)=? "
                         "AND state IN (?,?)", (len(prefix), prefix, *FILLED)):
        q = _dv(o["cum_quote"]) or ZERO
        if o["side"] == "SELL":
            sell += q
        else:
            buy += q
        k = (o["venue"], int(o["order_id"])) if o["order_id"] is not None else None
        paid, covered, trust = comm.get(k, (ZERO, ZERO, False)) if k is not None else (ZERO, ZERO, False)
        fees += paid
        if not trust and q - covered > FEE_TOL:
            fees += (q - covered) * rate
            est = True
    fund = None
    if not deal["sim"]:
        start = int(float(deal["created"]) * 1000)
        sql, args = "SELECT income FROM funding_income WHERE venue=? AND symbol=? AND ts>=?", [deal["perp_venue"],
                                                                                              deal["symbol"], start]
        if until_ms is not None:
            sql += " AND ts<=?"
            args.append(int(until_ms))
        fund = sum((_dv(r[0]) or ZERO for r in con.execute(sql, args)), ZERO)
    gas = report.gas_totals(deal_txs(con, did), None)["native"]
    return Journal(s_in, s_out, sell, buy, fees, est, fund, gas)


# --- оценка ----------------------------------------------------------------------------------------------------
@dataclass
class Mark:
    deal_id: str
    ts: float
    px_dex: D | None = None
    px_perp: D | None = None
    pnl_now: D | None = None
    pnl_exit: D | None = None
    exit_cost: D | None = None
    funding: D | None = None
    fees: D | None = None
    gas: D | None = None
    flags: dict = field(default_factory=dict)

    def err(self, text: str) -> None:
        self.flags.setdefault("errors", []).append(str(text)[:200])

    def as_dict(self) -> dict:
        """Тот же вид, что decode() строки deal_marks."""
        return {"deal_id": self.deal_id, "ts": self.ts, **{c: getattr(self, c) for c in COLS}, "flags": self.flags}


def save(con, m: Mark) -> None:
    store.add_mark(con, m.deal_id, m.ts, flags=m.flags, **{c: getattr(m, c) for c in COLS})


def decode(row: Mapping | None) -> dict | None:
    """Строка deal_marks → числа Decimal и flags словарём (битое — None / {}: страница не падает из-за строки)."""
    if row is None:
        return None
    try:
        flags = json.loads(row["flags_json"] or "{}")
    except (TypeError, ValueError):
        flags = {}
    return {"deal_id": row["deal_id"], "ts": float(row["ts"] or 0), **{c: _dv(row[c]) for c in COLS},
            "flags": flags if isinstance(flags, dict) else {}}


class _Once:
    """Цена BNB — одно чтение на оценку (NativePrice и так кэширует 60 с)."""

    def __init__(self, fn: Callable[[], Any] | None):
        self.fn, self.done, self.v = fn, False, None

    def __call__(self) -> D | None:
        if not self.done:
            self.done = True
            try:
                self.v = _dv(self.fn()) if self.fn is not None else None
            except Exception as e:             # noqa — неизвестная цена: газ в $ станет None, а не 0
                log.warning("оценка: цена BNB: %s", redact(e))
                self.v = None
        return self.v


def _read(m: Mark, what: str, fn: Callable[[], Any]) -> Any:
    """Чтение сети: сбой или пустой ответ — текст во flags (без секретов), результат None."""
    try:
        v = fn()
    except Exception as e:                     # noqa — ошибка чтения не роняет трейдер
        m.err(f"{what}: {type(e).__name__}: {redact(e)[:120]}")
        return None
    if v is None:
        m.err(f"{what}: не получено")
    return v


class Preempted(Exception):
    """Исполнение началось посреди оценки: дальше сеть не читаем (слот темпа OKX и вес Aster — исполнителю), строку
    не пишем (журнал мог меняться между чтениями). Проход повторится, когда исполнитель освободится."""


def _gate(busy: Callable[[], bool] | None) -> None:
    if busy is not None and busy():
        raise Preempted()


FUNDING_OVERLAP_MS = 24 * 3600 * 1000       # добор фандинга: от последнего записанного начисления минус сутки


def funding_since(con, deal: Mapping, venue: str) -> int:
    """С какого момента добирать начисления. Не с открытия каждый проход: income — 30 веса Aster за окно 7 сут, сделке
    30 сут — 5 окон каждые MARK_S, и растёт с возрастом. От последнего записанного минус сутки; дубли отсеет tran_id."""
    start = int(float(deal["created"]) * 1000)
    r = con.execute("SELECT MAX(ts) FROM funding_income WHERE venue=? AND symbol=? AND ts>=?",
                    (venue, deal["symbol"], start)).fetchone()
    last = r[0] if r else None
    return start if last is None else max(start, int(last) - FUNDING_OVERLAP_MS)


def _last_gas(con, kind: str, token: str, native: _Once) -> D | None:
    """Газ последней нашей транзакции вида kind по чеку, $ (approve — сначала того же токена)."""
    kinds = ("approve",) if kind == "approve" else ("swap", "bump")
    qs = ",".join("?" * len(kinds))
    r = con.execute(f"SELECT gas_used, eff_gas_price FROM dex_txs WHERE kind IN ({qs}) AND state='MINED_OK' AND "
                    f"gas_used IS NOT NULL AND eff_gas_price IS NOT NULL ORDER BY (to_addr=?) DESC, id DESC LIMIT 1",
                    (*kinds, str(token).lower())).fetchone()
    px = native() if r is not None else None
    if r is None or px is None:
        return None
    return D(int(r[0])) * D(int(r[1])) / WEI * px


def _allowance(m: Mark, spot, token: str, chain: str) -> int | None:
    """Allowance токена к spender'ам OKX из allowlist (как engine._allowance_ok, но сбой — None, а не «нет»)."""
    base = getattr(spot, "inner", spot)
    rpc, wallet = getattr(base, "rpc", None), getattr(base, "wallet", None)
    if rpc is None or not wallet:
        return None
    vals, errs = [], []
    for sp in sorted(tconfig.OKX_SPENDERS.get(tconfig.chain_index(chain), ())):
        try:
            vals.append(int(rpc.allowance(token, wallet, sp)))
        except Exception as e:                 # noqa
            errs.append(f"{type(e).__name__}: {redact(e)[:100]}")
    if not vals and errs:
        m.err(f"allowance: {errs[0]}")
    return max(vals) if vals else None


def _dex_exit(con, m: Mark, deal: Mapping, legs, units: int, sim: bool, native: _Once,
              gate: Callable[[], None] = lambda: None) -> tuple[D, D, D] | None:
    """(USDT по котировке на весь объём, газ свопа $, газ approve $). Одна котировка OKX на сделку за проход.
    gate() — перед каждым чтением сети (исполнение началось — Preempted)."""
    if units == 0:
        return ZERO, ZERO, ZERO
    if units < 0:
        m.err("токенов по книге меньше нуля — выход DEX не оценить")
        return None
    stable = getattr(legs.spot, "stable", None) or stable_of(deal["chain"])[0]
    gate()
    q = _read(m, "котировка OKX на выход", lambda: legs.spot.quote(deal["token"], stable, int(units)))
    if q is None:
        return None
    if int(q.amount_out) <= 0:
        m.err("котировка OKX на выход без выхода: маршрута нет")
        return None
    if q.honeypot:
        m.flags["honeypot"] = True
    usdt = D(int(q.amount_out)) / D(10) ** int(q.dec_out)
    g_swap = _dv(q.gas_usd)
    if g_swap is None:                          # tradeFee не пришёл — газ последнего своего свопа по чеку
        g_swap = _last_gas(con, "swap", deal["token"], native)
        if g_swap is None:
            m.err("газ свопа выхода неизвестен")
            return None
        m.flags["gas_src"] = "last"
    g_appr = ZERO
    if not sim:                                 # в симуляции approve не нужен (как plan_exit)
        gate()
        have = _allowance(m, legs.spot, deal["token"], deal["chain"])
        if have is None or have < units:
            m.flags["approve"] = "need" if have is not None else "unknown"
            est = _last_gas(con, "approve", deal["token"], native)
            m.flags["approve_src"] = "last" if est is not None else "swap"
            g_appr = est if est is not None else g_swap          # как план: approve ≈ газ свопа
    return usdt, g_swap, g_appr


def _dex_mid(m: Mark, legs, deal: Mapping, units_all: int, dec: int, ask: D | None,
             gate: Callable[[], None] = lambda: None) -> D | None:
    """P_dex — середина пула: (покупка + продажа) / 2 по малым котировкам ~OKX_DEX_QUOTE_USD. spot.pool_price — цена
    ПОКУПКИ (USDT → токен, комиссия пула сверху): длинный спот по ней завышен на комиссию пула (0.25 % от 200 $ ≈
    0.50 $), а в «удар DEX» выхода попадала бы комиссия покупки, которой при выходе нет. Середина — как мид стакана
    перпа. ask — уже прочитанный pool_price («позиции»). Одной стороны нет — None: односторонняя цена смещена на
    комиссию пула, не подменяем. gate() — перед каждым чтением сети (исполнение началось — Preempted)."""
    if ask is None:
        gate()
        ask = _dv(_read(m, "цена DEX", lambda: legs.spot.pool_price(deal["token"])))
    if ask is None or ask <= 0:
        return None
    units = min(units_all, int((D(config.OKX_DEX_QUOTE_USD) / ask * D(10) ** dec).to_integral_value(ROUND_FLOOR)))
    if units <= 0:
        m.err("цена DEX (продажа): объём малой котировки 0")
        return None
    stable = getattr(legs.spot, "stable", None) or stable_of(deal["chain"])[0]
    gate()
    q = _read(m, "цена DEX (продажа)", lambda: legs.spot.quote(deal["token"], stable, units))
    if q is None:
        return None
    if int(q.amount_in) <= 0 or int(q.amount_out) <= 0:
        m.err("цена DEX (продажа): котировка без выхода")
        return None
    bid = (D(int(q.amount_out)) / D(10) ** int(q.dec_out)) / (D(int(q.amount_in)) / D(10) ** int(q.dec_in))
    m.flags.update(px_dex_ask=ask, px_dex_bid=bid)
    return (ask + bid) / 2


def _perp_exit(m: Mark, book, q_short: D, fee_rate: D) -> tuple[D, D] | None:
    """(стоимость закрытия шорта проходом по стакану, комиссия тейкера). Стакан мельче — остаток по худшему уровню."""
    from .planner import walk
    if q_short == 0:
        return ZERO, ZERO
    if book is None:
        return None
    levels = book.asks if q_short > 0 else book.bids          # шорт откупается по асксам (лонг продаётся по бидам)
    qty = abs(q_short)
    got, quote, last = walk(levels, qty)
    rest = qty - got
    if rest > 0:
        if last is None:
            m.err("стакан пуст — закрытие перпа не оценить")
            return None
        quote += rest * last
        m.flags["uncovered"] = rest
    return (quote if q_short > 0 else -quote), quote * fee_rate


def liq_distance(liq: Any, mark: Any, short: bool = True) -> D | None:
    """До ликвидации, доля: шорт — liq / mark − 1 (ликвидация выше марка: плюс — запас, ≤ 0 — марк уже за ценой
    ликвидации), лонг — 1 − liq / mark. Цены нет или она 0 (биржа пишет 0, когда ликвидации нет) — None, не 0."""
    liq, mark = _dv(liq), _dv(mark)
    if liq is None or mark is None or liq <= 0 or mark <= 0:
        return None
    return liq / mark - 1 if short else 1 - liq / mark


def _liq(m: Mark, perp, symbol: str, venue: str, gate: Callable[[], None]) -> None:
    """«До ликвидации» для кабинета: liquidationPrice и markPrice строки символа из positionRisk (только чтение) и время
    прохода → flags.liq (добавочное поле: схема deal_marks не меняется). Нога без positionRisk — ничего (кабинет
    покажет «—»); сбой чтения — текст во flags, прошлый flags.liq кабинет покажет со своим временем."""
    fn = getattr(perp, "position_risk", None)
    if fn is None:
        return
    gate()
    rows = _read(m, f"positionRisk {venue}", lambda: fn(symbol))
    if rows is None:
        return
    mine = [r for r in rows if isinstance(r, dict) and r.get("symbol") == symbol
            and str(r.get("positionSide") or "BOTH") == "BOTH"] if isinstance(rows, list) else []
    if len(mine) != 1:
        m.err(f"positionRisk {venue}: нет одной строки {symbol}")
        return
    liq, mark = _dv(mine[0].get("liquidationPrice")), _dv(mine[0].get("markPrice"))
    m.flags["liq"] = {"price": liq if liq is not None and liq > 0 else None,
                      "mark": mark if mark is not None and mark > 0 else None, "ts": m.ts}


def mark_deal(con, deal: Mapping, legs, *, now: float, fetch_funding: bool = True, px_ask: D | None = None,
              busy: Callable[[], bool] | None = None) -> Mark:
    """Оценка одной сделки (только чтение сети). px_ask — уже прочитанный spot.pool_price (цена ПОКУПКИ; не читать
    второй раз), P_dex — середина пула (_dex_mid).
    busy — перед каждым чтением сети: исполнение началось — Preempted (строку не писать)."""
    from .engine import deal_book
    from .planner import mid
    gate = lambda: _gate(busy)                  # noqa: E731
    sim = bool(deal["sim"])
    m = Mark(deal["id"], float(now))
    if sim:
        m.flags["sim"] = True
    if legs is None:
        m.err("ног для чтения нет (режим dry без ключей) — не посчитано")
        return m
    symbol, dec = deal["symbol"], int(deal["token_dec"])
    venue = deal["perp_venue"]
    bk = deal_book(con, deal["id"])
    if not bk.known:
        m.err(f"книга сделки неизвестна: {bk.why}")
        return m
    q_tok, q_short = bk.tokens(dec), bk.short
    m.flags.update(q_tok=q_tok, q_short=q_short)
    if bk.m != 1:                              # q_short — контракты (× мид контракта = $); m = 1 — флаги прежние
        m.flags["m"] = bk.m
    if not sim and fetch_funding:
        gate()
        pv = legs.perp.venue
        try:                                   # добор начислений, как engine._deal_pnl; сбой — считаем по записанному
            store.add_funding_income(con, pv, legs.perp.funding_income(symbol, funding_since(con, deal, pv)))
        except Exception as e:                 # noqa
            m.err(f"фандинг не добран: {type(e).__name__}: {redact(e)[:120]}")
    j = journal(con, deal)
    native = _Once(getattr(legs, "native_px", None))
    m.fees = j.fees
    if j.fees_est:
        m.flags["fees_est"] = True
    if j.gas_native:
        gate()                                 # цена BNB — тоже запрос OKX (кэш 60 с)
    m.gas = j.gas_usd(native() if j.gas_native else None)
    if m.gas is None:
        m.err("цена BNB не получена — газ сделки в $ неизвестен")
    m.funding = j.funding if j.funding is not None else ZERO
    base = None if m.gas is None else j.spot_flow + j.perp_flow - j.fees - m.gas + m.funding
    m.px_dex = _dex_mid(m, legs, deal, int(bk.tokens_raw), dec, px_ask, gate) if q_tok != 0 else None
    gate()
    book = _read(m, f"стакан {venue}", lambda: legs.perp.book(symbol, tconfig.ASTER_DEPTH_LIMIT))
    m.px_perp = mid(book) if book is not None else None
    if book is not None and m.px_perp is None:
        m.err(f"стакан {venue} пуст с одной стороны")
    spot_val = ZERO if q_tok == 0 else (q_tok * m.px_dex if m.px_dex is not None else None)
    perp_val = ZERO if q_short == 0 else (q_short * m.px_perp if m.px_perp is not None else None)
    if base is not None and spot_val is not None and perp_val is not None:
        m.pnl_now = base + spot_val - perp_val
    dex = _dex_exit(con, m, deal, legs, int(bk.tokens_raw), sim, native, gate)
    perp = _perp_exit(m, book, q_short, D(str(config.FEES_TAKER[venue])))
    if base is not None and dex is not None and perp is not None:
        usdt, g_swap, g_appr = dex
        cost, fee_x = perp
        m.pnl_exit = base + usdt - g_swap - g_appr - cost - fee_x
        if m.pnl_now is not None:
            m.exit_cost = m.pnl_now - m.pnl_exit
            m.flags["exit"] = {"dex": spot_val - usdt, "book": cost - perp_val, "fee": fee_x, "gas": g_swap + g_appr}
    if not sim:                                # позиция биржи против книги: оценка по книге, расхождение — флаг
        gate()
        pos = _read(m, f"позиция {venue}", lambda: legs.perp.position(symbol))
        if pos is not None and pos != -q_short:
            m.flags["position"] = pos
        if q_short != 0:
            _liq(m, legs.perp, symbol, venue, gate)
        else:                                  # шорта по книге нет (перп закрыт, спот остался): прошлая цена
            m.flags["liq"] = {"price": None, "mark": None, "ts": m.ts}      # ликвидации кабинет больше не покажет
    return m


def final_mark(con, deal: Mapping, legs, *, now: float) -> Mark:
    """Итог закрытой сделки по журналу (как engine._deal_pnl: спот + перп − комиссии + фандинг − газ) — строка с
    flags.final, чтобы кабинет показал итог с газом в $ (цены BNB у кабинета нет)."""
    m = Mark(deal["id"], float(now), flags={"final": True})
    if deal["sim"]:
        m.flags["sim"] = True
    j = journal(con, deal, until_ms=int(float(deal["updated"] or now) * 1000))
    m.fees, m.funding = j.fees, (j.funding if j.funding is not None else ZERO)
    if j.fees_est:
        m.flags["fees_est"] = True
    m.gas = j.gas_usd(_Once(getattr(legs, "native_px", None))() if j.gas_native else None)
    if m.gas is not None:
        m.pnl_now = j.spot_flow + j.perp_flow - j.fees - m.gas + m.funding
    return m


def _safe_mark(con, d: Mapping, legs_fn: Callable[[bool], Any], now: float,
               busy: Callable[[], bool] | None = None) -> Mark:
    try:
        from .runtime import legs_of          # BSC — ровно legs_fn(sim); иная EVM-связка — её ноги из реестра
        return mark_deal(con, d, legs_of(legs_fn, d), now=now, busy=busy)
    except Preempted:
        raise
    except Exception as e:                     # noqa — одна сделка не останавливает проход
        log.exception("оценка сделки %s", d["id"])
        m = Mark(d["id"], float(now), flags={"sim": True} if d["sim"] else {})
        m.err(f"расчёт упал: {type(e).__name__}: {redact(e)[:120]}")
        return m


def run_pass(con, legs_fn: Callable[[bool], Any], *, now: float,
             busy: Callable[[], bool] = lambda: False) -> tuple[list[Mark], bool]:
    """Проход трейдера: оценка каждой активной сделки (всё, кроме DRAFT/CLOSED/ABORTED), итог недавно закрытых (один раз
    на сделку), чистка старше MARK_KEEP_S. Возвращает (оценки, проход завершён). Исполнение началось посреди прохода —
    остаток пропускается (False): во время исполнения не оцениваем."""
    out: list[Mark] = []
    from .runtime import is_sol_deal
    for d in store.active_deals(con):
        if busy():
            return out, False
        try:                                   # связка SOL × HL — своя оценка (sol_ledger) на ногах своей связки
            m = _safe_sol_mark(con, d, legs_fn, now, busy) if is_sol_deal(d) else _safe_mark(con, d, legs_fn, now, busy)
        except Preempted:                      # исполнение началось посреди сделки: остаток сети не читаем
            return out, False
        if busy():                             # журнал мог меняться между чтениями — такую строку не пишем
            return out, False
        save(con, m)
        out.append(m)
    for d in [dict(r) for r in con.execute(
            "SELECT * FROM deals WHERE state='CLOSED' AND updated>=? AND id NOT IN (SELECT deal_id FROM deal_marks "
            "WHERE json_extract(flags_json, '$.final') = 1)", (now - tconfig.MARK_KEEP_S,))]:
        try:
            if is_sol_deal(d):
                m = _sol_final(con, d, legs_fn, now)
            else:
                from .runtime import legs_of
                m = final_mark(con, d, legs_of(legs_fn, d), now=now)
        except Exception as e:                 # noqa
            log.warning("итог сделки %s: %s", d["id"], redact(e))
            continue
        if m.pnl_now is not None:              # без цены BNB — в следующий проход, а не итог без газа навсегда
            save(con, m)
    # связка SOL × HL: итог, посчитанный при неполной истории HL, пересчитывается (новая ревизия), пока учёт не полон
    for d in [dict(r) for r in con.execute(
            "SELECT * FROM deals WHERE state='CLOSED' AND updated>=? AND id IN (SELECT deal_id FROM deal_marks WHERE "
            "json_extract(flags_json, '$.final') = 1) AND id NOT IN (SELECT deal_id FROM deal_marks WHERE "
            "json_extract(flags_json, '$.final') = 1 AND json_extract(flags_json, '$.accounting_complete') = 1)",
            (now - tconfig.MARK_KEEP_S,))]:
        if not is_sol_deal(d) or busy():
            continue
        try:
            m = _sol_final(con, d, legs_fn, now)
        except Exception as e:                 # noqa
            log.warning("итог сделки %s: %s", d["id"], redact(e))
            continue
        if m.pnl_now is not None:
            save(con, m)
    store.prune_marks(con, now)
    return out, True


def sol_cfg(deal: Mapping):
    """Замороженный owner.toml сделки (допуск котировки выхода) — не свежий файл: оценка по условиям сделки."""
    from .owner import OwnerCfg
    try:
        return OwnerCfg.from_frozen(deal["owner_json"])
    except Exception:                          # noqa — битая копия: котировка выхода не запрашивается (ошибка в flags)
        return None


def _sol_legs(legs_fn, d: Mapping):
    from .runtime import legs_of
    return legs_of(legs_fn, d)


def _safe_sol_mark(con, d: Mapping, legs_fn, now: float, busy: Callable[[], bool] | None = None) -> Mark:
    from . import sol_ledger
    try:
        try:
            legs = _sol_legs(legs_fn, d)
        except Exception as e:                 # noqa — ProfileDown и прочее: сделка видна, оценки нет
            m = Mark(d["id"], float(now), flags={"sim": True} if d["sim"] else {})
            m.err(f"ноги связки не собраны: {redact(e)[:160]}")
            return m
        return sol_ledger.mark(con, d, legs, now=now, cfg=sol_cfg(d), busy=busy)
    except Preempted:
        raise
    except Exception as e:                     # noqa
        log.exception("оценка сделки %s", d["id"])
        m = Mark(d["id"], float(now), flags={"sim": True} if d["sim"] else {})
        m.err(f"расчёт упал: {type(e).__name__}: {redact(e)[:120]}")
        return m


def _sol_final(con, d: Mapping, legs_fn, now: float) -> Mark:
    from . import sol_ledger
    try:
        legs = _sol_legs(legs_fn, d)
    except Exception:                          # noqa — без ног: по журналу, без добора истории HL
        legs = None
    return sol_ledger.final_mark(con, d, legs, now=now)


def for_positions(con, deal: Mapping, legs, *, now: float, fresh: bool, px_ask: D | None = None,
                  busy: Callable[[], bool] | None = None) -> dict | None:
    """Оценка для «позиций»: последняя строка deal_marks, если она моложе MARK_S; иначе свежая той же функцией (и она
    же пишется в deal_marks). fresh=False (идёт исполнение) — только таблица, не старше MARK_STALE_S."""
    last = decode(store.last_mark(con, deal["id"]))
    recent = last if (last is not None and not last["flags"].get("final")
                      and now - last["ts"] < tconfig.MARK_STALE_S) else None
    if recent is not None and (now - recent["ts"] < tconfig.MARK_S or not fresh):
        return recent
    if not fresh:
        return None
    try:
        m = mark_deal(con, deal, legs, now=now, fetch_funding=False, px_ask=px_ask, busy=busy)
        save(con, m)
    except Preempted:                          # исполнение началось — свежую не пишем, показываем последнюю
        return recent
    except Exception as e:                     # noqa — «позиции» без строки PnL, но не без позиций
        log.warning("оценка %s для «позиций»: %s", deal["id"], redact(e))
        return None
    return m.as_dict()
