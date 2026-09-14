"""Фаза 0 по ревью 13.09: выход при возврате DEX (С2), отказ при множителе контракта (С1), заморозка инструмента
у кнопки (Н2), «продолжить» частичного выхода (Н1). Фейки — из test_trade_engine (без сети и ключей)."""
from decimal import Decimal as D

import pytest

from funding_bot.trade import engine as eng, store
from funding_bot.trade.store import DealState, IntentStatus
from funding_bot.trade.types import Book

import test_trade_engine as fx


def _table(**row):
    return dict(fx.TABLE, sf_rows=[dict(fx.TABLE["sf_rows"][0], **row)])


def _refund_last_exit_swap(e, pct: int):
    """Роутер вернул pct % входа последнего свопа выхода (списал меньше, чем просили)."""
    orig = e.spot.swap
    build = e.spot.build_swap

    def build_swap(t_in, t_out, amount, **kw):
        # A successful partial route must still honor its quoted minimum.
        # Model that route's actual input/output, not a receipt below minOut.
        if t_in.lower() != fx.STABLE:
            amount = amount * (100 - pct) // 100
        return build(t_in, t_out, amount, **kw)
    e.spot.build_swap = build_swap

    def swap(t_in, t_out, amount, clip_ref, **kw):
        if t_in.lower() == fx.STABLE:
            return orig(t_in, t_out, amount, clip_ref, **kw)
        result = orig(t_in, t_out, amount * (100 - pct) // 100, clip_ref, **kw)
        assert result.amount_out >= kw['approved_min_receive']
        return result
    e.spot.swap = swap


# ==== С2: полный выход, DEX вернул часть токенов ====================================================================
def test_full_exit_with_dex_refund_leaves_residual_hedged_then_second_exit_closes(tmp_path):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    fx.run_approved(e, p)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    _refund_last_exit_swap(e, 3)
    fx.run_approved(e, x)
    bk = eng.deal_book(e.con, p.deal_id)
    left, step = bk.tokens(18), fx.FILT.step
    assert left > step, "возврат оставил токены"
    assert bk.short == eng.floor_step(left, step) and 0 <= bk.delta(18) < step, "остаток захеджирован, а не голый"
    assert abs(e.perp.pos) == bk.short
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    assert any(ev["kind"] == "exit_residual" for ev in store.events(e.con, p.deal_id))
    assert "Выход не довыполнен" in e.hooks.reports[-1] and f"выход {p.deal_id}" in e.hooks.reports[-1]
    e.spot.swap = fx.LiveSpot.swap.__get__(e.spot)
    x2 = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    fx.run_approved(e, x2)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED
    assert e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0


def test_full_exit_without_refund_still_buys_whole_short(tmp_path):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    fx.run_approved(e, p)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED
    assert not any(ev["kind"] == "exit_residual" for ev in store.events(e.con, p.deal_id))


# ==== С1: множитель контракта ======================================================================================
@pytest.mark.parametrize("coin,symbol", [("AIW3", "1000AIW3USDT"), ("BONK", "1000BONKUSDT"),
                                         ("BABYDOGE", "1MBABYDOGEUSDT"), ("PEPE", "KPEPEUSDT")])
def test_contract_with_multiplier_is_refused_before_any_send(tmp_path, coin, symbol):
    e = fx.live_env(tmp_path)
    e.desk.table_loader = lambda: _table(base=coin, perp=symbol)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry(coin, "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert "1 контракт = 1 токен" in ei.value.html
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []


def test_hidden_multiplier_caught_by_price_ratio(tmp_path):
    """Имя символа без множителя, а контракт стоит ×1000 цены токена на DEX — единицы не сходятся, отказ."""
    e = fx.live_env(tmp_path)
    b = e.perp.b
    e.perp.b = Book(tuple((p * 1000, q) for p, q in b.bids), tuple((p * 1000, q) for p, q in b.asks), 0.0)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert "единицы не сходятся" in ei.value.html
    assert fx.sends(e) == (0, 0)


def test_intent_with_multiplier_symbol_approved_earlier_does_not_start(tmp_path):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    e.con.execute("UPDATE deals SET symbol='1000AIW3USDT' WHERE id=?", (p.deal_id,))
    e.con.commit()
    fx.run_approved(e, p)
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.FAILED


def test_plain_symbol_passes_unit_check():
    eng.unit_refusal("AIW3", "AIW3USDT")
    with pytest.raises(eng.Refused):
        eng.unit_refusal("AIW3", "BTCUSDT")


# ==== Н2: у кнопки строка таблицы указывает на другой токен ==========================================================
def test_replan_on_other_token_sends_nothing(tmp_path):
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    e.desk.table_loader = lambda: _table(spot="56:0x" + "b2" * 20)
    fx.run_approved(e, p)
    assert fx.sends(e) == (0, 0)
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.FAILED
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.ABORTED


def test_same_instrument_passes_and_refuses():
    deal = {"coin": "AIW3", "chain": "bsc", "token": fx.TOKEN, "token_dec": 18, "symbol": fx.SYMBOL}
    ok = eng.PairInfo(coin="AIW3", chain="bsc", token=fx.TOKEN, token_dec=18, symbol=fx.SYMBOL, period_h=D(1),
                      spot_label="okx·bsc")
    eng.Desk._same_instrument(None, deal, ok)
    for bad in (dict(token="0x" + "b2" * 20), dict(symbol="AIW3USDC"), dict(token_dec=9)):
        with pytest.raises(eng.Refused):
            eng.Desk._same_instrument(None, deal, eng.PairInfo(**{**vars(ok), **bad}))


# ==== Н1: «продолжить» частичного выхода ===========================================================================
def _interrupted_exit(tmp_path, usd):
    e = fx.live_env(tmp_path, clip="50")
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    fx.run_approved(e, p)
    x = e.desk.propose_exit(p.deal_id, usd, False, chat=fx.OWNER)
    e.spot.on_swap = lambda: store.set_paused(e.con2, True)
    fx.run_approved(e, x)
    store.set_paused(e.con, False)
    e.spot.on_swap = None
    sold = sum(int(c["dex_in"] or 0) for c in store.clips_of(e.con, x.intent_id))
    assert sold > 0
    return e, p


def test_resume_of_partial_exit_plans_the_rest(tmp_path):
    """Фаза 1.4 заменила отказ фазы 0: «продолжить» частичного выхода — план на остаток в токенах (цель − продано),
    частичный (all False), до кнопки ничего не отправлено."""
    e, p = _interrupted_exit(tmp_path, D(100))
    x1 = e.con.execute("SELECT * FROM intents WHERE deal_id=? AND kind='exit'", (p.deal_id,)).fetchone()
    spec1 = eng.json.loads(x1["spec_json"])
    sold = sum(int(c["dex_in"] or 0) for c in store.clips_of(e.con, x1["id"]))
    before = fx.sends(e)
    prop = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert prop.kind == "exit"
    spec = eng.json.loads(store.get_intent(e.con, prop.intent_id)["spec_json"])
    assert spec["all"] is False and spec["resume"] is True
    assert spec["units"] == spec1["units"] - sold
    assert fx.sends(e) == before


def test_resume_of_full_exit_still_gives_a_plan(tmp_path):
    e, p = _interrupted_exit(tmp_path, None)
    prop = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    assert prop.kind == "exit"
