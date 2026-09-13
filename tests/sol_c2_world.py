"""Мир сквозных тестов связки SOL × HL (шаг C2): НАСТОЯЩИЕ движок (Desk/Engine → sol_flow), SolanaExecutor (журнал
попыток, подпись проверенных байтов, резолвер по двум RPC, разбор finalized-чека), HyperliquidTrade (nonce/cloid в
журнале, settle_unknown), SpotRouter — поверх фейков сети:
  - Solana: два RPC-узла над общей «цепью» (подтверждение → finalized через 13 с, высота растёт 1 блок / 0.4 с,
    чеки строятся из РЕАЛЬНЫХ подписанных байтов), сценарии отправки (сел / потерян ответ / не сел / упал процесс);
  - HL: /info и /exchange (hl_support.FakeHL) с «биржей»: IOC исполняется по стакану не хуже кэпа, позиция, fills и
    orderStatus по cloid; сценарии (частично / таймаут после исполнения / потеряна до биржи / отказ / смерть процесса);
  - провайдеры маршрутов — кандидаты с настоящими v0-сообщениями solders (Jupiter Build, OKX; Order — только показ).
Часы — поддельные (sleep двигает время). Сети, ключей и .env нет; ключ HL — публичный тестовый ключ SDK."""
import base64, hashlib, json, re, struct
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import requests
from funding_bot.trade import instruments as I, owner, store
from funding_bot.trade.engine import Conns, Desk, Engine, Hooks
from funding_bot.trade.fees import NATIVE_SOL, PriceObs, network_components
from funding_bot.trade.hyperliquid_trade import HlJournal, HlSigner, HyperliquidTrade
from funding_bot.trade.owner import SOL_HL
from funding_bot.trade.runtime import RuntimeRegistry, SolLegs, _routing, hl_account_id
from funding_bot.trade.sim import SimHlPerp, SimSolSpot
from funding_bot.trade.sol_exec import SolanaExecutor
from funding_bot.trade.solana import (ANSEM_MINT, MAINNET_GENESIS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT, b58,
                                      wire)
from funding_bot.trade.solana.accounts import ata
from funding_bot.trade.solana.decoders import JUPITER_PROGRAM, OKX_ROUTER
from funding_bot.trade.solana.message import SoldersMessageBuilder
from funding_bot.trade.solana.rpc import RpcUnavailable, SendUnknown
from funding_bot.trade.solana.validate import Validation
from funding_bot.trade.spot_router import Payload, SpotRouter, SwapCandidate, Unavailable
from funding_bot.tg.parse import ProfileEntry
import hl_support as S
import sol_hl_fixtures as F
from solana_helpers import PcdSigner, RFC_SEED

ROOT = Path(__file__).resolve().parents[1]
WALLET = F.SOL_ADDR
USDC_ATA = ata(WALLET, USDC_MINT, TOKEN_PROGRAM)
ANSEM_ATA = ata(WALLET, ANSEM_MINT, TOKEN_2022_PROGRAM)
ACCOUNT = S.MASTER
FULL = "para:ANSEM"
ACCOUNT_ID = hl_account_id("mainnet", ACCOUNT, ACCOUNT, "para")
FEE = D("0.000675")                       # 0.045 % × 1.5 (HIP-3 para) — синтетика теста
RAW = re.compile(r"\d\.\d{10,}")              # сырой Decimal в тексте владельцу
TOML = F.sol_toml({"limits.sol_best_hyperliquid": {"max_clip_usdc": '"150"', "max_operation_usdc": '"150"',
                                                   "max_total_position_usdc": '"150"'}})


class Crash(BaseException):
    """Смерть процесса посреди шага: исполнитель ловит только Exception — БД остаётся такой, как в момент смерти."""


def registry_text(**over) -> str:
    doc = json.loads((ROOT / "deploy" / "instruments.json.example").read_text())
    rec = doc["instruments"][0]
    rec["enabled_for_live"] = True
    rec["perp"]["account_id"] = ACCOUNT_ID
    for k, v in over.items():
        sec, _, key = k.partition(".")
        rec[sec][key] = v
    return json.dumps(doc)


