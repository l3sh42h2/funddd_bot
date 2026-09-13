"""Поток hl: чистые правила Hyperliquid — asset id по сырым спискам (H01/H02), цена и размер (H06–H08, G15), комиссия
HIP-3, msgpack и подпись против эталона официального SDK 0.24.0, cloid (H13), разбор ответов /exchange (H10/H12).
Ожидаемые значения — из ТЗ/ACCEPTANCE, записанных ответов (tests/data/hl) и эталонов SDK, не из проверяемых функций."""
import copy, dataclasses, hashlib, pathlib
from decimal import Decimal as D
import pytest
import funding_bot
from funding_bot.trade import hl_rules as R
from funding_bot.trade.hyperliquid_trade import HlSigner
from hl_support import AGENT, GOLD, MASTER, PUB, TEST_KEY

eth_account = pytest.importorskip("eth_account")
from eth_account import Account                                   # noqa: E402
from eth_account.messages import encode_typed_data                # noqa: E402

DEXS, META = PUB["perpDexs"]["data"], PUB["meta_para"]["data"]


def _ref(coin="para:ANSEM", dexs=None, meta=None):
    return R.resolve_asset(DEXS if dexs is None else dexs, META if meta is None else meta, coin)


def test_code_is_from_this_tree():
    assert pathlib.Path(funding_bot.__file__).resolve().parents[2] == pathlib.Path(__file__).resolve().parents[1]


# --- H01/H02: asset id и идентичность ---------------------------------------------------------------
def test_h01_asset_id_from_raw_lists_both_live_snapshots():
    for dexs, meta in ((DEXS, META), (PUB["fresh"]["perpDexs"]["data"], PUB["fresh"]["meta_para"]["data"])):
        r = R.resolve_asset(dexs, meta, "para:ANSEM")
        assert (r.asset, r.dex_index, r.local_index, r.dex, r.fullcoin) == (180025, 8, 25, "para", "para:ANSEM")
        assert (r.sz_decimals, r.max_leverage, r.only_isolated, r.margin_mode, r.margin_table_id) == (0, 3, True,
                                                                                                       "noCross", 3)
        assert r.collateral_token == 0 and r.deployer_fee_scale == D("0.5") and r.growth_mode is None
        assert not r.is_delisted
    # независимая формула SDK Info: 110000 + i·10000 по perp_dexs()[1:]
    names = [d["name"] for d in DEXS[1:]]
    assert 110000 + names.index("para") * 10000 + 25 == 180025


def test_h01_exact_namespace_never_main_dex_lookalike():
    with pytest.raises(R.HlRuleError):            # на основном dex ANSEM нет (живой снимок 13.09)
        R.resolve_asset(DEXS, PUB["meta_main"]["data"], "ANSEM")
    for bad in ("para:ansem", "ANSEM", "PARA:ANSEM", " para:ANSEM", "para:", ":ANSEM"):
        with pytest.raises(R.HlRuleError):
            _ref(bad)
    main = copy.deepcopy(PUB["meta_main"]["data"])     # синтетика: «похожий» ANSEM появился на основном dex
    main["universe"].append({"name": "ANSEM", "szDecimals": 0, "maxLeverage": 3})
    m = R.resolve_asset(DEXS, main, "ANSEM")
    assert m.dex == "" and m.asset == len(main["universe"]) - 1 and m.asset != 180025
    assert _ref().asset == 180025 and m.identity_hash != _ref().identity_hash


