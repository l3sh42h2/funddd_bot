"""Фаза 1.2 по ревью 13.09: единицы по всей цепочке. m — токенов в одном контракте перпа (1000AIW3USDT: 1 контракт =
1000 AIW3). Дельта ног — в ТОКЕНАХ (токены − контракты·m), инвариант [0, шаг·m); заявки — в КОНТРАКТАХ (SELL
floor(δ/m), BUY ceil(−δ/m)); цена токена — цена контракта / m. Контракты с m ≠ 1 — только с ключом владельца
[perp.aster] allow_contract_multiplier = true (свежий owner.toml у кнопки для любой продажи перпа); выход, откат и дохедж
покупкой — без ключа. Для m = 1 всё побайтно прежнее (DQA9Q — регрессия). Фейки — из test_trade_engine: перп
1000AIW3USDT со стаканом ×1000 и моделью маржи Aster (−2019). Без сети и ключей."""
import json, re, time
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from funding_bot.trade import engine as eng, marks, owner, planner as P, reconcile, report as R, store
from funding_bot.trade.engine import Conns, Desk, Engine, Legs
from funding_bot.trade.sim import SimPerp, SimSpot
from funding_bot.trade.store import DealState, IntentStatus
from funding_bot.trade.types import Book, PerpFill, PerpInstrument
from funding_bot.tg import views

import test_phase0_hotfixes as t0
import test_phase1_instrument as t11
import test_trade_engine as fx
import test_trade_planner as tp

M = 1000
MSYM = "1000AIW3USDT"
MTABLE = dict(fx.TABLE, sf_rows=[dict(fx.TABLE["sf_rows"][0], perp=MSYM)])
KEY = "allow_contract_multiplier"
flat = fx.flat


def mult_book(b: Book | None = None) -> Book:
    """Стакан контракта ×1000: цена за контракт, объём — в контрактах."""
    b = b or fx.make_book()
    return Book(tuple((p * M, q) for p, q in b.bids), tuple((p * M, q) for p, q in b.asks), 0.0)


def with_key(toml: str, value: str = "true") -> str:
    """owner.toml с ключом владельца в [perp.aster] (строкой после liq_alert_pct)."""
    return re.sub(r"(?m)^(liq_alert_pct = .*)$", rf"\1\n{KEY} = {value}", toml, count=1)


class MultPerp(fx.LivePerp):
    """Перп 1000AIW3USDT: 1 контракт = 1000 AIW3, стакан ×1000; маржа по модели Aster: SELL (не reduceOnly) сверх
    доступного — REJECTED −2019 (заявка ушла, биржа отказала)."""

    def __init__(self, env, margin: D | None = None):
        super().__init__(env)
        self.b = mult_book()
        self.margin = margin                   # None — без модели маржи
        self.rejected = 0

    def funding(self, s):
        return fx.PX * M, D("0.0005"), 1_757_700_000_000

    def available_margin(self):
        return D(1000) if self.margin is None else self.margin

    def instrument(self, s):
        return PerpInstrument(s, "1000AIW3", "AIW3", D(M), "USDT", "PERPETUAL")

    def ioc(self, symbol, side, qty, px_cap, client_id, reduce_only, *, hedge=False, on_signed=None):
        if self.margin is not None and side == "SELL" and not reduce_only and qty * px_cap > self.margin:
            self.calls.append(dict(cid=client_id, side=side, qty=qty, cap=px_cap, ro=reduce_only, hedge=hedge,
                                   paused=False))
            if on_signed is not None:
                on_signed(len(self.calls))
            self.rejected += 1
            self.last_error = "code -2019 Margin is insufficient"
            f = PerpFill(client_id, None, "REJECTED", D(0), D(0), D(0), len(self.calls), -2019)
            self.orders[client_id] = f
            return f
        f = super().ioc(symbol, side, qty, px_cap, client_id, reduce_only, hedge=hedge, on_signed=on_signed)
        if self.margin is not None and side == "SELL" and f.qty:
            self.margin -= f.quote
        return f


def mult_env(tmp_path, *, key: bool = True, margin: D | None = None, clip: str = '"auto"'):
    e = fx.live_env(tmp_path, clip=clip)
    toml = fx.live_toml(clip)
    e.path.write_text(with_key(toml) if key else toml)
    e.perp = MultPerp(e, margin)
    e.legs_live = Legs(e.spot, e.perp, False, lambda: D(600), can_send=True)    # e.legs читает e.legs_live
    e.desk.table_loader = lambda: MTABLE
    return e


def set_key(e, on: bool) -> None:
    """Владелец правит owner.toml: ключ есть или его нет (свежее чтение у кнопки)."""
    toml = fx.live_toml()
    e.path.write_text(with_key(toml) if on else toml)


def enter(e, usd=100):
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(usd), chat=fx.OWNER)
    fx.run_approved(e, p)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    return p


def book(e, did):
    return eng.deal_book(e.con, did)


