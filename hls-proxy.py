#!/usr/bin/env python3
"""
HLS reverse proxy that injects custom HTTP headers (User-Agent, Referer, etc.)
into upstream requests. Rewrites m3u8 playlists so media players fetch all
segments through the proxy — no client-side header configuration needed.

Supports /channel/<slug> endpoints that auto-scrape fresh m3u8 URLs on the fly,
so media servers never see expired tokens.

Zero dependencies — stdlib only (Python 3.9+).
"""

import dataclasses
import http.server
import ipaddress
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class ConfigError(Exception):
    """Raised at startup for an unsafe or invalid configuration."""


class PolicyError(Exception):
    """Raised when an upstream URL fails the upstream URL policy."""


def _parse_client_networks(raw):
    """Parse a comma-separated list of IPs/CIDRs into ip_network objects."""
    nets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            raise ConfigError(f"Invalid HLS_ALLOWED_IPS entry: {item!r}")
    return tuple(nets)


def _split_filter(raw):
    """Split a filter setting on commas and semicolons (names may hold spaces)."""
    return tuple(x.strip() for x in re.split(r"[,;]", raw) if x.strip())


def _split_list(raw):
    """Split a comma/whitespace separated list, dropping empties."""
    return tuple(x for x in re.split(r"[,\s]+", raw) if x)


@dataclasses.dataclass
class Config:
    port: int = 8089
    bind_addr: str = "127.0.0.1"  # localhost only by default
    upstream_ua: str = DEFAULT_UA
    upstream_referer: str = ""
    channels_conf: str = ""
    cache_ttl: int = 3600
    # IPs / CIDRs allowed to talk to the proxy (empty = nobody is allowed
    # unless allow_all_clients is set; see build_server for the startup check)
    allowed_ips: tuple = ()
    allow_all_clients: bool = False
    # Default BANDWIDTH (bits/sec) advertised in the master playlist when a
    # channel has no explicit value. 0 = do not emit a master wrapper.
    default_bandwidth: int = 0
    # Optional upstream M3U playlist: every #EXTINF entry becomes a literal-mode
    # channel, so /playlist.m3u carries DEFAULT_BANDWIDTH on each stream.
    upstream_m3u_url: str = ""
    # Further upstream M3U lists, merged after upstream_m3u_url (first source
    # wins when two lists carry the same stream URL).
    upstream_m3u_urls: tuple = ()
    upstream_m3u_max_bytes: int = 10 * 1024 * 1024
    upstream_m3u_referer: str = ""
    upstream_m3u_ttl: int = 3600
    # Allow-lists applied to upstream M3U entries (not channels.conf). Empty
    # allows everything; an entry lacking the attribute a filter needs is kept.
    filter_groups: tuple = ()
    filter_countries: tuple = ()
    filter_languages: tuple = ()
    # Extra hostnames /proxy may fetch, on top of those learned from sources.
    extra_allowed_hosts: tuple = ()
    # UNSAFE, tests only, never read from the environment: lets upstreams
    # resolve to loopback/private addresses so a local fake upstream works.
    allow_private_upstreams: bool = False

    def m3u_sources(self):
        """All upstream M3U URLs, old single-URL setting first, no repeats."""
        out = []
        for u in ((self.upstream_m3u_url,) if self.upstream_m3u_url else ()) + tuple(self.upstream_m3u_urls):
            if u not in out:
                out.append(u)
        return out

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        cache_ttl = int(env.get("HLS_CACHE_TTL", "3600"))
        return cls(
            port=int(env.get("HLS_PROXY_PORT", "8089")),
            bind_addr=env.get("HLS_PROXY_BIND", "127.0.0.1"),
            upstream_ua=env.get("HLS_PROXY_UA", DEFAULT_UA),
            upstream_referer=env.get("HLS_PROXY_REFERER", ""),
            channels_conf=env.get("CHANNELS_CONF", ""),
            cache_ttl=cache_ttl,
            allowed_ips=_parse_client_networks(env.get("HLS_ALLOWED_IPS", "")),
            allow_all_clients=env.get("HLS_ALLOW_ALL_CLIENTS", "") == "1",
            default_bandwidth=int(env.get("HLS_DEFAULT_BANDWIDTH", "0")) or 0,
            upstream_m3u_url=env.get("HLS_UPSTREAM_M3U_URL", ""),
            upstream_m3u_urls=_split_list(env.get("HLS_UPSTREAM_M3U_URLS", "")),
            upstream_m3u_max_bytes=int(env.get("HLS_UPSTREAM_M3U_MAX_BYTES", str(10 * 1024 * 1024))),
            upstream_m3u_referer=env.get("HLS_UPSTREAM_M3U_REFERER", ""),
            upstream_m3u_ttl=int(env.get("HLS_UPSTREAM_M3U_TTL", str(cache_ttl))),
            filter_groups=_split_filter(env.get("HLS_FILTER_GROUPS", "")),
            filter_countries=_split_filter(env.get("HLS_FILTER_COUNTRIES", "")),
            filter_languages=_split_filter(env.get("HLS_FILTER_LANGUAGES", "")),
        )