# --- Hyperliquid: «биржа» ----------------------------------------------------------------------------------------
class Venue:
    def __init__(self, clock):
        self.clock = clock
        self.bids = [[D("0.16675"), D(1062)], [D("0.16655"), D(1799)], [D("0.1664"), D(5000)]]
        self.asks = [[D("0.1672"), D(1500)], [D("0.1674"), D(3000)]]
        self.pos = D(0)
        self.lev = 3
        self.fills: list[dict] = []
        self.orders: dict[str, dict] = {}
        self.script: list = []            # поведение следующих заявок: "fill" | ("partial", q) | "timeout" | "lost"
        self.oid, self.tid = 900_000, 5_000_000   # | ("error", текст) | "crash"
        self.withdrawable = "194.57"
        self.status_fail = False
        self.calls: list[tuple] = []      # (side, sz, px, reduce_only, поведение, время)
        self.abstraction = "disabled"
        self.funding: list[dict] = []     # userFunding счёта (C3: учёт фандинга)
        self.funding_fail = False
        self.agent_role = {"role": "agent", "data": {"user": ACCOUNT.lower()}}
        self.extra_agents: list[dict] = []

    def user_funding(self, body):
        if self.funding_fail:
            return S.Resp(500, {"error": "down"})
        s, e = body.get("startTime", 0), body.get("endTime", 10 ** 16)
        return [r for r in self.funding if s <= r["time"] <= e]

    def add_funding(self, t_ms: int, usdc: str, szi: str = "-903", rate: str = "0.0001") -> None:
        self.funding.append({"time": int(t_ms), "hash": "0x" + "0" * 64,
                             "delta": {"type": "funding", "coin": FULL, "usdc": usdc, "szi": szi, "fundingRate": rate,
                                       "nSamples": None}})

    def l2(self, body):
        lv = lambda rows: [{"px": str(p), "sz": str(q), "n": 1} for p, q in rows]      # noqa: E731
        return {"coin": FULL, "time": int(self.clock() * 1000), "levels": [lv(self.bids), lv(self.asks)]}

    def ch(self, body):
        return S.ch([(FULL, str(self.pos))] if self.pos != 0 else [], withdrawable=self.withdrawable)

    def order_status(self, body):
        if self.status_fail:
            return S.Resp(500, {"error": "down"})
        o = self.orders.get(body["oid"])
        if o is None:
            return S.UNKNOWN_OID
        st = "filled" if o["filled"] == o["orig"] else "canceled"
        return S.order_status(body["oid"], o["oid"], str(o["orig"]), str(o["orig"] - o["filled"]), st,
                              side="B" if o["buy"] else "A", px=str(o["px"]))

    def user_fills(self, body):
        if getattr(self, "fills_hidden", False):     # C3: fills ещё не видны в истории HL (оценка комиссии)
            return []
        s, e = body.get("startTime", 0), body.get("endTime", 10 ** 16)
        return [r for r in self.fills if s <= r["time"] <= e][:2000]

    def aad(self, body):
        return {"user": ACCOUNT.lower(), "coin": FULL,
                "leverage": {"type": "isolated", "value": self.lev, "rawUsd": "0"},
                "maxTradeSzs": ["1000000", "1000000"], "availableToTrade": ["194", "194"], "markPx": "0.1668"}

    def exchange(self, payload):
        a = payload["action"]
        if a["type"] == "updateLeverage":
            self.lev = a["leverage"]
            return S.Resp(200, S.OK_DEFAULT)
        assert a["type"] == "order", a
        o = a["orders"][0]
        buy, px, sz, ro, cloid = o["b"], D(o["p"]), D(o["s"]), o["r"], o["c"]
        beh = self.script.pop(0) if self.script else "fill"
        self.calls.append(("BUY" if buy else "SELL", sz, px, ro, beh, self.clock()))
        if beh == "lost":
            raise requests.ConnectionError("потеряна до биржи")
        if isinstance(beh, tuple) and beh[0] == "error":
            return S.Resp(200, S.ok_error(beh[1]))
        limit = beh[1] if isinstance(beh, tuple) and beh[0] == "partial" else sz
        if ro:
            limit = min(limit, -self.pos if buy else self.pos)
        got, notional = D(0), D(0)
        for p, q in (self.asks if buy else self.bids):
            if (p > px) if buy else (p < px):
                break
            take = min(q, limit - got)
            if take <= 0:
                break
            got += take
            notional += take * p
        self.oid += 1
        avg = (notional / got).quantize(D("0.00000001")) if got else D(0)
        self.orders[cloid] = {"oid": self.oid, "orig": sz, "filled": got, "px": px, "buy": buy}
        if got > 0:
            self.pos += got if buy else -got
            self.tid += 1
            self.fills.append(S.fill(self.oid, str(got), str(avg), int(self.clock() * 1000), self.tid,
                                     fee=str((got * avg * FEE).quantize(D("0.000001"))), cloid=cloid,
                                     side="B" if buy else "A", hash_="0x" + f"{self.tid:064x}"))
        if beh == "timeout":
            raise requests.Timeout("ответа нет")
        if beh == "crash":
            raise Crash()
        if got == 0:
            return S.Resp(200, S.ok_error(S.IOC_NO_MATCH))
        return S.Resp(200, S.ok_filled(str(got), str(avg), self.oid, cloid))


