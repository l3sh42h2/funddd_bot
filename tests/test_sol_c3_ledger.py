"""Шаг C3: учёт и сверка связки Solana × Hyperliquid — фактические fills и фандинг HL (H15–H17), расходы без
двойного вычета (G03, S17, S18), накопительная книга сделки (G10), PnL при выходе (G11), неполнота и ревизия итога
(V04), денежные единицы (V05), оценка с котировкой выхода и асксами HL, чужие исполнения на рынке сделки (§11),
«позиции» по книге сделки, а не кошельку; ноги трейдера без Aster для одной SOL-связки (M02)."""
import dataclasses
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from funding_bot import cabinet
from funding_bot.trade import engine as eng, marks, owner, reconcile, sol_flow, sol_ledger, store
from funding_bot.trade.engine import CfgHolder, Conns, Refused, deal_book
from funding_bot.trade.fees import lamports, receipt_components
from funding_bot.trade.owner import SOL_HL
from funding_bot.trade.store import DealState
from funding_bot.tg.bot import build_trader_legs
import hl_support as S
import sol_c2_world as W
import sol_hl_fixtures as F
import test_trade_engine as TE
from sol_c2_world import approve_run, enter, entry_cmd, make_world

pytest.importorskip("solders")
pytest.importorskip("eth_account")
NET, ACCT = "mainnet", W.ACCOUNT.lower()


def fill(t, tid, oid, *, coin=W.FULL, acct=ACCT, cloid=None, sz="10", px="0.1667", fee="0.001", h=None):
    return {"network": NET, "account": acct, "coin": coin, "time": t, "tid": tid, "oid": oid, "cloid": cloid,
            "side": "A", "px": D(px), "sz": D(sz), "fee": D(fee), "fee_token": "USDC", "builder_fee": None,
            "closed_pnl": D(0), "hash": h or "0x" + f"{tid:064x}"}


def fund(t, usdc, *, coin=W.FULL, acct=ACCT, h=None):
    return {"network": NET, "account": acct, "coin": coin, "time": t, "hash": h, "usdc": D(usdc), "szi": D(-903),
            "rate": D("0.0001")}


def deal(w, prop):
    return store.get_deal(w.con, prop.deal_id)


# --- H15–H17: факт вместо оценки, дедуп по пространству API -----------------------------------------------------------
def test_h15_actual_fill_fee_replaces_estimate_not_added(tmp_path):
    w = make_world(tmp_path)
    w.venue.fills_hidden = True                  # fills ещё не в истории HL: комиссия — оценкой по ставке
    prop = enter(w)
    d = deal(w, prop)
    L0 = sol_ledger.ledger(w.con, d, fee_rate=W.FEE)
    assert L0.fees_est and L0.perp_sell > 0 and L0.perp_fee == L0.perp_sell * W.FEE
    w.venue.fills_hidden = False
    got = sol_ledger.ingest(w.con, d, w.perp)
    assert got["fills"] is True and not got["errors"], got
    L1 = sol_ledger.ledger(w.con, d, fee_rate=W.FEE)
    fact = sum(D(f["fee"]) for f in w.venue.fills)
    assert not L1.fees_est and L1.perp_fee == fact and fact > 0
    assert sol_ledger.ingest(w.con, d, w.perp)["fills"] is True       # повтор страницы — те же строки, не вторые
    assert sol_ledger.ledger(w.con, d, fee_rate=W.FEE).perp_fee == fact


def test_h16_h17_funding_and_fills_dedup_by_api_namespace(tmp_path):
    con = store.connect(tmp_path / "t.db")
    assert store.add_hl_funding(con, [fund(1000, "0.20"), fund(2000, "-0.05")]) == 2
    assert store.add_hl_funding(con, [fund(2000, "-0.05"), fund(3000, "0.01")]) == 1       # перекрытие страниц
    other = "0x" + "cd" * 20
    assert store.add_hl_funding(con, [fund(1000, "0.20", acct=other), fund(1000, "0.20", coin="xyz:ANSEM")]) == 2
    tot = sum(D(r[0]) for r in con.execute("SELECT usdc FROM hl_funding WHERE account=? AND coin=?", (ACCT, W.FULL)))
    assert tot == D("0.16")
    with pytest.raises(store.StoreError):
        store.add_hl_funding(con, [fund(1000, "0.30")])                              # тот же ключ, другая сумма
    assert store.add_hl_fills(con, [fill(1000, 7, 1), fill(1000, 7, 1, acct=other), fill(1000, 7, 1, coin="xyz:X")]) == 3
    assert store.add_hl_fills(con, [fill(1000, 7, 1)]) == 0
    with pytest.raises(store.StoreError):
        store.add_hl_fills(con, [fill(1000, 7, 2)])                                  # коллизия: другой oid
    store.set_cursor(con, "s", watermark_ms=5000, complete=True)
    store.set_cursor(con, "s", watermark_ms=None, complete=False, gap="страница целиком")
    c = store.get_cursor(con, "s")
    assert c["watermark_ms"] == 5000 and not c["complete"] and c["gap"]             # отметка не уходит назад


