"""Строгий загрузчик runtime/owner.toml — параметров владельца фазы 2 (trade_spec §3).

Правило: пусто = запрещено. Ни одно денежное значение не придумывается кодом: чего владелец не задал, того нет,
и live-команда отказывает, называя недостающий ключ; dry-run всё равно строит план и перечисляет, что
заблокировало бы live.

Почему строго:
- неизвестный ключ или секция — ошибка, а не «проигнорировать»: опечатка `slipage_pct` иначе оставила бы
  настоящий ключ пустым молча, и владелец думал бы, что его 3 % действуют;
- число только числом TOML (без кавычек), дробные разбираются сразу в Decimal (parse_float) — float в
  деньгах не бывает; bool — только true/false (в Python bool — подкласс int, 1 не должно стать «да»);
- адрес со смешанным регистром проверяется по контрольной сумме EIP-55 (опечатка в одной букве кошелька).

Файл перечитывается на КАЖДУЮ команду (load() без кэша), а сделка хранит замороженную JSON-копию
(frozen_json) — отчёт и сверка потом видят именно те лимиты, с которыми сделка шла.

TOML не допускает «ключ =» без значения, поэтому пусто пишется как "" (или ключ отсутствует).
"""
from __future__ import annotations
import hashlib, json, re, time, tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from .. import config

MODES = ("dry", "readonly", "live")


class OwnerConfigError(ValueError):
    """owner.toml не читается или значение не того вида. Команда отклоняется (и в dry тоже: план по битому
    файлу показал бы не те лимиты)."""


class OwnerMissing(Exception):
    """Нужный ключ пуст. key — первый из недостающих, keys — все (для одной строки отказа)."""

    def __init__(self, *keys: str):
        self.keys = tuple(keys)
        self.key = self.keys[0] if self.keys else ""
        super().__init__(*self.keys)

    def __str__(self) -> str:
        return "в owner.toml не задано: " + ", ".join(self.keys)


@dataclass(frozen=True)
class _Spec:
    kind: str                         # num | int | enum | bool | addr
    lo: Decimal | None = None
    lo_open: bool = False             # True: строго больше lo
    hi: Decimal | None = None
    words: tuple[str, ...] = ()       # enum — допустимые значения; num — допустимые слова вместо числа


_POS = dict(lo=Decimal(0), lo_open=True)          # > 0
_NONNEG = dict(lo=Decimal(0))                     # ≥ 0
_PCT = dict(lo=Decimal(0), lo_open=True, hi=Decimal(100))

_TOP = {"mode": _Spec("enum", words=MODES)}
_SECTIONS: dict[str, dict[str, _Spec]] = {
    "telegram": {"owner_id": _Spec("int", lo=Decimal(1))},
    "wallets": {"bsc": _Spec("addr"), "aster_user": _Spec("addr"), "aster_signer": _Spec("addr")},
    "limits": {
        "deal_max_usd_per_leg": _Spec("num", **_POS),
        "max_open_deals": _Spec("int", **_NONNEG),
        # "off" — явное решение владельца «дневного стопа нет» (12.09); пусто — ещё не решено = запрет live
        "daily_loss_stop_usd": _Spec("num", **_POS, words=("off",)),
        "daily_loss_basis": _Spec("enum", words=("realized_costs", "mtm")),
        "min_entry_funding_pct_h": _Spec("num"),        # знак любой: порог, а не размер
        "min_entry_basis_bps": _Spec("num"),
    },
    "dex": {
        "slippage_pct": _Spec("num", **_PCT),
        "clip_slippage_pct": _Spec("num", **_PCT),
        "impact_cap_pct": _Spec("num", **_PCT),
        "approve_policy": _Spec("enum", words=("exact", "unlimited")),
        "broadcast": _Spec("enum", words=("public", "okx_mev")),
        "allow_tax_tokens": _Spec("bool"),
        "native_reserve": _Spec("num", **_NONNEG),
    },
    # "auto" (владелец 12.09) — число подбирает бот по правилу из planner.py и показывает его в плане; для live
    # «auto» считается заданным. clip_max_usd — размер клипа выбирает оптимизатор; clips_max — без потолка n;
    # unhedged_usd_max — plan_cost_drift_pct % клипа; exec_time_max_s — 3 × ожидаемая длительность плана + 60 с
    "exec": {
        "clip_max_usd": _Spec("num", **_POS, words=("auto",)),
        "clips_max": _Spec("int", lo=Decimal(1), words=("auto",)),
        "unhedged_usd_max": _Spec("num", **_POS, words=("auto",)),
        "exec_time_max_s": _Spec("num", **_POS, words=("auto",)),
        "refill_wait_max_s": _Spec("num", **_NONNEG),
        "plan_cost_drift_pct": _Spec("num", **_NONNEG),
        "auto_unwind_naked_after_s": _Spec("num", **_POS),
    },
}
# [perp.<площадка>] — одна схема на площадку; площадки — только известные коллектору.
# "auto" (владелец 12.09): α/β подбираются под каждый токен по стакану на момент плана и замораживаются в нём;
# liq_alert_pct — тревога при расстоянии до ликвидации меньше половины расстояния на входе
_PERP: dict[str, _Spec] = {
    "leverage": _Spec("int", lo=Decimal(1)),
    "margin_type": _Spec("enum", words=("ISOLATED", "CROSSED")),
    "max_slip_bps": _Spec("num", **_POS, words=("auto",)),
    "touch_frac_max": _Spec("num", lo=Decimal(0), lo_open=True, hi=Decimal(1), words=("auto",)),
    "maker_allowed": _Spec("bool"),
    "liq_alert_pct": _Spec("num", **_PCT, words=("auto",)),
    # ревью 13.09, С1: контракты с множителем (1000BONKUSDT: 1 контракт = 1000 BONK). Пусто или false — отказ во входе,
    # добор и дохедж продажей; выход и откат таких сделок — всегда. Разрешает только владелец
    "allow_contract_multiplier": _Spec("bool"),
}
PERP_VENUES = tuple(config.PERP_VENUES)


