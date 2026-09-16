"""AC-07 closure (docs/MIGRATION_ACCEPTANCE.md): trade/engine.py (Desk/Engine) and trade/sol_flow.py (SolDesk/
SolEngine) hand the interface facts only — never pre-rendered HTML. Every owner-facing text that Refused/
Proposal/Notice used to carry directly (desk-plan proposals, fix-plan proposals, refusals, executor notices) is
now a (topic, facts) or (view_topic, view_facts) pair; rendering happens exactly once, only in
interface.presenter, from that same data. This file is the receipt: core objects carry no HTML, and rendering
their facts reproduces byte-for-byte what the old inline views.* calls used to build.

`trade/engine.py`'s `plan_cli` (the `funding_bot plan` CLI command) is a deliberate, documented exception: it is
a single-process developer tool with no core/interface split to keep a boundary across, so it still renders
locally via tg.views (see PATCHNOTES/m4-ac07-full-core-interface-boundary-20260916.md)."""
import re
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest
from funding_bot.trade import engine as eng
from funding_bot.tg import views, sol_views
from funding_bot.interface.presenter import render_execution_notice, render_proposal_view
from funding_bot.ipc.notifications import validate_execution_notice, validate_proposal_view

import test_trade_engine as fx

HTML_TAG = re.compile(r'<[a-zA-Z/][^>]*>')
RENDERED_PREFIX = ('⛔', '⏳', '📝', '✅', '🛑', '⏸', '⚠️', '🔁', '⌛')


def _source(module) -> str:
    return Path(module.__file__).read_text(encoding='utf-8')


# --- структурная проверка: рендер HTML остался только в interface (и в объявленной оговорке plan_cli) ------

def test_engine_and_sol_flow_never_render_html_for_refusals_and_plans():
    import funding_bot.trade.engine as engine_mod
    import funding_bot.trade.sol_flow as sol_flow_mod
    engine_src = _source(engine_mod)
    sol_src = _source(sol_flow_mod)
    # plan_cli — единственная сознательная оговорка (см. docstring модуля выше и PATCHNOTES); всё до неё —
    # путь исполнителя владельца, который обязан отдавать только факты.
    engine_runtime_src = engine_src.split("# --- CLI `funding_bot plan`")[0]
    # Только вызовы через "v" — локальный алиас trade/formatters.py (AC-07 formatter extraction, 16.09; раньше
    # был _views()/_sv(), сам ленивый импорт tg убран — см. test_no_tg_import_outside_plan_cli ниже) — не
    # self.busy() (Engine — идёт ли исполнение сейчас) и не упоминания имён в docstring-прозе ("tg.views.PlanView"
    # и т.п., без accessor перед именем не совпадёт). formatters не знает про HTML вовсе, так что этот сет имён
    # (рендер плана/отказа) он физически не может дать — паттерн остаётся на случай будущей регрессии.
    accessor = r'(?:_views\(\)|_sv\(\)|\bv)\.'
    forbidden_calls = ('refused', 'busy', 'owner_missing', 'owner_config_error', 'fix_plan', 'plan')
    forbidden_ctors = ('PlanView', 'FixPlanView', 'SolPlanView', 'SolHaltView', 'SolProgressView', 'FixDoneView')
    for name in forbidden_calls:
        pattern = re.compile(accessor + re.escape(name) + r'\(')
        assert pattern.search(engine_runtime_src) is None, f'views.{name}(...) отрисован в trade/engine.py'
        assert pattern.search(sol_src) is None, f'views.{name}(...) отрисован в trade/sol_flow.py'
    for name in forbidden_ctors:
        pattern = re.compile(accessor + name + r'\(')
        assert pattern.search(engine_runtime_src) is None, f'{name}(...) сконструирован в trade/engine.py'
        assert pattern.search(sol_src) is None, f'{name}(...) сконструирован в trade/sol_flow.py'
    for name in ('hooks.report(', 'hooks.progress('):
        assert name not in engine_runtime_src, f'{name!r} остался в trade/engine.py — заменяется hooks.notice(...)'
        assert name not in sol_src, f'{name!r} остался в trade/sol_flow.py — заменяется hooks.notice(...)'


