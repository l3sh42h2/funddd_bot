"""Фаза 1.4 по ревью 13.09 (Н1): цель возобновления выхода — в ТОКЕНАХ. Частичный выход замораживает количество токенов
при предложении (spec.units): перекотировка у кнопки и «продолжить» его по цене пула не пересчитывают. Остаток
прерванного выхода = цель прерванного намерения − продано им; по цепочке «продолжить» это цель корня − Σ продаж, так что
всего продано ровно заказанное (а не 150-200 %). Частичный остаётся частичным (spec.all False); полный выход и «перп» —
как были. Фейки — из test_trade_engine и шагов 0, 1.1-1.3 (без сети и ключей)."""
import json
from decimal import Decimal as D

import pytest

from funding_bot.trade import engine as eng, reconcile, store
from funding_bot.trade.store import ClipState, DealState, IntentStatus
from funding_bot.tg import views
from funding_bot.tg.bot import Bot, Jobs
from funding_bot.tg.sender import Sender, html_ok

import test_marks as tm
import test_phase1_instrument as t11
import test_phase1_units as t12
import test_trade_engine as fx

E18 = D(10) ** 18
STEP = fx.FILT.step


def _spec(e, iid) -> dict:
    return json.loads(store.get_intent(e.con, iid)["spec_json"])


def _set_spec(e, iid, **kw) -> None:
    sp = _spec(e, iid)
    for k, v in kw.items():
        if v is None:
            sp.pop(k, None)
        else:
            sp[k] = v
    e.con.execute("UPDATE intents SET spec_json=? WHERE id=?", (store.jdump(sp), iid))


def _status(e, iid) -> str:
    return store.get_intent(e.con, iid)["status"]


def _state(e, did) -> str:
    return store.get_deal(e.con, did)["state"]


def _n_intents(e) -> int:
    return e.con.execute("SELECT count(*) FROM intents").fetchone()[0]


def _sold(e, iid) -> int:
    """Сырые токены, проданные намерением выхода (своп ушёл и не откатился)."""
    return sum(int(c["dex_in"] or 0) for c in store.clips_of(e.con, iid)
               if c["state"] not in (ClipState.PLANNED, ClipState.DEX_REVERTED))


def _stop_after(e, n: int) -> None:
    """«стоп» владельца после n-го свопа: хедж клипа доводится, следующий клип не начинается (намерение PARTIAL)."""
    k = [0]

    def on_swap():
        k[0] += 1
        if k[0] == n:
            store.set_paused(e.con2, True)
    e.spot.on_swap = on_swap


def _unstop(e) -> None:
    store.set_paused(e.con, False)
    e.spot.on_swap = None


def _open(e, usd=200):
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(usd), chat=fx.OWNER)
    fx.run_approved(e, p)
    assert _state(e, p.deal_id) == DealState.OPEN
    return p


def _interrupted(e, did, usd, n: int = 1):
    """Частичный выход на usd, «стоп» после n-го свопа → намерение PARTIAL, сделка PAUSED, продана часть цели."""
    x = e.desk.propose_exit(did, D(usd), False, chat=fx.OWNER)
    _stop_after(e, n)
    fx.run_approved(e, x)
    _unstop(e)
    assert _status(e, x.intent_id) == IntentStatus.PARTIAL
    assert 0 < _sold(e, x.intent_id) < _spec(e, x.intent_id)["units"]
    return x


def _even(e, did, m=1):
    """0 ≤ токены − контракты·m < шаг·m; позиция биржи = шорт журнала."""
    bk = eng.deal_book(e.con, did)
    assert bk.known and D(0) <= bk.delta(18) < STEP * m, bk.delta(18)
    assert e.perp.pos == -bk.short
    return bk


def _left_usd(e, did) -> D:
    return eng.deal_book(e.con, did).tokens(18) * fx.PX


