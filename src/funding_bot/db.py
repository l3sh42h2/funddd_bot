"""SQLite (WAL). `funding_events` — только вставка, никогда не обновляется: это сырьё для сверки и порогов.
Снимки — ряд для исследования, чистятся по сроку с чекпойнтом WAL внутри чистки (урок polypin3: WAL 3.5 ГБ
при полном диске).

Таблицы `pairs` и `snapshots` — наследие двухбиржевой версии (до 10.09 вечера): не пишутся, `snapshots`
дочищается по сроку. Их колонки mid_a/mid_b/gap_bps всё время писались NULL (ревью 10.09) — данные неполные.
"""
from __future__ import annotations
import json, sqlite3, time, logging
from pathlib import Path
from . import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
  exchange TEXT, symbol TEXT, base_asset TEXT, base TEXT, factor REAL,
  tick_size REAL, step_size REAL, min_notional REAL, onboard_ms INTEGER,
  interval_h INTEGER, cap REAL, floor REAL,
  first_seen INTEGER, last_seen INTEGER,
  PRIMARY KEY (exchange, symbol));
CREATE TABLE IF NOT EXISTS pairs (
  base TEXT PRIMARY KEY, aster TEXT, binance TEXT, spot TEXT, factor_a REAL, factor_b REAL,
  mismatch INTEGER DEFAULT 0, ratio REAL, checked_ts INTEGER,
  first_seen INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS funding_events (
  exchange TEXT, symbol TEXT, funding_ms INTEGER, rate REAL, mark REAL, interval_h INTEGER, seen_ts INTEGER,
  PRIMARY KEY (exchange, symbol, funding_ms));
CREATE INDEX IF NOT EXISTS idx_fe_time ON funding_events(funding_ms);
CREATE TABLE IF NOT EXISTS snapshots (
  ts INTEGER, base TEXT, rate_a REAL, rate_b REAL, rate_h_a REAL, rate_h_b REAL, spread_h REAL,
  mark_a REAL, mark_b REAL, mid_a REAL, mid_b REAL, half_a_bps REAL, half_b_bps REAL,
  spot_mid REAL, gap_bps REAL,
  PRIMARY KEY (ts, base));
CREATE TABLE IF NOT EXISTS ff_snapshots (
  ts INTEGER, key TEXT, base TEXT, va TEXT, vb TEXT, rate_a REAL, rate_b REAL, iv_a INTEGER, iv_b INTEGER,
  spread REAL, px_a REAL, px_b REAL, gap REAL, mismatch INTEGER,
  PRIMARY KEY (ts, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sf_snapshots (
  ts INTEGER, key TEXT, base TEXT, perp_ex TEXT, rate REAL, interval_h INTEGER,
  px_spot REAL, px_perp REAL, gap REAL,
  PRIMARY KEY (ts, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS identity (
  key TEXT PRIMARY KEY, mismatch INTEGER, ratio REAL, checked_ts INTEGER);
CREATE TABLE IF NOT EXISTS ident_src (
  venue TEXT, key TEXT, data TEXT, ts INTEGER,
  PRIMARY KEY (venue, key));
CREATE TABLE IF NOT EXISTS leg_depth (
  exchange TEXT, symbol TEXT, checked_from INTEGER, ts INTEGER,
  PRIMARY KEY (exchange, symbol));
CREATE TABLE IF NOT EXISTS health (
  exchange TEXT PRIMARY KEY, last_ok_ts INTEGER, used_weight INTEGER, n_429 INTEGER, n_err INTEGER,
  note TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# колонки, добавленные после первого выката: (таблица, колонка, тип). Только ADD COLUMN — прежняя версия кода
# работает с новой схемой, поэтому откат выката не требует обратной миграции.
MIGRATIONS = [("sf_snapshots", "mismatch", "INTEGER"),
              ("leg_depth", "synced_to", "INTEGER"),       # курсор непрерывности (ревью 11.09, п.1)
              ("ff_snapshots", "stale", "INTEGER"),        # строка на устаревшей ставке (п.3)
              ("sf_snapshots", "stale", "INTEGER"),
              ("sf_snapshots", "spot_ex", "TEXT")]         # споты Gate/KuCoin/Bitget (12.09); NULL у старых = Binance


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in have:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():     # другой процесс мигрировал первым
                    raise
    con.commit()
    # Единица ff_snapshots.spread: до 10.09 ~21 UTC — за период пары, дальше — в час («приводи к 1ч»).
    # Один ряд не должен смешивать две единицы (урок Coinglass: проценты вместо долей обнулили эквити) —
    # старые строки пересчитываются один раз, отметка в meta не даёт поделить второй раз.
    con.execute("BEGIN IMMEDIATE")
    if con.execute("SELECT v FROM meta WHERE k='ff_spread_unit'").fetchone() is None:
        con.execute("UPDATE ff_snapshots SET spread = spread / MAX(iv_a, iv_b) "
                    "WHERE spread IS NOT NULL AND MAX(iv_a, iv_b) > 0")
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('ff_spread_unit','per_hour')")
    con.commit()
    return con


# --- instruments -------------------------------------------------------------------------
def upsert_instruments(con, rows: list[dict]):
    now = int(time.time())
    con.executemany("""INSERT INTO instruments(exchange,symbol,base_asset,base,factor,tick_size,step_size,min_notional,
        onboard_ms,interval_h,cap,floor,first_seen,last_seen)
      VALUES(:exchange,:symbol,:base_asset,:base,:factor,:tick_size,:step_size,:min_notional,:onboard_ms,
        :interval_h,:cap,:floor,:now,:now)
      ON CONFLICT(exchange,symbol) DO UPDATE SET base_asset=:base_asset, base=:base, factor=:factor,
        tick_size=:tick_size, step_size=:step_size, min_notional=:min_notional, onboard_ms=:onboard_ms,
        interval_h=:interval_h, cap=:cap, floor=:floor, last_seen=:now""",
        [{"interval_h": None, "cap": None, "floor": None, **r, "now": now} for r in rows])
    con.commit()


def load_instruments(con, exchange: str) -> dict[str, dict]:
    cur = con.execute("SELECT symbol,base_asset,base,factor,tick_size,step_size,min_notional,onboard_ms,interval_h,cap,floor "
                      "FROM instruments WHERE exchange=?", (exchange,))
    cols = [c[0] for c in cur.description]
    return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


# --- источники «того же актива» (identity_src): списки монет, состав индексов, описания HIP-3 ----------------------
# Таблица identity (прежний флаг по цене) больше не пишется — оставлена в схеме, чтобы откат выката работал.
def save_ident_src(con, venue: str, entries: dict[str, object], ts: int | None = None):
    now = int(ts or time.time())
    con.executemany("INSERT OR REPLACE INTO ident_src(venue,key,data,ts) VALUES(?,?,?,?)",
                    [(venue, k, json.dumps(v, separators=(",", ":"), ensure_ascii=False), now) for k, v in entries.items()])
    con.commit()


def load_ident_src(con) -> dict[str, dict[str, tuple[object, int]]]:
    out: dict[str, dict[str, tuple[object, int]]] = {}
    for venue, k, data, ts in con.execute("SELECT venue, key, data, ts FROM ident_src"):
        try:
            out.setdefault(venue, {})[k] = (json.loads(data), int(ts or 0))
        except (TypeError, ValueError):
            continue
    return out


# --- funding events ---------------------------------------------------------------------
def insert_funding_events(con, rows: list[dict], intervals: dict[str, int] | None = None) -> int:
    """INSERT OR IGNORE по (exchange, symbol, funding_ms). Возвращает число НОВЫХ строк."""
    if not rows:
        return 0
    now = int(time.time())
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO funding_events(exchange,symbol,funding_ms,rate,mark,interval_h,seen_ts) "
                    "VALUES(?,?,?,?,?,?,?)",
                    [(r["exchange"], r["symbol"], r["funding_ms"], r["rate"], r.get("mark"),
                      (intervals or {}).get(r["symbol"]), now) for r in rows])
    con.commit()
    return con.total_changes - before


def last_funding_ms(con, exchange: str, symbol: str) -> int | None:
    r = con.execute("SELECT MAX(funding_ms) FROM funding_events WHERE exchange=? AND symbol=?", (exchange, symbol)).fetchone()
    return r[0] if r and r[0] is not None else None


def funding_span(con, exchange: str, symbol: str) -> tuple[int | None, int | None]:
    """(первый, последний) известный расчёт символа; (None, None) — истории нет."""
    r = con.execute("SELECT MIN(funding_ms), MAX(funding_ms) FROM funding_events WHERE exchange=? AND symbol=?",
                    (exchange, symbol)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def funding_events_since(con, since_ms: int) -> dict[tuple[str, str], list[tuple[int, float]]]:
    """(exchange, symbol) -> [(funding_ms, rate), ...] по возрастанию времени."""
    out: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for ex, sym, ms, rate in con.execute("SELECT exchange,symbol,funding_ms,rate FROM funding_events WHERE funding_ms>=? "
                                         "ORDER BY funding_ms", (since_ms,)):
        out.setdefault((ex, sym), []).append((ms, rate))
    return out


def count_funding_events(con) -> int:
    return con.execute("SELECT COUNT(*) FROM funding_events").fetchone()[0]


# --- окно событий в памяти (events_window): добор по rowid вместо перечитывания 30 сут ---------------------------------
# Таблица только вставляется (INSERT OR IGNORE), так что новая строка всегда получает rowid больше всех живых. Условие
# на время в доборе — с «+»: иначе планировщик мог бы пойти по idx_fe_time (все строки окна) вместо отрезка rowid.
def funding_rowid_max(con) -> int:
    r = con.execute("SELECT MAX(rowid) FROM funding_events").fetchone()
    return int(r[0] or 0) if r else 0


def funding_rows_after(con, after_rowid: int, upto_rowid: int, since_ms: int) -> list[tuple[str, str, int, float]]:
    """Строки с rowid в (after, upto] и funding_ms ≥ since: (exchange, symbol, funding_ms, rate), порядок любой."""
    return con.execute("SELECT exchange,symbol,funding_ms,rate FROM funding_events WHERE rowid>? AND rowid<=? "
                       "AND +funding_ms>=?", (int(after_rowid), int(upto_rowid), int(since_ms))).fetchall()


def funding_rows_band(con, lo_ms: int, hi_ms: int, upto_rowid: int) -> list[tuple[str, str, int, float]]:
    """Строки с funding_ms в [lo, hi) и rowid ≤ upto — окно сдвинулось влево (часы коллектора пошли назад)."""
    return con.execute("SELECT exchange,symbol,funding_ms,rate FROM funding_events WHERE funding_ms>=? AND funding_ms<? "
                       "AND +rowid<=?", (int(lo_ms), int(hi_ms), int(upto_rowid))).fetchall()


def funding_tail(con, upto_rowid: int, k: int) -> list[tuple]:
    """Последние k строк по rowid до upto включительно, целиком: отпечаток хвоста. Если строки хвоста удалили, SQLite
    может выдать их rowid новым строкам, и добор «rowid > последнего» их бы не увидел — хвост это ловит."""
    return con.execute("SELECT rowid,exchange,symbol,funding_ms,rate FROM funding_events WHERE rowid>? AND rowid<=? "
                       "ORDER BY rowid", (int(upto_rowid) - int(k), int(upto_rowid))).fetchall()


def count_funding_events_since(con, since_ms: int) -> int:
    """Строк в окне (funding_ms ≥ since) — по idx_fe_time, таблицу не читает."""
    return con.execute("SELECT COUNT(*) FROM funding_events WHERE funding_ms>=?", (int(since_ms),)).fetchone()[0]


def leg_depths(con) -> dict[tuple[str, str], int]:
    return {(ex, sym): chk for ex, sym, chk in con.execute("SELECT exchange, symbol, checked_from FROM leg_depth")}


def leg_sync(con) -> dict[tuple[str, str], int]:
    """Курсор непрерывности ноги: история в БД полная на отрезке [checked_from, synced_to]. Нет курсора — нога
    из прежней версии (непрерывность не подтверждена) или ещё не собиралась."""
    return {(ex, sym): s for ex, sym, s in con.execute("SELECT exchange, symbol, synced_to FROM leg_depth WHERE synced_to IS NOT NULL")}


def set_leg_depth(con, exchange: str, symbol: str, checked_from: int, synced_to: int | None = None):
    """«Спросили биржу с checked_from, всё, что было, лежит в БД». Хранится самое раннее подтверждение; курсор
    непрерывности — самый поздний (сбор с пола подтверждает весь отрезок до synced_to)."""
    con.execute("""INSERT INTO leg_depth(exchange,symbol,checked_from,ts,synced_to) VALUES(?,?,?,?,?)
      ON CONFLICT(exchange,symbol) DO UPDATE SET checked_from=MIN(checked_from, excluded.checked_from), ts=excluded.ts,
        synced_to=CASE WHEN excluded.synced_to IS NULL THEN synced_to
                       ELSE MAX(COALESCE(synced_to, 0), excluded.synced_to) END""",
                (exchange, symbol, int(checked_from), int(time.time()), None if synced_to is None else int(synced_to)))
    con.commit()


def advance_sync(con, exchange: str, symbol: str, synced_to: int):
    """Сбор шёл с synced_to+1 — отрезок продолжен без разрыва. Назад курсор не двигается."""
    con.execute("UPDATE leg_depth SET synced_to=MAX(COALESCE(synced_to, 0), ?), ts=? WHERE exchange=? AND symbol=?",
                (int(synced_to), int(time.time()), exchange, symbol))
    con.commit()


def advance_sync_batch(con, exchange: str, targets: dict[str, int], cover_from: int) -> int:
    """Пакет «последние расчёты по всем монетам» полон с cover_from: у кого курсор уже не раньше cover_from,
    тот продолжен до своей цели. Возвращает, сколько ног сдвинуто."""
    before = con.total_changes
    now = int(time.time())
    con.executemany("UPDATE leg_depth SET synced_to=?, ts=? WHERE exchange=? AND symbol=? AND synced_to IS NOT NULL "
                    "AND synced_to >= ? AND synced_to < ?",
                    [(int(t), now, exchange, s, int(cover_from), int(t)) for s, t in targets.items()])
    con.commit()
    return con.total_changes - before


# --- snapshots / health / meta --------------------------------------------------------------
def insert_ff_snapshots(con, ts: int, rows: list[dict]):
    con.executemany("INSERT OR REPLACE INTO ff_snapshots(ts,key,base,va,vb,rate_a,rate_b,iv_a,iv_b,spread,px_a,px_b,gap,mismatch,stale) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(ts, r["key"], r["base"], r["va"], r["vb"], r.get("rate_a"), r.get("rate_b"), r.get("iv_a"), r.get("iv_b"),
                      r.get("spread"), r.get("px_a"), r.get("px_b"), r.get("gap"), 1 if r.get("mismatch") else 0,
                      1 if r.get("stale") else 0) for r in rows])
    con.commit()


def insert_sf_snapshots(con, ts: int, rows: list[dict]):
    con.executemany("INSERT OR REPLACE INTO sf_snapshots(ts,key,base,perp_ex,rate,interval_h,px_spot,px_perp,gap,mismatch,stale,"
                    "spot_ex) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(ts, r["key"], r["base"], r["perp_ex"], r.get("rate"), r.get("period"), r.get("px_spot"),
                      r.get("px_perp"), r.get("gap"), 1 if r.get("mismatch") else 0, 1 if r.get("stale") else 0,
                      r.get("spot_ex")) for r in rows])
    con.commit()


def purge_snapshots(con, keep_days: int = config.SNAPSHOT_KEEP_DAYS) -> int:
    cut = int(time.time()) - keep_days * 86400
    before = con.total_changes
    for t in ("snapshots", "ff_snapshots", "sf_snapshots"):
        con.execute(f"DELETE FROM {t} WHERE ts<?", (cut,))
    con.commit()
    n = con.total_changes - before
    # чекпойнт внутри чистки: иначе удалённые страницы живут в WAL до следующего случая
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return n


def write_health(con, rows: list[dict]):
    now = int(time.time())
    con.executemany("INSERT OR REPLACE INTO health(exchange,last_ok_ts,used_weight,n_429,n_err,note,ts) VALUES(?,?,?,?,?,?,?)",
                    [(r["exchange"], r.get("last_ok_ts"), r.get("used_weight"), r.get("n_429"), r.get("n_err"),
                      r.get("note"), now) for r in rows])
    con.commit()


def set_meta(con, k: str, v: str):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, v)); con.commit()


def get_meta(con, k: str, default: str | None = None) -> str | None:
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default
