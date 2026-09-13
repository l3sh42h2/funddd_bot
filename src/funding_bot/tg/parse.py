"""Разбор команд владельца — чистая функция: текст → команда (trade_spec §6, отчёт telegram §5).

Русские слова — основной синтаксис: slash-команды Telegram допускают только латиницу (BotCommand), поэтому
кириллица приходит простым текстом. Алиасы: /status /positions /stop /help (+ /start для своего id).

Нормализация (владелец пишет с телефона):
  регистр, ё→е, лишние пробелы; ведущий «/» у русских слов («/стоп») не мешает;
  разделитель спота «· • . - / _ :» → «·»: okx·bsc = okx-bsc = okx.bsc = OKX/BSC; «okx bsc» через пробел — тоже;
  сумма: $500, 500$, 500 usdt, 500,5 → Decimal; экспонента («1e3») и мусор — отказ, а не догадка;
  монета — в верхний регистр.

Команда с деньгами разбирается строго: лишний или непонятный аргумент — Unknown с подсказкой формата, а не
«наверное, имелось в виду». Кнопка всё равно подтверждает план, но план по неверно понятой сумме — лишний круг.
«стоп» — наоборот, мягко: любое «стоп …» останавливает (остановка безопасна, пропущенная — нет).

Цель «выход»/«продолжить»/«дохедж»/«откат» — id сделки/намерения ИЛИ монета. Различает движок по базе:
«DEGEN» по форме похож на id (D + 4 знака алфавита id), поэтому parse только подсказывает looks_like_id().
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from .. import config

# сети спота OKX DEX: подпись строки таблицы (dexleg.TAG: bsc/sol/rh) и синонимы
SPOT_CHAINS = {"bsc": "bsc", "bnb": "bsc", "sol": "sol", "solana": "sol", "rh": "rh", "robinhood": "rh"}
SPOT_DEXES = {"okx": "okx", "okxdex": "okx"}
SPOT_ANY_CHAIN = "okx"          # «okx dex» без сети — сеть решает движок по таблице (engine.Desk._pair)
PERP_ALIASES = {**{v: v for v in config.PERP_VENUES}, "hl": "hyperliquid"}
SEP = "·"
_SEP_RE = re.compile(r"[·•.\-/_:]+")
_COIN_RE = re.compile(r"^[A-Z0-9]{1,20}$")
_AMOUNT_RE = re.compile(r"^(\d{1,9})(?:[.,](\d{1,6}))?$")
_ID_RE = re.compile(r"^[DEX][A-HJ-NP-Z2-9]{4}$")          # store.new_id: алфавит без I/O/0/1
_ALL_WORDS = frozenset({"все", "всё", "all", "весь", "вся"})
_PERP_ONLY = frozenset({"перп", "perp", "перп-только", "перптолько"})
_CB_RE = re.compile(r"^(ok|no):([A-Z0-9]{2,12}):([0-9a-f]{8})$")
CALLBACK_MAX_BYTES = 64                                     # лимит callback_data Bot API

ENTRY_FMT = ("вход <монета> <спот> <перп> <сумма на ногу>, например: вход AIW3 okx·bsc aster 500 "
             "или вход AIW3 okx dex aster 450")
EXIT_FMT = "выход <id|монета> [сумма|всё] или выход <id> перп"


# --- команды ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    coin: str
    spot: str               # «okx·bsc»
    perp: str               # «aster»
    usd: Decimal            # на ногу
    name: str = "entry"


@dataclass(frozen=True)
class Exit:
    target: str             # id сделки или монета (решает движок)
    usd: Decimal | None     # None — всё
    perp_only: bool = False     # «выход <id> перп» — закрыть только перп (явная команда, §6)
    name: str = "exit"


@dataclass(frozen=True)
class Positions:
    name: str = "positions"


@dataclass(frozen=True)
class Status:
    name: str = "status"


@dataclass(frozen=True)
class Stop:
    name: str = "stop"


@dataclass(frozen=True)
class Resume:
    target: str | None = None   # без id — снять паузу; с id — свежий план и кнопки для прерванного
    name: str = "resume"


@dataclass(frozen=True)
class Rehedge:
    target: str
    name: str = "rehedge"


@dataclass(frozen=True)
class Undo:
    target: str
    name: str = "undo"


@dataclass(frozen=True)
class Help:
    name: str = "help"


@dataclass(frozen=True)
class Start:
    name: str = "start"


@dataclass(frozen=True)
class Unknown:
    text: str
    reason: str
    name: str = "unknown"


Command = Entry | Exit | Positions | Status | Stop | Resume | Rehedge | Undo | Help | Start | Unknown

_SLASH = {"/status": "статус", "/positions": "позиции", "/stop": "стоп", "/help": "помощь", "/start": "/start"}
_WORDS = {
    "вход": "entry", "выход": "exit", "позиции": "positions", "статус": "status", "стоп": "stop",
    "продолжить": "resume", "дохедж": "rehedge", "откат": "undo", "помощь": "help", "/start": "start",
}


# --- нормализация -------------------------------------------------------------------------------
def normalize(text: str) -> list[str]:
    """Нижний регистр, ё→е, пробелы схлопнуты; «/status@bot» → «статус»; ведущий «/» у русских слов снят."""
    s = (text or "").replace(" ", " ").strip().lower().replace("ё", "е")
    toks = s.split()
    if not toks:
        return []
    head = toks[0].split("@", 1)[0]
    if head in _SLASH:
        head = _SLASH[head]
    elif head.startswith("/") and head[1:] in _WORDS:
        head = head[1:]
    head = head.rstrip("!.?")
    return [head, *toks[1:]]


def parse_amount(s: str) -> Decimal | None:
    """«$500», «500$», «500usdt», «500,5» → Decimal > 0; иначе None. Без экспонент и тысячных разделителей."""
    t = (s or "").strip().lower().replace(" ", "")
    for suf in ("usdt", "usd", "$"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    t = t.removeprefix("$")
    m = _AMOUNT_RE.match(t)
    if not m:
        return None
    try:
        d = Decimal(m.group(1) + ("." + m.group(2) if m.group(2) else ""))
    except InvalidOperation:
        return None
    return d if d > 0 else None


_CUR_WORDS = frozenset({"usdt", "usd", "$"})


def amount_of(toks: list[str]) -> Decimal | None:
    """Сумма из 1–2 токенов: «500», «$500», «500 usdt», «$ 500». Два числа подряд («500 600», «500 000») — отказ:
    склеить их в 500600 / 500000 значило бы угадать сумму сделки."""
    if len(toks) == 1:
        return parse_amount(toks[0])
    if len(toks) == 2:
        a, b = toks
        if b in _CUR_WORDS:
            return parse_amount(a + b)
        if a == "$":
            return parse_amount(b)
    return None


def parse_spot(s: str) -> str | None:
    """«okx·bsc» / «okx-bsc» / «OKX.BSC» / «okxdex/solana» → «okx·bsc»; «okx dex» / «okxdex» / «okx-dex» → «okx»
    (сеть не названа: движок берёт её из таблицы и показывает в плане до кнопки — владелец 12.09); голое «okx»,
    неизвестная сеть или DEX — None."""
    parts = [p for p in _SEP_RE.split((s or "").strip().lower()) if p]
    if parts in (["okxdex"], ["okx", "dex"]):
        return SPOT_ANY_CHAIN
    if len(parts) != 2:
        return None
    dex, chain = SPOT_DEXES.get(parts[0]), SPOT_CHAINS.get(parts[1])
    return f"{dex}{SEP}{chain}" if dex and chain else None


def parse_coin(s: str) -> str | None:
    c = (s or "").strip().upper().removeprefix("$")
    return c if _COIN_RE.match(c) else None


def parse_target(s: str) -> str | None:
    """id сделки/намерения или монета — в верхний регистр; мусор — None."""
    return parse_coin(s)


def looks_like_id(s: str | None) -> bool:
    """Форма id (D/E/X + 4 знака алфавита store.new_id). Монета той же формы («DEGEN») возможна — сверять с БД."""
    return bool(s) and bool(_ID_RE.match(s.upper()))


# --- разбор -------------------------------------------------------------------------------------
def parse(text: str) -> Command:
    toks = normalize(text)
    raw = (text or "").strip()
    if not toks:
        return Unknown(raw, "пустое сообщение")
    head, args = toks[0], toks[1:]
    kind = _WORDS.get(head)
    if kind is None:
        return Unknown(raw, f"нет такой команды «{head[:30]}»")
    if kind == "stop":
        return Stop()
    if kind in ("positions", "status", "help", "start"):
        if args:
            return Unknown(raw, f"«{head}» без аргументов")
        return {"positions": Positions, "status": Status, "help": Help, "start": Start}[kind]()
    if kind == "entry":
        return _entry(raw, args)
    if kind == "exit":
        return _exit(raw, args)
    if kind == "resume":
        if not args:
            return Resume()
        if len(args) == 1 and (t := parse_target(args[0])):
            return Resume(t)
        return Unknown(raw, "формат: продолжить [id]")
    # дохедж / откат — только с целью: действие над конкретной сделкой
    if len(args) == 1 and (t := parse_target(args[0])):
        return Rehedge(t) if kind == "rehedge" else Undo(t)
    return Unknown(raw, f"формат: {head} <id>")


def _entry(raw: str, args: list[str]) -> Command:
    if len(args) < 4:
        return Unknown(raw, "формат: " + ENTRY_FMT)
    coin = parse_coin(args[0])
    if coin is None:
        return Unknown(raw, f"монета «{args[0][:20]}» — только латиница и цифры")
    rest = args[1:]
    spot = parse_spot(rest[0])
    if spot is None and len(rest) >= 2 and rest[0] in SPOT_DEXES:       # «okx bsc» через пробел
        spot = parse_spot(f"{rest[0]}{SEP}{rest[1]}")
        if spot is not None:
            rest = rest[1:]
    if spot is None:
        return Unknown(raw, f"спот «{rest[0][:20]}» не понят — нужно okx dex или okx·bsc (okx·sol, okx·rh)")
    rest = rest[1:]
    if spot == SPOT_ANY_CHAIN and rest and rest[0] in SPOT_CHAINS:     # «okx dex bsc» — сеть всё же названа
        spot, rest = f"okx{SEP}{SPOT_CHAINS[rest[0]]}", rest[1:]
    if not rest:
        return Unknown(raw, "формат: " + ENTRY_FMT)
    perp = PERP_ALIASES.get(rest[0])
    if perp is None:
        return Unknown(raw, f"перп «{rest[0][:20]}» не понят — площадки: {', '.join(config.PERP_VENUES)}")
    amount_toks = rest[1:]
    if not amount_toks or len(amount_toks) > 2:
        return Unknown(raw, "формат: " + ENTRY_FMT)
    usd = amount_of(amount_toks)
    if usd is None:
        return Unknown(raw, f"сумма «{' '.join(amount_toks)[:20]}» не понята — число USDT на ногу, например 500")
    return Entry(coin=coin, spot=spot, perp=perp, usd=usd)


def _exit(raw: str, args: list[str]) -> Command:
    if not args:
        return Unknown(raw, "формат: " + EXIT_FMT)
    target = parse_target(args[0])
    if target is None:
        return Unknown(raw, f"цель «{args[0][:20]}» не понята — id сделки или монета")
    rest = args[1:]
    perp_only = False
    if rest and rest[0] in _PERP_ONLY:
        perp_only, rest = True, rest[1:]
    if not rest:
        return Exit(target, None, perp_only)
    if len(rest) == 1 and rest[0] in _ALL_WORDS:
        return Exit(target, None, perp_only)
    if perp_only:
        return Unknown(raw, "выход … перп — только целиком (без суммы)")
    if len(rest) > 2:
        return Unknown(raw, "формат: " + EXIT_FMT)
    usd = amount_of(rest)
    if usd is None:
        return Unknown(raw, f"сумма «{' '.join(rest)[:20]}» не понята — число USDT или «всё»")
    return Exit(target, usd, False)


# --- данные кнопок --------------------------------------------------------------------------------
@dataclass(frozen=True)
class Callback:
    action: str             # ok | no
    intent_id: str
    nonce: str


def callback_data(action: str, intent_id: str, nonce: str) -> str:
    """ok:E7K2:9f3a1c20 — 16 байт при лимите 64. Проверяется сразу: битая кнопка — ошибка кода, а не молчание."""
    s = f"{action}:{intent_id}:{nonce}"
    if parse_callback(s) is None or len(s.encode()) > CALLBACK_MAX_BYTES:
        raise ValueError(f"недопустимые данные кнопки: {s!r}")
    return s


def parse_callback(data: str | None) -> Callback | None:
    m = _CB_RE.match(data or "")
    return Callback(m.group(1), m.group(2), m.group(3)) if m else None
