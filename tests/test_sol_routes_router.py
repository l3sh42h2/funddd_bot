"""Выбор маршрута (trade/spot_router.py) на синтетических кандидатах: одинаковые условия, полная стоимость входа и
выхода, неизвестное ≠ 0, свежесть, ворота групп, пара с HL по глубине, одна повторная сессия, UNKNOWN в полёте,
замена после одобрения, журнал кандидатов (R01–R08, R10, R12–R14, G07, G12). Числа — синтетические, не лимиты
владельца и не котировки ANSEM."""
import hashlib, threading, time
from dataclasses import replace
from decimal import Decimal as D, localcontext
import pytest
from funding_bot.trade import fees as F, spot_router as sr
from funding_bot.trade.types import Book

NOW, WALL = 1000.0, 1_789_000_000.0
W = sr.b58encode(hashlib.sha256(b"wallet").digest())
USDC = sr.AssetRef("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", sr.TOKEN_PROGRAM, 6, "USDC")
ANSEM = sr.AssetRef("9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump", sr.TOKEN_2022_PROGRAM, 6, "ANSEM")
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
LIM = sr.RouteLimits(max_quote_age_ms=5000, collection_deadline_ms=3000, min_blockhash_validity_heights=60,
                     max_network_fee_lamports_per_tx=200_000, max_spot_slippage_bps=100, max_price_impact_bps=150,
                     max_native_price_age_ms=10_000, max_book_age_ms=2000, max_rent_locked_lamports=3_000_000)
PRICES = {F.NATIVE_SOL: F.PriceObs(F.NATIVE_SOL, USDC.mint, D(100), NOW, "test"),
          F.USD: F.PriceObs(F.USD, USDC.mint, D(1), NOW, "test")}
POL3 = sr.RoutingPolicy(order_execution_enabled=True)          # все три пути исполнимы (как в G12)
J_ORDER, J_BUILD, OKX = "jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6"


def mkreq(side="entry", amount=None, **kw):
    a_in, a_out = (USDC, ANSEM) if side == "entry" else (ANSEM, USDC)
    kw.setdefault("deadline_mono", NOW + 5)
    return sr.QuoteRequest(side=side, input=a_in, output=a_out, amount_in_raw=amount or 1_000_000_000, wallet=W,
                           slippage_bps=50, genesis_hash=GENESIS, **kw)


def usdc(amount_raw, included=False):
    return F.FeeComponent("router", USDC.mint, 6, amount_raw, W, included, False, source="test")


def cand(path, out, req, *, fees=(), min_out=None, **kw):
    base = dict(provider=sr.GROUP_OF[path], path=path, adapter_version="test", request_hash=req.request_hash,
                side=req.side, input_mint=req.input.mint, output_mint=req.output.mint, input_program=req.input.program,
                output_program=req.output.program, input_decimals=req.input.decimals,
                output_decimals=req.output.decimals, amount_in_raw=req.amount_in_raw, expected_out_raw=out,
                min_out_raw=out * 9950 // 10000 if min_out is None else min_out, onchain_min_out_raw=None,
                fees=tuple(fees), message_hash="h", last_valid_block_height=2000, price_impact_bps=D(5),
                received_mono=NOW, received_at=WALL, simulation_ok=True, validated=True, response_hash=path)
    base.update(kw)
    return sr.SwapCandidate(**base)


def hedge(px="1", depth=10 ** 7, **kw):
    # маржа шорта — синтетическая и с запасом: здесь проверяется выбор маршрута, маржа — отдельными тестами
    kw.setdefault("leverage", D(1))
    kw.setdefault("margin_reserve", D(0))
    kw.setdefault("available_margin", D(10 ** 12))
    lv = ((D(px), D(depth)),)
    return sr.HedgeContext(Book(bids=lv, asks=lv, ts=WALL),
                           sr.PairParams(fs=D(1), fp=D(1), step=D(1), perp_fee_rate=D("0.000675"), min_notional=D(10), **kw),
                           WALL)


