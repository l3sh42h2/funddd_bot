"""Планировщик клипов (trade_spec §7) и числа отчётов. Чистые функции: без сети, ключей и часов.

Стакан и фильтры — живые факты AIW3USDT 12.09 (bid 0.04093 × 62 344, ask 0.04094 × ~$41, шаг 1, тик 0.00001,
MIN_NOTIONAL 5); ступени асков за лучшим уровнем подобраны так, чтобы покупка $500 одной заявкой прошла ~18 б.п.
от мида — как в отчёте clip §4 (сам снимок стакана в отчёте не сохранился)."""
import dataclasses
from decimal import ROUND_FLOOR, Decimal as D
from pathlib import Path
import pytest
from funding_bot.trade import owner, planner as P, report as R, store
from funding_bot.trade.types import Book, DexQuote, Filters

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "owner.toml.example"
E18 = 10 ** 18

FILT = Filters(tick=D("0.00001"), step=D(1), min_qty=D(1), max_qty_limit=D(800000), max_qty_market=D(80000),
               min_notional=D(5), tifs=frozenset({"GTC", "IOC", "GTX", "HIDDEN"}))
BIDS = ((D("0.04093"), D(62344)), (D("0.04090"), D(50000)), (D("0.04085"), D(100000)))
ASKS = ((D("0.04094"), D(1001)), (D("0.04097"), D(2000)), (D("0.04100"), D(4000)), (D("0.04104"), D(6000)),
        (D("0.04108"), D(37900)), (D("0.04115"), D(97600)))
BOOK = Book(bids=BIDS, asks=ASKS, ts=0.0)
MID = (BIDS[0][0] + ASKS[0][0]) / 2
K_AIW3 = D("4.35e-7")          # 1/(L·√P) пула PancakeSwap v3 AIW3/USDT, fee 0.01 %
C0_AIW3 = D("0.0001")          # комиссия пула 0.01 %
G_BSC = D("0.01")              # tradeFee, $ за своп
FEE = D("0.0004")              # тейкер Aster


def _cal(kind="entry", k=K_AIW3, c0=C0_AIW3, g=G_BSC, p_ref=MID):
    return P.Calib(kind=kind, k=k, c0=c0, g=g, p_ref=p_ref, points=(), k_raw=k, resid_bps=D(0))


def _mkt(**over):
    kw = dict(book=BOOK, filters=FILT, fee_taker=FEE, approve_usd=D("0.002"))
    kw.update(over)
    return P.Market(**kw)


def _plan(kind="entry", usd=500, calib=None, lim=None, mkt=None, r=D(0), units=None, now=1000.0):
    if units is None:
        units = usd * E18 if kind == "entry" else int(D(usd) / MID) * E18
    return P.plan(deal_id="E7K2", kind=kind, coin="AIW3", spot="okx·bsc", perp="aster", symbol="AIW3USDT",
                  leg_usd=D(usd), total_in_units=units, dec_in=18, calib=calib or _cal(kind),
                  mkt=mkt or _mkt(), lim=lim or P.Limits(clips_max=10, clip_max_usd="auto"), r=r, now=now)


# ================================ калибровка ================================
def _quote(kind, s, p0=D("0.04"), c0=C0_AIW3, k=K_AIW3, gas=0.01):
    """Синтетическая котировка по модели p(s) = p0·(1 ± (c0 + k·s)) на размер s долларов."""
    if kind == "entry":
        tokens = s / (p0 * (1 + c0 + k * s))
        return DexQuote("bsc", "USDT", "AIW3", int(s * E18), int(tokens * E18), 18, 18, 0.06, gas, 0.0, False, 0.0)
    tokens = s / p0
    usd = s * (1 - c0 - k * s)
    return DexQuote("bsc", "AIW3", "USDT", int(tokens * E18), int(usd * E18), 18, 18, 0.06, gas, 0.0, False, 0.0)


