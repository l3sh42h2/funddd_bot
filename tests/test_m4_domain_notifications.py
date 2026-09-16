import json
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest
from funding_bot.core.journal import Journal, Outbox
from funding_bot.core.commands import Bot
from funding_bot.ipc.notifications import APPROVAL_REASONS, plan_summary
from funding_bot.interface.presenter import present
from funding_bot.interface.runtime import Interface, State
from funding_bot.trade.engine import Conns
from funding_bot.trade import store
from funding_bot.tg import views

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/migration'))
import server_job as job


def test_public_summary_render_matches_legacy_and_excludes_wallet_spec():
    for kind in ('entry', 'exit'):
        for profile in ('evm', 'sol_best_hyperliquid'):
            for all_ in (True, False):
                spec = dict(coin='<A&>', usd='200', all=all_, perp_only=False, sim=True, profile=profile,
                            owner={'wallet': 'DO_NOT_EXPORT'}, inst={'account': 'DO_NOT_EXPORT'})
                it = dict(kind=kind, spec_json=json.dumps(spec))
                deal = dict(coin='A', leg_usd='200', sim=True, owner_json='DO_NOT_EXPORT')
                summary = plan_summary(it, deal)
                assert 'DO_NOT_EXPORT' not in json.dumps(summary)
                ev = dict(kind='plan_closed', dto_version=2, chat_id=42, message_id=8,
                          action='ok', at=123, summary=summary)
                h = views.intent_head(it, deal)
                rendered = present(ev)
                assert rendered['text'] == views.plan_closed('ok', h.title, 123, h.sim)
                assert rendered['reply_markup'] is None
    for reason in APPROVAL_REASONS:
        assert present(dict(kind='approval_reply', dto_version=2, callback_id='a', reason=reason))['text'] == getattr(views, 'CB_'+reason.upper())


def test_dto_fence_commits_with_event_and_prevents_older_rollback(tmp_path):
    conns = Conns(tmp_path/'trade.db')
    out = Outbox(Journal(conns))
    old = dict(compatible_readers=[1, 2], schema_version=2, dto_version=1)
    assert job.compatible_reader(old, job.database_info(tmp_path/'trade.db'))
    con = conns.get()
    con.execute('BEGIN')
    out.approval_reply('cb', 'accepted')
    con.rollback()
    assert out.journal.notifications() == []
    assert job.compatible_reader(old, job.database_info(tmp_path/'trade.db'))
    out.approval_reply('cb', 'accepted')
    db = job.database_info(tmp_path/'trade.db')
    assert not job.compatible_reader(old, db)
    assert job.compatible_reader(dict(old, dto_version=2), db)
    event = out.journal.notifications()[0]
    assert 'text' not in event
    out.ack(event['id'], {})
    assert not job.compatible_reader(old, job.database_info(tmp_path/'trade.db'))


def test_execution_notice_crosses_core_without_html_and_renders_at_interface(tmp_path):
    from funding_bot.ipc.notifications import EXECUTION_NOTICE_VERSION
    out = Outbox(Journal(Conns(tmp_path/'trade.db')))
    facts = dict(intent_id='I1', kind='entry', coin='<A>', clip=1, clips=2,
                 spot_usd=D('2'), spot_qty=D('1'), spot_avg=D('2'), perp_qty=D('1'), perp_usd=D('2'),
                 perp_avg=D('2'), imbalance_qty=D(0), imbalance_usd=D(0), gas_usd=D('0.01'), fees_usd=D(0),
                 note=None, sim=True, total_usd=D('4'), step=D('.1'), m=D(1))
    out.execution_notice(42, 'progress', facts)
    event = out.journal.notifications()[0]
    assert event['dto_version'] == EXECUTION_NOTICE_VERSION and 'text' not in event
    assert event['facts']['coin'] == '<A>'
    wire = present(event)
    assert wire['kind'] == 'send' and wire['silent'] is True and '&lt;A&gt;' in wire['text']
    with pytest.raises(ValueError, match='invalid execution notice'):
        out.execution_notice(42, 'progress', dict(facts, extra='not allowed'))