def test_h02_reordered_or_filtered_metadata_is_identity_change():
    saved = _ref()
    dexs = copy.deepcopy(DEXS)
    dexs[8], dexs[9] = dexs[9], dexs[8]
    moved = R.resolve_asset(dexs, META, "para:ANSEM")
    assert moved.asset == 190025
    with pytest.raises(R.HlIdentityChanged, match="asset"):
        R.check_same_identity(saved, moved)
    meta = copy.deepcopy(META)                          # фильтр строки до ANSEM сдвигает ordinal — это смена
    del meta["universe"][0]
    with pytest.raises(R.HlIdentityChanged):
        R.check_same_identity(saved, R.resolve_asset(DEXS, meta, "para:ANSEM"))
    meta = copy.deepcopy(META)                          # пометка isDelisted у соседа индекс НЕ меняет
    meta["universe"][0]["isDelisted"] = True
    R.check_same_identity(saved, R.resolve_asset(DEXS, meta, "para:ANSEM"))
    meta = copy.deepcopy(META)                          # szDecimals — другие единицы
    meta["universe"][25]["szDecimals"] = 1
    with pytest.raises(R.HlIdentityChanged, match="sz_decimals"):
        R.check_same_identity(saved, R.resolve_asset(DEXS, meta, "para:ANSEM"))
    meta = copy.deepcopy(META)                          # fee scale — правило, не идентичность
    meta["universe"][25]["deployerFeeScale"] = "0.7"
    fresh = R.resolve_asset(DEXS, meta, "para:ANSEM")
    R.check_same_identity(saved, fresh)
    assert fresh.rules_hash != saved.rules_hash


def test_perp_dexs_format_guards():
    with pytest.raises(R.HlRuleError, match="null"):
        R.resolve_asset(DEXS[1:], META, "para:ANSEM")
    with pytest.raises(R.HlRuleError, match="2 раз"):
        R.resolve_asset(DEXS + [{"name": "para"}], META, "para:ANSEM")
    with pytest.raises(R.HlRuleError, match="universe"):
        R.resolve_asset(DEXS, {}, "para:ANSEM")


# --- цена и размер ---------------------------------------------------------------------------------
def test_g15_ansem_price_and_size_rules():
    assert not R.valid_px(D("0.158381"), 0)
    assert R.quantize_px(D("0.158381"), 0, "SELL") == D("0.15839")
    assert R.quantize_px(D("0.158381"), 0, "BUY") == D("0.15838")
    assert R.valid_px(D("100000"), 0) and R.valid_px(D("123456"), 0)      # целые — всегда
    assert R.valid_px(D("0.15838"), 0) and R.valid_px(D("1234.5"), 0)
    assert not R.valid_sz(D("1000.1"), 0) and R.valid_sz(D("1000"), 0)
    assert R.floor_sz(D("1000.1"), 0) == D(1000)
    assert R.quantize_px(D("12345.6"), 0, "SELL") == D(12346) and R.quantize_px(D("12345.6"), 0, "BUY") == D(12345)
    assert R.tick_at(D("0.1667"), 0) == D("0.00001")


def test_h07_conservative_direction():
    assert R.quantize_px(D("123.456"), 2, "SELL") == D("123.46")
    assert R.quantize_px(D("123.456"), 2, "BUY") == D("123.45")
    assert R.quantize_px(D("123.45"), 2, "SELL") == D("123.45")          # уже допустимая — без сдвига


def test_h08_sig_digits_and_decimals_both_checked():
    # szDecimals 2 → ≤ 4 знаков: 0.12345 проходит «5 значащих», но не «4 знака»
    assert not R.valid_px(D("0.12345"), 2)
    assert (R.quantize_px(D("0.12345"), 2, "SELL"), R.quantize_px(D("0.12345"), 2, "BUY")) == (D("0.1235"), D("0.1234"))
    # 1.23456 проходит «≤ 4 знака»? нет, но и 6 значащих — нарушает оба
    assert (R.quantize_px(D("1.23456"), 2, "SELL"), R.quantize_px(D("1.23456"), 2, "BUY")) == (D("1.2346"), D("1.2345"))
    assert (R.quantize_px(D("0.0000123"), 0, "SELL"), R.quantize_px(D("0.0000123"), 0, "BUY")) == (D("0.000013"),
                                                                                                     D("0.000012"))
    assert (R.quantize_px(D("0.35"), 5, "SELL"), R.quantize_px(D("0.35"), 5, "BUY")) == (D("0.4"), D("0.3"))
    assert R.quantize_px(D("0.0000001"), 0, "SELL") == D("0.000001")
    with pytest.raises(R.HlRuleError, match="не представима"):
        R.quantize_px(D("0.0000001"), 0, "BUY")
    assert R.quantize_px(D("9.999951"), 0, "SELL") == D(10)                # через степень 10 — целая
    # граница бага SDK _slippage_price (HIP-3 как spot → 8 знаков): у перпа только 6 − szDecimals
    assert not R.valid_px(D("0.0094905"), 0) and R.valid_px(D("0.0094905"), 0, spot=True)
    assert (R.quantize_px(D("0.0094905"), 0, "SELL"), R.quantize_px(D("0.0094905"), 0, "BUY")) == (D("0.009491"),
                                                                                                      D("0.00949"))
    for bad in (0.1583, D(0), D(-1), D("NaN")):
        with pytest.raises(R.HlRuleError):
            R.quantize_px(bad, 0, "SELL")
    with pytest.raises(R.HlRuleError):
        R.quantize_px(D("0.1"), 0, "sell")


