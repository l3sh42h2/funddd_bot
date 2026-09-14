"""Интеграция фазы 2 (trade_spec §6, §8, §9): полный dry-run входа и выхода на фейках, «стоп» между ногами доводит
хедж, рестарт посреди клипа в каждом состоянии, UNKNOWN-заявка не повторяется, dry не читает ключей, двойное «да»
даёт один submit. Плюс проводка бота, гейт выката и CLI.

Фейки — на уровне протоколов ног: симуляция идёт настоящими SimSpot/SimPerp поверх фейковых публичных данных;
«боевые» ноги — фейки SpotLeg/PerpLeg с теми же воротами, что у EvmWallet/AsterTrade (пауза пропускает только
hedge=True). Сбой процесса — исключение Crash(BaseException): исполнитель ловит только Exception, так что БД
остаётся ровно в том состоянии, в котором процесс «умер». Сеть не трогается, ключей нет.
"""
from __future__ import annotations
import re, subprocess, sys, threading, time
from dataclasses import dataclass, replace
from decimal import Decimal as D, ROUND_FLOOR
from pathlib import Path
from types import SimpleNamespace
import pytest
from funding_bot.trade import engine as eng, owner, reconcile, store
from funding_bot.trade.engine import Conns, Desk, Engine, Hooks, Legs, deal_book
from funding_bot.trade.keys import ModeForbidden
from funding_bot.trade.sim import SimPerp, SimSpot
from funding_bot.trade.store import ClipState, DealState, DexTxState, IntentStatus, PerpOrderState
from funding_bot.trade.types import Book, DexQuote, Filters, PerpFill, PerpInstrument, SwapResult
from funding_bot.trade.adapters.signing_fence import JournalBoundIoc
from funding_bot.tg import auth, views
from funding_bot.tg.bot import Bot, Jobs
from funding_bot.tg.sender import Sender, html_ok, to_plain

ROOT = Path(__file__).resolve().parents[1]
RAW_NUM = re.compile(r"\d\.\d{10,}")                 # сырой Decimal (18 знаков) в тексте владельцу
ASCII_MINUS = re.compile(r"(?:^|[\s(])-\d", re.M)    # минус в числах — только «−»
flat = lambda s: s.replace(views.NBSP, " ")         # noqa: E731 — неразрывные пробелы обычными
EXAMPLE = (ROOT / "deploy" / "owner.toml.example").read_text()
WALLET = "0x" + "b2" * 20  # synthetic fixture identity, independent of any configured account
STABLE = "0x55d398326f99059ff775485246999027b3197955"
TOKEN = "0x" + "a1" * 20
ROUTER = "0x5994814f2c4040b863a0125a45de152a8c2a4dec"
SYMBOL = "AIW3USDT"
PX = D("0.0409")
E18 = 10 ** 18
OWNER = 111
FILT = Filters(tick=D("0.00001"), step=D(1), min_qty=D(1), max_qty_limit=D(800000), max_qty_market=D(80000),
               min_notional=D(5), tifs=frozenset({"GTC", "IOC", "GTX"}))
TABLE = {"ts": 0, "sf_rows": [{"base": "AIW3", "spot_ex": "okxdex", "perp_ex": "aster", "spot": f"56:{TOKEN}",
                               "perp": SYMBOL, "ident": "same", "ident_ev": "fixture:verified", "mismatch": False, "period": 1,
                               "spot_label": "okx·bsc"}]}


def live_toml(clip: str = '"auto"') -> str:
    return f'''mode = "live"
[telegram]
owner_id = {OWNER}
[wallets]
bsc = "{WALLET}"
aster_user = "0x1111111111111111111111111111111111111111"
aster_signer = "0x2222222222222222222222222222222222222222"
[limits]
deal_max_usd_per_leg = 500
max_open_deals = 1
daily_loss_stop_usd = "off"
[dex]
slippage_pct = 3
impact_cap_pct = 5
approve_policy = "exact"
broadcast = "public"
allow_tax_tokens = false
native_reserve = 0.001
[perp.aster]
leverage = 1
margin_type = "ISOLATED"
max_slip_bps = 30
touch_frac_max = 0.5
liq_alert_pct = 20
[exec]
clip_max_usd = {clip}
clips_max = 4
unhedged_usd_max = 5
exec_time_max_s = 600
refill_wait_max_s = 0
plan_cost_drift_pct = 0.5
'''


class Crash(BaseException):
    """Смерть процесса посреди шага: исполнитель её не ловит (ловит только Exception)."""


# --- рынок и публичные данные ---------------------------------------------------------------------------
class Market:
    """Котировки пула: цена p·(1 ± (c0 + k·$)), газ tradeFee $0.01 (k, c0 — живые AIW3 12.09)."""

    def __init__(self):
        self.px, self.c0, self.k, self.gas = PX, D("0.0001"), D("4.35e-7"), 0.01

    def quote(self, t_in: str, t_out: str, amount: int) -> DexQuote:
        a = D(amount) / E18
        if t_in.lower() == STABLE:
            p = self.px * (1 + self.c0 + self.k * a)
            out = int((a / p * E18).to_integral_value(ROUND_FLOOR))
        else:
            p = self.px * (1 - self.c0 - self.k * a * self.px)
            out = int((a * p * E18).to_integral_value(ROUND_FLOOR))
        return DexQuote("bsc", t_in.lower(), t_out.lower(), int(amount), out, 18, 18, None, self.gas, None, False, 0.0)


def make_book() -> Book:
    bids = tuple((PX - D("0.00001") * i, D(50000)) for i in range(10))
    asks = tuple((D("0.04092") + D("0.00001") * i, D(50000)) for i in range(10))
    return Book(bids, asks, 0.0)


