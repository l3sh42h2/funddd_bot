"""Статьи расходов Solana (trade/fees.py): точные raw↔человеческие единицы, priority от запрошенного лимита,
неизвестное ≠ 0, включённое в потоки не вычитается, возвратный rent отдельно, спонсор — не наш расход (S17, S18, R04, R05).
Все числа синтетические."""
from dataclasses import replace
from decimal import Decimal as D
import pytest
from funding_bot.trade import fees as F

W = "DY2fMoW98bY2uGDeCTRez8LojKyR2rJY8f1CZUBb1Bv9"
SPONSOR = "GGztQqQ6pCPaJQnNpXBgELr5cs3WwDakRbh1iEMzjgSJ"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _sol(px="150", at=0.0):
    return {F.NATIVE_SOL: F.PriceObs(F.NATIVE_SOL, USDC, D(px), at, "test")}


def _value(comps, prices=None, now=0.0, age=1000):
    return F.value_external(comps, wallet=W, unit=USDC, prices=_sol() if prices is None else prices, now_mono=now,
                            max_price_age_ms=age)


def test_raw_human_exact_to_last_unit_s09():
    assert F.human(1_234_567, 6) == D("1.234567")
    assert F.raw_of(F.human(1_234_567, 6), 6) == 1_234_567
    assert F.human(200_000_000, 6) == D(200)                      # USDC 200, а не 2e-10
    assert F.human(1, 9) == D("0.000000001")
    assert F.human(2 ** 64 - 1, 6) == D("18446744073709.551615")  # вся область u64 без потери последней единицы
    assert F.raw_of(D("1234.567891"), 6) == 1_234_567_891
    with pytest.raises(F.FeeError):
        F.raw_of(D("0.0000001"), 6)                               # дробь ниже 1 raw — ошибка, не округление
    with pytest.raises(F.FeeError):
        F.human(1.5, 6)
    with pytest.raises(F.FeeError):
        F.raw_of(0.1, 6)


def test_priority_fee_from_requested_limit_ceil():
    assert F.priority_fee_lamports(200_000, 1603) == 321          # 320.6 → вверх
    assert F.priority_fee_lamports(1_400_000, 1603) == 2245       # цена CU из живого ответа Build (1603 мкл.)
    assert F.priority_fee_lamports(120_000, 1603) == 193
    assert F.priority_fee_lamports(1_000, 0) == 0


def test_network_components_unknown_is_not_zero():
    b, p = F.network_components(n_signatures=1, cu_limit=None, cu_price_micro=1000, payer=W, source="t")
    assert (b.amount_raw, p.amount_raw) == (5000, None)
    b, p = F.network_components(n_signatures=2, cu_limit=None, cu_price_micro=0, payer=W, source="t")
    assert (b.amount_raw, p.amount_raw) == (10_000, 0)
    _, p = F.network_components(n_signatures=1, cu_limit=None, cu_price_micro=None, payer=W, source="t")
    assert p.amount_raw is None
    _, p = F.network_components(n_signatures=1, cu_limit=1_400_000, cu_price_micro=1603, payer=W, source="t",
                                limit_is_upper_bound=True)
    assert p.amount_raw == 2245 and p.estimated


def test_s17_meta_fee_includes_priority_and_tip_is_separate():
    comps = F.receipt_components(fee_lamports=30_000, fee_payer=W, tip_lamports=10_000)
    assert [c.kind for c in comps] == ["network_total", "tip"]        # отдельной priority 25 000 нет — не дважды
    assert F.native_split(comps, W).nonrefundable == 40_000
    assert F.human(40_000, 9) == D("0.00004")
    assert _value(comps).total == D("0.006")                          # 0.00004 SOL × 150


def test_s18_rent_deposit_and_refund_are_not_fees():
    closed = F.receipt_components(fee_lamports=5000, fee_payer=W, rent_deposits=[2_000_000], rent_refunds=[2_000_000])
    s = F.native_split(closed, W)
    assert (s.nonrefundable, s.rent_locked) == (5000, 0)
    opened = F.receipt_components(fee_lamports=5000, fee_payer=W, rent_deposits=[2_000_000])
    assert F.native_split(opened, W).rent_locked == 2_000_000
    assert _value(opened).total == D("0.00075")                       # депозит в экономическую стоимость не идёт
    assert F.native_cash_needed(opened, W, reserve_lamports=1_000_000) == 5000 + 2_000_000 + 1_000_000
    nonref = F.receipt_components(fee_lamports=5000, fee_payer=W, rent_nonrefundable=1_000)
    assert F.native_split(nonref, W).nonrefundable == 6000


