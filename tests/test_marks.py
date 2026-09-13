"""Оценка сделки (trade/marks.py): «PnL сейчас» и «PnL при выходе» на числах живой сделки DQA9Q (фейковые котировка и
стакан), знаки при росте и падении цены, стакан мельче шорта, частичный выход, фандинг плюсом и минусом, газ approve
при малом allowance, пропуск во время исполнения, ошибка чтения → флаг, кабинет (обе строки, «устарело», «считается»,
экранирование, итог закрытой), «позиции», чистка таблицы. Сеть не трогается, ключей нет."""
from __future__ import annotations
import json, re, sqlite3, threading
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from funding_bot import cabinet, config
from funding_bot.trade import marks, reconcile, store, tconfig
from funding_bot.trade.engine import Conns, Legs
from funding_bot.trade.types import Book, DexQuote, Filters
from funding_bot.tg import views
from funding_bot.tg.bot import Bot, Jobs

T0 = 1_757_700_000.0
NOW = T0 + 3600
E18 = 10 ** 18
TOKEN = "0x" + "a1" * 20
STABLE = config.OKX_DEX_STABLES["56"][0]
WALLET = "0x" + "e4" * 20
ROUTER = "0x5994814f2c4040b863a0125a45de152a8c2a4dec"
SYMBOL = "AIW3USDT"
Q_RAW = 4902151 * 10 ** 15                     # 4 902.151 AIW3 (18 знаков)
Q_TOK = D("4902.151")
BNB = D(620)
GAS_APPROVE = D(76_000 * 10 ** 8) / D(E18) * BNB          # 0.004712 $
GAS_SWAP = D(150_000 * 10 ** 8) / D(E18) * BNB            # 0.0093 $
GAS = GAS_APPROVE + GAS_SWAP                               # 0.014012 $ ≈ «газ approve + своп ≈ 0.014 $»
BASE = D(-200) + D("200.63886") - D("0.0803") - GAS        # спот-поток + перп-поток − комиссии − газ (фандинга нет)
FEE = D("0.0004")                                          # config.FEES_TAKER["aster"]
flat = lambda s: s.replace(views.NBSP, " ")               # noqa: E731
_N = [7000]


# --- фейки ног (только чтение) ----------------------------------------------------------------------------
class Rpc:
    def __init__(self, allowance=10 ** 30):
        self.value = allowance
        self.calls: list = []

    def allowance(self, token, owner, spender):
        self.calls.append((token, owner, spender))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


SMALL = 1000 * E18                             # меньше — малая котировка продажи (сторона продажи для P_dex)


class Spot:
    """OKX DEX: pool_price — малая котировка ПОКУПКИ (px); малая продажа — по bid (по умолчанию = px, тогда середина
    = px); quote на весь объём — котировка выхода (out USDT, tradeFee gas $)."""
    chain = "bsc"

    def __init__(self, px="0.0420", out="205.20", gas=0.011, allowance=10 ** 30, bid=None):
        self.px, self.out, self.gas = D(px), D(out), gas
        self.bid = D(bid) if bid is not None else self.px
        self.wallet, self.stable, self.stable_dec = WALLET, STABLE, 18
        self.rpc = Rpc(allowance)
        self.quotes: list = []
        self.px_error: Exception | None = None
        self.bid_error: Exception | None = None

    def pool_price(self, token):
        if self.px_error is not None:
            raise self.px_error
        return self.px

    def quote(self, t_in, t_out, amount):
        self.quotes.append((t_in, t_out, amount))
        if amount < SMALL:                     # малая продажа: 1 000 токенов по bid (отношение точное)
            if self.bid_error is not None:
                raise self.bid_error
            return DexQuote("bsc", t_in, t_out, SMALL, int(self.bid * SMALL), 18, 18, None, None, None, False, 0.0)
        return DexQuote("bsc", t_in, t_out, int(amount), int(self.out * E18), 18, 18, None, self.gas, None, False, 0.0)

    def exits(self):
        """Котировки выхода на весь объём (без малых котировок цены)."""
        return [q for q in self.quotes if q[2] >= SMALL]


def book(mid: str, depth: int = 10_000):
    m = D(mid)
    return ((m - D("0.00001"), D(depth)),), ((m + D("0.00001"), D(depth)),)


class Perp:
    """Aster: стакан (по умолчанию мид 0.0421, асксы 3 000 @ 0.04215 + 5 000 @ 0.04220), позиция, начисления."""
    venue = "aster"

    def __init__(self, bids=((D("0.04205"), D(10_000)),), asks=((D("0.04215"), D(3000)), (D("0.04220"), D(5000))),
                 pos=D(-4902)):
        self.bids, self.asks, self.pos = tuple(bids), tuple(asks), pos
        self.income: list[dict] = []
        self.book_error: Exception | None = None
        self.income_error: Exception | None = None
        self.income_calls = 0

    def book(self, s, limit=20):
        if self.book_error is not None:
            raise self.book_error
        return Book(self.bids, self.asks, 0.0)

    def position(self, s):
        return self.pos

    def funding_income(self, s, start_ms):
        self.income_calls += 1
        if self.income_error is not None:
            raise self.income_error
        return list(self.income)

    def funding(self, s):
        return D("0.0421"), D("0.0005"), 0

    def filters(self, s):
        return Filters(tick=D("0.00001"), step=D(1), min_qty=D(1), max_qty_limit=D(10 ** 6), max_qty_market=D(10 ** 5),
                       min_notional=D(5), tifs=frozenset({"IOC"}))

    def fills(self, s, from_id):
        return []


def legs_of(spot=None, perp=None, sim=False):
    return Legs(spot or Spot(), perp or Perp(), sim, lambda: BNB)


# --- журнал сделки ------------------------------------------------------------------------------------------
def _intent(con, did, kind, iid, t):
    iid, nonce = store.create_intent(con, deal_id=did, kind=kind, spec={}, plan={}, intent_id=iid, now=t)
    assert store.approve_intent(con, iid, nonce, now=t + 1)
    store.set_intent_status(con, iid, "running")
    return iid


def _clip(con, iid, seq, a_in, a_out, t):
    c = store.create_clip(con, iid, seq, a_in, now=t)
    store.set_clip_state(con, c, "DEX_SENT", now=t)
    store.set_clip_state(con, c, "DEX_OK", dex_in=a_in, dex_out=a_out, now=t)
    return c


