"""Шаг C2: планировщик клипа связки по реальной котировке победителя и стакану HL (planner.route_clip) — без кривой
k/r (R11); единицы Fs/Fp (U02, U03, U06), ёмкость хеджа с излишком (G09), нехватка глубины — отказ, а не «почти»."""
from decimal import Decimal as D
import pytest
from funding_bot.trade.planner import PlanRefused, route_clip
from funding_bot.trade.types import Book

BOOK = Book(bids=((D("0.16675"), D(1062)), (D("0.16655"), D(1799))), asks=((D("0.1672"), D(1500)),), ts=0.0)
DEEP = Book(bids=((D(100), D(10 ** 9)),), asks=((D(101), D(10 ** 9)),), ts=0.0)


def test_entry_clip_from_winner_quote_and_hl_bids():
    rc = route_clip(side="entry", amount_in_raw=150_000_000, dec_in=6, dec_out=6, expected_out_raw=903_000_000,
                    min_out_raw=893_970_000, external=D("0.000753"), book=BOOK, step=D(1), fs=D(1), fp=D(1),
                    fee_rate=D("0.000675"), cap_px=D("0.16575"), surplus_tokens=D(20))
    assert (rc.tokens, rc.tokens_min, rc.stable) == (D(903), D("893.97"), D(150))
    assert (rc.qty, rc.qty_min, rc.capacity, rc.residual_tokens) == (D(903), D(893), D(923), D(0))
    assert rc.perp_notional == D(903) * D("0.16675") and rc.perp_vwap == D("0.16675")
    assert rc.perp_fee == rc.perp_notional * D("0.000675")
    spot = D(150) / D(903)
    assert abs(rc.basis_gross_bps - (D("0.16675") / spot - 1) * 10000) < D("1e-20")
    all_in = ((rc.perp_notional - rc.perp_fee) / 903) / ((D(150) + D("0.000753")) / 903) * 10000 - 10000
    assert abs(rc.basis_all_in_bps - all_in) < D("1e-20") and rc.basis_all_in_bps < rc.basis_gross_bps
    assert rc.children == ((D(903), D("0.16575")),)


@pytest.mark.parametrize("tokens,fs,fp,qty,residual", [
    (100_000, D(1), D(1000), D(100), D(0)),         # U02: SELL 100, не 100 000
    (2550, D(1), D(1000), D(2), D(550)),            # U03: 2 контракта, 550 токенов переноса
    (2750, D(2), D(1000), D(5), D(250)),            # U06: Fs=2 — 5 контрактов, остаток 250 токенов (= 500 базы)
])
def test_units_fs_fp(tokens, fs, fp, qty, residual):
    rc = route_clip(side="entry", amount_in_raw=1_000_000, dec_in=6, dec_out=0, expected_out_raw=tokens,
                    min_out_raw=tokens, external=D(0), book=DEEP, step=D(1), fs=fs, fp=fp, fee_rate=D(0), cap_px=D(99))
    assert (rc.qty, rc.residual_tokens) == (qty, residual)


def test_depth_or_cap_not_enough_is_refusal_not_estimate():
    with pytest.raises(PlanRefused, match="не покрывает"):
        route_clip(side="entry", amount_in_raw=150_000_000, dec_in=6, dec_out=6, expected_out_raw=3_000_000_000,
                   min_out_raw=None, external=None, book=BOOK, step=D(1), fs=D(1), fp=D(1), fee_rate=None,
                   cap_px=D("0.1"))
    with pytest.raises(PlanRefused, match="не покрывает"):            # кэп выше лучшего бида — ни одного уровня
        route_clip(side="entry", amount_in_raw=150_000_000, dec_in=6, dec_out=6, expected_out_raw=903_000_000,
                   min_out_raw=None, external=None, book=BOOK, step=D(1), fs=D(1), fp=D(1), fee_rate=None,
                   cap_px=D("0.17"))


def test_unknown_fee_or_external_keeps_all_in_unknown():
    rc = route_clip(side="entry", amount_in_raw=150_000_000, dec_in=6, dec_out=6, expected_out_raw=903_000_000,
                    min_out_raw=None, external=None, book=BOOK, step=D(1), fs=D(1), fp=D(1), fee_rate=None,
                    cap_px=D("0.16"))
    assert rc.perp_fee is None and rc.basis_all_in_bps is None and rc.basis_gross_bps is not None


def test_exit_clip_buys_close_qty_on_asks():
    rc = route_clip(side="exit", amount_in_raw=903_000_000, dec_in=6, dec_out=6, expected_out_raw=150_200_000,
                    min_out_raw=148_698_000, external=D("0.000753"), book=BOOK, step=D(1), fs=D(1), fp=D(1),
                    fee_rate=D("0.000675"), cap_px=D("0.1682"), close_qty=D(903))
    assert (rc.side, rc.tokens, rc.stable, rc.stable_min, rc.qty) == ("exit", D(903), D("150.2"), D("148.698"), D(903))
    assert rc.perp_vwap == D("0.1672") and rc.residual_tokens == 0
    assert abs(rc.basis_gross_bps - (D("0.1672") / (D("150.2") / D(903)) - 1) * 10000) < D("1e-20")
