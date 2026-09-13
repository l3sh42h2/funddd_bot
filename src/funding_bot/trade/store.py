"""runtime/trade.db — журнал исполнения фазы 2 (trade_spec §5). SQLite WAL, отдельно от funding_bot.db:
чистки и чекпойнты коллектора не держат блокировку исполнителя, а деплой читает только эту БД
(«идёт исполнение» = есть intents в approved/running).

Протокол записи-до для каждого внешнего действия: строка → COMMIT → внешний вызов → результат → COMMIT.
Упавший процесс после рестарта видит, ЧТО собирался сделать (подписанную транзакцию с хэшем и nonce,
client_id заявки), и выясняет исход у сети/биржи, а не повторяет вслепую (урок SentUnknown из lphedge).

Деньги и количества — TEXT (Decimal через format(…, "f") или сырой int строкой); float в эти колонки не
пропускается (TypeError). Ключи в БД не попадают никогда: raw_tx — подписанные байты, публичные после отправки;
тексты ошибок и событий проходят keys.redact_secrets.

Ограничения параллельности — уникальными частичными индексами, чтобы их не обошла и ошибка в движке:
одна активная сделка на (сеть, токен) и на (площадка, символ); одно намерение в approved/running на всю БД.

Соединение — одно на поток (исполнитель, опрос, задания): транзакции одного соединения из разных потоков
перемешались бы. isolation_level=None: одиночная запись фиксируется сразу, группа — через tx().
"""
from __future__ import annotations
import json, re, secrets, sqlite3, time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable
from . import tconfig
from .keys import redact_secrets
from .types import InstrumentSpec, PerpFill

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS tg_updates(update_id INTEGER PRIMARY KEY, ts REAL, user_id INT, chat_id INT, text TEXT,
  verdict TEXT);
CREATE TABLE IF NOT EXISTS deals(id TEXT PRIMARY KEY, created REAL, state TEXT, reason TEXT, coin TEXT, chain TEXT,
  token TEXT, token_dec INT, perp_venue TEXT, symbol TEXT, leg_usd TEXT, owner_json TEXT, sim INT,
  carry TEXT, dust TEXT, updated REAL, inst_json TEXT);
CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, deal_id TEXT, kind TEXT, spec_json TEXT, plan_json TEXT,
  nonce TEXT, status TEXT, created REAL, expires REAL, approved REAL, chat INT, msg_id INT, err TEXT);
CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY, intent_id TEXT, seq INT, state TEXT, planned_in TEXT,
  dex_in TEXT, dex_out TEXT, perp_qty TEXT, perp_quote TEXT, basis_bps REAL, carry_in TEXT, carry_out TEXT,
  recovery REAL, created REAL, updated REAL, UNIQUE(intent_id, seq));
CREATE TABLE IF NOT EXISTS dex_txs(id INTEGER PRIMARY KEY, clip_id INT, kind TEXT, chain TEXT, wallet TEXT, nonce INT,
  to_addr TEXT, value TEXT, min_receive TEXT, gas_limit INT, gas_price TEXT, raw_tx TEXT, tx_hash TEXT UNIQUE,
  state TEXT, block INT, status INT, gas_used INT, eff_gas_price TEXT, amount_in TEXT, amount_out TEXT, err TEXT,
  sent_ts REAL, resolved_ts REAL);
CREATE TABLE IF NOT EXISTS perp_orders(id INTEGER PRIMARY KEY, clip_id INT, client_id TEXT UNIQUE, venue TEXT,
  symbol TEXT, side TEXT, reduce_only INT, tif TEXT, price TEXT, qty TEXT, sign_nonce INT, state TEXT, order_id INT,
  executed_qty TEXT, avg_price TEXT, cum_quote TEXT, err_code INT, err TEXT, sent_ts REAL, resolved_ts REAL);
CREATE TABLE IF NOT EXISTS perp_fills(venue TEXT, trade_id INT, order_id INT, price TEXT, qty TEXT, quote_qty TEXT,
  commission_abs TEXT, commission_asset TEXT, maker INT, realized_pnl TEXT, ts INT, PRIMARY KEY(venue, trade_id));
CREATE TABLE IF NOT EXISTS funding_income(venue TEXT, tran_id INT, symbol TEXT, income TEXT, ts INT,
  PRIMARY KEY(venue, tran_id));
CREATE TABLE IF NOT EXISTS exec_events(ts REAL, deal_id TEXT, intent_id TEXT, clip_id INT, kind TEXT, json TEXT);
-- оценка сделки (trade/marks.py): «PnL сейчас» и «при выходе» — считает трейдер, кабинет только читает
CREATE TABLE IF NOT EXISTS deal_marks(deal_id TEXT, ts REAL, px_dex TEXT, px_perp TEXT, pnl_now TEXT, pnl_exit TEXT,
  exit_cost TEXT, funding TEXT, fees TEXT, gas TEXT, flags_json TEXT);
CREATE INDEX IF NOT EXISTS deal_marks_deal ON deal_marks(deal_id, ts);

CREATE UNIQUE INDEX IF NOT EXISTS deals_one_per_token ON deals(chain, token)
  WHERE state NOT IN ('DRAFT', 'CLOSED', 'ABORTED');