def rank(cands, req, policy=POL3, limits=LIM, h=None, **kw):
    return sr.rank(cands, req, policy=policy, limits=limits, prices=PRICES, now_mono=NOW, now_wall=WALL,
                   block_height=1000, hedge=h or hedge(), **kw)


def decide(cands, req, policy=POL3, unavailable=(), **kw):
    ranked, warns = rank(cands, req, policy=policy, **kw)
    return sr.gate(ranked, list(unavailable), req, policy, warns)


def dq(x):
    with localcontext() as c:
        c.prec = 50
        return eval(x, {"D": D})       # ожидание — отдельной формулой из матрицы, не функцией модуля


# --- B: одинаковые условия и полная стоимость -----------------------------------------------------------------
def test_r01_only_same_request_is_compared():
    r = mkreq()
    good = cand(OKX, 989_000_000, r)
    other = mkreq(amount=999_000_000)
    bad = [cand(J_BUILD, 995_000_000, r, request_hash=other.request_hash),
           cand(J_BUILD, 995_000_000, r, amount_in_raw=999_000_000),
           cand(J_BUILD, 995_000_000, r, output_mint=ANSEM.mint.lower()),            # S02: другой регистр — другой актив
           cand(J_BUILD, 995_000_000, r, output_decimals=9),
           cand(J_BUILD, 995_000_000, r, side="exit"),
           cand(J_BUILD, 995_000_000, r, output_program=sr.TOKEN_PROGRAM)]
    ranked, _ = rank([good] + bad, r)
    assert [x.cand.path for x in ranked if x.eligible] == [OKX]
    for x in ranked[1:]:
        assert any(y.startswith("request_mismatch") for y in x.cand.reasons) and not x.previewable


def test_r02_entry_cost_per_token_not_gross_out():
    r = mkreq(amount=1_000_000_000)
    dec = decide([cand(J_BUILD, 990_000_000, r, fees=[usdc(2_000_000)]),
                  cand(OKX, 989_000_000, r, fees=[usdc(200_000)])], r)
    assert dec.status == "complete" and dec.winner.path == OKX
    m = {x.cand.path: x.metric for x in dec.ranked}
    assert m[OKX] == dq("D('1000.20') / D(989)") and m[J_BUILD] == dq("D(1002) / D(990)")


def test_r03_exit_net_usdc_same_tokens():
    r = mkreq("exit", amount=1_000_000_000)
    dec = decide([cand(J_BUILD, 995_000_000, r, fees=[usdc(2_000_000)]),
                  cand(OKX, 994_000_000, r, fees=[usdc(200_000)])], r)
    m = {x.cand.path: x.metric for x in dec.ranked}
    assert (m[J_BUILD], m[OKX]) == (D("993"), D("993.8")) and dec.winner.path == OKX


def test_r04_embedded_fee_not_subtracted_twice():
    r = mkreq("exit")
    ranked, _ = rank([cand(OKX, 990_000_000, r, fees=[usdc(10_000_000, included=True), usdc(2_000_000)])], r)
    assert ranked[0].metric == D("988") and ranked[0].valuation.parts == (("router", D(2)),)


def test_r05_unknown_mandatory_fee_is_not_cheapest():
    r = mkreq()
    unknown = F.lamports("network_priority", None, W, estimated=True, source="t")
    jup = cand(J_BUILD, 1_000_000_000, r, fees=[F.lamports("network_base", 5000, W, estimated=False, source="t"), unknown])
    okx = cand(OKX, 990_000_000, r, fees=[F.lamports("network_base", 5000, W, estimated=False, source="t")])
    dec = decide([jup, okx], r)
    j = next(x for x in dec.ranked if x.cand.path == J_BUILD)
    assert "fee_unknown:network_priority:amount" in j.cand.reasons and not j.previewable and j.metric is None
    assert dec.status == "refused" and dec.reasons == ("incomplete_comparison:jupiter",)
    assert dec.preview_winner.path == OKX
    dec2 = decide([jup, okx], r, policy=replace(POL3, require_both_providers_for_entry=False))
    assert dec2.status == "single_provider" and dec2.winner.path == OKX and "jupiter" in dec2.note


