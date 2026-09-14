"""AC19 root-operation mapping: real store transactions, counters and recovery evidence."""
from __future__ import annotations

import json
from decimal import Decimal as D

import pytest

from funding_bot import config
from funding_bot.trade import operation_roots as roots
from funding_bot.trade import store, tconfig
from funding_bot.trade.store import ClipState, IntentStatus, OpState
from funding_bot.trade.types import ClipPlan, Plan


INST = "sha256:fixture-instrument"
APPROVAL_1 = {"side": "entry", "target_raw": 100, "max_total_pct": "0.40"}
APPROVAL_2 = {"side": "entry", "target_raw": 60, "max_total_pct": "0.35"}


@pytest.fixture
def con(tmp_path):
    db = store.connect(tmp_path / "trade.db")
    yield db
    db.close()


def deal(con, did="DROOT1", *, sim=True):
    store.create_deal(
        con, coin="ROOT", chain="bsc", token="0x" + "ab" * 20, token_dec=18,
        perp_venue="aster", symbol="ROOTUSDT", leg_usd=D(100), owner_json="{}",
        sim=sim, deal_id=did, now=1,
    )
    return store.get_deal(con, did)


def plan(did, kind, *amounts):
    return Plan(
        deal_id=did, kind=kind, coin="ROOT", spot="okx·bsc", perp="aster", symbol="ROOTUSDT",
        leg_usd=D(100), clips=[ClipPlan(i + 1, amount, []) for i, amount in enumerate(amounts)],
        est={}, inputs={}, missing_owner_keys=[], expires=10_000,
    )


def spec(kind, approval, **extra):
    base = {"kind": kind, "inst_hash": INST, "approval": approval}
    base.update(extra)
    return base


def approve(con, iid, nonce):
    """Compose the existing CAS and new helper exactly as store integration does."""
    with store.tx(con):
        assert store.approve_intent(con, iid, nonce, now=2)
        op = store.operation_of_intent(con, iid)
        # Parent integration invokes approve_linked from approve_intent. The fallback keeps this test
        # runnable on the helper's isolated branch before that integration is cherry-picked.
        if op is not None and op["state"] != OpState.APPROVED:
            roots.approve_linked(con, store.get_intent(con, iid))


def running(con, iid, nonce):
    approve(con, iid, nonce)
    assert store.set_intent_status(con, iid, IntentStatus.RUNNING, expect=IntentStatus.APPROVED)
    assert roots.start_linked(con, store.get_intent(con, iid)) == store.operation_of_intent(con, iid)["id"]


def row_count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_propose_entry_is_one_atomic_root_and_copies_spec(con):
    d = deal(con)
    approved = dict(APPROVAL_1)
    source = spec("entry", approved, amount_raw=100)
    p = plan(d["id"], "entry", 60, 40)

    iid, nonce = roots.propose(
        con, deal=d, kind="entry", spec=source, plan=p, profile_id="bsc_okx_aster", chat=7,
    )

    assert nonce and source == spec("entry", approved, amount_raw=100)
    it = store.get_intent(con, iid)
    frozen = json.loads(it["spec_json"])
    op = store.operation_of_intent(con, iid)
    stable, decimals = config.OKX_DEX_STABLES[tconfig.chain_index("bsc")]
    assert frozen["operation_id"] == op["id"]
    assert frozen["approval"] == approved
    assert (op["target_kind"], op["target_asset"], op["target_decimals"], op["target_raw"]) == (
        "stable_raw_budget", stable, decimals, "100",
    )
    assert op["bounds_hash"] == store._json_hash(approved)
    assert (row_count(con, "operations"), row_count(con, "intents"), row_count(con, "operation_intents")) == (1, 1, 1)


@pytest.mark.parametrize(
    "bad_plan,bad_spec",
    [
        ({"clips": [{"dex_in_units": 60}, {"dex_in_units": 0}]}, spec("entry", APPROVAL_1)),
        ({"clips": [{"dex_in_units": 60}, {"dex_in_units": 40}]}, spec("entry", APPROVAL_1, amount_raw=99)),
        ({"clips": [{"dex_in_units": 100}]}, {"kind": "entry", "inst_hash": INST}),
    ],
)
def test_invalid_proposal_leaves_no_partial_rows(con, bad_plan, bad_spec):
    d = deal(con)
    with pytest.raises(store.StoreError):
        roots.propose(
            con, deal=d, kind="entry", spec=bad_spec, plan=bad_plan,
            profile_id="bsc_okx_aster", chat=None,
        )
    assert (row_count(con, "operations"), row_count(con, "intents"), row_count(con, "operation_intents")) == (0, 0, 0)


