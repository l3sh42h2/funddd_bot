"""Веб-процесс: большие ответы сжимаются для браузера, /status и curl проверки выката получают JSON как есть."""
import email.utils, gzip, json, threading, time, urllib.request
from http.server import ThreadingHTTPServer
from funding_bot import config, serve


def test_gzip_for_big_json_plain_for_status_and_curl(tmp_path, monkeypatch):
    rows = [dict(key=f"k{i}", base=f"B{i}", windows={}) for i in range(500)]
    (tmp_path / "t.json").write_text(json.dumps(dict(ts=1, tick_ts=1, pid=42, ff_rows=rows, sf_rows=[], n_ff=500, n_sf=0)))
    monkeypatch.setattr(config, "TABLE_PATH", tmp_path / "t.json")
    from funding_bot.market_snapshot import publish_health
    publish_health(dict(json.loads(config.TABLE_PATH.read_text()), schema_version=1), config.TABLE_PATH)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = urllib.request.urlopen(urllib.request.Request(base + "/data.json", headers={"Accept-Encoding": "gzip"}))
        raw = r.read()
        assert r.headers["Content-Encoding"] == "gzip" and json.loads(gzip.decompress(raw))["n_ff"] == 500
        r = urllib.request.urlopen(base + "/data.json")                          # без Accept-Encoding — как есть
        assert r.headers.get("Content-Encoding") is None and json.loads(r.read())["n_ff"] == 500
        # по Date ответа /data.json страница поправляет таймер свежести на часы телефона — заголовок обязан быть
        assert abs(email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp() - time.time()) < 5
        r = urllib.request.urlopen(urllib.request.Request(base + "/status", headers={"Accept-Encoding": "gzip"}))
        st = json.loads(r.read())                                                  # маленький ответ не сжимается
        assert st["pid"] == 42 and r.headers.get("Content-Encoding") is None
        r = urllib.request.urlopen(urllib.request.Request(base + "/data.json", headers={"Accept-Encoding": "gzip;q=0, identity"}))
        assert r.headers.get("Content-Encoding") is None and json.loads(r.read())["n_ff"] == 500   # явный отказ от gzip
    finally:
        srv.shutdown()


def test_accepts_gzip_tokens_and_q():
    assert serve.accepts_gzip("gzip, deflate, br, zstd") and serve.accepts_gzip("deflate;q=0.5, gzip;q=0.8") and serve.accepts_gzip("*")
    assert not serve.accepts_gzip("gzip;q=0") and not serve.accepts_gzip("identity") and not serve.accepts_gzip(None)
    assert not serve.accepts_gzip("x-gzip-no")


def test_concurrent_cache_misses_render_once(tmp_path, monkeypatch):
    import concurrent.futures
    from funding_bot import config
    table = tmp_path / 'table.json'; table.write_text('{"n_ff": 0}')
    monkeypatch.setattr(config, 'TABLE_PATH', table)
    seen = set()
    all_started = threading.Event()
    class Cache(dict):
        def get(self, key):
            seen.add(threading.get_ident())
            if len(seen) == 3:
                all_started.set()
            return super().get(key)
    monkeypatch.setattr(serve, '_cache', Cache())
    calls = []
    entered, release = threading.Event(), threading.Event()
    def load():
        calls.append(1); entered.set()
        assert release.wait(3)
        return {'n_ff': 0}
    monkeypatch.setattr(serve, 'load_table', load)
    monkeypatch.setattr(serve.dashboard, 'render', lambda table: json.dumps(table, separators=(',', ':')))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(serve._cached, 'html', False) for _ in range(3)]
        assert entered.wait(3)
        assert all_started.wait(3)
        release.set()
        assert [f.result(3) for f in futures] == [b'{"n_ff":0}'] * 3
    assert len(calls) == 1


def test_data_response_keeps_atomic_snapshot_bytes_without_reencoding(tmp_path, monkeypatch):
    table = tmp_path / 'table.json'
    raw = b'{ "n_ff": 0, "tick_ts": 1, "snapshot_id": "first" }\n'
    table.write_bytes(raw)
    monkeypatch.setattr(config, 'TABLE_PATH', table)
    monkeypatch.setattr(serve, '_cache', {})
    def forbidden(): raise AssertionError('data.json must not parse the snapshot')
    monkeypatch.setattr(serve, 'load_table', forbidden)
    assert serve._cached('data', False) == raw
    new = tmp_path / 'next.json'; new.write_bytes(b'{"snapshot_id":"second"}')
    import os
    stat = table.stat(); os.utime(new, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    new.replace(table)
    assert serve._cached('data', False) == b'{"snapshot_id":"second"}'
    assert gzip.decompress(serve._cached('data', True)) == b'{"snapshot_id":"second"}'
