"""Споты Gate / KuCoin / Bitget на подставной HTTP-сессии: что считается торгуемой парой, как читается книга,
комиссия пары KuCoin, пауза после 429. Поля и ловушки — из исследования API 11–12.09 (см. шапку spot.py)."""
import json, time
import pytest
from funding_bot import calc, config, universe
from funding_bot.client import BannedError
from funding_bot.spot import GateSpot, KucoinSpot, BitgetSpot


class _R:
    def __init__(self, body, code=200, headers=None):
        self.status_code, self._b, self.headers = code, body, headers or {}
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self): return self._b

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(str(self.status_code))


class _S:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for k, v in self.routes.items():
            if url.endswith(k):
                return v() if callable(v) else v
        raise AssertionError(url)


CURRENCIES = [dict(currency="CRCLX", category=["stocks", "xstocks"]), dict(currency="CRCLON", category=["stocks", "ondo-stocks"]),
              dict(currency="CRCLG", category=["gstocks", "stocks"]), dict(currency="SPCX", category=["stocks"]),
              dict(currency="PAXG", category=["metals"]), dict(currency="ONDO", category=["ondo-stocks"]), dict(currency="BTC")]


def test_gate_tradable_pairs_skip_etf_st_delisting_premarket_and_one_way():
    pairs = [dict(id="BTC_USDT", base="BTC", quote="USDT", trade_status="tradable", type="normal", precision=1, amount_precision=6,
                  min_quote_amount="3", base_name="Bitcoin"),
             dict(id="龙虾_USDT", base="龙虾", quote="USDT", trade_status="tradable", type="normal", base_name="龙虾"),
             dict(id="BTC3L_USDT", base="BTC3L", quote="USDT", trade_status="tradable", type="normal", base_name="BTC3xLong"),
             dict(id="ICX_USDT", base="ICX", quote="USDT", trade_status="tradable", type="normal", st_tag=True, delisting_time=1789527600),
             dict(id="LON_USDT", base="LON", quote="USDT", trade_status="tradable", type="normal", delisting_time=1789527600),
             dict(id="PM_USDT", base="PM", quote="USDT", trade_status="tradable", type="premarket"),
             dict(id="ONE_USDT", base="ONE", quote="USDT", trade_status="buyable", type="normal"),
             dict(id="MANA3_USDT", base="MANA3", quote="USDT", trade_status="tradable", type="normal", base_name="X-EcoChain"),
             dict(id="CRCLX_USDT", base="CRCLX", quote="USDT", trade_status="tradable", type="normal", base_name="Circle xStock"),
             dict(id="CRCLON_USDT", base="CRCLON", quote="USDT", trade_status="tradable", type="normal",
                  base_name="Circle Internet Group Ondo Tokenized"),
             dict(id="CRCLG_USDT", base="CRCLG", quote="USDT", trade_status="tradable", type="normal", base_name="Circle"),
             dict(id="SPCX_USDT", base="SPCX", quote="USDT", trade_status="tradable", type="normal", base_name="SpaceX"),   # pre-IPO
             dict(id="PAXG_USDT", base="PAXG", quote="USDT", trade_status="tradable", type="normal", base_name="PAX Gold"),
             dict(id="ONDO_USDT", base="ONDO", quote="USDT", trade_status="tradable", type="normal", base_name="Ondo Finance"),
             dict(id="PENGU3L_USDT", base="PENGU3L", quote="USDT", trade_status="tradable", type="normal", base_name="三倍多PENGU"),
             dict(id="BTC_USDC", base="BTC", quote="USDC", trade_status="tradable", type="normal")]
    ticks = [dict(currency_pair="BTC_USDT", highest_bid="60000.1", lowest_ask="60000.2"),
             dict(currency_pair="龙虾_USDT", highest_bid="", lowest_ask="0.5"),           # пустая сторона — книги нет
             dict(currency_pair="BTC_USDC", highest_bid="1", lowest_ask="2")]
    g = GateSpot(session=_S({"/spot/currency_pairs": _R(pairs, headers={"x-gate-ratelimit-requests-remain": "150",
                                                                         "x-gate-ratelimit-limit": "200"}),
                             "/spot/currencies": _R(CURRENCIES), "/spot/tickers": _R(ticks)}))
    ins = {i["symbol"]: i for i in g.spot_instruments()}
    assert set(ins) == {"BTC_USDT", "龙虾_USDT", "MANA3_USDT", "CRCLX_USDT", "CRCLON_USDT", "CRCLG_USDT", "PAXG_USDT",
                        "ONDO_USDT"}                         # MANA3 — другая монета; SPCX — pre-IPO; PENGU3L — токен с плечом
    assert {s: (ins[s]["alt_base"], ins[s]["base"]) for s in ("CRCLX_USDT", "CRCLON_USDT", "CRCLG_USDT", "BTC_USDT", "PAXG_USDT", "ONDO_USDT")} == \
        {"CRCLX_USDT": ("CRCL", "~CRCLX"), "CRCLON_USDT": ("CRCL", "~CRCLON"), "CRCLG_USDT": ("CRCL", "~CRCLG"),
         "BTC_USDT": (None, "BTC"), "PAXG_USDT": (None, "PAXG"), "ONDO_USDT": (None, "ONDO")}   # по классу валюты, не по суффиксу
    assert ins["BTC_USDT"]["base"] == "BTC" and ins["BTC_USDT"]["tick_size"] == 0.1 and ins["BTC_USDT"]["min_notional"] == 3.0
    assert ins["MANA3_USDT"]["base"] == "MANA3"                                   # цифру не срезаем: не Decentraland
    assert g.health()["budget"] == 0.25
    books = g.books()
    assert set(books) == {"BTC_USDT"} and books["BTC_USDT"]["bid"] == 60000.1


