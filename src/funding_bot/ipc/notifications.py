"""Versioned operator notifications: facts only, no markup or transport client."""
import json

DTO_VERSION = 2
EXECUTION_REPORT_VERSION = 3
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
