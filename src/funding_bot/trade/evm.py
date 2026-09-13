"""EVM-нога фазы 2 (trade_spec §4, §1): чтение цепи с переключением узлов и ЕДИНСТВЕННЫЙ писатель кошелька.

EvmRpc — JSON-RPC поверх requests. Чтение переключается по списку BSC_RPC_URLS: публичные узлы режут частоту и
падают, а «ни один узел не ответил» — это НЕИЗВЕСТНО (RpcUnavailable), а не пустой баланс. Ответ узла с ошибкой
(откат симуляции) — RpcError: это ответ по существу, у другого узла он будет тем же.

EvmWallet.send_and_wait — одна транзакция от подписи до чека:
  - flock runtime/evm_<addr>.lock: один писатель на кошелёк, даже если по ошибке запущены два процесса;
  - ворота режима (gate) до подписи: новая отправка — только live и не пауза; замена уже отправленной (bump/cancel)
    разрешена и на паузе — она лишь доводит до конца то, что уже в сети («стоп», Q7);
  - nonce = eth_getTransactionCount(addr, "pending"); pending > latest — в сети висит наша прежняя транзакция, и новую
    на новом nonce НЕ подписываю (lphedge брал nonce по latest — это ломалось на зависших);
  - legacy EIP-155 {nonce, gasPrice, gas, to, value, data, chainId}: base fee на BSC = 0, тип 0 принимается (12.09);
  - сырые байты и хэш уходят в БД через on_signed ДО отправки (запись-до): упавший процесс найдёт хэш и nonce и
    спросит сеть, а не подпишет новую;
  - график зависшей (§1): 15 с без чека — ищем по хэшу; узел её не знает и nonce свободен — те же сырые байты ещё
    раз; 30 с — тот же nonce и calldata по цене ×1.2; 90 с — 0 BNB самому себе на том же nonce и SentUnknown
    (сделка на паузу, решает владелец). Новый своп на новом nonce, пока старый висит, — никогда.
Любое исключение после первой отправки становится SentUnknown: деньги, возможно, уже потрачены, и «повторить другим
путём» нельзя (lphedge: запасной маршрут свопа потратил вход второй раз).
"""
from __future__ import annotations
import fcntl, logging, os, time
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
import requests
from . import tconfig
from .keys import ModeForbidden, mark_public, redact
from .store import DexTxKind, DexTxState

log = logging.getLogger(__name__)

MAX_UINT256 = 2 ** 256 - 1
SEL_DECIMALS = "0x313ce567"
RPC_TIMEOUT_S = 10.0
LOCK_WAIT_S = 5.0            # чужой держатель flock дольше — отказ (второй процесс-писатель — это ошибка, а не очередь)


# --- ошибки ------------------------------------------------------------------------------------------
class RpcError(RuntimeError):
    """Узел ОТВЕТИЛ ошибкой JSON-RPC. reverted — откат симуляции (eth_call / eth_estimateGas): повторять незачем."""

    def __init__(self, method: str, code: Any, message: str, data: Any = None, host: str = ""):
        self.method, self.code, self.message, self.data, self.host = method, code, str(message or ""), data, host
        super().__init__(f"{method}: code {code}: {self.message[:200]}" + (f" ({host})" if host else ""))

    @property
    def reverted(self) -> bool:
        m = self.message.lower()
        return self.code == 3 or "revert" in m or "gas required exceeds" in m


class RpcUnavailable(RuntimeError):
    """Ни один узел не ответил (обрыв, 429, 5xx, мусор вместо JSON). НЕИЗВЕСТНО — никогда не «ноль»."""


class SentUnknown(RuntimeError):
    """Транзакция УШЛА в сеть, а исход неизвестен (нет чека к 90 с, nonce занят без нашего чека, сбой после
    отправки). Повторять другим путём нельзя: сделка на паузу, сверка по хэшам (hashes) на этом nonce."""

    def __init__(self, nonce: int, hashes: list[str], reason: str):
        self.nonce, self.hashes, self.reason = nonce, list(hashes), reason
        super().__init__(f"nonce {nonce}: {reason}; хэши: {', '.join(self.hashes) or '—'}")