def test_new_execution_and_proposal_dtos_fence_rollback_readers(tmp_path):
    """Versions 5 and 6 are persisted reader fences, just like versions 2–4."""
    conns = Conns(tmp_path/'trade.db')
    out = Outbox(Journal(conns))
    progress = dict(intent_id='I1', kind='entry', coin='A', clip=1, clips=1,
                    spot_usd=D('2'), spot_qty=D('1'), spot_avg=D('2'), perp_qty=D('1'), perp_usd=D('2'),
                    perp_avg=D('2'), imbalance_qty=D(0), imbalance_usd=D(0), gas_usd=D(0), fees_usd=D(0),
                    note=None, sim=True, total_usd=D('2'), step=D(1), m=D(1))
    old = dict(compatible_readers=[1, 2], schema_version=2, dto_version=4)
    out.execution_notice(42, 'progress', progress)
    db = job.database_info(tmp_path/'trade.db')
    assert db['notification_dto_version'] == 5
    assert not job.compatible_reader(old, db)
    assert job.compatible_reader(dict(old, dto_version=5), db)

    con = conns.get()
    did = store.create_deal(con, coin='A', chain='bsc', token='0x'+'a'*40, token_dec=18,
                            perp_venue='aster', symbol='AUSDT', leg_usd=D(100), owner_json='{}', sim=True)
    iid, nonce = store.create_intent(con, deal_id=did, kind='entry', spec={'coin': 'A'}, plan={})
    out.plan_guard = lambda _: None
    summary = plan_summary(store.get_intent(con, iid), store.get_deal(con, did))
    facts = dict(intent_id=iid, kind='rehedge', coin='A', deal_id=did, delta=None, qty=None, side=None, usd=None,
                 perp_venue='aster', step=None, ttl_s=60, sim=True, m=None)
    out.plan_proposed(42, iid, nonce, summary, 'fix_plan', facts)
    db = job.database_info(tmp_path/'trade.db')
    assert db['notification_dto_version'] == 6
    assert not job.compatible_reader(dict(old, dto_version=5), db)
    assert job.compatible_reader(dict(old, dto_version=6), db)
    # A current core must also accept the reader fence it just persisted after
    # an ordinary restart.
    assert Journal(conns).notifications()[0]['dto_version'] == 5


def test_ui_retries_unrenderable_event_and_ack_restart_does_not_resend(tmp_path):
    sent = []
    events = [dict(id=1, kind='approval_reply', dto_version=999, callback_id='cb', reason='accepted')]
    class Remote:
        def call(self, method, params=None):
            if method == 'read_notifications':
                return {'events': list(events)}
            events[:] = []
            return {'acked': True}
    api = SimpleNamespace(answer_callback_query=lambda cb, text: sent.append((cb, text)))
    path = tmp_path/'ui.json'
    ui = Interface(api, None, Remote(), State(path))
    with pytest.raises(Exception, match='unsupported_notification_version'):
        ui.deliver_once()
    assert not ui.inflight and not sent and not ui.state.pending()
    events[0]['dto_version'] = 2
    ui.deliver_once()
    assert len(sent) == 1 and ui.state.pending()
    Interface(api, None, Remote(), State(path)).deliver_once()
    assert len(sent) == 1 and not events
    legacy = dict(kind='send', chat_id=42, text='previous queued message')
    assert present(legacy) == legacy