def test_gate_429_pauses_until_reset_and_makes_no_calls_meanwhile():
    s = _S({"/spot/tickers": _R({"label": "TOO_MANY_REQUESTS"}, 429, {"x-gate-ratelimit-reset-timestamp": str(time.time() + 8)})})
    g = GateSpot(session=s)
    with pytest.raises(BannedError):
        g.books()
    assert 8 <= g.banned_until - time.time() <= 10 and g.n_429 == 1
    with pytest.raises(BannedError):
        g.books()
    assert len(s.calls) == 1


def test_kucoin_name_alias_fee_class_filters_and_snapshot_time():
    now = int(time.time() * 1000)
    rows = [dict(symbol="BTC-USDT", name="BTC-USDT", quoteCurrency="USDT", enableTrading=True, callauctionIsEnabled=False,
                 feeCategory=1, priceIncrement="0.1", baseIncrement="0.00000001", minFunds="0.1"),
            dict(symbol="BCHSV-USDT", name="BSV-USDT", quoteCurrency="USDT", enableTrading=True, feeCategory=3),
            dict(symbol="NIULAI-USDT", name="牛来-USDT", quoteCurrency="USDT", enableTrading=True, feeCategory=2),
            dict(symbol="VAI-USDT", name="VAI-USDT", quoteCurrency="USDT", enableTrading=True, st=True, feeCategory=3),
            dict(symbol="AUC-USDT", name="AUC-USDT", quoteCurrency="USDT", enableTrading=True, callauctionIsEnabled=True),
            dict(symbol="NEW-USDT", name="NEW-USDT", quoteCurrency="USDT", enableTrading=True, tradingStartTime=now + 3600_000),
            dict(symbol="OFF-USDT", name="OFF-USDT", quoteCurrency="USDT", enableTrading=False),
            dict(symbol="ETH-BTC", name="ETH-BTC", quoteCurrency="BTC", enableTrading=True),
            dict(symbol="TSLAX-USDT", name="TSLAX-USDT", quoteCurrency="USDT", enableTrading=True, market="Stocks", feeCategory=2)]
    tick = dict(time=now - 500, ticker=[dict(symbol="BTC-USDT", buy="60000", sell="60001", bestBidSize="0.5", bestAskSize="0.7"),
                                        dict(symbol="ETH-BTC", buy="0.03", sell="0.031")])
    k = KucoinSpot(session=_S({"/api/v2/symbols": _R({"code": "200000", "data": rows}),
                               "/api/v1/market/allTickers": _R({"code": "200000", "data": tick},
                                                               headers={"gw-ratelimit-remaining": "1985", "gw-ratelimit-limit": "2000"})}))
    ins = {i["symbol"]: i for i in k.spot_instruments()}
    assert set(ins) == {"BTC-USDT", "BCHSV-USDT", "NIULAI-USDT", "TSLAX-USDT"}
    assert (ins["TSLAX-USDT"]["alt_base"], ins["TSLAX-USDT"]["base"]) == ("TSLA", "~TSLAX") and ins["BTC-USDT"]["alt_base"] is None
    del ins["TSLAX-USDT"]
    assert ins["BCHSV-USDT"]["base"] == "BSV" and ins["NIULAI-USDT"]["base"] == "牛来"   # тикер рынка, а не код
    assert ins["BTC-USDT"]["taker_fee"] == 0.001 and abs(ins["BCHSV-USDT"]["taker_fee"] - 0.003) < 1e-12
    b = k.books()
    assert set(b) == {"BTC-USDT"} and b["BTC-USDT"]["ask_qty"] == 0.7
    assert "obs" not in b["BTC-USDT"]                  # data.time — время ответа, не снимка: наблюдение = время получения
    # комиссия пары доходит до строки: круг = 2 × (спот своего класса + перп)
    sf = universe.build_sf({"binance": [dict(symbol="BSVUSDT", base="BSV", factor=1.0)]}, {"kucoin_spot": list(ins.values())})
    assert [r["key"] for r in sf] == ["kucoin_spot:BCHSV-USDT|binance:BSVUSDT"]
    row = calc.build_sf_row(sf[0], {"interval_h": 8}, None, None, None, {}, now)
    assert abs(row["fee"] - 2 * (0.003 + config.FEES_TAKER["binance"])) < 1e-12
    assert row["urls"]["spot"] == "https://www.kucoin.com/trade/BCHSV-USDT" and row["spot_label"] == "kucoin"