CREATE UNIQUE INDEX IF NOT EXISTS deals_one_per_symbol ON deals(perp_venue, symbol)
  WHERE state NOT IN ('DRAFT', 'CLOSED', 'ABORTED');
CREATE UNIQUE INDEX IF NOT EXISTS intents_one_running ON intents((status IN ('approved', 'running')))
  WHERE status IN ('approved', 'running');
CREATE INDEX IF NOT EXISTS intents_deal ON intents(deal_id);
CREATE INDEX IF NOT EXISTS clips_intent ON clips(intent_id);
CREATE INDEX IF NOT EXISTS dex_txs_clip ON dex_txs(clip_id);
CREATE INDEX IF NOT EXISTS dex_txs_nonce ON dex_txs(chain, wallet, nonce);
CREATE INDEX IF NOT EXISTS perp_orders_clip ON perp_orders(clip_id);
CREATE INDEX IF NOT EXISTS exec_events_deal ON exec_events(deal_id, ts);

-- только вставка: сырьё для сверки и отчёта не переписывается задним числом
CREATE TRIGGER IF NOT EXISTS exec_events_no_update BEFORE UPDATE ON exec_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS exec_events_no_delete BEFORE DELETE ON exec_events BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS perp_fills_no_update BEFORE UPDATE ON perp_fills BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS perp_fills_no_delete BEFORE DELETE ON perp_fills BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS funding_income_no_update BEFORE UPDATE ON funding_income BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS funding_income_no_delete BEFORE DELETE ON funding_income BEGIN SELECT RAISE(ABORT, 'append-only'); END;
-- оценки только добавляются; удаление — только чистка старых строк (prune_marks)
CREATE TRIGGER IF NOT EXISTS deal_marks_no_update BEFORE UPDATE ON deal_marks BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


# --- состояния ------------------------------------------------------------------------------------
class DealState(StrEnum):
    DRAFT = "DRAFT"
    ENTERING = "ENTERING"
    PAUSED = "PAUSED"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    ABORTED = "ABORTED"
    HALTED_MISMATCH = "HALTED_MISMATCH"


class IntentStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ClipState(StrEnum):
    PLANNED = "PLANNED"
    DEX_SENT = "DEX_SENT"
    DEX_OK = "DEX_OK"
    DEX_REVERTED = "DEX_REVERTED"
    DEX_UNKNOWN = "DEX_UNKNOWN"
    PERP_SENT = "PERP_SENT"
    BALANCED = "BALANCED"
    HEDGE_DEFICIT = "HEDGE_DEFICIT"


class DexTxKind(StrEnum):
    APPROVE = "approve"
    SWAP = "swap"
    BUMP = "bump"
    CANCEL = "cancel"


class DexTxState(StrEnum):
    SIGNED = "SIGNED"            # сырые байты и хэш записаны, в сеть ещё не ушло (или ушло, но не отмечено)
    SENT = "SENT"
    MINED_OK = "MINED_OK"
    MINED_REVERTED = "MINED_REVERTED"
    REPLACED = "REPLACED"        # на этом nonce замайнилась другая наша транзакция (bump/cancel)
    DROPPED = "DROPPED"          # nonce свободен, хэша нет нигде
    UNKNOWN = "UNKNOWN"


class PerpOrderState(StrEnum):
    INTENT = "INTENT"            # client_id записан до отправки
    SENT = "SENT"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    EXPIRED = "EXPIRED"          # IOC без исполнения
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"          # 503/-1006/-1007/таймаут: не повторять, выяснять запросом
    NOT_PLACED = "NOT_PLACED"    # -2013 трижды за ~5 с и userTrades/positionRisk не менялись — решает движок


_ACTIVE_DEAL = frozenset({DealState.ENTERING, DealState.PAUSED, DealState.OPEN, DealState.EXITING,
                          DealState.HALTED_MISMATCH})
_BUSY_INTENT = (IntentStatus.APPROVED, IntentStatus.RUNNING)

