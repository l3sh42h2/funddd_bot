# Shared operator command grammar used by CLI and core; Telegram is one input transport.
# No Telegram client, authorization decisions, markup or delivery code belongs here.
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

Связка Solana × Hyperliquid (ТЗ SOL×HL §14) — отдельные команды ProfileEntry / ProfileExit / ProfilePositions:
  вход ANSEM sol-auto hyperliquid·para 200     (лучший маршрут из Jupiter и OKX; 200 — USDC на вход)
  вход ANSEM jupiter·sol hyperliquid·para 200  (только Jupiter)     вход ANSEM okx·sol hyperliquid·para 200
  выход ANSEM sol [всё | 500 ansem | 120 usdc]  выход ANSEM 500 ansem   позиции sol
«hyperliquid·para» — площадка + dex, не тикер. Монета — как написана (регистр у HL значим); mint и fullcoin из текста
не берутся — «ANSEM» разрешает движок по реестру профиля. Новый разбор включается только там, где старый отказал, и
только в узнаваемой форме: прежние команды BSC дают прежний результат и прежний текст отказа.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from . import config
from .trade.owner import LEGACY_PROFILE, SOL_HL

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
class EntryWithStop(Entry):
    """New syntax only.  Keeping legacy ``Entry`` structurally unchanged preserves old command snapshots."""
    stop_price: Decimal = Decimal(0)


@dataclass(frozen=True)
class Resize:
    target: str
    usd: Decimal
    name: str = "resize"


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


@dataclass(frozen=True)
class ProfileEntry:
    """Вход связки (ТЗ SOL×HL §7). spot_policy — политика выбора маршрута, а не фактический исполнитель клипа."""
    coin: str               # как написано, без префикса dex (регистр значим: kPEPE ≠ KPEPE)
    spot_policy: str        # auto | jupiter | okx
    spot_chain: str         # solana
    perp_venue: str         # hyperliquid
    perp_dex: str | None    # para; None — не назван: движок берёт единственную запись реестра профиля
    usdc: Decimal           # бюджет USDC на вход (ExactIn), не USD и не USDT
    profile: str = SOL_HL
    name: str = "profile_entry"

    @property
    def fullcoin(self) -> str | None:
        return f"{self.perp_dex}:{self.coin}" if self.perp_dex else None


@dataclass(frozen=True)
class ProfileExit:
    """Выход связки (ТЗ §8): цель — токены (tokens) или USDC (движок переведёт в сырые токены при предложении и покажет);
    оба None — всё, что принадлежит сделке."""
    target: str             # id сделки или монета как написаны (id движок сверяет без регистра)
    profile: str | None     # связка, если названа («sol», «hyperliquid·para»)
    perp_dex: str | None
    tokens: Decimal | None
    usdc: Decimal | None
    name: str = "profile_exit"


@dataclass(frozen=True)
class ProfilePositions:
    """«позиции sol» — позиции одной связки. name как у Positions: старый бот покажет все позиции (безвредно)."""
    profile: str
    perp_dex: str | None = None
    name: str = "positions"


Command = (Entry | Exit | Resize | Positions | Status | Stop | Resume | Rehedge | Undo | Help | Start | Unknown | ProfileEntry
           | ProfileExit | ProfilePositions)

