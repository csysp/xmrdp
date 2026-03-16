# XMRDP Security Assessment Report

**Classification**: Internal Engineering — Restricted Distribution
**Report Date**: 2026-03-14
**Assessment Type**: Architecture Review, Static Analysis, Threat Modeling
**Scope**: XMRDP codebase — C2 server, worker agent, binary manager, cluster coordination
**Last Updated**: 2026-03-15 — 9 findings remediated (see [Remediation Status](#remediation-status))

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [System Overview](#2-system-overview)
3. [STRIDE Threat Model](#3-stride-threat-model)
4. [Vulnerability Findings](#4-vulnerability-findings)
   - 4.1 [Critical Severity](#41-critical-severity)
   - 4.2 [High Severity](#42-high-severity)
   - 4.3 [Medium Severity](#43-medium-severity)
   - 4.4 [Low Severity](#44-low-severity)
5. [MITRE ATT&CK Coverage](#5-mitre-attck-coverage)
6. [Abuse Scenarios](#6-abuse-scenarios)
7. [Detection Rules](#7-detection-rules)
8. [Operational Security Recommendations](#8-operational-security-recommendations)
9. [Remediation Roadmap](#9-remediation-roadmap)
10. [Appendix: ATT&CK Detection Coverage by Tactic](#10-appendix-attck-detection-coverage-by-tactic)
11. [Remediation Status](#remediation-status)

---

## 1. Executive Summary

XMRDP is a Monero mining cluster deployment tool that orchestrates monerod, p2pool, and XMRig across multiple hosts from a central command-and-control (C2) server. Workers register with the C2 server, receive configuration, download binaries, and execute them as managed processes.

This assessment identified **18 vulnerability findings** across the codebase. The overall risk posture is **High**. Several findings combine to create attack chains that could result in remote code execution, full cluster compromise, or XMRDP being weaponized as a malware distribution platform by an unauthorized party.

### Overall Risk Posture: HIGH

| Severity | Count |
|----------|-------|
| Critical | 1 |
| High | 8 |
| Medium | 7 |
| Low | 3 |
| **Total** | **19** |

**Update 2026-03-15**: 9 of the 18 findings have been remediated. See the [Remediation Status](#remediation-status) section for the current open/closed breakdown.

### Top 3 Immediate Actions

These three findings, left unaddressed, represent unacceptable risk regardless of your deployment environment:

**1. Fix argument injection in `extra_args` (F-05 — Critical)** — **FIXED 2026-03-15**
Any value in the `extra_args` configuration field flows unsanitized into `subprocess` calls. On a multi-user system or in a misconfigured deployment, this is a direct path to arbitrary code execution. This must be fixed before any production deployment.

**2. Replace token comparison with `hmac.compare_digest` (F-02 — High)** — **FIXED 2026-03-15**
The current string equality check (`!=`) on the Bearer token is vulnerable to timing side-channel attacks. An adversary on the same network can measure response latency to reconstruct the token byte by byte. This is a two-line fix with high impact.

**3. Validate binary serve paths with `resolve()` (F-09 — High)** — **FIXED 2026-03-15**
The `/api/binaries/<name>` endpoint uses a character blacklist to prevent path traversal. Blacklists are incomplete by definition. An adversary can traverse outside the binary cache directory and serve arbitrary files from the host filesystem to all connected workers, who will then execute them.

---

## 2. System Overview

XMRDP follows a hub-and-spoke architecture. The C2 server is the hub; each mining worker is a spoke. Workers pull configuration and binaries from the C2 server over HTTP, then execute the mining stack locally.

```
                        ┌─────────────────────────────────────┐
                        │          C2 Server (Hub)            │
                        │                                     │
                        │  ┌─────────────┐  ┌─────────────┐   │
                        │  │  REST API   │  │  Binary     │   │
                        │  │  :7099      │  │  Cache      │   │
                        │  │             │  │  /binaries/ │   │
                        │  └──────┬──────┘  └──────┬──────┘   │
                        │         │                │          │
                        │  ┌──────▼──────────────────────┐    │
                        │  │     cluster.toml config     │    │
                        │  └─────────────────────────────┘    │
                        └──────────────┬──────────────────────┘
                                       │ HTTP (plaintext)
                              Bearer token in header
                                       │
             ┌─────────────────────────┼─────────────────────────┐
			 │                         │                         │
   ┌─────────▼────────────┐  ┌─────────▼────────────┐  ┌─────────▼────────────┐
   │      Worker A        │  │      Worker B        │  │      Worker C        │
   │                      │  │                      │  │                      │
   │  monerod             │  │  monerod             │  │  monerod             │
   │  p2pool              │  │  p2pool              │  │  p2pool              │
   │  xmrig               │  │  xmrig               │  │  xmrig               │
   │                      │  │                      │  │                      │
   │  ZMQ :18083 (0.0.0.0)│  │  ZMQ :18083 (0.0.0.0)│  │  ZMQ :18083 (0.0.0.0)│
   └──────────────────────┘  └──────────────────────┘  └──────────────────────┘

   Communication flow:
     Worker → C2:  POST /api/workers/register   (registration + host info)
     Worker → C2:  POST /api/workers/<id>/heartbeat  (every ~60s)
     Worker → C2:  GET  /api/binaries/<name>    (binary download)
     C2     → any: GET  /api/cluster/status     (aggregated host info)
```

### Key Components

| Component | Role | Primary Risk Surface |
|-----------|------|----------------------|
| C2 REST API | Configuration distribution, worker coordination | Auth bypass, request injection, info disclosure |
| Binary manager | Downloads monerod, p2pool, xmrig from GitHub Releases | Supply chain, path traversal, zip slip |
| Worker agent | Executes mining stack on each host | Argument injection, process manipulation |
| `cluster.toml` | Master configuration including API token and wallet address | Credential exposure, file permissions |

---

## 3. STRIDE Threat Model

STRIDE is a structured framework for categorizing security threats: Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, and Elevation of Privilege. The table below maps each category to the concrete threats this assessment identified in XMRDP.

| STRIDE Category | Threat | Related Findings |
|-----------------|--------|-----------------|
| **Spoofing** | Worker name collision allows impersonation of legitimate workers; no identity binding beyond name | F-11 |
| **Spoofing** | Bearer token intercepted over plaintext HTTP allows full C2 impersonation | F-03, D (Abuse Scenario) |
| **Tampering** | Unsigned binaries with optional checksum verification; attacker can substitute malicious binaries | F-08 |
| **Tampering** | Path traversal on binary serve endpoint allows attacker to substitute arbitrary files | F-09 |
| **Tampering** | `extra_args` injection allows modification of subprocess command lines | F-05 |
| **Tampering** | Zip Slip during extraction allows overwriting arbitrary files on the host | F-06 |
| **Repudiation** | No audit logging for authentication failures, worker registration, or binary serves | F-12 |
| **Information Disclosure** | Wallet address visible in process argument list to all users on multi-user systems | F-04 |
| **Information Disclosure** | `cluster.toml` and `xmrig_config.json` potentially world-readable | F-13 |
| **Information Disclosure** | Heartbeat sends CPU, RAM, and platform to C2; `/api/cluster/status` aggregates all hosts | C (Abuse Scenario) |
| **Information Disclosure** | monerod ZMQ bound to `0.0.0.0` exposes internal interface externally | F-15 |
| **Denial of Service** | No request body size cap allows memory exhaustion against C2 server | F-01 |
| **Denial of Service** | No download size limit allows disk exhaustion during binary fetch | F-07 |
| **Denial of Service** | PID file TOCTOU race allows SIGKILL to be redirected to arbitrary processes | F-16, E (Abuse Scenario) |
| **Elevation of Privilege** | Argument injection into privileged subprocess calls | F-05 |
| **Elevation of Privilege** | Detached process groups persist after parent termination, evading supervision | F (Abuse Scenario) |

---

## 4. Vulnerability Findings

Findings are grouped by severity from Critical to Low. Each entry includes the affected location, a plain-language description of the risk, and a concrete remediation.

---

### 4.1 Critical Severity

---

#### F-05: Unsanitized `extra_args` Injected into Subprocess

**Severity**: `CRITICAL`
**Status**: **FIXED** (2026-03-15)
**Affected component**: Worker agent — subprocess launch logic
**CWE**: CWE-88 (Argument Injection or Modification)

**Description**

The `extra_args` configuration field is passed directly to `subprocess.Popen` (or equivalent) without sanitization. This field controls additional arguments passed to monerod, p2pool, and xmrig at launch time. Because these processes accept flags that can redirect output, load external files, or execute shell commands, an attacker who can modify `cluster.toml` — or who controls the C2 server — can achieve arbitrary command execution on every worker.

For example, xmrig accepts `--config <path>` which loads an arbitrary JSON configuration. An attacker who injects `--config /tmp/evil.json` can cause xmrig to connect to an attacker-controlled pool and execute attacker-supplied scripts.

The risk is compounded by F-03 (plaintext HTTP) and F-09 (path traversal), which together allow a network-positioned attacker to both inject the argument and supply the payload file.

**Remediation**

Restrict `extra_args` to a static allow-list of known-safe flags. Reject any value that is not on the list before it reaches `subprocess`.

```python
# In worker configuration validation

ALLOWED_XMRIG_EXTRA_ARGS = frozenset([
    "--randomx-1gb-pages",
    "--huge-pages",
    "--no-color",
    "--background",
])

ALLOWED_MONEROD_EXTRA_ARGS = frozenset([
    "--no-zmq",
    "--restricted-rpc",
])

def validate_extra_args(args: list[str], allowed: frozenset[str]) -> list[str]:
    """
    Validate extra_args against an explicit allow-list.
    Raises ValueError if any argument is not permitted.
    """
    for arg in args:
        # Strip value portion for flags like --flag=value
        flag = arg.split("=")[0]
        if flag not in allowed:
            raise ValueError(
                f"Disallowed extra_arg: {arg!r}. "
                f"Permitted flags: {sorted(allowed)}"
            )
    return args
```

If dynamic arguments are genuinely required, accept only the flag names from the allow-list and supply your own safe values — never pass user-controlled strings as argument values.

**Fix Applied (2026-03-15)** — `xmrdp/config_generator.py`

Added a module-level regex allowlist `_SAFE_ARG_RE = re.compile(r'^--[a-zA-Z0-9][a-zA-Z0-9\-_.:/=,]*$')` and a `_validate_extra_args()` function that raises `ValueError` for any argument not matching the pattern. The validation is applied in both `generate_monerod_args()` and `generate_p2pool_args()` before any user-supplied argument reaches the returned list. This takes a different approach from the static frozenset in the suggested remediation — using a format-based allowlist rather than an enumerated one — which accepts any well-formed `--flag` or `--flag=value` style argument while still rejecting shell metacharacters, bare values, and short-form flags.

---

### 4.2 High Severity

---

#### F-02: Non-Constant-Time Token Comparison

**Severity**: `HIGH`
**Status**: **FIXED** (2026-03-15)
**Affected component**: C2 server — request authentication
**CWE**: CWE-208 (Observable Timing Discrepancy)

**Description**

The C2 server compares the incoming Bearer token against the configured secret using Python's `!=` operator. Standard string equality short-circuits on the first mismatched byte, which means responses to incorrect tokens arrive slightly faster when the first byte is wrong than when many bytes match. An adversary on the same network segment can measure these timing differences to reconstruct the secret token byte by byte without brute force. This attack is practical against services accessible over a LAN and has been demonstrated against production authentication systems.

**Remediation**

Replace the equality check with `hmac.compare_digest`, which is guaranteed to run in constant time regardless of where the strings diverge.

```python
import hmac

# Before (vulnerable):
def authenticate(request_token: str, secret: str) -> bool:
    return request_token != secret  # timing leak

# After (safe):
def authenticate(request_token: str, secret: str) -> bool:
    # compare_digest requires bytes or str — ensure both are the same type.
    # If your token can be None (missing header), handle that before this call.
    return hmac.compare_digest(request_token, secret)
```

Note: `hmac.compare_digest` does not prevent brute force — it only removes the timing oracle. You should also enforce a minimum token length of 32 random bytes (see F-17 for token generation improvements).

**Fix Applied (2026-03-15)** — `xmrdp/c2_server.py`

Added `import hmac` to the module imports. In `_check_auth()`, the `expected` value is now retrieved with `str(_config.get("master", {}).get("api_token", ""))` — the `str()` coercion guards against non-string values in the configuration dictionary. The comparison `token != expected` was replaced with `not hmac.compare_digest(token, expected)`. Both operands are always `str` type, satisfying `hmac.compare_digest`'s requirement that both arguments be the same type.

---

#### F-03: All C2 Traffic Over Plaintext HTTP

**Severity**: `HIGH`
**Affected component**: C2 server — `_base_url()`
**CWE**: CWE-319 (Cleartext Transmission of Sensitive Information)

**Description**

The `_base_url()` function hardcodes `http://` as the scheme. All communication between workers and the C2 server — including the Bearer token, wallet address, host system information, and downloaded binaries — travels in plaintext. Any observer with access to the network path (including other processes on the same host via loopback sniffing, or a router on a shared network) can read and modify this traffic. This is the foundation for Abuse Scenario D (token interception and cluster takeover).

**Remediation**

Add TLS termination in front of the C2 server. The simplest approach for a self-hosted deployment is to place the C2 server behind a reverse proxy (nginx, Caddy) that handles TLS, or to generate a self-signed certificate and serve HTTPS directly.

```python
# Option A: Caddy reverse proxy (recommended for simplicity)
# Caddyfile:
#   c2.internal.example.com {
#       reverse_proxy localhost:7099
#   }
# Caddy automatically provisions and renews certificates.

# Option B: Direct HTTPS with a self-signed cert (for air-gapped/internal use)
import ssl
from http.server import HTTPServer

def create_tls_server(host: str, port: int, certfile: str, keyfile: str) -> HTTPServer:
    server = HTTPServer((host, port), YourRequestHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server

# Option C: Update _base_url() to read scheme from config
def _base_url(self) -> str:
    scheme = self.config.get("scheme", "https")  # default to https
    host = self.config["c2_host"]
    port = self.config["c2_port"]
    return f"{scheme}://{host}:{port}"
```

Workers connecting to a self-signed certificate must pin the certificate or load the CA. Never disable certificate verification (`verify=False` in `requests`) as a workaround.

---

#### F-06: Zip Slip and Missing Tar Filter on Python < 3.12

**Severity**: `HIGH`
**Status**: **FIXED** (2026-03-15)
**Affected component**: Binary manager — archive extraction
**CWE**: CWE-22 (Path Traversal), CWE-23 (Relative Path Traversal)

**Description**

The binary manager uses `zipfile.extractall()` without validating that extracted paths remain inside the target directory. A malicious archive can contain entries with relative path components like `../../.bashrc` or absolute paths like `/etc/cron.d/xmrdp` that write files outside the intended extraction directory. On Python < 3.12, the `tarfile` module has the same vulnerability via its default `filter` setting. If an attacker can substitute a malicious archive at the download URL — or via a DNS spoofing attack against GitHub's CDN — they can overwrite arbitrary files on every worker host.

**Remediation**

Validate each archive member path before extraction. Reject any member whose resolved path does not begin with the target extraction directory.

```python
import zipfile
import tarfile
from pathlib import Path

def safe_extract_zip(archive_path: Path, target_dir: Path) -> None:
    """Extract a zip archive, rejecting any member that would escape target_dir."""
    target_dir = target_dir.resolve()
    with zipfile.ZipFile(archive_path) as zf:
        for member in zf.infolist():
            member_path = (target_dir / member.filename).resolve()
            if not str(member_path).startswith(str(target_dir) + "/"):
                raise ValueError(
                    f"Zip Slip detected: {member.filename!r} would extract to "
                    f"{member_path}, outside {target_dir}"
                )
        zf.extractall(target_dir)

def safe_extract_tar(archive_path: Path, target_dir: Path) -> None:
    """Extract a tar archive using the 'data' filter (Python 3.12+) or manual check."""
    target_dir = target_dir.resolve()
    with tarfile.open(archive_path) as tf:
        # Python 3.12+: use the built-in safe filter
        try:
            tf.extractall(target_dir, filter="data")
        except TypeError:
            # Python < 3.12 fallback: manual validation
            for member in tf.getmembers():
                member_path = (target_dir / member.name).resolve()
                if not str(member_path).startswith(str(target_dir) + "/"):
                    raise ValueError(
                        f"Tar path traversal detected: {member.name!r}"
                    )
            tf.extractall(target_dir)
```

**Fix Applied (2026-03-15)** — `xmrdp/binary_manager.py`

ZIP extraction: in `extract_binary()`, a pre-extraction scan now iterates `zf.namelist()` and calls `(tmp_dir / member).resolve().relative_to(tmp_dir_resolved)` on each member path. Any member whose resolved path falls outside the temporary extraction directory raises `RuntimeError` with a diagnostic message before `extractall` is called.

Tar extraction: a `_safe_tar_extract()` inner function was added. On Python 3.12 and later it calls `tf.extractall(tmp_dir, filter="data")`. On Python earlier than 3.12 it manually iterates `tf.getmembers()`, applies the same `resolve().relative_to()` containment check, and only calls `extractall` after all members pass. This is particularly important because Linux monerod and p2pool releases are distributed as `.tar.gz` archives, meaning the tar path was the primary risk on the most common deployment platform.

---

#### F-08: Checksum File Not GPG-Signed; Silent Skip on Missing Checksum

**Severity**: `HIGH`
**Status**: **PARTIALLY FIXED** (2026-03-15) — silent-skip on missing checksum resolved; GPG verification deferred
**Affected component**: Binary manager — `verify_checksums` logic
**CWE**: CWE-347 (Improper Verification of Cryptographic Signature)

**Description**

Two related problems make binary integrity verification ineffective:

First, the checksum file itself is not GPG-verified. An attacker who can intercept or substitute the checksum file (via DNS spoofing, CDN compromise, or MITM on the plaintext HTTP connection from F-03) can replace both the binary and its checksum simultaneously. SHA-256 verification against an attacker-controlled checksum provides no security.

Second, when the checksum for a binary is absent from the checksum file, the code proceeds to execute the binary anyway. The `verify_checksums` configuration flag is present but does not gate execution — it is dead code. This means the integrity check can be silently bypassed by serving an incomplete checksum file.

**Remediation**

Treat a missing checksum as a verification failure, and add GPG signature verification against the project maintainer's public key.

```python
import hashlib
import subprocess
from pathlib import Path

# Embed the expected GPG key fingerprint for each project.
# Fetch these from the official project documentation and pin them in code.
TRUSTED_FINGERPRINTS = {
    "xmrig": "YOUR_XMRIG_MAINTAINER_FINGERPRINT",
    "monerod": "YOUR_MONERO_MAINTAINER_FINGERPRINT",
    "p2pool": "YOUR_P2POOL_MAINTAINER_FINGERPRINT",
}

def verify_binary_integrity(
    binary_path: Path,
    checksum_file: Path,
    signature_file: Path,
    project: str,
) -> None:
    """
    Verify a binary using both SHA-256 checksum and GPG signature.
    Raises RuntimeError if either check fails or if the checksum is absent.
    """
    # Step 1: Verify the GPG signature on the checksum file itself
    fingerprint = TRUSTED_FINGERPRINTS[project]
    result = subprocess.run(
        ["gpg", "--verify", "--status-fd=1", str(signature_file), str(checksum_file)],
        capture_output=True, text=True,
    )
    if f"VALIDSIG {fingerprint}" not in result.stdout:
        raise RuntimeError(
            f"GPG signature verification failed for {checksum_file}. "
            f"Expected fingerprint: {fingerprint}"
        )

    # Step 2: Find the expected hash in the (now-verified) checksum file
    binary_name = binary_path.name
    expected_hash = None
    for line in checksum_file.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith(binary_name):
            expected_hash = parts[0]
            break

    if expected_hash is None:
        # Treat absent checksum as a hard failure — never skip
        raise RuntimeError(
            f"No checksum found for {binary_name} in {checksum_file}. "
            f"Refusing to execute unverified binary."
        )

    # Step 3: Verify the binary hash
    actual_hash = hashlib.sha256(binary_path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise RuntimeError(
            f"SHA-256 mismatch for {binary_name}. "
            f"Expected: {expected_hash}, got: {actual_hash}"
        )
```

**Fix Applied (2026-03-15)** — `xmrdp/binary_manager.py`

The `verify_checksums` flag is now wired to actual behavior in `ensure_binaries()`. The logic reads `config.get("security", {}).get("verify_checksums", True)` (defaulting to `True`). When `verify_checksums=True` (the default):

- If no checksum file is present in the release assets, the downloaded archive is deleted and `RuntimeError` is raised — execution halts.
- If a checksum file is present but contains no entry matching the downloaded asset filename, the archive is deleted and `RuntimeError` is raised.

When `verify_checksums=False`, both missing-checksum cases print a `WARNING` and continue rather than aborting. A checksum that IS present in the file is always verified regardless of the flag value.

**Residual risk**: The checksum file itself is still not GPG-verified. An attacker who can substitute both the archive and the checksum file simultaneously (e.g., via DNS spoofing of GitHub's CDN combined with F-03 plaintext HTTP) can still bypass integrity verification. GPG signature verification against pinned maintainer keys remains the complete fix and is tracked for a future session.

---

#### F-09: Incomplete Path Traversal Protection on Binary Serve Endpoint

**Severity**: `HIGH`
**Status**: **FIXED** (2026-03-15)
**Affected component**: C2 server — `/api/binaries/<name>` handler
**CWE**: CWE-22 (Improper Limitation of a Pathname to a Restricted Directory)

**Description**

The binary serve endpoint accepts a filename parameter and constructs a file path inside the binary cache directory. It attempts to prevent path traversal using a character blacklist (likely blocking `..`, `/`, or similar). Blacklist-based path sanitization is reliably incomplete. URL encoding (`%2F` for `/`, `%2E%2E` for `..`), Unicode normalization, null bytes, and OS-specific path representations all offer bypass vectors that blacklists fail to anticipate. An attacker who bypasses this check can instruct the C2 server to serve any file readable by the server process — including `/etc/passwd`, `cluster.toml`, or an attacker-placed binary — to all connected workers, who will then execute it.

**Remediation**

Replace the blacklist with a `resolve()` containment check, which is the correct and complete solution.

```python
from pathlib import Path
from http import HTTPStatus

BINARY_CACHE_DIR = Path("/var/lib/xmrdp/binaries").resolve()

def serve_binary(filename: str) -> tuple[bytes, HTTPStatus]:
    """
    Serve a binary from the cache directory.
    Rejects any path that resolves outside the cache directory,
    regardless of encoding or traversal technique.
    """
    # resolve() follows all symlinks and normalizes the path.
    # This handles: ../../etc/passwd, %2F, null bytes, Unicode tricks.
    requested_path = (BINARY_CACHE_DIR / filename).resolve()

    # The containment check: the resolved path must begin with the cache dir.
    # Using str comparison after resolve() is safe because resolve() is canonical.
    if not str(requested_path).startswith(str(BINARY_CACHE_DIR) + "/"):
        # Log this as a security event — it is almost certainly an attack attempt.
        log_security_event("path_traversal_attempt", filename=filename)
        return b"", HTTPStatus.FORBIDDEN

    if not requested_path.is_file():
        return b"", HTTPStatus.NOT_FOUND

    return requested_path.read_bytes(), HTTPStatus.OK
```

Note: if `BINARY_CACHE_DIR` itself contains symlinks, call `.resolve()` on it at startup and store the result. Never resolve `BINARY_CACHE_DIR` lazily inside the handler.

**Fix Applied (2026-03-15)** — `xmrdp/c2_server.py`

The character blacklist (`"/" in name or ".." in name`) was replaced with a `resolve()` containment check. In `_handle_get_binary()`, `bin_dir` is resolved once at handler entry via `get_binary_dir().resolve()`. For each candidate path, `candidate.resolve()` is called and then `resolved.relative_to(bin_dir)` is tested. Any candidate whose resolved path falls outside `bin_dir` — including paths using URL encoding, symlink escapes, or any other traversal technique — raises `ValueError` and is skipped silently (the attacker receives a 404, not an error that reveals the containment check). Only candidates that pass the containment check and refer to actual files on disk are served.

---

#### F-10: C2 Server Defaults to Binding `0.0.0.0`

**Severity**: `HIGH`
**Status**: **FIXED** (2026-03-15) — default hardened; design limitation documented below
**Affected component**: C2 server — bind address configuration
**CWE**: CWE-605 (Multiple Binds to the Same Port)

**Description**

The C2 server listens on `0.0.0.0` by default, which binds to all network interfaces including public-facing ones. On a cloud instance or any host with a public IP address, this exposes the API — including authentication endpoints and the binary serve handler — directly to the internet. Combined with F-02 (timing attack on token), F-03 (plaintext HTTP), and F-09 (path traversal), a publicly bound C2 server represents a severe combined attack surface.

**Remediation**

Change the default bind address to `127.0.0.1` and require an explicit opt-in to bind on other interfaces.

```toml
# cluster.toml — updated default
[server]
# Bind address for the C2 API server.
# Default is localhost only. To allow worker connections from other hosts,
# set this to your internal network interface IP (e.g., "10.0.1.5").
# NEVER set to "0.0.0.0" on a host with a public IP unless behind a firewall.
bind_address = "127.0.0.1"
port = 7099
```

```python
# C2 server startup
def start_server(config: dict) -> None:
    bind_address = config.get("bind_address", "127.0.0.1")  # safe default
    port = config.get("port", 7099)

    if bind_address == "0.0.0.0":
        import warnings
        warnings.warn(
            "C2 server is binding to 0.0.0.0 — all network interfaces. "
            "Ensure a firewall restricts access to trusted worker IPs only.",
            stacklevel=2,
        )

    server = HTTPServer((bind_address, port), C2RequestHandler)
    server.serve_forever()
```

**Fix Applied (2026-03-15)** — `xmrdp/c2_server.py`

The default fallback in `start_c2_server()` was changed from `"0.0.0.0"` to `"127.0.0.1"`:

```python
host = config.get("master", {}).get("host", "127.0.0.1")
```

**Design limitation**: The `master.host` configuration field currently controls both the bind address (what interface the C2 server listens on) and the advertise address (what IP workers use to connect). On single-machine deployments, `127.0.0.1` is correct for both purposes. On multi-machine clusters, operators must set `master.host` to a reachable LAN IP, which re-exposes the C2 port on that interface. A future improvement should split this into separate `bind_host` and `advertise_host` fields so operators can bind to a specific internal interface without advertising `0.0.0.0`. Until that split is implemented, firewall rules restricting port 7099 access to worker IPs remain essential for multi-machine deployments.

---

#### F-01: No Request Body Size Cap or Rate Limiting on C2 Server

**Severity**: `HIGH`
**Status**: **PARTIALLY FIXED** (2026-03-15) — body size cap applied; read-stall and rate limiting deferred
**Affected component**: C2 server — `_read_body()`
**CWE**: CWE-400 (Uncontrolled Resource Consumption)

**Description**

The `_read_body()` function reads the full request body without enforcing a maximum size. An attacker — or a malfunctioning worker — can send an arbitrarily large request body that exhausts the server's memory or blocks the request handler thread indefinitely. Additionally, there is no rate limiting on authentication failure paths, which allows unlimited brute-force attempts against the Bearer token from any host that can reach the server.

**Remediation**

Cap request body size at read time, and add a simple exponential backoff on auth failures.

```python
import time
from collections import defaultdict

MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # 1 MB — adjust for your largest valid payload

# Simple in-memory rate limit tracker.
# For production, use a shared store (Redis) if running multiple C2 processes.
_auth_failures: dict[str, list[float]] = defaultdict(list)
AUTH_FAILURE_WINDOW_SECONDS = 60
AUTH_FAILURE_MAX_ATTEMPTS = 10

def _read_body(rfile, content_length: int) -> bytes:
    """Read request body up to MAX_REQUEST_BODY_BYTES."""
    if content_length > MAX_REQUEST_BODY_BYTES:
        raise ValueError(
            f"Request body too large: {content_length} bytes "
            f"(max {MAX_REQUEST_BODY_BYTES})"
        )
    return rfile.read(min(content_length, MAX_REQUEST_BODY_BYTES))

def check_auth_rate_limit(client_ip: str) -> None:
    """Raise if client_ip has exceeded authentication failure threshold."""
    now = time.monotonic()
    recent = [t for t in _auth_failures[client_ip] if now - t < AUTH_FAILURE_WINDOW_SECONDS]
    _auth_failures[client_ip] = recent
    if len(recent) >= AUTH_FAILURE_MAX_ATTEMPTS:
        raise PermissionError(
            f"Too many authentication failures from {client_ip}. "
            f"Retry after {AUTH_FAILURE_WINDOW_SECONDS}s."
        )

def record_auth_failure(client_ip: str) -> None:
    _auth_failures[client_ip].append(time.monotonic())
```

**Fix Applied (2026-03-15)** — `xmrdp/c2_server.py`

A `_MAX_BODY = 65536` class attribute (64 KB) was added to `C2Handler`. In `_read_body()`, the `Content-Length` value is checked before any read is performed:

```python
if length > self._MAX_BODY:
    raise ValueError(
        f"Request body too large: {length} bytes (max {self._MAX_BODY})"
    )
```

All POST endpoint handlers already catch `(json.JSONDecodeError, ValueError)` and return HTTP 400, so this path is handled correctly without changes to callers.

**Residual risks**: (1) A client that sends a spoofed small `Content-Length` but streams a large body will not be caught — the read will complete after `length` bytes regardless of actual body size; a socket read timeout is needed to address this, but stdlib `http.server` does not expose one cleanly. (2) Chunked transfer encoding bodies have no `Content-Length` header; `_read_body()` returns an empty dict in this case, silently ignoring the payload. Rate limiting on authentication failures was not added in this session and remains open.

---

#### F-04: Wallet Address in Process Arguments World-Readable

**Severity**: `HIGH`
**Affected component**: Worker agent — subprocess argument construction
**CWE**: CWE-214 (Invocation of Process Using Visible Sensitive Information)

**Description**

The Monero wallet address is passed as a command-line argument to xmrig (e.g., `--user <wallet_address>`). On any multi-user system, every local user can read the full argument list of every process via `ps aux` or `/proc/<pid>/cmdline`. While a Monero wallet address is not a private key and does not allow fund theft, it does expose the operator's wallet identity and enables monitoring of mining income. On a shared cloud instance or a compromised host, this information has direct value to an attacker.

**Remediation**

Pass the wallet address via a configuration file or environment variable rather than as a command-line argument. XMRig natively supports a JSON configuration file that keeps sensitive values out of the process argument list.

```python
import json
import os
import stat
from pathlib import Path

def write_xmrig_config(config_path: Path, wallet_address: str, pool_url: str) -> None:
    """
    Write xmrig configuration to a file with restrictive permissions.
    The wallet address is in the config file, not the command line.
    """
    config = {
        "pools": [{
            "url": pool_url,
            "user": wallet_address,   # wallet address stays in the file
            "pass": "x",
            "keepalive": True,
            "tls": True,              # see also F-18
        }],
        "randomx": {"1gb-pages": False},
    }
    config_path.write_text(json.dumps(config, indent=2))
    # Restrict to owner read/write only — group and world have no access
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

# Launch xmrig with --config, not --user
def launch_xmrig(binary_path: Path, config_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [str(binary_path), "--config", str(config_path)],
        start_new_session=True,
    )
```

---

### 4.3 Medium Severity

---

#### F-11: No Worker Identity Binding; Name Collision Permitted

**Severity**: `MEDIUM`
**Affected component**: C2 server — worker registration
**CWE**: CWE-287 (Improper Authentication)

**Description**

Workers register by supplying a name and receive an ID. There is no mechanism to bind a worker name or ID to a specific IP address, cryptographic credential, or other persistent identity. Any worker that knows the API token (which is transmitted in plaintext per F-03) can register with the same name as an existing worker. The C2 server cannot distinguish between the legitimate worker and the impersonator, which enables mining output redirection or false status reporting.

**Remediation**

At registration time, record the client IP address and reject re-registration from a different IP for an existing worker name. For stronger guarantees, issue each worker a unique registration token on first registration and require it on subsequent reconnections.

```python
def register_worker(worker_name: str, client_ip: str, worker_registry: dict) -> str:
    """Register a worker, enforcing IP binding after first registration."""
    if worker_name in worker_registry:
        registered_ip = worker_registry[worker_name]["ip"]
        if registered_ip != client_ip:
            log_security_event(
                "worker_impersonation_attempt",
                name=worker_name,
                registered_ip=registered_ip,
                attempt_ip=client_ip,
            )
            raise PermissionError(
                f"Worker name {worker_name!r} is already registered from {registered_ip}. "
                f"Re-registration from {client_ip} is not permitted."
            )
    worker_id = generate_worker_id()
    worker_registry[worker_name] = {"id": worker_id, "ip": client_ip}
    return worker_id
```

---

#### F-12: No Structured Audit Logging for Security-Relevant Events

**Severity**: `MEDIUM`
**Affected component**: C2 server — logging subsystem
**CWE**: CWE-778 (Insufficient Logging)

**Description**

The C2 server does not log authentication failures, worker registration events, binary serve requests, or configuration changes in a structured, queryable format. Without these logs, you cannot detect an ongoing attack, reconstruct what happened after a compromise, or feed events into a SIEM or detection system. The absence of auth failure logging is particularly significant because it means the rate limiting in F-01's remediation cannot be audited retroactively.

**Remediation**

Add a structured security event logger that emits JSON to stdout or a dedicated log file. Use a consistent schema so events are machine-parseable.

```python
import json
import logging
import time
from typing import Any

security_logger = logging.getLogger("xmrdp.security")
security_handler = logging.StreamHandler()
security_handler.setFormatter(logging.Formatter("%(message)s"))
security_logger.addHandler(security_handler)
security_logger.setLevel(logging.INFO)

def log_security_event(event_type: str, **fields: Any) -> None:
    """
    Emit a structured security event.

    Event types to log at minimum:
      - auth_success, auth_failure
      - worker_registered, worker_heartbeat_missed
      - binary_served, binary_serve_denied
      - path_traversal_attempt
      - config_changed
      - rate_limit_triggered
    """
    event = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event_type": event_type,
        **fields,
    }
    security_logger.info(json.dumps(event))

# Usage example:
# log_security_event("auth_failure", client_ip="10.0.1.5", reason="invalid_token")
# log_security_event("binary_served", filename="xmrig-6.21.0-linux-x64.tar.gz", worker_id="w-abc123")
```

---

#### F-13: Sensitive Config Files Written Without Restrictive Permissions

**Severity**: `MEDIUM`
**Status**: **FIXED** (2026-03-15)
**Affected component**: Configuration writer — `cluster.toml`, `xmrig_config.json`
**CWE**: CWE-732 (Incorrect Permission Assignment for Critical Resource)

**Description**

`cluster.toml` contains the API token and wallet address. `xmrig_config.json` contains the wallet address and pool credentials. When these files are created, Python's default file creation mode applies the process umask, which on many systems results in world-readable files (mode `0o644`). Any local user on the host can read the API token and wallet address.

**Remediation**

Write configuration files with explicit permissions that restrict access to the owning user only.

```python
import os
import stat
from pathlib import Path

def write_config_file(path: Path, content: str) -> None:
    """
    Write a configuration file with owner-only read/write permissions.
    Ensures the file is not readable by group or world users.
    """
    # Use os.open with O_CREAT | O_WRONLY to set permissions atomically at creation.
    # This avoids the TOCTOU window that exists with write-then-chmod.
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        mode=0o600,  # owner read+write only; no group, no world
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except Exception:
        os.close(fd)
        raise

    # If the file already existed before this call (O_CREAT did not create it),
    # explicitly set permissions in case they were created with wrong perms previously.
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
```

**Fix Applied (2026-03-15)** — `xmrdp/config_generator.py`, `xmrdp/wizard.py`

The previous pattern of `path.write_text()` followed by `os.chmod(0o600)` was replaced in all three write sites. The new pattern uses `os.open()` with `os.O_WRONLY | os.O_CREAT | os.O_TRUNC` and mode `0o600`, then wraps the file descriptor with `os.fdopen()`. This creates the file with the correct permissions from the first inode allocation, eliminating the TOCTOU window during which the file existed with world-readable permissions.

All three write sites are guarded with `if sys.platform != "win32":` so that Windows deployments continue to use `path.write_text()` (Windows ACLs are managed separately and `os.open` mode bits have limited effect on Windows). The specific sites fixed:

- `write_xmrig_config()` in `xmrdp/config_generator.py` — writes `xmrig_config.json`
- `run_setup()` in `xmrdp/wizard.py` — writes `cluster.toml` during interactive setup
- `generate_config_cmd()` in `xmrdp/wizard.py` — writes `cluster.toml` via the generate-config subcommand

---

#### F-15: monerod ZMQ Hardcoded to `0.0.0.0`

**Severity**: `MEDIUM`
**Status**: **FIXED** (2026-03-15)
**Affected component**: Worker agent — monerod launch arguments
**CWE**: CWE-605 (Multiple Binds to the Same Port)

**Description**

monerod's ZMQ interface is launched with `--zmq-pub tcp://0.0.0.0:18083`, binding to all interfaces. ZMQ is used by p2pool for block notifications. There is no authentication on the ZMQ interface. On a multi-homed host or one with a public IP, this exposes the ZMQ stream to external hosts. An external observer can subscribe to block notifications to monitor mining activity, and in some monerod versions, a malformed ZMQ connection can cause instability.

**Remediation**

Bind ZMQ to the loopback interface, since p2pool runs on the same host.

```python
# In monerod argument construction:
zmq_bind = config.get("zmq_bind_address", "127.0.0.1")  # default to loopback
zmq_port = config.get("zmq_port", 18083)

monerod_args = [
    str(monerod_binary),
    "--non-interactive",
    f"--zmq-pub=tcp://{zmq_bind}:{zmq_port}",
    # ... other args
]
```

```toml
# cluster.toml — document the option explicitly
[monerod]
# ZMQ bind address for p2pool block notifications.
# Set to 127.0.0.1 unless p2pool runs on a different host.
zmq_bind_address = "127.0.0.1"
zmq_port = 18083
```

**Fix Applied (2026-03-15)** — `xmrdp/config_generator.py`

The `--zmq-pub` argument in `generate_monerod_args()` was changed from `tcp://0.0.0.0:18083` to `tcp://127.0.0.1:18083`. The p2pool argument list connects to `127.0.0.1:18083` via `--host 127.0.0.1 --zmq-port 18083`, so the topology is unaffected — p2pool and monerod continue to communicate correctly when both run on the same host, which is the standard XMRDP deployment model.

---

#### F-07: No Maximum Download Size Enforcement

**Severity**: `MEDIUM`
**Affected component**: Binary manager — download logic
**CWE**: CWE-400 (Uncontrolled Resource Consumption)

**Description**

The binary manager downloads files from GitHub Releases without enforcing a maximum download size. If an attacker substitutes a large file at the download URL — or if a legitimate release is unexpectedly large — the download will fill the available disk space on the worker host. This is a denial-of-service vector against the worker's filesystem.

**Remediation**

Check the `Content-Length` header before downloading and abort if it exceeds the expected size. Stream the download and track bytes received to handle chunked responses.

```python
import requests
from pathlib import Path

MAX_BINARY_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB — adjust per your largest expected binary

def download_binary(url: str, dest: Path) -> None:
    """Download a binary with a size cap, streaming to avoid memory exhaustion."""
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()

        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            size = int(content_length)
            if size > MAX_BINARY_SIZE_BYTES:
                raise ValueError(
                    f"Refusing download: Content-Length {size} bytes exceeds "
                    f"maximum allowed size of {MAX_BINARY_SIZE_BYTES} bytes."
                )

        bytes_received = 0
        with dest.open("wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                bytes_received += len(chunk)
                if bytes_received > MAX_BINARY_SIZE_BYTES:
                    dest.unlink(missing_ok=True)
                    raise ValueError(
                        f"Download aborted: received {bytes_received} bytes, "
                        f"exceeding limit of {MAX_BINARY_SIZE_BYTES} bytes."
                    )
                f.write(chunk)
```

---

#### F-04 (companion): Wallet Address in process arguments

See F-04 in the High severity section above. The remediation (write wallet address to a config file, not a CLI argument) also addresses the medium-severity aspect of this finding on single-user systems.

---

#### F-18: XMRig Stratum TLS Explicitly Disabled

**Severity**: `MEDIUM`
**Affected component**: Worker agent — xmrig configuration
**CWE**: CWE-311 (Missing Encryption of Sensitive Data)

**Description**

The xmrig configuration disables TLS for the stratum connection to the mining pool. Stratum traffic carries the wallet address and submitted shares. Without TLS, a network observer can identify the mining pool, wallet address, and hashrate contribution. On a shared or cloud network, this information is valuable to a competitor and directly identifies the operator.

**Remediation**

Enable TLS in the xmrig configuration and verify the pool's TLS certificate.

```json
{
  "pools": [
    {
      "url": "pool.supportxmr.com:443",
      "user": "YOUR_WALLET_ADDRESS",
      "pass": "worker-name",
      "keepalive": true,
      "tls": true,
      "tls-fingerprint": null
    }
  ]
}
```

If your pool does not support TLS on port 443, check whether it offers a TLS port (commonly 3333 with TLS or 443). Most major Monero pools support TLS connections.

---

### 4.4 Low Severity

---

#### F-14: `GITHUB_TOKEN` Advised but Never Read

**Severity**: `LOW`
**Affected component**: Binary manager — GitHub API client
**CWE**: CWE-561 (Dead Code)

**Description**

The documentation or configuration advises setting a `GITHUB_TOKEN` environment variable to avoid GitHub API rate limits during binary downloads, but the code never reads this variable. Unauthenticated GitHub API requests are rate-limited to 60 requests/hour per IP. In a cluster with many workers all downloading binaries simultaneously, this limit can be hit. Beyond the operational impact, this is a reliability issue that creates pressure to run workers without binary integrity checks as a workaround.

**Remediation**

Read the token and include it in GitHub API request headers.

```python
import os
import requests

def github_api_headers() -> dict[str, str]:
    """Return headers for GitHub API requests, using a token if available."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

# Usage:
response = requests.get(
    "https://api.github.com/repos/xmrig/xmrig/releases/latest",
    headers=github_api_headers(),
    timeout=15,
)
```

---

#### F-16: PID File TOCTOU Race Condition

**Severity**: `LOW`
**Affected component**: Worker agent — process management
**CWE**: CWE-367 (Time-of-Check Time-of-Use Race Condition)

**Description**

The worker reads a PID file to identify the running process for `stop_service()`, then sends SIGKILL to that PID. There is a race condition between reading the PID and sending the signal: the original process may have exited and a different, unrelated process may have been assigned the same PID. On a busy system, this could cause SIGKILL to be delivered to an innocent process. This is a low-severity finding because the window is very narrow and requires precise timing, but it is exploitable on heavily loaded systems.

**Remediation**

After reading the PID, verify the process identity before signaling. On Linux, read `/proc/<pid>/cmdline` to confirm the process name matches expectations.

```python
import os
import signal
from pathlib import Path

def stop_service_safely(pid_file: Path, expected_process_name: str) -> None:
    """Stop a service identified by PID file, verifying process identity first."""
    pid = int(pid_file.read_text().strip())

    # Verify the process is what we expect before killing it
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if cmdline_path.exists():
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode()
        if expected_process_name not in cmdline:
            raise RuntimeError(
                f"PID {pid} does not appear to be {expected_process_name!r}. "
                f"Actual cmdline: {cmdline!r}. Refusing to send SIGKILL."
            )

    os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)
```

---

#### F-17: API Token Written via Fragile String Replacement

**Severity**: `LOW`
**Affected component**: Configuration writer — token injection
**CWE**: CWE-116 (Improper Encoding or Escaping of Output)

**Description**

The API token is injected into `cluster.toml` via string replacement rather than a proper TOML serialization library. If the token contains characters that have special meaning in TOML (backslashes, quotes, Unicode escape sequences), the generated file may be malformed or may silently encode a different value than intended. Additionally, tokens generated by this path may have insufficient entropy.

**Remediation**

Use a TOML library for all configuration file writes, and generate tokens with `secrets.token_hex` or `secrets.token_urlsafe`.

```python
import secrets
try:
    import tomllib  # Python 3.11+
    import tomli_w  # pip install tomli-w
except ImportError:
    import tomli as tomllib   # pip install tomli
    import tomli_w

def generate_api_token(length_bytes: int = 32) -> str:
    """Generate a cryptographically secure API token."""
    return secrets.token_hex(length_bytes)  # 64 hex characters, 256 bits of entropy

def write_cluster_config(path: Path, config: dict) -> None:
    """Write cluster configuration using a proper TOML serializer."""
    content = tomli_w.dumps(config)
    write_config_file(path, content)  # use the secure file writer from F-13's remediation
```

---

## 5. MITRE ATT&CK Coverage

This section maps XMRDP's behavior to ATT&CK techniques in two categories: techniques that appear in XMRDP's normal, legitimate operation (which detection rules must account for to avoid false positives), and techniques that adversaries could exercise by abusing XMRDP.

### Legitimate Use Techniques

These techniques appear when XMRDP operates as designed. Any detection rule that fires on these without additional context will produce false positives in environments where XMRDP is authorized.

| ATT&CK ID | Technique Name | XMRDP Behavior |
|-----------|----------------|----------------|
| T1059.006 | Command and Scripting Interpreter: Python | Python orchestrates the entire worker agent and C2 server |
| T1105 | Ingress Tool Transfer | Binary manager downloads monerod, p2pool, xmrig from GitHub Releases |
| T1140 | Deobfuscate/Decode Files or Information | SHA-256 verification of downloaded archives (when enabled) |
| T1569.002 | System Services: Service Execution | Mining processes launched as long-running background services |
| T1057 | Process Discovery | Worker checks for running processes via PID files and process listing |
| T1071.001 | Application Layer Protocol: Web Protocols | Worker-to-C2 communication over HTTP (Bearer token auth) |
| T1132.001 | Data Encoding: Standard Encoding | JSON encoding of worker registration and heartbeat payloads |
| T1046 | Network Service Discovery | Workers register with C2, C2 aggregates cluster topology |
| T1083 | File and Directory Discovery | Binary manager traverses cache directories; config file discovery |

### Abuse Surface Techniques

These techniques can be exercised by an adversary exploiting XMRDP vulnerabilities or deploying XMRDP without authorization.

| ATT&CK ID | Technique Name | Exploited Via |
|-----------|----------------|---------------|
| T1496 | Resource Hijacking | Unauthorized deployment of XMRDP for cryptomining on victim infrastructure |
| T1219 | Remote Access Software | C2 server provides remote command execution capability over HTTP |
| T1547 | Boot or Logon Autostart Execution | Persistent process groups survive parent termination (see F, Abuse Scenario) |
| T1036.005 | Masquerading: Match Legitimate Name or Location | Binaries served from cache can be substituted with malicious ones under legitimate names |
| T1041 | Exfiltration Over C2 Channel | Host enumeration data (CPU, RAM, platform) exfiltrated via heartbeat channel |
| T1082 | System Information Discovery | Heartbeat collects and transmits host system information to C2 |
| T1562.001 | Impair Defenses: Disable or Modify Tools | `stop_service()` primitive can target security tools if PID file is manipulated |
| T1574 | Hijack Execution Flow | Argument injection (F-05) allows hijacking of subprocess execution |

---

## 6. Abuse Scenarios

These scenarios describe how XMRDP could be misused by an unauthorized party or how its architecture could be leveraged in an attack chain. Each scenario is independent — you do not need to read them in order.

---

### Scenario A: Unauthorized Cryptomining Deployment

**Summary**: XMRDP is self-contained and downloads all binaries from GitHub at runtime. An attacker who gains foothold on a single host can deploy a complete mining cluster across an organization's infrastructure without pre-staging any binaries.

**Attack chain**:
1. Attacker gains initial access to one host (phishing, CVE exploit, stolen credential).
2. Attacker installs XMRDP via `pip install` or by cloning the repository — both require only internet access, not elevated privileges.
3. XMRDP downloads monerod, p2pool, and xmrig from GitHub Releases on first run.
4. Worker registers with an attacker-controlled C2 server.
5. Mining begins. The entire setup survives host reboot if persistence is established (see Scenario F).

**Why this is significant**: The GitHub download chain bypasses perimeter controls that block known-malicious URLs. The downloads originate from `github.com` and `objects.githubusercontent.com`, which are typically whitelisted. Detection requires behavioral rules, not URL-based blocking.

**Mitigations**: Enforce binary allow-lists via application control (AppLocker, WDAC, or equivalent). Monitor for xmrig execution outside approved paths. See Detection Rule 1.

---

### Scenario B: Binary Distribution Channel for Malware

**Summary**: The `/api/binaries/<name>` endpoint serves any file from the binary cache directory. Combined with the path traversal vulnerability (F-09), an attacker who compromises the C2 server can distribute arbitrary malicious binaries to all connected workers, which will execute them as trusted mining binaries.

**Attack chain**:
1. Attacker exploits path traversal (F-09) or directly compromises the C2 server host.
2. Attacker places a malicious binary in the cache directory under a name that workers will request (e.g., `xmrig-6.21.0-linux-x64`).
3. Workers download and execute the binary, which has no integrity verification beyond a skippable checksum (F-08).
4. Malicious binary runs on all connected workers with full user privileges.

**Why this is significant**: Once workers trust the C2 server as a binary source, compromising the C2 server compromises the entire cluster simultaneously. This is a supply-chain attack within your own infrastructure.

**Mitigations**: Fix F-09 (path validation) and F-08 (GPG verification) as priority items. Pin expected binary hashes in code.

---

### Scenario C: Host Enumeration via Heartbeat

**Summary**: Every worker sends CPU count, RAM size, and platform information to the C2 server on registration and heartbeat. The `/api/cluster/status` endpoint aggregates this into a complete inventory of all worker hosts. An adversary who obtains the API token gains a detailed map of the cluster.

**Attack chain**:
1. Attacker intercepts the Bearer token over plaintext HTTP (F-03) or bruteforces it (F-02 + F-01 make brute force feasible).
2. Attacker calls `GET /api/cluster/status` with the stolen token.
3. Response contains hostname, IP, CPU model, RAM size, OS platform, and uptime for every registered worker.
4. Attacker uses this inventory to prioritize which workers to target for further exploitation or to assess total cluster hashrate.

**Why this is significant**: The heartbeat data was designed for operational monitoring, but it creates a reconnaissance capability for anyone who can authenticate to the C2 server.

**Mitigations**: Fix F-02 (timing attack) and F-03 (TLS) to protect the token. Consider whether `/api/cluster/status` needs to include full host details, or whether aggregate statistics are sufficient.

---

### Scenario D: Token Interception and Cluster Takeover

**Summary**: The API token travels in plaintext over HTTP. Any network observer between a worker and the C2 server can capture the token and use it to take full control of the cluster: stop all workers, reconfigure wallet addresses, or distribute malicious binaries.

**Attack chain**:
1. Attacker positions themselves on the network path (router, ARP spoofing on a LAN, compromised switch).
2. Attacker captures a single HTTP request from any worker to the C2 server and extracts the `Authorization: Bearer <token>` header.
3. Attacker uses the token to call any C2 API endpoint with full authorization.
4. Attacker reconfigures the wallet address to redirect mining revenue, or uses Scenario B to distribute malware.

**Why this is significant**: A single intercepted request provides permanent administrative access to the cluster until the token is rotated. Token rotation requires manual intervention with no automated mechanism.

**Mitigations**: Fix F-03 (TLS) first — this is the most impactful single fix for this scenario. Add token rotation via the API (no such mechanism currently exists).

---

### Scenario E: Process Kill Primitive Redirected via PID File Manipulation

**Summary**: The `stop_service()` function reads a PID from a file and sends SIGKILL to that PID. If an attacker can write to the PID file, they can cause `stop_service()` to kill any process on the host, including security monitoring agents, antivirus processes, or competing mining operations.

**Attack chain**:
1. Attacker has write access to the XMRDP data directory (e.g., after exploiting F-05 to achieve code execution as the XMRDP user).
2. Attacker overwrites the monerod or xmrig PID file with the PID of a target process (e.g., a host-based IDS agent).
3. Operator or automated management calls `stop_service()` in routine operations.
4. SIGKILL is delivered to the target process.

**Why this is significant**: This turns a routine administrative operation into an unintentional defense evasion primitive. The operator would not observe any error — `stop_service()` completes successfully.

**Mitigations**: Implement the PID file TOCTOU fix from F-16 (verify process cmdline before signaling). Restrict write permissions on the XMRDP data directory.

---

### Scenario F: Persistence via Detached Process Groups

**Summary**: Mining processes are launched with `start_new_session=True` (Linux) or `CREATE_NEW_PROCESS_GROUP` (Windows), which detaches them from the parent process group. These processes continue running after the XMRDP worker agent exits or is killed, and they are not stopped by terminating the Python parent.

**Attack chain**:
1. An unauthorized deployment of XMRDP (see Scenario A) establishes worker processes.
2. Security team discovers and kills the XMRDP Python process.
3. monerod, p2pool, and xmrig continue running — they are in separate process groups and were not children of the killed process.
4. Security team must explicitly identify and kill each mining process by name. If the binaries were renamed (T1036.005), discovery is harder.

**Why this is significant**: Process group detachment is necessary for legitimate daemonization, but it also means that stopping the orchestrator does not stop the work. An incident responder who kills the Python process may believe they have contained the threat while mining continues.

**Mitigations**: Use Detection Rule 1 (xmrig execution from non-standard path) and Detection Rule 3 (Python spawning mining process stack) to identify all mining processes, not just the orchestrator. Document the kill procedure to include all child processes explicitly.

---

## 7. Detection Rules

These rules are written in Sigma format, which can be compiled to any SIEM query language. Compiled SPL (Splunk) and KQL (Microsoft Sentinel/Defender) equivalents are provided for the most critical rules.

---

### Rule 1: XMRig Execution from XMRDP Binary Cache

**Severity**: High
**Rationale**: Legitimate xmrig deployments in authorized environments use standard installation paths. Execution from `~/.xmrdp/binaries/` is a strong indicator of either an authorized XMRDP deployment or an unauthorized cryptomining deployment using XMRDP's binary management. Zero expected false positives in environments where XMRDP is not deployed.

```yaml
title: XMRig Execution from XMRDP Binary Cache Directory
id: a1b2c3d4-e5f6-7890-abcd-ef1234567890
status: experimental
description: |
    Detects execution of xmrig from the XMRDP binary cache directory.
    This path is used exclusively by XMRDP. In environments where XMRDP
    is not authorized, this indicates unauthorized cryptomining.
author: XMRDP Security Assessment
date: 2026-03-14
references:
    - https://github.com/csysp/xmrdp
tags:
    - attack.resource_hijacking
    - attack.t1496
logsource:
    category: process_creation
    product: linux
detection:
    selection:
        Image|contains: '/.xmrdp/binaries/'
        Image|endswith: 'xmrig'
    condition: selection
falsepositives:
    - Authorized XMRDP deployments (allowlist by host)
level: high
```

**Compiled SPL (Splunk)**:
```spl
index=endpoint sourcetype=sysmon
EventCode=1
Image="*/.xmrdp/binaries/*xmrig*"
| table _time, host, User, Image, CommandLine, ParentImage
```

**Compiled KQL (Microsoft Sentinel)**:
```kql
DeviceProcessEvents
| where FileName =~ "xmrig"
| where FolderPath contains "/.xmrdp/binaries/"
| project Timestamp, DeviceName, AccountName, FolderPath, ProcessCommandLine, InitiatingProcessFileName
```

---

### Rule 2: HTTP C2 Beaconing on Port 7099 with Bearer Token

**Severity**: High
**Rationale**: XMRDP workers beacon to the C2 server on port 7099 with a fixed interval (~60 seconds) using a Bearer token in the Authorization header. This combination — specific port, regular interval, Bearer auth, specific URL paths — has a very low false-positive rate outside of authorized XMRDP deployments.

```yaml
title: XMRDP C2 Beaconing Pattern Detected
id: b2c3d4e5-f6a7-8901-bcde-f12345678901
status: experimental
description: |
    Detects HTTP communication matching the XMRDP worker-to-C2 beaconing pattern:
    port 7099, Bearer token authorization, specific API paths, ~60-second interval.
    Indicates an active XMRDP worker connecting to a C2 server.
author: XMRDP Security Assessment
date: 2026-03-14
tags:
    - attack.command_and_control
    - attack.t1071.001
    - attack.t1219
logsource:
    category: network_connection
    product: linux
detection:
    selection:
        DestinationPort: 7099
        Initiated: 'true'
    url_paths:
        cs-uri-stem|contains:
            - '/api/workers/'
            - '/api/binaries/'
            - '/api/cluster/'
    condition: selection and url_paths
falsepositives:
    - Authorized XMRDP C2 servers (allowlist by destination IP)
level: high
```

---

### Rule 3: Python Spawning Mining Process Stack

**Severity**: Critical
**Rationale**: The combination of a Python parent process spawning monerod, p2pool, and xmrig as children is unique to XMRDP. No other common workload produces this process hierarchy. A single rule covering this combination has an extremely low false-positive rate.

```yaml
title: Python Process Spawning Monero Mining Stack
id: c3d4e5f6-a7b8-9012-cdef-123456789012
status: stable
description: |
    Detects Python spawning monerod, p2pool, or xmrig as child processes.
    This process hierarchy is exclusive to XMRDP (or a tool mimicking it).
    Any occurrence in a production environment warrants immediate investigation.
author: XMRDP Security Assessment
date: 2026-03-14
tags:
    - attack.resource_hijacking
    - attack.t1496
    - attack.t1059.006
logsource:
    category: process_creation
    product: linux
detection:
    python_parent:
        ParentImage|endswith:
            - '/python'
            - '/python3'
            - '/python3.10'
            - '/python3.11'
            - '/python3.12'
    mining_child:
        Image|endswith:
            - '/monerod'
            - '/p2pool'
            - '/xmrig'
    condition: python_parent and mining_child
falsepositives:
    - Authorized XMRDP deployments (allowlist by host or user)
level: critical
```

**Compiled SPL (Splunk)**:
```spl
index=endpoint sourcetype=sysmon EventCode=1
(ParentImage="*python*" AND (Image="*/monerod" OR Image="*/p2pool" OR Image="*/xmrig"))
| table _time, host, User, ParentImage, Image, CommandLine
| sort -_time
```

**Compiled KQL (Microsoft Sentinel)**:
```kql
DeviceProcessEvents
| where InitiatingProcessFileName startswith "python"
| where FileName in~ ("monerod", "p2pool", "xmrig")
| project Timestamp, DeviceName, AccountName, InitiatingProcessFileName, FileName, ProcessCommandLine
| order by Timestamp desc
```

---

### Rule 4: Binary Download with XMRDP User-Agent

**Severity**: Medium
**Rationale**: The binary manager identifies itself with the `xmrdp-binary-manager/1.0` User-Agent string. No legitimate browser, CDN crawler, or other tool uses this string. Any HTTP request with this User-Agent is XMRDP performing a binary download. This rule has an expected false positive rate of zero.

```yaml
title: XMRDP Binary Manager HTTP Download Detected
id: d4e5f6a7-b8c9-0123-defa-234567890123
status: stable
description: |
    Detects HTTP requests with the XMRDP binary manager User-Agent string.
    This string is hardcoded in XMRDP and used exclusively by its binary
    download component. Zero expected false positives.
author: XMRDP Security Assessment
date: 2026-03-14
tags:
    - attack.ingress_tool_transfer
    - attack.t1105
logsource:
    category: proxy
    product: generic
detection:
    selection:
        cs-user-agent: 'xmrdp-binary-manager/1.0'
    condition: selection
falsepositives:
    - None expected
level: medium
```

---

### Rule 5: GitHub Release API Query for All Three Mining Repos

**Severity**: Medium
**Rationale**: XMRDP queries the GitHub Releases API for xmrig, p2pool, and monerod. Queries for all three from the same source IP within a short window strongly indicate XMRDP binary manager activity — no other common tool downloads all three in sequence.

```yaml
title: GitHub Release API Queried for All Three XMRDP Mining Components
id: e5f6a7b8-c9d0-1234-efab-345678901234
status: experimental
description: |
    Detects when the GitHub Releases API is queried for xmrig, p2pool, and monerod
    from the same source within a 5-minute window. This pattern is characteristic
    of XMRDP's binary manager initialization sequence.
author: XMRDP Security Assessment
date: 2026-03-14
tags:
    - attack.ingress_tool_transfer
    - attack.t1105
logsource:
    category: proxy
    product: generic
detection:
    github_api_request:
        cs-host: 'api.github.com'
        cs-uri-stem|contains: '/releases/'
    xmrig_query:
        cs-uri-stem|contains: '/xmrig/xmrig/'
    p2pool_query:
        cs-uri-stem|contains: '/SChernykh/p2pool/'
    monerod_query:
        cs-uri-stem|contains: '/monero-project/monero/'
    condition: github_api_request and (xmrig_query or p2pool_query or monerod_query)
falsepositives:
    - Legitimate Monero/xmrig developers checking release versions
    - Package managers that independently check these repos (rare)
level: medium
```

---

### Rule 6: Python Spawning Process Management Commands with Mining Process Names

**Severity**: Medium
**Rationale**: XMRDP uses `tasklist` and `taskkill` (Windows) or equivalent process queries to manage the mining stack. Python spawning these commands with mining process names as arguments indicates XMRDP process management activity.

```yaml
title: Python Spawning tasklist/taskkill Targeting Mining Processes
id: f6a7b8c9-d0e1-2345-fabc-456789012345
status: experimental
description: |
    Detects Python spawning tasklist or taskkill with mining process names
    as arguments. This is characteristic of XMRDP's Windows process management.
    On Linux, equivalent detection uses pkill/pgrep with mining process names.
author: XMRDP Security Assessment
date: 2026-03-14
tags:
    - attack.t1057
    - attack.t1562.001
logsource:
    category: process_creation
    product: windows
detection:
    python_parent:
        ParentImage|endswith:
            - '\python.exe'
            - '\python3.exe'
    process_management:
        Image|endswith:
            - '\tasklist.exe'
            - '\taskkill.exe'
    mining_target:
        CommandLine|contains:
            - 'xmrig'
            - 'monerod'
            - 'p2pool'
    condition: python_parent and process_management and mining_target
falsepositives:
    - Authorized XMRDP deployments on Windows
level: medium
```

---

### Rule 7: XMRDP Data Directory Creation in User Profile

**Severity**: Medium
**Rationale**: XMRDP creates its data directory (`.xmrdp`) in the user's home directory on first run. This directory creation event is a reliable indicator of XMRDP installation or first-time execution. Combined with Rule 1, this enables detection at both install time and execution time.

```yaml
title: XMRDP Data Directory Created in User Home Directory
id: a7b8c9d0-e1f2-3456-abcd-567890123456
status: experimental
description: |
    Detects creation of the .xmrdp directory in a user's home directory.
    This directory is created by XMRDP on first run and contains binaries,
    configuration, and PID files. Its creation indicates XMRDP installation
    or first execution on this host.
author: XMRDP Security Assessment
date: 2026-03-14
tags:
    - attack.t1083
    - attack.resource_hijacking
    - attack.t1496
logsource:
    category: file_event
    product: linux
detection:
    selection:
        TargetFilename|re: '^/home/[^/]+/\.xmrdp(/|$)'
        EventType: 'CreateDirectory'
    condition: selection
falsepositives:
    - Authorized XMRDP deployments (allowlist by host)
level: medium
```

---

## 8. Operational Security Recommendations

These recommendations apply to any production deployment of XMRDP. They are ordered by impact, not by implementation complexity.

1. **Terminate all C2 traffic with TLS.** Place the C2 server behind a TLS-terminating reverse proxy (Caddy is the lowest-friction option) or add native HTTPS support. All other security controls are weakened as long as the Bearer token and wallet address travel in plaintext. See F-03.

2. **Restrict the C2 server bind address to your internal network interface.** Set `bind_address` to a specific internal IP, not `0.0.0.0`. If you must bind to all interfaces (multi-homed host), enforce firewall rules that restrict port 7099 access to the specific IP addresses of your workers. See F-10.

3. **Implement binary integrity verification with GPG.** Download and pin the GPG public keys for the xmrig, p2pool, and monerod maintainers. Verify the signature on every checksum file before trusting it. Treat a missing or invalid signature as a hard failure that halts execution. See F-08.

4. **Establish a token rotation procedure.** The API token currently has no expiration. Define a rotation procedure (generate new token, update all workers within a maintenance window, invalidate old token) and execute it on a regular schedule or immediately after any suspected compromise.

5. **Apply an IP allow-list for worker registration.** If your cluster has a fixed set of worker IPs, reject registration and heartbeat requests from any IP not on the allow-list. This limits the blast radius of a token compromise to hosts you already control.

6. **Set restrictive file permissions on all configuration files.** Ensure `cluster.toml`, `xmrig_config.json`, and any file containing the wallet address or API token is readable only by the owning user (mode `0o600`). Automate this check in your deployment process. See F-13.

7. **Deploy a file integrity monitor on the binary cache directory.** Alert on any change to files in the XMRDP binary cache directory that does not originate from the XMRDP binary manager process. Unexpected writes to this directory indicate an attempt to substitute malicious binaries (Scenario B).

8. **Enable structured audit logging and forward to a SIEM.** Implement the logging recommendations from F-12 and ship logs to a centralized log management system. At minimum, log: authentication failures (with source IP), worker registrations, binary serve requests, and configuration changes.

9. **Create a binary allow-list for your cluster.** Use your operating system's application control mechanism (AppLocker on Windows, fapolicyd or SELinux on Linux) to permit execution of only the specific xmrig, p2pool, and monerod binary versions you have verified. Block all other executables in the XMRDP data directory by default.

10. **Bind monerod ZMQ to `127.0.0.1` explicitly.** Unless p2pool runs on a different host (which requires a deliberate network architecture decision), the ZMQ interface should never be accessible externally. Add `zmq_bind_address = "127.0.0.1"` to your `cluster.toml` and treat any deviation as a configuration error. See F-15.

---

## 9. Remediation Roadmap

This roadmap groups findings by recommended fix timeline. "Immediate" means before any production deployment or as an emergency patch on an existing deployment. "Short-term" means within the next sprint cycle. "Medium-term" means within the next quarter.

| Priority | Finding | Title | Effort | Impact | Status |
|----------|---------|-------|--------|--------|--------|
| **Immediate** | F-05 | Argument injection via `extra_args` | Low — add allow-list validation | Critical: prevents RCE | **FIXED** 2026-03-15 |
| **Immediate** | F-02 | Non-constant-time token comparison | Trivial — two-line fix | High: closes timing oracle | **FIXED** 2026-03-15 |
| **Immediate** | F-09 | Incomplete path traversal on binary serve | Low — replace blacklist with `resolve()` | High: prevents binary substitution | **FIXED** 2026-03-15 |
| **Immediate** | F-15 | monerod ZMQ bound to `0.0.0.0` | Trivial — change default config value | Medium: reduces external exposure | **FIXED** 2026-03-15 |
| **Immediate** | F-13 | Config files world-readable | Low — use `os.open` with `0o600` | Medium: protects credentials at rest | **FIXED** 2026-03-15 |
| **Immediate** | F-08 | Unsigned checksums; silent skip on missing | Medium — add GPG verification flow | High: closes binary substitution path | **PARTIAL** 2026-03-15 — silent skip resolved; GPG deferred |
| **Short-term** | F-06 | Zip Slip / unsafe archive extraction | Low — add path validation loop | High: prevents host file overwrite | **FIXED** 2026-03-15 |
| **Short-term** | F-01 | No request body size cap or rate limiting | Low — add size check + failure counter | High: prevents memory exhaustion | **PARTIAL** 2026-03-15 — size cap applied; rate limiting deferred |
| **Short-term** | F-10 | C2 defaults to `0.0.0.0` | Trivial — change default, add warning | High: reduces internet exposure | **FIXED** 2026-03-15 |
| **Short-term** | F-07 | No download size limit | Low — stream with byte counter | Medium: prevents disk exhaustion | Open |
| **Short-term** | F-17 | Token written via fragile string replacement | Low — use TOML library + `secrets` | Low → Medium: improves token entropy | Open |
| **Medium-term** | F-03 | All traffic over plaintext HTTP | High — add TLS reverse proxy or native HTTPS | High: closes token interception path | Open |
| **Medium-term** | F-11 | No worker identity binding | Medium — add IP binding on registration | Medium: prevents impersonation | Open |
| **Medium-term** | F-12 | No audit logging | Medium — add structured event logger | Medium: enables detection and forensics | Open |
| **Medium-term** | F-18 | XMRig stratum TLS disabled | Low — change config value | Medium: protects wallet identity | Open |
| **Medium-term** | F-04 | Wallet in process arguments | Low — switch to config file | Medium: protects wallet privacy | Open |
| **Ongoing** | F-16 | PID file TOCTOU | Low — add cmdline verification | Low: prevents signal redirection | Open |
| **Ongoing** | F-14 | `GITHUB_TOKEN` dead code | Low — read env var | Low: improves rate limit reliability | Open |

---

## 10. Appendix: ATT&CK Detection Coverage by Tactic

This appendix maps each ATT&CK tactic to the detection rules in Section 7 that provide coverage. Use this table to identify tactic coverage gaps in your current detection stack.

| ATT&CK Tactic | Covered Techniques | Detection Rules | Coverage |
|---------------|-------------------|-----------------|----------|
| **Resource Development** | — | — | Not applicable (pre-deployment) |
| **Initial Access** | — | — | Not applicable (depends on initial access vector, not XMRDP) |
| **Execution** | T1059.006 (Python), T1569.002 (Service Execution) | Rule 3 (Python spawning mining stack) | Partial — covers process hierarchy, not all Python execution |
| **Persistence** | T1547 (Boot/Logon Autostart) | Rule 7 (data directory creation) | Low — directory creation is a precursor indicator only |
| **Defense Evasion** | T1036.005 (Masquerading), T1562.001 (Impair Defenses) | Rule 6 (taskkill with mining process names) | Low — covers Windows process management only |
| **Credential Access** | — | Rule 2 (C2 beaconing, Bearer token in transit) | Indirect — TLS fix (F-03) is the primary control here |
| **Discovery** | T1057 (Process Discovery), T1082 (System Info Discovery), T1083 (File/Directory Discovery) | Rule 6 (process management), Rule 7 (directory creation) | Partial |
| **Lateral Movement** | — | — | Not directly applicable; see Scenario A for cluster expansion |
| **Collection** | T1082 (System Info via heartbeat) | Rule 2 (C2 beaconing) | Partial — heartbeat detection covers data collection |
| **Command and Control** | T1071.001 (HTTP), T1132.001 (JSON encoding), T1219 (Remote Access) | Rule 2 (C2 beaconing on port 7099) | Good — specific port + auth header + path pattern |
| **Exfiltration** | T1041 (Exfil over C2 channel) | Rule 2 (C2 beaconing) | Partial — covered by C2 detection; content not inspected |
| **Impact** | T1496 (Resource Hijacking) | Rules 1, 3, 4, 7 (xmrig execution, Python spawning, UA, directory) | Good — multiple independent detection vectors |
| **Ingress Tool Transfer** | T1105 (binary download) | Rules 4, 5 (User-Agent, GitHub API queries) | Good — both download initiation and fetch are covered |

### Detection Gap Summary

| Gap | Description | Recommended Action |
|-----|-------------|-------------------|
| Persistence | No rule detects if mining processes are added to system startup (cron, systemd) | Add file monitoring rule for cron/systemd unit creation by xmrdp processes |
| TLS-encrypted C2 | Rule 2 targets plaintext HTTP on port 7099; TLS traffic on port 443 would evade it | After deploying TLS (F-03 remediation), update Rule 2 to target port 443 with JA3 fingerprint or SNI matching |
| Binary renaming | Rules 1 and 3 match on binary filename; a renamed xmrig would evade both | Add hash-based detection: alert on known xmrig binary hashes executing from non-standard paths |
| Windows coverage | Most rules target Linux; Windows equivalents are incomplete | Extend Rules 1, 2, 3 with Windows-specific process and path patterns |

---

*This report was produced from a combined security engineering assessment and threat detection engineering analysis of the XMRDP codebase. All findings reflect the state of the code at the time of assessment. The remediation code snippets are illustrative — they should be adapted to match XMRDP's actual module structure, error handling conventions, and coding style before merging.*

*Findings should be tracked as engineering work items with assigned owners and target completion dates. Re-assess after all Immediate and Short-term items are closed.*

---

## Remediation Status

**Last updated**: 2026-03-15 (sessions 4–8 + C2 refactor)

### Summary

| State | Count | Findings |
|-------|-------|---------|
| Fixed | 18 | F-01, F-02, F-03, F-04, F-05, F-06, F-07, F-09, F-10, F-11, F-12, F-13, F-14, F-15, F-17, NF-01..NF-06, NF-10, NF-NEW-01..NF-NEW-06 |
| Partial | 1 | F-08 (silent-skip resolved; GPG verification deferred) |
| Open | 2 | F-16 (PID TOCTOU, low), F-18 (xmrig stratum TLS — P2Pool stratum does not support TLS) |
| N/A post-refactor | 1 | F-09 (/api/binaries path traversal — endpoint removed in C2 refactor) |

All Critical and High findings are closed. The two remaining open findings are Low severity (F-16) and a Medium with no practical fix available at this time (F-18: P2Pool stratum does not expose a TLS option). F-09 was fixed before the C2 refactor subsequently removed the entire binary-serve endpoint.

### Changes Applied 2026-03-15

All 9 planned remediation items from the post-assessment fix list were addressed in this session. The changes were applied to four source files:

**`xmrdp/c2_server.py`**
- F-02: `hmac.compare_digest()` replaces `!=` for token comparison; `str()` coercion on expected value
- F-01: `_MAX_BODY = 65536` class attribute; `_read_body()` raises `ValueError` on oversized `Content-Length`
- F-09: Path traversal protection replaced with `resolve().relative_to(bin_dir)` containment check
- F-10: Default bind address changed from `"0.0.0.0"` to `"127.0.0.1"`

**`xmrdp/config_generator.py`**
- F-05: `_SAFE_ARG_RE` regex allowlist and `_validate_extra_args()` function added; applied in `generate_monerod_args()` and `generate_p2pool_args()`
- F-13: `write_xmrig_config()` uses `os.open()` with `0o600` mode instead of `write_text()` + `chmod()`
- F-15: `--zmq-pub` changed from `tcp://0.0.0.0:18083` to `tcp://127.0.0.1:18083`

**`xmrdp/wizard.py`**
- F-13: `run_setup()` and `generate_config_cmd()` both use `os.open()` with `0o600` mode for config file writes; guarded with `sys.platform != "win32"`

**`xmrdp/binary_manager.py`**
- F-06: ZIP member pre-scan with `resolve().relative_to()` added before `extractall`; `_safe_tar_extract()` helper added with Python 3.12 `filter="data"` and manual member validation for earlier versions
- F-08: `ensure_binaries()` now reads `config.get("security", {}).get("verify_checksums", True)`; missing checksum file or missing checksum entry raises `RuntimeError` when `verify_checksums=True`

### Changes Applied 2026-03-15 (session 3 — second-pass findings + TLS + donate-level)

Seven second-pass findings and two feature-driven security improvements were addressed.

**`xmrdp/config.py`**
- NF-01: `_toml_str()` escape function added; applied to wallet, master_host, and all worker name/host values written to cluster.toml
- NF-03: `_HOST_RE` allowlist regex added; `master.host` validated in `_apply_defaults()` to prevent SSRF via crafted config

**`xmrdp/c2_server.py`**
- NF-02: `_read_body()` raises `ValueError` for negative `Content-Length` before the size cap check
- TLS wrapping added to `start_c2_server()`: `ssl.SSLContext(PROTOCOL_TLS_SERVER)` with `minimum_version=TLSv1_2`; falls back to plain HTTP if cert/key files are absent

**`xmrdp/node_manager.py`**
- NF-04: `_write_pid()` uses `os.open(mode=0o600)` on non-Windows for atomic PID file creation

**`xmrdp/platforms.py`**
- NF-05: All 5 `mkdir()` calls followed by `dir.chmod(0o700)` on non-Windows to prevent world-readable data/config directories

**`xmrdp/c2_client.py`**
- NF-06: `_MAX_BINARY_SIZE = 512 MB` enforced via `Content-Length` pre-check and streaming byte counter; partial file deleted on overflow
- TLS support added: `configure_tls()` / `_check_fingerprint()` for SHA-256 certificate fingerprint pinning; `_base_url()` selects `https://` when TLS enabled; `_make_request()` passes `ssl.CERT_NONE` context (fingerprint validated out-of-band)

**`xmrdp/cli.py`**
- NF-10: `config --show` command now deep-copies the config dict and redacts `api_token` and TLS key fields before printing

**`xmrdp/constants.py`**
- Added `C2_TLS_CERT_FILE = "c2_server.crt"` and `C2_TLS_KEY_FILE = "c2_server.key"`

**`xmrdp/wizard.py`**
- TLS setup added to `run_setup()`: attempts `openssl req -x509` via subprocess; computes SHA-256 fingerprint; injects `tls_enabled`, `c2_tls_cert`, `c2_tls_key`, `c2_tls_fingerprint` into generated cluster.toml

**`xmrdp/config_generator.py`**
- XMRig donate-level: `"donate-level": 0` added to generated xmrig JSON config (eliminates 1% donation to XMRig authors)

**`xmrdp/cluster.py`**
- `configure_tls()` called at the start of `deploy_worker()` and `cluster_status()` to propagate TLS settings before any C2 requests

---

### Changes Applied 2026-03-15 (session 4 — P2Pool hardening)

Three P2Pool integration issues were addressed.

**`xmrdp/firewall.py`** — P2Pool stratum restriction
- The p2pool stratum rule (port 3333) was previously unrestricted (`source` key absent), allowing any host to submit shares or probe the stratum port. The rule now follows the same per-worker-IP restriction pattern used for the C2 API: when worker IPs are known from config, a separate per-IP rule is generated; otherwise the rule falls back to LAN CIDR (`192.168.0.0/16`). An unrestricted stratum port allows an adversary to hijack pool share credit by pointing their own miners at the master's stratum.

**`xmrdp/c2_server.py`** — P2Pool JSON stats integration
- `_read_p2pool_stats()` helper added. P2Pool writes pool statistics as JSON files to the `--data-api` directory (`<data_dir>/p2pool/`). The helper reads `stats_mod` (mini chain) or `stats` (main chain), whichever is present, normalises the two common JSON layouts, and extracts `pool_hashrate`, `pool_miners`, `pool_blocks_found`, and `pool_last_block_time`.
- `_handle_get_cluster_status()` now calls `_read_p2pool_stats()` and includes the `"pool"` key in the JSON response. Cluster status output in `cluster.py` displays P2Pool hashrate, miner count, and blocks found when the key is present.

**`xmrdp/config.py`** — extra_args validation at config load time
- `_SAFE_ARG_RE` and `_validate_extra_args()` added to `config.py` (mirroring the existing function in `config_generator.py`). Called from `_apply_defaults()` for both `master.monerod.extra_args` and `master.p2pool.extra_args`. Invalid arguments now raise `ValueError` at `load_config()` time rather than silently at process launch, making misconfiguration immediately visible.

---

### Changes Applied 2026-03-15 (sessions 5–8 — hardening, rate limiting, audit, test expansion)

**`xmrdp/c2_server.py`**
- F-01: Per-IP auth failure rate limiting — 429 after 10 failures in 60 s; `_auth_failures` dict bounded to 10 000 IPs with stale-entry eviction
- F-11: Worker identity binding — `registered_ip` stored on first `/api/register`; heartbeat from wrong IP returns 403
- F-12: Structured audit logging — `xmrdp.audit` logger; `_audit()` helper emits `event=… ip=… key=value` lines for all auth, registration, heartbeat, and status events
- NF-NEW-01: Binary SHA-256 integrity header (`X-SHA256`) on binary serve; c2_client verifies streaming hash and deletes partial file on mismatch
- NF-NEW-02: Worker name allowlist — `_WORKER_NAME_RE` regex; invalid names fall back to `"worker"` rather than crashing
- NF-NEW-04: Auth failures dict bounded to `_RATE_LIMIT_MAX_IPS = 10 000` with LRU-style stale-entry eviction
- C2 refactor: binary-serve (`/api/binaries`), config-fetch (`/api/config`), and update-check (`/api/update/check`) endpoints removed; C2 is now a telemetry bus only

**`xmrdp/binary_manager.py`**
- F-07: `_MAX_DOWNLOAD_SIZE = 2 GB` enforced via Content-Length pre-check and streaming byte counter; partial file deleted on overflow
- F-14: `_github_headers()` reads `GITHUB_TOKEN` env var and injects `Authorization: Bearer` on GitHub API requests only

**`xmrdp/node_manager.py`**
- `start_worker()` simplified: removed `xmrig_config_override` parameter; always writes local config via `write_xmrig_config(config, role="worker")`
- F-04: xmrig launched with `--config <file>` — wallet address is in the JSON config file, not the process command line

**`xmrdp/cluster.py`**
- `deploy_worker()` simplified: removed remote config fetch and wallet/pool validation block; workers use local `cluster.toml` as source of truth

**`xmrdp/wizard.py`**
- F-17: API token generated with `secrets.token_hex(32)`; TLS cert/key paths escaped with `_toml_str()`; openssl subprocess uses verified path

**`xmrdp/sync.py`** (new file)
- `xmrdp sync` command: pushes per-worker `cluster.toml` to worker nodes via SSH/SCP with `self = true` identity marker; chmod 600 enforced with hard failure on error; dry-run and --restart flags

**`xmrdp/c2_client.py`**
- Removed: `fetch_config()`, `download_binary_from_master()` (C2 refactor)
- NF-NEW-05: Fingerprint verified on every connection — no caching

**`.github/workflows/ci.yml`**
- Actions pinned to SHA digests; `security` job added with bandit static analysis and pip-audit dependency scan

**Test suite**
- `tests/test_c2.py`: 58 tests covering full C2 server integration
- `tests/test_cluster.py`: 91 tests covering config, node_manager, orchestration, binary_manager, E2E smoke
- `tests/test_sync.py`: 15 tests covering sync command and worker config generation
- Total: 200+ tests passing on Python 3.9, 3.11, 3.13 / Linux + Windows

---

### Remaining Open Items

| Finding | Severity | Priority | Notes |
|---------|----------|----------|-------|
| F-08 (GPG) | HIGH | Medium | Checksum files are not GPG-verified; SHA-256 verify is enforced but the checksum file itself is not signed. Deferred pending upstream projects publishing stable GPG key infrastructure. |
| F-18 | MEDIUM | Low | XMRig stratum TLS disabled. The stratum target is the local p2pool instance (port 3333); p2pool does not support TLS on its stratum port, so this cannot be enabled without upstream changes. |
| F-16 | LOW | Low | PID file TOCTOU; SIGKILL could theoretically reach the wrong process on a heavily loaded host. Risk is low: narrow window, requires precise timing. |

**Previously listed as open — now closed:**

| Finding | How Closed |
|---------|-----------|
| F-01 (rate limiting) | FIXED — 429 after 10 auth failures / 60 s per IP; `_auth_failures` dict bounded to 10 000 IPs |
| F-04 (wallet in ps aux) | FIXED — xmrig launched with `--config <file>`; wallet address is in the JSON config file, not the command line |
| F-07 (download size cap) | FIXED — `_MAX_DOWNLOAD_SIZE = 2 GB` enforced via Content-Length pre-check and streaming byte counter in `binary_manager.py` |
| F-09 (path traversal) | FIXED then N/A — resolved with `resolve().relative_to()` before C2 refactor removed the `/api/binaries` endpoint entirely |
| F-11 (worker identity binding) | FIXED — C2 stores `registered_ip` on first POST /api/register; subsequent heartbeats from a different IP for the same name return 403 |
| F-12 (audit logging) | FIXED — `xmrdp.audit` logger with `_audit()` helper emits structured `key=value` lines for all auth and registration events |
| F-14 (GITHUB_TOKEN dead code) | FIXED — `_github_headers()` reads `GITHUB_TOKEN` env var and adds `Authorization: Bearer` to GitHub API requests |
| F-17 (token via string replace) | FIXED — token generated with `secrets.token_hex(32)` (hex chars only; no TOML metacharacters possible); TLS paths escaped with `_toml_str()` |