class TxRejected(RuntimeError):
    """Узел ОТВЕРГ первую отправку (нет средств на газ, nonce уже занят, цена ниже минимума узла): в сеть она не
    ушла, деньги не двигались. Движок решает сам — автоматического повтора нет."""

    def __init__(self, nonce: int, tx_hash: str, reason: str):
        self.nonce, self.tx_hash, self.reason = nonce, tx_hash, reason
        super().__init__(f"nonce {nonce}: узел отверг {tx_hash}: {reason[:200]}")


class NoncePending(RuntimeError):
    """pending > latest: на кошельке висит неподтверждённая транзакция (наша прежняя или чужая). Новую не подписываю,
    пока та не решится — иначе два свопа на двух nonce вместо одного."""

    def __init__(self, latest: int, pending: int):
        self.latest, self.pending = latest, pending
        super().__init__(f"на кошельке висит неподтверждённая транзакция (latest {latest}, pending {pending}) — "
                         "новую не подписываю, сначала сверка")


class WalletBusy(RuntimeError):
    """flock кошелька держит другой процесс: второго писателя быть не должно — отказ, ничего не подписано."""


# --- числа и адреса ----------------------------------------------------------------------------------
def _int(x: Any) -> int:
    """Целое из JSON-RPC ("0x…") или десятичной строки. bool, float и пустой "0x" — ошибка: сырые единицы только
    int, а пустой ответ eth_call (адрес без кода) нельзя принять за ноль."""
    if isinstance(x, bool) or x is None:
        raise ValueError(f"не целое: {x!r}")
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s[:2].lower() == "0x" and len(s) > 2:
            return int(s, 16)
        if s.isdigit():
            return int(s)
    raise ValueError(f"не целое: {x!r}")


def _addr(a: Any) -> str:
    """Адрес EVM в нижнем регистре; не 0x+40 hex — ValueError (гард не должен сравнивать мусор)."""
    s = str(a or "").strip().lower()
    if len(s) != 42 or not s.startswith("0x") or any(c not in "0123456789abcdef" for c in s[2:]):
        raise ValueError(f"не адрес EVM: {a!r}")
    return s


def _word(a: str) -> str:
    """Адрес → 32-байтное слово ABI (без 0x)."""
    return "0" * 24 + _addr(a)[2:]


def _topic_addr(t: Any) -> str | None:
    s = str(t or "").lower()
    return "0x" + s[-40:] if len(s) == 66 and s.startswith("0x") else None


def _hexbytes(data: Any) -> bytes:
    s = str(data or "0x").strip()
    if not s.startswith("0x") or len(s) % 2:
        raise ValueError(f"calldata не hex: {s[:20]!r}")
    return bytes.fromhex(s[2:])


def _checksum(a: str) -> str:
    from eth_utils import to_checksum_address      # лениво: сухой режим и коллектор без eth-account живут
    return to_checksum_address(_addr(a))


def _host(url: str) -> str:
    """В сообщения — только хост: в URL своего RPC бывает токен провайдера."""
    return urlsplit(url).hostname or "?"


def bump_price(price: int) -> int:
    """Цена замены: ×TX_BUMP с округлением вверх и минимум +1 wei (geth-узлам нужна надбавка ≥10 %)."""
    return max(int((Decimal(int(price)) * tconfig.TX_BUMP).to_integral_value(ROUND_CEILING)), int(price) + 1)


# --- RPC ---------------------------------------------------------------------------------------------
_SEND_KNOWN = ("already known", "known transaction", "already exists", "alreadyknown", "already imported")
_SEND_NONCE = ("nonce too low", "nonce is too low")
_SEND_UNDER = ("underpriced",)
_SEND_REJECT = ("insufficient funds", "intrinsic gas", "exceeds block gas limit", "invalid sender", "gas too low",
                "oversized data", "invalid chain", "replay-protected", "gas price too low", "exceeds the configured cap")


