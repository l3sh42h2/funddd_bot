"""Шаг C4 «выпуск» связки SOL×HL: зависимости (pyproject extra sol + deploy/requirements.lock), ставка lock до тестов в
чистый venv .next и в боевой venv на переключении из тех же колёс (M09), ворота выката и отката по сделкам и
неразрешённым попыткам Solana/HL (M06, M10), readonly doctor в проверке после рестарта, TimeoutStopSec трейдера под срок
blockhash Solana и разбор HL. Скрипты выката гоняются bash'ем на поддельной боевой папке — сервер не нужен."""
from __future__ import annotations
import hashlib, os, re, shutil, subprocess, sys, tomllib
from decimal import Decimal as D
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from funding_bot.tg import bot as tg_bot
from funding_bot.trade import hyperliquid_trade as H
from funding_bot.trade import sol_exec, store
from funding_bot.trade.solana import journal as J
from funding_bot.trade.solana import rpc as solrpc

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
LIB = DEPLOY / "remote_lib.sh"
LOCK = DEPLOY / "requirements.lock"
SOL_PINS = {"solders": "0.29.0", "base58": "2.1.1", "hyperliquid-python-sdk": "0.24.0", "msgpack": "1.2.2",
            "websocket-client": "1.9.2", "eth-utils": "5.3.1", "pycryptodome": "3.23.0", "eth-account": "0.13.7"}


def _lock() -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in LOCK.read_text().splitlines():
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        m = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.\-]*)==([0-9][A-Za-z0-9_.+!\-]*)", ln)
        assert m, f"в lock только имя==версия: {ln!r}"
        name = canonicalize_name(m[1])
        assert name not in out, f"дубль в lock: {name}"
        out[name] = m[2]
    return out


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


# ==== зависимости ================================================================================================
def test_sol_extra_exact_pins_and_same_versions_in_lock():
    p, lock = _pyproject(), _lock()
    sol = {canonicalize_name(Requirement(r).name): Requirement(r) for r in p["optional-dependencies"]["sol"]}
    assert set(sol) == {canonicalize_name(n) for n in SOL_PINS}
    for name, want in SOL_PINS.items():
        r = sol[canonicalize_name(name)]
        assert str(r.specifier) == f"=={want}", f"{name}: только точная версия"
        assert lock[canonicalize_name(name)] == want, f"{name}: pyproject и lock расходятся"
    assert "==0.13.7" in p["optional-dependencies"]["trade"][0] and lock["eth-account"] == "0.13.7"
    for r in map(Requirement, p["dependencies"] + p["optional-dependencies"]["dev"]):
        assert r.specifier.contains(lock[canonicalize_name(r.name)]), r


def test_lock_is_closed_and_matches_this_environment():
    """Полное замыкание: всё, что требуют корни (runtime + trade + sol + dev) и их зависимости (с extras, маркерами
    ЭТОГО окружения), есть в lock с подходящей версией, и стоит ровно она. На сервере тест идёт в чистом venv .next
    (Python 3.11 Linux) — там же проверяется, что маркеры не добавили пакета сверх lock."""
    p, lock = _pyproject(), _lock()
    roots = [Requirement(r) for r in p["dependencies"]] + [
        Requirement(r) for k in ("trade", "sol", "dev") for r in p["optional-dependencies"][k]]
    seen: set[tuple[str, frozenset]] = set()
    todo = [(r, frozenset()) for r in roots]
    problems: list[str] = []
    while todo:
        req, parent_extras = todo.pop()
        if req.marker is not None and not any(req.marker.evaluate({"extra": e}) for e in ("", *parent_extras)):
            continue
        name = canonicalize_name(req.name)
        if name not in lock:
            problems.append(f"{req} — нет в lock")
            continue
        if not req.specifier.contains(lock[name], prereleases=True):
            problems.append(f"{req} — lock даёт {lock[name]}")
        try:
            have = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name} не установлен")
            continue
        if have != lock[name]:
            problems.append(f"{name}: стоит {have}, в lock {lock[name]}")
        key = (name, frozenset(req.extras))
        if key in seen:
            continue
        seen.add(key)
        for dep in metadata.requires(req.name) or []:
            todo.append((Requirement(dep), frozenset(req.extras)))
    assert not problems, problems
    assert {"solders", "hyperliquid-python-sdk", "base58", "pycryptodome", "msgpack", "websocket-client"} <= \
        {n for n, _ in seen}


