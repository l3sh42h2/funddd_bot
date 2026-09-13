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

Связка Solana × Hyperliquid (ТЗ SOL×HL 13.09) — load_sol_hl(), своя готовность profile_readiness():
  ключей Aster/EVM не требует и не трогает, старая связка — ключей Solana/HL (M01/M02);
  ключ Solana — SOLANA_SECRET_B58 (base58 64 байта: секрет 32 + открытый ключ 32, формат владельца) ИЛИ файл
  SOLANA_KEYPAIR_FILE (JSON solana-keygen из 64 чисел, права 0600); оба сразу — отказ (формат не угадываем);
  разбор и сверка половин — trade/solana/keypair.py (один разборщик на проект); здесь — адрес владельца
  ([wallets.sol_hl] solana_address, регистр как есть), роль, маскировка. SolanaKey подписывает сырые байты
  (Ed25519 pycryptodome — ставится с eth-account); транзакцию подписывает только sign.sign_validated по сообщению,
  закреплённому валидатором (validate.py ждёт solders: пока его нет, подписывать нечего);
  ключ агента HL (HL_AGENT_PRIVATE_KEY, secp256k1) → адрес == hl_agent_address и ≠ мастеру/счёту;
  один и тот же секрет в двух ролях (Solana, агент HL, EVM, Aster) — отказ;
  readonly приватные ключи не разбирает (переменные только стираются), берёт ключи API и URL RPC;
  redact() дополнительно маскирует base58-секреты (и любые 64-байтные base58 вне mark_public — это форма подписи
  Solana и секрета), JSON-массивы байт, api-key в URL и заголовках, части URL RPC с ключом;
  URL RPC с логином/паролем (user:pass@host) — отказ: они ушли бы в display/host (repr, doctor), а redact их не знает.