def assert_even(e, did):
    """Денежный инвариант m = 1000: 0 ≤ токены − контракты·m < шаг·m; позиция биржи = шорт журнала (контракты)."""
    bk = book(e, did)
    assert bk.known and bk.m == M
    assert D(0) <= bk.delta(18) < fx.FILT.step * M, bk.delta(18)
    assert bk.delta(18) == bk.tokens(18) - bk.short * M
    assert e.perp.pos == -bk.short
    return bk


def sells(e) -> D:
    return sum((c["qty"] for c in e.perp.calls if c["side"] == "SELL"), D(0))


# ==== 1. С1: вход $100 в 1000AIW3 с ключом — шорт в контрактах ======================================================
def test_entry_m1000_with_key_hedges_in_contracts(tmp_path):
    e = mult_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert sum((q for c in p.plan.clips for q, _ in c.children), D(0)) == 2, "дочерние — контракты, не 2 444 токена"
    assert p.plan.est["contracts"] == 2 and p.plan.est["units_per_contract"] == M
    assert abs(p.plan.est["basis_bps"]) < 100, "базис по цене контракта / m, а не 9 988 565 б.п."
    fx.run_approved(e, p)
    bk = assert_even(e, p.deal_id)
    assert bk.short == eng.floor_step(bk.tokens(18) / M, fx.FILT.step) == 2
    assert e.perp.pos == -2 and sells(e) == 2
    assert t11._deal_inst(e.con, p.deal_id).m == M
    chk = reconcile.check_deal(e.con, store.get_deal(e.con, p.deal_id), e.legs_live)
    assert chk.matched is True and chk.hedged is True and chk.m == M
    assert "шорт 2 контр. (= 2 000 токенов) — как в журнале" in flat(chk.detail)
    fin = flat(e.hooks.reports[-1])
    assert "Спот +2 445 · шорт Aster −2 контр. (= −2 000 токенов)" in fin, fin
    assert "Без хеджа" not in fin, "δ ≈ 445 токенов < шага·m = 1000 — ноги ровно"
    kurs = D(re.search(r"курсовой ([+−][\d.]+) %", fin).group(1).replace("−", "-"))
    assert abs(kurs) < 1, fin
    for c in store.clips_of(e.con, p.intent_id):             # базис клипа в журнале — по цене токена
        assert c["basis_bps"] is not None and abs(c["basis_bps"]) < 100
    assert fx.html_ok(fin) and not fx.RAW_NUM.search(e.hooks.reports[-1])


def test_two_clip_entry_m1000_progress_and_carry(tmp_path):
    """Два клипа по $50: клип — 1.22 контракта → SELL 1 и перенос 222 токенов; второй — SELL 1 (1 444 → 444)."""
    e = mult_env(tmp_path, clip="60")
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert len(p.plan.clips) == 2 and [sum(q for q, _ in c.children) for c in p.plan.clips] == [1, 1]
    fx.run_approved(e, p)
    assert_even(e, p.deal_id)
    assert [c["qty"] for c in e.perp.calls] == [1, 1]
    cl = store.clips_of(e.con, p.intent_id)
    assert D(0) <= D(cl[0]["carry_out"]) < M and D(0) <= D(cl[1]["carry_out"]) < M
    pr = flat(e.hooks.progresses[0][1])
    assert "шорт −1 контр. (= −1 000 токенов) ✓" in pr and "Без хеджа" not in pr, pr


# ==== 2. без ключа — прежний отказ, текст называет ключ ================================================================
@pytest.mark.parametrize("value", [None, "false"])
def test_entry_m1000_without_key_is_refused_before_any_send(tmp_path, value):
    e = mult_env(tmp_path, key=False)
    if value is not None:
        e.path.write_text(with_key(fx.live_toml(), value))
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert KEY in ei.value.html and "1 контракт = 1 токен" in ei.value.html
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []
    assert e.con.execute("SELECT count(*) FROM deals").fetchone()[0] == 0


# ==== 3. чужая база — отказ и с ключом ====================================================================================
@pytest.mark.parametrize("how", ["exchange_base", "table_symbol"])
def test_other_asset_is_refused_even_with_key(tmp_path, how):
    e = mult_env(tmp_path)
    if how == "exchange_base":
        e.perp.instrument = lambda s: PerpInstrument(s, "1000BTC", "BTC", D(M), "USDT", "PERPETUAL")
    else:
        e.desk.table_loader = lambda: dict(fx.TABLE, sf_rows=[dict(fx.TABLE["sf_rows"][0], perp="1000BTCUSDT")])
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert "другой актив" in ei.value.html
    assert fx.sends(e) == (0, 0)


# ==== 4. маржа: заявки в контрактах × цена контракта ===================================================================
def test_margin_check_refuses_before_swap_and_passes_when_enough(tmp_path):
    (tmp_path / "a").mkdir()
    e = mult_env(tmp_path / "a", margin=D(50))
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    assert "Маржа Aster 50.00 — меньше 100.00 USDT" in flat(ei.value.html)
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []
    (tmp_path / "b").mkdir()
    ok = mult_env(tmp_path / "b", margin=D(100))
    p = enter(ok)
    assert ok.perp.rejected == 0 and sells(ok) == 2, "маржи 100 $ хватило на 2 контракта (~82 $)"
    assert_even(ok, p.deal_id)


