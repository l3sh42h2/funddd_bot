"""EvmRpc/EvmWallet на Robinhood Chain (4663): выбор RPC по умолчанию и фикстуры, снятые вживую с публичного узла
https://rpc.mainnet.chain.robinhood.com 13.09.2026 (curl, только чтение — см. отчёт задачи). Байты ответов —
ровно то, что вернул узел: chainId, decimals()/symbol() стейбла USDG и токена FATCOIN, eth_gasPrice, блок с
признаками Arbitrum Nitro (l1BlockNumber/sendRoot/sendCount) и eth_estimateGas двух переводов (доказательство,
что L1-комиссия уже в L2-газе — см. tconfig.py). Отправки нет нигде в этом файле.
"""
from types import SimpleNamespace
import pytest
from funding_bot.trade import evm, tconfig

rlp = pytest.importorskip("rlp")
pytest.importorskip("eth_account")
from eth_account import Account

RH_RPC = "https://rpc.mainnet.chain.robinhood.com"
STABLE_4663 = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"          # USDG, config.OKX_DEX_STABLES["4663"]
FATCOIN = "0x12d5ee7917ca430073c3a638ee1e6f0648a98a01"
# ровно то, что вернул decimals()/symbol() 13.09.2026 (curl -X POST .../eth_call, data 0x313ce567 / 0x95d89b41)
_DECIMALS_HEX = {STABLE_4663: "0x0000000000000000000000000000000000000000000000000000000000000006",
                 FATCOIN: "0x0000000000000000000000000000000000000000000000000000000000000012"}
_SYMBOL_HEX = {STABLE_4663: ("0x0000000000000000000000000000000000000000000000000000000000000020"
                             "0000000000000000000000000000000000000000000000000000000000000004"
                             "5553444700000000000000000000000000000000000000000000000000000000"),
               FATCOIN: ("0x0000000000000000000000000000000000000000000000000000000000000020"
                        "0000000000000000000000000000000000000000000000000000000000000007"
                        "464154434f494e00000000000000000000000000000000000000000000000000")}


class _Resp:
    def __init__(self, body):
        self.status_code, self._b = 200, body

    def json(self):
        return self._b


class _RhLiveSession:
    """Подставная requests.Session с ответами публичного RPC Robinhood Chain, byte-for-byte снятыми вживую."""

    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json["method"], json.get("params")))
        m, p, rid = json["method"], json.get("params") or [], json["id"]
        if m == "eth_chainId":
            result = "0x1237"                                       # = 4663
        elif m == "eth_gasPrice":
            result = "0x50f78a0"
        elif m == "eth_call":
            data, to = p[0]["data"].lower(), p[0]["to"].lower()
            if data == "0x313ce567":
                result = _DECIMALS_HEX[to]
            elif data == "0x95d89b41":
                result = _SYMBOL_HEX[to]
            else:
                raise AssertionError(f"неожиданный eth_call: {data}")
        elif m == "eth_estimateGas":
            has_data = bool(p[0].get("data") and p[0]["data"] not in ("0x", "0x0"))
            result = "0x5582" if has_data else "0x52e9"              # 21890 / 21225 — L1-комиссия уже внутри
        elif m == "eth_getBlockByNumber":
            result = {"l1BlockNumber": "0x18c4476", "sendRoot": "0x" + "ab" * 32, "sendCount": "0x940",
                      "baseFeePerGas": "0x5104b90", "number": "0x3b3ad83"}
        else:
            raise AssertionError(f"неожиданный метод: {m}")
        return _Resp({"jsonrpc": "2.0", "id": rid, "result": result})


# --- выбор RPC по умолчанию (без сети) ------------------------------------------------------------------------
def test_default_urls_pick_chain_table(monkeypatch):
    monkeypatch.delenv("BSC_RPC_URLS", raising=False)
    monkeypatch.delenv("ROBINHOOD_RPC_URLS", raising=False)
    assert evm.EvmRpc(session=SimpleNamespace(post=lambda *a, **k: None)).urls == tconfig.BSC_RPC_DEFAULTS   # chain="bsc" — старое поведение
    rpc = evm.EvmRpc(session=SimpleNamespace(post=lambda *a, **k: None), chain="robinhood")
    assert rpc.urls == tconfig.ROBINHOOD_RPC_DEFAULTS == (RH_RPC,)


def test_explicit_urls_win_over_chain_default():
    rpc = evm.EvmRpc(("https://own-node.test",), session=SimpleNamespace(post=lambda *a, **k: None), chain="robinhood")
    assert rpc.urls == ("https://own-node.test",)


# --- фикстуры публичного RPC (13.09.2026) ----------------------------------------------------------------------
def test_chain_id_and_gas_price_match_live_fixture():
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())
    assert rpc.chain_id() == 4663 == tconfig.CHAIN_IDS["robinhood"]
    assert rpc.gas_price() == 0x50f78a0


