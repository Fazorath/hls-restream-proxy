import os
import re
import tempfile

from tests.helpers import hls_proxy
from tests.test_sources import SourcesBase

M3U_BODY = (
    '#EXTM3U\n'
    '#EXTINF:-1 tvg-id="ABC.us@SD" group-title="News;Local" tvg-language="English",ABC\n{abc}\n'
    '#EXTINF:-1 tvg-id="Tele.mx" group-title="Sports" tvg-language="Spanish",Tele\n{tele}\n'
    '#EXTINF:-1 tvg-id="Fr.fr" tvg-country="FR" group-title="Movies" tvg-language="French;English",Fr\n{fr}\n'
    '#EXTINF:-1 tvg-id="bare",Bare\n{bare}\n'
)


class FilterBase(SourcesBase):
    def setup_list(self, **kw):
        self.list_route("/a.m3u", M3U_BODY.format(**{n: self.stream(n) for n in ("abc", "tele", "fr", "bare")}))
        return self.start(upstream_m3u_urls=(self.up.base + "/a.m3u",), **kw)


class FilterTest(FilterBase):
    def listed(self, p):
        _, body = p.get("/playlist.m3u")
        return body.decode()

    def test_no_filters_keeps_everything(self):
        text = self.listed(self.setup_list())
        self.assertEqual(text.count("/channel/"), 4)

    def test_group_filter_matches_packed_group_case_insensitively(self):
        text = self.listed(self.setup_list(filter_groups=("local",)))
        self.assertIn("ABC", text)
        self.assertIn("Bare", text)  # no group-title: kept
        self.assertNotIn("Tele", text)
        self.assertNotIn(",Fr", text)

    def test_country_filter_uses_attribute_then_tvg_id_suffix(self):
        text = self.listed(self.setup_list(filter_countries=("us", "FR")))
        self.assertIn("ABC", text)      # suffix .us@SD
        self.assertIn(",Fr", text)      # tvg-country attribute
        self.assertNotIn("Tele", text)  # suffix .mx
        self.assertIn("Bare", text)     # no country info: kept

    def test_language_filter_matches_any_packed_language(self):
        text = self.listed(self.setup_list(filter_languages=("english",)))
        self.assertIn("ABC", text)
        self.assertIn(",Fr", text)
        self.assertNotIn("Tele", text)
        self.assertIn("Bare", text)

    def test_filters_combine_with_and(self):
        text = self.listed(self.setup_list(filter_countries=("us", "fr"), filter_languages=("french",)))
        self.assertNotIn("ABC", text)
        self.assertIn(",Fr", text)

    def test_filtered_channel_is_404_and_kept_one_is_not(self):
        p = self.setup_list(filter_groups=("news",))
        listed = self.listed(p)
        kept = re.search(r"/channel/(\S+)", listed.split(",ABC")[1]).group(1)
        self.assertEqual(p.get("/channel/tele-mx")[0], 404)
        self.assertNotEqual(p.get(f"/channel/{kept}")[0], 404)
        self.assertFalse([r for r in self.up.requests if "tele" in r[0]])

    def test_channels_conf_entries_are_never_filtered(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "channels.conf")
            with open(conf, "w") as f:
                f.write(f"mine|Mine|1||Sports|{self.stream('mine')}|literal|||mine\n")
            p = self.setup_list(filter_groups=("news",), channels_conf=conf)
            self.assertIn("Mine", self.listed(p))

    def test_index_shows_per_source_counts(self):
        p = self.setup_list(filter_groups=("news",))
        p.get("/playlist.m3u")
        _, body = p.get("/")
        text = body.decode()
        self.assertIn("source 1: loaded 4, kept 2, dropped 2, unfiltered 1", text)

    def test_unfiltered_counts_only_entries_missing_an_active_attribute(self):
        p = self.setup_list(filter_languages=("english",))
        p.get("/playlist.m3u")
        _, body = p.get("/")
        self.assertIn("loaded 4, kept 3, dropped 1, unfiltered 1", body.decode())


class FilterEnvTest(FilterBase):
    def test_env_vars_split_on_commas_and_semicolons(self):
        cfg = hls_proxy.Config.from_env({
            "HLS_FILTER_GROUPS": "News, Local Sports;Kids",
            "HLS_FILTER_COUNTRIES": "us,CA",
            "HLS_FILTER_LANGUAGES": "English",
        })
        self.assertEqual(cfg.filter_groups, ("News", "Local Sports", "Kids"))
        self.assertEqual(cfg.filter_countries, ("us", "CA"))
        self.assertEqual(cfg.filter_languages, ("English",))
