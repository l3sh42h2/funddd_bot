"""JSON-RPC Solana поверх requests (ТЗ CONNECTION §4.1, SOLANA_ROUTERS §7).

SolanaRpc — один узел:
  - свой счётчик id: ответ с чужим id — мусор, а не ответ на наш запрос;
  - общий дедлайн запроса на все повторы; повтор ТОЛЬКО для методов чтения (обрыв, 429, 5xx, «узел отстаёт»);
  - sendTransaction — ровно одна попытка, без повтора и без переключения узла: обрыв после отправки —
    SendUnknown (байты, возможно, уже в сети; исход решает резолвер по подписи, а не повтор «на всякий случай»);
  - до первого запроса — getGenesisHash против ожидаемой сети (mainnet-beta 5eykt4…): чужая сеть (devnet,
    testnet, форк) — GenesisMismatch навсегда для этого узла; chainIndex 501 у OKX сетью не считается;
  - URL провайдера часто несёт ключ (Helius ?api-key=…, QuickNode /<token>/): наружу (ошибки, repr, метрики)
    идёт только label = имя@хост, текст исключений requests не пробрасывается (в нём полный URL).

RpcPool — независимые узлы (разные хосты): read() — первый ответивший (ответ-ошибка узла по существу у соседа
будет тем же и не повторяется), each() — ответ каждого отдельно: резолверу нужна согласованная история двух
RPC, а не «кто первый ответил».
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence, TypeVar
from urllib.parse import urlsplit
import requests
from . import MAINNET_GENESIS
from .b58 import check_pubkey, signature_bytes

RPC_TIMEOUT_S = 10.0            # на один HTTP-запрос
READ_BUDGET_S = 20.0            # на чтение со всеми повторами
READ_RETRIES = 2
BACKOFF_S = (0.3, 1.0)
COMMITMENTS = ("processed", "confirmed", "finalized")

READ_METHODS = frozenset({
    "getGenesisHash", "getHealth", "getAccountInfo", "getMultipleAccounts", "getBalance", "getLatestBlockhash",
    "getBlockHeight", "getSlot", "isBlockhashValid", "getMinimumBalanceForRentExemption", "getSignatureStatuses",
    "getTransaction", "getSignaturesForAddress", "getTokenAccountsByOwner", "minimumLedgerSlot",
    "simulateTransaction", "getEpochInfo"})
SEND_METHOD = "sendTransaction"
# коды JSON-RPC «узел сейчас не может» — повтор/другой узел; прочие ошибки — ответ по существу
TRANSIENT_CODES = frozenset({-32005, -32004, -32014, -32016, -32603, 429, -32429})
T = TypeVar("T")


def redact_url(url: str) -> str:
    """URL → схема://хост[:порт]: путь и query (там ключ провайдера) отбрасываются."""
    try:
        u = urlsplit(str(url))
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return "?"
    return f"{u.scheme or '?'}://{u.hostname or '?'}{port}"


# --- ошибки ------------------------------------------------------------------------------------------
class RpcError(RuntimeError):
    """Узел ОТВЕТИЛ ошибкой JSON-RPC по существу (неверные параметры, провал preflight и т. п.)."""

    def __init__(self, method: str, code: Any, message: Any, data: Any = None, label: str = ""):
        self.method, self.code, self.data, self.label = method, code, data, label
        self.message = str(message or "")[:300]
        super().__init__(f"{method} @ {label}: code {code}: {self.message[:200]}")


class RpcUnavailable(RuntimeError):
    """Ответа нет (обрыв, 429/5xx, мусор, дедлайн, узел отстаёт). НЕИЗВЕСТНО — никогда не «пусто» и не «ноль»."""


class GenesisMismatch(RuntimeError):
    """Узел из другой сети. Не используется вовсе — ошибка настройки."""


class SendUnknown(RuntimeError):
    """sendTransaction: ответа нет или он странный — байты, возможно, ушли. Исход — только через резолвер."""

    def __init__(self, signature: str, label: str, reason: str):
        self.signature, self.label, self.reason = signature, label, reason
        super().__init__(f"отправка {signature} @ {label}: исход неизвестен ({reason})")


class _Transient(Exception):
    def __init__(self, text: str, code: Any = None, data: Any = None):
        self.code, self.data = code, data
        super().__init__(text)