# Допустимые переходы. Смена на то же состояние — просто обновление полей.
DEAL_NEXT: dict[str, frozenset] = {
    DealState.DRAFT: frozenset({DealState.ENTERING, DealState.ABORTED}),
    DealState.ENTERING: frozenset({DealState.PAUSED, DealState.OPEN, DealState.ABORTED, DealState.HALTED_MISMATCH}),
    DealState.PAUSED: frozenset({DealState.ENTERING, DealState.EXITING, DealState.OPEN, DealState.CLOSED,
                                 DealState.ABORTED, DealState.HALTED_MISMATCH}),
    DealState.OPEN: frozenset({DealState.EXITING, DealState.PAUSED, DealState.HALTED_MISMATCH}),
    # EXITING → OPEN: частичный выход («выход AIW3 200») доведён — остаток сделки открыт и захеджирован
    DealState.EXITING: frozenset({DealState.OPEN, DealState.PAUSED, DealState.CLOSED, DealState.HALTED_MISMATCH}),
    DealState.HALTED_MISMATCH: frozenset({DealState.PAUSED}),     # только после действий владельца и сверки
    DealState.CLOSED: frozenset(),
    DealState.ABORTED: frozenset(),
}
INTENT_NEXT: dict[str, frozenset] = {
    IntentStatus.PROPOSED: frozenset({IntentStatus.APPROVED, IntentStatus.REJECTED, IntentStatus.EXPIRED}),
    IntentStatus.APPROVED: frozenset({IntentStatus.RUNNING, IntentStatus.FAILED, IntentStatus.INTERRUPTED}),
    IntentStatus.RUNNING: frozenset({IntentStatus.DONE, IntentStatus.PARTIAL, IntentStatus.FAILED,
                                     IntentStatus.INTERRUPTED}),
}
CLIP_NEXT: dict[str, frozenset] = {
    # PLANNED → PERP_SENT: клип только из перп-ноги («дохедж», «выход <id> перп») — DEX в нём нет
    ClipState.PLANNED: frozenset({ClipState.DEX_SENT, ClipState.PERP_SENT}),
    ClipState.DEX_SENT: frozenset({ClipState.DEX_OK, ClipState.DEX_REVERTED, ClipState.DEX_UNKNOWN}),
    ClipState.DEX_UNKNOWN: frozenset({ClipState.DEX_OK, ClipState.DEX_REVERTED}),
    ClipState.DEX_REVERTED: frozenset({ClipState.DEX_SENT}),                  # один повтор по свежей котировке
    ClipState.DEX_OK: frozenset({ClipState.PERP_SENT, ClipState.BALANCED}),   # BALANCED: весь приход ушёл в перенос
    ClipState.PERP_SENT: frozenset({ClipState.BALANCED, ClipState.HEDGE_DEFICIT}),
    ClipState.HEDGE_DEFICIT: frozenset({ClipState.PERP_SENT, ClipState.BALANCED}),   # дохедж / откат
}
_DEX_DONE = frozenset({DexTxState.MINED_OK, DexTxState.MINED_REVERTED, DexTxState.REPLACED, DexTxState.DROPPED})
DEX_TX_NEXT: dict[str, frozenset] = {
    DexTxState.SIGNED: frozenset({DexTxState.SENT, DexTxState.UNKNOWN}) | _DEX_DONE,   # упали между записью и отметкой
    DexTxState.SENT: frozenset({DexTxState.UNKNOWN}) | _DEX_DONE,
    DexTxState.UNKNOWN: _DEX_DONE,
}
_PERP_RESULT = frozenset({PerpOrderState.FILLED, PerpOrderState.PARTIALLY_FILLED, PerpOrderState.EXPIRED,
                          PerpOrderState.REJECTED})
PERP_ORDER_NEXT: dict[str, frozenset] = {
    PerpOrderState.INTENT: frozenset({PerpOrderState.SENT, PerpOrderState.UNKNOWN, PerpOrderState.NOT_PLACED})
                           | _PERP_RESULT,
    PerpOrderState.SENT: frozenset({PerpOrderState.UNKNOWN}) | _PERP_RESULT,
    PerpOrderState.UNKNOWN: frozenset({PerpOrderState.NOT_PLACED}) | _PERP_RESULT,
}


class StoreError(RuntimeError):
    pass


class BadTransition(StoreError):
    """Переход, которого нет в таблице: ошибка движка, а не «пропустить»."""


class StoreBusy(StoreError):
    """Сработал индекс параллельности: уже есть активная сделка на токен/символ или идущее намерение."""


# --- соединение и транзакции ------------------------------------------------------------------
def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else tconfig.TRADE_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    mode = con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        con.close()
        raise StoreError(f"trade.db не перешла в WAL (journal_mode={mode})")
    # запись-до: COMMIT должен пережить и сбой ОС, а не только процесса (записей мало — цена FULL ничтожна)
    con.execute("PRAGMA synchronous=FULL")
    con.executescript(SCHEMA)
    for t, c, d in _ADD_COLUMNS:
        _ensure_column(con, t, c, d)
    return con


# Миграции схемы — ТОЛЬКО добавление колонок, идемпотентно на каждом connect(): прежняя версия кода (откат на .prev)
# новую колонку не видит (INSERT с явным списком колонок, SELECT * — лишний ключ).
_ADD_COLUMNS = (("deals", "inst_json", "TEXT"),)      # ревью 13.09, С1/Н2: спецификация инструмента сделки


def _ensure_column(con, table: str, col: str, decl: str) -> None:
    if col in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
        return
    try:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except sqlite3.OperationalError as e:       # второй процесс успел раньше — это не ошибка
        if "duplicate column" not in str(e).lower():
            raise
    _COLS.pop(table, None)                      # кэш колонок _transition, заполненный до миграции, устарел


@contextmanager
def tx(con: sqlite3.Connection):
    """BEGIN IMMEDIATE … COMMIT. IMMEDIATE берёт блокировку записи сразу: проверка и запись не расходятся между
    процессами (трейдер и деплой-гейт). Внутри уже открытой транзакции — просто присоединяется к ней."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


# --- значения ------------------------------------------------------------------------------------
def amt(v: Any) -> str | None:
    """Деньги/количество → TEXT. Decimal — без экспоненты, int — сырые единицы. float — TypeError."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise TypeError("сумма не может быть bool")
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        try:
            v = Decimal(v)
        except InvalidOperation:
            raise ValueError(f"не число: {v!r}") from None
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise ValueError(f"не конечное число: {v}")
        return format(v, "f")
    raise TypeError(f"сумма должна быть Decimal или int, а не {type(v).__name__}")


