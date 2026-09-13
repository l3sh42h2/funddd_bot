"""Разбор секрета Solana (формат владельца base58 64 байта и keypair-файл): сверка половин, отказ чужих форм
без эха значения (S19, M08 для этого слоя). Ключ — открытый вектор RFC 8032, не ключ кошелька."""
import copy, json, os, pickle
import pytest
from funding_bot.trade.solana import b58
from funding_bot.trade.solana.keypair import SolanaKeyError, load_keypair_file, parse_secret_b58
from solana_helpers import RFC_PUB, RFC_SEED

SECRET_B58 = b58.b58encode(RFC_SEED + RFC_PUB)


def test_owner_format_parses():
    s = parse_secret_b58(SECRET_B58)
    assert s.public_key() == b58.b58encode(RFC_PUB) and s.with_seed(bytes) == RFC_SEED
    assert parse_secret_b58("  " + SECRET_B58 + "\n").public_key() == s.public_key()   # хвост строки .env


def test_secret_never_printed_or_copied():
    s = parse_secret_b58(SECRET_B58)
    for text in (repr(s), str(s), f"{s}", f"{s:>10}"):
        assert SECRET_B58 not in text and RFC_SEED.hex() not in text and s.public_key() in text
    for f in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            f(s)


def test_foreign_forms_refused_without_echo():           # S19: не та роль/формат
    cases = {b58.b58encode(RFC_SEED + bytes(32)): "публичная половина",
             "0x" + RFC_SEED.hex(): "EVM", RFC_SEED.hex(): "EVM",
             b58.b58encode(RFC_SEED): "32 байта",
             json.dumps(list(RFC_SEED + RFC_PUB)): "JSON",
             SECRET_B58 + "0": "не base58",
             b58.b58encode(RFC_SEED + RFC_PUB + b"\1"): "65 байт",
             "": "пусто"}
    for raw, word in cases.items():
        with pytest.raises(SolanaKeyError) as e:
            parse_secret_b58(raw, "SOLANA_SECRET_B58")
        msg = str(e.value)
        assert word in msg and "SOLANA_SECRET_B58" in msg, (word, msg)
        assert RFC_SEED.hex() not in msg and (not raw or raw not in msg)


def test_keypair_file(tmp_path):
    p = tmp_path / "id.json"
    p.write_text(json.dumps(list(RFC_SEED + RFC_PUB)))
    os.chmod(p, 0o600)
    assert load_keypair_file(p).public_key() == b58.b58encode(RFC_PUB)
    os.chmod(p, 0o644)
    with pytest.raises(SolanaKeyError, match="0600"):
        load_keypair_file(p)
    assert load_keypair_file(p, check_perms=False).public_key() == b58.b58encode(RFC_PUB)
    for content in ("not json", json.dumps({"k": 1}), json.dumps(list(RFC_SEED + RFC_PUB)[:-1] + [256]),
                    json.dumps([True] * 64), json.dumps(list(RFC_SEED + bytes(32))), json.dumps(list(RFC_SEED))):
        p.write_text(content)
        os.chmod(p, 0o600)
        with pytest.raises(SolanaKeyError) as e:
            load_keypair_file(p)
        assert RFC_SEED.hex() not in str(e.value) and "157, 97, 177" not in str(e.value)
    with pytest.raises(SolanaKeyError, match="недоступен"):
        load_keypair_file(tmp_path / "nope.json")
    with pytest.raises(SolanaKeyError, match="не обычный"):
        load_keypair_file(tmp_path)


def test_evm_loader_refuses_solana_secret():             # S19: секрет Solana в слот EVM/Aster — отказ без эха
    keys = pytest.importorskip("funding_bot.trade.keys")
    eth_account = pytest.importorskip("eth_account")
    with pytest.raises(keys.KeysError) as e:
        keys._from_key(eth_account.Account, SECRET_B58, "DEX_EVM_KEY")
    assert SECRET_B58 not in str(e.value)
