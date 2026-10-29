# hls-restream-proxy (Fazorath fork)

Stdlib-only Python HLS proxy that injects headers for free live streams. Single file: `hls-proxy.py`.

## Source hard line
Allowed sources only: iptv-org/iptv category lists and guide data, free ad-supported TV services (Pluto, Samsung TV Plus and similar), and broadcasters' own official free live streams.
Not allowed: pirate embed or restream sites, paid channels taken from third-party sites, getting around DRM, or defeating token systems that keep paying customers in.
No committed config, example, test fixture or doc may name or point at a disallowed source. This repo is public: no hostnames, IPs, tailnet names or household details either.

## Tests
- Run: `python3 -m unittest discover -s tests -t .` (stdlib only, offline, no new dependencies).
- Build servers with `build_server(Config(...))` on port 0 via `tests/helpers.py` (`RunningProxy`, `FakeUpstream`); talk to them over real HTTP and assert on status, bodies and what the fake upstream received.
- Every refusal test must also assert the fake upstream saw no request.
- Fake upstreams are on loopback, so tests set `allow_private_upstreams=True` (UNSAFE, test-only, never read from the environment) except the private-address tests.

## Security rules (don't weaken)
- All upstream fetches go through `ProxyApp.fetch` / `check_url`: http(s) only, host in the allowed set (empty set allows nothing), public addresses only, re-checked on every redirect hop.
- A non-loopback bind needs `HLS_ALLOWED_IPS` or an explicit `HLS_ALLOW_ALL_CLIENTS=1`.

## Git
- PRs target `Fazorath/hls-restream-proxy` `main` only. Never open a PR against upstream `pcruz1905`.
- Never commit a real `channels.conf` (it is gitignored); use `channels.conf.example`.
