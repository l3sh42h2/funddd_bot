"""Шаг C3: Telegram и кабинет связки Solana × Hyperliquid поверх настоящих Desk/Engine/SolanaExecutor/HyperliquidTrade
на фейках сети (tests/sol_c2_world.py): «вход ANSEM sol-auto hyperliquid·para 150» → план с кнопками → исполнение →
итог со ссылками Solscan/HL; «позиции» (и «позиции sol»); «выход <id>» → итог сделки; кнопки привязаны к операции
(старая кнопка после нового плана не исполняется); частичный выход — честный отказ; перекотировка входа SOL; «статус»;
кабинет (mint с регистром, фактический маршрут, USDC). Тексты BSC не меняются."""
import re
from decimal import Decimal as D
import pytest
from funding_bot import cabinet
from funding_bot.trade import store
from funding_bot.trade.owner import SOL_HL
from funding_bot.trade.runtime import RuntimeRegistry
from funding_bot.trade.solana import ANSEM_MINT
from funding_bot.trade.store import DealState, IntentStatus, OpState
from funding_bot.tg import auth, views
from funding_bot.tg.bot import Bot, Jobs
from funding_bot.tg.sender import Sender, html_ok
import test_trade_engine as TE
from sol_c2_world import approve_run, enter, make_world

pytest.importorskip("solders")
pytest.importorskip("eth_account")
OWNER = 42                                         # telegram.owner_id мира (sol_hl_fixtures)
FOREIGN = re.compile(r"USDT|BNB|Aster|bscscan|BscScan|okx·sol")    # в текстах SOL/HL их быть не должно (V02)
flat = lambda s: s.replace(views.NBSP, " ")        # noqa: E731


def bot_world(tmp_path, **kw):
    w = make_world(tmp_path, **kw)
    tg = TE.FakeTg()
    sender = Sender(tg, gap_s=0, sleep=lambda s: None)
    bot = Bot(conns=w.conns, desk=w.desk, engine=w.engine, sender=sender, legs=w.reg, poll_api=tg,
              owner_loader=w.loader, mode="live", jobs=Jobs(sync=True), clock=w.clock)
    return w, tg, sender, bot


def say(w, bot, sender, text):
    v = bot.handle(TE.msg(text, uid=OWNER, ts=w.clock()))
    sender.drain()
    return v


def sends(tg):
    return [c for c in tg.calls if c[0] == "send"]


def plans(tg):
    return [c for c in sends(tg) if c[3]]


def press(w, bot, sender, plan, *, run=True):
    """Нажать ✅ под планом; исполнитель — тот же поток (очередь Engine), затем досылка сообщений."""
    data = plan[3]["inline_keyboard"][0][0]["callback_data"]
    bot.handle(TE.cb(data, uid=OWNER, mid=plan[4]))
    iid = data.split(":")[1]
    if run and not w.engine.q.empty():
        w.engine.execute(w.engine.q.get_nowait())
    sender.drain()
    return iid


def texts(tg):
    return [c[2] for c in sends(tg)] + [c[3] for c in tg.calls if c[0] == "edit"]