def test_lock_hash_policy_documented_and_enforced_by_scripts():
    head = LOCK.read_text().split("\n\n", 1)[0]
    assert "Хеши: при выкате" in head and "--no-deps" in head and "pip check" in head
    lib, t = LIB.read_text(), (DEPLOY / "remote_test.sh").read_text()
    assert "sha256sum -- *.whl > SHA256SUMS" in t and "sha256sum -c --quiet --strict SHA256SUMS" in lib
    assert 'cmp -s "$lockf" "$WHEELS/requirements.lock"' in lib and "--no-index" in lib and "--no-deps" in lib


# ==== remote_test.sh / remote_switch.sh / remote_rollback.sh: порядок шагов ====================================
def _order(text: str, parts: list[str]) -> list[int]:
    idx = [text.index(s) for s in parts]
    assert idx == sorted(idx), [p for p, _ in sorted(zip(parts, idx), key=lambda x: x[1])]
    return idx


def test_remote_test_installs_lock_into_next_venv_before_tests():
    t = (DEPLOY / "remote_test.sh").read_text()
    _order(t, ['need_lock "${1:-}"', "--only-binary=:all: --no-deps -d \"$WHEELS\"", "*.whl > SHA256SUMS",
               'rm -rf "$NEXT/.venv"', '-m venv "$NEXT/.venv"', 'install_lock "$NEXT/.venv" "$LOCKF"',
               'PY="$NEXT/.venv/bin/python"', "startswith(root)", '"$PY" -m pytest tests'])
    # боевой venv до тестов получает только setuptools (как раньше); lock в него — на переключении
    assert not re.search(r'install_lock "\$DEST', t) and "'requests>=2.28'" not in t


def test_remote_switch_gates_then_lock_then_code_then_import_check():
    sw = (DEPLOY / "remote_switch.sh").read_text()
    _order(sw, ['mult_gate "$NEXT"', 'sol_gate "$NEXT"', "trader_gate\ninstall_lock",
                'install_lock "$DEST/.venv" "$NEXT/$LOCKFILE"', 'trader_gate\nrsync -a --delete "${EXCL[@]}" "$NEXT/" "$DEST/"',
                "ensure_installed\n.venv/bin/python -c", "systemctl restart funding_bot-collector",
                "trader_gate\n# без TG_BOT_TOKEN", "systemctl restart funding_bot-trader"])
    body = sw.split("trader_gate() {", 1)[1].split("\n}", 1)[0]
    assert "trader_busy" in body and "trader_idle" in body


def test_rollback_gates_sol_and_restarts_trader_only_when_idle():
    rb = (DEPLOY / "remote_rollback.sh").read_text()
    _order(rb, ['mult_gate "$PREV"', 'sol_gate "$PREV"', 'rsync -a --delete "${EXCL[@]}" "$PREV/" "$DEST/"',
                "systemctl restart funding_bot-collector funding_bot-web", "if why=$(trader_idle); then",
                "deploy/funding_bot-trader.service", "systemctl restart funding_bot-trader"])
    assert "trade.db" not in rb.split("rsync", 1)[1].replace("runtime/trade.db не трогается", "")