def classify_send_error(message: str) -> str:
    """Ответ узла на eth_sendRawTransaction → known | nonce_used | underpriced | rejected | unknown."""
    m = str(message or "").lower()
    for words, out in ((_SEND_KNOWN, "known"), (_SEND_NONCE, "nonce_used"), (_SEND_UNDER, "underpriced"),
                       (_SEND_REJECT, "rejected")):
        if any(w in m for w in words):
            return out
    return "unknown"


class EvmRpc:
    """JSON-RPC узлов сети. Чтение — с переключением по списку (липко: последний ответивший идёт первым)."""

    def __init__(self, urls: tuple[str, ...] | list[str] | None = None, session: requests.Session | None = None,
                 timeout: float = RPC_TIMEOUT_S):
        self.urls = tuple(urls) if urls else tconfig.bsc_rpc_urls()
        if not self.urls:
            raise ValueError("нет ни одного RPC")
        self._s = session or requests.Session()
        self.timeout = timeout
        self._i = 0
        self._id = 0
        self.n_failover = 0

    # --- транспорт ---
    def _post(self, url: str, method: str, params: list) -> Any:
        self._id += 1
        r = self._s.post(url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                         timeout=self.timeout)
        if r.status_code != 200:
            raise ConnectionError(f"HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise ConnectionError("ответ не JSON") from None
        if not isinstance(body, dict):
            raise ConnectionError("ответ не объект JSON-RPC")
        err = body.get("error")
        if err:
            if isinstance(err, dict):
                raise RpcError(method, err.get("code"), err.get("message"), err.get("data"), _host(url))
            raise RpcError(method, None, str(err), None, _host(url))
        if "result" not in body:
            raise ConnectionError("в ответе нет result")
        return body["result"]

    def _order(self) -> list[int]:
        n = len(self.urls)
        return [(self._i + k) % n for k in range(n)]

    def call(self, method: str, params: list) -> Any:
        """Чтение с переключением. Откат симуляции — сразу RpcError (у соседа он тот же). Прочие ошибки узла и обрывы —
        следующий узел; не ответил никто — первая ошибка-ответ, если была, иначе RpcUnavailable."""
        answer: RpcError | None = None
        last = ""
        for i in self._order():
            url = self.urls[i]
            try:
                res = self._post(url, method, params)
            except RpcError as e:
                if e.reverted:
                    raise
                answer = answer or e
                continue
            except Exception as e:       # noqa — сеть/HTTP/мусор: следующий узел
                last = f"{type(e).__name__} @ {_host(url)}"
                continue
            if i != self._i:
                self._i = i
                self.n_failover += 1
            return res
        if answer is not None:
            raise answer
        raise RpcUnavailable(f"{method}: ни один узел не ответил ({last})")

    # --- отправка ---
    def _send_one(self, url: str, raw: str) -> tuple[str, str]:
        try:
            res = self._post(url, "eth_sendRawTransaction", [raw])
        except RpcError as e:
            return classify_send_error(e.message), e.message[:200]
        except Exception as e:           # noqa — обрыв: ушло или нет — неизвестно
            return "unknown", f"{type(e).__name__} @ {_host(url)}"
        return "ok", str(res)

    def send_raw(self, raw: str) -> tuple[str, str]:
        """Отправка подписанных байтов: текущему узлу; обрыв или непонятный ответ — ТЕ ЖЕ байты остальным узлам.
        Одни и те же байты = один хэш, исполниться может только одна — повтор безопасен (а «другим путём» — никогда).
        Узел ответил по существу (принял или отверг) — дальше не шлём. Итог: ok | known | nonce_used | underpriced |
        rejected | unknown."""
        best = ("unknown", "нет узлов")
        for i in self._order():
            out, text = self._send_one(self.urls[i], raw)
            if out != "unknown":
                return out, text
            best = (out, text)
        return best

    def rebroadcast(self, raw: str) -> tuple[str, str]:
        """Те же байты — ВСЕМ узлам (транзакция пропала из пула узла). Итог — лучший из ответов."""
        res = [self._send_one(u, raw) for u in self.urls]
        for pref in ("ok", "known", "nonce_used", "underpriced", "rejected"):
            for out, text in res:
                if out == pref:
                    return out, text
        return res[-1]

    # --- чтение ---
    @staticmethod
    def _tag(block: int | str) -> str:
        return hex(block) if isinstance(block, int) and not isinstance(block, bool) else str(block)

    def chain_id(self) -> int:
        return _int(self.call("eth_chainId", []))

    def block_number(self) -> int:
        return _int(self.call("eth_blockNumber", []))

    def nonce(self, addr: str, tag: str = "pending") -> int:
        return _int(self.call("eth_getTransactionCount", [_addr(addr), tag]))

    def gas_price(self) -> int:
        return _int(self.call("eth_gasPrice", []))

    def native_balance(self, addr: str, block: int | str = "latest") -> int:
        return _int(self.call("eth_getBalance", [_addr(addr), self._tag(block)]))

    def eth_call(self, to: str, data: str, block: int | str = "latest") -> str:
        return self.call("eth_call", [{"to": _addr(to), "data": data}, self._tag(block)])

    def u256_call(self, to: str, data: str, block: int | str = "latest") -> int:
        """Первое слово ответа eth_call. Пустой ответ ("0x" — по адресу нет кода) — ошибка, не ноль."""
        res = self.eth_call(to, data, block)
        if not isinstance(res, str) or not res.startswith("0x") or len(res) < 66:
            raise RpcError("eth_call", None, f"пустой или короткий ответ {str(res)[:20]!r} — по адресу нет контракта?")
        return int(res[2:66], 16)

    def erc20_balance(self, token: str, who: str, block: int | str = "latest") -> int:
        return self.u256_call(token, tconfig.SEL_BALANCE_OF + _word(who), block)

    def allowance(self, token: str, owner: str, spender: str) -> int:
        return self.u256_call(token, tconfig.SEL_ALLOWANCE + _word(owner) + _word(spender))

    def decimals(self, token: str) -> int:
        return self.u256_call(token, SEL_DECIMALS)

    def estimate_gas(self, tx: dict) -> int:
        return _int(self.call("eth_estimateGas", [tx]))

    def receipt(self, tx_hash: str, everywhere: bool = False) -> dict | None:
        """Чек или None. everywhere — спросить каждый узел (nonce занят, а липкий узел чека не видит: отстаёт)."""
        if not everywhere:
            return self.call("eth_getTransactionReceipt", [tx_hash])
        for url in self.urls:
            try:
                rc = self._post(url, "eth_getTransactionReceipt", [tx_hash])
            except Exception:            # noqa — этот узел не ответил, спросим следующий
                continue
            if rc:
                return rc
        return None

    def tx_by_hash(self, tx_hash: str) -> dict | None:
        return self.call("eth_getTransactionByHash", [tx_hash])


# --- кошелёк -----------------------------------------------------------------------------------------
@dataclass
class SendInfo:
    """Всё, что ушло в сеть на одном nonce: хэши по порядку (исходная, bump, cancel), их вид, байты и цена."""
    nonce: int
    hashes: list[str] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)
    raws: dict[str, str] = field(default_factory=dict)
    prices: dict[str, int] = field(default_factory=dict)
    sent_at: dict[str, float] = field(default_factory=dict)
    looked: set[str] = field(default_factory=set)
    dead: set[str] = field(default_factory=set)     # узел отверг окончательно: не майнится, не ждём
    winner: str | None = None


