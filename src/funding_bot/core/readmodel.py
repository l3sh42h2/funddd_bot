"""Private read model. Only core accesses trade.db / calculates money."""
import hashlib, json, logging, math, sqlite3, time
from decimal import Decimal
from pathlib import Path
from .. import config
from ..trade import marks as tmarks, tconfig
from ..trade.report import dval, fmt_num
from ..cabinet_text import event_text
from ..market_snapshot import load_market_table
D = Decimal
ZERO = D(0)
DEALS_MAX = 500
log = logging.getLogger(__name__)
# --- trade.db только на чтение ---------------------------------------------------------------------------
def open_ro(path) -> sqlite3.Connection:
    """Соединение, которое не может писать: URI mode=ro (базу не создаст и не изменит) + query_only."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _dv(v) -> D | None:
    """TEXT из БД → Decimal; битое или бесконечное — None (страница не падает из-за одной строки)."""
    try:
        x = dval(v)
    except (ArithmeticError, ValueError, TypeError):
        return None
    return x if x is None or x.is_finite() else None


def _row_key(d: dict):
    """Ключ строки пары в table.json: спот DEX «<chainIndex>:<токен>», перп, символ (как dexleg строит строку). EVM-адрес —
    нижним регистром (как раньше); mint Solana (501) — base58 как есть: регистр значим (S01)."""
    try:
        ci = tconfig.chain_index(d["chain"])
    except (KeyError, TypeError):
        return None
    spot = f"{ci}:{d['token']}"
    return (spot if _sol_spot(spot) else spot.lower(), d["perp_venue"], d["symbol"])


def _sol_spot(spot: str) -> bool:
    return str(spot).startswith("501:")


def _is_sol(d: dict) -> bool:
    from ..trade.runtime import is_sol_deal
    try:
        return is_sol_deal(d)
    except Exception:                           # noqa — битая строка сделки: карточка BSC-вида, а не падение страницы
        return False


SOL_ROUTE = {"jupiter_build_v2": "Jupiter", "jupiter_order_v2": "Jupiter Order", "okx_solana_v6": "OKX"}
IDENT_TEXT = {"reviewed_override": "подтверждено владельцем", "verified_source": "подтверждено источником"}


def _sol_view(con: sqlite3.Connection, d: dict) -> dict:
    """Связка Solana × Hyperliquid: сеть, mint (как есть), фактические маршруты клипов, точный рынок HL и основание
    соответствия — из замороженной спецификации сделки и журнала (не из политики «auto»)."""
    from ..trade import sol_ledger
    inst = sol_ledger.inst_of(d)
    try:
        routes = sol_ledger.ledger(con, d, fee_rate=None).routes
    except (sqlite3.Error, ArithmeticError, ValueError, TypeError, KeyError):
        routes = ()
    return {"mint": d.get("token"), "fullcoin": inst.get("perp_symbol") or d.get("symbol"),
            "routes": tuple(dict.fromkeys(SOL_ROUTE.get(r, r) for r in routes)),
            "identity": IDENT_TEXT.get(inst.get("identity_status")), "ident_to": str(inst.get("identity_expires_at")
                                                                                    or "")[:10]}


_TERMINAL = ("CLOSED", "ABORTED")
_DEX_UNKNOWN = ("DEX_SENT", "DEX_UNKNOWN")        # как engine.deal_book: исход свопа не выяснен
_PERP_OPEN = ("INTENT", "SENT", "UNKNOWN")        # исход заявки не выяснен


def load_deals(con: sqlite3.Connection, limit: int = DEALS_MAX, now: float | None = None) -> dict:
    """Все сделки (кроме черновиков — планов без «да»), новые сверху: статус, PnL (оценка трейдера или итог), у активной
    — «до ликвидации» (positionRisk из оценки трейдера) и история выплат фандинга (funding_income).
    Объёмы и средние ног карточка больше не показывает (правка владельца 13.09: «лишняя инфа»); остались только пометки
    «исход свопа / заявки выясняется» — клип в DEX_SENT/DEX_UNKNOWN, заявка сделки (префикс client_id fb-<сделка>-, как
    engine.deal_book) в INTENT/SENT/UNKNOWN."""
    deals = [dict(r) for r in con.execute("SELECT * FROM deals WHERE state != 'DRAFT' ORDER BY created DESC, id DESC "
                                          "LIMIT ?", (int(limit),))]
    drafts = con.execute("SELECT count(*) FROM deals WHERE state = 'DRAFT'").fetchone()[0]
    if not deals:
        return {"deals": [], "drafts": drafts}
    opened = {r[0]: r[1] for r in con.execute("SELECT deal_id, MIN(approved) FROM intents WHERE kind = 'entry' "
                                               "AND approved IS NOT NULL GROUP BY deal_id")}
    qs = lambda xs: ",".join("?" * len(xs))                                        # noqa: E731
    spot_unknown = {r[0] for r in con.execute(f"SELECT DISTINCT i.deal_id FROM clips c JOIN intents i ON i.id = c.intent_id "
                                              f"WHERE c.state IN ({qs(_DEX_UNKNOWN)})", _DEX_UNKNOWN)}
    perp_unknown = set()
    for (cid,) in con.execute(f"SELECT client_id FROM perp_orders WHERE client_id LIKE 'fb-%' "
                              f"AND state IN ({qs(_PERP_OPEN)})", _PERP_OPEN):
        parts = str(cid).split("-")
        if len(parts) >= 3:
            perp_unknown.add(parts[1])
    now = time.time() if now is None else now
    out = []
    for d in deals:
        last = con.execute("SELECT e.ts, e.kind, e.json, i.kind AS ikind FROM exec_events e LEFT JOIN intents i "
                           "ON i.id = e.intent_id WHERE e.deal_id = ? ORDER BY e.ts DESC, e.rowid DESC LIMIT 1",
                           (d["id"],)).fetchone()
        v = _deal_view(d, opened.get(d["id"]), d["id"] in spot_unknown, d["id"] in perp_unknown, last)
        if _is_generic(d):
            from .leg_report import build
            v['legs'] = build(con, deal_id=d['id'])
            v['pnl'] = {'final': d['state'] in _TERMINAL, 'total': None, 'incomplete': True,
                        'reason': 'valuation_not_available'}
            v['row_key'] = None
            out.append(v)
            continue
        if _is_sol(d):
            v["sol"], v["unit"] = _sol_view(con, d), "USDC"
        v["pnl"] = _pnl_view(con, d, now)
        if d.get("state") not in _TERMINAL:
            v["liq"] = None if v["sim"] else _liq_view(con, d, now)
            v["hist"] = None if v["sim"] else funding_history(con, d, now=now)
        out.append(v)
    return {"deals": out, "drafts": drafts}


def _is_generic(deal):
    try:
        return json.loads(deal.get('inst_json') or '{}').get('generic_position_v1') is True
    except (TypeError, ValueError, AttributeError):
        return False


def _liq_view(con: sqlite3.Connection, d: dict, now: float) -> dict | None:
    """«До ликвидации» — последняя оценка трейдера С flags.liq (positionRisk; trade/marks.py): строка, где чтение не
    удалось, прошлое не стирает — показывается прошлое со своим временем, старше MARK_STALE_S — «устарело»."""
    try:
        r = con.execute("SELECT flags_json FROM deal_marks WHERE deal_id = ? AND CASE WHEN json_valid(flags_json) "
                        "THEN json_type(flags_json, '$.liq') END = 'object' ORDER BY ts DESC, rowid DESC LIMIT 1",
                        (d["id"],)).fetchone()
    except sqlite3.OperationalError:            # трейдер старой версии: deal_marks ещё нет
        return None
    if r is None:
        return None
    try:
        lq = json.loads(r[0]).get("liq")
        ts = float(lq.get("ts"))
    except (TypeError, ValueError, AttributeError):
        return None
    if not math.isfinite(ts):
        return None
    price, mark = _dv(lq.get("price")), _dv(lq.get("mark"))
    return {"price": price, "mark": mark, "ts": ts, "dist": tmarks.liq_distance(price, mark),
            "stale": now - ts > tconfig.MARK_STALE_S}


HIST_MAX = 200                          # строк истории выплат в карточке (всего — по всем)


def funding_history(con: sqlite3.Connection, d: dict, *, now: float | None = None) -> dict:
    """Выплаты фандинга сделки: funding_income площадки и символа сделки с её открытия — те же границы, что у фандинга в
    PnL (marks.journal). rows — (время с, сумма $, итог с начала), новые сверху, не больше HIST_MAX; total — по всем.
    Связка Solana × Hyperliquid — фактические userFunding счёта сделки в её окне (sol_ledger), USDC."""
    from ..trade import accounting
    bound = accounting.sources(con, d['id'], until_ms=int((time.time() if now is None else now) * 1000))
    if bound is not None:
        rows = [(r['ts'], r['income']) for r in bound.funding_rows]
    elif _is_sol(d):
        from ..trade import sol_ledger
        try:
            rows = [(int(t * 1000), x) for t, x in sol_ledger.funding_rows(con, d)]
        except (sqlite3.Error, TypeError, ValueError):
            rows = []
    else:
        try:
            start = int(float(d["created"]) * 1000)
            rows = con.execute("SELECT ts, income FROM funding_income WHERE venue = ? AND symbol = ? AND ts >= ? "
                               "ORDER BY ts, tran_id", (d["perp_venue"], d["symbol"], start)).fetchall()
        except (sqlite3.OperationalError, TypeError, ValueError):
            return {"rows": [], "n": 0, "total": ZERO}
    acc, out = ZERO, []
    for ts, inc in rows:
        x = _dv(inc)
        if bound is not None:
            acc = acc + x if acc is not None and x is not None else None
            out.append((int(ts) / 1000, x, acc))
            continue
        if x is None or ts is None:
            continue
        acc += x
        out.append((int(ts) / 1000, x, acc))
    result = {"rows": out[::-1][:HIST_MAX], "n": len(out), "total": acc}
    if bound is not None:
        result.update(total=bound.funding, incomplete=bool(bound.missing), source_revision=bound.revision)
    return result


def _pnl_view(con: sqlite3.Connection, d: dict, now: float) -> dict | None:
    """PnL для карточки. Активная — последняя оценка трейдера из deal_marks (trade/marks.py; кабинет в биржи не ходит).
    Закрытая — итог: строка final трейдера (с газом в $), без неё — по журналу здесь же (engine._deal_pnl без газа:
    цены BNB у кабинета нет — так и подписано). Отменённая — ничего."""
    st = d.get("state")
    if st == "ABORTED":
        return None
    try:
        mk = tmarks.decode(con.execute("SELECT * FROM deal_marks WHERE deal_id = ? ORDER BY ts DESC, rowid DESC LIMIT 1",
                                       (d["id"],)).fetchone())
    except sqlite3.OperationalError:            # трейдер старой версии ещё не создал deal_marks — расчёта нет
        mk = None
    # Старый кэш мог быть рассчитан до строгой проекции денежных потоков. Проверяем исходный журнал при каждом чтении:
    # FILLED/DEX_OK с отсутствующей суммой не имеет права остаться известным PnL только потому, что deal_marks старше.
    evm_missing: tuple[str, ...] = ()
    if not _is_sol(d):
        try:
            journal = tmarks.journal(
                con, d, until_ms=int(float(d.get("updated") or now) * 1000) if st == "CLOSED" else
                (int(mk["ts"] * 1000) if mk is not None else int(now * 1000)))
            evm_missing = journal.missing_flows
            if journal.accounting_revision is not None and mk is not None and (
                    mk['flags'].get('accounting_revision') != journal.accounting_revision):
                mk = None
        except (sqlite3.Error, ArithmeticError, ValueError, TypeError, KeyError):
            evm_missing = ("journal:unreadable",)
    if st == "CLOSED":
        if evm_missing:
            return {"final": True, "total": None, "no_gas": False, "incomplete": True}
        if mk is not None and mk["flags"].get("final") and mk["pnl_now"] is not None:
            out = {"final": True, "total": mk["pnl_now"], "no_gas": False}
            if mk["flags"].get("accounting_complete") is False:
                out["incomplete"] = True
            return out
        if _is_sol(d):
            from ..trade import sol_ledger
            return sol_ledger.realized_view(con, d)
        try:
            j = tmarks.journal(con, d, until_ms=int(float(d.get("updated") or now) * 1000))
        except (sqlite3.Error, ArithmeticError, ValueError, TypeError, KeyError) as e:
            log.warning("кабинет: итог %s не посчитан: %s", d.get("id"), type(e).__name__)
            return None
        gas = j.gas_usd(None)                   # газа не было — 0; был — в $ не перевести
        total = None if j.spot_flow is None or j.perp_flow is None else \
            j.spot_flow + j.perp_flow - j.fees + (j.funding or ZERO) - (gas or ZERO)
        return {"final": True, "total": total,
                "no_gas": gas is None}
    if mk is None or mk["flags"].get("final"):
        return {"final": False, "mark": None}
    if evm_missing:
        mk = dict(mk)
        mk["flags"] = dict(mk["flags"], accounting_complete=False, missing_flows=list(evm_missing))
        for key in ("pnl_now", "pnl_exit", "exit_cost"):
            mk[key] = None
    return {"final": False, "mark": mk, "stale": now - mk["ts"] > tconfig.MARK_STALE_S}


def _deal_view(d: dict, opened, spot_unknown: bool, perp_unknown: bool, last) -> dict:
    ev = None
    if last is not None:
        try:
            data = json.loads(last["json"]) if last["json"] else {}
        except ValueError:
            data = {}
        ev = (event_text(last["kind"], data if isinstance(data, dict) else {}, last["ikind"]), last["ts"])
    return {
        "id": d["id"], "coin": d.get("coin"), "chain": d.get("chain"), "venue": d.get("perp_venue"),
        "symbol": d.get("symbol"), "leg_usd": _dv(d.get("leg_usd")), "state": d.get("state"), "reason": d.get("reason"),
        "sim": bool(d.get("sim")), "opened": opened or d.get("created"),
        "closed": d.get("updated") if d.get("state") in _TERMINAL else None,
        "spot_unknown": spot_unknown, "perp_unknown": perp_unknown,
        "last": ev, "row_key": _row_key(d), "live": None, "liq": None, "hist": None,
    }




def snapshot(con, now=None):
    now = time.time() if now is None else now
    # A consistent SQLite read snapshot, no network calls and no new connection/DB creation.
    con.execute('BEGIN')
    try:
        data = load_deals(con, now=now)
        con.commit()
    except BaseException:
        con.rollback()
        raise
    t = load_market_table()
    tick = t.get('tick_ts')
    if not isinstance(tick, (int,float)) or not math.isfinite(tick):
        tick = None
    old = tick is None or abs(now-tick)>config.STALE_S
    idx = {}
    for r in t.get('sf_rows') or []:
        if r.get('spot_ex') == 'okxdex':
            spot = str(r.get('spot') or '')
            idx[(spot if spot.startswith('501:') else spot.lower(), r.get('perp_ex'), r.get('perp'))] = r
    for d in data['deals']:
        r = idx.get(d['row_key']) if d['state'] not in _TERMINAL else None
        if r:
            d['live'] = dict(rate_h=r.get('spread'), gap=r.get('gap'), px_spot=r.get('px_spot'),
                             px_perp=r.get('px_perp'), ts=tick, stale=old or bool(r.get('stale')))
    return dict(now=now, as_of=now, revision=str(time.time_ns()), schema_version=1, err=None, **data)


def legacy_snapshot(path, now):
    """Explicit offline test/transition backend; never selected by production interface."""
    try:
        con = open_ro(path)
    except FileNotFoundError:
        return dict(now=now, deals=[], drafts=0, err=None)
    except sqlite3.Error:
        return dict(now=now, deals=[], drafts=0, err='журнал сделок не открылся')
    try:
        return snapshot(con, now)
    except sqlite3.Error:
        return dict(now=now, deals=[], drafts=0, err='журнал сделок не прочитан')
    finally:
        con.close()