@pytest.mark.parametrize("script,pattern", [("remote_switch.sh", r"\.venv/bin/python -c '([^']*)'")])
def test_switch_import_check_imports_here(script, pattern):
    code = re.search(pattern, (DEPLOY / script).read_text())[1]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def test_remote_test_import_check_runs_in_this_tree():
    if not sys.prefix.startswith(str(ROOT) + os.sep):
        pytest.skip("тесты идут не venv этого дерева — проверка пути импорта здесь неприменима")
    t = (DEPLOY / "remote_test.sh").read_text()
    code = t.split("<<'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-", str(ROOT)], input=code, capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "solders 0.29.0" in r.stdout and "hyperliquid-python-sdk 0.24.0" in r.stdout, r
    r = subprocess.run([sys.executable, "-", "/nonexistent-next"], input=code, capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "импорт не из .next" in r.stderr


# ==== install_lock: те же колёса, тот же lock ===================================================================
def _stub_venv(tmp_path: Path) -> tuple[Path, Path]:
    venv, log = tmp_path / "venv", tmp_path / "pip.log"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text(f"#!/bin/sh\necho \"$*\" >> '{log}'\n")
    py.chmod(0o755)
    return venv, log


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="нет sha256sum (GNU coreutils) — есть на сервере")
@pytest.mark.parametrize("tamper", [None, "wheel", "lock"])
def test_install_lock_uses_only_tested_wheels(tmp_path, tamper):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "a-1-py3-none-any.whl").write_bytes(b"wheel")
    (wheels / "SHA256SUMS").write_text(f"{hashlib.sha256(b'wheel').hexdigest()}  a-1-py3-none-any.whl\n")
    lockf = tmp_path / "requirements.lock"
    lockf.write_text("a==1\n")
    shutil.copy(lockf, wheels / "requirements.lock")
    if tamper == "wheel":
        (wheels / "a-1-py3-none-any.whl").write_bytes(b"other")
    if tamper == "lock":
        lockf.write_text("a==2\n")
    venv, log = _stub_venv(tmp_path)
    r = subprocess.run(["bash", "-c", 'source "$1"; WHEELS="$2"; install_lock "$3" "$4"; echo LOCK-OK', "_",
                        str(LIB), str(wheels), str(venv), str(lockf)], capture_output=True, text=True)
    if tamper is None:
        assert r.returncode == 0 and "LOCK-OK" in r.stdout, r
        calls = log.read_text().splitlines()
        assert calls[0].startswith("-m pip -q install --no-index --find-links") and "--no-deps -r" in calls[0]
        assert calls[1] == "-m pip check"
    else:
        assert r.returncode == 1 and "LOCK-OK" not in r.stdout and not log.exists(), r
        assert ("изменились" if tamper == "wheel" else "не тот") in r.stdout


# ==== trade_state / trader_idle / sol_gate на поддельной боевой папке ============================================
def _dest(tmp_path: Path) -> tuple[Path, Path]:
    dest = tmp_path / "dest"
    (dest / "runtime").mkdir(parents=True)
    (dest / ".venv" / "bin").mkdir(parents=True)
    py = dest / ".venv" / "bin" / "python"
    py.write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n")
    py.chmod(0o755)
    return dest, dest / "runtime" / "trade.db"


def _target(tmp_path: Path, schema: int | None) -> Path:
    t = tmp_path / f"target_{schema}"
    (t / "src" / "funding_bot" / "trade").mkdir(parents=True)
    body = "inst_json\n" + (f"SCHEMA_VERSION = {schema}\nMIN_READER = {schema}\n" if schema is not None else "")
    (t / "src" / "funding_bot" / "trade" / "store.py").write_text(body)
    return t


def _bash(snippet: str, dest: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "$1"; DEST="$2"; {snippet}', "_", str(LIB), str(dest), *args],
                          capture_output=True, text=True)


def _state(dest: Path) -> list[int]:
    r = _bash("trade_state", dest)
    assert r.returncode == 0, r.stderr
    return [int(x) for x in r.stdout.split()]


def _gate(dest: Path, target: Path) -> subprocess.CompletedProcess:
    return _bash('sol_gate "$3"; echo GATE-PASSED', dest, str(target))


def _idle(dest: Path) -> str:
    r = _bash('if why=$(trader_idle); then echo IDLE; else echo "BUSY: $why"; fi', dest)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _bsc_deal(con) -> str:
    """DQA9Q-подобная открытая сделка BSC/Aster с незакрытой заявкой Aster — в ворота SOL/HL не входит."""
    did = store.create_deal(con, coin="AIW3", chain="bsc", token="0x" + "a1" * 20, token_dec=18, perp_venue="aster",
                            symbol="AIW3USDT", leg_usd=D(500), owner_json="{}", sim=False)
    con.execute("UPDATE deals SET state='OPEN' WHERE id=?", (did,))
    con.execute("INSERT INTO perp_orders(clip_id, client_id, venue, symbol, side, state) "
                "VALUES (1, 'bsc-1', 'aster', 'AIW3USDT', 'SELL', 'UNKNOWN')")
    return did


