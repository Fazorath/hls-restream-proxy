"""Shared test harness: load the proxy module, run it and a fake upstream in-process."""

import http.client
import http.server
import importlib.util
import os
import threading
import urllib.parse

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("hls_proxy", os.path.join(_HERE, "..", "hls-proxy.py"))
hls_proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hls_proxy)


class FakeUpstream:
    """A loopback HTTP server that records every request it receives.

    routes maps a path to (status, headers dict, body bytes). Add routes
    before or after start(); `requests` lists (path, headers) in arrival order.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, dict(self.headers)))
                status, headers, body = outer.routes.get(
                    self.path.split("?", 1)[0], (404, {}, b"not found")
                )
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class RunningProxy:
    """A proxy built through build_server on an ephemeral loopback port."""

    def __init__(self, **config_kwargs):
        config_kwargs.setdefault("port", 0)
        self.config = hls_proxy.Config(**config_kwargs)
        self.server = hls_proxy.build_server(self.config)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def proxy_url(self, upstream_url):
        return "/proxy?url=" + urllib.parse.quote(upstream_url, safe="")