def test_approval_and_start_validate_actual_link_and_bounds(con):
    d = deal(con)
    iid, nonce = roots.propose(
        con, deal=d, kind="entry", spec=spec("entry", APPROVAL_1), plan=plan(d["id"], "entry", 100),
        profile_id="bsc_okx_aster", chat=None,
    )
    op_id = store.operation_of_intent(con, iid)["id"]

    approve(con, iid, nonce)
    op = store.get_operation(con, op_id)
    assert (op["state"], op["approval_version"], op["bounds_hash"]) == (
        OpState.APPROVED, 1, store._json_hash(APPROVAL_1),
    )
    assert store.set_intent_status(con, iid, IntentStatus.RUNNING, expect=IntentStatus.APPROVED)
    assert roots.start_linked(con, store.get_intent(con, iid)) == op_id
    assert store.get_operation(con, op_id)["state"] == OpState.RUNNING
    with pytest.raises(store.StoreError, match="cannot start"):
        roots.start_linked(con, store.get_intent(con, iid))


def test_failed_link_validation_rolls_back_intent_cas(con):
    d = deal(con)
    iid, nonce = store.create_intent(
        con, deal_id=d["id"], kind="entry",
        spec=spec("entry", APPROVAL_1, operation_id="OMISSING"), plan=plan(d["id"], "entry", 100), now=1,
    )
    with pytest.raises(store.StoreError, match="no durable link"):
        with store.tx(con):
            assert store.approve_intent(con, iid, nonce, now=2)
            roots.approve_linked(con, store.get_intent(con, iid))
    assert store.get_intent(con, iid)["status"] == IntentStatus.PROPOSED
    assert row_count(con, "operations") == row_count(con, "operation_intents") == 0


@pytest.mark.parametrize("pause_state", [OpState.STOPPED, OpState.PAUSED_RISK])
def test_same_root_resume_keeps_target_and_counters_until_fresh_approval(con, pause_state):
    d = deal(con)
    iid, nonce = roots.propose(
        con, deal=d, kind="entry", spec=spec("entry", APPROVAL_1), plan=plan(d["id"], "entry", 100),
        profile_id="bsc_okx_aster", chat=None,
    )
    running(con, iid, nonce)
    op_id = store.operation_of_intent(con, iid)["id"]
    store.operation_reserve(con, op_id, 100)
    store.operation_settle(con, op_id, released_raw=100, executed_raw=40)
    assert store.set_operation_state(con, op_id, pause_state, expect=OpState.RUNNING)
    assert store.set_intent_status(con, iid, IntentStatus.PARTIAL, expect=IntentStatus.RUNNING)
    before = store.get_operation(con, op_id)

    source = spec("entry", APPROVAL_2, amount_raw=60, resume=True)
    iid2, nonce2 = roots.propose(
        con, deal=d, kind="entry", spec=source, plan=plan(d["id"], "entry", 60),
        profile_id="bsc_okx_aster", chat=9, operation_id=op_id,
    )
    proposed = store.get_operation(con, op_id)
    assert (proposed["target_raw"], proposed["confirmed_raw"], proposed["reserved_raw"]) == ("100", "40", "0")
    assert (proposed["bounds_hash"], proposed["approval_version"], proposed["state"]) == (
        before["bounds_hash"], 1, pause_state,
    )
    assert json.loads(store.get_intent(con, iid2)["spec_json"])["operation_id"] == op_id
    assert source.get("operation_id") is None

    approve(con, iid2, nonce2)
    approved = store.get_operation(con, op_id)
    assert (approved["target_raw"], approved["confirmed_raw"], approved["reserved_raw"]) == ("100", "40", "0")
    assert (approved["bounds_hash"], approved["approval_version"], approved["state"]) == (
        store._json_hash(APPROVAL_2), 2, OpState.APPROVED,
    )


def old_active(con, d, *, state=OpState.PARTIAL, reserve=0):
    oid = store.create_operation(
        con, deal_id=d["id"], profile_id="bsc_okx_aster", inst_hash=INST, mode="dry", side="entry",
        target_kind="stable_raw_budget", target_asset="OLD", target_decimals=18, target_raw=20,
    )
    store.set_operation_state(con, oid, OpState.APPROVED)
    store.set_operation_state(con, oid, OpState.RUNNING)
    if reserve:
        store.operation_reserve(con, oid, reserve)
    store.set_operation_state(con, oid, state)
    return oid