def dec(s: str | None) -> Decimal | None:
    return None if s is None else Decimal(s)


def units(s: str | None) -> int | None:
    return None if s is None else int(s)


def _json_default(o: Any):
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, (set, frozenset)):
        return sorted(str(x) for x in o)
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, StrEnum):
        return str(o)
    raise TypeError(f"не сериализуется в JSON: {type(o).__name__}")


def jdump(obj: Any) -> str:
    """JSON для plan_json/spec_json/событий: Decimal строкой (без потери знаков), ключи отсортированы."""
    return json.dumps(obj, default=_json_default, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # без I/O/0/1: id читает человек в Telegram


def new_id(prefix: str, n: int = 4) -> str:
    return prefix + "".join(secrets.choice(_ID_ALPHABET) for _ in range(n))


def new_nonce() -> str:
    """nonce кнопки: ok:<intent>:<nonce8> — старая кнопка другого плана не одобрит новый."""
    return secrets.token_hex(4)


_CLIENT_ID_RE = re.compile(tconfig.ASTER_CLIENT_ID_RE)


def client_order_id(deal_id: str, kind: str, clip_seq: int, child: int, attempt: int) -> str:
    """fb-D7K2-e03-c1-a1 (сделка, вид+клип, дочерняя, попытка). Уникален у Aster только среди ОТКРЫТЫХ заявок —
    исполненный IOC им не дедуплицируется, поэтому повтор — только с новой попыткой и только после выяснения исхода."""
    cid = f"fb-{deal_id}-{kind[:1].lower()}{int(clip_seq):02d}-c{int(child)}-a{int(attempt)}"
    if not _CLIENT_ID_RE.match(cid):
        raise ValueError(f"client_id не проходит шаблон Aster: {cid}")
    return cid


# --- общий переход состояния ---------------------------------------------------------------------
_AMOUNT_COLS = frozenset({"leg_usd", "carry", "dust", "planned_in", "dex_in", "dex_out", "perp_qty", "perp_quote",
                          "carry_in", "carry_out", "value", "min_receive", "gas_price", "eff_gas_price", "amount_in",
                          "amount_out", "price", "qty", "executed_qty", "avg_price", "cum_quote", "quote_qty",
                          "commission_abs", "realized_pnl", "income"})
_TEXT_SCRUB = frozenset({"err", "reason"})
_COLS: dict[str, frozenset] = {}


def _cols(con, table: str) -> frozenset:
    if table not in _COLS:
        _COLS[table] = frozenset(r[1] for r in con.execute(f"PRAGMA table_info({table})"))
    return _COLS[table]


def _norm(col: str, v: Any) -> Any:
    if col in _AMOUNT_COLS:
        return amt(v)
    if col in _TEXT_SCRUB and v is not None:
        return redact_secrets(v)[:2000]
    if isinstance(v, StrEnum):
        return str(v)
    return v


def _transition(con, table: str, key_col: str, key: Any, state_col: str, new: str, nxt: dict,
                expect: Iterable[str] | str | None, fields: dict, now: float | None) -> bool:
    """CAS-переход: False — текущее состояние не из expect (кто-то успел раньше). Недопустимый переход —
    BadTransition. Поля-суммы проходят amt(), тексты ошибок — redact_secrets."""
    cols = _cols(con, table)
    bad = [c for c in fields if c not in cols or c in (key_col, state_col)]
    if bad:
        raise StoreError(f"{table}: нет таких полей {bad}")
    new = str(new)
    exp = None if expect is None else ({str(expect)} if isinstance(expect, str) else {str(e) for e in expect})
    ts = time.time() if now is None else now
    with tx(con):
        row = con.execute(f"SELECT {state_col} FROM {table} WHERE {key_col}=?", (key,)).fetchone()
        if row is None:
            raise LookupError(f"{table}: нет строки {key}")
        cur = row[0]
        if exp is not None and cur not in exp:
            return False
        if new != cur and new not in nxt.get(cur, frozenset()):
            raise BadTransition(f"{table} {key}: {cur} → {new} недопустимо")
        sets = {state_col: new, **{c: _norm(c, v) for c, v in fields.items()}}
        if "updated" in cols:
            sets["updated"] = ts
        try:
            n = con.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in sets)} WHERE {key_col}=? AND {state_col}=?",
                            (*sets.values(), key, cur)).rowcount
        except sqlite3.IntegrityError as e:
            raise StoreBusy(f"{table} {key} → {new}: {e}") from None
    return n == 1


def _row(con, sql: str, args=()) -> dict | None:
    r = con.execute(sql, args).fetchone()
    return dict(r) if r is not None else None


