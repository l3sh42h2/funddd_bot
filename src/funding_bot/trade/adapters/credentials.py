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
        if K.effective_mode(mode, k.mode) != mode:
            raise K.ModeForbidden('EVM signing permissions require restart')
        return k

    def legacy(self, cfg, mode):
        evm = self.evm(mode, cfg.get('wallets.bsc'))
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
            return K.Keys(mode, evm.evm_address, user, acct.address, K.SignerKey(acct, 'aster'), evm.evm)
        return self.once('aster', load)

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
        return self.once('solana', load)

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
        return self.once('hyperliquid', load)

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
