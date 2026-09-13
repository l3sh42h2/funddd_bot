"""OKX DEX на EVM — спот-нога SpotLeg (trade_spec §4: «Guards before signing», «Gas», «Result»). BSC и Robinhood
Chain (4663, Arbitrum Nitro L2) — тот же класс, сеть только в chain/chain_id/RPC (tconfig.py). Allowlist роутера и
spender для 4663 пока ПУСТ (13.09.2026: доки OKX не отвечают с этой машины, см. tconfig.py у OKX_ROUTERS) — своп
и approve на этой сети сейчас всегда отказывают гардом "router"/"spender" с текстом «не подтверждён»; снять адреса
и заполнить allowlist — задача доктора на VPS с ключом, отдельно от этой копии.

Путь клипа: /approve-transaction → approve токену (если allowance не хватает) → /swap → гарды → предполётный
eth_estimateGas → EvmWallet.send_and_wait → разбор чека. Подписывается ТОЛЬКО то, что прошло все гарды; любой
провал — отказ с именем гарда (GuardError.guard) и отчёт владельцу, никаких «поправим и отправим».

Гарды до подписи — каждый против своей подмены ответа API:
  получатель: поле получателя, если оно вообще есть в ответе, = наш кошелёк (swapReceiverAddress мы не шлём);
  tx.from = кошелёк, tx.value = 0 (вход — ERC-20, нативную монету сети — BNB/ETH — роутеру не отдаём);
  tx.to — роутер из allowlist (OKX меняла роутер 30.03 и 04.08.2026: новый = стоп и отчёт, а не «принять»);
  токены маршрута и fromTokenAmount = запрошенные;
  не honeypot (ни вход, ни выход); taxRate = 0, если владелец не разрешил токены с налогом (пусто = не разрешил);
  signatureData пуст (иначе API просит подписать что-то сверх свопа);
  minReceiveAmount ≥ toTokenAmount·(1 − slip) — проскальзывание не шире заданного владельцем;
  |priceImpactPercent| ≤ impact_cap_pct (API проверяет его и сам — здесь второй замок);
  calldata (её и исполнит роутер; JSON выше — лишь слова API о ней): метод dagSwapByOrderId, в BaseRequest токены,
           сумма — запрошенные, minReturn ≥ minReceiveAmount; ABI канонический; хвост — только trim OKX (allowlist);
  approve: spender (dexContractAddress) из allowlist; calldata = approve(spender, ровно запрошенная сумма);
           to — КОНТРАКТ ТОКЕНА, а не dexContractAddress;
  предполётный eth_estimateGas от кошелька не откатывается (заодно бесплатная симуляция).

Газ: лимит max(tx.gas·1.5, оценка·1.3) (документация: «+50 %»), цена max(tx.gasPrice, eth_gasPrice); потолка нет
(владелец 12.09) — газ только в отчёте. Нативного должно хватить на лимит × цену (+ резерв владельца, если задан).

Итог — по чеку: amount_out = Transfer токена-выхода НА кошелёк (минус обратно), amount_in = Transfer токена-входа
С кошелька минус возвраты (роутер возвращает неизрасходованный вход). Сверка balanceOf на блоках N и N−1: нет
состояния N−1 у узла — «n/a», не провал; расхождение — status unknown (сделка на паузу). Баланс «сразу после» не
используется: отставание RPC так уже продало 0.033 из 0.083 ZEC (lphedge live_sol.close).
"""
from __future__ import annotations
import logging, time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Callable
from eth_abi import decode as abi_decode, encode as abi_encode
from .. import config
from . import tconfig
from .evm import EvmRpc, EvmWallet, MAX_UINT256, RpcError, RpcUnavailable, _addr, _int, _topic_addr, _word
from .keys import redact
from .store import DexTxKind, DexTxState
from .types import DexQuote, SwapResult

log = logging.getLogger(__name__)
D = Decimal
WEI = D(10) ** 18
# Где в ответе мог бы оказаться получатель. Мы его не шлём — значит, любое такое поле не на наш адрес = подмена.
RECEIVER_KEYS = ("swapReceiverAddress", "receiver", "receiverAddress", "recipient")
ALLOWANCE_WAIT_S = 6.0      # после замайненного approve узел может ещё секунду-две отдавать старый allowance


class GuardError(RuntimeError):
    """Гард до подписи не пройден: НЕ подписываю, отчёт владельцу. guard — короткое имя проверки."""

    def __init__(self, guard: str, msg: str):
        self.guard = guard
        super().__init__(f"гард {guard}: {msg}")


@dataclass
class EvmSwapResult(SwapResult):
    """SwapResult + то, что нужно отчёту и сверке. tx_state — итог строки dex_txs (MINED_OK, REPLACED, DROPPED…);
    balance_check — ok | mismatch | n/a; note — почему status unknown при замайненной транзакции."""
    kind: str = "swap"
    tx_state: str = ""
    min_receive: int | None = None
    quoted_out: int | None = None
    gas_used: int = 0
    eff_gas_price: int = 0
    balance_check: str = "n/a"
    hashes: tuple = ()
    note: str = ""