def _rows(con, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


# --- flags ---------------------------------------------------------------------------------------
FLAG_PAUSED = "paused"
FLAG_TG_OFFSET = "tg_offset"


def get_flag(con, k: str, default: str | None = None) -> str | None:
    r = con.execute("SELECT v FROM flags WHERE k=?", (k,)).fetchone()
    return r[0] if r is not None else default


def set_flag(con, k: str, v: str) -> None:
    con.execute("INSERT INTO flags(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def is_paused(con) -> bool:
    return get_flag(con, FLAG_PAUSED, "0") == "1"


def set_paused(con, on: bool) -> None:
    """«стоп» переживает рестарт: флаг в БД, а не только Event в памяти."""
    set_flag(con, FLAG_PAUSED, "1" if on else "0")


def tg_offset(con) -> int:
    return int(get_flag(con, FLAG_TG_OFFSET, "0") or 0)


def set_tg_offset(con, offset: int) -> None:
    """Смещение getUpdates только растёт: откат назад повторно отдал бы уже разобранные команды."""
    con.execute("INSERT INTO flags(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET "
                "v=CAST(MAX(CAST(v AS INTEGER), CAST(excluded.v AS INTEGER)) AS TEXT)", (FLAG_TG_OFFSET, str(int(offset))))


# --- tg_updates ----------------------------------------------------------------------------------
def claim_update(con, update_id: int, ts: float, user_id: int | None, chat_id: int | None, text: str | None) -> bool:
    """INSERT OR IGNORE ДО продвижения смещения: повторно доставленная команда не исполнится второй раз."""
    return con.execute("INSERT OR IGNORE INTO tg_updates(update_id, ts, user_id, chat_id, text) VALUES(?,?,?,?,?)",
                       (int(update_id), ts, user_id, chat_id,
                        None if text is None else redact_secrets(text)[:4096])).rowcount == 1


def set_update_verdict(con, update_id: int, verdict: str) -> None:
    con.execute("UPDATE tg_updates SET verdict=? WHERE update_id=?", (verdict, int(update_id)))


# --- deals ---------------------------------------------------------------------------------------
def create_deal(con, *, coin: str, chain: str, token: str, token_dec: int, perp_venue: str, symbol: str,
                leg_usd: Decimal, owner_json: str, sim: bool, deal_id: str | None = None,
                now: float | None = None, inst: InstrumentSpec | None = None) -> str:
    """Сделка в DRAFT (план показан, ждёт кнопки). DRAFT не занимает токен/символ — это делает переход в ENTERING.
    inst — спецификация инструмента (ревью 13.09): замораживается в той же вставке."""
    ts = time.time() if now is None else now
    with tx(con):
        did = deal_id
        while did is None:
            cand = new_id("D")
            if con.execute("SELECT 1 FROM deals WHERE id=?", (cand,)).fetchone() is None:
                did = cand
        con.execute("INSERT INTO deals(id, created, state, reason, coin, chain, token, token_dec, perp_venue, symbol, "
                    "leg_usd, owner_json, sim, carry, dust, updated, inst_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, ts, str(DealState.DRAFT), None, coin, chain, token.lower(), int(token_dec), perp_venue, symbol,
                     amt(leg_usd), owner_json, 1 if sim else 0, "0", "0", ts, inst.to_json() if inst else None))
    return did


def set_deal_inst(con, deal_id: str, inst_json: str) -> bool:
    """Бэкфилл спецификации инструмента: только если её ещё нет (повтор и гонка безвредны). updated не трогает —
    это не переход состояния (итог закрытой сделки считает фандинг до updated)."""
    return con.execute("UPDATE deals SET inst_json=? WHERE id=? AND inst_json IS NULL", (inst_json, deal_id)).rowcount == 1


def get_deal(con, deal_id: str) -> dict | None:
    return _row(con, "SELECT * FROM deals WHERE id=?", (deal_id,))


def set_deal_state(con, deal_id: str, new: str, *, expect=None, reason: str | None = None, now: float | None = None,
                   **fields) -> bool:
    """Переход сделки. reason — причина паузы/остановки (PAUSED('restart'), HEDGE_DEFICIT…). StoreBusy — токен или
    символ уже заняты другой активной сделкой."""
    if reason is not None:
        fields["reason"] = reason
    return _transition(con, "deals", "id", deal_id, "state", new, DEAL_NEXT, expect, fields, now)


def active_deals(con) -> list[dict]:
    """Сделки с позицией или в работе (всё, кроме DRAFT/CLOSED/ABORTED) — для лимита max_open_deals и сверки."""
    qs = ",".join("?" * len(_ACTIVE_DEAL))
    return _rows(con, f"SELECT * FROM deals WHERE state IN ({qs}) ORDER BY created", tuple(str(s) for s in _ACTIVE_DEAL))


# --- intents -------------------------------------------------------------------------------------
def create_intent(con, *, deal_id: str, kind: str, spec: Any, plan: Any, ttl_s: float = tconfig.PLAN_TTL_S,
                  chat: int | None = None, msg_id: int | None = None, intent_id: str | None = None,
                  now: float | None = None) -> tuple[str, str]:
    """Предложение (proposed) с кнопками; истекает через ttl_s. Возвращает (id, nonce кнопки).
    kind: entry → id на E, exit → на X (как в тексте кнопки)."""
    ts = time.time() if now is None else now
    nonce = new_nonce()
    prefix = "X" if str(kind).lower().startswith(("exit", "x")) else "E"
    with tx(con):
        iid = intent_id
        while iid is None:
            cand = new_id(prefix)
            if con.execute("SELECT 1 FROM intents WHERE id=?", (cand,)).fetchone() is None:
                iid = cand
        con.execute("INSERT INTO intents(id, deal_id, kind, spec_json, plan_json, nonce, status, created, expires, "
                    "approved, chat, msg_id, err) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, deal_id, kind, spec if isinstance(spec, str) else jdump(spec),
                     plan if isinstance(plan, str) else jdump(plan), nonce, str(IntentStatus.PROPOSED), ts, ts + ttl_s,
                     None, chat, msg_id, None))
    return iid, nonce


