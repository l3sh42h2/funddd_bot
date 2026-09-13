"""Фаза 1 по ревью 13.09, §5: сквозные денежные инварианты после каждого конечного состояния сценария — вход и выход
m = 1 и m = 1000, частичный выход, возврат DEX, стоп между ногами, дохедж и откат после рестарта, «продолжить» выхода.
OPEN/CLOSED: 0 ≤ токены − контракты·m < шаг·m, позиция биржи = шорт журнала, кошелёк ≥ журнала; CLOSED: шорт 0 и
токенов меньше шага·m. PAUSED/HALTED: причина названа (у паузы исполнителя — та же, что в событии paused). Продано на
DEX не больше купленного. Ни одной отправки без совпадения отпечатка намерения и сделки (шпион 1.3). Сверка «итоги
Telegram = кабинет = final_mark» — фаза 3.1 (Н5). Фейки — из test_trade_engine и шагов 0, 1.1-1.4 (без сети и ключей)."""
import json
import time
from decimal import Decimal as D

import pytest

from funding_bot.trade import engine as eng, reconcile, store
from funding_bot.trade.store import DealState

import test_phase0_hotfixes as t0
import test_phase1_exit_resume as t14
import test_phase1_frozen as t13
import test_phase1_instrument as t11
import test_phase1_units as t12
import test_trade_engine as fx

FILLED = ("DEX_OK", "PERP_SENT", "BALANCED", "HEDGE_DEFICIT")     # своп клипа исполнен


def _dex_sum(e, did, col: str, kinds: tuple[str, ...]) -> int:
    q = (f"SELECT c.{col} FROM clips c JOIN intents i ON c.intent_id=i.id WHERE i.deal_id=? AND i.kind IN "
         f"({','.join('?' * len(kinds))}) AND c.state IN ({','.join('?' * len(FILLED))})")
    return sum(int(r[0] or 0) for r in e.con.execute(q, (did, *kinds, *FILLED)))


def check_final(e, did, m: int = 1) -> str:
    """Инварианты §5 для конечного состояния сделки; возвращает состояние."""
    deal = store.get_deal(e.con, did)
    bk = eng.deal_book(e.con, did)
    st = deal["state"]
    assert bk.m == m
    if st in (DealState.OPEN, DealState.CLOSED):
        ts = fx.FILT.step * bk.m
        assert bk.known, bk.why
        d = bk.tokens(18) - bk.short * bk.m    # s = 1 (okx·bsc)
        assert d == bk.delta(18) and D(0) <= d < ts, (st, d)
        assert e.perp.pos == -bk.short, (e.perp.pos, bk.short)
        assert e.spot.bal.get(fx.TOKEN, 0) >= bk.tokens_raw
        if st == DealState.CLOSED:
            assert bk.short == 0 and bk.tokens(18) < ts
    elif st in (DealState.PAUSED, DealState.HALTED_MISMATCH):
        assert deal["reason"], st
        evs = store.events(e.con, did)
        if evs and evs[-1]["kind"] == "paused":
            assert json.loads(evs[-1]["json"])["reason"] == deal["reason"]
    assert _dex_sum(e, did, "dex_in", ("exit", "undo")) <= _dex_sum(e, did, "dex_out", ("entry",)), \
        "продано на DEX больше купленного"
    return st


def _env(tmp_path, m):
    e = fx.live_env(tmp_path, clip="50") if m == 1 else t12.mult_env(tmp_path, clip="50")
    bad, _seen = t13._spy(e)
    return e, bad


def _open(e, m, usd=200):
    p = t14._open(e, usd)
    assert check_final(e, p.deal_id, m) == DealState.OPEN
    return p


@pytest.mark.parametrize("m", [1, 1000])
def test_entry_and_full_exit(tmp_path, m):
    e, bad = _env(tmp_path, m)
    p = _open(e, m)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert check_final(e, p.deal_id, m) == DealState.CLOSED
    assert bad == []


@pytest.mark.parametrize("m", [1, 1000])
def test_partial_exit(tmp_path, m):
    e, bad = _env(tmp_path, m)
    p = _open(e, m)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER))
    assert check_final(e, p.deal_id, m) == DealState.OPEN
    assert bad == []


@pytest.mark.parametrize("m", [1, 1000])
def test_dex_refund_on_full_exit(tmp_path, m):
    """Роутер вернул 10 % каждого свопа выхода: m = 1 — остаток захеджирован (OPEN), второй выход закрывает; m = 1000 —
    остаток меньше контракта (≈ 489 токенов): CLOSED с пылью."""
    e, bad = _env(tmp_path, m)
    p = _open(e, m)
    spied = e.spot.swap
    t0._refund_last_exit_swap(e, 10)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    st = check_final(e, p.deal_id, m)
    assert st == (DealState.OPEN if m == 1 else DealState.CLOSED)
    e.spot.swap = spied
    if st == DealState.OPEN:
        fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
        assert check_final(e, p.deal_id, m) == DealState.CLOSED
    assert bad == []


@pytest.mark.parametrize("m", [1, 1000])
def test_stop_between_legs_then_resume_entry(tmp_path, m):
    e, bad = _env(tmp_path, m)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    t14._stop_after(e, 1)
    fx.run_approved(e, p)
    t14._unstop(e)
    assert check_final(e, p.deal_id, m) == DealState.PAUSED
    t14._even(e, p.deal_id, m)                 # «стоп» между ногами хедж доводит
    fx.run_approved(e, e.desk.propose_resume(p.deal_id, chat=fx.OWNER))
    assert check_final(e, p.deal_id, m) == DealState.OPEN
    assert bad == []


@pytest.mark.parametrize("m", [1, 1000])
def test_rehedge_and_undo_after_restart(tmp_path, m):
    """Голый лонг (шорт журнала и биржи меньше) → рестарт → дохедж; снова → рестарт → откат. m = 1: 200 токенов
    (≈ 8 $ > minNotional); m = 1000: 1 контракт."""
    e, bad = _env(tmp_path, m)
    p = _open(e, m)
    k = D(t13.NAKED) if m == 1 else D(1)
    for kind in ("rehedge", "undo"):
        t11._shrink_short(e, p.deal_id, k)
        reconcile.startup(e.con, e.legs, now=time.time())
        fx.run_approved(e, e.desk.propose_fix(kind, p.deal_id, chat=fx.OWNER))
        assert check_final(e, p.deal_id, m) == DealState.OPEN, kind
    assert bad == []


@pytest.mark.parametrize("m", [1, 1000])
def test_resume_of_interrupted_partial_exit(tmp_path, m):
    e, bad = _env(tmp_path, m)
    p = _open(e, m)
    x1 = t14._interrupted(e, p.deal_id, 100)
    assert check_final(e, p.deal_id, m) == DealState.PAUSED
    r = e.desk.propose_resume(p.deal_id, chat=fx.OWNER)
    fx.run_approved(e, r)
    assert check_final(e, p.deal_id, m) == DealState.OPEN
    assert t14._sold(e, x1.intent_id) + t14._sold(e, r.intent_id) == t14._spec(e, x1.intent_id)["units"]
    assert bad == []