@dataclass(frozen=True)
class SwapBuild:
    """Ответ /swap, прошедший гарды: ровно то, что будет подписано, и цифры для отчёта."""
    token_in: str
    token_out: str
    amount_in: int
    quoted_out: int
    min_receive: int
    to: str
    data: str
    api_gas: int
    api_gas_price: int
    impact_pct: Decimal | None
    tax_in: Decimal
    tax_out: Decimal
    slippage_pct: Decimal
    trade_fee_usd: Decimal | None
    est_gas: int | None = None
    gas_limit: int = 0
    gas_price: int = 0
    preflight: str = "skipped"          # ok | skipped (dry без allowance: симуляция заведомо откатится)


# --- разбор значений ответа -------------------------------------------------------------------------
def _uint(x: Any, guard: str, what: str) -> int:
    try:
        v = _int(x)
    except (ValueError, TypeError):
        raise GuardError(guard, f"{what}: не целое ({str(x)[:40]!r})") from None
    if v < 0:
        raise GuardError(guard, f"{what}: отрицательное")
    return v


def _dec(x: Any, guard: str, what: str, default: Decimal | None = None) -> Decimal | None:
    if x is None or (isinstance(x, str) and not x.strip()):
        return default
    if isinstance(x, bool):
        raise GuardError(guard, f"{what}: bool вместо числа")
    try:
        d = D(str(x).strip())
    except InvalidOperation:
        raise GuardError(guard, f"{what}: не число ({str(x)[:40]!r})") from None
    if not d.is_finite():
        raise GuardError(guard, f"{what}: не конечное число")
    return d


def _flag(x: Any) -> bool:
    """isHoneyPot: bool или строка. Непонятное значение — «да»: гард ошибается только в безопасную сторону."""
    if isinstance(x, bool):
        return x
    if x is None:
        return False
    if isinstance(x, int):
        return x != 0
    return str(x).strip().lower() not in ("", "false", "0", "no")


def _lc(a: Any) -> str | None:
    try:
        return _addr(a)
    except ValueError:
        return None


def _empty_sig(v: Any) -> bool:
    if v in (None, "", [], {}, ()):
        return True
    if isinstance(v, (list, tuple)):
        return all(x in (None, "", [], {}) for x in v)
    return False


def _ceil_mul(n: int, m: Decimal) -> int:
    return int((D(int(n)) * m).to_integral_value(ROUND_CEILING))


def gas_limit_for(api_gas: int, est: int | None) -> int:
    """max(tx.gas·1.5, оценка·1.3): документация OKX просит +50 % к своему gas; оценка узла — со своим запасом."""
    return max(_ceil_mul(api_gas, tconfig.GAS_LIMIT_TX_MULT), _ceil_mul(est, tconfig.GAS_LIMIT_EST_MULT) if est else 0)


def min_receive_floor(quoted: int, slippage_pct: Decimal) -> int:
    """Наименьший допустимый minReceiveAmount: floor(toTokenAmount·(1 − slip/100)), целочисленно через Decimal."""
    return int((D(int(quoted)) * (D(100) - slippage_pct) / D(100)).to_integral_value(ROUND_FLOOR))


def _clip_id(ref: Any) -> int | None:
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref
    s = str(ref or "").strip()
    return int(s) if s.isdigit() else None


# --- calldata роутера ---------------------------------------------------------------------------------
def decode_dag_swap(data: str) -> tuple[tuple, bytes]:
    """calldata dagSwapByOrderId → (аргументы ABI, хвост после канонического ABI). Иной метод или не разбирается —
    GuardError."""
    if data[:10].lower() != tconfig.SEL_DAG_SWAP:
        raise GuardError("selector", f"метод роутера {data[:10]} незнаком (известен только dagSwapByOrderId "
                                     f"{tconfig.SEL_DAG_SWAP}) — стоп, нужен разбор нового метода")
    body = bytes.fromhex(data[10:])
    try:
        args = abi_decode(list(tconfig.DAG_SWAP_TYPES), body)
        head = abi_encode(list(tconfig.DAG_SWAP_TYPES), args)
    except Exception as e:               # noqa — любой сбой разбора = не подписываем
        raise GuardError("calldata", f"calldata не разбирается как dagSwapByOrderId ({type(e).__name__})") from None
    if body[:len(head)] != head:
        raise GuardError("calldata", "calldata не в каноническом ABI")
    return args, body[len(head):]


