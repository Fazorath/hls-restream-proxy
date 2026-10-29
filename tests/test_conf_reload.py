import os
import tempfile
import unittest

from tests.helpers import FakeUpstream, RunningProxy

HLS = {"Content-Type": "application/vnd.apple.mpegurl"}
MEDIA = b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nseg1.ts\n"


class ConfReloadTest(unittest.TestCase):
    def setUp(self):
        self.up = FakeUpstream().start()
        self.addCleanup(self.up.stop)
        for name in ("a", "b"):
            self.up.routes[f"/{name}.m3u8"] = (200, HLS, MEDIA)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conf = os.path.join(self.dir.name, "channels.conf")

    def write_conf(self, text):
        # Same atomic temp-file + rename the bridge uses.
        tmp = self.conf + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, self.conf)

    def start(self):
        p = RunningProxy(channels_conf=self.conf, allow_private_upstreams=True,
                         cache_ttl=3600).start()
        self.addCleanup(p.stop)
        return p

    def hits(self, name):
        return [h for path, h in self.up.requests if path == f"/{name}.m3u8"]

    def test_changed_literal_url_drops_cache_without_waiting_for_ttl(self):
        self.write_conf(f"ch|Ch|1||Stremio Bridge|{self.up.base}/a.m3u8|literal||\n")
        p = self.start()
        self.assertEqual(p.get("/channel/ch")[0], 200)
        self.assertEqual(len(self.hits("a")), 1)
        self.write_conf(f"ch|Ch|1||Stremio Bridge|{self.up.base}/b.m3u8|literal||\n")
        self.assertEqual(p.get("/channel/ch")[0], 200)
        self.assertEqual(len(self.hits("b")), 1)
        self.assertEqual(len(self.hits("a")), 1)

    def test_unchanged_conf_keeps_cache(self):
        self.write_conf(f"ch|Ch|1||News|{self.up.base}/a.m3u8|literal||\n")
        p = self.start()
        p.get("/channel/ch")
        p.get("/channel/ch")
        # one playlist fetch per request, but the cache entry is kept
        self.assertIn("ch", p.server.app.channel_cache)

    def test_eleventh_field_user_agent_is_sent(self):
        self.write_conf(f"ch|Ch|1||Stremio Bridge|{self.up.base}/a.m3u8|literal|https://site.example/||ch|Custom-UA/1.0\n")
        p = self.start()
        self.assertEqual(p.get("/channel/ch")[0], 200)
        h = self.hits("a")[0]
        self.assertEqual(h.get("User-Agent"), "Custom-UA/1.0")
        self.assertEqual(h.get("Referer"), "https://site.example/")

    def test_removed_user_agent_falls_back_to_default(self):
        self.write_conf(f"ch|Ch|1|||{self.up.base}/a.m3u8|literal||||Custom-UA/1.0\n")
        p = self.start()
        p.get("/channel/ch")
        self.write_conf(f"ch|Ch|1|||{self.up.base}/b.m3u8|literal||||\n")
        p.get("/channel/ch")
        self.assertNotEqual(self.hits("b")[0].get("User-Agent"), "Custom-UA/1.0")


if __name__ == "__main__":
    unittest.main()
