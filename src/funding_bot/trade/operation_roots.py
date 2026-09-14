"""Transactional root-operation helpers shared by native execution flows.

The existing ``operations`` row is the only budget authority.  Intents are
approval attempts linked to that immutable root; clips and venue journals remain
execution evidence.  These helpers never rewrite historical intent/deal JSON.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import config
from . import store, tconfig
from .store import ClipState, IntentStatus, OpState

_RESUMABLE = frozenset({OpState.PARTIAL, OpState.STOPPED, OpState.PAUSED_RISK})
_PROVEN_SPOT = frozenset({ClipState.DEX_OK, ClipState.PERP_SENT, ClipState.BALANCED, ClipState.HEDGE_DEFICIT})
_UNKNOWN_SPOT = frozenset({ClipState.DEX_SENT, ClipState.DEX_UNKNOWN})
_NO_SPOT_EFFECT = frozenset({ClipState.PLANNED, ClipState.DEX_REVERTED})
_UNFINISHED = frozenset({IntentStatus.INTERRUPTED, IntentStatus.PARTIAL, IntentStatus.FAILED})


def _dict(value: Any, what: str) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise store.StoreError(f"{what}: expected object")


def _raw(value: Any, what: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise store.StoreError(f"{what}: invalid raw quantity {value!r}")
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raise store.StoreError(f"{what}: invalid raw quantity {value!r}") from None
    if str(value).strip() != str(raw) and not isinstance(value, int):
        raise store.StoreError(f"{what}: non-integral raw quantity {value!r}")
    if raw < 0 or (positive and raw == 0):
        raise store.StoreError(f"{what}: raw quantity must be {'positive' if positive else 'nonnegative'}")
    return raw


def _plan_clips(plan: Any) -> list[dict]:
    data = _dict(plan, "plan")
    clips = data.get("clips")
    if not isinstance(clips, list):
        raise store.StoreError("plan.clips: expected list")
    return [_dict(c, "plan clip") for c in clips]


def _planned_raw(kind: str, spec: Mapping, plan: Any) -> int:
    clips = _plan_clips(plan)
    if kind == "entry":
        amounts = [_raw(c.get("dex_in_units"), "entry clip input", positive=True) for c in clips]
        if not amounts:
            raise store.StoreError("entry root has no positive spot clips")
        planned = sum(amounts)
        if spec.get("amount_raw") is not None and \
                _raw(spec["amount_raw"], "entry approved amount", positive=True) != planned:
            raise store.StoreError("entry approved amount does not match its spot clips")
        return planned
    if kind == "exit":
        if spec.get("perp_only"):
            raise store.StoreError("perp-only intent has no spot root target")
        approved = _raw(spec.get("units"), "exit approved token snapshot", positive=True)
        amounts = [_raw(c.get("dex_in_units"), "exit clip input", positive=True) for c in clips]
        if not amounts or sum(amounts) != approved:
            raise store.StoreError("exit approved token snapshot does not match the sum of its spot clips")
        return approved
    raise store.StoreError(f"intent kind {kind!r} has no spot root target")


def _target(deal: Mapping, kind: str, spec: Mapping, plan: Any) -> tuple[str, str, int, int]:
    raw = _planned_raw(kind, spec, plan)
    if kind == "exit":
        return ("full_position_snapshot" if spec.get("all") else "token_raw_to_sell",
                str(deal["token"]), int(deal["token_dec"]), raw)
    chain = str(deal["chain"])
    if chain.lower() in {"sol", "solana", "solana-mainnet"}:
        try:
            inst = json.loads(deal.get("inst_json") or "{}")
            return "stable_raw_budget", str(inst["quote_mint"]), int(inst["quote_dec"]), raw
        except (KeyError, TypeError, ValueError):
            raise store.StoreError("entry root: Solana quote identity is missing") from None
    try:
        asset, decimals = config.OKX_DEX_STABLES[tconfig.chain_index(chain)]
    except (KeyError, ValueError):
        raise store.StoreError(f"entry root: stable identity is unknown for chain {chain!r}") from None
    return "stable_raw_budget", asset, int(decimals), raw


def _spec(intent: Mapping) -> dict:
    return _dict(intent.get("spec_json"), f"intent {intent.get('id')} spec")


def _plan(intent: Mapping) -> dict:
    return _dict(intent.get("plan_json"), f"intent {intent.get('id')} plan")


def _linked(con, intent: Mapping) -> tuple[dict, dict] | tuple[None, dict]:
    spec = _spec(intent)
    op = store.operation_of_intent(con, intent["id"])
    wanted = spec.get("operation_id")
    if op is None:
        if wanted:
            raise store.StoreError(f"intent {intent['id']} names operation {wanted} but has no durable link")
        return None, spec
    if wanted and wanted != op["id"]:
        raise store.StoreError(f"intent {intent['id']} links {op['id']} but spec names {wanted}")
    if op["deal_id"] != intent["deal_id"] or op["side"] != intent["kind"]:
        raise store.StoreError(f"intent {intent['id']} does not match root deal/side")
    if not spec.get("inst_hash") or spec["inst_hash"] != op["inst_hash"]:
        raise store.StoreError(f"intent {intent['id']} instrument hash does not match root")
    approval = spec.get("approval")
    if not isinstance(approval, dict):
        raise store.StoreError(f"intent {intent['id']} has no approval bounds")
    return op, spec


def propose(con, *, deal: Mapping, kind: str, spec: Mapping, plan: Any, profile_id: str, chat: int | None,
            operation_id: str | None = None) -> tuple[str, str]:
    """Create a new approval intent, optionally continuing the same root.

    The caller's spec is copied.  Bounds on an existing root remain unchanged
    until ``approve_linked`` atomically accepts the fresh intent.
    """
    source = dict(spec)
    approval = source.get("approval")
    if not isinstance(approval, dict):
        raise store.StoreError("root proposal requires explicit approval bounds")
    inst_hash = source.get("inst_hash")
    if not isinstance(inst_hash, str) or not inst_hash:
        raise store.StoreError("root proposal requires instrument hash")
    target_kind, asset, decimals, planned = _target(deal, kind, source, plan)
    with store.tx(con):
        from .adapters.obligations import require_resolved
        require_resolved(con, deal)
        if operation_id is None:
            op_id = store.create_operation(
                con, deal_id=deal["id"], profile_id=profile_id, inst_hash=inst_hash,
                mode="dry" if deal.get("sim") else "live", side=kind, target_kind=target_kind,
                target_asset=asset, target_decimals=decimals, target_raw=planned, bounds=approval,
            )
        else:
            op_id = operation_id
            op = store.get_operation(con, op_id)
            if op is None:
                raise store.StoreError(f"operation {op_id} is missing")
            expected = (deal["id"], profile_id, inst_hash, kind, asset, decimals)
            actual = (op["deal_id"], op["profile_id"], op["inst_hash"], op["side"],
                      op["target_asset"], int(op["target_decimals"]))
            if actual != expected or op["target_kind"] not in store.OP_TARGETS.get(kind, ()) or \
                    op["state"] not in _RESUMABLE or op["reserved_raw"] != "0":
                raise store.StoreError(f"operation {op_id} cannot accept a continuation intent")
            if planned != store.operation_remaining(op):
                raise store.StoreError(f"operation {op_id}: planned {planned} != remaining {store.operation_remaining(op)}")
        fresh_spec = dict(source, operation_id=op_id)
        iid, nonce = store.create_intent(con, deal_id=deal["id"], kind=kind, spec=fresh_spec, plan=plan, chat=chat)
        store.link_intent(con, op_id, iid)
    return iid, nonce


def approve_linked(con, intent: Mapping) -> str | None:
    """Validate and approve the root inside the caller's intent-CAS transaction."""
    with store.tx(con):
        op, spec = _linked(con, intent)
        if op is None:
            return None
        from .adapters.obligations import require_resolved
        require_resolved(con, store.get_deal(con, intent["deal_id"]))
        planned = _planned_raw(intent["kind"], spec, _plan(intent))
        remaining = store.operation_remaining(op)
        if planned != remaining:
            raise store.StoreError(f"operation {op['id']}: approved plan {planned} != remaining {remaining}")
        target_kind, asset, decimals, _ = _target(
            store.get_deal(con, intent["deal_id"]), intent["kind"], spec, _plan(intent),
        )
        if (op["target_asset"], int(op["target_decimals"])) != (asset, decimals) or \
                (op["state"] == OpState.PROPOSED and op["target_kind"] != target_kind):
            raise store.StoreError(f"operation {op['id']}: approved plan does not match root target identity")
        approval = spec["approval"]
        approval_hash = store._json_hash(approval)
        if op["state"] == OpState.PROPOSED:
            if op["bounds_hash"] != approval_hash:
                raise store.StoreError(f"operation {op['id']}: proposed bounds do not match intent")
            older = store.active_operation(con, op["deal_id"])
            if older is not None and older["id"] != op["id"]:
                if older["state"] not in _RESUMABLE or older["reserved_raw"] != "0":
                    raise store.StoreError(f"operation {older['id']}: unresolved active root blocks approval")
                store.set_operation_state(con, older["id"], OpState.ABANDONED,
                                          expect=older["state"], reason=f"superseded by approved {op['id']}")
            store.set_operation_state(con, op["id"], OpState.APPROVED, expect=OpState.PROPOSED)
        elif op["state"] in _RESUMABLE:
            if op["reserved_raw"] != "0":
                raise store.StoreError(f"operation {op['id']}: unresolved reserve blocks approval")
            store.set_operation_state(con, op["id"], OpState.APPROVED, expect=op["state"], bounds=approval)
        elif op["state"] != OpState.APPROVED:
            raise store.StoreError(f"operation {op['id']} in {op['state']} cannot be approved")
        return op["id"]


