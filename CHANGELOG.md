# Changelog

All notable changes to XMRDP are documented here.

## [Unreleased]

### Added
- `master.bind_host` config option — controls what interface the C2 server
  listens on, independent of `master.host` (the advertise address workers
  connect to). Defaults to `master.host` so existing configs are unaffected.
  Set to `0.0.0.0` on multi-homed masters to accept connections on all
  interfaces.
- Worker registry persistence — the C2 server now loads and saves
  `~/.xmrdp/workers.json` on startup and after every registration/heartbeat,
  so a C2 restart no longer loses worker state.

### Documentation
- README Security Model: documented SHA256 (not GPG) binary verification with
  explicit mitigation rationale and links to upstream signing keys.
- README Security Model: documented wallet-address-in-process-list limitation
  (public address only; no private keys involved).
- README Key Design Choices: added `master.host` vs `master.bind_host`
  explanation.

## [0.1.0] — 2026-03-15

Initial public release.

### Features
- Interactive setup wizard (`xmrdp setup`) — downloads monerod, p2pool, and
  xmrig from GitHub with SHA256 verification; generates `cluster.toml` with
  a random API token and optional self-signed TLS certificate.
- Master node deployment (`xmrdp start master`) — starts monerod, p2pool,
  xmrig, and C2 telemetry server in the correct sequence, each gated on the
  previous service being ready.
- Worker node deployment (`xmrdp start worker`) — registers with master,
  starts xmrig using local config, runs a background heartbeat loop reporting
  hashrate and uptime.
- Config sync (`xmrdp sync`) — pushes `cluster.toml` to workers over SSH/SCP
  with per-worker `self = true` injection; supports `--restart` and
  `--dry-run`.
- Cluster status (`xmrdp status`) — queries C2 API for aggregate hashrate and
  per-worker status; falls back to local process checks when master is
  unreachable.
- Binary update (`xmrdp update`) — checks for newer releases, downloads and
  verifies, performs rolling restart.
- Firewall rule generation (`xmrdp firewall`) — prints platform-specific rules
  (ufw, iptables, netsh, pf); never applied automatically.
- C2 telemetry API — worker registration, heartbeat, and cluster status
  endpoints; Bearer token auth with constant-time comparison and per-IP rate
  limiting; optional TLS with in-band cert fingerprint pinning; structured
  audit logging.

### Security hardening (vs. initial prototype)
- Subprocess injection prevention via `extra_args` allowlist (`_SAFE_ARG_RE`)
- Zip Slip mitigation on archive extraction
- Path traversal containment on C2 binary endpoint
- Config files and data directories created with mode 0600/0700
- C2 HTTP request body size cap (64 KB)
- Negative `Content-Length` rejection
- ZMQ port bound to `127.0.0.1` (not `0.0.0.0`)
- TOML injection prevention via `_toml_str()` escaping
- Per-IP auth failure rate limiting (10 failures / 60 s → HTTP 429)
- Worker identity binding — heartbeats from unexpected IPs return HTTP 403
- Structured audit log (`xmrdp.audit` logger) for all security events
- GitHub binary download size cap (2 GB) with streaming byte-count enforcement
- `config --show` redacts `api_token` and `http_token`
- `master.host` SSRF prevention via `_HOST_RE` allowlist
- PID file created with `O_EXCL` to prevent TOCTOU
- Binary SHA-256 header on C2 binary downloads; client verifies before use
- Worker name allowlist (`_WORKER_NAME_RE`)
- Auth failure dict bounded to 10 000 IPs with stale-entry eviction
- TLS cert fingerprint verified on every connection — no caching
- xmrig `donate-level: 0`
- xmrig HTTP API token round-tripped through all config/wizard/C2 paths
- Lazy 24-hour eviction of stale workers from in-memory registry
- `GITHUB_TOKEN` environment variable read for authenticated GitHub API calls
- CI: SHA-pinned GitHub Actions; Bandit static analysis; pip-audit
  dependency scan
