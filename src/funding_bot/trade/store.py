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

Схема 2 (связка SOL×HL, ТЗ 13.09 §6, §12) — только добавление: журналы Solana (sol_tx_attempts, свидетельства, чеки) и
Hyperliquid (hl_nonces, hl_order_attempts), корневые цели операций (operations), журнал сравнения маршрутов
(route_candidates), статьи расходов (fee_events), колонка deals.perp_scope (одна незакрытая сделка на scope перпа).
Миграция — одной транзакцией вместе с таблицей schema_version; ворота: код не стартует на БД, которой нужен читатель
новее (min_reader > SCHEMA_VERSION). Прежний код (фаза 1) новых таблиц не видит и работает с этой схемой как раньше.
"""
from __future__ import annotations
import hashlib, json, re, secrets, sqlite3, time
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
  carry TEXT, dust TEXT, updated REAL, inst_json TEXT, perp_scope TEXT);
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

# --- схема 2: версия, ворота, журналы SOL×HL ---------------------------------------------------------------------
# Версия схемы и ворота (M06): код откажется стартовать, если min_reader БД больше его SCHEMA_VERSION — старый код на
# БД со сделками, которых он не понимает, выбрал бы не те ноги. min_reader только растёт (require_reader).
SCHEMA_VERSION = 2
MIN_READER = 2

# состояния trade/solana/journal.AttemptState для частичных индексов (журнал сверяет их с собой при открытии)
SOL_TX_INFLIGHT_STATES = ("BROADCAST_ATTEMPTED", "SIGNED_DURABLE", "UNKNOWN")
SOL_TX_OPEN_STATES = SOL_TX_INFLIGHT_STATES + ("CONFIRMED_ERR", "CONFIRMED_OK", "VALIDATED")


def _in(states) -> str:
    return "(" + ", ".join(f"'{s}'" for s in sorted(states)) + ")"


# журнал попыток Solana (протокол и проверки — trade/solana/journal.py): запись-до, одна попытка в полёте на кошелёк
SOL_JOURNAL_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sol_meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sol_tx_attempts(
  attempt_id TEXT PRIMARY KEY,
  network TEXT NOT NULL,                  -- genesis hash сети
  wallet TEXT NOT NULL,
  logical_action_id TEXT NOT NULL,        -- экономическое действие (клип/подготовка)
  predecessor_attempt_id TEXT REFERENCES sol_tx_attempts(attempt_id),
  clip_ref TEXT,
  provider TEXT NOT NULL,
  path TEXT NOT NULL,
  request_id TEXT,
  payload_kind TEXT NOT NULL,
  message_hash TEXT NOT NULL,
  recent_blockhash TEXT NOT NULL,
  last_valid_block_height INTEGER,
  lvbh_exact INTEGER NOT NULL CHECK (lvbh_exact IN (0, 1)),
  blockhash_slot INTEGER,
  provider_pending INTEGER NOT NULL DEFAULT 0 CHECK (provider_pending IN (0, 1)),
  plan_json TEXT NOT NULL,
  signature TEXT,
  signed_payload BLOB,
  state TEXT NOT NULL,
  outcome_json TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS sol_tx_signature ON sol_tx_attempts(network, signature)
  WHERE signature IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sol_tx_request ON sol_tx_attempts(network, provider, request_id)
  WHERE request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS sol_tx_one_open_per_action ON sol_tx_attempts(logical_action_id)
  WHERE state IN {_in(SOL_TX_OPEN_STATES)};
CREATE UNIQUE INDEX IF NOT EXISTS sol_tx_one_inflight_per_wallet ON sol_tx_attempts(network, wallet)
  WHERE state IN {_in(SOL_TX_INFLIGHT_STATES)};
CREATE INDEX IF NOT EXISTS sol_tx_state ON sol_tx_attempts(state);
CREATE INDEX IF NOT EXISTS sol_tx_action ON sol_tx_attempts(logical_action_id);
-- закреплённое при проверке и подписанные байты не переписываются
CREATE TRIGGER IF NOT EXISTS sol_tx_pinned BEFORE UPDATE ON sol_tx_attempts
  WHEN NEW.network IS NOT OLD.network OR NEW.wallet IS NOT OLD.wallet
    OR NEW.logical_action_id IS NOT OLD.logical_action_id OR NEW.message_hash IS NOT OLD.message_hash
    OR NEW.recent_blockhash IS NOT OLD.recent_blockhash
    OR NEW.last_valid_block_height IS NOT OLD.last_valid_block_height OR NEW.lvbh_exact IS NOT OLD.lvbh_exact
    OR NEW.blockhash_slot IS NOT OLD.blockhash_slot OR NEW.plan_json IS NOT OLD.plan_json
    OR (OLD.signature IS NOT NULL AND NEW.signature IS NOT OLD.signature)
    OR (OLD.signed_payload IS NOT NULL AND NEW.signed_payload IS NOT OLD.signed_payload)
  BEGIN SELECT RAISE(ABORT, 'sol_tx_attempts: pinned fields are immutable'); END;
CREATE TRIGGER IF NOT EXISTS sol_tx_no_delete BEFORE DELETE ON sol_tx_attempts
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS sol_tx_evidence(id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL
  REFERENCES sol_tx_attempts(attempt_id), ts REAL NOT NULL, kind TEXT NOT NULL, state_from TEXT, state_to TEXT,
  json TEXT);
CREATE INDEX IF NOT EXISTS sol_tx_evidence_attempt ON sol_tx_evidence(attempt_id, id);
CREATE TRIGGER IF NOT EXISTS sol_tx_evidence_no_update BEFORE UPDATE ON sol_tx_evidence
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS sol_tx_evidence_no_delete BEFORE DELETE ON sol_tx_evidence
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

CREATE TABLE IF NOT EXISTS sol_receipts(
  network TEXT NOT NULL, signature TEXT NOT NULL, logical_leg TEXT NOT NULL,
  attempt_id TEXT NOT NULL REFERENCES sol_tx_attempts(attempt_id),
  slot INTEGER NOT NULL, block_time INTEGER, ok INTEGER NOT NULL, err_json TEXT, fee_lamports TEXT NOT NULL,
  flows_json TEXT NOT NULL, receipt_hash TEXT NOT NULL, commitment TEXT NOT NULL,
  first_seen REAL NOT NULL, updated REAL NOT NULL,
  PRIMARY KEY(network, signature, logical_leg));
-- факт чека неизменен; меняется только уровень финальности
CREATE TRIGGER IF NOT EXISTS sol_receipts_facts BEFORE UPDATE ON sol_receipts
  WHEN NEW.attempt_id IS NOT OLD.attempt_id OR NEW.slot IS NOT OLD.slot OR NEW.ok IS NOT OLD.ok
    OR NEW.err_json IS NOT OLD.err_json OR NEW.fee_lamports IS NOT OLD.fee_lamports
    OR NEW.flows_json IS NOT OLD.flows_json OR NEW.receipt_hash IS NOT OLD.receipt_hash
  BEGIN SELECT RAISE(ABORT, 'sol_receipts: facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS sol_receipts_no_delete BEFORE DELETE ON sol_receipts
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""

# журнал адаптера Hyperliquid (trade/hyperliquid_trade.HlJournal): nonce агента и попытки (client_id ↔ cloid, nonce,
# хэш действия, подпись, исход). hl_order_attempts — perp_order_attempts ТЗ §12 для HL; deal_id/intent_id/clip_id —
# прямые ссылки движка (не разбор client_id)
HL_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS hl_nonces(network TEXT NOT NULL, signer TEXT NOT NULL, last_nonce INTEGER NOT NULL,
  updated REAL, PRIMARY KEY(network, signer));
CREATE TABLE IF NOT EXISTS hl_order_attempts(client_id TEXT PRIMARY KEY, cloid TEXT UNIQUE, kind TEXT NOT NULL,
  network TEXT NOT NULL, master TEXT NOT NULL, account TEXT NOT NULL, vault TEXT, signer TEXT NOT NULL, dex TEXT,
  fullcoin TEXT, asset INT, side TEXT, sz TEXT, px TEXT, reduce_only INT, nonce INTEGER NOT NULL,
  expires_after INTEGER, action_json TEXT NOT NULL, action_hash TEXT NOT NULL, sig_json TEXT, state TEXT NOT NULL,
  http INT, response_json TEXT, filled TEXT, avg_px TEXT, oid INTEGER, err_kind TEXT, err TEXT, created REAL,
  signed_ts REAL, resolved_ts REAL, deal_id TEXT, intent_id TEXT, clip_id INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS hl_attempts_nonce ON hl_order_attempts(network, signer, nonce);
CREATE INDEX IF NOT EXISTS hl_attempts_scope ON hl_order_attempts(network, account, fullcoin, state);
"""
_JOURNALS = {"sol": SOL_JOURNAL_SCHEMA, "hl": HL_JOURNAL_SCHEMA}

