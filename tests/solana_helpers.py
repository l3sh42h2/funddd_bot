"""Общее для tests/test_solana_*.py: фикстуры mainnet (tests/data/solana — публичный read-only RPC 13.09, скрипты
сбора fetch.py/fetch2.py рядом), сборка простого legacy-сообщения, тестовый подписант, подделка HTTP для RPC.

Подписант в тестах — pycryptodome (независимая реализация Ed25519, та же, что в keys.SolanaKey). Ключи — открытые
векторы RFC 8032 и seed из sha256 тестовой строки, не ключи кошельков."""
import json, struct
from pathlib import Path
from funding_bot.trade.solana import MAINNET_GENESIS, SYSTEM_PROGRAM, b58, wire

DATA = Path(__file__).parent / "data" / "solana"
# RFC 8032 §7.1 TEST 1 — открытый тестовый вектор
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUB = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")


def fx(name: str):
    """Ответ JSON-RPC из фикстуры (целиком: jsonrpc/id/result)."""
    return json.loads((DATA / name).read_text())["response"]


def fx_doc(name: str):
    return json.loads((DATA / name).read_text())


def legacy_transfer(payer: bytes, dest: bytes, lamports: int, blockhash: str, *,
                    extra_signer: bytes | None = None) -> bytes:
    """Байты legacy-сообщения: один System Transfer payer → dest."""
    keys = [payer] + ([extra_signer] if extra_signer else []) + [dest, b58.pubkey_bytes(SYSTEM_PROGRAM)]
    nsig = 2 if extra_signer else 1
    data = struct.pack("<IQ", 2, lamports)
    ix = (bytes([len(keys) - 1]) + wire.encode_compact_u16(2) + bytes([0, len(keys) - 2])
          + wire.encode_compact_u16(len(data)) + data)
    return (bytes([nsig, 0, 1]) + wire.encode_compact_u16(len(keys)) + b"".join(keys) + b58.pubkey_bytes(blockhash)
            + wire.encode_compact_u16(1) + ix)


class PcdSigner:
    """Подписант на pycryptodome (только тесты): public_key() и sign_message() как у keys.SolanaKey."""

    def __init__(self, seed: bytes):
        from Crypto.PublicKey import ECC
        self._k = ECC.construct(curve="Ed25519", seed=seed)
        self._pub = self._k.public_key().export_key(format="raw")

    def public_key(self) -> str:
        return b58.b58encode(self._pub)

    def sign_message(self, message: bytes) -> bytes:
        from Crypto.Signature import eddsa
        return eddsa.new(self._k, "rfc8032").sign(bytes(message))


class PcdBackend:
    """Бэкенд для sign.SeedSigner на pycryptodome (только тесты; в проде — solders Keypair.from_seed)."""

    def __init__(self, seed: bytes):
        from Crypto.PublicKey import ECC
        self._k = ECC.construct(curve="Ed25519", seed=seed)

    def public_key_bytes(self) -> bytes:
        return self._k.public_key().export_key(format="raw")

    def sign(self, message: bytes) -> bytes:
        from Crypto.Signature import eddsa
        return eddsa.new(self._k, "rfc8032").sign(message)


# --- подделка HTTP для SolanaRpc -----------------------------------------------------------------
class Resp:
    def __init__(self, status: int = 200, body=None, text: str | None = None):
        self.status_code, self._body, self._text = status, body, text

    def json(self):
        if self._text is not None:
            raise ValueError("не JSON")
        return self._body


def rpc_error(code, message, data=None):
    return lambda p, rid: Resp(200, {"jsonrpc": "2.0", "id": rid,
                                     "error": {"code": code, "message": message, "data": data}})


class FakeHttp:
    """Вместо requests.Session. handlers — {метод: функция(params, rid) | значение result}; script — очередь
    ответов для любых методов, кроме getGenesisHash (он отвечает genesis, если не задан в handlers).
    Ответ обработчика: Resp, исключение (бросается), иначе — result."""

    def __init__(self, handlers=None, script=None, genesis=MAINNET_GENESIS):
        self.handlers, self.script, self.genesis = dict(handlers or {}), list(script or []), genesis
        self.calls = []

    def post(self, url, json=None, timeout=None):
        m, p, rid = json["method"], json["params"], json["id"]
        self.calls.append((url, m, p, timeout))
        if m == "getGenesisHash" and m not in self.handlers:
            return Resp(200, {"jsonrpc": "2.0", "id": rid, "result": self.genesis})
        h = self.script.pop(0) if self.script else self.handlers[m]
        out = h(p, rid) if callable(h) else h
        if isinstance(out, BaseException):
            raise out
        if isinstance(out, Resp):
            return out
        return Resp(200, {"jsonrpc": "2.0", "id": rid, "result": out})

    def methods(self):
        return [c[1] for c in self.calls]