def _tx(con, clip, kind, gas_used, to):
    _N[0] += 1
    h = "0x%064x" % _N[0]
    store.dex_tx_signed(con, clip_id=clip, kind=kind, chain="bsc", wallet=WALLET, nonce=_N[0], to_addr=to, value=0,
                        min_receive=None, gas_limit=300_000, gas_price=10 ** 8, raw_tx="0xf86b", tx_hash=h)
    store.dex_tx_sent(con, h)
    store.dex_tx_resolve(con, h, "MINED_OK", block=100, status=1, gas_used=gas_used, eff_gas_price=10 ** 8)
    return h


def _order(con, did, clip, letter, seq, side, qty, quote, oid, venue, fee=None):
    cid = store.client_order_id(did, letter, seq, 1, 1)
    store.perp_order_intent(con, clip_id=clip, client_id=cid, venue=venue, symbol=SYMBOL, side=side,
                            reduce_only=side == "BUY", tif="IOC", price=quote / qty, qty=qty)
    store.perp_order_result(con, cid, "FILLED", order_id=oid, executed_qty=qty, avg_price=quote / qty, cum_quote=quote)
    if fee is not None:
        store.add_perp_fills(con, venue, [dict(trade_id=oid * 10, order_id=oid, price=quote / qty, qty=qty,
                                               quote_qty=quote, commission_abs=fee, commission_asset="USDT",
                                               maker=False, realized_pnl=D(0), ts=int(T0 * 1000))])


def dqa9q(con, *, did="DQA9Q", sim=False, approve=True, token=TOKEN, symbol=SYMBOL, oid=111):
    """Живой пример 13.09: AIW3, спот okx·bsc, перп aster, 200 $ на ногу. Куплено 4 902.151 AIW3 за 200 USDT
    (ср. 0.04080), продано на перпе 4 902 по ср. 0.04093 (cum_quote 200.63886), комиссия перпа 0.0803 USDT,
    газ approve USDT + своп ≈ 0.014 $."""
    venue = "sim:aster" if sim else "aster"
    store.create_deal(con, coin="AIW3", chain="bsc", token=token, token_dec=18, perp_venue="aster", symbol=symbol,
                      leg_usd=D(200), owner_json="{}", sim=sim, deal_id=did, now=T0)
    iid = _intent(con, did, "entry", "E" + did[1:], T0)
    store.set_deal_state(con, did, "ENTERING", now=T0 + 1)
    c = _clip(con, iid, 1, 200 * E18, Q_RAW, T0 + 5)
    if not sim:
        if approve:
            h = _tx(con, None, "approve", 76_000, STABLE)
            store.event(con, "approve", deal_id=did, intent_id=iid, hashes=[h], status="ok", now=T0 + 3)
        _tx(con, c, "swap", 150_000, ROUTER)
    _order(con, did, c, "e", 1, "SELL", D(4902), D("200.63886"), oid, venue, fee=D("0.0803"))
    store.set_intent_status(con, iid, "done")
    store.set_deal_state(con, did, "OPEN", now=T0 + 60)
    return store.get_deal(con, did)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "trade.db")
    yield c
    c.close()


# ==== 1. формулы на числах DQA9Q ================================================================================
def test_dqa9q_pnl_now_and_exit(con):
    deal = dqa9q(con)
    spot, perp = Spot(px="0.0420", out="205.20"), Perp()
    m = marks.mark_deal(con, deal, legs_of(spot, perp), now=NOW)
    assert GAS == D("0.014012") and m.gas == GAS and m.fees == D("0.0803") and m.funding == 0
    assert m.px_dex == D("0.0420") and m.px_perp == D("0.0421")
    # сейчас: база + Q_tok·P_dex − Q_short·P_perp = 0.544548 + 205.890342 − 206.3742
    assert m.pnl_now == BASE + Q_TOK * D("0.0420") - 4902 * D("0.0421") == D("0.06069")
    # выход: 205.20 USDT по котировке − газ свопа 0.011; откуп 3 000 @ 0.04215 + 1 902 @ 0.04220 и комиссия 0.04 %
    cost = 3000 * D("0.04215") + 1902 * D("0.04220")
    assert cost == D("206.7144")
    assert m.pnl_exit == BASE + D("205.20") - D("0.011") - cost - cost * FEE
    ex = m.flags["exit"]
    assert ex == {"dex": Q_TOK * D("0.0420") - D("205.20"), "book": cost - 4902 * D("0.0421"), "fee": cost * FEE,
                  "gas": D("0.011")}
    assert m.exit_cost == m.pnl_now - m.pnl_exit == sum(ex.values()) > 0
    assert spot.exits() == [(TOKEN, STABLE, Q_RAW)], "одна котировка OKX на весь объём"
    assert not {"uncovered", "approve", "errors", "position", "sim"} & set(m.flags)
    marks.save(con, m)
    row = marks.decode(store.last_mark(con, "DQA9Q"))
    assert row["pnl_now"] == m.pnl_now and row["exit_cost"] == m.exit_cost and row["gas"] == GAS
    assert row["flags"]["exit"]["fee"] == format(cost * FEE, "f")


def test_signs_when_price_moves(con):
    """Рост спота — плюс на Q_tok; рост перпа — минус на Q_short; вместе — только голая разница ног."""
    deal = dqa9q(con)

    def pnl(px, mid):
        bids, asks = book(mid)
        return marks.mark_deal(con, deal, legs_of(Spot(px=px), Perp(bids, asks)), now=NOW).pnl_now

    p0 = pnl("0.0408", "0.04093")
    assert p0 == BASE + Q_TOK * D("0.0408") - 4902 * D("0.04093")
    assert pnl("0.0420", "0.04093") - p0 == Q_TOK * D("0.0012") > 0          # спот вырос
    assert pnl("0.0408", "0.04213") - p0 == -4902 * D("0.0012") < 0          # перп вырос — шорт теряет
    assert pnl("0.0420", "0.04213") - p0 == D("0.151") * D("0.0012") > 0     # вместе: 0.151 AIW3 без хеджа
    assert pnl("0.0398", "0.03993") - p0 == -D("0.151") * D("0.0010") < 0    # вместе вниз


