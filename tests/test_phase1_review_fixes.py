"""Ревью фазы 1 (13.09): находки M1–M3 (деньги, m ≠ 1) и F1 (откат на версию до фазы 1 с открытой сделкой m ≠ 1).
На каждую — регрессия, падающая без исправления (проверено мутацией). Фейки — из test_trade_engine / test_phase1_units:
перп 1000AIW3USDT (1 контракт = 1000 AIW3) со стаканом ×1000 и моделью маржи Aster (−2019). Без сети и ключей."""
import json, re, subprocess, sys, time
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from funding_bot.trade import engine as eng, planner as P, reconcile, store
from funding_bot.trade.store import DealState, IntentStatus
from funding_bot.trade.types import Book, ClipPlan, InstrumentSpec
from funding_bot.tg import views
from funding_bot.tg.sender import to_plain

import test_phase1_exit_resume as tr
import test_phase1_instrument as t11
import test_phase1_units as t12
import test_trade_engine as fx
import test_trade_planner as tp

M = t12.M
flat = fx.flat
ROOT = Path(__file__).resolve().parents[1]


def _n_intents(e) -> int:
    return e.con.execute("SELECT count(*) FROM intents").fetchone()[0]


# ==== M3: inst_json записан, но не читается — m не известен, а не «1» ====================================================
def _broken(e, did):
    """Спецификация сделки не читается: схема будущей версии (откат на .prev) или ручная правка."""
    raw = json.loads(store.get_deal(e.con, did)["inst_json"])
    raw["schema"] = 2
    e.con.execute("UPDATE deals SET inst_json=? WHERE id=?", (json.dumps(raw), did))


def test_unreadable_inst_m1000_no_undo_no_rehedge_and_full_exit_closes(tmp_path):
    """Сценарий ревьюера: m = 1000, 2 444.6 токена, шорт 2 контр. По m = 1 «откат» продал бы 2 442 захеджированных
    токена (голый шорт −2 000), а полный выход был закрыт «ноги не ровно». Теперь: m не известен — дохедж, откат и
    частичный выход отказаны, сверка и «позиции» не советуют их, полный выход продаёт весь спот и откупает весь шорт."""
    e = t12.mult_env(tmp_path)
    did = t12.enter(e).deal_id
    _broken(e, did)
    inst = eng.deal_instrument(e.con, store.get_deal(e.con, did))
    assert inst.source == "unreadable" and not eng.m_known(inst) and "не читается" in inst.why
    bk = eng.deal_book(e.con, did)
    assert bk.known and not bk.m_known and bk.delta(18) is None and bk.hedged(18, fx.FILT.step) is None
    before, n = fx.sends(e), _n_intents(e)
    for kind in ("rehedge", "undo"):
        with pytest.raises(eng.Refused) as ei:
            e.desk.propose_fix(kind, did, chat=fx.OWNER)
        assert f"только «выход {did}» целиком" in flat(ei.value.html), ei.value.html
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_exit(did, D(50), False, chat=fx.OWNER)
    assert f"«выход {did}» целиком" in ei.value.html
    assert fx.sends(e) == before and _n_intents(e) == n
    # сверка: совпало с правдой, без советов «дохедж»/«откат» и без ложной голой ноги
    chk = reconcile.check_deal(e.con, store.get_deal(e.con, did), e.legs_live)
    assert chk.matched is True and chk.hedged is None and chk.m == 0
    assert f"множитель контракта не известен — только «выход {did}» целиком" in chk.detail, chk.detail
    assert "дохедж" not in chk.detail and "откат" not in chk.detail and "шорт 2 контр. —" in flat(chk.detail)
    rows, matched, _p = reconcile.positions(e.con, e.legs, now=time.time())
    assert matched is True and rows[0]["delta_qty"] is None and rows[0]["delta_usd"] is None and rows[0]["m_unknown"]
    txt = flat(views.positions([views.PositionView(**r) for r in rows], ts=time.time(), matched=matched))
    assert f"множитель контракта не известен — только <code>выход {did}</code> целиком" in txt, txt
    assert "без хеджа" not in txt
    # полный выход: весь спот одним свопом, весь шорт reduceOnly — CLOSED, ничего не осталось
    x = e.desk.propose_exit(did, None, False, chat=fx.OWNER)
    t = flat(x.html)
    assert "Выход AIW3 · всё" in t and "Продать спот 2 445 · откупить шорт 2 контр." in t and "(= " not in t, t
    assert "Множитель контракта не известен — продаю весь спот и откупаю весь шорт" in t
    n0 = len(e.perp.calls)
    fx.run_approved(e, x)
    assert [(c["side"], c["qty"], c["ro"]) for c in e.perp.calls[n0:]] == [("BUY", 2, True)]
    assert e.perp.pos == 0 and e.spot.bal[fx.TOKEN] == 0
    assert store.get_deal(e.con, did)["state"] == DealState.CLOSED
    assert "закрыта" in e.hooks.reports[-1] and fx.html_ok(e.hooks.reports[-1])


