"""Фаза 1.3 по ревью 13.09 (Н2): замороженный инструмент в намерениях и перекотировке. Исполнитель не выбирает
инструмент заново по монете: план у кнопки строится из спецификации сделки (Desk.pair_from); таблица только
подтверждает, что строка той же монеты не сменилась и не отозвана (вход, добор), а выход, откат и дохедж её не читают
вовсе. Намерение исполняется, только если его отпечаток inst_hash = инструменту сделки, колонки сделки = её
инструменту, а токен/символ/decimals spec = сделке — иначе FAILED до любого действия (ни свопа, ни заявки, ни approve,
ни setup). Второй слой — перекотировка: свежий план по другому инструменту → FAILED без нового предложения.
Фейки — из test_trade_engine и шагов 0, 1.1, 1.2 (без сети и ключей)."""
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from funding_bot.trade import engine as eng, reconcile, store
from funding_bot.trade.store import DealState, IntentStatus
from funding_bot.trade.types import PerpInstrument

import test_marks as tm
import test_phase0_hotfixes as t0
import test_phase1_instrument as t11
import test_phase1_units as t12
import test_trade_engine as fx

B_TOKEN = "0x" + "b2" * 20
ZERO_SNAP = ((0, 0), 0, 0)
HASH_TEXT = "≠ инструменту сделки"                       # первая буква отказа — заглавная (views.refused)
RESEND = "пришлите команду заново"
NAKED = 200                                             # голая нога для дохеджа/отката: 200 × 0.04 ≈ 8 $ > minNotional 5 $


def _spec_of(e, iid) -> dict:
    return json.loads(store.get_intent(e.con, iid)["spec_json"])


def _set_spec(e, iid, **kw) -> None:
    """Правка spec_json намерения: подделка или «план прежнего кода»; значение None — убрать ключ."""
    sp = _spec_of(e, iid)
    for k, v in kw.items():
        if v is None:
            sp.pop(k, None)
        else:
            sp[k] = v
    e.con.execute("UPDATE intents SET spec_json=? WHERE id=?", (store.jdump(sp), iid))


def _deal_hash(e, did) -> str:
    return eng.deal_instrument(e.con, store.get_deal(e.con, did)).inst_hash()


def _snap(e):
    """Всё, что уходит наружу: свопы и заявки, approve, настройка плеча."""
    return fx.sends(e), len(e.spot.approvals), len(e.perp.setups)


def _failed(e, iid, text: str | None = None) -> None:
    it = store.get_intent(e.con, iid)
    assert it["status"] == IntentStatus.FAILED, it["status"]
    if text is not None:
        assert text in (it["err"] or ""), it["err"]


def _mismatch(e, did) -> list[str]:
    return [json.loads(ev["json"])["why"] for ev in store.events(e.con, did) if ev["kind"] == "inst_mismatch"]


def _state(e, did) -> str:
    return store.get_deal(e.con, did)["state"]


def _propose(e, usd=100):
    return e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(usd), chat=fx.OWNER)


def _open(e, usd=200):
    p = _propose(e, usd)
    fx.run_approved(e, p)
    assert _state(e, p.deal_id) == DealState.OPEN
    return p


def _no_table():
    raise RuntimeError("table.json не читается")


def _even(e, did, m=1):
    bk = eng.deal_book(e.con, did)
    assert bk.known and D(0) <= bk.delta(18) < fx.FILT.step * m and e.perp.pos == -bk.short
    return bk


# ==== 1. подделанный отпечаток — FAILED до любого действия, для каждого вида намерения ===============================
def test_forged_hash_on_entry_fails_before_anything(tmp_path):
    e = fx.live_env(tmp_path)
    p = _propose(e)
    _set_spec(e, p.intent_id, inst_hash="0" * 16)
    fx.run_approved(e, p)
    assert _snap(e) == ZERO_SNAP
    _failed(e, p.intent_id, HASH_TEXT)
    assert _state(e, p.deal_id) == DealState.ABORTED
    assert len(_mismatch(e, p.deal_id)) == 1 and HASH_TEXT in _mismatch(e, p.deal_id)[0]
    assert not any(ev["kind"] == "start" for ev in store.events(e.con, p.deal_id)), "до RUNNING и события start"
    assert HASH_TEXT in e.hooks.reports[-1] and e.hooks.requotes == []


