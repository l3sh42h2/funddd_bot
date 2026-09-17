"""Compatibility command controller for M1/M2. No Telegram network client.
Legacy presentation is retained until M3/M4; transport and persistence belong to separate processes.
"""
from __future__ import annotations
import json, logging, os, queue, signal, threading, time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from ..trade import marks, owner as owner_mod, reconcile, store, tconfig
from ..trade.engine import CfgHolder, Conns, Desk, Engine, Hooks, Notice, Refused, build_runtime, dget
from ..trade.keys import KeysError, effective_mode, install_log_redaction, redact
from ..trade.owner import OwnerConfigError
from ..trade.store import DealState
from ..ipc.reports import PositionView, StatusView, RestartView
from . import authority as auth
from ..ipc.source import legacy_telegram_source
from .approvals import ApprovalAction, decide
from .. import operator_commands as parse

log = logging.getLogger(__name__)

EXIT_CONFIG = 78            # EX_CONFIG: нет токена, битый owner.toml, ключ не сошёлся — перезапуск по кругу не лечит
EXIT_WATCHDOG = 3           # сторож: Telegram не оживает — systemd перезапустит (только при свободном исполнителе)
EXIT_TRANSIENT = 1          # сеть на старте (Aster/RPC не ответили) — systemd перезапустит через RestartSec
HOUSEKEEP_S = 2.0           # истёкший план теряет кнопки не позже чем через 2 с после срока
JOBS_MAX = 20               # больше — владелец шлёт команды быстрее, чем они считаются: лишнее отклоняется
STOP_WAIT_S = 250           # SIGTERM: довести пару ног; своп Solana + разбор HL ≈243 с (расчёт — юнит трейдера)
REQUOTE_IDLE_S = 15.0       # перекотировка: подождать, пока исполнитель закроет отказанное намерение