def _check_trim(tail: bytes, chain: str, quoted: int) -> None:
    flag = bytes.fromhex(tconfig.OKX_TRIM_FLAG)
    if len(tail) != 64 or tail[:6] != flag or tail[32:38] != flag or tail[6] != 0x80:
        raise GuardError("calldata_tail", f"хвост calldata ({len(tail)} байт) — не trim OKX: возможна чужая "
                                          "комиссия; стоп")
    expect, rate, to = int.from_bytes(tail[7:32], "big"), int.from_bytes(tail[38:44], "big"), "0x" + tail[44:].hex()
    ci = tconfig.chain_index(chain)
    if to not in tconfig.OKX_TRIM_RECEIVERS.get(ci, frozenset()):
        if not tconfig.OKX_TRIM_RECEIVERS.get(ci):
            raise GuardError("calldata_tail", f"получатель trim OKX для сети {ci} не подтверждён — allowlist пуст "
                                              "(сеть новая): снять живым /swap при doctor")
        raise GuardError("calldata_tail", f"получатель trim {to} не из allowlist OKX — стоп")
    if rate > tconfig.OKX_TRIM_RATE_MAX:
        raise GuardError("calldata_tail", f"доля trim {rate} больше увиденной вживую {tconfig.OKX_TRIM_RATE_MAX}")
    if expect < int(quoted):
        raise GuardError("calldata_tail", f"trim с {expect} < котировки {quoted}: доля OKX шла бы из котировки, "
                                          "а не из излишка")


def check_calldata(data: str, *, chain: str, token_in: str, token_out: str, amount: int, min_receive: int,
                   quoted: int) -> None:
    """Гарды самой calldata: подписывается и исполняется она, а не JSON. BaseRequest(fromToken, toToken,
    fromTokenAmount, minReturnAmount, deadLine) = запрошенное, minReturn не ниже проверенного minReceiveAmount
    (его и проверит роутер в сети); получатель у метода — msg.sender (= кошелёк, гард from); хвост — только trim."""
    (_oid, (from_tok, to_tok, amt, min_ret, _deadline), _paths), tail = decode_dag_swap(data)
    if from_tok >> 160 or "0x%040x" % from_tok != _addr(token_in):
        raise GuardError("calldata_token_in", f"calldata продаёт {hex(from_tok)}, а просили {token_in}")
    if str(to_tok).lower() != _addr(token_out):
        raise GuardError("calldata_token_out", f"calldata покупает {to_tok}, а просили {token_out}")
    if amt != int(amount):
        raise GuardError("calldata_amount", f"calldata тратит {amt}, а просили {amount}")
    if min_ret < int(min_receive):
        raise GuardError("calldata_min_return", f"minReturn в calldata {min_ret} < minReceiveAmount {min_receive}")
    if tail:
        _check_trim(tail, chain, quoted)


# --- гарды (чистые функции: тест подсовывает подменённый ответ) ------------------------------------------
def check_swap(resp: Any, *, chain: str, wallet: str, token_in: str, token_out: str, amount: int,
               slippage_pct: Decimal, impact_cap_pct: Decimal | None, allow_tax: bool) -> SwapBuild:
    """Все гарды ответа /swap. Провал — GuardError с именем гарда; успех — SwapBuild для подписи."""
    if not isinstance(resp, dict) or not isinstance(resp.get("routerResult"), dict) or not isinstance(resp.get("tx"), dict):
        raise GuardError("shape", "в ответе /swap нет routerResult или tx")
    rr, tx = resp["routerResult"], resp["tx"]
    me = _addr(wallet)
    for src in (resp, rr, tx):
        for k in RECEIVER_KEYS:
            v = src.get(k)
            if v not in (None, "") and _lc(v) != me:
                raise GuardError("receiver", f"{k} = {v}: получатель не наш кошелёк")
    if _lc(tx.get("from")) != me:
        raise GuardError("from", f"tx.from = {tx.get('from')}, а кошелёк {wallet}")
    if _uint(tx.get("value", "0"), "value", "tx.value") != 0:
        native = tconfig.NATIVE_SYMBOL.get(chain, "нативные")
        raise GuardError("value", f"tx.value = {tx.get('value')}: своп ERC-20 не отдаёт {native}")
    to = tx.get("to")
    if not tconfig.router_allowed(chain, to if isinstance(to, str) else None):
        if not tconfig.router_confirmed(chain):
            raise GuardError("router", f"адрес роутера OKX для сети {tconfig.chain_index(chain)} не подтверждён — "
                                       "allowlist пуст (сеть новая): снять живым /swap при doctor и отдать "
                                       "владельцу на подтверждение")
        raise GuardError("router", f"незнакомый роутер {to}: OKX меняла роутер 30.03 и 04.08 — стоп, нужен новый "
                                   "адрес в allowlist после проверки владельцем")
    ft, tt = rr.get("fromToken") or {}, rr.get("toToken") or {}
    if _lc(ft.get("tokenContractAddress")) != _addr(token_in):
        raise GuardError("token_in", f"маршрут продаёт {ft.get('tokenContractAddress')}, а просили {token_in}")
    if _lc(tt.get("tokenContractAddress")) != _addr(token_out):
        raise GuardError("token_out", f"маршрут покупает {tt.get('tokenContractAddress')}, а просили {token_out}")
    if _uint(rr.get("fromTokenAmount"), "amount", "fromTokenAmount") != int(amount):
        raise GuardError("amount", f"fromTokenAmount {rr.get('fromTokenAmount')} ≠ запрошенным {amount}")
    if _flag(ft.get("isHoneyPot")) or _flag(tt.get("isHoneyPot")):
        raise GuardError("honeypot", "токен помечен как honeypot")
    tax_in = _dec(ft.get("taxRate"), "tax", "fromToken.taxRate", D(0))
    tax_out = _dec(tt.get("taxRate"), "tax", "toToken.taxRate", D(0))
    if (tax_in != 0 or tax_out != 0) and not allow_tax:
        raise GuardError("tax", f"налог токена {tax_in}/{tax_out}, а allow_tax_tokens не включён")
    if not _empty_sig(tx.get("signatureData")):
        raise GuardError("signature_data", "signatureData не пуст: API просит подписать что-то сверх свопа")
    quoted = _uint(rr.get("toTokenAmount"), "min_receive", "toTokenAmount")
    mr = _uint(tx.get("minReceiveAmount"), "min_receive", "minReceiveAmount")
    need = min_receive_floor(quoted, slippage_pct)
    if quoted <= 0 or mr <= 0 or mr < need:
        raise GuardError("min_receive", f"minReceiveAmount {mr} < {need} = toTokenAmount {quoted}·(1 − {slippage_pct} %)")
    tx_slip = _dec(tx.get("slippagePercent"), "slippage", "tx.slippagePercent")
    if tx_slip is not None and tx_slip > slippage_pct:
        raise GuardError("slippage", f"в транзакции проскальзывание {tx_slip} % шире заданного {slippage_pct} %")
    impact = _dec(rr.get("priceImpactPercent"), "impact", "priceImpactPercent")
    if impact_cap_pct is not None and impact is not None and abs(impact) > impact_cap_pct:
        raise GuardError("impact", f"влияние на цену {impact} % больше потолка {impact_cap_pct} %")
    data = tx.get("data")
    if (not isinstance(data, str) or not data.startswith("0x") or len(data) < 10 or len(data) % 2
            or any(c not in "0123456789abcdefABCDEF" for c in data[2:])):
        raise GuardError("shape", "tx.data — не calldata")
    check_calldata(data, chain=chain, token_in=token_in, token_out=token_out, amount=amount, min_receive=mr,
                   quoted=quoted)
    api_gas = _uint(tx.get("gas"), "gas", "tx.gas")
    api_gp = _uint(tx.get("gasPrice"), "gas", "tx.gasPrice")
    if api_gas <= 0 or api_gp <= 0:
        raise GuardError("gas", f"tx.gas {api_gas} / gasPrice {api_gp}")
    try:
        fee = _dec(rr.get("tradeFee"), "shape", "tradeFee")
    except GuardError:
        fee = None                          # справочная цифра отчёта — не повод отказывать
    return SwapBuild(token_in=_addr(token_in), token_out=_addr(token_out), amount_in=int(amount), quoted_out=quoted,
                     min_receive=mr, to=_addr(to), data=data, api_gas=api_gas, api_gas_price=api_gp, impact_pct=impact,
                     tax_in=tax_in, tax_out=tax_out, slippage_pct=slippage_pct, trade_fee_usd=fee)


