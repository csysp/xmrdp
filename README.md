# <p align="center"> xmrdp </p>

Deploy a Monero mining cluster on P2Pool mini in minutes. No private keys on any node. Payouts go directly to your wallet via P2Pool coinbase transactions.

## Quick Start

```bash
pip install .
xmrdp setup          # Download binaries + interactive config wizard
xmrdp start master   # Start master node (monerod + p2pool + xmrig + C2)
```

On each worker:

```bash
# On master — push config to the worker after setup
xmrdp sync --worker worker-1

# On worker
xmrdp setup --skip-binaries   # or full setup to download xmrig
xmrdp start worker
```

---

## How It Works

One **master node** runs monerod, p2pool, and xmrig, and hosts a lightweight C2 telemetry server. Any number of **worker nodes** run xmrig only, connecting to the master's p2pool stratum port. Each node uses a local copy of `cluster.toml` as its source of truth — you push config updates to workers explicitly with `xmrdp sync`.

```
Monero Network
      |
   monerod           (syncs blockchain, provides RPC/ZMQ)
      |
   p2pool            (sidechain; pays directly to your wallet)
      | :3333 stratum
   +-- xmrig (master)
   +-- xmrig (worker-1)
   +-- xmrig (worker-2)  ...

C2 server :7099       (telemetry bus only — registration + heartbeats)
```

---

## Configuration

XMRDP uses a single `cluster.toml` file. Run `xmrdp setup` to generate it interactively, or:

```bash
xmrdp config --generate    # Write a default config to ~/.xmrdp/config/cluster.toml
xmrdp config --validate    # Validate the current config
xmrdp config --show        # Print current config (sensitive fields redacted)
```

The config file is written with mode `0600` (owner-readable only). It contains the wallet address, API token, xmrig HTTP token, and per-worker entries. Workers identify themselves by the `self = true` field set in their local copy of the config; if that field is absent, xmrdp falls back to `socket.gethostname()`.

---

## Commands

| Command | Description |
|---------|-------------|
| `xmrdp setup [--skip-binaries]` | Download binaries and run the config wizard |
| `xmrdp start master\|worker` | Start services for the given role |
| `xmrdp stop master\|worker` | Stop services |
| `xmrdp status` | Show cluster status (queries C2; falls back to local process checks) |
| `xmrdp sync [OPTIONS]` | Push cluster.toml to workers via SSH/SCP |
| `xmrdp update [--check-only]` | Check for and apply binary updates from GitHub |
| `xmrdp logs [service] [-n N]` | Tail logs for a service (monerod, p2pool, xmrig, c2) |
| `xmrdp firewall master\|worker` | Print firewall rules for the given role |
| `xmrdp config` | Manage cluster configuration |

### xmrdp sync

Pushes `cluster.toml` from the master to one or more workers over SSH/SCP. For each target worker, xmrdp generates a per-worker config that is identical to the master's config except that the matching `[[workers]]` entry has `self = true` added. The generated file is copied to `~/.xmrdp/config/cluster.toml` on the remote machine with permissions set to `0600`.

```bash
xmrdp sync                            # Sync all workers defined in cluster.toml
xmrdp sync --worker worker-1          # Sync one specific worker
xmrdp sync --worker worker-1 worker-2 # Sync multiple workers
xmrdp sync --ssh-user deploy          # Use a specific SSH username
xmrdp sync --dry-run                  # Show what would be synced, no changes made
xmrdp sync --dry-run -v               # Also print the generated TOML for each worker
xmrdp sync --restart                  # Restart xmrig on each worker after syncing
```

Requires `ssh` and `scp` on PATH. Authentication uses the system SSH agent or default key files — xmrdp never stores or prompts for credentials. Reports per-worker OK/FAILED and exits with code 1 if any worker failed.

---

## Practical Workflow

### 1. Set up the master (once)

```bash
xmrdp setup
```

The wizard asks for your wallet address, master IP, and worker IPs. It generates a 64-char hex API token and an xmrig HTTP token, optionally creates a TLS certificate via `openssl`, downloads monerod, p2pool, and xmrig from GitHub with SHA256 verification, and writes `~/.xmrdp/config/cluster.toml`.

### 2. Start the master

```bash
xmrdp start master
```

Starts services in sequence, each gated on the previous being ready:

1. monerod — waits for RPC port 18081
2. p2pool — waits for stratum port 3333
3. xmrig (mines locally to p2pool)
4. C2 telemetry server on port 7099

### 3. Set up and start each worker

On the **master**, push the config to the worker:

```bash
xmrdp sync --worker worker-1
```

On the **worker machine**:

```bash
xmrdp setup          # Downloads xmrig; wizard only needs wallet + master IP
xmrdp start worker
```

Worker startup sequence:

1. Registers with master (POST /api/register — sends CPU/RAM/platform info)
2. Starts xmrig using the **local** cluster.toml as the source of truth
3. Spawns a background heartbeat thread that reads local xmrig stats every 60 seconds and reports them to the master (POST /api/status)

