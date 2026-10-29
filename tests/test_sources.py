import os
import tempfile
import unittest
import urllib.parse

from tests.helpers import FakeUpstream, RunningProxy

M3U = "application/x-mpegurl"
HLS = "application/vnd.apple.mpegurl"
MEDIA = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg1.ts\n"


class SourcesBase(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream().start()
        self.addCleanup(self.up.stop)

    def start(self, **kw):
        kw.setdefault("allow_private_upstreams", True)
        p = RunningProxy(**kw).start()
        self.addCleanup(p.stop)
        return p

    def list_route(self, path, body):
        self.up.routes[path] = (200, {"Content-Type": M3U}, body.encode())

    def stream(self, name):
        return f"{self.up.base}/{name}.m3u8"


class MultiSourceTest(SourcesBase):
    def test_two_lists_merge_and_duplicate_url_appears_once(self):
        self.list_route("/a.m3u", f'#EXTM3U\n#EXTINF:-1 tvg-id="one",One\n{self.stream("one")}\n'
                                  f'#EXTINF:-1 tvg-id="shared",Shared A\n{self.stream("shared")}\n')
        self.list_route("/b.m3u", f'#EXTM3U\n#EXTINF:-1 tvg-id="two",Two\n{self.stream("two")}\n'
                                  f'#EXTINF:-1 tvg-id="shared-b",Shared B\n{self.stream("shared")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u", self.up.base + "/b.m3u"))
        status, body = p.get("/playlist.m3u")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertEqual(text.count("/channel/"), 3)
        self.assertIn("Shared A", text)
        self.assertNotIn("Shared B", text)
        self.assertIn('tvg-id="two"', text)

    def test_old_single_url_setting_is_merged_in(self):
        self.list_route("/old.m3u", f'#EXTINF:-1 tvg-id="old",Old\n{self.stream("old")}\n')
        self.list_route("/new.m3u", f'#EXTINF:-1 tvg-id="new",New\n{self.stream("new")}\n')
        p = self.start(upstream_m3u_url=self.up.base + "/old.m3u",
                       upstream_m3u_urls=(self.up.base + "/new.m3u",))
        _, body = p.get("/playlist.m3u")
        self.assertIn(b'tvg-id="old"', body)
        self.assertIn(b'tvg-id="new"', body)

    def test_failing_list_keeps_last_good_copy(self):
        self.list_route("/a.m3u", f'#EXTINF:-1 tvg-id="one",One\n{self.stream("one")}\n')
        self.list_route("/b.m3u", f'#EXTINF:-1 tvg-id="two",Two\n{self.stream("two")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u", self.up.base + "/b.m3u"),
                       upstream_m3u_ttl=0)
        _, body = p.get("/playlist.m3u")
        self.assertEqual(body.decode().count("/channel/"), 2)
        self.up.routes["/b.m3u"] = (500, {}, b"boom")
        _, body = p.get("/playlist.m3u")
        self.assertEqual(body.decode().count("/channel/"), 2)
        self.assertIn(b'tvg-id="two"', body)

    def test_oversized_list_is_rejected(self):
        self.list_route("/big.m3u", "#EXTM3U\n" + "".join(
            f'#EXTINF:-1 tvg-id="c{i}",C{i}\n{self.stream(f"c{i}")}\n' for i in range(50)))
        p = self.start(upstream_m3u_urls=(self.up.base + "/big.m3u",), upstream_m3u_max_bytes=200)
        status, _ = p.get("/playlist.m3u")
        self.assertEqual(status, 404)

    def test_urls_env_var_is_split(self):
        from tests.helpers import hls_proxy
        cfg = hls_proxy.Config.from_env({"HLS_UPSTREAM_M3U_URLS": "http://a/x.m3u, http://b/y.m3u\nhttp://c/z.m3u"})
        self.assertEqual(cfg.upstream_m3u_urls, ("http://a/x.m3u", "http://b/y.m3u", "http://c/z.m3u"))


class ChannelHeadersTest(SourcesBase):
    def test_extvlcopt_headers_reach_playlist_and_segment_requests(self):
        self.up.routes["/ch.m3u8"] = (200, {"Content-Type": HLS}, MEDIA)
        self.up.routes["/seg1.ts"] = (200, {"Content-Type": "video/mp2t"}, b"ts")
        self.list_route("/a.m3u", '#EXTINF:-1 tvg-id="hdr",Hdr\n'
                                  '#EXTVLCOPT:http-referrer=https://ref.example/page\n'
                                  '#EXTVLCOPT:http-user-agent=CustomAgent/1.0\n'
                                  f'{self.stream("ch")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",),
                       upstream_m3u_referer="https://global.example/")
        status, body = p.get("/channel/hdr")
        self.assertEqual(status, 200)
        seg = [l for l in body.decode().splitlines() if l.startswith("/proxy")][0]
        status, _ = p.get(seg)
        self.assertEqual(status, 200)
        playlist_req, seg_req = [h for path, h in self.up.requests if not path.endswith(".m3u")]
        for h in (playlist_req, seg_req):
            self.assertEqual(h["Referer"], "https://ref.example/page")
            self.assertEqual(h["User-Agent"], "CustomAgent/1.0")

    def test_precedence_falls_back_to_list_referer_then_global_ua(self):
        self.up.routes["/ch.m3u8"] = (200, {"Content-Type": HLS}, MEDIA)
        self.list_route("/a.m3u", f'#EXTINF:-1 tvg-id="plain",Plain\n{self.stream("ch")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",),
                       upstream_m3u_referer="https://list.example/", upstream_ua="GlobalUA")
        p.get("/channel/plain")
        _, h = self.up.requests[-1]
        self.assertEqual(h["Referer"], "https://list.example/")
        self.assertEqual(h["User-Agent"], "GlobalUA")

    def test_extvlcopt_lines_are_not_emitted(self):
        self.list_route("/a.m3u", '#EXTINF:-1 tvg-id="hdr",Hdr\n'
                                  '#EXTVLCOPT:http-referrer=https://ref.example/\n'
                                  f'{self.stream("ch")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",))
        _, body = p.get("/playlist.m3u")
        self.assertNotIn(b"EXTVLCOPT", body)
        self.assertNotIn(b"ref.example", body)


class TvgIdTest(SourcesBase):
    def test_source_tvg_id_passes_through_unchanged(self):
        self.list_route("/a.m3u", f'#EXTINF:-1 tvg-id="BBC.One.uk" group-title="News",BBC One\n{self.stream("bbc")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",))
        _, body = p.get("/playlist.m3u")
        self.assertIn('tvg-id="BBC.One.uk"', body.decode())

    def conf(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_channels_conf_tvg_id_field_used_slug_when_empty(self):
        path = self.conf(
            f"withid|With Id|1|||{self.stream('a')}|literal|||guide.id.1\n"
            f"emptyid|Empty Id|2|||{self.stream('b')}|literal||||\n"
        )
        p = self.start(channels_conf=path)
        _, body = p.get("/playlist.m3u")
        text = body.decode()
        self.assertIn('tvg-id="guide.id.1"', text)
        self.assertIn('tvg-id="emptyid"', text)
        self.assertIn("/channel/withid", text)

    def test_old_nine_field_and_six_field_lines_still_parse(self):
        path = self.conf(
            f"six|Six|1|||{self.stream('a')}\n"
            f"nine|Nine|2|||{self.stream('b')}|literal|https://r.example/|5000000\n"
        )
        p = self.start(channels_conf=path)
        _, body = p.get("/playlist.m3u")
        text = body.decode()
        self.assertIn('tvg-id="six"', text)
        self.assertIn('tvg-id="nine"', text)


class BandwidthRegressionTest(SourcesBase):
    def test_media_playlist_is_wrapped_in_master_with_bandwidth(self):
        self.up.routes["/m.m3u8"] = (200, {"Content-Type": HLS}, MEDIA)
        self.list_route("/a.m3u", f'#EXTINF:-1 tvg-id="m",M\n{self.stream("m")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",), default_bandwidth=4000000)
        _, body = p.get("/channel/m")
        text = body.decode()
        self.assertIn("#EXT-X-STREAM-INF:BANDWIDTH=4000000", text)
        self.assertIn("/channel/m/media.m3u8", text)
        _, media = p.get("/channel/m/media.m3u8")
        self.assertNotIn(b"STREAM-INF", media)
        self.assertIn(b"/proxy.ts?url=", media)

    def test_upstream_master_bandwidth_is_overridden(self):
        master = (b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=20000000,AVERAGE-BANDWIDTH=19000000\nv1.m3u8\n"
                  b"#EXT-X-STREAM-INF:RESOLUTION=640x360\nv2.m3u8\n")
        self.up.routes["/mm.m3u8"] = (200, {"Content-Type": HLS}, master)
        self.list_route("/a.m3u", f'#EXTINF:-1 tvg-id="mm",MM\n{self.stream("mm")}\n')
        p = self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",), default_bandwidth=3000000)
        _, body = p.get("/channel/mm")
        text = body.decode()
        self.assertEqual(text.count("BANDWIDTH=3000000"), 3)  # 2 variants + AVERAGE
        self.assertNotIn("20000000", text)
        self.assertNotIn("19000000", text)


if __name__ == "__main__":
    unittest.main()