@pytest.mark.parametrize("kind", ["exit", "exit_perp", "rehedge", "undo"])
def test_forged_hash_on_open_deal_fails_for_every_intent_kind(tmp_path, kind):
    """Дохедж, откат и «выход перп» перекотировку не проходят — ловит только сверка в _execute."""
    e = fx.live_env(tmp_path)
    p = _open(e)
    if kind in ("rehedge", "undo"):
        t11._shrink_short(e, p.deal_id, 10)
    prop = {"exit": lambda: e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER),
            "exit_perp": lambda: e.desk.propose_exit(p.deal_id, None, True, chat=fx.OWNER),
            "rehedge": lambda: e.desk.propose_fix("rehedge", p.deal_id, chat=fx.OWNER),
            "undo": lambda: e.desk.propose_fix("undo", p.deal_id, chat=fx.OWNER)}[kind]()
    _set_spec(e, prop.intent_id, inst_hash="0" * 16)
    before = _snap(e)
    fx.run_approved(e, prop)
    assert _snap(e) == before
    _failed(e, prop.intent_id, HASH_TEXT)
    assert _state(e, p.deal_id) == DealState.OPEN, "открытая сделка не тронута"


# ==== 2. подделанные токен/символ/decimals при целом отпечатке (проба: было sends (1,1), OPEN) ======================
@pytest.mark.parametrize("forge", [dict(token=B_TOKEN), dict(symbol="AIW3USDC"), dict(token_dec=9),
                                   dict(token=B_TOKEN, symbol="AIW3USDC")])
def test_forged_spec_fields_with_intact_hash_send_nothing(tmp_path, forge):
    e = fx.live_env(tmp_path)
    p = _propose(e)
    _set_spec(e, p.intent_id, **forge)
    fx.run_approved(e, p)
    assert _snap(e) == ZERO_SNAP
    _failed(e, p.intent_id, "≠ сделке")
    assert _state(e, p.deal_id) == DealState.ABORTED


# ==== 3. колонку сделки правили после плана ===========================================================================
@pytest.mark.parametrize("col,val", [("token", B_TOKEN), ("symbol", "AIW3USDC"), ("token_dec", 9),
                                     ("perp_venue", "binance")])
def test_deal_column_changed_after_plan_sends_nothing(tmp_path, col, val):
    e = fx.live_env(tmp_path)
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    e.con.execute(f"UPDATE deals SET {col}=? WHERE id=?", (val, p.deal_id))
    before = _snap(e)
    fx.run_approved(e, x)
    assert _snap(e) == before
    _failed(e, x.intent_id, "расходятся")
    # и новый план по такой сделке — отказ у Desk, а не у кнопки
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    assert "и её инструмент расходятся" in ei.value.html


# ==== 4. намерение прежнего кода (без отпечатка) ======================================================================
@pytest.mark.parametrize("kind", ["entry", "exit", "rehedge"])
def test_intent_without_fingerprint_asks_to_resend(tmp_path, kind):
    e = fx.live_env(tmp_path)
    if kind == "entry":
        prop = _propose(e)
    else:
        p = _open(e)
        if kind == "rehedge":
            t11._shrink_short(e, p.deal_id, 10)
            prop = e.desk.propose_fix("rehedge", p.deal_id, chat=fx.OWNER)
        else:
            prop = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    _set_spec(e, prop.intent_id, inst_hash=None, instrument=None)
    before = _snap(e)
    fx.run_approved(e, prop)
    assert _snap(e) == before
    _failed(e, prop.intent_id, RESEND)
    assert RESEND in e.hooks.reports[-1]
    assert _state(e, prop.deal_id) == (DealState.ABORTED if kind == "entry" else DealState.OPEN)


# ==== 5. identity отозвана или строка сменилась к моменту кнопки ======================================================
REVOKED = {"mismatch": dict(mismatch=True), "ident_unknown": dict(ident="unknown"),
           "dex_link": dict(ident_ev="dex_link:binance"), "other_token": dict(spot=f"56:{B_TOKEN}"),
           "other_symbol": dict(perp="AIW3USDC"), "row_gone": None}


def _table_as(case):
    return dict(fx.TABLE, sf_rows=[]) if REVOKED[case] is None else t0._table(**REVOKED[case])


