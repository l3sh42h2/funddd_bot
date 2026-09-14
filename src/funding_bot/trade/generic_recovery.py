"""Read-only reconciliation of generic legs, never a new send after restart."""
from dataclasses import dataclass
from decimal import Decimal as D
import json
import math
import time

from . import store, leg_accounting
from .operation_plan import OperationPlan


def is_generic(deal):
    try:
        return json.loads(deal.get('inst_json') or '{}').get('generic_position_v1') is True
    except (TypeError, ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class Check:
    matched: bool | None
    hedged: bool | None
    delta: D | None
    detail: str


def check(con, deal, *, registry=None, context_factory=None, now=None):
    rows = con.execute('SELECT * FROM intents WHERE deal_id=? ORDER BY created DESC,rowid DESC', (deal['id'],)).fetchall()
    intent = next((dict(x) for x in rows if json.loads(x['spec_json'] or '{}').get('generic_operation_v1')), None)
    if intent is None:
        return Check(None, None, None, 'нет замороженного плана двух ног')
    plan = OperationPlan.from_json(intent['plan_json'])
    projection = leg_accounting.rebuild(con, deal_id=deal['id'])
    book = {(x['leg_id'], x['spec_hash']): D(x['qty']) for x in projection['legs']}
    known = all((s.leg_id, s.fingerprint) in book for s in plan.legs)
    delta = sum(book[(s.leg_id, s.fingerprint)] for s in plan.legs) if known else None
    hedge = next(s for s in plan.legs if s.leg_id != plan.leading_leg_id)
    hedged = abs(delta) < hedge.step * hedge.multiplier if known else None
    op = store.active_operation(con, deal['id'])
    if op is not None and (int(op['reserved_raw']) or op['state'] == store.OpState.PAUSED_UNKNOWN):
        return Check(None, hedged, delta, 'исход исполнения выясняется; повторная отправка запрещена')
    if registry is None or context_factory is None:
        return Check(None, hedged, delta, 'адаптеры для сверки двух ног не подключены')
    try:
        ctx = context_factory(con, intent, deal, json.loads(intent['spec_json']))
        pair = registry.compose(plan.legs[0], plan.legs[1], ctx)
        observations = [adapter.observe() for adapter in (pair.first, pair.second)]
        ts = time.time() if now is None else now
        for obs in observations:
            if (obs.quantity is None or not isinstance(obs.as_of, (int, float)) or not math.isfinite(obs.as_of)
                    or abs(ts - obs.as_of) > 60 or obs.quality not in {'authoritative', 'confirmed', 'finalized'}):
                return Check(None, hedged, delta, 'свежие позиции обеих ног не подтверждены')
        if not known:
            return Check(None, hedged, delta, 'количества обеих ног не восстановлены из фактов')
        matched = all(obs.quantity * s.multiplier == book[(s.leg_id, s.fingerprint)]
                      for s, obs in zip(plan.legs, observations))
        return Check(matched, hedged, delta, 'обе ноги сверены' if matched else 'позиции площадок расходятся с журналом')
    except Exception as exc:
        return Check(None, hedged, delta, f'сверка двух ног недоступна: {type(exc).__name__}')
