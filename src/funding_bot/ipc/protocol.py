"""Bounded, versioned local RPC. Socket peer identity is checked before dispatch."""
import json
import os
import socket
import socketserver
import struct
import threading
import uuid
from pathlib import Path

VERSION = 1
MAX_FRAME = 1024 * 1024
TIMEOUT = 5


class RpcError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _exact(sock, n):
    chunks = bytearray()
    while len(chunks) < n:
        b = sock.recv(n - len(chunks))
        if not b:
            raise RpcError('truncated_frame')
        chunks.extend(b)
    return bytes(chunks)


def receive(sock):
    n = struct.unpack('!I', _exact(sock, 4))[0]
    if not 0 < n <= MAX_FRAME:
        raise RpcError('frame_too_large')
    try:
        obj = json.loads(_exact(sock, n), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise RpcError('invalid_json') from None
    if not isinstance(obj, dict):
        raise RpcError('invalid_request')
    return obj


def send(sock, obj):
    body = json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    if len(body) > MAX_FRAME:
        raise RpcError('frame_too_large')
    sock.sendall(struct.pack('!I', len(body)) + body)


def peer_uid(sock):
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    # BSD/macOS LOCAL_PEERCRED: xucred {uint version; uid; short ngroups; ...}
    if hasattr(socket, 'LOCAL_PEERCRED'):
        return struct.unpack_from('II', sock.getsockopt(0, socket.LOCAL_PEERCRED, 80))[1]
    # Darwin constant may not be exported by Python.
    if os.uname().sysname == 'Darwin':
        return struct.unpack_from('II', sock.getsockopt(0, 1, 80))[1]
    raise RpcError('peer_credentials_unsupported')


class Client:
    def __init__(self, path=None, timeout=TIMEOUT):
        from .paths import socket_path
        self.path, self.timeout = str(path or socket_path()), timeout

    def call(self, method, payload=None, *, key=None):
        rid = uuid.uuid4().hex
        req = dict(protocol_version=VERSION, request_id=rid, method=method,
                   idempotency_key=key, payload=payload or {})
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                send(sock, req)
                res = receive(sock)
        except (OSError, TimeoutError):
            raise RpcError('core_unavailable') from None
        if res.get('request_id') != rid or res.get('protocol_version') != VERSION:
            raise RpcError('incompatible_response')
        if not res.get('ok'):
            raise RpcError(res.get('error', 'rpc_error'))
        return res['result']


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(TIMEOUT)
        rid = None
        try:
            uid = peer_uid(self.request)
            if uid not in self.server.allowed:
                raise RpcError('unauthorized_peer')
            req = receive(self.request)
            rid = req.get('request_id')
            if req.get('protocol_version') != VERSION:
                raise RpcError('unsupported_version')
            if set(req) - {'protocol_version', 'request_id', 'method', 'idempotency_key', 'payload'}:
                raise RpcError('invalid_fields')
            if not isinstance(rid, str) or len(rid) > 100 or not isinstance(req.get('payload'), dict):
                raise RpcError('invalid_request')
            result = self.server.dispatch(req['method'], req['payload'], req.get('idempotency_key'), uid)
            response = dict(ok=True, result=result)
        except RpcError as e:
            response = dict(ok=False, error=e.code)
        except Exception:
            # No SQL, paths, credentials or raw exchange exceptions to UI.
            response = dict(ok=False, error='internal_error')
        try:
            send(self.request, dict(protocol_version=VERSION, request_id=rid, **response))
        except (OSError, RpcError):
            pass  # Durable acceptance is recovered with the same key.


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, path, dispatch, allowed, max_clients=16):
        self.dispatch, self.allowed = dispatch, set(allowed)
        self.slots = threading.BoundedSemaphore(max_clients)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Caller must hold singleton before removing a stale socket.
        if path.exists():
            import stat
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RpcError('socket_path_not_socket')
            path.unlink()
        super().__init__(str(path), _Handler)
        path.chmod(0o660)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()  # No request accepted; client retries same key.
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
