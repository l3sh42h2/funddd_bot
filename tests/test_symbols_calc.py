from funding_bot.symbols import norm_symbol_factor
from funding_bot import calc, config
from funding_bot.funding import expected_settlements

H = 3600_000


def test_norm_symbol_factor():
    assert norm_symbol_factor("1000PEPE") == ("PEPE", 1000.0)
    assert norm_symbol_factor("kPEPE") == ("PEPE", 1000.0)
    assert norm_symbol_factor("1000000MOG") == ("MOG", 1_000_000.0)
    assert norm_symbol_factor("PEPE") == ("PEPE", 1.0)
    assert norm_symbol_factor("KAITO") == ("KAITO", 1.0)      # заглавная K — не префикс
    assert norm_symbol_factor("1000CAT") == ("CAT", 1000.0)
    assert norm_symbol_factor("1MBABYDOGE") == ("BABYDOGE", 1_000_000.0)   # перп Binance: миллион BABYDOGE
    assert norm_symbol_factor("1INCH") == ("1INCH", 1.0) and norm_symbol_factor("1MIL") == ("1MIL", 1.0)


def test_venue_label_shows_hip3_dex():
    assert calc.label("hyperliquid", "xyz:NATGAS") == "hyperliquid·xyz"
    assert calc.label("hyperliquid", "BTC") == "hyperliquid" and calc.label("binance", "NATGASUSDT") == "binance"
    assert calc.url("hyperliquid", "xyz:NATGAS", "NATGAS") == "https://app.hyperliquid.xyz/trade/xyz:NATGAS"


def test_hourly():
    assert calc.hourly(0.0008, 8) == 0.0001
    assert calc.hourly(0.0001, 1) == 0.0001
    assert calc.hourly(None, 8) is None
    assert calc.hourly(0.001, 0) is None


def test_window_sums_mixed_intervals_and_empty():
    now = 1000 * H
    ev8 = [(now - 16 * H, 0.001), (now - 8 * H, 0.002), (now, 0.003)]        # три расчёта по 8ч
    ws = calc.window_sums(ev8, now)
    assert ws[4] == (0.003, 1)                                  # только тот, что ровно сейчас
    assert ws[24] == (0.006, 3)
    assert calc.window_sums(ev8, now - 1)[4] == (None, 0)       # окно без расчётов — None, не 0
    ev1 = [(now - k * H, 0.0001) for k in range(800)]
    ws = calc.window_sums(ev1, now)
    assert ws[24][1] == 24 and abs(ws[24][0] - 0.0024) < 1e-12
    assert ws[720][1] == 720 and ws[168][1] == 168 and ws[72][1] == 72


def test_expected_settlements_aligned_with_grace():
    now = 96 * H + 5 * 60_000                  # 96:05, сетка 8ч: …80, 88, 96, 104
    assert expected_settlements(8, now, 24, grace_s=600) == [80 * H, 88 * H]          # 96ч ещё в льготе, 72ч = граница окна
    assert expected_settlements(8, now, 24, grace_s=0) == [80 * H, 88 * H, 96 * H]
    assert expected_settlements(8, now + 6 * 60_000, 24, grace_s=600) == [80 * H, 88 * H, 96 * H]   # 96:11 — льгота прошла
    assert expected_settlements(4, now, 4, grace_s=600) == []                         # 96ч в льготе, 92ч = граница окна
    assert expected_settlements(1, now, 4, grace_s=600) == [93 * H, 94 * H, 95 * H]
    assert expected_settlements(0, now, 4) == []


AB_ABC = dict(key="aster|binance:ABC", base="ABC", va="aster", vb="binance", sa="ABCUSDT", sb="ABCUSDT", fa=1.0, fb=1.0)


def _ff(item=None, **kw):
    args = dict(ins_a={"interval_h": 8}, ins_b={"interval_h": 4}, prem_a={"rate": 0.0008, "mark": 10.0},
                prem_b={"rate": 0.0002, "mark": 10.0}, book_a={"bid": 9.99, "ask": 10.01}, book_b={"bid": 9.995, "ask": 10.005},
                ws_a={}, ws_b={}, now_ms=1000 * H, comp_a=None, comp_b=None)
    args.update(kw)
    return calc.build_ff_row(item or AB_ABC, **args)