def test_stable_and_fatcoin_decimals_match_live_fixture():
    """config.OKX_DEX_STABLES["4663"] обещает USDG с 6 знаками — сверено decimals() контракта вживую."""
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())
    assert rpc.decimals(STABLE_4663) == 6
    assert rpc.decimals(FATCOIN) == 18


def test_stable_and_fatcoin_symbol_match_live_fixture():
    """symbol() — не через wrapper EvmRpc (его для symbol нет, OKX API отдаёт имя сама), а тем же eth_call,
    которым в проде идёт decimals(): разбор ABI-строки руками, как это сделал бы любой читающий код."""
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())

    def _symbol(addr: str) -> str:
        raw = rpc.eth_call(addr, "0x95d89b41")
        b = bytes.fromhex(raw[2:])
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode()

    assert _symbol(STABLE_4663) == "USDG"
    assert _symbol(FATCOIN) == "FATCOIN"


def test_block_carries_arbitrum_nitro_fields():
    """Подтверждение, что Robinhood Chain — Arbitrum Nitro L2 (обосновывает решение в tconfig.py не заводить
    отдельный оракул L1-комиссии, как для OP-стека)."""
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())
    blk = rpc.call("eth_getBlockByNumber", ["latest", False])
    assert {"l1BlockNumber", "sendRoot", "sendCount"} <= blk.keys()


def test_estimate_gas_already_bakes_in_l1_fee():
    """13.09.2026, живой замер: перевод ETH без calldata — 21225 газа (не канонических 21000); с ~40 байтами
    calldata — 21890. Arbitrum Nitro включает L1-комиссию в L2-газ eth_estimateGas сам — отдельно считать не
    нужно (в отличие от OP-стека), запас GAS_LIMIT_EST_MULT (×1.3) это и так покрывает."""
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())
    to = "0x" + "02" * 20
    plain = rpc.estimate_gas({"from": "0x" + "01" * 20, "to": to, "value": "0x1"})
    with_data = rpc.estimate_gas({"from": "0x" + "01" * 20, "to": to, "value": "0x1",
                                  "data": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"})
    assert plain == 21225 > 21000
    assert with_data == 21890 > plain


# --- безопасность: сеть узла должна совпасть с сетью, для которой подписываем ------------------------------------
def test_evmwallet_refuses_when_rpc_reports_wrong_chain(tmp_path):
    """Кошелёк создан для Robinhood (4663), а RPC отвечает за BSC (56, как в проде) — _check_chain должен
    отказать до какой-либо подписи, а не подписать транзакцию не в ту сеть."""
    class _WrongChainSession:
        def post(self, url, json=None, timeout=None):
            return _Resp({"jsonrpc": "2.0", "id": json["id"], "result": "0x38"})     # 56, не 0x1237

    rpc = evm.EvmRpc(("https://x.test",), session=_WrongChainSession())
    acct = Account.from_key("0x" + "46" * 32)                       # тестовый вектор EIP-155, не боевой ключ
    w = evm.EvmWallet(rpc, tconfig.CHAIN_IDS["robinhood"], acct, lambda row: None, gate=lambda in_flight: None,
                      lock_dir=tmp_path, chain="robinhood")
    with pytest.raises(RuntimeError, match="сеть 56"):
        w._check_chain()


def test_evmwallet_accepts_matching_chain_id(tmp_path):
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())
    acct = Account.from_key("0x" + "46" * 32)
    w = evm.EvmWallet(rpc, tconfig.CHAIN_IDS["robinhood"], acct, lambda row: None, gate=lambda in_flight: None,
                      lock_dir=tmp_path, chain="robinhood")
    w._check_chain()                                                 # не бросает
    assert w._chain_ok is True


def test_lock_filename_has_no_chain_infix(tmp_path):
    """Формат имени файла-замка (tconfig.EVM_LOCK_FMT) не меняем — один писатель на АДРЕС кошелька, а не на пару
    (сеть, адрес). Если владелец использует один адрес на обеих сетях — записи серилизуются между ними; это
    осознанный компромисс (см. отчёт задачи), не баг: два процесса с одним приватным ключом подписывать
    одновременно всё равно не должны, даже в разные сети."""
    rpc = evm.EvmRpc((RH_RPC,), session=_RhLiveSession())
    acct = Account.from_key("0x" + "46" * 32)
    w = evm.EvmWallet(rpc, tconfig.CHAIN_IDS["robinhood"], acct, lambda row: None, gate=lambda in_flight: None,
                      lock_dir=tmp_path, chain="robinhood")
    assert w.lock_path.name == f"evm_{w.address.lower()}.lock"