class HL(S.FakeHL):
    def __init__(self, clock, venue: Venue):
        super().__init__(clock)
        self.venue = venue
        self.market["l2Book"] = venue.l2
        self.market["allMids"] = {"SOL": "150.0"}
        self.user(ACCOUNT, "userAbstraction", lambda b: venue.abstraction)
        self.user(ACCOUNT, "clearinghouseState", venue.ch)
        self.user(ACCOUNT, "orderStatus", venue.order_status)
        self.user(ACCOUNT, "userFillsByTime", venue.user_fills)
        self.user(ACCOUNT, "userFunding", venue.user_funding)
        self.user(ACCOUNT, "activeAssetData", venue.aad)
        self.user(S.AGENT, "userRole", lambda b: venue.agent_role)        # агент подписанта одобрен мастером
        self.user(ACCOUNT, "extraAgents", lambda b: venue.extra_agents)
        self.user(ACCOUNT, "userFees", {"userCrossRate": "0.00045", "userAddRate": "0.00015",
                                        "activeReferralDiscount": "0.0"})
        self.user(ACCOUNT, "spotClearinghouseState", {"balances": [{"coin": "USDC", "token": 0, "total": "300.0",
                                                                    "hold": "0.0"}]})

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        if url.endswith("/exchange"):
            payload = json.loads(data)
            self.calls.append(("/exchange", payload))
            return self.venue.exchange(payload)
        return super().post(url, data=data, headers=headers, timeout=timeout, **kw)