def test_unreadable_inst_perp_only_exit_then_full_exit_closes(tmp_path):
    """Безопасный путь без m: «выход перп» (весь шорт) → текст зовёт «выход», не «откат» → «выход» продаёт спот."""
    e = t12.mult_env(tmp_path)
    did = t12.enter(e).deal_id
    _broken(e, did)
    fx.run_approved(e, e.desk.propose_exit(did, None, True, chat=fx.OWNER))
    assert e.perp.pos == 0 and store.get_deal(e.con, did)["state"] == DealState.PAUSED
    rep = flat(e.hooks.reports[-1])
    assert "Откуплено 2 контр. = " in rep and f"<code>выход {did}</code>" in rep and "откат" not in rep, rep
    with pytest.raises(eng.Refused):
        e.desk.propose_fix("undo", did, chat=fx.OWNER)
    fx.run_approved(e, e.desk.propose_exit(did, None, False, chat=fx.OWNER))
    assert store.get_deal(e.con, did)["state"] == DealState.CLOSED and e.spot.bal[fx.TOKEN] == 0


@pytest.mark.parametrize("variant,source,known", [("ratio", "legacy:m_unknown", False),
                                                  ("name", "legacy:m_unknown", False),
                                                  ("naked", "legacy:unverified", True)])
def test_legacy_fallback_that_found_a_multiplier_does_not_give_m1(tmp_path, variant, source, known):
    """Запасной путь сам нашёл множитель в имени или расхождение цены — m не известен; «нечем сверить» (шорта нет) —
    по-прежнему m = 1 журнала прежнего кода, неподтверждённый."""
    e, p = t11._partial_entry(tmp_path)
    t11._legacy(e, p.deal_id, variant)
    inst = eng.legacy_instrument(e.con, store.get_deal(e.con, p.deal_id))
    assert inst.source == source and not inst.verified and eng.m_known(inst) is known
    assert (eng.deal_book(e.con, p.deal_id).delta(18) is None) is (not known)
    if not known:
        with pytest.raises(eng.Refused) as ei:
            e.desk.propose_fix("undo", p.deal_id, chat=fx.OWNER)
        assert "множитель контракта не известен" in flat(ei.value.html).lower()


def test_approved_undo_and_auto_unwind_send_nothing_when_m_becomes_unknown(tmp_path):
    """Страховка исполнителя: откат одобрен при известном m, спецификация испортилась до кнопки (отпечаток m = 1 тот же) —
    ничего не продано, пауза inst_unverified; авто-откат по сроку тоже ничего не предлагает."""
    e = fx.live_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(200), chat=fx.OWNER)
    fx.run_approved(e, p)
    did = p.deal_id
    t11._shrink_short(e, did, 10)                            # голый лонг 10 AIW3: откат продаст 10
    und = e.desk.propose_fix("undo", did, chat=fx.OWNER)
    _broken(e, did)
    before, appr = fx.sends(e), list(e.spot.approvals)
    fx.run_approved(e, und)
    assert fx.sends(e) == before and e.spot.approvals == appr
    assert store.get_intent(e.con, und.intent_id)["status"] == IntentStatus.FAILED
    assert store.get_deal(e.con, did)["reason"] == "inst_unverified"
    e.engine._unwind_due[did] = 0.0
    n = _n_intents(e)
    e.engine._check_unwinds()
    assert fx.sends(e) == before and _n_intents(e) == n


