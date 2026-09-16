from dataclasses import asdict
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from funding_bot.core.commands import BotHooks
from funding_bot.core.journal import Journal, Outbox
from funding_bot.interface.presenter import present
from funding_bot.ipc.reports import FinalView
from funding_bot.trade.engine import Conns
from funding_bot.tg import views


@pytest.mark.parametrize('kind', ['entry', 'exit'])
@pytest.mark.parametrize('partial', [False, True])
@pytest.mark.parametrize('known', [False, True])
def test_final_wire_matches_old_renderer_and_survives_restart(tmp_path, kind, partial, known):
    conns=Conns(tmp_path/'trade.db')
    out=Outbox(Journal(conns))
    snap=FinalView('E1',kind,'<A&>','bsc','aster','D1',partial=partial,sim=True,
        leg_usd=D(200),spot_qty=D('4902.151'),perp_qty=D(4902),
        cost_usd=D('.08') if known else None, pnl_total_usd=D('-0.8') if known else None,
        partial_reason='<unsafe&>',tx_hashes=('0x'+'a'*64,))
    expected=views.final(snap)
    hooks=BotHooks(SimpleNamespace(chat=lambda:42,sender=out))
    hooks.final_report(snap)
    event=Outbox(Journal(conns)).journal.notifications()[0]
    assert event['kind']=='final_report' and event['dto_version']==3
    assert 'text' not in event and 'legacy_body' not in event
    assert present(event)['text']==expected
    out.ack(event['id'],{'message_id':9})
    assert not Journal(conns).notifications()


def test_final_dto_fence_is_atomic_monotonic_and_rejects_old_reader(tmp_path):
    from test_m4_domain_notifications import job
    conns=Conns(tmp_path/'trade.db'); out=Outbox(Journal(conns));con=conns.get()
    snap=FinalView('E1','entry','A','bsc','aster','D1')
    old=dict(compatible_readers=[1,2,3],schema_version=3,dto_version=2)
    con.execute('BEGIN')
    out.final_report(42,snap)
    con.rollback()
    assert job.compatible_reader(old,job.database_info(tmp_path/'trade.db'))
    assert not out.journal.notifications()
    out.final_report(42,snap)
    out.approval_reply('cb','accepted')  # DTO2 must never lower the reader floor.
    assert con.execute("SELECT value FROM core_meta WHERE key='schema_version'").fetchone()[0]=='3'
    db=job.database_info(tmp_path/'trade.db')
    assert db['notification_dto_version']==3 and not job.compatible_reader(old,db)
    assert job.compatible_reader(dict(old,dto_version=3),db)
    event=out.journal.notifications()[0]
    with pytest.raises(Exception,match='unsupported_notification_version'):
        present(dict(event,dto_version=2))
    out.ack(event['id'],{})
    assert not job.compatible_reader(old,job.database_info(tmp_path/'trade.db'))


def test_final_core_does_not_invoke_renderer(tmp_path,monkeypatch):
    """core-final (_final) must never touch the Telegram presenter. Until 16.09 this was enforced by hijacking
    engine._views (the lazy tg.views accessor) mid-call so any access would raise. That shared accessor is gone
    now: engine.py/sol_flow.py import only the pure trade/formatters.py (AC-07 formatter extraction,
    PATCHNOTES/m4-ac07-formatter-extraction-20260916.md) and reach `tg` only from plan_cli's own local import,
    which _final() cannot reach. The stronger, static guarantee — no `tg` import anywhere in engine.py/sol_flow.py
    outside plan_cli — is asserted once for the whole file by
    test_m4_ac07_core_interface_boundary.py::test_no_tg_import_outside_plan_cli."""
    from test_m4_accounting_integration import final_run,bind,history
    import test_marks as legacy
    from funding_bot.trade import store
    con=store.connect(tmp_path/'trade.db')
    legacy.dqa9q(con);scope=bind(con);history(con,scope)
    worker,run=final_run(con,monkeypatch)
    snapshot,cost,extra=worker._final(run,legacy.NOW,False)
    assert type(snapshot) is FinalView and snapshot.perp_qty==D(4902) and cost is not None
    assert extra['accounting_cost_revision']
    assert views.FinalView is FinalView


def test_final_rejects_arbitrary_object_before_enqueue(tmp_path):
    out=Outbox(Journal(Conns(tmp_path/'trade.db')))
    with pytest.raises(Exception,match='invalid_final_snapshot'):
        out.final_report(42,SimpleNamespace(secret='must not cross'))
    assert not out.journal.notifications()


@pytest.mark.parametrize('kind,state', [('entry','OPEN'),('exit','CLOSED'),('exit','OPEN')])
@pytest.mark.parametrize('complete', [True,False])
def test_sol_final_wire_preserves_old_text_and_unknowns(tmp_path,kind,state,complete):
    from funding_bot.ipc.reports import SolFinalView
    from funding_bot.tg import sol_views
    out=Outbox(Journal(Conns(tmp_path/'trade.db')))
    snap=SolFinalView(kind,'<ANSEM>','para:ANSEM','D1',state,tokens=D('100.123'),usdc=D(15),
        perp_qty=D(100),pnl_usdc=D('.003') if complete else None,pnl_complete=complete,
        warn=('<unsafe>',),dust=state=='OPEN',hedged=True,sim=True)
    BotHooks(SimpleNamespace(chat=lambda:42,sender=out)).final_report(snap)
    event=out.journal.notifications()[0]
    assert event['solana'] is True and sol_views.SolFinalView is SolFinalView
    assert present(event)['text']==sol_views.final(snap)
    with pytest.raises(Exception,match='invalid_final_snapshot'):
        present(dict(event,solana='true'))


def test_actual_sol_final_does_not_access_presenter(tmp_path,monkeypatch):
    """SOL final (_final) must never touch the presenter — same reasoning as
    test_final_core_does_not_invoke_renderer above. sol_flow._sv (the lazy tg.sol_views accessor) is gone;
    superseded by the static no-tg-import test (see that docstring)."""
    import sol_c2_world as w
    from funding_bot.trade import sol_flow
    from funding_bot.ipc.reports import SolFinalView
    original=sol_flow.SolEngine._final
    snapshots=[]
    def no_presenter(*args,**kwargs):
        result=original(*args,**kwargs)
        assert type(result) is SolFinalView
        snapshots.append(result)
        return result
    monkeypatch.setattr(sol_flow.SolEngine,'_final',no_presenter)
    world=w.make_world(tmp_path)
    prop=w.enter(world)
    w.approve_run(world,world.desk.propose_exit(prop.deal_id,None,False,chat=None))
    assert [s.kind for s in snapshots]==['entry','exit']