def test_book_not_covering_the_short(con):
    deal = dqa9q(con)
    m = marks.mark_deal(con, deal, legs_of(Spot(), Perp(asks=((D("0.04215"), D(3000)),))), now=NOW)
    assert m.flags["uncovered"] == D(1902), "стакан не покрывает — флаг и остаток"
    cost = 4902 * D("0.04215")                                              # остаток — по худшему уровню
    assert m.pnl_exit == BASE + D("205.20") - D("0.011") - cost - cost * FEE
    assert m.flags["exit"]["book"] == cost - 4902 * m.px_perp
    empty = marks.mark_deal(con, deal, legs_of(Spot(), Perp(asks=())), now=NOW)
    assert empty.pnl_exit is None and any("стакан" in x for x in empty.flags["errors"])


def test_partial_exit_flows_are_counted(con):
    deal = dqa9q(con)
    x = _intent(con, "DQA9Q", "exit", "XQA9Q", T0 + 100)
    store.set_deal_state(con, "DQA9Q", "EXITING", now=T0 + 101)
    c = _clip(con, x, 1, 2000 * E18, 815 * 10 ** 17, T0 + 110)             # 2 000 AIW3 → 81.5 USDT
    _order(con, "DQA9Q", c, "x", 1, "BUY", D(2000), D("81.9"), 112, "aster", fee=D("0.03276"))
    store.set_intent_status(con, x, "done")
    store.set_deal_state(con, "DQA9Q", "OPEN", now=T0 + 120)
    spot = Spot(out="121.50")
    m = marks.mark_deal(con, store.get_deal(con, "DQA9Q"), legs_of(spot, Perp(pos=D(-2902))), now=NOW)
    assert m.flags["q_tok"] == D("2902.151") and m.flags["q_short"] == D(2902)
    assert spot.exits() == [(TOKEN, STABLE, 2902151 * 10 ** 15)], "котировка — на остаток по книге"
    base = (D("81.5") - 200) + (D("200.63886") - D("81.9")) - (D("0.0803") + D("0.03276")) - GAS
    assert m.fees == D("0.11306")
    assert m.pnl_now == base + D("2902.151") * D("0.0420") - 2902 * D("0.0421")
    cost = 2902 * D("0.04215")
    assert m.pnl_exit == base + D("121.50") - D("0.011") - cost - cost * FEE
    assert "position" not in m.flags


def test_funding_plus_and_minus(con):
    deal = dqa9q(con)
    perp = Perp()
    legs = legs_of(Spot(), perp)
    m0 = marks.mark_deal(con, deal, legs, now=NOW)
    ms = int(T0 * 1000)
    perp.income = [dict(tran_id=1, symbol=SYMBOL, income=D("0.30"), ts=ms + 1000),
                   dict(tran_id=2, symbol=SYMBOL, income=D("-0.10"), ts=ms + 2000),
                   dict(tran_id=9, symbol=SYMBOL, income=D("100"), ts=ms - 1)]          # до открытия — не наше
    m1 = marks.mark_deal(con, deal, legs, now=NOW)
    assert m1.funding == D("0.20") and m1.pnl_now - m0.pnl_now == D("0.20") and m1.pnl_exit - m0.pnl_exit == D("0.20")
    perp.income.append(dict(tran_id=3, symbol=SYMBOL, income=D("-0.50"), ts=ms + 3000))
    m2 = marks.mark_deal(con, deal, legs, now=NOW)
    assert m2.funding == D("-0.30") and m2.pnl_now - m0.pnl_now == D("-0.30")
    assert con.execute("SELECT count(*) FROM funding_income").fetchone()[0] == 4, "добор начислений — в журнал"
    perp.income_error = RuntimeError("income 503")
    m3 = marks.mark_deal(con, deal, legs, now=NOW)                                # не добрали — по записанному
    assert m3.funding == D("-0.30") and m3.pnl_now == m2.pnl_now
    assert m3.flags["errors"] == ["фандинг не добран: RuntimeError: income 503"]


def test_approve_gas_when_allowance_is_small(con, tmp_path):
    deal = dqa9q(con)
    big = marks.mark_deal(con, deal, legs_of(Spot()), now=NOW)
    spot = Spot(allowance=1000 * E18)                                            # меньше Q_tok
    m = marks.mark_deal(con, deal, legs_of(spot), now=NOW)
    assert GAS_APPROVE == D("0.004712")
    assert m.flags["approve"] == "need" and m.flags["approve_src"] == "last"     # по чеку последнего approve
    assert m.flags["exit"]["gas"] == D("0.011") + GAS_APPROVE
    assert m.pnl_exit == big.pnl_exit - GAS_APPROVE and m.pnl_now == big.pnl_now
    assert {c[2] for c in spot.rpc.calls} == set(tconfig.OKX_SPENDERS["56"]) and spot.rpc.calls[0][0] == TOKEN
    spot.rpc.value = RuntimeError("rpc down")                                    # не прочитан — заложить и пометить
    u = marks.mark_deal(con, deal, legs_of(spot), now=NOW)
    assert u.flags["approve"] == "unknown" and u.pnl_exit == m.pnl_exit
    assert "allowance: RuntimeError: rpc down" in u.flags["errors"]
    c2 = store.connect(tmp_path / "second.db")                                   # approve ещё не было — ≈ газ свопа
    d2 = dqa9q(c2, approve=False)
    n = marks.mark_deal(c2, d2, legs_of(Spot(allowance=0)), now=NOW)
    assert n.flags["approve_src"] == "swap" and n.flags["exit"]["gas"] == D("0.022")
    s = marks.mark_deal(c2, dqa9q(c2, did="DSIM2", sim=True, token="0x" + "b2" * 20, symbol="BBBUSDT", oid=222),
                        legs_of(Spot(allowance=0), sim=True), now=NOW)
    assert "approve" not in s.flags and s.flags["sim"] is True, "в симуляции approve не нужен (как plan_exit)"
    c2.close()


# ==== 1б. адверсарное ревью чисел ====================================================================================
def test_review_numbers_by_hand(con):
    """Пересчёт руками: куплено 4 902.151 за 200 USDT; продано 4 902 на 200.63886; комиссия 0.0803; газ 0.014012;
    P_dex 0.0409 (середина: покупка 0.0410, продажа 0.0408), мид перпа 0.04095, асксы 0.04096×3 000 и 0.04098×10 000,
    котировка продажи всех токенов 200.30 USDT, газ свопа 0.01, фандинг +0.07."""
    deal = dqa9q(con)
    perp = Perp(bids=((D("0.04094"), D(10_000)),), asks=((D("0.04096"), D(3000)), (D("0.04098"), D(10_000))))
    perp.income = [dict(tran_id=1, symbol=SYMBOL, income=D("0.07"), ts=int(T0 * 1000) + 1000)]
    m = marks.mark_deal(con, deal, legs_of(Spot(px="0.0410", bid="0.0408", out="200.30", gas=0.01), perp), now=NOW)
    assert m.px_dex == D("0.0409") and m.px_perp == D("0.04095") and m.funding == D("0.07") and m.gas == GAS
    assert BASE + D("0.07") == D("0.614548")                   # −200 + 200.63886 − 0.0803 − 0.014012 + 0.07
    assert m.pnl_now == D("0.3756239")                         # база + 200.4979759 − 200.7369
    assert m.pnl_exit == D("0.000258416")                      # база + 200.30 − 0.01 − 200.82396 − 0.080329584
    assert m.exit_cost == D("0.375365484")
    assert m.flags["exit"] == {"dex": D("0.1979759"), "book": D("0.08706"), "fee": D("0.080329584"), "gas": D("0.01")}