def test_h06_size_floor_never_up():
    assert R.floor_sz(D("1.2345678"), 6) == D("1.234567")
    assert R.floor_sz(D("0.9"), 0) == 0 and not R.valid_sz(R.floor_sz(D("0.9"), 0), 0)   # 0 → заявки нет
    assert R.floor_sz(D("1234.567890"), 0) == D(1234)
    with pytest.raises(R.HlRuleError):
        R.floor_sz(D("-1"), 0)
    with pytest.raises(R.HlRuleError):
        R.floor_sz(1.5, 0)


def test_wire_matches_sdk_float_to_wire():
    for n, w in GOLD["float_to_wire"].items():
        assert R.wire(D(n)) == w
    assert R.wire(D("1E+3")) == "1000" and R.wire(D("0.1500")) == "0.15" and R.wire(D("0")) == "0"
    with pytest.raises(TypeError):
        R.wire(1.5)
    with pytest.raises(R.HlRuleError):
        R.wire(D(-1))


# --- комиссии и прогноз фандинга ---------------------------------------------------------------------
def test_hip3_fee_scale_and_estimate():
    assert R.hip3_fee_scale(D("0.5")) == D("1.5") and R.hip3_fee_scale(D("1.0")) == 2
    assert R.hip3_fee_scale(D(0)) == 1 and R.hip3_fee_scale(D(2)) == 4
    ref = _ref()
    assert R.taker_rate_estimate(D("0.00045"), ref) == D("0.000675")          # ТЗ: 0.045 % × 1.5
    assert R.taker_rate_estimate(D("0.00045"), dataclasses.replace(ref, growth_mode="enabled")) is None
    assert R.taker_rate_estimate(D("0.00045"), dataclasses.replace(ref, dex="")) == D("0.00045")
    with pytest.raises(R.HlRuleError):
        R.hip3_fee_scale(None)


def test_h18_hourly_forecast_not_divided_by_8():
    assert R.hourly_funding_usd(D("0.001"), D(200)) == D("0.2")
    live = PUB["fresh"]["metaAndAssetCtxs_para"]["data"][1][25]["funding"]
    assert R.hourly_funding_usd(live, D(1000)) == D(live) * 1000


# --- msgpack и подпись против SDK --------------------------------------------------------------------
def _value(v):
    if isinstance(v, dict) and "gen" in v:
        return eval(v["gen"])                       # noqa: S307 — генератор из нашей же фикстуры
    if isinstance(v, dict) and "bytes_hex" in v:
        return bytes.fromhex(v["bytes_hex"])
    return v


def test_msgpack_subset_byte_exact_vs_msgpack_121():
    assert len(GOLD["msgpack_vectors"]) == 55
    for v in GOLD["msgpack_vectors"]:
        b = R.packb(_value(v["value"]))
        assert hashlib.sha256(b).hexdigest() == v["sha256"] and len(b) == v["len"], str(v["value"])[:80]
        if v["hex"] is not None:
            assert b.hex() == v["hex"]
    for bad in (1.5, {1: 2}, object(), 2 ** 64, -2 ** 63 - 1):
        with pytest.raises(R.HlRuleError):
            R.packb(bad)