def test_margin_check_catches_a_plan_in_mixed_units(tmp_path):
    """Контроль смешения: план, построенный как «1 контракт = 1 токен» на стакане ×1000, просит ≈ 99 660 $ маржи —
    предпроверка это видит (Σ кол-во · кэп дочерних), даже если маржи хватает на сумму ноги."""
    e = mult_env(tmp_path)
    plan, ctx = e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(100), sim=False)
    assert ctx["notes"] == []
    mixed = P.plan(deal_id="", kind="entry", coin="AIW3", spot="okx·bsc", perp="aster", symbol=MSYM, leg_usd=D(100),
                   total_in_units=100 * fx.E18, dec_in=18, calib=eng._calib_from(plan.inputs["calib"]),
                   mkt=ctx["mkt"], lim=P.limits_from_owner(ctx["cfg"], "aster", "bsc"), now=time.time())
    need = sum((q * cap for c in mixed.clips for q, cap in c.children), D(0))
    assert need > 90_000
    notes = e.desk._entry_live_notes(ctx["cfg"], e.legs_live, ctx["pair"], ctx["bal"], ctx["total"], D(100),
                                     ctx["sdec"], mixed)
    assert any(n.startswith("маржа Aster 1 000.00 — меньше") for n in map(flat, notes)), notes
    assert e.desk._entry_live_notes(ctx["cfg"], e.legs_live, ctx["pair"], ctx["bal"], ctx["total"], D(100),
                                    ctx["sdec"], plan) == []


