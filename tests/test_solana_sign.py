"""Подпись только закреплённого сообщения (§9.1, G14): SeedSigner без бэкенда не подписывает (ждёт solders),
с бэкендом — сверяет ключ и каждую подпись; интерфейсы message/validate честно отказывают. Стык с потоком
profiles — keys.solana_signer(). Бэкенд в тестах — pycryptodome, ключ — открытый вектор RFC 8032."""
import copy, hashlib, pickle
import pytest
from funding_bot.trade.solana import WaitsSolders, b58, ed25519, message, validate, wire
from funding_bot.trade.solana.keypair import parse_secret_b58
from funding_bot.trade.solana.sign import SeedSigner, SignError, SolanaSigner, sign_validated
from solana_helpers import RFC_PUB, RFC_SEED, PcdBackend, PcdSigner, fx, legacy_transfer

BH = fx("tx_jup_sell.json")["result"]["transaction"]["message"]["recentBlockhash"]
DEST = hashlib.sha256(b"dest").digest()
SECRET = parse_secret_b58(b58.b58encode(RFC_SEED + RFC_PUB))


def test_no_backend_nothing_signed():
    with pytest.raises(WaitsSolders):
        SeedSigner(SECRET)


def test_seed_signer_checks_backend():
    s = SeedSigner(SECRET, PcdBackend)
    assert isinstance(s, SolanaSigner) and s.public_key() == b58.b58encode(RFC_PUB)
    assert ed25519.verify(RFC_PUB, b"abc", s.sign_message(b"abc"))
    with pytest.raises(SignError, match="другой публичный"):
        SeedSigner(SECRET, lambda seed: PcdBackend(hashlib.sha256(seed).digest()))

    class Liar(PcdBackend):
        def sign(self, m):
            return b"\1" * 64

    with pytest.raises(SignError, match="проверку"):
        SeedSigner(SECRET, Liar).sign_message(b"abc")
    assert RFC_SEED.hex() not in repr(s) and s.public_key() in repr(s)
    for f in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            f(s)


def test_signs_only_pinned_single_signer_message():
    s = SeedSigner(SECRET, PcdBackend)
    m = legacy_transfer(RFC_PUB, DEST, 5, BH)
    h = wire.message_hash(m)
    w = wire.parse_transaction(sign_validated(s, m, h))
    assert w.message.raw == m and w.message.message_hash == h and w.signature_ok() == (True,)
    assert w.message.fee_payer == s.public_key()
    with pytest.raises(SignError, match="изменилось"):
        sign_validated(s, legacy_transfer(RFC_PUB, DEST, 6, BH), h)
    other = legacy_transfer(DEST, RFC_PUB, 5, BH)
    with pytest.raises(SignError, match="плательщик"):
        sign_validated(s, other, wire.message_hash(other))
    two = legacy_transfer(RFC_PUB, DEST, 5, BH, extra_signer=hashlib.sha256(b"mm").digest())
    with pytest.raises(SignError, match="внешние"):        # G14: внешние подписанты — не в первом выпуске
        sign_validated(s, two, wire.message_hash(two))


def test_bad_signer_output_never_reaches_journal():
    m = legacy_transfer(RFC_PUB, DEST, 5, BH)
    h = wire.message_hash(m)

    class Liar(PcdSigner):
        def sign_message(self, message):
            return b"\1" * 64

    class WrongKey(PcdSigner):                             # называет наш адрес, подписывает другим ключом
        def public_key(self):
            return b58.b58encode(RFC_PUB)

    class NotAKey(PcdSigner):
        def public_key(self):
            return "not-a-key"

    for bad in (Liar(RFC_SEED), WrongKey(hashlib.sha256(b"other").digest())):
        with pytest.raises(SignError, match="проверку"):
            sign_validated(bad, m, h)
    with pytest.raises(SignError, match="не адрес"):
        sign_validated(NotAKey(RFC_SEED), m, h)


def test_keys_solana_key_meets_contract():               # стык с keys.py (поток profiles): SolanaKey из секрета
    keys = pytest.importorskip("funding_bot.trade.keys")
    k = keys.SolanaKey.from_secret(SECRET)
    assert isinstance(k, SolanaSigner) and k.public_key() == SECRET.public_key()
    m = legacy_transfer(RFC_PUB, DEST, 7, BH)
    assert wire.parse_transaction(sign_validated(k, m, wire.message_hash(m))).signature_ok() == (True,)
    with pytest.raises(SignError, match="изменилось"):
        sign_validated(k, legacy_transfer(RFC_PUB, DEST, 8, BH), wire.message_hash(m))


