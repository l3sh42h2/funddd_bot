"""The mixed-case registry address must match runtime without weakening account isolation."""
from dataclasses import replace
from pathlib import Path

import pytest

from funding_bot.trade import instruments as I
from funding_bot.trade.runtime import hl_account_id
from funding_bot.trade.solana import ANSEM_MINT
from funding_bot.trade.types import InstrumentSpec as DealInstrument

MASTER = "0x" + "aB" * 20
ACCOUNT = "0x" + "cD" * 20
EXAMPLE = Path(__file__).resolve().parents[1] / "deploy/instruments.json.example"


def record(account_id):
    s = I.load_registry(EXAMPLE).get("ansem_sol_para_v1")
    return replace(s, perp=replace(s.perp, account_id=account_id))


@pytest.mark.parametrize("master,account", [
    (MASTER, ACCOUNT), (MASTER.lower(), ACCOUNT),
    (MASTER, ACCOUNT.lower()), (MASTER.lower(), ACCOUNT.lower()),
])
def test_registry_and_runtime_accept_same_address_bytes(master, account):
    raw = f"hyperliquid:mainnet:{master}:{account}:para"
    s = record(raw)
    before = (s.identity_hash, s.record_hash)
    runtime = hl_account_id("mainnet", MASTER, ACCOUNT, "para")
    result = I.deal_spec(s, account_id=runtime)
    assert result.perp_account == runtime
    assert result.perp_scope == I.deal_spec(record(runtime), account_id=runtime).perp_scope
    assert result.token == ANSEM_MINT and result.perp_symbol == "para:ANSEM"
    assert s.perp.account_id == raw
    assert (s.identity_hash, s.record_hash) == before
    assert DealInstrument.from_json(result.to_json()).inst_hash() == result.inst_hash()


@pytest.mark.parametrize("field,replacement", [
    (0, "Hyperliquid"), (1, "testnet"), (1, "MAINNET"),
    (2, "0x" + "ef" * 20), (3, "0x" + "ef" * 20),
    (4, "xyz"), (4, "PARA"),
])
def test_different_scope_still_refused(field, replacement):
    runtime = hl_account_id("mainnet", MASTER, ACCOUNT, "para")
    parts = runtime.split(":")
    parts[field] = replacement
    with pytest.raises(I.RegistryError, match="account_id"):
        I.deal_spec(record(":".join(parts)), account_id=runtime)


def test_new_spec_canonical_without_runtime_override_and_old_json_unchanged():
    raw = f"hyperliquid:mainnet:{MASTER}:{ACCOUNT}:para"
    result = I.deal_spec(record(raw))
    assert result.perp_account == hl_account_id("mainnet", MASTER, ACCOUNT, "para")
    # Existing frozen records must not change account scope/hash when read back.
    old = replace(result, perp_account=raw)
    restored = DealInstrument.from_json(old.to_json())
    assert restored.perp_account == raw and restored.inst_hash() == old.inst_hash()


def test_opaque_identifiers_remain_case_sensitive():
    with pytest.raises(I.RegistryError, match="account_id"):
        I.deal_spec(record("hl:mainnet:Account:para"), account_id="hl:mainnet:account:para")


def test_missing_account_still_refused():
    with pytest.raises(I.RegistryError, match="account_id"):
        I.deal_spec(record(None))