def start_linked(con, intent: Mapping) -> str | None:
    """Advance an approved linked root immediately before native execution."""
    with store.tx(con):
        op, _ = _linked(con, intent)
        if op is None:
            return None
        if op["state"] != OpState.APPROVED:
            raise store.StoreError(f"operation {op['id']} in {op['state']} cannot start")
        store.set_operation_state(con, op["id"], OpState.RUNNING, expect=OpState.APPROVED)
        return op["id"]


def _legacy_id(root_intent_id: str) -> str:
    return "OL" + hashlib.sha256(root_intent_id.encode()).hexdigest()[:20].upper()


def _legacy_chain(con, deal_id: str) -> tuple[list[dict], dict, dict] | None:
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM intents WHERE deal_id=? AND kind IN ('entry','exit') ORDER BY created,id", (deal_id,))]
    accepted = [r for r in rows if r["status"] not in {
        IntentStatus.PROPOSED, IntentStatus.REJECTED, IntentStatus.EXPIRED}]
    if not accepted or accepted[-1]["status"] not in _UNFINISHED:
        return None
    last = accepted[-1]
    last_spec = _spec(last)
    if last_spec.get("perp_only"):
        return None
    # A fresh entry quote can own a different root on the same draft deal.
    # Once a durable link exists it outranks chronological legacy inference.
    if last_spec.get("operation_id") or store.operation_of_intent(con, last["id"]) is not None:
        return [last], last, last_spec
    if last["kind"] == "entry":
        chain = [r for r in rows if r["kind"] == "entry" and r["created"] <= last["created"]]
        root = chain[0]
    else:
        root_id = last_spec.get("root") or last["id"]
        root = next((r for r in rows if r["id"] == root_id and r["kind"] == "exit"), None)
        if root is None:
            raise store.StoreError(f"legacy exit {last['id']}: root {root_id} is missing")
        chain = []
        for row in rows:
            if row["kind"] != "exit" or row["created"] > last["created"]:
                continue
            rs = _spec(row)
            if row["id"] == root_id or rs.get("root") == root_id:
                chain.append(row)
    if not chain:
        raise store.StoreError(f"legacy intent {last['id']}: empty root chain")
    return chain, root, _spec(root)