def _is_loopback_bind(addr):
    if addr == "localhost":
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _normalize_ip(text):
    """Parse an IP string, unwrapping IPv4-mapped IPv6 (::ffff:a.b.c.d)."""
    ip = ipaddress.ip_address(text.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def _host_of(url):
    """Lower-cased hostname of a URL, or None if it has none."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def _origin_of(url):
    m = re.match(r"https?://[^/]+", url)
    return m.group(0) if m else None


def _slugify(s: str) -> str:
    """Reduce a string to a URL-safe channel slug."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", s).strip("-").lower()
    return s or "channel"


def _attr_values(extinf, name):
    """Values of an #EXTINF attribute, split on ';' and lowercased; None if absent or empty."""
    m = re.search(rf'{re.escape(name)}="([^"]*)"', extinf)
    vals = [v.strip().lower() for v in m.group(1).split(";") if v.strip()] if m else []
    return vals or None


def _entry_countries(extinf):
    vals = _attr_values(extinf, "tvg-country")
    if vals:
        return vals
    # iptv-org style ids: "ChannelName.us" or "ChannelName.us@SD"
    m = re.search(r'tvg-id="[^"]*\.([A-Za-z]{2})(?:@[^"]*)?"', extinf)
    return [m.group(1).lower()] if m else None