@pytest.mark.parametrize("case", list(REVOKED))
def test_identity_revoked_before_button_sends_nothing(tmp_path, case):
    e = fx.live_env(tmp_path)
    p = _propose(e)
    e.desk.table_loader = lambda: _table_as(case)
    fx.run_approved(e, p)
    assert _snap(e) == ZERO_SNAP
    _failed(e, p.intent_id)
    assert _state(e, p.deal_id) == DealState.ABORTED
    assert e.hooks.requotes == [], "новый план по отозванному инструменту бот сам не предлагает"
    if case in ("other_token", "other_symbol"):
        assert "в таблице сменился" in e.hooks.reports[-1]


# ==== 6. Н2 ревьюера: A подорожал, таблица на B — ворота издержек не обходятся =======================================
def _dear_a(e, extra=D("0.02")):
    """Токен A на DEX подорожал на 2 п.п. (котировки покупки A хуже пула на extra); B — как был."""
    real = e.spot.quote

    def quote(t_in, t_out, amount):
        q = real(t_in, t_out, amount)
        if t_out.lower() == fx.TOKEN:
            q = replace(q, amount_out=int(D(q.amount_out) / (1 + extra)))
        return q
    e.spot.quote = quote


@pytest.mark.parametrize("table_on_b", [False, True])
def test_dearer_token_with_table_switched_to_b_cannot_bypass_cost_drift(tmp_path, table_on_b):
    e = fx.live_env(tmp_path)
    p = _propose(e)
    _dear_a(e)
    if table_on_b:
        e.desk.table_loader = lambda: t0._table(spot=f"56:{B_TOKEN}")
    fx.run_approved(e, p)
    assert _snap(e) == ZERO_SNAP
    _failed(e, p.intent_id)
    if table_on_b:                             # проба ревью: был requote_ok с издержками B и своп A на +2 %
        assert e.hooks.requotes == [] and "в таблице сменился" in e.hooks.reports[-1]
        assert not any(ev["kind"] == "requote_ok" for ev in store.events(e.con, p.deal_id))
    else:                                      # контроль: тот же A без смены таблицы — ворота издержек
        assert len(e.hooks.requotes) == 1 and "издержки" in e.hooks.requotes[0][1]


# ==== 7. второй слой: перекотировка вернула план по другому инструменту ===============================================
@pytest.mark.parametrize("kind", ["entry", "exit"])
def test_requote_plan_for_other_instrument_fails_without_new_offer(tmp_path, monkeypatch, kind):
    e = fx.live_env(tmp_path)
    if kind == "entry":
        prop = _propose(e)
    else:
        p = _open(e)
        prop = e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER)
    real = e.desk.replan

    def forged(it, deal):
        pl = real(it, deal)
        pl.inputs["inst_hash"] = "x"
        return pl
    monkeypatch.setattr(e.desk, "replan", forged)
    before = _snap(e)
    fx.run_approved(e, prop)
    assert _snap(e) == before
    _failed(e, prop.intent_id, "перекотировка: инструмент сменился")
    assert e.hooks.requotes == [] and "ничего не отправлено" in e.hooks.reports[-1]
    assert _mismatch(e, prop.deal_id) == ["перекотировка: инструмент сменился"]
    assert _state(e, prop.deal_id) == (DealState.ABORTED if kind == "entry" else DealState.OPEN)


def test_every_plan_carries_the_deal_fingerprint(tmp_path):
    """plan.inputs.inst_hash — у входа, выхода (частичного, полного, «перп»), дохеджа и отката: перекотировка и шпион
    сверяют его с намерением."""
    e = fx.live_env(tmp_path)
    p = _open(e)
    h = _deal_hash(e, p.deal_id)
    assert p.plan.inputs["inst_hash"] == h
    for prop in (e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER),
                 e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER),
                 e.desk.propose_exit(p.deal_id, None, True, chat=fx.OWNER)):
        assert prop.plan.inputs["inst_hash"] == _spec_of(e, prop.intent_id)["inst_hash"] == h
    t11._shrink_short(e, p.deal_id, 10)
    for kind in ("rehedge", "undo"):
        prop = e.desk.propose_fix(kind, p.deal_id, chat=fx.OWNER)
        assert prop.plan.inputs == {"inst_hash": h} and _spec_of(e, prop.intent_id)["inst_hash"] == h


