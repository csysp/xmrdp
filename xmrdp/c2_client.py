"""C2 client for worker nodes.

Handles registration, heartbeat reporting, and cluster status queries
against the master's telemetry bus.  Uses only stdlib -- no external
dependencies.
"""

import hashlib
import http.client
import json
import logging
import os
import ssl
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from xmrdp.platforms import detect_platform

log = logging.getLogger("xmrdp.c2client")

# ---------------------------------------------------------------------------
# TLS configuration (set once at startup via configure_tls)
# ---------------------------------------------------------------------------

_tls_config: dict = {"enabled": False, "fingerprint": ""}

_xmrig_http_token: str = ""


def configure_xmrig_token(token: str) -> None:
    """Configure the xmrig HTTP API token for local stats reading.

    Call once at worker startup with the token from cluster config.
    """
    global _xmrig_http_token
    _xmrig_http_token = token.strip() if token else ""


def configure_tls(enabled: bool, fingerprint: str = "") -> None:
    """Configure TLS for all outgoing C2 connections.

    Call this once before any C2 requests (e.g. in deploy_worker / cluster_status).
    """
    global _tls_config
    _tls_config = {"enabled": bool(enabled), "fingerprint": fingerprint.lower().strip()}


def _verify_peer_fingerprint(conn: "http.client.HTTPSConnection", host: str, port: int) -> None:
    """Verify the fingerprint of the peer cert from the live TLS connection.

    Reads the DER-encoded certificate directly from the established SSL socket
    (in-band), so there is no TOCTOU race between a probe connection and the
    real request connection.  Verification runs on every connection — no
    caching — so a cert swap after the first connection is always detected
    (NF-NEW-05).
    """
    expected = _tls_config["fingerprint"]
    if not expected:
        # No fingerprint configured; accept any valid TLS cert.
        return

    # getpeercert(binary_form=True) returns DER cert from the live connection — no TOCTOU
    sock = conn.sock
    if sock is None:
        raise ConnectionError("TLS connection has no socket — cannot verify fingerprint")

    # HTTPSConnection.sock is an ssl.SSLSocket
    der_cert = sock.getpeercert(binary_form=True)
    if not der_cert:
        raise ConnectionError("Server did not provide a certificate")

    actual = hashlib.sha256(der_cert).hexdigest().lower()
    if actual != expected:
        raise RuntimeError(
            f"C2 server certificate fingerprint mismatch!\n"
            f"  Expected: {expected}\n"
            f"  Actual:   {actual}\n"
            "Refusing to connect — possible MitM attack or cert rotation.\n"
            "If the cert was regenerated, update c2_tls_fingerprint in cluster.toml."
        )

    log.debug("TLS fingerprint verified for %s:%d (in-band)", host, port)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_url(host: str, port: int) -> str:
    """Build the base URL for the C2 API (http or https based on TLS config)."""
    scheme = "https" if _tls_config["enabled"] else "http"
    return f"{scheme}://{host}:{port}"