def test_ff_spread_per_period_side_fee_urls():
    row = _ff()
    assert abs(row["rate_h_a"] - 0.0001) < 1e-12 and abs(row["rate_h_b"] - 0.00005) < 1e-12
    assert row["period"] == 8 and abs(row["spread"] - 0.00005) < 1e-12           # час A − час B («приводи к 1ч»)
    assert row["side"] == "short_a"
    assert abs(row["fee"] - 2 * (0.0004 + 0.0005)) < 1e-12                       # круг тейкером на обеих ногах
    assert row["urls"][0].endswith("/pro/futures/ABCUSDT") and row["urls"][1].endswith("/en/futures/ABCUSDT")
    assert "dev" not in row                                                      # владелец 12.09: «откл не нужно»
    for w in config.WINDOWS_H:
        assert row["windows"][str(w)]["spread"] is None and row["windows"][str(w)]["na"] == 0
    # равные интервалы: разность ставок, делённая на интервал
    row = _ff(ins_a={"interval_h": 4}, ins_b={"interval_h": 4}, prem_a={"rate": 0.0003}, prem_b={"rate": 0.0005})
    assert row["period"] == 4 and abs(row["spread"] - (-0.00005)) < 1e-12 and row["side"] == "short_b"
    row = _ff(ins_a={"interval_h": 4}, ins_b={"interval_h": 4}, prem_a={"rate": 0.0005}, prem_b={"rate": 0.0001},
              ws_a={24: (0.0024, 6)}, ws_b={24: (0.0012, 6)})
    assert abs(row["windows"]["24"]["spread"] - 0.0012) < 1e-12


def test_ff_pair_with_hyperliquid():
    item = dict(key="binance|hyperliquid:BTC", base="BTC", va="binance", vb="hyperliquid", sa="BTCUSDT", sb="BTC", fa=1.0, fb=1.0)
    row = _ff(item, ins_a={"interval_h": 8}, ins_b={"interval_h": 1}, prem_a={"rate": 0.0008}, prem_b={"rate": 0.00002})
    assert row["period"] == 8 and (row["iv_a"], row["iv_b"]) == (8, 1)
    assert abs(row["spread"] - (0.0001 - 0.00002)) < 1e-12 and row["side"] == "short_a"
    assert abs(row["rate_h_b"] - 0.00002) < 1e-15                              # часовая биржа не проигрывает 8-часовой в 8 раз
    assert abs(row["fee"] - 2 * (0.0005 + 0.00045)) < 1e-12
    assert row["urls"][1] == "https://app.hyperliquid.xyz/trade/BTC"


def test_ff_gap_uses_per_token_price_with_factors_and_mark_fallback():
    item = dict(AB_ABC, key="aster|hyperliquid:PEPE", base="PEPE", vb="hyperliquid", sa="1000PEPEUSDT", sb="kPEPE", fa=1000.0, fb=1000.0)
    row = _ff(item, book_a={"bid": 0.0099, "ask": 0.0101}, book_b={"bid": 0.0099, "ask": 0.0101})
    assert abs(row["gap"]) < 1e-9
    item = dict(item, fb=1.0)                             # вторая площадка котирует за один токен, первая за тысячу
    row = _ff(item, book_a={"bid": 9.9, "ask": 10.1}, book_b={"bid": 0.0099, "ask": 0.0101})
    assert abs(row["gap"]) < 1e-6
    # книги нет — курс по маркам, а не «—»
    row = _ff(item, prem_a={"rate": 0, "mark": 10.2}, prem_b={"rate": 0, "mark": 0.01}, book_a=None, book_b=None)
    assert abs(row["gap"] - 0.02) < 1e-9 and row["px_a"] == 10.2