def test_dex_price_is_pool_mid_not_buy_price(con):
    """pool_price — цена ПОКУПКИ (USDT → токен, комиссия пула сверху): длинный спот по ней завышен на комиссию пула.
    P_dex — середина покупки и продажи по малым котировкам (как мид стакана перпа). Продажа не прочитана — «PnL
    сейчас» не считается (односторонняя цена смещена на комиссию), выход — считается."""
    deal = dqa9q(con)
    spot = Spot(px="0.0421", bid="0.0419")                                       # пул ~0.24 %: покупка выше продажи
    m = marks.mark_deal(con, deal, legs_of(spot), now=NOW)
    assert m.px_dex == D("0.0420") and m.flags["px_dex_ask"] == D("0.0421") and m.flags["px_dex_bid"] == D("0.0419")
    assert m.pnl_now == BASE + Q_TOK * D("0.0420") - 4902 * D("0.0421")
    assert (BASE + Q_TOK * D("0.0421") - 4902 * D("0.0421")) - m.pnl_now == D("0.4902151"), \
        "по цене покупки PnL сейчас был бы завышен на ~0.49 $"
    small = [q for q in spot.quotes if q[2] < SMALL]
    assert small == [(TOKEN, STABLE, int(D(config.OKX_DEX_QUOTE_USD) / D("0.0421") * E18))], "продажа ~15 $: токен → USDT"
    assert spot.exits() == [(TOKEN, STABLE, Q_RAW)]
    same = marks.mark_deal(con, deal, legs_of(Spot()), now=NOW)
    assert m.pnl_exit == same.pnl_exit, "выход — по котировке на весь объём, от P_dex не зависит"
    assert m.flags["exit"]["dex"] == Q_TOK * D("0.0420") - D("205.20")
    spot.bid_error = RuntimeError("okx 429")
    f = marks.mark_deal(con, deal, legs_of(spot), now=NOW)
    assert f.px_dex is None and f.pnl_now is None and f.exit_cost is None and f.pnl_exit == m.pnl_exit
    assert "цена DEX (продажа): RuntimeError: okx 429" in f.flags["errors"]


def test_fees_not_lost_when_commission_not_in_stables_or_fills_partial(con):
    """Комиссия не в стейблах (скидка BNB) и недобранные userTrades: непокрытый оборот — по тарифу (fees_est), а не
    молча 0 (было 0.0803 + 0 + 0.00984 = 0.09014 — занижено на 0.03932 $)."""
    deal = dqa9q(con)
    j0 = marks.journal(con, deal)
    assert j0.fees == D("0.0803") and j0.fees_est is False, "userTrades покрывают оборот — без оценки"
    x = _intent(con, "DQA9Q", "exit", "XQA9Q", T0 + 100)
    store.set_deal_state(con, "DQA9Q", "EXITING", now=T0 + 101)
    c1 = _clip(con, x, 1, 2000 * E18, 815 * 10 ** 17, T0 + 110)                 # 2 000 AIW3 → 81.5 USDT
    _order(con, "DQA9Q", c1, "x", 1, "BUY", D(2000), D("81.9"), 112, "aster")
    c2 = _clip(con, x, 2, 1000 * E18, 41 * E18, T0 + 120)                       # 1 000 AIW3 → 41 USDT
    _order(con, "DQA9Q", c2, "x", 2, "BUY", D(1000), D("41.0"), 113, "aster")
    ts = int(T0 * 1000)
    store.add_perp_fills(con, "aster", [
        dict(trade_id=1121, order_id=112, price=D("0.04095"), qty=D(2000), quote_qty=D("81.9"),
             commission_abs=D("0.00005"), commission_asset="BNB", maker=False, realized_pnl=D(0), ts=ts),   # скидка BNB
        dict(trade_id=1131, order_id=113, price=D("0.041"), qty=D(600), quote_qty=D("24.6"),
             commission_abs=D("0.00984"), commission_asset="USDT", maker=False, realized_pnl=D(0), ts=ts)])  # 600 из 1 000
    store.set_intent_status(con, x, "done")
    store.set_deal_state(con, "DQA9Q", "OPEN", now=T0 + 130)
    j = marks.journal(con, store.get_deal(con, "DQA9Q"))
    assert j.fees == D("0.0803") + D("81.9") * FEE + D("0.00984") + D("16.4") * FEE == D("0.12946") and j.fees_est
    m = marks.mark_deal(con, store.get_deal(con, "DQA9Q"), legs_of(Spot(out="78.00"), Perp(pos=D(-1902))), now=NOW)
    assert m.fees == D("0.12946") and m.flags["fees_est"] is True


# ==== 2. ошибки чтения и периодичность =============================================================================
def test_read_errors_become_flags_without_secrets(con):
    deal = dqa9q(con)
    spot, perp = Spot(), Perp()
    spot.px_error = RuntimeError("okx 50011 " + "ab" * 32)
    m = marks.mark_deal(con, deal, legs_of(spot, perp), now=NOW)
    assert m.pnl_now is None and m.exit_cost is None and m.pnl_exit is not None   # выход считается и без P_dex
    assert m.flags["errors"][0].startswith("цена DEX: RuntimeError: okx 50011 <hex64>")
    assert "ab" * 32 not in json.dumps(m.flags, default=str)
    spot.px_error, perp.book_error = None, TimeoutError("depth")
    m = marks.mark_deal(con, deal, legs_of(spot, perp), now=NOW)
    assert m.pnl_now is None and m.pnl_exit is None and m.px_perp is None
    assert "стакан aster: TimeoutError: depth" in m.flags["errors"]
    perp.book_error, perp.pos = None, D(-4800)                                  # позиция биржи ≠ журнал — флаг
    assert marks.mark_deal(con, deal, legs_of(spot, perp), now=NOW).flags["position"] == D(-4800)

    def boom(sim):
        raise KeyError("legs")
    ms, complete = marks.run_pass(con, boom, now=NOW)                           # сбой целиком — строка с флагом
    assert complete and ms[0].pnl_now is None and ms[0].flags["errors"][0].startswith("расчёт упал: KeyError")
    assert "расчёт упал" in store.last_mark(con, "DQA9Q")["flags_json"]


