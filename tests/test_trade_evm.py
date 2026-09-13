"""EVM-нога фазы 2: RPC с переключением, единственный писатель кошелька (запись-до, график зависшей), гарды /swap и
/approve-transaction, разбор чека, сверка после рестарта, общий темп ключа OKX. Всё на подделках: FakeChain (узлы
JSON-RPC в памяти, транзакции раскодируются из сырых байтов) + FakeOkx (ответы агрегатора с подменами).

Ключи — заведомо фальшивые скаляры (7) и открытый тестовый вектор из текста EIP-155 (0x46…46): сети нет, реальных
ключей нет, ничего не подписывается для настоящей сети."""
import fcntl, json, os
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import pytest
import requests

rlp = pytest.importorskip("rlp")
pytest.importorskip("eth_account")
from eth_account import Account
from eth_utils import keccak
from funding_bot import okxdex
from funding_bot.okxdex import OkxDex
from funding_bot.trade import evm, evm_swap, keys, owner, store, tconfig
from funding_bot.trade.evm import (EvmRpc, EvmWallet, NoncePending, RpcError, RpcUnavailable, SentUnknown,
                                   TxRejected, WalletBusy)
from funding_bot.trade.evm_swap import GuardError, OkxEvmSpot, check_approve, check_swap
from funding_bot.trade.types import SpotLeg

USDT = "0x55d398326f99059ff775485246999027b3197955"
AIW3 = "0x37e94fc028903e74275478b65160d6d2c0e8880b"
ROUTER = "0x5994814f2c4040b863a0125a45de152a8c2a4dec"
SPENDER = "0x2c34a2fb1d0b4f55de51e1d0bdefaddce6b7cdd6"
POOL = "0x60f9c05e000000000000000000000000000fab44"
OTHER = "0x000000000000000000000000000000000000beef"
NEW_ROUTER = "0x" + "12" * 20
URLS = ("https://rpc-a.test", "https://rpc-b.test")
TRANSFER = tconfig.ERC20_TRANSFER_TOPIC
APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
E18 = 10 ** 18
RATE = 24                                   # AIW3 за 1 USDT в подделке


def _w(a: str) -> str:
    return "0" * 24 + a.lower()[2:]


def _log(token, frm, to, v):
    return {"address": token, "topics": [TRANSFER, "0x" + _w(frm), "0x" + _w(to)], "data": "0x" + f"{v:064x}"}