def adopt_legacy(con, deal: Mapping) -> str | None:
    """Map one interrupted legacy spot chain exactly once, without rewriting it.

    Missing approved targets or execution quantities abort the whole transaction.
    UNKNOWN reserves its original planned input and is never treated as zero.
    """
    with store.tx(con):
        return _adopt_legacy(con, deal)


def _adopt_legacy(con, deal: Mapping) -> str | None:
    found = _legacy_chain(con, deal["id"])
    if found is None:
        return None
    chain, root, root_spec = found
    if root_spec.get("operation_id"):
        linked, _ = _linked(con, root)
        return linked["id"]
    linked = store.operation_of_intent(con, root["id"])
    if linked is not None:
        if (linked["deal_id"], linked["side"], linked["inst_hash"]) != (
                deal["id"], root["kind"], root_spec.get("inst_hash")):
            raise store.StoreError("legacy durable root identity differs from its intent")
        return linked["id"]
    inst_hash = root_spec.get("inst_hash")
    if not isinstance(inst_hash, str) or not inst_hash:
        raise store.StoreError(f"legacy root {root['id']}: instrument hash is missing")
    try:
        from .runtime import profile_of_deal
        profile_id = profile_of_deal(deal)
    except Exception as exc:
        raise store.StoreError(f"legacy root {root['id']}: profile is unknown") from exc
    target_kind, asset, decimals, target = _target(deal, root["kind"], root_spec, _plan(root))
    op_id = _legacy_id(root["id"])
    chain_specs = [(row, _spec(row)) for row in chain]
    for row, row_spec in chain_specs:
        if row_spec.get("inst_hash") != inst_hash:
            raise store.StoreError(f"legacy intent {row['id']}: instrument hash differs from root")
        linked = store.operation_of_intent(con, row["id"])
        named = row_spec.get("operation_id")
        if named and (linked is None or linked["id"] != named):
            raise store.StoreError(f"legacy intent {row['id']} names operation {named} without that durable link")
    chain_ids = [r["id"] for r in chain]
    clips = [dict(r) for r in con.execute(
        f"SELECT * FROM clips WHERE intent_id IN ({','.join('?' for _ in chain_ids)}) ORDER BY id", tuple(chain_ids))]
    evidence: list[tuple[str, int, int]] = []
    confirmed = reserved = 0
    for clip in clips:
        state = clip["state"]
        if state in _PROVEN_SPOT:
            planned = _raw(clip.get("planned_in"), f"legacy clip {clip['id']} planned input", positive=True)
            executed = _raw(clip.get("dex_in"), f"legacy clip {clip['id']} executed input")
            if executed > planned:
                raise store.StoreError(f"legacy clip {clip['id']}: executed input exceeds reservation")
            evidence.append(("proven", planned, executed))
            confirmed += executed
        elif state in _UNKNOWN_SPOT:
            planned = _raw(clip.get("planned_in"), f"legacy clip {clip['id']} unknown reservation", positive=True)
            evidence.append(("unknown", planned, 0))
            reserved += planned
        elif state not in _NO_SPOT_EFFECT:
            raise store.StoreError(f"legacy clip {clip['id']}: unsupported state {state!r}")
    if confirmed + reserved > target:
        raise store.StoreError(f"legacy root {root['id']}: confirmed+reserved exceeds immutable target")
    with store.tx(con):
        existing = store.get_operation(con, op_id)
        if existing is not None:
            expected = (deal["id"], profile_id, inst_hash, root["kind"], target_kind, asset, decimals, target)
            actual = (existing["deal_id"], existing["profile_id"], existing["inst_hash"], existing["side"],
                      existing["target_kind"], existing["target_asset"], int(existing["target_decimals"]),
                      int(existing["target_raw"]))
            if actual != expected:
                raise store.StoreError(f"legacy operation id collision for {root['id']}")
            for row in chain:
                store.link_intent(con, op_id, row["id"])
            return op_id
        if any(store.operation_of_intent(con, row["id"]) is not None for row in chain):
            raise store.StoreError(f"legacy root {root['id']}: chain already belongs to another operation")
        store.create_operation(con, deal_id=deal["id"], profile_id=profile_id, inst_hash=inst_hash,
                               mode="dry" if deal.get("sim") else "live", side=root["kind"],
                               target_kind=target_kind, target_asset=asset, target_decimals=decimals,
                               target_raw=target, bounds=None, op_id=op_id)
        for row in chain:
            store.link_intent(con, op_id, row["id"])
        store.set_operation_state(con, op_id, OpState.APPROVED, expect=OpState.PROPOSED)
        store.set_operation_state(con, op_id, OpState.RUNNING, expect=OpState.APPROVED)
        for verdict, planned, executed in evidence:
            store.operation_reserve(con, op_id, planned)
            if verdict == "proven":
                store.operation_settle(con, op_id, released_raw=planned, executed_raw=executed)
        op = store.get_operation(con, op_id)
        if reserved:
            final = OpState.PAUSED_UNKNOWN
        elif confirmed == target:
            final = OpState.OPEN if root["kind"] == "entry" else OpState.CLOSED
        elif confirmed:
            final = OpState.PARTIAL
        else:
            final = OpState.STOPPED
        store.set_operation_state(con, op_id, final, expect=OpState.RUNNING, reason="legacy interrupted adoption")
        return op_id