def test_startup_says_only_full_exit_for_unknown_m(tmp_path):
    e, tg, sender, bot, _fe = fx.bot_env(tmp_path)
    p = e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(500), chat=fx.OWNER)
    fx.run_approved(e, p)
    _broken(e, p.deal_id)
    bot.startup()
    sender.drain()
    text = flat([c[2] for c in tg.calls if c[0] == "send"][-1])
    assert f"Множитель контракта не известен — только <code>выход {p.deal_id}</code> целиком" in text, text
    assert "дохедж" not in text and "откат" not in text and "⚠️" in text and "✅" not in text


def test_views_for_unknown_m():
    assert views.contracts(D(2), D(0)) == "2 контр." and views.contracts(D(-2), D(0), True) == "−2 контр."
    assert views.contracts(D(2), D(1)) == "2" and views.contracts(D(2), None, coin="AIW3") == "2 AIW3"   # m = 1 прежние
    h = flat(views.halt(views.HaltView(intent_id="X1", kind="exit", coin="AIW3", reason="тест", deal_id="D1",
                                       perp_pos=D(-2), wallet_tokens=D(10), m=D(0), step=D(1))))
    assert "шорт −2 контр." in h and "Множитель контракта не известен — только <code>выход D1</code> целиком" in h, h
    r = flat(views.restart(views.RestartView(intent_id="X1", kind="exit", deal_id="D1", matched=True, coin="AIW3",
                                             hedged=None, m=D(0), step=D(1))))
    assert "только <code>выход D1</code> целиком" in r and "шаг перпа не прочитан" not in r, r
    c = views.restart_check("AIW3", "D1", True, "сверено", "OPEN", m=D(0))
    assert c.startswith("⚠️") and "только <code>выход D1</code> целиком" in c
    assert views.restart_check("AIW3", "D1", True, "сверено", "OPEN", m=D(1)).startswith("✅")


# ==== M1: предпроверка маржи при m ≠ 1 и нескольких клипах — контракты с переносом, как у исполнителя ====================
def _scaled(e, k: D) -> None:
    """Перп дороже DEX в k раз (положительный базис)."""
    b = e.perp.b
    e.perp.b = Book(tuple((px * k, q) for px, q in b.bids), tuple((px * k, q) for px, q in b.asks), 0.0)
    e.perp.funding = lambda s, _p=e.perp.b.bids[0][0]: (_p, D("0.0005"), 1_757_700_000_000)


def test_multiclip_m1000_margin_check_counts_contracts_like_the_executor(tmp_path):
    """Ревьюер: 4 клипа по 1 527.9 токена — план [1, 1, 1, 1] (floor на клип), исполнитель SELL 1, 2, 1, 2 (перенос);
    при марже 251 и базисе +3 % последний клип — −2019 после свопа. Теперь план считает [1, 2, 1, 2] и отказывает до
    свопа; маржи 255 хватает — заявки ровно по плану, без отказа биржи."""
    (tmp_path / "a").mkdir()
    e = t12.mult_env(tmp_path / "a", margin=D(251), clip="80")
    _scaled(e, D("1.03"))
    with pytest.raises(eng.Refused) as ei:
        e.desk.propose_entry("AIW3", "okx·bsc", "aster", D(250), chat=fx.OWNER)
    assert "Маржа Aster 251.00 — меньше 252.00 USDT" in flat(ei.value.html), ei.value.html
    assert fx.sends(e) == (0, 0) and e.spot.approvals == []
    (tmp_path / "b").mkdir()
    ok = t12.mult_env(tmp_path / "b", margin=D(255), clip="80")
    _scaled(ok, D("1.03"))
    p = ok.desk.propose_entry("AIW3", "okx·bsc", "aster", D(250), chat=fx.OWNER)
    per_clip = [sum((q for q, _ in c.children), D(0)) for c in p.plan.clips]
    assert per_clip == [1, 2, 1, 2] and p.plan.est["contracts"] == 6
    fx.run_approved(ok, p)
    assert [c["qty"] for c in ok.perp.calls if c["side"] == "SELL"] == per_clip and ok.perp.rejected == 0
    t12.assert_even(ok, p.deal_id)