# --- G03, S17, S18: расходы Solana — каждый факт один раз, rent — не расход -------------------------------------------
def test_g03_s17_s18_network_fees_once_and_rent_separate(tmp_path):
    w = make_world(tmp_path)
    prop = enter(w)
    d = deal(w, prop)
    L = sol_ledger.ledger(w.con, d, fee_rate=W.FEE)
    assert L.network_lamports == 5020 and L.rent_locked_lamports == 0            # meta.fee чека мира
    sig = next(r["origin_ref"] for r in store.fee_events(w.con, deal_id=d["id"]))
    comps = [r for r in store.fee_events(w.con, deal_id=d["id"]) if r["origin_ref"] == sig]
    assert store.add_fee_events(w.con, origin_kind="sol_swap", origin_ref=sig,
                                components=[{k: (bool(r[k]) if k in ("included", "estimated", "refundable", "superseded")
                                                 else r[k]) for k in ("kind", "asset", "decimals", "amount_raw",
                                                                      "payer", "recipient", "included", "estimated",
                                                                      "refundable", "superseded")} for r in comps],
                                deal_id=d["id"]) == 0                               # G03: повтор чека — 0 новых
    # S17: meta.fee 30 000 уже содержит base 5000 + priority 25 000; tip 10 000 отдельно; оценка priority — superseded
    rc = receipt_components(fee_lamports=30_000, fee_payer=W.WALLET, tip_lamports=10_000,
                            rent_deposits=(2_000_000,), rent_refunds=(2_000_000,), source="receipt:test")
    est = dataclasses.replace(lamports("network_priority", 25_000, W.WALLET, estimated=True, source="cand"),
                              superseded=True)
    store.add_fee_events(w.con, origin_kind="sol_swap", origin_ref="sig-s17", components=[*rc, est], deal_id=d["id"])
    L = sol_ledger.ledger(w.con, d, fee_rate=W.FEE)
    assert L.network_lamports == 5020 + 40_000 and L.rent_locked_lamports == 0       # S18: депозит и возврат — не расход
    assert L.network_usdc(D(150)) == D(45_020) / D(10) ** 9 * D(150)
    assert L.network_usdc(None) is None                                                # цены SOL нет — не 0


# --- G10, G11: накопительная книга сделки -------------------------------------------------------------------------
def test_g11_pnl_exit_formula_and_cashflow_identity():
    assert sol_ledger.pnl_exit_of(D(7), D(110), D(108), D(1)) == D(8)
    # купили 20 за 200, продали 10 за 102 (реализовано по споту +2 при средней 10); шорт 20 по 10.5, откуплено 10 по 10.5
    L = sol_ledger.Ledger(quote_dec=6, spot_in=D(200), spot_out=D(102), perp_sell=D(210), perp_buy=D(105),
                          perp_fee=D(0), funding=D(0))
    realised, remaining_cost, open_notional = D(2), D(100), D(105)
    assert L.base(None) == realised - remaining_cost + open_notional == D(7)
    assert sol_ledger.pnl_exit_of(L.base(None), D(110), D(108), D(1)) == D(8)