"""
from __future__ import annotations
import logging, os, re, stat, threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from .instruments import b58decode, b58encode
from .owner import LEGACY_PROFILE, MODES, OKX_ENV, SOL_HL, SOL_PATHS, OwnerCfg, OwnerMissing

if TYPE_CHECKING:
    from .solana.keypair import SolanaSecret

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
    _remember_secret(h, name)               # маскируется в логах с этого момента, даже если дальше отказ
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


# --- связка Solana × Hyperliquid ------------------------------------------------------------------------
SOL_ROLE = "solana"
_URL_RE = re.compile(r"^(https|wss)://([^\s/?#@]+)([^\s]*)$")
_AUTH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)")     # authority: до первого / ? #
_KEYFILE_MAX = 4096


def _userinfo(u: str) -> str | None:
    """Логин/пароль URL (user:pass@host — authority до последнего «@»); нет — None. «@» в пути или query — не они."""
    m = _AUTH_RE.match(u)
    if not m or "@" not in m.group(1):
        return None
    return m.group(1).rpartition("@")[0]


class ApiSecret:
    """Ключ API провайдера (Jupiter, OKX) или URL RPC с ключом: reveal() — только для заголовка/адреса запроса адаптера;
    печатается именем переменной, не копируется и не сериализуется."""
    __slots__ = ("_v", "name")

    def __init__(self, value: str, name: str):
        self._v = value
        self.name = name

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return f"<{self.name}>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):
        raise TypeError("секрет не сериализуется")

    def __reduce_ex__(self, protocol):
        raise TypeError("секрет не сериализуется")

    def __copy__(self):
        raise TypeError("секрет не копируется")

    def __deepcopy__(self, memo):
        raise TypeError("секрет не копируется")


class SecretUrl(ApiSecret):
    """URL RPC (ключ — в пути или в query). display — схема и хост, для логов и doctor. С логином/паролем загрузчик
    URL не примет; обёртка, созданная в обход него, всё равно их не покажет (host «?», display «<url>»)."""
    __slots__ = ()

    @property
    def host(self) -> str:
        m = _URL_RE.match(self._v)
        return m.group(2) if m and _userinfo(self._v) is None else "?"

    @property
    def display(self) -> str:
        m = _URL_RE.match(self._v)
        return f"{m.group(1)}://{m.group(2)}/…" if m and _userinfo(self._v) is None else "<url>"

    def __repr__(self) -> str:
        return f"<{self.name} {self.display}>"


@dataclass(frozen=True, repr=False, eq=False)
class OkxCreds:
    key: ApiSecret
    secret: ApiSecret
    passphrase: ApiSecret


@dataclass(frozen=True, repr=False, eq=False)
class SolHlKeys:
    """Ключи связки sol_best_hyperliquid. sol/hl — только в live; в readonly — публичные адреса и ключи API."""
    mode: str
    solana_address: str
    hl_user: str
    hl_account: str
    hl_vault: str | None
    hl_agent_address: str | None
    sol: SolanaKey | None = None
    hl: SignerKey | None = None
    jupiter: ApiSecret | None = None
    okx: OkxCreds | None = None
    rpc_primary: SecretUrl | None = None
    rpc_secondary: SecretUrl | None = None
    rpc_ws: SecretUrl | None = None

    def gate(self, owner_mode: str | None, action: str, paused: bool = False, hedge: bool = False) -> None:
        """owner_mode — текущий cfg.profile_mode(SOL_HL): ворота по меньшему из него и режима загрузки."""
        gate(effective_mode(owner_mode, self.mode), action, paused=paused, hedge=hedge)

    def __repr__(self) -> str:
        rpc = ", ".join(u.display for u in (self.rpc_primary, self.rpc_secondary) if u) or None
        return (f"SolHlKeys(mode={self.mode}, sol={'<key>' if self.sol else None}, hl={'<key>' if self.hl else None}, "
                f"solana={self.solana_address}, hl_account={self.hl_account}, hl_agent={self.hl_agent_address}, "
                f"jupiter={'<set>' if self.jupiter else None}, okx={'<set>' if self.okx else None}, rpc={rpc})")


def _ed25519():
    """Ed25519 из pycryptodome — стоит с eth-account (eth-keyfile); сверен векторами RFC 8032 в тестах."""
    try:
        from Crypto.PublicKey import ECC
        from Crypto.Signature import eddsa
    except ImportError:
        raise KeysError("нет pycryptodome (ставится с eth-account): pip install -e '.[trade]'") from None
    return ECC, eddsa


class SolanaKey:
    """Подписант Solana (протокол sign.SolanaSigner): public_key() и sign_message(bytes) → 64 байта Ed25519 (RFC 8032).
    Подписывает то, что дали: транзакцию — только через sign.sign_validated по закреплённому валидатором сообщению.
    Секрет наружу не отдаётся, печатается как <key>, не копируется и не сериализуется."""
    __slots__ = ("_signer", "address", "role")

    def __init__(self, seed: bytes, role: str = SOL_ROLE):
        if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
            raise KeysError("ключ Solana: нужен seed 32 байта")
        ECC, eddsa = _ed25519()
        key = ECC.construct(curve="Ed25519", seed=bytes(seed))
        self._signer = eddsa.new(key, "rfc8032")
        self.address: str = b58encode(key.public_key().export_key(format="raw"))
        self.role = role

    @classmethod
    def from_secret(cls, sec: "SolanaSecret") -> "SolanaKey":
        """Из секрета, уже сверенного trade/solana/keypair.py (seed не покидает with_seed)."""
        return sec.with_seed(cls)

    def public_key(self) -> str:
        return self.address

    def sign_message(self, message: bytes) -> bytes:
        if not isinstance(message, (bytes, bytearray, memoryview)):
            raise TypeError("подписываются только байты сообщения")
        return self._signer.sign(bytes(message))

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


def _register_solana(sec: "SolanaSecret", name: str) -> None:
    """Все формы загруженного секрета — в маскировку; тот же секрет в другой роли — отказ."""
    full = sec.with_seed(bytes) + b58decode(sec.public_key())
    _remember_bytes(full)
    prior = _role_of(full[:32].hex())
    if prior not in (None, SOL_ROLE):
        raise KeyMismatch(f"{name}: этот же секрет уже загружен как {prior} — одна seed на две роли не допускается")
    _remember_secret(full[:32].hex(), SOL_ROLE)


def _keyfile_checked(path: str, name: str) -> str:
    """Строже trade/solana/keypair.py: абсолютный путь, свой файл, не больше 4 КБ (права 0600 и формат — там)."""
    p = path.strip()
    if not os.path.isabs(p):
        raise KeysError(f"{name}: нужен абсолютный путь к файлу ключа")
    try:
        st = os.stat(p)
    except OSError as e:
        raise KeysError(f"{name}: файл недоступен ({p}: {e.strerror})") from None
    if stat.S_ISREG(st.st_mode):
        if st.st_uid != os.getuid():
            raise KeysError(f"{name}: {p} принадлежит другому пользователю")
        if st.st_size > _KEYFILE_MAX:
            raise KeysError(f"{name}: {p} больше {_KEYFILE_MAX} байт — не файл ключа")
    return p


def _load_solana(raw_b58: str | None, keyfile: str | None, n_b58: str, n_file: str, want: str) -> SolanaKey:
    """Разбор и сверка половин — trade/solana/keypair.py; здесь — один источник, адрес владельца, роль, маскировка."""
    from .solana.keypair import SolanaKeyError, load_keypair_file, parse_secret_b58
    has_b58, has_file = bool(raw_b58 and raw_b58.strip()), bool(keyfile and keyfile.strip())
    if has_b58 and has_file:
        raise KeysError(f"заданы и {n_b58}, и {n_file} — оставьте один источник ключа Solana")
    if not (has_b58 or has_file):
        raise KeysError(f"нет {n_b58} или {n_file} в окружении (.env)")
    try:
        if has_b58:
            _remember_exact(raw_b58.strip())        # маскируется с этого момента, даже если дальше отказ
            sec = parse_secret_b58(raw_b58, n_b58)
        else:
            sec = load_keypair_file(_keyfile_checked(keyfile, n_file), source=n_file)
    except SolanaKeyError as e:                     # текст без значения секрета (так устроен keypair.py)
        raise KeysError(str(e)) from None
    _register_solana(sec, n_b58 if has_b58 else n_file)
    if sec.public_key() != want:                    # base58 сравнивается как есть: регистр — часть адреса
        raise KeyMismatch(f"ключ Solana даёт адрес {sec.public_key()}, а в owner.toml [wallets.sol_hl] solana_address "
                          f"= {want} — не запускаюсь")
    key = SolanaKey.from_secret(sec)
    if key.address != sec.public_key():             # две независимые реализации Ed25519 обязаны сойтись
        raise KeysError("ключ Solana: pycryptodome и разборщик секрета дали разные открытые ключи — не запускаюсь")
    return key


def _load_hl_agent(raw: str | None, name: str, agent: str, user: str, account: str) -> SignerKey:
    m = _KEY_RE.match((raw or "").strip())
    prior = _role_of(m.group(1)) if m else None
    if prior not in (None, name):
        raise KeyMismatch(f"{name}: этот же секрет уже загружен как {prior} — одна seed на две роли не допускается")
    acct = _from_key(_account_cls(), raw, name)
    if _lc(acct.address) in {_lc(user), _lc(account)}:
        raise KeyMismatch(f"{name} — ключ мастера/счёта HL ({acct.address}): на сервере был бы мастер-ключ. Нужен "
                          "отдельный API-кошелёк (агент) — не запускаюсь")
    if _lc(acct.address) != _lc(agent):
        raise KeyMismatch(f"{name} даёт адрес {acct.address}, а в owner.toml [wallets.sol_hl] hl_agent_address = "
                          f"{agent} — не запускаюсь")
    return SignerKey(acct, "hl_agent")


def _secret_url(env, name: str, schemes: tuple[str, ...]) -> SecretUrl | None:
    v = (env.get(name) or "").strip()
    if not v:
        return None
    m = _URL_RE.match(v)
    if not m or m.group(1) not in schemes:
        raise KeysError(f"{name}: нужен URL {' или '.join(s + '://…' for s in schemes)} (значение не показываю)")
    _remember_url(v)
    ui = _userinfo(v)
    if ui is not None:                  # маскировать до отказа: пару и части (токен бывает логином)
        for part in (ui, *ui.split(":")):
            _remember_exact(part)
        raise KeysError(f"{name}: логин/пароль в URL (…@хост) не поддерживаются — ключ RPC передаётся в пути или query "
                        "(значение не показываю)")
    return SecretUrl(v, name)


def _api_creds(cfg: OwnerCfg, env) -> dict:
    out: dict = {}
    jn = cfg.env_name("providers.jupiter.api_key_env")
    jv = (env.get(jn) or "").strip()
    if jv:
        _remember_exact(jv)
        out["jupiter"] = ApiSecret(jv, jn)
    vals = {n: (env.get(n) or "").strip() for n in OKX_ENV.values()}
    got = [n for n, v in vals.items() if v]
    if got and len(got) != len(vals):
        raise KeysError(f"OKX: задан не весь набор — нет {', '.join(n for n in vals if n not in got)}")
    if got:
        for v in vals.values():
            _remember_exact(v)
        out["okx"] = OkxCreds(*(ApiSecret(vals[n], n) for n in OKX_ENV.values()))
    p = _secret_url(env, cfg.env_name("spot.solana.rpc_primary_env"), ("https",))
    s = _secret_url(env, cfg.env_name("spot.solana.rpc_secondary_env"), ("https",))
    if p and s and p.reveal() == s.reveal():
        raise KeysError("основной и резервный RPC Solana — один и тот же URL: сверка UNKNOWN не была бы независимой")
    out.update(rpc_primary=p, rpc_secondary=s, rpc_ws=_secret_url(env, cfg.env_name("spot.solana.rpc_ws_env"), ("wss",)))
    return out


def load_sol_hl(cfg: OwnerCfg, mode: str | None = None, environ=None) -> SolHlKeys:
    """Ключи связки sol_best_hyperliquid. Ключей Aster/EVM не требует и не трогает.
      dry      — KeysForbidden ДО обращения к окружению;
      readonly — приватные ключи (Solana, агент HL) не разбираются и не хранятся: переменные только стираются из
                 окружения; берутся публичные адреса owner.toml, ключи API провайдеров и URL RPC;
      live     — связка включена (иначе режим не выше readonly), ключ Solana и ключ агента HL сверены с owner.toml."""
    m = effective_mode(cfg.profile_mode(SOL_HL), mode)
    if m == "dry":
        raise KeysForbidden("связка sol_best_hyperliquid в режиме dry: ключи не загружаются")
    env = os.environ if environ is None else environ
    n_b58 = cfg.env_name("spot.solana.secret_b58_env")
    n_file = cfg.env_name("spot.solana.keypair_file_env")
    n_hl = cfg.env_name("perp.hyperliquid.agent_key_env")
    # забрать и стереть из окружения сразу (и в readonly — там они не нужны); стирается и при отказе ниже
    raw_b58 = env.pop(n_b58, None)
    raw_hl = env.pop(n_hl, None)
    try:
        need = ["wallets.sol_hl.solana_address", "wallets.sol_hl.hl_user_address", "wallets.sol_hl.hl_account_address"]
        if m == "live":
            need.append("wallets.sol_hl.hl_agent_address")
        try:
            sol_addr, user, account, *rest = cfg.require(*need)
        except OwnerMissing as e:
            raise KeysError(str(e)) from None
        agent = rest[0] if rest else cfg.get("wallets.sol_hl.hl_agent_address")
        api = _api_creds(cfg, env)
        sol = hl = None
        if m == "live":
            sol = _load_solana(raw_b58, env.get(n_file), n_b58, n_file, sol_addr)
            hl = _load_hl_agent(raw_hl, n_hl, agent, user, account)
        return SolHlKeys(mode=m, solana_address=sol_addr, hl_user=user, hl_account=account,
                         hl_vault=cfg.get("wallets.sol_hl.hl_vault_address"), hl_agent_address=agent,
                         sol=sol, hl=hl, **api)
    finally:
        raw_b58 = raw_hl = None


@dataclass(frozen=True)
class Readiness:
    """Готовность связки к live — отдельно по профилю: ключи одной связки не нужны другой (M01/M02)."""
    profile: str
    mode: str
    enabled: bool
    blockers: tuple[str, ...]                 # owner.toml: выключена, режим, пустые ключи, неподдержанное
    env_issues: tuple[str, ...] | None        # переменные окружения; None — не смотрели (dry)

    @property
    def live_ready(self) -> bool:
        return not self.blockers and self.env_issues == ()


def _live_env(cfg: OwnerCfg, profile_id: str, env) -> list[str]:
    """Чего нет в окружении для live этой связки (имена, без значений). Смотрит только присутствие."""
    has = lambda n: bool((env.get(n) or "").strip())     # noqa: E731
    if profile_id == LEGACY_PROFILE:
        return [n for n in (EVM_KEY_ENV, ASTER_KEY_ENV, "ASTER_SIGNER_ADDRESS") if not has(n)]
    out = []
    n_b58, n_file = cfg.env_name("spot.solana.secret_b58_env"), cfg.env_name("spot.solana.keypair_file_env")
    if has(n_b58) and has(n_file):
        out.append(f"{n_b58} и {n_file} заданы оба — оставьте один")
    elif not (has(n_b58) or has(n_file)):
        out.append(f"{n_b58} или {n_file}")
    names = [cfg.env_name("perp.hyperliquid.agent_key_env"), cfg.env_name("spot.solana.rpc_primary_env"),
             cfg.env_name("spot.solana.rpc_secondary_env")]
    paths = cfg.get("routing.solana.paths") or SOL_PATHS
    if any(p.startswith("jupiter_") for p in paths):
        names.append(cfg.env_name("providers.jupiter.api_key_env"))
    if any(p.startswith("okx_") for p in paths):
        names += list(OKX_ENV.values())
    return out + [n for n in names if not has(n)]


def profile_readiness(cfg: OwnerCfg, profile_id: str, environ=None) -> Readiness:
    """Для doctor и старта — ДО загрузки ключей (load стирает приватные переменные). В dry окружение не читается."""
    mode = cfg.profile_mode(profile_id)
    env_issues = None
    if mode != "dry":
        env = os.environ if environ is None else environ
        env_issues = tuple(_live_env(cfg, profile_id, env))
    return Readiness(profile_id, mode, cfg.profile_enabled(profile_id), tuple(cfg.profile_live_blockers(profile_id)),
                     env_issues)


# --- маскировка -----------------------------------------------------------------------------------
_TG_RE = re.compile(r"bot\d+:[\w-]+")
_HEX64_RE = re.compile(r"(?<![0-9A-Za-z])(?:0x)?([0-9a-fA-F]{64})(?![0-9A-Za-z])")
_B58C = "1-9A-HJ-NP-Za-km-z"
_B58_64_RE = re.compile(rf"(?<![{_B58C}])[{_B58C}]{{86,88}}(?![{_B58C}])")     # 64 байта base58: подпись или секрет
_INT_ARR_RE = re.compile(r"\[\s*\d{1,3}(?:\s*,\s*\d{1,3}){31,}\s*\]")          # JSON-массив ≥ 32 чисел (байты ключа)
_URL_Q_RE = re.compile(r"(?i)([?&;](?:api[-_]?key|apikey|access[-_]?token|token|key|secret|auth|passphrase|password)=)"
                       r"[^&#\s\"'<>]+")
_HDR_RE = re.compile(r"(?i)\b(x-api-key|ok-access-key|ok-access-passphrase|ok-access-sign|authorization)"
                     r"([\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s\"',;}]+")
_SECRETS: set[str] = set()                      # загруженные ключи (hex, нижний регистр)
_EXACT: set[str] = set()                        # секреты с регистром: base58, ключи API, части URL RPC
_SECRET_BYTES: set[bytes] = set()               # байты загруженных секретов — для JSON-массивов
_ROLE_OF: dict[str, str] = {}                   # hex секрета → роль (имя переменной): «одна seed на две роли»
_PUBLIC: "OrderedDict[str, None]" = OrderedDict()   # хэши транзакций и прочие публичные 64-hex
_PUBLIC_B58: "OrderedDict[str, None]" = OrderedDict()   # подписи Solana (base58 64 байта), помеченные публичными
_PUBLIC_MAX = 4096
_EXACT_MIN = 8                                  # короче — не маскируем (задело бы обычные слова)
_lock = threading.Lock()


def _remember_secret(h: str, role: str | None = None) -> None:
    with _lock:
        _SECRETS.add(h.lower())
        _PUBLIC.pop(h.lower(), None)             # секрет никогда не бывает «публичным»
        if role is not None:
            _ROLE_OF.setdefault(h.lower(), role)


def _role_of(h: str) -> str | None:
    with _lock:
        return _ROLE_OF.get(h.lower())


def _remember_exact(s: str) -> None:
    s = (s or "").strip()
    if len(s) >= _EXACT_MIN:
        with _lock:
            _EXACT.add(s)
            _PUBLIC_B58.pop(s, None)


def _remember_bytes(b: bytes) -> None:
    """Все формы секрета Solana: hex, base58 целиком и половины, байты (для JSON-массивов)."""
    with _lock:
        for part in (b, b[:32]):
            _SECRETS.add(part.hex())
            _SECRET_BYTES.add(bytes(part))
            _EXACT.add(b58encode(part))
            _PUBLIC_B58.pop(b58encode(part), None)


def _remember_url(u: str) -> None:
    """URL RPC с ключом: сам URL, значения query и длинные сегменты пути (QuickNode держит токен в пути)."""
    _remember_exact(u)
    m = _URL_RE.match(u)
    if not m:
        return
    rest = m.group(3)
    for part in re.split(r"[/?&#;=]", rest):
        if len(part) >= 16:
            _remember_exact(part)
    for q in re.findall(r"=([^&#]+)", rest):
        _remember_exact(q)


def _is_b58_64(s: str) -> bool:
    try:
        return len(b58decode(s)) == 64
    except ValueError:
        return False


def mark_public(*values: str) -> None:
    """Пометить 64-hex как публичные (хэш подписанной транзакции — он и так уйдёт в сеть): redact() их не прячет.
    Подпись Solana (base58, 64 байта) — так же: без пометки redact() её прячет (формат совпадает с секретом)."""
    with _lock:
        for v in values:
            h = str(v or "").strip().lower().removeprefix("0x")
            if len(h) == 64 and h not in _SECRETS and all(c in "0123456789abcdef" for c in h):
                _PUBLIC[h] = None
                _PUBLIC.move_to_end(h)
                while len(_PUBLIC) > _PUBLIC_MAX:
                    _PUBLIC.popitem(last=False)
                continue
            s = str(v or "").strip()
            if s not in _EXACT and _is_b58_64(s):
                _PUBLIC_B58[s] = None
                _PUBLIC_B58.move_to_end(s)
                while len(_PUBLIC_B58) > _PUBLIC_MAX:
                    _PUBLIC_B58.popitem(last=False)


def _arr_secret(m: re.Match, secret_bytes: frozenset) -> str:
    try:
        b = bytes(int(x) for x in re.findall(r"\d+", m.group(0)))
    except ValueError:                           # число > 255 — не байты
        return m.group(0)
    return "<key>" if b in secret_bytes else m.group(0)


def redact_secrets(text) -> str:
    """Для БД и сообщений: загруженные ключи и токен Telegram. Хэши транзакций остаются (они нужны в учёте)."""
    s = str(text)
    with _lock:
        secrets = tuple(_SECRETS)
        exact = tuple(sorted(_EXACT, key=len, reverse=True))
        sbytes = frozenset(_SECRET_BYTES)
    for sec in secrets:
        s = re.sub(re.escape(sec), "<key>", s, flags=re.IGNORECASE)
    for sec in exact:
        s = s.replace(sec, "<secret>")
    if sbytes:
        s = _INT_ARR_RE.sub(lambda mm: _arr_secret(mm, sbytes), s)
    s = _URL_Q_RE.sub(r"\1<redacted>", s)
    s = _HDR_RE.sub(r"\1\2<redacted>", s)
    return _TG_RE.sub("bot<redacted>", s)


def redact(text) -> str:
    """Для логов: redact_secrets + любая 64-hex строка, кроме помеченных mark_public; любая base58 64 байт (подпись
    или секрет Solana), кроме помеченных; любой JSON-массив из ≥ 32 чисел 0..255."""
    s = redact_secrets(text)
    with _lock:
        public = set(_PUBLIC)
        public_b58 = set(_PUBLIC_B58)
    s = _HEX64_RE.sub(lambda mm: mm.group(0) if mm.group(1).lower() in public else "<hex64>", s)
    s = _B58_64_RE.sub(lambda mm: mm.group(0) if mm.group(0) in public_b58 or not _is_b58_64(mm.group(0))
                       else "<b58-64>", s)
    return _INT_ARR_RE.sub(lambda mm: "<bytes>" if all(int(x) <= 255 for x in re.findall(r"\d+", mm.group(0)))
                           else mm.group(0), s)


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
        _EXACT.clear()
        _SECRET_BYTES.clear()
        _ROLE_OF.clear()
        _PUBLIC_B58.clear()