def test_approving_independent_root_abandons_resolved_old_root_atomically(con):
    d = deal(con)
    old_id = old_active(con, d)
    iid, nonce = roots.propose(
        con, deal=d, kind="entry", spec=spec("entry", APPROVAL_1), plan=plan(d["id"], "entry", 100),
        profile_id="bsc_okx_aster", chat=None,
    )
    approve(con, iid, nonce)
    assert store.get_operation(con, old_id)["state"] == OpState.ABANDONED
    assert store.operation_of_intent(con, iid)["state"] == OpState.APPROVED


@pytest.mark.parametrize("old_state,reserve", [(OpState.PAUSED_UNKNOWN, 10), (OpState.RUNNING, 0)])
def test_unresolved_old_root_blocks_new_approval_and_rolls_back(con, old_state, reserve):
    d = deal(con)
    old_id = old_active(con, d, state=old_state, reserve=reserve)
    iid, nonce = roots.propose(
        con, deal=d, kind="entry", spec=spec("entry", APPROVAL_1), plan=plan(d["id"], "entry", 100),
        profile_id="bsc_okx_aster", chat=None,
    )
    with pytest.raises(store.StoreError, match="blocks approval"):
        approve(con, iid, nonce)
    assert store.get_intent(con, iid)["status"] == IntentStatus.PROPOSED
    assert store.get_operation(con, old_id)["state"] == old_state
    assert store.operation_of_intent(con, iid)["state"] == OpState.PROPOSED


@pytest.mark.parametrize('amounts', [(123,), (60,63)])
def test_exit_target_is_exact_approved_snapshot(con,amounts):
    d = deal(con)
    approval = {"side": "exit", "target_raw": 123, "min_receive": 90}
    iid, _ = roots.propose(
        con, deal=d, kind="exit", spec=spec("exit", approval, units=123, all=True, perp_only=False),
        plan=plan(d["id"], "exit", *amounts), profile_id="bsc_okx_aster", chat=None,
    )
    op = store.operation_of_intent(con, iid)
    assert (op["target_kind"], op["target_asset"], op["target_decimals"], op["target_raw"]) == (
        "full_position_snapshot", d["token"], d["token_dec"], "123",
    )
    with pytest.raises(store.StoreError, match="sum of its spot clips"):
        roots.propose(
            con, deal=d, kind="exit", spec=spec("exit", approval, units=123, all=False, perp_only=False),
            plan=plan(d["id"], "exit", 100), profile_id="bsc_okx_aster", chat=None,
        )


def test_unlinked_legacy_is_unsupported_but_named_root_must_exist(con):
    d = deal(con)
    plain, _ = store.create_intent(con, deal_id=d["id"], kind="rehedge", spec={}, plan={})
    assert roots.approve_linked(con, store.get_intent(con, plain)) is None
    assert roots.start_linked(con, store.get_intent(con, plain)) is None
    named, _ = store.create_intent(
        con, deal_id=d["id"], kind="entry", spec={"operation_id": "OGONE"}, plan={},
    )
    with pytest.raises(store.StoreError, match="no durable link"):
        roots.start_linked(con, store.get_intent(con, named))


def legacy_intent(con, d, iid, kind, frozen_spec, frozen_plan, *, status, created):
    got, nonce = store.create_intent(
        con, deal_id=d["id"], kind=kind, spec=frozen_spec, plan=frozen_plan,
        intent_id=iid, now=created, ttl_s=100,
    )
    assert got == iid and store.approve_intent(con, iid, nonce, now=created + 1)
    assert store.set_intent_status(con, iid, IntentStatus.RUNNING, expect=IntentStatus.APPROVED)
    if status != IntentStatus.RUNNING:
        assert store.set_intent_status(con, iid, status, expect=IntentStatus.RUNNING)
    return iid


def clip(con, iid, seq, planned, *, state, executed=None):
    cid = store.create_clip(con, iid, seq, planned)
    if state != ClipState.PLANNED:
        assert store.set_clip_state(con, cid, ClipState.DEX_SENT, expect=ClipState.PLANNED)
    if state not in (ClipState.PLANNED, ClipState.DEX_SENT):
        fields = {} if executed is None else {"dex_in": executed, "dex_out": executed * 2}
        assert store.set_clip_state(con, cid, state, expect=ClipState.DEX_SENT, **fields)
    return cid