# учёт HL (ТЗ §12, §13; H15–H17): фактические fills и начисления фандинга счёта — ключ по пространству API (сеть, счёт,
# fullcoin с dex, время, tid/hash), а не глобальный max(id) площадки; курсор — непрозрачная отметка времени по scope,
# ставится ТОЛЬКО после записи строк. hash фандинга у HL обычно нулевой — ключ хранит '' вместо NULL (NULL в PRIMARY
# KEY SQLite уникальность не держит).
HL_ACCT_SCHEMA = """
CREATE TABLE IF NOT EXISTS hl_fills(network TEXT NOT NULL, account TEXT NOT NULL, coin TEXT NOT NULL,
  time INTEGER NOT NULL, tid INTEGER NOT NULL, oid INTEGER NOT NULL, cloid TEXT, side TEXT NOT NULL, px TEXT NOT NULL,
  sz TEXT NOT NULL, fee TEXT NOT NULL, fee_token TEXT, builder_fee TEXT, closed_pnl TEXT, hash TEXT, ingested REAL,
  PRIMARY KEY(network, account, coin, time, tid));
CREATE INDEX IF NOT EXISTS hl_fills_cloid ON hl_fills(cloid);
CREATE TRIGGER IF NOT EXISTS hl_fills_no_update BEFORE UPDATE ON hl_fills BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS hl_fills_no_delete BEFORE DELETE ON hl_fills BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS hl_funding(network TEXT NOT NULL, account TEXT NOT NULL, coin TEXT NOT NULL,
  time INTEGER NOT NULL, hash TEXT NOT NULL, usdc TEXT NOT NULL, szi TEXT, rate TEXT, ingested REAL,
  PRIMARY KEY(network, account, coin, time, hash));
CREATE TRIGGER IF NOT EXISTS hl_funding_no_update BEFORE UPDATE ON hl_funding BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS hl_funding_no_delete BEFORE DELETE ON hl_funding BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS ingest_cursors(scope TEXT PRIMARY KEY, watermark_ms INTEGER, complete INTEGER NOT NULL,
  gap TEXT, updated REAL NOT NULL);
"""
# колонки, добавленные к журналам после их первой версии (таблица могла быть создана раньше)
_JOURNAL_COLUMNS = {"hl": (("hl_order_attempts", "deal_id", "TEXT"), ("hl_order_attempts", "intent_id", "TEXT"),
                           ("hl_order_attempts", "clip_id", "INTEGER"))}

_DIGITS = "<> '' AND {c} NOT GLOB '*[^0-9]*'"      # сырое целое ≥ 0 строкой: только цифры


def _raw_check(col: str, null: bool = False) -> str:
    body = f"{col} {_DIGITS.format(c=col)}"
    return f"CHECK ({col} IS NULL OR ({body}))" if null else f"CHECK ({body})"