def test_g10_funding_and_fees_counted_once_per_deal(tmp_path):
    w = make_world(tmp_path)
    t_before = int(w.clock() * 1000) - 3_600_000
    prop = enter(w)
    d = deal(w, prop)
    now_ms = int(w.clock() * 1000)
    w.venue.add_funding(t_before, "5")                        # до сделки — не её фандинг
    w.venue.add_funding(now_ms + 1000, "2")
    w.venue.add_funding(now_ms + 2000, "1")
    w.clock.sleep(5)
    cfg = marks.sol_cfg(d)
    m1 = sol_ledger.mark(w.con, d, w.live, now=w.clock(), cfg=cfg)
    m2 = sol_ledger.mark(w.con, d, w.live, now=w.clock(), cfg=cfg)
    assert m1.funding == m2.funding == D(3)
    x = w.desk.propose_exit(d["id"], None, False, chat=None)
    approve_run(w, x)
    d = store.get_deal(w.con, d["id"])
    assert d["state"] == DealState.CLOSED
    L = sol_ledger.ledger(w.con, d, fee_rate=W.FEE)
    assert L.funding == D(3) and L.funding_n == 2 and not L.fees_est
    f1 = sol_ledger.final_mark(w.con, d, w.live, now=w.clock())
    f2 = sol_ledger.final_mark(w.con, d, w.live, now=w.clock())
    net = L.network_usdc(D(150))
    assert f1.pnl_now == f2.pnl_now == L.spot_flow + L.perp_flow - L.perp_fee - net + D(3)
    assert f1.flags["accounting_complete"] is True and f1.flags["unit"] == "USDC"


# --- V04, V05: неполнота видна, ревизия итога ------------------------------------------------------------------------
def test_v04_v05_incomplete_history_is_flagged_and_final_is_revised(tmp_path):
    w = make_world(tmp_path)
    w.venue.funding_fail = True
    prop = enter(w)
    d = deal(w, prop)
    m = sol_ledger.mark(w.con, d, w.live, now=w.clock(), cfg=marks.sol_cfg(d))
    assert m.flags["funding_incomplete"] and m.flags["accounting_complete"] is False
    assert m.flags["unit"] == "USDC" and m.flags["sol_px"]["source"] == "test" and m.flags["sol_px"]["unit"] == "USDC"
    approve_run(w, w.desk.propose_exit(d["id"], None, False, chat=None))
    fin = marks.decode(store.last_mark(w.con, d["id"]))
    assert fin["flags"]["final"] and fin["flags"]["accounting_complete"] is False
    snap = cabinet.load_deals(cabinet.open_ro(w.db), now=w.clock())
    (v,) = [x for x in snap["deals"] if x["id"] == d["id"]]
    assert "учёт Hyperliquid догружается" in cabinet.pnl_block(v)
    w.venue.funding_fail = False
    w.venue.add_funding(int(float(d["created"]) * 1000) + 1000, "0.5")     # начисление внутри окна сделки
    marks.run_pass(w.con, w.reg, now=w.clock())
    fin2 = marks.decode(store.last_mark(w.con, d["id"]))
    assert fin2["flags"]["accounting_complete"] is True and fin2["pnl_now"] == fin["pnl_now"] + D("0.5")
    snap = cabinet.load_deals(cabinet.open_ro(w.db), now=w.clock())
    (v,) = [x for x in snap["deals"] if x["id"] == d["id"]]
    assert "догружается" not in cabinet.pnl_block(v) and v["pnl"]["total"] == fin2["pnl_now"]


# --- оценка: котировка выхода на весь остаток, асксы HL, нехватка глубины --------------------------------------------
def test_mark_uses_exit_quote_and_hl_asks_and_depth_gap_gives_none(tmp_path):
    w = make_world(tmp_path)
    prop = enter(w)
    d = deal(w, prop)
    bk = deal_book(w.con, d["id"])
    m = sol_ledger.mark(w.con, d, w.live, now=w.clock(), cfg=marks.sol_cfg(d))
    assert m.pnl_now is not None and m.pnl_exit is not None and m.exit_cost is not None, m.flags
    assert m.flags["exit_path"] == "jupiter_build_v2" and m.flags["px_src"] == "exit_quote"
    assert m.px_dex == D(150_200_000) / D(10) ** 6 / bk.tokens(6) and m.flags["liq"]["mark"] is not None
    w.venue.asks = [[D("0.1672"), D(100)]]                   # асков меньше шорта
    m = sol_ledger.mark(w.con, d, w.live, now=w.clock(), cfg=marks.sol_cfg(d))
    assert m.pnl_exit is None and m.flags["uncovered"] == bk.short - 100
    assert any("асков HL" in e for e in m.flags["errors"])
    ms, done = marks.run_pass(w.con, w.reg, now=w.clock())
    assert done and ms[0].deal_id == d["id"] and ms[0].pnl_exit is None


