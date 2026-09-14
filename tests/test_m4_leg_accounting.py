from decimal import Decimal as D

from funding_bot.trade import store
from funding_bot.trade.leg_accounting import ExecutionFact, Fee, record_fact, rebuild
from funding_bot.trade.adapters.contracts import Capabilities, LegSpec, NativeRef, QuoteAmount, Result, Status


def _con(tmp_path):
    return store.connect(tmp_path / "trade.db")


def _fact(*, leg="spot", account="acct:a", ref="order-1", side="BUY", qty="10", fees=(), funding=()):
    return ExecutionFact("op-1", leg, "spec-" + leg, account, ref, side, D(qty), D("1"), "confirmed",
                         fees=tuple(fees), funding=tuple(funding), base_currency="BASE",
                         settlement_currency="USD")


def test_record_is_durable_and_rebuilds_after_reopen(tmp_path):
    con = _con(tmp_path)
    assert record_fact(con, _fact(fees=(Fee(D(".1"), "BASE"),)))
    con.close()
    con = _con(tmp_path)
    report = rebuild(con)
    assert report["legs"][0]["qty"] == "9.9"
    assert report["legs"][0]["fees"] == {"BASE": "0.1"}


def test_duplicate_is_idempotent_but_changed_fact_conflicts(tmp_path):
    con = _con(tmp_path)
    fact = _fact()
    assert record_fact(con, fact) is True
    assert record_fact(con, fact) is False
    try:
        record_fact(con, _fact(qty="11"))
    except ValueError as exc:
        assert "conflicting" in str(exc)
    else:
        raise AssertionError("changed durable identity must be rejected")


def test_same_native_order_id_isolated_by_account_scope(tmp_path):
    con = _con(tmp_path)
    assert record_fact(con, _fact(account="acct:a", ref="same"))
    assert record_fact(con, _fact(account="acct:b", ref="same"))
    rows = rebuild(con)["legs"]
    assert len(rows) == 2  # distinct account scopes remain distinct projections
    assert {x["scope"] for x in rows} == {"acct:a", "acct:b"}


def test_unknown_fee_does_not_remove_quantity_and_funding_is_currency_scoped(tmp_path):
    con = _con(tmp_path)
    assert record_fact(con, _fact(fees=(Fee(D("1"), "BASE", known=False),),
                                funding=((D(".2"), "USDC"),)))
    leg = rebuild(con)["legs"][0]
    assert leg["qty"] == "10"
    assert leg["fees"] == {}
    assert leg["funding"] == {"USDC": "0.2"}


def test_two_perp_directions_are_separate_legs_and_currencies(tmp_path):
    con = _con(tmp_path)
    assert record_fact(con, _fact(leg="perp-long", account="p:a", side="BUY", ref="x",
                                  funding=((D("1"), "USDC"),)))
    assert record_fact(con, _fact(leg="perp-short", account="p:b", side="SELL", ref="x",
                                  funding=((D("2"), "USDT"),)))
    rows = {x["leg_id"]: x for x in rebuild(con)["legs"]}
    assert rows["perp-long"]["qty"] == "10"
    assert rows["perp-short"]["qty"] == "-10"
    assert rows["perp-long"]["funding"] == {"USDC": "1"}
    assert rows["perp-short"]["funding"] == {"USDT": "2"}


def test_real_result_maps_records_reopens_and_rebuilds(tmp_path):
    con = _con(tmp_path)
    spec = LegSpec("l1", "perp", "long", "fixture", "cex", "acct-1", "BTC-PERP", "BTC",
                   "proof", D("1"), D("1"), D("1"), "USDT", "USDT",
                   Capabilities("cex", "perpetual", short=True, reduce_only=True),
                   metadata_revision="fixture")
    result = Result(Status.SETTLED, D("2"), "filled", False, ("receipt",), terminal=True,
                    version=2, leg_id="l1", spec_hash=spec.fingerprint, scope=spec.scope,
                    native_ref=NativeRef("order", "77"),
                    perp_quote=QuoteAmount(D("100"), "USDT"), trade_notional=QuoteAmount(D("100"), "USDT"))
    from funding_bot.trade.leg_accounting import fact_from_result
    fact = fact_from_result(result, spec, operation_id="op-r", side="SELL")
    assert record_fact(con, fact)
    con.close()
    con = _con(tmp_path)
    assert rebuild(con, operation_id="op-r")["legs"][0]["qty"] == "-2"


