"""Read-only projection of historical execution journals into quote flows.

Never nets different currencies. Fee/funding valuation remains explicit at the
caller. Missing historical amounts are reported separately; legacy zero fallback
is exposed as such, not presented as a proven zero in a new ledger.
"""
from dataclasses import dataclass
from decimal import Decimal as D

FLOW_STATES = ('DEX_OK', 'PERP_SENT', 'BALANCED', 'HEDGE_DEFICIT')


@dataclass(frozen=True)
class QuoteFlows:
    debit: D
    credit: D
    missing: tuple[str, ...] = ()

    @property
    def net(self):
        return None if self.missing else self.credit - self.debit

    @property
    def legacy_net(self):
        """Compatibility only: previous code treated missing values as zero."""
        return self.credit - self.debit


def spot_quote_flows(con, deal_id, quote_decimals, *, exit_kinds=('exit', 'undo')):
    debit = credit = D(0)
    missing = []
    for r in con.execute("SELECT c.id,c.dex_in,c.dex_out,i.kind FROM clips c JOIN intents i ON c.intent_id=i.id "
                         "WHERE i.deal_id=? AND c.state IN (?,?,?,?) ORDER BY c.id", (deal_id, *FLOW_STATES)):
        incoming = r[3] == 'entry'
        if not incoming and r[3] not in exit_kinds:
            continue
        amount = r[1] if incoming else r[2]
        if amount is None:
            missing.append(f'clip:{r[0]}:quote')
        value = D(int(amount or 0)) / D(10) ** quote_decimals
        if incoming:
            debit += value
        else:
            credit += value
    return QuoteFlows(debit, credit, tuple(missing))


def filled_orders(con, deal_id):
    """Keep original client IDs and final-partial semantics; no invented fill IDs."""
    prefix = f'fb-{deal_id}-'
    return con.execute("SELECT client_id,side,cum_quote,executed_qty FROM perp_orders "
                       "WHERE substr(client_id,1,?)=? AND state IN ('FILLED','PARTIALLY_FILLED') "
                       "ORDER BY client_id", (len(prefix), prefix)).fetchall()


def perp_quote_flows(con, deal_id):
    debit = credit = D(0)
    missing = []
    for row in filled_orders(con, deal_id):
        if row['cum_quote'] is None:
            missing.append(f"order:{row['client_id']}:quote")
        value = D(row['cum_quote'] or '0')
        if row['side'] == 'SELL':
            credit += value
        else:
            debit += value
    return QuoteFlows(debit, credit, tuple(missing))
