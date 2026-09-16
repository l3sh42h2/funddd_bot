"""Versioned operator notifications: facts only, no markup or transport client."""
import json

DTO_VERSION = 2
EXECUTION_REPORT_VERSION = 3
GENERIC_POSITION_VERSION = 4
EXECUTION_NOTICE_VERSION = 5
PROPOSAL_VIEW_VERSION = 6
APPROVAL_REASONS = frozenset((
    'unknown', 'cancelled', 'paused', 'busy', 'accepted', 'stale', 'old_button',
    'expired', 'already', 'running', 'already_cancelled', 'done', 'interrupted',
))
NOTICE_FIELDS = {
    'command_stale': {'date'}, 'operator_identity': {'chat_id', 'user_id'},
    'configuration_error': {'reason'}, 'command_unknown': {'reason'},
    'planning_started': {'coin', 'side'}, 'pause_changed': {'already', 'clip', 'clips'},
    'resume_changed': {'was', 'interrupted'}, 'error': {'reason'},
    'help_requested': {'sim', 'sol'}, 'plan_requoted': {'reason', 'sim'},
}

# Execution notifications carry facts only.  The interface chooses the Telegram
# renderer; the engine has no formatting or transport dependency for these
# paths.  Decimal values are encoded by ipc.reports.encode.
EXECUTION_NOTICE_FIELDS = {
    'executor_crash': frozenset(('intent_id', 'error')),
    'refused': frozenset(('reason',)),
    'busy': frozenset(('intent_id',)),
    'owner_missing': frozenset(('keys', 'action')),
    'owner_config_error': frozenset(('reason',)),
    'resume_checked': frozenset(('deal_id', 'sim')),
    'resume_mismatch': frozenset(('deal_id', 'detail', 'sim')),
    'halt': frozenset(('intent_id', 'kind', 'coin', 'reason', 'perp_venue', 'deal_id', 'clip', 'clips',
                       'perp_pos', 'wallet_tokens', 'unhedged_qty', 'unhedged_usd', 'auto_unwind_s', 'sim',
                       'ts', 'clips_done', 'state', 'step', 'm')),
    'auto_unwind': frozenset(('coin', 'qty', 'usd', 'sim')),
    'progress': frozenset(('intent_id', 'kind', 'coin', 'clip', 'clips', 'spot_usd', 'spot_qty', 'spot_avg',
                           'perp_qty', 'perp_usd', 'perp_avg', 'imbalance_qty', 'imbalance_usd', 'gas_usd',
                           'fees_usd', 'note', 'sim', 'total_usd', 'step', 'm')),
    'perp_closed': frozenset(('coin', 'deal_id', 'qty', 'usd', 'spot_qty', 'spot_usd', 'step', 'sim', 'm')),
    'fix_done': frozenset(('kind', 'coin', 'deal_id', 'state', 'qty', 'usd', 'side', 'noop', 'delta', 'step', 'sim', 'm')),
    'sol_halt': frozenset(('kind', 'coin', 'deal_id', 'intent_id', 'reason', 'state', 'wallet_tokens', 'perp_pos',
                           'delta', 'delta_usd', 'need_qty', 'sim')),
    # intent_id is routing only (which message to edit), like EVM 'progress' — sol_views.progress() never
    # renders it into text.
    'sol_progress': frozenset(('intent_id', 'kind', 'coin', 'fullcoin', 'stage', 'path', 'tokens', 'perp_qty', 'sim')),
}

