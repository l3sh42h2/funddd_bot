from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from funding_bot.trade import aster_trade as at, gate_trade as gt


def aster(body):
    native=at.AsterTrade(now=lambda:2)
    native._signed_ok=lambda *args:body
    return native


def gate(body):
    native=gt.GateTrade(now=lambda:2)
    native._m=lambda symbol:D(1)
    native._signed_ok=lambda *args:body
    return native


def trade(**changes):
    return dict(dict(id=1,order_id=2,contract='X_USDT',size='1',price='2',fee='0',point_fee='0',create_time='1.8'),**changes)


def income(**changes):
    return dict(dict(id=1,contract='X_USDT',type='fund',time='1.8',change='0.2'),**changes)


def test_aster_requires_explicit_signed_user_for_identity():
    n=aster([]); n.signer=SimpleNamespace(user='0x'+'a'*40,send_user=False)
    with pytest.raises(at.AsterError,match='explicit signed user'):
        n.history_account()
    n.signer.send_user=True
    first=n.history_account()
    n.signer.user=n.signer.user.upper()
    assert n.history_account()==first
    n.base='https://different-environment.example'
    assert n.history_account()!=first


def test_gate_identity_survives_key_rotation_but_not_uid_or_endpoint_change():
    n=gate(dict(user=123)); first=n.history_account()
    n._key='new-key'
    assert n.history_account()==first
    n._signed_ok=lambda *args:dict(user=456)
    assert n.history_account()!=first
    n._signed_ok=lambda *args:dict(available='9')
    with pytest.raises(gt.GateError,match='user ID'):
        n.history_account()


@pytest.mark.parametrize('body', [[None],[trade(contract='OTHER')],[trade(order_id=None)]])
def test_gate_refuses_history_identity_loss(body):
    with pytest.raises(gt.GateError):
        gate(body).history_fills('X_USDT',0)


@pytest.mark.parametrize('point_fee', ['9',None])
def test_gate_point_fee_cannot_be_counted_as_known_zero(point_fee):
    rows=gate([trade(point_fee=point_fee)]).history_fills('X_USDT',0)
    assert rows[0]['commission_abs'] is None


def test_gate_funding_subseconds_are_preserved_and_missing_amount_refused():
    assert gate([income()]).history_funding('X_USDT',1500)[0]['ts']==1800
    with pytest.raises(gt.GateError):
        gate([income(change=None)]).history_funding('X_USDT',1500)
    with pytest.raises(gt.GateError):
        gate([income(id='bad')]).history_funding('X_USDT',1500)


def test_conflicting_native_ids_are_not_silently_overwritten():
    with pytest.raises(gt.GateError,match='duplicate'):
        gate([trade(fee='1'),trade(fee='9')]).history_fills('X_USDT',0)
    with pytest.raises(gt.GateError,match='duplicate'):
        gate([income(change='1'),income(change='9')]).history_funding('X_USDT',0)
    fill=dict(id=1,orderId=2,symbol='XUSDT',side='SELL',price='2',qty='1',quoteQty='2',
              commission='1',commissionAsset='USDT',maker=False,time=1000)
    with pytest.raises(at.AsterError,match='duplicate'):
        aster([fill,dict(fill,commission='9')]).history_fills('XUSDT',0)


def test_aster_saturated_timestamp_is_gap_not_success(monkeypatch):
    monkeypatch.setattr(at,'PAGE_LIMIT',2)
    rows=[dict(tranId=i,symbol='XUSDT',incomeType='FUNDING_FEE',income='1',asset='USDT',time=1000) for i in (1,2)]
    with pytest.raises(at.AsterError,match='saturated'):
        aster(rows).history_funding('XUSDT',1000)


@pytest.mark.parametrize('venue', ['aster', 'gate'])
@pytest.mark.parametrize('field', ['id','order'])
@pytest.mark.parametrize('bad', [True,False,7.9,7.0,-1,'01','7.0','+7',' 7'])
def test_strict_native_identifiers_cannot_be_lossily_coerced(venue,field,bad):
    row = trade() if venue=='gate' else dict(id=1,orderId=2,symbol='XUSDT',side='SELL',price='2',qty='1',
                                            quoteQty='2',commission='0',commissionAsset='USDT',time=1000)
    key = 'id' if field=='id' else 'order_id' if venue=='gate' else 'orderId'
    row[key]=bad
    n=gate([row]) if venue=='gate' else aster([row])
    with pytest.raises((at.AsterError,gt.GateError)):
        n.history_fills('X_USDT' if venue=='gate' else 'XUSDT',0)