def test_r06_single_available_exit_is_labelled():
    r = mkreq("exit")
    pol = replace(POL3, allow_single_provider_risk_reducing_exit=True)
    un = [sr.Unavailable("jupiter", J_BUILD, "deadline", "нет ответа"), sr.Unavailable("jupiter", J_ORDER, "deadline")]
    dec = decide([cand(OKX, 990_000_000, r)], r, policy=pol, unavailable=un)
    assert dec.status == "single_provider" and dec.winner.path == OKX
    assert "лучший из доступных" in dec.note and "jupiter" in dec.note and "deadline" in dec.note
    assert decide([cand(OKX, 990_000_000, r)], r, unavailable=un).status == "refused"      # политика не включена
    e = mkreq()
    deg = decide([cand(OKX, 990_000_000, e)], e, policy=replace(POL3, allow_degraded_entry=True), unavailable=un)
    assert deg.status == "degraded" and deg.winner.path == OKX


def test_r07_nothing_valid_means_no_swap_and_no_zero_price():
    r = mkreq()
    router = sr.SpotRouter([_Prov("jupiter", (J_ORDER, J_BUILD), lambda q: [sr.Unavailable("jupiter", J_BUILD, "http", "500")]),
                            _Prov("okx", (OKX,), lambda q: [cand(OKX, 990_000_000, q, min_out=0)])], limits=LIM)
    dec = router.select(replace(r, deadline_mono=time.monotonic() + 2), prices=PRICES, block_height=1000, hedge=hedge())
    assert dec.status == "refused" and dec.winner is None and dec.preview_winner is None
    assert dec.reasons[0] in ("no_eligible_route", "plan_expired") and "no_eligible_route" in dec.reasons
    rows = dec.records("op", 1)
    assert {x["path"] for x in rows} == {J_BUILD, OKX} and not any(x["selected"] for x in rows)
    assert all(x.get("metric") in (None,) or x["metric"] != "0" for x in rows)


def test_r08_stale_gross_best_is_excluded():
    r = mkreq()
    stale = cand(J_BUILD, 1_000_000_000, r, received_mono=NOW - 6)
    fresh = cand(OKX, 990_000_000, r)
    ranked, _ = rank([stale, fresh], r)
    assert "stale" in ranked[1].cand.reasons and ranked[0].cand.path == OKX
    built_late = cand(J_BUILD, 1_000_000_000, r, received_mono=NOW - 6, built_mono=NOW - 1)   # свежесть — по финальной сборке
    assert rank([built_late], r)[0][0].eligible


def test_r10_final_build_fee_changes_winner_and_breaks_approved_bound():
    r = mkreq(amount=100_000_000)
    preview = cand(J_BUILD, 1_002_000_000, r, fees=[F.lamports("network_base", 5000, W, estimated=False, source="t")])
    final = replace(preview, fees=(F.lamports("network_base", 5000, W, estimated=False, source="t"),
                                   F.lamports("network_priority", 1_500_000, W, estimated=False, source="t")))
    okx = cand(OKX, 1_001_000_000, r, fees=[F.lamports("network_base", 5000, W, estimated=False, source="t")])
    wide = replace(LIM, max_network_fee_lamports_per_tx=5_000_000)  # синтетический потолок, чтобы решала цена
    first = decide([preview, okx], r, limits=wide)
    assert first.winner.path == J_BUILD
    ap = sr.Approval(r.request_hash, "entry", "fixed:jupiter", "jupiter", min_out_raw=preview.min_out_raw,
                     max_cost_per_token=first.winner_ranked.metric)
    second = decide([final, okx], r, limits=wide)
    assert second.winner.path == OKX                               # сравнивается финальная версия, не превью
    b = next(x for x in second.ranked if x.cand.path == J_BUILD)
    assert b.eligible and b.metric == dq("(D(100) + D('0.001505') * D(100)) / D(1002)")   # 1 505 000 лампортов × 100
    capped = decide([final, okx], r)                               # и превышение одобренного потолка сети
    assert "network_fee_over_cap" in next(x for x in capped.ranked if x.cand.path == J_BUILD).cand.reasons
    allowed, why, audit = sr.reselect_allowed(ap, second)
    assert not allowed and "policy_fixed" in why and audit["to_group"] == "okx"