class Eng:
    def __init__(self):
        self.is_busy = False
        self.hooks = None
        self.pause_evt = threading.Event()

    def busy(self):
        return self.is_busy


def make_bot(tmp_path, legs):
    clock = SimpleNamespace(t=NOW)
    eng = Eng()
    bot = Bot(conns=Conns(tmp_path / "trade.db"), desk=None, engine=eng, sender=None, legs=lambda sim: legs,
              owner_loader=lambda: SimpleNamespace(owner_id=None), mode="readonly", jobs=Jobs(sync=True),
              clock=lambda: clock.t)
    return bot, eng, clock


def test_marks_every_mark_s_and_skipped_while_executing(tmp_path, con):
    dqa9q(con)
    spot = Spot()
    bot, eng, clock = make_bot(tmp_path, legs_of(spot))
    count = lambda: con.execute("SELECT count(*) FROM deal_marks").fetchone()[0]
    eng.is_busy = True
    bot.housekeep()
    assert count() == 0 and spot.quotes == [], "во время исполнения — ни расчёта, ни котировки"
    eng.is_busy = False
    bot.housekeep()
    assert count() == 1 and store.last_mark(con, "DQA9Q")["pnl_now"] is not None
    clock.t += 10
    bot.housekeep()
    assert count() == 1, "не чаще MARK_S"
    clock.t += tconfig.MARK_S
    bot.housekeep()
    assert count() == 2 and len(spot.exits()) == 2
    spot.px_error = RuntimeError("down")                                        # ошибка чтения — строка, не падение
    clock.t += tconfig.MARK_S
    bot.housekeep()
    assert count() == 3 and "цена DEX" in store.last_mark(con, "DQA9Q")["flags_json"]


def test_pass_stops_when_execution_starts_midway(con):
    dqa9q(con)
    dqa9q(con, did="DQB22", token="0x" + "b2" * 20, symbol="BBBUSDT", oid=222)
    written = lambda: con.execute("SELECT count(*) FROM deal_marks").fetchone()[0] > 0     # noqa: E731
    ms, complete = marks.run_pass(con, lambda sim: legs_of(), now=NOW, busy=written)   # «да» — после первой сделки
    assert [m.deal_id for m in ms] == ["DQA9Q"] and complete is False


# ==== 3. закрытая сделка и чистка ====================================================================================
def _close_dqa9q(con):
    x = _intent(con, "DQA9Q", "exit", "XQA9Q", T0 + 100)
    store.set_deal_state(con, "DQA9Q", "EXITING", now=T0 + 101)
    c = _clip(con, x, 1, Q_RAW, 2052 * 10 ** 17, T0 + 110)                      # всё → 205.2 USDT
    _order(con, "DQA9Q", c, "x", 1, "BUY", D(4902), D("206.7144"), 113, "aster", fee=D("0.08269"))
    store.set_intent_status(con, x, "done")
    store.set_deal_state(con, "DQA9Q", "CLOSED", now=T0 + 200)


def test_closed_deal_gets_one_final_row(con):
    dqa9q(con)
    _close_dqa9q(con)
    legs = legs_of()
    ms, complete = marks.run_pass(con, lambda sim: legs, now=NOW)
    assert ms == [] and complete
    fin = marks.decode(store.last_mark(con, "DQA9Q"))
    total = (D("205.2") - 200) + (D("200.63886") - D("206.7144")) - (D("0.0803") + D("0.08269")) - GAS
    assert fin["flags"]["final"] is True and fin["pnl_now"] == total and fin["pnl_exit"] is None
    marks.run_pass(con, lambda sim: legs, now=NOW + tconfig.MARK_S)
    assert con.execute("SELECT count(*) FROM deal_marks").fetchone()[0] == 1, "итог пишется один раз"


def test_marks_table_is_pruned_but_last_row_of_each_deal_stays(con):
    old = NOW - tconfig.MARK_KEEP_S - 60
    for did, ts in (("DA", old), ("DA", old + 1), ("DA", NOW - 60), ("DB", old)):
        store.add_mark(con, did, ts, pnl_now=D("0.1"), flags={})
    assert store.prune_marks(con, NOW) == 2
    assert [tuple(r) for r in con.execute("SELECT deal_id, ts FROM deal_marks ORDER BY deal_id")] == \
        [("DA", NOW - 60), ("DB", old)]
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE deal_marks SET pnl_now='9'")                        # только добавление
    with pytest.raises(TypeError):
        store.add_mark(con, "DA", NOW, pnl_now=0.1)                             # float в суммы не пропускается
    store.add_mark(con, "DA", old, flags={})
    marks.run_pass(con, lambda sim: None, now=NOW)                             # проход чистит сам
    assert con.execute("SELECT count(*) FROM deal_marks WHERE ts < ?", (NOW - tconfig.MARK_KEEP_S,)).fetchone()[0] == 1


# ==== 4. кабинет =====================================================================================================
def cards(page: str) -> dict:
    out = {}
    for part in page.split("<article")[1:]:
        out[re.search(r'<span class="m mono">(D[A-Z0-9]+)</span>', part).group(1)] = part
    return out