class EvmWallet:
    """Единственный отправитель транзакций кошелька. acct — keys.SignerKey (или LocalAccount в тестах): address +
    sign_transaction. gate(in_flight) — ворота режима, бросает ModeForbidden: in_flight=False — новая транзакция,
    True — замена уже отправленной (bump/cancel). Обработчики журнала:
      on_signed(row)             — строка для store.dex_tx_signed (запись-до; исключение = НЕ отправляем);
      on_sent(tx_hash)           — store.dex_tx_sent;
      on_resolved(hash, state, info) — store.dex_tx_resolve.
    Сбой on_sent/on_resolved после отправки не роняет исполнение (строка останется SIGNED/SENT — сверка на старте
    найдёт исход по хэшу), но пишется в лог как ошибка."""

    def __init__(self, rpc: EvmRpc, chain_id: int, acct, on_signed: Callable[[dict], Any], *,
                 gate: Callable[[bool], None], on_sent: Callable[[str], Any] | None = None,
                 on_resolved: Callable[[str, str, dict], Any] | None = None, chain: str = "bsc",
                 lock_dir: Path | str | None = None, lock_wait_s: float = LOCK_WAIT_S,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 lookup_s: float = tconfig.TX_LOOKUP_S, bump_s: float = tconfig.TX_BUMP_S,
                 cancel_s: float = tconfig.TX_CANCEL_S, poll_s: float = tconfig.TX_RECEIPT_POLL_S):
        if acct is None or not getattr(acct, "address", None) or not hasattr(acct, "sign_transaction"):
            raise ValueError("нет EVM-ключа: отправитель существует только в live")
        if not callable(gate):
            raise ValueError("ворота режима обязательны: подпись без проверки режима запрещена")
        self.rpc, self.chain_id, self.chain = rpc, int(chain_id), chain
        self._acct = acct
        self.address = _checksum(acct.address)
        self._gate = gate
        self._on_signed, self._on_sent, self._on_resolved = on_signed, on_sent, on_resolved
        self.lock_path = Path(lock_dir or tconfig.TRADE_DB_PATH.parent) / tconfig.EVM_LOCK_FMT.format(
            addr=self.address.lower())
        self.lock_wait_s = lock_wait_s
        self.clock, self.sleep = clock, sleep
        self.lookup_s, self.bump_s, self.cancel_s, self.poll_s = lookup_s, bump_s, cancel_s, poll_s
        self._chain_ok = False
        self.last: SendInfo | None = None

    def __repr__(self) -> str:
        return f"EvmWallet({self.chain}, {self.address})"

    # --- замок кошелька ---
    @contextmanager
    def _wallet_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + self.lock_wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise WalletBusy(f"кошелёк {self.address} занят другим писателем ({self.lock_path.name})") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # --- журнал ---
    def _sent(self, h: str) -> None:
        if self._on_sent is None:
            return
        try:
            self._on_sent(h)
        except Exception as e:           # noqa — см. docstring класса
            log.error("evm: журнал SENT %s не записан: %s", h, redact(e))

    def note(self, tx_hash: str, state: str, **info) -> None:
        """Итог транзакции в журнал (и дописать суммы свопа после разбора чека)."""
        if self._on_resolved is None:
            return
        try:
            self._on_resolved(tx_hash, str(state), {k: v for k, v in info.items() if v is not None})
        except Exception as e:           # noqa
            log.error("evm: журнал %s → %s не записан: %s", tx_hash, state, redact(e))

    def _mark_unknown(self, info: SendInfo, why: str) -> None:
        for h in info.hashes:
            if h not in info.dead:
                self.note(h, DexTxState.UNKNOWN, err=why)

    # --- подпись ---
    def _sign_persist(self, info: SendInfo, kind: str, tx: dict, price: int, meta: dict) -> str:
        signed = self._acct.sign_transaction({"nonce": info.nonce, "gasPrice": int(price), "gas": int(tx["gas"]),
                                              "to": _checksum(tx["to"]), "value": int(tx["value"]),
                                              "data": _hexbytes(tx["data"]), "chainId": self.chain_id})
        raw = "0x" + bytes(signed.raw_transaction).hex()
        h = "0x" + bytes(signed.hash).hex()          # hexbytes ≥ 1: .hex() без 0x — собираем сами
        mark_public(h)
        self._on_signed({"clip_id": meta.get("clip_id"), "kind": str(kind), "chain": self.chain,
                         "wallet": self.address, "nonce": info.nonce, "to_addr": _addr(tx["to"]),
                         "value": int(tx["value"]),
                         "min_receive": meta.get("min_receive") if kind != DexTxKind.CANCEL else None,
                         "gas_limit": int(tx["gas"]), "gas_price": int(price), "raw_tx": raw, "tx_hash": h})
        info.hashes.append(h)
        info.kinds[h], info.raws[h], info.prices[h], info.sent_at[h] = str(kind), raw, int(price), self.clock()
        return h

    def _check_chain(self) -> None:
        if self._chain_ok:
            return
        got = self.rpc.chain_id()
        if got != self.chain_id:
            raise RuntimeError(f"RPC отвечает за сеть {got}, а подписываем для {self.chain_id} — не отправляю")
        self._chain_ok = True

    # --- отправка ---
    def send_and_wait(self, to: str, data: str, value: int, gas: int, gas_price: int, *, kind: str = DexTxKind.SWAP,
                      meta: dict | None = None) -> tuple[str, dict]:
        """Подписать, записать, отправить и дождаться чека. Возвращает (хэш замайненной, чек) — чек может быть с
        status 0 (откат: газ заплачен, токены не двигались). SentUnknown — исход неизвестен (пауза, сверка);
        TxRejected — узел отверг первую отправку (в сеть не ушла)."""
        meta = dict(meta or {})
        tx = {"to": _addr(to), "data": data, "value": int(value), "gas": int(gas)}
        _hexbytes(data)
        if tx["value"] < 0 or tx["gas"] <= 0 or int(gas_price) <= 0:
            raise ValueError("value < 0 или gas/gasPrice ≤ 0")
        with self._wallet_lock():
            self._gate(False)                        # до любых чтений и подписи: dry/readonly/пауза — отказ
            self._check_chain()
            latest = self.rpc.nonce(self.address, "latest")
            pending = self.rpc.nonce(self.address, "pending")
            if pending > latest:
                raise NoncePending(latest, pending)
            info = SendInfo(nonce=pending)
            self.last = info
            h = self._sign_persist(info, kind, tx, int(gas_price), meta)   # исключение здесь — ничего не ушло
            outcome, text = self.rpc.send_raw(info.raws[h])
            if outcome in ("rejected", "underpriced", "nonce_used"):
                info.dead.add(h)
                self.note(h, DexTxState.DROPPED, err=f"узел отверг: {text}")
                raise TxRejected(info.nonce, h, text)
            if outcome in ("ok", "known"):
                self._sent(h)
            else:
                log.warning("evm: отправка %s (nonce %d) — исход неизвестен (%s), жду чек и сверяю по хэшу",
                            h, info.nonce, redact(text))
            try:
                return self._wait(info, tx, int(gas_price), meta)
            except SentUnknown:
                raise
            except Exception as e:               # noqa — после отправки любой сбой = исход неизвестен
                why = f"сбой после отправки: {type(e).__name__}: {redact(e)}"
                self._mark_unknown(info, why)
                raise SentUnknown(info.nonce, info.hashes, why) from e

    def _sweep(self, info: SendInfo, everywhere: bool = False) -> tuple[str, dict] | None:
        for h in info.hashes:
            if h in info.dead:
                continue
            try:
                rc = self.rpc.receipt(h, everywhere)
            except Exception:            # noqa — чтение не удалось: следующий опрос
                continue
            if rc:
                return h, rc
        return None

    def _settle(self, info: SendInfo, h: str, rc: dict) -> tuple[str, dict]:
        info.winner = h
        st = _int(rc.get("status"))
        egp = rc.get("effectiveGasPrice")
        self.note(h, DexTxState.MINED_OK if st == 1 else DexTxState.MINED_REVERTED, block=_int(rc["blockNumber"]),
                  status=st, gas_used=_int(rc["gasUsed"]), eff_gas_price=_int(egp) if egp else info.prices[h])
        for other in info.hashes:                # один nonce майнится один раз: остальные наши — заменены
            if other != h and other not in info.dead:
                self.note(other, DexTxState.REPLACED)
        return h, rc

    def _lookup(self, info: SendInfo, h: str) -> None:
        """+15 с без чека: узел не знает хэш, а nonce свободен — те же сырые байты всем узлам (идемпотентно)."""
        try:
            if self.rpc.tx_by_hash(h):
                return                            # в пуле — ждём
            if self.rpc.nonce(self.address, "latest") > info.nonce:
                return                            # nonce уже занят — перепосылать нечего, решит опрос чеков
        except Exception:                # noqa — не знаем: ничего не делаем
            return
        outcome, text = self.rpc.rebroadcast(info.raws[h])
        log.warning("evm: %s пропала из пула — те же байты повторно (%s)", h, outcome)
        if outcome in ("ok", "known"):
            self._sent(h)

    def _replace(self, info: SendInfo, kind: str, tx: dict, price: int, meta: dict) -> int:
        """bump / cancel на ТОМ ЖЕ nonce. Возвращает новую цену (для следующей замены)."""
        newp = bump_price(price)
        try:
            newp = max(newp, self.rpc.gas_price())
        except Exception:                # noqa — без свежей цены сети хватит ×1.2
            pass
        try:
            self._gate(True)
        except ModeForbidden as e:
            log.error("evm: %s на nonce %d не отправлен — ворота режима: %s", kind, info.nonce, e)
            return price
        m = meta if kind != DexTxKind.CANCEL else {"clip_id": meta.get("clip_id")}
        h = self._sign_persist(info, kind, tx, newp, m)
        outcome, text = self.rpc.send_raw(info.raws[h])
        if outcome in ("ok", "known"):
            self._sent(h)
        elif outcome != "unknown":
            info.dead.add(h)
            self.note(h, DexTxState.DROPPED, err=f"узел отверг {kind}: {text}")
        log.warning("evm: %s nonce %d по цене %d → %s (%s)", kind, info.nonce, newp, h, outcome)
        return newp

    def _wait(self, info: SendInfo, tx: dict, price: int, meta: dict) -> tuple[str, dict]:
        t0 = info.sent_at[info.hashes[0]]
        bumped = False
        next_nonce_check = t0 + self.lookup_s
        consumed_at: float | None = None
        while True:
            hit = self._sweep(info, everywhere=consumed_at is not None)
            if hit:
                return self._settle(info, *hit)
            now = self.clock()
            for h in list(info.hashes):
                if h not in info.looked and h not in info.dead and now - info.sent_at[h] >= self.lookup_s:
                    info.looked.add(h)
                    self._lookup(info, h)
            if consumed_at is None and now >= next_nonce_check:
                next_nonce_check = now + self.lookup_s
                try:
                    if self.rpc.nonce(self.address, "latest") > info.nonce:
                        consumed_at = now
                except Exception:        # noqa
                    pass
            if consumed_at is not None and now - consumed_at >= self.lookup_s:
                why = "nonce занят, а чека ни по одному нашему хэшу нет (чужая транзакция или отстающие узлы)"
                self._mark_unknown(info, why)
                raise SentUnknown(info.nonce, info.hashes, why)
            if not bumped and consumed_at is None and now - t0 >= self.bump_s:
                bumped = True
                price = self._replace(info, DexTxKind.BUMP, tx, price, meta)
            if now - t0 >= self.cancel_s:
                if consumed_at is None:
                    self._replace(info, DexTxKind.CANCEL, {"to": self.address, "data": "0x", "value": 0,
                                                            "gas": tconfig.CANCEL_GAS}, price, meta)
                why = f"нет чека за {int(self.cancel_s)} с — отправлена отмена (0 самому себе на том же nonce)"
                self._mark_unknown(info, why)
                raise SentUnknown(info.nonce, info.hashes, why)
            self.sleep(self.poll_s)


def store_callbacks(con) -> dict:
    """Обработчики журнала EvmWallet поверх trade.db: EvmWallet(rpc, 56, keys.evm, gate=…, **store_callbacks(con))."""
    from . import store
    return {"on_signed": lambda row: store.dex_tx_signed(con, **row),
            "on_sent": lambda h: store.dex_tx_sent(con, h),
            "on_resolved": lambda h, state, info: store.dex_tx_resolve(con, h, state, **info)}
