"""Robinhood Chain (4663) в tconfig.py: chainId, RPC по умолчанию/через переменную окружения, allowlist OKX,
имя нативной монеты. Сеть новая — без сети (публичный ключ OKX недоступен с этой машины, см. шапку tconfig.py),
поэтому всё здесь — чистые функции над константами, без сети и без изменения поведения BSC.
"""
import pytest
from funding_bot.trade import tconfig


def test_chain_id_matches_live_rpc_13_09():
    """eth_chainId публичного RPC Robinhood Chain 13.09.2026 вернул 0x1237 = 4663 (см. tests/test_rh_evm.py
    для самой фикстуры RPC); BSC — прежние 56."""
    assert tconfig.CHAIN_IDS == {"bsc": 56, "robinhood": 4663}


def test_native_symbol_table():
    assert tconfig.NATIVE_SYMBOL == {"bsc": "BNB", "robinhood": "ETH"}


def test_bsc_rpc_urls_unchanged_by_refactor():
    """bsc_rpc_urls теперь делегирует общему rpc_urls() — сама функция и её дефолты не изменились ни на байт."""
    assert tconfig.bsc_rpc_urls({}) == tconfig.BSC_RPC_DEFAULTS == ("https://bsc-rpc.publicnode.com",
                                                                     "https://bsc-dataseed.bnbchain.org")
    assert tconfig.bsc_rpc_urls({"BSC_RPC_URLS": " https://a , ,https://b"}) == ("https://a", "https://b")


def test_robinhood_rpc_urls_default_and_env_override():
    assert tconfig.robinhood_rpc_urls({}) == tconfig.ROBINHOOD_RPC_DEFAULTS == ("https://rpc.mainnet.chain.robinhood.com",)
    assert tconfig.robinhood_rpc_urls({"ROBINHOOD_RPC_URLS": " https://x , ,https://y"}) == ("https://x", "https://y")
    # своё RPC не трогает BSC и наоборот — переменные не должны путаться местами
    assert tconfig.robinhood_rpc_urls({"BSC_RPC_URLS": "https://not-robinhood"}) == tconfig.ROBINHOOD_RPC_DEFAULTS
    assert tconfig.bsc_rpc_urls({"ROBINHOOD_RPC_URLS": "https://not-bsc"}) == tconfig.BSC_RPC_DEFAULTS


def test_rpc_urls_dispatch_and_unknown_chain():
    assert tconfig.rpc_urls("bsc", {}) == tconfig.BSC_RPC_DEFAULTS
    assert tconfig.rpc_urls("robinhood", {}) == tconfig.ROBINHOOD_RPC_DEFAULTS
    with pytest.raises(KeyError):
        tconfig.rpc_urls("solana", {})           # Solana не EVM — RPC-таблицы здесь для неё нет


def test_chain_index_robinhood():
    assert tconfig.chain_index("robinhood") == "4663" == tconfig.chain_index("4663")
    assert tconfig.chain_index("ROBINHOOD") == "4663"                   # регистр не важен, как у bsc


def test_router_spender_allowlist_empty_for_robinhood_not_forgotten():
    """4663 — не «забыли добавить», а «нет источника»: router_confirmed/spender_confirmed различают это от
    обычного случая «нет такого адреса», чтобы evm_swap мог дать другое сообщение (test_rh_swap.py)."""
    some_addr = "0x" + "ab" * 20
    assert not tconfig.router_allowed("robinhood", some_addr)
    assert not tconfig.spender_allowed("robinhood", some_addr)
    assert not tconfig.router_confirmed("robinhood") and not tconfig.spender_confirmed("robinhood")
    # BSC не задет: allowlist там по-прежнему непуст
    assert tconfig.router_confirmed("bsc") and tconfig.spender_confirmed("bsc")
    assert tconfig.router_allowed("bsc", "0x5994814f2c4040b863a0125a45de152a8c2a4dec")


def test_okx_dex_stable_for_robinhood_already_in_config():
    """config.OKX_DEX_STABLES["4663"] — не эта задача (config.py не трогаем), но значение сверено живым RPC
    13.09.2026: USDG, 6 знаков (decimals() и symbol() контракта 0x5fc5…d168) — см. test_rh_evm.py."""
    from funding_bot import config
    addr, dec = config.OKX_DEX_STABLES["4663"]
    assert addr == "0x5fc5360d0400a0fd4f2af552add042d716f1d168" and dec == 6
