"""Solana-ядро: base58, Ed25519 (RFC 8032 + сверка с независимой реализацией pycryptodome), разбор провода по
реальным транзакциям mainnet, явная кодировка payload (G13), PDA/ATA против адресов, которые создала и приняла
сама ATA-программа в сети (S07), регистр адреса (S02)."""
import base64, hashlib
import pytest
from funding_bot.trade.solana import (ANSEM_MINT, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT, b58, ed25519,
                                      wire)
from funding_bot.trade.solana.accounts import ata, create_program_address, find_program_address
from funding_bot.trade.solana.receipt import parse_receipt, wire_of
from solana_helpers import RFC_PUB, RFC_SEED, fx

TX = ("jup_buy", "jup_sell", "ata_create", "failed", "failed_jup")
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
OKX = "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma"


# --- base58 --------------------------------------------------------------------------------------
def test_b58_roundtrip_and_leading_zeros():
    for b in (b"", b"\0", b"\0\0\x01", bytes(range(32)), b"\xff" * 64):
        assert b58.b58decode(b58.b58encode(b)) == b
    assert b58.b58encode(b"\0" * 32) == "1" * 32
    assert b58.pubkey_bytes("11111111111111111111111111111111") == b"\0" * 32
    assert b58.check_pubkey(ANSEM_MINT) is ANSEM_MINT or b58.check_pubkey(ANSEM_MINT) == ANSEM_MINT


def test_b58_strict_and_no_echo():
    bad = "0OIl" + "A" * 40
    with pytest.raises(b58.Base58Error) as e:
        b58.b58decode(bad)
    assert bad not in str(e.value)
    for s in (ANSEM_MINT + " ", " " + ANSEM_MINT, ANSEM_MINT[:-2], "", USDC_MINT + "1"):
        assert not b58.is_pubkey(s)
    with pytest.raises(b58.Base58Error):
        b58.signature_bytes(ANSEM_MINT)


def test_case_variant_is_another_key():                 # S02: регистр — часть адреса
    variant = ANSEM_MINT.replace("pump", "PUMP")
    assert variant != ANSEM_MINT
    try:
        vb = b58.pubkey_bytes(variant)
    except b58.Base58Error:
        vb = None
    assert vb != b58.pubkey_bytes(ANSEM_MINT)
    assert variant.lower() != ANSEM_MINT and ANSEM_MINT.lower() != ANSEM_MINT