def test_calib_amounts_are_s8_to_s():
    assert P.calib_amounts(500 * E18) == [62 * E18 + E18 // 2, 125 * E18, 250 * E18, 500 * E18]


@pytest.mark.parametrize("kind", ["entry", "exit"])
def test_calibrate_recovers_k_and_c0(kind):
    qs = [_quote(kind, D(s)) for s in ("62.5", "125", "250", "500")]
    qs[1] = _quote(kind, D(125), gas=0.02)              # газ — медиана, выброс не решает
    cal = P.calibrate(qs, kind, p_ref=D("0.04"))
    assert cal.k == pytest.approx(K_AIW3, rel=D("0.02"))
    assert cal.c0 == pytest.approx(C0_AIW3, abs=D("2e-6"))
    assert cal.g == D("0.01") and cal.resid_bps < 1
    # без опорной цены весь сдвиг уходит в свободный член: c0 = 0, наклон тот же
    free = P.calibrate(qs, kind)
    assert free.c0 == 0 and free.k == pytest.approx(K_AIW3, rel=D("0.02"))


def test_calibrate_clamps_negative_slope_and_refuses_honeypot():
    good = [_quote("entry", D(s), k=D("-2e-7")) for s in (100, 200, 400)]
    cal = P.calibrate(good, "entry")
    assert cal.k == 0 and cal.k_raw < 0
    bad = [DexQuote(**{**_quote("entry", D(100)).__dict__, "honeypot": True}), _quote("entry", D(200))]
    with pytest.raises(P.PlanRefused, match="honeypot"):
        P.calibrate(bad, "entry")
    with pytest.raises(P.PlanRefused, match="двух разных"):
        P.calibrate([_quote("entry", D(100))] * 3, "entry")


def test_k_from_v3_matches_live_aiw3_pool():
    # liquidity() и slot0().sqrtPriceX96 пула 0x60F9…bdAB44 (публичный RPC, 12.09); token0 AIW3, token1 USDT
    L, sp = int("096a703bae65fb692594e6", 16), int("33ae83ef2daa2a6e610ae6da", 16)
    k1 = P.k_from_v3(L, sp, 18, 18, stable_is_token1=True)
    assert k1 == pytest.approx(D("4.35e-7"), rel=D("0.005"))
    # зеркальный пул (стейбл — token0): цена обратная, √P' = 2^192 / √P, та же L — тот же k
    sp_inv = 2 ** 192 // sp
    assert P.k_from_v3(L, sp_inv, 18, 18, stable_is_token1=False) == pytest.approx(k1, rel=D("1e-12"))
    cal = P.calibrate([_quote("entry", D(s)) for s in (125, 250, 500)], "entry", p_ref=D("0.04"), k_v3=k1)
    assert cal.k_ratio == pytest.approx(D(1), abs=D("0.03"))


# ================================ стоимость DEX и восстановление ================================
def test_r0_total_impact_is_k_s2_whatever_n():
    S = D(500)
    for n in range(1, 9):
        dc = P.dex_cost(n, S, K_AIW3, G_BSC, D(0), C0_AIW3)
        assert dc.impact == pytest.approx(K_AIW3 * S * S, rel=D("1e-20"))
        assert dc.fee == pytest.approx(C0_AIW3 * S, rel=D("1e-20")) and dc.gas == G_BSC * n
    one = P.dex_cost(1, S, K_AIW3, G_BSC, D(0), C0_AIW3)
    assert one.total == D("0.05") + D("0.10875") + D("0.01")          # отчёт clip: $0.05 + $0.109 + $0.01


def test_r0_gives_one_clip():
    p = _plan(r=D(0), lim=P.Limits(clips_max=20, clip_max_usd="auto"))
    assert p.est["n"] == 1 and len(p.clips) == 1 and p.clips[0].dex_in_units == 500 * E18


def test_r1_gives_n_close_to_s_over_sqrt_g_over_k():
    cal = _cal(k=D("1e-6"), c0=D(0), g=D("0.01"))                    # q* = √(g/k) = $100
    p = _plan(usd=1000, calib=cal, r=D(1), lim=P.Limits(clips_max=40))
    assert P.q_star(D("0.01"), D("1e-6")) == 100
    assert p.est["n"] == 10 and p.est["clip_usd"] == 100
    # AIW3: q* ≈ $152 → 3 клипа на $500, если пул восстанавливается (отчёт clip §4)
    pa = _plan(r=D(1), lim=P.Limits(clips_max=20))
    assert pa.est["q_star_usd"] == pytest.approx(D("151.6"), abs=D("0.2"))
    assert pa.est["n"] == 3
    assert pa.est["dex_impact_usd"] + pa.est["gas_usd"] == pytest.approx(D("0.066"), abs=D("0.001"))


def test_recovery_measurement_nets_market_move():
    assert P.recovery(D(1), D("1.001"), D("1.0005")) == D("0.5")
    assert P.recovery(D(1), D("1.001"), D("1.001")) == 0
    # марк перпа ушёл на те же +0.05 % — пул не «держит» наш след, это рынок
    assert P.recovery(D(1), D("1.001"), D("1.0005"), D(2), D("2.001")) == 1
    assert P.recovery(D(1), D("1.001"), D("0.99")) == 1                # перелёт — обрезка до 1
    assert P.recovery(D(1), D(1), D("1.01")) is None
    assert P.recovered(D("0.95")) and not P.recovered(D("0.5")) and not P.recovered(None)


# ================================ дочерние перпа (α/β) ================================
def test_alpha_beta_child_sizing_sell():
    one = P.perp_children(BOOK, "SELL", D(12200), FILT, alpha=D("0.2"), beta_bps=D(10))
    assert one == [(D(12200), D("0.04088"))]                          # floor(0.04093·0.999) к тику
    many = P.perp_children(BOOK, "SELL", D(12200), FILT, alpha=D("0.05"), beta_bps=D(10))
    h = P.floor_to(D("0.05") * 62344, D(1))                          # 3117
    assert len(many) == 4 and sum(q for q, _ in many) == 12200
    assert all(q <= h and q == q.to_integral_value() for q, _ in many)
    assert max(q for q, _ in many) - min(q for q, _ in many) <= 1   # поровну, остаток шагами вперёд
    # β без α: объём в пределах β от лучшей цены (1 б.п. — только лучший уровень, 10 б.п. — ещё 0.04090)
    assert P.child_cap(BOOK, "SELL", FILT, None, D(1)) == 62344
    big = dataclasses.replace(FILT, max_qty_market=D(800000))
    assert P.child_cap(BOOK, "SELL", big, None, D(10)) == 112344
    assert P.child_cap(BOOK, "SELL", FILT, None, D(10)) == 80000         # MARKET maxQty связывает раньше
    # maxQty: берётся меньший из LOT и MARKET (80 000)
    assert P.child_cap(BOOK, "SELL", FILT, None, None) == 80000


def test_alpha_beta_child_sizing_buy_and_dry_cap():
    ch = P.perp_children(BOOK, "BUY", D(1500), FILT, alpha=D("0.5"), beta_bps=D(5), reduce_only=True)
    assert [q for q, _ in ch] == [D(500), D(500), D(500)]
    assert ch[0][1] == D("0.04097")                                   # ceil(0.04094·1.0005) к тику
    # без α и β (dry): кэп — последний уровень, до которого идёт план, «не глубже плана»
    dry = P.perp_children(BOOK, "BUY", D(12214), FILT, None, None, reduce_only=True)
    assert dry == [(D(12214), D("0.04104"))]
    assert P.perp_children(BOOK, "SELL", D("0.7"), FILT, D("0.5"), D(10)) == []     # меньше шага — перенос


def test_child_below_min_notional_refused_unless_reduce_only():
    thin = Book(bids=((D("0.04093"), D(100)),), asks=((D("0.04094"), D(100)),), ts=0.0)
    with pytest.raises(P.PlanRefused, match="minNotional"):
        P.perp_children(thin, "SELL", D(400), FILT, alpha=D("0.5"), beta_bps=D(10))
    ok = P.perp_children(thin, "BUY", D(400), FILT, alpha=D("0.5"), beta_bps=D(10), reduce_only=True)
    assert sum(q for q, _ in ok) == 400 and len(ok) == 8
    with pytest.raises(P.PlanRefused, match="тоньше минимума"):
        P.perp_children(thin, "SELL", D(400), FILT, alpha=D("0.001"), beta_bps=D(10))


def test_perp_cost_refill_vs_walk():
    tok = D(12214)
    one = P.perp_children(BOOK, "BUY", tok, FILT, None, None, reduce_only=True)
    pc = P.perp_cost(one, BOOK, "BUY", FEE)
    walk_bps = pc.spread / pc.notional * 10000
    assert walk_bps == pytest.approx(D(18), abs=D(1))                       # отчёт clip: одна заявка — ~18 б.п.
    sliced = P.perp_children(BOOK, "BUY", tok, FILT, D("0.5"), D(30), reduce_only=True)
    refill = P.perp_cost(sliced, BOOK, "BUY", FEE)
    assert refill.spread / refill.notional * 10000 == pytest.approx(D("1.22"), abs=D("0.01"))
    no_refill = P.perp_cost(sliced, BOOK, "BUY", FEE, refill=False)
    assert no_refill.spread > 10 * refill.spread                      # не восстановится — съедим стакан


# ================================ ограничения плана ================================
def test_refusal_when_unhedged_cap_cannot_be_met():
    mkt = _mkt(sigma_1s=D("0.000114"))                               # 1-мин σ марка 0.088 % → за 1 с
    with pytest.raises(P.PlanRefused, match="unhedged_usd_max"):
        _plan(mkt=mkt, lim=P.Limits(clips_max=10, unhedged_usd_max=D("0.01")))
    # σ неизвестна при заданном пределе — не оценить, отказ (а не «риск 0»)
    with pytest.raises(P.PlanRefused, match="σ"):
        _plan(lim=P.Limits(clips_max=10, unhedged_usd_max=D(1)))


def test_unhedged_cap_forces_smaller_clips():
    mkt = _mkt(sigma_1s=D("0.000114"))
    free = _plan(mkt=mkt)
    assert free.est["n"] == 1 and free.est["unhedged_risk_usd"] == pytest.approx(D("0.2294"), abs=D("0.001"))
    capped = _plan(mkt=mkt, lim=P.Limits(clips_max=10, unhedged_usd_max=D("0.15")))
    assert capped.est["n"] == 2 and capped.est["unhedged_risk_usd"] <= D("0.15")
    assert capped.est["candidates"][0]["excluded"].startswith("риск голой ноги")


def test_clip_max_usd_number_binds_and_auto_does_not():
    assert _plan(lim=P.Limits(clips_max=10, clip_max_usd=D(100))).est["n"] == 5
    assert _plan(lim=P.Limits(clips_max=10, clip_max_usd="auto")).est["n"] == 1
    with pytest.raises(P.PlanRefused, match="clip_max_usd"):
        _plan(lim=P.Limits(clips_max=3, clip_max_usd=D(100)))


def test_gas_must_be_payable_from_native_balance():
    with pytest.raises(P.PlanRefused, match="натива"):
        _plan(mkt=_mkt(native_usd=D("0.01"), native_px=D("737.19")))
    # резерв владельца вычитается: 0.0029 BNB ≈ $2.14 при балансе ровно на резерв
    with pytest.raises(P.PlanRefused, match="натива"):
        _plan(mkt=_mkt(native_usd=D("2.14"), native_px=D("737.19")),
              lim=P.Limits(clips_max=10, native_reserve=D("0.0029")))
    ok = _plan(mkt=_mkt(native_usd=D("2.14"), native_px=D("737.19")))
    assert ok.est["gas_payable"] is True
    assert _plan().est["gas_payable"] is None                        # баланс неизвестен — не «оплачиваемо»


def test_small_deal_and_deal_cap_refused():
    with pytest.raises(P.PlanRefused, match="минимальной заявки"):
        _plan(usd=5)
    with pytest.raises(P.PlanRefused, match="deal_max_usd_per_leg"):
        _plan(usd=600, lim=P.Limits(clips_max=3, deal_max_usd=D(500)))


def test_owner_example_plans_in_dry_and_lists_missing_keys():
    lim = P.limits_from_owner(owner.load(EXAMPLE))
    assert lim.clip_max_usd == "auto" and lim.deal_max_usd == 500
    assert lim.alpha == lim.beta_bps == lim.clips_max == lim.unhedged_usd_max == lim.exec_time_max_s == "auto"
    assert lim.plan_cost_drift_pct == D("0.5") and lim.refill_wait_s == 30
    p = _plan(lim=lim, mkt=_mkt(sigma_1s=D("0.000114")))
    # «auto» владельца 12.09 для live задано — в списке остались только кошельки Aster и id владельца
    assert p.missing_owner_keys == ["telegram.owner_id", "wallets.aster_user", "wallets.aster_signer"]
    assert p.est["n"] == 1 and len(p.est["candidates"]) >= 2          # clips_max auto: перебор без потолка
    assert p.est["alpha"] in P.tconfig.AB_ALPHA_GRID and p.est["beta_auto"] is True
    assert p.est["unhedged_cap_usd"] == D("0.005") * p.est["clip_usd"]   # 0.5 % клипа
    assert p.inputs["entry_thresholds"]["funding_ok"] is None         # порог пуст — справка, не запрет


def test_plan_structure_units_and_json():
    p = _plan(lim=P.Limits(clips_max=10, clip_max_usd=D(150), alpha=D("0.05"), beta_bps=D(10)), now=1000.0)
    assert p.expires == 1060.0 and p.kind == "entry" and p.est["n"] == 4
    assert sum(c.dex_in_units for c in p.clips) == 500 * E18
    assert [c.seq for c in p.clips] == [1, 2, 3, 4]
    for c in p.clips:
        assert all(q == q.to_integral_value() and cap == D("0.04088") for q, cap in c.children)
    js = store.jdump(p)                                               # plan_json: Decimal строкой, без float
    assert '"deal_id":"E7K2"' in js and "E-" not in js                # Decimal без экспоненты (format "f")
    assert '"k":"0.000000435"' in js


# ================================ рабочие числа AIW3 (спека §7) ================================
def test_worked_aiw3_numbers():
    lim = P.Limits(clips_max=10, clip_max_usd="auto", alpha=D("0.5"), beta_bps=D(30))
    entry = _plan(lim=lim)
    assert entry.est["n"] == 1
    assert entry.est["dex_fee_usd"] + entry.est["dex_impact_usd"] + entry.est["gas_usd"] == D("0.16875")
    assert entry.est["perp_spread_usd"] == pytest.approx(D("0.061"), abs=D("0.001"))   # полуспред 1.2 б.п.
    assert entry.est["perp_fee_usd"] == pytest.approx(D("0.20"), abs=D("0.001"))
    assert entry.est["total_usd"] == pytest.approx(D("0.43"), abs=D("0.01"))           # «вход ≈ $0.43»
    sliced = _plan("exit", lim=lim, calib=_cal("exit"))
    assert sliced.est["total_usd"] == pytest.approx(D("0.43"), abs=D("0.01"))          # «≈ $0.43 нарезкой»
    assert sliced.est["m"] > 1 and sliced.est["perp_no_refill_usd"] > D("0.8")
    one = _plan("exit", lim=P.Limits(clips_max=1), calib=_cal("exit"))
    assert one.est["total_usd"] == pytest.approx(D("1.27"), abs=D("0.05"))             # «≈ $1.27 одной заявкой»
    assert one.est["m"] == 1 and one.clips[0].children[0][1] == D("0.04104")


def test_pacing_cost_only_penalises():
    assert P._pace("entry", D("0.0005"), D(100), 3, D(60)) > 0
    assert P._pace("exit", D("0.0005"), D(100), 3, D(60)) == 0          # затянуть выход — не «выгода»
    assert P._pace("exit", D("-0.0005"), D(100), 3, D(60)) > 0
    assert P._pace("entry", D("0.0005"), D(100), 1, D(60)) == 0


# ================================ report ================================
def test_formatting():
    assert R.fmt_num(D("1234567.891")) == "1\u00a0234\u00a0567.89"
    assert R.fmt_num(D("-0.06"), 2) == "−0.06" and R.fmt_num(D("0.05"), 3, sign=True) == "+0.050"
    assert R.fmt_qty(D(2057611)) == "2\u00a0057\u00a0611" and R.fmt_qty(D("0.5")) == "0.5"
    assert R.fmt_pct(D("0.0005")) == "+0.050\u00a0%" and R.fmt_num(None) == "—"
    assert R.short_addr("0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919") == "0xE4Eb…8919"


def test_plan_numbers():
    p = _plan(mkt=_mkt(funding_h=D("0.0005")), lim=P.Limits(clips_max=10, alpha=D("0.5"), beta_bps=D(30)))
    n = R.plan_numbers(p)
    assert n["dex_cost_usd"] == D("0.15875") and n["gas_usd"] == D("0.01")
    assert n["usd_per_h"] == D("0.25")                                # +0.05 %/ч на $500
    # одна формула окупаемости: (вход + выход) / фандинг в $/ч; выход — пока издержки входа по плану
    assert n["exit_est_usd"] == n["total_usd"]
    assert n["breakeven_h"] == pytest.approx(2 * n["total_usd"] / D("0.25"))
    assert n["missing"] == [] and n["n"] == 1


def test_payback_is_one_formula_everywhere():
    """Эталон владельца 12.09: план (0.14 + 0.14) / 0.072 = 3.9 ч, итог (0.13 + 0.14) / 0.072 = 3.8 ч — одна
    функция для плана, итога и «позиций»; фандинг против нас или неизвестен — окупаемости нет (не «0 ч»)."""
    assert R.payback_h(D("0.1397"), D("0.1397"), D("0.072")).quantize(D("0.1")) == D("3.9")
    assert R.payback_h(D("0.134"), D("0.1397"), D("0.0722")).quantize(D("0.1")) == D("3.8")
    assert R.payback_h(D("0.14"), D("0.14"), D(0)) is None and R.payback_h(D("0.14"), D("0.14"), D("-0.1")) is None
    assert R.payback_h(None, D(1), D(1)) is None and R.payback_h(D(1), D(1), None) is None
    assert R.payback_h(D("0.14"), D("0.14"), D("0.07"), received_usd=D(1)) == 0      # фандингом уже окупилось
    assert R.payback_h(D("0.14"), D("0.14"), D("0.07"), received_usd=D("0.14")) == 2


def _clips_entry():
    return [{"dex_in": str(300 * E18), "dex_out": str(7_300 * E18), "perp_qty": "7300", "perp_quote": "298.9",
             "state": "BALANCED"},
            {"dex_in": str(200 * E18), "dex_out": str(4_900_5 * E18 // 10), "perp_qty": "4900",
             "perp_quote": "200.6", "state": "BALANCED"},
            {"dex_in": None, "dex_out": None, "state": "PLANNED"}]


def test_progress_and_final_numbers():
    clips = _clips_entry()
    txs = [{"kind": "approve", "gas_used": 46000, "eff_gas_price": "50000000", "tx_hash": "0xa", "status": 1},
           {"kind": "swap", "gas_used": 150000, "eff_gas_price": "50000000", "tx_hash": "0xb", "status": 1},
           {"kind": "swap", "gas_used": 160000, "eff_gas_price": "50000000", "tx_hash": "0xc", "status": 1},
           {"kind": "swap", "gas_used": None, "eff_gas_price": None, "tx_hash": "0xd", "status": None}]
    fills = [{"qty": "7300", "quote_qty": "298.9", "commission_abs": "0.11956", "commission_asset": "USDT", "maker": 0},
             {"qty": "4900", "quote_qty": "200.6", "commission_abs": "0.08024", "commission_asset": "USDT", "maker": 1}]
    pr = R.progress_numbers(kind="entry", clips=clips, dec_token=18, dec_stable=18, n_planned=3, txs=txs,
                            native_px=D("737.19"), fee_taker=FEE)
    assert pr["clip"] == 2 and pr["dex_usd"] == 500 and pr["dex_tokens"] == D("12200.5")
    assert pr["imbalance"] == D("0.5") and pr["commission_est"] is True
    assert pr["commission_usd"] == D("499.5") * FEE
    fin = R.final_numbers(kind="entry", clips=clips, txs=txs, fills=fills, dec_token=18, dec_stable=18,
                          native_px=D("737.19"), ref_px=D("0.0409"), perp_mid_ref=D("0.040935"),
                          plan_total_usd=D("0.43"), est_exit_usd=D("0.43"), funding_h=D("0.0005"),
                          position={"liquidationPrice": "0.08", "markPrice": "0.04", "leverage": "1"},
                          started=100.0, finished=147.0)
    gas_swap = D(310000) * D(50000000) / D(10) ** 18
    assert fin["gas"]["swap_native"] == gas_swap and fin["gas"]["approve_native"] == D(46000) * D(50000000) / D(10) ** 18
    assert fin["perp"]["qty"] == 12200 and fin["perp"]["vwap"] == D("499.5") / 12200
    assert fin["perp"]["commission_usd"] == D("0.19980") and fin["perp"]["maker_share"] == D("200.6") / D("499.5")
    assert fin["dust"] == D("0.5")
    assert fin["impact_usd"] == 500 - D("12200.5") * D("0.0409")
    assert fin["perp_slip_usd"] == D("0.040935") * 12200 - D("499.5")
    assert fin["total_complete"] and fin["total_usd"] == (fin["impact_usd"] + fin["perp_slip_usd"]
                                                          + D("0.19980") + fin["gas"]["usd"])
    assert fin["breakeven_h"] == (fin["total_usd"] + D("0.43")) / (D("0.0005") * D("499.5"))
    assert fin["liq_dist_frac"] == 1 and fin["duration_s"] == 47.0 and fin["tx_hashes"] == ["0xb", "0xc"]
    # цена BNB неизвестна — газ не ноль, а неизвестен, и итог помечен неполным
    unk = R.final_numbers(kind="entry", clips=clips, txs=txs, fills=fills, dec_token=18, dec_stable=18,
                          native_px=None, ref_px=D("0.0409"), perp_mid_ref=D("0.040935"))
    assert unk["gas_usd"] is None and unk["unknown"] == ["gas_usd"] and unk["breakeven_h"] is None


def test_positions_unknown_stays_unknown():
    kw = dict(spot_units=12200 * E18 + E18 // 2, dec_token=18, spot_px=D("0.041"), mark=D("0.041"),
              unrealized=D("-0.5"), income_rows=[{"income": "0.25"}, {"income": "0.26"}], created=0.0, now=3 * 3600.0,
              book_tokens=D("12200.5"), book_short=D(12200), tol=D(0))
    ok = R.positions_numbers(position_amt=D(-12200), **kw)
    assert ok["short_qty"] == 12200 and ok["delta"] == D("0.5") and ok["matches"] is True
    assert ok["funding_usd"] == D("0.51") and ok["funding_count"] == 2 and ok["hold_h"] == 3
    unk = R.positions_numbers(position_amt=None, **kw)
    assert unk["short_qty"] is None and unk["delta"] is None and unk["matches"] is None
    bad = R.positions_numbers(position_amt=D(-10000), **kw)
    assert bad["matches"] is False


# ================================ «auto» владельца 12.09 ================================
AUTO_LIM = dict(alpha="auto", beta_bps="auto", clips_max="auto", clip_max_usd="auto")
# лучший бид 60 токенов ($2.46 < minNotional $5), следующие уровни — в 4, 5, 6 и 13 тиках
THIN_L1 = Book(bids=((D("0.04093"), D(60)), (D("0.04089"), D(20000)), (D("0.04088"), D(20000)),
                     (D("0.04087"), D(50000)), (D("0.04080"), D(100000))), asks=ASKS, ts=0.0)


def test_beta_candidates_are_book_levels_plus_tick_with_floor():
    tick_bps = FILT.tick / D("0.04093") * 10000                          # 2.44 б.п.
    cands = P.beta_candidates(THIN_L1, "SELL", FILT)
    assert cands[0] == (3 * tick_bps).quantize(D("0.0001"), ROUND_FLOOR)   # пол: 3 тика (> 5 б.п.)
    assert cands[1] == (5 * tick_bps).quantize(D("0.0001"), ROUND_FLOOR)   # L2 в 4 тиках + тик
    # кэп каждого кандидата — ровно k тиков: L2+1, L3+1, L4+1, L5+1 (β вниз к 0.0001 б.п., кэп наружу к тику)
    assert [P.beta_px(D("0.04093"), "SELL", FILT, b) for b in cands] == \
        [D("0.04090"), D("0.04088"), D("0.04087"), D("0.04086"), D("0.04079")]
    # тик мельче: пол — 5 б.п., уровни ближе пола схлопываются в один кандидат
    fine = Book(bids=((D("4.09300"), D(10)), (D("4.09299"), D(10)), (D("4.09280"), D(10))), asks=(), ts=0.0)
    assert P.beta_candidates(fine, "SELL", FILT) == [D(5)]


def test_auto_widens_beta_when_best_level_is_below_exchange_minimum():
    """Лучший бид мельче минимума биржи, ближние уровни рядом: числа α/β владельца отказали бы, auto расширяет β
    ровно до ближнего уровня (не дальше) и держит α минимальной."""
    with pytest.raises(P.PlanRefused, match="minNotional"):
        _plan(lim=P.Limits(clips_max=10, alpha=D("0.5"), beta_bps=D(10)), mkt=_mkt(book=THIN_L1))
    p = _plan(lim=P.Limits(**AUTO_LIM), mkt=_mkt(book=THIN_L1))
    floor, near = P.beta_candidates(THIN_L1, "SELL", FILT)[:2]
    assert p.est["beta_bps"] == near > floor and p.est["alpha"] == D("0.1") and p.est["ab_band"] is True
    rows = {(r["alpha"], r["beta_bps"]): r for r in p.est["ab_grid"]}
    assert "minNotional" in rows[(D("0.1"), floor)]["excluded"]        # на полу β — только $2.46 лучшего уровня
    assert all(cap == D("0.04088") for c in p.clips for _, cap in c.children)
    assert rows[(D("0.1"), near)]["total_usd"] < rows[(D("0.1"), P.beta_candidates(THIN_L1, "SELL", FILT)[2])]["total_usd"]
    # толстый лучший уровень: все α стоят одинаково — к меньшим дочерним (α 0.1), β на полу
    thick = _plan(lim=P.Limits(**AUTO_LIM))
    assert thick.est["beta_bps"] == P.beta_candidates(BOOK, "SELL", FILT)[0] and thick.est["alpha"] == D("0.1")


def test_auto_prefers_smaller_alpha_on_thin_book():
    """Тонкий стакан (1000 токенов на уровень через тик): крупная дочерняя проходит по уровням и стоит дороже —
    auto берёт наименьшую α, дочерние — не глубже лучшего уровня («не бить по стакану»)."""
    thin = Book(bids=tuple((D("0.04093") - D("0.00001") * i, D(1000)) for i in range(8)),
                asks=tuple((D("0.04094") + D("0.00001") * i, D(1000)) for i in range(8)), ts=0.0)
    p = _plan("exit", usd=100, calib=_cal("exit"), lim=P.Limits(**AUTO_LIM), mkt=_mkt(book=thin))
    assert p.est["alpha"] == D("0.1") and p.est["child_max_qty"] < 1000
    grid = {(r["alpha"], r["beta_bps"]): r["total_usd"] for r in p.est["ab_grid"]}
    b0 = p.est["beta_bps"]
    assert grid[(D("1"), b0)] > grid[(D("0.1"), b0)]                  # α = 1 бьёт по стакану — дороже
    assert p.est["total_usd"] == min(t for t in grid.values() if t is not None)


def test_auto_respects_executor_refill_retries():
    """Живой стакан AIW3 на выходе: в полосе пола β (L1 + L2) 3 001 токен из 12 214 — одним клипом исполнитель ждал
    бы пополнения 4 раза, а после 3 недоборов встаёт на HEDGE_DEFICIT. auto расширяет β до L3 и считает ожидания в
    длительность (refill_wait_max_s каждое)."""
    lim = P.Limits(**AUTO_LIM, refill_wait_s=D(30), exec_time_max_s="auto")
    ex = _plan("exit", calib=_cal("exit"), lim=lim)
    floor = P.beta_candidates(BOOK, "BUY", FILT)[0]
    assert ex.est["n"] == 1 and ex.est["beta_bps"] > floor
    assert 0 < ex.est["refill_waits"] <= P.tconfig.ASTER_IOC_PARTIAL_RETRIES
    assert all(r["n"] is None or r["n"] >= 2 for r in ex.est["ab_grid"] if r["beta_bps"] == floor)  # одним — нельзя
    assert ex.est["exec_expected_s"] == ex.est["unhedged_dt_s"] + 30 * ex.est["refill_waits"]
    # числа владельца — поведение прежнее: правила пополнений нет, ожидания только считаются
    num = _plan("exit", calib=_cal("exit"), lim=P.Limits(clips_max=10, alpha=D("0.5"), beta_bps=D(5)))
    assert num.est["n"] == 1 and num.est["refill_waits"] == 4 and num.est["ab_band"] is False


def test_auto_unhedged_cap_is_half_percent_of_clip():
    """unhedged_usd_max = "auto": предел = 0.5 % клипа (plan_cost_drift_pct), риск — прежний 3σ√Δt·q. Большой клип
    набирает больше дочерних (Δt растёт) и исключается; два клипа по $250 проходят."""
    mkt = _mkt(sigma_1s=D("0.0008"))
    lim = P.Limits(clips_max=10, alpha=D("0.05"), beta_bps=D(10), unhedged_usd_max="auto",
                   plan_cost_drift_pct=D("0.5"))
    p = _plan(mkt=mkt, lim=lim)
    one = p.est["candidates"][0]
    assert one["n"] == 1 and "unhedged_usd_max auto 0.5 % клипа = 2.50 $" in one["excluded"]
    assert p.est["n"] == 2 and p.est["unhedged_cap_usd"] == D("1.25") and p.est["unhedged_auto_pct"] == D("0.5")
    assert p.est["unhedged_risk_usd"] <= p.est["unhedged_cap_usd"]
    # без допуска владельца «auto» не разрешается: dry строит без предела (live — ключ в missing, owner.py)
    free = _plan(mkt=mkt, lim=dataclasses.replace(lim, plan_cost_drift_pct=None))
    assert free.est["n"] == 1 and free.est["unhedged_cap_usd"] is None
    with pytest.raises(P.PlanRefused, match="σ"):                    # σ неизвестна — предел не оценить
        _plan(lim=lim)


def test_exec_time_auto_is_computed_and_frozen_in_plan():
    lim = P.Limits(clips_max=20, alpha=D("0.5"), beta_bps=D(30), exec_time_max_s="auto", refill_wait_s=D(30))
    p = _plan(r=D(1), lim=lim)                                        # пул восстанавливается — 3 клипа
    e = p.est
    assert e["n"] == 3 and e["refill_waits"] == 0
    assert e["exec_expected_s"] == 3 * (e["unhedged_dt_s"] + P.CLIP_GAP_S) == D("185.4")   # 3 × (1.8 + 60) с
    assert e["exec_time_max_s"] == 3 * e["exec_expected_s"] + 60 == D("616.2") and e["exec_time_auto"] is True
    assert '"exec_time_max_s":"616.2"' in store.jdump(p)              # в plan_json — число, а не "auto"
    num = _plan(r=D(1), lim=dataclasses.replace(lim, exec_time_max_s=D(600)))
    assert num.est["exec_time_max_s"] == 600 and num.est["exec_time_auto"] is False
    assert _plan(lim=P.Limits(clips_max=10)).est["exec_time_max_s"] is None       # пусто — предела нет (dry)


def test_numeric_owner_values_behave_as_before():
    p = _plan(lim=P.Limits(clips_max=10, clip_max_usd=D(150), alpha=D("0.05"), beta_bps=D(10)))
    assert p.est["alpha"] == D("0.05") and p.est["beta_bps"] == D(10) and p.est["ab_band"] is False
    assert p.est["alpha_auto"] is False and p.est["beta_auto"] is False and p.est["ab_grid"] == []
    for c in p.clips:                                                 # дочерние — прежний perp_children с числами
        tok = sum(q for q, _ in c.children)
        assert c.children == P.perp_children(BOOK, "SELL", tok, FILT, D("0.05"), D(10))


def test_pick_perp_for_perp_only_exit_and_rehedge():
    lim = P.Limits(alpha="auto", beta_bps="auto", refill_wait_s=D(30), exec_time_max_s="auto")
    pk = P.pick_perp(BOOK, "BUY", D(12214), FILT, FEE, lim, reduce_only=True)
    assert pk.alpha == D("0.1") and pk.band and pk.waits <= P.tconfig.ASTER_IOC_PARTIAL_RETRIES
    assert pk.est()["exec_time_max_s"] == 3 * pk.expected_s + 60 and pk.est()["alpha_auto"] is True
    num = P.pick_perp(BOOK, "BUY", D(12214), FILT, FEE, P.Limits(alpha=D("0.5"), beta_bps=D(30)), reduce_only=True)
    assert num.children == P.perp_children(BOOK, "BUY", D(12214), FILT, D("0.5"), D(30), reduce_only=True)
    with pytest.raises(P.PlanRefused, match="minNotional"):           # число — ошибка стакана как раньше
        P.pick_perp(THIN_L1, "SELL", D(400), FILT, FEE, P.Limits(alpha=D("0.5"), beta_bps=D(10)))
    assert P.pick_perp(THIN_L1, "SELL", D(400), FILT, FEE, lim).beta_bps > P.beta_candidates(THIN_L1, "SELL", FILT)[0]


def test_liq_alert_threshold():
    assert P.liq_alert_pct("auto", D(80)) == 40 and P.liq_alert_pct("auto", None) is None
    assert P.liq_alert_pct(D(20), D(80)) == 20 and P.liq_alert_pct(None, D(80)) is None


def test_auto_plan_builds_fast():
    import time
    lim = P.Limits(**AUTO_LIM, unhedged_usd_max="auto", plan_cost_drift_pct=D("0.5"), refill_wait_s=D(30),
                   exec_time_max_s="auto")
    for kind in ("entry", "exit"):
        t = time.perf_counter()
        _plan(kind, calib=_cal(kind), lim=lim, mkt=_mkt(sigma_1s=D("0.000114")))
        # 1 с — на Маке; ireland (2 vCPU рядом с бэкфиллом коллектора) дал 1.9 с на выкате 12.09 — порог с запасом:
        # тест ловит взрыв перебора (минуты), а не разницу машин
        assert time.perf_counter() - t < 6.0, kind
