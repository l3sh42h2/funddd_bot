"""Строгий разбор Borsh по Anchor IDL (формат 0.30+: instructions[].discriminator/args, types[] struct|enum).

Только чтение аргументов инструкций до подписи (ТЗ §9.1, SOLANA_ROUTERS §6): декодер ведёт IDL, а не ручные
смещения, поэтому поля ПОСЛЕ маршрута (commission_info, platform_fee_rate, positive_slippage_bps…) читаются так же
надёжно, как префикс. Строгость:
  - данные обязаны кончиться ровно там, где кончились аргументы (хвост — ошибка, а не «лишнее игнорируем»);
  - bool только 0/1, тег Option только 0/1, индекс варианта enum — в границах IDL;
  - длина vec/bytes/string не больше оставшихся байт (нельзя «разогнать» разбор длиной 2^32);
  - неизвестный тип IDL — ошибка разбора, а не пропуск.
IDL пинится sha256 файла (IdlDecoder.sha256): сменился файл — другой декодер, старые ожидания не переносятся.
"""
from __future__ import annotations
import hashlib, json, struct
from pathlib import Path
from typing import Any, Mapping
from .b58 import b58encode

_INTS = {"u8": ("<B", 1), "i8": ("<b", 1), "u16": ("<H", 2), "i16": ("<h", 2), "u32": ("<I", 4), "i32": ("<i", 4),
         "u64": ("<Q", 8), "i64": ("<q", 8)}
MAX_DEPTH = 32


class BorshError(ValueError):
    """Данные не разбираются по IDL. Кандидат непригоден (не «угадываем» раскладку)."""


class _Reader:
    __slots__ = ("b", "off")

    def __init__(self, b: bytes, off: int = 0):
        self.b, self.off = bytes(b), off

    def take(self, n: int) -> bytes:
        if n < 0 or self.off + n > len(self.b):
            raise BorshError(f"обрыв: нужно {n} байт на смещении {self.off}, есть {len(self.b) - self.off}")
        out = self.b[self.off:self.off + n]
        self.off += n
        return out

    def left(self) -> int:
        return len(self.b) - self.off