def filter_entry(config, extinf):
    """Apply the allow-lists to one upstream entry.

    Returns (keep, unfiltered): unfiltered is True when the entry was kept only
    because it lacks an attribute that an active filter needs.
    """
    unfiltered = False
    for wanted, values in (
        (config.filter_groups, _attr_values(extinf, "group-title")),
        (config.filter_countries, _entry_countries(extinf)),
        (config.filter_languages, _attr_values(extinf, "tvg-language")),
    ):
        if not wanted:
            continue
        if values is None:
            unfiltered = True
        elif not set(values) & {w.lower() for w in wanted}:
            return False, False
    return True, unfiltered


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-applies the upstream URL policy to every redirect hop."""

    def __init__(self, app):
        self.app = app

    def _check_hop(self, req, headers):
        target = headers.get("location") or headers.get("uri")
        if target:
            # Runs before urllib's own scheme check, so file:/ftp: hops are
            # refused as policy errors rather than generic HTTP errors.
            self.app.check_url(urllib.parse.urljoin(req.full_url, target))

    def http_error_302(self, req, fp, code, msg, headers):
        self._check_hop(req, headers)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.app.check_url(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ProxyApp:
    """Config plus all mutable proxy state. One instance per server."""

    def __init__(self, config):
        self.config = config
        # Guards every shared dict below. Never held across a network fetch.
        self.lock = threading.RLock()
        self.allowed_hosts = set(h.lower() for h in config.extra_allowed_hosts)
        for u in config.m3u_sources():
            self.allow_url_host(u)
        # Cache: slug -> {m3u8_url, embed_host, fetched_at}
        self.channel_cache = {}
        # Maps upstream origin -> referer (learned from /channel/ scrapes + literal mode preload)
        self.referer_map = {}
        # Per-channel declared bandwidth (bits/sec) from channels.conf field 9
        self.channel_bandwidth = {}
        # Per-channel extraction mode from channels.conf field 7 ("iframe" | "direct" | "literal")
        self.channel_mode = {}
        # Per-channel declared referer from channels.conf field 8 (used by literal mode)
        self.channel_referer = {}
        # Per-channel declared User-Agent (upstream M3U #EXTVLCOPT or similar)
        self.channel_ua = {}
        # Merged upstream M3U entries: list of (slug, extinf_line, url)
        self.upstream_m3u = []
        # slug -> (referer, user_agent) from #EXTVLCOPT lines, "" when absent
        self.m3u_opts = {}
        # Per-source state: url -> {"entries": [(extinf, url, referer, ua)], "fetched_at": float}
        self.m3u_sources = {}
        # Per-source filter counts, in source order: [{"loaded", "kept", "dropped", "unfiltered"}]
        self.m3u_stats = []
        # Per-channel #EXTINF line built from channels.conf (slug -> extinf)
        self.channel_extinf = {}
        # slug -> source_url as last read from channels.conf, to spot changed URLs
        self.conf_urls = {}
        # (inode, mtime_ns, size) of channels.conf at the last read
        self.conf_stamp = None
        self._resolve = socket.getaddrinfo  # overridable seam for offline tests
        self._opener = urllib.request.OpenerDirector()
        # Only http(s): no file:, ftp: or data: handlers are installed.
        for h in (
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPErrorProcessor(),
            _PolicyRedirectHandler(self),
        ):
            self._opener.add_handler(h)

    # ---- upstream URL policy -------------------------------------------

    def allow_url_host(self, url):
        host = _host_of(url)
        if host:
            with self.lock:
                self.allowed_hosts.add(host)

    def check_url(self, url):
        """Raise PolicyError unless url may be fetched. Fetches nothing."""
        try:
            parts = urllib.parse.urlsplit(url)
            host = (parts.hostname or "").lower()
            parts.port  # validates the port
        except ValueError:
            raise PolicyError("Malformed URL")
        if parts.scheme not in ("http", "https"):
            raise PolicyError("Scheme not allowed")
        if not host:
            raise PolicyError("Missing host")
        if parts.username is not None or parts.password is not None:
            raise PolicyError("Userinfo not allowed")
        if host not in self.allowed_hosts:
            raise PolicyError("Unknown upstream host")
        if self.config.allow_private_upstreams:
            return
        try:
            infos = self._resolve(host, parts.port or 80, type=socket.SOCK_STREAM)
        except OSError:
            raise PolicyError("Host does not resolve")
        if not infos:
            raise PolicyError("Host does not resolve")
        for info in infos:
            ip = _normalize_ip(info[4][0])
            if not ip.is_global or ip.is_multicast:
                raise PolicyError("Non-public upstream address")

    def fetch(self, url, headers, timeout=15):
        """Open url through the policy-enforcing opener."""
        self.check_url(url)
        return self._opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)

    def client_allowed(self, client_ip):
        if not self.config.allowed_ips:
            return self.config.allow_all_clients or _is_loopback_bind(self.config.bind_addr)
        try:
            ip = _normalize_ip(client_ip)
        except ValueError:
            return False
        return any(ip.version == net.version and ip in net for net in self.config.allowed_ips)

    # ---- channel loading -----------------------------------------------

    def _fetch_m3u_source(self, url):
        """Fetch and parse one upstream M3U into [(extinf, url, referer, ua)].

        Touches no shared state, so it runs outside the lock.
        """
        cfg = self.config
        limit = cfg.upstream_m3u_max_bytes
        with self.fetch(url, {"User-Agent": cfg.upstream_ua}) as resp:
            raw = resp.read(limit + 1)
        if len(raw) > limit:
            raise ValueError(f"upstream M3U larger than {limit} bytes")
        text = raw.decode("utf-8", errors="replace")

        entries = []
        extinf = None
        referer = ua = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                extinf, referer, ua = line, "", ""
            elif line.upper().startswith("#EXTVLCOPT:"):
                key, _, val = line[len("#EXTVLCOPT:"):].partition("=")
                key = key.strip().lower()
                if key in ("http-referrer", "http-referer"):
                    referer = val.strip()
                elif key == "http-user-agent":
                    ua = val.strip()
            elif line.startswith("#"):
                continue
            elif extinf is not None:
                entries.append((extinf, line, referer, ua))
                extinf = None
                referer = ua = ""
        return entries

    def refresh_upstream_m3u(self) -> None:
        """Fetch every upstream M3U and merge them into literal channels.

        Each source is cached for upstream_m3u_ttl seconds on its own. A source
        that fails (or comes back empty or oversized) keeps its last good copy,
        so a transient error never wipes channels. Sources are merged in order
        (HLS_UPSTREAM_M3U_URL first) and a stream URL appearing in several is
        kept once, from the first source. Fetches happen outside the lock.
        """
        cfg = self.config
        now = time.time()
        fetched = {}
        for url in cfg.m3u_sources():
            with self.lock:
                state = self.m3u_sources.get(url)
                fresh = bool(state and state["entries"]
                             and (now - state["fetched_at"]) < cfg.upstream_m3u_ttl)
            if fresh:
                continue
            try:
                entries = self._fetch_m3u_source(url)
            except Exception as e:
                print(f"[hls-proxy] Failed to fetch upstream M3U: {e}")
                continue
            if entries:
                fetched[url] = entries
        if not fetched:
            return

        with self.lock:
            for url, entries in fetched.items():
                self.m3u_sources[url] = {"entries": entries, "fetched_at": now}
            merged = []
            stats = []
            seen_urls = set()
            seen_slugs = set()
            idx = 0
            for url in cfg.m3u_sources():
                st = {"loaded": 0, "kept": 0, "dropped": 0, "unfiltered": 0}
                stats.append(st)
                for extinf, stream, referer, ua in self.m3u_sources.get(url, {"entries": []})["entries"]:
                    st["loaded"] += 1
                    keep, unfiltered = filter_entry(cfg, extinf)
                    if not keep:
                        st["dropped"] += 1
                        continue
                    st["kept"] += 1
                    st["unfiltered"] += unfiltered
                    if stream in seen_urls:
                        continue
                    seen_urls.add(stream)
                    idx += 1
                    tvg_id = re.search(r'tvg-id="([^"]*)"', extinf)
                    name_match = re.search(r",\s*(.+)$", extinf)
                    if tvg_id and tvg_id.group(1).strip():
                        base = tvg_id.group(1).strip()
                    elif name_match and name_match.group(1).strip():
                        base = name_match.group(1).strip()
                    else:
                        base = f"ch{idx}"
                    slug = orig_slug = _slugify(base)
                    n = 1
                    while slug in seen_slugs:
                        n += 1
                        slug = f"{orig_slug}-{n}"
                    seen_slugs.add(slug)
                    merged.append((slug, extinf, stream))
                    self.m3u_opts[slug] = (referer, ua)
                    # Invalidate stale cache so a refreshed upstream URL replaces the old one
                    self.channel_cache.pop(slug, None)
            self.upstream_m3u = merged
            self.m3u_stats = stats
        print(f"[hls-proxy] Loaded {len(merged)} channels from {len(cfg.m3u_sources())} upstream M3U source(s)")

    def load_channels(self):
        """Load channel config: slug -> source_page_url."""
        with self.lock:
            channels = self._read_conf()
        # The upstream M3U fetch happens outside the lock.
        self.refresh_upstream_m3u()
        cfg = self.config
        with self.lock:
            # File entries win on slug collisions so a channel can be overridden by hand.
            for slug, _extinf, url in self.upstream_m3u:
                if slug in channels:
                    continue
                channels[slug] = url
                referer, ua = self.m3u_opts.get(slug, ("", ""))
                referer = referer or cfg.upstream_m3u_referer
                self.channel_mode[slug] = "literal"
                if referer:
                    self.channel_referer[slug] = referer
                else:
                    self.channel_referer.pop(slug, None)
                if ua:
                    self.channel_ua[slug] = ua
                else:
                    self.channel_ua.pop(slug, None)
                origin = _origin_of(url)
                if origin:
                    self.referer_map.setdefault(origin, cfg.upstream_m3u_referer)
                    self.allow_url_host(url)
        return channels

    def _conf_path(self):
        conf = self.config.channels_conf
        if not conf:
            for p in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.conf"), "/etc/hls-proxy/channels.conf"]:
                if os.path.exists(p):
                    conf = p
                    break
        return conf if conf and os.path.exists(conf) else None

    def _conf_changed(self):
        """True when channels.conf is not the file last read (atomic rename included)."""
        conf = self._conf_path()
        if not conf:
            return False
        st = os.stat(conf)
        with self.lock:
            return self.conf_stamp != (st.st_ino, st.st_mtime_ns, st.st_size)

    def _read_conf(self):
        """Parse channels.conf into shared state. Caller holds self.lock."""
        channels = {}
        conf = self._conf_path()
        if conf:
            st = os.stat(conf)
            self.conf_stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
            with open(conf) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 6:
                        slug = parts[0]
                        name = parts[1].strip() if len(parts) >= 2 else slug
                        chno = parts[2].strip() if len(parts) >= 3 else ""
                        logo = parts[3].strip() if len(parts) >= 4 else ""
                        group = parts[4].strip() if len(parts) >= 5 else ""
                        source_url = parts[5]
                        channels[slug] = source_url
                        # A changed URL must not keep serving the cached one.
                        if self.conf_urls.get(slug, source_url) != source_url:
                            self.channel_cache.pop(slug, None)
                        self.conf_urls[slug] = source_url
                        self.allow_url_host(source_url)
                        tvg_id = parts[9].strip() if len(parts) >= 10 and parts[9].strip() else slug
                        extinf_attrs = [f'tvg-id="{tvg_id}"']
                        if chno:
                            extinf_attrs.append(f'tvg-chno="{chno}"')
                        if logo:
                            extinf_attrs.append(f'tvg-logo="{logo}"')
                        if group:
                            extinf_attrs.append(f'group-title="{group}"')
                        self.channel_extinf[slug] = f'#EXTINF:-1 {" ".join(extinf_attrs)},{name or slug}'
                        mode = parts[6].strip().lower() if len(parts) >= 7 and parts[6].strip() else "iframe"
                        referer = parts[7].strip() if len(parts) >= 8 and parts[7].strip() else ""
                        self.channel_mode[slug] = mode
                        if referer:
                            self.channel_referer[slug] = referer
                        else:
                            self.channel_referer.pop(slug, None)
                        # Optional 11th field: User-Agent for this channel
                        ua = parts[10].strip() if len(parts) >= 11 else ""
                        if ua:
                            self.channel_ua[slug] = ua
                        else:
                            self.channel_ua.pop(slug, None)
                        # Literal mode: source_url IS the m3u8. Pre-seed referer_map so
                        # /proxy?url=... requests for this upstream host use the right
                        # Referer without needing a successful /channel/ scrape first.
                        if mode == "literal" and referer:
                            origin = _origin_of(source_url)
                            if origin:
                                self.referer_map[origin] = referer
                        # Optional 9th field: declared BANDWIDTH in bits/sec
                        if len(parts) >= 9 and parts[8].strip():
                            try:
                                self.channel_bandwidth[slug] = int(parts[8].strip())
                            except ValueError:
                                pass

        return channels

    def scrape_m3u8(self, source_url):
        """Scrape a source page to get the fresh m3u8 URL and embed host."""
        ua = self.config.upstream_ua
        # Step 1: get iframe
        with self.fetch(source_url, {"User-Agent": ua}) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        iframe = ""
        for line in html.splitlines():
            m = re.search(r'iframe\s+src="([^"]+)"', line)
            if m:
                iframe = m.group(1)
                break

        if not iframe:
            return None, None

        embed_host = _origin_of(iframe) or ""

        # Step 2: get m3u8 from embed page. The iframe comes from a configured
        # page, so its host (and the m3u8's host below) join the allowed set.
        self.allow_url_host(iframe)
        page_origin = _origin_of(source_url)
        referer = (page_origin + "/") if page_origin else ""
        with self.fetch(iframe, {"User-Agent": ua, "Referer": referer}) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        m3u8 = ""
        for m in re.finditer(r"https?://[^\"'\s]+\.m3u8[^\"'\s]*", html):
            m3u8 = m.group(0)
            break
        if m3u8:
            self.allow_url_host(m3u8)

        return m3u8, embed_host

    def channel_headers(self, slug, embed_host=""):
        """Upstream request headers for a channel: per-channel, then global."""
        cfg = self.config
        with self.lock:
            ua = self.channel_ua.get(slug)
            referer = self.channel_referer.get(slug)
            literal = self.channel_mode.get(slug) == "literal"
        headers = {"User-Agent": ua or cfg.upstream_ua}
        if not referer or not literal:
            referer = (embed_host + "/") if embed_host else (referer or cfg.upstream_referer)
        if referer:
            headers["Referer"] = referer
        return headers

    def get_channel_m3u8(self, slug):
        """Get a fresh m3u8 URL for a channel, using cache if still valid."""
        now = time.time()
        # Pick up a rewritten channels.conf before trusting the cache.
        if self._conf_changed():
            with self.lock:
                self._read_conf()
        with self.lock:
            cached = self.channel_cache.get(slug)
        if cached and (now - cached["fetched_at"]) < self.config.cache_ttl:
            return cached["m3u8_url"], cached["embed_host"]

        channels = self.load_channels()
        source_url = channels.get(slug)
        if not source_url:
            return None, None

        # Literal mode: source_url is the m3u8 itself; skip scraping. Treat the
        # declared referer as the "embed host" proxy so downstream /proxy calls use it.
        with self.lock:
            literal = self.channel_mode.get(slug) == "literal"
            referer = self.channel_referer.get(slug, "")
        if literal:
            embed_host = referer.rstrip("/") if referer else ""
            with self.lock:
                self.channel_cache[slug] = {"m3u8_url": source_url, "embed_host": embed_host, "fetched_at": now}
            return source_url, embed_host

        m3u8, embed_host = self.scrape_m3u8(source_url)
        if m3u8:
            # Learn the referer for this upstream host so /proxy requests use it
            origin = _origin_of(m3u8)
            with self.lock:
                self.channel_cache[slug] = {"m3u8_url": m3u8, "embed_host": embed_host, "fetched_at": now}
                if origin and embed_host:
                    self.referer_map[origin] = embed_host + "/"
        return m3u8, embed_host


class HLSProxyHandler(http.server.BaseHTTPRequestHandler):
    @property
    def app(self):
        return self.server.app

    # Advertise HTTP/1.1 so clients (e.g. .NET HttpClient in Jellyfin) get
    # proper response framing. Combined with explicit Content-Length and
    # Connection: close below, this avoids "Connection reset by peer"
    # errors on clients that expect a well-framed HTTP/1.1 response.
    protocol_version = "HTTP/1.1"

    def _write_body(self, status: int, content_type: str, body: bytes, extra_headers=None) -> None:
        """Send a fully-buffered response with Content-Length and Connection: close.

        Also swallows BrokenPipeError / ConnectionResetError when the client
        disconnects mid-write — these are expected for players and bots that
        probe the playlist and close early; they are not proxy bugs.
        """
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for key, value in (extra_headers or []):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        # IP allowlist check
        parsed = urllib.parse.urlparse(self.path)

        # Answered for any client so the container HEALTHCHECK (from loopback)
        # keeps working when HLS_ALLOWED_IPS only lists the Docker subnet.
        if parsed.path == "/health":
            self._write_body(200, "text/plain", b"ok")
            return

        if not self.app.client_allowed(self.client_address[0]):
            self.send_error(403, "Forbidden")
            return

        if parsed.path == "/":
            self._handle_index()
            return

        if parsed.path == "/playlist.m3u":
            self._handle_playlist()
            return

        # /channel/<slug>              — master playlist (if bandwidth declared)
        # /channel/<slug>/media.m3u8   — underlying media playlist
        if parsed.path.startswith("/channel/"):
            tail = parsed.path[len("/channel/"):].strip("/").split("/", 1)
            slug = tail[0]
            is_media_request = len(tail) > 1 and tail[1] == "media.m3u8"
            self._handle_channel(slug, is_media_request=is_media_request)
            return

        # Accept /proxy and /proxy.<ext>. The extension suffix is appended when
        # rewriting segment URLs so ffmpeg's HLS demuxer accepts them under its
        # allowed_segment_extensions whitelist (default: ts,m4s,mp4,aac,m3u8,…).
        if not (parsed.path == "/proxy" or parsed.path.startswith("/proxy.")):
            self.send_error(404)
            return

        params = urllib.parse.parse_qs(parsed.query)
        upstream_url = params.get("url", [None])[0]

        if not upstream_url:
            self.send_error(400, "Missing ?url= parameter")
            return

        try:
            # One policy for every upstream fetch: http(s) only, allowed host,
            # public addresses. Refusals happen before anything is fetched.
            self.app.check_url(upstream_url)

            # Use learned referer from /channel/ scrapes, fall back to env var
            cfg = self.app.config
            referer = cfg.upstream_referer
            upstream_host = _origin_of(upstream_url)
            if upstream_host and upstream_host in self.app.referer_map:
                referer = self.app.referer_map[upstream_host]

            headers = {"User-Agent": cfg.upstream_ua}
            if referer:
                headers["Referer"] = referer
            # ?ch=<slug> marks URLs rewritten for a channel with its own headers
            ch = params.get("ch", [None])[0]
            with self.app.lock:
                if ch in self.app.channel_referer:
                    headers["Referer"] = self.app.channel_referer[ch]
                if ch in self.app.channel_ua:
                    headers["User-Agent"] = self.app.channel_ua[ch]

            resp = self.app.fetch(upstream_url, headers)
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            is_playlist = upstream_url.endswith(".m3u8") or "mpegurl" in content_type.lower()

            if is_playlist:
                # Playlists are small — read fully to rewrite URLs
                content = resp.read()
                resp.close()
                if b"#EXTM3U" in content:
                    content = self._rewrite_playlist(content, upstream_url, ch)
                    content_type = "application/vnd.apple.mpegurl"
                self._write_body(200, content_type, content, [
                    ("Access-Control-Allow-Origin", "*"),
                    ("Cache-Control", "no-cache"),
                ])
            else:
                # Segments (.ts) — stream chunk-by-chunk, never buffer fully.
                # Content-Length comes from upstream when known so clients
                # don't rely on connection-close for EOF.
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                cl = resp.headers.get("Content-Length")
                if cl:
                    self.send_header("Content-Length", cl)
                self.end_headers()
                self._stream_body(resp)

        except PolicyError as e:
            self.send_error(403, str(e))
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")

    def _stream_body(self, resp):
        """Copy resp to the client after headers are sent.

        Any failure here (client gone, upstream died) just closes the
        connection: a second status line would corrupt the response.
        """
        self.close_connection = True
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except Exception:
            pass
        finally:
            resp.close()

    def _handle_index(self):
        """Plain-text landing page describing available endpoints."""
        cfg = self.app.config
        host = self.headers.get("Host", f"{cfg.bind_addr}:{cfg.port}").split("/", 1)[0]
        upstream = "configured" if cfg.m3u_sources() else "not set (HLS_UPSTREAM_M3U_URLS)"
        channels_loaded = len(self.app.upstream_m3u)
        with self.app.lock:
            stats = list(self.app.m3u_stats)
        per_source = "".join(
            f"  source {i}: loaded {st['loaded']}, kept {st['kept']}, "
            f"dropped {st['dropped']}, unfiltered {st['unfiltered']}\n"
            for i, st in enumerate(stats, 1))
        body = (
            f"hls-restream-proxy\n"
            f"==================\n\n"
            f"Upstream M3U:   {upstream}\n"
            f"Channels loaded: {channels_loaded}\n"
            f"{per_source}\n"
            f"Endpoints:\n"
            f"  http://{host}/playlist.m3u           — M3U for your media player\n"
            f"  http://{host}/channel/<slug>         — single channel (master playlist)\n"
            f"  http://{host}/channel/<slug>/media.m3u8\n"
            f"  http://{host}/health                 — health check\n"
        ).encode("utf-8")
        self._write_body(200, "text/plain; charset=utf-8", body)

    def _handle_playlist(self):
        """Emit a rewritten M3U pointing at /channel/<slug> for each upstream entry.

        Used when HLS_UPSTREAM_M3U_URL is configured — hand this URL to the
        media player so every stream is proxied and receives the declared
        BANDWIDTH wrapper.
        """
        # Refresh both sources so the playlist reflects current state
        channels = self.app.load_channels()
        cfg = self.app.config
        host_header = self.headers.get("Host", f"{cfg.bind_addr}:{cfg.port}")
        # Strip any path/query an attacker might smuggle via Host (defense in depth)
        host_header = host_header.split("/", 1)[0]
        lines = ["#EXTM3U"]
        emitted = set()

        # channels.conf entries first (user-curated order)
        for slug in channels:
            if slug in emitted or slug not in self.app.channel_extinf:
                continue
            lines.append(self.app.channel_extinf[slug])
            lines.append(f"http://{host_header}/channel/{slug}")
            emitted.add(slug)

        # Then upstream-M3U entries that weren't overridden by channels.conf
        for slug, extinf, _url in self.app.upstream_m3u:
            if slug in emitted:
                continue
            lines.append(extinf)
            lines.append(f"http://{host_header}/channel/{slug}")
            emitted.add(slug)

        if not emitted:
            self.send_error(
                404,
                "No channels configured (set HLS_UPSTREAM_M3U_URLS or populate channels.conf)",
            )
            return

        body = ("\n".join(lines) + "\n").encode("utf-8")
        # audio/x-mpegurl marks this as a playlist (list of channels), not an
        # HLS media playlist. application/vnd.apple.mpegurl triggers browsers
        # and VLC to try single-stream HLS playback instead of parsing entries.
        self._write_body(200, "audio/x-mpegurl", body, [
            ("Content-Disposition", 'attachment; filename="playlist.m3u"'),
            ("Access-Control-Allow-Origin", "*"),
            ("Cache-Control", "no-cache"),
        ])

    def _handle_channel(self, slug, is_media_request: bool = False):
        """Resolve a channel's upstream and proxy it.

        The upstream can be either HLS (a .m3u8 playlist) or a direct stream
        (MPEG-TS, MP4, etc.). This is detected from the upstream response's
        Content-Type and URL path, then handled accordingly:

        - HLS: read the playlist, optionally wrap in a thin master carrying
          a BANDWIDTH tag (so Jellyfin does not assume its ~20 Mbps default),
          and rewrite segment URLs to route through /proxy.
        - Direct stream: pass through chunk-by-chunk. BANDWIDTH wrapping is
          skipped — it is an HLS-only concept and would break MPEG-TS.
        """
        try:
            m3u8_url, embed_host = self.app.get_channel_m3u8(slug)
        except PolicyError as e:
            self.send_error(403, str(e))
            return
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")
            return
        if not m3u8_url:
            self.send_error(404, f"Channel not found or scrape failed: {slug}")
            return

        try:
            resp = self.app.fetch(m3u8_url, self.app.channel_headers(slug, embed_host))
            upstream_ct = resp.headers.get("Content-Type", "") or ""
            path = m3u8_url.split("?", 1)[0].rstrip("/")
            is_hls = "mpegurl" in upstream_ct.lower() or path.endswith(".m3u8")

            if not is_hls:
                # Direct stream (Dispatcharr MPEG-TS, ErsatzTV, raw MP4, …).
                # Don't buffer — these broadcasts never end. Stream through.
                self.send_response(200)
                self.send_header("Content-Type", upstream_ct or "video/mp2t")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                cl = resp.headers.get("Content-Length")
                if cl:
                    self.send_header("Content-Length", cl)
                self.end_headers()
                self._stream_body(resp)
                return

            content = resp.read()
            resp.close()
            bandwidth = self.app.channel_bandwidth.get(slug) or self.app.config.default_bandwidth
            is_master = b"#EXT-X-STREAM-INF" in content

            if bandwidth and not is_master and not is_media_request:
                master = (
                    "#EXTM3U\n"
                    f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth}\n"
                    f"/channel/{slug}/media.m3u8\n"
                ).encode("utf-8")
                self._write_body(200, "application/vnd.apple.mpegurl", master, [
                    ("Access-Control-Allow-Origin", "*"),
                    ("Cache-Control", "no-cache"),
                ])
                return

            content = self._rewrite_playlist(content, m3u8_url, slug)
            if bandwidth and is_master:
                content = self._override_master_bandwidth(content, bandwidth)
            self._write_body(200, "application/vnd.apple.mpegurl", content, [
                ("Access-Control-Allow-Origin", "*"),
                ("Cache-Control", "no-cache"),
            ])

        except PolicyError as e:
            self.send_error(403, str(e))
        except Exception as e:
            self.send_error(502, f"Upstream error: {e}")

    def _override_master_bandwidth(self, content: bytes, bandwidth: int) -> bytes:
        """Pin BANDWIDTH on every variant of an upstream master playlist.

        Some upstreams ship a master that omits BANDWIDTH or advertises a value
        Jellyfin treats as its ~20 Mbps default — which forces transcoding on
        bandwidth-limited clients. Rewrites every #EXT-X-STREAM-INF (and any
        AVERAGE-BANDWIDTH) so the configured value wins.
        """
        text = content.decode("utf-8", errors="replace")
        out = []
        for line in text.splitlines():
            if line.startswith("#EXT-X-STREAM-INF"):
                if re.search(r"BANDWIDTH=\d+", line):
                    line = re.sub(r"BANDWIDTH=\d+", f"BANDWIDTH={bandwidth}", line)
                else:
                    line = line.replace(
                        "#EXT-X-STREAM-INF:",
                        f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},",
                        1,
                    )
                line = re.sub(
                    r"AVERAGE-BANDWIDTH=\d+", f"AVERAGE-BANDWIDTH={bandwidth}", line
                )
            out.append(line)
        return "\n".join(out).encode("utf-8")

    def _rewrite_playlist(self, content: bytes, playlist_url: str, ch=None) -> bytes:
        """Rewrite URLs in m3u8 playlists to route through this proxy."""
        text = content.decode("utf-8", errors="replace")
        base_url = playlist_url.rsplit("/", 1)[0] + "/"
        lines = text.splitlines()
        result = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                if "URI=" in line:
                    line = re.sub(
                        r'URI="([^"]+)"',
                        lambda m: f'URI="{self._proxy_url(m.group(1), base_url, ch)}"',
                        line,
                    )
                result.append(line)
            else:
                result.append(self._proxy_url(line, base_url, ch))

        return "\n".join(result).encode("utf-8")

    def _proxy_url(self, url: str, base_url: str, ch=None) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            full = url
        else:
            full = base_url + url
        # The playlist came from an allowed host, so the hosts it references
        # (CDNs for variants and segments) become allowed too.
        self.app.allow_url_host(full)
        # Mirror the upstream file extension onto the proxy path. FFmpeg's HLS
        # demuxer checks the path (not the query) against allowed_segment_extensions,
        # so /proxy?url=... gets rejected while /proxy.ts?url=... is accepted.
        upstream_path = urllib.parse.urlparse(full).path
        ext_match = re.search(r"\.([A-Za-z0-9]{1,5})$", upstream_path)
        ext = f".{ext_match.group(1).lower()}" if ext_match else ".ts"
        out = f"/proxy{ext}?url={urllib.parse.quote(full, safe='')}"
        with self.app.lock:
            has_opts = ch in self.app.channel_referer or ch in self.app.channel_ua
        if ch and has_opts:
            out += "&ch=" + urllib.parse.quote(ch, safe="")
        return out

    def log_message(self, format, *args):
        if args and "200" not in str(args[0]) and "206" not in str(args[0]):
            super().log_message(format, *args)


def build_server(config):
    """Build (but don't start) an HTTP server bound to config.

    Raises ConfigError for an unsafe configuration: a non-loopback bind with
    no client allowlist, unless allow_all_clients is set.
    """
    if (
        not config.allowed_ips
        and not config.allow_all_clients
        and not _is_loopback_bind(config.bind_addr)
    ):
        raise ConfigError(
            f"Refusing to listen on {config.bind_addr} with no client allowlist. "
            "Set HLS_ALLOWED_IPS (IPs/CIDRs), or HLS_ALLOW_ALL_CLIENTS=1 to opt out."
        )
    app = ProxyApp(config)
    server = http.server.ThreadingHTTPServer((config.bind_addr, config.port), HLSProxyHandler)
    server.daemon_threads = True
    server.app = app
    return server


def main():
    try:
        config = Config.from_env()
        server = build_server(config)
    except ConfigError as e:
        print(f"[hls-proxy] ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    # Preload channels (including upstream M3U) so /playlist.m3u works on first request.
    server.app.load_channels()
    print(f"[hls-proxy] Listening on {config.bind_addr}:{config.port}")
    if config.allowed_ips:
        print(f"[hls-proxy] Allowed IPs: {', '.join(str(n) for n in config.allowed_ips)}")
    elif config.allow_all_clients:
        print("[hls-proxy] WARNING: HLS_ALLOW_ALL_CLIENTS=1 and no HLS_ALLOWED_IPS. All clients accepted.")
    if config.m3u_sources():
        print(f"[hls-proxy] Upstream M3U sources: {len(config.m3u_sources())} (refresh every {config.upstream_m3u_ttl}s)")
    print("[hls-proxy] Endpoints:")
    print("  /proxy?url=<encoded_url>       — proxy with headers")
    print("  /channel/<slug>                — auto-resolve fresh m3u8 (master if bandwidth set)")
    print("  /channel/<slug>/media.m3u8     — underlying media playlist")
    print("  /playlist.m3u                  — rewritten M3U from upstream (if configured)")
    print("  /health                        — health check")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[hls-proxy] Stopped")
        server.server_close()


if __name__ == "__main__":
    main()