# ==== 5-6. выходы m = 1000 ===============================================================================================
def test_partial_exit_m1000_buys_contracts_and_stays_even(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    tok0 = book(e, p.deal_id).tokens(18)
    x = e.desk.propose_exit(p.deal_id, D(50), False, chat=fx.OWNER)
    assert "Продать спот 1 222 · откупить шорт 1 контр. (= 1 000 токенов)" in flat(x.html), flat(x.html)
    n0 = len(e.perp.calls)
    fx.run_approved(e, x)
    buys = e.perp.calls[n0:]
    assert [(c["side"], c["qty"], c["ro"]) for c in buys] == [("BUY", 1, True)]
    bk = assert_even(e, p.deal_id)
    sold = tok0 - bk.tokens(18)
    assert abs(sold - D("1222.3")) < 2 and e.perp.pos == -1
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    fin = flat(e.hooks.reports[-1])
    assert "открыт и захеджирован ✓" in fin and "Без хеджа" not in fin, fin


def test_full_exit_m1000_closes(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    assert "откупить шорт 2 контр. (= 2 000 токенов)" in flat(x.html)
    fx.run_approved(e, x)
    bk = book(e, p.deal_id)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED
    assert bk.short == 0 and bk.tokens_raw == 0 and e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0
    assert "закрыта" in e.hooks.reports[-1]


def test_full_exit_without_key_is_allowed(tmp_path):
    """Выход таких сделок — всегда (R8): ключ снят после входа, «выход» доводит до CLOSED."""
    e = mult_env(tmp_path)
    p = enter(e)
    set_key(e, False)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED and e.perp.pos == 0


# ==== 7. возврат DEX на полном выходе m = 1000 ===========================================================================
def test_dex_refund_on_full_exit_m1000_leaves_hedged_residual(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e, 500)
    assert book(e, p.deal_id).short == 12
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    t0._refund_last_exit_swap(e, 10)
    fx.run_approved(e, x)
    bk = assert_even(e, p.deal_id)
    left = bk.tokens(18)
    assert left >= M and bk.short == eng.floor_step(left / M, fx.FILT.step) == 1
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    ev = [ev for ev in store.events(e.con, p.deal_id) if ev["kind"] == "exit_residual"]
    assert len(ev) == 1 and D(str(json.loads(ev[0]["json"])["m"])) == M
    e.spot.swap = fx.LiveSpot.swap.__get__(e.spot)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED and e.perp.pos == 0


def test_dex_refund_below_one_contract_closes_with_dust(tmp_path):
    """Возврат 3 %: остаток ≈ 366 токенов < шага·m — меньше одного контракта не хеджируется: весь шорт откуплен,
    CLOSED, пыль — в dust (штатно, в пределах инварианта)."""
    e = mult_env(tmp_path)
    p = enter(e, 500)
    x = e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER)
    t0._refund_last_exit_swap(e, 3)
    fx.run_approved(e, x)
    bk = book(e, p.deal_id)
    d = store.get_deal(e.con, p.deal_id)
    assert d["state"] == DealState.CLOSED and bk.short == 0 and e.perp.pos == 0
    assert D(0) < bk.tokens(18) < M and D(d["dust"]) == bk.tokens(18)
    assert not any(ev["kind"] == "exit_residual" for ev in store.events(e.con, p.deal_id))


# ==== 8. дохедж и откат m = 1000 ==========================================================================================
def _short_by(e, did, k):
    """Шорт журнала и позиция биржи меньше на k контрактов (k < 0 — больше): голый лонг / шорт k·m токенов."""
    t11._shrink_short(e, did, D(k))


def test_rehedge_and_undo_m1000(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    did = p.deal_id
    # δ ≈ 444.6 токена < шага·m: ноги ровно — дохеджировать и откатывать нечего, выход разрешён
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_fix("rehedge", did, chat=fx.OWNER)
    assert "Ноги ровно (дельта +445 меньше шага)" in ei.value.html
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_fix("undo", did, chat=fx.OWNER)
    assert "только для голого лонга" in ei.value.html
    e.desk.propose_exit(did, None, False, chat=fx.OWNER)
    # шорт 1 контракт: δ ≈ 1 444.6 — дохедж продаёт 1 КОНТРАКТ (а не 1 444)
    _short_by(e, did, 1)
    fix = e.desk.propose_fix("rehedge", did, chat=fx.OWNER)
    sp = t11._spec_of(e.con, fix.intent_id)
    assert (sp["side"], D(sp["qty"])) == ("SELL", D(1))
    t = flat(fix.html)
    assert "Без хеджа +1 445 AIW3 ≈ 59.10 $ спота" in t and "Продать 1 контр. (= 1 000 токенов) на перпе Aster" in t, t
    n0 = len(e.perp.calls)
    fx.run_approved(e, fix)
    assert [(c["side"], c["qty"]) for c in e.perp.calls[n0:]] == [("SELL", 1)]
    assert_even(e, did)
    assert "ноги ровно ✓" in e.hooks.reports[-1] and "Продано 1 контр. (= 1 000 токенов)" in flat(e.hooks.reports[-1])
    # снова голый лонг 1 000 токенов: откат продаёт 1 000 токенов на DEX (кратно шагу·m), перп не трогает
    _short_by(e, did, 1)
    und = e.desk.propose_fix("undo", did, chat=fx.OWNER)
    assert t11._spec_of(e.con, und.intent_id)["units"] == M * fx.E18
    assert "Продать 1 000 AIW3 на DEX" in flat(und.html)
    tok0, n0 = book(e, did).tokens(18), len(e.perp.calls)
    fx.run_approved(e, und)
    assert tok0 - book(e, did).tokens(18) == M and len(e.perp.calls) == n0
    assert_even(e, did)
    # голый шорт: шорт 3 при ~1 444.6 токенах (δ ≈ −1 555.4) — дохедж откупает ceil(1.5554) = 2 контракта (вверх к
    # шагу, не больше шорта), δ снова ≈ 444.6
    _short_by(e, did, -2)
    buy = e.desk.propose_fix("rehedge", did, chat=fx.OWNER)
    assert (t11._spec_of(e.con, buy.intent_id)["side"], D(t11._spec_of(e.con, buy.intent_id)["qty"])) == ("BUY", 2)
    fx.run_approved(e, buy)
    assert_even(e, did)


# ==== R8: продажа перпа с множителем — по СВЕЖЕМУ ключу владельца =========================================================
def test_sell_needs_fresh_key_buy_and_exit_do_not(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    did = p.deal_id
    _short_by(e, did, 1)                                     # голый лонг: дохедж продажей
    fix = e.desk.propose_fix("rehedge", did, chat=fx.OWNER)  # план — с ключом
    set_key(e, False)                                        # владелец снял разрешение до кнопки
    before = fx.sends(e)
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_fix("rehedge", did, chat=fx.OWNER)
    assert KEY in ei.value.html
    fx.run_approved(e, fix)
    assert fx.sends(e) == before, "одобренная раньше продажа контрактов без ключа не отправлена"
    assert store.get_intent(e.con, fix.intent_id)["status"] == IntentStatus.FAILED
    d = store.get_deal(e.con, did)
    assert d["state"] == DealState.PAUSED and d["reason"] == "limit"
    assert KEY in e.hooks.reports[-1]
    # откат и дохедж покупкой — без ключа
    e.desk.propose_fix("undo", did, chat=fx.OWNER)
    _short_by(e, did, -2)
    fx.run_approved(e, e.desk.propose_fix("rehedge", did, chat=fx.OWNER))
    assert_even(e, did)


def test_key_removed_after_entry_plan_pauses_before_any_send(tmp_path):
    e = mult_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
    set_key(e, False)
    fx.run_approved(e, p)
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.ABORTED
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.FAILED
    assert KEY in e.hooks.reports[-1] and "не начат" in e.hooks.reports[-1]


# ==== 9. инвариант ног m = 1000 ===========================================================================================
def test_invariant_m1000(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    run = SimpleNamespace(did=p.deal_id, dec=18, f=fx.FILT, legs=e.legs_live, symbol=MSYM)
    bk = e.engine._invariant(run)                            # δ ≈ 444.6 < 1000 и позиция −2 = журнал: без паузы
    assert bk.short == 2
    _short_by(e, p.deal_id, -1)                              # шорт 3: δ ≈ −555.4 — голый шорт
    with pytest.raises(eng.Pause) as pi:
        e.engine._invariant(run)
    assert pi.value.reason == "hedge_deficit" and "шаг 1 000" in flat(pi.value.text)
    _short_by(e, p.deal_id, 2)                               # шорт 1: δ ≈ 1 444.6 — голый лонг больше шага·m
    with pytest.raises(eng.Pause):
        e.engine._invariant(run)
    _short_by(e, p.deal_id, -1)
    e.perp.pos = D(-3)                                       # биржа ≠ журнал (контракты)
    with pytest.raises(eng.Pause) as pi:
        e.engine._invariant(run)
    assert pi.value.reason == "position_mismatch"
    assert "−3 контр. (= −3 000 токенов) ≠ журнал сделки −2 контр. (= −2 000 токенов)" in flat(pi.value.text)


def test_halt_naked_leg_m1000_in_tokens_and_dollars_per_token(tmp_path, monkeypatch):
    """Пауза: голая нога — в токенах, $ — по цене контракта / m; меньше шага·m — ноги ровно, авто-отката нет."""
    cfg = SimpleNamespace(get=lambda k: D(600) if k == "exec.auto_unwind_naked_after_s" else None)
    for sub, delta, naked in (("naked", D(1500), True), ("even", D(500), False)):
        (tmp_path / sub).mkdir()
        e = mult_env(tmp_path / sub)
        p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(100), chat=fx.OWNER)
        assert store.approve_intent(e.con, p.intent_id, p.nonce)
        store.set_intent_status(e.con, p.intent_id, IntentStatus.RUNNING, expect=IntentStatus.APPROVED)
        store.set_deal_state(e.con, p.deal_id, DealState.ENTERING)
        bk = SimpleNamespace(known=True, tokens_raw=3500 * fx.E18, short=D(2), delta=lambda dec, d=delta: d,
                             tokens=lambda dec: D(3500), m=D(M))
        run = SimpleNamespace(did=p.deal_id, iid=p.intent_id, kind="entry", dec=18, f=fx.FILT, legs=e.legs_live,
                              symbol=MSYM, token=fx.TOKEN, cfg=cfg, deal=store.get_deal(e.con, p.deal_id), seq=1,
                              n_total=1)
        with monkeypatch.context() as mp:
            mp.setattr(eng, "deal_book", lambda con, did: bk)
            e.engine._paused(run, eng.Pause("hedge_deficit", "тест"))
        h = flat(e.hooks.reports[-1])
        if naked:
            assert "без хеджа 1 500 ≈ 61.36 $ спота" in h and "откачу сам" in h, h      # 1 500 × 40.91 / 1000
            assert "шорт 0" in h or "шорт −" in h
            assert p.deal_id in e.engine._unwind_due
        else:
            assert "Ноги ровно ✓" in h and "откачу" not in h and p.deal_id not in e.engine._unwind_due, h


# ==== «выход перп» и откат m = 1000 =======================================================================================
def test_perp_only_exit_then_undo_m1000(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    x = e.desk.propose_exit(p.deal_id, None, True, chat=fx.OWNER)
    assert "Откупить шорт 2 контр. (= 2 000 токенов) на Aster" in flat(x.html)
    fx.run_approved(e, x)
    assert e.perp.pos == 0 and store.get_deal(e.con, p.deal_id)["state"] == DealState.PAUSED
    t = flat(e.hooks.reports[-1])
    assert "Откуплено 2 контр. (= 2 000 токенов) = 81.84 $" in t and "Спот 2 445 AIW3 ≈ 100 $" in t, t   # × 40.92/m
    und = e.desk.propose_fix("undo", p.deal_id, chat=fx.OWNER)
    fx.run_approved(e, und)
    bk = book(e, p.deal_id)
    assert bk.short == 0 and D(0) <= bk.tokens(18) < M                    # откат — кратно шагу·m
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED


# ==== каждый план знает m: вход, перекотировка у кнопки, остаток клипов, выход ============================================
def test_every_plan_gets_units_per_contract(tmp_path, monkeypatch):
    e = mult_env(tmp_path, clip="60")
    seen = []
    real = P.plan

    def spy(**kw):
        seen.append((kw["kind"], kw.get("units_per_contract")))
        return real(**kw)
    monkeypatch.setattr(P, "plan", spy)
    p = enter(e)
    fx.run_approved(e, e.desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    kinds = [k for k, _ in seen]
    assert kinds.count("entry") >= 3 and "exit" in kinds, seen      # план, у кнопки, остаток после клипа 1
    assert all(m == M for _, m in seen), seen


# ==== 10. планировщик — чистая функция ====================================================================================
def _mkt_m(**over):
    return tp._mkt(book=mult_book(tp.BOOK), **over)


def _plan_m(kind, units, m=D(M), book_m=True):
    return P.plan(deal_id="E7K2", kind=kind, coin="AIW3", spot="okx·bsc", perp="aster", symbol=MSYM,
                  leg_usd=D(100), total_in_units=units, dec_in=18, calib=tp._cal(kind),
                  mkt=_mkt_m() if book_m else tp._mkt(), lim=P.Limits(clips_max=10, clip_max_usd="auto"), r=D(0),
                  now=1000.0, units_per_contract=m)


def test_planner_m1000_children_in_contracts_and_basis_per_token():
    ent = _plan_m("entry", 100 * fx.E18)
    assert sum((q for c in ent.clips for q, _ in c.children), D(0)) == 2 == ent.est["contracts"]
    assert ent.est["units_per_contract"] == M and D(2400) < ent.est["tokens"] < D(2500)
    assert abs(ent.est["basis_bps"]) < 100
    ext = _plan_m("exit", int(D("2444.6") * fx.E18))
    assert sum((q for c in ext.clips for q, _ in c.children), D(0)) == 2 == ext.est["contracts"], "вниз к шагу"
    for bad in (D(0), D(-1000), D("NaN")):
        with pytest.raises(ValueError):
            _plan_m("entry", 100 * fx.E18, m=bad)


def test_planner_m1_is_the_same_plan():
    a = tp._plan("entry", usd=500)
    b = P.plan(deal_id="E7K2", kind="entry", coin="AIW3", spot="okx·bsc", perp="aster", symbol="AIW3USDT",
               leg_usd=D(500), total_in_units=500 * fx.E18, dec_in=18, calib=tp._cal("entry"), mkt=tp._mkt(),
               lim=P.Limits(clips_max=10, clip_max_usd="auto"), r=D(0), now=1000.0, units_per_contract=D(1))
    assert store.jdump(a) == store.jdump(b)
    assert a.est["basis_bps"] == b.est["basis_bps"] and a.est["contracts"] == sum(
        (q for c in a.clips for q, _ in c.children), D(0))


# ==== 11. отчёты: дисбаланс, пыль и дельта — в токенах =====================================================================
def test_report_numbers_in_tokens_for_m1000():
    clips = [{"dex_in": 100 * fx.E18, "dex_out": int(D("2444.6") * fx.E18), "perp_qty": "2", "perp_quote": "81.8"}]
    pn = R.progress_numbers(kind="entry", clips=clips, dec_token=18, dec_stable=18, n_planned=1, m=D(M))
    assert pn["imbalance"] == D("444.6") and pn["perp_avg_px"] == D("40.9")
    assert abs(pn["imbalance_usd"] - D("444.6") * 100 / D("2444.6")) < D("0.0001")
    fills = [{"qty": D(2), "quote_qty": D("81.8"), "price": D("40.9"), "commission_abs": D("0.03"),
              "commission_asset": "USDT", "maker": 0, "realized_pnl": None}]
    fn = R.final_numbers(kind="entry", clips=clips, txs=(), fills=fills, dec_token=18, dec_stable=18, native_px=None,
                         ref_px=None, perp_mid_ref=None, m=D(M))
    assert fn["dust"] == D("444.6") and abs(fn["basis_bps"]) < 5       # 0.0409 / 0.040907 − 1 ≈ −1.8 б.п.
    ps = R.positions_numbers(spot_units=int(D("2444.6") * fx.E18), dec_token=18, spot_px=D("0.0409"),
                             position_amt=D(-2), mark=D("40.9"), unrealized=None, income_rows=(), created=0.0,
                             now=3600.0, book_tokens=D("2444.6"), book_short=D(2), m=D(M))
    assert ps["delta"] == D("444.6") and ps["short_usd"] == D("81.8") and ps["matches"] is True
    assert ps["delta_usd"] == D("444.6") * D("0.0409")
    # m = 1 — то же, что без параметра
    for fn_, kw in ((R.progress_numbers, dict(kind="entry", clips=clips, dec_token=18, dec_stable=18, n_planned=1)),
                    (R.final_numbers, dict(kind="entry", clips=clips, txs=(), fills=fills, dec_token=18, dec_stable=18,
                                           native_px=None, ref_px=None, perp_mid_ref=None)),
                    (R.positions_numbers, dict(spot_units=10 ** 18, dec_token=18, spot_px=D(1), position_amt=D(-1),
                                               mark=D(1), unrealized=None, income_rows=(), created=0.0, now=1.0))):
        assert fn_(**kw) == fn_(**kw, m=D(1))


# ==== 12. тексты: m ≠ 1 — «N контр. (= N·m токенов)», m = 1 — прежние ======================================================
def test_views_contracts_and_m1_texts_unchanged():
    assert flat(views.contracts(D(2), D(1000))) == "2 контр. (= 2 000 токенов)"
    assert flat(views.contracts(D(-2), D(1000), True)) == "−2 контр. (= −2 000 токенов)"
    assert views.contracts(D(4902), D(1), coin="AIW3") == views.tok(D(4902)) + " AIW3"
    assert views.contracts(D(4902), None, True, D(1)) == views.tok(D(4902), True, D(1))
    assert views.contracts(None, D(1000)) == views.DASH
    # m = 1 и m не задан — одни и те же строки (побайтно; прежние тексты закреплены test_tg)
    for d in (D(5), D(-3), D("0.151"), None):
        for st in (D(1), D("0.001"), None):
            assert views._naked_cmds("D1", d, st) == views._naked_cmds("D1", d, st, D(1))
            assert views.restart_check("AIW3", "D1", True, "x", "PAUSED", hedged=False, delta=d, step=st) == \
                views.restart_check("AIW3", "D1", True, "x", "PAUSED", hedged=False, delta=d, step=st, m=D(1))
    for cls, fn, kw in (
            (views.FinalView, views.final, dict(intent_id="E1", kind="entry", coin="AIW3", chain="bsc",
                                                 perp_venue="aster", deal_id="D1", leg_usd=D(200), spot_qty=D("4902.2"),
                                                 perp_qty=D(4902), dust_qty=D("0.2"), step=D(1))),
            (views.HaltView, views.halt, dict(intent_id="E1", kind="entry", coin="AIW3", reason="r", deal_id="D1",
                                               perp_pos=D(-10), wallet_tokens=D(15), unhedged_qty=D(5), step=D(1))),
            (views.ProgressView, views.progress, dict(intent_id="E1", kind="exit", coin="AIW3", clip=1, clips=2,
                                                       spot_qty=D(10), perp_qty=D(9), imbalance_qty=D(1), step=D(1))),
            (views.FixPlanView, views.fix_plan, dict(intent_id="H1", kind="rehedge", coin="AIW3", deal_id="D1",
                                                      delta=D(-5), qty=D(5), side="BUY", step=D(1))),
            (views.FixDoneView, views.fix_done, dict(kind="rehedge", coin="AIW3", deal_id="D1", state="OPEN",
                                                      qty=D(5), usd=D("0.2"), side="BUY", delta=D(2), step=D(1))),
            (views.PerpClosedView, views.perp_closed, dict(coin="AIW3", deal_id="D1", qty=D(5), usd=D("0.2"),
                                                            spot_qty=D(5), step=D(1))),
            (views.RestartView, views.restart, dict(intent_id="E1", kind="entry", matched=True, coin="AIW3",
                                                     hedged=False, delta=D(-5), step=D(1)))):
        assert fn(cls(**kw)) == fn(cls(**kw, m=D(1))), cls.__name__
    # m = 1000: голая нога и команды — в токенах, контракты — «контр.»
    cm = [flat(x) for x in views._naked_cmds("D1", D("-555.4"), D(1), D(M))]
    assert cm[0] == "<code>дохедж D1</code> — откупить 1 контр. (= 1 000 токенов) на перпе"
    cm = [flat(x) for x in views._naked_cmds("D1", D("1444.6"), D(1), D(M))]
    assert cm[1] == "<code>откат D1</code> — продать 1 000 на DEX"
    rc = flat(views.restart_check("AIW3", "D1", True, "x", "PAUSED", hedged=False, delta=D("1444.6"), step=D(1),
                                  m=D(M)))
    assert "Без хеджа +1 445 AIW3" in rc and "продать 1 000 на DEX" in rc
    assert views.key_label(f"perp.aster.{KEY}") == "контракты с множителем Aster"


# ==== 13. симуляция m = 1000 ==============================================================================================
class MultPub(fx.PerpPub):
    """Публичная нога 1000AIW3USDT под SimPerp (стакан ×1000)."""

    def __init__(self):
        super().__init__()
        self.b = mult_book()

    def funding(self, s):
        return fx.PX * M, D("0.0005"), 1_757_700_000_000

    def instrument(self, s):
        return PerpInstrument(s, "1000AIW3", "AIW3", D(M), "USDT", "PERPETUAL")


def test_sim_entry_and_exit_m1000(tmp_path):
    p_ = tmp_path / "owner.toml"
    p_.write_text(with_key(fx.EXAMPLE))
    loader = lambda: owner.load(p_)                          # noqa: E731
    conns = Conns(tmp_path / "trade.db")
    legs_sim = Legs(SimSpot(fx.SpotRO(fx.Market()), native_px=lambda: D(600)), SimPerp(MultPub()), True,
                    lambda: D(600))
    legs = lambda sim: legs_sim if sim else None             # noqa: E731
    desk = Desk(conns, legs, owner_loader=loader, table_loader=lambda: MTABLE)
    engine = Engine(conns, legs, desk, fx.RecHooks(), owner_loader=loader, sleep=lambda s: None, clip_gap_s=0)
    e = SimpleNamespace(con=conns.get(), engine=engine)
    p = desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=fx.OWNER)
    fx.run_approved(e, p)
    bk = eng.deal_book(e.con, p.deal_id)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN and bk.m == M
    assert bk.short == 12 and D(0) <= bk.delta(18) < M and legs_sim.perp.pos[MSYM] == -12
    fx.run_approved(e, desk.propose_exit(p.deal_id, None, False, chat=fx.OWNER))
    bk = eng.deal_book(e.con, p.deal_id)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED and bk.short == 0 and bk.tokens_raw == 0
    assert legs_sim.perp.pos[MSYM] == 0


# ==== сверка, «позиции», перезапуск и marks m = 1000 ==========================================================================
def test_positions_restart_and_marks_m1000(tmp_path):
    e = mult_env(tmp_path)
    p = enter(e)
    rows, matched, problems = reconcile.positions(e.con, e.legs, now=time.time())
    r = rows[0]
    assert matched is True and problems is None
    assert r["perp_qty"] == -2 and r["step"] == M and D(444) < r["delta_qty"] < D(445)
    txt = flat(views.positions([views.PositionView(**{k: v for k, v in r.items()
                                                      if k in views.PositionView.__dataclass_fields__})],
                               ts=time.time(), matched=matched))
    assert "без хеджа" not in txt, txt
    m = marks.mark_deal(e.con, store.get_deal(e.con, p.deal_id), e.legs_live, now=time.time())
    assert m.flags.get("m") == M and m.pnl_now is not None and abs(m.pnl_now) < 2, (m.pnl_now, m.flags)
    # перезапуск с голым лонгом 1 000 токенов: сверка сходится, ноги не ровно, дельта — в токенах
    _short_by(e, p.deal_id, 1)
    rep = reconcile.startup(e.con, e.legs, now=time.time())
    chk = rep.deals[0].check
    assert chk.matched is True and chk.hedged is False and chk.m == M and D(1444) < chk.delta < D(1445)
    assert "голый лонг +1 445 AIW3" in flat(chk.detail)
    closed = [d for d in (store.get_deal(e.con, p.deal_id),) if d["state"] == DealState.CLOSED]
    assert closed == []


def test_restart_exiting_closes_only_below_one_contract(tmp_path):
    """Перезапуск посреди выхода: CLOSED — только если шорт 0 и токенов меньше шага·m."""
    e = mult_env(tmp_path)
    p = enter(e)
    bk = book(e, p.deal_id)
    chk = reconcile.DealCheck(p.deal_id, True, "", bk)
    one = SimpleNamespace(**{**vars(bk), "short": D(0)})
    one.tokens, one.delta, one.tstep, one.m = (lambda dec: D(999)), (lambda dec: D(999)), bk.tstep, bk.m
    one.tokens_raw = 999 * fx.E18
    assert reconcile._decide(DealState.EXITING, reconcile.DealCheck(p.deal_id, True, "", one), False, 18,
                             fx.FILT.step)[0] == DealState.CLOSED
    one.tokens = lambda dec: D(1000)
    assert reconcile._decide(DealState.EXITING, reconcile.DealCheck(p.deal_id, True, "", one), False, 18,
                             fx.FILT.step)[0] == DealState.PAUSED
    assert chk.m == M


# ==== регрессия m = 1: DQA9Q — тексты и числа прежние ======================================================================
def test_dqa9q_m1_texts_and_numbers_unchanged(tmp_path):
    e = t11._dqa9q_env(tmp_path)
    reconcile.startup(e.con, e.legs, now=t11.tm.NOW)
    deal = store.get_deal(e.con, "DQA9Q")
    chk = reconcile.check_deal(e.con, deal, e.legs_live)
    assert chk.m == 1 and chk.detail == "кошелёк 4 902 AIW3, шорт 4 902 — как в журнале".replace(" 9", views.NBSP + "9")
    rows, _m, _p = reconcile.positions(e.con, e.legs, now=time.time())
    assert rows[0]["step"] == fx.FILT.step and rows[0]["delta_qty"] == D("0.151")
    mk = marks.mark_deal(e.con, deal, e.legs_live, now=time.time())
    assert "m" not in mk.flags, "флаги m = 1 прежние"
    x = e.desk.propose_exit("DQA9Q", None, False, chat=fx.OWNER)
    assert "Продать спот 4 902 · откупить шорт 4 902" in flat(x.html)
    assert "контр." not in x.html
    fx.run_approved(e, x)
    assert store.get_deal(e.con, "DQA9Q")["state"] == DealState.CLOSED and "контр." not in e.hooks.reports[-1]


# ==== ключ владельца: owner.toml ============================================================================================
def test_owner_key_allow_contract_multiplier(tmp_path):
    p = tmp_path / "owner.toml"
    p.write_text(fx.EXAMPLE)
    cfg = owner.load(p)
    assert cfg.get(f"perp.aster.{KEY}") is None and not eng._allow_multiplier(cfg)
    assert f"perp.aster.{KEY}" not in cfg.live_missing("aster"), "пусто = запрещено, live его не требует"
    assert f"# {KEY} = true" in fx.EXAMPLE, "в примере — закомментирован, не включён"
    p.write_text(with_key(fx.EXAMPLE))
    assert owner.load(p).get(f"perp.aster.{KEY}") is True and eng._allow_multiplier(owner.load(p))
    p.write_text(with_key(fx.EXAMPLE, "false"))
    assert owner.load(p).get(f"perp.aster.{KEY}") is False and not eng._allow_multiplier(owner.load(p))
    for bad in ("1", '"true"'):
        p.write_text(with_key(fx.EXAMPLE, bad))
        with pytest.raises(owner.OwnerConfigError):
            owner.load(p)
    # замороженная копия с ключом читается обратно (перекотировка у кнопки)
    p.write_text(with_key(fx.EXAMPLE))
    c = owner.load(p)
    assert owner.OwnerCfg.from_frozen(c.frozen_json()).get(f"perp.aster.{KEY}") is True