def test_no_tg_import_outside_plan_cli():
    """AC-07 закрыт буквально (ревью Codex 16.09, docs/migration/CODEX_REVIEW_CLAUDE_MIGRATION_20260916.md):
    тест выше (test_engine_and_sol_flow_never_render_html_for_refusals_and_plans) проверял только, что через
    _views()/_sv()/"v" не рендерится HTML — но сам факт ленивого импорта tg.views/tg.sol_views ради чистых
    числовых форматтеров (num, pct, dur, tok, money(html=False), DEAL_STATE_LABEL, VENUE_LABEL и т.п.) оставался:
    Codex указал, что это формально всё ещё нарушает «core не зависит от Telegram/presenters» (docs/
    MIGRATION_ACCEPTANCE.md). Эти форматтеры перенесены в trade/formatters.py — чистый модуль без единого
    упоминания Telegram/HTML (PATCHNOTES/m4-ac07-formatter-extraction-20260916.md); engine.py/sol_flow.py/
    reconcile.py импортируют только его, обычным top-level импортом (скрывать больше нечего).

    Проверяется весь каталог trade/ (рекурсивно, как в задаче), а не только engine.py/sol_flow.py — вдруг импорт
    остался где-то ещё (он и был: trade/reconcile.py, тем же способом, до этого патча).
    `trade/engine.py: plan_cli()` — единственная оставленная, документированная оговорка (см. её docstring и тест
    выше): однопроцессный CLI-инструмент разработчика (`funding_bot plan`), core/interface границы здесь нет."""
    import funding_bot.trade as trade_pkg
    trade_dir = Path(trade_pkg.__file__).parent
    tg_import = re.compile(r'_views\(\)|_sv\(\)|from funding_bot\.tg|from \.\.tg|from \.tg\b|^\s*import tg\b', re.M)
    offenders = []
    for py in sorted(trade_dir.rglob('*.py')):
        src = py.read_text(encoding='utf-8')
        if py.name == 'engine.py':
            src = src.split("# --- CLI `funding_bot plan`")[0]   # plan_cli — документированное исключение ниже
        for m in tg_import.finditer(src):
            line_no = src.count('\n', 0, m.start()) + 1
            offenders.append(f'{py.relative_to(trade_dir.parent)}:{line_no}: {m.group(0)!r}')
    assert not offenders, f'lazy/top-level tg-импорт остался в trade/: {offenders}'
    # plan_cli действительно единственное место — и оно действительно всё ещё импортирует tg (иначе оговорка в
    # docstring выше врёт).
    import funding_bot.trade.engine as engine_mod
    plan_cli_src = _source(engine_mod).split("# --- CLI `funding_bot plan`")[1]
    assert 'from ..tg import views' in plan_cli_src and 'from ..tg.sender import to_plain' in plan_cli_src


def test_refused_proposal_notice_carry_no_html():
    """Refused/Proposal/Notice несут факты — не строку с HTML: нет .html, значения не эскейплены/не отрисованы."""
    r = eng.Refused("тест — не отрисован")
    assert not hasattr(r, 'html')
    assert r.topic == 'refused' and r.facts == {'reason': 'тест — не отрисован'}
    n = eng.Notice('resume_checked', {'deal_id': 'D1', 'sim': True})
    assert not hasattr(n, 'html')
    p = eng.Proposal('I1', 'nonce', 'D1', 'entry', 'plan', {'coin': 'A', 'kind': 'entry'})
    assert not hasattr(p, 'html')
    for facts in (r.facts, n.facts, p.view_facts):
        for v in facts.values():
            if isinstance(v, str):
                assert not HTML_TAG.search(v), f'факт содержит HTML-разметку: {v!r}'
                assert not v.startswith(RENDERED_PREFIX), f'факт уже отрисован (эмодзи-префикс): {v!r}'


def test_refused_topic_and_facts_default_to_plain_reason():
    e = eng.Refused("живую сделку в этом режиме не трогаю: ключи live не загружены")
    assert e.topic == 'refused'
    assert e.facts == {'reason': "живую сделку в этом режиме не трогаю: ключи live не загружены"}
    e2 = eng.Refused(topic='busy', facts={'intent_id': 'I5'})
    assert e2.topic == 'busy' and e2.facts == {'intent_id': 'I5'}