def test_actions_equal_sdk_built_actions():
    g = {c["name"]: c for c in GOLD["sign_vectors"]}
    cl = "0x0123456789abcdef0123456789abcdef"
    sell = R.order_action(180025, False, D("0.16608"), D("600"), False, cl, sz_decimals=0)
    buy = R.order_action(180025, True, D("0.1583"), D("1000"), True, cl, sz_decimals=0)
    assert sell == g["sell_ioc_mainnet"]["action"] and R.packb(sell).hex() == g["sell_ioc_mainnet"]["packed_hex"]
    assert buy == g["buy_ro_vault_expires"]["action"]
    assert R.update_leverage_action(180025, 1) == g["update_leverage"]["action"]
    assert R.noop_action() == g["noop"]["action"]
    with pytest.raises(R.HlRuleError):
        R.order_action(180025, False, D("0.158381"), D("600"), False, cl, sz_decimals=0)
    with pytest.raises(R.HlRuleError):
        R.order_action(180025, False, D("0.15838"), D("600.5"), False, cl, sz_decimals=0)
    with pytest.raises(R.HlRuleError):
        R.order_action(180025, False, D("0.15838"), D("600"), False, "fb-D7K2-e03-c1-a1", sz_decimals=0)


def _independent_typed(h: bytes, mainnet: bool) -> dict:
    """Копия l1_payload SDK — написана в тесте, не взята из модуля."""
    return {"domain": {"chainId": 1337, "name": "Exchange", "verifyingContract": "0x" + "0" * 40, "version": "1"},
            "types": {"Agent": [{"name": "source", "type": "string"}, {"name": "connectionId", "type": "bytes32"}],
                      "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                       {"name": "chainId", "type": "uint256"},
                                       {"name": "verifyingContract", "type": "address"}]},
            "primaryType": "Agent", "message": {"source": "a" if mainnet else "b", "connectionId": h}}


def test_signatures_equal_official_sdk_and_recover_agent():
    acct = Account.from_key(TEST_KEY)
    assert acct.address == AGENT
    assert len(GOLD["sign_vectors"]) == 7
    for c in GOLD["sign_vectors"]:
        h = R.action_hash(c["action"], c["vault"], c["nonce"], c["expires_after"])
        assert "0x" + h.hex() == c["action_hash"], c["name"]
        s = HlSigner(acct, agent=AGENT, master=MASTER, account=c["vault"] or MASTER,
                     network="mainnet" if c["mainnet"] else "testnet")
        assert s.vault == c["vault"]
        sig = s.sign(c["action"], c["nonce"], c["expires_after"])
        assert sig == c["sig"], c["name"]
        rec = Account.recover_message(encode_typed_data(full_message=_independent_typed(h, c["mainnet"])),
                                      vrs=(sig["v"], int(sig["r"], 16), int(sig["s"], 16)))
        assert rec == AGENT
    # mainnet и testnet — разные подписи одного действия
    g = {c["name"]: c for c in GOLD["sign_vectors"]}
    assert g["sell_ioc_mainnet"]["sig"] != g["sell_ioc_testnet"]["sig"]


def test_action_hash_guards():
    with pytest.raises(R.HlRuleError):
        R.action_hash({"type": "noop"}, "0x123", 1, None)
    for bad in (0, -1, 2 ** 64, True):
        with pytest.raises(R.HlRuleError):
            R.action_hash({"type": "noop"}, None, bad, None)


# --- cloid ------------------------------------------------------------------------------------------
def test_h13_cloid_128bit_from_client_id():
    engine_cid = "fb-D7K2-e03-c1-a1"
    assert not R.is_cloid(engine_cid)
    c = R.derive_cloid("mainnet", MASTER, engine_cid)
    expected = "0x" + hashlib.sha256(f"mainnet|{MASTER.lower()}|{engine_cid}".encode()).hexdigest()[:32]
    assert c == expected and R.is_cloid(c)
    assert R.derive_cloid("mainnet", MASTER.upper().replace("0X", "0x"), engine_cid) == c    # адрес EVM — без регистра
    assert R.derive_cloid("mainnet", MASTER, "fb-D7K2-e03-c1-a2") != c                     # новая попытка — новый cloid
    assert R.derive_cloid("testnet", MASTER, engine_cid) != c
    with pytest.raises(R.HlRuleError):
        R.derive_cloid("mainnet", "0xnope", engine_cid)


# --- разбор ответов /exchange ------------------------------------------------------------------------
CL = "0x" + "1" * 32