def test_cabinet_card_shows_both_lines_stale_pending_and_escapes(tmp_path):
    p = tmp_path / "trade.db"
    w = store.connect(p)
    dqa9q(w)
    dqa9q(w, did="DQB22", token="0x" + "b2" * 20, symbol="BBBUSDT", oid=222)
    dqa9q(w, did="DQC33", token="0x" + "c3" * 20, symbol="CCCUSDT", oid=333)
    parts = {"dex": D("0.10"), "book": D("0.05"), "fee": D("0.08"), "gas": D("0.01")}
    store.add_mark(w, "DQA9Q", NOW - 60, pnl_now=D("0.52"), pnl_exit=D("0.28"), exit_cost=D("0.24"),
                   flags={"exit": parts, "errors": ["<script>alert(1)</script>"]})
    store.add_mark(w, "DQB22", NOW - tconfig.MARK_STALE_S - 1, pnl_now=D("-0.30"), pnl_exit=D("-0.75"),
                   flags={"exit": parts, "uncovered": D(12)})
    w.close()
    cab = cabinet.Cabinet(environ={}, db_path=p, clock=lambda: NOW)
    frag = cabinet.deals_fragment(cab.snapshot())
    c = cards(frag)
    a = c["DQA9Q"]
    assert 'PnL сейчас <b class="big mono g">+0.52 $</b>' in a
    assert 'PnL при выходе <b class="big mono g">+0.28 $</b>' in a
    # владелец 13.09: «на 12:06 · выход: удар DEX …» — «лишняя инфа»: у свежего расчёта ни разбивки, ни времени
    assert "выход:" not in a and "удар DEX" not in a and "комиссия" not in a and "на <time" not in a
    assert '<div class="pn m"><span class="unk">не всё прочитано: &lt;script&gt;' in a and "устарело" not in a
    assert "<script>" not in frag
    # добавочное поле: старые строки deal_marks без flags.liq — «до ликвидации —», не падение
    assert 'до ликвидации</span><span class="m">—</span></div>' in a
    b = c["DQB22"]
    assert "устарело" in b and '<b class="big mono m">−0.30\u00a0$</b>' in b and "−0.75" in b
    assert f'на <time data-ts="{int(NOW - tconfig.MARK_STALE_S - 1)}">' in b         # устаревшая — с датой
    assert "стакан не покрывает" in b
    assert "PnL: считается…" in c["DQC33"] and "PnL при выходе" not in c["DQC33"]


def test_cabinet_closed_deal_shows_total_only(tmp_path):
    p = tmp_path / "trade.db"
    w = store.connect(p)
    dqa9q(w)
    _close_dqa9q(w)
    cab = cabinet.Cabinet(environ={}, db_path=p, clock=lambda: NOW)
    total = (D("205.2") - 200) + (D("200.63886") - D("206.7144")) - (D("0.0803") + D("0.08269"))
    a = cards(cabinet.deals_fragment(cab.snapshot()))["DQA9Q"]                 # итога трейдера ещё нет — журнал
    assert total == D("-1.03853") and 'PnL итог <b class="big mono r">−1.04\u00a0$</b>' in a    # минус — красным
    assert "газ в $ не учтён" in a and "PnL при выходе" not in a and "PnL сейчас" not in a
    marks.run_pass(w, lambda sim: legs_of(), now=NOW)                          # трейдер записал итог с газом
    a = cards(cabinet.deals_fragment(cab.snapshot()))["DQA9Q"]
    assert f"{cabinet.fmt_num(total - GAS, 2, sign=True)}\u00a0$" in a and "газ в $ не учтён" not in a
    w.execute("DROP TABLE deal_marks")                                          # трейдер старой версии: таблицы нет
    w.close()
    frag = cabinet.deals_fragment(cab.snapshot())
    assert "PnL итог" in frag and "внутренняя ошибка" not in frag


def test_cabinet_without_marks_table_says_counting(tmp_path):
    p = tmp_path / "trade.db"
    w = store.connect(p)
    dqa9q(w)
    w.execute("DROP TABLE deal_marks")
    w.close()
    snap = cabinet.Cabinet(environ={}, db_path=p, clock=lambda: NOW).snapshot()
    frag = cabinet.deals_fragment(snap)
    assert snap["err"] is None and "PnL: считается…" in frag
    assert 'до ликвидации</span><span class="m">—</span>' in frag and "история выплат" in frag


# ==== 5. «позиции» ======================================================================================================
def test_positions_line_fresh_then_from_table(con):
    dqa9q(con, sim=True)
    spot, perp = Spot(), Perp()
    legs = legs_of(spot, perp, sim=True)
    rows, matched, _ = reconcile.positions(con, lambda sim: legs, now=NOW)
    r = rows[0]
    want = D(-200) + D("200.63886") - D("0.0803") + Q_TOK * D("0.0420") - 4902 * D("0.0421")   # sim: газа и фандинга нет
    assert matched is True and r["pnl_now_usd"] == want and r["pnl_exit_usd"] is not None
    assert len(spot.exits()) == 1 and perp.income_calls == 0
    assert json.loads(store.last_mark(con, "DQA9Q")["flags_json"])["sim"] is True, "свежий расчёт записан, sim"
    rows2, _, _ = reconcile.positions(con, lambda sim: legs, now=NOW + 10)     # моложе MARK_S — из таблицы
    assert len(spot.exits()) == 1 and rows2[0]["pnl_now_usd"] == r["pnl_now_usd"]
    rows3, _, _ = reconcile.positions(con, lambda sim: legs, now=NOW + tconfig.MARK_STALE_S + 60, resolve=False)
    assert rows3[0]["pnl_now_usd"] is None and len(spot.exits()) == 1, "идёт исполнение — не считаем, старое не берём"
    text = flat(views.positions([views.PositionView(**r)], ts=NOW, matched=True, sim=True))
    assert f"PnL сейчас {flat(views.money(want, sign=True))} · при выходе " in text


def test_positions_view_pnl_line_style_c():
    ok = views.PositionView("DQA9Q", "AIW3", "OPEN", spot_qty=Q_TOK, perp_qty=D(-4902), delta_qty=D("0.151"),
                            step=D(1), leg_usd=D(200), pnl_now_usd=D("0.52"), pnl_exit_usd=D("0.10"))
    assert flat(views.positions([ok], ts=NOW, matched=True)).splitlines() == [
        "📊 <b>1 сделка</b> · сверка ✓", "AIW3 <code>DQA9Q</code> · 200 $ на ногу",
        "PnL сейчас +0.52 $ · при выходе +0.10 $"]
    neg = views.PositionView("DQA9Q", "AIW3", "OPEN", pnl_now_usd=D("-1.5"), pnl_exit_usd=D("-2.25"),
                             pnl_uncovered=True)
    t = flat(views.positions([neg], ts=NOW, matched=True))
    assert "PnL сейчас −1.50 $ · при выходе −2.25 $" in t and "стакан Aster мельче шорта" in t
    assert "PnL" not in flat(views.positions([views.PositionView("DQA9Q", "AIW3", "OPEN")], ts=NOW, matched=True))


