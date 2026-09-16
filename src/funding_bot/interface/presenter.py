"""Render durable domain notifications at the interface boundary."""
from ..ipc.notifications import DTO_VERSION, EXECUTION_REPORT_VERSION, GENERIC_POSITION_VERSION, EXECUTION_NOTICE_VERSION, APPROVAL_REASONS
from ..ipc.protocol import RpcError


def present(event, *, transport_health=None):
    kind = event.get('kind')
    if kind in ('send', 'edit', 'answer'):
        return event  # Already queued legacy messages remain deliverable.
    expected = EXECUTION_REPORT_VERSION if kind == 'final_report' else DTO_VERSION
    if kind == 'execution_notice':
        expected = EXECUTION_NOTICE_VERSION
    if kind == 'positions_report' and event.get('dto_version') == GENERIC_POSITION_VERSION:
        expected = GENERIC_POSITION_VERSION
    if type(event.get('dto_version')) is not int or event['dto_version'] != expected:
        raise RpcError('unsupported_notification_version')
    from ..tg import views
    if kind == 'execution_notice':
        return dict(kind='send', chat_id=event['chat_id'], text=render_execution_notice(event['topic'], event['facts']), html=True,
                    silent=event['topic'] == 'progress')
    if kind == 'final_report':
        from ..ipc.reports import decode, FinalView, SolFinalView
        if type(event.get('solana', False)) is not bool:
            raise RpcError('invalid_final_snapshot')
        if event.get('solana', False):
            from ..tg import sol_views
            return dict(kind='send', chat_id=event['chat_id'],
                        text=sol_views.final(SolFinalView(**decode(event['snapshot']))), html=True)
        return dict(kind='send', chat_id=event['chat_id'],
                    text=views.final(FinalView(**decode(event['snapshot']))), html=True)
    if kind == 'restart_report':
        from ..ipc.reports import decode, RestartView
        renderer = views
        if event['solana']:
            from ..tg import sol_views
            renderer = sol_views
        snap = RestartView(**decode(event['snapshot']))
        if event['check_only']:
            text = renderer.restart_check(snap.coin, snap.deal_id, snap.matched, snap.details, snap.state,
                                          snap.sim, hedged=snap.hedged, delta=snap.delta, usd=snap.delta_usd,
                                          step=snap.step, m=snap.m)
        else:
            text = renderer.restart(snap)
        return dict(kind='send', chat_id=event['chat_id'], text=text, html=True)
    if kind == 'status_report':
        from ..ipc.reports import decode
        snapshot = decode(event['snapshot'])
        if transport_health is not None:
            snapshot.update({k: transport_health[k] for k in ('sender_fails', 'tg_last_ok_ago_s')})
        return dict(kind='send', chat_id=event['chat_id'], text=views.status(views.StatusView(**snapshot)), html=True)
    if kind == 'positions_report':
        from ..ipc.reports import decode
        snapshots = [views.PositionView(**r) for r in decode(event['snapshots'])]
        generic = [r for r in snapshots if r.generic_legs is not None]
        snapshots = [r for r in snapshots if r.generic_legs is None]
        text = views.positions(snapshots, ts=event['at'], matched=event['matched'],
                               mismatch=event['mismatch'], sim=event['sim'])
        if generic:
            from .leg_presenter import render
            text = ('\n\n'.join([text] if snapshots else []) + '\n\n' +
                    '\n\n'.join(render(r.generic_legs) for r in generic)).strip()
        return dict(kind='send', chat_id=event['chat_id'], text=text, html=True)
    if kind == 'operator_notice':
        from ..ipc.notifications import validate_notice
        from types import SimpleNamespace
        topic, f = event['topic'], event['facts']
        validate_notice(topic, f)
        renderers = {
            'command_stale': lambda: views.stale(f['date']),
            'operator_identity': lambda: views.start_reply(f['chat_id'], f['user_id']),
            'configuration_error': lambda: views.owner_config_error(f['reason']),
            'command_unknown': lambda: views.unknown(SimpleNamespace(reason=f['reason'])),
            'planning_started': lambda: views.planning(f['coin'], f['side']),
            'pause_changed': lambda: views.already_paused() if f['already'] else views.paused(f['clip'], f['clips']),
            'resume_changed': lambda: views.resumed(f['interrupted']) if f['was'] else views.not_paused(),
            'error': lambda: views.error(f['reason']),
            'help_requested': lambda: views.help_text(sim=f['sim'], sol=f['sol']),
            'plan_requoted': lambda: views.requote(f['reason'], sim=f['sim']),
        }
        return dict(kind='send', chat_id=event['chat_id'], text=renderers[topic](), html=True)
    if kind == 'approval_reply':
        reason = event.get('reason')
        if reason not in APPROVAL_REASONS:
            raise RpcError('unsupported_approval_reason')
        return dict(kind='answer', callback_id=event['callback_id'], text=getattr(views, 'CB_' + reason.upper()))
    if kind in ('plan_closed', 'plan_proposed'):
        summary = event['summary']
        if summary.get('fallback'):
            head = views.PlanHead('План', '✅ Да', summary['sim'])
        else:
            head = views.intent_head(summary['intent'], summary['deal'])
        if kind == 'plan_proposed':
            return dict(kind='send', chat_id=event['chat_id'], text=event['legacy_body'], html=True,
                        reply_markup=views.plan_keyboard(event['plan_id'], event['nonce'], head.ok))
        return dict(kind='edit', chat_id=event['chat_id'], message_id=event['message_id'],
                    text=views.plan_closed(event['action'], head.title, event['at'], head.sim),
                    html=True, reply_markup=None)
    raise RpcError('unsupported_notification')


def render_execution_notice(topic, encoded_facts):
    """Renderer used only by interface and the legacy Telegram compatibility bot."""
    from ..ipc.notifications import validate_execution_notice
    from ..ipc.reports import decode
    from types import SimpleNamespace
    facts = decode(encoded_facts)
    validate_execution_notice(topic, facts)
    from ..tg import views
    if topic == 'executor_crash':
        return views.executor_crash(facts['intent_id'], facts['error'])
    if topic == 'refused':
        return views.refused(facts['reason'])
    if topic == 'auto_unwind':
        return views.auto_unwind(facts['coin'], facts['qty'], facts['usd'], facts['sim'])
    renderers = {'halt': views.halt, 'progress': views.progress, 'perp_closed': views.perp_closed, 'fix_done': views.fix_done}
    return renderers[topic](SimpleNamespace(**facts))
