# XMRDP Installation and Quick-Start Guide

> Monero Mining Cluster Rapid Deployment — v0.1.0 alpha

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/downloads/)

XMRDP automates the setup and operation of a Monero mining cluster running
[monerod](https://github.com/monero-project/monero),
[p2pool](https://github.com/SChernykh/p2pool), and
[xmrig](https://github.com/xmrig/xmrig) across one or many machines.
One master node coordinates; workers connect to it over a private API.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
   - [From source with pip](#21-from-source-with-pip)
   - [Development mode](#22-development-mode)
   - [Isolated install with pipx](#23-isolated-install-with-pipx-recommended-for-end-users)
   - [Verifying the install](#24-verifying-the-install)
3. [Platform notes](#3-platform-notes)
   - [Linux](#31-linux)
   - [macOS](#32-macos)
   - [Windows](#33-windows)
4. [Data and config directories](#4-data-and-config-directories)
5. [Quick start — master node](#5-quick-start--master-node)
6. [Configuration reference](#6-configuration-reference)
7. [Firewall rules](#7-firewall-rules)
8. [Adding worker nodes](#8-adding-worker-nodes)
9. [Stopping services](#9-stopping-services)
10. [Updating binaries](#10-updating-binaries)
11. [Viewing logs](#11-viewing-logs)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Prerequisites

### Required

| Dependency | Minimum version | Notes |
|---|---|---|
| Python | 3.8 | 3.11+ recommended; avoids the `tomli` back-port dependency |
| pip | any current | included with Python; or use `pipx` |

### Conditional

| Dependency | When needed |
|---|---|
| `ssh` and `scp` | `xmrdp sync` (push config to workers) |
| `openssl` | `xmrdp setup` TLS cert generation (optional but recommended) |

### Disk space

| Phase | Approximate size |
|---|---|
| Downloaded binaries (monerod, p2pool, xmrig) | ~300 MB |
| Pruned monerod blockchain | ~50 GB |
| Full monerod blockchain (`prune = false`) | ~200 GB |

Pruning is enabled by default in the generated config. It is recommended for
clusters that do not need to serve the full chain.

---

## 2. Installation

### 2.1 From source with pip

Clone the repository and install:

```bash
git clone https://github.com/csysp/xmrdp.git
cd xmrdp
pip install .
```

On Python 3.8–3.10 pip will automatically install the `tomli` back-port.
Python 3.11+ uses the standard library's `tomllib` and has no extra runtime
dependencies.

### 2.2 Development mode

Install in editable mode with the test suite included:

```bash
pip install -e ".[dev]"
```

This lets you edit source files without reinstalling. Run the tests with:

```bash
pytest
```

### 2.3 Isolated install with pipx (recommended for end users)

[pipx](https://pipx.pypa.io/) installs the tool into its own virtual
environment and puts `xmrdp` on your PATH without touching your system Python:

```bash
pipx install .
```

Or directly from GitHub (once a release is published):

```bash
pipx install git+https://github.com/csysp/xmrdp.git
```

### 2.4 Verifying the install

```bash
xmrdp --version
```

Expected output:

```
xmrdp 0.1.0
```

```bash
xmrdp --help
```

Lists all available subcommands.

---

## 3. Platform notes

### 3.1 Linux

**Supported architectures:** x86_64, aarch64

**SSH/SCP:** Available in most distributions. Install via your package manager
if needed (`openssh-client` on Debian/Ubuntu, `openssh` on Arch/Fedora).

**openssl:** Usually pre-installed. If missing:

```bash
# Debian / Ubuntu
sudo apt install openssl

# Fedora / RHEL
sudo dnf install openssl

# Arch
sudo pacman -S openssl
```

**Firewall:** `xmrdp firewall` outputs both `ufw` and `iptables` commands
on Linux. Apply whichever matches your system. See [section 7](#7-firewall-rules).

**File permissions:** XMRDP enforces `0700` on all data and config directories
and `0600` on `cluster.toml`. Do not change these; the tool will refuse to
read a config file that is world-readable.

---

### 3.2 macOS

**Supported architectures:** x86_64, Apple Silicon (aarch64/arm64)

**SSH/SCP:** Included with macOS via the system OpenSSH installation.

**openssl:** The system `openssl` on macOS is a LibreSSL stub. For TLS cert
generation, install the full version via Homebrew:

```bash
brew install openssl
```

XMRDP searches `/usr/bin/openssl`, `/usr/local/bin/openssl`, and
`/opt/homebrew/bin/openssl` in that order, so Homebrew's version is found
automatically.

**xmrig and huge pages:** xmrig requests huge pages for performance. On macOS
this may trigger a security prompt or silently fail. If xmrig will not start:

1. Open **System Settings > Privacy & Security** and allow the binary.
2. If huge pages cause persistent problems, add `"--no-huge-pages"` to
   `[master.xmrig] extra_args` in your `cluster.toml`.

**Firewall:** `xmrdp firewall` outputs `pf.conf` rules on macOS.
See [section 7](#7-firewall-rules).

**File permissions:** Same as Linux — `0700` directories, `0600` secrets.

---

### 3.3 Windows

**Supported architectures:** x86_64

**Shell:** Use PowerShell or Git Bash. CMD is not tested and may not work
correctly with path handling.

**Python PATH:** After `pip install`, ensure the Python `Scripts` directory
is on your `PATH`. The installer usually offers to do this automatically; if
`xmrdp` is not found after install, add the Scripts directory manually:

```powershell
# Find the Scripts directory
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

Add the printed path to your user `PATH` via **System Properties > Environment
Variables**.

**SSH/SCP:** Two options:

- **Git for Windows** — includes `ssh` and `scp` in the Git Bash environment.
  Download from [git-scm.com](https://git-scm.com/download/win).
- **Windows OpenSSH feature** — enable via **Settings > Optional Features >
  OpenSSH Client**. Available on Windows 10 1809+ and Windows 11.

**openssl:** Two options:

- **Git for Windows** — includes `openssl.exe` at
  `C:\Program Files\Git\usr\bin\openssl.exe`. XMRDP checks this path
  automatically.
- **Standalone installer** — download from
  [slproweb.com/products/Win32OpenSSL.html](https://slproweb.com/products/Win32OpenSSL.html).
  The Win64 installer places the binary at
  `C:\Program Files\OpenSSL-Win64\bin\openssl.exe`, which XMRDP also checks.

**Firewall:** `xmrdp firewall` outputs `netsh advfirewall` commands on
Windows. Run PowerShell as Administrator to apply them.
See [section 7](#7-firewall-rules).

**File permissions:** Windows does not enforce Unix-style permission bits.
XMRDP does not set ACLs on Windows. Protect `cluster.toml` by placing it in a
directory only your user account can access (`%LOCALAPPDATA%\xmrdp\config\` by
default, which satisfies this requirement under standard user account
configurations).

---

## 4. Data and config directories

XMRDP follows platform conventions for storing data and configuration. All
directories are created automatically on first run.

### Data directory (binaries, logs, PIDs)

| Platform | Default path |
|---|---|
| Linux | `$XDG_DATA_HOME/xmrdp` or `~/.xmrdp` |
| macOS | `~/Library/Application Support/xmrdp` |
| Windows | `%LOCALAPPDATA%\xmrdp` or `%USERPROFILE%\.xmrdp` |

Subdirectories created inside the data directory:

| Subdirectory | Contents |
|---|---|
| `binaries/` | monerod, p2pool, xmrig executables |
| `logs/` | service log files |
| `pids/` | PID files for running services |

### Config directory (cluster.toml, TLS certs)

| Platform | Default path |
|---|---|
| Linux | `$XDG_CONFIG_HOME/xmrdp` or `~/.xmrdp/config` |
| macOS | `~/Library/Application Support/xmrdp/config` |
| Windows | `%LOCALAPPDATA%\xmrdp\config` or `%USERPROFILE%\.xmrdp\config` |

`cluster.toml` is written here by `xmrdp setup`. It contains your API token
and is created with mode `0600` on Linux and macOS.

To use a config file in a non-default location, pass `-c` to any command:

```bash
xmrdp -c /path/to/cluster.toml start master
```

---

## 5. Quick start — master node

This section walks through setting up the first machine in a new cluster. It
assumes you have installed XMRDP and have a Monero wallet address ready.
You only need the public address — XMRDP never asks for or stores private keys.

### Step 1: Run the setup wizard

```bash
xmrdp setup
```

The wizard prompts you for:

- Your Monero wallet address (public)
- The role of this machine (`master` or `worker`)
- The master node's IP address or hostname (must be reachable by workers)
- Worker node entries (name + IP, one per line; leave blank when done)

After collecting answers the wizard:

1. Generates a `cluster.toml` with a random API token and xmrig HTTP token.
2. Attempts to generate a self-signed TLS certificate for the C2 API via
   `openssl`. If `openssl` is not found, TLS is skipped and a warning is shown.
   You can enable TLS later by installing `openssl` and re-running `xmrdp setup`.
3. Downloads monerod, p2pool, and xmrig from their official GitHub releases,
   verifying SHA256 checksums before extracting.

To skip binary downloads (config only):

```bash
xmrdp setup --skip-binaries
```

### Step 2: Review and apply firewall rules

```bash
xmrdp firewall master
```

This prints rules for your platform — review them, then apply manually.
XMRDP never modifies your firewall automatically.
See [section 7](#7-firewall-rules) for the full port reference and apply
instructions per platform.

### Step 3: Start the master

```bash
xmrdp start master
```

This starts services in order: monerod, p2pool, xmrig, and the C2 telemetry
API server. Each service is health-checked before the next one starts.

### Step 4: Check status

```bash
xmrdp status
```

Shows which services are running and basic health information.

---

## 6. Configuration reference

### Annotated cluster.toml

The example file at `configs/cluster.example.toml` in the repository root
shows every available option with comments. Key fields are described below.

```toml
[cluster]
name = "my-cluster"
# Your Monero wallet address (public only — no private keys needed)
wallet = "4..."

[master]
# IP or hostname reachable by all workers
host = "192.168.1.100"
# C2 API port for worker coordination
api_port = 7099
# Auth token — auto-generated by setup; treat as a secret
api_token = ""

[master.monerod]
# Prune blockchain to save disk space (~50 GB vs ~200 GB)
prune = true
# Extra CLI args passed directly to monerod
extra_args = []

[master.p2pool]
# Use P2Pool mini sidechain (recommended for < 100 KH/s total hashrate)
mini = true
extra_args = []

[master.xmrig]
# CPU threads (0 = auto-detect all cores)
threads = 0

[[workers]]
name = "worker-1"
host = "192.168.1.101"

[binaries]
# "latest" fetches the newest release. Pin to a tag for reproducibility.
monero_version = "latest"
p2pool_version = "latest"
xmrig_version  = "latest"

[security]
verify_checksums = true
tls_enabled      = false
c2_tls_cert      = ""   # path to PEM certificate file
c2_tls_key       = ""   # path to PEM private key file
# SHA-256 fingerprint of the cert — workers pin this to prevent MITM
c2_tls_fingerprint = ""
```

### Validating your config

```bash
xmrdp config --validate
```

### Printing the active config (secrets redacted)

```bash
xmrdp config --show
```

Sensitive keys (`api_token`, `c2_tls_key`, etc.) are replaced with
`[REDACTED]` in the output.

### Generating a blank config without the wizard

```bash
xmrdp config --generate
```

Writes a default `cluster.toml` to the platform config directory. Edit it
manually, then run `xmrdp setup --skip-binaries` to download binaries using
the values you set.

### TLS for the C2 API

TLS encrypts traffic between workers and the master C2 server and enables
certificate pinning so workers reject connections to unexpected hosts.

To enable TLS after initial setup:

1. Ensure `openssl` is installed.
2. Re-run `xmrdp setup`. The wizard regenerates the certificate and updates
   `cluster.toml` automatically.

Or set the paths manually in `cluster.toml`:

```toml
[security]
tls_enabled        = true
c2_tls_cert        = "/path/to/c2_server.crt"
c2_tls_key         = "/path/to/c2_server.key"
c2_tls_fingerprint = "aabbcc..."   # computed by setup
```

The private key is stored at mode `0600` on Linux and macOS.

### Version pinning

To pin binaries to specific releases for reproducibility:

```toml
[binaries]
monero_version = "v0.18.3.4"
p2pool_version = "v4.1"
xmrig_version  = "v6.22.2"
```

Set to `"latest"` to always fetch the newest release on `xmrdp setup` or
`xmrdp update`.

---

## 7. Firewall rules

`xmrdp firewall <role>` prints rules for your platform based on what is
detected at runtime. It never applies them — review and apply them yourself.

### Ports used by XMRDP

| Port | Protocol | Direction (master) | Service | Notes |
|---|---|---|---|---|
| 18080 | TCP | inbound | monerod P2P | Blockchain sync with Monero network |
| 18081 | TCP | inbound | monerod RPC | Restrict to LAN; used by p2pool |
| 18083 | TCP | inbound | monerod ZMQ | Localhost only; used by p2pool |
| 37888 | TCP | inbound | p2pool P2P | p2pool sidechain sync |
| 3333  | TCP | inbound (from workers) | p2pool stratum | Restrict to worker IPs |
| 7099  | TCP | inbound (from workers) | C2 telemetry API | Restrict to worker IPs |

Workers need only outbound access to the master on ports 3333 and 7099.

When worker IPs are defined in `cluster.toml`, the generated rules restrict
ports 3333 and 7099 to those specific IPs. If no workers are defined yet, the
rules fall back to `192.168.0.0/16`. Tighten these rules once your worker IPs
are known — leaving stratum open to the internet allows anyone to submit shares
to your pool instance.

### Generating rules

```bash
# Master node
xmrdp firewall master

# Worker node
xmrdp firewall worker
```

The output includes your platform's primary format and the other formats
commented out for reference.

### Applying rules — Linux (ufw)

```bash
# Copy the ufw lines from the output and run them, e.g.:
sudo ufw allow 18080/tcp
sudo ufw allow from 192.168.1.101 to any port 3333 proto tcp
sudo ufw reload
```

### Applying rules — Linux (iptables)

```bash
# Copy the iptables lines from the output and run them, e.g.:
sudo iptables -A INPUT -p tcp --dport 18080 -j ACCEPT
# Persist rules using iptables-save / netfilter-persistent as appropriate
# for your distribution.
```

### Applying rules — macOS (pf)

```bash
# Add the pf rules printed by xmrdp firewall to /etc/pf.conf, then:
sudo pfctl -f /etc/pf.conf
sudo pfctl -e
```

### Applying rules — Windows (netsh)

Run PowerShell as Administrator, then paste the `netsh` commands:

```powershell
netsh advfirewall firewall add rule name="XMRDP - monerod P2P" `
  dir=in action=allow protocol=tcp localport=18080
# ... (paste remaining lines from xmrdp firewall master output)
```

---

## 8. Adding worker nodes

### On the master: push configuration

`xmrdp sync` copies `cluster.toml` to each worker via SCP. It marks each
worker's copy with `self = true` so the worker knows its own identity.
The remote file is written to `~/.xmrdp/config/cluster.toml` with mode `0600`.

SSH authentication uses your SSH agent or default key files
(`~/.ssh/id_*`). XMRDP does not store or transmit SSH credentials.

```bash
# Sync to all workers defined in cluster.toml
xmrdp sync

# Sync to specific workers only
xmrdp sync --worker worker-1 worker-2

# Use a specific SSH username
xmrdp sync --ssh-user deploy

# Preview what would be synced without making changes
xmrdp sync --dry-run

# Sync and restart xmrig on each worker after copying
xmrdp sync --restart
```

SSH must be configured for passwordless authentication (key-based) to each
worker. XMRDP will time out after 30 seconds per SSH/SCP operation.

### On each worker: install and start

On each worker machine, install XMRDP (same steps as [section 2](#2-installation)),
then start the worker role using the config that was synced from the master:

```bash
xmrdp start worker
```

The worker starts xmrig, which connects to the master's p2pool stratum port
(3333), and a C2 client that reports telemetry to the master on port 7099.

### Worker firewall rules

On each worker, generate and apply outbound rules:

```bash
xmrdp firewall worker
```

---

## 9. Stopping services

```bash
# Stop the master (all services)
xmrdp stop master

# Stop a worker
xmrdp stop worker
```

---

## 10. Updating binaries

### Check for updates without applying them

```bash
xmrdp update --check-only
```

Prints a table comparing installed versions against the latest GitHub releases.
Exits with code `0` if everything is up to date, `2` if updates are available.

### Apply updates

```bash
xmrdp update
```

Prompts for confirmation, then updates binaries in rolling-restart order:
xmrig first (miners reconnect automatically), then p2pool, then monerod.
If a download fails, the previous binary is restarted so the service is not
left down.

---

## 11. Viewing logs

Log files are stored in `<data_dir>/logs/`. Use `xmrdp logs` to tail them:

```bash
# Show last 50 lines from all services
xmrdp logs

# Tail a specific service
xmrdp logs monerod
xmrdp logs p2pool
xmrdp logs xmrig
xmrdp logs c2

# Show a specific number of lines
xmrdp logs monerod -n 200
xmrdp logs p2pool --lines 100
```

---

## 12. Troubleshooting

### "No config file found" or FileNotFoundError on startup

Run `xmrdp setup` first to generate `cluster.toml`, or pass `-c` to point
to an existing file:

```bash
xmrdp -c /path/to/cluster.toml start master
```

### "Permission denied" reading cluster.toml

On Linux and macOS, `cluster.toml` must be mode `0600` and owned by the user
running `xmrdp`. Check and fix:

```bash
ls -l ~/.xmrdp/config/cluster.toml
chmod 600 ~/.xmrdp/config/cluster.toml
```

### Workers cannot connect to the master (ports 3333 or 7099)

1. Verify the master firewall allows inbound connections on ports 3333 and
   7099 from the worker IPs. Re-run `xmrdp firewall master` after adding
   workers to `cluster.toml` — the rules become more specific once worker IPs
   are known.
2. Check `master.host` in `cluster.toml`. If the master has multiple network
   interfaces, set `host` to the IP on the interface that workers can reach,
   not `127.0.0.1`. The C2 server binds to this address.
3. Confirm `xmrdp status` on the master shows the C2 server as running.

### xmrig will not start on macOS

macOS may block the xmrig binary on first run because it is not code-signed
by Apple. In **System Settings > Privacy & Security**, scroll down to find the
blocked binary and click **Allow Anyway**, then re-run `xmrdp start master`.

If xmrig starts but performance is lower than expected, it may have failed
to allocate huge pages. Add `"--no-huge-pages"` to `[master.xmrig] extra_args`
in `cluster.toml` to suppress the error:

```toml
[master.xmrig]
threads    = 0
extra_args = ["--no-huge-pages"]
```

### Python Scripts directory not on PATH (Windows)

After `pip install`, the `xmrdp` command may not be found if the Python
Scripts directory is not in `PATH`. Find and add it:

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
# Add the printed path to your user PATH in System Properties > Environment Variables
```

If using `pipx`, run `pipx ensurepath` and restart your shell.

### ssh or scp not found (xmrdp sync)

`xmrdp sync` requires `ssh` and `scp` to be on `PATH`. On Linux, install
`openssh-client`. On macOS, OpenSSH is included. On Windows, enable the
OpenSSH Client optional feature or install Git for Windows.

### Binary download fails during setup

`xmrdp setup` downloads binaries from GitHub Releases over HTTPS. Common
causes of failure:

- No internet access or a proxy blocking GitHub.
- GitHub API rate limiting (unauthenticated requests are limited to 60/hour
  per IP). Wait and retry.
- A pinned version tag in `cluster.toml` that no longer exists. Set it back
  to `"latest"` or correct the tag.

After fixing the underlying issue, re-run:

```bash
xmrdp setup --skip-binaries   # skip if config is already correct
# or
xmrdp setup                   # redo full setup
```

### Verbose logging

Pass `-v` to any command to enable DEBUG-level output:

```bash
xmrdp -v start master
xmrdp -v sync --dry-run
```

---

## Additional resources

- **Example config:** `configs/cluster.example.toml` in the repository root
- **GitHub repository:** https://github.com/csysp/xmrdp
- **Issue tracker:** https://github.com/csysp/xmrdp/issues
- **Monero project:** https://getmonero.org
- **p2pool:** https://github.com/SChernykh/p2pool
- **xmrig:** https://github.com/xmrig/xmrig