# ==== 6. ревью: исполнение посреди сделки, вес income ======================================================================
def test_execution_starting_mid_deal_stops_network_reads_and_row(con):
    """Исполнитель стартовал после цены DEX: котировка выхода, allowance, позиция и строка — нет (слоты темпа OKX и
    вес Aster достаются исполнителю; журнал мог меняться между чтениями)."""
    dqa9q(con)
    flag = {"busy": False}

    class Spot2(Spot):
        def pool_price(self, token):
            flag["busy"] = True                                                   # «да» нажато, пока читали цену
            return super().pool_price(token)

    spot, perp = Spot2(), Perp()
    ms, complete = marks.run_pass(con, lambda sim: legs_of(spot, perp), now=NOW, busy=lambda: flag["busy"])
    assert ms == [] and complete is False
    assert spot.quotes == [] and spot.rpc.calls == [], "котировка выхода и allowance не запрошены"
    assert con.execute("SELECT count(*) FROM deal_marks").fetchone()[0] == 0, "строка не пишется"
    with pytest.raises(marks.Preempted):
        marks.mark_deal(con, store.get_deal(con, "DQA9Q"), legs_of(Spot2(), Perp()), now=NOW,
                        busy=lambda: flag["busy"])
    flag["busy"] = False                                                          # исполнитель свободен — проход идёт
    ms, complete = marks.run_pass(con, lambda sim: legs_of(Spot(), Perp()), now=NOW, busy=lambda: flag["busy"])
    assert complete and len(ms) == 1 and ms[0].pnl_exit is not None


def test_funding_is_fetched_from_last_recorded_not_from_open(con):
    """income — 30 веса Aster за окно 7 сут: не с открытия каждый проход, а от последнего записанного минус сутки."""
    deal = dqa9q(con)
    starts: list[int] = []

    class Perp2(Perp):
        def funding_income(self, s, start_ms):
            starts.append(int(start_ms))
            return super().funding_income(s, start_ms)

    perp = Perp2()
    ms0 = int(T0 * 1000)
    marks.mark_deal(con, deal, legs_of(Spot(), perp), now=NOW)
    assert starts == [ms0], "записей нет — с открытия сделки"
    last = ms0 + 30 * 86_400_000                                                  # сделке 30 суток
    perp.income = [dict(tran_id=1, symbol=SYMBOL, income=D("0.30"), ts=ms0 + 1000),
                   dict(tran_id=2, symbol=SYMBOL, income=D("0.20"), ts=last)]
    marks.mark_deal(con, deal, legs_of(Spot(), perp), now=NOW)
    m = marks.mark_deal(con, deal, legs_of(Spot(), perp), now=NOW)
    assert starts[-1] == last - marks.FUNDING_OVERLAP_MS, "одно окно, а не пять"
    assert m.funding == D("0.50"), "дубли по tran_id не удваивают, старые начисления в итоге остаются"
    store.add_funding_income(con, "aster", [dict(tran_id=7, symbol=SYMBOL, income=D("9"), ts=ms0 - 5)])
    later = T0 + 90 * 86400                                                       # следующая сделка на том же символе
    assert marks.funding_since(con, {"created": later, "symbol": SYMBOL}, "aster") == int(later * 1000), \
        "начисления прошлой сделки на символе не сдвигают начало раньше открытия"


def test_positions_mark_preempted_shows_last_row(con):
    dqa9q(con)
    store.add_mark(con, "DQA9Q", NOW - tconfig.MARK_S - 5, pnl_now=D("0.4"), pnl_exit=D("0.1"), flags={})
    spot = Spot()
    got = marks.for_positions(con, store.get_deal(con, "DQA9Q"), legs_of(spot), now=NOW, fresh=True,
                              busy=lambda: True)
    assert got is not None and got["pnl_now"] == D("0.4") and spot.quotes == []
    assert con.execute("SELECT count(*) FROM deal_marks").fetchone()[0] == 1


# ==== 7. «до ликвидации» (правка владельца 13.09): positionRisk → flags.liq → кабинет ====================================
class RiskPerp(Perp):
    """Aster с positionRisk: по умолчанию шорт 4 902, ликвидация 0.05473 при марке 0.0421 (на 30\u00a0% выше)."""

    def __init__(self, rows=None, **kw):
        super().__init__(**kw)
        self.rows = rows if rows is not None else [dict(symbol=SYMBOL, positionSide="BOTH", positionAmt="-4902",
                                                        liquidationPrice="0.05473", markPrice="0.0421")]
        self.risk_calls = 0
        self.risk_error: Exception | None = None

    def position_risk(self, s=None):
        self.risk_calls += 1
        if self.risk_error is not None:
            raise self.risk_error
        return self.rows


def test_liq_distance_short_liq_above_mark():
    assert marks.liq_distance(D("0.05473"), D("0.0421")) == D("0.3")             # шорт: ликвидация на 30 % выше марка
    assert marks.liq_distance("0.05473", "0.0421") == D("0.3")                   # строки из flags_json
    assert marks.liq_distance(D("0.0421"), D("0.0421")) == 0
    assert marks.liq_distance(D("0.04"), D("0.05")) == D("-0.2")                 # марк уже за ценой ликвидации
    assert marks.liq_distance(D("0.04"), D("0.05"), short=False) == D("0.2")     # лонг — ликвидация ниже марка
    for bad in ((None, D(1)), (D(1), None), (D(0), D(1)), (D(1), D(0)), (D(-1), D(1)), ("x", D(1)), (D("NaN"), D(1))):
        assert marks.liq_distance(*bad) is None, bad


def test_liq_from_position_risk_is_stored_in_flags(con):
    deal = dqa9q(con)
    perp = RiskPerp()
    m = marks.mark_deal(con, deal, legs_of(Spot(), perp), now=NOW)
    assert perp.risk_calls == 1 and m.flags["liq"] == {"price": D("0.05473"), "mark": D("0.0421"), "ts": NOW}
    assert "errors" not in m.flags
    plain = marks.mark_deal(con, deal, legs_of(Spot(), Perp()), now=NOW)        # нога без positionRisk — без flags.liq
    assert "liq" not in plain.flags and (plain.pnl_now, plain.pnl_exit) == (m.pnl_now, m.pnl_exit)
    marks.save(con, m)                                                            # добавочное поле: схема та же
    row = marks.decode(store.last_mark(con, "DQA9Q"))
    assert row["flags"]["liq"] == {"price": "0.05473", "mark": "0.0421", "ts": NOW}
    assert [r[1] for r in con.execute("PRAGMA table_info(deal_marks)")] == ["deal_id", "ts", *store.MARK_COLS,
                                                                            "flags_json"]


