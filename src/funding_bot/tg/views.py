"""Русские шаблоны сообщений (HTML) — чистые функции: данные → текст (trade_spec §6, отчёт telegram §9).

Вариант C владельца 12.09 «только то, что требует внимания» — для ВСЕХ сообщений бота:
- штатное не пишется вовсе: ни свёрнутых блоков, ни техники (α/β, шаг, слиппедж, газ по частям, остатки, адреса,
  id плана), пока с ними всё в норме;
- вышло за порог показа (tconfig.SHOW_*) — отдельная строка «⚠️ …» сразу под заголовком; маркер заголовка — ✅,
  если замечаний нет, ⚠️, если есть; у плана всегда 📝, а ⚠️ идут строками под ним;
- всегда видно: 🧪 в симуляции (первая строка SIM_PREFIX), действие, монета, «на ногу» или итог в $; у плана — ⏱
  срок; фандинг в $/ч с направлением («нам» или «⚠️ шорт ПЛАТИТ»); курсовой; издержки «вход + выход» и окупаемость;
  голая нога в $; PnL выхода; следующая команда в <code>; ссылка на tx в live;
- один маркер в начале: 🧪 📝 ⏳ ✅ ⚠️ 🛑 🚨 ⏸ ▶️ ⛔ 🔁 ⌛ ❌ ♻️ 📊 🩺 🟢 ⏹ ❓.

Числа — одни форматтеры для views, движка и сверки (trade/engine.py, trade/reconcile.py пишут через них):
- токены целые с разрядами «4 902» (при шаге перпа меньше 1 — до его знаков); дробь только у остатка меньше шага;
- $: размер ноги «200 $», издержки/PnL/фандинг 2 знака, меньше цента — «< 0.01 $»;
- проценты: фандинг 3 знака «+0.036 %/ч», остальное 2; цены — 4 значащих; минус «−», у потоков явный «+»;
- время UTC «ЧЧ:ММ», длительности «17 с» / «3 ч 10 мин»; id — в <code>, только где его придётся набрать;
- тысячи — неразрывным пробелом: число не переносится.
Прежние правила: КАЖДАЯ динамическая строка проходит escape() (текст ошибки биржи бывает с «<»); неизвестное —
«—» или «не прочитано», НИКОГДА 0 (позиция None = UNKNOWN, trade_spec §4); числа приходят уже посчитанными
(trade/report.py), здесь только форматирование, подписи и пороги показа.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Iterable, Mapping
from ..trade import tconfig
from .parse import Unknown, callback_data
from .sender import escape, tx_link
from ..trade.formatters import (
    NBSP, MINUS, DASH, USD, CENT, VENUE_LABEL, DEAL_STATE_LABEL,
    _d, _signed, num, money, _leg_num, leg, tok, px, pct, dur, plural, contracts, m_unknown_text,
)

SIM_MARK = "🧪"
SIM_PREFIX = f"{SIM_MARK} <b>СИМУЛЯЦИЯ</b> · деньги не двигаются"

CHAIN_LABEL = {"bsc": "BSC", "sol": "Solana", "rh": "Robinhood", "robinhood": "Robinhood"}
NATIVE = {"bsc": "BNB", "sol": "SOL", "rh": "ETH", "robinhood": "ETH"}
MODE_LABEL = {"dry": "симуляция", "readonly": "только чтение", "live": "live"}
FIX_WORD = {"rehedge": "Дохедж", "undo": "Откат"}
# DEAL_STATE_LABEL, VENUE_LABEL -- теперь в trade/formatters.py (AC-07, 16.09)
# Причина паузы сделки — внутренний код (engine.Pause.reason); владельцу — по-русски
REASON_LABEL = {"stop": "стоп владельца", "terminate": "остановка службы", "restart": "перезапуск",
                "hedge_deficit": "перп не добран", "reduce_only_reject": "биржа отклонила reduceOnly",
                "perp_unknown": "исход заявки неизвестен", "perp_refused": "заявка не отправлена",
                "perp_rejected": "биржа отклонила заявку", "dex_unknown": "исход свопа неизвестен",
                "dex_refused": "своп не отправлен", "dex_revert": "своп откатился",
                "position_mismatch": "расхождение с биржей", "position_unknown": "позиция не прочитана",
                "book_unknown": "книга сделки неизвестна", "wallet_unknown": "кошелёк не прочитан",
                "exec_time": "время исполнения вышло", "funding_sign": "фандинг сменил знак",
                "funding": "фандинг не прочитан", "limit": "лимит владельца", "native": "мало BNB на газ",
                "mode": "режим не live", "owner": "owner.toml не прочитан", "owner_missing": "не задано в owner.toml",
                "error": "сбой исполнителя", "approve_unknown": "исход approve неизвестен",
                "approve_refused": "approve не отправлен", "approve_failed": "approve не прошёл",
                "setup": "настройка перпа", "replan": "остаток не планируется", "changed": "дельта сменила знак",
                "rehedge": "дохедж", "undo": "откат", "state": "состояние сделки", "busy": "занято другой сделкой",
                "plan_ab": "α/β не заморожены"}

# Ответы на нажатие кнопки (answerCallbackQuery, ≤ 200 символов)
CB_ACCEPTED = "✅ принято"
CB_CANCELLED = "❌ отменено"
CB_ALREADY = "уже принято"
CB_ALREADY_CANCELLED = "уже отменено"
CB_RUNNING = "⏳ уже выполняется"
CB_EXPIRED = "⌛ план истёк — пришлите команду заново"
CB_DONE = "уже исполнено"
CB_INTERRUPTED = "прервано перезапуском — «продолжить <id>» даст свежий план"
CB_PAUSED = "⏸ пауза: план не принят. Снять — «продолжить»"
CB_BUSY = "⏳ уже идёт другое исполнение"
CB_OLD_BUTTON = "кнопка от старого плана"
CB_STALE = "неактуально"
CB_UNKNOWN = "кнопка не распознана"

# Подписи ключей owner.toml для отказов («⛔ Вход запрещён: не задано «дневной стоп»»)
OWNER_KEY_LABELS = {
    "mode": "режим",
    "telegram.owner_id": "id владельца в Telegram",
    "wallets.bsc": "кошелёк BSC",
    "wallets.aster_user": "основной аккаунт Aster",
    "wallets.aster_signer": "API-кошелёк (агент) Aster",
    "limits.deal_max_usd_per_leg": "сумма сделки на ногу",
    "limits.max_open_deals": "число открытых сделок",
    "limits.daily_loss_stop_usd": "дневной стоп",
    "limits.daily_loss_basis": "определение дневного стопа",
    "limits.min_entry_funding_pct_h": "порог фандинга",
    "limits.min_entry_basis_bps": "порог курсового спреда",
    "dex.slippage_pct": "слиппедж DEX",
    "dex.clip_slippage_pct": "слиппедж клипа",
    "dex.impact_cap_pct": "защита от удара цены",
    "dex.approve_policy": "политика approve",
    "dex.broadcast": "способ отправки транзакций",
    "dex.allow_tax_tokens": "токены с налогом",
    "dex.native_reserve": "резерв газа в кошельке",
    "exec.clip_max_usd": "максимум клипа",
    "exec.clips_max": "максимум клипов",
    "exec.unhedged_usd_max": "предел незахеджированного",
    "exec.exec_time_max_s": "время исполнения",
    "exec.refill_wait_max_s": "ожидание пополнения стакана",
    "exec.plan_cost_drift_pct": "допуск перекотировки",
    "exec.auto_unwind_naked_after_s": "авто-откат голой ноги",
}
_PERP_KEY_LABELS = {"leverage": "плечо", "margin_type": "тип маржи", "max_slip_bps": "предел цены IOC",
                    "touch_frac_max": "доля лучшего уровня", "maker_allowed": "мейкер-заявки",
                    "liq_alert_pct": "тревога до ликвидации", "allow_contract_multiplier": "контракты с множителем"}


def key_label(key: str) -> str:
    if key in OWNER_KEY_LABELS:
        return OWNER_KEY_LABELS[key]
    parts = key.split(".")
    if len(parts) == 3 and parts[0] == "perp" and parts[2] in _PERP_KEY_LABELS:
        return f"{_PERP_KEY_LABELS[parts[2]]} {VENUE_LABEL.get(parts[1], parts[1])}"
    return key


# --- форматирование чисел и времени (общие для views, движка и сверки) ------------------------------------
def usd(v: Any, places: int = 2, sign: bool = False) -> str:
    return num(v, places, sign)


def about(v: Any) -> str:
    """Оценка в $ после «≈»: от 100 $ — целыми («≈ 250 $»), меньше — с центами."""
    d = _d(v)
    if d is None:
        return DASH
    return num(d, 0) + USD if abs(d) >= 100 else money(d)


def hm(ts: float | None) -> str:
    """Время UTC «ЧЧ:ММ»."""
    if ts is None:
        return DASH
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M")


def hms(ts: float | None, seconds: bool = False) -> str:
    if ts is None:
        return DASH
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%H:%M:%S" if seconds else "%H:%M") + " UTC"


def hours(h: Any) -> str:
    """Окупаемость: меньше часа — минутами, до 48 ч — «3.9 ч», дальше — «5 д 2 ч»."""
    d = _d(h)
    if d is None:
        return DASH
    if 1 <= d < 48:
        return f"{num(d, 1)} ч"
    return dur(float(d) * 3600)


def short_addr(a: str | None) -> str:
    a = str(a or "")
    return escape(a if len(a) <= 12 else f"{a[:6]}…{a[-4:]}") if a else DASH


# --- сборка сообщения ----------------------------------------------------------------------------------
def _sim(text: str, sim: bool) -> str:
    return f"{SIM_PREFIX}\n{text}" if sim else text


def _sim1(text: str, sim: bool) -> str:
    """Одна строка: 🧪 — первым символом, без отдельной строки «СИМУЛЯЦИЯ»."""
    return f"{SIM_MARK} {text}" if sim else text


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s and not s.startswith("&") else s


def _add(ws: list[str], text: str) -> None:
    """Строка ⚠️ без повторов: заметка движка «баланс BNB не прочитан — …» поглощает такую же проверку вида."""
    k = text.lower()
    if not any(k.startswith(x.lower()) or x.lower().startswith(k) for x in ws):
        ws.append(text)


def _compose(icon: str, head: str, ws: list[str], body: Iterable[str | None], sim: bool = False) -> str:
    lines = [f"{icon} {head}"] + [f"⚠️ {w}" for w in ws] + [b for b in body if b]
    return _sim("\n".join(lines), sim)


def _chain(c: str | None) -> str:
    return escape(CHAIN_LABEL.get(str(c or ""), str(c or "").upper() or DASH))


def _venue(v: str | None) -> str:
    return escape(VENUE_LABEL.get(str(v or ""), str(v or "") or DASH))


def _stable(c: str | None) -> str:
    """Стейбл спота сети в текстах: Robinhood — USDG, прочие — USDT (как было)."""
    return "USDG" if str(c or "").lower() in ("rh", "robinhood") else "USDT"


def _native(c: str | None) -> str:
    return escape(NATIVE.get(str(c or ""), "газ"))


def _labels(keys: Iterable[str], limit: int = 8) -> str:
    ks = list(keys)
    s = ", ".join(f"«{escape(key_label(k))}»" for k in ks[:limit])
    return s + (f" и ещё {len(ks) - limit}" if len(ks) > limit else "")


def _mark(ok: bool | None) -> str:
    return "✓" if ok is True else ("✗" if ok is False else "?")


def _sum(*vals: Any) -> Decimal | None:
    ds = [_d(v) for v in vals if _d(v) is not None]
    return sum(ds, Decimal(0)) if ds else None


def _hedged(delta: Any, step: Any) -> bool | None:
    """Ноги ровно: 0 ≤ токены − |шорт| < шаг перпа (шаг неизвестен — только точный ноль). None — дельта неизвестна."""
    d, st = _d(delta), _d(step)
    if d is None:
        return None
    return d == 0 if st is None else Decimal(0) <= d < st


def _tstep(step: Any, m: Any) -> Any:
    """Шаг перпа в токенах (шаг · токенов в контракте) — для дельты ног и количеств спота. m = 1 или неизвестен —
    шаг как есть (тексты m = 1 прежние)."""
    s, k = _d(step), _d(m)
    return step if (s is None or k is None or k == 1 or k == 0) else s * k     # 0 — m не известен


def _m_unknown(m: Any) -> bool:
    """m = 0 в тексте — множитель контракта сделки не известен (engine.DealBook.m_view)."""
    k = _d(m)
    return k is not None and k == 0


def _m_unknown_cmd(did: str) -> str:
    return f"Множитель контракта не известен — только <code>выход {did}</code> целиком"


def _links(chain: str | None, hashes: Iterable[str]) -> str:
    """Ссылки на транзакции: первая — «tx 0x4b5e…1764», следующие — хэшем; больше трёх — «и ещё N»."""
    hs = [h for h in hashes if h]
    if not hs:
        return ""
    parts = [tx_link(hs[0], str(chain or ""), f"tx {hs[0][:6]}…{hs[0][-4:]}")]
    parts += [tx_link(h, str(chain or "")) for h in hs[1:3]]
    if len(hs) > 3:
        parts.append(f"и ещё {len(hs) - 3}")
    return " · ".join(parts)


# --- пороги показа (tconfig.SHOW_*): что выносится строкой ⚠️ ----------------------------------------------
def _warn_gas(ws: list[str], gas: Any) -> None:
    g = _d(gas)
    if g is not None and g > tconfig.SHOW_GAS_USD:
        _add(ws, f"Газ {money(g)}")


def _warn_native(ws: list[str], chain: str | None, native: Any, native_px: Any, gas_per_swap: Any) -> None:
    n_, p_, g_ = _d(native), _d(native_px), _d(gas_per_swap)
    if None in (n_, p_, g_) or g_ <= 0:
        return
    swaps = int(n_ * p_ / g_)
    if swaps < tconfig.SHOW_NATIVE_SWAPS_MIN:
        _add(ws, f"{_native(chain)} {num(n_, 4)} — хватит на {swaps} {plural(swaps, 'своп', 'свопа', 'свопов')}")


def _warn_leverage(ws: list[str], lev: Any, mt: str | None) -> None:
    bad_lev = lev is not None and _d(lev) != tconfig.SHOW_LEVERAGE
    bad_mt = bool(mt) and mt != tconfig.SHOW_MARGIN_TYPE
    if bad_lev or bad_mt:
        _add(ws, f"Плечо {escape(lev) if lev is not None else DASH}x {escape(mt or DASH)}")


def _warn_unread(ws: list[str], venue: str, stable: Any, native: Any, margin: Any, chain: str | None = "bsc") -> None:
    if stable is None:
        _add(ws, f"Баланс {_stable(chain)} не прочитан")
    if native is None:
        _add(ws, f"Баланс {_native(chain)} не прочитан")
    if margin is None:
        _add(ws, f"Маржа {venue} не прочитана")


def _warn_next_deal(ws: list[str], venue: str, leg_usd: Any, stable_after: Any, margin_after: Any, lev: Any,
                    open_after: Any, max_open: Any) -> None:
    """Следующая сделка того же размера — только если лимит сделок её допускает: не хватит USDT или маржи — ⚠️."""
    lg, mx = _d(leg_usd), _d(max_open)
    if lg is None or mx is None or open_after is None or int(open_after) >= mx:
        return
    st, mg = _d(stable_after), _d(margin_after)
    if st is not None and st < lg:
        _add(ws, f"USDT на следующую сделку {leg(lg)} не хватит: {num(st)}")
    need = lg / (_d(lev) or 1)
    if mg is not None and mg < need:
        _add(ws, f"Маржи {venue} на следующую сделку не хватит: {num(mg)} из {num(need)}")


def _warn_naked(ws: list[str], coin: str, delta: Any, step: Any, usd_: Any = None) -> None:
    if _hedged(delta, step) is False:
        u = _d(usd_)
        _add(ws, f"Без хеджа {tok(delta, True, step)} {coin}" + (f" ≈ {about(abs(u))}" if u else ""))


def _warn_liq(ws: list[str], dist: Any, thr: Any, who: str = "") -> None:
    d, t = _d(dist), _d(thr)
    if d is not None and t is not None and d < t * tconfig.SHOW_LIQ_ALERT_X:
        v = f"<b>{pct(d)}</b>" if d < t else pct(d)
        _add(ws, f"{who}До ликвидации {v} — тревога при {pct(t)}")


# --- строки каркаса -------------------------------------------------------------------------------------
def _funding_line(pct_h: Any, usd_h: Any) -> str:
    """План: «Фандинг +0.036 %/ч → нам +0.07 $/ч»; шорт платит — «→ ⚠️ шорт ПЛАТИТ 0.02 $/ч»."""
    f, u = _d(pct_h), _d(usd_h)
    if f is None:
        return f"Фандинг {DASH}"
    s = f"Фандинг {pct(f, 3, sign=True)}/ч"
    if f > 0 and u is not None:
        s += f" → нам {money(u, sign=True)}/ч"
    elif f < 0:
        s += " → ⚠️ шорт ПЛАТИТ" + (f" {money(abs(u))}/ч" if u is not None else "")
    return s


def _funding_usd(usd_h: Any) -> str:
    """Итог: «Фандинг +0.07 $/ч»; шорт платит — «Фандинг: ⚠️ шорт ПЛАТИТ 0.07 $/ч»."""
    u = _d(usd_h)
    if u is None:
        return f"Фандинг {DASH}"
    if u < 0:
        return f"Фандинг: ⚠️ шорт ПЛАТИТ {money(-u)}/ч"
    return f"Фандинг {money(u, sign=True)}/ч"


def _basis_line(b: Any) -> str:
    d = _d(b)
    if d is None:
        return f"Курсовой {DASH}"
    s = f"Курсовой {pct(d, 2, sign=True)}"
    q = d.quantize(CENT)
    return s + (" в нашу пользу" if q > 0 else (" против нас" if q < 0 else ""))


def _cost_line(entry_usd: Any, exit_usd: Any, payback: Any, usd_h: Any) -> str:
    """«Вход + выход 0.28 $ · окупится за 3.9 ч» — окупаемость одна (report.payback_h)."""
    e = _d(entry_usd)
    s = f"Вход + выход {money(None if e is None else e + (_d(exit_usd) or 0))}"
    u = _d(usd_h)
    if _d(payback) is not None:
        s += f" · окупится за {hours(payback)}"
    elif u is not None and u <= 0:
        s += " · не окупится: фандинг против нас"
    return s


def _clips_line(clips: Iterable[Any]) -> str | None:
    """Клипов ≥ 2 — видимая строка «2 клипа по 250 $» (разные — «3 клипа: 250 + 150 + 100 $»)."""
    cs = [_leg_num(c) for c in clips]
    n = len(cs)
    if n < 2:
        return None
    word = plural(n, "клип", "клипа", "клипов")
    if len(set(cs)) == 1:
        return f"{n} {word} по {cs[0]}{USD}"
    return f"{n} {word}: " + " + ".join(cs[:6]) + (" + …" if n > 6 else "") + USD


def _naked_cmds(did: str, delta: Any, step: Any, m: Any = None) -> list[str]:
    """Три команды для голой ноги с пояснением (откат — только для голого лонга). delta — в токенах; m — токенов в
    контракте: откат продаёт кратное шагу·m токенов, дохедж откупает контракты вверх к шагу (как propose_fix)."""
    d = _d(delta) or Decimal(0)
    ts = _tstep(step, m)
    if d > 0:
        st = _d(ts)
        sell = d - d % st if (st is not None and st > 0) else d      # откат продаёт кратное шагу (как propose_fix)
        return [f"<code>дохедж {did}</code> — шорт ещё раз",
                f"<code>откат {did}</code> — продать {tok(sell, step=ts)} на DEX",
                f"<code>выход {did}</code> — закрыть всё"]
    k, st = _d(m), _d(step)
    if k is None or k == 1:
        buy = tok(abs(d), step=step)
    else:
        q = abs(d) / k
        if st is not None and st > 0:
            q = (q / st).to_integral_value(ROUND_CEILING) * st
        buy = contracts(q, k, step=step)
    return [f"<code>дохедж {did}</code> — откупить {buy} на перпе",
            f"<code>выход {did}</code> — закрыть всё"]


# --- короткие ответы ---------------------------------------------------------------------------------
def start_reply(chat_id: int | None, user_id: int | None) -> str:
    """Незнакомцу на /start — только его ids (владелец так узнаёт свой id для owner.toml)."""
    return f"chat_id: <code>{escape(chat_id)}</code>\nuser_id: <code>{escape(user_id)}</code>"


def help_text(sim: bool = False, sol: bool = False) -> str:
    """sol — связка Solana × Hyperliquid включена в owner.toml: строка её команд (иначе текст прежний)."""
    lines = [
        "<b>Команды</b> (только владелец):",
        "<code>вход AIW3 okx·bsc aster 500</code> — план, 500 $ на ногу",
        "<code>выход DS455</code> · <code>выход AIW3 200</code> · <code>выход DS455 перп</code>",
        "<code>позиции</code> · <code>статус</code> · <code>стоп</code> · <code>продолжить [id]</code>",
        "<code>дохедж id</code> · <code>откат id</code> — для голой ноги",
        f"Деньги двигаются только после ✅ под планом; план живёт {tconfig.PLAN_TTL_S} с",
    ]
    if sol:
        lines.insert(3, "<code>вход ANSEM sol-auto hyperliquid·para 150</code> — Solana × Hyperliquid, 150 USDC · "
                        "<code>позиции sol</code>")
    return _sim("\n".join(lines), sim)


def unknown(u: Unknown) -> str:
    return f"❓ {_cap(escape(u.reason))} · список — <code>помощь</code>"


def stale(date: int | float | None) -> str:
    return f"⌛ Команда от {hm(date)} устарела (бот был недоступен) — повторите"


def refused(reason: str) -> str:
    return f"⛔ {_cap(escape(reason))}"


def owner_missing(keys: Iterable[str], action: str = "вход") -> str:
    """Отказ live: чего нет в owner.toml. Подпись + путь ключа — владелец видит, что именно заполнить."""
    ks = list(keys)
    head = f"⛔ <b>{_cap(escape(action))} запрещён</b>"
    if len(ks) == 1:
        return f"{head}: не задано «{escape(key_label(ks[0]))}»\nowner.toml → <code>{escape(ks[0])}</code>"
    return (f"{head}: не задано {len(ks)}\n"
            + "\n".join(f"{escape(key_label(k))} — <code>{escape(k)}</code>" for k in ks))


def owner_config_error(err: Any) -> str:
    return f"⛔ <b>owner.toml не прочитан</b>: {escape(err)}"


def busy(intent_id: str | None) -> str:
    who = f" {escape(intent_id)}" if intent_id else ""
    return f"⏳ Занят исполнением{who} — дождитесь итога или <code>стоп</code>"


def planning(coin: str, kind: str = "entry") -> str:
    what = "входа" if kind == "entry" else "выхода"
    return f"⏳ Считаю план {what} {escape(coin)}…"


def requote(reason: str, sim: bool = False) -> str:
    return _sim(f"🔁 Пересчитал план: {escape(reason)}. Ничего не отправлено — новый план ниже", sim)


def error(text: Any) -> str:
    return f"⚠️ {_cap(escape(text))}"


def executor_crash(intent_id: str, err: Any) -> str:
    return f"⚠️ <b>Сбой исполнителя</b> на {escape(intent_id)}: {escape(err)} · проверить: <code>позиции</code>"


def paused(clip: int | None = None, clips: int | None = None) -> str:
    if clip:
        return f"⏸ Пауза: клип {clip}/{clips or '?'} довожу до пары ног, дальше стоп"
    return "⏸ Пауза: новых клипов и входов нет. Снять — <code>продолжить</code>"


def already_paused() -> str:
    return "⏸ Пауза уже стоит. Снять — <code>продолжить</code>"


def resumed(interrupted: Iterable[str] = ()) -> str:
    ids = [escape(i) for i in interrupted]
    s = "▶️ Пауза снята, сам ничего не продолжаю"
    if ids:
        s += "\nПрерваны: " + " · ".join(f"<code>продолжить {i}</code>" for i in ids)
    return s


def not_paused() -> str:
    return "▶️ Паузы нет"


def service_stopping() -> str:
    return "⏹ Служба останавливается: новое не начинаю, текущую пару ног довожу"


def bot_started(mode: str, username: str | None = None) -> str:
    who = f" @{escape(username)}" if username else ""
    if mode == "dry":
        return f"{SIM_MARK} <b>Бот{who} запущен</b> · симуляция, ключи не загружены"
    return f"🟢 <b>Бот{who} запущен</b> · {escape(MODE_LABEL.get(mode, mode))}"


def conflict_alarm() -> str:
    return "⚠️ <b>Telegram 409</b>: токен опрашивает другой процесс — команды могут уходить не сюда"


def webhook_alarm(host: str) -> str:
    return (f"⚠️ <b>Вебхук на токене</b> ({escape(host)}): команды не принимаю, выхожу. Нужен отдельный бот или снять "
            "вебхук вручную")


def watchdog_alarm(renewals_1h: int, busy_: bool) -> str:
    n = int(renewals_1h)
    tail = "идёт исполнение, не перезапускаюсь" if busy_ else "перезапускаю службу"
    return f"⚠️ Telegram: {n} {plural(n, 'обрыв', 'обрыва', 'обрывов')} за час — {tail}"


def auto_unwind(coin: str, qty: Any = None, usd_: Any = None, sim: bool = False) -> str:
    what = f" {tok(qty)} {escape(coin)}" if _d(qty) is not None else ""
    tail = f" ≈ {about(usd_)}" if _d(usd_) else ""
    return _sim1(f"⚠️ <b>{escape(coin)}: авто-откат голой ноги</b> — без команды продаю{what}{tail} на DEX", sim)


# --- план и кнопки ---------------------------------------------------------------------------------------
def plan_title(kind: str, coin: str, *, usd: Any = None, exit_all: bool = True, perp_only: bool = False,
               deal_usd: Any = None) -> str:
    """Шапка плана и строки его закрытия (HTML, экранирована): «Вход AIW3 · 200 $ на ногу», «Выход AIW3 · всё»,
    «Выход AIW3 · 200 из 500 $», «Выход AIW3 · только перп», «Дохедж AIW3»."""
    c = escape(coin)
    if kind == "entry":
        return f"Вход {c} · {leg(usd)} на ногу" if _d(usd) is not None else f"Вход {c}"
    if kind == "exit":
        u = _d(usd)
        if perp_only:
            return f"Выход {c} · только перп"
        if exit_all or u is None:
            return f"Выход {c} · всё"
        return f"Выход {c} · {_leg_num(u)} из {leg(deal_usd)}" if _d(deal_usd) is not None else f"Выход {c} · {leg(u)}"
    return f"{FIX_WORD.get(kind, escape(kind))} {c}"


def ok_label(kind: str, *, usd: Any = None, exit_all: bool = True, perp_only: bool = False,
             deal_usd: Any = None) -> str:
    """Подпись кнопки «да» (простой текст): «✅ Войти 200 $», «✅ Выйти всё», «✅ Дохедж»."""
    if kind == "entry":
        return f"✅ Войти {leg(usd)}" if _d(usd) is not None else "✅ Войти"
    if kind == "exit":
        u = _d(usd)
        if perp_only:
            return "✅ Выйти из перпа"
        return "✅ Выйти всё" if (exit_all or u is None) else f"✅ Выйти {leg(u)}"
    return f"✅ {FIX_WORD.get(kind, 'Да')}"


@dataclass(frozen=True)
class PlanHead:
    title: str          # HTML, экранирован
    ok: str             # подпись кнопки «да»
    sim: bool


def intent_head(intent: Mapping | None, deal: Mapping | None = None) -> PlanHead:
    """Шапка плана из данных намерения (spec_json) и сделки — переживает перезапуск: и подпись кнопки, и строка
    закрытия плана строятся отсюда, а не из HTML в памяти."""
    if not intent:
        return PlanHead("План", "✅ Да", False)
    try:
        spec = json.loads(intent["spec_json"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        spec = {}
    spec = spec if isinstance(spec, dict) else {}
    coin = spec.get("coin") or (deal["coin"] if deal else "")
    kw = dict(usd=spec.get("usd"), exit_all=bool(spec.get("all", True)), perp_only=bool(spec.get("perp_only")),
              deal_usd=deal["leg_usd"] if deal else None)
    sim = bool(deal["sim"]) if deal else bool(spec.get("sim"))
    kind = str(intent["kind"])
    if spec.get("profile") == "sol_best_hyperliquid" and kind == "entry" and _d(spec.get("usd")) is not None:
        # связка Solana × Hyperliquid: вход — бюджет USDC (не $ и не «на ногу»)
        u = _leg_num(spec.get("usd"))
        return PlanHead(f"Вход {escape(coin)} · {u} USDC", f"✅ Войти {u} USDC", sim)
    return PlanHead(plan_title(kind, coin, **kw), ok_label(kind, **kw), sim)


def plan_keyboard(intent_id: str, nonce: str, ok: str = "✅ Да") -> dict:
    """[✅ Войти 200 $] [❌ Нет] — callback ok:<intent>:<nonce8> / no:<intent>:<nonce8> (данные кнопок прежние)."""
    return {"inline_keyboard": [[{"text": ok, "callback_data": callback_data("ok", intent_id, nonce)},
                                 {"text": "❌ Нет", "callback_data": callback_data("no", intent_id, nonce)}]]}


def plan_closed(action: str, title: str = "План", ts: float | None = None, sim: bool = False) -> str:
    """Одна строка, которой правится сообщение плана (кнопки снимаются): «⏳ <b>Вход AIW3 · 200 $ на ногу</b> —
    принят 18:58, исполняю». title — plan_title()/intent_head().title (уже экранирован)."""
    t = f"<b>{title}</b>"
    line = {
        "ok": f"⏳ {t} — принят {hm(ts)}, исполняю",
        "no": f"❌ {t} — отменён",
        "expired": f"⌛ {t} — истёк, ничего не сделано",
        "requote": f"🔁 {t} — заменён, новый ниже",
    }.get(action, f"⌛ {t} — неактуален")
    return _sim1(line, sim)


@dataclass(frozen=True)
class PlanView:
    intent_id: str
    kind: str                                   # entry | exit
    coin: str
    chain: str                                  # bsc
    perp_venue: str                             # aster
    symbol: str                                 # AIW3USDT
    leg_usd: Decimal | None                     # вход: USDT на ногу; выход: оценка $ продаваемого
    clips_usd: tuple[Decimal, ...] = ()
    token_qty: Decimal | None = None            # выход: токенов продать (перп: спот, что останется без хеджа)
    perp_qty: Decimal | None = None             # выход: |positionAmt| к откупу
    deal_id: str | None = None
    perp_only: bool = False                     # «выход <id> перп»
    ttl_s: int = tconfig.PLAN_TTL_S
    sim: bool = False
    exit_all: bool = True                       # выход всей сделки (False — «выход <id> 200»)
    req_usd: Decimal | None = None              # выход: сумма из команды
    deal_leg_usd: Decimal | None = None         # размер сделки на ногу: «200 из 500 $», «остаток 250 из 500 $»
    resume: bool = False                        # «продолжить»: план на остаток входа
    step: Decimal | None = None                 # шаг количества перпа
    leverage: int | None = None
    margin_type: str | None = None
    impact_usd: Decimal | None = None           # удар + комиссия пула по плану, $
    gas_usd_clip: Decimal | None = None         # газ одного свопа, $
    gas_usd_total: Decimal | None = None
    approve_gas_usd: Decimal | None = None
    native_px: Decimal | None = None            # цена газовой монеты, $
    total_usd: Decimal | None = None            # издержки входа (или выхода) по плану
    exit_cost_usd: Decimal | None = None        # вход: оценка выхода
    breakeven_h: Decimal | None = None          # report.payback_h
    funding_pct_h: Decimal | None = None        # + шорт получает
    usd_per_h: Decimal | None = None            # фандинг в $/ч на ногу
    basis_pct: Decimal | None = None            # perp_bid / dex_buy_px − 1, %
    min_funding_pct_h: Decimal | None = None    # пороги владельца: ниже — ⚠️
    min_basis_bps: Decimal | None = None
    wallet_stable: Decimal | None = None
    wallet_native: Decimal | None = None
    margin_avail: Decimal | None = None
    open_deals: int | None = None
    max_open_deals: int | None = None
    missing_owner_keys: tuple[str, ...] = ()    # dry-run: что заблокировало бы live
    notes: tuple[str, ...] = ()                 # предупреждения движка (одна строка каждое)
    m: Decimal | None = None                    # токенов в контракте (perp_qty — контракты); None/1 — прежний текст
    exit_root_qty: Decimal | None = None        # «продолжить» выхода: цель исходного выхода, токены


def plan(p: PlanView) -> str:
    entry = p.kind == "entry"
    coin, venue = escape(p.coin), _venue(p.perp_venue)
    ws: list[str] = []
    for n_ in p.notes:
        _add(ws, _cap(escape(n_)))
    if p.missing_owner_keys:
        _add(ws, f"Для live не задано: {len(p.missing_owner_keys)} · <code>статус</code>")
    _warn_unread(ws, venue, p.wallet_stable, p.wallet_native, p.margin_avail, p.chain)
    _warn_gas(ws, _sum(p.gas_usd_total, p.approve_gas_usd))
    _warn_native(ws, p.chain, p.wallet_native, p.native_px, p.gas_usd_clip)
    body: list[str | None] = []
    if entry:
        usd_h = p.usd_per_h
        if usd_h is None and _d(p.funding_pct_h) is not None and _d(p.leg_usd) is not None:
            usd_h = _d(p.funding_pct_h) * _d(p.leg_usd) / 100
        _warn_leverage(ws, p.leverage, p.margin_type)
        fd, mf = _d(p.funding_pct_h), _d(p.min_funding_pct_h)
        if fd is not None and mf is not None and fd < mf:
            _add(ws, f"Фандинг ниже порога {pct(mf, 3)}/ч")
        b, mb = _d(p.basis_pct), _d(p.min_basis_bps)
        if b is not None and mb is not None and b < mb / 100:
            _add(ws, f"Курсовой ниже порога {pct(mb / 100)}")
        lg, lev = _d(p.leg_usd), _d(p.leverage) or 1
        st, mg = _d(p.wallet_stable), _d(p.margin_avail)
        _warn_next_deal(ws, venue, lg, None if (st is None or lg is None) else st - lg,
                        None if (mg is None or lg is None) else mg - lg / lev, lev,
                        None if p.open_deals is None else p.open_deals + 1, p.max_open_deals)
        body += [f"Купить спот OKX DEX·{_chain(p.chain)} · шорт перп {venue}", _clips_line(p.clips_usd)]
        if p.resume and _d(p.deal_leg_usd) is not None:
            body.append(f"Остаток входа: {_leg_num(p.leg_usd)} из {leg(p.deal_leg_usd)}")
        body += [_funding_line(p.funding_pct_h, usd_h), _basis_line(p.basis_pct),
                 _cost_line(p.total_usd, p.exit_cost_usd, p.breakeven_h, usd_h)]
    elif p.perp_only:
        _add(ws, f"Спот {tok(p.token_qty, step=_tstep(p.step, p.m))} {coin} ≈ {about(p.leg_usd)} останется без хеджа")
        body += [f"Откупить шорт {contracts(p.perp_qty, p.m, step=p.step, coin=coin)} на {venue}",
                 f"Издержки ≈ {money(p.total_usd)}"]
    else:
        ts = _tstep(p.step, p.m)
        body.append(f"Продать спот {tok(p.token_qty, step=ts)} · откупить шорт {contracts(p.perp_qty, p.m, step=p.step)}")
        if p.resume and _d(p.exit_root_qty) is not None:
            body.append(f"Остаток выхода: {tok(p.token_qty, step=ts)} из {tok(p.exit_root_qty, step=ts)} {coin}")
        body += [_clips_line(p.clips_usd), f"Издержки выхода ≈ {money(p.total_usd)}"]
    body.append(f"⏱ {int(p.ttl_s)} с")
    title = plan_title(p.kind, p.coin, usd=p.leg_usd if entry else p.req_usd, exit_all=p.exit_all,
                       perp_only=p.perp_only, deal_usd=p.deal_leg_usd)
    return _compose("📝", f"<b>{title}</b>", ws, body, p.sim)


# --- дохедж и откат: план и итог ------------------------------------------------------------------------
@dataclass(frozen=True)
class FixPlanView:
    intent_id: str
    kind: str                       # rehedge | undo
    coin: str
    deal_id: str
    delta: Decimal | None           # токены − |шорт|: + голый лонг, − голый шорт
    qty: Decimal | None             # сколько отправим (перп — дохедж; DEX — откат)
    side: str | None = None         # дохедж: SELL | BUY
    usd: Decimal | None = None      # голая нога ≈ $
    perp_venue: str = "aster"
    step: Decimal | None = None
    ttl_s: int = tconfig.PLAN_TTL_S
    sim: bool = False
    m: Decimal | None = None        # токенов в контракте: qty дохеджа — контракты, отката — токены


def fix_plan(v: FixPlanView) -> str:
    coin, venue = escape(v.coin), _venue(v.perp_venue)
    d = _d(v.delta) or Decimal(0)
    ts = _tstep(v.step, v.m)
    side = "спота" if d > 0 else "шорта"
    body = [f"Без хеджа {tok(d, True, ts)} {coin}" + (f" ≈ {about(v.usd)} {side}" if _d(v.usd) else f" {side}")]
    if v.kind == "rehedge":
        verb = "Продать" if v.side == "SELL" else "Откупить"
        body.append(f"{verb} {contracts(v.qty, v.m, step=v.step, coin=coin)} на перпе {venue}")
    else:
        body.append(f"Продать {tok(v.qty, step=ts)} {coin} на DEX, перп не трогаю")
    body.append(f"⏱ {int(v.ttl_s)} с")
    return _compose("📝", f"<b>{plan_title(v.kind, v.coin)}</b>", [], body, v.sim)


_FIX_STATE = {"OPEN": "сделка открыта", "PAUSED": "сделка на паузе", "CLOSED": "сделка закрыта"}


@dataclass(frozen=True)
class FixDoneView:
    kind: str                       # rehedge | undo
    coin: str
    deal_id: str
    state: str                      # состояние сделки после
    qty: Decimal | None = None      # продано / откуплено
    usd: Decimal | None = None      # перп: $ заявки; откат: USDT на выходе свопа
    side: str | None = None         # дохедж: SELL | BUY
    noop: str | None = None         # «ноги уже ровно — ничего не отправлено»
    delta: Decimal | None = None    # дельта ног после (токены)
    step: Decimal | None = None
    sim: bool = False
    m: Decimal | None = None        # токенов в контракте: qty дохеджа — контракты


def fix_done(v: FixDoneView) -> str:
    coin, did = escape(v.coin), escape(v.deal_id)
    word = FIX_WORD.get(v.kind, escape(v.kind))
    ts = _tstep(v.step, v.m)
    hedged = _hedged(v.delta, ts)
    ws: list[str] = []
    q, u, dl = _d(v.qty), _d(v.usd), _d(v.delta)
    if q and v.kind == "rehedge" and _d(v.m) is not None:
        q = q * _d(v.m)                                      # контракты → токены: цена ниже — $ за токен
    avg = (u / q) if (q and u is not None) else None          # цена исполнения — оценка остатка голой ноги в $
    _warn_naked(ws, coin, v.delta, ts, (dl * avg) if (dl is not None and avg is not None) else None)
    state = _FIX_STATE.get(str(v.state), escape(DEAL_STATE_LABEL.get(str(v.state), str(v.state))))
    if v.noop:
        head = f"<b>{word} {coin}</b>: {escape(v.noop)}"
        body = [_cap(state)]
    else:
        head = f"<b>{word} {coin} выполнен</b>" + (" · ноги ровно ✓" if hedged else "")
        if v.kind == "rehedge":
            act = (f"{'Продано' if v.side == 'SELL' else 'Откуплено'} {contracts(v.qty, v.m, step=v.step)} на перпе = "
                   f"{money(v.usd)}")
        else:
            act = f"Продано {tok(v.qty, step=ts)} {coin} → {num(v.usd)} USDT"
        body = [f"{act} · {state}"]
    if str(v.state) == "PAUSED":
        body.append(f"<code>продолжить {did}</code> · <code>выход {did}</code>")
    elif str(v.state) == "OPEN":
        body.append(f"<code>выход {did}</code>")
    return _compose("⚠️" if ws else "✅", head, ws, body, v.sim)


@dataclass(frozen=True)
class PerpClosedView:
    coin: str
    deal_id: str
    qty: Decimal | None             # откуплено на перпе
    usd: Decimal | None             # $ откупа
    spot_qty: Decimal | None = None # спот, что остался без хеджа
    spot_usd: Decimal | None = None
    step: Decimal | None = None
    sim: bool = False
    m: Decimal | None = None        # токенов в контракте: qty — контракты


def perp_closed(v: PerpClosedView) -> str:
    coin, did = escape(v.coin), escape(v.deal_id)
    cmd = "выход" if _m_unknown(v.m) else "откат"        # m не известен (M3): откат считает дельту — продаст «выход»
    body = [f"Откуплено {contracts(v.qty, v.m, step=v.step)} = {money(v.usd)}",
            f"Спот {tok(v.spot_qty, step=_tstep(v.step, v.m))} {coin} ≈ {about(v.spot_usd)} — продать: "
            f"<code>{cmd} {did}</code>"]
    return _compose("⚠️", f"<b>{coin}: перп закрыт, спот без хеджа</b>", [], body, v.sim)


def resume_checked(deal_id: str, sim: bool = False) -> str:
    did = escape(deal_id)
    return _sim(f"✅ Сделка {did} сверена — ноги ровно ✓, пауза\n<code>продолжить {did}</code> · "
                f"<code>выход {did}</code>", sim)


def resume_mismatch(deal_id: str, detail: Any, sim: bool = False) -> str:
    return _sim(f"🛑 Сделка {escape(deal_id)} не сходится: {escape(detail)} — сначала <code>позиции</code>", sim)


# --- прогресс ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ProgressView:
    intent_id: str
    kind: str                       # entry | exit
    coin: str
    clip: int
    clips: int
    spot_usd: Decimal | None = None
    spot_qty: Decimal | None = None
    spot_avg: Decimal | None = None
    perp_qty: Decimal | None = None     # модуль
    perp_usd: Decimal | None = None
    perp_avg: Decimal | None = None
    imbalance_qty: Decimal | None = None    # спот − |перп|: + лишний спот
    imbalance_usd: Decimal | None = None
    gas_usd: Decimal | None = None
    fees_usd: Decimal | None = None
    note: str | None = None             # «жду пополнения стакана (до 30 с)»
    sim: bool = False
    total_usd: Decimal | None = None    # сколько всего по плану ($ на ногу)
    step: Decimal | None = None
    m: Decimal | None = None            # токенов в контракте: perp_qty — контракты


def progress(v: ProgressView) -> str:
    """Только при 2 клипах и больше (так решает движок), без звука (так шлёт бот)."""
    entry = v.kind == "entry"
    coin = escape(v.coin)
    head = f"<b>{'Вход' if entry else 'Выход'} {coin}</b> · клип {v.clip}/{v.clips}"
    if _d(v.total_usd) is not None:
        head += f" · {_leg_num(v.spot_usd)} из {leg(v.total_usd)}"
    ws: list[str] = []
    ts = _tstep(v.step, v.m)
    _warn_naked(ws, coin, v.imbalance_qty, ts, v.imbalance_usd)
    _warn_gas(ws, v.gas_usd)
    ok = " ✓" if _hedged(v.imbalance_qty, ts) else ""
    pq = _d(v.perp_qty)
    if entry:
        line = (f"Спот {tok(v.spot_qty, True, ts)} · шорт {contracts(None if pq is None else -pq, v.m, True, v.step)}"
                f"{ok}")
    else:
        line = f"Продано {tok(v.spot_qty, step=ts)} · откуплено {contracts(pq, v.m, step=v.step)}{ok}"
    return _compose("⏳", head, ws, [line, _cap(escape(v.note)) if v.note else None], v.sim)


# --- итог -----------------------------------------------------------------------------------------------
from ..ipc.reports import FinalView


def _final_warns(v: FinalView, coin: str, venue: str) -> list[str]:
    ws: list[str] = []
    imp, pimp = _d(v.impact_usd), _d(v.planned_impact_usd)
    if imp is not None and pimp is not None and pimp > 0 and imp > pimp * tconfig.SHOW_IMPACT_X \
            and imp - pimp >= CENT / 2:
        _add(ws, f"Удар спота {money(imp)} — план {money(pimp)}")
    cost, pc = _d(v.cost_usd), _d(v.planned_cost_usd)
    if cost is not None and pc is not None and pc > 0 and cost > pc * (1 + tconfig.SHOW_COST_OVER_PLAN):
        _add(ws, f"Издержки {money(cost)} — план {money(pc)} (+{num((cost / pc - 1) * 100, 0)}{NBSP}%)")
    if not v.partial:
        _warn_naked(ws, coin, v.dust_qty, _tstep(v.step, v.m), v.dust_usd)
    if v.kind == "entry":
        _warn_liq(ws, v.liq_dist_pct, v.liq_alert_pct)
    _warn_gas(ws, _sum(v.gas_usd, v.approve_gas_usd))
    g = _d(v.gas_usd)
    _warn_native(ws, v.chain, v.wallet_native, v.native_px, (g / v.swaps) if (g is not None and v.swaps) else None)
    if not v.sim:
        _warn_unread(ws, venue, v.wallet_stable, v.wallet_native, v.margin_avail, v.chain)
    if v.kind == "entry":
        _warn_leverage(ws, v.leverage, v.margin_type)
        _warn_next_deal(ws, venue, v.leg_usd, v.wallet_stable, v.margin_avail, v.leverage, v.open_deals,
                        v.max_open_deals)
    return ws


def final(v: FinalView) -> str:
    coin, venue, did = escape(v.coin), _venue(v.perp_venue), escape(v.deal_id)
    ws = _final_warns(v, coin, venue)
    u = _d(v.expected_usd_h)                # шорт ПЛАТИТ фандинг — тоже замечание: заголовок ⚠️, а не ✅
    icon = "⚠️" if (ws or (v.kind == "entry" and u is not None and u < 0)) else "✅"
    links = _links(v.chain, v.tx_hashes)
    ts = _tstep(v.step, v.m)
    if v.kind == "entry":
        pq = _d(v.perp_qty)
        head = f"<b>{coin} открыта · {leg(v.leg_usd if v.leg_usd is not None else v.spot_usd)} на ногу</b>"
        body = [f"Спот {tok(v.spot_qty, True, ts)} · шорт {venue} "
                f"{contracts(None if pq is None else -pq, v.m, True, v.step, coin=coin)}",
                f"{_funding_usd(v.expected_usd_h)} · курсовой {pct(v.basis_pct, 2, sign=True)}",
                _cost_line(v.cost_usd, v.exit_cost_usd, v.breakeven_h, v.expected_usd_h),
                " · ".join([f"<code>выход {did}</code>"] + ([links] if links else []))]
        return _compose(icon, head, ws, body, v.sim)
    if not v.partial:
        spot, perp, fund = _d(v.pnl_spot_usd), _d(v.pnl_perp_usd), _d(v.funding_usd)
        total, no_fund = _d(v.pnl_total_usd), False
        if total is None and v.sim and spot is not None and perp is not None:
            total, no_fund = spot + perp, True          # в симуляции фандинг не начисляется
        flows = f"спот {money(spot, True, unit=False)} · перп {money(perp, True, unit=False)}"
        body = [f"Без фандинга (симуляция): {flows}" if no_fund
                else f"{_cap(flows)} · фандинг {money(fund, True, unit=False)}",
                f"Издержки выхода {money(v.cost_usd)}", links]
        return _compose(icon, f"<b>{coin} закрыта · {money(total, sign=True)}</b>", ws, body, v.sim)
    if v.exit_all:                          # заказан весь выход, а сделка открыта — недовыполнен
        _add(ws, "Выход не довыполнен" + (f": {escape(v.partial_reason)}" if v.partial_reason else ""))
    hedged = _hedged(v.dust_qty, ts)
    _warn_naked(ws, coin, v.dust_qty, ts, v.dust_usd)
    part = (f"{_leg_num(v.spot_usd)} из {leg(v.deal_leg_usd)}" if _d(v.deal_leg_usd) is not None
            else leg(v.spot_usd))
    body = [f"Остаток {tok(v.rest_qty, step=ts)} {coin} открыт" + (" и захеджирован ✓" if hedged else ""),
            f"Издержки {money(v.cost_usd)}", f"<code>выход {did}</code> · <code>позиции</code>", links]
    return _compose("⚠️" if ws else "✅", f"<b>{coin}: выход {part}</b>", ws, body, v.sim)


# --- остановка / ошибка --------------------------------------------------------------------------------
@dataclass(frozen=True)
class HaltView:
    intent_id: str
    kind: str                           # entry | exit
    coin: str
    reason: str                         # «Aster −2019 Margin is insufficient»
    perp_venue: str = "aster"
    deal_id: str | None = None
    clip: int | None = None             # клип, на котором встали
    clips: int | None = None
    perp_pos: Decimal | None = None     # позиция на бирже со знаком; None = не прочитана
    wallet_tokens: Decimal | None = None
    unhedged_qty: Decimal | None = None  # + лишний спот, − лишний шорт; 0 — ноги ровно
    unhedged_usd: Decimal | None = None
    auto_unwind_s: Decimal | None = None
    sim: bool = False
    ts: float | None = None             # когда встали: «откачу сам в 19:17 UTC»
    clips_done: int | None = None       # клипов доведено до пары ног
    state: str | None = None            # состояние сделки после остановки
    step: Decimal | None = None
    m: Decimal | None = None            # токенов в контракте: perp_pos — контракты, unhedged_qty — токены


def halt(v: HaltView) -> str:
    coin, venue = escape(v.coin), _venue(v.perp_venue)
    did, iid = escape(v.deal_id or v.intent_id), escape(v.intent_id)
    what = "Вход" if v.kind == "entry" else "Выход"
    reason = _cap(escape(v.reason))
    if v.state == "ABORTED":
        return _compose("⛔", f"<b>{what} {coin} не начат</b>", [], [reason, "Ничего не куплено — сделка снята"], v.sim)
    uq = _d(v.unhedged_qty)
    pos, wt = _d(v.perp_pos), _d(v.wallet_tokens)
    ws: list[str] = []
    if pos is None:
        _add(ws, f"Позиция {venue} не прочитана")
    if wt is None:
        _add(ws, "Кошелёк не прочитан")
    ts = _tstep(v.step, v.m)
    legs = (f"Спот {tok(wt, True, ts) if wt is not None else 'не прочитан'} · шорт "
            f"{contracts(pos, v.m, True, v.step) if pos is not None else 'не прочитан'}")
    where = f" на клипе {v.clip}/{v.clips or '?'}" if v.clip else ""
    if uq is None and v.unhedged_usd is None:
        tail = _m_unknown_cmd(did) if _m_unknown(v.m) else "Сначала <code>позиции</code>, потом команда"
        return _compose("🛑", f"<b>{coin}: баланс ног неизвестен</b>", ws,
                        [f"{what} встал{where}: {escape(v.reason)}", legs, tail], v.sim)
    if uq is None or uq != 0:
        side = "шорта" if (uq is not None and uq < 0) else "спота"
        size = f"{tok(abs(uq), step=ts)} ≈ {about(v.unhedged_usd)}" if uq is not None else f"≈ {about(v.unhedged_usd)}"
        body = [f"{what} встал{where}: {escape(v.reason)}", legs, *_naked_cmds(did, uq, v.step, v.m)]
        if v.auto_unwind_s is not None and v.ts is not None:
            body.append(f"Без команды откачу сам в {hms(v.ts + float(v.auto_unwind_s))}")
        elif v.auto_unwind_s is not None:
            body.append(f"Без команды откачу сам через {dur(float(v.auto_unwind_s))}")
        return _compose("🛑", f"<b>{coin}: без хеджа {size} {side}</b>", ws, body, v.sim)
    if v.state == "HALTED_MISMATCH":
        return _compose("🛑", f"<b>{coin}: {what.lower()} остановлен, расхождение</b>", ws,
                        [reason, legs, "Сначала <code>позиции</code>, потом команда"], v.sim)
    after = f" после клипа {v.clips_done}/{v.clips or '?'}" if v.clips_done else ""
    return _compose("⏸", f"<b>{coin}: {what.lower()} на паузе</b>{after}", ws,
                    [reason, "Ноги ровно ✓", f"<code>продолжить {iid}</code> · <code>выход {did}</code>"], v.sim)


# --- перезапуск -------------------------------------------------------------------------------------------
from ..ipc.reports import RestartView


def restart(v: RestartView) -> str:
    what = "входа" if v.kind == "entry" else "выхода"
    coin = escape(v.coin or v.intent_id)
    iid, did = escape(v.intent_id), escape(v.deal_id or v.intent_id)
    if v.matched is not True:
        verdict = "не сходится" if v.matched is False else "не сверена"
        body = [_cap(escape(v.details)) if v.details else None, "Сначала <code>позиции</code>, потом команда"]
        return _compose("🛑", f"<b>Перезапуск: {coin} {verdict}</b> · сделка {did} остановлена", [], body, v.sim)
    head = f"<b>Перезапуск во время {what} {coin}</b>" + (f" · клип {v.clip}/{v.clips or '?'}" if v.clip else "")
    ws: list[str] = []
    if v.state == "ABORTED":
        body = ["Ничего не куплено — сделка снята"]
    elif v.state == "CLOSED":
        body = ["Выход был доведён — сделка закрыта ✓"]
    elif v.hedged is False:
        u = _d(v.delta_usd)                 # голая нога в $ — всегда видна (C)
        _add(ws, (f"Без хеджа {tok(v.delta, True, _tstep(v.step, v.m))} {coin}" + (f" ≈ {about(abs(u))}" if u else ""))
             if _d(v.delta) is not None else "Ноги не ровно")
        body = ["Сам не продолжаю", *_naked_cmds(did, v.delta, v.step, v.m)]
    elif v.hedged is None and _m_unknown(v.m):     # m не известен (ревью 13.09, M3): дельта не считается
        body = ["Сам не продолжаю", _m_unknown_cmd(did)]
    elif v.hedged is None:                  # шаг перпа не прочитан — «ноги ровно ✓» было бы неправдой
        _add(ws, "Ровность ног не проверена: шаг перпа не прочитан")
        body = ["Сам не продолжаю", "Сначала <code>позиции</code>, потом команда"]
    else:
        body = ["Ноги ровно ✓ · сам не продолжаю", f"<code>продолжить {iid}</code> · <code>выход {did}</code>"]
    return _compose("♻️", head, ws, body, v.sim)


def restart_check(coin: str, deal_id: str, matched: bool | None, detail: str | None, state: str,
                  sim: bool = False, *, hedged: bool | None = None, delta: Any = None, usd: Any = None,
                  step: Any = None, m: Any = None) -> str:
    """Сверка после перезапуска для сделки без прерванного намерения (сошлась и не менялась — не пишется вовсе).
    Сошлась с журналом, но нога голая — не «✅»: голая нога в $ и три команды (C, правило голой ноги)."""
    c, did = escape(coin), escape(deal_id)
    st = escape(DEAL_STATE_LABEL.get(str(state), str(state)))
    if matched is True and hedged is False and _d(delta) is not None:
        u = _d(usd)
        ws = [f"Без хеджа {tok(delta, True, _tstep(step, m))} {c}" + (f" ≈ {about(abs(u))}" if u else "")]
        return _compose("⚠️", f"<b>{c}</b>: сверено после перезапуска · {st}", ws, _naked_cmds(did, delta, step, m),
                        sim)
    if matched is True and _m_unknown(m):   # m не известен (ревью 13.09, M3): не «✅», одна команда
        return _compose("⚠️", f"<b>{c}</b>: сверено после перезапуска · {st}", [], [_m_unknown_cmd(did)], sim)
    if matched is True:
        return _sim1(f"✅ <b>{c}</b>: сверено после перезапуска ✓ · {st}", sim)
    verdict = "не сходится" if matched is False else "не сверена"
    icon = "🛑" if (matched is False or state == "HALTED_MISMATCH") else "⚠️"
    body = [_cap(escape(detail)) if detail else None, "Сначала <code>позиции</code>, потом команда"]
    return _compose(icon, f"<b>Перезапуск: {c} {verdict}</b> · сделка {did}: {st}", [], body, sim)


# --- позиции -----------------------------------------------------------------------------------------------
from ..ipc.reports import PositionView


def pnl_line(now: Any, exit_: Any) -> str | None:
    """«PnL сейчас +0.52 $ · при выходе +0.10 $» (вариант C: PnL выхода всегда виден). Расчёта нет — строки нет."""
    if _d(now) is None and _d(exit_) is None:
        return None
    s = f"PnL сейчас {money(now, sign=True)}"
    return s + (f" · при выходе {money(exit_, sign=True)}" if _d(exit_) is not None else "")


def _position_problems(p: PositionView) -> list[str]:
    coin, venue = escape(p.coin), _venue(p.perp_venue)
    out: list[str] = []
    if p.perp_qty is None:
        _add(out, f"позиция {venue} не прочитана")
    if p.spot_qty is None:
        _add(out, "кошелёк не прочитан")
    if _hedged(p.delta_qty, p.step) is False:
        u = _d(p.delta_usd)                # голая нога в $ — всегда видна (спот и перп в $ в «позициях» больше нет)
        _add(out, f"без хеджа {tok(p.delta_qty, True, p.step)} {coin}" + (f" ≈ {about(abs(u))}" if u else ""))
    d, t = _d(p.liq_dist_pct), _d(p.liq_alert_pct)
    if d is not None and t is not None and d < t * tconfig.SHOW_LIQ_ALERT_X:
        _add(out, f"до ликвидации {f'<b>{pct(d)}</b>' if d < t else pct(d)} (порог {pct(t)})")
    if p.pnl_uncovered:
        _add(out, f"стакан {venue} мельче шорта — выход оценён по худшей цене")
    if p.m_unknown:
        _add(out, f"множитель контракта не известен — только <code>выход {escape(p.deal_id)}</code> целиком")
    if p.state != "OPEN":
        st = escape(DEAL_STATE_LABEL.get(p.state, p.state))
        _add(out, st + (f" — {escape(REASON_LABEL.get(p.reason, p.reason))}" if p.reason else ""))
    return out


def positions(items: Iterable[PositionView], *, ts: float | None, matched: bool | None,
              mismatch: str | None = None, sim: bool = False) -> str:
    items = list(items)
    if not items:
        return _sim("📊 Открытых сделок нет", sim)
    n = len(items)
    fund = _sum(*(p.funding_usd for p in items))
    show_fund = fund is not None and fund != 0            # «фандинг 0.00 $» до первого расчёта — штатное, не пишем
    head = (f"📊 <b>{n} {plural(n, 'сделка', 'сделки', 'сделок')}"
            + (f" · фандинг {money(fund, sign=True)}" if show_fund else "") + f"</b> · сверка {_mark(matched)}")
    lines = [head]
    if matched is False:
        lines.append(f"⚠️ Расхождение: {escape(mismatch or DASH)}")
    elif matched is None and mismatch:
        lines.append(f"⚠️ Не сверено: {escape(mismatch)}")
    rows = []
    for p in items:
        probs = _position_problems(p)
        name = f"{SIM_MARK + ' ' if p.sim else ''}{escape(p.coin)} <code>{escape(p.deal_id)}</code>"
        meta = [f"{leg(p.leg_usd)} на ногу" if _d(p.leg_usd) is not None else None,
                dur(p.held_s) if p.held_s is not None else None]
        pb = _d(p.payback_h)
        pay = None if pb is None else ("окупилась ✓" if pb == 0 else f"до окупаемости ~{hours(pb)}")
        pnl = pnl_line(p.pnl_now_usd, p.pnl_exit_usd)
        if probs:
            block = [f"⚠️ {name}: {'; '.join(probs)}", _cap(" · ".join(x for x in meta + [pay] if x)), pnl]
        else:
            block = [" · ".join([name] + [x for x in meta if x]), _cap(pay) if pay else None, pnl]
        rows.append((not probs, [b for b in block if b]))
    for _ok, block in sorted(rows, key=lambda r: r[0]):     # проблемные — первыми (сортировка устойчива)
        lines += block
    return _sim("\n".join(lines), sim)


# --- статус -------------------------------------------------------------------------------------------------
from ..ipc.reports import StatusView


def status(v: StatusView) -> str:
    probs: list[str] = []
    for what, ok, det in v.checks:
        if ok is not True:                                   # прошедшие проверки — штатное, не пишем
            probs.append(f"{_mark(ok)} {escape(what)}" + (f": {escape(det)}" if det else ""))
    if v.sender_fails:
        n = int(v.sender_fails)
        probs.append(f"⚠️ Telegram: {n} {plural(n, 'отправка', 'отправки', 'отправок')} подряд не ушли")
    if v.tg_last_ok_ago_s is not None and v.tg_last_ok_ago_s > tconfig.TG_RENEW_AFTER_S:
        probs.append(f"⚠️ Telegram: успешный опрос {dur(v.tg_last_ok_ago_s)} назад")
    if v.mode != "dry":                                      # в dry балансы не читаются по устройству
        ws: list[str] = []
        _warn_unread(ws, "Aster", v.wallet_stable, v.wallet_native, v.margin_avail, v.chain)
        probs += [f"⚠️ {w}" for w in ws]
    if v.missing_owner_keys:
        probs.append(f"⚠️ Для live не задано: {_labels(v.missing_owner_keys)}")
    n = len(probs)
    verdict = "всё в порядке ✓" if not n else f"⚠️ {n} {plural(n, 'проблема', 'проблемы', 'проблем')}"
    deals = (f"{escape(v.open_deals) if v.open_deals is not None else DASH}/"
             f"{escape(v.max_open_deals) if v.max_open_deals is not None else DASH}")
    state = ["⏸ пауза" if v.paused else "пауза нет"]
    if v.running:
        state.append(f"идёт {escape(v.running)}")
    state.append(f"сделок {deals}")
    if isinstance(v.daily_stop, Decimal):
        state.append(f"день {money(v.daily_used_usd, sign=True, unit=False)} из {leg(v.daily_stop)}")
    elif v.daily_stop is None:
        state.append("дн. стоп не задан")
    lines = [f"🩺 <b>Статус</b> · {escape(MODE_LABEL.get(v.mode, v.mode))} · {verdict}", *probs, _cap(" · ".join(state))]
    return _sim("\n".join(lines), v.mode == "dry")