def get_intent(con, intent_id: str) -> dict | None:
    return _row(con, "SELECT * FROM intents WHERE id=?", (intent_id,))


def set_intent_message(con, intent_id: str, chat: int, msg_id: int) -> None:
    con.execute("UPDATE intents SET chat=?, msg_id=? WHERE id=?", (chat, msg_id, intent_id))


def approve_intent(con, intent_id: str, nonce: str, now: float | None = None) -> bool:
    """Одно атомарное UPDATE (compare-and-set): двойное нажатие, повтор доставки и два устройства дают ровно одно
    одобрение. False — уже принято/истекло/чужой nonce. StoreBusy — другое намерение уже идёт."""
    ts = time.time() if now is None else now
    try:
        return con.execute("UPDATE intents SET status=?, approved=? WHERE id=? AND nonce=? AND status=? AND expires>?",
                           (str(IntentStatus.APPROVED), ts, intent_id, nonce, str(IntentStatus.PROPOSED),
                            ts)).rowcount == 1
    except sqlite3.IntegrityError:
        raise StoreBusy("уже идёт другое исполнение") from None


def reject_intent(con, intent_id: str, nonce: str) -> bool:
    return con.execute("UPDATE intents SET status=? WHERE id=? AND nonce=? AND status=?",
                       (str(IntentStatus.REJECTED), intent_id, nonce, str(IntentStatus.PROPOSED))).rowcount == 1


def expire_intents(con, now: float | None = None) -> list[str]:
    """Просроченные предложения → expired; возвращает их id (снять кнопки с сообщений)."""
    ts = time.time() if now is None else now
    with tx(con):
        ids = [r[0] for r in con.execute("SELECT id FROM intents WHERE status=? AND expires<=?",
                                         (str(IntentStatus.PROPOSED), ts))]
        if ids:
            con.executemany("UPDATE intents SET status=? WHERE id=? AND status=?",
                            [(str(IntentStatus.EXPIRED), i, str(IntentStatus.PROPOSED)) for i in ids])
    return ids


def set_intent_status(con, intent_id: str, new: str, *, expect=None, err: str | None = None) -> bool:
    fields = {} if err is None else {"err": err}
    return _transition(con, "intents", "id", intent_id, "status", new, INTENT_NEXT, expect, fields, None)


def interrupt_unfinished(con) -> list[str]:
    """Рестарт: approved/running → interrupted. Сам исполнитель не продолжает — только свежий план и кнопки."""
    qs = ",".join("?" * len(_BUSY_INTENT))
    with tx(con):
        ids = [r[0] for r in con.execute(f"SELECT id FROM intents WHERE status IN ({qs})",
                                         tuple(str(s) for s in _BUSY_INTENT))]
        con.executemany("UPDATE intents SET status=?, err=COALESCE(err, 'restart') WHERE id=?",
                        [(str(IntentStatus.INTERRUPTED), i) for i in ids])
    return ids


def busy_intents(con) -> int:
    """Гейт деплоя: SELECT count(*) … IN ('approved','running') — не ноль значит «идёт исполнение»."""
    return con.execute("SELECT count(*) FROM intents WHERE status IN ('approved', 'running')").fetchone()[0]


# --- clips ---------------------------------------------------------------------------------------
def create_clip(con, intent_id: str, seq: int, planned_in: int | Decimal, now: float | None = None) -> int:
    ts = time.time() if now is None else now
    return con.execute("INSERT INTO clips(intent_id, seq, state, planned_in, created, updated) VALUES(?,?,?,?,?,?)",
                       (intent_id, int(seq), str(ClipState.PLANNED), amt(planned_in), ts, ts)).lastrowid


def get_clip(con, clip_id: int) -> dict | None:
    return _row(con, "SELECT * FROM clips WHERE id=?", (clip_id,))


def clips_of(con, intent_id: str) -> list[dict]:
    return _rows(con, "SELECT * FROM clips WHERE intent_id=? ORDER BY seq", (intent_id,))


def set_clip_state(con, clip_id: int, new: str, *, expect=None, now: float | None = None, **fields) -> bool:
    return _transition(con, "clips", "id", clip_id, "state", new, CLIP_NEXT, expect, fields, now)


# --- dex_txs: запись-до для транзакций -------------------------------------------------------------
def dex_tx_signed(con, *, clip_id: int | None, kind: str, chain: str, wallet: str, nonce: int, to_addr: str,
                  value: int, min_receive: int | None, gas_limit: int, gas_price: int, raw_tx: str, tx_hash: str,
                  now: float | None = None) -> int:
    """Подписанная транзакция — В БД ДО отправки (on_signed у EvmWallet). После падения рестарт найдёт хэш и
    nonce и спросит сеть, а не подпишет новую на новом nonce."""
    if str(kind) not in {k.value for k in DexTxKind}:
        raise ValueError(f"неизвестный вид транзакции: {kind}")
    return con.execute(
        "INSERT INTO dex_txs(clip_id, kind, chain, wallet, nonce, to_addr, value, min_receive, gas_limit, gas_price, "
        "raw_tx, tx_hash, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (clip_id, str(kind), chain, wallet.lower(), int(nonce), to_addr.lower(), amt(int(value)),
         amt(min_receive), int(gas_limit), amt(int(gas_price)), raw_tx, tx_hash.lower(), str(DexTxState.SIGNED))).lastrowid