# ==== 8. биржа сменила контракт до кнопки =============================================================================
@pytest.mark.parametrize("pi", [PerpInstrument(fx.SYMBOL, "1000AIW3", "AIW3", D(1000), "USDT", "PERPETUAL"),
                                PerpInstrument(fx.SYMBOL, "BTC", "BTC", D(1), "USDT", "PERPETUAL"), None])
def test_exchange_changed_contract_before_button_sends_nothing(tmp_path, pi):
    e = fx.live_env(tmp_path)
    p = _propose(e)
    e.perp.instrument = lambda s: pi
    fx.run_approved(e, p)
    assert _snap(e) == ZERO_SNAP
    _failed(e, p.intent_id)
    assert _state(e, p.deal_id) == DealState.ABORTED


def test_decimals_changed_on_chain_before_button_refused_by_frozen_pair(tmp_path):
    """Перекотировка входа строит план из инструмента сделки: decimals с цепи сверяются с замороженными ещё до
    котировок (первый слой), а не только постфактум по строке таблицы."""
    e = fx.live_env(tmp_path)
    p = _propose(e)
    e.spot.decimals = lambda token, hint=None: 9
    fx.run_approved(e, p)
    assert _snap(e) == ZERO_SNAP
    _failed(e, p.intent_id, "перекотировка не удалась")
    assert "ecimals токена AIW3 на цепи (9) ≠ сделке (18)" in e.hooks.reports[-1]
    assert _state(e, p.deal_id) == DealState.ABORTED and e.hooks.requotes == []


def _frozen_pair(e, **kw):
    inst = t11._spec(**kw)
    deal = {"id": "DX", "coin": "AIW3", "chain": inst.chain, "token": inst.token, "token_dec": inst.token_dec,
            "perp_venue": inst.perp_venue, "symbol": inst.perp_symbol}
    return e.desk.pair_from(inst, deal, verify_table=False)


def test_plan_entry_with_frozen_pair_checks_chain_decimals_and_exchange_contract(tmp_path):
    e = fx.live_env(tmp_path)
    e.desk.table_loader = _no_table            # с замороженным инструментом строку по монете не выбирают
    pair = _frozen_pair(e)
    plan, ctx = e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(100), sim=False, write_checks=False, pair=pair)
    assert plan.inputs["inst_hash"] == pair.spec.inst_hash() == ctx["inst"].inst_hash()
    assert ctx["pair"] is not pair and pair.token_dec == 18 and ctx["pair"].token == fx.TOKEN
    # биржа говорит «1 токен в контракте», а заморожено 1000 на том же символе — контракт другой
    with pytest.raises(eng.Refused) as ei:
        e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(100), sim=False, write_checks=False,
                          pair=_frozen_pair(e, units_per_contract=D(1000)))
    assert "на бирже изменился (m 1000 → 1)" in ei.value.html
    # decimals с цепи ≠ замороженным
    e.spot.decimals = lambda token, hint=None: 9
    with pytest.raises(eng.Refused) as ei:
        e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(100), sim=False, write_checks=False, pair=_frozen_pair(e))
    assert "ecimals токена AIW3 на цепи (9) ≠ сделке (18)" in ei.value.html
    assert fx.sends(e) == (0, 0)


def test_pair_from_takes_frozen_instrument_not_the_table_row(tmp_path):
    e = fx.live_env(tmp_path)
    p = _open(e)
    deal = store.get_deal(e.con, p.deal_id)
    inst = eng.deal_instrument(e.con, deal)
    e.desk.table_loader = _no_table
    pr = e.desk.pair_from(inst, deal, verify_table=False)
    assert (pr.chain, pr.token, pr.token_dec, pr.symbol, pr.spec) == ("bsc", fx.TOKEN, 18, fx.SYMBOL, inst)
    e.desk.table_loader = lambda: t0._table(period=8)
    pr2 = e.desk.pair_from(inst, deal, verify_table=True, spot_s="okx·bsc", perp_s="aster", cfg=e.loader())
    assert pr2.period_h == 8 and (pr2.token, pr2.symbol, pr2.spec) == (fx.TOKEN, fx.SYMBOL, inst), \
        "период — свежий из таблицы (справка), идентичность — замороженная"
    for bad in (dict(spot=f"56:{B_TOKEN}"), dict(perp="AIW3USDC")):
        e.desk.table_loader = lambda bad=bad: t0._table(**bad)
        with pytest.raises(eng.Refused) as ei:
            e.desk.pair_from(inst, deal, verify_table=True, spot_s="okx·bsc", perp_s="aster", cfg=e.loader())
        assert "в таблице сменился" in ei.value.html
    with pytest.raises(eng.Refused) as ei:
        e.desk.pair_from(inst, dict(deal, perp_venue="binance"), verify_table=False)
    assert "площадка binance ≠ aster" in ei.value.html