def test_bot_entry_positions_exit_full_cycle_texts_links_and_cabinet(tmp_path):
    w, tg, sender, bot = bot_world(tmp_path)
    assert say(w, bot, sender, "вход ANSEM sol-auto hyperliquid·para 150") == auth.OWNER_MSG
    (plan,) = plans(tg)
    t = plan[2]
    assert "<b>Вход ANSEM · 150 USDC</b>" in flat(t) and "para:ANSEM" in t and "Jupiter" in t
    assert "mint 9cRC…pump ↔ para:ANSEM: подтверждено владельцем до " in t          # основание соответствия
    assert "Маржа HL" in t and "мин." in t and "Шорт Hyperliquid para:ANSEM" in t
    assert html_ok(t) and not FOREIGN.search(t), t
    assert flat(plan[3]["inline_keyboard"][0][0]["text"]) == "✅ Войти 150 USDC"
    iid = press(w, bot, sender, plan)
    it = store.get_intent(w.con, iid)
    did = it["deal_id"]
    assert it["status"] == IntentStatus.DONE and store.get_deal(w.con, did)["state"] == DealState.OPEN
    fin = sends(tg)[-1][2]
    assert "Вход ANSEM выполнен" in fin and "ноги ровно" in fin
    assert 'href="https://solscan.io/tx/' in fin and 'href="https://app.hyperliquid.xyz/explorer/tx/0x' in fin
    assert html_ok(fin) and not FOREIGN.search(fin), fin
    prog = [x for x in texts(tg) if x.startswith("⏳") or "⏳ <b>Вход" in x]
    assert any("Своп Jupiter отправлен — жду finalized" in x for x in prog), prog
    assert any("Получено" in x and "шорт para:ANSEM" in x for x in prog), prog
    # «позиции» и «позиции sol»: сверка по сделке, PnL сейчас / при выходе
    say(w, bot, sender, "позиции")
    pos = sends(tg)[-1][2]
    assert did in pos and "сверка ✓" in pos and "PnL сейчас" in pos and "при выходе" in pos, pos
    say(w, bot, sender, "позиции sol")
    assert did in sends(tg)[-1][2]
    # выход целиком: план → кнопка → итог сделки со ссылками
    say(w, bot, sender, f"выход {did}")
    xplan = plans(tg)[-1]
    assert "<b>Выход ANSEM · всё</b>" in xplan[2] and "Откупить" in xplan[2] and not FOREIGN.search(xplan[2])
    assert flat(xplan[3]["inline_keyboard"][0][0]["text"]) == "✅ Выйти всё"
    press(w, bot, sender, xplan)
    assert store.get_deal(w.con, did)["state"] == DealState.CLOSED
    xfin = sends(tg)[-1][2]
    assert "сделка закрыта" in xfin and "Итог сделки" in xfin and "USDC" in xfin, xfin
    assert 'href="https://solscan.io/tx/' in xfin and html_ok(xfin) and not FOREIGN.search(xfin)
    # кабинет: mint с регистром (S01), фактический маршрут, USDC, итог сделки
    con = cabinet.open_ro(w.db)
    snap = cabinet.load_deals(con, now=w.clock())
    (v,) = [x for x in snap["deals"] if x["id"] == did]
    assert v["row_key"][0] == f"501:{ANSEM_MINT}" and v["sol"]["mint"] == ANSEM_MINT
    card = cabinet.deal_card(v)
    assert "спот Solana · Jupiter" in card and "para:ANSEM" in card and "USDC" in card and "okx·" not in card
    assert "подтверждено владельцем" in card and v["pnl"]["final"] is True and v["pnl"]["total"] is not None
    assert "PnL итог" in card and "USDC" in cabinet.pnl_block(v)


def test_old_button_after_new_plan_does_nothing_and_loses_buttons(tmp_path):
    w, tg, sender, bot = bot_world(tmp_path)
    prop = enter(w)
    did = prop.deal_id
    say(w, bot, sender, f"выход {did}")
    say(w, bot, sender, f"выход {did}")
    old, new = plans(tg)[-2], plans(tg)[-1]
    old_iid = old[3]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    assert store.get_intent(w.con, old_iid)["status"] == IntentStatus.EXPIRED       # снят новым планом
    assert any(c[0] == "edit" and c[2] == old[4] and c[4] is None and "истёк" in c[3] for c in tg.calls)
    sends0 = len(w.sol.sends)
    press(w, bot, sender, old)
    assert w.engine.q.empty() and len(w.sol.sends) == sends0                        # ничего не отправлено
    press(w, bot, sender, new)
    assert store.get_deal(w.con, did)["state"] == DealState.CLOSED


def test_engine_refuses_button_whose_operation_is_no_longer_the_approved_one(tmp_path):
    """Кнопка привязана к операции (id и хеш одобренных границ): операция снята — ничего не отправляется."""
    w = make_world(tmp_path)
    prop = enter(w)
    x = w.desk.propose_exit(prop.deal_id, None, False, chat=None)
    op = store.operation_of_intent(w.con, x.intent_id)
    assert store.approve_intent(w.con, x.intent_id, x.nonce, now=w.clock())
    store.set_operation_state(w.con, op["id"], OpState.STOPPED)
    store.set_operation_state(w.con, op["id"], OpState.ABANDONED)
    n = len(w.sol.sends)
    w.engine.execute(x.intent_id)
    assert store.get_intent(w.con, x.intent_id)["status"] == IntentStatus.FAILED
    assert "кнопка от старого плана" in w.hooks.reports[-1].lower() and len(w.sol.sends) == n
    assert store.get_deal(w.con, prop.deal_id)["state"] == DealState.OPEN


