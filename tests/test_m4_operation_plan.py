from decimal import Decimal as D
import pytest
from funding_bot.trade.adapters.contracts import Capabilities, LegSpec
from funding_bot.trade.operation_plan import LegBound, OperationPlan

def specs():
    caps = Capabilities("cex", "perpetual", short=True, reduce_only=True)
    kw = dict(adapter_id="x", venue="v", account="a", instrument="BTC-PERP", asset_id="BTC",
              identity_evidence="proof", multiplier=D(1), step=D(1), tick=D(1), quote_currency="USDT",
              settlement_currency="USDT", capabilities=caps, metadata_revision="r")
    return (LegSpec("l1", "lead", "long", **kw), LegSpec("l2", "hedge", "short", **{**kw, "account":"b", "identity_evidence":"proof2"}))

def plan():
    a,b=specs()
    return OperationPlan("op1", "entry", (a,b), "l1", D(10), D(1), 1000.0, "floor",
        {"l1":LegBound("BUY",D(1),D(10),"USDT",D(100)), "l2":LegBound("SELL",D(1),D(10),"USDT",D(100))},
        {"approval":"owner:v1"})

def test_roundtrip_and_fingerprint_covers_all_fields():
    p=plan(); q=OperationPlan.from_json(p.to_json())
    assert q.to_json()==p.to_json() and q.fingerprint==p.fingerprint
    assert OperationPlan("op1","entry",p.legs,"l1",D(11),D(1),1000.0,"floor",p.bounds,p.authorization).fingerprint != p.fingerprint

def test_rejects_missing_scope_permissions_currency_and_spot_short():
    a,b=specs()
    with pytest.raises(ValueError): OperationPlan("x","entry",(a,b),"l1",D(1),D(0),1,"floor",{"l1":p_bound(a)}, {})
    with pytest.raises(ValueError): LegBound("BUY",D(2),D(1),"USDT",D(1))

def p_bound(spec): return LegBound("BUY",D(1),D(1),spec.quote_currency,D(1))