def _sol_deal(con, state: str = "OPEN") -> None:
    con.execute("INSERT INTO deals(id, created, state, coin, chain, token, token_dec, perp_venue, symbol, leg_usd, "
                "owner_json, sim, perp_scope) VALUES ('SOL1', 0, ?, 'ANSEM', 'solana', "
                "'9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump', 6, 'hyperliquid', 'para:ANSEM', '150', '{}', 0, "
                "'hyperliquid:mainnet:0xm:0xa:para:para:ANSEM')", (state,))


_N = [0]


def _sol_attempt(con, state: str, receipt: str | None = None) -> str:
    _N[0] += 1
    n = _N[0]
    aid, sig = f"att{n}", f"sig{n}"
    con.execute("INSERT INTO sol_tx_attempts(attempt_id, network, wallet, logical_action_id, provider, path, "
                "payload_kind, message_hash, recent_blockhash, last_valid_block_height, lvbh_exact, plan_json, "
                "signature, state, created, updated) VALUES (?, 'mainnet', ?, ?, 'jupiter', 'build', 'v0', 'mh', "
                "'bh', 100, 1, '{}', ?, ?, 0, 0)",
                (aid, f"wallet{n}", f"act{n}", None if state == "VALIDATED" else sig, state))
    if receipt is not None:
        con.execute("INSERT INTO sol_receipts(network, signature, logical_leg, attempt_id, slot, ok, fee_lamports, "
                    "flows_json, receipt_hash, commitment, first_seen, updated) VALUES ('mainnet', ?, 'swap', ?, 1, "
                    "1, '5000', '{}', 'rh', ?, 0, 0)", (sig, aid, receipt))
    return aid


def _hl_attempt(con, state: str) -> None:
    _N[0] += 1
    con.execute("INSERT INTO hl_order_attempts(client_id, kind, network, master, account, signer, nonce, action_json, "
                "action_hash, state) VALUES (?, 'order', 'mainnet', '0xm', '0xa', '0xs', ?, '{}', 'ah', ?)",
                (f"hl{_N[0]}", _N[0], state))


def test_trade_state_sol_count_equals_journal_unresolved_for_every_state(tmp_path):
    dest, db = _dest(tmp_path)
    con = store.connect(db)
    for s in J.S:
        if s in J.FINALIZED:
            for rc in (None, "confirmed", "finalized"):
                _sol_attempt(con, str(s), rc)
        else:
            _sol_attempt(con, str(s))
    want = len(J.unresolved(con))
    assert want > 0 and not any(r["state"] == "VALIDATED" for r in J.unresolved(con))
    con.close()
    intents, sol, hl, deals, min_reader = _state(dest)
    assert (intents, sol, hl, deals) == (0, want, 0, 0) and min_reader == store.MIN_READER


def test_trade_state_hl_counts_adapter_open_states_and_hl_orders(tmp_path):
    dest, db = _dest(tmp_path)
    con = store.connect(db)
    states = ["PREPARED", "SIGNED", "UNKNOWN", "FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED", "NOT_FOUND",
              "NOT_SENT"]
    for s in states:
        _hl_attempt(con, s)
    for i, s in enumerate(("INTENT", "SENT", "UNKNOWN", "FILLED", "NOT_PLACED")):
        con.execute("INSERT INTO perp_orders(clip_id, client_id, venue, symbol, side, state) VALUES (1, ?, "
                    "'hyperliquid', 'para:ANSEM', 'SELL', ?)", (f"hlo{i}", s))
    con.close()
    assert _state(dest)[2] == sum(s in H.OPEN_STATES for s in states) + 2       # + SENT и UNKNOWN заявки HL


def test_bsc_deal_dqa9q_does_not_block_anything(tmp_path):
    dest, db = _dest(tmp_path)
    con = store.connect(db)
    _bsc_deal(con)
    con.close()
    assert _state(dest) == [0, 0, 0, 0, store.MIN_READER]
    assert _idle(dest) == "IDLE"
    for schema in (None, store.SCHEMA_VERSION):
        r = _gate(dest, _target(tmp_path, schema))
        assert r.returncode == 0 and "GATE-PASSED" in r.stdout, r