def check_approve(resp: Any, *, chain: str, token: str, amount: int) -> tuple[str, str, int, int]:
    """Гарды ответа /approve-transaction → (spender, calldata, gasLimit, gasPrice). calldata сверяется целиком:
    селектор approve, spender из ответа и ровно запрошенная сумма — ни байта сверх."""
    if not isinstance(resp, dict):
        raise GuardError("shape", "ответ /approve-transaction не объект")
    sp = resp.get("dexContractAddress")
    if not tconfig.spender_allowed(chain, sp if isinstance(sp, str) else None):
        if not tconfig.spender_confirmed(chain):
            raise GuardError("spender", f"адрес spender OKX (TokenApprove) для сети {tconfig.chain_index(chain)} "
                                        "не подтверждён — allowlist пуст (сеть новая): снять живым "
                                        "/approve-transaction при doctor и отдать владельцу на подтверждение")
        raise GuardError("spender", f"незнакомый spender {sp} — стоп, нужен allowlist после проверки владельцем")
    want = tconfig.SEL_APPROVE + _word(sp) + f"{int(amount):064x}"
    data = str(resp.get("data") or "").lower()
    if data != want:
        raise GuardError("approve_data", "calldata — не approve(spender, запрошенная сумма)")
    api_gas = _uint(resp.get("gasLimit"), "gas", "gasLimit")
    api_gp = _uint(resp.get("gasPrice"), "gas", "gasPrice")
    if api_gas <= 0 or api_gp <= 0:
        raise GuardError("gas", f"gasLimit {api_gas} / gasPrice {api_gp}")
    return _addr(sp), want, api_gas, api_gp


def transfer_flows(receipt: dict, wallet: str) -> dict[str, list[int]]:
    """ERC-20 Transfer из логов чека: токен → [пришло на кошелёк, ушло с кошелька]. ERC-721 (4 топика) и чужие
    события не считаются."""
    me = _addr(wallet)
    out: dict[str, list[int]] = {}
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        if len(topics) != 3 or str(topics[0]).lower() != tconfig.ERC20_TRANSFER_TOPIC:
            continue
        frm, to = _topic_addr(topics[1]), _topic_addr(topics[2])
        if me not in (frm, to):
            continue
        raw = str(lg.get("data") or "0x")
        v = int(raw, 16) if len(raw) > 2 else 0
        acc = out.setdefault(str(lg.get("address") or "").lower(), [0, 0])
        if to == me:
            acc[0] += v
        if frm == me:
            acc[1] += v
    return out