def test_delivery_failure_after_approval_cannot_prevent_submit_or_repeat_it(tmp_path):
    conns = Conns(tmp_path/'trade.db'); con = conns.get()
    did = store.create_deal(con, coin='A', chain='bsc', token='0x'+'a'*40, token_dec=18,
                            perp_venue='aster', symbol='AUSDT', leg_usd=D(100), owner_json='{}', sim=True)
    iid, nonce = store.create_intent(con, deal_id=did, kind='entry', spec={}, plan={}, now=100)
    submissions = []
    def fail(*a, **kw):
        raise RuntimeError('UI unavailable')
    bot = object.__new__(Bot)
    bot.conns = conns; bot.mode = 'dry'
    bot.engine = SimpleNamespace(pause_evt=threading.Event(), drain_evt=threading.Event(), submit=submissions.append)
    bot.poll_api = SimpleNamespace(approval_reply=fail)
    bot.sender = SimpleNamespace(plan_closed=fail)
    d = SimpleNamespace(text=f'ok:{iid}:{nonce}', callback_id='cb', chat_id=42, message_id=3)
    bot._press(d, 101); bot._press(d, 102)
    assert submissions == [iid]
    assert store.get_intent(con, iid)['status'] == 'approved'


def test_previous_journal_refuses_new_notifications_before_startup(tmp_path):
    old_source = (Path(__file__).parent/'fixtures/migration/journal_076bf30.py').read_text()
    namespace = {'__name__': 'funding_bot.core.previous_journal', '__package__': 'funding_bot.core'}
    exec(compile(old_source, 'previous_journal.py', 'exec'), namespace)
    conns = Conns(tmp_path/'trade.db')
    out = Outbox(Journal(conns))
    namespace['Journal'](conns)
    out.approval_reply('cb', 'accepted')
    with pytest.raises(Exception, match='unsupported_core_schema'):
        namespace['Journal'](conns)
    assert Journal(conns).notifications()[0]['reason'] == 'accepted'


def test_plan_guard_precedes_queue_and_ack_binds_original_intent(tmp_path):
    conns = Conns(tmp_path/'trade.db'); con = conns.get()
    did = store.create_deal(con, coin='A', chain='bsc', token='0x'+'a'*40, token_dec=18,
                            perp_venue='aster', symbol='AUSDT', leg_usd=D(100), owner_json='{}', sim=True)
    iid, nonce = store.create_intent(con, deal_id=did, kind='entry', spec={'coin': 'A'}, plan={})
    summary = plan_summary(store.get_intent(con, iid), store.get_deal(con, did))
    out = Outbox(Journal(conns))
    # AC-07: core hands the interface facts (view_topic/view_facts), not a rendered body — 'fix_plan' is the
    # smallest of the three proposal-view shapes.
    facts = dict(intent_id=iid, kind='rehedge', coin='A', deal_id=did, delta=None, qty=None, side=None, usd=None,
                perp_venue='aster', step=None, ttl_s=60, sim=True, m=None)
    with pytest.raises(Exception, match='plan_guard_missing'):
        out.plan_proposed(42, iid, nonce, summary, 'fix_plan', facts)
    assert out.journal.notifications() == []
    checked = []
    out.plan_guard = checked.append
    out.plan_proposed(42, iid, nonce, summary, 'fix_plan', facts)
    assert checked == [iid]
    event = out.journal.notifications()[0]
    rendered = present(event)
    assert rendered['reply_markup']['inline_keyboard'][0][0]['callback_data'] == f'ok:{iid}:{nonce}'
    out.ack(event['id'], {'message_id': 87})
    row = store.get_intent(con, iid)
    assert (row['chat'], row['msg_id'], row['status']) == (42, 87, 'proposed')