# --- рендер факта == прежний прямой вызов views.* --------------------------------------------------------

@pytest.mark.parametrize("reason", [
    "живую сделку в этом режиме не трогаю: ключи live не загружены (mode в owner.toml и перезапуск службы)",
    "инструмент <AIW3> в таблице сменился — это другой актив, нужен новый план",
])
def test_refused_renders_same_text_as_direct_views_refused_call(reason):
    e = eng.Refused(reason)
    validate_execution_notice(e.topic, e.facts)
    assert render_execution_notice(e.topic, e.facts) == views.refused(reason)


def test_busy_owner_missing_owner_config_error_render_same_as_before():
    for iid in ('I9', None):
        facts = {'intent_id': iid}
        validate_execution_notice('busy', facts)
        assert render_execution_notice('busy', facts) == views.busy(iid)

    keys, action = ['wallets.bsc', 'wallets.aster_user'], 'вход'
    facts = {'keys': keys, 'action': action}
    validate_execution_notice('owner_missing', facts)
    assert render_execution_notice('owner_missing', facts) == views.owner_missing(keys, action)

    reason = 'owner.toml: строка 4 — не число'
    facts = {'reason': reason}
    validate_execution_notice('owner_config_error', facts)
    assert render_execution_notice('owner_config_error', facts) == views.owner_config_error(reason)


def test_resume_checked_and_mismatch_render_same_as_before():
    facts = {'deal_id': 'D7', 'sim': True}
    validate_execution_notice('resume_checked', facts)
    assert render_execution_notice('resume_checked', facts) == views.resume_checked('D7', sim=True)

    detail = 'perp −5 ≠ журнал −4'
    facts = {'deal_id': 'D7', 'detail': detail, 'sim': False}
    validate_execution_notice('resume_mismatch', facts)
    assert render_execution_notice('resume_mismatch', facts) == views.resume_mismatch('D7', detail, sim=False)


def test_sol_halt_renders_same_text_as_direct_sol_views_call():
    facts = dict(kind='entry', coin='ANSEM', deal_id='D1', intent_id='I1', reason='дельта вне допуска',
                state='PAUSED', wallet_tokens=D('10'), perp_pos=D('-10'), delta=D('0.5'), delta_usd=D('2.5'),
                need_qty=D('0.5'), sim=True)
    validate_execution_notice('sol_halt', facts)
    assert render_execution_notice('sol_halt', facts) == sol_views.halt(sol_views.SolHaltView(**facts))


def test_sol_progress_renders_same_text_as_direct_sol_views_call():
    facts = dict(intent_id='I1', kind='entry', coin='ANSEM', fullcoin='para:ANSEM', stage='hedge', path='jupiter',
                tokens=D('12.3'), perp_qty=D('12.3'), sim=True)
    validate_execution_notice('sol_progress', facts)
    # intent_id — только маршрутизация (какое сообщение править), sol_views.progress его не читает.
    view_fields = {k: v for k, v in facts.items() if k != 'intent_id'}
    assert render_execution_notice('sol_progress', facts) == sol_views.progress(sol_views.SolProgressView(**view_fields))


def test_sol_plan_renders_same_text_as_direct_sol_views_call():
    """SolDesk.plan_view()/propose_entry() строят 'sol_plan' факты (SolDesk.propose_entry → Proposal.view_topic
    == 'sol_plan') — их не проверить через реальный SolEngine в этом окружении (нет пакета solders, см.
    PATCHNOTES), поэтому здесь напрямую: те же факты, что и SolDesk.plan_view() вернул бы, должны рендериться
    в тот же текст, что раньше строил sol_views.plan(SolPlanView(...)) внутри Desk."""
    facts = dict(intent_id='I1', kind='entry', coin='ANSEM', fullcoin='para:ANSEM', deal_id='D1', usdc=D('150'),
                usdc_min=D('149'), tokens=D('1000'), tokens_min=D('995'), perp_qty=D('1000'), perp_px=D('0.15'),
                path='jupiter', others=('OKX: дороже на 0.12 %',), basis_bps=D('12'), basis_gross_bps=D('15'),
                spot_fee_usd=D('0.3'), perp_fee_usd=D('0.1'), funding_pct_h=D('0.01'), leverage=1, identity=None,
                notes=(), missing=(), sim=True, ttl_s=60, margin_need=D('10'), margin_avail=D('100'))
    validate_proposal_view('sol_plan', facts)
    assert render_proposal_view('sol_plan', facts) == sol_views.plan(sol_views.SolPlanView(**facts))


