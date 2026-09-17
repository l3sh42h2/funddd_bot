"""Проводка бота владельца с исполнителем (trade_spec §6, отчёт telegram §3, §7): один процесс `funding_bot trader`.

Потоки (почему так):
  опрос     — getUpdates → auth.classify → parse; здесь только быстрое: «стоп» (флаг в БД + Event), CAS кнопки
              (auth.press — одно атомарное UPDATE) и короткие ответы. Двигать деньги диспетчер не может;
  задания   — медленные чтения: план (4 котировки калибровки при 1 запросе/с ключа OKX ≈ 6 с), «позиции», «статус»;
              раз в HOUSEKEEP_S — истечение планов (кнопки снимаются правкой сообщения);
  исполнитель (engine.Engine) — единственный, кто подписывает и отправляет, и только одобренное намерение;
  отправитель (sender.Sender) — очередь: исполнение никогда не ждёт Telegram;
  сторож    — живость опроса, tg_health.json, выход с кодом 3 только при свободном исполнителе.

Соединения с trade.db — по одному на поток (Conns.get() внутри потока): транзакции одного соединения из разных
потоков перемешались бы. Кнопку жмут из потока опроса — тем же путём одобрение шло бы и из двух потоков сразу:
CAS в store.approve_intent даёт ровно один submit.

Рестарт: прерванное (approved/running) → interrupted, сверка (reconcile.startup) и сообщение «♻️ … сам не
продолжаю». Автоматического продолжения нет никогда — только «продолжить <id>» со свежим планом и кнопками.
SIGTERM (TimeoutStopSec=280): новое не начинается (engine.term), текущая пара ног доводится, «⏹ служба
останавливается», отправитель досылает очередь.
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
from . import auth, parse, views
from .poller import Poller, Watchdog, WebhookSet, startup_check
from .sender import Sender, escape, to_plain

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
        from ..interface.presenter import render_execution_notice
        text = render_execution_notice(topic, facts)
        if topic in ('progress', 'sol_progress'):
            self.bot.progress(facts['intent_id'], text)
        else:
            self.bot.say(text)

    def final_report(self, snapshot) -> None:
        from ..ipc.reports import SolFinalView
        if type(snapshot) is SolFinalView:
            from . import sol_views
            self.bot.say(sol_views.final(snapshot))
        else:
            self.bot.say(views.final(snapshot))

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
            log.warning("owner_id не задан — сообщение только в журнал: %s", redact(to_plain(html)[:300]))
            return False
        return self.sender.send(chat, html, reply_markup=reply_markup, on_done=on_done, silent=silent)

    def alarm(self, kind: str, text: str) -> None:
        chat = self.chat()
        if chat is None:
            log.error("тревога %s без владельца: %s", kind, redact(to_plain(text)[:200]))
            return
        self.sender.alarm(chat, text, kind)

    def _head(self, iid: str | None) -> views.PlanHead:
        """Шапка плана из данных намерения и сделки (переживает перезапуск, в отличие от HTML в памяти): подпись
        кнопки «✅ Войти 200 $» и строка закрытия «⏳ Вход AIW3 · 200 $ на ногу — принят 18:58, исполняю».
        Никогда не бросает: в _press шапка строится ДО engine.submit — сбой подписи не должен оставить принятое
        намерение без исполнения (и план без кнопок)."""
        try:
            con = self.conns.get()
            it = store.get_intent(con, iid) if iid else None
            return views.intent_head(it, store.get_deal(con, it["deal_id"]) if it else None)
        except Exception as e:                 # noqa
            log.warning("tg: шапка плана %s не построена: %s", iid, redact(e))
            return views.PlanHead("План", "✅ Да", self.mode == "dry")

    def _closed(self, iid: str | None, action: str, ts: float) -> str:
        h = self._head(iid)
        return views.plan_closed(action, h.title, ts, h.sim)

    # --- диспетчер (поток опроса) ---
    def handle(self, u: dict) -> str | None:
        """Вердикт пишется в tg_updates.verdict. Исключение здесь поймает Poller (команда не повторится)."""
        now = self.clock()
        owner_id, cfg_err = self.owner()
        d = auth.classify(u, owner_id, now=now, limiter=self.limiter)
        v = d.verdict
        if v == auth.STALE:
            self.sender.send(d.chat_id, views.stale(d.date))
        elif v == auth.STRANGER_START:
            self.sender.send(d.chat_id, views.start_reply(d.chat_id, d.user_id))
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
        paused = store.is_paused(con) or self.engine.pause_evt.is_set()
        r = auth.press(con, d.text, paused=paused, now=now)
        self._answer(d.callback_id, r.answer)
        if r.closed and d.chat_id is not None and d.message_id is not None:
            self.sender.edit(d.chat_id, d.message_id, self._closed(r.intent_id, r.closed, now), reply_markup=None)
        # A close plan is intentionally short lived: its quote and reduce-only quantity must not be
        # accepted after the market moved.  Previously an owner who pressed an expired exit button
        # had to type the command again. Rebuild a new proposal through the normal Desk path; never
        # revive or submit the expired intent itself.
        if r.closed == "expired" and r.intent_id and d.chat_id is not None:
            it = store.get_intent(con, r.intent_id)
            if it is not None and it["kind"] == "exit":
                self._job(d.chat_id, "refresh-expired-exit", lambda iid=r.intent_id: self.requote(iid, "план истёк"))
        if r.submit:
            log.info("tg: намерение %s одобрено владельцем — исполнителю", r.intent_id)
            self.engine.submit(r.intent_id)

    def _job(self, chat: int, name: str, fn: Callable[[], Any]) -> None:
        if not self.jobs.put(name, fn):
            self.sender.send(chat, views.error("команд в очереди слишком много — повторите через минуту"))

    def _command(self, d: auth.Decision, cfg_err: OwnerConfigError | None) -> None:
        cmd = parse.parse(d.text or "")
        chat = d.chat_id
        name = cmd.name
        if cfg_err is not None and name not in ("stop", "help", "start", "unknown"):
            self.sender.send(chat, views.owner_config_error(cfg_err))    # «стоп» работает и с битым файлом
            return
        if name == "stop":
            self.stop_cmd(chat)
        elif name in ("help", "start"):
            self.sender.send(chat, views.help_text(sim=self.mode == "dry", sol=self._sol_on()))
        elif name == "unknown":
            self.sender.send(chat, views.unknown(cmd))
        elif name == "resume":
            if cmd.target is None:
                self.resume_cmd(chat)
            else:                              # «продолжить <id>» — явное решение владельца: снять паузу и дать план
                self.resume_cmd(chat, quiet=True)
                t = cmd.target
                self._job(chat, name, lambda: self.propose(chat, lambda: self.desk.propose_resume(t, chat)))
        elif name == "entry":
            self.sender.send(chat, views.planning(cmd.coin, "entry"))
            self._job(chat, name, lambda: self.propose(
                chat, lambda: self.desk.propose_entry(cmd.coin, cmd.spot, cmd.perp, cmd.usd, chat)))
        elif name == "exit":
            self.sender.send(chat, views.planning(cmd.target, "exit"))
            self._job(chat, name, lambda: self.propose(
                chat, lambda: self.desk.propose_exit(cmd.target, cmd.usd, cmd.perp_only, chat)))
        elif name == "profile_entry":             # связка Solana × Hyperliquid: инструмент — из реестра профиля
            self.sender.send(chat, views.planning(cmd.coin, "entry"))
            self._job(chat, name, lambda: self.propose(chat, lambda: self.desk.propose_profile_entry(cmd, chat)))
        elif name == "profile_exit":
            self.sender.send(chat, views.planning(cmd.target, "exit"))
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
        if was:
            text = views.already_paused()
        else:
            text = views.paused(cur[1], cur[2]) if cur else views.paused()
        self.sender.send(chat, text)

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
        self.sender.send(chat, views.resumed(ids) if was else views.not_paused())

    # --- планы (поток заданий) ---
    def propose(self, chat: int, fn: Callable[[], Any]):
        """Предложение с кнопками. Отказ предпроверок и итоги без плана (Refused/Notice) — факты; текст строит
        только render_execution_notice/render_proposal_view (interface.presenter), как и в core/commands.py
        (AC-07: движок этого процесса тоже не хранит готовый HTML). Сбой — «⚠️» с причиной."""
        try:
            p = fn()
        except Refused as e:
            from ..interface.presenter import render_execution_notice
            self.sender.send(chat, render_execution_notice(e.topic, e.facts))
            return None
        except OwnerConfigError as e:
            self.sender.send(chat, views.owner_config_error(str(e)))
            return None
        except Exception as e:                 # noqa
            log.exception("план не построен")
            self.sender.send(chat, views.error(f"план не построен: {type(e).__name__}: {redact(e)}"))
            return None
        if isinstance(p, Notice):
            from ..interface.presenter import render_execution_notice
            self.sender.send(chat, render_execution_notice(p.topic, p.facts))
            return None
        from ..interface.presenter import render_proposal_view
        text = render_proposal_view(p.view_topic, p.view_facts)
        self.sender.send(chat, text, reply_markup=views.plan_keyboard(p.intent_id, p.nonce, self._head(p.intent_id).ok),
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
            self.sender.edit(int(row["chat"]), int(row["msg_id"]), self._closed(iid, action, self.clock()),
                             reply_markup=None)

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
        self.sender.send(chat, views.requote(reason, sim=bool(deal["sim"])))
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
        rows, matched, mism = reconcile.positions(con, self.legs, now=now, busy_deal=busy_deal,
                                                  resolve=not self.engine.busy(), **kw)
        items = [views.PositionView(**r) for r in rows]
        sim = bool(items) and all(i.sim for i in items)
        self.sender.send(chat, views.positions(items, ts=now, matched=matched, mismatch=mism, sim=sim))

    def status_view(self) -> views.StatusView:
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
            for r in con.execute("SELECT json FROM exec_events WHERE kind='final' AND ts>=?", (day0,)):
                try:
                    used -= Decimal(str(json.loads(r[0]).get("cost_usd") or 0))
                except (ValueError, InvalidOperation, AttributeError, TypeError):
                    continue
        return views.StatusView(
            ts=now, mode=mode, running=running, paused=store.is_paused(con) or self.engine.pause_evt.is_set(),
            open_deals=len(store.active_deals(con)), max_open_deals=cfg.get("limits.max_open_deals") if cfg else None,
            tg_last_ok_ago_s=self.poller.last_ok_age() if self.poller else None,
            tg_reconnects_24h=self.watchdog.renewals_within(86400) if self.watchdog else 0,
            sender_fails=self.sender.fails, data_ages=tuple(ages), aster_weight_pct=weight, chain="bsc",
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
        self.sender.send(chat, views.status(self.status_view()))

    # --- старт и остановка ---
    def startup(self) -> reconcile.StartupReport:
        """Сверка на старте и сообщения «♻️ … сам не продолжаю». Ничего не отправляет на площадки."""
        con = self.conns.get()
        rep = reconcile.startup(con, self.legs, now=self.clock())
        for iid in rep.expired:
            self._close_plan(iid, "expired")
        for dr in rep.deals:
            sim = bool(dr.deal["sim"])
            tv = self._texts_of(dr.deal)       # связка Solana × Hyperliquid — свои тексты голой ноги (без «откат»)
            for it in dr.intents:
                clip, clips = reconcile.restart_clip(con, it)
                ok = dr.check.matched is True and dr.new != DealState.HALTED_MISMATCH
                matched = True if ok else (False if dr.check.matched is False else None)
                self.say(tv.restart(views.RestartView(
                    intent_id=it["id"], kind="entry" if it["kind"] == "entry" else "exit", deal_id=dr.deal["id"],
                    clip=clip, clips=clips, matched=matched, details=self._restart_details(dr), sim=sim,
                    coin=dr.deal["coin"], hedged=dr.check.hedged, delta=dr.check.delta, state=str(dr.new),
                    delta_usd=dr.check.delta_usd, step=dr.check.step, m=dr.check.m)))
            # m сделки не известен (ревью 13.09, M3) — не штатное: сказать, что закрывает только «выход» целиком
            if not dr.intents and (dr.new != dr.old or dr.check.matched is not True
                                   or not getattr(dr.check.book, "m_known", True)):
                self.say(self._check_line(dr))
        for p in rep.wallet_problems:
            self.say(views.error(f"кошелёк после перезапуска: {p}"))
        return rep

    @staticmethod
    def _restart_details(dr: reconcile.DealRestart) -> str:
        tail = {DealState.ABORTED: "ничего не куплено — сделка снята, «вход» заново",
                DealState.CLOSED: "выход был доведён — сделка закрыта"}.get(dr.new)
        return "; ".join(x for x in (dr.check.detail, tail) if x)

    @staticmethod
    def _texts_of(deal):
        from ..trade.runtime import is_sol_deal
        if is_sol_deal(deal):
            from . import sol_views
            return sol_views
        return views

    @staticmethod
    def _check_line(dr: reconcile.DealRestart) -> str:
        d, chk = dr.deal, dr.check
        return Bot._texts_of(d).restart_check(d["coin"], d["id"], chk.matched, chk.detail, str(dr.new),
                                              bool(d["sim"]), hedged=chk.hedged, delta=chk.delta, usd=chk.delta_usd,
                                              step=chk.step, m=chk.m)

    def start_poller(self) -> None:
        self._poll_thread = threading.Thread(target=self.poller.run, name="tg-poller", daemon=True)
        self._poll_thread.start()

    def start(self, poll_api) -> None:
        """Потоки опроса и заданий, сторож (его цикл запускает run_trader)."""
        self.poll_api = poll_api
        # у опроса своё соединение (claim/offset в его потоке); диспетчер берёт своё через Conns в том же потоке
        self.poller = Poller(poll_api, store.connect(self.conns.path), self.handle, on_alarm=self.alarm)
        self.start_poller()
        self.jobs.start()
        self.watchdog = Watchdog(poll_api, self.poller, executor_busy=self.engine.busy,
                                 poller_alive=lambda: self._poll_thread is not None and self._poll_thread.is_alive(),
                                 restart_poller=self.start_poller, on_alarm=self.alarm, health_path=tconfig.TG_HEALTH)

    def shutdown(self, code: int, wd_stop: threading.Event | None = None) -> int:
        self.engine.term.set()                 # новое не начинается; начатая пара ног доводится
        if wd_stop is not None:
            wd_stop.set()
        self.jobs.stop()
        if self.poller is not None:
            self.poller.stop()
        if code == 0:
            self.say(views.service_stopping())
        if not self.engine.wait_idle(STOP_WAIT_S):
            log.error("исполнитель не освободился за %d с — выхожу (запись-до: рестарт сверит)", STOP_WAIT_S)
        self.engine.stop()
        left = self.sender.close(10)
        if left:
            log.error("при остановке не доставлено сообщений: %d", left)
        return code


from ..trade.assembly import build_trader_legs

def _notify(api, chat: int | None, html: str) -> None:
    """Одно сообщение мимо очереди — когда процесс не стартует (владелец иначе не узнает почему)."""
    if api is None or not chat:
        return
    try:
        api.send_message(chat, html)
    except Exception as e:                     # noqa
        log.warning("tg: сообщение об отказе старта не доставлено: %s", redact(e))


def _run_trader(environ=None) -> int:
    """`funding_bot trader`. Код выхода: 0 — SIGTERM; 3 — сторож Telegram; 78 — настройка (без перезапуска
    systemd); 1 — сеть на старте (systemd перезапустит)."""
    env = os.environ if environ is None else environ
    install_log_redaction()
    token = (env.get("TG_BOT_TOKEN") or "").strip()
    if not token:
        log.error("нет TG_BOT_TOKEN в окружении (.env) — трейдер не запускаю")
        return EXIT_CONFIG
    try:
        cfg = owner_mod.load()
    except OwnerConfigError as e:
        log.error("owner.toml: %s — трейдер не запускаю", e)
        return EXIT_CONFIG
    from .api import TgApi
    try:
        poll_api, send_api = TgApi(token), TgApi(token)
    except ValueError as e:
        log.error("%s", e)
        return EXIT_CONFIG
    conns, holder = Conns(), CfgHolder()
    try:
        rt, legs, keys_mode, mode, legacy_on = build_trader_legs(cfg, conns, holder, env)
    except KeysError as e:
        log.error("ключи: %s — трейдер не запускаю", redact(e))
        _notify(send_api, cfg.owner_id, views.refused(f"трейдер не запущен: {redact(e)}"))
        return EXIT_CONFIG
    except (owner_mod.OwnerMissing, OwnerConfigError) as e:
        # пустое значение владельца — настройка, а не сеть: без кода 78 systemd перезапускал бы молча каждые 5 с
        log.error("owner.toml: %s — трейдер не запускаю", e)
        _notify(send_api, cfg.owner_id, views.refused(f"трейдер не запущен в режиме {cfg.mode}: {escape(str(e))}"))
        return EXIT_CONFIG
    except Exception as e:                     # noqa — сеть/узел при сборке ног
        log.error("сборка ног: %s", redact(e))
        return EXIT_TRANSIENT
    ref: dict[str, Engine] = {}
    desk = Desk(conns, legs, keys_mode=keys_mode, busy=lambda: ref["e"].busy())
    engine = Engine(conns, legs, desk, keys_mode=keys_mode, holder=holder, busy_path=tconfig.TRADING_BUSY)
    ref["e"] = engine
    from ..serve import load_table
    sender = Sender(send_api).start()
    bot = Bot(conns=conns, desk=desk, engine=engine, sender=sender, legs=legs, poll_api=poll_api, mode=mode, rt=rt,
              table_loader=load_table)
    try:
        info = startup_check(poll_api, bot.alarm)
    except WebhookSet:
        sender.close(10)
        return EXIT_CONFIG
    except Exception as e:                     # noqa — сеть: опрос переподключится сам
        log.warning("tg: getMe/getWebhookInfo: %s", redact(e))
        info = {}
    try:
        bot.startup()
    except Exception:                          # noqa
        log.exception("сверка на старте упала")
        bot.say(views.error("сверка после перезапуска упала — сам ничего не продолжаю; «позиции» перед командами"))
    engine.start()
    bot.start(poll_api)
    bot.say(views.bot_started(mode, info.get("username")))
    stop, code = threading.Event(), [0]

    def on_exit() -> None:
        code[0] = EXIT_WATCHDOG
        stop.set()

    def on_signal(signum, _frame) -> None:
        log.info("сигнал %s — останавливаюсь (пара ног доводится)", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    wd_stop = threading.Event()
    threading.Thread(target=bot.watchdog.run, args=(wd_stop, on_exit), name="tg-watchdog", daemon=True).start()
    while not stop.wait(1.0):
        pass
    return bot.shutdown(code[0], wd_stop)


def run_trader(environ=None) -> int:
    """Transition entrypoint: same execution lock as headless core, held through shutdown."""
    from ..ipc.lock import ExecutionLock
    from ..ipc.paths import execution_lock
    try:
        with ExecutionLock(execution_lock()):
            return _run_trader(environ)
    except BlockingIOError:
        log.error("another trader/core owns execution lock")
        return EXIT_CONFIG