def test_interfaces_refuse_until_solders():
    m = legacy_transfer(RFC_PUB, DEST, 5, BH)
    w = wire.parse_transaction(sign_validated(PcdSigner(RFC_SEED), m, wire.message_hash(m)))
    with pytest.raises(WaitsSolders):
        message.UNAVAILABLE.resolve(w)
    with pytest.raises(WaitsSolders):
        message.UNAVAILABLE.build_v0(payer=w.message.fee_payer, instructions=(), recent_blockhash=BH, alts=())
    intent = validate.SwapIntent(wallet=w.message.fee_payer, input_mint="i", input_program="p", input_account="a",
                                 output_mint="o", output_program="p", output_account="b", amount_in_raw=1,
                                 min_out_raw=1, cu_limit_cap=None, cu_price_cap_micro_lamports=None,
                                 tip_cap_lamports=None, native_budget_lamports=None)
    for v in (validate.REFUSING.validate(None, intent), validate.REFUSING.check_simulation({}, intent, {})):
        assert not v.ok and "solders" in v.reasons[0] and v.message_hash is None


# --- solders: бэкенд подписи и полный набор условий подписи (поток solders) ------------------------------------------
from funding_bot.trade.solana.sign import BlockhashRef, expiry_reasons, sign_checked, solders_backend, solders_signer


def test_solders_backend_signs_and_never_shows_secret():
    s = solders_signer(SECRET)
    assert s.public_key() == SECRET.public_key() and isinstance(s, SolanaSigner)
    m = legacy_transfer(RFC_PUB, DEST, 5, BH)
    sig = s.sign_message(m)
    assert ed25519.verify(RFC_PUB, m, sig) and sig == PcdSigner(RFC_SEED).sign_message(m)   # RFC 8032 детерминирован
    be = solders_backend(RFC_SEED)
    full = b58.b58encode(RFC_SEED + RFC_PUB)
    for text in (repr(s), str(s), repr(be), str(be), f"{be}"):
        assert full not in text and RFC_SEED.hex() not in text
    for f in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            f(be)
    with pytest.raises(SignError):
        solders_backend(b"\1" * 31)


def _checked_message():
    import sol_tx_helpers as H
    from funding_bot.trade.solana.validate import ManifestValidator
    raw = H.build(H.std_ixs())
    rm = H.resolved(raw)
    mv = ManifestValidator()
    plan = mv.validate(rm, H.intent(recent_blockhash=H.BH))
    sim, pre = H.good_sim()
    eff = mv.check_simulation(sim, H.intent(), pre, msg=rm, plan=plan)
    assert plan.ok and eff.ok
    return H, raw, plan, eff


def test_sign_checked_full_path_v0():
    from solders.transaction import VersionedTransaction
    H, raw, plan, eff = _checked_message()
    s = solders_signer(SECRET)
    ref = BlockhashRef(H.BH, 1_000)
    tx = sign_checked(s, raw, plan, eff, blockhash=ref, block_height=900, min_validity_heights=50)
    w = wire.parse_transaction(tx)
    assert w.message.raw == raw and w.signature_ok() == (True,) and w.message.version == 0
    vt = VersionedTransaction.from_bytes(tx)                # независимая проверка подписи solders
    assert all(vt.verify_with_results()) and str(vt.signatures[0]) == w.signature


@pytest.mark.parametrize("change,why", [
    (dict(plan_ok=False), "validation_failed"), (dict(eff=None), "simulation_missing"),
    (dict(ref_hash="other"), "blockhash_mismatch"), (dict(exact=False), "lvbh_not_exact"),
    (dict(height=1_001), "blockhash_expired"), (dict(height=960), "blockhash_expiring"),
    (dict(height=None), "block_height_unknown"), (dict(minh=None), "limit_missing:min_blockhash_validity_heights"),
    (dict(other_msg=True), "validation_other_message"), (dict(manifest="x"), "manifest_mismatch"),
])
def test_sign_checked_refuses_before_signing(change, why):
    import dataclasses
    H, raw, plan, eff = _checked_message()
    calls = []

    class Spy(PcdSigner):
        def sign_message(self, message):
            calls.append(1)
            return super().sign_message(message)
    if change.get("plan_ok") is False:
        plan = dataclasses.replace(plan, ok=False, reasons=("ix_min_out",))
    if "eff" in change:
        eff = change["eff"]
    if change.get("manifest"):
        eff = dataclasses.replace(eff, manifest_version=change["manifest"])
    msg = raw
    if change.get("other_msg"):
        msg = H.build(H.std_ixs(H.route_v2(slip=40)))
    ref = BlockhashRef(H.key("x") if change.get("ref_hash") else H.BH, 1_000, change.get("exact", True))
    with pytest.raises(SignError) as e:
        sign_checked(Spy(RFC_SEED), msg, plan, eff, blockhash=ref, block_height=change.get("height", 900),
                     min_validity_heights=change.get("minh", 50))
    assert why in str(e.value) and calls == [] and RFC_SEED.hex() not in str(e.value)


def test_expiry_reasons_exact_blockhash():
    m = legacy_transfer(RFC_PUB, DEST, 5, BH)
    assert expiry_reasons(m, BlockhashRef(BH, 100), block_height=40, min_validity_heights=60) == ()
    assert expiry_reasons(m, BlockhashRef(BH, 100), block_height=41, min_validity_heights=60) == ("blockhash_expiring",)
    assert expiry_reasons(m, None, block_height=1, min_validity_heights=1) == ("blockhash_unknown",)