def test_rollback_rechecks_reader_after_late_drain_notification(tmp_path, monkeypatch):
    import subprocess
    paths = job.Paths(tmp_path/'opt', tmp_path/'state', tmp_path/'legacy')
    paths.execution_lock = tmp_path/'execution.lock'
    (paths.state/'core').mkdir(parents=True)
    out = Outbox(Journal(Conns(paths.state/'core/trade.db')))
    manifest = dict(release_id='old', schema_version=2, compatible_readers=[1, 2], dto_version=1)
    calls = []
    monkeypatch.setattr(job, 'wait_drain', lambda *a, **k: out.approval_reply('cb', 'paused'))
    monkeypatch.setattr(job, '_wait_inactive', lambda *a: None)
    monkeypatch.setattr(job, 'execution_lock_free', lambda *a: calls.append('lock_free'))
    monkeypatch.setattr(job, 'install_units', lambda *a: calls.append('units'))
    monkeypatch.setattr(job, 'switch_link', lambda *a: calls.append('switch'))
    class Client:
        def call(self, method, payload):
            if method == 'get_status':
                return dict(drain=True, drain_epoch='epoch-1')
            if method == 'get_drain_state':
                return dict(release_id='old')
            raise AssertionError(method)
    commands = SimpleNamespace(run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, ''))
    with pytest.raises(job.DeployFailure, match='ROLLBACK_READER_INCOMPATIBLE'):
        job.rollback_release(paths, commands, Client, tmp_path/'old', manifest)
    assert calls == ['lock_free']


@pytest.mark.parametrize('topic,facts,expected', [
    ('command_stale', {'date': 123}, views.stale(123)),
    ('operator_identity', {'chat_id': 42, 'user_id': 51}, views.start_reply(42, 51)),
    ('configuration_error', {'reason': '<error>'}, views.owner_config_error('<error>')),
    ('command_unknown', {'reason': '<bad>'}, views.unknown(SimpleNamespace(reason='<bad>'))),
    ('planning_started', {'coin': '<A>', 'side': 'entry'}, views.planning('<A>', 'entry')),
    ('pause_changed', {'already': True, 'clip': 1, 'clips': 3}, views.already_paused()),
    ('pause_changed', {'already': False, 'clip': 1, 'clips': 3}, views.paused(1, 3)),
    ('resume_changed', {'was': True, 'interrupted': ['D1', 'D2']}, views.resumed(['D1', 'D2'])),
    ('resume_changed', {'was': False, 'interrupted': []}, views.not_paused()),
    ('error', {'reason': '<error>'}, views.error('<error>')),
    ('help_requested', {'sim': True, 'sol': True}, views.help_text(True, True)),
    ('plan_requoted', {'reason': '<new>', 'sim': True}, views.requote('<new>', True)),
])
def test_notice_has_only_facts_in_queue_and_preserves_rendering(tmp_path, topic, facts, expected):
    out = Outbox(Journal(Conns(tmp_path/'trade.db')))
    out.notice(42, topic, **facts)
    event = out.journal.notifications()[0]
    assert 'text' not in event and event['facts'] == facts
    assert present(event)['text'] == expected
    assert present(event)['chat_id'] == 42
    with pytest.raises(ValueError):
        out.notice(42, topic, **dict(facts, owner={'private': 'not allowed'}))
    assert len(out.journal.notifications()) == 1


def test_report_snapshots_preserve_exact_amounts_unknowns_and_interface_health(tmp_path):
    from dataclasses import asdict, replace
    from funding_bot.ipc.reports import StatusView, PositionView, RestartView, decode
    from funding_bot.tg import sol_views
    out = Outbox(Journal(Conns(tmp_path/'trade.db')))
    status = StatusView(ts=123, mode='dry', wallet_stable=D('0.12345678901234567890123456789'),
                        wallet_native=None, paused=True, checks=(('test', None, '<missing>'),))
    out.status_report(42, status)
    event = out.journal.notifications()[0]
    decoded = decode(event['snapshot'])
    assert decoded['wallet_stable'] == status.wallet_stable and decoded['wallet_native'] is None
    assert present(event)['text'] == views.status(status)
    health = dict(sender_fails=3, tg_last_ok_ago_s=999)
    assert present(event, transport_health=health)['text'] == views.status(replace(status, **health))
    pos = PositionView(deal_id='D1', coin='<A>', state='PAUSED', delta_qty=D('0.000000000000000001'),
                       perp_qty=None, m_unknown=True, sim=True)
    out.positions_report(42, [pos], at=123, matched=False, mismatch=True, sim=True)
    ev = out.journal.notifications()[1]
    assert PositionView(**decode(ev['snapshots'])[0]) == pos
    assert 'generic_legs' not in decode(ev['snapshots'])[0]  # legacy DTO remains readable by the previous interface
    assert present(ev)['text'] == views.positions([pos], ts=123, matched=False, mismatch=True, sim=True)
    restart = RestartView(intent_id='I1', kind='exit', deal_id='D1', coin='A', delta=D('0.2'),
                          delta_usd=None, matched=False, details='<not reconciled>', sim=True)
    for sol, renderer in ((False, views), (True, sol_views)):
        out.restart_report(42, restart, solana=sol)
        assert present(out.journal.notifications()[-1])['text'] == renderer.restart(restart)
        out.restart_report(42, restart, solana=sol, check_only=True)
        assert present(out.journal.notifications()[-1])['text'] == renderer.restart_check(
            restart.coin, restart.deal_id, restart.matched, restart.details, restart.state,
            restart.sim, hedged=restart.hedged, delta=restart.delta, usd=restart.delta_usd,
            step=restart.step, m=restart.m)