def test_sf_row_spot_long_perp_short():
    item = dict(key="binance_spot:PEPEUSDT|binance:1000PEPEUSDT", base="PEPE", spot_ex="binance_spot", spot="PEPEUSDT",
                spot_asset="PEPE", spot_factor=1.0, perp_ex="binance", perp="1000PEPEUSDT", perp_factor=1000.0)
    row = calc.build_sf_row(item, {"interval_h": 4}, {"rate": 0.0002, "mark": 0.0102, "next_ms": 5},
                            {"bid": 0.0100, "ask": 0.0102}, {"bid": 0.0000099, "ask": 0.0000101},
                            {24: (0.0006, 6)}, 1000 * H, dict(first_ms=900 * H, latest_missing=False, holes=[], shallow=[]))
    assert row["px_src_perp"] == "book" and row["px_src_spot"] == "book"
    assert abs(row["spread"] - 0.00005) < 1e-15 and row["rate"] == 0.0002 and row["period"] == 4   # в час, ставка биржи сохранена
    assert abs(row["gap"] - 0.01) < 1e-9                        # перп 1.01e-5 за токен против спота 1e-5
    assert "dev" not in row
    assert abs(row["fee"] - 2 * (0.0010 + 0.0005)) < 1e-12       # спот ×2 + перп Binance ×2
    assert row["urls"]["spot"].endswith("/trade/PEPE_USDT") and row["urls"]["perp"].endswith("/futures/1000PEPEUSDT")
    assert row["spot_label"] == "binance" and row["spot_ex"] == "binance_spot"
    assert row["windows"]["24"]["spread"] == 0.0006 and row["windows"]["4"]["spread"] is None
    # нет книг и ставки: цена перпа — марк, курсового нет, текущего нет, период по умолчанию
    row = calc.build_sf_row(item, None, {"rate": None, "mark": 0.0102}, None, None, {}, 1000 * H, None)
    assert row["gap"] is None and row["px_perp"] == 0.0102 and row["period"] == 8 and row["spread"] is None
    item_h = dict(item, perp_ex="hyperliquid", perp="kPEPE", key="binance_spot:PEPEUSDT|hyperliquid:kPEPE")
    row = calc.build_sf_row(item_h, {"interval_h": 1}, None, None, None, {}, 0)
    assert abs(row["fee"] - 2 * (0.0010 + 0.00045)) < 1e-12 and row["urls"]["perp"].endswith("/trade/kPEPE")
    # спот другой площадки: своя комиссия, своя подпись и своя страница токена
    for sv in config.SPOT_VENUES[1:]:
        it = dict(item_h, spot_ex=sv, spot="PEPE_USDT", key=f"{sv}:PEPE_USDT|hyperliquid:kPEPE")
        row = calc.build_sf_row(it, {"interval_h": 1}, None, None, None, {}, 0)
        assert abs(row["fee"] - 2 * (config.FEES_TAKER[sv] + 0.00045)) < 1e-12 and row["spot_label"] == config.LABELS[sv]
        if config.URLS.get(sv):
            assert row["urls"]["spot"].startswith("https://") and "PEPE" in row["urls"]["spot"]
        else:
            assert row["urls"]["spot"] is None          # 12.09: у Lighter на Robinhood Chain страницы рынка нет — без ссылки


def test_incomplete_from_holes_shallow_and_latest_missing():
    now = 1000 * H
    ca = dict(latest_missing=False, holes=[(now - 100 * H, now - 80 * H)], shallow=[])
    cb = dict(latest_missing=False, holes=[], shallow=[])
    row = _ff(comp_a=ca, comp_b=cb)
    assert not row["windows"]["24"]["incomplete"] and not row["windows"]["72"]["incomplete"]
    assert row["windows"]["168"]["incomplete"] and row["windows"]["720"]["incomplete"]   # дыра 80-100 ч назад
    cb["shallow"] = [720]                                                                # история короче месяца, глубина не подтверждена
    row = _ff(comp_a=dict(ca, holes=[]), comp_b=cb)
    assert [w for w in config.WINDOWS_H if row["windows"][str(w)]["incomplete"]] == [720]
    cb["latest_missing"] = True
    row = _ff(comp_a=ca, comp_b=cb)
    assert all(row["windows"][str(w)]["incomplete"] for w in config.WINDOWS_H)         # нет последнего — неполно всё
    item = dict(key="aster:X", base="X", spot="XUSDT", spot_factor=1.0, perp_ex="aster", perp="XUSDT", perp_factor=1.0)
    row = calc.build_sf_row(item, None, None, None, None, {}, now, dict(latest_missing=False, holes=[], shallow=[168, 720]))
    assert [w for w in config.WINDOWS_H if row["windows"][str(w)]["incomplete"]] == [168, 720]
