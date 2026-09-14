"""Frozen parent and scope ownership remain protected at approval/admission."""
from dataclasses import replace
from types import SimpleNamespace
import json
import time

import pytest

from funding_bot.trade import store
from funding_bot.trade.generic_operations import GenericOperationCoordinator, EventAttemptJournal
from common_adapter_fixtures import registry, spec, Transport, context
from test_generic_adapter_matrix import make_plan, setup


def parent(con, plan):
    return store.create_generic_deal(con, asset_id=plan.legs[0].asset_id,
        position_spec={'generic_position_v1': True, 'legs': plan.to_dict()['legs']},
        owner_json='{}', sim=True)


def test_two_draft_proposals_cannot_both_approve_the_same_native_scopes(tmp_path):
    con = store.connect(tmp_path / 'trade.db')
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short')
    plans = [make_plan(a, b, operation_id='op-' + str(i)) for i in range(2)]
    coordinator = GenericOperationCoordinator(con, registry(), None)
    proposals = []
    for plan in plans:
        did = parent(con, plan)
        proposals.append(coordinator.propose(deal=store.get_deal(con, did), plan=plan, profile_id='fixture'))
    assert coordinator.approve(*proposals[0])
    with pytest.raises(store.StoreError):
        coordinator.approve(*proposals[1])
    first_id = proposals[0][0]
    first_deal = store.get_intent(con, first_id)['deal_id']
    transports = [Transport(), Transport()]
    for transport in transports:
        transport.clock = time.time()
    journals = [EventAttemptJournal(con, deal_id=first_deal, intent_id=first_id,
                operation_id=plans[0].operation_id, leg_id=leg.leg_id) for leg in (a, b)]
    coordinator.context = context((a, b), transports, journals)
    assert coordinator.execute(first_id).state == store.OpState.OPEN
    # The global single approved/running guard no longer applies. Scope
    # ownership must still reject the old second draft's approval.
    with pytest.raises(store.StoreError, match='scope is already owned'):
        coordinator.approve(*proposals[1])
    assert store.get_intent(con, proposals[1][0])['status'] == store.IntentStatus.PROPOSED
    assert store.get_operation(con, plans[1].operation_id)['state'] == store.OpState.PROPOSED
    assert [len(t.sent) for t in transports] == [1, 1]


def test_proposal_cannot_replace_frozen_parent_account_or_instrument(tmp_path):
    con = store.connect(tmp_path / 'trade.db')
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short')
    original = make_plan(a, b)
    did = parent(con, original)
    substituted = make_plan(a, replace(b, account='different-account'))
    with pytest.raises(store.StoreError, match='frozen parent legs'):
        GenericOperationCoordinator(con, registry(), None).propose(
            deal=store.get_deal(con, did), plan=substituted, profile_id='fixture')
    assert not con.execute('SELECT 1 FROM intents').fetchone()
    assert not con.execute('SELECT 1 FROM operations').fetchone()


def test_parent_changed_after_approval_is_rejected_before_any_native_action(tmp_path):
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    frozen = json.loads(store.get_deal(con, did)['inst_json'])
    frozen['legs'][1]['account'] = 'different-account'
    con.execute('UPDATE deals SET inst_json=? WHERE id=?', (json.dumps(frozen), did))
    with pytest.raises(store.StoreError, match='frozen parent legs'):
        coordinator.execute(iid)
    assert [len(t.sent) for t in transports] == [0, 0]
    assert store.get_intent(con, iid)['status'] == store.IntentStatus.APPROVED
    assert store.get_operation(con, plan.operation_id)['reserved_raw'] == '0'
    assert store.get_deal(con, did)['state'] == store.DealState.DRAFT


def test_slow_preflight_cannot_extend_first_dispatch_approval_with_a_fresh_quote(tmp_path, monkeypatch):
    from funding_bot.trade import generic_operations as G
    from funding_bot.trade.adapters.native import NativeAdapter
    a, b = spec('fixture_cex_spot', 'lead', 'long'), spec('fixture_cex_perp', 'hedge', 'short')
    con, did, plan, iid, coordinator, transports = setup(tmp_path, a, b)
    clock = [time.time()]
    monkeypatch.setattr(G, 'time', SimpleNamespace(time=lambda: clock[0]))
    original = NativeAdapter.quote
    advanced = []
    def delayed(adapter, action, bounds):
        quote = original(adapter, action, bounds)
        if not advanced:
            advanced.append(True)
            clock[0] = plan.expires_at + 1
            for transport in transports:
                transport.clock = clock[0]
            quote = replace(quote, expires_at=clock[0] + 60)
        return quote
    monkeypatch.setattr(NativeAdapter, 'quote', delayed)
    with pytest.raises(store.StoreError, match='first dispatch approval expired'):
        coordinator.execute(iid)
    assert [len(t.sent) for t in transports] == [0, 0]
    assert store.get_operation(con, plan.operation_id)['reserved_raw'] == '0'
    assert not con.execute('SELECT 1 FROM exec_events WHERE kind=?', (G.EVENT_DISPATCH,)).fetchone()