# ==== 9. «продолжить» входа: инструмент — сделки, таблица только подтверждает =========================================
@pytest.mark.parametrize("table", [dict(spot=f"56:{B_TOKEN}"), dict(perp="AIW3USDC"), dict(mismatch=True)])
def test_resume_entry_refuses_when_table_moved_to_other_instrument(tmp_path, table):
    e, p = t11._partial_entry(tmp_path)
    e.desk.table_loader = lambda: t0._table(**table)
    n = e.con.execute("SELECT count(*) FROM intents").fetchone()[0]
    before = _snap(e)
    with pytest.raises(eng.Refused):
        e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert e.con.execute("SELECT count(*) FROM intents").fetchone()[0] == n, "намерение не создано"
    assert _snap(e) == before and _state(e, p.deal_id) == DealState.PAUSED


def test_resume_entry_same_instrument_plans_from_deal_and_completes(tmp_path):
    e, p = t11._partial_entry(tmp_path)
    more = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    sp, deal = _spec_of(e, more.intent_id), store.get_deal(e.con, p.deal_id)
    assert (sp["token"], sp["symbol"], sp["token_dec"]) == (deal["token"], deal["symbol"], deal["token_dec"])
    assert sp["resume"] is True and sp["inst_hash"] == _deal_hash(e, p.deal_id) == more.plan.inputs["inst_hash"]
    fx.run_approved(e, more)
    assert _state(e, p.deal_id) == DealState.OPEN
    _even(e, p.deal_id)


def test_resume_entry_table_switched_before_button_sends_nothing_and_keeps_deal(tmp_path):
    e, p = t11._partial_entry(tmp_path)
    more = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    e.desk.table_loader = lambda: t0._table(spot=f"56:{B_TOKEN}")
    before = _snap(e)
    fx.run_approved(e, more)
    assert _snap(e) == before
    _failed(e, more.intent_id)
    assert _state(e, p.deal_id) == DealState.PAUSED and e.hooks.requotes == []
    _even(e, p.deal_id)


def test_resume_entry_m1000_keeps_the_frozen_contract(tmp_path):
    e = t12.mult_env(tmp_path, clip="50")
    p = _propose(e, 200)
    e.spot.on_swap = lambda: store.set_paused(e.con2, True)
    fx.run_approved(e, p)
    store.set_paused(e.con, False)
    e.spot.on_swap = None
    assert _state(e, p.deal_id) == DealState.PAUSED
    more = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    sp = _spec_of(e, more.intent_id)
    assert sp["inst_hash"] == _deal_hash(e, p.deal_id) and sp["symbol"] == t12.MSYM
    assert sp["instrument"]["units_per_contract"] == "1000"
    fx.run_approved(e, more)
    assert _state(e, p.deal_id) == DealState.OPEN
    t12.assert_even(e, p.deal_id)


# ==== 10. выход, откат, дохедж — без таблицы ==========================================================================
def test_exit_undo_rehedge_do_not_read_the_table(tmp_path):
    e = fx.live_env(tmp_path)
    p = _open(e)
    e.desk.table_loader = _no_table
    t11._shrink_short(e, p.deal_id, NAKED)
    fix = e.desk.propose_fix("rehedge", p.deal_id, chat=fx.OWNER)
    fx.run_approved(e, fix)
    assert store.get_intent(e.con, fix.intent_id)["status"] == IntentStatus.DONE
    _even(e, p.deal_id)
    t11._shrink_short(e, p.deal_id, NAKED)
    und = e.desk.propose_fix("undo", p.deal_id, chat=fx.OWNER)
    fx.run_approved(e, und)
    assert store.get_intent(e.con, und.intent_id)["status"] == IntentStatus.DONE
    _even(e, p.deal_id)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER))
    assert _state(e, p.deal_id) == DealState.OPEN
    _even(e, p.deal_id)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert _state(e, p.deal_id) == DealState.CLOSED
    assert e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0


