import http.client
import os
import socket
import tempfile
import threading
import time
import unittest

from tests.helpers import FakeUpstream, RunningProxy, hls_proxy


class _RawUpstream:
    """Loopback server with scripted raw behaviour per path."""

    def __init__(self, handler):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.release = threading.Event()
        self._handler = handler
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handler, args=(self, conn), daemon=True).start()

    def stop(self):
        self.release.set()
        self.sock.close()


def _read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        data += conn.recv(4096)
    return data.split(b"\r\n")[0].decode()


def _endless(up, conn):
    _read_request(conn)
    conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: video/mp2t\r\n\r\n")
    try:
        while not up.release.is_set():
            conn.sendall(b"x" * 1024)
            time.sleep(0.05)
    except OSError:
        pass
    conn.close()


def _dies(up, conn):
    _read_request(conn)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: video/mp2t\r\nContent-Length: 100000\r\n\r\n" + b"x" * 500)
    time.sleep(0.2)
    conn.close()


class ConcurrencyTest(unittest.TestCase):
    def start(self, **kw):
        kw.setdefault("allow_private_upstreams", True)
        kw.setdefault("extra_allowed_hosts", ("127.0.0.1",))
        p = RunningProxy(**kw).start()
        self.addCleanup(p.stop)
        return p

    def test_endless_stream_does_not_block_other_requests(self):
        up = _RawUpstream(_endless)
        self.addCleanup(up.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conf = os.path.join(tmp.name, "channels.conf")
        with open(conf, "w") as f:
            f.write(f"demo|Demo|1|||{up.base}/demo.m3u8|literal\n")
        p = self.start(channels_conf=conf)
        conn = http.client.HTTPConnection("127.0.0.1", p.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("GET", p.proxy_url(up.base + "/live.ts"))
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        resp.read(10)  # stream is open and flowing
        status, _ = p.get("/health")
        self.assertEqual(status, 200)
        status, _ = p.get("/playlist.m3u")
        self.assertEqual(status, 200)

    def test_midstream_upstream_death_sends_no_second_status_line(self):
        up = _RawUpstream(_dies)
        self.addCleanup(up.stop)
        p = self.start()
        s = socket.create_connection(("127.0.0.1", p.port), timeout=10)
        self.addCleanup(s.close)
        path = p.proxy_url(up.base + "/live.ts")
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.0 200") or data.startswith(b"HTTP/1.1 200"))
        self.assertEqual(data.count(b"HTTP/1."), 1, data[:300])

    def test_slow_fetch_does_not_hold_state_lock(self):
        up = _RawUpstream(_endless)
        self.addCleanup(up.stop)
        p = self.start()
        app = p.server.app
        done = threading.Event()

        def slow():
            try:
                with app.fetch(up.base + "/x", {}) as r:
                    r.read(1)
                    done.wait(5)
            except Exception:
                pass

        t = threading.Thread(target=slow, daemon=True)
        t.start()
        time.sleep(0.2)
        got = threading.Event()

        def probe():
            with app.lock:
                app.allow_url_host("http://example.invalid/")
            got.set()

        threading.Thread(target=probe, daemon=True).start()
        self.assertTrue(got.wait(2), "state lock held during upstream fetch")
        done.set()

    def test_concurrent_requests_keep_state_consistent(self):
        up = FakeUpstream().start()
        self.addCleanup(up.stop)
        up.routes["/a.m3u8"] = (200, {"Content-Type": "application/vnd.apple.mpegurl"}, b"#EXTM3U\n#EXTINF:1,\nseg.ts\n")
        p = self.start()
        app = p.server.app
        errors = []

        def hammer(i):
            try:
                for j in range(30):
                    app.allow_url_host(f"http://h{i}-{j}.example/")
                    with app.lock:
                        app.channel_cache[f"c{i}-{j}"] = {"m3u8_url": "u", "embed_host": "", "fetched_at": 0}
                    p.get(p.proxy_url(up.base + "/a.m3u8"))
            except Exception as e:
                errors.append(e)

        ts = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(app.channel_cache), 240)
        self.assertTrue(all(f"h{i}-{j}.example" in app.allowed_hosts for i in range(8) for j in range(30)))


if __name__ == "__main__":
    unittest.main()