_SLASH = {"/status": "статус", "/positions": "позиции", "/stop": "стоп", "/help": "помощь", "/start": "/start"}
_WORDS = {
    "вход": "entry", "выход": "exit", "добор": "resize", "уменьшить": "exit", "позиции": "positions", "статус": "status", "стоп": "stop",
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
            if kind == "positions" and len(args) == 1 and (pw := _profile_word(args[0])):
                return ProfilePositions(*pw)
            return Unknown(raw, f"«{head}» без аргументов")
        return {"positions": Positions, "status": Status, "help": Help, "start": Start}[kind]()
    if kind == "entry":
        res = _entry(raw, args)
        return (_profile_entry(raw, args, _raw_args(text, len(args))) or res) if isinstance(res, Unknown) else res
    if kind == "resize":
        if len(args) in (2, 3) and (t := parse_target(args[0])) and (usd := amount_of(args[1:])):
            return Resize(t, usd)
        return Unknown(raw, "формат: добор <id|монета> <сумма>")
    if kind == "exit":
        res = _exit(raw, args)
        return (_profile_exit(raw, args, _raw_args(text, len(args))) or res) if isinstance(res, Unknown) else res
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
    stop_price = None
    if len(amount_toks) >= 2 and amount_toks[-2] in ("sl", "стоплосс"):
        stop_price = parse_amount(amount_toks[-1])
        amount_toks = amount_toks[:-2]
        if stop_price is None:
            return Unknown(raw, "SL — положительная цена, например: вход AIW3 okx·bsc aster 500 sl 0.03")
    if not amount_toks or len(amount_toks) > 2:
        return Unknown(raw, "формат: " + ENTRY_FMT)
    usd = amount_of(amount_toks)
    if usd is None:
        return Unknown(raw, f"сумма «{' '.join(amount_toks)[:20]}» не понята — число USDT на ногу, например 500")
    if stop_price is not None:
        return EntryWithStop(coin=coin, spot=spot, perp=perp, usd=usd, stop_price=stop_price)
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


# --- связка Solana × Hyperliquid --------------------------------------------------------------------
SOL_POLICIES = {"auto": "auto", "best": "auto", "jupiter": "jupiter", "jup": "jupiter", "okx": "okx", "okxdex": "okx"}
SOL_WORDS = frozenset({"sol", "solana"})
PROFILE_ROUTES = {("solana", "hyperliquid"): SOL_HL}       # (сеть спота, площадка перпа) → связка
PROFILE_WORDS = {"sol": SOL_HL, "solana": SOL_HL, SOL_HL: SOL_HL, "bsc": LEGACY_PROFILE, LEGACY_PROFILE: LEGACY_PROFILE}
_DEX_RE = re.compile(r"^[a-z0-9]{1,16}$")
_RAW_COIN_RE = re.compile(r"^(?:([A-Za-z0-9]{1,16}):)?\$?([A-Za-z0-9]{1,20})$")
_TOKENS_RE = re.compile(r"^(\d{1,12})(?:[.,](\d{1,9}))?$")
_TOKEN_WORDS = frozenset({"токен", "токена", "токенов", "tok", "token", "tokens", "шт"})
PROFILE_ENTRY_FMT = ("вход <монета> <sol-auto | jupiter·sol | okx·sol> hyperliquid·<dex> <USDC>, например: "
                     "вход ANSEM sol-auto hyperliquid·para 200")
PROFILE_EXIT_FMT = "выход <id|монета> [sol | hyperliquid·<dex>] [<N> <монета|токенов> | <USDC> | всё]"


def _raw_args(text: str, n: int) -> list[str] | None:
    """Аргументы как написаны (регистр монеты), в тех же позициях, что у normalize(); расхождение — None."""
    toks = (text or "").replace(" ", " ").strip().split()[1:]
    return toks if len(toks) == n else None


def _parts(tok: str) -> list[str]:
    return [p for p in _SEP_RE.split(tok) if p]


def _sol_policy(tok: str) -> str | None:
    """«sol-auto» / «auto·sol» / «best·sol» / «jupiter·sol» / «jup:solana» / «okx·sol» → политика; иначе None."""
    parts = _parts(tok)
    if len(parts) != 2:
        return None
    a, b = parts
    if a in SOL_WORDS and b in SOL_POLICIES:
        return SOL_POLICIES[b]
    if a in SOL_POLICIES and b in SOL_WORDS:
        return SOL_POLICIES[a]
    return None


def _perp_dex(tok: str) -> tuple[str, str | None] | None:
    """«hyperliquid·para» / «hl:para» → ('hyperliquid', 'para'); «hyperliquid» → ('hyperliquid', None)."""
    parts = _parts(tok)
    if not parts or len(parts) > 2 or parts[0] not in PERP_ALIASES:
        return None
    if len(parts) == 2 and not _DEX_RE.match(parts[1]):
        return None
    return PERP_ALIASES[parts[0]], (parts[1] if len(parts) == 2 else None)


def _profile_word(tok: str) -> tuple[str, str | None] | None:
    """Имя связки в команде: «sol», «solana», «bsc», id профиля или перп с dex («hyperliquid·para»)."""
    if tok in PROFILE_WORDS:
        return PROFILE_WORDS[tok], None
    pv = _perp_dex(tok)
    if pv and pv[1] is not None and ("solana", pv[0]) in PROFILE_ROUTES:
        return PROFILE_ROUTES[("solana", pv[0])], pv[1]
    return None


def _usdc_of(toks: list[str]) -> Decimal | None:
    """Сумма USDC: «200», «$200», «200$», «200 usdc», «200usdc». USDT здесь — отказ: котировка связки в USDC."""
    if len(toks) == 2:
        a, b = toks
        if b in ("usdc", "$"):
            toks = [a + b]
        elif a == "$":
            toks = [b]
        else:
            return None
    if len(toks) != 1:
        return None
    t = toks[0]
    for suf in ("usdc", "$"):
        if t.endswith(suf):
            t = t[: -len(suf)]
            break
    t = t.removeprefix("$")
    m = _AMOUNT_RE.match(t)
    if not m:
        return None
    d = Decimal(m.group(1) + ("." + m.group(2) if m.group(2) else ""))
    return d if d > 0 else None


def _profile_entry(raw: str, args: list[str], raw_args: list[str] | None) -> Command | None:
    """None — не синтаксис связки (остаётся ответ старого разбора)."""
    if raw_args is None or len(args) < 4:
        return None
    rest = args[1:]
    policy, used = _sol_policy(rest[0]), 1
    if policy is None and len(rest) >= 2 and rest[0] in SOL_POLICIES and rest[1] in SOL_WORDS:   # «jupiter sol»
        policy, used = SOL_POLICIES[rest[0]], 2
    if policy is None:
        return None
    rest = rest[used:]
    pv = _perp_dex(rest[0]) if rest else None
    if policy == "okx" and not (pv and pv[1]):       # okx·sol + перп без dex — старая форма, её ответ
        return None
    if not rest:
        return Unknown(raw, "формат: " + PROFILE_ENTRY_FMT)
    if pv is None:
        return Unknown(raw, f"перп «{rest[0][:20]}» не понят — нужно hyperliquid·<dex>, например hyperliquid·para")
    venue, dex = pv
    profile = PROFILE_ROUTES.get(("solana", venue))
    if profile is None:
        return Unknown(raw, f"связки sol → {venue} нет (есть: sol → hyperliquid·<dex>)")
    m = _RAW_COIN_RE.match(raw_args[0])
    if m is None:
        return Unknown(raw, f"монета «{raw_args[0][:20]}» — латиница и цифры (можно dex:МОНЕТА)")
    cdex, coin = (m.group(1) or "").lower() or None, m.group(2)
    if cdex and dex and cdex != dex:
        return Unknown(raw, f"dex монеты «{cdex}» ≠ dex перпа «{dex}»")
    amt = rest[1:]
    usdc = _usdc_of(amt)
    if usdc is None:
        return Unknown(raw, f"сумма «{' '.join(amt)[:20]}» не понята — число USDC на вход, например 200")
    return ProfileEntry(coin, policy, "solana", venue, dex or cdex, usdc, profile)


def _profile_exit(raw: str, args: list[str], raw_args: list[str] | None) -> Command | None:
    """None — не синтаксис связки. Своя форма — только с именем связки или с количеством в токенах."""
    if raw_args is None or not args:
        return None
    m = _RAW_COIN_RE.match(raw_args[0])
    rest = args[1:]
    profile = dex = None
    if rest and (pw := _profile_word(rest[0])):
        (profile, dex), rest = pw, rest[1:]
    coin = m.group(2).lower() if m else None
    tokens = usdc = None
    if not rest or (len(rest) == 1 and rest[0] in _ALL_WORDS):
        pass
    elif len(rest) == 2 and (rest[1] in _TOKEN_WORDS or rest[1] == coin) and (tm := _TOKENS_RE.match(rest[0])):
        tokens = Decimal(tm.group(1) + ("." + tm.group(2) if tm.group(2) else ""))
        if tokens <= 0:
            return Unknown(raw, "количество токенов — больше нуля")
    elif profile is not None and (usdc := _usdc_of(rest)) is not None:
        pass
    else:
        return Unknown(raw, "формат: " + PROFILE_EXIT_FMT) if profile is not None else None
    if profile is None and tokens is None:
        return None
    if m is None:
        return Unknown(raw, f"цель «{raw_args[0][:20]}» не понята — id сделки или монета")
    cdex = (m.group(1) or "").lower() or None
    if cdex and dex and cdex != dex:
        return Unknown(raw, f"dex монеты «{cdex}» ≠ dex связки «{dex}»")
    return ProfileExit(raw_args[0].replace("$", ""), profile, dex or cdex, tokens, usdc)


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