@pytest.mark.parametrize("setup,old_blocked,idle", [
    (lambda con: _sol_deal(con), "сделок SOL/HL: 1", "IDLE"),
    (lambda con: _sol_deal(con, "PAUSED"), "сделок SOL/HL: 1", "IDLE"),
    (lambda con: _sol_deal(con, "CLOSED"), None, "IDLE"),
    (lambda con: _sol_attempt(con, "UNKNOWN"), "Solana: 1", "BUSY: неразрешённые попытки: Solana 1, Hyperliquid 0"),
    (lambda con: _sol_attempt(con, "FINALIZED_OK"), "Solana: 1", "BUSY: неразрешённые попытки: Solana 1"),
    (lambda con: _sol_attempt(con, "FINALIZED_OK", "finalized"), None, "IDLE"),
    (lambda con: _sol_attempt(con, "VALIDATED"), None, "IDLE"),
    (lambda con: _hl_attempt(con, "UNKNOWN"), "Hyperliquid: 1", "BUSY: неразрешённые попытки: Solana 0, Hyperliquid 1"),
    (lambda con: _hl_attempt(con, "FILLED"), None, "IDLE"),
])
def test_sol_gate_and_trader_idle(tmp_path, setup, old_blocked, idle):
    """M10: сделка SOL на паузе с UNKNOWN — не «тихо», хотя approved/running нет. M06: версия без связки SOL×HL не
    ставится при сделке или попытке SOL/HL; версия с воротами схемы 2 — ставится (у неё свои резолверы)."""
    dest, db = _dest(tmp_path)
    con = store.connect(db)
    _bsc_deal(con)
    setup(con)
    con.close()
    assert _idle(dest).startswith(idle)
    old = _gate(dest, _target(tmp_path, None))
    if old_blocked:
        assert old.returncode == 1 and "версия без связки SOL×HL" in old.stdout and old_blocked in old.stdout, old
        assert "GATE-PASSED" not in old.stdout
    else:
        assert old.returncode == 0 and "GATE-PASSED" in old.stdout, old
    new = _gate(dest, _target(tmp_path, store.SCHEMA_VERSION))
    assert new.returncode == 0 and "GATE-PASSED" in new.stdout, new


def test_trader_idle_counts_running_intent(tmp_path):
    dest, db = _dest(tmp_path)
    con = store.connect(db)
    did = _bsc_deal(con)
    iid, nonce = store.create_intent(con, deal_id=did, kind="entry", spec={}, plan={})
    assert store.approve_intent(con, iid, nonce)
    con.close()
    assert _idle(dest) == "BUSY: идёт исполнение (намерений approved/running: 1)"


def test_sol_gate_schema_below_min_reader_unreadable_db_and_no_db(tmp_path):
    dest, db = _dest(tmp_path)
    assert _gate(dest, _target(tmp_path, None)).returncode == 0                  # нет БД — нечего защищать
    assert _state(dest) == [0, 0, 0, 0, 0] and _idle(dest) == "IDLE"
    store.connect(db).close()
    r = _gate(dest, _target(tmp_path, 1))
    assert r.returncode == 1 and "не откроет trade.db (min_reader 2)" in r.stdout, r
    db.write_bytes(b"not a database" * 100)
    r = _gate(dest, _target(tmp_path, store.SCHEMA_VERSION))
    assert r.returncode == 1 and "trade.db не прочитана" in r.stdout, r
    assert _idle(dest) == "BUSY: trade.db не прочитана"


def test_trade_state_before_schema_2_reads_zero(tmp_path):
    """БД выкачанного релиза (до C1): таблиц связки и версии нет — нули, а не ошибка."""
    dest, db = _dest(tmp_path)
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE intents(id TEXT, status TEXT)")
    con.execute("CREATE TABLE deals(id TEXT, state TEXT, chain TEXT, perp_venue TEXT)")
    con.execute("INSERT INTO deals VALUES ('D1', 'OPEN', 'bsc', 'aster')")
    con.commit()
    con.close()
    assert _state(dest) == [0, 0, 0, 0, 0]