def test_partial_exit_and_foreign_profile_are_honest_refusals(tmp_path):
    w, tg, sender, bot = bot_world(tmp_path)
    prop = enter(w)
    for cmd in ("выход ANSEM sol 100 usdc", "выход ANSEM 500 ansem", f"выход {prop.deal_id} sol 20"):
        say(w, bot, sender, cmd)
        t = sends(tg)[-1][2]
        assert "частичный выход в пилоте выключен" in t.lower() and not plans(tg), (cmd, t)
    say(w, bot, sender, "выход ANSEM bsc")
    assert "связки Solana × Hyperliquid" in sends(tg)[-1][2]
    assert store.get_deal(w.con, prop.deal_id)["state"] == DealState.OPEN


def test_requote_of_sol_entry_gives_fresh_profile_plan(tmp_path):
    w, tg, sender, bot = bot_world(tmp_path)
    say(w, bot, sender, "вход ANSEM sol-auto hyperliquid·para 150")
    (plan,) = plans(tg)
    iid = plan[3]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    bot.requote(iid, "min_out_below_approved")
    sender.drain()
    assert len(plans(tg)) == 2 and "<b>Вход ANSEM · 150 USDC</b>" in flat(plans(tg)[-1][2])
    assert any(c[0] == "edit" and c[2] == plan[4] and "заменён" in c[3] for c in tg.calls)
    old = store.get_intent(w.con, iid)
    assert old["status"] == IntentStatus.EXPIRED and store.get_deal(w.con, old["deal_id"])["state"] == DealState.ABORTED


def test_stop_resume_help_and_status_for_profile(tmp_path):
    w, tg, sender, bot = bot_world(tmp_path)
    say(w, bot, sender, "помощь")
    assert "sol-auto hyperliquid·para" in sends(tg)[-1][2]
    assert "sol-auto" not in views.help_text(False) and views.help_text(False) == views.help_text(False, sol=False)
    say(w, bot, sender, "стоп")
    assert store.is_paused(w.con) and "пауза" in sends(tg)[-1][2].lower()
    say(w, bot, sender, "вход ANSEM sol-auto hyperliquid·para 150")
    assert "пауза" in sends(tg)[-1][2].lower() and not plans(tg)
    say(w, bot, sender, "продолжить")
    assert not store.is_paused(w.con)
    # связка не собралась — «статус» называет причину (BSC-статус без неё прежний)
    bot.legs = RuntimeRegistry(None, {SOL_HL: lambda s: (_ for _ in ()).throw(RuntimeError("нет RPC"))})
    with pytest.raises(Exception):
        bot.legs.for_profile(SOL_HL, False)
    st = views.status(bot.status_view())
    assert "связка Solana × Hyperliquid не собрана" in st and "нет RPC" in st


def test_cabinet_sol_row_key_keeps_mint_case_and_bsc_key_is_unchanged():
    bsc = {"chain": "bsc", "token": "0xABCdef" + "0" * 34, "perp_venue": "aster", "symbol": "AIW3USDT"}
    assert cabinet._row_key(bsc) == (("56:0xabcdef" + "0" * 34), "aster", "AIW3USDT")
    sol = {"chain": "sol", "token": ANSEM_MINT, "perp_venue": "hyperliquid", "symbol": "para:ANSEM",
           "inst_json": '{"schema": 2, "profile_id": "sol_best_hyperliquid"}'}
    assert cabinet._row_key(sol) == (f"501:{ANSEM_MINT}", "hyperliquid", "para:ANSEM")
    assert cabinet._row_key(dict(sol, token=ANSEM_MINT.lower()))[0] != cabinet._row_key(sol)[0]     # S02