def dex_tx_sent(con, tx_hash: str, now: float | None = None) -> bool:
    ts = time.time() if now is None else now
    return _transition(con, "dex_txs", "tx_hash", tx_hash.lower(), "state", DexTxState.SENT, DEX_TX_NEXT,
                       DexTxState.SIGNED, {"sent_ts": ts}, now)


def dex_tx_resolve(con, tx_hash: str, state: str, *, block: int | None = None, status: int | None = None,
                   gas_used: int | None = None, eff_gas_price: int | None = None, amount_in: int | None = None,
                   amount_out: int | None = None, err: str | None = None, now: float | None = None) -> bool:
    ts = time.time() if now is None else now
    fields = {"block": block, "status": status, "gas_used": gas_used, "eff_gas_price": eff_gas_price,
              "amount_in": amount_in, "amount_out": amount_out, "err": err}
    fields = {k: v for k, v in fields.items() if v is not None}
    if str(state) != DexTxState.UNKNOWN:
        fields["resolved_ts"] = ts
    return _transition(con, "dex_txs", "tx_hash", tx_hash.lower(), "state", state, DEX_TX_NEXT, None, fields, now)


def get_dex_tx(con, tx_hash: str) -> dict | None:
    return _row(con, "SELECT * FROM dex_txs WHERE tx_hash=?", (tx_hash.lower(),))


def dex_txs_unresolved(con) -> list[dict]:
    """Для сверки на старте: подписанные, отправленные и неизвестные — по возрастанию nonce."""
    return _rows(con, "SELECT * FROM dex_txs WHERE state IN (?,?,?) ORDER BY chain, wallet, nonce, id",
                 (str(DexTxState.SIGNED), str(DexTxState.SENT), str(DexTxState.UNKNOWN)))


def dex_txs_on_nonce(con, chain: str, wallet: str, nonce: int) -> list[dict]:
    """Все наши транзакции на одном nonce (своп, его bump и cancel): замайниться может только одна."""
    return _rows(con, "SELECT * FROM dex_txs WHERE chain=? AND wallet=? AND nonce=? ORDER BY id",
                 (chain, wallet.lower(), int(nonce)))


# --- perp_orders: запись-до для заявок --------------------------------------------------------------
def perp_order_intent(con, *, clip_id: int | None, client_id: str, venue: str, symbol: str, side: str,
                      reduce_only: bool, tif: str, price: Decimal, qty: Decimal) -> int:
    """client_id — в БД ДО отправки. UNIQUE не даст записать ту же попытку дважды."""
    if not _CLIENT_ID_RE.match(client_id):
        raise ValueError(f"client_id не проходит шаблон Aster: {client_id}")
    return con.execute("INSERT INTO perp_orders(clip_id, client_id, venue, symbol, side, reduce_only, tif, price, qty, "
                       "state) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (clip_id, client_id, venue, symbol, side, 1 if reduce_only else 0, tif, amt(price), amt(qty),
                        str(PerpOrderState.INTENT))).lastrowid


def perp_order_sent(con, client_id: str, sign_nonce: int | None = None, now: float | None = None) -> bool:
    ts = time.time() if now is None else now
    fields = {"sent_ts": ts}
    if sign_nonce is not None:
        fields["sign_nonce"] = int(sign_nonce)
    return _transition(con, "perp_orders", "client_id", client_id, "state", PerpOrderState.SENT, PERP_ORDER_NEXT,
                       PerpOrderState.INTENT, fields, now)


def perp_order_result(con, client_id: str, state: str, *, order_id: int | None = None,
                      executed_qty: Decimal | None = None, avg_price: Decimal | None = None,
                      cum_quote: Decimal | None = None, sign_nonce: int | None = None, err_code: int | None = None,
                      err: str | None = None, now: float | None = None) -> bool:
    ts = time.time() if now is None else now
    fields = {"order_id": order_id, "executed_qty": executed_qty, "avg_price": avg_price, "cum_quote": cum_quote,
              "sign_nonce": sign_nonce, "err_code": err_code, "err": err}
    fields = {k: v for k, v in fields.items() if v is not None}
    if str(state) != PerpOrderState.UNKNOWN:
        fields["resolved_ts"] = ts
    return _transition(con, "perp_orders", "client_id", client_id, "state", state, PERP_ORDER_NEXT, None, fields, now)


def record_perp_fill(con, fill: PerpFill, err: str | None = None, now: float | None = None) -> bool:
    """Итог PerpLeg.ioc/query → строка заявки. NOT_FOUND не пишется: «не выставлена» (NOT_PLACED) — решение
    движка после трёх -2013 и неизменных сделок/позиции, а не одного ответа."""
    if fill.status == "NOT_FOUND":
        raise ValueError("NOT_FOUND — не итог: NOT_PLACED ставит движок после проверки userTrades и positionRisk")
    return perp_order_result(con, fill.client_id, fill.status, order_id=fill.order_id, executed_qty=fill.qty,
                             avg_price=fill.avg_px, cum_quote=fill.quote, sign_nonce=fill.sign_nonce or None,
                             err_code=fill.err_code, err=err, now=now)