class SpotRO:
    """Нога DEX только для чтения (как OkxEvmSpot без отправителя) — под SimSpot."""
    chain = "bsc"

    def __init__(self, market: Market):
        self.m, self.wallet, self.stable, self.stable_dec = market, WALLET, STABLE, 18

    def decimals(self, token, hint=None):
        return 18

    def quote(self, t_in, t_out, amount):
        return self.m.quote(t_in, t_out, amount)

    def balances(self, token):
        return {"stable": 1000 * E18, "token": 0, "native": E18 // 10}

    def pool_price(self, token):
        return self.m.px


class PerpPub:
    """Публичная нога перпа (фильтры, стакан, фандинг, свечи для σ) — под SimPerp. Свечи нужны с 12.09: в примере
    owner.toml unhedged_usd_max = "auto" (0.5 % клипа), а предел риска без σ не оценить — план отказал бы."""
    venue = "aster"

    def __init__(self):
        self.b = make_book()
        self.http = Klines()

    def filters(self, s):
        return FILT

    def book(self, s, limit=20):
        return self.b

    def funding(self, s):
        return PX, D("0.0005"), 1_757_700_000_000

    def instrument(self, s):
        return instrument_of(s)


def instrument_of(s: str) -> PerpInstrument:
    """Контракт по имени символа, как baseAsset Aster (AIW3USDT → AIW3 ×1; 1000BONKUSDT → BONK ×1000) — фаза 1:
    без instrument() вход отказал бы (m не известен)."""
    from funding_bot.symbols import norm_symbol_factor
    base_asset = s[:-4]
    base, fac = norm_symbol_factor(base_asset)
    return PerpInstrument(s, base_asset, base, D(int(fac)), "USDT", "PERPETUAL")


class CrashingSimPerp(SimPerp):
    """SimPerp, у которого можно «убить процесс» сразу после подписи заявки (nonce записан, итог — нет)."""
    crash = False

    def ioc(self, *a, on_signed=None, **kw):
        if self.crash:
            if on_signed is not None:
                on_signed(0)
            raise Crash()
        return super().ioc(*a, on_signed=on_signed, **kw)


class RecHooks(Hooks):
    def __init__(self):
        self.progresses, self.reports, self.requotes = [], [], []

    def progress(self, iid, html):
        self.progresses.append((iid, html))

    def report(self, html):
        self.reports.append(html)

    def final_report(self, snapshot):
        self.reports.append(views.final(snapshot))

    def requote(self, iid, reason):
        self.requotes.append((iid, reason))


def sim_env(tmp_path, toml: str | None = None):
    p = tmp_path / "owner.toml"
    p.write_text(toml if toml is not None else EXAMPLE)
    loader = lambda: owner.load(p)
    conns = Conns(tmp_path / "trade.db")
    market = Market()
    legs_sim = Legs(SimSpot(SpotRO(market), native_px=lambda: D(600)), CrashingSimPerp(PerpPub()), True, lambda: D(600))
    legs = lambda sim: legs_sim if sim else None
    desk = Desk(conns, legs, owner_loader=loader, table_loader=lambda: TABLE)
    hooks = RecHooks()
    engine = Engine(conns, legs, desk, hooks, owner_loader=loader, sleep=lambda s: None, clip_gap_s=0)
    return SimpleNamespace(conns=conns, con=conns.get(), loader=loader, legs=legs, legs_sim=legs_sim, desk=desk,
                           engine=engine, hooks=hooks, market=market, path=p)


def run_approved(e, prop) -> None:
    assert store.approve_intent(e.con, prop.intent_id, prop.nonce)
    e.engine.execute(prop.intent_id)


# --- «боевые» ноги-фейки с воротами -------------------------------------------------------------------------
@dataclass
class Resolved:
    tx_state: str
    status: str
    amount_in: int
    amount_out: int
    block: int = 0
    gas_used: int = 150_000
    eff_gas_price: int = 50_000_000
    gas_wei: int = 0
    gas_usd: float | None = None
    nonce: int = 0
    note: str = ""
    tx_hash: str = ''


class LiveSpot:
    """SpotLeg «live». swap проходит ворота EvmWallet (пауза → отказ до подписи; bump/cancel тут не нужны).
    crash: before_sign | after_send (своп исполнен в цепи, процесс умер) | pending | dropped | pool."""
    chain = "bsc"
    ci = '56'

    def __init__(self, market: Market, env):
        self.m, self.env = market, env
        self.wallet, self.stable, self.stable_dec = WALLET, STABLE, 18
        self.bal = {STABLE: 1000 * E18, TOKEN: 0}
        self.native = E18 // 10
        self.swaps, self.approvals = [], []
        self.resolutions: dict[str, Resolved] = {}
        self.crash: str | None = None
        self.on_swap = None
        self.on_approve = None
        self.nonce = 40

    def decimals(self, token, hint=None):
        return 18

    def quote(self, t_in, t_out, amount):
        return self.m.quote(t_in, t_out, amount)

    def balances(self, token):
        return {"stable": self.bal[STABLE], "token": self.bal.get(token.lower(), 0), "native": self.native}

    def pool_price(self, token):
        if self.crash == "pool":
            raise Crash()
        return self.m.px

    def ensure_allowance(self, token, need, clip_ref):
        self.approvals.append((token, need))
        if self.on_approve:
            self.on_approve()
        return None

    def _apply(self, t_in, t_out, a_in, a_out):
        self.bal[t_in.lower()] = self.bal.get(t_in.lower(), 0) - a_in
        self.bal[t_out.lower()] = self.bal.get(t_out.lower(), 0) + a_out

    def build_swap(self, t_in, t_out, amount, *, preflight=False):
        return SimpleNamespace(min_receive=self.quote(t_in, t_out, amount).amount_out)

    def swap(self, t_in, t_out, amount, clip_ref, *, approved_min_receive=None):
        _mode, paused = self.env.mode_state()
        if paused:
            raise ModeForbidden("пауза («стоп»): новые отправки запрещены")
        if self.crash == "before_sign":
            raise Crash()
        q = self.m.quote(t_in, t_out, amount)
        self.nonce += 1
        h = "0x%064x" % (1000 + self.nonce)
        if self.crash in ("after_send", "pending", "dropped"):
            # запись-до, как у EvmWallet: подписанная транзакция в dex_txs ДО отправки, потом «ушла»
            c2 = self.env.con2
            store.dex_tx_signed(c2, clip_id=int(clip_ref), kind="swap", chain="bsc", wallet=WALLET, nonce=self.nonce,
                                to_addr=ROUTER, value=0, min_receive=q.amount_out * 97 // 100, gas_limit=300_000,
                                gas_price=50_000_000, raw_tx="0xf86b", tx_hash=h)
            store.dex_tx_sent(c2, h)
            if self.crash == "after_send":
                self._apply(t_in, t_out, amount, q.amount_out)
                self.resolutions[h] = Resolved(DexTxState.MINED_OK, "ok", amount, q.amount_out, block=101)
            elif self.crash == "pending":
                self.resolutions[h] = Resolved(DexTxState.SENT, "unknown", 0, 0)
            else:
                self.resolutions[h] = Resolved(DexTxState.DROPPED, "unknown", 0, 0)
            raise Crash()
        self._apply(t_in, t_out, amount, q.amount_out)
        self.swaps.append((t_in, t_out, amount, q.amount_out))
        if self.on_swap:
            self.on_swap()
        return SwapResult(h, "ok", amount, q.amount_out, 150_000 * 50_000_000, 0.01, 101, self.nonce)

    def resolve(self, row):
        return replace(self.resolutions[row["tx_hash"]], tx_hash=row['tx_hash'])


class Klines:
    """/fapi/v1/klines для σ марка: 61 минутная свеча с колебанием на тик."""

    def get(self, path, params=None, retries=3, **kw):
        assert path == "/fapi/v1/klines"
        return [[i, "0", "0", "0", str(PX + (D("0.00001") if i % 2 else 0)), "0"] for i in range(61)]


class LivePerp(JournalBoundIoc):
    ioc_partial_terminal = True

    def history_account(self):
        return 'acct:v1:' + self.venue + ':fixture'

    """PerpLeg «live» с воротами AsterTrade: на паузе проходит только hedge=True. script — поведение очередной
    заявки: fill | unknown_filled (исполнена, ответ потерян) | unknown_lost (не дошла) | crash_after_fill |
    crash_lost. crash="before_sign" — процесс умер до подписи (nonce не записан)."""
    venue = "aster"

    def __init__(self, env):
        self.env = env
        self.b = make_book()
        self.pos = D(0)
        self.orders: dict[str, PerpFill] = {}
        self.trades: list[dict] = []
        self.calls: list[dict] = []
        self.settles: list[str] = []
        self.script: list[str] = []
        self.prove_not_found = False
        self.crash: str | None = None
        self.setups: list = []
        self.http = Klines()
        self.last_error = None
        self.seq = 500

    def filters(self, s):
        return FILT

    def book(self, s, limit=20):
        return self.b

    def funding(self, s):
        return PX, D("0.0005"), 1_757_700_000_000

    def instrument(self, s):
        return instrument_of(s)

    def position(self, s):
        return self.pos

    def available_margin(self):
        return D(1000)

    def setup(self, s, lev, mt):
        self.setups.append((s, lev, mt))

    def _exec(self, cid, side, qty, cap) -> PerpFill:
        levels = self.b.bids if side == "SELL" else self.b.asks
        left, got, quote = qty, D(0), D(0)
        for px, q in levels:
            if left <= 0 or ((px < cap) if side == "SELL" else (px > cap)):
                break
            t = min(q, left)
            got, quote, left = got + t, quote + t * px, left - t
        self.seq += 1
        oid = self.seq
        self.pos += -got if side == "SELL" else got
        if got:
            self.seq += 1
            self.trades.append({"trade_id": self.seq, "order_id": oid, "price": quote / got, "qty": got,
                                "quote_qty": quote, "commission_abs": quote * D("0.0004"), "commission_asset": "USDT",
                                "maker": False, "realized_pnl": D(0), "ts": 1})
        st = "FILLED" if got == qty else ("PARTIALLY_FILLED" if got else "EXPIRED")
        f = PerpFill(cid, oid, st, got, (quote / got) if got else D(0), quote, 0)
        self.orders[cid] = f
        return f

    def ioc(self, symbol, side, qty, px_cap, client_id, reduce_only, *, hedge=False, on_signed=None):
        on_signed = self._ioc_callback(on_signed, symbol=symbol, side=side, quantity=qty,
                                       price=px_cap, client_id=client_id, reduce_only=reduce_only)
        _mode, paused = self.env.mode_state()
        if paused and not hedge:
            raise ModeForbidden("пауза («стоп»): новые отправки запрещены")
        self.calls.append(dict(cid=client_id, side=side, qty=qty, cap=px_cap, ro=reduce_only, hedge=hedge,
                               paused=paused))
        if self.crash == "before_sign":
            raise Crash()
        n = len(self.calls)
        if on_signed is not None:
            on_signed(n)
        how = self.script.pop(0) if self.script else "fill"
        if how == "crash_after_fill":
            self._exec(client_id, side, qty, px_cap)
            raise Crash()
        if how == "crash_lost":
            raise Crash()
        if how == "unknown_filled":
            self._exec(client_id, side, qty, px_cap)
            return PerpFill(client_id, None, "UNKNOWN", D(0), D(0), D(0), n)
        if how == "unknown_lost":
            return PerpFill(client_id, None, "UNKNOWN", D(0), D(0), D(0), n)
        return self._exec(client_id, side, qty, px_cap)

    def query(self, symbol, cid):
        return self.orders.get(cid) or PerpFill(cid, None, "NOT_FOUND", D(0), D(0), D(0), 0, -2013)

    def settle_unknown(self, symbol, cid, *, pos_before, since_ms, known_order_ids=frozenset()):
        self.settles.append(cid)
        f = self.orders.get(cid)
        if f is not None:
            return f
        if self.prove_not_found and pos_before is not None and self.pos == pos_before:
            return PerpFill(cid, None, "NOT_FOUND", D(0), D(0), D(0), 0, -2013)
        return PerpFill(cid, None, "UNKNOWN", D(0), D(0), D(0), 0, -2013)

    def fills(self, symbol, from_id):
        return [dict(t) for t in self.trades if from_id is None or t["trade_id"] >= from_id]

    def funding_income(self, symbol, start_ms):
        return []


def live_env(tmp_path, clip: str = '"auto"'):
    p = tmp_path / "owner.toml"
    p.write_text(live_toml(clip))
    loader = lambda: owner.load(p)
    db = tmp_path / "trade.db"
    conns = Conns(db)
    e = SimpleNamespace(conns=conns, con=conns.get(), con2=store.connect(db), loader=loader, path=p)
    e.mode_state = lambda: (loader().mode, store.is_paused(e.con2))
    e.market = Market()
    e.spot, e.perp = LiveSpot(e.market, e), LivePerp(e)
    e.legs_live = Legs(e.spot, e.perp, False, lambda: D(600), can_send=True)
    e.legs = lambda sim: None if sim else e.legs_live
    e.desk = Desk(conns, e.legs, owner_loader=loader, table_loader=lambda: TABLE, keys_mode="live")
    e.hooks = RecHooks()
    e.engine = Engine(conns, e.legs, e.desk, e.hooks, owner_loader=loader, keys_mode="live", sleep=lambda s: None,
                      clip_gap_s=0)
    return e


def sends(e) -> tuple[int, int]:
    return len(e.spot.swaps), len(e.perp.calls)


# ==== 1. полный dry-run входа и выхода ==========================================================================
def test_dry_run_entry_and_exit_full_flow(tmp_path):
    e = sim_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    assert p.html.startswith(views.SIM_PREFIX) and "📝 <b>Вход AIW3" in p.html
    assert p.plan.missing_owner_keys, "dry строит план и перечисляет, что заблокировало бы live"
    run_approved(e, p)
    deal = store.get_deal(e.con, p.deal_id)
    assert deal["state"] == DealState.OPEN and deal["sim"] == 1
    bk = deal_book(e.con, p.deal_id)
    assert bk.known and bk.short > 0 and D(0) <= bk.delta(18) < FILT.step
    assert e.legs_sim.perp.pos[SYMBOL] == -bk.short
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.DONE
    assert any("AIW3 открыта" in h and h.startswith(views.SIM_PREFIX) for h in e.hooks.reports)
    assert bool(e.hooks.progresses) == (len(p.plan.clips) >= 2), "прогресс — только при 2 клипах и больше"
    assert e.legs_sim.perp.setups == [(SYMBOL, 1, "ISOLATED")]

    x = e.desk.propose_exit(p.deal_id, None, False, chat=OWNER)
    assert "📝 <b>Выход AIW3 · всё</b>" in x.html
    run_approved(e, x)
    deal = store.get_deal(e.con, p.deal_id)
    bk = deal_book(e.con, p.deal_id)
    assert deal["state"] == DealState.CLOSED and bk.tokens_raw == 0 and bk.short == 0
    assert e.legs_sim.perp.pos[SYMBOL] == 0
    assert any("закрыта" in h for h in e.hooks.reports)
    rows = e.con.execute("SELECT venue, state, reduce_only, side FROM perp_orders ORDER BY id").fetchall()
    assert {r[0] for r in rows} == {"sim:aster"}, "сделки симуляции не смешиваются с биржевыми"
    assert {r[1] for r in rows} == {"FILLED"}
    assert [(r[2], r[3]) for r in rows] == [(0, "SELL"), (1, "BUY")]
    # симуляция в заявках на биржу не превращается: ни одной строки dex_txs
    assert e.con.execute("SELECT count(*) FROM dex_txs").fetchone()[0] == 0
    # все тексты владельцу: HTML безопасен, без сырых Decimal и ASCII-минуса; в симуляции ссылок на tx нет
    for t in [p.html, x.html, *e.hooks.reports, *(h for _i, h in e.hooks.progresses)]:
        assert html_ok(t) and not RAW_NUM.search(t) and not ASCII_MINUS.search(t) and "<a " not in t, t
        assert t.startswith(views.SIM_MARK), t               # 🧪 — первым символом в КАЖДОМ сообщении симуляции


def test_dry_run_live_legs_are_never_used_for_sim_deal(tmp_path):
    """dry: живых ног нет вовсе — Desk в живую сделку не пустит, план идёт в симуляции."""
    e = sim_env(tmp_path)
    assert e.desk.mode(e.loader()) == "dry"
    with pytest.raises(eng.Refused):
        e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(500), sim=False)