def _make_request(url: str, token: str, data=None, method=None):
    """Send an HTTP request to the C2 server and return parsed JSON.

    Parameters
    ----------
    url : str
        Full URL to request.
    token : str
        Bearer token for authentication.
    data : dict or None
        JSON body to send.  If provided and method is None, defaults to POST.
    method : str or None
        HTTP method override.

    Returns
    -------
    dict
        Parsed JSON response body.

    Raises
    ------
    ConnectionError
        When the master is unreachable.
    RuntimeError
        On HTTP error responses.
    """
    headers = {"Authorization": f"Bearer {token}"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if method is None:
        method = "POST" if body is not None else "GET"

    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port
    path = parsed.path
    if parsed.query:
        path = f"{path}?{parsed.query}"

    if _tls_config["enabled"]:
        # Build CERT_NONE context — we verify fingerprint in-band below
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(host, port, timeout=15, context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=15)

    try:
        conn.request(method, path, body=body, headers=headers)

        # Verify fingerprint against the LIVE connection's peer cert (in-band — no TOCTOU)
        if _tls_config["enabled"]:
            _verify_peer_fingerprint(conn, host, port)

        resp = conn.getresponse()
        if resp.status >= 400:
            error_body = resp.read().decode("utf-8", errors="replace")
            log.error("C2 HTTP %d from %s: %s", resp.status, url, error_body)
            raise RuntimeError(f"C2 request failed (HTTP {resp.status}): {error_body}")
        raw = resp.read()
        if raw:
            return json.loads(raw.decode("utf-8"))
        return {}
    except (http.client.HTTPException, OSError) as exc:
        log.error("Cannot reach C2 at %s: %s", url, exc)
        raise ConnectionError(f"Cannot reach master at {url}: {exc}") from exc
    finally:
        conn.close()


def _get_ram_mb() -> int:
    """Best-effort detection of total system RAM in megabytes."""
    # Linux: /proc/meminfo
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    # Format: "MemTotal:       16384000 kB"
                    parts = line.split()
                    return int(parts[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass

    # Windows: wmic (or ctypes if available)
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(stat)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys // (1024 * 1024)
    except (AttributeError, OSError):
        pass

    # macOS: sysctl
    try:
        import subprocess
        out = subprocess.check_output(
            ["sysctl", "-n", "hw.memsize"], stderr=subprocess.DEVNULL
        )
        return int(out.strip()) // (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register(master_host: str, master_port: int, token: str, name: str) -> dict:
    """Register this worker with the master's C2 server.

    Parameters
    ----------
    master_host : str
        Hostname or IP of the master node.
    master_port : int
        C2 API port on the master.
    token : str
        Bearer token for authentication.
    name : str
        Human-readable name for this worker.

    Returns
    -------
    dict
        Server response (e.g. ``{"status": "registered"}``).
    """
    system, machine = detect_platform()
    url = f"{_base_url(master_host, master_port)}/api/register"
    payload = {
        "name": name,
        "platform": f"{system}-{machine}",
        "cpus": os.cpu_count() or 1,
        "ram": _get_ram_mb(),
    }
    return _make_request(url, token, data=payload)


def report_status(
    master_host: str,
    master_port: int,
    token: str,
    name: str,
    stats: dict,
) -> dict:
    """Report worker status and hashrate to the master.

    Parameters
    ----------
    master_host : str
        Hostname or IP of the master node.
    master_port : int
        C2 API port on the master.
    token : str
        Bearer token for authentication.
    name : str
        Worker name.
    stats : dict
        Statistics dict with keys such as ``hashrate``, ``uptime``,
        ``cpu_usage``.

    Returns
    -------
    dict
        Server acknowledgement.
    """
    url = f"{_base_url(master_host, master_port)}/api/status"
    payload = {"name": name}
    payload.update(stats)
    return _make_request(url, token, data=payload)


def get_cluster_status(master_host: str, master_port: int, token: str) -> dict:
    """Retrieve the full cluster status from the master.

    Returns
    -------
    dict
        Cluster status including master info, worker list, and aggregate
        hashrate.
    """
    url = f"{_base_url(master_host, master_port)}/api/cluster/status"
    return _make_request(url, token, method="GET")


def run_heartbeat_loop(
    master_host: str,
    master_port: int,
    token: str,
    name: str,
    interval: int = 60,
) -> None:
    """Run a blocking heartbeat loop (intended for a daemon thread).

    Every *interval* seconds, attempt to read local xmrig stats and
    report them to the master.  Errors are logged and swallowed so the
    loop never crashes.

    Parameters
    ----------
    master_host : str
        Hostname or IP of the master node.
    master_port : int
        C2 API port on the master.
    token : str
        Bearer token for authentication.
    name : str
        Worker name.
    interval : int
        Seconds between heartbeats (default 60).
    """
    while True:
        try:
            stats = _read_local_xmrig_stats()
            report_status(master_host, master_port, token, name, stats)
            log.debug("Heartbeat sent: %s", stats)
        except Exception as exc:
            log.warning("Heartbeat failed: %s", exc)

        time.sleep(interval)



# ---------------------------------------------------------------------------
# Local xmrig stats reader
# ---------------------------------------------------------------------------

def _read_local_xmrig_stats() -> dict:
    """Attempt to read hashrate and status from local xmrig API.

    xmrig exposes a summary endpoint at http://127.0.0.1:<port>/1/summary
    by default (when http API is enabled).  If unavailable, return zeroes.
    """
    stats = {
        "hashrate": 0.0,
        "uptime": 0,
        "cpu_usage": 0.0,
    }

    try:
        headers = {}
        if _xmrig_http_token:
            headers["Authorization"] = f"Bearer {_xmrig_http_token}"
        req = Request("http://127.0.0.1:8080/1/summary", headers=headers, method="GET")
        with urlopen(req, timeout=3) as resp:  # nosec B310 — hardcoded loopback only
            data = json.loads(resp.read().decode("utf-8"))
            # xmrig summary has hashrate.total[] with 10s, 60s, 15m averages.
            hr_total = data.get("hashrate", {}).get("total", [0])
            stats["hashrate"] = hr_total[0] if hr_total else 0.0
            stats["uptime"] = data.get("uptime", 0)
            # cpu_usage is not directly in xmrig API; approximate from resources.
            resources = data.get("resources", {})
            stats["cpu_usage"] = resources.get("load_average", [0])[0] if resources.get("load_average") else 0.0
    except Exception:
        # xmrig API not available -- return zeroes silently.
        pass

    return stats