def test_perp_only_exit_then_undo_without_table(tmp_path):
    e = fx.live_env(tmp_path)
    p = _open(e)
    e.desk.table_loader = _no_table
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, True, chat=fx.OWNER))
    assert e.perp.pos == 0 and _state(e, p.deal_id) == DealState.PAUSED
    fx.run_approved(e, e.desk.propose_fix("undo", p.deal_id, chat=fx.OWNER))
    assert _state(e, p.deal_id) == DealState.CLOSED


# ==== DQA9Q (m = 1): после выката — без ручных действий; план прежнего кода не исполняется ============================
@pytest.mark.parametrize("started", [True, False])
def test_dqa9q_exit_after_update_uses_frozen_instrument(tmp_path, started):
    e = t11._dqa9q_env(tmp_path, old_db=started)
    if started:
        reconcile.startup(e.con, e.legs, now=tm.NOW)
    e.desk.table_loader = _no_table
    # намерение прежнего кода (без отпечатка), одобренное до выката: старт его гасит, а если нет — не исполняется
    x0 = e.desk.propose_exit("DQA9Q", None, False, chat=fx.OWNER)
    _set_spec(e, x0.intent_id, inst_hash=None, instrument=None)
    fx.run_approved(e, x0)
    _failed(e, x0.intent_id, RESEND)
    assert fx.sends(e) == (0, 0) and _state(e, "DQA9Q") == DealState.OPEN
    # частичный и полный выход — по инструменту сделки (бэкфилл или вердикт на лету: тот же отпечаток)
    x1 = e.desk.propose_exit("DQA9Q", D(100), False, chat=fx.OWNER)
    assert _spec_of(e, x1.intent_id)["inst_hash"] == _deal_hash(e, "DQA9Q") == x1.plan.inputs["inst_hash"]
    assert (store.get_deal(e.con, "DQA9Q")["inst_json"] is None) is (not started)
    fx.run_approved(e, x1)
    assert _state(e, "DQA9Q") == DealState.OPEN
    _even(e, "DQA9Q")
    fx.run_approved(e, e.desk.propose_exit("DQA9Q", None, False, chat=fx.OWNER))
    assert _state(e, "DQA9Q") == DealState.CLOSED and e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0
    assert _mismatch(e, "DQA9Q") == [f"план построен без отпечатка инструмента (до обновления) — {RESEND}"]


# ==== 11. шпион: ни одной отправки без совпадения отпечатка намерения и сделки ========================================
def _spy(e):
    bad, seen = [], []

    def check(what):
        cur = e.engine.current
        it = store.get_intent(e.con, cur[0]) if cur else None
        if it is None:
            bad.append((what, "вне намерения"))
            return
        got, want = json.loads(it["spec_json"]).get("inst_hash"), _deal_hash(e, it["deal_id"])
        if got is None or got != want:
            bad.append((what, it["id"], got, want))
        seen.append((what, it["kind"]))
    swap, ioc = e.spot.swap, e.perp.ioc

    def spy_swap(*a, **kw):
        check("swap")
        return swap(*a, **kw)

    def spy_ioc(*a, **kw):
        check("ioc")
        return ioc(*a, **kw)
    e.spot.swap, e.perp.ioc = spy_swap, spy_ioc
    return bad, seen


@pytest.mark.parametrize("m", [1, 1000])
def test_spy_no_send_without_matching_fingerprint(tmp_path, m):
    e = fx.live_env(tmp_path, clip="50") if m == 1 else t12.mult_env(tmp_path, clip="50")
    bad, seen = _spy(e)
    p = _open(e, 200)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER))
    if m == 1:                                 # дохедж и откат по голой ноге NAKED токенов (меньше контракта при m = 1000)
        t11._shrink_short(e, p.deal_id, NAKED)
        fx.run_approved(e, e.desk.propose_fix("rehedge", p.deal_id, chat=fx.OWNER))
        t11._shrink_short(e, p.deal_id, NAKED)
        fx.run_approved(e, e.desk.propose_fix("undo", p.deal_id, chat=fx.OWNER))
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert _state(e, p.deal_id) == DealState.CLOSED
    assert bad == []
    kinds = {k for _w, k in seen}
    assert {"entry", "exit"} <= kinds and ("swap", "entry") in seen and ("ioc", "exit") in seen
    if m == 1:
        assert {"rehedge", "undo"} <= kinds