# ==== 2. «стоп» между ногами доводит хедж ========================================================================
def test_stop_between_legs_still_completes_the_hedge(tmp_path):
    e = live_env(tmp_path, clip="300")                       # два клипа по ~$250
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    assert len(p.plan.clips) == 2
    e.spot.on_swap = lambda: store.set_paused(e.con2, True)  # «стоп» пришёл, пока своп клипа 1 был в сети
    run_approved(e, p)
    assert len(e.spot.swaps) == 1, "второй клип после «стоп» не начат"
    assert len(e.perp.calls) == 1
    call = e.perp.calls[0]
    assert call["paused"] and call["hedge"] and call["side"] == "SELL", "хедж исполненной ноги ушёл и на паузе"
    bk = deal_book(e.con, p.deal_id)
    assert D(0) <= bk.delta(18) < FILT.step and e.perp.pos == -bk.short
    deal = store.get_deal(e.con, p.deal_id)
    assert deal["state"] == DealState.PAUSED and deal["reason"] == "stop"
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.PARTIAL
    assert len(store.clips_of(e.con, p.intent_id)) == 1
    assert any("на паузе" in h and "Ноги ровно ✓" in h for h in e.hooks.reports)
    assert e.hooks.progresses, "два клипа — прогресс после первого"
    # «продолжить»: только свежий план и кнопки — сам исполнитель не продолжает
    store.set_paused(e.con, False)
    r = e.desk.propose_resume(p.deal_id, chat=OWNER)
    assert r.kind == "entry" and r.intent_id != p.intent_id and sends(e) == (1, 1)


def test_new_send_refused_while_paused_only_hedge_passes(tmp_path):
    """Нижние ворота: на паузе новый своп не подписывается, а SELL без hedge=True не уходит."""
    e = live_env(tmp_path)
    store.set_paused(e.con2, True)
    with pytest.raises(ModeForbidden):
        e.spot.swap(STABLE, TOKEN, 10 * E18, "1")
    with pytest.raises(ModeForbidden):
        e.perp.ioc(SYMBOL, "SELL", D(100), D("0.04"), "fb-DAAAA-e01-c1-a1", False)
    assert e.perp.ioc(SYMBOL, "SELL", D(100), D("0.04"), "fb-DAAAA-e01-c1-a2", False, hedge=True).status == "FILLED"


# ==== 3. UNKNOWN-заявка не повторяется ============================================================================
def test_unknown_perp_order_is_never_resent(tmp_path):
    e = live_env(tmp_path)
    e.perp.script = ["unknown_lost"]                         # ответа нет; -2013, но доказать «не выставлена» нельзя
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    assert len(e.perp.calls) == 1, "UNKNOWN не отправляется повторно"
    assert e.perp.settles == [e.perp.calls[0]["cid"]]
    row = store.get_perp_order(e.con, e.perp.calls[0]["cid"])
    assert row["state"] == PerpOrderState.UNKNOWN
    assert deal_book(e.con, p.deal_id).short is None, "неизвестный исход не превращается в 0"
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.PAUSED
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.PARTIAL
    # сверка потом доказала «не выставлена» — и всё равно ничего не отправила: голый лонг ждёт «дохедж»
    e.perp.prove_not_found = True
    chk = reconcile.check_deal(e.con, store.get_deal(e.con, p.deal_id), e.legs_live)
    assert store.get_perp_order(e.con, e.perp.calls[0]["cid"])["state"] == PerpOrderState.NOT_PLACED
    assert chk.matched is True and chk.hedged is False and "дохедж" in chk.detail
    assert len(e.perp.calls) == 1