def test_r12_unknown_inflight_blocks_new_route_before_any_request():
    p = _Prov("okx", (OKX,), lambda q: [cand(OKX, 990_000_000, q)])
    dec = sr.SpotRouter([p], limits=LIM).select(mkreq(), prices=PRICES, inflight_unknown=lambda: "sig 5xyz… UNKNOWN")
    assert dec.status == "refused" and dec.reasons == ("unknown_inflight:sig 5xyz… UNKNOWN",) and p.calls == 0


def test_r13_best_policy_within_bounds_vs_violation():
    r = mkreq(amount=100_000_000)
    dec = decide([cand(J_BUILD, 1_000_000_000, r, fees=[usdc(20_000)]), cand(OKX, 1_001_000_000, r, fees=[usdc(30_000)])], r)
    assert dec.winner.path == OKX
    ok_ap = sr.Approval(r.request_hash, "entry", "best", "jupiter", min_out_raw=995_000_000, max_cost_per_token=D("0.1001"))
    allowed, why, audit = sr.reselect_allowed(ok_ap, dec)
    assert allowed and why == () and (audit["from_group"], audit["to_group"]) == ("jupiter", "okx")
    tight = replace(ok_ap, max_cost_per_token=D("0.0999"))
    assert sr.reselect_allowed(tight, dec)[1] == ("cost_above_approved",)
    assert "min_out_below_approved" in sr.reselect_allowed(replace(ok_ap, min_out_raw=999_000_000), dec)[1]
    other = mkreq(amount=100_000_001)
    assert "tuple_changed" in sr.reselect_allowed(replace(ok_ap, request_hash=other.request_hash), dec)[1]
    assert "bound_missing" in sr.reselect_allowed(replace(ok_ap, max_cost_per_token=None), dec)[1]


def test_r14_fee_caps_and_tips():
    r = mkreq()
    base = F.lamports("network_base", 5000, W, estimated=False, source="t")
    over = cand(OKX, 990_000_000, r, fees=[base, F.lamports("network_priority", 300_000, W, estimated=False, source="t")])
    tip = cand(OKX, 990_000_000, r, fees=[base, F.lamports("tip", 10_000, W, estimated=False, source="t")])
    tip_unknown = cand(OKX, 990_000_000, r, fees=[base, F.lamports("tip", None, W, estimated=True, source="t")])
    ranked, _ = rank([over, tip, tip_unknown], r)
    reasons = [set(x.cand.reasons) for x in ranked]
    assert any("network_fee_over_cap" in s for s in reasons)
    assert any("tip_forbidden" in s for s in reasons)                      # нет лимита владельца — tip запрещён
    assert any("fee_unknown:tip:amount" in s for s in reasons)
    capped, _ = rank([tip], r, limits=replace(LIM, max_tip_lamports_per_tx=5_000))
    assert "tip_over_cap" in capped[0].cand.reasons


# --- G: группы и три пути --------------------------------------------------------------------------------------
def test_g07_jupiter_group_needs_eligible_order_or_build():
    r = mkreq()
    order_ext = cand(J_ORDER, 1_000_000_000, r, reasons=("external_signer:fee_payer", "capability:order_preview_only"))
    okx = cand(OKX, 990_000_000, r)
    dec = decide([order_ext, okx], r, policy=sr.RoutingPolicy())
    assert dec.status == "refused" and dec.reasons == ("incomplete_comparison:jupiter",) and dec.winner is None
    dec = decide([order_ext, okx, cand(J_BUILD, 985_000_000, r)], r, policy=sr.RoutingPolicy())
    assert dec.status == "complete" and dec.groups_eligible == ("jupiter", "okx") and dec.winner.path == OKX


