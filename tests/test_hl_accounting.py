"""Поток hl: учёт — fills по времени с перекрытием на живой странице из 2000 fills (пачки на одном timestamp),
фактическая комиссия вместо оценки (H15), funding со знаком и дедупом перекрытия (H16), ключи по scope счёта/dex
и идемпотентный повтор страницы (H17), gap вместо «полной истории»."""
from decimal import Decimal as D
import pytest
from funding_bot.trade import hl_rules as R
from funding_bot.trade import hyperliquid_trade as ht
from funding_bot.trade.hyperliquid_trade import HlError, HyperliquidTrade
import hl_support as S

A = "para:ANSEM"


def trade(account):
    clock = S.Clock()
    fake = S.FakeHL(clock)
    return HyperliquidTrade(account, session=fake, now=clock, sleep=clock.sleep), fake


def test_live_2000_fill_page_paginates_from_last_time_inclusive():
    t, fake = trade(S.U0)
    src = S.PUB["user_0"]["userFillsByTime"]
    rows = src["data"]
    # свойство записанного ответа: самые ранние 2000 с startTime, по возрастанию, до «сейчас» ещё далеко
    assert len(rows) == 2000 and rows[0]["time"] >= src["payload"]["startTime"]
    assert all(rows[i]["time"] <= rows[i + 1]["time"] for i in range(1999))
    p = t.fills_since(A, src["payload"]["startTime"])
    calls = fake.info_calls("userFillsByTime")
    assert len(calls) == 2 and calls[1]["startTime"] == rows[-1]["time"]            # без +1
    assert all(c["aggregateByTime"] is False and c["user"] == S.U0.lower() for c in calls)
    assert p.complete and p.gap is None and len(p.rows) == 2000
    assert len({R.fill_key(r) for r in p.rows}) == 2000
    same_ts = sum(1 for i in range(1999) if rows[i]["time"] == rows[i + 1]["time"])
    assert same_ts > 200                                                          # пачки на одном времени — живые
    assert sum(r["fee"] for r in p.rows) == sum(D(r["fee"]) for r in rows)


def test_h15_actual_fees_replace_estimate_builder_fee_not_double():
    rows = [R.fill_row(S.fill(1, "500.0", "0.16", 1000, 11, fee="0.04"), network="mainnet", account=S.MASTER),
            R.fill_row(S.fill(1, "500.0", "0.16", 1000, 12, fee="0.06", builder_fee="0.01"), network="mainnet",
                       account=S.MASTER)]
    assert R.fee_totals(rows) == {"USDC": D("0.10")}                  # builderFee уже внутри fee
    assert rows[1]["builder_fee"] == D("0.01")
    est = R.taker_rate_estimate(D("0.00045"), R.resolve_asset(S.PUB["perpDexs"]["data"], S.PUB["meta_para"]["data"], A))
    assert est * D(1000) * D("0.16") != R.fee_totals(rows)["USDC"]    # оценка — не факт; факт — из fills
    with pytest.raises(R.HlRuleError):
        R.fee_totals([{**rows[0], "fee_token": None}])


def test_h16_funding_signs_and_overlap_dedup(monkeypatch):
    t, fake = trade(S.MASTER)
    ev = [{"time": 1789300800000, "hash": "0x" + "0" * 64,
           "delta": {"type": "funding", "coin": A, "usdc": "0.20", "szi": "-1000.0", "fundingRate": "0.0002",
                     "nSamples": None}},
          {"time": 1789304400000, "hash": "0x" + "0" * 64,
           "delta": {"type": "funding", "coin": A, "usdc": "-0.05", "szi": "-1000.0", "fundingRate": "-0.00005",
                     "nSamples": None}}]
    monkeypatch.setattr(ht, "FUNDING_PAGE", 2)
    fake.user(S.MASTER, "userFunding", lambda b: [e for e in ev if e["time"] >= b["startTime"]][:2])
    p = t.funding_since(A, 1789300000000)
    assert p.complete and len(p.rows) == 2 and p.pages == 2                      # вторая страница повторила событие
    assert sum(r["usdc"] for r in p.rows) == D("0.15")
    assert t.funding_income(A, 1789300000000) == p.rows


def test_live_funding_fixture_signs_kept():
    t, _ = trade(S.U0)
    src = S.PUB["user_0"]["userFunding"]
    rows = t.funding_income(A, src["payload"]["startTime"])
    assert len(rows) == 48 and sum(r["usdc"] for r in rows) == sum(D(e["delta"]["usdc"]) for e in src["data"])
    assert any(r["usdc"] < 0 for r in rows) and any(r["usdc"] > 0 for r in rows)
    assert len({R.funding_key(r) for r in rows}) == 48