# --- нога -------------------------------------------------------------------------------------------
class OkxEvmSpot:
    """SpotLeg на OKX DEX + свой кошелёк. sender=None (dry/readonly) — котировки, балансы, сборка и гарды /swap
    работают; отправка — отказ (исполнение в dry — SimSpot). owner_cfg — OwnerCfg или функция без аргументов,
    возвращающая его (замороженная копия сделки): значения владельца читаются на каждый вызов."""

    def __init__(self, okx, rpc: EvmRpc, owner_cfg, *, chain: str = "bsc", wallet: str | None = None,
                 sender: EvmWallet | None = None, native_usd: Callable[[], Decimal | None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, allowance_wait_s: float = ALLOWANCE_WAIT_S):
        self.chain = chain
        self.ci = tconfig.chain_index(chain)
        self.chain_id = tconfig.CHAIN_IDS[chain]
        self._cfg = owner_cfg if callable(owner_cfg) else (lambda c=owner_cfg: c)
        if sender is not None and sender.chain_id != self.chain_id:
            raise ValueError(f"отправитель для сети {sender.chain_id}, а нога — {self.chain_id}")
        addr = wallet or (sender.address if sender is not None else None)
        if not addr:
            try:
                addr = self._cfg().get(f"wallets.{chain}")
            except KeyError:
                addr = None
        if not addr:
            raise ValueError(f"кошелёк сети {chain} не задан")
        if sender is not None and _addr(sender.address) != _addr(addr):
            raise ValueError(f"кошелёк отправителя {sender.address} ≠ кошелёк ноги {addr}")
        self.wallet = sender.address if sender is not None else addr
        self.okx, self.rpc, self.sender = okx, rpc, sender
        self.native_usd = native_usd
        self.stable, self.stable_dec = config.OKX_DEX_STABLES[self.ci]
        self._dec_cache: dict[str, int] = {_addr(self.stable): int(self.stable_dec)}
        self._sleep, self.allowance_wait_s = sleep, allowance_wait_s

    def __repr__(self) -> str:
        return f"OkxEvmSpot({self.chain}, {self.wallet}, {'live' if self.sender else 'без отправки'})"

    # --- чтение ---
    def decimals(self, token: str, hint: Any = None) -> int:
        """decimals() с цепи (поле OKX `decimal` — «справочное»). Цепь не ответила — подсказка OKX без кэша."""
        t = _addr(token)
        if t in self._dec_cache:
            return self._dec_cache[t]
        try:
            d = self.rpc.decimals(t)
        except Exception as e:           # noqa
            if hint:
                log.warning("evm: decimals(%s) не прочитан (%s) — беру OKX %s", t, redact(e), hint)
                return int(hint)
            raise
        if hint and int(hint) != d:
            log.warning("evm: OKX decimal %s ≠ decimals() %s у %s — беру цепь", hint, d, t)
        self._dec_cache[t] = d
        return d

    def quote(self, token_in: str, token_out: str, amount_units: int) -> DexQuote:
        """Котировка агрегатора. Неизвестные impact/gas/tax — None, а не 0 (цифры справочные, для планировщика)."""
        q = self.okx.quote(self.ci, token_in, token_out, int(amount_units))
        taxes = [t for t in (q.get("buy_tax"), q.get("sell_tax")) if t is not None]
        return DexQuote(chain=self.chain, token_in=_addr(token_in), token_out=_addr(token_out),
                        amount_in=int(q.get("from_amount") or amount_units), amount_out=int(q.get("to_amount") or 0),
                        dec_in=self.decimals(token_in, q.get("from_decimals")),
                        dec_out=self.decimals(token_out, q.get("to_decimals")),
                        impact_pct=q.get("price_impact"), gas_usd=q.get("gas_usd"),
                        tax=max(taxes) if taxes else None, honeypot=bool(q.get("honeypot")),
                        t=float(q.get("t") or time.time()))

    def balances(self, token: str) -> dict[str, int | None]:
        """stable / token / native в сырых единицах; не прочиталось — None (НЕИЗВЕСТНО, не ноль)."""
        out: dict[str, int | None] = {"stable": None, "token": None, "native": None}
        reads = (("stable", lambda: self.rpc.erc20_balance(self.stable, self.wallet)),
                 ("token", lambda: self.rpc.erc20_balance(token, self.wallet)),
                 ("native", lambda: self.rpc.native_balance(self.wallet)))
        for k, fn in reads:
            try:
                out[k] = int(fn())
            except Exception as e:       # noqa
                log.warning("evm: баланс %s не прочитан: %s", k, redact(e))
        return out

    def pool_price(self, token: str) -> Decimal | None:
        """Цена токена в стейбле по малой котировке (OKX_DEX_QUOTE_USD) — для замера восстановления пула r между
        клипами. Сбой — None."""
        amount = int(D(config.OKX_DEX_QUOTE_USD) * D(10) ** self.stable_dec)
        try:
            q = self.quote(self.stable, token, amount)
        except Exception as e:           # noqa
            log.warning("evm: цена пула %s не получена: %s", token, redact(e))
            return None
        if q.amount_out <= 0 or q.amount_in <= 0:
            return None
        return (D(q.amount_in) / D(10) ** q.dec_in) / (D(q.amount_out) / D(10) ** q.dec_out)

    def _allowance_any(self, token: str) -> int:
        """Наибольший allowance токена к spender'ам allowlist (для решения «гнать ли симуляцию» в dry)."""
        best = 0
        for sp in tconfig.OKX_SPENDERS.get(self.ci, ()):
            try:
                best = max(best, self.rpc.allowance(token, self.wallet, sp))
            except Exception:            # noqa — не знаем: считаем, что allowance нет
                pass
        return best

    # --- параметры владельца ---
    @staticmethod
    def _slippage(cfg) -> Decimal:
        """clip_slippage_pct (если задан) — но не шире потолка slippage_pct (3 % — жёсткий максимум владельца)."""
        slip, = cfg.require("dex.slippage_pct")
        clip = cfg.get("dex.clip_slippage_pct")
        if clip is not None and clip > slip:
            raise GuardError("slippage", f"clip_slippage_pct {clip} шире потолка slippage_pct {slip}")
        return clip if clip is not None else slip

    def _native_symbol(self) -> str:
        """Имя нативной монеты сети для текста гардов (BSC — BNB, Robinhood Chain — ETH); неизвестная сеть здесь
        не бывает — chain_id уже проверен в __init__ через tconfig.CHAIN_IDS."""
        return tconfig.NATIVE_SYMBOL.get(self.chain, "нативных")

    def _check_native(self, gas_limit: int, gas_price: int, cfg) -> None:
        """Газ без потолка, но оплатимым он быть обязан: нативной монеты сети ≥ лимит × цена (+ резерв владельца,
        если задан)."""
        native = self._native_symbol()
        try:
            bal = self.rpc.native_balance(self.wallet)
        except Exception as e:           # noqa
            raise GuardError("native", f"баланс {native} не прочитан: {redact(e)}") from None
        reserve = cfg.get("dex.native_reserve") or D(0)
        need = int(gas_limit) * int(gas_price) + int((D(reserve) * WEI).to_integral_value(ROUND_CEILING))
        if bal < need:
            raise GuardError("native", f"{native} {bal} wei < газ {gas_limit}×{gas_price} + резерв {reserve}")

    def _net_gas_price(self, api_gp: int) -> int:
        try:
            return max(int(api_gp), self.rpc.gas_price())
        except Exception as e:           # noqa — без цены сети — цена OKX (ниже 0.05 gwei она не бывала)
            log.warning("evm: eth_gasPrice не прочитан (%s) — цена OKX %d", redact(e), api_gp)
            return int(api_gp)

    def _estimate(self, tx: dict) -> int:
        try:
            return self.rpc.estimate_gas(tx)
        except RpcError as e:
            raise GuardError("preflight", f"предполётная симуляция откатилась: {e.message[:200]}") from None
        except RpcUnavailable as e:
            raise GuardError("preflight", f"симуляция недоступна: {e}") from None

    # --- approve ---
    def ensure_allowance(self, token: str, need: int, clip_ref: str) -> EvmSwapResult | None:
        """Allowance токена к spender'у OKX ≥ need; иначе approve отдельной транзакцией на КОНТРАКТ ТОКЕНА (размер —
        approve_policy владельца: exact = need, unlimited = 2^256−1). None — approve не нужен."""
        need = int(need)
        if need <= 0:
            return None
        cfg = self._cfg()
        policy, = cfg.require("dex.approve_policy")
        amount = need if policy == "exact" else MAX_UINT256
        resp = self.okx.approve_tx(self.ci, token, amount)
        spender, data, api_gas, api_gp = check_approve(resp, chain=self.chain, token=token, amount=amount)
        try:
            cur = self.rpc.allowance(token, self.wallet, spender)
        except Exception as e:           # noqa — неизвестный allowance не повод ни approve, ни своп
            raise GuardError("allowance", f"allowance не прочитан: {redact(e)}") from None
        if cur >= need:
            return None
        if self.sender is None:
            raise GuardError("mode", "нужен approve, а отправителя нет (режим не live)")
        est = self._estimate({"from": _addr(self.wallet), "to": _addr(token), "data": data})
        gas_limit, gp = gas_limit_for(api_gas, est), self._net_gas_price(api_gp)
        self._check_native(gas_limit, gp, cfg)
        h, rc = self.sender.send_and_wait(token, data, 0, gas_limit, gp, kind=DexTxKind.APPROVE,
                                          meta={"clip_id": _clip_id(clip_ref)})
        res = self._from_receipt(h, rc, kind="approve", fallback_price=gp)
        if res.status != "ok":
            return res
        for _ in range(int(self.allowance_wait_s) + 1):     # после approve — перечитать, прежде чем свопать
            try:
                if self.rpc.allowance(token, self.wallet, spender) >= need:
                    return res
            except Exception:            # noqa
                pass
            self._sleep(1.0)
        res.status, res.note = "unknown", "approve замайнен, а allowance не виден — своп не начинаю"
        return res

    # --- своп ---
    def build_swap(self, token_in: str, token_out: str, amount_units: int, *, preflight: bool | None = None,
                   cfg=None) -> SwapBuild:
        """/swap + все гарды (+ предполётная симуляция и газ). preflight=None — симуляция только при достаточном
        allowance (dry может гонять гарды при allowance 0: там estimateGas заведомо откатится)."""
        cfg = cfg or self._cfg()
        slip = self._slippage(cfg)
        if self.sender is not None:
            impact_cap, broadcast = cfg.require("dex.impact_cap_pct", "dex.broadcast")
            if broadcast != "public":
                raise GuardError("broadcast", f"broadcast = {broadcast}: отправка через OKX MEV не реализована — "
                                              "только public")
        else:
            impact_cap = cfg.get("dex.impact_cap_pct")
        amount = int(amount_units)
        if amount <= 0:
            raise ValueError("сумма свопа ≤ 0")
        resp = self.okx.swap(self.ci, token_in, token_out, amount, slip, impact_cap, self.wallet)
        b = check_swap(resp, chain=self.chain, wallet=self.wallet, token_in=token_in, token_out=token_out,
                       amount=amount, slippage_pct=slip, impact_cap_pct=impact_cap,
                       allow_tax=cfg.get("dex.allow_tax_tokens") is True)
        if preflight is None:
            preflight = self._allowance_any(token_in) >= amount
        if not preflight:
            return replace(b, gas_limit=gas_limit_for(b.api_gas, None), gas_price=b.api_gas_price)
        est = self._estimate({"from": _addr(self.wallet), "to": b.to, "data": b.data, "value": "0x0"})
        return replace(b, est_gas=est, gas_limit=gas_limit_for(b.api_gas, est),
                       gas_price=self._net_gas_price(b.api_gas_price), preflight="ok")

    def swap(self, token_in: str, token_out: str, amount_units: int, clip_ref: str) -> EvmSwapResult:
        """Своп клипа. SentUnknown / TxRejected / NoncePending пробрасываются как есть — движок ставит паузу."""
        if self.sender is None:
            raise GuardError("mode", "отправителя нет — своп только в live (в dry исполняет SimSpot)")
        cfg = self._cfg()
        b = self.build_swap(token_in, token_out, amount_units, preflight=True, cfg=cfg)
        self._check_native(b.gas_limit, b.gas_price, cfg)
        h, rc = self.sender.send_and_wait(b.to, b.data, 0, b.gas_limit, b.gas_price, kind=DexTxKind.SWAP,
                                          meta={"clip_id": _clip_id(clip_ref), "min_receive": b.min_receive})
        return self._from_receipt(h, rc, kind="swap", token_in=b.token_in, token_out=b.token_out,
                                  min_receive=b.min_receive, quoted_out=b.quoted_out, requested_in=b.amount_in,
                                  fallback_price=b.gas_price, record=True)

    # --- чек ---
    def _gas_usd(self, gas_wei: int) -> float | None:
        if self.native_usd is None:
            return None
        try:
            px = self.native_usd()
        except Exception:                # noqa
            return None
        return None if px is None else float(D(gas_wei) / WEI * D(str(px)))

    def _balance_check(self, token_in: str, token_out: str, block: int, a_in: int, a_out: int) -> str:
        """balanceOf на N и N−1 против логов. Нет состояния N−1 (или узла) — n/a, не провал."""
        if block <= 0:
            return "n/a"
        try:
            o1 = self.rpc.erc20_balance(token_out, self.wallet, block)
            o0 = self.rpc.erc20_balance(token_out, self.wallet, block - 1)
            i1 = self.rpc.erc20_balance(token_in, self.wallet, block)
            i0 = self.rpc.erc20_balance(token_in, self.wallet, block - 1)
        except Exception:                # noqa
            return "n/a"
        return "ok" if (o1 - o0 == a_out and i0 - i1 == a_in) else "mismatch"

    def _from_receipt(self, h: str, rc: dict, *, kind: str, token_in: str | None = None, token_out: str | None = None,
                      min_receive: int | None = None, quoted_out: int | None = None, requested_in: int | None = None,
                      fallback_price: int = 0, record: bool = False, nonce: int | None = None) -> EvmSwapResult:
        info = self.sender.last if self.sender is not None else None
        if nonce is None:
            nonce = info.nonce if info is not None else -1
        hashes = tuple(info.hashes) if (info is not None and h in info.hashes) else (h,)
        base = dict(tx_hash=h, nonce=nonce, kind=kind, min_receive=min_receive, quoted_out=quoted_out, hashes=hashes)
        try:
            st, block, gas_used = _int(rc.get("status")), _int(rc.get("blockNumber")), _int(rc.get("gasUsed"))
            egp = _int(rc["effectiveGasPrice"]) if rc.get("effectiveGasPrice") else int(fallback_price)
        except (ValueError, TypeError, KeyError) as e:
            return EvmSwapResult(status="unknown", amount_in=0, amount_out=0, gas_wei=0, gas_usd=None, block=0,
                                 tx_state=DexTxState.UNKNOWN, note=f"чек не разобран: {e}", **base)
        gas_wei = gas_used * egp
        common = dict(gas_wei=gas_wei, gas_usd=self._gas_usd(gas_wei), block=block, gas_used=gas_used,
                      eff_gas_price=egp, **base)
        if st != 1:
            return EvmSwapResult(status="reverted", amount_in=0, amount_out=0, tx_state=DexTxState.MINED_REVERTED,
                                 **common)
        if kind != "swap":                  # approve / cancel: токены не двигаются
            return EvmSwapResult(status="ok", amount_in=0, amount_out=0, tx_state=DexTxState.MINED_OK, **common)
        fl = transfer_flows(rc, self.wallet)
        notes: list[str] = []
        if token_in is None or token_out is None:   # сверка после рестарта: токены — по логам
            outs = [t for t, (i, o) in fl.items() if o > i]
            ins = [t for t, (i, o) in fl.items() if i > o]
            token_in = token_in or (outs[0] if len(outs) == 1 else None)
            token_out = token_out or (ins[0] if len(ins) == 1 else None)
        if token_in is None or token_out is None:
            notes.append("токены свопа по логам не определить")
            a_in = a_out = 0
            check = "n/a"
        else:
            ti, to = _addr(token_in), _addr(token_out)
            got_in, got_out = fl.get(ti, [0, 0]), fl.get(to, [0, 0])
            a_out = got_out[0] - got_out[1]
            a_in = got_in[1] - got_in[0]            # ушло минус возврат роутера
            if a_out <= 0:
                notes.append("выход не пришёл на кошелёк")
            if a_in <= 0:
                notes.append("вход не списан")
            if min_receive is not None and a_out < min_receive:
                notes.append(f"получено {a_out} < minReceive {min_receive}: получатель не мы или токен с налогом")
            if requested_in is not None and a_in > requested_in:
                notes.append(f"списано {a_in} > запрошенных {requested_in}")
            check = self._balance_check(ti, to, block, a_in, a_out)
            if check == "mismatch":
                notes.append("balanceOf на N/N−1 не сходится с логами Transfer")
        res = EvmSwapResult(status="unknown" if notes else "ok", amount_in=max(a_in, 0), amount_out=max(a_out, 0),
                            tx_state=DexTxState.MINED_OK, balance_check=check, note="; ".join(notes), **common)
        if record and self.sender is not None:
            self.sender.note(h, DexTxState.MINED_OK, amount_in=res.amount_in, amount_out=res.amount_out,
                             err=res.note or None)
        if notes:
            log.error("evm: своп %s замайнен, но: %s", h, res.note)
        return res

    # --- сверка на старте ---
    def resolve(self, tx_row: dict) -> EvmSwapResult:
        """Исход строки dex_txs по цепи — только чтение, ничего не отправляет. tx_row может нести token_in/token_out
        (из сделки); иначе токены определяются по логам. tx_state: MINED_OK / MINED_REVERTED; REPLACED — nonce занят
        другой транзакцией (ищите чек у соседей по nonce: store.dex_txs_on_nonce); SENT — ещё в пуле; DROPPED — узел
        не знает хэш, а nonce свободен; UNKNOWN — сеть не ответила."""
        h = str(tx_row["tx_hash"]).lower()
        nonce = int(tx_row["nonce"]) if tx_row.get("nonce") is not None else -1
        kind = str(tx_row.get("kind") or "swap")
        logical = kind if kind in ("approve", "cancel") else "swap"
        base = dict(tx_hash=h, status="unknown", amount_in=0, amount_out=0, gas_wei=0, gas_usd=None, block=0,
                    nonce=nonce, kind=logical, hashes=(h,))
        try:
            rc = self.rpc.receipt(h, everywhere=True)
        except Exception as e:           # noqa
            return EvmSwapResult(tx_state=DexTxState.UNKNOWN, note=f"чек не прочитан: {redact(e)}", **base)
        if rc:
            mr = tx_row.get("min_receive")
            return self._from_receipt(h, rc, kind=logical, token_in=tx_row.get("token_in"),
                                      token_out=tx_row.get("token_out"), min_receive=int(mr) if mr else None,
                                      fallback_price=int(tx_row.get("gas_price") or 0), nonce=nonce)
        try:
            latest = self.rpc.nonce(self.wallet, "latest")
            known = self.rpc.tx_by_hash(h)
        except Exception as e:           # noqa
            return EvmSwapResult(tx_state=DexTxState.UNKNOWN, note=f"сеть не ответила: {redact(e)}", **base)
        if nonce >= 0 and latest > nonce:
            return EvmSwapResult(tx_state=DexTxState.REPLACED, note="nonce занят другой транзакцией", **base)
        if known:
            return EvmSwapResult(tx_state=DexTxState.SENT, note="ещё в пуле", **base)
        return EvmSwapResult(tx_state=DexTxState.DROPPED, note="узел хэш не знает, nonce свободен", **base)