# ==== 1. Н1, сценарий ревьюера: выход 100 из 200, стоп после клипа 1, «продолжить» ====================================
def test_reviewer_scenario_resume_sells_exactly_the_rest(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    target, sold1 = _spec(e, x1.intent_id)["units"], _sold(e, x1.intent_id)
    before = fx.sends(e)
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert fx.sends(e) == before, "план — ничего не отправлено"
    s = _spec(e, r.intent_id)
    assert r.kind == "exit" and s["all"] is False and s["perp_only"] is False and s["resume"] is True
    assert s["root"] == x1.intent_id and s["root_units"] == target
    assert s["units"] == target - sold1, "остаток = цель − продано, а не исходные 100 $ заново"
    assert s["inst_hash"] == eng.deal_instrument(e.con, store.get_deal(e.con, p.deal_id)).inst_hash()
    fx.run_approved(e, r)
    assert _status(e, r.intent_id) == IntentStatus.DONE
    assert sold1 + _sold(e, r.intent_id) == target, "продано ровно заказанное (было 150 %)"
    assert _state(e, p.deal_id) == DealState.OPEN
    _even(e, p.deal_id)
    assert abs(_left_usd(e, p.deal_id) - 100) < 1, _left_usd(e, p.deal_id)    # осталось ≈ 100 $ из 200


# ==== 2. две паузы подряд: сумма по цепочке = цель корня ============================================================
def test_two_interruptions_in_a_row_sum_to_the_root_target(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 150)
    target = _spec(e, x1.intent_id)["units"]
    r1 = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    _stop_after(e, 1)
    fx.run_approved(e, r1)
    _unstop(e)
    assert _status(e, r1.intent_id) == IntentStatus.PARTIAL
    r2 = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s2 = _spec(e, r2.intent_id)
    assert s2["root"] == x1.intent_id and s2["root_units"] == target and s2["all"] is False
    assert s2["units"] == target - _sold(e, x1.intent_id) - _sold(e, r1.intent_id)
    fx.run_approved(e, r2)
    assert sum(_sold(e, i) for i in (x1.intent_id, r1.intent_id, r2.intent_id)) == target, "было 200 % и CLOSED"
    assert _state(e, p.deal_id) == DealState.OPEN
    _even(e, p.deal_id)
    assert abs(_left_usd(e, p.deal_id) - 50) < D("0.5"), _left_usd(e, p.deal_id)


# ==== 3. выход 150 из 200, стоп после 50: «продолжить» не превращается в полный выход ================================
def test_resume_of_large_partial_exit_does_not_turn_into_full_exit(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 150)
    left_before = eng.deal_book(e.con, p.deal_id).tokens_raw
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    assert s["all"] is False and s["units"] < left_before, "прежний код: 150 $ ≥ остатка позиции → all = True"
    fx.run_approved(e, r)
    assert not any(ev["kind"] == "exit_residual" for ev in store.events(e.con, p.deal_id))
    assert _state(e, p.deal_id) == DealState.OPEN and eng.deal_book(e.con, p.deal_id).short > 0
    _even(e, p.deal_id)
    assert abs(_left_usd(e, p.deal_id) - 50) < D("0.5"), _left_usd(e, p.deal_id)
    assert _sold(e, x1.intent_id) + _sold(e, r.intent_id) == _spec(e, x1.intent_id)["units"]


def test_mutated_legacy_spec_cannot_increase_durable_root_target(tmp_path):
    """M4: редактирование прежнего spec не увеличивает уже одобренную корневую цель."""
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    approved=_spec(e,x1.intent_id)['units']
    remaining=approved-_sold(e,x1.intent_id)
    _set_spec(e, x1.intent_id, units=_spec(e, x1.intent_id)["units"] * 10)
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    assert s["all"] is False and s["units"] == remaining < eng.deal_book(e.con,p.deal_id).tokens_raw
    head = views.intent_head(store.get_intent(e.con, r.intent_id), store.get_deal(e.con, p.deal_id))
    assert f"<b>{head.title}</b>" in fx.render(r) and "всё" not in head.title, (head.title, fx.render(r))
    fx.run_approved(e, r)
    assert _sold(e,x1.intent_id)+_sold(e,r.intent_id)==approved
    assert _state(e,p.deal_id)==DealState.OPEN and e.perp.pos < 0 and e.spot.bal[fx.TOKEN] > 0


# ==== 4. цена пула сдвинулась между стопом и «продолжить» — остаток в токенах тот же ==================================
def test_pool_price_move_between_stop_and_resume_keeps_the_rest_in_tokens(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    target, sold1 = _spec(e, x1.intent_id)["units"], _sold(e, x1.intent_id)
    e.market.px = fx.PX * D("0.8")
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    assert s["units"] == target - sold1
    assert D(str(s["usd"])) < 45, "≈ $ остатка по новой цене — только шапка; цель — токены"
    fx.run_approved(e, r)
    assert sold1 + _sold(e, r.intent_id) == target
    _even(e, p.deal_id)


# ==== 5. перекотировка у кнопки не пересчитывает $ → токены ==========================================================
def test_requote_at_button_keeps_the_approved_tokens(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, D(100), False, chat=fx.OWNER)
    units = _spec(e, x.intent_id)["units"]
    e.market.px = fx.PX * D("0.9")             # до кнопки: 100 $ по новой цене — 2 716.65 токена вместо 2 444.99
    fx.run_approved(e, x)
    assert any(ev["kind"] == "requote_ok" for ev in store.events(e.con, p.deal_id)), "перекотировка была"
    assert _status(e, x.intent_id) == IntentStatus.DONE
    assert _sold(e, x.intent_id) == units, (_sold(e, x.intent_id), units)
    _even(e, p.deal_id)


def test_requote_of_full_exit_still_sells_everything(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    e.market.px = fx.PX * D("0.9")
    fx.run_approved(e, x)
    assert _state(e, p.deal_id) == DealState.CLOSED and e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0


# ==== 6. исход клипа прерванного выхода неизвестен — сначала «позиции» ================================================
@pytest.mark.parametrize("state", [ClipState.DEX_UNKNOWN, ClipState.DEX_SENT])
def test_unknown_clip_outcome_refuses_resume(tmp_path, state):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    cid = store.clips_of(e.con, x1.intent_id)[0]["id"]
    e.con.execute("UPDATE clips SET state=? WHERE id=?", (str(state), cid))
    n, before = _n_intents(e), fx.sends(e)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert "неизвестен" in fx.render(ei.value) and "сначала «позиции»" in fx.render(ei.value) and x1.intent_id in fx.render(ei.value)
    assert _n_intents(e) == n and fx.sends(e) == before


# ==== 7. намерение без цели в токенах (план прежнего кода) — отказ с подсказкой ======================================
def test_intent_without_token_target_refuses_with_hint(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    _set_spec(e, x1.intent_id, units=None)
    n, before = _n_intents(e), fx.sends(e)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert "остаток не вычислить" in fx.render(ei.value)
    assert f"выход {p.deal_id} &lt;остаток $&gt;" in fx.render(ei.value), "угловые скобки экранированы один раз"
    assert _n_intents(e) == n and fx.sends(e) == before


def test_resume_after_target_already_sold_is_refused(tmp_path):
    """Последнее звено продало всю свою цель, но осталось не DONE (пауза на итоге) — продолжать нечего."""
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, D(100), False, chat=fx.OWNER)
    fx.run_approved(e, x)
    assert _sold(e, x.intent_id) == _spec(e, x.intent_id)["units"]
    e.con.execute("UPDATE intents SET status=? WHERE id=?", (str(IntentStatus.PARTIAL), x.intent_id))
    n = _n_intents(e)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert f"Выход {x.intent_id} выполнен" in fx.render(ei.value)     # первая буква отказа — заглавная (views.refused)
    assert _n_intents(e) == n


def test_resume_of_partial_exit_refused_while_stopped(tmp_path):
    """«стоп» владельца: «продолжить <id>» выхода нового не начинает — те же предпроверки, что у выхода."""
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    _interrupted(e, p.deal_id, 100)
    store.set_paused(e.con, True)
    n = _n_intents(e)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert "Пауза («стоп»)" in fx.render(ei.value) and _n_intents(e) == n


def test_resume_of_partial_exit_refused_when_instrument_unverified(tmp_path):
    """Tampering with an execution-bound frozen instrument blocks all new sends."""
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    _interrupted(e, p.deal_id, 100)
    e.con.execute("UPDATE deals SET inst_json='{' WHERE id=?", (p.deal_id,))
    assert not eng.deal_book(e.con, p.deal_id).inst_ok
    n, before = _n_intents(e), fx.sends(e)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert "не подтверждён" in fx.render(ei.value) and f"«выход {p.deal_id}» целиком" in fx.render(ei.value)
    assert _n_intents(e) == n and fx.sends(e) == before
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert _state(e, p.deal_id) == DealState.PAUSED and fx.sends(e) == before


# ==== 8. bot.requote для «продолжить» выхода — снова через остаток, а не usd / цена ====================================
def test_bot_requote_of_resume_exit_goes_through_the_rest(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    rest = _spec(e, x1.intent_id)["units"] - _sold(e, x1.intent_id)
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    e.market.c0 = D("0.01")                    # спред пула ×100: издержки у кнопки выше допуска 0.5 п.п.
    e.market.px = fx.PX * D("0.9")             # и цена другая: пересчёт $ → токены дал бы другое число
    before = fx.sends(e)
    fx.run_approved(e, r)
    assert fx.sends(e) == before and _status(e, r.intent_id) == IntentStatus.FAILED
    assert e.hooks.requotes and e.hooks.requotes[-1][0] == r.intent_id
    tg = fx.FakeTg()
    sender = Sender(tg, gap_s=0, sleep=lambda s: None)
    bot = Bot(conns=e.conns, desk=e.desk, engine=fx.FakeEngine(), sender=sender, legs=e.legs, poll_api=tg,
              owner_loader=e.loader, mode="live", jobs=Jobs(sync=True))
    bot.requote(r.intent_id, e.hooks.requotes[-1][1])
    sender.drain()
    new = e.con.execute("SELECT id FROM intents WHERE deal_id=? AND kind='exit' ORDER BY created DESC LIMIT 1",
                        (p.deal_id,)).fetchone()["id"]
    assert new not in (x1.intent_id, r.intent_id)
    s = _spec(e, new)
    assert s["resume"] is True and s["all"] is False and s["units"] == rest, (s["units"], rest)
    assert s["root"] == x1.intent_id
    plans = [c[2] for c in tg.calls if c[0] == "send" and c[3]]
    assert plans and "Остаток выхода" in plans[-1]
    assert fx.sends(e) == before


# ==== 9. регрессия: «продолжить» полного выхода и «перп» — как были =================================================
def test_resume_of_full_exit_still_sells_everything(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    _stop_after(e, 1)
    fx.run_approved(e, x)
    _unstop(e)
    assert _status(e, x.intent_id) == IntentStatus.PARTIAL
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    assert s["all"] is True and s.get("resume") is True
    assert s['units']==store.operation_remaining(store.operation_of_intent(e.con,r.intent_id))
    assert store.operation_of_intent(e.con,r.intent_id)['id']==store.operation_of_intent(e.con,x.intent_id)['id']
    fx.run_approved(e, r)
    assert _state(e, p.deal_id) == DealState.CLOSED and e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0


def test_resume_of_perp_only_exit_is_as_before(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x = e.desk.propose_exit(p.deal_id, None, True, chat=fx.OWNER)
    store.set_paused(e.con, True)
    fx.run_approved(e, x)
    store.set_paused(e.con, False)
    assert _status(e, x.intent_id) == IntentStatus.FAILED
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert _spec(e, r.intent_id)["perp_only"] is True
    fx.run_approved(e, r)
    bk = eng.deal_book(e.con, p.deal_id)
    assert _state(e, p.deal_id) == DealState.PAUSED and bk.short == 0 and e.perp.pos == 0 and bk.tokens(18) > 0


# ==== 10. текст плана «продолжить» выхода =============================================================================
def test_resume_plan_text(tmp_path):
    e = fx.live_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    assert "Остаток выхода" not in fx.render(x1), "обычный частичный выход — текст прежний"
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    rest, root = D(s["units"]) / E18, D(s["root_units"]) / E18
    assert f"Остаток выхода: {views.tok(rest, step=STEP)} из {views.tok(root, step=STEP)} AIW3" in fx.render(r)
    assert "Остаток выхода: 1 222 из 2 445 AIW3" in fx.render(r).replace(" ", " ").replace("\xa0", " "), fx.render(r)
    head = views.intent_head(store.get_intent(e.con, r.intent_id), store.get_deal(e.con, p.deal_id))
    assert f"<b>{head.title}</b>" in fx.render(r), "шапка сообщения = шапка из намерения (кнопка, строка закрытия)"
    assert "Выход AIW3 · 50 из 200" in head.title.replace("\xa0", " "), head.title
    assert head.ok.replace("\xa0", " ") == "✅ Выйти 50 $"
    assert "Продать спот 1 222 · откупить шорт 1 222" in fx.render(r).replace(" ", " ").replace("\xa0", " ")
    assert html_ok(fx.render(r)) and not fx.RAW_NUM.search(fx.render(r)) and not fx.ASCII_MINUS.search(fx.render(r))


# ==== 11. m = 1000: остаток в токенах, откуп в контрактах ============================================================
def test_resume_m1000_sells_exactly_the_rest_and_stays_even(tmp_path):
    e = t12.mult_env(tmp_path, clip="50")
    p = _open(e)
    x1 = _interrupted(e, p.deal_id, 100)
    t12.assert_even(e, p.deal_id)
    target, sold1 = _spec(e, x1.intent_id)["units"], _sold(e, x1.intent_id)
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    assert s["units"] == target - sold1 and s["all"] is False
    assert "контр. (= 1 000 токенов)" in fx.render(r).replace(" ", " ").replace("\xa0", " ") and "Остаток выхода" in fx.render(r)
    fx.run_approved(e, r)
    assert sold1 + _sold(e, r.intent_id) == target
    assert _state(e, p.deal_id) == DealState.OPEN
    bk = t12.assert_even(e, p.deal_id)
    assert bk.short == 2                        # 4 889 − 2 445 = 2 444 токена: 2 контракта, дельта 444 < 1 000


# ==== DQA9Q-подобная сделка (m = 1, открыта до фазы 1): частичный выход, стоп, «продолжить» ==========================
def test_dqa9q_partial_exit_resume_after_update(tmp_path):
    e = t11._dqa9q_env(tmp_path)
    e.path.write_text(fx.live_toml("50"))      # клипы по 50 $ — чтобы выход прервался посередине
    t11._activate_core(e)
    x1 = _interrupted(e, "DQA9Q", 100)
    target = _spec(e, x1.intent_id)["units"]
    r = e.desk.propose_resume("DQA9Q", chat=fx.OWNER)
    s = _spec(e, r.intent_id)
    assert s["units"] == target - _sold(e, x1.intent_id) and s["root"] == x1.intent_id
    assert s["inst_hash"] == eng.deal_instrument(e.con, store.get_deal(e.con, "DQA9Q")).inst_hash()
    assert "контр." not in fx.render(r) and "не подтверждён" not in fx.render(r)
    fx.run_approved(e, r)
    assert _sold(e, x1.intent_id) + _sold(e, r.intent_id) == target
    assert _state(e, "DQA9Q") == DealState.OPEN
    bk = _even(e, "DQA9Q")
    assert bk.tokens_raw == tm.Q_RAW - target and e.spot.bal[fx.TOKEN] == bk.tokens_raw