def test_liq_read_error_hedge_rows_zero_price_and_sim(con):
    deal = dqa9q(con)
    perp = RiskPerp()
    perp.risk_error = RuntimeError("positionRisk 503")
    m = marks.mark_deal(con, deal, legs_of(Spot(), perp), now=NOW)
    assert "liq" not in m.flags and "positionRisk aster: RuntimeError: positionRisk 503" in m.flags["errors"]
    assert m.pnl_now is not None, "сбой positionRisk не ломает оценку"
    hedge = RiskPerp(rows=[dict(symbol=SYMBOL, positionSide="SHORT", positionAmt="-4902", liquidationPrice="0.05",
                                markPrice="0.0421")])
    m = marks.mark_deal(con, deal, legs_of(Spot(), hedge), now=NOW)
    assert "liq" not in m.flags and any("нет одной строки" in x for x in m.flags["errors"])
    zero = RiskPerp(rows=[dict(symbol=SYMBOL, positionSide="BOTH", positionAmt="-4902", liquidationPrice="0",
                               markPrice="0.0421")])
    m = marks.mark_deal(con, deal, legs_of(Spot(), zero), now=NOW)                # 0 у биржи — «ликвидации нет»
    assert m.flags["liq"] == {"price": None, "mark": D("0.0421"), "ts": NOW}
    sim = RiskPerp()
    s = marks.mark_deal(con, dqa9q(con, did="DSIM2", sim=True, token="0x" + "b2" * 20, symbol="BBBUSDT", oid=222),
                        legs_of(Spot(), sim, sim=True), now=NOW)
    assert sim.risk_calls == 0 and "liq" not in s.flags, "симуляция: подписанного чтения нет"


def test_cabinet_liq_cell_fresh_stale_below_mark_and_sim(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TABLE_PATH", tmp_path / "no-table.json")
    p = tmp_path / "trade.db"
    w = store.connect(p)
    dqa9q(w)
    dqa9q(w, did="DSIM2", sim=True, token="0x" + "b2" * 20, symbol="BBBUSDT", oid=222)
    store.add_mark(w, "DQA9Q", NOW - 60, pnl_now=D("0.52"), pnl_exit=D("0.28"),
                   flags={"liq": {"price": D("0.05473"), "mark": D("0.0421"), "ts": NOW - 60}})
    store.add_mark(w, "DQA9Q", NOW - 30, pnl_now=D("0.50"), pnl_exit=D("0.27"),       # новее, но positionRisk не прочитан
                   flags={"errors": ["positionRisk aster: RuntimeError: 503"]})
    store.add_mark(w, "DQA9Q", NOW - 20, pnl_now=D("0.49"), flags={"liq": "<b>битое</b>"})   # не объект — мимо
    w.close()
    card = lambda clock: cards(cabinet.deals_fragment(                                # noqa: E731
        cabinet.Cabinet(environ={}, db_path=p, clock=lambda: clock).snapshot()))
    c = card(NOW)
    a = c["DQA9Q"]
    assert 'до ликвидации</span><b class="mono ">+30.00\u00a0%</b></div>' in a      # 2 знака, одна цифра, не «зелёная»
    assert "ликв." not in a and "0.05473" not in a
    assert "+0.49\u00a0$" in a and "битое" not in a and "устарело" not in a            # PnL — по последней строке
    s = c["DSIM2"]
    assert 'до ликвидации</span><span class="m">—</span><span class="s">симуляция</span>' in s
    assert "история выплат" not in s                                              # фандинг симуляции не начисляется
    a = card(NOW - 60 + tconfig.MARK_STALE_S + 1)["DQA9Q"]                        # оценка трейдера не обновлялась
    assert 'до ликвидации</span><b class="mono m">+30.00\u00a0%</b>' in a
    assert f'на <time data-ts="{int(NOW - 60)}">' in a and "устарело" in a
    w = store.connect(p)
    store.add_mark(w, "DQA9Q", NOW, pnl_now=D("0.4"), flags={"liq": {"price": D("0.04"), "mark": D("0.0421"),
                                                                     "ts": NOW}})
    w.close()
    a = card(NOW)["DQA9Q"]
    assert 'до ликвидации</span><b class="mono r">−4.99\u00a0%</b>' in a            # марк выше ликвидации — красным


def test_liq_cleared_when_short_is_gone(tmp_path, monkeypatch):
    """Перп закрыт, спот остался (пауза «перп закрыт, спот остался»): прошлая цена ликвидации к сделке больше не
    относится — оценка пишет flags.liq без цен (positionRisk не читается), кабинет показывает «—», а не +30 % шорта,
    которого нет (раньше — 15 мин как текущее, потом серым навсегда)."""
    monkeypatch.setattr(config, "TABLE_PATH", tmp_path / "no-table.json")
    p = tmp_path / "trade.db"
    w = store.connect(p)
    dqa9q(w)
    store.add_mark(w, "DQA9Q", NOW - 300, pnl_now=D("0.52"),
                   flags={"liq": {"price": D("0.05473"), "mark": D("0.0421"), "ts": NOW - 300}})
    x = _intent(w, "DQA9Q", "exit", "XQA9Q", T0 + 100)                            # откуплен весь шорт, спот не продан
    store.set_deal_state(w, "DQA9Q", "EXITING", now=T0 + 101)
    c = store.create_clip(w, x, 1, 0, now=T0 + 110)
    _order(w, "DQA9Q", c, "x", 1, "BUY", D(4902), D("206.7144"), 113, "aster", fee=D("0.08269"))
    store.set_intent_status(w, x, "partial", err="x")
    store.set_deal_state(w, "DQA9Q", "PAUSED", reason="перп закрыт, спот остался", now=T0 + 120)
    perp = RiskPerp(pos=D(0))
    m = marks.mark_deal(w, store.get_deal(w, "DQA9Q"), legs_of(Spot(), perp), now=NOW)
    assert m.flags["q_short"] == 0 and perp.risk_calls == 0
    assert m.flags["liq"] == {"price": None, "mark": None, "ts": NOW}
    marks.save(w, m)
    w.close()
    a = cards(cabinet.deals_fragment(cabinet.Cabinet(environ={}, db_path=p, clock=lambda: NOW).snapshot()))["DQA9Q"]
    assert 'до ликвидации</span><span class="m">—</span></div>' in a and "30.00" not in a