def test_margin_insurance_counts_all_plan_tokens_in_one_go(tmp_path):
    """Страховка предпроверки (M1): план, чьи дочерние занижены (floor на клип без переноса — прежний расчёт, carry0
    не передан), всё равно просит маржу на floor(Σ токенов плана / m) контрактов по кэпу лучшей дочерней."""
    e = t12.mult_env(tmp_path, clip="80")                      # маржи 1 000 хватает — план строится
    _scaled(e, D("1.03"))
    plan, ctx = e.desk.plan_entry("AIW3", "okx·bsc", "aster", D(250), sim=False)
    assert ctx["notes"] == []
    e.perp.margin = D(251)
    low = P.plan(deal_id="", kind="entry", coin="AIW3", spot="okx·bsc", perp="aster", symbol=t12.MSYM, leg_usd=D(250),
                 total_in_units=250 * fx.E18, dec_in=18, calib=eng._calib_from(plan.inputs["calib"]), mkt=ctx["mkt"],
                 lim=P.limits_from_owner(ctx["cfg"], "aster", "bsc"), now=time.time(), units_per_contract=D(M))
    assert [sum((q for q, _ in c.children), D(0)) for c in low.clips] == [1, 1, 1, 1]
    assert sum((q * cap for c in low.clips for q, cap in c.children), D(0)) < 251
    notes = e.desk._entry_live_notes(ctx["cfg"], e.legs_live, ctx["pair"], ctx["bal"], ctx["total"], D(250),
                                     ctx["sdec"], low)
    assert any(n.startswith("маржа Aster 251.00 — меньше 252.00") for n in map(flat, notes)), notes


def test_clip_contracts_carry_like_the_executor():
    toks = [D("1527.9")] * 4
    assert P.clip_contracts(toks, D(M), D(1), "entry") == [1, 2, 1, 2]
    assert P.clip_contracts(toks, D(M), D(1), "entry", D(500)) == [2, 1, 2, 1]      # «продолжить»: перенос сделки
    assert P.clip_contracts([D(1467)], D(M), D(1), "exit", D("422.5")) == [2]      # ceil((1 467 − 422.5)/1000)
    assert P.clip_contracts([D(400), D(1067)], D(M), D(1), "exit", D("422.5")) == [0, 2]
    # m = 1: перенос плану не передаётся — план побайтно прежний
    kw = dict(deal_id="E7K2", kind="entry", coin="AIW3", spot="okx·bsc", perp="aster", symbol="AIW3USDT",
              leg_usd=D(500), total_in_units=500 * fx.E18, dec_in=18, calib=tp._cal("entry"), mkt=tp._mkt(),
              lim=P.Limits(clips_max=10, clip_max_usd="auto"), r=D(0), now=1000.0, units_per_contract=D(1))
    assert store.jdump(P.plan(**kw)) == store.jdump(P.plan(**kw, carry0=D(7)))