def test_g12_three_paths_two_groups_okx_wins():
    r = mkreq(amount=100_000_000)
    cs = [cand(J_ORDER, 1_000_000_000, r, fees=[usdc(20_000)]), cand(J_BUILD, 1_002_000_000, r, fees=[usdc(350_000)]),
          cand(OKX, 1_001_000_000, r, fees=[usdc(30_000)])]
    dec = decide(cs, r)
    assert dec.winner.path == OKX and dec.status == "complete"
    assert [(x.cand.path, x.rank) for x in dec.ranked] == [(OKX, 1), (J_ORDER, 2), (J_BUILD, 3)]
    m = {x.cand.path: x.metric for x in dec.ranked}
    assert m[J_ORDER] == D("0.10002") and m[OKX] == dq("D('100.03') / D(1001)") and m[J_BUILD] == dq("D('100.35') / D(1002)")
    pilot = [replace(cs[0], reasons=("capability:order_preview_only",))] + cs[1:]      # пилот: Order — только показ
    dec = decide(pilot, r, policy=sr.RoutingPolicy())
    assert dec.winner.path == OKX and dec.groups_eligible == ("jupiter", "okx")
    order = next(x for x in dec.ranked if x.cand.path == J_ORDER)
    assert not order.eligible and order.previewable and order.rank is None


# --- экономика пары ------------------------------------------------------------------------------------------
def test_pair_vwap_over_depth_not_best_bid_and_insufficient_depth():
    r = mkreq(amount=1_000_000_000)
    c = cand(OKX, 1_000_000_000, r)
    h = sr.HedgeContext(Book(bids=((D("1.0"), D(600)), (D("0.9"), D(1000))), asks=(), ts=WALL),
                        sr.PairParams(D(1), D(1), D(1), D("0.000675"), D(10)), WALL)
    x = rank([c], r, h=h)[0][0]
    assert (x.pair.qty, x.pair.notional, x.pair.vwap) == (D(1000), D(960), D("0.96"))
    assert x.pair.basis_bps == dq("(D('0.96') / (D(1000) / D(1000)) - 1) * 10000")
    shallow = sr.HedgeContext(Book(bids=((D("1.0"), D(500)),), asks=(), ts=WALL), h.params, WALL)
    y = rank([c], r, h=shallow)[0][0]
    assert "hedge_depth" in y.cand.reasons and not y.eligible and y.pair.vwap is None


def test_pair_edge_matches_formula_and_min_notional_scenarios():
    r = mkreq(amount=1_000_000_000)
    x = rank([cand(OKX, 989_000_000, r, fees=[usdc(200_000)])], r, h=hedge("1.02"))[0][0]
    assert x.pair.edge == dq("D(989) * D('1.02') - D('1000.20') - D(989) * D('1.02') * D('0.000675')")
    small = mkreq(amount=10_000_000)
    # ожидание: 10 токенов → 10 контрактов × 1.02 = 10.2 ≥ 10; минимум 9.95 → 9 контрактов × 1.02 = 9.18 < 10
    y = rank([cand(OKX, 10_000_000, small, min_out=9_950_000)], small, h=hedge("1.02"))[0][0]
    assert "hedge_below_min_notional_at_min_out" in y.cand.reasons and "hedge_below_min_notional" not in y.cand.reasons


def test_hedge_missing_stale_or_fee_unknown_are_not_silently_ok():
    r = mkreq()
    c = cand(OKX, 990_000_000, r)
    none = sr.rank([c], r, policy=POL3, limits=LIM, prices=PRICES, now_mono=NOW, now_wall=WALL, block_height=1000)[0][0]
    assert "hedge_unchecked" in none.cand.reasons and none.previewable and not none.eligible
    stale = sr.HedgeContext(replace(hedge().book, ts=WALL - 5), hedge().params, WALL)
    assert "book_stale" in rank([c], r, h=stale)[0][0].cand.reasons
    nofee = sr.HedgeContext(hedge().book, replace(hedge().params, perp_fee_rate=None), WALL)
    assert "perp_fee_unknown" in rank([c], r, h=nofee)[0][0].cand.reasons
    exit_r = mkreq("exit")
    ex = rank([cand(OKX, 990_000_000, exit_r)], exit_r, h=hedge(exit_close_qty=D(999)))[0][0]
    assert ex.pair.qty == D(999) and ex.eligible