When you change pool, wallet, or any other config on the master, push the update:

```bash
xmrdp sync --restart    # Push new config and restart xmrig on all workers
```

### 4. Monitor

```bash
xmrdp status              # Per-worker hashrate, uptime, CPU via C2 API
xmrdp logs xmrig -n 100   # Last 100 lines of xmrig log
```

### 5. Update binaries

```bash
xmrdp update
```

Downloads the latest releases from GitHub, verifies SHA256 checksums, and performs a rolling restart: xmrig, then p2pool, then monerod. Workers auto-reconnect to p2pool after the stratum restarts.

### 6. Firewall rules

```bash
xmrdp firewall master
xmrdp firewall worker
```

Prints platform-specific rules (ufw, iptables, netsh, pf). Rules are never applied automatically — you review and apply them.

---

## Key Design Choices

| Choice | Rationale |
|--------|-----------|
| No private keys stored | Only the public wallet address is required; P2Pool pays directly to it via coinbase transactions |
| Config pushed via sync, not pulled | Workers use local `cluster.toml` as the source of truth; `xmrdp sync` pushes updates explicitly — no runtime config fetching from master |
| Binary verification (SHA256) | Every download from GitHub releases is verified before execution; `security.verify_checksums = false` disables this (not recommended) |
| Bearer token + rate limiting | C2 API uses constant-time token comparison; 10 auth failures per 60-second window per IP triggers a 429 response |
| In-band TLS fingerprint check | Worker verifies the C2 server's cert fingerprint against the live connection — no TOCTOU race between probe and request |
| Firewall rules are print-only | The operator must consciously review and apply firewall changes |
| cluster.toml mode 0600 | API tokens and xmrig HTTP token are not readable by other OS users |
| `master.host` vs `master.bind_host` | `host` is the address workers use to reach the master; `bind_host` controls what interface the C2 server listens on (defaults to `host`). Set `bind_host = "0.0.0.0"` on a multi-homed master to accept connections on all interfaces while keeping `host` set to the LAN IP that workers should connect to. |

---

## C2 API Reference

The C2 server runs on the master at port 7099 (configurable via `master.api_port`). All endpoints require a `Authorization: Bearer <token>` header. The token must match `master.api_token` in cluster.toml.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/register` | Worker announces CPU count, RAM, and platform on startup |
| `POST` | `/api/status` | Worker heartbeat — reports hashrate, uptime, cpu_usage every 60s |
| `GET` | `/api/cluster/status` | Aggregate view of all workers; read by `xmrdp status` |

Workers are marked stale after 3 missed heartbeats (180 seconds) and evicted from memory after 24 hours without contact.

---

## Data Directory Layout

```
~/.xmrdp/
├── config/
│   └── cluster.toml       (mode 0600 — API tokens live here)
├── binaries/
│   ├── monero/            (monerod binary + version cache)
│   ├── p2pool/            (p2pool binary)
│   └── xmrig/             (xmrig binary)
├── logs/                  (per-service log files)
├── pids/                  (PID files for process tracking)
└── monerod/               (blockchain data — ~100 GB pruned)
```

---

## Security Model

- No private keys anywhere — only the public wallet address is stored
- SHA256 checksum verification for every binary downloaded from GitHub (see note below)
- API authentication via pre-shared Bearer token (64-char hex, constant-time comparison)
- C2 server is a telemetry bus only — it stores and reports hashrate/uptime data; it cannot push config or execute commands on workers
- Optional TLS on the C2 connection with in-band certificate fingerprint pinning
- Config changes are pushed to workers explicitly via `xmrdp sync` over SSH — workers do not pull from master at runtime
- Firewall rule generation per role; rules are never applied automatically
- No external telemetry — the tool only contacts the GitHub API (for releases) and the Monero/P2Pool p2p networks

### Known limitations

**Binary verification (SHA256, not GPG):** XMRDP verifies every downloaded binary against the SHA256 checksum published in the same GitHub release. It does not perform GPG signature verification. The trust anchor is the GitHub release page itself, served over HTTPS. For additional assurance, manually verify GPG signatures using the keys published by the [Monero project](https://www.getmonero.org/downloads/), [P2Pool](https://github.com/SChernykh/p2pool/releases), and [XMRig](https://github.com/xmrig/xmrig/releases) before running `xmrdp setup`.

**Wallet address visible in process list:** P2Pool requires the wallet address as a command-line argument (`--wallet`). This means it will appear in `ps aux` / Task Manager output on any machine running the master node. The wallet address is a public receive address — not a private key — so this does not expose funds. However, operators who want to keep their wallet address private should be aware of this limitation.

---

## Requirements

- Python 3.8+
- No external runtime dependencies (`tomli` is installed automatically on Python < 3.11; everything else uses stdlib)
- `ssh` and `scp` on PATH (required only for `xmrdp sync`)

## License

MIT