# --- Ed25519 -------------------------------------------------------------------------------------
RFC = [   # RFC 8032 §7.1 TEST 1–3
    (RFC_SEED.hex(), RFC_PUB.hex(), "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("seed,pub,msg,sig", RFC)
def test_rfc8032_vectors(seed, pub, msg, sig):
    s, p, m, g = map(bytes.fromhex, (seed, pub, msg, sig))
    assert ed25519.public_from_seed(s) == p
    assert ed25519.verify(p, m, g)
    assert not ed25519.verify(p, m + b"\0", g)
    flipped = bytearray(g)
    flipped[0] ^= 1
    assert not ed25519.verify(p, m, bytes(flipped))
    s_plus_l = g[:32] + (int.from_bytes(g[32:], "little") + ed25519.L).to_bytes(32, "little")
    assert not ed25519.verify(p, m, s_plus_l)            # S ≥ L — отказ (податливость)
    assert not ed25519.verify(p[:31], m, g) and not ed25519.verify(p, m, g[:63])


def test_matches_pycryptodome():
    ECC = pytest.importorskip("Crypto.PublicKey.ECC")
    eddsa = pytest.importorskip("Crypto.Signature.eddsa")
    for i in range(10):
        seed = hashlib.sha256(b"seed-%d" % i).digest()
        k = ECC.construct(curve="Ed25519", seed=seed)
        pub = k.public_key().export_key(format="raw")
        assert ed25519.public_from_seed(seed) == pub
        msg = hashlib.sha512(b"m-%d" % i).digest()[:i * 6]
        assert ed25519.verify(pub, msg, eddsa.new(k, "rfc8032").sign(msg))


def test_on_curve_matches_pycryptodome_decoding():
    eddsa = pytest.importorskip("Crypto.Signature.eddsa")
    n_on = 0
    for i in range(64):
        h = hashlib.sha256(b"pt-%d" % i).digest()
        try:
            eddsa.import_public_key(h)
            ref = True
        except ValueError:
            ref = False
        assert ed25519.is_on_curve(h) is ref
        n_on += ref
    assert 10 < n_on < 54                                # примерно половина хешей — точки кривой
    with pytest.raises(ValueError):
        ed25519.is_on_curve(b"\0" * 31)


def test_wallets_on_curve_atas_off_curve():
    doc = fx("token_accounts_jsonParsed.json")
    for v in doc["result"]["value"]:
        if v is None:
            continue
        owner = v["data"]["parsed"]["info"]["owner"]
        assert ed25519.is_on_curve(b58.pubkey_bytes(owner))       # кошелёк — точка кривой
    for addr in fx_params("token_accounts_jsonParsed.json"):
        assert not ed25519.is_on_curve(b58.pubkey_bytes(addr))    # ATA — PDA, вне кривой


def fx_params(name):
    from solana_helpers import fx_doc
    return fx_doc(name)["params"][0]


# --- провод --------------------------------------------------------------------------------------
@pytest.mark.parametrize("nm", TX)
def test_wire_matches_rpc_json(nm):
    w = wire_of(fx(f"tx_{nm}_b64.json")["result"])
    j = fx(f"tx_{nm}.json")["result"]
    m, jm = w.message, j["transaction"]["message"]
    assert [b58.b58encode(s) for s in w.signatures] == j["transaction"]["signatures"]
    assert w.signature == j["transaction"]["signatures"][0]
    assert list(m.static_keys) == jm["accountKeys"]
    assert m.recent_blockhash == jm["recentBlockhash"]
    assert m.version == j["version"]
    h = jm["header"]
    assert (m.num_required_signatures, m.num_readonly_signed, m.num_readonly_unsigned) == \
        (h["numRequiredSignatures"], h["numReadonlySignedAccounts"], h["numReadonlyUnsignedAccounts"])
    assert [(ix.program_index, list(ix.accounts), ix.data) for ix in m.instructions] == \
        [(ix["programIdIndex"], ix["accounts"], b58.b58decode(ix["data"])) for ix in jm["instructions"]]
    assert [(lk.table, list(lk.writable), list(lk.readonly)) for lk in m.lookups] == \
        [(lk["accountKey"], lk["writableIndexes"], lk["readonlyIndexes"]) for lk in jm.get("addressTableLookups", [])]
    assert m.n_loaded == len(j["meta"]["loadedAddresses"]["writable"]) + len(j["meta"]["loadedAddresses"]["readonly"])
    assert w.fully_signed and all(w.signature_ok())             # подписи mainnet проходят нашу проверку
    assert m.message_hash == hashlib.sha256(m.raw).hexdigest()
    assert m.fee_payer == jm["accountKeys"][0] and m.static_writable(0)


def test_wire_signature_check_catches_tamper():
    raw = bytearray(base64.b64decode(fx("tx_jup_sell_b64.json")["result"]["transaction"][0]))
    raw[-1] ^= 1                                          # байт message изменён — подпись уже не его
    assert wire.parse_transaction(bytes(raw)).signature_ok() == (False,)
    zero = wire.parse_transaction(bytes(raw[:1]) + b"\0" * 64 + bytes(raw[65:]))
    assert not zero.fully_signed and zero.signature_ok() == (False,)


def test_encoding_is_explicit():                         # G13
    raw_b64 = fx("tx_jup_sell_b64.json")["result"]["transaction"][0]
    raw = base64.b64decode(raw_b64)
    raw_b58 = b58.b58encode(raw)
    assert wire.decode(raw_b64, kind="tx", encoding="base64").raw == raw           # Jupiter: base64
    assert wire.decode(raw_b58, kind="tx", encoding="base58").raw == raw           # OKX /swap tx.data: base58
    for s, enc in ((raw_b64, "base58"), (raw_b58, "base64")):                       # перепутанные — отказ
        with pytest.raises(wire.WireError):
            wire.decode(s, kind="tx", encoding=enc)
    data = b"\x02\0\0\0" + (5).to_bytes(8, "little")
    assert wire.decode(base64.b64encode(data).decode(), kind="instruction_data", encoding="base64") == data
    for bad in ("", raw_b64 + "\n", " " + raw_b64, raw_b64.rstrip("=") if raw_b64.endswith("=") else raw_b64 + "=",
                raw_b64.replace("+", "-").replace("/", "_") if ("+" in raw_b64 or "/" in raw_b64) else "***"):
        with pytest.raises(wire.WireError):
            wire.decode(bad, kind="tx", encoding="base64")
    with pytest.raises(wire.WireError):
        wire.decode(raw_b64, kind="tx", encoding="hex")
    with pytest.raises(wire.WireError):
        wire.decode(raw_b64, kind="bundle", encoding="base64")
    msg_b64 = base64.b64encode(wire.parse_transaction(raw).message.raw).decode()
    with pytest.raises(wire.WireError):                   # сообщение, поданное как транзакция
        wire.decode(msg_b64, kind="tx", encoding="base64")
    assert wire.decode(msg_b64, kind="message", encoding="base64").raw == wire.parse_transaction(raw).message.raw


def test_wire_strictness():
    raw = base64.b64decode(fx("tx_jup_sell_b64.json")["result"]["transaction"][0])
    w = wire.parse_transaction(raw)
    with pytest.raises(wire.WireError, match="лишние"):
        wire.parse_transaction(raw + b"\0")
    with pytest.raises(wire.WireError):
        wire.parse_transaction(raw[:-1])
    msg = bytearray(w.message.raw)
    msg[0] = 0x81                                         # версия 1 — не поддержана
    with pytest.raises(wire.WireError, match="версия"):
        wire.parse_message(bytes(msg))
    with pytest.raises(wire.WireError, match="подписей"):
        wire.parse_transaction(wire.encode_compact_u16(2) + w.signatures[0] * 2 + w.message.raw)
    with pytest.raises(wire.WireError):
        wire.parse_transaction(b"\x00" + w.message.raw)
    for bad in (b"\x80\x00", b"\xff\xff\x04", b"\x80", b"\xff\xff\xff\x00"):
        with pytest.raises(wire.WireError):
            wire.compact_u16(bad, 0)
    assert wire.compact_u16(b"\xff\xff\x03", 0) == (0xFFFF, 3)
    for n in (0, 1, 127, 128, 16383, 16384, 65535):
        enc = wire.encode_compact_u16(n)
        assert wire.compact_u16(enc, 0) == (n, len(enc))


def test_wire_header_rules():
    from solana_helpers import legacy_transfer
    bh = fx("tx_jup_sell.json")["result"]["transaction"]["message"]["recentBlockhash"]
    payer, dest = RFC_PUB, hashlib.sha256(b"dest").digest()
    m = wire.parse_message(legacy_transfer(payer, dest, 5, bh))
    assert m.version == "legacy" and m.signers == (b58.b58encode(payer),)
    assert [m.static_writable(i) for i in range(3)] == [True, True, False]
    good = legacy_transfer(payer, dest, 5, bh)
    prog0 = bytearray(good)
    prog0[-(1 + 1 + 2 + 1 + 12)] = 0                     # программа = плательщик
    with pytest.raises(wire.WireError, match="программа"):
        wire.parse_message(bytes(prog0))
    ro_payer = bytearray(good)
    ro_payer[1] = 1                                       # плательщик только для чтения
    with pytest.raises(wire.WireError, match="плательщик"):
        wire.parse_message(bytes(ro_payer))
    dup = legacy_transfer(payer, payer, 5, bh)            # повтор ключа
    with pytest.raises(wire.WireError, match="повтор"):
        wire.parse_message(dup)
    with pytest.raises(wire.WireError):
        wire.assemble([b"\0" * 64, b"\0" * 64], good)     # подписей больше, чем в заголовке


# --- PDA / ATA -----------------------------------------------------------------------------------
def test_ata_equals_accounts_accepted_by_ata_program():   # S07: эталон — сеть, а не наша функция
    doc = fx("token_accounts_jsonParsed.json")
    addrs = fx_params("token_accounts_jsonParsed.json")
    n = 0
    for addr, v in zip(addrs, doc["result"]["value"]):
        if v is None:
            continue
        info = v["data"]["parsed"]["info"]
        assert ata(info["owner"], info["mint"], v["owner"]) == addr
        n += 1
        if v["owner"] == TOKEN_2022_PROGRAM:              # под классической программой — другой адрес
            assert ata(info["owner"], info["mint"], TOKEN_PROGRAM) != addr
    assert n >= 5
    # ATA, созданный в самой транзакции инструкцией ATA-программы (она проверяет PDA ончейн)
    rc = parse_receipt(fx("tx_ata_create.json")["result"])
    created = [a for a in rc.token_accounts if a.created and a.mint == ANSEM_MINT]
    assert len(created) == 1 and created[0].program == TOKEN_2022_PROGRAM
    assert ata(created[0].owner, ANSEM_MINT, TOKEN_2022_PROGRAM) == created[0].address
    with pytest.raises(ValueError):
        ata(created[0].owner, ANSEM_MINT, JUP)


def test_ata_over_all_fixture_token_accounts():
    matched = 0
    for nm in TX:
        rc = parse_receipt(fx(f"tx_{nm}.json")["result"])
        for a in rc.token_accounts:
            if a.owner and a.program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) and \
                    ata(a.owner, a.mint, a.program) == a.address:
                matched += 1
    assert matched >= 25


def test_anchor_idl_addresses_from_onchain_probe():
    """PDA([], program) + createWithSeed("anchor:idl") — адреса IDL, которые реально читались getAccountInfo
    (sol_plan/probe_idl*_20260913.json): независимая проверка вывода PDA на двух программах."""
    for prog, idl in ((JUP, "C88XWfp26heEmDkmfSzeXP7Fd7GQJ2j9dDTUsyiZbUTa"),
                      (OKX, "CwtKR21uLb3toNmRGqk5y2zqbjVJeVtnQnjn7njcRKu")):
        base, bump = find_program_address([], prog)
        assert 0 <= bump <= 255
        assert b58.b58encode(hashlib.sha256(b58.pubkey_bytes(base) + b"anchor:idl" +
                                            b58.pubkey_bytes(prog)).digest()) == idl
    with pytest.raises(ValueError):
        create_program_address([b"x" * 33], JUP)