def get_perp_order(con, client_id: str) -> dict | None:
    return _row(con, "SELECT * FROM perp_orders WHERE client_id=?", (client_id,))


def perp_orders_unresolved(con) -> list[dict]:
    return _rows(con, "SELECT * FROM perp_orders WHERE state IN (?,?,?) ORDER BY id",
                 (str(PerpOrderState.INTENT), str(PerpOrderState.SENT), str(PerpOrderState.UNKNOWN)))


# --- сделки биржи и фандинг (только вставка) ----------------------------------------------------------
def add_perp_fills(con, venue: str, rows: list[dict]) -> int:
    """userTrades → perp_fills, INSERT OR IGNORE по (venue, trade_id). commission_abs — модуль (знак у бирж разный).
    Возвращает число НОВЫХ строк."""
    if not rows:
        return 0
    with tx(con):
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO perp_fills(venue, trade_id, order_id, price, qty, quote_qty, commission_abs, "
                        "commission_asset, maker, realized_pnl, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        [(venue, int(r["trade_id"]), r.get("order_id"), amt(r["price"]), amt(r["qty"]),
                          amt(r.get("quote_qty")),
                          None if r.get("commission_abs") is None else amt(abs(Decimal(str(r["commission_abs"])))),
                          r.get("commission_asset"), 1 if r.get("maker") else 0, amt(r.get("realized_pnl")),
                          int(r["ts"])) for r in rows])
        return con.total_changes - before


def last_trade_id(con, venue: str) -> int | None:
    r = con.execute("SELECT MAX(trade_id) FROM perp_fills WHERE venue=?", (venue,)).fetchone()
    return r[0] if r and r[0] is not None else None


def add_funding_income(con, venue: str, rows: list[dict]) -> int:
    """income?incomeType=FUNDING_FEE → funding_income; дедуп по tranId."""
    if not rows:
        return 0
    with tx(con):
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO funding_income(venue, tran_id, symbol, income, ts) VALUES(?,?,?,?,?)",
                        [(venue, int(r["tran_id"]), r["symbol"], amt(r["income"]), int(r["ts"])) for r in rows])
        return con.total_changes - before


# --- deal_marks: оценка сделок (trade/marks.py) -------------------------------------------------------------
MARK_COLS = ("px_dex", "px_perp", "pnl_now", "pnl_exit", "exit_cost", "funding", "fees", "gas")


def add_mark(con, deal_id: str, ts: float, *, flags: dict | None = None, **vals) -> None:
    """Строка оценки (только вставка). Суммы — Decimal через amt() (float — TypeError), None — «не посчитано».
    flags — JSON: ошибки чтения, разбивка цены выхода, «стакан не покрывает», sim; тексты проходят redact_secrets."""
    bad = [k for k in vals if k not in MARK_COLS]
    if bad:
        raise StoreError(f"deal_marks: нет таких полей {bad}")
    con.execute("INSERT INTO deal_marks(deal_id, ts, px_dex, px_perp, pnl_now, pnl_exit, exit_cost, funding, fees, gas, "
                "flags_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (deal_id, float(ts), *(amt(vals.get(c)) for c in MARK_COLS), redact_secrets(jdump(flags or {}))))


def last_mark(con, deal_id: str) -> dict | None:
    return _row(con, "SELECT * FROM deal_marks WHERE deal_id=? ORDER BY ts DESC, rowid DESC LIMIT 1", (deal_id,))


def prune_marks(con, now: float | None = None, keep_s: float = tconfig.MARK_KEEP_S) -> int:
    """Чистка: строки старше keep_s удаляются, кроме последней строки каждой сделки (итог закрытой сделки и последний
    расчёт застрявшей не пропадают). Возвращает число удалённых."""
    ts = time.time() if now is None else now
    return con.execute("DELETE FROM deal_marks WHERE ts < ? AND ts < (SELECT MAX(m.ts) FROM deal_marks m "
                       "WHERE m.deal_id = deal_marks.deal_id)", (ts - keep_s,)).rowcount


def event(con, kind: str, *, deal_id: str | None = None, intent_id: str | None = None, clip_id: int | None = None,
          now: float | None = None, **data) -> None:
    """Журнал исполнения (только вставка). Текст проходит redact_secrets — в журнал уходят и тексты ошибок."""
    ts = time.time() if now is None else now
    con.execute("INSERT INTO exec_events(ts, deal_id, intent_id, clip_id, kind, json) VALUES(?,?,?,?,?,?)",
                (ts, deal_id, intent_id, clip_id, kind, redact_secrets(jdump(data)) if data else None))


def events(con, deal_id: str | None = None) -> list[dict]:
    if deal_id is None:
        return _rows(con, "SELECT * FROM exec_events ORDER BY ts, rowid")
    return _rows(con, "SELECT * FROM exec_events WHERE deal_id=? ORDER BY ts, rowid", (deal_id,))