def test_r04_included_fee_is_explained_not_subtracted():
    c = F.FeeComponent("platform", USDC, 6, 10_000_000, W, included_in_input_output=True, estimated=False)
    v = _value([c])
    assert v.total == 0 and v.parts == ()
    ext = F.FeeComponent("router", USDC, 6, 2_000_000, W, included_in_input_output=False, estimated=False)
    assert _value([c, ext]).total == D(2)                             # 990 − 2 = 988, не 978 (R04)


def test_r05_unknown_amount_payer_price_or_stale_price_gives_no_total():
    assert _value([F.lamports("network_priority", None, W, estimated=True, source="t")]).unknown == ("network_priority:amount",)
    assert _value([F.lamports("network_base", 5000, None, estimated=True, source="t")]).unknown == ("network_base:payer",)
    v = _value([F.lamports("network_base", 5000, W, estimated=False, source="t")], prices={})
    assert v.total is None and v.unknown == ("network_base:price",)
    v = _value([F.lamports("network_base", 5000, W, estimated=False, source="t")], prices=_sol(at=0.0), now=10.0, age=1000)
    assert v.total is None and v.unknown == ("network_base:price_stale",)
    v = _value([F.lamports("network_base", 5000, W, estimated=False, source="t")], age=None)
    assert v.total == D("0.00075") and v.unchecked == ("network_base:price_age",)


def test_sponsored_fee_is_visible_but_not_our_debit():
    c = F.lamports("network_base", 5000, SPONSOR, estimated=True, source="t")
    v = _value([c])
    assert v.total == 0 and v.sponsored == ("network_base",)
    s = F.native_split([c], W)
    assert (s.nonrefundable, s.sponsored) == (0, 5000)


def test_okx_trade_fee_superseded_is_not_added_to_own_estimate():
    own = F.lamports("network_base", 5000, W, estimated=False, source="t")
    tf = F.FeeComponent("network_estimate_usd", F.USD, 4, 21, W, False, True, superseded=True)    # 0.0021 USD
    prices = {**_sol(), F.USD: F.PriceObs(F.USD, USDC, D(1), 0.0, "test")}
    assert _value([own, tf], prices=prices).total == D("0.00075")
    assert _value([own, replace(tf, superseded=False)], prices=prices).total == D("0.00075") + D("0.0021")
    assert all(c.superseded for c in F.with_superseded([tf, own], frozenset({"network_estimate_usd"})) if c.kind != "network_base")


def test_fee_component_contract():
    with pytest.raises(F.FeeError):
        F.FeeComponent("bogus", USDC, 6, 1, W, False, False)
    with pytest.raises(F.FeeError):
        F.lamports("network_base", -1, W, estimated=False, source="t")
    with pytest.raises(F.FeeError):
        F.lamports("network_base", 5000.0, W, estimated=False, source="t")
    with pytest.raises(F.FeeError):
        F.FeeComponent("rent_deposit", F.NATIVE_SOL, 9, 1, W, False, False, refundable=False)
    rec = F.lamports("tip", 10_000, W, estimated=False, source="t").as_record()
    assert rec["amount_raw"] == "10000" and rec["asset"] == F.NATIVE_SOL


def test_s20_cash_needed_is_unknown_without_reserve_or_with_unknown_item():
    base = F.lamports("network_base", 5000, W, estimated=False, source="t")
    assert F.native_cash_needed([base, F.lamports("rent_deposit", None, W, estimated=True, source="t")], W, 1000) is None
    assert F.native_cash_needed([base], W, None) is None             # нет запаса владельца — проверить нельзя
    assert F.native_cash_needed([base], W, 0) == 5000                # лампорты, без деления на 10^18


def test_legacy_evm_gas_as_fee_component():
    c = F.legacy_evm(21_000, 3_000_000_000, "0xabc")
    assert (c.amount_raw, c.decimals, c.human()) == (63_000_000_000_000, 18, D("0.000063"))
