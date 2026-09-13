"""Полное сообщение: раскрытие ALT по RPC, кэш с версией, второй разбор solders, сборка MessageV0 (S11, §9.1).

Истина для раскрытия — не наш код, а ответ RPC: meta.loadedAddresses тех же mainnet-транзакций (tests/data/solana),
таблицы — сохранённые getAccountInfo тех же ALT (fetch_alt.py)."""
import base64, dataclasses
import pytest
from funding_bot.trade.solana import ALT_PROGRAM, TOKEN_PROGRAM, wire
from funding_bot.trade.solana import message as M
import sol_tx_helpers as H

REAL = ["tx_jup_buy_b64.json", "tx_jup_sell_b64.json", "tx_failed_jup_b64.json", "tx_ata_create_b64.json"]


@pytest.mark.parametrize("name", REAL)
def test_s11_resolved_keys_equal_rpc_loaded_addresses(name):
    res = M.RpcMessageResolver(M.AltCache(H.AltRpc()))
    w = H.fixture_tx(name)
    rm = res.resolve(w)
    la = H.fx_result(name)["meta"]["loadedAddresses"]
    assert rm.keys[:rm.n_static] == w.message.static_keys
    assert rm.keys[rm.n_static:] == tuple(la["writable"] + la["readonly"])      # независимая истина — RPC
    assert set(la["writable"]) <= rm.writable and not set(la["readonly"]) & rm.writable
    assert rm.message_hash == w.message.message_hash and len(rm.alts) == len(w.message.lookups)
    assert all(len(s.content_hash) == 64 and s.owner == ALT_PROGRAM for s in rm.alts)


def test_alt_parse_real_and_second_parser():
    rpc = H.AltRpc()
    addr = "2g9GUb86EvcPpdn574Rdz9xoWDy77AQrp8ouyQfMLYxi"
    v, slot = rpc.values[addr]
    s = M.parse_alt(addr, v, slot)
    assert s.deactivation_slot is None and len(s.addresses) == (7928 - 56) // 32 and s.slot == slot
    assert s.version == (len(s.addresses), s.content_hash)
    raw = bytearray(base64.b64decode(v["data"][0]))
    for bad, code in ((dict(v, owner=TOKEN_PROGRAM), "alt_owner"), (None, "alt_missing"),
                      (dict(v, data=[base64.b64encode(bytes(raw[:-5])).decode(), "base64"]), "alt_layout"),
                      (dict(v, data=[v["data"][0], "base58"]), "alt_layout"),
                      (H.alt_value(s.addresses[:3], typ=0), "alt_layout")):
        with pytest.raises(M.MessageError) as e:
            M.parse_alt(addr, bad, slot)
        assert e.value.code == code


def test_alt_deactivated_or_not_yet_active_refused():
    t = H.key("alt-d")
    raw = H.build(H.std_ixs(), alts=[H.snap(t, H.POOL)])
    m = wire.parse_message(raw)
    dead = M.parse_alt(t, H.alt_value(H.POOL, deact=900), 1000)
    with pytest.raises(M.MessageError) as e:
        M.resolve_with(m, {t: dead})
    assert e.value.code == "alt_deactivated"
    fresh = M.parse_alt(t, H.alt_value(H.POOL, last_ext=1000, start=2), 1000)   # дописаны в слоте чтения
    assert fresh.active_len() == 2
    with pytest.raises(M.MessageError) as e:
        M.resolve_with(m, {t: fresh})
    assert e.value.code == "alt_index"
    with pytest.raises(M.MessageError) as e:
        M.resolve_with(m, {})
    assert e.value.code == "alt_missing"


def test_alt_cache_versions_and_content_change():
    t = H.key("alt-c")
    rpc = H.AltRpc({t: H.alt_value(H.POOL[:3])}, slot=1000, real=False)
    clock = [0.0]
    c = M.AltCache(rpc, max_age_s=30, clock=lambda: clock[0])
    s1 = c.get({t: 2})[t]
    assert c.get({t: 1})[t] is s1 and c.fetches == 1                     # из кэша
    rpc.values[t] = (H.alt_value(H.POOL[:5]), 1001)                      # таблицу дописали
    s2 = c.get({t: 4})[t]                                                 # индекс вне кэша — перечитать
    assert c.fetches == 2 and s2.addresses[:3] == s1.addresses and s2.version != s1.version
    clock[0] = 31
    c.get({t: 0})
    assert c.fetches == 3                                                 # срок записи вышел
    rpc.values[t] = (H.alt_value([H.FOREIGN] + H.POOL[1:5]), 1002)        # «задним числом» другой адрес
    clock[0] = 70
    with pytest.raises(M.MessageError) as e:
        c.get({t: 0})
    assert e.value.code == "alt_content_changed" and c.peek(t) is None    # запись сброшена, не «молча другие»