def _st(s):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [s]}}}


@pytest.mark.parametrize("http,body,status,outcome,kind", [
    (200, _st({"filled": {"totalSz": "100.0", "avgPx": "0.1661", "oid": 5}}), "FILLED", "FILLED_TERMINAL", None),
    (200, _st({"filled": {"totalSz": "60.0", "avgPx": "0.1661", "oid": 5}}), "PARTIALLY_FILLED", "PARTIAL_TERMINAL",
     None),
    (200, _st({"error": "Order could not immediately match against any resting orders. asset=180025"}), "EXPIRED",
     "REJECTED_ZERO_FILL", "ioc_no_match"),
    (200, _st({"error": "Order must have minimum value of $10."}), "REJECTED", "REJECTED_ZERO_FILL", "min_notional"),
    (200, {"status": "err", "response": "Invalid nonce: duplicate nonce"}, "REJECTED", "REJECTED_ZERO_FILL", "nonce"),
    (None, None, "UNKNOWN", "UNKNOWN", None),
    (502, {"_raw": "<html>bad gateway</html>"}, "UNKNOWN", "UNKNOWN", None),
    (200, {"status": "ok"}, "UNKNOWN", "UNKNOWN", None),
    (200, _st({"resting": {"oid": 9}}), "UNKNOWN", "UNKNOWN", None),
    (200, _st({"filled": {"totalSz": "101.0", "avgPx": "0.1661", "oid": 5}}), "UNKNOWN", "UNKNOWN", None),
    (200, _st({"filled": {"totalSz": "100.0", "avgPx": "0.1661", "oid": 5, "cloid": "0x" + "2" * 32}}), "UNKNOWN",
     "UNKNOWN", None),
    (200, {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "x"}, {"error": "y"}]}}},
     "UNKNOWN", "UNKNOWN", None),
    (200, _st("waitingForFill"), "UNKNOWN", "UNKNOWN", None),
])
def test_h10_h12_order_response_normalization(http, body, status, outcome, kind):
    o = R.parse_order_response(http, body, req_sz=D(100), cloid=CL)
    assert (o.status, o.outcome, o.err_kind) == (status, outcome, kind)
    if status == "PARTIALLY_FILLED":
        assert o.filled == D(60) and o.avg_px == D("0.1661") and o.oid == 5
    if status in ("EXPIRED", "REJECTED", "UNKNOWN"):
        assert o.filled == 0


def test_classify_documented_error_strings():
    cases = {"Price must be divisible by tick size.": "tick_size",
             "Order must have minimum value of $10.": "min_notional",
             "Insufficient margin to place order.": "margin",
             "Reduce only order would increase position.": "reduce_only",
             "Order could not immediately match against any resting orders. asset=180025": "ioc_no_match",
             "Order would increase open interest while open interest is capped": "oi_cap",
             "Price too far from oracle": "oracle_bounds",
             "Order would cause position to exceed margin tier limit at current leverage.": "margin",
             "No liquidity available for market order.": "no_liquidity",
             "Invalid nonce: duplicate nonce": "nonce",
             "User or API Wallet 0x1234 does not exist.": "signature_or_agent",
             "Too many cumulative requests sent": "rate_limit",
             "something new": "other"}
    for text, kind in cases.items():
        assert R.classify_error(text) == kind, text


def test_parse_action_response():
    assert R.parse_action_response(200, {"status": "ok", "response": {"type": "default"}}) == ("ok", None)
    assert R.parse_action_response(200, {"status": "err", "response": "bad"})[0] == "err"
    assert R.parse_action_response(None, None)[0] == "unknown"
    assert R.parse_action_response(500, {"_raw": ""})[0] == "unknown"