def test_mark_without_exit_quote_is_unknown_not_zero(tmp_path):
    w = make_world(tmp_path)
    prop = enter(w)
    d = deal(w, prop)
    w.sol.outs["exit"] = {"jupiter_build_v2": 0, "okx_solana_v6": 0}
    m = sol_ledger.mark(w.con, d, w.live, now=w.clock(), cfg=marks.sol_cfg(d))
    assert m.pnl_exit is None and m.pnl_now is None and any("котировки продажи" in e for e in m.flags["errors"])


# --- §11: ручные/чужие исполнения на рынке сделки ------------------------------------------------------------------
def test_foreign_fill_on_deal_market_halts_and_blocks_new_entry(tmp_path):
    w = make_world(tmp_path)
    prop = enter(w)
    d = deal(w, prop)
    w.venue.fills.append(S.fill(1, "5", "0.1670", int(w.clock() * 1000), 9_999_999, fee="0.0006", cloid=None,
                                side="A", hash_="0x" + "ee" * 32))
    chk = sol_flow.check_deal(w.con, d, w.live)
    assert chk.matched is False and "не заявками сделки" in chk.detail
    rep = reconcile.startup(w.con, w.reg, now=w.clock())
    (dr,) = [r for r in rep.deals if r.deal["id"] == d["id"]]
    assert dr.new == DealState.HALTED_MISMATCH
    with pytest.raises(Refused):
        w.desk.propose_profile_entry(entry_cmd(), chat=None)


def test_positions_row_is_deal_book_not_wallet_and_marks_pnl(tmp_path):
    w = make_world(tmp_path)
    prop = enter(w)
    d = deal(w, prop)
    w.sol.ansem += 5_000_000                                  # свои токены владельца в том же кошельке — не сделки
    rows, matched, prob = reconcile.positions(w.con, w.reg, now=w.clock())
    (r,) = rows
    bk = deal_book(w.con, d["id"])
    assert matched is True and r["spot_qty"] == bk.tokens(6) and r["perp_qty"] == -bk.short
    assert r["leg_usd"] is None and r["pnl_now_usd"] is not None and r["pnl_exit_usd"] is not None
    assert r["delta_qty"] is not None and D(0) <= r["delta_qty"] < 1
    rows, _m, _p = reconcile.positions(w.con, w.reg, now=w.clock(), profile="bsc_okx_aster")
    assert rows == []


# --- M02: одна SOL-связка без Aster/EVM; прежняя связка — прежний вызов -----------------------------------------------
def test_m02_trader_legs_sol_only_reads_no_aster_keys(tmp_path, monkeypatch):
    import funding_bot.trade.keys as K
    monkeypatch.setattr(K, "load", lambda *a, **k: pytest.fail("ключи Aster/EVM читать нельзя"))
    p = F.write(tmp_path, F.sol_toml({"profiles.bsc_okx_aster": {"enabled": "false"}}))
    cfg = owner.load(p)
    calls, made = [], {}

    def build(cfg, conns, **kw):
        calls.append(kw)
        return eng.build_runtime(cfg, conns, okx=object(), rpc=object(), aster=TE.PerpPub(), **kw)

    class Fac:
        def __init__(self, loader, conns, *, keys_mode, environ):
            made.update(keys_mode=keys_mode, environ=environ)

        def __call__(self, sim):
            return "SOL-LEGS"
    rt, reg, km, mode, legacy_on = build_trader_legs(cfg, Conns(tmp_path / "t.db"), CfgHolder(), {}, build=build,
                                                     factory=Fac)
    assert legacy_on is False and calls[0]["mode"] == "dry" and rt.keys is None and rt.live is None
    assert km == "live" and mode == "live" and made["keys_mode"] == "live"
    assert reg.for_profile(SOL_HL, False) == "SOL-LEGS" and reg(False) is None and reg(True) is rt.sim


def test_m01_legacy_trader_legs_call_is_unchanged(tmp_path):
    p = tmp_path / "owner.toml"
    p.write_text(TE.live_toml())
    cfg = owner.load(p)
    calls = []
    fake = SimpleNamespace(mode="live", keys=object(), sim="SIM", live="LIVE")

    def build(cfg, conns, **kw):
        calls.append(kw)
        return fake
    rt, reg, km, mode, legacy_on = build_trader_legs(cfg, Conns(tmp_path / "t.db"), CfgHolder(), {"X": "1"},
                                                     build=build)
    assert legacy_on and set(calls[0]) == {"holder", "environ"} and calls[0]["environ"] == {"X": "1"}
    assert reg(False) == "LIVE" and reg(True) == "SIM" and km == "live" and mode == "live" and reg.factories == {}