# --- Solana: цепь и узлы ----------------------------------------------------------------------------------------
class SolWorld:
    FINALITY_S = 13.0

    def __init__(self, clock):
        self.clock = clock
        self.t0 = clock()
        self.h0, self.slot0 = 300_000_000, 400_000_000
        self.usdc, self.ansem, self.lamports = 500_000_000, 0, 750_000_000
        self.txs: dict[str, dict] = {}
        self.script: list = []             # отправки: dict(beh=land|noresp|drop|crash_before|crash_after|err|late)
        self.sends: list[bytes] = []
        self.bh_n = 0
        self.lvbh: dict[str, int] = {}
        self.slot_of: dict[str, int] = {}
        self.outs = {"entry": {"jupiter_build_v2": 903_000_000, "okx_solana_v6": 902_500_000},
                     "exit": {"jupiter_build_v2": 150_200_000, "okx_solana_v6": 150_000_000}}
        self.down: set[str] = set()
        self.on_land = None
        self.okx_down = False
        self.blackhole = False

    def height(self) -> int:
        return self.h0 + int((self.clock() - self.t0) / 0.4)

    def slot(self) -> int:
        return self.slot0 + int((self.clock() - self.t0) / 0.4)

    def latest_blockhash(self):
        self.bh_n += 1
        bh = b58.b58encode(hashlib.sha256(f"bh-{self.bh_n}".encode()).digest())
        self.lvbh[bh] = self.height() + 150
        self.slot_of[bh] = self.slot()
        return bh, self.lvbh[bh]

    # --- отправка и приземление ---
    def send(self, b64: str, sig: str):
        raw = base64.b64decode(b64)
        self.sends.append(raw)
        if self.blackhole:                  # узел не отвечает и транзакция никуда не доходит
            raise SendUnknown(sig, "a", "нет ответа")
        sc = self.script.pop(0) if self.script else {"beh": "land"}
        beh = sc.get("beh", "land")
        if beh == "crash_before":
            raise Crash()
        if beh == "drop":
            raise SendUnknown(sig, "a", "нет ответа")
        if beh in ("land", "noresp", "err", "crash_after", "late"):
            self.land(raw, sig, sc, at=self.clock() + float(sc.get("delay", 0)))
        if beh == "noresp":
            raise SendUnknown(sig, "a", "нет ответа")
        if beh == "crash_after":
            raise Crash()
        return sig

    def land(self, raw: bytes, sig: str, sc: dict, at: float) -> None:
        if sig in self.txs:
            return                          # те же байты второй раз — та же транзакция (не второе исполнение)
        wtx = wire.parse_transaction(raw)
        data = wtx.message.instructions[-1].data
        side = "entry" if data[:1] == b"E" else "exit"
        amount, expected = struct.unpack_from("<QQ", data, 1)
        err = {"InstructionError": [0, {"Custom": 6001}]} if sc.get("beh") == "err" else None
        spent = 0 if err else amount - int(sc.get("refund", 0))
        out = 0 if err else int(sc.get("out", expected))
        fee = 5020
        t = {"sig": sig, "raw": raw, "land_t": at, "slot": self.slot() + 1, "err": err, "fee": fee, "side": side,
             "lam_pre": self.lamports, "usdc_pre": self.usdc, "ansem_pre": self.ansem}
        self.lamports -= fee
        if not err:
            if side == "entry":
                self.usdc -= spent
                self.ansem += out
            else:
                self.ansem -= spent
                self.usdc += out
        t.update(usdc_post=self.usdc, ansem_post=self.ansem, lam_post=self.lamports)
        self.txs[sig] = t
        if self.on_land is not None:
            self.on_land(t)

    def status(self, sig: str):
        t = self.txs.get(sig)
        if t is None or self.clock() < t["land_t"]:
            return None
        fin = self.clock() >= t["land_t"] + self.FINALITY_S
        return {"slot": t["slot"], "confirmations": None if fin else 1, "err": t["err"],
                "confirmationStatus": "finalized" if fin else "confirmed"}

    def transaction(self, sig: str, commitment: str):
        st = self.status(sig)
        if st is None or (commitment == "finalized" and st["confirmationStatus"] != "finalized"):
            return None
        t = self.txs[sig]
        m = wire.parse_transaction(t["raw"]).message
        keys = list(m.static_keys)
        pre, post = [0] * len(keys), [0] * len(keys)
        pre[0], post[0] = t["lam_pre"], t["lam_post"]
        iu, ia = keys.index(USDC_ATA), keys.index(ANSEM_ATA)

        def tb(i, mint, prog, amt):
            return {"accountIndex": i, "mint": mint, "owner": WALLET, "programId": prog,
                    "uiTokenAmount": {"amount": str(amt), "decimals": 6}}
        return {"slot": t["slot"], "blockTime": int(t["land_t"]), "version": 0,
                "transaction": {"signatures": [sig], "message": {
                    "accountKeys": keys, "recentBlockhash": m.recent_blockhash, "instructions": [],
                    "header": {"numRequiredSignatures": 1, "numReadonlySignedAccounts": 0,
                               "numReadonlyUnsignedAccounts": m.num_readonly_unsigned}}},
                "meta": {"err": t["err"], "fee": t["fee"], "preBalances": pre, "postBalances": post,
                         "preTokenBalances": [tb(iu, USDC_MINT, TOKEN_PROGRAM, t["usdc_pre"]),
                                              tb(ia, ANSEM_MINT, TOKEN_2022_PROGRAM, t["ansem_pre"])],
                         "postTokenBalances": [tb(iu, USDC_MINT, TOKEN_PROGRAM, t["usdc_post"]),
                                               tb(ia, ANSEM_MINT, TOKEN_2022_PROGRAM, t["ansem_post"])],
                         "innerInstructions": [], "loadedAddresses": {"writable": [], "readonly": []},
                         "computeUnitsConsumed": 150_000}}


class FakeRpc:
    """Независимый узел Solana над общей цепью (для резолвера — свой ответ у каждого)."""

    def __init__(self, world: SolWorld, label: str):
        self.w, self.label = world, label

    def _up(self):
        if self.label in self.w.down:
            raise RpcUnavailable(f"{self.label}: нет ответа")

    def signature_statuses(self, sigs, *, search_history=True):
        self._up()
        return [self.w.status(s) for s in sigs], self.w.slot()

    def transaction(self, sig, *, commitment="confirmed", encoding="json"):
        self._up()
        return self.w.transaction(sig, commitment)

    def block_height(self, commitment="finalized"):
        self._up()
        return self.w.height()

    def minimum_ledger_slot(self):
        self._up()
        return self.w.slot0 - 1000

    def is_blockhash_valid(self, bh, *, commitment="processed"):
        self._up()
        return self.w.height() <= self.w.lvbh.get(bh, 0), self.w.slot()

    def send_transaction(self, b64, *, expected_signature, mode, **kw):
        self._up()
        return self.w.send(b64, expected_signature)