# --- прочие фильтры -----------------------------------------------------------------------------------------
def test_min_out_checks_and_impact_and_blockhash():
    r = mkreq()
    cases = {"min_out_mismatch": cand(OKX, 990_000_000, r, onchain_min_out_raw=985_000_000),
             "min_out_above_expected": cand(OKX, 990_000_000, r, min_out=991_000_000),
             "min_out_below_slippage": cand(OKX, 990_000_000, r, min_out=900_000_000),
             "price_impact_over_limit": cand(OKX, 990_000_000, r, price_impact_bps=D(-151)),
             "blockhash_expiring": cand(OKX, 990_000_000, r, last_valid_block_height=1059),
             "rfq_expired": cand(OKX, 990_000_000, r, rfq_expire_at=WALL - 1)}
    for want, c in cases.items():
        assert want in rank([c], r)[0][0].cand.reasons, want
    assert "price_impact_unknown" in rank([cand(OKX, 990_000_000, r, price_impact_bps=None)], r)[0][0].cand.reasons


def test_sol_costs_valued_by_one_price_and_rent_kept_apart():
    r = mkreq(amount=1_000_000_000)
    fees = [F.lamports("network_base", 5000, W, estimated=False, source="t"),
            F.lamports("network_priority", 15_000, W, estimated=False, source="t"),
            F.lamports("rent_deposit", 2_074_080, W, estimated=False, source="t")]
    x = rank([cand(OKX, 1_000_000_000, r, fees=fees)], r)[0][0]
    assert x.valuation.total == D("0.002")                              # 20 000 лампортов × 100; депозит не расход
    assert x.metric == dq("(D(1000) + D('0.002')) / D(1000)") and x.eligible
    y = rank([cand(OKX, 1_000_000_000, r, fees=fees)], r, limits=replace(LIM, max_rent_locked_lamports=2_000_000))[0][0]
    assert "rent_over_cap" in y.cand.reasons
    z = rank([cand(OKX, 1_000_000_000, r, fees=[F.lamports("rent_deposit", None, W, estimated=True, source="t")])], r)[0][0]
    assert "rent_unknown" in z.cand.reasons and z.previewable


def test_missing_owner_limits_block_live_but_keep_preview():
    r = mkreq()
    dec = decide([cand(J_BUILD, 985_000_000, r), cand(OKX, 990_000_000, r)], r, limits=sr.RouteLimits())
    assert dec.status == "refused" and dec.winner is None and dec.preview_winner.path == OKX
    reasons = set(dec.ranked[0].cand.reasons)
    for k in ("max_quote_age_ms", "max_spot_slippage_bps", "max_network_fee_lamports_per_tx", "collection_deadline_ms",
              "min_blockhash_validity_heights", "max_price_impact_bps", "max_book_age_ms"):
        assert f"limit_missing:{k}" in reasons


def test_not_simulated_or_not_validated_are_live_only():
    r = mkreq()
    x = rank([cand(OKX, 990_000_000, r, simulation_ok=None, validated=None, message_hash=None)], r)[0][0]
    assert {"not_simulated", "not_validated"} <= set(x.cand.reasons) and x.previewable and not x.eligible


def test_unstable_rank_warning_and_hysteresis():
    r = mkreq(amount=100_000_000)
    a = cand(J_BUILD, 1_002_000_000, r, min_out=997_000_000)
    b = cand(OKX, 1_001_000_000, r, min_out=1_000_000_000)
    _, warns = rank([a, b], r)
    assert warns == ("rank_unstable_within_slippage:jupiter_build_v2/okx_solana_v6",)
    lim = replace(LIM, min_route_improvement_usdc=D("0.5"))
    ranked, warns = rank([a, b], r, limits=lim, incumbent_path=OKX)
    assert ranked[0].cand.path == OKX and "hysteresis_kept:okx_solana_v6" in warns
    ranked, _ = rank([a, b], r, incumbent_path=OKX)                     # гистерезиса нет, пока не задан владельцем
    assert ranked[0].cand.path == J_BUILD


