"""ЕДИНСТВЕННОЕ место, где читаются приватные ключи DEX_EVM_KEY и ASTER_SIGNER_KEY (trade_spec §3, §8).

Режимы (owner.toml `mode`, пусто = dry):
  dry      — ключи НЕ читаются вовсе: load() отказывает до первого обращения к окружению. Только публичные данные.
  readonly — EVM-ключ читается лишь для сверки адреса и сразу выбрасывается; ключ агента Aster хранится
             для подписанных GET (баланс, позиция, комиссия, плечо).
  live     — оба ключа; отправка — только после кнопки владельца и через gate("send").
Повысить режим можно только перезапуском (ключи загружаются на старте); понизить — правкой owner.toml,
gate() берёт меньший из режима загрузки и текущего режима файла.

После чтения ключи удаляются из os.environ: дочерние процессы (и случайный дамп окружения) их не видят.
Обёртка SignerKey печатается как <key>, не копируется и не сериализуется. redact() маскирует в логах
токен Telegram (`bot<id>:<secret>`), загруженные ключи и любые 64-hex строки, кроме помеченных публичными
(хэши транзакций — mark_public), — формат приватного ключа и хэша одинаков, поэтому по умолчанию прячется всё.

Сверки (отказ с понятной строкой, значения ключей не печатаются никогда):
  адрес из DEX_EVM_KEY == owner.toml [wallets].bsc (и == DEX_EVM_ADDRESS, если он задан);
  адрес из ASTER_SIGNER_KEY == ASTER_SIGNER_ADDRESS == [wallets].aster_signer;
  адрес из ASTER_SIGNER_KEY != aster_user (иначе на сервере мастер-ключ основного аккаунта Aster — Q3).
"""
from __future__ import annotations
import logging, os, re, threading
from collections import OrderedDict
from dataclasses import dataclass
from .owner import MODES, OwnerCfg, OwnerMissing

EVM_KEY_ENV = "DEX_EVM_KEY"
ASTER_KEY_ENV = "ASTER_SIGNER_KEY"
_RANK = {m: i for i, m in enumerate(MODES)}
# действие → минимальный режим. send — всё, что меняет состояние у площадки или в сети.
ACTIONS = {"public": "dry", "signed_read": "readonly", "send": "live"}


class KeysError(RuntimeError):
    """Отказ загрузки ключей. В тексте никогда нет значения ключа — только имя переменной и адреса."""


class KeysForbidden(KeysError):
    """Режим не позволяет ключи (dry)."""


class KeyMismatch(KeysError):
    """Адрес из ключа не тот, что задан владельцем, или подписант Aster — мастер-кошелёк."""


class ModeForbidden(KeysError):
    """Ворота режима: действие не разрешено текущим режимом или паузой."""


def effective_mode(owner_mode: str | None, requested: str | None = None) -> str:
    """Режим файла владельца, понижаемый запросом (CLI `trade-check` может попросить readonly при live в файле,
    но не наоборот)."""
    m = owner_mode or "dry"
    if m not in _RANK:
        raise KeysError(f"неизвестный режим: {m}")
    if requested is None:
        return m
    if requested not in _RANK:
        raise KeysError(f"неизвестный режим: {requested}")
    return requested if _RANK[requested] < _RANK[m] else m


def mode_allows(mode: str | None, action: str, paused: bool = False, hedge: bool = False) -> bool:
    try:
        gate(mode, action, paused=paused, hedge=hedge)
        return True
    except ModeForbidden:
        return False


def gate(mode: str | None, action: str, paused: bool = False, hedge: bool = False) -> None:
    """Ворота на нижнем уровне (AsterTrade.call, EvmWallet.send_and_wait). hedge=True — хедж уже исполненной ноги
    DEX: он уменьшает риск и по решению «стоп» (Q7) ставится и на паузе; всё прочее на паузе не отправляется."""
    if action not in ACTIONS:
        raise ValueError(f"неизвестное действие: {action}")
    m = mode or "dry"
    if m not in _RANK:
        raise ModeForbidden(f"неизвестный режим {m}: {action} запрещено")
    need = ACTIONS[action]
    if _RANK[m] < _RANK[need]:
        raise ModeForbidden(f"режим {m}: {action} запрещено (нужен {need})")
    if action == "send" and paused and not hedge:
        raise ModeForbidden("пауза («стоп»): новые отправки запрещены")


