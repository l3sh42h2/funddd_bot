"""Core-only projection of two independent legs; no network or signing data."""
from ..trade import leg_accounting, leg_cash


def build(con, *, operation_id=None, deal_id=None):
    """Caller owns the consistent SQLite read transaction.

    Exposures, currency cash and funding are separate; no unproven USD PnL is
    inferred from a perpetual notional or from a missing FX observation.
    """
    quantities = leg_accounting.rebuild(con, operation_id=operation_id, deal_id=deal_id)
    cash = leg_cash.rebuild(con, operation_id=operation_id, deal_id=deal_id)
    currencies = {(x['leg_id'], x['scope']): x for x in cash['legs']}
    out = []
    for leg in quantities['legs']:
        flows = currencies.pop((leg['leg_id'], leg['scope']), None)
        if flows is not None and flows['spec_hash'] != leg['spec_hash']:
            raise ValueError('quantity and cash frozen leg identities differ')
        out.append({**leg, 'funding_complete': False, 'cash': {} if flows is None else flows['cash'],
            'notional': {} if flows is None else flows['notional'],
            'rent_locked_delta': {} if flows is None else flows['rent_locked_delta'],
            'cash_complete': False if flows is None else flows['complete'],
            'funding': leg['funding'] if flows is None else flows['funding']})
    for flows in currencies.values():
        # Funding without quantity evidence does not imply a known flat leg.
        out.append(dict(leg_id=flows['leg_id'], scope=flows['scope'], spec_hash=flows['spec_hash'],
            qty=None, executions=0, fees={}, unknown_fees=0, fees_complete=False,
            cash=flows['cash'], notional=flows['notional'], rent_locked_delta=flows['rent_locked_delta'],
            cash_complete=False, funding=flows['funding'], funding_complete=False, market_kind='perpetual'))
    return dict(version=2, operation_id=operation_id, deal_id=deal_id, legs=tuple(out),
                pnl=None, pnl_reason='valuation_not_available')