# ==== M2: частичный выход при m ≠ 1 — контракты как у исполнителя; остаток меньше контракта — полный выход =============
def test_partial_exit_that_would_buy_the_whole_short_is_a_full_exit(tmp_path):
    """Ревьюер: 2 444.6 токена, шорт 2, «выход 60» (1 467 токенов): план «1 контр.», исполнитель BUY 2 — CLOSED с 977.6
    токена вне учёта и итогом −40 $. Теперь это полный выход: план и шапка так и называют, продаётся весь спот."""
    e = t12.mult_env(tmp_path)
    did = t12.enter(e).deal_id
    x = e.desk.propose_exit(did, D(60), False, chat=fx.OWNER)
    sp = t11._spec_of(e.con, x.intent_id)
    assert sp["all"] is True and sp["units"] == eng.deal_book(e.con, did).tokens_raw
    t = flat(x.html)
    assert "Выход AIW3 · всё" in t and "Продать спот 2 445 · откупить шорт 2 контр. (= 2 000 токенов)" in t, t
    assert "меньше 1 контр. (= 1 000 токенов) — выход всей сделки" in t
    fx.run_approved(e, x)
    d, bk = store.get_deal(e.con, did), eng.deal_book(e.con, did)
    assert d["state"] == DealState.CLOSED and bk.tokens_raw == 0 and e.spot.bal[fx.TOKEN] == 0 and e.perp.pos == 0
    total = re.search(r"закрыта · ([+−][\d.]+) \$", flat(e.hooks.reports[-1]))
    assert total and abs(D(total.group(1).replace("−", "-"))) < 1, e.hooks.reports[-1]


def test_partial_exit_plan_shows_the_contracts_the_executor_buys(tmp_path):
    """Ревьюер: $140, «выход 60» — план «1 контр.», факт BUY 2. Теперь план — 2 (ceil((1 467 − δ0)/m)), факт — 2."""
    e = t12.mult_env(tmp_path)
    did = t12.enter(e, 140).deal_id
    x = e.desk.propose_exit(did, D(60), False, chat=fx.OWNER)
    assert "откупить шорт 2 контр. (= 2 000 токенов)" in flat(x.html) and x.plan.est["contracts"] == 2
    assert t11._spec_of(e.con, x.intent_id)["all"] is False
    n0 = len(e.perp.calls)
    fx.run_approved(e, x)
    assert sum((c["qty"] for c in e.perp.calls[n0:] if c["side"] == "BUY"), D(0)) == 2
    assert store.get_deal(e.con, did)["state"] == DealState.OPEN
    t12.assert_even(e, did)


@pytest.mark.parametrize("second_layer", [False, True])
def test_approved_partial_exit_is_not_turned_into_a_full_one_at_the_button(tmp_path, monkeypatch, second_layer):
    """После одобрения частичного цель изменена так, что исполнитель откупил бы весь шорт (правкой spec):
    перекотировка отказывает, а если её обойти — сам исполнитель, до любой отправки."""
    e = t12.mult_env(tmp_path)
    did = t12.enter(e).deal_id
    x = e.desk.propose_exit(did, D(50), False, chat=fx.OWNER)             # 1 222 токена — частичный
    assert t11._spec_of(e.con, x.intent_id)["all"] is False
    big = int(D(1500) * fx.E18)
    assert store.approve_intent(e.con,x.intent_id,x.nonce)  # second/executor-layer probe after approval
    tr._set_spec(e, x.intent_id, units=big)
    if second_layer:
        fake = replace(x.plan, clips=[ClipPlan(seq=1, dex_in_units=big, children=x.plan.clips[0].children)])
        monkeypatch.setattr(e.desk, "replan", lambda it, deal: fake)
    before, appr = fx.sends(e), list(e.spot.approvals)
    e.engine.execute(x.intent_id)
    assert fx.sends(e) == before and e.spot.approvals == appr
    assert store.get_intent(e.con, x.intent_id)["status"] == IntentStatus.FAILED
    if second_layer:
        assert store.get_deal(e.con, did)["reason"] == "changed"
    else:
        assert "нужен новый план" in e.hooks.reports[-1]