# --- обёртка ключа -----------------------------------------------------------------------------
class SignerKey:
    """Подписант вместо LocalAccount: address + sign_message/sign_transaction/sign_typed_data. Самого ключа
    наружу не отдаёт (атрибута key нет), печатается как <key>, не копируется и не сериализуется (pickle,
    copy, dataclasses.asdict — TypeError), чтобы ключ не уехал в лог, БД или отчёт вместе со структурой."""
    __slots__ = ("_acct", "address", "role")

    def __init__(self, acct, role: str):
        self._acct = acct
        self.address: str = acct.address
        self.role = role

    def sign_message(self, signable):
        return self._acct.sign_message(signable)

    def sign_transaction(self, tx: dict):
        return self._acct.sign_transaction(tx)

    def sign_typed_data(self, *a, **kw):
        return self._acct.sign_typed_data(*a, **kw)

    def __repr__(self) -> str:
        return "<key>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<key>"

    def __reduce__(self):
        raise TypeError("ключ не сериализуется")

    def __reduce_ex__(self, protocol):
        raise TypeError("ключ не сериализуется")

    def __copy__(self):
        raise TypeError("ключ не копируется")

    def __deepcopy__(self, memo):
        raise TypeError("ключ не копируется")


@dataclass(frozen=True, repr=False, eq=False)
class Keys:
    """Загруженные и сверенные ключи. evm — только в live (в readonly адрес сверен, ключ выброшен)."""
    mode: str
    evm_address: str
    aster_user: str
    aster_signer: str
    aster: SignerKey
    evm: SignerKey | None = None

    def gate(self, owner_mode: str | None, action: str, paused: bool = False, hedge: bool = False) -> None:
        """Ворота по МЕНЬШЕМУ из режима загрузки и текущего режима owner.toml (файл перечитывается на команду)."""
        gate(effective_mode(owner_mode, self.mode), action, paused=paused, hedge=hedge)

    def __repr__(self) -> str:
        return (f"Keys(mode={self.mode}, evm={'<key>' if self.evm else None}, aster=<key>, "
                f"evm_address={self.evm_address}, aster_user={self.aster_user}, aster_signer={self.aster_signer})")


# --- загрузка ----------------------------------------------------------------------------------
_KEY_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _account_cls():
    try:
        from eth_account import Account
    except ImportError:
        raise KeysError("eth-account не установлен: pip install -e '.[trade]' (eth-account==0.13.7)") from None
    return Account


def _from_key(account_cls, raw: str | None, name: str):
    if raw is None or not raw.strip():
        raise KeysError(f"нет {name} в окружении (.env)")
    m = _KEY_RE.match(raw.strip())
    if not m:
        raise KeysError(f"{name}: не 64 hex-символа (длина {len(raw.strip())})")
    h = m.group(1).lower()
    _remember_secret(h)                     # маскируется в логах с этого момента, даже если дальше отказ
    # диапазон скаляра проверяем сами: eth_account 0.13.7 + eth-keys 0.7.0 молча принимают нулевой ключ и выводят
    # из него адрес (проверено 12.09) — отказ тогда случился бы лишь на сверке адреса, а не на самом ключе
    if not 0 < int(h, 16) < _SECP256K1_N:
        raise KeysError(f"{name}: недопустимый приватный ключ (вне диапазона secp256k1)")
    try:
        return account_cls.from_key(bytes.fromhex(h))
    except Exception:                       # текст исключения библиотеки не пробрасываем: мало ли что в нём
        raise KeysError(f"{name}: недопустимый приватный ключ") from None


def _lc(a: str | None) -> str:
    return (a or "").strip().lower()


