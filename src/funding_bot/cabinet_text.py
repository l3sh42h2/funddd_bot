"""Pure cabinet labels shared by core DTO builder and HTML interface."""
import json
from .trade.report import fmt_num
# --- тексты статусов и событий ---------------------------------------------------------------------------
STATE_VIEW = {"OPEN": ("🟢", "открыта", "st-open"), "ENTERING": ("🔄", "входит", "st-run"),
              "EXITING": ("🔄", "выходит", "st-run"), "PAUSED": ("⏸️", "пауза", "st-pause"),   # FE0F — эмодзи, а не «II»
              "HALTED_MISMATCH": ("🛑", "остановлена: расхождение", "st-halt"), "CLOSED": ("✅", "закрыта", "st-done"),
              "ABORTED": ("⚪", "отменена", "st-done"), "DRAFT": ("📝", "черновик", "st-done")}

# причины паузы движка (Pause(reason, …) в trade/engine.py, reconcile) → человеческий текст без технических деталей
REASON_TEXT = {
    "error": "сбой исполнителя", "stop": "стоп владельца", "terminate": "служба останавливалась",
    "owner": "owner.toml не прочитан", "owner_missing": "в owner.toml не хватает параметров",
    "mode": "режим запрещает отправки", "native": "мало BNB на газ", "limit": "сработал лимит владельца",
    "funding": "фандинг не прочитан", "funding_sign": "фандинг сменил знак", "exec_time": "исполнение дольше допустимого",
    "plan_ab": "план устарел — нужен свежий", "book_unknown": "журнал сделки неполон — нужна сверка",
    "hedge_deficit": "ноги не сбалансированы", "position_unknown": "позиция на бирже не прочитана",
    "position_mismatch": "позиция не совпадает с журналом", "setup": "настройка перпа не удалась",
    "approve_unknown": "исход разрешения токена неизвестен", "approve_refused": "разрешение токена не отправлено",
    "approve_failed": "разрешение токена не прошло", "dex_unknown": "исход свопа выясняется",
    "dex_refused": "своп не отправлен", "dex_revert": "своп откатился дважды",
    "perp_refused": "заявка на перпе не отправлена", "perp_unknown": "исход заявки на перпе выясняется",
    "perp_rejected": "заявка на перпе нулевая после округления", "reduce_only_reject": "биржа отклонила откуп шорта",
    "busy": "токен или символ заняты другой сделкой", "state": "состояние сделки изменилось",
    "replan": "остаток не планируется", "changed": "дельта ног сменила знак", "wallet_unknown": "баланс кошелька не прочитан",
    "restart": "перезапуск во время исполнения", "entry": "вход не завершён", "exit": "выход не завершён",
    "rehedge": "дохедж не завершён", "undo": "откат не завершён", "inst_unverified": "инструмент сделки не подтверждён",
}


def reason_text(reason) -> str | None:
    """Код причины → текст; русская причина (их пишет сам движок: «сверено владельцем», «перп закрыт, спот остался…») —
    как есть; незнакомый латинский код и детали после «restart:» — не показываются."""
    if not reason:
        return None
    r = str(reason).strip()
    if r in REASON_TEXT:
        return REASON_TEXT[r]
    if r.startswith("restart"):
        return "после перезапуска ноги не сошлись — нужна сверка"
    if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in r):
        return r[:200]
    return None


_START = {"entry": "вход начат", "exit": "выход начат", "rehedge": "дохедж начат", "undo": "откат начат"}
_FIXED = {"rehedge": "дохедж выполнен", "undo": "откат выполнен"}
_PLAN = {"entry": "вход", "exit": "выход", "rehedge": "дохедж", "undo": "откат"}


def event_text(kind: str, data: dict, ikind: str | None = None) -> str:
    """Последнее событие exec_events → короткий текст для владельца (без хэшей, чисел заявок и текстов ошибок)."""
    k = data.get("intent_kind") or ikind
    st = data.get("state")
    if kind == "proposed":
        return "предложен план: " + _PLAN.get(k, "исполнение")
    if kind == "start":
        return _START.get(k, "исполнение начато")
    if kind == "requote":
        return "перекотировка не прошла — план отменён"
    if kind == "requote_ok":
        return "котировка обновлена, исполнение идёт"
    if kind == "paused":
        head = {"HALTED_MISMATCH": "остановка", "ABORTED": "вход отменён"}.get(st, "пауза")
        why = reason_text(data.get("reason"))
        return f"{head}: {why}" if why else head
    if kind == "auto_unwind":
        return "автооткат голой ноги"
    if kind == "approve":
        return "разрешение токена выдано" if data.get("status") == "ok" else "разрешение токена не прошло"
    if kind == "dex_not_sent":
        return "своп не отправлен"
    if kind == "dex":
        return {"ok": "своп на DEX выполнен", "reverted": "своп на DEX откатился"}.get(data.get("status"),
                                                                                      "исход свопа выясняется")
    if kind == "perp_unknown":
        return "исход заявки на перпе выясняется"
    if kind == "replan":
        return "остаток перепланирован"
    if kind == "fixed":
        return _FIXED.get(k, "исполнение завершено")
    if kind == "final":
        if st == "CLOSED":
            return "выход завершён — сделка закрыта"
        if st == "OPEN":
            return "частичный выход завершён" if k == "exit" else "вход завершён"
        return "исполнение завершено"
    if kind in ("reconcile_clip", "reconcile_order"):
        return "сверка после перезапуска"
    if kind == "restart_check":
        m = data.get("matched")
        return "проверка после перезапуска: " + ("ноги сходятся" if m is True else "расхождение" if m is False else "идёт")
    return "событие исполнения"


# --- числа ------------------------------------------------------------------------------------------------