# ==== F1: откат на версию до фазы 1 с открытой сделкой m ≠ 1 ===============================================================
def _dest(tmp_path, deals: list[tuple[str, str | None]] | None, garbage: bool = False) -> Path:
    """Боевая папка: runtime/trade.db со сделками (состояние, inst_json) и .venv/bin/python (обёртка над текущим)."""
    dest = tmp_path / "dest"
    (dest / "runtime").mkdir(parents=True)
    (dest / ".venv" / "bin").mkdir(parents=True)
    py = dest / ".venv" / "bin" / "python"
    py.write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
    py.chmod(0o755)
    db = dest / "runtime" / "trade.db"
    if garbage:
        db.write_bytes(b"not a database" * 100)
    elif deals is not None:
        con = store.connect(db)
        for i, (state, inst) in enumerate(deals):
            did = store.create_deal(con, coin=f"C{i}", chain="bsc", token=f"0x{i:040x}", token_dec=18,
                                    perp_venue="aster", symbol=f"C{i}USDT", leg_usd=D(100), owner_json="{}", sim=False)
            con.execute("UPDATE deals SET state=?, inst_json=? WHERE id=?", (state, inst, did))
        con.close()
    return dest


def _target(tmp_path, phase1: bool) -> Path:
    t = tmp_path / ("new" if phase1 else "old")
    (t / "src" / "funding_bot" / "trade").mkdir(parents=True)
    (t / "src" / "funding_bot" / "trade" / "store.py").write_text("inst_json\n" if phase1 else "# до фазы 1\n")
    return t


def _gate(dest: Path, target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", 'source "$1"; DEST="$2"; mult_gate "$3"; echo GATE-PASSED', "_",
                           str(ROOT / "deploy" / "remote_lib.sh"), str(dest), str(target)],
                          capture_output=True, text=True)


def _inst(m: str) -> str:
    return InstrumentSpec(chain="bsc", token="0x" + "a1" * 20, token_dec=18, perp_venue="aster",
                          perp_symbol="1000AIW3USDT" if m != "1" else "AIW3USDT", units_per_contract=D(m)).to_json()


@pytest.mark.parametrize("deals,garbage,blocked", [
    ([("OPEN", _inst("1000"))], False, "открыта сделка с множителем контракта (1)"),
    ([("PAUSED", _inst("1000")), ("OPEN", _inst("1"))], False, "(1)"),
    ([("OPEN", '{"schema": 2')], False, "(1)"),                                  # не читается — m не известен
    ([("OPEN", _inst("1")), ("OPEN", None)], False, None),                       # DQA9Q (m = 1) и сделка до фазы 1
    ([("CLOSED", _inst("1000")), ("DRAFT", _inst("1000"))], False, None),
    (None, True, "trade.db не прочитана"),
])
def test_rollback_gate_to_version_before_phase1(tmp_path, deals, garbage, blocked):
    dest = _dest(tmp_path, deals, garbage)
    old = _gate(dest, _target(tmp_path, phase1=False))
    if blocked:
        assert old.returncode == 1 and blocked in old.stdout and "GATE-PASSED" not in old.stdout, old
    else:
        assert old.returncode == 0 and "GATE-PASSED" in old.stdout, old
    new = _gate(dest, _target(tmp_path, phase1=True))                            # версия фазы 1 — ворот нет
    assert new.returncode == 0 and "GATE-PASSED" in new.stdout, new


def test_rollback_gate_without_db_passes_and_scripts_call_it_before_rsync(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    assert _gate(dest, _target(tmp_path, phase1=False)).returncode == 0
    d = ROOT / "deploy"
    sw, rb = (d / "remote_switch.sh").read_text(), (d / "remote_rollback.sh").read_text()
    assert 0 < sw.index('mult_gate "$NEXT"') < sw.index('rsync -a --delete "${EXCL[@]}" "$NEXT/" "$DEST/"')
    assert 0 < rb.index('mult_gate "$PREV"') < rb.index('rsync -a --delete "${EXCL[@]}" "$PREV/" "$DEST/"')
    ex = (d / "owner.toml.example").read_text()
    assert "# allow_contract_multiplier = true" in ex and "Не включать, пока .prev" in ex
    assert not re.search(r"(?m)^\s*allow_contract_multiplier\s*=", ex), "ключ в примере — только закомментирован"