def _schema() -> dict[str, _Spec]:
    out = dict(_TOP)
    for sec, keys in _SECTIONS.items():
        out.update({f"{sec}.{k}": s for k, s in keys.items()})
    for v in PERP_VENUES:
        out.update({f"perp.{v}.{k}": s for k, s in _PERP.items()})
    return out


SCHEMA: Mapping[str, _Spec] = MappingProxyType(_schema())

# Ключи, у которых пусто — осмысленное значение, а не запрет (live их не требует):
#   clip_slippage_pct пусто → шлётся slippage_pct; maker_allowed пусто → только тейкер IOC;
#   auto_unwind_naked_after_s пусто → никогда; пороги входа по фандингу/базису — справка в плане
#   (владелец 12.09: входы только по его команде); daily_loss_basis нужен, только если стоп задан числом;
#   allow_contract_multiplier пусто → контракты с множителем запрещены (как false).
OPTIONAL = frozenset({"mode", "dex.clip_slippage_pct", "exec.auto_unwind_naked_after_s",
                      "limits.min_entry_funding_pct_h", "limits.min_entry_basis_bps", "limits.daily_loss_basis",
                      *(f"perp.{v}.maker_allowed" for v in PERP_VENUES),
                      *(f"perp.{v}.allow_contract_multiplier" for v in PERP_VENUES)})
# Кошельки площадки перпа, без которых live не начинается (Aster v3: user = основной, signer = агент)
_VENUE_WALLETS = {"aster": ("wallets.aster_user", "wallets.aster_signer")}
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _checksum_ok(addr: str) -> bool:
    body = addr[2:]
    if body == body.lower() or body == body.upper():
        return True                               # без смешанного регистра контрольной суммы нет
    try:
        from eth_utils import is_checksum_address  # идёт с eth-account (extra trade)
    except ImportError:                           # без extra: сравнения адресов всё равно в нижнем регистре
        return True
    return bool(is_checksum_address(addr))


def _num_text(spec: _Spec) -> str:
    rng = []
    if spec.lo is not None:
        rng.append(("> " if spec.lo_open else "≥ ") + format(spec.lo, "f"))
    if spec.hi is not None:
        rng.append("≤ " + format(spec.hi, "f"))
    words = f" или {' / '.join(repr(w) for w in spec.words)}" if spec.words else ""
    return ("целое" if spec.kind == "int" else "число") + (" " + ", ".join(rng) if rng else "") + words


def _check(key: str, spec: _Spec, v: Any) -> Any:
    """Проверенное значение или None (пусто). Ошибка вида — OwnerConfigError с именем ключа."""
    if isinstance(v, str) and v.strip() == "":
        return None
    if spec.kind == "enum":
        if isinstance(v, str) and v in spec.words:
            return v
        raise OwnerConfigError(f"{key} = {v!r}: допустимо {' | '.join(spec.words)} или \"\"")
    if spec.kind == "bool":
        if isinstance(v, bool):
            return v
        raise OwnerConfigError(f"{key} = {v!r}: нужно true или false (без кавычек) или \"\"")
    if spec.kind == "addr":
        if isinstance(v, str) and _ADDR_RE.match(v.strip()):
            a = v.strip()
            if not _checksum_ok(a):
                raise OwnerConfigError(f"{key} = {a}: неверная контрольная сумма EIP-55 (опечатка в адресе?)")
            return a
        raise OwnerConfigError(f"{key} = {v!r}: нужен адрес 0x + 40 hex-символов")
    # num / int
    if isinstance(v, str):
        if v.strip() in spec.words:
            return v.strip()
        raise OwnerConfigError(f"{key} = {v!r}: нужно {_num_text(spec)} (число — без кавычек)")
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        raise OwnerConfigError(f"{key} = {v!r}: нужно {_num_text(spec)}")
    if spec.kind == "int" and not isinstance(v, int):
        raise OwnerConfigError(f"{key} = {v}: нужно {_num_text(spec)}")
    d = Decimal(v)
    if not d.is_finite():
        raise OwnerConfigError(f"{key} = {v}: нужно конечное число")
    if spec.lo is not None and (d <= spec.lo if spec.lo_open else d < spec.lo):
        raise OwnerConfigError(f"{key} = {v}: нужно {_num_text(spec)}")
    if spec.hi is not None and d > spec.hi:
        raise OwnerConfigError(f"{key} = {v}: нужно {_num_text(spec)}")
    return v if spec.kind == "int" else d