# --- поток заданий ---------------------------------------------------------------------------------------
class Jobs:
    """Медленные чтения по очереди + tick() раз в HOUSEKEEP_S. sync=True — тесты: сразу в вызывающем потоке."""

    def __init__(self, *, tick: Callable[[], Any] | None = None, sync: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.q: "queue.Queue[tuple[str, Callable[[], Any]]]" = queue.Queue(maxsize=JOBS_MAX)
        self.tick = tick
        self.sync = sync
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_tick = 0.0

    def put(self, name: str, fn: Callable[[], Any]) -> bool:
        if self.sync:
            self._run(name, fn)
            return True
        try:
            self.q.put_nowait((name, fn))
            return True
        except queue.Full:
            log.error("задания: очередь полна (%d) — %s отклонено", self.q.maxsize, name)
            return False

    @staticmethod
    def _run(name: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:                      # noqa — поток заданий не умирает от одной команды
            log.exception("задание %s упало", name)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                name, fn = self.q.get(timeout=HOUSEKEEP_S / 2)
                self._run(name, fn)
            except queue.Empty:
                pass
            now = self._clock()
            if self.tick is not None and now - self._last_tick >= HOUSEKEEP_S:
                self._last_tick = now
                self._run("tick", self.tick)

    def start(self) -> "Jobs":
        if not self.sync and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self.run, name="jobs", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


class BotHooks(Hooks):
    """Куда исполнитель пишет владельцу: только очередь отправителя (исполнение не ждёт Telegram)."""

    def __init__(self, bot: "Bot"):
        self.bot = bot

    def progress(self, iid: str, html: str) -> None:
        self.bot.progress(iid, html)

    def report(self, html: str) -> None:
        self.bot.say(html)

    def notice(self, topic: str, facts: dict) -> None:
        self.bot.execution_notice(topic, facts)

    def final_report(self, snapshot) -> None:
        chat = self.bot.chat()
        if chat is None:
            log.warning('owner_id не задан — итог не доставлен')
            return
        self.bot.sender.final_report(chat, snapshot)

    def requote(self, iid: str, reason: str) -> None:
        self.bot.jobs.put("requote", lambda: self.bot.requote(iid, reason))


# --- бот ---------------------------------------------------------------------------------------------------
class Bot:
    """Диспетчер и ответы. legs(sim) → engine.Legs | None (как у Desk/Engine); rt — engine.Runtime (для проверок
    «статус»; в тестах None)."""

    def __init__(self, *, conns: Conns, desk: Desk, engine: Engine, sender: Sender,
                 legs: Callable[[bool], Any], poll_api=None, owner_loader: Callable[[], Any] = owner_mod.load,
                 mode: str = "dry", rt=None, jobs: Jobs | None = None, limiter: auth.StartLimiter | None = None,
                 clock: Callable[[], float] = time.time, table_loader: Callable[[], dict] | None = None):
        self.conns, self.desk, self.engine, self.sender, self.legs = conns, desk, engine, sender, legs
        self.poll_api = poll_api
        self.owner_loader = owner_loader
        self.mode = mode
        self.rt = rt
        self.jobs = jobs or Jobs()
        if self.jobs.tick is None:
            self.jobs.tick = self.housekeep
        self.limiter = limiter or auth.StartLimiter()
        self.clock = clock
        self.table_loader = table_loader
        self._owner_id: int | None = None
        self._lock = threading.Lock()
        self._progress: dict[str, int | None] = {}        # намерение → message_id прогресса (None — отправка в пути)
        self._progress_pending: dict[str, str] = {}
        self._marks_at = 0.0                              # последний завершённый проход оценки сделок (marks)
        self.poller: Poller | None = None
        self.watchdog: Watchdog | None = None
        self._poll_thread: threading.Thread | None = None
        engine.hooks = BotHooks(self)

    # --- владелец и отправка ---
    def owner(self) -> tuple[int | None, OwnerConfigError | None]:
        """owner_id из свежего owner.toml. Файл битый — прежний владелец (он должен увидеть причину), команды с
        деньгами при этом отклоняются с текстом ошибки."""
        try:
            cfg = self.owner_loader()
        except OwnerConfigError as e:
            return self._owner_id, e
        self._owner_id = cfg.owner_id
        return cfg.owner_id, None

    def chat(self) -> int | None:
        return self.owner()[0]

    def say(self, html: str, *, reply_markup: dict | None = None, on_done=None, silent: bool = False) -> bool:
        chat = self.chat()
        if chat is None:
            log.warning("owner_id не задан — сообщение не доставлено")
            return False
        return self.sender.send(chat, html, reply_markup=reply_markup, on_done=on_done, silent=silent)

    def alarm(self, kind: str, text: str) -> None:
        chat = self.chat()
        if chat is None:
            log.error("тревога без владельца — сообщение не доставлено")
            return
        self.sender.alarm(chat, text, kind)

    def _plan_summary(self, iid):
        from ..ipc.notifications import plan_summary
        try:
            con = self.conns.get()
            it = store.get_intent(con, iid) if iid else None
            return plan_summary(it, store.get_deal(con, it['deal_id']) if it else None)
        except Exception as e:
            log.warning('plan summary: %s', redact(e))
            return {'fallback': True, 'sim': self.mode == 'dry'}

    # --- диспетчер (поток опроса) ---
    def handle(self, u: dict) -> str | None:
        """Вердикт пишется в tg_updates.verdict. Исключение здесь поймает Poller (команда не повторится)."""
        now = self.clock()
        owner_id, cfg_err = self.owner()
        d = auth.classify(legacy_telegram_source(u), owner_id, now=now, limiter=self.limiter)
        v = d.verdict
        if v == auth.STALE:
            self.sender.notice(d.chat_id, 'command_stale', date=d.date)
        elif v == auth.STRANGER_START:
            self.sender.notice(d.chat_id, 'operator_identity', chat_id=d.chat_id, user_id=d.user_id)
        elif v == auth.STRANGER_CB:
            self._answer(d.callback_id, None)
            log.info("tg: чужое нажатие от %s — игнор", d.user_id)
        elif v == auth.OWNER_CB:
            self._press(d, now)
        elif v == auth.OWNER_MSG:
            self._command(d, cfg_err)
        else:
            log.info("tg: update %s — %s", d.update_id, v)
        return v

    def _answer(self, callback_id: str | None, text: str | None) -> None:
        """answerCallbackQuery — сразу и мимо очереди отправителя: «часики» у владельца не должны ждать отчётов."""
        if not callback_id or self.poll_api is None:
            return
        try:
            self.poll_api.answer_callback_query(callback_id, text)
        except Exception as e:                 # noqa
            log.warning("tg: answerCallbackQuery: %s", redact(e))

    def _press(self, d: auth.Decision, now: float) -> None:
        con = self.conns.get()
        paused = store.execution_paused(con) or self.engine.pause_evt.is_set() or self.engine.drain_evt.is_set()
        cb = parse.parse_callback(d.text)
        action = ApprovalAction(cb.action, cb.intent_id, cb.nonce) if cb else None
        r = decide(con, action, paused=paused, now=now)
        try:
            if d.callback_id and self.poll_api is not None:
                try:
                    self.poll_api.approval_reply(d.callback_id, r.reason)
                except Exception as e:
                    log.warning('approval notification: %s', redact(e))
            if r.closed and d.chat_id is not None and d.message_id is not None:
                self.sender.plan_closed(d.chat_id, d.message_id, self._plan_summary(r.intent_id), r.closed, now)
        except Exception as e:
            log.warning("approval delivery deferred: %s", redact(e))
        finally:
            # An expired exit is never revived: quote and reduce-only size are stale.
            # Build a fresh proposal through the regular path so one late tap cannot trap
            # the owner in a dead plan or submit an old order.
            if r.closed == "expired" and r.intent_id and d.chat_id is not None:
                it = store.get_intent(con, r.intent_id)
                if it is not None and it["kind"] == "exit":
                    self._job(d.chat_id, "refresh-expired-exit",
                              lambda iid=r.intent_id: self.requote(iid, "план истёк"))
            if r.submit:
                log.info("tg: намерение %s одобрено владельцем — исполнителю", r.intent_id)
                self.engine.submit(r.intent_id)

    def _job(self, chat: int, name: str, fn: Callable[[], Any]) -> None:
        if not self.jobs.put(name, fn):
            self.sender.notice(chat, 'error', reason='команд в очереди слишком много — повторите через минуту')

    def _command(self, d: auth.Decision, cfg_err: OwnerConfigError | None) -> None:
        cmd = parse.parse(d.text or "")
        chat = d.chat_id
        name = cmd.name
        if cfg_err is not None and name not in ("stop", "help", "start", "unknown"):
            self.sender.notice(chat, 'configuration_error', reason=redact(cfg_err))    # «стоп» работает и с битым файлом
            return
        if name == "stop":
            self.stop_cmd(chat)
        elif name in ("help", "start"):
            self.sender.notice(chat, 'help_requested', sim=self.mode == 'dry', sol=self._sol_on())
        elif name == "unknown":
            self.sender.notice(chat, 'command_unknown', reason=cmd.reason)
        elif name == "resume":
            if cmd.target is None:
                self.resume_cmd(chat)
            else:                              # «продолжить <id>» — явное решение владельца: снять паузу и дать план
                self.resume_cmd(chat, quiet=True)
                t = cmd.target
                self._job(chat, name, lambda: self.propose(chat, lambda: self.desk.propose_resume(t, chat)))
        elif name == "entry":
            self.sender.notice(chat, 'planning_started', coin=cmd.coin, side='entry')
            self._job(chat, name, lambda: self.propose(
                chat, lambda: self.desk.propose_entry(cmd.coin, cmd.spot, cmd.perp, cmd.usd, chat, stop_price=getattr(cmd, "stop_price", None))))
        elif name == "exit":
            self.sender.notice(chat, 'planning_started', coin=cmd.target, side='exit')
            self._job(chat, name, lambda: self.propose(
                chat, lambda: self.desk.propose_exit(cmd.target, cmd.usd, cmd.perp_only, chat)))
        elif name == "resize":
            self.sender.notice(chat, 'planning_started', coin=cmd.target, side='entry')
            self._job(chat, name, lambda: self.propose(
                chat, lambda: self.desk.propose_resize(cmd.target, cmd.usd, chat)))
        elif name == "profile_entry":             # связка Solana × Hyperliquid: инструмент — из реестра профиля
            self.sender.notice(chat, 'planning_started', coin=cmd.coin, side='entry')
            self._job(chat, name, lambda: self.propose(chat, lambda: self.desk.propose_profile_entry(cmd, chat)))
        elif name == "profile_exit":
            self.sender.notice(chat, 'planning_started', coin=cmd.target, side='exit')
            self._job(chat, name, lambda: self.propose(chat, lambda: self.desk.propose_profile_exit(cmd, chat)))
        elif name in ("rehedge", "undo"):
            self._job(chat, name, lambda: self.propose(chat, lambda: self.desk.propose_fix(name, cmd.target, chat)))
        elif name == "positions":
            self._job(chat, name, lambda: self.positions_cmd(chat, getattr(cmd, "profile", None)))
        elif name == "status":
            self._job(chat, name, lambda: self.status_cmd(chat))

    # --- быстрые команды ---
    def stop_cmd(self, chat: int) -> None:
        """«стоп»: флаг в БД (переживает рестарт) + Event. Начатая пара ног доводится (хедж — hedge=True)."""
        con = self.conns.get()
        was = store.is_paused(con)
        store.set_paused(con, True)
        self.engine.pause_evt.set()
        store.event(con, "stop", was=was)
        cur = self.engine.current
        self.sender.notice(chat, 'pause_changed', already=was, clip=cur[1] if cur else None,
                           clips=cur[2] if cur else None)

    def resume_cmd(self, chat: int, quiet: bool = False) -> None:
        con = self.conns.get()
        was = store.is_paused(con) or self.engine.pause_evt.is_set()
        store.set_paused(con, False)
        self.engine.pause_evt.clear()
        if was:
            store.event(con, "resume")
        if quiet:
            return
        ids = [r[0] for r in con.execute(
            "SELECT i.id FROM intents i JOIN deals d ON d.id = i.deal_id WHERE i.status IN ('interrupted','partial') "
            "AND d.state = 'PAUSED' ORDER BY i.created DESC LIMIT 5")]
        self.sender.notice(chat, 'resume_changed', was=was, interrupted=ids)

    # --- планы (поток заданий) ---
    def propose(self, chat: int, fn: Callable[[], Any]):
        """Предложение с кнопками. Отказ предпроверок и итоги без плана (Refused/Notice) — факты, не готовый
        HTML: interface рендерит их через execution_notice (AC-07); сбой — «⚠️» с причиной."""
        try:
            p = fn()
        except Refused as e:
            self.sender.execution_notice(chat, e.topic, e.facts)
            return None
        except OwnerConfigError as e:
            self.sender.notice(chat, 'configuration_error', reason=redact(e))
            return None
        except Exception as e:                 # noqa
            log.exception("план не построен")
            self.sender.notice(chat, 'error', reason=f'план не построен: {type(e).__name__}: {redact(e)}')
            return None
        if isinstance(p, Notice):
            self.sender.execution_notice(chat, p.topic, p.facts)
            return None
        self.sender.plan_proposed(chat, p.intent_id, p.nonce, self._plan_summary(p.intent_id),
                                  p.view_topic, p.view_facts,
                                  on_done=lambda m, iid=p.intent_id: self._plan_sent(iid, chat, m))
        for old in getattr(p, "superseded", ()) or ():     # новый план сделки — у прежних снимаются кнопки
            self._close_plan(old, "expired")
        return p

    def _sol_on(self) -> bool:
        """Связка Solana × Hyperliquid включена в owner.toml (строка команд в «помощь», проверки в «статус»)."""
        try:
            return self.owner_loader().profile_enabled(owner_mod.SOL_HL)
        except Exception:                      # noqa — битый файл: прежний текст
            return False

    def _plan_sent(self, iid: str, chat: int, m: dict | None) -> None:
        """В потоке отправителя (своё соединение): message_id плана — чтобы по истечении снять кнопки."""
        if not m or not m.get("message_id"):
            return
        try:
            store.set_intent_message(self.conns.get(), iid, chat, int(m["message_id"]))
        except Exception as e:                 # noqa
            log.warning("message_id плана %s не записан: %s", iid, redact(e))

    def _close_plan(self, iid: str, action: str) -> None:
        row = store.get_intent(self.conns.get(), iid)
        if row and row["chat"] and row["msg_id"]:
            self.sender.plan_closed(int(row["chat"]), int(row["msg_id"]), self._plan_summary(iid), action, self.clock())

    def housekeep(self) -> None:
        for iid in store.expire_intents(self.conns.get(), now=self.clock()):
            self._close_plan(iid, "expired")
        self.marks_tick()

    def marks_tick(self) -> None:
        """Раз в MARK_S — «PnL сейчас / при выходе» каждой активной сделки в deal_marks (для кабинета; trade/marks.py,
        только чтение сети). Во время исполнения — пропуск: проход начнётся, когда исполнитель освободится.
        Сбой прохода — в журнал, следующий через MARK_S (поток заданий не умирает)."""
        now = self.clock()
        if now - self._marks_at < tconfig.MARK_S or self.engine.busy():
            return
        try:
            _done, complete = marks.run_pass(self.conns.get(), self.legs, now=now, busy=self.engine.busy)
        except Exception as e:                 # noqa
            log.error("оценка сделок: %s: %s", type(e).__name__, redact(e))
            complete = True
        if complete:
            self._marks_at = now

    def requote(self, iid: str, reason: str) -> None:
        """§6 п.7: условия у кнопки хуже плана — ничего не отправлено; свежий план с новыми кнопками."""
        self.engine.wait_idle(REQUOTE_IDLE_S)
        con = self.conns.get()
        it = store.get_intent(con, iid)
        if it is None:
            return
        deal = store.get_deal(con, it["deal_id"])
        spec = json.loads(it["spec_json"])
        chat = it["chat"] or self.chat()
        if chat is None or deal is None:
            return
        self._close_plan(iid, "requote")
        self.sender.notice(chat, 'plan_requoted', reason=reason, sim=bool(deal['sim']))
        if spec.get("resume"):
            fn = lambda: self.desk.propose_resume(deal["id"], chat)
        elif it["kind"] == "entry" and spec.get("profile") == owner_mod.SOL_HL:
            try:                               # связка Solana × Hyperliquid: тот же вход заново — из реестра профиля
                usd = Decimal(str(spec["usd"]))
            except (KeyError, InvalidOperation):
                return
            dex = str(spec.get("perp") or "").split("·")[-1] or None
            cmd = parse.ProfileEntry(str(spec["coin"]), "auto", "solana", "hyperliquid", dex, usd)
            fn = lambda: self.desk.propose_profile_entry(cmd, chat)
        elif it["kind"] == "entry":
            try:
                usd = Decimal(str(spec["usd"]))
            except (KeyError, InvalidOperation):
                return
            fn = lambda: self.desk.propose_entry(spec["coin"], spec["spot"], spec["perp"], usd, chat,
                                                 deal_id=deal["id"] if deal["state"] == DealState.DRAFT else None)
        elif it["kind"] == "exit":
            fn = lambda: self.desk.propose_exit(deal["id"], None if spec.get("all") else dget(spec.get("usd")),
                                                bool(spec.get("perp_only")), chat)
        else:
            return
        self.propose(chat, fn)

    # --- прогресс исполнения ---
    def execution_notice(self, topic: str, facts: dict) -> None:
        chat = self.chat()
        if chat is not None:
            self.sender.execution_notice(chat, topic, facts)

    def progress(self, iid: str, html: str) -> None:
        """Одно сообщение прогресса на намерение: первое — новое, дальше — правка не чаще TG_EDIT_MIN_S."""
        chat = self.chat()
        if chat is None:
            return
        with self._lock:
            if iid in self._progress:
                mid = self._progress[iid]
                if mid is None:                # первое ещё в пути — правка уйдёт, как только придёт его message_id
                    self._progress_pending[iid] = html
                    return
            else:
                self._progress[iid] = None
                mid = None
        if mid is None:
            self.sender.send(chat, html, silent=True, on_done=lambda m: self._progress_sent(iid, chat, m))
        else:
            self.sender.edit(chat, mid, html, throttle=True)

    def _progress_sent(self, iid: str, chat: int, m: dict | None) -> None:
        with self._lock:
            mid = int(m["message_id"]) if (m and m.get("message_id")) else None
            if mid is None:
                self._progress.pop(iid, None)  # не доставлено — следующее обновление пойдёт новым сообщением
                pend = None
            else:
                self._progress[iid] = mid
                pend = self._progress_pending.pop(iid, None)
        if mid is not None and pend is not None:
            self.sender.edit(chat, mid, pend, throttle=True)

    # --- «позиции» и «статус» (поток заданий) ---
    def _running(self, con) -> tuple[str | None, str | None]:
        r = con.execute("SELECT id, deal_id FROM intents WHERE status IN ('approved','running') LIMIT 1").fetchone()
        return (r[0], r[1]) if r else (None, None)

    def positions_cmd(self, chat: int, profile: str | None = None) -> None:
        con = self.conns.get()
        now = self.clock()
        _iid, busy_deal = self._running(con)
        kw = {} if profile is None else {"profile": profile}      # «позиции sol» — сделки одной связки
        if getattr(getattr(self, 'engine', None), 'generic_context_factory', None) is not None:
            kw.update(generic_context_factory=self.engine.generic_context_factory,
                      generic_registry=self.engine.generic_registry or getattr(self.legs, 'adapters', None))
        rows, matched, mism = reconcile.positions(con, self.legs, now=now, busy_deal=busy_deal,
                                                  resolve=not self.engine.busy(), **kw)
        items = [PositionView(**r) for r in rows]
        sim = bool(items) and all(i.sim for i in items)
        self.sender.positions_report(chat, items, at=now, matched=matched, mismatch=mism, sim=sim)

    def status_view(self) -> StatusView:
        now = self.clock()
        con = self.conns.get()
        try:
            cfg = self.owner_loader()
        except OwnerConfigError:
            cfg = None
        mode = self.mode if cfg is None else effective_mode(cfg.mode, self.mode)
        running, _ = self._running(con)
        ages: list[tuple[str, float | None]] = []
        if self.table_loader is not None:
            try:
                ts = (self.table_loader() or {}).get("ts")
                ages.append(("таблица", (now - float(ts)) if ts else None))
            except Exception:                  # noqa
                ages.append(("таблица", None))
        legs = self.legs(False) or self.legs(True)
        w_stable = w_native = margin = weight = None
        checks: list = []
        if legs is not None:
            spot = legs.spot
            stable = getattr(spot, "stable", None)
            sdec = int(getattr(spot, "stable_dec", None) or 18)
            if stable:
                try:
                    b = spot.balances(stable)
                    w_stable = None if b.get("stable") is None else Decimal(b["stable"]) / Decimal(10) ** sdec
                    w_native = None if b.get("native") is None else Decimal(b["native"]) / Decimal(10) ** 18
                except Exception as e:         # noqa
                    log.warning("статус: балансы: %s", redact(e))
            try:
                margin = legs.perp.available_margin()
            except Exception:                  # noqa — dry: подписанное чтение запрещено воротами
                margin = None
            http = getattr(legs.perp, "http", None)
            if http is not None and hasattr(http, "budget_used"):
                try:
                    weight = Decimal(str(round(http.budget_used() * 100, 1)))
                except Exception:              # noqa
                    weight = None
        if self.rt is not None and cfg is not None:
            deals = store.active_deals(con)
            try:
                checks = reconcile.health_checks(self.rt, cfg, deals[0]["symbol"] if deals else None)
            except Exception as e:             # noqa
                checks = [("проверки", None, redact(e)[:160])]
        if cfg is not None:
            checks = list(checks) + self._sol_checks(cfg)
        used = None
        if cfg is not None and isinstance(cfg.get("limits.daily_loss_stop_usd"), Decimal):
            day0 = int(now // 86400) * 86400
            used = Decimal(0)
            from ..trade.accounting import is_bound, event_cost
            for r in con.execute("SELECT json,deal_id,intent_id FROM exec_events WHERE kind='final' AND ts>=?", (day0,)):
                if is_bound(con, r[1]):
                    try:
                        cost = event_cost(con, r[1], r[2], json.loads(r[0]))
                    except Exception:
                        cost = None
                    if cost is None:
                        used = None
                        break
                    used -= cost
                    continue
                try:
                    used -= Decimal(str(json.loads(r[0]).get("cost_usd") or 0))
                except (ValueError, InvalidOperation, AttributeError, TypeError):
                    continue
        return StatusView(
            ts=now, mode=mode, running=running, paused=store.is_paused(con) or self.engine.pause_evt.is_set(),
            open_deals=len(store.active_deals(con)), max_open_deals=cfg.get("limits.max_open_deals") if cfg else None,
            tg_last_ok_ago_s=self.poller.last_ok_age() if self.poller else None,
            tg_reconnects_24h=self.watchdog.renewals_within(86400) if self.watchdog else 0,
            sender_fails=0, data_ages=tuple(ages), aster_weight_pct=weight, chain="bsc",
            wallet_stable=w_stable, wallet_native=w_native, margin_avail=margin,
            cap_usd=cfg.get("limits.deal_max_usd_per_leg") if cfg else None,
            daily_stop=cfg.get("limits.daily_loss_stop_usd") if cfg else None, daily_used_usd=used,
            checks=tuple(checks), missing_owner_keys=tuple(cfg.live_missing("aster", "bsc")) if cfg else ())

    def _sol_checks(self, cfg) -> list[tuple[str, bool | None, str]]:
        """«статус» связки Solana × Hyperliquid — только если она включена (иначе текст прежний): ноги не собрались
        (причина сборки), владелец просит live, а live не готов (что именно мешает)."""
        try:
            if not cfg.profile_enabled(owner_mod.SOL_HL):
                return []
        except Exception:                      # noqa
            return []
        out: list[tuple[str, bool | None, str]] = []
        down = (getattr(self.legs, "last_error", None) or {}).get(owner_mod.SOL_HL)
        if down:
            out.append(("связка Solana × Hyperliquid не собрана", False, str(down)[:160]))
        if cfg.values.get(f"profiles.{owner_mod.SOL_HL}.mode") == "live":
            bl = cfg.profile_live_blockers(owner_mod.SOL_HL)
            out.append(("Solana × Hyperliquid: live", not bl, "; ".join(bl)[:240]))
        return out

    def status_cmd(self, chat: int) -> None:
        self.sender.status_report(chat, self.status_view())

    # --- старт и остановка ---
    def startup(self) -> reconcile.StartupReport:
        """Сверка на старте и сообщения «♻️ … сам не продолжаю». Ничего не отправляет на площадки."""
        con = self.conns.get()
        from ..trade.adapters.execution_scope import prepare_active_accounts
        from ..trade.engine import backfill_instruments
        now = self.clock()
        backfilled = backfill_instruments(con, now=now)
        prepare_active_accounts(con, self.legs)
        generic = {}
        if getattr(getattr(self, 'engine', None), 'generic_context_factory', None) is not None:
            generic = dict(generic_context_factory=self.engine.generic_context_factory,
                           generic_registry=self.engine.generic_registry or getattr(self.legs, 'adapters', None))
        rep = reconcile.startup(con, self.legs, now=now, **generic)
        rep.inst_backfill[:0] = backfilled
        for iid in rep.expired:
            self._close_plan(iid, "expired")
        for dr in rep.deals:
            sim = bool(dr.deal["sim"])
            for it in dr.intents:
                clip, clips = reconcile.restart_clip(con, it)
                ok = dr.check.matched is True and dr.new != DealState.HALTED_MISMATCH
                matched = True if ok else (False if dr.check.matched is False else None)
                self._restart_report(dr.deal, RestartView(
                    intent_id=it["id"], kind="entry" if it["kind"] == "entry" else "exit", deal_id=dr.deal["id"],
                    clip=clip, clips=clips, matched=matched, details=self._restart_details(dr), sim=sim,
                    coin=dr.deal["coin"], hedged=dr.check.hedged, delta=dr.check.delta, state=str(dr.new),
                    delta_usd=dr.check.delta_usd, step=dr.check.step, m=dr.check.m))
            # m сделки не известен (ревью 13.09, M3) — не штатное: сказать, что закрывает только «выход» целиком
            if not dr.intents and (dr.new != dr.old or dr.check.matched is not True
                                   or not getattr(dr.check.book, "m_known", True)):
                chk = dr.check
                self._restart_report(dr.deal, RestartView(
                    intent_id='', kind='check', deal_id=dr.deal['id'], coin=dr.deal['coin'],
                    matched=chk.matched, details=chk.detail, state=str(dr.new), sim=sim,
                    hedged=chk.hedged, delta=chk.delta, delta_usd=chk.delta_usd, step=chk.step, m=chk.m), check_only=True)
        for p in rep.wallet_problems:
            chat = self.chat()
            if chat is not None:
                self.sender.notice(chat, 'error', reason=f'кошелёк после перезапуска: {redact(p)}')
        return rep

    @staticmethod
    def _restart_details(dr: reconcile.DealRestart) -> str:
        tail = {DealState.ABORTED: "ничего не куплено — сделка снята, «вход» заново",
                DealState.CLOSED: "выход был доведён — сделка закрыта"}.get(dr.new)
        return "; ".join(x for x in (dr.check.detail, tail) if x)

    def _restart_report(self, deal, snapshot, *, check_only=False):
        from ..trade.runtime import is_sol_deal
        chat = self.chat()
        if chat is not None:
            self.sender.restart_report(chat, snapshot, solana=is_sol_deal(deal), check_only=check_only)


from ..trade.assembly import build_trader_legs