# Proposal (button-plan) views carry facts only, same spirit as execution notices, but for the
# desk-plan/fix-plan text the owner approves with a button (Desk.propose_* → interface.presenter
# renders the same PlanView/FixPlanView/SolPlanView-shaped text as before). Unlike execution
# notices, a handful of fields are short tuples of scalars (warning notes, per-clip USD amounts).
PROPOSAL_VIEW_FIELDS = {
    'plan': frozenset(('intent_id', 'kind', 'coin', 'chain', 'perp_venue', 'symbol', 'leg_usd', 'clips_usd',
                       'token_qty', 'perp_qty', 'deal_id', 'perp_only', 'ttl_s', 'sim', 'exit_all', 'req_usd',
                       'deal_leg_usd', 'resume', 'step', 'leverage', 'margin_type', 'impact_usd', 'gas_usd_clip',
                       'gas_usd_total', 'approve_gas_usd', 'native_px', 'total_usd', 'exit_cost_usd', 'breakeven_h',
                       'funding_pct_h', 'usd_per_h', 'basis_pct', 'min_funding_pct_h', 'min_basis_bps',
                       'wallet_stable', 'wallet_native', 'margin_avail', 'open_deals', 'max_open_deals',
                       'missing_owner_keys', 'notes', 'm', 'exit_root_qty')),
    'sol_plan': frozenset(('intent_id', 'kind', 'coin', 'fullcoin', 'deal_id', 'usdc', 'usdc_min', 'tokens',
                           'tokens_min', 'perp_qty', 'perp_px', 'path', 'others', 'basis_bps', 'basis_gross_bps',
                           'spot_fee_usd', 'perp_fee_usd', 'funding_pct_h', 'leverage', 'identity', 'notes',
                           'missing', 'sim', 'ttl_s', 'margin_need', 'margin_avail')),
    'fix_plan': frozenset(('intent_id', 'kind', 'coin', 'deal_id', 'delta', 'qty', 'side', 'usd', 'perp_venue',
                           'step', 'ttl_s', 'sim', 'm')),
}


def validate_notice(topic, facts):
    if topic not in NOTICE_FIELDS or not isinstance(facts, dict) or set(facts) != NOTICE_FIELDS[topic]:
        raise ValueError('invalid operator notice')
    # Only public scalar facts (or a list of intent IDs) cross this boundary.
    for name, value in facts.items():
        if name == 'interrupted':
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError('invalid interrupted IDs')
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ValueError('invalid notice fact')


def _scalar_fact(value):
    from decimal import Decimal
    if value is None or type(value) in (str, int, float, bool, Decimal):
        return True
    # A short tuple/list of scalars (warning notes, missing owner keys, per-clip USD amounts,
    # other-route summaries) — never HTML, never a nested object.
    if isinstance(value, (list, tuple)):
        return all(type(x) in (str, int, float, bool, Decimal) for x in value)
    return False


def validate_execution_notice(topic, facts):
    if topic not in EXECUTION_NOTICE_FIELDS or not isinstance(facts, dict) or set(facts) != EXECUTION_NOTICE_FIELDS[topic]:
        raise ValueError('invalid execution notice')
    for value in facts.values():
        if not _scalar_fact(value):
            raise ValueError('invalid execution notice fact')


def validate_proposal_view(topic, facts):
    if topic not in PROPOSAL_VIEW_FIELDS or not isinstance(facts, dict) or set(facts) != PROPOSAL_VIEW_FIELDS[topic]:
        raise ValueError('invalid proposal view')
    for value in facts.values():
        if not _scalar_fact(value):
            raise ValueError('invalid proposal view fact')


def plan_summary(intent, deal=None):
    """Explicit public projection. Never serialize the frozen owner/wallet spec."""
    if intent is None:
        return {'intent': None, 'deal': None}
    try:
        spec = json.loads(intent['spec_json'] or '{}')
    except (TypeError, ValueError, KeyError, IndexError):
        spec = {}
    if not isinstance(spec, dict):
        spec = {}
    public = {k: spec[k] for k in ('coin', 'usd', 'all', 'perp_only', 'sim', 'profile') if k in spec}
    return {'intent': {'kind': intent['kind'], 'spec_json': json.dumps(public)},
            'deal': {k: deal[k] for k in ('coin', 'leg_usd', 'sim')} if deal is not None else None}