def test_unknown_order_found_filled_is_counted_not_resent(tmp_path):
    e = live_env(tmp_path)
    e.perp.script = ["unknown_filled"]                       # исполнилась, ответ потерян — settle находит итог
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    assert len(e.perp.calls) == 1
    assert store.get_perp_order(e.con, e.perp.calls[0]["cid"])["state"] == PerpOrderState.FILLED
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    assert e.perp.pos == -deal_book(e.con, p.deal_id).short


def test_proven_not_placed_resends_under_new_attempt_id(tmp_path):
    e = live_env(tmp_path)
    e.perp.script = ["unknown_lost"]
    e.perp.prove_not_found = True                            # -2013 ×3 + позиция и сделки неизменны
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    cids = [c["cid"] for c in e.perp.calls]
    assert len(cids) == 2 and cids[0].endswith("-a1") and cids[1].endswith("-a2") and cids[0][:-1] == cids[1][:-1]
    assert store.get_perp_order(e.con, cids[0])["state"] == PerpOrderState.NOT_PLACED
    assert store.get_perp_order(e.con, cids[1])["state"] == PerpOrderState.FILLED
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN


def test_requote_on_cost_drift_sends_nothing(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    e.market.c0 = D("0.02")                                  # у кнопки издержки выросли на ~1 п.п. > допуска 0.5
    run_approved(e, p)
    assert sends(e) == (0, 0) and e.spot.approvals == []
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.FAILED
    assert e.hooks.requotes and "издержки" in e.hooks.requotes[0][1]


def test_live_full_entry_then_exit(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    assert e.perp.setups == [(SYMBOL, 1, "ISOLATED")] and e.spot.approvals[0][0] == STABLE
    x = e.desk.propose_exit("AIW3", None, False, chat=OWNER)          # монета вместо id
    run_approved(e, x)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.CLOSED
    assert e.perp.pos == 0 and e.spot.bal[TOKEN] == 0
    assert e.perp.calls[-1]["ro"] is True and e.perp.calls[-1]["side"] == "BUY"
    assert e.spot.approvals[-1][0] == TOKEN, "approve токена — перед первым клипом выхода"
    assert store.last_trade_id(e.con, "aster") is not None, "userTrades добраны для отчёта"


def test_max_open_deals_rechecked_when_the_button_is_pressed(tmp_path):
    """План построен при 0 открытых; до кнопки открылась другая сделка — вход не начинается (max_open_deals = 1):
    ни approve, ни свопа, ни заявки."""
    e = live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    row = dict(store.get_deal(e.con, p.deal_id))
    row.update(id="DOTHER", state="OPEN", token="0x" + "b2" * 20, symbol="BBBUSDT")
    e.con.execute(f"INSERT INTO deals({','.join(row)}) VALUES({','.join('?' * len(row))})", tuple(row.values()))
    e.con.commit()
    run_approved(e, p)
    assert sends(e) == (0, 0) and e.spot.approvals == [] and e.perp.setups == []
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.ABORTED
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.FAILED
    assert any("max_open_deals" in h for h in e.hooks.reports)


def test_approve_gas_is_linked_to_the_intent(tmp_path):
    """approve идёт до первого клипа (clip_id пуст) — и всё равно попадает в транзакции намерения: в отчёт, в
    итог издержек и в PnL сделки."""
    from funding_bot.trade import report
    e = live_env(tmp_path)
    h = "0x" + "ab" * 32

    def approve_with_tx(token, need, clip_ref):
        e.spot.approvals.append((token, need))
        store.dex_tx_signed(e.con2, clip_id=None, kind="approve", chain="bsc", wallet=WALLET, nonce=39, to_addr=token,
                            value=0, min_receive=None, gas_limit=60_000, gas_price=50_000_000, raw_tx="0xf86b",
                            tx_hash=h)
        store.dex_tx_sent(e.con2, h)
        store.dex_tx_resolve(e.con2, h, DexTxState.MINED_OK, block=100, status=1, gas_used=46_000,
                             eff_gas_price=50_000_000)
        return SimpleNamespace(status="ok", tx_hash=h, hashes=(h,), note="")

    e.spot.ensure_allowance = approve_with_tx
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    txs = eng.intent_txs(e.con, p.intent_id)
    assert [t["tx_hash"] for t in txs if t["kind"] == "approve"] == [h]
    assert report.gas_totals(txs, D(600))["approve_native"] == D(46_000 * 50_000_000) / E18


def test_okx_dex_without_chain_resolves_chain_from_table(tmp_path):
    """«вход AIW3 okx dex aster 450» (владелец 12.09): сеть берётся из таблицы и названа в плане до кнопки; монета
    только в другой сети — честный отказ с перечнем сетей, а не догадка."""
    e = sim_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx", "aster", D(450), chat=OWNER)
    assert "📝 <b>Вход AIW3" in p.html and "OKX DEX·BSC" in p.html
    sol = {"ts": 0, "sf_rows": [dict(TABLE["sf_rows"][0], spot="501:So11111111111111111111111111111111111111112",
                                     spot_label="okx·sol")]}
    d2 = Desk(e.conns, e.legs, owner_loader=e.loader, table_loader=lambda: sol)
    with pytest.raises(eng.Refused) as ei:
        d2.plan_entry("AIW3", "okx", "aster", D(450), sim=True)
    assert "только okx·bsc" in ei.value.html


# ==== 4. рестарт посреди клипа в каждом состоянии ====================================================================
def _arm(name: str):
    def planned(e):
        e.spot.on_approve = lambda: setattr(e.spot, "crash", "pool")   # клип создан (PLANNED), до свопа
    arms = {
        "planned": planned,
        "dex_unsigned": lambda e: setattr(e.spot, "crash", "before_sign"),
        "dex_mined": lambda e: setattr(e.spot, "crash", "after_send"),
        "dex_pending": lambda e: setattr(e.spot, "crash", "pending"),
        "dex_dropped": lambda e: setattr(e.spot, "crash", "dropped"),
        "wallet_short": lambda e: setattr(e.spot, "crash", "after_send"),
        "perp_intent": lambda e: setattr(e.perp, "crash", "before_sign"),
        "perp_sent_filled": lambda e: setattr(e.perp, "script", ["crash_after_fill"]),
        "perp_sent_lost": lambda e: setattr(e.perp, "script", ["crash_lost"]),
        "perp_sent_lost_proven": lambda e: setattr(e.perp, "script", ["crash_lost"]),
    }
    return arms[name]


RESTART_CASES = [
    # (состояние в момент смерти, сделка после сверки, matched, ноги сбалансированы)
    ("planned", DealState.ABORTED, True, True),
    ("dex_unsigned", DealState.ABORTED, True, True),
    ("dex_dropped", DealState.ABORTED, True, True),
    ("dex_mined", DealState.PAUSED, True, False),
    ("dex_pending", DealState.HALTED_MISMATCH, None, None),
    ("wallet_short", DealState.HALTED_MISMATCH, False, False),
    ("perp_intent", DealState.PAUSED, True, False),
    ("perp_sent_filled", DealState.PAUSED, True, True),
    ("perp_sent_lost", DealState.HALTED_MISMATCH, None, None),
    ("perp_sent_lost_proven", DealState.PAUSED, True, False),
]


@pytest.mark.parametrize("case,want_state,want_matched,want_hedged", RESTART_CASES, ids=[c[0] for c in RESTART_CASES])
def test_restart_mid_clip_in_each_state(tmp_path, case, want_state, want_matched, want_hedged):
    e = live_env(tmp_path, clip="300")
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    assert store.approve_intent(e.con, p.intent_id, p.nonce)
    _arm(case)(e)
    with pytest.raises(Crash):
        e.engine.execute(p.intent_id)
    assert store.busy_intents(e.con) == 1, "умер посреди исполнения: намерение всё ещё running"
    e.spot.crash = e.perp.crash = None
    if case == "wallet_short":
        e.spot.bal[TOKEN] -= 5 * E18                          # токены ушли мимо сделки
    if case == "perp_sent_lost_proven":
        e.perp.prove_not_found = True
    before = sends(e)

    rep = reconcile.startup(e.con, e.legs, now=time.time())

    assert rep.interrupted == [p.intent_id]
    assert store.get_intent(e.con, p.intent_id)["status"] == IntentStatus.INTERRUPTED
    assert store.busy_intents(e.con) == 0, "гейт выката снова пропускает"
    (dr,) = rep.deals
    assert store.get_deal(e.con, p.deal_id)["state"] == want_state, dr.check.detail
    assert dr.check.matched is want_matched, dr.check.detail
    if want_hedged is not None and want_state != DealState.ABORTED:
        assert dr.check.hedged is want_hedged, dr.check.detail
    assert sends(e) == before, "сверка ничего не отправляет"
    # автоматического продолжения нет: очередь пуста, прерванное намерение не исполняется даже при submit
    assert e.engine.q.empty()
    e.engine.execute(p.intent_id)
    assert sends(e) == before
    # висящих строк не осталось там, где исход выяснен
    open_orders = store.perp_orders_unresolved(e.con)
    open_txs = store.dex_txs_unresolved(e.con)
    if want_matched is True:
        assert open_orders == [] and open_txs == []
    clips = store.clips_of(e.con, p.intent_id)
    if case == "dex_mined":
        assert clips[0]["state"] == ClipState.DEX_OK and int(clips[0]["dex_out"]) == e.spot.bal[TOKEN]
    if case in ("dex_dropped", "dex_unsigned"):
        assert clips[0]["state"] == ClipState.DEX_REVERTED
    if case == "dex_pending":
        assert clips[0]["state"] == ClipState.DEX_UNKNOWN
    if case == "perp_intent":
        assert store.get_perp_order(e.con, e.perp.calls[0]["cid"])["state"] == PerpOrderState.NOT_PLACED


def test_after_restart_rehedge_restores_the_pair(tmp_path):
    """Своп прошёл, процесс умер до хеджа: сверка → PAUSED с голым лонгом; «дохедж» (кнопкой) закрывает дельту."""
    e = live_env(tmp_path, clip="300")
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    assert store.approve_intent(e.con, p.intent_id, p.nonce)
    e.spot.crash = "after_send"
    with pytest.raises(Crash):
        e.engine.execute(p.intent_id)
    e.spot.crash = None
    reconcile.startup(e.con, e.legs, now=time.time())
    fix = e.desk.propose_fix("rehedge", p.deal_id, chat=OWNER)
    run_approved(e, fix)
    bk = deal_book(e.con, p.deal_id)
    assert D(0) <= bk.delta(18) < FILT.step and e.perp.pos == -bk.short
    assert e.perp.calls[-1]["side"] == "SELL"
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.PAUSED   # вход прерван — дальше решает владелец
    for t in [fix.html, *e.hooks.reports]:                      # план и итог дохеджа — без сырых Decimal
        assert html_ok(t) and not RAW_NUM.search(t) and not ASCII_MINUS.search(t), t


def test_sim_restart_seeds_simulation_from_journal_and_reports(tmp_path):
    e = sim_env(tmp_path, toml=EXAMPLE.replace('owner_id = ""', f"owner_id = {OWNER}"))
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    assert store.approve_intent(e.con, p.intent_id, p.nonce)
    e.legs_sim.perp.crash = True                             # подписали заявку — и процесс умер
    with pytest.raises(Crash):
        e.engine.execute(p.intent_id)
    # «новый процесс»: виртуальное состояние симуляции потеряно
    fresh = Legs(SimSpot(SpotRO(e.market), native_px=lambda: D(600)), SimPerp(PerpPub()), True, lambda: D(600))
    tg = FakeTg()
    sender = Sender(tg, gap_s=0, sleep=lambda s: None)
    bot = Bot(conns=e.conns, desk=e.desk, engine=FakeEngine(), sender=sender, legs=lambda sim: fresh if sim else None,
              poll_api=tg, owner_loader=e.loader, jobs=Jobs(sync=True))
    rep = bot.startup()
    (dr,) = rep.deals
    deal = store.get_deal(e.con, p.deal_id)
    bk = deal_book(e.con, p.deal_id)
    assert deal["state"] == DealState.PAUSED and dr.check.matched is True and dr.check.hedged is False
    assert fresh.perp.position(SYMBOL) == -bk.short == 0
    assert fresh.spot.ledger[TOKEN] == bk.tokens_raw > 0, "SimSpot засеян книгой"
    sender.drain()
    texts = [c[2] for c in tg.calls if c[0] == "send"]
    assert any("Перезапуск во время входа AIW3" in t and "<code>дохедж" in t and t.startswith(views.SIM_PREFIX)
               for t in texts)
    # C: голая нога в $ — по средней цене входа из журнала (без сети): 500 $ спота куплено, шорта нет
    assert dr.check.delta_usd is not None and abs(dr.check.delta_usd - D(500)) < D(5), dr.check.delta_usd
    assert any(re.search(r"⚠️ Без хеджа \+12\s221 AIW3 ≈ (499|500|501)\s\$", t) for t in texts), texts


def test_restart_leaves_open_deal_open_and_aborts_orphan_drafts(tmp_path):
    e = live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    stale = e.desk.propose_exit(p.deal_id, None, False, chat=OWNER)        # кнопки остались от прежнего процесса
    rep = reconcile.startup(e.con, e.legs, now=time.time())
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    assert stale.intent_id in rep.expired
    assert store.get_intent(e.con, stale.intent_id)["status"] == IntentStatus.EXPIRED
    draft = store.create_deal(e.con, coin="X", chain="bsc", token="0x" + "b2" * 20, token_dec=18, perp_venue="aster",
                              symbol="XUSDT", leg_usd=D(10), owner_json="{}", sim=True)
    rep = reconcile.startup(e.con, e.legs, now=time.time())
    assert draft in rep.aborted_drafts and store.get_deal(e.con, draft)["state"] == DealState.ABORTED


# ==== 5. dry не читает ключей ================================================================================
def test_dry_mode_loads_no_keys(tmp_path, monkeypatch):
    import funding_bot.trade.keys as keys_mod
    monkeypatch.setattr(keys_mod, "load", lambda *a, **k: pytest.fail("dry вызвал keys.load"))
    monkeypatch.setenv("DEX_EVM_KEY", "11" * 32)
    monkeypatch.setenv("ASTER_SIGNER_KEY", "22" * 32)
    env = {"DEX_EVM_KEY": "33" * 32, "ASTER_SIGNER_KEY": "44" * 32}
    p = tmp_path / "owner.toml"
    p.write_text(EXAMPLE)                                     # mode пуст → dry
    cfg = owner.load(p)
    for kw in ({"environ": env}, {"environ": None}):
        rt = eng.build_runtime(cfg, Conns(tmp_path / "t.db"), okx=object(), rpc=object(), aster=PerpPub(), **kw)
        assert rt.mode == "dry" and rt.keys is None and rt.live is None and rt.sim.sim is True
    p.write_text(live_toml())                                 # файл просит live, процесс понижен до dry
    rt = eng.build_runtime(owner.load(p), Conns(tmp_path / "t.db"), mode="dry", environ=env, okx=object(),
                           rpc=object(), aster=PerpPub())
    assert rt.keys is None and rt.live is None
    assert env == {"DEX_EVM_KEY": "33" * 32, "ASTER_SIGNER_KEY": "44" * 32}, "окружение не тронуто"
    import os
    assert os.environ["DEX_EVM_KEY"] == "11" * 32 and os.environ["ASTER_SIGNER_KEY"] == "22" * 32


# ==== 6. бот: кнопки, «стоп», устаревшее, истечение ================================================================
class FakeTg:
    def __init__(self):
        self.calls: list[tuple] = []
        self.mid = 100
        self._lock = threading.Lock()

    def send_message(self, chat_id, text, *, html=True, reply_markup=None, silent=False):
        with self._lock:
            self.mid += 1
            self.calls.append(("send", chat_id, text, reply_markup, self.mid))
            return {"message_id": self.mid}

    def edit_message_text(self, chat_id, message_id, text, *, html=True, reply_markup=None):
        self.calls.append(("edit", chat_id, message_id, text, reply_markup))
        return True

    def answer_callback_query(self, cid, text=None, show_alert=False):
        with self._lock:
            self.calls.append(("answer", cid, text))
        return True


class FakeEngine:
    def __init__(self):
        self.submitted: list[str] = []
        self.pause_evt = threading.Event()
        self.term = threading.Event()
        self.current = None
        self.hooks = None
        self._lock = threading.Lock()

    def submit(self, iid):
        with self._lock:
            self.submitted.append(iid)

    def busy(self):
        return False

    def wait_idle(self, t):
        return True


_UPD = [0]


def msg(text: str, uid: int = OWNER, ts: float | None = None) -> dict:
    _UPD[0] += 1
    return {"update_id": _UPD[0], "message": {"message_id": _UPD[0], "date": int(ts or time.time()), "text": text,
                                              "chat": {"id": uid, "type": "private"},
                                              "from": {"id": uid, "is_bot": False}}}


def cb(data: str, uid: int = OWNER, mid: int = 102) -> dict:
    _UPD[0] += 1
    return {"update_id": _UPD[0], "callback_query": {"id": f"cb{_UPD[0]}", "from": {"id": uid, "is_bot": False},
                                                     "data": data, "message": {"message_id": mid,
                                                                               "chat": {"id": uid, "type": "private"}}}}


def bot_env(tmp_path):
    e = sim_env(tmp_path, toml=EXAMPLE.replace('owner_id = ""', f"owner_id = {OWNER}"))
    tg = FakeTg()
    sender = Sender(tg, gap_s=0, sleep=lambda s: None)
    fe = FakeEngine()
    bot = Bot(conns=e.conns, desk=e.desk, engine=fe, sender=sender, legs=e.legs, poll_api=tg, owner_loader=e.loader,
              mode="dry", jobs=Jobs(sync=True))
    return e, tg, sender, bot, fe


def _plan_msgs(tg):
    return [c for c in tg.calls if c[0] == "send" and c[3]]


def test_owner_entry_gives_plan_with_buttons_and_double_tap_one_submit(tmp_path):
    e, tg, sender, bot, fe = bot_env(tmp_path)
    assert bot.handle(msg("вход AIW3 okx·bsc aster 500")) == auth.OWNER_MSG
    sender.drain()
    (plan,) = _plan_msgs(tg)
    assert "📝 <b>Вход AIW3" in plan[2] and plan[2].startswith(views.SIM_PREFIX)
    assert [flat(b["text"]) for b in plan[3]["inline_keyboard"][0]] == ["✅ Войти 500 $", "❌ Нет"]
    data = plan[3]["inline_keyboard"][0][0]["callback_data"]
    iid = data.split(":")[1]
    assert store.get_intent(e.con, iid)["msg_id"] == plan[4], "message_id плана записан (для снятия кнопок)"
    bot.handle(cb(data, mid=plan[4]))
    bot.handle(cb(data, mid=plan[4]))                        # двойное нажатие / второе устройство
    assert fe.submitted == [iid]
    assert [c[2] for c in tg.calls if c[0] == "answer"] == [views.CB_ACCEPTED, views.CB_ALREADY]
    sender.drain()
    edit = [c for c in tg.calls if c[0] == "edit"][-1]
    assert edit[2] == plan[4] and edit[4] is None and "\n" not in edit[3]          # одной строкой, кнопки сняты
    assert flat(edit[3]).startswith("🧪 ⏳ <b>Вход AIW3 · 500 $ на ногу</b> — принят ")


def test_press_submits_even_if_plan_head_fails(tmp_path, monkeypatch):
    """Ревью 12.09: строка закрытия плана теперь строится из БД ДО engine.submit — её сбой не должен оставить
    принятое (CAS) намерение без исполнения; кнопки всё равно снимаются."""
    e, tg, sender, bot, fe = bot_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(views, "intent_head", boom)
    bot.handle(cb(f"ok:{p.intent_id}:{p.nonce}"))
    assert fe.submitted == [p.intent_id]
    sender.drain()
    edit = [c for c in tg.calls if c[0] == "edit"][-1]
    assert edit[4] is None and edit[3].startswith("🧪 ⏳ <b>План</b> — принят ")


def test_sim_replies_to_resume_and_requote_carry_sim_mark(tmp_path):
    """🧪 во ВСЕХ сообщениях симуляции: перекотировка и ответ «продолжить» для остановленной сделки."""
    e, tg, sender, bot, fe = bot_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    bot.requote(p.intent_id, "издержки 0.05 % → 0.67 %")
    sender.drain()
    rq = [c[2] for c in tg.calls if c[0] == "send" and "Пересчитал план" in c[2]]
    assert len(rq) == 1 and rq[0].startswith(views.SIM_PREFIX + "\n"), rq
    run_approved(e, p)
    store.set_deal_state(e.con, p.deal_id, DealState.HALTED_MISMATCH, reason="position_mismatch")
    t = e.desk.propose_resume(p.deal_id, chat=OWNER)
    assert isinstance(t, str) and t.startswith(views.SIM_PREFIX + "\n") and "сверена" in t, t
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.PAUSED


def test_halt_promises_auto_unwind_only_when_it_is_scheduled(tmp_path, monkeypatch):
    """Ревью 12.09: «Без команды откачу сам в …» — только когда авто-откат действительно запланирован (сделка на
    паузе, то же условие, что у _unwind_due). Остановка с расхождением его не планирует — и не обещает."""
    from funding_bot.trade import engine as engine_mod
    cfg = SimpleNamespace(get=lambda k: D(600) if k == "exec.auto_unwind_naked_after_s" else None)
    naked = SimpleNamespace(known=True, tokens_raw=10 ** 19, short=D(5), delta=lambda dec: D(5),
                            tokens=lambda dec: D(10), m=D(1))      # книга: +5 AIW3 без хеджа (шаг 1, 1 контракт = 1)
    for sub, reason, promised in (("paused", "hedge_deficit", True), ("halted", "position_mismatch", False)):
        (tmp_path / sub).mkdir()
        e = sim_env(tmp_path / sub)
        p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
        assert store.approve_intent(e.con, p.intent_id, p.nonce)
        store.set_intent_status(e.con, p.intent_id, IntentStatus.RUNNING, expect=IntentStatus.APPROVED)
        store.set_deal_state(e.con, p.deal_id, DealState.ENTERING)
        run = SimpleNamespace(did=p.deal_id, iid=p.intent_id, kind="entry", dec=18, f=FILT, legs=e.legs_sim,
                              symbol=SYMBOL, cfg=cfg, deal=store.get_deal(e.con, p.deal_id), seq=1, n_total=1)
        with monkeypatch.context() as m:
            m.setattr(engine_mod, "deal_book", lambda con, did: naked)
            e.engine._paused(run, engine_mod.Pause(reason, "тест"))
        halt = e.hooks.reports[-1]
        assert halt.startswith(views.SIM_PREFIX) and "без хеджа" in halt, halt
        assert ("откачу сам" in halt) is promised and (p.deal_id in e.engine._unwind_due) is promised, (reason, halt)


def test_two_threads_pressing_yes_at_once_give_exactly_one_submit(tmp_path):
    e, tg, sender, bot, fe = bot_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    data = f"ok:{p.intent_id}:{p.nonce}"
    barrier = threading.Barrier(4)

    def press():
        barrier.wait()
        bot.handle(cb(data))

    ts = [threading.Thread(target=press) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert fe.submitted == [p.intent_id]
    answers = sorted(c[2] for c in tg.calls if c[0] == "answer")
    assert answers.count(views.CB_ACCEPTED) == 1 and len(answers) == 4


def test_stop_command_pauses_and_blocks_new_plans_and_buttons(tmp_path):
    e, tg, sender, bot, fe = bot_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    bot.handle(msg("стоп"))
    assert store.is_paused(e.con) and fe.pause_evt.is_set()
    bot.handle(cb(f"ok:{p.intent_id}:{p.nonce}"))
    assert fe.submitted == [] and [c[2] for c in tg.calls if c[0] == "answer"] == [views.CB_PAUSED]
    bot.handle(msg("вход AIW3 okx·bsc aster 500"))
    sender.drain()
    texts = [c[2] for c in tg.calls if c[0] == "send"]
    assert any(t.startswith("⏸ Пауза") for t in texts)
    assert any("пауза" in t.lower() and "⛔" in t for t in texts), "новый план на паузе не строится"
    assert not _plan_msgs(tg)
    bot.handle(msg("продолжить"))
    sender.drain()
    assert not store.is_paused(e.con) and not fe.pause_evt.is_set()
    assert "Пауза снята" in [c[2] for c in tg.calls if c[0] == "send"][-1]


def test_stale_and_stranger_commands_do_nothing(tmp_path):
    e, tg, sender, bot, fe = bot_env(tmp_path)
    assert bot.handle(msg("вход AIW3 okx·bsc aster 500", ts=time.time() - 200)) == auth.STALE
    assert bot.handle(msg("вход AIW3 okx·bsc aster 500", uid=999)) == auth.IGNORE
    assert bot.handle(msg("/start", uid=999)) == auth.STRANGER_START
    sender.drain()
    texts = [c[2] for c in tg.calls if c[0] == "send"]
    assert any("устарела" in t for t in texts) and any("user_id: <code>999</code>" in t for t in texts)
    assert e.con.execute("SELECT count(*) FROM intents").fetchone()[0] == 0


def test_plan_expiry_removes_buttons(tmp_path):
    e, tg, sender, bot, fe = bot_env(tmp_path)
    bot.handle(msg("вход AIW3 okx·bsc aster 500"))
    sender.drain()
    (plan,) = _plan_msgs(tg)
    iid = plan[3]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    bot.clock = lambda: time.time() + 120
    bot.housekeep()
    sender.drain()
    edit = [c for c in tg.calls if c[0] == "edit"][-1]
    assert edit[2] == plan[4] and "истёк, ничего не сделано" in edit[3] and edit[4] is None
    assert store.get_intent(e.con, iid)["status"] == IntentStatus.EXPIRED
    bot.handle(cb(plan[3]["inline_keyboard"][0][0]["callback_data"], mid=plan[4]))
    assert fe.submitted == []


def test_engine_progress_and_report_go_to_owner_via_queue(tmp_path):
    """Проводка хуков: прогресс — одно сообщение и правки, итог — новое сообщение (исполнение не ждёт Telegram)."""
    e, tg, sender, bot, fe = bot_env(tmp_path)
    bot.progress("E2222", "⏳ клип 1/2")
    bot.progress("E2222", "⏳ клип 1/2 (обновлено)")          # первое ещё в очереди — правка после его message_id
    sender.drain()
    bot.progress("E2222", "⏳ клип 2/2")
    fe.hooks.report("✅ итог")
    sender.drain()
    kinds = [(c[0], c[2] if c[0] == "send" else c[3]) for c in tg.calls]
    assert kinds[0] == ("send", "⏳ клип 1/2")
    assert ("edit", "⏳ клип 2/2") in kinds and ("send", "✅ итог") in kinds
    assert sum(1 for k in kinds if k[0] == "send" and k[1].startswith("⏳")) == 1


def test_positions_command_reports_journal_vs_truth(tmp_path):
    e, tg, sender, bot, fe = bot_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    bot.handle(msg("позиции"))
    sender.drain()
    text = [c[2] for c in tg.calls if c[0] == "send"][-1]
    assert text.startswith(views.SIM_PREFIX) and p.deal_id in text and "сверка ✓" in text


# ==== 7. гейт выката, юнит, CLI ==================================================================================
def test_remote_switch_refuses_while_intent_approved_or_running(tmp_path):
    path = ROOT / "deploy" / "remote_switch.sh"
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    sh = path.read_text()
    py = sh.split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    db = tmp_path / "trade.db"

    def count():
        r = subprocess.run([sys.executable, "-", str(db)], input=py, text=True, capture_output=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    con = store.connect(db)
    assert count() == "0"
    did = store.create_deal(con, coin="AIW3", chain="bsc", token=TOKEN, token_dec=18, perp_venue="aster",
                            symbol=SYMBOL, leg_usd=D(500), owner_json="{}", sim=True)
    iid, nonce = store.create_intent(con, deal_id=did, kind="entry", spec={}, plan={})
    assert count() == "0", "proposed — не исполнение"
    assert store.approve_intent(con, iid, nonce)
    assert count() == "1"
    # гейт стоит до замены кода и прямо перед рестартом трейдера
    assert sh.index("trader_gate\nrsync -a --delete \"${EXCL[@]}\" \"$NEXT/\" \"$DEST/\"") > 0
    assert sh.rindex("trader_gate") < sh.index("systemctl restart funding_bot-trader")
    assert "идёт исполнение" in sh


def test_trader_unit_file():
    lines = (ROOT / "deploy" / "funding_bot-trader.service").read_text().splitlines()
    kv = dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith("#"))
    assert kv["EnvironmentFile"] == "/home/admin/hyper/funding_bot/.env", "без «-»: без .env трейдер не стартует"
    assert kv["Restart"] == "always" and kv["TimeoutStopSec"] == "280" and kv["KillSignal"] == "SIGTERM"
    assert kv["ExecStart"].endswith("funding_bot trader") and kv["RestartPreventExitStatus"] == "78"


def test_plan_cli_and_trade_check_in_dry(tmp_path, capsys, monkeypatch):
    e = sim_env(tmp_path)
    rt = SimpleNamespace(sim=e.legs_sim, live=None, keys=None)
    text = eng.plan_cli("AIW3", "okx·bsc", "aster", D(500), owner_path=e.path, db_path=tmp_path / "trade.db",
                        table_loader=lambda: TABLE, runtime=rt)
    assert "Вход AIW3" in text and to_plain(views.SIM_PREFIX) in text and "<b>" not in text
    code, report = reconcile.check_report("readonly", None, owner_path=e.path, runtime=rt)
    assert code == 0 and "режим проверки: dry" in report and "для live не задано" in report
    from funding_bot import cli
    monkeypatch.setattr(eng, "plan_cli", lambda *a, **k: "ПЛАН-ОК")
    assert cli.main(["plan", "AIW3", "okx·bsc", "aster", "500"]) == 0
    assert "ПЛАН-ОК" in capsys.readouterr().out
    assert cli.main(["plan", "AIW3", "okx·bsc", "aster", "5e2"]) == 2


def test_trader_refuses_to_start_without_valid_token():
    """Код 78 (EX_CONFIG): systemd не перезапускает по кругу (RestartPreventExitStatus=78). Сеть не трогается."""
    from funding_bot.tg.bot import EXIT_CONFIG, run_trader
    assert run_trader({}) == EXIT_CONFIG
    assert run_trader({"TG_BOT_TOKEN": "не-токен"}) == EXIT_CONFIG


# ==== 8. «auto» владельца 12.09: числа плана доходят до заявок =====================================================
def auto_toml(clip: str = '"auto"') -> str:
    """live_toml с решениями владельца 12.09: α/β, clips_max, unhedged, exec_time, liq — "auto" (ожидание стакана 0 —
    часы в тестах настоящие)."""
    t = live_toml(clip)
    for k, v in (("max_slip_bps = 30", 'max_slip_bps = "auto"'), ("touch_frac_max = 0.5", 'touch_frac_max = "auto"'),
                 ("liq_alert_pct = 20", 'liq_alert_pct = "auto"'), ("clips_max = 4", 'clips_max = "auto"'),
                 ("unhedged_usd_max = 5", 'unhedged_usd_max = "auto"'),
                 ("exec_time_max_s = 600", 'exec_time_max_s = "auto"')):
        assert k in t
        t = t.replace(k, v)
    return t


def thin_bid_book() -> Book:
    """Лучший бид 60 токенов ($2.45 < minNotional 5), дальше толстые уровни с 4-го тика."""
    return Book(((PX, D(60)),) + tuple((PX - D("0.00001") * (4 + i), D(50000)) for i in range(9)), make_book().asks, 0.0)


def test_live_ioc_children_use_the_plans_alpha_beta_after_requote(tmp_path, monkeypatch):
    """auto: α/β подбирает план; у кнопки стакан поредел и перекотировка подобрала другой β — заявки ушли ровно с
    ним (кэп, размер ≤ α·полоса). Строка "auto" до построения дочерних не доходит ни разу."""
    import json
    from funding_bot.trade import planner as P
    e = live_env(tmp_path)
    e.path.write_text(auto_toml())
    seen = []
    orig = P.perp_children

    def spy(book, side, qty, f, alpha, beta, *a, **k):
        seen.append((alpha, beta))
        return orig(book, side, qty, f, alpha, beta, *a, **k)

    monkeypatch.setattr(P, "perp_children", spy)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    a0, b0 = p.plan.est["alpha"], p.plan.est["beta_bps"]
    assert p.plan.est["alpha_auto"] and a0 == D("0.1") and b0 == P.beta_candidates(make_book(), "SELL", FILT)[0]
    assert "α" not in p.html and "β" not in p.html                 # C: числа в плане, в сообщение не идут
    e.perp.b = thin_bid_book()                                  # до кнопки лучший бид поредел
    run_approved(e, p)
    assert store.get_deal(e.con, p.deal_id)["state"] == DealState.OPEN
    ev = [x for x in store.events(e.con, p.deal_id) if x["kind"] == "requote_ok"]
    js = json.loads(ev[0]["json"])
    a1, b1 = D(js["alpha"]), D(js["beta_bps"])
    assert b1 > b0, "перекотировка расширила β до ближнего уровня"
    cap = P.beta_px(PX, "SELL", FILT, b1)
    band = sum(q for px, q in e.perp.b.bids if px >= cap)
    assert e.perp.calls and all(c["cap"] == cap and c["qty"] <= a1 * band for c in e.perp.calls)
    assert seen and not any(isinstance(x, str) for pair in seen for x in pair)


@pytest.mark.parametrize("jump, want", [(10_000, DealState.ABORTED), (30, DealState.OPEN)])
def test_exec_time_auto_guard_uses_number_frozen_in_plan(tmp_path, jump, want):
    """exec_time_max_s = "auto": guard сравнивает с числом плана (3 × ожидаемая + 60 с ≥ 60 с), слово не перечитывает."""
    e = live_env(tmp_path)
    e.path.write_text(auto_toml())
    T = [time.time()]
    e.engine.clock = lambda: T[0]
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    est = p.plan.est
    assert est["exec_time_auto"] and est["exec_time_max_s"] == 3 * est["exec_expected_s"] + 60
    e.spot.on_approve = lambda: T.__setitem__(0, T[0] + jump)    # «время прошло» до первого клипа
    run_approved(e, p)
    d = store.get_deal(e.con, p.deal_id)
    assert d["state"] == want
    if want == DealState.ABORTED:
        assert d["reason"] == "exec_time" and sends(e) == (0, 0)
        assert any("exec_time_max_s auto" in h for h in e.hooks.reports)


def test_plan_message_hides_auto_numbers_and_missing_skips_auto_keys(tmp_path):
    """Решения владельца 12.09 («auto») заморожены в плане, но в сообщение не идут (C: техника скрыта); «для live не
    задано» — одной строкой с числом, и «auto»-ключи в это число не входят."""
    e = sim_env(tmp_path)                                        # пример owner.toml: решения владельца 12.09
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    est = p.plan.est
    assert (est.get("alpha_auto") or est.get("beta_auto")) and est.get("exec_time_max_s") is not None   # в плане
    assert not any(w in p.html for w in ("α", "β", "риск голой ноги", "тревога ликвидации"))
    miss = next(ln for ln in p.html.splitlines() if "Для live не задано" in ln)
    assert miss == f"⚠️ Для live не задано: {len(p.plan.missing_owner_keys)} · <code>статус</code>"
    labels = [views.key_label(k) for k in p.plan.missing_owner_keys]
    assert "id владельца в Telegram" in labels
    for label in ("предел цены IOC", "доля лучшего уровня", "максимум клипов", "предел незахеджированного",
                  "время исполнения", "тревога до ликвидации", "допуск перекотировки", "ожидание пополнения"):
        assert not any(label in x for x in labels), label


def test_rehedge_auto_freezes_alpha_beta_in_its_plan(tmp_path):
    from funding_bot.trade import planner as P
    e = live_env(tmp_path, clip="300")
    e.path.write_text(auto_toml(clip="300"))
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    assert store.approve_intent(e.con, p.intent_id, p.nonce)
    e.spot.crash = "after_send"                                  # своп прошёл, процесс умер до хеджа
    with pytest.raises(Crash):
        e.engine.execute(p.intent_id)
    e.spot.crash = None
    reconcile.startup(e.con, e.legs, now=time.time())
    fix = e.desk.propose_fix("rehedge", p.deal_id, chat=OWNER)
    assert fix.plan.est["alpha_auto"] and fix.plan.est["alpha"] == D("0.1")
    assert D(str(fix.plan.est["beta_bps"])).quantize(D("0.1")) == D("7.3")          # подобраны под AIW3, в плане
    lines = flat(fix.html).splitlines()
    assert lines[0] == "📝 <b>Дохедж AIW3</b>" and lines[1].startswith("Без хеджа +") and lines[1].endswith(" спота")
    assert lines[2].startswith("Продать ") and lines[2].endswith(" AIW3 на перпе Aster") and lines[3] == "⏱ 60 с"
    assert "α" not in fix.html and html_ok(fix.html) and not RAW_NUM.search(fix.html)
    run_approved(e, fix)
    cap = P.beta_px(PX, "SELL", FILT, fix.plan.est["beta_bps"])
    assert e.perp.calls and all(c["cap"] == cap for c in e.perp.calls)
    bk = deal_book(e.con, p.deal_id)
    assert D(0) <= bk.delta(18) < FILT.step and e.perp.pos == -bk.short
    done = e.hooks.reports[-1]
    assert done.startswith("✅ <b>Дохедж AIW3 выполнен</b> · ноги ровно ✓") and "<code>выход " in done
    assert not RAW_NUM.search(done) and html_ok(done)


def test_liq_alert_auto_is_half_the_entry_distance_and_reaches_positions(tmp_path):
    e = live_env(tmp_path)
    e.path.write_text(auto_toml())
    liq = {"px": "0.0818"}                                       # плечо 1x: ликвидация шорта у двойной цены
    e.perp.position_risk = lambda s: [{"symbol": SYMBOL, "liquidationPrice": liq["px"], "markPrice": "0.0409",
                                       "leverage": "1"}]
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    fin = [h for h in e.hooks.reports if "AIW3 открыта" in h][-1]
    assert "До ликвидации" not in fin          # 100 % при тревоге 50 % — штатное (показ — меньше 1.25 × тревоги)
    assert fin.startswith("✅") and html_ok(fin) and "<code>выход " in fin
    assert reconcile._liq_alert(e.con, p.deal_id) == 50

    def pos_text():
        rows, _m, _x = reconcile.positions(e.con, e.legs, now=time.time())
        assert rows[0]["payback_h"] is not None                 # та же формула окупаемости, что в плане и итоге
        return flat(views.positions([views.PositionView(**r) for r in rows], ts=time.time(), matched=True))

    assert "до ликвидации" not in pos_text() and "До окупаемости ~" in pos_text()
    liq["px"] = "0.0590"                                         # цена ушла к ликвидации: 44 % < 50 %
    assert "до ликвидации <b>44.25 %</b> (порог 50.00 %)" in pos_text()
    liq["px"] = "0.0740"                                         # 80.9 % ≥ 1.25 × 50 % — штатно, строки нет (не мигает)
    assert "до ликвидации" not in pos_text()
    liq["px"] = "0.0655"                                         # 60.1 % < 1.25 × 50 % — уже видно, без жирного
    assert "до ликвидации 60.15 % (порог 50.00 %)" in pos_text()


def test_perp_reject_text_names_venue_with_real_minus(tmp_path):
    """Ревью соответствия C 12.09: отказ биржи в остановке — «Aster −2022: …», а не «aster -2022» (движок пишет
    мимо views: подпись площадки и настоящий минус — теми же словарём и знаком, что в сообщениях)."""
    e = sim_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=OWNER)
    run_approved(e, p)
    x = e.desk.propose_exit(p.deal_id, None, True, chat=OWNER)          # «выход <id> перп»
    e.legs_sim.perp.pos[SYMBOL] = D(0)                                  # шорта на бирже нет — reduceOnly отвергнут
    run_approved(e, x)
    halt = next(t for t in reversed(e.hooks.reports) if "−2022" in t or "-2022" in t)
    assert "Aster −2022: " in halt and "aster -2022" not in halt and html_ok(halt), halt
    assert halt.startswith(views.SIM_PREFIX)


def test_dex_link_rows_are_view_only_not_tradable(tmp_path):
    """DEX-строка, связанная с перпом через другую площадку (ident_ev «dex_link:…»), видна на дашборде, но вход по
    ней — отказ: деньги только по токену, доказанному для самого перпа."""
    e = sim_env(tmp_path)
    linked = {"ts": 0, "sf_rows": [dict(TABLE["sf_rows"][0], ident_ev="dex_link:index")]}
    d2 = Desk(e.conns, e.legs, owner_loader=e.loader, table_loader=lambda: linked)
    with pytest.raises(eng.Refused) as ei:
        d2.plan_entry("AIW3", "okx·bsc", "aster", D(100), sim=True)
    assert "прямое доказательство" in ei.value.html
    assert e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(100), sim=True) is not None   # свой контракт — как раньше