def _spot_result(*, complete=False, embedded=False, amount=100, terminal=True):
    from funding_bot.trade.adapters.contracts import RawAmount
    from funding_bot.trade.fees import FeeComponent
    spec = LegSpec('spot:1', 'inventory', 'long', 'fixture', 'sol_best', 'wallet', 'mint',
                   'solana:mint', 'proof', D(1), D('.001'), D(1), 'quote', 'quote',
                   Capabilities('dex', 'spot', 'solana'), network='genesis', decimals=3,
                   metadata_revision='frozen')
    fee = FeeComponent('platform', 'mint', 3, amount, 'wallet', embedded, False)
    result = Result(Status.PARTIAL, D(2), 'final', False, ('proof',), fees=(fee,),
                    terminal=terminal, fees_complete=complete, version=2, leg_id=spec.leg_id,
                    spec_hash=spec.fingerprint, scope=spec.scope, native_ref=NativeRef('tx', 'tx1'),
                    spot_input_raw=RawAmount(asset_id='mint', raw=2000, decimals=3),
                    spot_output_raw=RawAmount(asset_id='quote', raw=3000, decimals=3))
    return spec, result


def test_terminal_replay_does_not_double_apply_fees_and_rejects_conflict(tmp_path):
    from dataclasses import replace
    import pytest
    from funding_bot.trade.leg_accounting import fact_from_result
    con = _con(tmp_path)
    spec, result = _spot_result(complete=True)
    fact = fact_from_result(result, spec, operation_id='op', side='SELL')
    assert record_fact(con, fact)
    assert not record_fact(con, fact)
    with pytest.raises(ValueError, match='conflicting'):
        record_fact(con, replace(fact, actual_qty=D(3)))
    leg = rebuild(con)['legs'][0]
    assert D(leg['qty']) == D('-2.1') and D(leg['fees']['solana:mint']) == D('.1')
    assert leg['executions'] == 1


def test_partial_nonterminal_is_not_an_accounting_fact():
    import pytest
    from funding_bot.trade.leg_accounting import fact_from_result
    spec, result = _spot_result(terminal=False)
    with pytest.raises(ValueError, match='proven final'):
        fact_from_result(result, spec, operation_id='op', side='SELL')


def test_embedded_fee_and_unknown_fee_quality_survive_reopen_and_dto(tmp_path):
    from funding_bot.trade.leg_accounting import fact_from_result
    from funding_bot.ipc.leg_reports import from_projection, to_dict
    con = _con(tmp_path)
    spec, result = _spot_result(embedded=True)
    record_fact(con, fact_from_result(result, spec, operation_id='op', side='SELL'))
    con.close()
    con = _con(tmp_path)
    leg = to_dict(from_projection(rebuild(con)))['legs'][0]
    assert leg['qty'] == '-2'  # fee already included in the native debit
    assert leg['fees_complete'] is False
    con.close()


def test_unknown_amount_is_json_null_and_survives_reopen(tmp_path):
    from funding_bot.trade.leg_accounting import fact_from_result
    con = _con(tmp_path)
    spec, result = _spot_result(amount=None)
    record_fact(con, fact_from_result(result, spec, operation_id='op', side='SELL'))
    con.close()
    con = _con(tmp_path)
    leg = rebuild(con)['legs'][0]
    assert leg['qty'] == '-2' and leg['unknown_fees'] == 1
    assert leg['fees_complete'] is False