def test_kucoin_error_code_and_429000_pause():
    k = KucoinSpot(session=_S({"/api/v1/market/allTickers": _R({"code": "400100", "msg": "bad"})}))
    with pytest.raises(RuntimeError, match="400100"):
        k.books()
    k2 = KucoinSpot(session=_S({"/api/v1/market/allTickers": _R({"code": "429000"}, 429, {"gw-ratelimit-reset": "12000"})}))
    with pytest.raises(BannedError):
        k2.books()
    assert 11 <= k2.banned_until - time.time() <= 12.5


def test_bitget_stock_tokens_get_underlying_skips_halt_scheduled_delisting_and_empty_side():
    rows = [dict(symbol="BTCUSDT", baseCoin="BTC", quoteCoin="USDT", status="online", offTime="", pricePrecision="2",
                 quantityPrecision="6", minTradeUSDT="1"),
            dict(symbol="RNVDAUSDT", baseCoin="rNVDA", quoteCoin="USDT", status="online", offTime=""),
            dict(symbol="PRESPCXUSDT", baseCoin="preSPCX", quoteCoin="USDT", status="online", offTime=""),
            dict(symbol="TURBOUSDT", baseCoin="TURBO", quoteCoin="USDT", status="online", offTime="1789725600000"),
            dict(symbol="CROUSDT", baseCoin="CRO", quoteCoin="USDT", status="halt", offTime=""),
            dict(symbol="RVNOMUSDT", baseCoin="RVNOM", quoteCoin="USDT", status="online", offTime=""),
            dict(symbol="ETHUSDC", baseCoin="ETH", quoteCoin="USDC", status="online", offTime="")]
    ticks = [dict(symbol="BTCUSDT", bidPr="60000", askPr="60000.5", bidSz="1", askSz="2"),
             dict(symbol="RVNOMUSDT", bidPr="0.01", askPr="0", bidSz="5", askSz=None)]
    b = BitgetSpot(session=_S({"/api/v2/spot/public/symbols": _R({"code": "00000", "data": rows}),
                               "/api/v2/spot/market/tickers": _R({"code": "00000", "data": ticks},
                                                                 headers={"x-mbx-used-remain-limit": "19"})}))
    ins = {i["symbol"]: i for i in b.spot_instruments()}
    assert set(ins) == {"BTCUSDT", "RVNOMUSDT", "RNVDAUSDT"}                 # preSPCX — pre-IPO, не берём
    assert ins["BTCUSDT"]["tick_size"] == 0.01 and ins["BTCUSDT"]["min_notional"] == 1.0
    # акция: своя база «~RNVDA» (с монетами не совпадёт никогда), тикер акции — в alt_base
    assert (ins["RNVDAUSDT"]["base"], ins["RNVDAUSDT"]["alt_base"]) == ("~RNVDA", "NVDA") and ins["BTCUSDT"]["alt_base"] is None
    books = b.books()
    assert set(books) == {"BTCUSDT"} and books["BTCUSDT"]["bid_qty"] == 1.0          # «0» у аска — книги нет
    b2 = BitgetSpot(session=_S({"/api/v2/spot/market/tickers": _R({"code": "429", "msg": "Too Many Requests"}, 429)}))
    with pytest.raises(BannedError):
        b2.books()
    assert b2.banned_until - time.time() > 55