def abandon_unstarted(con, deal_id: str) -> None:
    """Release only a root with proof that no spot/perpetual action began."""
    with store.tx(con):
        op = store.active_operation(con, deal_id)
        if op is None:
            return
        if int(op['reserved_raw']) or int(op['confirmed_raw']):
            raise store.StoreError('cannot abandon a root with executed or reserved input')
        unsafe = con.execute(
            "SELECT 1 FROM clips c JOIN operation_intents oi ON oi.intent_id=c.intent_id "
            "WHERE oi.operation_id=? AND c.state NOT IN ('PLANNED','DEX_REVERTED') LIMIT 1", (op['id'],)).fetchone()
        orders = con.execute(
            "SELECT p.state,p.executed_qty FROM perp_orders p JOIN clips c ON c.id=p.clip_id "
            "JOIN operation_intents oi ON oi.intent_id=c.intent_id WHERE oi.operation_id=?", (op['id'],)).fetchall()
        for state, quantity in orders:
            try:
                qty = Decimal(quantity)
                no_effect = state in {'NOT_PLACED', 'REJECTED', 'EXPIRED'} and qty.is_finite() and qty == 0
            except (TypeError, InvalidOperation):
                no_effect = False
            if not no_effect:
                raise store.StoreError('unstarted root has unresolved perpetual execution evidence')
        if unsafe:
            raise store.StoreError('unstarted root has unresolved spot execution evidence')
        if op['state'] != OpState.STOPPED:
            store.set_operation_state(con, op['id'], OpState.STOPPED, reason='no action started')
        store.set_operation_state(con, op['id'], OpState.ABANDONED, reason='no action started')
