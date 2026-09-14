from decimal import Decimal as D
import os
import subprocess
import sys
import pytest
from funding_bot.core.approvals import ApprovalAction, decide
from funding_bot.trade import store


def proposed(tmp_path):
    con = store.connect(tmp_path/'trade.db')
    did = store.create_deal(con, coin='A', chain='bsc', token='0x'+'a'*40, token_dec=18,
                            perp_venue='aster', symbol='AUSDT', leg_usd=D(100), owner_json='{}', sim=True)
    iid, nonce = store.create_intent(con, deal_id=did, kind='entry', spec={}, plan={}, now=100)
    return con, ApprovalAction('ok', iid, nonce)


def test_decision_is_durable_and_retry_never_grants_second_submit(tmp_path):
    con, action = proposed(tmp_path)
    first = decide(con, action, now=101)
    assert first.reason == 'accepted' and first.submit and first.version == 1
    con.close()
    con = store.connect(tmp_path/'trade.db')
    repeat = decide(con, action, now=102)
    assert repeat.reason == 'already' and not repeat.submit


def test_pause_allows_cancel_but_not_approval(tmp_path):
    con, action = proposed(tmp_path)
    assert decide(con, action, paused=True, now=101).reason == 'paused'
    result = decide(con, ApprovalAction('no', action.intent_id, action.nonce), paused=True, now=101)
    assert result.applied and result.reason == 'cancelled' and not result.submit


def test_nonce_expiry_and_outer_transaction_fail_closed(tmp_path):
    con, action = proposed(tmp_path)
    wrong = ApprovalAction('ok', action.intent_id, '00000000' if action.nonce != '00000000' else 'ffffffff')
    assert decide(con, wrong, now=101).reason == 'old_button'
    con.execute('BEGIN')
    with pytest.raises(ValueError):
        decide(con, action, now=101)
    con.rollback()
    assert store.get_intent(con, action.intent_id)['status'] == 'proposed'
    expired = decide(con, action, now=100000)
    assert expired.reason == 'expired' and not expired.submit


def test_approval_domain_import_does_not_load_telegram_or_presenters():
    code = ("import sys; import funding_bot.core.approvals; import funding_bot.core.authority; "
            "import funding_bot.operator_commands; "
            "assert not any(x.startswith('funding_bot.tg') for x in sys.modules)")
    subprocess.run([sys.executable, '-c', code], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), check=True)


def test_neutral_source_cannot_use_bool_id_or_contradictory_conversation():
    from funding_bot.ipc.source import OperatorSource
    with pytest.raises(ValueError):
        OperatorSource('action', 1, user_id=True)
    with pytest.raises(ValueError):
        OperatorSource('action', 1, user_id=1, chat_id=2, has_conversation=False)


def test_source_authorization_keeps_owner_private_and_stale_gates():
    from funding_bot.ipc.source import OperatorSource
    from funding_bot.core import authority as a
    source = OperatorSource('message', 1, user_id=10, chat_id=10, text='стоп', date=100,
                            private=True, has_conversation=True)
    assert a.classify(source, 10, now=101).is_owner
    assert not a.classify(source, 11, now=101).is_owner
    assert a.classify(source, 10, now=1000).verdict == a.STALE