class IdlDecoder:
    """Декодер инструкций одной программы по её IDL. by_disc: 8 байт дискриминатора → имя инструкции."""

    def __init__(self, idl: Mapping, sha256: str):
        self.idl, self.sha256 = idl, sha256
        self.address = idl.get("address")
        self.types = {t["name"]: t["type"] for t in idl.get("types", [])}
        self.ixs = {ix["name"]: ix for ix in idl.get("instructions", [])}
        self.by_disc: dict[bytes, str] = {}
        for ix in idl.get("instructions", []):
            d = bytes(ix["discriminator"])
            if len(d) != 8 or d in self.by_disc:
                raise BorshError(f"IDL: дискриминатор {ix['name']} не 8 байт или повторяется")
            self.by_disc[d] = ix["name"]

    @classmethod
    def load(cls, path: str | Path, *, expect_sha256: str | None = None) -> "IdlDecoder":
        raw = Path(path).read_bytes()
        h = hashlib.sha256(raw).hexdigest()
        if expect_sha256 is not None and h != expect_sha256:
            raise BorshError(f"IDL {Path(path).name}: sha256 {h[:12]}… ≠ закреплённому {expect_sha256[:12]}…")
        return cls(json.loads(raw), h)

    def name_of(self, data: bytes) -> str | None:
        return self.by_disc.get(bytes(data[:8]))

    def accounts(self, name: str) -> list[Mapping]:
        return list(self.ixs[name]["accounts"])

    def decode_ix(self, data: bytes) -> tuple[str, dict]:
        """(имя, аргументы). Неизвестный дискриминатор, хвост, обрыв — BorshError."""
        name = self.name_of(data)
        if name is None:
            raise BorshError(f"неизвестный дискриминатор {bytes(data[:8]).hex()}")
        r = _Reader(data, 8)
        args = {a["name"]: self._val(a["type"], r, 0) for a in self.ixs[name]["args"]}
        if r.left():
            raise BorshError(f"{name}: лишние {r.left()} байт после аргументов")
        return name, args

    def decode_type(self, type_name: str, data: bytes) -> Any:
        r = _Reader(data)
        v = self._val({"defined": {"name": type_name}}, r, 0)
        if r.left():
            raise BorshError(f"{type_name}: лишние {r.left()} байт")
        return v

    # --- значения ---
    def _val(self, t: Any, r: _Reader, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise BorshError("слишком глубокая вложенность типов")
        if isinstance(t, str):
            if t in _INTS:
                fmt, n = _INTS[t]
                return struct.unpack(fmt, r.take(n))[0]
            if t in ("u128", "i128"):
                return int.from_bytes(r.take(16), "little", signed=t == "i128")
            if t == "bool":
                b = r.take(1)[0]
                if b > 1:
                    raise BorshError(f"bool: байт {b}")
                return b == 1
            if t == "pubkey":
                return b58encode(r.take(32))
            if t in ("bytes", "string"):
                n = struct.unpack("<I", r.take(4))[0]
                if n > r.left():
                    raise BorshError(f"{t}: длина {n} больше остатка {r.left()}")
                raw = r.take(n)
                if t == "string":
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        raise BorshError("string: не UTF-8") from None
                return raw
            raise BorshError(f"тип IDL {t!r} не поддержан")
        if not isinstance(t, dict) or len(t) != 1:
            raise BorshError(f"тип IDL {t!r} не поддержан")
        (k, v), = t.items()
        if k == "vec":
            n = struct.unpack("<I", r.take(4))[0]
            if n > r.left():                         # каждый элемент — хотя бы байт (кроме пустых структур — их нет)
                raise BorshError(f"vec: длина {n} больше остатка {r.left()}")
            return [self._val(v, r, depth + 1) for _ in range(n)]
        if k == "option":
            tag = r.take(1)[0]
            if tag > 1:
                raise BorshError(f"option: тег {tag}")
            return self._val(v, r, depth + 1) if tag else None
        if k == "array":
            et, n = v
            if not isinstance(n, int) or n < 0:
                raise BorshError("array: размер не число")
            return [self._val(et, r, depth + 1) for _ in range(n)]
        if k == "defined":
            name = v["name"] if isinstance(v, dict) else v
            td = self.types.get(name)
            if td is None:
                raise BorshError(f"тип {name!r} не описан в IDL")
            return self._defined(name, td, r, depth + 1)
        raise BorshError(f"тип IDL {k!r} не поддержан")

    def _fields(self, fields: list, r: _Reader, depth: int) -> Any:
        if fields and isinstance(fields[0], dict) and "name" in fields[0]:
            return {f["name"]: self._val(f["type"], r, depth) for f in fields}
        return [self._val(f, r, depth) for f in fields]          # tuple-варианты: список типов

    def _defined(self, name: str, td: Mapping, r: _Reader, depth: int) -> Any:
        kind = td.get("kind")
        if kind == "struct":
            return self._fields(td.get("fields", []), r, depth)
        if kind == "enum":
            variants = td["variants"]
            i = r.take(1)[0]
            if i >= len(variants):
                raise BorshError(f"{name}: вариант {i} вне {len(variants)}")
            var = variants[i]
            fields = var.get("fields")
            return {var["name"]: self._fields(fields, r, depth) if fields else None}
        raise BorshError(f"{name}: вид {kind!r} не поддержан")


# --- кодирование (только для тестов и своих инструкций: синтетические сообщения S10–S12) ---------------------------
def encode_args(dec: IdlDecoder, name: str, args: Mapping) -> bytes:
    ix = dec.ixs[name]
    return bytes(ix["discriminator"]) + b"".join(_enc(dec, a["type"], args[a["name"]]) for a in ix["args"])


def _enc(dec: IdlDecoder, t: Any, v: Any) -> bytes:
    from .b58 import pubkey_bytes
    if isinstance(t, str):
        if t in _INTS:
            return struct.pack(_INTS[t][0], v)
        if t in ("u128", "i128"):
            return int(v).to_bytes(16, "little", signed=t == "i128")
        if t == "bool":
            return b"\1" if v else b"\0"
        if t == "pubkey":
            return pubkey_bytes(v)
        if t == "bytes":
            return struct.pack("<I", len(v)) + bytes(v)
        if t == "string":
            b = v.encode()
            return struct.pack("<I", len(b)) + b
        raise BorshError(f"тип {t!r}")
    (k, x), = t.items()
    if k == "vec":
        return struct.pack("<I", len(v)) + b"".join(_enc(dec, x, e) for e in v)
    if k == "option":
        return b"\0" if v is None else b"\1" + _enc(dec, x, v)
    if k == "array":
        return b"".join(_enc(dec, x[0], e) for e in v)
    if k == "defined":
        name = x["name"] if isinstance(x, dict) else x
        td = dec.types[name]
        if td["kind"] == "struct":
            return _enc_fields(dec, td.get("fields", []), v)
        (vn, fv), = v.items()
        idx = [var["name"] for var in td["variants"]].index(vn)
        var = td["variants"][idx]
        return bytes([idx]) + (_enc_fields(dec, var["fields"], fv) if var.get("fields") else b"")
    raise BorshError(f"тип {k!r}")


def _enc_fields(dec: IdlDecoder, fields: list, v: Any) -> bytes:
    if fields and isinstance(fields[0], dict) and "name" in fields[0]:
        return b"".join(_enc(dec, f["type"], v[f["name"]]) for f in fields)
    return b"".join(_enc(dec, f, e) for f, e in zip(fields, v))