# --- сбор, срок, повторная сессия, журнал ----------------------------------------------------------------------
class _Prov:
    def __init__(self, group, paths, fn):
        self.group, self.paths, self.fn, self.calls = group, paths, fn, 0

    def candidates(self, req):
        self.calls += 1
        return self.fn(req)


def _live(path, out):
    return lambda q: [cand(path, out, q, received_mono=time.monotonic(), received_at=time.time())]


def test_collect_deadline_makes_slow_provider_unavailable():
    def slow(q):
        time.sleep(1.0)
        return _live(J_BUILD, 1_000_000_000)(q)
    router = sr.SpotRouter([_Prov("jupiter", (J_ORDER, J_BUILD), slow), _Prov("okx", (OKX,), _live(OKX, 990_000_000))],
                           limits=replace(LIM, collection_deadline_ms=300),
                           policy=replace(POL3, allow_degraded_entry=True, max_collection_rounds=1))
    t0 = time.monotonic()
    dec = router.select(mkreq(deadline_mono=time.monotonic() + 0.3), prices=_now_prices(), block_height=1000,
                        hedge=_now_hedge())
    assert time.monotonic() - t0 < 0.9
    assert {(u.path, u.reason) for u in dec.unavailable} == {(J_ORDER, "deadline"), (J_BUILD, "deadline")}
    assert dec.status == "degraded" and dec.winner.path == OKX


def test_one_retry_then_plan_expired_and_no_retry_for_permanent():
    stale = _Prov("okx", (OKX,), lambda q: [cand(OKX, 990_000_000, q, received_mono=time.monotonic() - 60)])
    jup = _Prov("jupiter", (J_BUILD,), lambda q: [cand(J_BUILD, 985_000_000, q, received_mono=time.monotonic() - 60)])
    router = sr.SpotRouter([jup, stale], limits=LIM)
    dec = router.select(mkreq(deadline_mono=time.monotonic() + 2), prices=_now_prices(), block_height=1000,
                        hedge=_now_hedge())
    assert (stale.calls, jup.calls) == (2, 2) and dec.round_no == 2 and "plan_expired" in dec.reasons
    perm = _Prov("okx", (OKX,), lambda q: [cand(OKX, 990_000_000, q, amount_in_raw=1, received_mono=time.monotonic())])
    router = sr.SpotRouter([perm], limits=LIM)
    dec = router.select(mkreq(deadline_mono=time.monotonic() + 2), prices=_now_prices(), block_height=1000,
                        hedge=_now_hedge())
    assert perm.calls == 1 and "plan_expired" not in dec.reasons


def test_records_have_all_candidates_reasons_and_one_selected():
    r = mkreq(amount=100_000_000)
    un = [sr.Unavailable("jupiter", J_ORDER, "no_transaction")]
    dec = decide([cand(J_BUILD, 1_002_000_000, r, fees=[usdc(350_000)]), cand(OKX, 1_001_000_000, r, fees=[usdc(30_000)])],
                 r, unavailable=un)
    rows = dec.records("op-7", 3)
    assert len(rows) == 3 and [x["path"] for x in rows if x["selected"]] == [OKX]
    by = {x["path"]: x for x in rows}
    assert by[OKX]["metric"] == str(dec.winner_ranked.metric) and by[OKX]["operation_id"] == "op-7"
    assert by[OKX]["clip_seq"] == 3 and by[OKX]["fees"][0]["amount_raw"] == "30000" and by[OKX]["rank"] == 1
    assert by[J_ORDER]["reasons"] == ["no_transaction"] and by[J_ORDER]["selected"] is False
    assert len({x["candidate_id"] for x in rows if x["candidate_id"]}) == 2