def test_binance_bstocks_by_tag_not_by_suffix():
    """ARB → AR, BNB → BN дали бы чужие пары: акция Binance — только с тегом bStocks; нет списка — акций нет."""
    from funding_bot import exchanges
    from fakes import FakeExchange, spot
    s = FakeExchange("binance_spot", kind="spot")
    s.symbols = [spot("CRCLBUSDT", base="CRCLB"), spot("ARBUSDT", base="ARB"), spot("BNBUSDT", base="BNB")]
    s.bstock_symbols = {"CRCLBUSDT"}
    ins = {i["symbol"]: i for i in exchanges.spot_instruments(s)}
    assert (ins["CRCLBUSDT"]["alt_base"], ins["CRCLBUSDT"]["base"]) == ("CRCL", "~CRCLB")
    assert ins["ARBUSDT"]["alt_base"] is None and ins["ARBUSDT"]["base"] == "ARB" and ins["BNBUSDT"]["alt_base"] is None
    s.fail_paths.add("bstocks")
    ins = {i["symbol"]: i for i in exchanges.spot_instruments(s)}
    assert ins["CRCLBUSDT"]["alt_base"] is None and ins["CRCLBUSDT"]["base"] == "CRCLB"    # без списка — просто монета CRCLB


def test_make_clients_builds_spot_venues_in_owner_order():
    from funding_bot.client import make_clients
    cl = make_clients()
    # 12.09: после Binance/Gate/KuCoin/Bitget — споты Lighter основного инстанса и Robinhood Chain (lighter.py)
    assert [v for v in config.SPOT_VENUES if v in cl] == ["binance_spot", "gate_spot", "kucoin_spot", "bitget_spot",
                                                          "lighter_spot", "lighter_rh_spot"]
    assert all(hasattr(cl[v], "spot_instruments")
               for v in ("gate_spot", "kucoin_spot", "bitget_spot", "lighter_spot", "lighter_rh_spot"))
