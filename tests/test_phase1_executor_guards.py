"""Финальная проверка фазы 1: страховки исполнителя дохеджа и отката при m = 1000 пересчитывают количество от
ДЕЛЬТЫ СДЕЛКИ У КНОПКИ (а не берут из плана): дельта уменьшилась между планом и кнопкой — продаётся меньше.
Закрывает две выжившие мутации (Engine._rehedge SELL floor(δ/m), Engine._undo кратно шагу·m)."""
from decimal import Decimal as D

import test_phase1_units as tu
import test_trade_engine as fx
from funding_bot.trade import store


def test_executor_rehedge_sell_recounts_contracts_from_delta_at_button(tmp_path):
    e = tu.mult_env(tmp_path)
    did = tu.enter(e).deal_id                                  # ≈ 2 444.6 токена, шорт 2, δ ≈ 444.6
    tu._short_by(e, did, 2)                                    # δ ≈ 2 444.6: план SELL 2
    fix = e.desk.propose_fix("rehedge", did, chat=fx.OWNER)
    assert D(tu.t11._spec_of(e.con, fix.intent_id)["qty"]) == 2
    tu._short_by(e, did, -1)                                   # до кнопки δ ≈ 1 444.6: продать можно только 1
    n0 = len(e.perp.calls)
    fx.run_approved(e, fix)
    assert [(c["side"], c["qty"]) for c in e.perp.calls[n0:]] == [("SELL", 1)]
    tu.assert_even(e, did)


def test_executor_undo_sells_multiple_of_step_m_from_delta_at_button(tmp_path):
    e = tu.mult_env(tmp_path)
    did = tu.enter(e).deal_id
    tu._short_by(e, did, 2)                                    # δ ≈ 2 444.6: план отката 2 000 токенов
    und = e.desk.propose_fix("undo", did, chat=fx.OWNER)
    assert tu.t11._spec_of(e.con, und.intent_id)["units"] == 2000 * fx.E18
    tu._short_by(e, did, -1)                                   # до кнопки δ ≈ 1 444.6: кратно шагу·m — 1 000
    tok0 = tu.book(e, did).tokens(18)
    fx.run_approved(e, und)
    assert tok0 - tu.book(e, did).tokens(18) == 1000
    tu.assert_even(e, did)
    assert store.get_deal(e.con, did)["state"] in ("OPEN", "PAUSED")