def test_s11_alt_substitution_changes_resolution_and_is_seen():
    """Те же байты сообщения, другое содержимое таблицы — раскрытие видит подменённый счёт (а статические ключи нет)."""
    t = H.key("alt-s")
    good = H.snap(t, H.POOL[:4] + [H.ANSEM_ATA])
    raw = H.build(H.std_ixs(), alts=[good])
    m = wire.parse_message(raw)
    assert H.ANSEM_ATA not in m.static_keys                               # получатель — только через ALT
    evil = H.snap(t, H.POOL[:4] + [H.FOREIGN])
    rm = M.resolve_with(m, {t: evil})
    assert H.FOREIGN in rm.keys and H.ANSEM_ATA not in rm.keys


def test_keys_loaded_twice_refused():
    from solders.hash import Hash
    from solders.instruction import CompiledInstruction
    from solders.message import MessageAddressTableLookup, MessageHeader, MessageV0, to_bytes_versioned
    from solders.pubkey import Pubkey
    t = H.key("alt-dup")
    keys = [Pubkey.from_string(k) for k in (H.WALLET, H.POOL[0], TOKEN_PROGRAM)]
    ix = CompiledInstruction(2, bytes([3]), bytes([0, 1]))
    msg = MessageV0(MessageHeader(1, 0, 1), keys, Hash.from_string(H.BH), [ix],
                    [MessageAddressTableLookup(Pubkey.from_string(t), bytes([0]), bytes())])
    m = wire.parse_message(to_bytes_versioned(msg))
    with pytest.raises(M.MessageError) as e:
        M.resolve_with(m, {t: H.snap(t, [H.WALLET])})                     # ALT грузит статический ключ
    assert e.value.code == "keys_duplicate"


def test_parsers_must_agree(monkeypatch):
    w = H.fixture_tx("tx_ata_create_b64.json")
    M.cross_parse(w.message)                                              # настоящие байты: оба парсера согласны
    other = wire.parse_message(H.build(H.std_ixs()))
    import solders.message as sm
    real = sm.from_bytes_versioned
    monkeypatch.setattr(sm, "from_bytes_versioned", lambda b: real(bytes(other.raw)))
    with pytest.raises(M.MessageError) as e:
        M.cross_parse(w.message)
    assert e.value.code == "parsers_disagree"


def test_builder_round_trip_real_instructions():
    res = M.RpcMessageResolver(M.AltCache(H.AltRpc()))
    w = H.fixture_tx("tx_ata_create_b64.json")
    rm = res.resolve(w)
    ixs = [(ix.program_id, [(rm.keys[a], rm.keys[a] in rm.signers, rm.keys[a] in rm.writable) for a in wi.accounts],
            ix.data) for ix, wi in zip(rm.instructions, w.message.instructions)]
    raw = M.SoldersMessageBuilder().build_v0(payer=rm.payer, instructions=ixs, recent_blockhash=rm.recent_blockhash,
                                             alts=rm.alts)
    rb = M.resolve_with(wire.parse_message(raw), {s.address: s for s in rm.alts})
    assert rb.instructions == rm.instructions and rb.signers == rm.signers and rb.payer == rm.payer
    assert set(rb.keys) == set(rm.keys) and rb.writable == rm.writable


def test_builder_refuses_deactivated_table_and_bad_input():
    t = H.key("alt-b")
    dead = dataclasses.replace(H.snap(t, H.POOL), deactivation_slot=5)
    with pytest.raises(M.MessageError):
        H.build(H.std_ixs(), alts=[dead])
    with pytest.raises(M.MessageError):
        H.build(H.std_ixs(), blockhash="not-a-hash")


def test_resolver_uses_cache_between_calls():
    rpc = H.AltRpc()
    res = M.RpcMessageResolver(M.AltCache(rpc))
    w = H.fixture_tx("tx_jup_buy_b64.json")
    a, b = res.resolve(w), res.resolve(w)
    assert a == b and len(rpc.calls) == 1


def test_unavailable_still_refuses():
    from funding_bot.trade.solana import WaitsSolders
    with pytest.raises(WaitsSolders):
        M.UNAVAILABLE.resolve(H.fixture_tx("tx_jup_buy_b64.json"))