def test_core_status_uses_public_snapshot_without_telegram_sender(tmp_path):
    from funding_bot.core.service import CoreService
    from funding_bot.ipc.reports import StatusView
    cfg = SimpleNamespace(owner_id=42, mode='dry', get=lambda *a: None,
                          profile_enabled=lambda *a: False, live_missing=lambda *a: [])
    engine = SimpleNamespace(pause_evt=threading.Event())
    service = CoreService(Conns(tmp_path/'trade.db'), SimpleNamespace(), engine, lambda *a: None,
                          owner_loader=lambda: cfg)
    assert not hasattr(service.bot.sender, 'fails')
    service.bot.clock = lambda: 123
    snapshot = service.bot.status_view()
    assert isinstance(snapshot, StatusView)
    service.bot.status_cmd(42)
    events = service.bot.sender.journal.notifications()
    assert events[-1]['kind'] == 'status_report'
    assert present(events[-1])['text'] == views.status(snapshot)


def test_missing_owner_logs_never_include_escaped_message_bodies(caplog):
    from html import escape
    from funding_bot.trade.keys import _remember_exact
    secret = 'Synthetic&Passphrase<For-Log-Check>'
    _remember_exact(secret)
    bot = object.__new__(Bot)
    bot.chat = lambda: None
    body = '<b>' + escape(secret) + '</b>'
    assert bot.say(body) is False
    bot.alarm('test', body)
    assert len(caplog.records) == 2
    assert secret not in caplog.text and escape(secret) not in caplog.text
    assert 'Synthetic' not in caplog.text and 'не доставлено' in caplog.text


def test_health_reports_reader_floor_activated_after_service_construction(tmp_path):
    from funding_bot.core.service import CoreService
    cfg = SimpleNamespace(owner_id=42, mode='dry', get=lambda *a: None,
                          profile_enabled=lambda *a: False, live_missing=lambda *a: [])
    engine = SimpleNamespace(pause_evt=threading.Event(), busy=lambda: False)
    conns = Conns(tmp_path/'trade.db')
    service = CoreService(conns, SimpleNamespace(), engine, lambda *a: None, owner_loader=lambda: cfg)
    assert service.health()['min_reader'] == 2
    store.require_reader(conns.get(), 4)
    assert service.health()['min_reader'] == store.schema_info(conns.get())['min_reader'] == 4


def test_deploy_readiness_rejects_stale_runtime_reader_floor():
    manifest = dict(release_id='candidate', source_sha256='s', artifact_sha256='a', ipc_version=1,
                    schema_version=4)
    health = dict(manifest, ready=True, drain=True, drain_epoch='e', recovery_complete=True,
                  execution_lock_held=True, min_reader=2)
    db = dict(schema_version=4, min_reader=4)
    with pytest.raises(job.DeployFailure, match='reader floor'):
        job.health_matches(health, manifest, database=db)
    health['min_reader'] = 4
    assert job.health_matches(health, manifest, database=db) == health
