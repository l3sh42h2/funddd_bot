"""Strict deploy-peer client for the M5 drain/readiness contract."""
from __future__ import annotations
import json
import socket
import struct
import uuid

VERSION = 1
MAX_FRAME = 1024 * 1024


class IpcRefused(RuntimeError):
    pass


def _exact(sock, size):
    out = bytearray()
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise IpcRefused('truncated core response')
        out.extend(chunk)
    return bytes(out)


class DeployClient:
    def __init__(self, path='/run/funding-bot/core.sock', timeout=5):
        self.path, self.timeout = str(path), timeout

    def call(self, method, payload):
        rid = uuid.uuid4().hex
        body = json.dumps({'protocol_version': VERSION, 'request_id': rid, 'method': method,
                           'idempotency_key': None, 'payload': payload},
                          separators=(',', ':'), allow_nan=False).encode()
        if len(body) > MAX_FRAME:
            raise IpcRefused('request too large')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                sock.sendall(struct.pack('!I', len(body)) + body)
                size = struct.unpack('!I', _exact(sock, 4))[0]
                if not 0 < size <= MAX_FRAME:
                    raise IpcRefused('invalid core frame size')
                response = json.loads(_exact(sock, size))
        except (OSError, ValueError, json.JSONDecodeError) as e:
            raise IpcRefused('core IPC unavailable/incompatible') from e
        if response.get('protocol_version') != VERSION or response.get('request_id') != rid:
            raise IpcRefused('incompatible core response')
        if response.get('ok') is not True or not isinstance(response.get('result'), dict):
            raise IpcRefused('core refused: ' + str(response.get('error', 'invalid_response')))
        return response['result']


def validate_drain(value, *, release_id, epoch=None, require_safe=False):
    required = {'drain', 'drain_epoch', 'state_revision'}
    if not isinstance(value, dict) or not required <= set(value):
        raise IpcRefused('incomplete drain response')
    if value['drain'] is not True or not isinstance(value['drain_epoch'], str) or not value['drain_epoch']:
        raise IpcRefused('drain not active')
    if type(value['state_revision']) is not int or value['state_revision'] < 0:
        raise IpcRefused('invalid drain revision')
    if epoch is not None and value['drain_epoch'] != epoch:
        raise IpcRefused('drain epoch changed')
    if value.get('release_id') not in (None, release_id):
        raise IpcRefused('drain belongs to another release')
    if require_safe:
        recovery, execution, inventory = value.get('recovery'), value.get('execution'), value.get('inventory')
        if not isinstance(recovery, dict) or recovery.get('complete') is not True:
            raise IpcRefused('recovery not complete')
        if not isinstance(execution, dict) or execution.get('busy') is not False or execution.get('lock_held') is not True:
            raise IpcRefused('execution ownership not proved')
        if not isinstance(inventory, dict) or type(inventory.get('unresolved')) is not int:
            raise IpcRefused('unresolved inventory missing')
        if inventory['unresolved'] != 0 or value.get('safe_to_switch') is not True:
            raise IpcRefused('unsafe to switch')
    return value