def test_fix_done_renders_same_for_evm_and_sol_shapes():
    """propose_fix/_settle_fix (EVM) и SolDesk.propose_fix/_settle_fix (Sol) заполняют один и тот же 'fix_done' —
    рендерит один и тот же views.fix_done для обоих движков (как и раньше, когда оба вызывали его напрямую)."""
    facts = dict(kind='rehedge', coin='AIW3', deal_id='D1', state='OPEN', qty=D('5'), usd=D('10'), side='SELL',
                noop=None, delta=D('0.1'), step=D('1'), sim=True, m=D('1'))
    validate_execution_notice('fix_done', facts)
    assert render_execution_notice('fix_done', facts) == views.fix_done(views.FixDoneView(**facts))


# --- Desk (EVM) реально отдаёт факты плана, а не HTML -------------------------------------------------------

def test_propose_entry_hands_plan_facts_not_html(tmp_path):
    e = fx.sim_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=111)
    assert p.view_topic == 'plan'
    assert isinstance(p.view_facts, dict)
    validate_proposal_view(p.view_topic, p.view_facts)
    for v in p.view_facts.values():
        if isinstance(v, str):
            assert not HTML_TAG.search(v) and not v.startswith(RENDERED_PREFIX)
    rendered = render_proposal_view(p.view_topic, p.view_facts)
    assert rendered == views.plan(SimpleNamespace(**p.view_facts))
    assert "📝" in rendered and "AIW3" in rendered


def test_propose_fix_hands_fix_plan_facts_not_html(tmp_path):
    """Тот же сценарий, что test_rehedge_auto_freezes_alpha_beta_in_its_plan (test_trade_engine.py): своп прошёл,
    процесс умер до хеджа — голая нога, «дохедж» строит реальный FixPlan."""
    import time
    from funding_bot.trade import reconcile
    e = fx.live_env(tmp_path, clip="300")
    e.path.write_text(fx.auto_toml(clip="300"))
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=111)
    assert fx.store.approve_intent(e.con, p.intent_id, p.nonce)
    e.spot.crash = "after_send"
    with pytest.raises(fx.Crash):
        e.engine.execute(p.intent_id)
    e.spot.crash = None
    reconcile.startup(e.con, e.legs, now=time.time())
    fix = e.desk.propose_fix("rehedge", p.deal_id, chat=111)
    assert fix.view_topic == 'fix_plan'
    validate_proposal_view(fix.view_topic, fix.view_facts)
    for v in fix.view_facts.values():
        if isinstance(v, str):
            assert not HTML_TAG.search(v) and not v.startswith(RENDERED_PREFIX)
    rendered = render_proposal_view(fix.view_topic, fix.view_facts)
    assert rendered == views.fix_plan(SimpleNamespace(**fix.view_facts))
    assert "📝" in rendered and "Дохедж AIW3" in rendered


# --- валидаторы fail-closed: неизвестные/лишние поля и не-факты отклоняются --------------------------------

def test_validators_reject_unknown_topic_missing_field_and_non_scalar():
    with pytest.raises(ValueError):
        validate_execution_notice('not_a_real_topic', {'reason': 'x'})
    with pytest.raises(ValueError):
        validate_execution_notice('refused', {'reason': 'x', 'extra': 1})     # лишнее поле
    with pytest.raises(ValueError):
        validate_execution_notice('busy', {})                                 # поля не хватает
    with pytest.raises(ValueError):
        validate_execution_notice('owner_config_error', {'reason': object()})  # не факт (не сериализуется)
    with pytest.raises(ValueError):
        validate_proposal_view('plan', {'intent_id': 'I1'})                    # неполный набор полей
