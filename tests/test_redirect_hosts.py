import unittest
import urllib.parse

from tests.helpers import FakeUpstream, RunningProxy, hls_proxy

M3U = "application/x-mpegurl"
HLS = "application/vnd.apple.mpegurl"
# Relative variant and audio URIs, like a stitcher master playlist.
MASTER = (
    b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",URI="audio/en.m3u8"\n'
    b"#EXT-X-STREAM-INF:BANDWIDTH=1000\nv/low.m3u8\n"
)
# "short" is the host the playlist names; "stitch" is where it redirects to.
SHORT = "127.0.0.1"
STITCH = "localhost"


class EnvParsingTest(unittest.TestCase):
    def test_extra_allowed_hosts_split_and_lowercased(self):
        cfg = hls_proxy.Config.from_env({"HLS_EXTRA_ALLOWED_HOSTS": " CDN.Example.com, b.example.com\n,c.example.com  "})
        self.assertEqual(cfg.extra_allowed_hosts, ("cdn.example.com", "b.example.com", "c.example.com"))

    def test_unset_is_empty(self):
        self.assertEqual(hls_proxy.Config.from_env({}).extra_allowed_hosts, ())


class RedirectHostTest(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream().start()
        self.addCleanup(self.up.stop)
        self.port = self.up.port
        self.up.routes["/list.m3u"] = (
            200, {"Content-Type": M3U},
            f'#EXTM3U\n#EXTINF:-1 tvg-id="news",News\nhttp://{SHORT}:{self.port}/short/news\n'.encode(),
        )
        self.up.routes["/short/news"] = (
            302, {"Location": f"http://{STITCH}:{self.port}/stitch/main/master.m3u8?sid=SECRET"}, b"")
        self.up.routes["/stitch/main/master.m3u8"] = (200, {"Content-Type": HLS}, MASTER)

    def start(self, **kw):
        p = RunningProxy(
            allow_private_upstreams=True,
            upstream_m3u_url=f"http://{SHORT}:{self.port}/list.m3u", **kw).start()
        self.addCleanup(p.stop)
        return p

    def paths(self):
        return [path.split("?", 1)[0] for path, _ in self.up.requests]

    def test_channel_redirect_to_listed_host_followed_and_rewritten_against_final_url(self):
        p = self.start(extra_allowed_hosts=(STITCH,))
        status, body = p.get("/channel/news")
        text = body.decode()
        self.assertEqual(status, 200, text)
        self.assertIn("/short/news", self.paths())
        self.assertIn("/stitch/main/master.m3u8", self.paths())
        urls = [urllib.parse.unquote(u) for u in
                (line.split("url=", 1)[1].split("&")[0] for line in text.replace('"', "\n").splitlines() if "url=" in line)]
        self.assertEqual(sorted(urls), [
            f"http://{STITCH}:{self.port}/stitch/main/audio/en.m3u8",
            f"http://{STITCH}:{self.port}/stitch/main/v/low.m3u8",
        ])

    def test_channel_redirect_to_unlisted_host_refused_and_names_host(self):
        p = self.start()
        status, body = p.get("/channel/news")
        self.assertEqual(status, 403)
        self.assertIn(b"Unknown upstream host", body)
        self.assertIn(STITCH.encode(), body)
        self.assertNotIn(b"SECRET", body)
        self.assertNotIn("/stitch/main/master.m3u8", self.paths())

    def test_proxy_redirect_rewrites_against_final_url(self):
        p = self.start(extra_allowed_hosts=(STITCH,))
        status, body = p.get(p.proxy_url(f"http://{SHORT}:{self.port}/short/news"))
        text = body.decode()
        self.assertEqual(status, 200, text)
        self.assertIn(urllib.parse.quote(f"http://{STITCH}:{self.port}/stitch/main/v/low.m3u8", safe=""), text)
        self.assertNotIn(urllib.parse.quote(f"http://{SHORT}:{self.port}/short/v/low.m3u8", safe=""), text)

    def test_proxy_redirect_to_unlisted_host_refused(self):
        p = self.start(extra_allowed_hosts=(SHORT,))
        status, body = p.get(p.proxy_url(f"http://{SHORT}:{self.port}/short/news"))
        self.assertEqual(status, 403)
        self.assertIn(STITCH.encode(), body)
        self.assertNotIn("/stitch/main/master.m3u8", self.paths())


if __name__ == "__main__":
    unittest.main()
