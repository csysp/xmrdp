"""C2 client for worker nodes.

Handles registration, config fetching, heartbeat reporting, and binary
downloads from the master's C2 server.  Uses only stdlib urllib -- no
external dependencies.
"""

import json
import logging
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from xmrdp.platforms import detect_platform

log = logging.getLogger("xmrdp.c2client")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _base_url(host: str, port: int) -> str:
    """Build the base URL for the C2 API."""
    return f"http://{host}:{port}"


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
    headers = {
        "Authorization": f"Bearer {token}",
    }

    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if method is None:
        method = "POST" if body is not None else "GET"

    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
            if raw:
                return json.loads(raw.decode("utf-8"))
            return {}
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        log.error("C2 HTTP %d from %s: %s", exc.code, url, error_body)
        raise RuntimeError(
            f"C2 request failed (HTTP {exc.code}): {error_body}"
        ) from exc
    except URLError as exc:
        log.error("Cannot reach C2 at %s: %s", url, exc.reason)
        raise ConnectionError(
            f"Cannot reach master at {url}: {exc.reason}"
        ) from exc


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


def fetch_config(master_host: str, master_port: int, token: str, name: str) -> dict:
    """Fetch xmrig configuration from the master for this worker.

    Parameters
    ----------
    master_host : str
        Hostname or IP of the master node.
    master_port : int
        C2 API port on the master.
    token : str
        Bearer token for authentication.
    name : str
        Worker name (used to personalize the config).

    Returns
    -------
    dict
        xmrig configuration dictionary.
    """
    encoded_name = quote(name, safe="")
    url = f"{_base_url(master_host, master_port)}/api/config/worker?name={encoded_name}"
    return _make_request(url, token, method="GET")


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


def download_binary_from_master(
    master_host: str,
    master_port: int,
    token: str,
    name: str,
    dest_path: str,
) -> Path:
    """Download a binary from the master's cache.

    Parameters
    ----------
    master_host : str
        Hostname or IP of the master node.
    master_port : int
        C2 API port on the master.
    token : str
        Bearer token for authentication.
    name : str
        Binary name (e.g. ``"xmrig"``).
    dest_path : str
        Local file path to write the downloaded binary.

    Returns
    -------
    Path
        The path to the downloaded file.
    """
    encoded = quote(name, safe="")
    url = f"{_base_url(master_host, master_port)}/api/binaries/{encoded}"
    headers = {
        "Authorization": f"Bearer {token}",
    }
    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=120) as resp:
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            log.info("Downloaded binary %s to %s", name, dest)
            return dest
    except HTTPError as exc:
        raise RuntimeError(
            f"Failed to download binary '{name}' (HTTP {exc.code})"
        ) from exc
    except URLError as exc:
        raise ConnectionError(
            f"Cannot reach master at {url}: {exc.reason}"
        ) from exc


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
        req = Request("http://127.0.0.1:8080/1/summary", method="GET")
        with urlopen(req, timeout=3) as resp:
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