def _decode(raw: str) -> dict:
    """Сырые байты → поля legacy-транзакции + отправитель по подписи (как это сделает узел)."""
    b = bytes.fromhex(raw[2:])
    assert b[0] >= 0xC0, "не legacy (тип 0) транзакция"
    f = rlp.decode(b)
    i = lambda x: int.from_bytes(x, "big")
    return {"hash": "0x" + keccak(b).hex(), "raw": raw, "nonce": i(f[0]), "gasPrice": i(f[1]), "gas": i(f[2]),
            "to": "0x" + f[3].hex(), "value": i(f[4]), "data": "0x" + f[5].hex(), "v": i(f[6]),
            "chainId": (i(f[6]) - 35) // 2, "sender": Account.recover_transaction(raw).lower()}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, dt):
        self.t += dt


class _Resp:
    def __init__(self, body, status=200):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class _RpcErr(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


class FakeChain:
    """Узлы JSON-RPC одной сети (состояние общее, узлы различаются только хостом для отказов). Пул транзакций,
    замена по nonce с надбавкой ≥10 %, майнинг при опросе чека по правилу mine_rule, логи Transfer, возвраты
    роутера, откаты, пропавшие и зависшие транзакции, чужая транзакция на нашем nonce, история балансов по блокам."""
    TOKENS = (USDT, AIW3)

    def __init__(self, wallet: str, clock: Clock):
        self.w, self.clock = wallet.lower(), clock
        self.block, self.latest, self.gas_price = 100, 43, 50_000_000
        self.bal: dict = {}
        self.native = {self.w: 10 ** 17}
        self.allow: dict = {}
        self.snap: dict = {}
        self.pool: dict = {}
        self.known: set = set()
        self.receipts: dict = {}
        self.sent: list = []
        self.calls: list = []
        self.down: set = set()
        self.mine_rule = lambda tx: True
        self.drop_next = 0
        self.send_error = None
        self.estimate_error = None
        self.refund = 0
        self.revert_next = False
        self.steal_to = None
        self.silent_credit = 0
        self.no_state_below = None
        self.foreign_at = None
        self.malformed = False
        self.before_send = None
        self.pending_extra = 0

    # --- транспорт requests.Session.post ---
    def post(self, url, json=None, timeout=None):
        payload, host = json, url.split("//", 1)[1]
        m = payload["method"]
        self.calls.append((host, m))
        if host in self.down or (host, m) in self.down:
            raise requests.ConnectionError(f"{host} недоступен")
        try:
            res = getattr(self, m)(*payload["params"])
        except _RpcErr as e:
            return _Resp({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": e.code, "message": e.msg}})
        return _Resp({"jsonrpc": "2.0", "id": payload["id"], "result": res})

    def methods(self, name):
        return [c for c in self.calls if c[1] == name]

    # --- чтение ---
    def eth_chainId(self):
        return hex(56)

    def eth_blockNumber(self):
        return hex(self.block)

    def eth_gasPrice(self):
        return hex(self.gas_price)

    def eth_getTransactionCount(self, addr, tag):
        assert addr.lower() == self.w
        n = self.latest
        if tag == "pending":
            n += len({t["nonce"] for t in self.pool.values()}) + self.pending_extra
        return hex(n)

    def eth_getBalance(self, addr, tag):
        return hex(self.native.get(addr.lower(), 0))

    def _state(self, tag):
        if tag == "latest":
            return self.bal
        blk = int(tag, 16)
        if self.no_state_below is not None and blk < self.no_state_below:
            raise _RpcErr(-32000, "missing trie node 5f3a (path )")
        if blk >= self.block:
            return self.bal
        ks = [k for k in self.snap if k <= blk]
        return self.snap[max(ks)] if ks else {}

    def eth_call(self, tx, tag):
        to, data = tx["to"].lower(), tx["data"]
        if to not in self.TOKENS:
            return "0x"                                      # по адресу нет кода
        sel = data[:10]
        if sel == tconfig.SEL_BALANCE_OF:
            v = self._state(tag).get((to, "0x" + data[-40:]), 0)
        elif sel == tconfig.SEL_ALLOWANCE:
            v = self.allow.get((to, "0x" + data[34:74], "0x" + data[98:138]), 0)
        elif sel == evm.SEL_DECIMALS:
            v = 18
        else:
            raise _RpcErr(3, "execution reverted")
        return "0x" + f"{v:064x}"

    def eth_estimateGas(self, tx):
        if self.estimate_error:
            raise _RpcErr(3, self.estimate_error)
        to, data = tx["to"].lower(), tx["data"]
        if to == ROUTER:
            (_oid, (ft, _tout, amt, _mn, _dl), _paths), _tail = evm_swap.decode_dag_swap(data)
            ti = "0x%040x" % (ft & ((1 << 160) - 1))
            if self.allow.get((ti, self.w, SPENDER), 0) < amt:
                raise _RpcErr(3, "execution reverted: allowance")
            return hex(300_000)
        if data.startswith(tconfig.SEL_APPROVE):
            return hex(46_000)
        return hex(21_000)

    def eth_getTransactionByHash(self, h):
        return {"hash": h} if h in self.known else None

    def eth_getTransactionReceipt(self, h):
        self._mine()
        rc = self.receipts.get(h)
        if rc and self.malformed:
            rc = {k: v for k, v in rc.items() if k != "status"}
        return rc

    # --- отправка и майнинг ---
    def eth_sendRawTransaction(self, raw):
        rec = _decode(raw)
        rec["t"] = self.clock()
        if self.before_send:
            self.before_send(rec)
        if self.send_error:
            raise _RpcErr(-32000, self.send_error)
        assert rec["sender"] == self.w and rec["chainId"] == 56
        if rec["hash"] in self.known:
            raise _RpcErr(-32000, "already known")
        if rec["nonce"] < self.latest:
            raise _RpcErr(-32000, "nonce too low")
        old = [h for h, t in self.pool.items() if t["nonce"] == rec["nonce"]]
        for h in old:
            if rec["gasPrice"] * 10 < self.pool[h]["gasPrice"] * 11:
                raise _RpcErr(-32000, "replacement transaction underpriced")
        for h in old:                                        # замена вытесняет прежнюю из пула
            self.pool.pop(h)
            self.known.discard(h)
        self.sent.append(rec)
        if self.drop_next:                                   # принял и потерял
            self.drop_next -= 1
            return rec["hash"]
        self.pool[rec["hash"]] = rec
        self.known.add(rec["hash"])
        return rec["hash"]

    def _next_block(self, winner=None):
        for h in [h for h, t in self.pool.items() if t["nonce"] == self.latest]:
            self.pool.pop(h)
            if h != winner:
                self.known.discard(h)
        self.snap[self.block] = dict(self.bal)                # состояние на конец блока N−1
        self.block += 1
        self.latest += 1

    def _mine(self):
        if self.foreign_at is not None and self.clock() >= self.foreign_at:
            self.foreign_at = None
            self._next_block()                                # чужая транзакция заняла наш nonce
        while True:
            cand = [t for t in self.pool.values() if t["nonce"] == self.latest and self.mine_rule(t)]
            if not cand:
                return
            self._execute(max(cand, key=lambda t: t["gasPrice"]))

    def _execute(self, t):
        self._next_block(t["hash"])
        to, data, logs, ok, gas_used = t["to"], t["data"], [], True, 21_000
        if self.revert_next:
            self.revert_next, ok, gas_used = False, False, 120_000
        elif to == ROUTER:
            gas_used = 250_000
            (_oid, (ft, tout, amt, mn, _dl), _paths), _tail = evm_swap.decode_dag_swap(data)
            ti, tout = "0x%040x" % (ft & ((1 << 160) - 1)), tout.lower()
            spent = amt - self.refund
            out = spent * RATE if ti == USDT else spent // RATE
            key = (ti, self.w, SPENDER)
            if self.allow.get(key, 0) < amt or self.bal.get((ti, self.w), 0) < amt or out < mn:
                ok = False
            else:
                rcpt = self.steal_to or self.w
                self.allow[key] -= spent
                self.bal[(ti, self.w)] -= spent
                self.bal[(tout, rcpt)] = self.bal.get((tout, rcpt), 0) + out
                if self.silent_credit:
                    self.bal[(tout, self.w)] = self.bal.get((tout, self.w), 0) + self.silent_credit
                logs = [_log(ti, self.w, ROUTER, amt), _log(tout, POOL, rcpt, out),
                        _log(tout, POOL, OTHER, 777),                                   # чужой перевод
                        {"address": ti, "topics": [APPROVAL, "0x" + _w(self.w), "0x" + _w(SPENDER)], "data": "0x00"},
                        {"address": tout, "topics": [TRANSFER, "0x" + _w(POOL), "0x" + _w(self.w), "0x" + "0" * 63 + "5"],
                         "data": "0x"}]                                                  # ERC-721: 4 топика
                if self.refund:
                    logs.append(_log(ti, ROUTER, self.w, self.refund))
        elif data.startswith(tconfig.SEL_APPROVE):
            gas_used = 46_000
            self.allow[(to, self.w, "0x" + data[34:74])] = int(data[74:138], 16)
            logs = [{"address": to, "topics": [APPROVAL, "0x" + _w(self.w), "0x" + data[10:74]], "data": "0x" + data[74:138]}]
        self.native[self.w] -= gas_used * t["gasPrice"]
        self.receipts[t["hash"]] = {"transactionHash": t["hash"], "status": "0x1" if ok else "0x0",
                                    "blockNumber": hex(self.block), "gasUsed": hex(gas_used),
                                    "effectiveGasPrice": hex(t["gasPrice"]), "logs": logs if ok else []}


TRIM_TO = "0xfa00a9ed787f3793db668bff3e6e6e7db0f92a1b"


def dag_calldata(token_in, token_out, amount, min_ret, quoted, *, from_hi=0, sel=tconfig.SEL_DAG_SWAP, trim_to=TRIM_TO,
                 trim_rate=100, expect=None, extra=b""):
    """calldata dagSwapByOrderId в кодировке живого OKX (ABI + 64 байта trim), как в ответах /swap 12.09."""
    from eth_abi import encode
    path = ([POOL], [POOL], [(1 << 255) | 10000], [b""], int(token_in, 16))
    body = encode(list(tconfig.DAG_SWAP_TYPES),
                  [7, ((from_hi << 160) | int(token_in, 16), token_out.lower(), amount, min_ret, 1_900_000_000), [path]])
    flag = bytes.fromhex(tconfig.OKX_TRIM_FLAG)
    tail = (flag + b"\x80" + (quoted if expect is None else expect).to_bytes(25, "big")
            + flag + trim_rate.to_bytes(6, "big") + bytes.fromhex(trim_to[2:])) if trim_to else b""
    return sel + (body + tail + extra).hex()


class FakeOkx:
    """Ответы агрегатора (форма v6) + подмены tamper/tamper_approve. calldata роутера — настоящая кодировка
    dagSwapByOrderId с хвостом trim (dag_calldata); FakeChain исполняет именно её."""

    def __init__(self, wallet: str):
        self.wallet, self.calls = wallet, []
        self.tamper = self.tamper_approve = None
        self.impact = "-0.06"

    def swap(self, chain, token_in, token_out, amount_units, slippage_pct, impact_cap_pct, wallet):
        self.calls.append(("swap", dict(chain=chain, token_in=token_in, token_out=token_out, amount=amount_units,
                                        slip=slippage_pct, impact=impact_cap_pct, wallet=wallet)))
        out = amount_units * RATE if token_in.lower() == USDT else amount_units // RATE
        mr = evm_swap.min_receive_floor(out, Decimal(slippage_pct))
        data = dag_calldata(token_in, token_out, amount_units, mr, out)
        tok = lambda a: {"tokenContractAddress": a, "decimal": "18", "isHoneyPot": False, "taxRate": "0"}
        resp = {"routerResult": {"chainIndex": chain, "fromTokenAmount": str(amount_units), "toTokenAmount": str(out),
                                 "priceImpactPercent": self.impact, "tradeFee": "0.012", "estimateGasFee": "250000",
                                 "fromToken": tok(token_in), "toToken": tok(token_out)},
                "tx": {"from": wallet, "to": ROUTER, "data": data, "value": "0", "gas": "250000", "gasPrice": "50000000",
                       "maxPriorityFeePerGas": "50000000", "minReceiveAmount": str(mr), "signatureData": [""],
                       "slippagePercent": format(Decimal(slippage_pct), "f")}}
        if self.tamper:
            self.tamper(resp)
        return resp

    def approve_tx(self, chain, token, amount_units):
        self.calls.append(("approve", dict(chain=chain, token=token, amount=amount_units)))
        resp = {"data": "0x095ea7b3" + _w(SPENDER) + f"{amount_units:064x}", "dexContractAddress": SPENDER,
                "gasLimit": "50000", "gasPrice": "50000000"}
        if self.tamper_approve:
            self.tamper_approve(resp)
        return resp

    def quote(self, chain, from_addr, to_addr, amount_units):
        out = amount_units * RATE if from_addr.lower() == USDT else amount_units // RATE
        return dict(from_amount=amount_units, to_amount=out, price_impact=-0.06, gas_usd=0.012, from_decimals=18,
                    to_decimals=18, buy_tax=0.0, sell_tax=0.0, honeypot=False, t=1.0)


def _cfg(tmp_path, wallet, **dex):
    d = {"slippage_pct": "3", "impact_cap_pct": "5", "approve_policy": '"exact"', "broadcast": '"public"',
         "allow_tax_tokens": "false", "native_reserve": "0.001"}
    d.update(dex)
    lines = ['mode = "live"', "[wallets]", f'bsc = "{wallet}"', "[dex]"] + [f"{k} = {v}" for k, v in d.items() if v is not None]
    p = tmp_path / f"owner_{len(list(tmp_path.glob('owner_*')))}.toml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return owner.load(p)


@pytest.fixture
def env(tmp_path):
    keys._reset_redaction_for_tests()
    clock = Clock()
    acct = keys.SignerKey(Account.from_key(bytes.fromhex(f"{7:064x}")), "evm")
    chain = FakeChain(acct.address, clock)
    rpc = EvmRpc(URLS, session=chain)
    con = store.connect(tmp_path / "trade.db")
    gs = {"mode": "live", "paused": False}

    def gate(in_flight):
        keys.gate(gs["mode"], "send", paused=gs["paused"], hedge=in_flight)

    wallet = EvmWallet(rpc, 56, acct, gate=gate, lock_dir=tmp_path, clock=clock, sleep=clock.sleep,
                       **evm.store_callbacks(con))
    okx = FakeOkx(acct.address)
    cfg = _cfg(tmp_path, acct.address)
    spot = OkxEvmSpot(okx, rpc, cfg, sender=wallet, native_usd=lambda: Decimal("600"), sleep=clock.sleep)
    w = acct.address.lower()
    chain.bal[(USDT, w)] = 950 * E18
    yield SimpleNamespace(clock=clock, acct=acct, chain=chain, rpc=rpc, con=con, gs=gs, wallet=wallet, okx=okx,
                          cfg=cfg, spot=spot, w=w, tmp=tmp_path)
    keys._reset_redaction_for_tests()


def _allow(env, token=USDT):
    env.chain.allow[(token, env.w, SPENDER)] = 10 ** 30


def _row(env, h):
    return store.get_dex_tx(env.con, h)


# ================================ okxdex: swap / approve / общий темп ================================
class _R:
    def __init__(self, body, code=200):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self):
        return self._b


class _S:
    def __init__(self, body):
        self.body, self.calls, self.headers = body, [], {}

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append(dict(method=method, url=url, data=data))
        return _R(self.body)


def _okx(tmp_path, body=None):
    return OkxDex(session=_S(body or {"code": "0", "data": [{"routerResult": {}, "tx": {}}]}), key="k", secret="s",
                  passphrase="p", rps=1000, pace_path=tmp_path / "okxdex.pace")


def _q(call):
    u = urlsplit(call["url"])
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


def test_okxdex_swap_sends_only_allowed_params(tmp_path):
    d = _okx(tmp_path)
    wallet = "0xE4Ebf0815d0980E5a03f7D675F86dc5079fB8919"
    assert d.swap("56", USDT, AIW3, 500 * E18, Decimal("3"), Decimal("1.5"), wallet) == {"routerResult": {}, "tx": {}}
    path, q = _q(d._s.calls[0])
    assert path == "/api/v6/dex/aggregator/swap" and d._s.calls[0]["method"] == "GET"
    assert q == {"chainIndex": "56", "amount": str(500 * E18), "fromTokenAddress": USDT, "toTokenAddress": AIW3,
                 "slippagePercent": "3", "userWalletAddress": wallet, "gasLevel": "average",
                 "priceImpactProtectionPercent": "1.5"}
    for bad in ("swapReceiverAddress", "feePercent", "dexIds", "slippage", "approveTransaction", "approveAmount"):
        assert bad not in q
    d.swap("56", USDT, AIW3, 1, Decimal("0.5"), None, wallet)            # без потолка — параметр не шлётся
    _, q2 = _q(d._s.calls[1])
    assert "priceImpactProtectionPercent" not in q2 and q2["slippagePercent"] == "0.5"
    with pytest.raises(TypeError):
        d.swap("56", USDT, AIW3, 1, 3.0, None, wallet)                     # float в процентах — нет
    with pytest.raises(ValueError):
        d.swap("56", USDT, AIW3, 0, Decimal(3), None, wallet)
    with pytest.raises(ValueError):
        d.swap("56", USDT, AIW3, 1, Decimal(0), None, wallet)
    assert len(d._s.calls) == 2                                            # отказы — до запроса


def test_okxdex_approve_tx_params_and_empty_answer(tmp_path):
    d = _okx(tmp_path, {"code": "0", "data": [{"data": "0x095e", "dexContractAddress": SPENDER}]})
    assert d.approve_tx("56", USDT, 123)["dexContractAddress"] == SPENDER
    path, q = _q(d._s.calls[0])
    assert path == "/api/v6/dex/aggregator/approve-transaction"
    assert q == {"chainIndex": "56", "tokenContractAddress": USDT, "approveAmount": "123"}
    with pytest.raises(RuntimeError, match="пустой ответ"):
        _okx(tmp_path, {"code": "0", "data": []}).approve_tx("56", USDT, 1)


def test_okxdex_pace_is_shared_between_processes(tmp_path):
    p = tmp_path / "okxdex.pace"
    a = OkxDex(key="k", secret="s", passphrase="p", rps=1, pace_path=p)     # «коллектор»
    b = OkxDex(key="k", secret="s", passphrase="p", rps=1, pace_path=p)     # «трейдер»
    gap = a.min_gap
    assert a._reserve(100.0) == 0
    assert b._reserve(100.0) == pytest.approx(gap)                           # чужой слот занят — ждёт за ним
    assert a._reserve(100.5) == pytest.approx(100 + 2 * gap - 100.5)
    assert float(p.read_text()) == pytest.approx(100 + 2 * gap)
    for junk in ("9e18", "nan", "мусор", ""):                               # мусор/часы назад — не ждать часами
        p.write_text(junk)
        assert OkxDex(key="k", secret="s", passphrase="p", rps=1, pace_path=p)._reserve(200.0) == 0
    assert OkxDex(key="k", secret="s", passphrase="p", pace_path=tmp_path / "нет" / "x.pace").pace_path is None
    assert OkxDex(key="k", secret="s", passphrase="p", pace_path=False).pace_path is None
    assert okxdex.PACE_PATH == tconfig.OKX_PACE_LOCK


def test_okxdex_request_goes_through_shared_pace(tmp_path):
    d = _okx(tmp_path)
    d.approve_tx("56", USDT, 1)
    assert float((tmp_path / "okxdex.pace").read_text()) > 0


def test_okxdex_pace_file_failure_falls_back_to_process_pace(tmp_path):
    p = tmp_path / "pace_dir"
    p.mkdir()                                      # каталог вместо файла — os.open упадёт
    d = OkxDex(key="k", secret="s", passphrase="p", rps=1, pace_path=p)
    assert d._reserve(50.0) == 0 and d.pace_path is None
    assert d._reserve(50.0) == pytest.approx(d.min_gap)


# ================================ подпись EIP-155 ================================
def test_eip155_text_vector_exact():
    """Пример из текста EIP-155 (открытый тестовый ключ 0x46…46): байт в байт."""
    a = Account.from_key(bytes.fromhex("46" * 32))
    s = a.sign_transaction({"nonce": 9, "gasPrice": 20 * 10 ** 9, "gas": 21000,
                            "to": "0x3535353535353535353535353535353535353535", "value": 10 ** 18, "data": b"",
                            "chainId": 1})
    raw = "0x" + bytes(s.raw_transaction).hex()
    assert raw == ("0xf86c098504a817c800825208943535353535353535353535353535353535353535880de0b6b3a76400008025a028ef61"
                   "340bd939bc2195fe537567866003e1a15d3c71ff63e1590620aa636276a067cbe9d8997f761aecb703304b3800ccf555c9"
                   "f3dc64214b297fb1966a3b6d83")
    assert (s.v, s.r, s.s) == (37, 18515461264373351373200002665853028612451056578545711640558177340181847433846,
                               46948507304638947509940763649030358759909902576025900602547168820602576006531)
    assert Account.recover_transaction(raw) == a.address


def test_wallet_signs_legacy_eip155_and_sender_recovers(env):
    data = tconfig.SEL_APPROVE + _w(SPENDER) + f"{5:064x}"
    h, rc = env.wallet.send_and_wait(USDT, data, 0, 60_000, 50_000_000, kind="approve", meta={"clip_id": 3})
    tx = env.chain.sent[0]
    assert tx["sender"] == env.w and tx["chainId"] == 56 and tx["v"] in (147, 148)      # EIP-155: v = 2·56+35/36
    assert (tx["nonce"], tx["gasPrice"], tx["gas"], tx["to"], tx["value"], tx["data"]) == (43, 50_000_000, 60_000, USDT, 0, data)
    assert h == tx["hash"] == "0x" + keccak(bytes.fromhex(tx["raw"][2:])).hex()
    row = _row(env, h)
    assert row["raw_tx"] == tx["raw"] and row["clip_id"] == 3 and row["kind"] == "approve" and row["nonce"] == 43
    assert keys.redact(f"tx {h}") == f"tx {h}"                  # хэш помечен публичным — в логе виден


# ================================ RPC ================================
def test_rpc_read_failover_sticky_and_unknown_is_not_zero(env):
    chain, rpc = env.chain, env.rpc
    chain.down = {"rpc-a.test"}
    assert rpc.nonce(env.w, "latest") == 43 and rpc.n_failover == 1
    chain.down = set()
    chain.calls.clear()
    rpc.gas_price()
    assert chain.calls == [("rpc-b.test", "eth_gasPrice")]         # липко: ответивший идёт первым
    chain.down = {"rpc-a.test", "rpc-b.test"}
    with pytest.raises(RpcUnavailable):
        rpc.native_balance(env.w)
    chain.down = set()
    with pytest.raises(RpcError):                                   # по адресу нет кода: "0x" — не ноль
        rpc.erc20_balance(OTHER, env.w)
    assert env.spot.balances(OTHER) == {"stable": 950 * E18, "token": None, "native": 10 ** 17}


def test_rpc_revert_is_an_answer_not_failover(env):
    env.chain.estimate_error = "execution reverted: TRANSFER_FROM_FAILED"
    env.chain.calls.clear()
    with pytest.raises(RpcError) as ei:
        env.rpc.estimate_gas({"from": env.w, "to": ROUTER, "data": "0x00"})
    assert ei.value.reverted and len(env.chain.calls) == 1


def test_send_raw_same_bytes_to_next_node_on_transport_error(env):
    raw = env.acct.sign_transaction({"nonce": 43, "gasPrice": 50_000_000, "gas": 21000, "to": env.acct.address,
                                     "value": 0, "data": b"", "chainId": 56}).raw_transaction
    raw = "0x" + bytes(raw).hex()
    env.chain.down = {("rpc-a.test", "eth_sendRawTransaction")}
    assert env.rpc.send_raw(raw)[0] == "ok"
    assert env.chain.methods("eth_sendRawTransaction") == [("rpc-a.test", "eth_sendRawTransaction"),
                                                           ("rpc-b.test", "eth_sendRawTransaction")]
    assert env.rpc.send_raw(raw)[0] == "known"
    assert evm.classify_send_error("insufficient funds for gas * price + value") == "rejected"
    assert evm.classify_send_error("replacement transaction underpriced") == "underpriced"
    assert evm.classify_send_error("nonce too low") == "nonce_used"


# ================================ кошелёк: запись-до, ворота, замок ================================
def test_signed_row_is_committed_before_broadcast(env):
    seen = []
    env.chain.before_send = lambda rec: seen.append(_row(env, rec["hash"]))
    data = tconfig.SEL_APPROVE + _w(SPENDER) + f"{9:064x}"
    h, rc = env.wallet.send_and_wait(USDT, data, 0, 60_000, 50_000_000, kind="approve")
    assert seen[0] is not None and seen[0]["state"] == "SIGNED" and seen[0]["raw_tx"] == env.chain.sent[0]["raw"]
    row = _row(env, h)
    assert row["state"] == "MINED_OK" and row["status"] == 1 and row["gas_used"] == 46_000
    assert row["eff_gas_price"] == "50000000" and row["sent_ts"] and row["resolved_ts"]


def test_journal_failure_means_nothing_is_sent(env):
    def boom(row):
        raise RuntimeError("БД недоступна")
    w = EvmWallet(env.rpc, 56, env.acct, boom, gate=lambda f: None, lock_dir=env.tmp, clock=env.clock,
                  sleep=env.clock.sleep)
    with pytest.raises(RuntimeError, match="БД"):
        w.send_and_wait(USDT, "0x", 0, 21000, 50_000_000)
    assert env.chain.sent == [] and env.chain.latest == 43


@pytest.mark.parametrize("mode,paused", [("dry", False), ("readonly", False), ("live", True)])
def test_gate_refuses_before_any_rpc_or_signature(env, mode, paused):
    env.gs.update(mode=mode, paused=paused)
    env.chain.calls.clear()
    with pytest.raises(keys.ModeForbidden):
        env.wallet.send_and_wait(USDT, "0x", 0, 21000, 50_000_000)
    assert env.chain.calls == [] and env.chain.sent == [] and store.dex_txs_unresolved(env.con) == []


def test_wallet_requires_key_and_gate(env):
    with pytest.raises(ValueError):
        EvmWallet(env.rpc, 56, None, lambda r: None, gate=lambda f: None)
    with pytest.raises(ValueError):
        EvmWallet(env.rpc, 56, env.acct, lambda r: None, gate=None)


def test_pending_tx_on_wallet_blocks_new_nonce(env):
    env.chain.pending_extra = 1
    with pytest.raises(NoncePending):
        env.wallet.send_and_wait(USDT, "0x", 0, 21000, 50_000_000)
    assert env.chain.sent == []


def test_second_writer_is_refused_by_flock(env):
    env.wallet.lock_wait_s = 0.1
    fd = os.open(env.wallet.lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(WalletBusy):
            env.wallet.send_and_wait(USDT, "0x", 0, 21000, 50_000_000)
    finally:
        os.close(fd)
    assert env.chain.sent == []
    assert env.wallet.lock_path.name == f"evm_{env.w}.lock"


def test_wrong_chain_rpc_refused(env):
    env.chain.eth_chainId = lambda: hex(1)
    with pytest.raises(RuntimeError, match="сеть 1"):
        env.wallet.send_and_wait(USDT, "0x", 0, 21000, 50_000_000)
    assert env.chain.sent == []


# ================================ график зависшей ================================
def test_stuck_swap_bump_then_cancel_then_sentunknown_never_new_nonce(env):
    _allow(env)
    env.chain.mine_rule = lambda tx: False
    t0 = env.clock()
    with pytest.raises(SentUnknown) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    e = ei.value
    assert e.nonce == 43 and len(e.hashes) == 3
    sent = env.chain.sent
    assert [r["hash"] for r in sent] == e.hashes                     # 15 с: узел знал — повтора байтов не было
    assert {r["nonce"] for r in sent} == {43}                        # ни одной транзакции на другом nonce
    swap, bump, cancel = sent
    assert [_row(env, h)["kind"] for h in e.hashes] == ["swap", "bump", "cancel"]
    assert (bump["to"], bump["data"], bump["gas"], bump["value"]) == (swap["to"], swap["data"], swap["gas"], 0)
    assert bump["gasPrice"] == 60_000_000 and cancel["gasPrice"] == 72_000_000     # ×1.2 и ещё ×1.2
    assert (cancel["to"], cancel["value"], cancel["gas"], cancel["data"]) == (env.w, 0, tconfig.CANCEL_GAS, "0x")
    assert bump["t"] - t0 == pytest.approx(30, abs=1.01) and cancel["t"] - t0 == pytest.approx(90, abs=1.01)
    assert all(_row(env, h)["state"] == "UNKNOWN" for h in e.hashes)
    assert _row(env, e.hashes[0])["min_receive"] == _row(env, e.hashes[1])["min_receive"]
    assert _row(env, e.hashes[2])["min_receive"] is None
    n_quotes = len([c for c in env.okx.calls if c[0] == "swap"])
    with pytest.raises(NoncePending):                                 # новый своп, пока старый висит, — отказ
        env.spot.swap(USDT, AIW3, 100 * E18, "6")
    assert {r["nonce"] for r in env.chain.sent} == {43} and len(env.chain.sent) == 3
    assert len([c for c in env.okx.calls if c[0] == "swap"]) == n_quotes + 1   # сборка была, подписи — нет


def test_stop_mid_flight_still_settles_but_readonly_does_not(env):
    _allow(env)
    env.chain.mine_rule = lambda tx: False
    env.chain.before_send = lambda rec: env.gs.update(paused=True)   # «стоп» сразу после первой отправки
    with pytest.raises(SentUnknown) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert len(ei.value.hashes) == 3                                  # bump и cancel — доводят уже отправленное

    env.chain.pool.clear()                  # прежний nonce «пропал»; другая сумма — другие байты и хэш
    env.chain.sent.clear()
    env.gs.update(paused=False)
    env.chain.before_send = lambda rec: env.gs.update(mode="readonly")   # режим понижен посреди
    with pytest.raises(SentUnknown) as ei:
        env.spot.swap(USDT, AIW3, 50 * E18, "7")
    assert len(ei.value.hashes) == 1 and len(env.chain.sent) == 1


def test_dropped_tx_rebroadcast_with_identical_bytes(env):
    _allow(env)
    env.chain.drop_next = 1
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    sent = env.chain.sent
    assert len(sent) == 2 and sent[0]["raw"] == sent[1]["raw"]         # те же байты, тот же хэш
    assert sent[1]["t"] - sent[0]["t"] == pytest.approx(15, abs=1.01)
    assert res.status == "ok" and res.tx_hash == sent[0]["hash"] and res.hashes == (sent[0]["hash"],)
    assert _row(env, res.tx_hash)["state"] == "MINED_OK"


def test_bump_mined_original_replaced_and_result_from_bump(env):
    _allow(env)
    env.chain.mine_rule = lambda tx: tx["gasPrice"] > 50_000_000
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    swap, bump = env.chain.sent
    assert res.tx_hash == bump["hash"] and res.status == "ok" and res.amount_out == 100 * E18 * RATE
    assert _row(env, swap["hash"])["state"] == "REPLACED" and _row(env, bump["hash"])["state"] == "MINED_OK"
    assert res.gas_wei == 250_000 * 60_000_000


def test_nonce_taken_by_foreign_tx_is_unknown_without_cancel(env):
    _allow(env)
    env.chain.mine_rule = lambda tx: False
    env.chain.foreign_at = env.clock() + 5
    with pytest.raises(SentUnknown, match="nonce занят"):
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert len(env.chain.sent) == 1                                  # ни bump, ни cancel — nonce уже не наш


def test_first_send_rejected_is_not_unknown(env):
    _allow(env)
    env.chain.send_error = "insufficient funds for gas * price + value"
    with pytest.raises(TxRejected) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert _row(env, ei.value.tx_hash)["state"] == "DROPPED"
    assert "insufficient funds" in _row(env, ei.value.tx_hash)["err"]


def test_failure_after_send_becomes_sentunknown(env):
    _allow(env)
    env.chain.malformed = True
    with pytest.raises(SentUnknown, match="сбой после отправки"):
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert _row(env, env.chain.sent[0]["hash"])["state"] == "UNKNOWN"


# ================================ гарды /swap ================================
def _set(path, value):
    def f(resp):
        d = resp
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = value(resp) if callable(value) else value
    return f


TAMPERS = [
    ("receiver", _set(("tx", "swapReceiverAddress"), OTHER)),
    ("receiver", _set(("routerResult", "receiver"), OTHER)),
    ("from", _set(("tx", "from"), OTHER)),
    ("value", _set(("tx", "value"), "1")),
    ("router", _set(("tx", "to"), NEW_ROUTER)),
    ("token_in", _set(("routerResult", "fromToken", "tokenContractAddress"), OTHER)),
    ("token_out", _set(("routerResult", "toToken", "tokenContractAddress"), OTHER)),
    ("amount", _set(("routerResult", "fromTokenAmount"), lambda r: str(int(r["routerResult"]["fromTokenAmount"]) + 1))),
    ("honeypot", _set(("routerResult", "toToken", "isHoneyPot"), True)),
    ("honeypot", _set(("routerResult", "fromToken", "isHoneyPot"), "true")),
    ("tax", _set(("routerResult", "toToken", "taxRate"), "0.05")),
    ("signature_data", _set(("tx", "signatureData"), ['{"approveContract":"0x1","approveTxCalldata":"0x095ea7b3"}'])),
    ("min_receive", _set(("tx", "minReceiveAmount"), lambda r: str(int(r["routerResult"]["toTokenAmount"]) * 96 // 100))),
    ("min_receive", _set(("tx", "minReceiveAmount"), "0")),
    ("slippage", _set(("tx", "slippagePercent"), "50")),
    ("impact", _set(("routerResult", "priceImpactPercent"), "-7.5")),
    ("gas", _set(("tx", "gas"), "0")),
    ("shape", _set(("tx", "data"), "not-hex")),
    ("shape", lambda r: r.pop("tx")),
    # calldata против JSON: JSON честный, подменена только подписываемая calldata
    ("selector", lambda r: _cd(sel="0x12345678")(r)),
    ("calldata", _set(("tx", "data"), tconfig.SEL_DAG_SWAP + "00" * 40)),
    ("calldata_token_in", lambda r: _cd(token_in=OTHER)(r)),
    ("calldata_token_in", lambda r: _cd(from_hi=1)(r)),
    ("calldata_token_out", lambda r: _cd(token_out=OTHER)(r)),
    ("calldata_amount", lambda r: _cd(amount=lambda k: k["amount"] + 1)(r)),
    ("calldata_min_return", lambda r: _cd(min_ret=0)(r)),
    ("calldata_tail", lambda r: _cd(trim_to=OTHER)(r)),
    ("calldata_tail", lambda r: _cd(trim_rate=3000)(r)),
    ("calldata_tail", lambda r: _cd(expect=lambda k: k["min_ret"])(r)),
    ("calldata_tail", lambda r: _cd(extra=bytes.fromhex("3ca20afc2aaa") + bytes(26))(r)),   # комиссия дописана
]


def _cd(**over):
    """Пересобрать tx.data из самого ответа с подменой полей calldata (JSON не трогается)."""
    def f(resp):
        rr, tx = resp["routerResult"], resp["tx"]
        kw = dict(token_in=rr["fromToken"]["tokenContractAddress"], token_out=rr["toToken"]["tokenContractAddress"],
                  amount=int(rr["fromTokenAmount"]), min_ret=int(tx["minReceiveAmount"]),
                  quoted=int(rr["toTokenAmount"]))
        kw.update({k: (v(kw) if callable(v) else v) for k, v in over.items()})
        tx["data"] = dag_calldata(**kw)
    return f


def _real(side):
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "data" / "okx_swap_live_20260912.json").read_text())[side]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_real_okx_swap_responses_pass_and_a_flipped_byte_is_refused(side):
    """Живые ответы /swap 12.09 (AIW3, покупка на $5 и продажа 100 AIW3, 3 %): разбор calldata и trim — по настоящей
    кодировке OKX, а не только по подделке."""
    resp = _real(side)
    rr = resp["routerResult"]
    kw = dict(chain="bsc", wallet=resp["tx"]["from"], token_in=rr["fromToken"]["tokenContractAddress"],
              token_out=rr["toToken"]["tokenContractAddress"], amount=int(rr["fromTokenAmount"]),
              slippage_pct=Decimal(3), impact_cap_pct=Decimal(5), allow_tax=False)
    b = check_swap(resp, **kw)
    assert b.min_receive == int(resp["tx"]["minReceiveAmount"]) and b.to == ROUTER
    data = resp["tx"]["data"]
    resp["tx"]["data"] = data[:-2] + ("00" if data[-2:] != "00" else "01")   # последний байт адреса trim
    with pytest.raises(GuardError) as ei:
        check_swap(resp, **kw)
    assert ei.value.guard == "calldata_tail"


def test_calldata_without_trim_tail_passes(env):
    resp = env.okx.swap("56", USDT, AIW3, 100 * E18, Decimal(3), Decimal(5), env.acct.address)
    _cd(trim_to=None)(resp)
    b = check_swap(resp, chain="bsc", wallet=env.acct.address, token_in=USDT, token_out=AIW3, amount=100 * E18,
                   slippage_pct=Decimal(3), impact_cap_pct=Decimal(5), allow_tax=False)
    assert b.data == resp["tx"]["data"]


@pytest.mark.parametrize("guard,tamper", TAMPERS, ids=[f"{g}-{i}" for i, (g, _) in enumerate(TAMPERS)])
def test_each_swap_guard_refuses_its_tampered_payload(env, guard, tamper):
    _allow(env)
    env.okx.tamper = tamper
    with pytest.raises(GuardError) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert ei.value.guard == guard
    assert env.chain.sent == [] and env.chain.methods("eth_estimateGas") == []   # отказ до симуляции и подписи
    assert store.dex_txs_unresolved(env.con) == []


def test_untampered_payload_passes_and_min_receive_boundary(env):
    resp = env.okx.swap("56", USDT, AIW3, 100 * E18, Decimal(3), Decimal(5), env.acct.address)
    kw = dict(chain="bsc", wallet=env.acct.address, token_in=USDT, token_out=AIW3, amount=100 * E18,
              slippage_pct=Decimal(3), impact_cap_pct=Decimal(5), allow_tax=False)
    b = check_swap(resp, **kw)
    assert b.min_receive == evm_swap.min_receive_floor(100 * E18 * RATE, Decimal(3)) and b.to == ROUTER
    resp["tx"]["minReceiveAmount"] = str(b.min_receive - 1)
    with pytest.raises(GuardError, match="min_receive"):
        check_swap(resp, **kw)
    resp["tx"]["minReceiveAmount"] = str(b.min_receive)
    resp["tx"]["signatureData"] = None
    resp["tx"]["swapReceiverAddress"] = env.acct.address.upper().replace("0X", "0x")   # наш адрес — можно
    assert check_swap(resp, **kw).min_receive == b.min_receive


def test_tax_token_allowed_only_by_owner(env):
    _allow(env)
    env.okx.tamper = _set(("routerResult", "toToken", "taxRate"), "0.05")
    env.spot._cfg = lambda c=_cfg(env.tmp, env.acct.address, allow_tax_tokens="true"): c
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert res.status == "ok"


def test_preflight_revert_refuses(env):
    _allow(env)
    env.chain.estimate_error = "execution reverted: Min return not reached"
    with pytest.raises(GuardError) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert ei.value.guard == "preflight" and env.chain.sent == []


def test_gas_must_be_payable_plus_reserve(env):
    _allow(env)
    env.chain.native[env.w] = 10 ** 15 + 390_000 * 50_000_000 - 1             # на волос меньше газа + резерва
    with pytest.raises(GuardError) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert ei.value.guard == "native" and env.chain.sent == []


@pytest.mark.parametrize("over,err", [
    (dict(broadcast='"okx_mev"'), "broadcast"),
    (dict(broadcast=None), owner.OwnerMissing),
    (dict(impact_cap_pct=None), owner.OwnerMissing),
])
def test_owner_values_gate_live_swap(env, over, err):
    _allow(env)
    env.spot._cfg = lambda c=_cfg(env.tmp, env.acct.address, **over): c
    if isinstance(err, str):
        with pytest.raises(GuardError) as ei:
            env.spot.swap(USDT, AIW3, 100 * E18, "5")
        assert ei.value.guard == err
    else:
        with pytest.raises(err):
            env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert env.chain.sent == []


def test_clip_slippage_wider_than_cap_refused_twice(env):
    """Первый замок — загрузчик owner.toml; второй — сама нога (копия сделки в обход загрузчика)."""
    with pytest.raises(owner.OwnerConfigError):
        _cfg(env.tmp, env.acct.address, clip_slippage_pct="5")
    vals = dict(env.cfg.values)
    vals["dex.clip_slippage_pct"] = Decimal(5)
    bad = owner.OwnerCfg(values=MappingProxyType(vals), path="x", sha256=None, loaded=0.0)
    env.spot._cfg = lambda: bad
    _allow(env)
    with pytest.raises(GuardError) as ei:
        env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert ei.value.guard == "slippage" and env.chain.sent == [] and env.okx.calls == []


def test_clip_slippage_below_cap_is_sent(env):
    _allow(env)
    env.spot._cfg = lambda c=_cfg(env.tmp, env.acct.address, clip_slippage_pct="0.5"): c
    env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert env.okx.calls[-1][1]["slip"] == Decimal("0.5") and env.okx.calls[-1][1]["impact"] == Decimal(5)


# ================================ approve ================================
@pytest.mark.parametrize("guard,tamper", [
    ("spender", _set(("dexContractAddress",), NEW_ROUTER)),
    ("approve_data", _set(("data",), "0x095ea7b3" + _w(OTHER) + f"{100 * E18:064x}")),
    ("approve_data", _set(("data",), "0x095ea7b3" + _w(SPENDER) + f"{2 ** 256 - 1:064x}")),
    ("approve_data", _set(("data",), "0xa9059cbb" + _w(SPENDER) + f"{100 * E18:064x}")),   # transfer вместо approve
    ("approve_data", _set(("data",), "0x095ea7b3" + _w(SPENDER) + f"{100 * E18:064x}" + "00" * 32)),
    ("gas", _set(("gasLimit",), "0")),
])
def test_approve_guards(env, guard, tamper):
    env.okx.tamper_approve = tamper
    with pytest.raises(GuardError) as ei:
        env.spot.ensure_allowance(USDT, 100 * E18, "5")
    assert ei.value.guard == guard and env.chain.sent == []


def test_ensure_allowance_exact_goes_to_token_contract(env):
    env.chain.gas_price = 60_000_000
    res = env.spot.ensure_allowance(USDT, 100 * E18, "5")
    tx = env.chain.sent[0]
    assert tx["to"] == USDT                                     # to = контракт токена, НЕ dexContractAddress
    assert tx["data"] == "0x095ea7b3" + _w(SPENDER) + f"{100 * E18:064x}"
    assert tx["gas"] == 75_000 and tx["gasPrice"] == 60_000_000   # max(50000·1.5, 46000·1.3); max(API, сеть)
    assert res.status == "ok" and res.kind == "approve" and res.amount_in == 0 and res.gas_wei == 46_000 * 60_000_000
    assert env.chain.allow[(USDT, env.w, SPENDER)] == 100 * E18
    assert _row(env, res.tx_hash)["kind"] == "approve" and _row(env, res.tx_hash)["clip_id"] == 5
    assert env.spot.ensure_allowance(USDT, 100 * E18, "6") is None and len(env.chain.sent) == 1


def test_ensure_allowance_unlimited_policy(env):
    env.spot._cfg = lambda c=_cfg(env.tmp, env.acct.address, approve_policy='"unlimited"'): c
    env.spot.ensure_allowance(AIW3, 7, "x")
    assert env.okx.calls[-1][1]["amount"] == 2 ** 256 - 1
    assert env.chain.allow[(AIW3, env.w, SPENDER)] == 2 ** 256 - 1


def test_approve_policy_empty_refuses(env):
    env.spot._cfg = lambda c=_cfg(env.tmp, env.acct.address, approve_policy=None): c
    with pytest.raises(owner.OwnerMissing):
        env.spot.ensure_allowance(USDT, 1, "x")


# ================================ чек: суммы, газ, сверка ================================
def test_receipt_math_refund_gas_and_balance_check(env):
    _allow(env)
    env.chain.gas_price = 70_000_000
    env.chain.refund = 1 * E18
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    tx = env.chain.sent[0]
    assert tx["gas"] == 390_000 and tx["gasPrice"] == 70_000_000 and tx["to"] == ROUTER and tx["value"] == 0
    assert res.status == "ok" and res.tx_state == "MINED_OK" and res.balance_check == "ok"
    assert res.amount_in == 99 * E18                            # ушло 100, роутер вернул 1
    assert res.amount_out == 99 * E18 * RATE                    # чужой перевод и ERC-721 не считаются
    assert res.gas_wei == 250_000 * 70_000_000 and res.gas_used == 250_000
    assert res.gas_usd == pytest.approx(250_000 * 70_000_000 / 1e18 * 600)
    assert res.nonce == 43 and res.block == 101 and res.quoted_out == 100 * E18 * RATE
    row = _row(env, res.tx_hash)
    assert (row["amount_in"], row["amount_out"], row["state"]) == (str(99 * E18), str(99 * E18 * RATE), "MINED_OK")
    assert row["min_receive"] == str(res.min_receive)


def test_balance_check_na_when_node_has_no_state(env):
    _allow(env)
    env.chain.no_state_below = 10 ** 9
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert res.status == "ok" and res.balance_check == "n/a"


def test_balance_mismatch_marks_unknown(env):
    _allow(env)
    env.chain.silent_credit = 5
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert res.status == "unknown" and res.balance_check == "mismatch" and "balanceOf" in res.note
    assert "balanceOf" in _row(env, res.tx_hash)["err"]


def test_output_to_someone_else_is_unknown(env):
    _allow(env)
    env.chain.steal_to = OTHER
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert res.status == "unknown" and res.amount_out == 0 and "minReceive" in res.note


def test_reverted_swap_pays_gas_moves_nothing(env):
    _allow(env)
    env.chain.revert_next = True
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    assert res.status == "reverted" and res.amount_in == res.amount_out == 0 and res.gas_wei == 120_000 * 50_000_000
    assert _row(env, res.tx_hash)["state"] == "MINED_REVERTED"
    assert env.chain.bal[(USDT, env.w)] == 950 * E18


def test_transfer_flows_ignores_noise():
    me = "0x" + "ab" * 20
    rc = {"logs": [_log(USDT, me, ROUTER, 10), _log(USDT, ROUTER, me, 3), _log(AIW3, POOL, me, 50),
                   _log(AIW3, POOL, OTHER, 99), {"address": AIW3, "topics": [APPROVAL, "0x" + _w(me), "0x" + _w(me)],
                                                 "data": "0x01"}]}
    assert evm_swap.transfer_flows(rc, me) == {USDT: [3, 10], AIW3: [50, 0]}


# ================================ сверка после рестарта ================================
def test_resolve_mined_row_infers_tokens_from_logs(env):
    _allow(env)
    res = env.spot.swap(USDT, AIW3, 100 * E18, "5")
    row = _row(env, res.tx_hash)
    ro = OkxEvmSpot(env.okx, env.rpc, env.cfg)                  # без отправителя: сверка только читает
    n = len(env.chain.sent)
    r2 = ro.resolve(row)
    assert (r2.status, r2.tx_state, r2.amount_in, r2.amount_out) == ("ok", "MINED_OK", res.amount_in, res.amount_out)
    assert r2.nonce == 43 and len(env.chain.sent) == n


def test_resolve_unmined_rows(env):
    ro = OkxEvmSpot(env.okx, env.rpc, env.cfg)
    free = {"tx_hash": "0x" + "11" * 32, "nonce": 43, "kind": "swap"}
    assert ro.resolve(free).tx_state == "DROPPED"
    env.chain.known.add(free["tx_hash"])
    assert ro.resolve(free).tx_state == "SENT"
    assert ro.resolve({"tx_hash": "0x" + "22" * 32, "nonce": 40, "kind": "bump"}).tx_state == "REPLACED"
    env.chain.down = {"rpc-a.test", "rpc-b.test"}
    r = ro.resolve(free)
    assert r.tx_state == "UNKNOWN" and r.status == "unknown"


# ================================ сухой режим и протокол ================================
def test_dry_leg_runs_guards_without_preflight_and_never_sends(env):
    cfg = _cfg(env.tmp, env.acct.address, impact_cap_pct=None, broadcast=None)
    dry = OkxEvmSpot(env.okx, env.rpc, cfg)
    b = dry.build_swap(USDT, AIW3, 100 * E18)                       # allowance 0 — симуляции нет
    assert b.preflight == "skipped" and env.chain.methods("eth_estimateGas") == []
    assert b.gas_limit == 375_000 and env.okx.calls[-1][1]["impact"] is None
    env.okx.tamper = _set(("tx", "to"), NEW_ROUTER)
    with pytest.raises(GuardError, match="router"):
        dry.build_swap(USDT, AIW3, 100 * E18)
    env.okx.tamper = None
    with pytest.raises(GuardError, match="mode"):
        dry.swap(USDT, AIW3, 100 * E18, "5")
    with pytest.raises(GuardError, match="mode"):
        dry.ensure_allowance(USDT, 100 * E18, "5")
    _allow(env)
    assert dry.build_swap(USDT, AIW3, 100 * E18).preflight == "ok"   # allowance есть — симуляция гоняется
    assert env.chain.sent == []


def test_spotleg_protocol_quote_balances_pool_price(env):
    assert isinstance(env.spot, SpotLeg) and env.spot.chain == "bsc" and env.spot.wallet == env.acct.address
    q = env.spot.quote(USDT, AIW3, 15 * E18)
    assert (q.amount_in, q.amount_out, q.dec_in, q.dec_out, q.honeypot) == (15 * E18, 15 * E18 * RATE, 18, 18, False)
    assert env.spot.balances(AIW3) == {"stable": 950 * E18, "token": 0, "native": 10 ** 17}
    assert env.spot.pool_price(AIW3) == Decimal(1) / Decimal(RATE)
    with pytest.raises(ValueError):
        OkxEvmSpot(env.okx, env.rpc, env.cfg, wallet=OTHER, sender=env.wallet)   # кошелёк ≠ отправитель


def test_bump_price_rounding():
    assert evm.bump_price(50_000_000) == 60_000_000
    assert evm.bump_price(1) == 2 and evm.bump_price(7) == 9