def load(cfg: OwnerCfg, mode: str | None = None, environ=None) -> Keys:
    """Загрузить и сверить ключи. dry — KeysForbidden ДО любого обращения к окружению."""
    m = effective_mode(cfg.mode, mode)
    if m == "dry":
        raise KeysForbidden("режим dry: ключи не загружаются (нужен mode = \"readonly\" или \"live\" в owner.toml)")
    env = os.environ if environ is None else environ
    # забрать и сразу стереть из окружения — дальше ключ живёт только в обёртке; стирается и при отказе ниже
    raw_evm = env.pop(EVM_KEY_ENV, None)
    raw_aster = env.pop(ASTER_KEY_ENV, None)
    try:
        wallet, user, signer = cfg.require("wallets.bsc", "wallets.aster_user", "wallets.aster_signer")
    except OwnerMissing as e:               # ошибка настройки, как и ключи: трейдер — код 78 и сообщение владельцу
        raise KeysError(str(e)) from None
    account_cls = _account_cls()
    evm_acct = _from_key(account_cls, raw_evm, EVM_KEY_ENV)
    aster_acct = _from_key(account_cls, raw_aster, ASTER_KEY_ENV)
    del raw_evm, raw_aster
    evm_addr, aster_addr = evm_acct.address, aster_acct.address

    # мастер-ключ основного аккаунта Aster на сервере — первым: это самый опасный случай (Q3)
    user_env = (env.get("ASTER_USER") or "").strip()
    if _lc(aster_addr) in {_lc(user), _lc(user_env)} - {""}:
        raise KeyMismatch(f"{ASTER_KEY_ENV} — ключ основного кошелька Aster ({aster_addr}): на сервере был бы "
                          "мастер-ключ. Нужен отдельный API-кошелёк (агент) без права вывода — не запускаюсь")
    if _lc(evm_addr) != _lc(wallet):
        raise KeyMismatch(f"{EVM_KEY_ENV} даёт адрес {evm_addr}, а в owner.toml [wallets] bsc = {wallet} — не запускаюсь")
    evm_env = (env.get("DEX_EVM_ADDRESS") or "").strip()
    if evm_env and _lc(evm_env) != _lc(evm_addr):
        raise KeyMismatch(f"{EVM_KEY_ENV} даёт адрес {evm_addr}, а DEX_EVM_ADDRESS = {evm_env} — не запускаюсь")
    signer_env = (env.get("ASTER_SIGNER_ADDRESS") or "").strip()
    if not signer_env:
        raise KeysError("нет ASTER_SIGNER_ADDRESS в окружении (.env) — не с чем сверить ключ агента Aster")
    if _lc(aster_addr) != _lc(signer_env):
        raise KeyMismatch(f"{ASTER_KEY_ENV} даёт адрес {aster_addr}, а ASTER_SIGNER_ADDRESS = {signer_env} — не запускаюсь")
    if _lc(aster_addr) != _lc(signer):
        raise KeyMismatch(f"{ASTER_KEY_ENV} даёт адрес {aster_addr}, а в owner.toml [wallets] aster_signer = {signer} "
                          "— не запускаюсь")
    if user_env and _lc(user_env) != _lc(user):
        raise KeyMismatch(f"ASTER_USER = {user_env}, а в owner.toml [wallets] aster_user = {user} — не запускаюсь")

    evm = SignerKey(evm_acct, "evm") if m == "live" else None
    del evm_acct
    return Keys(mode=m, evm_address=evm_addr, aster_user=user, aster_signer=aster_addr,
                aster=SignerKey(aster_acct, "aster"), evm=evm)


# --- маскировка -----------------------------------------------------------------------------------
_TG_RE = re.compile(r"bot\d+:[\w-]+")
_HEX64_RE = re.compile(r"(?<![0-9A-Za-z])(?:0x)?([0-9a-fA-F]{64})(?![0-9A-Za-z])")
_SECRETS: set[str] = set()                      # загруженные ключи (hex, нижний регистр)
_PUBLIC: "OrderedDict[str, None]" = OrderedDict()   # хэши транзакций и прочие публичные 64-hex
_PUBLIC_MAX = 4096
_lock = threading.Lock()


def _remember_secret(h: str) -> None:
    with _lock:
        _SECRETS.add(h.lower())
        _PUBLIC.pop(h.lower(), None)             # секрет никогда не бывает «публичным»


def mark_public(*values: str) -> None:
    """Пометить 64-hex как публичные (хэш подписанной транзакции — он и так уйдёт в сеть): redact() их не прячет."""
    with _lock:
        for v in values:
            h = str(v or "").strip().lower().removeprefix("0x")
            if len(h) == 64 and h not in _SECRETS and all(c in "0123456789abcdef" for c in h):
                _PUBLIC[h] = None
                _PUBLIC.move_to_end(h)
                while len(_PUBLIC) > _PUBLIC_MAX:
                    _PUBLIC.popitem(last=False)


def redact_secrets(text) -> str:
    """Для БД и сообщений: загруженные ключи и токен Telegram. Хэши транзакций остаются (они нужны в учёте)."""
    s = str(text)
    with _lock:
        secrets = tuple(_SECRETS)
    for sec in secrets:
        s = re.sub(re.escape(sec), "<key>", s, flags=re.IGNORECASE)
    return _TG_RE.sub("bot<redacted>", s)


def redact(text) -> str:
    """Для логов: redact_secrets + любая 64-hex строка, кроме помеченных mark_public."""
    s = redact_secrets(text)
    with _lock:
        public = set(_PUBLIC)
    return _HEX64_RE.sub(lambda mm: mm.group(0) if mm.group(1).lower() in public else "<hex64>", s)


class RedactFormatter(logging.Formatter):
    """Оборачивает форматтер обработчика: маскируется уже готовая строка, вместе с трассировкой исключения
    (в тексте исключения requests лежит полный URL с /bot<token>/)."""

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__()
        self.inner = inner or logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        return redact(self.inner.format(record))


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Все обработчики логгера (по умолчанию корневого) — через RedactFormatter. Повторный вызов безвреден."""
    lg = logger or logging.getLogger()
    for h in lg.handlers:
        if not isinstance(h.formatter, RedactFormatter):
            h.setFormatter(RedactFormatter(h.formatter))


def _reset_redaction_for_tests() -> None:
    with _lock:
        _SECRETS.clear()
        _PUBLIC.clear()