# --- orderStatus, fills, funding ---------------------------------------------------------------------
def test_order_status_live_fixture_and_zero_fill_mapping():
    st = R.parse_order_status(PUB["user_1"]["orderStatus_oid"]["data"])
    assert st.found and st.status == "filled" and st.terminal and st.tif == "Ioc"
    assert (st.oid, st.cloid, st.orig_sz, st.remaining_sz, st.executed) == (
        543755048911, "0xff56591eae7a4ccaaf4e64029808b988", D(136), D(0), D(136))
    assert not R.parse_order_status(PUB["zero"]["orderStatus_cloid"]["data"]).found
    assert R.status_zero_fill(R.OrderStatus(True, "iocCancelRejected")) == ("EXPIRED", "ioc_no_match")
    assert R.status_zero_fill(R.OrderStatus(True, "canceled")) == ("EXPIRED", "ioc_no_match")
    assert R.status_zero_fill(R.OrderStatus(True, "reduceOnlyRejected")) == ("REJECTED", "reduce_only")
    assert R.status_zero_fill(R.OrderStatus(True, "perpMarginRejected")) == ("REJECTED", "margin")
    assert not R.OrderStatus(True, "open").terminal and R.OrderStatus(True, "marginCanceled").terminal
    with pytest.raises(R.HlRuleError):
        R.parse_order_status({"status": "order"})


def test_fill_and_funding_rows_from_live_fixture():
    raw = PUB["user_1"]["userFillsByTime"]["data"][0]
    r = R.fill_row(raw, network="mainnet", account=PUB["user_1"]["address"])
    assert (r["coin"], r["oid"], r["tid"], r["sz"], r["px"], r["fee"], r["fee_token"]) == (
        "para:ANSEM", 542805133880, 35071277947815, D(59), D("0.14412"), D("0.005509"), "USDC")
    assert r["cloid"] == "0x8725760767dd4f0aba5ca5baecf8654e" and r["builder_fee"] is None
    assert R.fill_key(r) == ("mainnet", PUB["user_1"]["address"].lower(), "para:ANSEM", raw["time"], raw["tid"])
    f = R.funding_row(PUB["user_1"]["userFunding"]["data"][0], network="mainnet", account=PUB["user_1"]["address"])
    assert (f["coin"], f["usdc"], f["szi"], f["rate"]) == ("para:ANSEM", D("0.119911"), D("-4758.0"), D("0.0001596952"))
    with pytest.raises(R.HlRuleError):
        R.fill_row({**raw, "sz": 1.5}, network="mainnet", account=PUB["user_1"]["address"])
    with pytest.raises(R.HlRuleError):
        R.funding_row({"time": 1, "delta": {"type": "deposit"}}, network="mainnet", account=PUB["user_1"]["address"])


# --- режим счёта и маржа -------------------------------------------------------------------------------
def test_abstraction_modes_reported_honestly():
    assert R.parse_abstraction("disabled").mode == "standard" and R.parse_abstraction("disabled").trade_supported
    d = R.parse_abstraction(PUB["user_0"]["userAbstraction"]["data"])
    assert (d.raw, d.mode, d.trade_supported) == ("default", "default", False)
    u = R.parse_abstraction(PUB["user_1"]["userAbstraction"]["data"])
    assert (u.mode, u.trade_supported) == ("unified", False)
    assert R.parse_abstraction("portfolioMargin").mode == "portfolio"
    assert R.parse_abstraction("dexAbstraction").mode == "dex_abstraction"
    for junk in (None, 5, "weird", {"a": 1}):
        assert R.parse_abstraction(junk).mode == "unknown" and not R.parse_abstraction(junk).trade_supported


def test_margin_view_by_mode():
    std = R.margin_view(R.parse_abstraction("disabled"), {"withdrawable": "100.5"}, None)
    assert (std.available, std.trade_supported) == (D("100.5"), True)
    assert R.margin_view(R.parse_abstraction("disabled"), None, None).available is None
    u = R.margin_view(R.parse_abstraction("unifiedAccount"), None, PUB["user_1"]["spotClearinghouseState"]["data"])
    assert u.available == D("19787.61012389") and "AfterMaintenance" in u.source and not u.trade_supported
    sp = copy.deepcopy(PUB["user_1"]["spotClearinghouseState"]["data"])
    del sp["tokenToAvailableAfterMaintenance"]
    u2 = R.margin_view(R.parse_abstraction("unifiedAccount"), None, sp)
    assert u2.available == D("21401.03695889") - D("1613.426835")
    assert R.margin_view(R.parse_abstraction("weird"), {"withdrawable": "5"}, None).available is None