class FakeChain:
    def __init__(self, world: SolWorld):
        self.w = world

    def latest_blockhash(self):
        return self.w.latest_blockhash()

    def block_height(self):
        return self.w.height()

    def blockhash_slot(self, bh):
        return self.w.slot_of.get(bh)


class FakeValidator:
    """Мост проверки и симуляции (RouteTxValidator.results_for): для байтов кандидатов мира — «проверено»."""

    def __init__(self):
        self.refuse: set[str] = set()

    def results_for(self, mh):
        if mh in self.refuse:
            return None
        v = Validation(True, (), mh, "sol_tx_manifest_v1")
        return v, v


class WorldExecutor(SolanaExecutor):
    """Настоящий исполнитель; чтения кошелька — из мира (разбор token-счетов — отдельные тесты accounts)."""

    def __init__(self, world: SolWorld, **kw):
        super().__init__(**kw)
        self.w = world

    def token_balance(self, mint, program):
        return self.w.usdc if mint == USDC_MINT else self.w.ansem if mint == ANSEM_MINT else 0

    def native_balance(self):
        return self.w.lamports

    def account_rent(self, mint, program, exts=()):
        return 0

    def mint_value(self, mint):
        return {"owner": TOKEN_2022_PROGRAM, "data": {"parsed": {"type": "mint", "info": {
            "isInitialized": True, "decimals": 6,
            "extensions": [{"extension": "metadataPointer"}, {"extension": "tokenMetadata"}]}}}}