def _flatten(doc: dict) -> tuple[dict[str, Any], list[str]]:
    """Документ TOML → {ключ.через.точку: значение}; неизвестное — в список ошибок."""
    flat: dict[str, Any] = {}
    errs: list[str] = []
    for top, val in doc.items():
        if top in _TOP:
            flat[top] = val
        elif top in _SECTIONS:
            if not isinstance(val, dict):
                errs.append(f"[{top}] должно быть секцией")
                continue
            for k, v in val.items():
                if k in _SECTIONS[top]:
                    flat[f"{top}.{k}"] = v
                else:
                    errs.append(f"неизвестный ключ {top}.{k}")
        elif top == "perp":
            if not isinstance(val, dict):
                errs.append("[perp] должно быть секцией")
                continue
            for venue, sec in val.items():
                if venue not in PERP_VENUES or not isinstance(sec, dict):
                    errs.append(f"неизвестная площадка [perp.{venue}] (знаю: {', '.join(PERP_VENUES)})")
                    continue
                for k, v in sec.items():
                    if k in _PERP:
                        flat[f"perp.{venue}.{k}"] = v
                    else:
                        errs.append(f"неизвестный ключ perp.{venue}.{k}")
        else:
            errs.append(f"неизвестный ключ или секция: {top}")
    return flat, errs