# ==== remote_verify: readonly doctor — справка, не ворота =======================================================
def _doctor_block() -> str:
    v = (DEPLOY / "remote_verify.sh").read_text()
    block = v.split("# >>> doctor\n", 1)[1].split("# <<< doctor", 1)[0]
    assert v.index("# <<< doctor") < v.index("      exit 0")
    assert "exit" not in block, "doctor не решает судьбу выката"
    return block


@pytest.mark.parametrize("cli_has,out,rc,want", [
    (True, "sol-hl doctor · sol_best_hyperliquid · режим dry\n✗ нет лимита\nlive_ready=false — причин 1", 1,
     "doctor sol-hl: sol-hl doctor · sol_best_hyperliquid · режим dry · live_ready=false — причин 1"),
    (True, "Traceback (most recent call last):\nModuleNotFoundError: No module named 'solders'", 1,
     "!! doctor sol-hl не отработал (код 1): Traceback"),
    (False, "", 0, ""),
])
def test_verify_runs_readonly_doctor_without_affecting_exit(tmp_path, cli_has, out, rc, want):
    dest = tmp_path / "dest"
    (dest / "src" / "funding_bot").mkdir(parents=True)
    (dest / ".venv" / "bin").mkdir(parents=True)
    (dest / "src" / "funding_bot" / "cli.py").write_text('sub.add_parser("sol-hl")\n' if cli_has else "# старый cli\n")
    fb = dest / ".venv" / "bin" / "funding_bot"
    args_log = tmp_path / "args"
    fb.write_text(f"#!/bin/sh\necho \"$*\" > '{args_log}'\ncat <<'X'\n{out}\nX\nexit {rc}\n")
    fb.chmod(0o755)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "timeout").write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    (stub / "timeout").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", f'set -euo pipefail; DEST="$1"\n{_doctor_block()}\necho VERIFY-OK', "_",
                        str(dest)], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.rstrip().endswith("VERIFY-OK"), r
    assert r.stdout.replace("VERIFY-OK", "").strip().startswith(want) if want else \
        r.stdout.strip() == "VERIFY-OK"
    if cli_has:
        assert args_log.read_text().strip() == "sol-hl doctor --no-quotes"
    else:
        assert not args_log.exists()


# ==== SIGTERM: TimeoutStopSec трейдера под своп Solana и разбор HL ===============================================
def _unit() -> dict[str, str]:
    lines = (DEPLOY / "funding_bot-trader.service").read_text().splitlines()
    return dict(ln.split("=", 1) for ln in lines if "=" in ln and not ln.startswith("#"))


def test_stop_budget_covers_solana_blockhash_and_hl_unknown():
    """Своп отправлен прямо перед SIGTERM: доводим до finalized/доказанного истечения и хеджа с разбором UNKNOWN."""
    slot_s, validity_blocks, finality_slots = 0.4, 150, 32
    assert sol_exec.WAIT_S >= (validity_blocks + finality_slots) * slot_s         # 90 ≥ 72.8: срок + финальность
    rpc_t = solrpc.RPC_TIMEOUT_S
    sol = rpc_t + sol_exec.WAIT_S + 4 * rpc_t + sol_exec.POLL_S                    # отправка + ожидание + опрос 2 RPC
    read, exch = sum(H.READ_TIMEOUT), sum(H.EXCHANGE_TIMEOUT)
    hl = (read + exch + H.EXPIRES_MS / 1000 + H.CLOCK_SLACK_MS / 1000
          + (H.NOT_FOUND_POLLS + 1) * H.UNKNOWN_POLL_GAP_S + read + read)          # стакан, IOC, settle, статус, позиция
    assert sol + hl <= tg_bot.STOP_WAIT_S, (sol, hl, tg_bot.STOP_WAIT_S)
    sender_close, margin = 10, 20
    assert int(_unit()["TimeoutStopSec"]) >= tg_bot.STOP_WAIT_S + sender_close + margin
    assert _unit()["KillSignal"] == "SIGTERM"
