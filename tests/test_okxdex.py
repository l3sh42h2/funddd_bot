"""Транспорт OKX DEX на подставной сессии: подпись запроса, пачки цен, котировка, ошибки. Живой ключ не нужен."""
import base64, hashlib, hmac, json
import pytest
from funding_bot import config
from funding_bot.client import PermanentHTTPError, BannedError
from funding_bot.okxdex import OkxDex, QuotaExhausted, NoLiquidity, Unsupported


class _R:
    def __init__(self, body, code=200):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self): return self._b


class _S:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append(dict(method=method, url=url, data=data, headers=headers))
        return self.handler(method, url, data)


def _dex(handler, **kw):
    return OkxDex(session=_S(handler), key="k", secret="s3cr3t", passphrase="p", rps=1000, clock=lambda: 1_789_000_000.123, **kw)


def test_disabled_without_key_makes_no_requests(monkeypatch):
    for v in ("OKX_DEX_API_KEY", "OKX_DEX_SECRET", "OKX_DEX_PASSPHRASE"):
        monkeypatch.delenv(v, raising=False)
    s = _S(lambda *a: _R({"code": "0", "data": []}))
    d = OkxDex(session=s)
    assert not d.enabled() and d.health()["enabled"] is False
    with pytest.raises(PermanentHTTPError, match="ключа нет"):
        d.market_prices([("501", "x")])
    assert s.calls == []


def test_signature_headers_and_price_batches():
    seen = []

    def h(method, url, data):
        body = json.loads(data)
        seen.append(len(body))
        return _R({"code": "0", "data": [{"chainIndex": b["chainIndex"], "tokenContractAddress": b["tokenContractAddress"],
                                          "price": "1.5", "time": "1789000000000"} for b in body]})
    d = _dex(h)
    toks = [("56", f"0x{i:040x}") for i in range(250)]
    px = d.market_prices(toks)
    assert seen == [100, 100, 50] and len(px) == 250 and px[("56", toks[0][1])] == (1.5, 1789000000000)
    c = d._s.calls[0]
    ts = c["headers"]["OK-ACCESS-TIMESTAMP"]
    import datetime, re
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", ts)                  # ISO UTC с миллисекундами
    assert ts == datetime.datetime.fromtimestamp(1_789_000_000.123, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123Z")
    want = base64.b64encode(hmac.new(b"s3cr3t", (ts + "POST" + "/api/v6/dex/market/price" + c["data"]).encode(), hashlib.sha256).digest()).decode()
    assert c["headers"]["OK-ACCESS-SIGN"] == want and c["headers"]["OK-ACCESS-KEY"] == "k" and c["headers"]["OK-ACCESS-PASSPHRASE"] == "p"
    assert "OK-ACCESS-PROJECT" not in c["headers"]


def test_quote_parses_gas_tax_impact_and_signs_query():
    body = {"code": "0", "data": [{"fromTokenAmount": "15000000", "toTokenAmount": "123456789", "priceImpactPercent": "-0.12",
                                   "tradeFee": "0.0021", "fromToken": {"decimal": "6", "taxRate": "0"},
                                   "toToken": {"decimal": "9", "taxRate": "0.01", "isHoneyPot": False}}]}
    d = _dex(lambda *a: _R(body))
    q = d.quote("501", "EPjF", "TOKEN", 15_000_000)
    assert (q["to_amount"], q["price_impact"], q["gas_usd"], q["buy_tax"], q["to_decimals"], q["honeypot"]) == (123456789, -0.12, 0.0021, 0.01, 9, False)
    c = d._s.calls[0]
    assert c["method"] == "GET" and "/api/v6/dex/aggregator/quote?chainIndex=501&amount=15000000" in c["url"] and c["data"] is None
    path_q = c["url"].replace("https://web3.okx.com", "")
    want = base64.b64encode(hmac.new(b"s3cr3t", (c["headers"]["OK-ACCESS-TIMESTAMP"] + "GET" + path_q).encode(), hashlib.sha256).digest()).decode()
    assert c["headers"]["OK-ACCESS-SIGN"] == want


@pytest.mark.parametrize("resp,err", [
    (_R({"code": "50011", "msg": "Too Many Requests"}, 429), BannedError),
    (_R({"code": "0", "msg": "payment required"}, 402), QuotaExhausted),
    (_R({"code": "50113", "msg": "Invalid Sign"}, 401), PermanentHTTPError),
    (_R({"code": "82000", "msg": "Insufficient liquidity"}), NoLiquidity),
    (_R({"code": "82104", "msg": "token not supported"}), Unsupported),
    (_R({"code": "50026", "msg": "system error"}, 500), RuntimeError),
])
def test_error_codes(resp, err):
    d = _dex(lambda *a: resp)
    with pytest.raises(err):
        d.quote("56", "a", "b", 1)
    if err is BannedError:                                   # пауза: следующий запрос без обращения к API
        n = len(d._s.calls)
        with pytest.raises(BannedError):
            d.quote("56", "a", "b", 1)
        assert len(d._s.calls) == n


def test_owner_decisions_in_config():
    assert set(config.OKX_DEX_CHAINS) == {"solana", "bsc", "robinhood"} and config.OKX_DEX_MIN_LIQUIDITY_USD == 50_000
    assert config.OKX_DEX_QUOTE_USD == 15