@dataclass
class RpcStats:
    n_ok: int = 0
    n_fail: int = 0
    n_retry: int = 0
    last_ms: float | None = None
    last_error: str = ""


@dataclass(frozen=True)
class BlockhashInfo:
    """blockhash и его lastValidBlockHeight из ОДНОГО ответа getLatestBlockhash — точная пара (G08)."""
    blockhash: str
    last_valid_block_height: int
    context_slot: int
    commitment: str
    source: str


def _commit(c: str) -> str:
    if c not in COMMITMENTS:
        raise ValueError(f"commitment {c!r} не из {COMMITMENTS}")
    return c


class SolanaRpc:
    def __init__(self, url: Any, *, name: str = "primary", expected_genesis: str = MAINNET_GENESIS,
                 session: requests.Session | None = None, timeout_s: float = RPC_TIMEOUT_S,
                 budget_s: float = READ_BUDGET_S, retries: int = READ_RETRIES,
                 backoff_s: Sequence[float] = BACKOFF_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        raw = url.reveal() if hasattr(url, "reveal") else url     # keys.SecretUrl: ключ провайдера в URL
        try:
            u = urlsplit(str(raw or ""))
            host = u.hostname
        except ValueError:
            host = None
            u = None
        if u is None or u.scheme not in ("https", "http") or not host:
            raise ValueError(f"{name}: URL RPC не http(s)")
        self._url = str(raw)
        self.name, self.host = name, host
        self.label = f"{name}@{host}"
        self.expected_genesis = expected_genesis
        self._s = session or requests.Session()
        self.timeout_s, self.budget_s, self.retries = float(timeout_s), float(budget_s), int(retries)
        self.backoff_s = tuple(backoff_s)
        self._clock, self._sleep = clock, sleep
        self._id = 0
        self._genesis_ok = False
        self._genesis_bad: str | None = None
        self._rent: dict[int, int] = {}
        self.stats = RpcStats()

    def __repr__(self) -> str:
        return f"SolanaRpc({self.label})"

    # --- транспорт ---
    def _post(self, method: str, params: list, timeout: float) -> Any:
        self._id += 1
        rid = self._id
        t0 = self._clock()
        try:
            r = self._s.post(self._url, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
                             timeout=timeout)
        except requests.RequestException as e:
            raise _Transient(type(e).__name__) from None      # str(e) несёт полный URL — не берём
        finally:
            self.stats.last_ms = round((self._clock() - t0) * 1000, 1)
        if r.status_code == 429 or r.status_code >= 500:
            raise _Transient(f"HTTP {r.status_code}")
        if r.status_code != 200:
            raise RpcUnavailable(f"{method} @ {self.label}: HTTP {r.status_code} (ключ или адрес RPC?)")
        try:
            body = r.json()
        except ValueError:
            raise _Transient("ответ не JSON") from None
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            raise _Transient("не JSON-RPC 2.0")
        rid_back = body.get("id")
        if isinstance(rid_back, bool) or rid_back != rid:
            raise _Transient("чужой id ответа")
        err = body.get("error")
        if err is not None:
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message") if isinstance(err, dict) else err
            data = err.get("data") if isinstance(err, dict) else None
            if code in TRANSIENT_CODES:
                raise _Transient(f"code {code}", code, data)
            raise RpcError(method, code, msg, data, self.label)
        if "result" not in body:
            raise _Transient("нет result")
        self.stats.n_ok += 1
        return body["result"]

    def _read(self, method: str, params: list, budget_s: float | None) -> Any:
        deadline = self._clock() + (self.budget_s if budget_s is None else float(budget_s))
        last = "дедлайн"
        for attempt in range(self.retries + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                return self._post(method, params, min(self.timeout_s, remaining))
            except _Transient as e:
                last = str(e)
                self.stats.n_fail += 1
                self.stats.last_error = f"{method}: {last}"
            if attempt == self.retries:
                break
            pause = self.backoff_s[min(attempt, len(self.backoff_s) - 1)] if self.backoff_s else 0.0
            if self._clock() + pause >= deadline:
                break
            self._sleep(pause)
            self.stats.n_retry += 1
        raise RpcUnavailable(f"{method} @ {self.label}: нет ответа ({last})")

    def check_genesis(self) -> str:
        """Сеть узла = ожидаемая? Проверяется один раз; чужая сеть — отказ навсегда."""
        if self._genesis_bad is not None:
            raise GenesisMismatch(f"{self.label}: genesis {self._genesis_bad} ≠ {self.expected_genesis}")
        if self._genesis_ok:
            return self.expected_genesis
        g = self._read("getGenesisHash", [], None)
        if g != self.expected_genesis:
            self._genesis_bad = str(g)[:64]
            raise GenesisMismatch(f"{self.label}: genesis {self._genesis_bad} ≠ {self.expected_genesis} — чужая сеть, "
                                  "узел не используется")
        self._genesis_ok = True
        return g

    def call(self, method: str, params: list | None = None, *, budget_s: float | None = None) -> Any:
        """Чтение: genesis уже сверен, повтор в пределах дедлайна. Отправка сюда не пускается."""
        if method == SEND_METHOD:
            raise ValueError("sendTransaction — только send_transaction(): без повтора и после журнала")
        if method not in READ_METHODS:
            raise ValueError(f"метод {method} не в списке чтений")
        self.check_genesis()
        return self._read(method, list(params or []), budget_s)

    # --- разбор ответов ---
    def _uint(self, x: Any, what: str) -> int:
        if isinstance(x, bool) or not isinstance(x, int) or x < 0:
            raise RpcUnavailable(f"{self.label}: {what} — не целое ≥ 0 ({type(x).__name__})")
        return x

    def _ctx(self, res: Any, method: str) -> tuple[Any, int]:
        if not isinstance(res, dict) or "value" not in res or not isinstance(res.get("context"), dict):
            raise RpcUnavailable(f"{method} @ {self.label}: ответ без context/value")
        return res["value"], self._uint(res["context"].get("slot"), f"{method}.context.slot")

    # --- чтения ---
    def genesis_hash(self) -> str:
        return self._read("getGenesisHash", [], None)

    def health(self) -> tuple[bool, int | None]:
        """(здоров, отставание в слотах или None). Одна попытка: это замер, а не чтение данных."""
        self.check_genesis()
        try:
            return self._post("getHealth", [], self.timeout_s) == "ok", None
        except _Transient as e:
            behind = e.data.get("numSlotsBehind") if isinstance(e.data, dict) else None
            return False, behind if isinstance(behind, int) and not isinstance(behind, bool) else None

    def account_info(self, pubkey: str, *, encoding: str = "jsonParsed",
                     commitment: str = "confirmed") -> tuple[dict | None, int]:
        check_pubkey(pubkey, "счёт")
        res = self.call("getAccountInfo", [pubkey, {"encoding": encoding, "commitment": _commit(commitment)}])
        value, slot = self._ctx(res, "getAccountInfo")
        if value is not None and not isinstance(value, dict):
            raise RpcUnavailable(f"getAccountInfo @ {self.label}: value не объект")
        return value, slot

    def multiple_accounts(self, pubkeys: Sequence[str], *, encoding: str = "jsonParsed",
                          commitment: str = "confirmed") -> tuple[list[dict | None], int]:
        keys = list(pubkeys)
        if not 0 < len(keys) <= 100:
            raise ValueError("getMultipleAccounts: от 1 до 100 счетов")
        for k in keys:
            check_pubkey(k, "счёт")
        res = self.call("getMultipleAccounts", [keys, {"encoding": encoding, "commitment": _commit(commitment)}])
        value, slot = self._ctx(res, "getMultipleAccounts")
        if not isinstance(value, list) or len(value) != len(keys):
            raise RpcUnavailable(f"getMultipleAccounts @ {self.label}: не тот размер ответа")
        return value, slot

    def balance(self, pubkey: str, *, commitment: str = "confirmed") -> tuple[int, int]:
        check_pubkey(pubkey, "счёт")
        value, slot = self._ctx(self.call("getBalance", [pubkey, {"commitment": _commit(commitment)}]), "getBalance")
        return self._uint(value, "getBalance"), slot

    def latest_blockhash(self, commitment: str = "confirmed") -> BlockhashInfo:
        value, slot = self._ctx(self.call("getLatestBlockhash", [{"commitment": _commit(commitment)}]),
                                "getLatestBlockhash")
        if not isinstance(value, dict):
            raise RpcUnavailable(f"getLatestBlockhash @ {self.label}: value не объект")
        bh = value.get("blockhash")
        check_pubkey(bh, "blockhash")
        return BlockhashInfo(bh, self._uint(value.get("lastValidBlockHeight"), "lastValidBlockHeight"), slot,
                             commitment, self.label)

    def block_height(self, commitment: str = "finalized") -> int:
        return self._uint(self.call("getBlockHeight", [{"commitment": _commit(commitment)}]), "getBlockHeight")

    def slot(self, commitment: str = "confirmed") -> int:
        return self._uint(self.call("getSlot", [{"commitment": _commit(commitment)}]), "getSlot")

    def is_blockhash_valid(self, blockhash: str, *, commitment: str = "processed") -> tuple[bool, int]:
        """Справка: False не доказывает, что транзакция с этим hash не исполнилась (§9.3)."""
        check_pubkey(blockhash, "blockhash")
        value, slot = self._ctx(self.call("isBlockhashValid", [blockhash, {"commitment": _commit(commitment)}]),
                                "isBlockhashValid")
        if not isinstance(value, bool):
            raise RpcUnavailable(f"isBlockhashValid @ {self.label}: value не bool")
        return value, slot

    def minimum_ledger_slot(self) -> int:
        return self._uint(self.call("minimumLedgerSlot", []), "minimumLedgerSlot")

    def rent_exempt_lamports(self, size: int) -> int:
        """Rent-exempt минимум для счёта размера size — у узла, не константой (ставка rent меняется)."""
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"размер счёта: {size!r}")
        if size not in self._rent:
            self._rent[size] = self._uint(self.call("getMinimumBalanceForRentExemption", [size]), "rent")
        return self._rent[size]

    def signature_statuses(self, signatures: Sequence[str], *,
                           search_history: bool = True) -> tuple[list[dict | None], int]:
        sigs = list(signatures)
        if not 0 < len(sigs) <= 256:
            raise ValueError("getSignatureStatuses: от 1 до 256 подписей")
        for s in sigs:
            signature_bytes(s)
        value, slot = self._ctx(self.call("getSignatureStatuses", [sigs, {"searchTransactionHistory":
                                                                           bool(search_history)}]),
                                "getSignatureStatuses")
        if not isinstance(value, list) or len(value) != len(sigs):
            raise RpcUnavailable(f"getSignatureStatuses @ {self.label}: не тот размер ответа")
        for st in value:
            if st is None:
                continue
            if not isinstance(st, dict) or st.get("confirmationStatus") not in (*COMMITMENTS, None):
                raise RpcUnavailable(f"getSignatureStatuses @ {self.label}: статус не разобран")
            self._uint(st.get("slot"), "status.slot")
        return value, slot

    def transaction(self, signature: str, *, commitment: str = "confirmed", encoding: str = "json") -> dict | None:
        """Чек или None. None одного узла не доказывает неисполнения (§9.3)."""
        signature_bytes(signature)
        if commitment not in ("confirmed", "finalized"):
            raise ValueError("getTransaction: commitment confirmed или finalized")
        if encoding not in ("json", "base64", "jsonParsed"):
            raise ValueError(f"getTransaction: encoding {encoding!r}")
        res = self.call("getTransaction", [signature, {"encoding": encoding, "commitment": commitment,
                                                       "maxSupportedTransactionVersion": 0}])
        if res is not None and not isinstance(res, dict):
            raise RpcUnavailable(f"getTransaction @ {self.label}: ответ не объект")
        return res

    def signatures_for_address(self, address: str, *, limit: int = 100, before: str | None = None,
                               commitment: str = "confirmed") -> list[dict]:
        check_pubkey(address, "адрес")
        cfg: dict[str, Any] = {"limit": int(limit), "commitment": _commit(commitment)}
        if before is not None:
            signature_bytes(before)
            cfg["before"] = before
        res = self.call("getSignaturesForAddress", [address, cfg])
        if not isinstance(res, list):
            raise RpcUnavailable(f"getSignaturesForAddress @ {self.label}: не список")
        return res

    def simulate(self, tx_b64: str, *, sig_verify: bool = False, replace_recent_blockhash: bool = False,
                 accounts: Sequence[str] = (), inner: bool = True, commitment: str = "confirmed") -> dict:
        """Симуляция ТОЧНЫХ байтов. replaceRecentBlockhash — только для диагностики CU: исполнимость исходного
        сообщения она не доказывает (SOLANA_ROUTERS §6)."""
        if sig_verify and replace_recent_blockhash:
            raise ValueError("sigVerify и replaceRecentBlockhash вместе RPC не принимает")
        cfg: dict[str, Any] = {"encoding": "base64", "sigVerify": bool(sig_verify),
                               "replaceRecentBlockhash": bool(replace_recent_blockhash),
                               "commitment": _commit(commitment), "innerInstructions": bool(inner)}
        if accounts:
            for a in accounts:
                check_pubkey(a, "счёт симуляции")
            cfg["accounts"] = {"encoding": "base64", "addresses": list(accounts)}
        value, slot = self._ctx(self.call("simulateTransaction", [tx_b64, cfg]), "simulateTransaction")
        if not isinstance(value, dict):
            raise RpcUnavailable(f"simulateTransaction @ {self.label}: value не объект")
        return {"context_slot": slot, **value}

    # --- отправка ---
    def send_transaction(self, tx_b64: str, *, expected_signature: str, mode: str | None,
                         skip_preflight: bool = False, preflight_commitment: str = "confirmed",
                         max_retries: int | None = None) -> str:
        """Ровно одна попытка на этот узел. Вызывать только исполнителю ПОСЛЕ journal.mark_broadcast().
        «Принято» — не исполнение; ответ-ошибка узла тоже не доказывает, что байтов нет в сети."""
        from ..keys import gate                  # ворота режима общие с EVM/Aster: send — только live
        gate(mode, "send")
        signature_bytes(expected_signature)
        self.check_genesis()
        cfg: dict[str, Any] = {"encoding": "base64", "skipPreflight": bool(skip_preflight),
                               "preflightCommitment": _commit(preflight_commitment)}
        if max_retries is not None:
            cfg["maxRetries"] = int(max_retries)
        try:
            res = self._post(SEND_METHOD, [tx_b64, cfg], self.timeout_s)
        except (_Transient, RpcUnavailable) as e:
            raise SendUnknown(expected_signature, self.label, str(e)) from None
        if res != expected_signature:
            raise SendUnknown(expected_signature, self.label, "узел вернул другую подпись")
        return res


class RpcPool:
    """Несколько независимых узлов. Одинаковый хост дважды — не независимые источники: отказ."""

    def __init__(self, endpoints: Sequence[SolanaRpc]):
        eps = tuple(endpoints)
        if not eps:
            raise ValueError("нет ни одного RPC")
        hosts = [e.host for e in eps]
        if len(set(hosts)) != len(hosts):
            raise ValueError("два RPC на одном хосте — это не независимые источники")
        self.endpoints = eps

    @classmethod
    def from_urls(cls, urls: Sequence[str], **kw) -> "RpcPool":
        names = ("primary", "secondary")
        return cls([SolanaRpc(u, name=names[i] if i < 2 else f"rpc{i}", **kw)
                    for i, u in enumerate(u for u in urls if u)])

    @property
    def primary(self) -> SolanaRpc:
        return self.endpoints[0]

    def read(self, fn: Callable[[SolanaRpc], T]) -> T:
        """Первый ответивший. RpcError (ответ по существу) и GenesisMismatch — сразу наверх."""
        last: Exception | None = None
        for ep in self.endpoints:
            try:
                return fn(ep)
            except RpcUnavailable as e:
                last = e
        raise RpcUnavailable(f"ни один RPC не ответил: {last}")

    def each(self, fn: Callable[[SolanaRpc], T]) -> list[tuple[str, T | Exception]]:
        """Ответ каждого узла отдельно; сбой узла — объект исключения на его месте, не пропуск."""
        out: list[tuple[str, Any]] = []
        for ep in self.endpoints:
            try:
                out.append((ep.label, fn(ep)))
            except (RpcUnavailable, RpcError, GenesisMismatch) as e:
                out.append((ep.label, e))
        return out
