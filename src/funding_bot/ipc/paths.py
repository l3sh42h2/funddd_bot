"""Shared transport paths; no trading imports or secret loading."""
import os
from pathlib import Path
from .. import config


def socket_path():
    return Path(os.environ.get('FUNDING_CORE_SOCKET', '/run/funding-bot/core.sock'))


def execution_lock():
    return Path(os.environ.get('FUNDING_EXECUTION_LOCK', config.RUNTIME / 'execution.lock'))


def interface_state():
    return Path(os.environ.get('FUNDING_INTERFACE_STATE', config.RUNTIME / 'interface' / 'state.json'))
