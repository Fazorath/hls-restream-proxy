import socket
import os
import tempfile
import unittest

from tests.helpers import FakeUpstream, RunningProxy, hls_proxy


class UpstreamPolicyTest(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream().start()
        self.up.routes["/ok.ts"] = (200, {"Content-Type": "video/mp2t"}, b"data")
        self.addCleanup(self.up.stop)

    def start(self, **kw):
        kw.setdefault("allow_private_upstreams", True)
        p = RunningProxy(**kw).start()
        self.addCleanup(p.stop)
        return p

    def assertRefused(self, proxy, url):
        status, _ = proxy.get(proxy.proxy_url(url))
        self.assertEqual(status, 403, url)
        self.assertEqual(self.up.requests, [], f"upstream was fetched for {url}")

    def test_allowed_host_is_fetched(self):
        p = self.start(extra_allowed_hosts=("127.0.0.1",))
        status, body = p.get(p.proxy_url(self.up.base + "/ok.ts"))
        self.assertEqual((status, body), (200, b"data"))

    def test_non_http_schemes_refused(self):
        p = self.start(extra_allowed_hosts=("127.0.0.1",))
        for url in (
            "file:///proc/self/environ",
            "file://127.0.0.1/etc/passwd",
            "ftp://127.0.0.1/x",
            "data:text/plain;base64,aGk=",
            "gopher://127.0.0.1/x",
            "//127.0.0.1/x",
        ):
            self.assertRefused(p, url)

    def test_unknown_host_refused_with_empty_allowlist(self):
        p = self.start()
        self.assertRefused(p, self.up.base + "/ok.ts")

    def test_userinfo_trick_refused(self):
        p = self.start(extra_allowed_hosts=("127.0.0.1",))
        self.assertRefused(p, f"http://127.0.0.1@localhost:{self.up.port}/ok.ts")
        self.assertRefused(p, f"http://user:pw@127.0.0.1:{self.up.port}/ok.ts")
        # allowed host as the userinfo, the real host being something else
        self.assertRefused(p, f"http://127.0.0.1:80@localhost:{self.up.port}/ok.ts")

    def test_redirect_to_disallowed_targets_refused(self):
        p = self.start(extra_allowed_hosts=("127.0.0.1",))
        for i, target in enumerate((
            "file:///etc/passwd",
            "ftp://127.0.0.1/x",
            "http://10.0.0.1/x",
            "http://evil.invalid/x",
        )):
            path = f"/r{i}.ts"
            self.up.routes[path] = (302, {"Location": target}, b"")
            status, _ = p.get(p.proxy_url(self.up.base + path))
            self.assertEqual(status, 403, target)
        # Only the redirecting requests reached the upstream, nothing followed.
        self.assertEqual(len(self.up.requests), 4)

    def test_redirect_to_allowed_target_followed(self):
        p = self.start(extra_allowed_hosts=("127.0.0.1",))
        self.up.routes["/r.ts"] = (302, {"Location": "/ok.ts"}, b"")
        status, body = p.get(p.proxy_url(self.up.base + "/r.ts"))
        self.assertEqual((status, body), (200, b"data"))

    def test_private_and_special_addresses_refused_without_unsafe_flag(self):
        hosts = (
            "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "100.64.0.1",
            "169.254.169.254", "0.0.0.0", "224.0.0.1",
            "[::1]", "[fd00::1]", "[fe80::1]",
            "[::ffff:127.0.0.1]", "[::ffff:10.0.0.1]", "[::ffff:100.64.0.1]",
        )
        p = self.start(
            allow_private_upstreams=False,
            extra_allowed_hosts=tuple(h.strip("[]") for h in hosts),
        )
        for h in hosts:
            self.assertRefused(p, f"http://{h}:{self.up.port}/ok.ts")

    def test_redirect_to_private_ip_refused_without_unsafe_flag(self):
        # Offline: the resolver makes "localhost" look public (the fake upstream
        # really is on loopback) and "internal.test" resolve to a private address.
        p = self.start(allow_private_upstreams=False,
                       extra_allowed_hosts=("localhost", "internal.test"))

        def resolver(host, *args, **kw):
            if host == "internal.test":
                return [(2, 1, 6, "", ("10.0.0.5", 0))]
            if host == "localhost":
                return [(2, 1, 6, "", ("8.8.8.8", 0))]
            return [(2, 1, 6, "", ("8.8.8.8", 0))]

        p.server.app._resolve = resolver
        # the pinned connect dials the "public" address; send it to the fake on loopback
        p.server.app._dial = lambda ip, port, timeout: socket.create_connection(("127.0.0.1", port), timeout)
        self.up.routes["/r.ts"] = (302, {"Location": f"http://internal.test:{self.up.port}/ok.ts"}, b"")
        status, _ = p.get(p.proxy_url(f"http://localhost:{self.up.port}/r.ts"))
        self.assertEqual(status, 403)
        self.assertEqual([path for path, _ in self.up.requests], ["/r.ts"])

    def test_dns_rebinding_between_check_and_connect_is_refused(self):
        # First answer is public (passes check_url), every later one is loopback,
        # where the fake upstream really listens. The connection must not be made.
        p = self.start(allow_private_upstreams=False, extra_allowed_hosts=("localhost",))
        answers = iter(["8.8.8.8"] * 2)  # the handler and fetch() each run check_url once
        p.server.app._resolve = lambda host, *a, **k: [(2, 1, 6, "", (next(answers, "127.0.0.1"), 0))]
        self.up.routes["/ok.ts"] = (200, {"Content-Type": "video/mp2t"}, b"data")
        status, _ = p.get(p.proxy_url(f"http://localhost:{self.up.port}/ok.ts"))
        self.assertEqual(status, 403)
        self.assertEqual(self.up.requests, [])

    def test_playlist_referenced_hosts_become_allowed(self):
        self.up.routes["/pl.m3u8"] = (
            200, {"Content-Type": "application/vnd.apple.mpegurl"},
            b"#EXTM3U\n#EXTINF:4,\nseg1.ts\n",
        )
        p = self.start(extra_allowed_hosts=("127.0.0.1",))
        status, body = p.get(p.proxy_url(self.up.base + "/pl.m3u8"))
        self.assertEqual(status, 200)
        self.assertIn(b"/proxy.ts?url=", body)


class ClientAllowlistTest(unittest.TestCase):
    def check(self, allowed, expected):
        p = RunningProxy(allowed_ips=hls_proxy._parse_client_networks(allowed)).start()
        self.addCleanup(p.stop)
        self.assertEqual(p.get("/")[0], expected)

    def test_cidr_allows(self):
        self.check("10.0.0.0/8, 127.0.0.0/8", 200)

    def test_single_ip_allows(self):
        self.check("127.0.0.1", 200)

    def test_cidr_denies(self):
        self.check("10.0.0.0/8", 403)

    def test_ipv4_mapped_client_normalised(self):
        app = hls_proxy.ProxyApp(hls_proxy.Config(
            allowed_ips=hls_proxy._parse_client_networks("192.168.0.0/16")))
        self.assertTrue(app.client_allowed("::ffff:192.168.1.5"))
        self.assertFalse(app.client_allowed("::ffff:10.0.0.5"))
        self.assertFalse(app.client_allowed("not-an-ip"))

    def test_health_answers_any_client(self):
        p = RunningProxy(allowed_ips=hls_proxy._parse_client_networks("10.0.0.0/8")).start()
        self.addCleanup(p.stop)
        self.assertEqual(p.get("/health")[0], 200)

    def test_health_exempt_but_other_paths_still_refused(self):
        p = RunningProxy(allowed_ips=hls_proxy._parse_client_networks("10.0.0.0/8")).start()
        self.addCleanup(p.stop)
        self.assertEqual(p.get("/health")[0], 200)
        self.assertEqual(p.get("/")[0], 403)

    def test_dockerfile_healthcheck_uses_ipv4_loopback(self):
        # Alpine resolves "localhost" to ::1, but the proxy binds IPv4 only.
        path = os.path.join(os.path.dirname(__file__), "..", "Dockerfile")
        with open(path) as f:
            text = f.read()
        self.assertIn("http://127.0.0.1:8089/health", text)
        self.assertNotIn("localhost", text)

    def test_invalid_entry_is_a_config_error(self):
        with self.assertRaises(hls_proxy.ConfigError):
            hls_proxy._parse_client_networks("10.0.0.0/8,banana")


class StartupTest(unittest.TestCase):
    def test_non_loopback_bind_without_allowlist_refused(self):
        for bind in ("0.0.0.0", "::", "192.0.2.1"):
            with self.assertRaises(hls_proxy.ConfigError, msg=bind):
                hls_proxy.build_server(hls_proxy.Config(port=0, bind_addr=bind))

    def test_non_loopback_bind_with_allowlist_or_opt_out_starts(self):
        for cfg in (
            hls_proxy.Config(port=0, bind_addr="0.0.0.0",
                             allowed_ips=hls_proxy._parse_client_networks("10.0.0.0/8")),
            hls_proxy.Config(port=0, bind_addr="0.0.0.0", allow_all_clients=True),
        ):
            server = hls_proxy.build_server(cfg)
            server.server_close()

    def test_env_opt_out_flag(self):
        cfg = hls_proxy.Config.from_env({"HLS_PROXY_BIND": "0.0.0.0", "HLS_ALLOW_ALL_CLIENTS": "1"})
        self.assertTrue(cfg.allow_all_clients)
        self.assertFalse(cfg.allow_private_upstreams)

    def test_main_exits_nonzero(self):
        old = dict(os.environ)
        os.environ.update({"HLS_PROXY_BIND": "0.0.0.0", "HLS_PROXY_PORT": "0"})
        os.environ.pop("HLS_ALLOWED_IPS", None)
        os.environ.pop("HLS_ALLOW_ALL_CLIENTS", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                hls_proxy.main()
            self.assertEqual(cm.exception.code, 1)
        finally:
            os.environ.clear()
            os.environ.update(old)


class ChannelFetchPolicyTest(unittest.TestCase):
    def test_conf_literal_channel_fetched_and_scheme_refused(self):
        up = FakeUpstream().start()
        self.addCleanup(up.stop)
        up.routes["/live.m3u8"] = (200, {"Content-Type": "application/vnd.apple.mpegurl"},
                                   b"#EXTM3U\n#EXTINF:4,\ns.ts\n")
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as f:
            f.write(f"good|Good|1||News|{up.base}/live.m3u8|literal||\n")
            f.write("bad|Bad|2||News|file:///etc/passwd|literal||\n")
        self.addCleanup(os.unlink, f.name)
        p = RunningProxy(channels_conf=f.name, allow_private_upstreams=True).start()
        self.addCleanup(p.stop)
        self.assertEqual(p.get("/channel/good")[0], 200)
        n = len(up.requests)
        self.assertEqual(p.get("/channel/bad")[0], 403)
        self.assertEqual(len(up.requests), n)


if __name__ == "__main__":
    unittest.main()


class ConnectFallbackTest(unittest.TestCase):
    def test_next_address_is_tried_when_the_first_is_unreachable(self):
        app = hls_proxy.ProxyApp(hls_proxy.Config())
        app._resolve = lambda host, *a, **k: [(10, 1, 6, "", ("2606:4700::1", 0)), (2, 1, 6, "", ("8.8.8.8", 0))]
        tried = []

        def dial(ip, port, timeout):
            tried.append(ip)
            if ":" in ip:
                raise OSError("no route")
            return "sock"
        app._dial = dial
        self.assertEqual(app.connect("cdn.example", 443, 5), "sock")
        self.assertEqual(tried, ["2606:4700::1", "8.8.8.8"])