SCHEMA_V2 = f"""
CREATE TABLE IF NOT EXISTS schema_version(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL,
  min_reader INTEGER NOT NULL, updated REAL NOT NULL, note TEXT);
-- одна незакрытая сделка владеет (площадка, сеть, счёт, dex, fullcoin): у HL одна net-позиция на рынок счёта
CREATE UNIQUE INDEX IF NOT EXISTS deals_one_per_perp_scope ON deals(perp_scope)
  WHERE perp_scope IS NOT NULL AND state NOT IN ('DRAFT', 'CLOSED', 'ABORTED');

-- корневая операция (ТЗ §6): цель неизменна, исполнено — только по доказанным фактам, в полёте — не ноль
CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  inst_hash TEXT NOT NULL, mode TEXT NOT NULL, side TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_asset TEXT NOT NULL, target_decimals INTEGER NOT NULL, target_raw TEXT NOT NULL {_raw_check("target_raw")},
  confirmed_raw TEXT NOT NULL {_raw_check("confirmed_raw")}, reserved_raw TEXT NOT NULL {_raw_check("reserved_raw")},
  fee_cap_raw TEXT {_raw_check("fee_cap_raw", null=True)}, bounds_json TEXT, bounds_hash TEXT,
  approval_version INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS operations_deal ON operations(deal_id);
CREATE UNIQUE INDEX IF NOT EXISTS operations_one_active_per_deal ON operations(deal_id)
  WHERE state IN ('APPROVED', 'RUNNING', 'PARTIAL', 'STOPPED', 'PAUSED_RISK', 'PAUSED_UNKNOWN');
CREATE TRIGGER IF NOT EXISTS operations_target_immutable BEFORE UPDATE ON operations
  WHEN NEW.id IS NOT OLD.id OR NEW.deal_id IS NOT OLD.deal_id OR NEW.profile_id IS NOT OLD.profile_id
    OR NEW.inst_hash IS NOT OLD.inst_hash OR NEW.mode IS NOT OLD.mode OR NEW.side IS NOT OLD.side
    OR NEW.target_kind IS NOT OLD.target_kind OR NEW.target_asset IS NOT OLD.target_asset
    OR NEW.target_decimals IS NOT OLD.target_decimals OR NEW.target_raw IS NOT OLD.target_raw
    OR NEW.fee_cap_raw IS NOT OLD.fee_cap_raw OR NEW.created IS NOT OLD.created
  BEGIN SELECT RAISE(ABORT, 'operations: root target is immutable'); END;
CREATE TRIGGER IF NOT EXISTS operations_no_settle_with_reserved BEFORE UPDATE OF state ON operations
  WHEN NEW.state IN ('OPEN', 'CLOSED', 'ABANDONED') AND NEW.reserved_raw <> '0'
  BEGIN SELECT RAISE(ABORT, 'operations: in-flight amount is not resolved'); END;
CREATE TRIGGER IF NOT EXISTS operations_no_delete BEFORE DELETE ON operations
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TABLE IF NOT EXISTS operation_intents(intent_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL
  REFERENCES operations(id), seq INTEGER NOT NULL, UNIQUE(operation_id, seq));
CREATE TRIGGER IF NOT EXISTS operation_intents_no_update BEFORE UPDATE ON operation_intents
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS operation_intents_no_delete BEFORE DELETE ON operation_intents
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- сравнение маршрутов (ТЗ §12, spot_router.Decision.records): все кандидаты, причины исключения, выбранный
CREATE TABLE IF NOT EXISTS route_candidates(id INTEGER PRIMARY KEY, ts REAL NOT NULL, operation_id TEXT NOT NULL,
  clip_seq INTEGER NOT NULL, round_no INTEGER NOT NULL, candidate_id TEXT, provider TEXT NOT NULL, path TEXT NOT NULL,
  provider_group TEXT, side TEXT, route_fingerprint TEXT, request_hash TEXT, amount_in_raw TEXT, expected_out_raw TEXT,
  min_out_raw TEXT, onchain_min_out_raw TEXT, metric TEXT, conservative TEXT, external_cost TEXT, pair_edge TEXT,
  pair_basis_bps TEXT, pair_margin TEXT, received_at REAL, provider_time REAL, latency_ms INTEGER, built INTEGER,
  message_hash TEXT, request_id TEXT, rank_no INTEGER, eligible INTEGER NOT NULL, selected INTEGER NOT NULL,
  preview_selected INTEGER NOT NULL, response_hash TEXT, metric_version TEXT, decision TEXT, fees_json TEXT,
  reasons_json TEXT, notes_json TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS route_candidates_uniq ON route_candidates(operation_id, clip_seq, round_no, candidate_id)
  WHERE candidate_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS route_candidates_unavailable ON route_candidates(operation_id, clip_seq, round_no,
  provider, path) WHERE candidate_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS route_candidates_one_selected ON route_candidates(operation_id, clip_seq, round_no)
  WHERE selected = 1;
CREATE TRIGGER IF NOT EXISTS route_candidates_no_update BEFORE UPDATE ON route_candidates
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS route_candidates_no_delete BEFORE DELETE ON route_candidates
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;

-- статьи расходов (ТЗ §12, §13): актив/decimals/сырая сумма (NULL — неизвестно, не 0), плательщик, включена ли в
-- суммы свопа, возвратный депозит отдельно; оценка в валюте — со временем и источником
CREATE TABLE IF NOT EXISTS fee_events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, origin_kind TEXT NOT NULL,
  origin_ref TEXT NOT NULL, idx INTEGER NOT NULL, deal_id TEXT, operation_id TEXT, clip_id INTEGER, kind TEXT NOT NULL,
  asset TEXT NOT NULL, decimals INTEGER NOT NULL, amount_raw TEXT {_raw_check("amount_raw", null=True)}, payer TEXT,
  recipient TEXT, included INTEGER NOT NULL, estimated INTEGER NOT NULL, refundable INTEGER NOT NULL,
  superseded INTEGER NOT NULL, source TEXT, note TEXT, val_unit TEXT, val_amount TEXT, val_ts REAL, val_source TEXT,
  UNIQUE(origin_kind, origin_ref, idx));
CREATE INDEX IF NOT EXISTS fee_events_deal ON fee_events(deal_id);
CREATE TRIGGER IF NOT EXISTS fee_events_no_update BEFORE UPDATE ON fee_events
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
CREATE TRIGGER IF NOT EXISTS fee_events_no_delete BEFORE DELETE ON fee_events
  BEGIN SELECT RAISE(ABORT, 'append-only'); END;
""" + SOL_JOURNAL_SCHEMA + HL_JOURNAL_SCHEMA + HL_ACCT_SCHEMA


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


class SchemaTooNew(StoreError):
    """БД требует читателя новее этого кода (откат кода поверх новой схемы): не запускаемся, БД не трогаем."""


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
    try:
        _gate(con)                              # ворота версии — до любой записи в БД
        con.executescript(SCHEMA)
        for t, c, d in _ADD_COLUMNS:
            _ensure_column(con, t, c, d)
        _migrate(con)
    except BaseException:
        con.close()
        raise
    return con


# Миграции схемы — ТОЛЬКО добавление колонок, идемпотентно на каждом connect(): прежняя версия кода (откат на .prev)
# новую колонку не видит (INSERT с явным списком колонок, SELECT * — лишний ключ).
_ADD_COLUMNS = (("deals", "inst_json", "TEXT"),       # ревью 13.09, С1/Н2: спецификация инструмента сделки
                ("deals", "perp_scope", "TEXT"))      # SOL×HL §6: чья net-позиция перпа (schema 2; у BSC — NULL)


def _schema_row(con) -> tuple[int, int] | None:
    """(version, min_reader) или None — БД до схемы 2 (таблицы ещё нет)."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone() is None:
        return None
    r = con.execute("SELECT version, min_reader FROM schema_version WHERE id=1").fetchone()
    return None if r is None else (int(r[0]), int(r[1]))


def _gate(con) -> None:
    row = _schema_row(con)
    if row is not None and row[1] > SCHEMA_VERSION:
        raise SchemaTooNew(f"trade.db схемы {row[0]} читает только код версии ≥ {row[1]}, а этот код — {SCHEMA_VERSION}: "
                           "не запускаюсь (откат кода поверх новой БД запрещён)")


def schema_info(con) -> dict | None:
    return _row(con, "SELECT * FROM schema_version WHERE id=1") if _schema_row(con) is not None else None


def _run_ddl(con, script: str) -> None:
    """DDL по одному оператору: executescript сам делает COMMIT и вышел бы из транзакции миграции."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            con.execute(buf)
            buf = ""
    if any(ln.strip() and not ln.strip().startswith("--") for ln in buf.splitlines()):
        raise StoreError("DDL: незавершённый оператор")


