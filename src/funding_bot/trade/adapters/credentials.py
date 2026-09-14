"""Process-scoped credential loader. EVM signing does not require a futures venue.

No raw environment snapshot, no reload after destructive pop, no mode escalation.
Only the requesting adapter receives its credential object.
"""
import os
import threading
from dataclasses import dataclass
from .. import keys as K


@dataclass(frozen=True, repr=False)
class EvmCredentials:
    mode: str
    evm_address: str
    evm: object

    def gate(self, owner_mode, action, paused=False, hedge=False):
        K.gate(K.effective_mode(owner_mode, self.mode), action, paused=paused, hedge=hedge)


def _require(cfg, *names):
    try:
        return cfg.require(*names)
    except K.OwnerMissing as e:
        raise K.KeysError(str(e)) from None


class CredentialProvider:
    def __init__(self, environ=None):
        self._env = os.environ if environ is None else environ
        self._cache = {}
        self._errors = set()
        self._lock = threading.RLock()

    def __repr__(self):
        return '<CredentialProvider>'

    def __reduce__(self):
        raise TypeError('credentials cannot be serialized')

    def once(self, scope, loader):
        with self._lock:
            if scope in self._errors:
                raise K.KeysError('credential initialization failed; fix configuration and restart')
            if scope not in self._cache:
                try:
                    self._cache[scope] = loader()
                except Exception:
                    self._errors.add(scope)
                    raise
            return self._cache[scope]

    @staticmethod
    def _mode(mode, loaded, label):
        # A cached credential is a ceiling, never a reason to silently escalate.
        if K.effective_mode(mode, loaded.mode) != mode:
            raise K.ModeForbidden(f'{label} permissions require restart')

    def evm(self, mode, wallet):
        if mode == 'dry':
            raise K.KeysForbidden('dry: EVM credentials are not loaded')
        def load():
            raw = self._env.pop(K.EVM_KEY_ENV, None)
            acct = K._from_key(K._account_cls(), raw, K.EVM_KEY_ENV)
            expected = self._env.get('DEX_EVM_ADDRESS')
            if expected and K._lc(expected) != K._lc(acct.address):
                raise K.KeyMismatch('DEX_EVM_ADDRESS differs from signing account')
            return EvmCredentials(mode, acct.address, K.SignerKey(acct, 'evm') if mode == 'live' else None)
        k = self.once('evm', load)
        if not wallet or K._lc(wallet) != K._lc(k.evm_address):
            raise K.KeyMismatch('configured EVM wallet differs from signing account')
        self._mode(mode, k, 'EVM signing')
        return k

    def aster(self, cfg, mode):
        if mode == 'dry':
            raise K.KeysForbidden('dry: Aster credentials are not loaded')
        def load():
            raw = self._env.pop(K.ASTER_KEY_ENV, None)
            acct = K._from_key(K._account_cls(), raw, K.ASTER_KEY_ENV)
            user, signer = _require(cfg, 'wallets.aster_user', 'wallets.aster_signer')
            eu, es = self._env.get('ASTER_USER'), self._env.get('ASTER_SIGNER_ADDRESS')
            if K._lc(acct.address) in {K._lc(user), K._lc(eu)} - {''}:
                raise K.KeyMismatch('Aster master key is forbidden')
            if not es or K._lc(es) != K._lc(acct.address) or K._lc(signer) != K._lc(acct.address):
                raise K.KeyMismatch('Aster signer identity mismatch')
            if eu and K._lc(eu) != K._lc(user):
                raise K.KeyMismatch('Aster user identity mismatch')
            return AsterCredentials(mode, user, acct.address, K.SignerKey(acct, 'aster'))
        user = cfg.get('wallets.aster_user')
        signer = cfg.get('wallets.aster_signer')
        k = self.once('aster', load)
        if not user or not signer or (K._lc(user), K._lc(signer)) != k.identity:
            raise K.KeyMismatch('configured Aster identity differs from signing credentials')
        self._mode(mode, k, 'Aster signing')
        return k

    def legacy(self, cfg, mode):
        # Historical alias: compose the two independent credential scopes only when
        # the legacy EVM×Aster profile is actually requested.
        evm = self.evm(mode, cfg.get('wallets.bsc'))
        aster = self.aster(cfg, mode)
        return K.Keys(mode, evm.evm_address, aster.aster_user, aster.aster_signer,
                      aster.aster, evm.evm)

    def gate(self):
        def load():
            from ..gate_trade import load_env_keys
            values = load_env_keys(self._env)
            self._env.pop('GATE_API_KEY', None)
            self._env.pop('GATE_API_SECRET', None)
            for value in values:
                K._remember_exact(value)
            return tuple(K.ApiSecret(value, name) for value, name in zip(values, ('GATE_API_KEY', 'GATE_API_SECRET')))
        return self.once('gate', load)

    def solana(self, cfg, mode):
        if mode == 'dry':
            raise K.KeysForbidden('dry: Solana credentials are not loaded')
        def load():
            name = cfg.env_name('spot.solana.secret_b58_env')
            raw = self._env.pop(name, None)
            wallet, = _require(cfg, 'wallets.sol_hl.solana_address')
            secret = None
            if mode == 'live':
                file_name = cfg.env_name('spot.solana.keypair_file_env')
                secret = K._load_solana(raw, self._env.get(file_name), name, file_name, wallet)
            return SolanaCredentials(mode, wallet, secret, **K._api_creds(cfg, self._env))
        k = self.once('solana', load)
        wallet = cfg.get('wallets.sol_hl.solana_address')
        if wallet and K._lc(wallet) != K._lc(k.solana_address):
            raise K.KeyMismatch('configured Solana wallet differs from cached credentials')
        self._mode(mode, k, 'Solana signing')
        return k

    def hyperliquid(self, cfg, mode):
        if mode == 'dry':
            raise K.KeysForbidden('dry: Hyperliquid credentials are not loaded')
        def load():
            name = cfg.env_name('perp.hyperliquid.agent_key_env')
            raw = self._env.pop(name, None)
            user, account = _require(cfg, 'wallets.sol_hl.hl_user_address', 'wallets.sol_hl.hl_account_address')
            agent = cfg.get('wallets.sol_hl.hl_agent_address')
            signer = K._load_hl_agent(raw, name, agent, user, account) if mode == 'live' else None
            return HlCredentials(mode, user, account, cfg.get('wallets.sol_hl.hl_vault_address'), agent, signer)
        k = self.once('hyperliquid', load)
        user, account = cfg.get('wallets.sol_hl.hl_user_address'), cfg.get('wallets.sol_hl.hl_account_address')
        if (user and K._lc(user) != K._lc(k.hl_user)) or (account and K._lc(account) != K._lc(k.hl_account)):
            raise K.KeyMismatch('configured Hyperliquid identity differs from cached credentials')
        self._mode(mode, k, 'Hyperliquid signing')
        return k

    def sol_hl(self, cfg, mode):
        # Historical alias only: each half loads independently and is reusable by another composition.
        sol, hl = self.solana(cfg, mode), self.hyperliquid(cfg, mode)
        return K.SolHlKeys(mode=K.effective_mode(sol.mode, hl.mode), solana_address=sol.solana_address,
                           hl_user=hl.hl_user, hl_account=hl.hl_account, hl_vault=hl.hl_vault,
                           hl_agent_address=hl.hl_agent_address, sol=sol.sol, hl=hl.hl, jupiter=sol.jupiter,
                           okx=sol.okx, rpc_primary=sol.rpc_primary, rpc_secondary=sol.rpc_secondary,
                           rpc_ws=sol.rpc_ws)


class ModeCredentials:
    def gate(self, owner_mode, action, paused=False, hedge=False):
        K.gate(K.effective_mode(owner_mode, self.mode), action, paused=paused, hedge=hedge)

    def __reduce__(self):
        raise TypeError('credentials cannot be serialized')


@dataclass(frozen=True, repr=False)
class AsterCredentials(ModeCredentials):
    mode: str
    aster_user: str
    aster_signer: str
    aster: object

    @property
    def identity(self):
        return K._lc(self.aster_user), K._lc(self.aster_signer)


@dataclass(frozen=True, repr=False)
class SolanaCredentials(ModeCredentials):
    mode: str
    solana_address: str
    sol: object
    jupiter: object = None
    okx: object = None
    rpc_primary: object = None
    rpc_secondary: object = None
    rpc_ws: object = None


@dataclass(frozen=True, repr=False)
class HlCredentials(ModeCredentials):
    mode: str
    hl_user: str
    hl_account: str
    hl_vault: str | None
    hl_agent_address: str | None
    hl: object