def _validate(flat: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {k: None for k in SCHEMA}
    errs: list[str] = []
    for k, v in flat.items():
        try:
            values[k] = _check(k, SCHEMA[k], v)
        except OwnerConfigError as e:
            errs.append(str(e))
    # сквозные проверки: потолок проскальзывания владельца — жёсткий максимум; мастер ≠ агент
    clip, cap = values["dex.clip_slippage_pct"], values["dex.slippage_pct"]
    if isinstance(clip, Decimal) and isinstance(cap, Decimal) and clip > cap:
        errs.append(f"dex.clip_slippage_pct = {clip} больше dex.slippage_pct = {cap} (потолок владельца)")
    u, s = values["wallets.aster_user"], values["wallets.aster_signer"]
    if u and s and u.lower() == s.lower():
        errs.append("wallets.aster_signer совпадает с aster_user: подписант должен быть отдельным API-кошельком "
                    "(агентом), иначе на сервере лежал бы мастер-ключ")
    return values, errs


def _jsonable(v: Any) -> Any:
    return format(v, "f") if isinstance(v, Decimal) else v


@dataclass(frozen=True)
class OwnerCfg:
    """Снимок owner.toml на момент команды. values — ВСЕ известные ключи (None = пусто)."""
    values: Mapping[str, Any]
    path: str
    sha256: str | None                # None — файла нет (всё пусто, режим dry)
    loaded: float

    # --- доступ ---
    def _known(self, key: str) -> None:
        if key not in self.values:     # опечатка в коде — ошибка программиста, а не «пусто»
            raise KeyError(f"нет такого ключа owner.toml: {key}")

    def get(self, key: str, default: Any = None) -> Any:
        self._known(key)
        v = self.values[key]
        return default if v is None else v

    def is_set(self, key: str) -> bool:
        self._known(key)
        return self.values[key] is not None

    def missing(self, *keys: str) -> list[str]:
        return [k for k in keys if not self.is_set(k)]

    def require(self, *keys: str) -> tuple:
        """Значения ключей по порядку; хоть один пуст — OwnerMissing со всеми пустыми."""
        miss = self.missing(*keys)
        if miss:
            raise OwnerMissing(*miss)
        return tuple(self.values[k] for k in keys)

    @property
    def mode(self) -> str:
        return self.values.get("mode") or "dry"

    @property
    def owner_id(self) -> int | None:
        return self.values.get("telegram.owner_id")

    # --- что нужно для live ---
    def live_required(self, perp_venue: str = "aster", chain: str = "bsc") -> tuple[str, ...]:
        """Ключи, без которых live-вход/выход отказывает. dry-run показывает live_missing() в плане."""
        if perp_venue not in PERP_VENUES:
            raise KeyError(f"неизвестная площадка перпа: {perp_venue}")
        wallet = f"wallets.{chain}"
        self._known(wallet)
        keys = ["telegram.owner_id", wallet, *_VENUE_WALLETS.get(perp_venue, ()),
                "limits.deal_max_usd_per_leg", "limits.max_open_deals", "limits.daily_loss_stop_usd",
                "dex.slippage_pct", "dex.impact_cap_pct", "dex.approve_policy", "dex.broadcast",
                "dex.allow_tax_tokens", "dex.native_reserve",
                *(f"perp.{perp_venue}.{k}" for k in _PERP if f"perp.{perp_venue}.{k}" not in OPTIONAL),
                "exec.clip_max_usd", "exec.clips_max", "exec.unhedged_usd_max", "exec.exec_time_max_s",
                "exec.refill_wait_max_s", "exec.plan_cost_drift_pct"]
        if isinstance(self.values.get("limits.daily_loss_stop_usd"), Decimal):
            keys.append("limits.daily_loss_basis")     # стоп задан числом — нужно и его определение
        return tuple(keys)

    def unresolved_auto(self) -> list[str]:
        """«auto», которое нечем разрешить, — для live это то же, что пусто. unhedged_usd_max = "auto" считается как
        plan_cost_drift_pct % клипа: без допуска владельца числа нет (в dry план строится без этого предела)."""
        out = []
        if self.values.get("exec.unhedged_usd_max") == "auto" and self.values.get("exec.plan_cost_drift_pct") is None:
            out.append("exec.unhedged_usd_max")
        return out

    def live_missing(self, perp_venue: str = "aster", chain: str = "bsc") -> list[str]:
        """Пустые ключи из live_required плюс неразрешимые «auto»; порядок — как в live_required."""
        req = self.live_required(perp_venue, chain)
        bad = set(self.missing(*req)) | set(self.unresolved_auto())
        return [k for k in req if k in bad]

    def require_live(self, perp_venue: str = "aster", chain: str = "bsc") -> None:
        """Отказ live, если чего-то не хватает (пусто или неразрешимое «auto»): OwnerMissing со всеми ключами."""
        miss = self.live_missing(perp_venue, chain)
        if miss:
            raise OwnerMissing(*miss)

    # --- замороженная копия для сделки ---
    def frozen(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "loaded": self.loaded,
                "values": {k: _jsonable(v) for k, v in self.values.items()}}

    def frozen_json(self) -> str:
        return json.dumps(self.frozen(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_frozen(cls, s: str) -> "OwnerCfg":
        """Обратно из deals.owner_json: числа снова Decimal, проверки те же (битая копия — ошибка, а не пусто)."""
        d = json.loads(s)
        flat = {}
        for k, v in (d.get("values") or {}).items():
            if k not in SCHEMA:
                raise OwnerConfigError(f"в копии неизвестный ключ {k}")
            if v is None:
                continue
            spec = SCHEMA[k]
            if spec.kind == "num" and isinstance(v, str) and v not in spec.words:
                try:
                    v = Decimal(v)
                except InvalidOperation:
                    raise OwnerConfigError(f"{k} = {v!r}: в копии не число") from None
            flat[k] = v
        values, errs = _validate(flat)
        if errs:
            raise OwnerConfigError("копия owner.toml не проходит проверку: " + "; ".join(errs))
        return cls(values=MappingProxyType(values), path=d.get("path") or "", sha256=d.get("sha256"),
                   loaded=float(d.get("loaded") or 0))


def load(path: Path | str | None = None) -> OwnerCfg:
    """Свежее чтение файла — без кэша (правка владельца действует со следующей команды). Нет файла — всё пусто:
    режим dry, live запрещён."""
    p = Path(path) if path else config.OWNER_PATH
    now = time.time()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return OwnerCfg(values=MappingProxyType({k: None for k in SCHEMA}), path=str(p), sha256=None, loaded=now)
    try:
        doc = tomllib.loads(raw.decode("utf-8"), parse_float=Decimal)
    except UnicodeDecodeError:
        raise OwnerConfigError(f"{p}: не UTF-8") from None
    except tomllib.TOMLDecodeError as e:
        raise OwnerConfigError(f"{p}: ошибка TOML — {e} (пустое значение пишется как \"\")") from None
    flat, errs = _flatten(doc)
    values, verrs = _validate(flat)
    errs += verrs
    if errs:
        raise OwnerConfigError(f"{p}: " + "; ".join(errs))
    return OwnerCfg(values=MappingProxyType(values), path=str(p), sha256=hashlib.sha256(raw).hexdigest(), loaded=now)