def test_adopt_legacy_replays_proven_and_unknown_evidence_once_without_rewrite(con):
    d = deal(con)
    legacy_spec = {"kind": "entry", "inst_hash": INST, "coin": "ROOT"}
    legacy_plan = {"clips": [{"seq": 1, "dex_in_units": 60}, {"seq": 2, "dex_in_units": 40}]}
    iid = legacy_intent(
        con, d, "ELEGACY1", "entry", legacy_spec, legacy_plan,
        status=IntentStatus.INTERRUPTED, created=10,
    )
    clip(con, iid, 1, 60, state=ClipState.DEX_OK, executed=50)
    clip(con, iid, 2, 40, state=ClipState.DEX_UNKNOWN)
    before = store.get_intent(con, iid)

    op_id = roots.adopt_legacy(con, d)
    op = store.get_operation(con, op_id)
    assert op_id == roots._legacy_id(iid)
    assert (op["target_raw"], op["confirmed_raw"], op["reserved_raw"], op["state"]) == (
        "100", "50", "40", OpState.PAUSED_UNKNOWN,
    )
    assert store.operation_remaining(op) == 10
    after = store.get_intent(con, iid)
    assert (after["spec_json"], after["plan_json"]) == (before["spec_json"], before["plan_json"])

    assert roots.adopt_legacy(con, d) == op_id
    assert row_count(con, "operations") == row_count(con, "operation_intents") == 1
    again = store.get_operation(con, op_id)
    assert (again["confirmed_raw"], again["reserved_raw"], again["state"]) == ("50", "40", OpState.PAUSED_UNKNOWN)


def test_adopt_legacy_exit_chain_uses_root_snapshot_and_all_execution(con):
    d = deal(con)
    root_spec = {"kind": "exit", "inst_hash": INST, "units": 100, "all": False, "perp_only": False,
                 "root": None, "root_units": 100}
    first = legacy_intent(
        con, d, "XLEGACY1", "exit", root_spec, {"clips": [{"dex_in_units": 100}]},
        status=IntentStatus.PARTIAL, created=10,
    )
    clip(con, first, 1, 100, state=ClipState.DEX_OK, executed=40)
    more_spec = dict(root_spec, units=60, root=first, resume=True)
    second = legacy_intent(
        con, d, "XLEGACY2", "exit", more_spec, {"clips": [{"dex_in_units": 60}]},
        status=IntentStatus.INTERRUPTED, created=20,
    )
    clip(con, second, 1, 60, state=ClipState.DEX_OK, executed=60)

    op_id = roots.adopt_legacy(con, d)
    op = store.get_operation(con, op_id)
    assert (op["target_kind"], op["target_raw"], op["confirmed_raw"], op["reserved_raw"], op["state"]) == (
        "token_raw_to_sell", "100", "100", "0", OpState.CLOSED,
    )
    links = con.execute(
        "SELECT intent_id,seq FROM operation_intents WHERE operation_id=? ORDER BY seq", (op_id,),
    ).fetchall()
    assert [tuple(r) for r in links] == [(first, 1), (second, 2)]


def test_adopt_legacy_missing_amount_fails_closed_without_rows(con):
    d = deal(con)
    iid = legacy_intent(
        con, d, "ELEGACY1", "entry", {"kind": "entry", "inst_hash": INST},
        {"clips": [{"dex_in_units": 100}]}, status=IntentStatus.INTERRUPTED, created=10,
    )
    clip(con, iid, 1, 100, state=ClipState.DEX_OK)
    with pytest.raises(store.StoreError, match="executed input"):
        roots.adopt_legacy(con, d)
    assert row_count(con, "operations") == row_count(con, "operation_intents") == 0


def test_adopt_does_not_revive_an_older_failure_after_later_completion(con):
    d = deal(con)
    common = {"kind": "entry", "inst_hash": INST}
    legacy_intent(
        con, d, "EOLDFAIL", "entry", common, {"clips": [{"dex_in_units": 100}]},
        status=IntentStatus.FAILED, created=10,
    )
    legacy_intent(
        con, d, "ENEWDONE", "entry", common, {"clips": [{"dex_in_units": 100}]},
        status=IntentStatus.DONE, created=20,
    )
    assert roots.adopt_legacy(con, d) is None
    assert row_count(con, "operations") == 0


def test_adopt_legacy_perp_only_is_explicitly_outside_spot_root(con):
    d = deal(con)
    legacy_intent(
        con, d, "XPERPONLY", "exit", {"kind": "exit", "inst_hash": INST, "perp_only": True, "units": 1},
        {"clips": [{"dex_in_units": 1}]}, status=IntentStatus.INTERRUPTED, created=10,
    )
    assert roots.adopt_legacy(con, d) is None
    assert row_count(con, "operations") == row_count(con, "operation_intents") == 0