class Prov:
    """Провайдер маршрута: кандидат — финальная сборка (v0-сообщение со своим blockhash, проверено, симулировано)."""

    def __init__(self, world: SolWorld, clock, group: str, paths: tuple[str, ...]):
        self.w, self.clock, self.group, self.paths = world, clock, group, paths

    def candidates(self, req):
        out = []
        for path in self.paths:
            if path == "jupiter_order_v2":      # готовая транзакция внешнего подписанта — только показ
                out.append(Unavailable("jupiter", path, "external_signer", "Order — только показ", self.clock()))
                continue
            if self.group == "okx" and self.w.okx_down:
                out.append(Unavailable("okx", path, "no_credentials", "нет ключа", self.clock()))
                continue
            exp = self.w.outs[req.side][path]
            mo = -(-exp * (10000 - req.slippage_bps) // 10000)
            bh, lvbh = self.w.latest_blockhash()
            data = (b"E" if req.side == "entry" else b"X") + struct.pack("<QQ", req.amount_in_raw, exp) + path.encode()
            prog = JUPITER_PROGRAM if self.group == "jupiter" else OKX_ROUTER
            ix = (prog, [(WALLET, True, True), (req.input_account, False, True), (req.output_account, False, True)],
                  data)
            raw = SoldersMessageBuilder().build_v0(payer=WALLET, instructions=[ix], recent_blockhash=bh, alts=())
            base, prio = network_components(n_signatures=1, cu_limit=200_000, cu_price_micro=100, payer=req.wallet,
                                            source=path)
            t = self.clock()
            out.append(SwapCandidate(
                provider=self.group, path=path, adapter_version="test", request_hash=req.request_hash, side=req.side,
                input_mint=req.input.mint, output_mint=req.output.mint, input_program=req.input.program,
                output_program=req.output.program, input_decimals=req.input.decimals,
                output_decimals=req.output.decimals, amount_in_raw=req.amount_in_raw, expected_out_raw=exp,
                min_out_raw=mo, onchain_min_out_raw=mo, fees=(base, prio),
                payload=Payload("message", "base64", data=base64.b64encode(raw).decode()),
                required_signers=(req.wallet,), recent_blockhash=bh, last_valid_block_height=lvbh,
                message_hash=wire.message_hash(raw), cu_limit=200_000, cu_price_micro=100, price_impact_bps=D(10),
                route_fingerprint=path, received_at=t, received_mono=t, built_mono=t, simulation_ok=True,
                validated=True, response_hash="h-" + path))
        return out


class RecHooks(Hooks):
    def __init__(self):
        self.reports, self.progresses, self.requotes = [], [], []

    def report(self, html):
        self.reports.append(html)

    def progress(self, iid, html):
        self.progresses.append((iid, html))

    def requote(self, iid, reason):
        self.requotes.append((iid, reason))


def make_world(tmp_path, *, keys_mode: str | None = "live", toml: str | None = None, registry: str | None = None,
               db_path=None, legacy=None):
    clock = S.Clock()
    venue = Venue(clock)
    hl = HL(clock, venue)
    sw = SolWorld(clock)
    p = tmp_path / "sol_owner.toml"
    p.write_text(toml or TOML)
    loader = lambda: owner.load(p)               # noqa: E731
    cfg = loader()
    db = db_path or tmp_path / "trade.db"
    conns = Conns(db)
    from eth_account import Account
    jr = HlJournal(store.connect(db), now=clock)
    signer = HlSigner(Account.from_key(S.TEST_KEY), agent=S.AGENT, master=ACCOUNT, account=ACCOUNT)
    mode_state = lambda: ("live", store.is_paused(conns.get()))    # noqa: E731
    perp = HyperliquidTrade(ACCOUNT, fullcoin=FULL, master=ACCOUNT, signer=signer, journal=jr, session=hl, now=clock,
                            sleep=clock.sleep, mode_state=mode_state, weight_limit=1_000_000)
    eps = [FakeRpc(sw, "a"), FakeRpc(sw, "b")]
    chain = FakeChain(sw)
    validator = FakeValidator()
    spot = WorldExecutor(sw, wallet=WALLET, genesis=MAINNET_GENESIS, signer=PcdSigner(RFC_SEED), endpoints=eps,
                         chain=chain, validator=validator, mode_state=mode_state, clock=clock, sleep=clock.sleep)
    policy, limits = _routing(cfg)
    router = SpotRouter([Prov(sw, clock, "jupiter", ("jupiter_order_v2", "jupiter_build_v2")),
                         Prov(sw, clock, "okx", ("okx_solana_v6",))], policy=policy, limits=limits, clock=clock,
                        wall=clock)
    obs = lambda: PriceObs(NATIVE_SOL, USDC_MINT, D(150), clock(), "test")     # noqa: E731
    fee = lambda: FEE                                                          # noqa: E731
    live = SolLegs(spot, perp, router, False, ACCOUNT_ID, WALLET, lambda: D(150), obs, fee, can_send=True,
                   block_height=chain.block_height)
    simlegs = SolLegs(SimSolSpot(spot, wallet=WALLET), SimHlPerp(perp, fee_taker=FEE, clock=clock), router, True,
                      ACCOUNT_ID, WALLET, lambda: D(150), obs, fee, block_height=chain.block_height)
    w = SimpleNamespace(clock=clock, venue=venue, hl=hl, sol=sw, loader=loader, conns=conns, perp=perp, spot=spot,
                        router=router, live=live, simlegs=simlegs, validator=validator, path=p, db=db,
                        keys_mode=keys_mode, legacy=legacy)
    w.registry = I.parse_registry(registry or registry_text())
    w.reg = RuntimeRegistry(legacy or (lambda s: None), {SOL_HL: lambda s: simlegs if s else live})
    restart(w)
    return w


def restart(w) -> None:
    """Новый «процесс»: свои Desk/Engine/соединение над той же БД и тем же миром (сеть не забывает)."""
    w.conns = Conns(w.db)
    w.con = w.conns.get()
    w.hooks = RecHooks()
    w.desk = Desk(w.conns, w.reg, owner_loader=w.loader, table_loader=lambda: {}, keys_mode=w.keys_mode,
                  clock=w.clock, registry_loader=lambda cfg: w.registry)
    w.engine = Engine(w.conns, w.reg, w.desk, w.hooks, owner_loader=w.loader, keys_mode=w.keys_mode, clock=w.clock,
                      sleep=w.clock.sleep, clip_gap_s=0)


def entry_cmd(usdc=D(150), coin="ANSEM", policy="auto", dex="para") -> ProfileEntry:
    return ProfileEntry(coin, policy, "solana", "hyperliquid", dex, usdc)


def approve_run(w, prop) -> None:
    assert store.approve_intent(w.con, prop.intent_id, prop.nonce, now=w.clock())
    w.engine.execute(prop.intent_id)


def enter(w, usdc=D(150)):
    prop = w.desk.propose_profile_entry(entry_cmd(usdc), chat=None)
    approve_run(w, prop)
    return prop


def book(w, did):
    from funding_bot.trade.engine import deal_book
    return deal_book(w.con, did)