def _ensure_acct(con) -> None:
    """Таблицы учёта HL (HL_ACCT_SCHEMA) на БД, уже бывшей схемой 2 до их появления: только добавление, одной
    транзакцией; прежний код их не читает и не видит."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_cursors'").fetchone() is not None:
        return
    with tx(con):
        _run_ddl(con, HL_ACCT_SCHEMA)


def _migrate(con, now: float | None = None) -> None:
    """Схема 2 — одной транзакцией вместе со строкой schema_version: сбой посередине не оставит половины (M05).
    Повтор безвреден; версия не понижается (БД новее, но совместимая, остаётся своей версии)."""
    row = _schema_row(con)
    if row is not None and row[0] >= SCHEMA_VERSION:
        _ensure_acct(con)
        return
    ts = time.time() if now is None else now
    with tx(con):
        _gate(con)                              # под блокировкой записи: другой процесс мог успеть
        row = _schema_row(con)
        if row is not None and row[0] >= SCHEMA_VERSION:
            return
        _run_ddl(con, SCHEMA_V2)
        for t, c, d in _JOURNAL_COLUMNS["hl"]:
            _ensure_column(con, t, c, d)
        con.execute("INSERT INTO schema_version(id, version, min_reader, updated, note) VALUES(1, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version, "
                    "min_reader=MAX(schema_version.min_reader, excluded.min_reader), updated=excluded.updated, "
                    "note=excluded.note", (SCHEMA_VERSION, MIN_READER, ts, "SOL×HL: журналы, операции, маршруты, расходы"))


def require_reader(con, version: int, now: float | None = None) -> None:
    """Поднять min_reader: код ниже version на этой БД больше не стартует (например, появилась сделка, которую
    прежний код не понимает). Никогда не понижает; требовать читателя новее себя нельзя."""
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= SCHEMA_VERSION:
        raise ValueError(f"min_reader {version!r}: от 1 до {SCHEMA_VERSION}")
    ts = time.time() if now is None else now
    with tx(con):
        _gate(con)
        if _schema_row(con) is None:
            raise StoreError("schema_version нет — сначала connect()")
        con.execute("UPDATE schema_version SET min_reader=MAX(min_reader, ?), updated=? WHERE id=1", (version, ts))


def ensure_journal_tables(con, *groups: str) -> None:
    """Таблицы журналов SOL/HL на любом соединении (trade.db после connect() их уже имеет; отдельная БД журнала
    адаптера — создаются здесь). Идемпотентно; ворота версии — те же."""
    unknown = [g for g in groups if g not in _JOURNALS]
    if unknown or not groups:
        raise ValueError(f"журналы: {unknown or 'не указаны'} (есть {sorted(_JOURNALS)})")
    if con.row_factory is None:
        con.row_factory = sqlite3.Row
    with tx(con):
        _gate(con)
        for g in groups:
            _run_ddl(con, _JOURNALS[g])
            for t, c, d in _JOURNAL_COLUMNS.get(g, ()):
                _ensure_column(con, t, c, d)


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


def execution_paused(con) -> bool:
    """Owner pause or durable deployment fence, including native signing gates."""
    if is_paused(con):
        return True
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_meta'").fetchone():
        return False
    row = con.execute("SELECT value FROM core_meta WHERE key='deployment_drain'").fetchone()
    if row is None:
        return False
    try:
        value = json.loads(row[0]).get('drain')
        if type(value) is not bool:
            return True
        return value
    except (ValueError, TypeError, AttributeError):
        return True


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
    tok, scope = _deal_token(chain, token, token_dec, perp_venue, symbol, inst)
    with tx(con):
        did = deal_id
        while did is None:
            cand = new_id("D")
            if con.execute("SELECT 1 FROM deals WHERE id=?", (cand,)).fetchone() is None:
                did = cand
        con.execute("INSERT INTO deals(id, created, state, reason, coin, chain, token, token_dec, perp_venue, symbol, "
                    "leg_usd, owner_json, sim, carry, dust, updated, inst_json, perp_scope) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, ts, str(DealState.DRAFT), None, coin, chain, tok, int(token_dec), perp_venue, symbol,
                     amt(leg_usd), owner_json, 1 if sim else 0, "0", "0", ts, inst.to_json() if inst else None, scope))
    return did


_SOL_CHAINS = frozenset({"sol", "solana", "solana-mainnet"})


def _deal_token(chain: str, token: str, token_dec: int, perp_venue: str, symbol: str,
                inst: InstrumentSpec | None) -> tuple[str, str | None]:
    """Ключ токена и scope перпа сделки. schema 1 (okx·bsc × Aster) — как раньше: адрес нижним регистром, scope нет.
    schema 2 — mint base58 как есть, строка сделки обязана совпасть с замороженной спецификацией."""
    if inst is not None and inst.schema >= 2:
        if (chain, token, int(token_dec), perp_venue, symbol) != (inst.chain, inst.token, inst.token_dec,
                                                                  inst.perp_venue, inst.perp_symbol):
            raise StoreError("сделка и её спецификация инструмента расходятся (сеть/токен/decimals/площадка/символ)")
        return token, inst.perp_scope
    if str(chain).strip().lower() in _SOL_CHAINS:
        raise StoreError("сделка Solana — только со спецификацией schema 2 (mint с регистром, scope перпа)")
    return token.lower(), None


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
        with tx(con):
            n = con.execute("UPDATE intents SET status=?, approved=? WHERE id=? AND nonce=? AND status=? AND expires>?",
                            (str(IntentStatus.APPROVED), ts, intent_id, nonce, str(IntentStatus.PROPOSED), ts)).rowcount
            if n:
                from .operation_roots import approve_linked
                approve_linked(con, get_intent(con, intent_id))
            return n == 1
    except sqlite3.IntegrityError:
        raise StoreBusy("уже идёт другое исполнение") from None


def _close_proposed_root(con, intent_id, state):
    op = operation_of_intent(con, intent_id)
    if op is not None and op['state'] == OpState.PROPOSED:
        set_operation_state(con, op['id'], state, expect=OpState.PROPOSED)


def reject_intent(con, intent_id: str, nonce: str) -> bool:
    with tx(con):
        n = con.execute("UPDATE intents SET status=? WHERE id=? AND nonce=? AND status=?",
                        (str(IntentStatus.REJECTED), intent_id, nonce, str(IntentStatus.PROPOSED))).rowcount
        if n:
            _close_proposed_root(con, intent_id, OpState.REJECTED)
        return n == 1


def expire_intents(con, now: float | None = None) -> list[str]:
    """Просроченные предложения → expired; возвращает их id (снять кнопки с сообщений)."""
    ts = time.time() if now is None else now
    with tx(con):
        ids = [r[0] for r in con.execute("SELECT id FROM intents WHERE status=? AND expires<=?",
                                         (str(IntentStatus.PROPOSED), ts))]
        if ids:
            con.executemany("UPDATE intents SET status=? WHERE id=? AND status=?",
                            [(str(IntentStatus.EXPIRED), i, str(IntentStatus.PROPOSED)) for i in ids])
            for iid in ids:
                _close_proposed_root(con, iid, OpState.EXPIRED)
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
                  now: float | None = None, new_action: bool = True) -> int:
    """Подписанная транзакция — В БД ДО отправки (on_signed у EvmWallet). После падения рестарт найдёт хэш и
    nonce и спросит сеть, а не подпишет новую на новом nonce."""
    if str(kind) not in {k.value for k in DexTxKind}:
        raise ValueError(f"неизвестный вид транзакции: {kind}")
    with tx(con):
        if new_action and execution_paused(con):
            raise StoreError('new EVM action fenced by owner/deployment pause')
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


# --- учёт Hyperliquid: fills и фандинг счёта (ТЗ §12, §13; H15–H17) ------------------------------------------------
def _fill_tuple(r: dict, ts: float) -> tuple:
    return (str(r["network"]), str(r["account"]).lower(), str(r["coin"]), int(r["time"]), int(r["tid"]), int(r["oid"]),
            r.get("cloid"), str(r["side"]), amt(r["px"]), amt(r["sz"]), amt(r["fee"]), r.get("fee_token"),
            amt(r.get("builder_fee")), amt(r.get("closed_pnl")), r.get("hash"), ts)


def add_hl_fills(con, rows: Iterable[dict], now: float | None = None) -> int:
    """hl_rules.fill_row → hl_fills, один раз по (сеть, счёт, fullcoin, время, tid). Повтор страницы — 0 новых; тот же
    ключ с другим oid/hash/объёмом — StoreError (коллизия, не молча вторая сделка). Возвращает число новых строк."""
    ts = time.time() if now is None else now
    rows = [_fill_tuple(r, ts) for r in rows]
    n = 0
    with tx(con):
        for t in rows:
            old = con.execute("SELECT oid, hash, sz, px FROM hl_fills WHERE network=? AND account=? AND coin=? AND "
                              "time=? AND tid=?", t[:5]).fetchone()
            if old is not None:
                if (int(old[0]), old[1], old[2], old[3]) != (t[5], t[14], t[9], t[8]):
                    raise StoreError(f"hl_fills: коллизия ключа {t[:5]} (oid/hash/объём другие)")
                continue
            con.execute("INSERT INTO hl_fills(network, account, coin, time, tid, oid, cloid, side, px, sz, fee, fee_token, "
                        "builder_fee, closed_pnl, hash, ingested) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", t)
            n += 1
    return n


def add_hl_funding(con, rows: Iterable[dict], now: float | None = None) -> int:
    """hl_rules.funding_row → hl_funding, один раз по (сеть, счёт, fullcoin, время, hash). Перекрытие страниц — 0 новых;
    тот же ключ с другой суммой — StoreError."""
    ts = time.time() if now is None else now
    n = 0
    with tx(con):
        for r in rows:
            key = (str(r["network"]), str(r["account"]).lower(), str(r["coin"]), int(r["time"]), str(r.get("hash") or ""))
            old = con.execute("SELECT usdc FROM hl_funding WHERE network=? AND account=? AND coin=? AND time=? AND hash=?",
                              key).fetchone()
            if old is not None:
                if old[0] != amt(r["usdc"]):
                    raise StoreError(f"hl_funding: коллизия ключа {key} (сумма другая)")
                continue
            con.execute("INSERT INTO hl_funding(network, account, coin, time, hash, usdc, szi, rate, ingested) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (*key, amt(r["usdc"]), amt(r.get("szi")), amt(r.get("rate")), ts))
            n += 1
    return n


def get_cursor(con, scope: str) -> dict | None:
    return _row(con, "SELECT * FROM ingest_cursors WHERE scope=?", (scope,))


def set_cursor(con, scope: str, *, watermark_ms: int | None, complete: bool, gap: str | None = None,
               now: float | None = None) -> None:
    """Отметка добора по scope — вызывать ПОСЛЕ записи строк (падение между ними — повтор с перекрытием, а не пропуск).
    Отметка не уходит назад: неполная страница оставляет прежнюю."""
    ts = time.time() if now is None else now
    with tx(con):
        old = get_cursor(con, scope)
        wm = watermark_ms
        if old is not None and old["watermark_ms"] is not None and (wm is None or int(old["watermark_ms"]) > int(wm)):
            wm = int(old["watermark_ms"])
        con.execute("INSERT INTO ingest_cursors(scope, watermark_ms, complete, gap, updated) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(scope) DO UPDATE SET watermark_ms=excluded.watermark_ms, complete=excluded.complete, "
                    "gap=excluded.gap, updated=excluded.updated",
                    (scope, None if wm is None else int(wm), 1 if complete else 0, gap, ts))


def supersede_intents(con, deal_id: str, keep: str) -> list[str]:
    """Новый план сделки делает прежние её предложения неактуальными: proposed → expired (кнопки старого плана больше
    не исполняются — одобрить можно только proposed). Возвращает id снятых (бот снимет с них кнопки)."""
    with tx(con):
        ids = [r[0] for r in con.execute("SELECT id FROM intents WHERE deal_id=? AND status=? AND id<>?",
                                         (deal_id, str(IntentStatus.PROPOSED), keep))]
        con.executemany("UPDATE intents SET status=?, err=COALESCE(err, 'superseded') WHERE id=? AND status=?",
                        [(str(IntentStatus.EXPIRED), i, str(IntentStatus.PROPOSED)) for i in ids])
        for iid in ids:
            _close_proposed_root(con, iid, OpState.EXPIRED)
    return ids


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


# --- operations: корневая цель операции (ТЗ §6) --------------------------------------------------------------------
class OpState(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    RUNNING = "RUNNING"
    PARTIAL = "PARTIAL"                  # остановилась с остатком цели: продолжение — новым одобрением ТОЙ ЖЕ цели
    STOPPED = "STOPPED"                  # «стоп»/рестарт: новых свопов нет, остаток цели сохранён
    PAUSED_RISK = "PAUSED_RISK"
    PAUSED_UNKNOWN = "PAUSED_UNKNOWN"    # исход отправки неизвестен: резерв держится, новых отправок нет
    OPEN = "OPEN"                        # вход доведён — позиция открыта
    CLOSED = "CLOSED"                    # выход доведён
    ABANDONED = "ABANDONED"              # владелец отказался от остатка цели
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


OP_ACTIVE = (OpState.APPROVED, OpState.RUNNING, OpState.PARTIAL, OpState.STOPPED, OpState.PAUSED_RISK,
             OpState.PAUSED_UNKNOWN)             # = условие индекса operations_one_active_per_deal
OP_DONE = frozenset({OpState.OPEN, OpState.CLOSED, OpState.ABANDONED, OpState.REJECTED, OpState.EXPIRED})
_OP_SETTLED = frozenset({OpState.OPEN, OpState.CLOSED, OpState.ABANDONED})     # итог: в полёте обязано быть 0
OP_NEXT: dict[str, frozenset] = {
    OpState.PROPOSED: frozenset({OpState.APPROVED, OpState.REJECTED, OpState.EXPIRED}),
    OpState.APPROVED: frozenset({OpState.RUNNING, OpState.STOPPED, OpState.PAUSED_RISK, OpState.PAUSED_UNKNOWN}),
    OpState.RUNNING: frozenset({OpState.OPEN, OpState.PARTIAL, OpState.CLOSED, OpState.STOPPED, OpState.PAUSED_RISK,
                                OpState.PAUSED_UNKNOWN}),
    OpState.PARTIAL: frozenset({OpState.APPROVED, OpState.STOPPED, OpState.ABANDONED, OpState.PAUSED_RISK,
                                OpState.PAUSED_UNKNOWN}),
    OpState.STOPPED: frozenset({OpState.APPROVED, OpState.ABANDONED, OpState.PAUSED_RISK, OpState.PAUSED_UNKNOWN}),
    OpState.PAUSED_RISK: frozenset({OpState.APPROVED, OpState.RUNNING, OpState.PARTIAL, OpState.STOPPED, OpState.ABANDONED,
                                    OpState.PAUSED_UNKNOWN}),
    OpState.PAUSED_UNKNOWN: frozenset({OpState.RUNNING, OpState.PARTIAL, OpState.STOPPED, OpState.PAUSED_RISK}),
}
# вид цели по стороне: вход — бюджет котировки (USDC raw), выход — токены к продаже или снимок всей позиции сделки
OP_TARGETS = {"entry": ("stable_raw_budget",), "exit": ("token_raw_to_sell", "full_position_snapshot")}
OP_MODES = ("dry", "live")


def _raw_int(v: Any, what: str, *, positive: bool = False) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0 or (positive and v == 0):
        raise ValueError(f"{what}: нужно целое сырое {'> 0' if positive else '≥ 0'}, получено {v!r}")
    return v


def _json_hash(obj: Any) -> str | None:
    return None if obj is None else "sha256:" + hashlib.sha256(jdump(obj).encode()).hexdigest()


def create_operation(con, *, deal_id: str, profile_id: str, inst_hash: str, mode: str, side: str, target_kind: str,
                     target_asset: str, target_decimals: int, target_raw: int, fee_cap_raw: int | None = None,
                     bounds: Any = None, op_id: str | None = None, now: float | None = None) -> str:
    """Корневая операция в PROPOSED. Цель (вид, актив, сырое количество) после записи не меняется — продолжение
    частичного исполнения вычитает подтверждённое, а не пересчитывает цель по новой цене (U14/U15)."""
    if mode not in OP_MODES:
        raise ValueError(f"режим операции {mode!r}: {' | '.join(OP_MODES)}")
    if target_kind not in OP_TARGETS.get(side, ()):
        raise ValueError(f"цель {target_kind!r} не для стороны {side!r}")
    _raw_int(target_raw, "target_raw", positive=True)
    if fee_cap_raw is not None:
        _raw_int(fee_cap_raw, "fee_cap_raw")
    if isinstance(target_decimals, bool) or not isinstance(target_decimals, int) or not 0 <= target_decimals <= 255:
        raise ValueError(f"target_decimals {target_decimals!r}")
    for v, what in ((target_asset, "target_asset"), (inst_hash, "inst_hash"), (profile_id, "profile_id")):
        if not isinstance(v, str) or not v:
            raise ValueError(f"{what}: пусто")
    ts = time.time() if now is None else now
    bj = None if bounds is None else jdump(bounds)
    with tx(con):
        if con.execute("SELECT 1 FROM deals WHERE id=?", (deal_id,)).fetchone() is None:
            raise LookupError(f"deals: нет строки {deal_id}")
        oid = op_id
        while oid is None:
            cand = new_id("O")
            if con.execute("SELECT 1 FROM operations WHERE id=?", (cand,)).fetchone() is None:
                oid = cand
        con.execute("INSERT INTO operations(id, deal_id, profile_id, inst_hash, mode, side, target_kind, target_asset, "
                    "target_decimals, target_raw, confirmed_raw, reserved_raw, fee_cap_raw, bounds_json, bounds_hash, "
                    "approval_version, state, reason, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (oid, deal_id, profile_id, inst_hash, mode, side, target_kind, target_asset, target_decimals,
                     str(target_raw), "0", "0", None if fee_cap_raw is None else str(fee_cap_raw), bj,
                     _json_hash(bounds), 0, str(OpState.PROPOSED), None, ts, ts))
    return oid


def get_operation(con, op_id: str) -> dict | None:
    return _row(con, "SELECT * FROM operations WHERE id=?", (op_id,))


def active_operation(con, deal_id: str) -> dict | None:
    qs = ",".join("?" * len(OP_ACTIVE))
    return _row(con, f"SELECT * FROM operations WHERE deal_id=? AND state IN ({qs})",
                (deal_id, *(str(s) for s in OP_ACTIVE)))


def operation_remaining(op: dict) -> int:
    """Остаток корневой цели: цель − подтверждённо исполнено − в полёте (неизвестное — не ноль, пока не разрешено)."""
    return int(op["target_raw"]) - int(op["confirmed_raw"]) - int(op["reserved_raw"])


def set_operation_state(con, op_id: str, new: str, *, expect=None, reason: str | None = None, bounds: Any = None,
                        now: float | None = None) -> bool:
    """Переход операции. Итог (OPEN/CLOSED/ABANDONED) при ненулевом «в полёте» — StoreError: UNKNOWN не становится
    нулём. Одобрение (APPROVED) поднимает approval_version; bounds — новые одобренные пределы (хеш в строке)."""
    new = str(new)
    with tx(con):
        row = get_operation(con, op_id)
        if row is None:
            raise LookupError(f"operations: нет строки {op_id}")
        if new in _OP_SETTLED and row["reserved_raw"] != "0":
            raise StoreError(f"операция {op_id}: в полёте {row['reserved_raw']} — исход не доказан, итог не ставлю")
        fields: dict[str, Any] = {}
        if reason is not None:
            fields["reason"] = reason
        if new == OpState.APPROVED and row["state"] != OpState.APPROVED:
            fields["approval_version"] = int(row["approval_version"]) + 1
            if bounds is not None:
                fields.update(bounds_json=jdump(bounds), bounds_hash=_json_hash(bounds))
        return _transition(con, "operations", "id", op_id, "state", new, OP_NEXT, expect, fields, now)


def operation_reserve(con, op_id: str, raw: int, *, now: float | None = None) -> int:
    """Перед отправкой: raw уходит «в полёт» (запись-до). Больше остатка корневой цели — StoreError, отправлять
    нельзя. Только в RUNNING. Возвращает остаток после резерва."""
    _raw_int(raw, "резерв", positive=True)
    ts = time.time() if now is None else now
    with tx(con):
        r = get_operation(con, op_id)
        if r is None:
            raise LookupError(f"operations: нет строки {op_id}")
        if r["state"] != OpState.RUNNING:
            raise StoreError(f"операция {op_id} в {r['state']}: резерв только в RUNNING")
        left = operation_remaining(r)
        if raw > left:
            raise StoreError(f"операция {op_id}: резерв {raw} больше остатка цели {left}")
        con.execute("UPDATE operations SET reserved_raw=?, updated=? WHERE id=? AND reserved_raw=?",
                    (str(int(r["reserved_raw"]) + raw), ts, op_id, r["reserved_raw"]))
    return left - raw


def operation_settle(con, op_id: str, *, released_raw: int, executed_raw: int, now: float | None = None) -> int:
    """Доказанный исход части «в полёте»: released снимается с резерва, executed (≤ released: фактически ушло
    с учётом возврата) — в подтверждённое. Неизвестный исход сюда не приходит: он держит резерв. Возвращает остаток."""
    _raw_int(released_raw, "снятие резерва", positive=True)
    _raw_int(executed_raw, "исполнено")
    if executed_raw > released_raw:
        raise ValueError(f"исполнено {executed_raw} больше снятого резерва {released_raw}")
    ts = time.time() if now is None else now
    with tx(con):
        r = get_operation(con, op_id)
        if r is None:
            raise LookupError(f"operations: нет строки {op_id}")
        if r["state"] in OP_DONE:
            raise StoreError(f"операция {op_id} уже {r['state']}")
        res = int(r["reserved_raw"])
        if released_raw > res:
            raise StoreError(f"операция {op_id}: снимаю {released_raw}, а в полёте {res}")
        con.execute("UPDATE operations SET reserved_raw=?, confirmed_raw=?, updated=? WHERE id=? AND reserved_raw=? "
                    "AND confirmed_raw=?", (str(res - released_raw), str(int(r["confirmed_raw"]) + executed_raw), ts,
                                            op_id, r["reserved_raw"], r["confirmed_raw"]))
        return operation_remaining(get_operation(con, op_id))


def link_intent(con, operation_id: str, intent_id: str) -> int:
    """Намерение (кнопка входа/выхода/продолжения) — шаг корневой операции, seq по порядку. Повтор — тот же seq;
    то же намерение в другой операции — StoreError."""
    with tx(con):
        r = con.execute("SELECT operation_id, seq FROM operation_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if r is not None:
            if r[0] != operation_id:
                raise StoreError(f"намерение {intent_id} уже в операции {r[0]}")
            return int(r[1])
        if get_operation(con, operation_id) is None:
            raise LookupError(f"operations: нет строки {operation_id}")
        seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM operation_intents WHERE operation_id=?",
                          (operation_id,)).fetchone()[0]
        con.execute("INSERT INTO operation_intents(intent_id, operation_id, seq) VALUES(?,?,?)",
                    (intent_id, operation_id, int(seq)))
    return int(seq)


def operation_of_intent(con, intent_id: str) -> dict | None:
    return _row(con, "SELECT o.* FROM operations o JOIN operation_intents i ON i.operation_id = o.id "
                     "WHERE i.intent_id=?", (intent_id,))


# --- route_candidates: сравнение маршрутов (ТЗ §12) -------------------------------------------------------------
_RC_MONEY = ("amount_in_raw", "expected_out_raw", "min_out_raw", "onchain_min_out_raw", "metric", "conservative",
             "external_cost", "pair_edge", "pair_basis_bps", "pair_margin")
_RC_TEXT = ("candidate_id", "provider", "path", "side", "route_fingerprint", "request_hash", "message_hash",
            "request_id", "response_hash", "metric_version", "decision")
_RC_FLAGS = ("built", "eligible", "selected", "preview_selected")
_RC_KEYS = frozenset({"operation_id", "clip_seq", "round_no", "group", "received_at", "provider_time", "latency_ms",
                      "rank", "fees", "reasons", "notes", *_RC_MONEY, *_RC_TEXT, *_RC_FLAGS})
_RC_COLS = ("ts", "operation_id", "clip_seq", "round_no", "provider_group", *_RC_TEXT, *_RC_MONEY, "received_at",
            "provider_time", "latency_ms", *_RC_FLAGS, "rank_no", "fees_json", "reasons_json", "notes_json")


def _opt_int(v: Any, what: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise StoreError(f"{what}: нужно целое, получено {v!r}")
    return v


def _opt_real(v: Any, what: str) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise StoreError(f"{what}: нужно время числом, получено {v!r}")
    return float(v)


def append_route_rounds(con, rows: Iterable[dict]) -> int:
    """Persist a fresh quote batch after earlier rounds of the same immutable root."""
    rows = [dict(r) for r in rows]
    with tx(con):
        offsets = {}
        for r in rows:
            key = (r["operation_id"], r["clip_seq"])
            if key not in offsets:
                previous = con.execute("SELECT MAX(round_no) FROM route_candidates WHERE operation_id=? "
                                       "AND clip_seq=?", key).fetchone()[0]
                offsets[key] = 0 if previous is None else int(previous) + 1
            r["round_no"] = int(r["round_no"]) + offsets[key]
        return add_route_candidates(con, rows)


def add_route_candidates(con, rows: Iterable[dict], *, now: float | None = None) -> int:
    """spot_router.Decision.records() → route_candidates (только вставка): все кандидаты, причины исключения, времена,
    выбранный. Суммы — строкой (float — TypeError), тексты причин проходят redact_secrets. Повтор той же записи
    безвреден (первая побеждает); второй «выбранный» того же клипа и раунда — StoreError. Возвращает число новых."""
    ts = time.time() if now is None else now
    n = 0
    with tx(con):
        for r in rows:
            bad = sorted(set(r) - _RC_KEYS)
            if bad:
                raise StoreError(f"route_candidates: неизвестные поля {bad}")
            if not r.get("operation_id") or not r.get("provider") or not r.get("path"):
                raise StoreError("route_candidates: нет операции, провайдера или пути")
            v: dict[str, Any] = {"ts": ts, "operation_id": r["operation_id"],
                                 "clip_seq": _opt_int(r.get("clip_seq"), "clip_seq"),
                                 "round_no": _opt_int(r.get("round_no"), "round_no"), "provider_group": r.get("group")}
            if v["clip_seq"] is None or v["round_no"] is None:
                raise StoreError("route_candidates: нет clip_seq или round_no")
            v.update({k: r.get(k) for k in _RC_TEXT})
            v.update({k: amt(r.get(k)) for k in _RC_MONEY})
            v.update(received_at=_opt_real(r.get("received_at"), "received_at"),
                     provider_time=_opt_real(r.get("provider_time"), "provider_time"),
                     latency_ms=_opt_int(r.get("latency_ms"), "latency_ms"), rank_no=_opt_int(r.get("rank"), "rank"))
            v.update({k: 1 if r.get(k) else 0 for k in _RC_FLAGS})
            v["fees_json"] = jdump(list(r.get("fees") or []))
            v["reasons_json"] = jdump([redact_secrets(str(x)) for x in r.get("reasons") or []])
            v["notes_json"] = jdump([redact_secrets(str(x)) for x in r.get("notes") or []])
            conflict = ("ON CONFLICT(operation_id, clip_seq, round_no, candidate_id) WHERE candidate_id IS NOT NULL "
                        "DO NOTHING" if v["candidate_id"] is not None else
                        "ON CONFLICT(operation_id, clip_seq, round_no, provider, path) WHERE candidate_id IS NULL "
                        "DO NOTHING")
            try:
                cur = con.execute(f"INSERT INTO route_candidates({', '.join(_RC_COLS)}) "
                                  f"VALUES({', '.join('?' * len(_RC_COLS))}) {conflict}", tuple(v[c] for c in _RC_COLS))
            except sqlite3.IntegrityError as e:
                raise StoreError(f"route_candidates {r['operation_id']}/{v['clip_seq']}: {e}") from None
            n += cur.rowcount
    return n


def route_candidates(con, operation_id: str) -> list[dict]:
    return _rows(con, "SELECT * FROM route_candidates WHERE operation_id=? ORDER BY clip_seq, round_no, id",
                 (operation_id,))


# --- fee_events: статьи расходов (ТЗ §12, §13) ------------------------------------------------------------------
_ORIGIN_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_FEE_CORE = ("kind", "asset", "decimals", "amount_raw", "payer", "recipient", "included", "estimated", "refundable",
             "superseded")
_FEE_COLS = ("ts", "origin_kind", "origin_ref", "idx", "deal_id", "operation_id", "clip_id", *_FEE_CORE, "source",
             "note", "val_unit", "val_amount", "val_ts", "val_source")


def _fee_row(r: dict) -> dict:
    extra = sorted(set(r) - {*_FEE_CORE, "source", "note", "valuation"})
    if extra:
        raise StoreError(f"fee_events: неизвестные поля {extra}")
    for k in ("kind", "asset"):
        if not isinstance(r.get(k), str) or not r[k]:
            raise StoreError(f"fee_events: {k} пусто")
    dec_ = r.get("decimals")
    if isinstance(dec_, bool) or not isinstance(dec_, int) or not 0 <= dec_ <= 255:
        raise StoreError(f"fee_events: decimals {dec_!r}")
    a = r.get("amount_raw")
    if a is not None:
        if isinstance(a, str) and a.isascii() and a.isdigit():
            a = int(a)
        a = str(_raw_int(a, f"fee_events {r['kind']}: amount_raw"))     # float/знак — ошибка; None — неизвестно
    for k in ("included", "estimated"):
        if not isinstance(r.get(k), bool):
            raise StoreError(f"fee_events: {k} — нужно true/false")
    val = r.get("valuation") or {}
    if set(val) - {"unit", "amount", "ts", "source"}:
        raise StoreError(f"fee_events: неизвестные поля оценки {sorted(set(val) - {'unit', 'amount', 'ts', 'source'})}")
    return {"kind": r["kind"], "asset": r["asset"], "decimals": dec_, "amount_raw": a, "payer": r.get("payer"),
            "recipient": r.get("recipient"), "included": int(r["included"]), "estimated": int(r["estimated"]),
            "refundable": int(bool(r.get("refundable"))), "superseded": int(bool(r.get("superseded"))),
            "source": r.get("source") or None, "note": redact_secrets(r["note"])[:500] if r.get("note") else None,
            "val_unit": val.get("unit"), "val_amount": amt(val.get("amount")),
            "val_ts": _opt_real(val.get("ts"), "valuation.ts"), "val_source": val.get("source")}


def add_fee_events(con, *, origin_kind: str, origin_ref: str, components: Iterable[Any], deal_id: str | None = None,
                   operation_id: str | None = None, clip_id: int | None = None, now: float | None = None) -> int:
    """Статьи расходов одного факта (fees.FeeComponent или его as_record()): запись один раз на источник
    (origin_kind, origin_ref). Повтор того же состава — 0 новых; другой состав у того же источника — StoreError
    (факт не переписывается: откат/форк — отдельным событием). Возвращает число новых строк."""
    if not isinstance(origin_kind, str) or not _ORIGIN_RE.match(origin_kind):
        raise ValueError(f"origin_kind {origin_kind!r}")
    if not isinstance(origin_ref, str) or not origin_ref or len(origin_ref) > 300:
        raise ValueError("origin_ref: пусто или слишком длинно")
    rows = [_fee_row(c.as_record() if hasattr(c, "as_record") else dict(c)) for c in components]
    ts = time.time() if now is None else now
    with tx(con):
        old = _rows(con, "SELECT * FROM fee_events WHERE origin_kind=? AND origin_ref=? ORDER BY idx",
                    (origin_kind, origin_ref))
        if old:
            same = len(old) == len(rows) and all(
                all(o[k] == r[k] for k in _FEE_CORE) for o, r in zip(old, rows))
            if not same:
                raise StoreError(f"fee_events {origin_kind}/{origin_ref}: у источника уже другой состав статей")
            return 0
        for i, r in enumerate(rows):
            v = {"ts": ts, "origin_kind": origin_kind, "origin_ref": origin_ref, "idx": i, "deal_id": deal_id,
                 "operation_id": operation_id, "clip_id": clip_id, **r}
            con.execute(f"INSERT INTO fee_events({', '.join(_FEE_COLS)}) VALUES({', '.join('?' * len(_FEE_COLS))})",
                        tuple(v[c] for c in _FEE_COLS))
    return len(rows)


def fee_events(con, *, deal_id: str | None = None, origin_kind: str | None = None,
               origin_ref: str | None = None) -> list[dict]:
    where, args = [], []
    for col, val in (("deal_id", deal_id), ("origin_kind", origin_kind), ("origin_ref", origin_ref)):
        if val is not None:
            where.append(f"{col}=?")
            args.append(val)
    return _rows(con, "SELECT * FROM fee_events" + (" WHERE " + " AND ".join(where) if where else "") +
                 " ORDER BY id", tuple(args))