def test_h17_scopes_and_idempotent_repeat_and_collision():
    a = R.fill_row(S.fill(1, "1.0", "0.16", 1000, 42), network="mainnet", account=S.MASTER)
    b = R.fill_row(S.fill(1, "1.0", "0.16", 1000, 42), network="mainnet", account=S.SUB)
    c = R.fill_row(S.fill(1, "1.0", "0.16", 1000, 42, coin="xyz:TSLA"), network="mainnet", account=S.MASTER)
    assert len({R.fill_key(x) for x in (a, b, c)}) == 3                          # один tid в разных scope — разные
    pages = [[S.fill(2, "1.0", "0.16", 2000, 50), S.fill(1, "1.0", "0.16", 1000, 42)],   # порядок внутри — любой
             [S.fill(2, "1.0", "0.16", 2000, 50), S.fill(3, "1.0", "0.16", 3000, 51)],
             [S.fill(3, "1.0", "0.16", 3000, 51)]]
    it = iter(pages)
    p = R.paginate_by_time(lambda cur: next(it), 0, page_limit=2, parse=lambda x: R.fill_row(
        x, network="mainnet", account=S.MASTER), key=R.fill_key, max_pages=5, collide=("oid", "hash"))
    assert [r["tid"] for r in p.rows] == [42, 50, 51] and p.complete and p.pages == 3
    it = iter(pages)
    p = R.paginate_by_time(lambda cur: next(it), 0, page_limit=2, parse=lambda x: R.fill_row(
        x, network="mainnet", account=S.MASTER), key=R.fill_key, max_pages=2, collide=("oid", "hash"))
    assert not p.complete and "больше 2 страниц" in p.gap
    rep = [[S.fill(1, "1.0", "0.16", 1000, 42), S.fill(2, "1.0", "0.16", 2000, 50)],
           [S.fill(2, "1.0", "0.16", 2000, 50)]]
    it = iter(rep)
    p = R.paginate_by_time(lambda cur: next(it), 0, page_limit=2, parse=lambda x: R.fill_row(
        x, network="mainnet", account=S.MASTER), key=R.fill_key, max_pages=5, collide=("oid", "hash"))
    assert p.complete and [r["tid"] for r in p.rows] == [42, 50]                  # повтор страницы — без дублей
    bad = [[S.fill(1, "1.0", "0.16", 1000, 42), S.fill(2, "1.0", "0.16", 2000, 50)],
           [S.fill(9, "1.0", "0.16", 2000, 50)]]
    it = iter(bad)
    with pytest.raises(R.HlRuleError, match="коллизия"):
        R.paginate_by_time(lambda cur: next(it), 0, page_limit=2, parse=lambda x: R.fill_row(
            x, network="mainnet", account=S.MASTER), key=R.fill_key, max_pages=5, collide=("oid", "hash"))


def test_other_coin_kept_for_completeness_then_filtered():
    t, fake = trade(S.MASTER)
    fake.user(S.MASTER, "userFillsByTime", [S.fill(1, "1.0", "0.16", 1000, 1),
                                            S.fill(2, "1.0", "100.0", 1001, 2, coin="xyz:TSLA"),
                                            S.fill(3, "2.0", "0.16", 1002, 3)])
    p = t.fills_since(A, 0)
    assert p.complete and [r["tid"] for r in p.rows] == [1, 3]
    assert len(t.acct.fills_since(0).rows) == 3


def test_gap_single_timestamp_page_and_cap(monkeypatch):
    t, fake = trade(S.MASTER)
    fake.user(S.MASTER, "userFillsByTime", [S.fill(i, "1.0", "0.16", 5000, 100 + i) for i in range(ht.FILLS_PAGE)])
    p = t.fills_since(A, 0)
    assert not p.complete and "одном времени" in p.gap
    monkeypatch.setattr(ht, "FILLS_CAP", 3)
    fake.user(S.MASTER, "userFillsByTime", [S.fill(i, "1.0", "0.16", 5000 + i, 100 + i) for i in range(5)])
    p = t.fills_since(A, 0)
    assert not p.complete and "предел" in p.gap
    monkeypatch.setattr(ht, "FILLS_CAP", 10_000)
    monkeypatch.setattr(ht, "FUNDING_PAGE", 1)
    fake.user(S.MASTER, "userFunding", lambda b: [{"time": 7, "hash": "0x0", "delta": {
        "type": "funding", "coin": A, "usdc": "1", "szi": "-1", "fundingRate": "0.1"}}])
    with pytest.raises(HlError, match="неполная"):
        t.funding_income(A, 0)