def test_presign_check_rechecks_freshness_and_blockhash():
    t = [NOW]
    router = sr.SpotRouter([], limits=LIM, clock=lambda: t[0], wall=lambda: WALL)
    r = mkreq()
    dec = decide([cand(J_BUILD, 985_000_000, r), cand(OKX, 990_000_000, r)], r)
    assert router.presign_check(dec, block_height=1000) == ()
    t[0] = NOW + 6
    assert router.presign_check(dec, block_height=1950) == ("stale", "blockhash_expiring")


def test_rate_gate_priority_and_deadline():
    g = sr.RateGate(0.0)
    order = []

    def run(p):
        with g.slot(p, time.monotonic() + 5) as ok:
            if ok:
                order.append(p)
    with g.slot("entry", time.monotonic() + 5) as ok:
        assert ok
        ta = threading.Thread(target=run, args=("scanner",))
        ta.start()
        time.sleep(0.05)
        tb = threading.Thread(target=run, args=("recovery",))
        tb.start()
        time.sleep(0.05)
    ta.join(2)
    tb.join(2)
    assert order == ["recovery", "scanner"]                    # защита раньше сканера
    g2 = sr.RateGate(1.0)
    with g2.slot("entry", time.monotonic() + 5) as ok:
        assert ok
    with g2.slot("entry", time.monotonic() + 0.1) as ok:
        assert ok is False                                      # слот не успевает к сроку — не занимается


# --- конфигурация и запрос ------------------------------------------------------------------------------------
def test_policy_and_limits_from_config():
    pol = sr.RoutingPolicy.from_config({"paths": ["jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6"],
                                        "require_both_providers_for_entry": True, "allow_degraded_entry": False,
                                        "allow_single_provider_risk_reducing_exit": True,
                                        "allow_external_signer_managed_routes": False, "max_collection_rounds": 2})
    assert pol.allow_single_provider_risk_reducing_exit and not pol.order_execution_enabled
    assert not pol.external_signer_recovery
    assert sr.RouteLimits.from_config({"max_clip_usdc": "30"}) == sr.RouteLimits()       # чужие ключи — не наши
    lim = sr.RouteLimits.from_config({"max_quote_age_ms": 5000, "min_route_improvement_usdc": "0.05"})
    assert lim.max_quote_age_ms == 5000 and lim.min_route_improvement_usdc == D("0.05")
    for bad in ({"max_quote_age_ms": 5.5}, {"max_quote_age_ms": True}, {"min_route_improvement_usdc": 0.05},
                {"max_quote_age_ms": -1}):
        with pytest.raises(ValueError):
            sr.RouteLimits.from_config(bad)
    with pytest.raises(ValueError):
        sr.RoutingPolicy(paths=("jupiter_ultra",))
    with pytest.raises(ValueError):
        sr.RoutingPolicy(max_collection_rounds=3)


def test_quote_request_validation_and_hash():
    r = mkreq()
    assert r.request_hash == mkreq(deadline_mono=NOW + 99, purpose="exit").request_hash   # срок/приоритет не в кортеже
    assert r.request_hash != mkreq(amount=1_000_000_001).request_hash
    for bad in (dict(amount_in_raw=1.5), dict(amount_in_raw=0), dict(slippage_bps=0), dict(wallet="not-base58-0OIl"),
                dict(side="buy"), dict(purpose="whatever"), dict(output=USDC)):
        with pytest.raises((ValueError, TypeError)):
            replace(r, **bad)
    with pytest.raises(ValueError):
        sr.AssetRef(USDC.mint, "11111111111111111111111111111111", 6)                       # не программа токена
    assert sr.b58decode(sr.b58encode(b"\x00\x00\x01\x02")) == b"\x00\x00\x01\x02"
    assert sr.is_pubkey(ANSEM.mint) and not sr.is_pubkey(ANSEM.mint + "1")


def _now_prices():
    now = time.monotonic()
    return {k: replace(v, observed_mono=now) for k, v in PRICES.items()}


def _now_hedge():
    h = hedge()
    return sr.HedgeContext(replace(h.book, ts=time.time()), h.params, time.time())
