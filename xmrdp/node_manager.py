"""Manage service processes via subprocess with PID-file based lifecycle."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

from xmrdp.constants import (
    HEALTH_CHECK_INTERVAL,
    HEALTH_CHECK_RETRIES,
    HEALTH_CHECK_TIMEOUT,
    PORTS,
)
from xmrdp.platforms import get_data_dir, get_log_dir, get_pid_dir


_IS_WINDOWS = sys.platform == "win32"

# Registry of live Popen objects by service name.
_procs: dict = {}
_procs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _wait_for_port(host, port, timeout=None, interval=None):
    """Block until a TCP connection to *host*:*port* succeeds.

    Returns True on success, False on timeout.
    """
    if timeout is None:
        timeout = HEALTH_CHECK_TIMEOUT * HEALTH_CHECK_RETRIES
    if interval is None:
        interval = HEALTH_CHECK_INTERVAL

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            time.sleep(interval)
    return False


def _wait_for_rpc(host, port, timeout=None, interval=None):
    """Block until monerod's JSON-RPC responds on *host*:*port*/get_info.

    Returns True on success, False on timeout.
    """
    if timeout is None:
        timeout = HEALTH_CHECK_TIMEOUT * HEALTH_CHECK_RETRIES
    if interval is None:
        interval = HEALTH_CHECK_INTERVAL

    url = f"http://{host}:{port}/get_info"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urlopen(url, timeout=HEALTH_CHECK_TIMEOUT)  # nosec B310 — URL is 127.0.0.1 health check; scheme is always http on loopback
            if resp.status == 200:
                return True
        except (URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def _read_pid(name):
    """Read PID from the pid file for *name*. Returns int or None."""
    pid_file = get_pid_dir() / f"{name}.pid"
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def _write_pid(name, pid):
    """Write *pid* to the pid file for *name* with mode 0o600 (NF-04)."""
    pid_file = get_pid_dir() / f"{name}.pid"
    if not _IS_WINDOWS:
        fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(pid))
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    else:
        pid_file.write_text(str(pid), encoding="utf-8")


def _remove_pid(name):
    """Remove the pid file for *name*."""
    pid_file = get_pid_dir() / f"{name}.pid"
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass


def _process_exists(pid):
    """Return True if a process with *pid* exists."""
    if _IS_WINDOWS:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in result.stdout
        except (subprocess.SubprocessError, OSError):
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack permission to signal it.
            return True


# ---------------------------------------------------------------------------
# Service lifecycle
# ---------------------------------------------------------------------------

def start_service(name, binary_path, args, env=None):
    """Start a background process and record its PID.

    Parameters
    ----------
    name : str
        Logical service name (e.g. "monerod", "p2pool", "xmrig").
    binary_path : str or Path
        Path to the executable.
    args : list[str]
        Command-line arguments (not including the binary itself).
    env : dict, optional
        Extra environment variables merged with the current env.

    Returns
    -------
    int
        The PID of the launched process.
    """
    log_dir = get_log_dir()
    log_file = log_dir / f"{name}.log"

    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    cmd = [str(binary_path)] + list(args)

    log_handle = open(log_file, "a", encoding="utf-8", errors="replace")

    kwargs = {
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": full_env,
        "start_new_session": not _IS_WINDOWS,
    }

    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(cmd, **kwargs)
    log_handle.close()
    with _procs_lock:
        _procs[name] = proc
    _write_pid(name, proc.pid)
    return proc.pid


def stop_service(name):
    """Stop a service by its PID file.

    Returns True if the service was stopped (or was already gone),
    False if the PID file did not exist.
    """
    pid = _read_pid(name)
    if pid is None:
        return False

    if not _process_exists(pid):
        _remove_pid(name)
        return True

    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        except (subprocess.SubprocessError, OSError):
            pass
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # Wait up to 10 seconds for graceful shutdown.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not _process_exists(pid):
                break
            time.sleep(0.5)
        else:
            # Force kill if still alive.
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    _remove_pid(name)
    return True


def is_running(name):
    """Return True if the service *name* is currently running."""
    pid = _read_pid(name)
    if pid is None:
        return False
    alive = _process_exists(pid)
    if not alive:
        # Stale pid file — clean it up.
        _remove_pid(name)
    return alive


# ---------------------------------------------------------------------------
# Master orchestration
# ---------------------------------------------------------------------------

def start_master(config):
    """Start all master-node services in the correct order.

    Sequence: monerod -> p2pool -> xmrig -> c2 server.
    """
    from xmrdp.binary_manager import get_binary_path
    from xmrdp.config_generator import (
        generate_monerod_args,
        generate_p2pool_args,
        write_xmrig_config,
    )

    # --- monerod ---
    print("[1/4] Starting monerod ...")
    monerod_bin = get_binary_path("monero")
    if monerod_bin is None:
        raise RuntimeError(
            "monerod binary not found. Run 'xmrdp setup' to download binaries."
        )
    monerod_args = generate_monerod_args(config)
    pid = start_service("monerod", monerod_bin, monerod_args)
    print(f"       monerod started (PID {pid}). Waiting for RPC ...")

    rpc_timeout = HEALTH_CHECK_RETRIES * HEALTH_CHECK_INTERVAL
    if not _wait_for_rpc("127.0.0.1", PORTS["monerod_rpc"],
                         timeout=rpc_timeout, interval=HEALTH_CHECK_INTERVAL):
        print("       WARNING: monerod RPC did not become ready within timeout.")
        print("       Continuing anyway — it may still be syncing.")
    else:
        print("       monerod RPC is ready.")

    # --- p2pool ---
    print("[2/4] Starting p2pool ...")
    p2pool_bin = get_binary_path("p2pool")
    if p2pool_bin is None:
        raise RuntimeError(
            "p2pool binary not found. Run 'xmrdp setup' to download binaries."
        )
    p2pool_args = generate_p2pool_args(config)
    pid = start_service("p2pool", p2pool_bin, p2pool_args)
    print(f"       p2pool started (PID {pid}). Waiting for stratum port ...")

    if not _wait_for_port("127.0.0.1", PORTS["p2pool_stratum"],
                          timeout=rpc_timeout, interval=HEALTH_CHECK_INTERVAL):
        print("       WARNING: p2pool stratum port did not become ready.")
    else:
        print("       p2pool stratum is ready.")

    # --- xmrig ---
    print("[3/4] Starting xmrig ...")
    config_path = write_xmrig_config(config, role="master")
    xmrig_bin = get_binary_path("xmrig")
    if xmrig_bin is None:
        raise RuntimeError(
            "xmrig binary not found. Run 'xmrdp setup' to download binaries."
        )
    xmrig_args = ["--config", str(config_path)]
    pid = start_service("xmrig", xmrig_bin, xmrig_args)
    print(f"       xmrig started (PID {pid}).")

    print()
    print("Master node is running.")
    _print_service_status(["monerod", "p2pool", "xmrig"])


def start_worker(config):
    """Start the xmrig process for a worker node.

    This handles only the xmrig subprocess.  C2 registration and heartbeat
    setup are managed by ``cluster.deploy_worker``.

    Parameters
    ----------
    config : dict
        The loaded cluster config used to generate the local xmrig config.
    """
    from xmrdp.binary_manager import get_binary_path
    from xmrdp.config_generator import write_xmrig_config

    print("[*] Starting xmrig ...")

    config_path = write_xmrig_config(config, role="worker")

    xmrig_bin = get_binary_path("xmrig")
    if xmrig_bin is None:
        raise RuntimeError(
            "xmrig binary not found. Run 'xmrdp setup' to download binaries."
        )
    xmrig_args = ["--config", str(config_path)]
    pid = start_service("xmrig", xmrig_bin, xmrig_args)
    print(f"    xmrig started (PID {pid}).")

    _print_service_status(["xmrig"])


def stop_master():
    """Stop all master-node services in reverse order."""
    services = ["xmrig", "p2pool", "monerod"]
    print("Stopping master services ...")
    for name in services:
        if is_running(name):
            stopped = stop_service(name)
            status = "stopped" if stopped else "failed to stop"
            print(f"  {name}: {status}")
        else:
            print(f"  {name}: not running")
    # C2 server runs in-process; stopping master implies it exits.
    _remove_pid("c2_server")
    print("Master node stopped.")


def stop_worker():
    """Stop all worker-node services."""
    print("Stopping worker services ...")
    if is_running("xmrig"):
        stopped = stop_service("xmrig")
        status = "stopped" if stopped else "failed to stop"
        print(f"  xmrig: {status}")
    else:
        print("  xmrig: not running")
    print("Worker node stopped.")


# ---------------------------------------------------------------------------
# Log viewing
# ---------------------------------------------------------------------------

def cmd_logs(args):
    """Tail log files for the requested service(s).

    Parameters
    ----------
    args : argparse.Namespace
        Expected attributes: service (str or None), lines (int).
    """
    log_dir = get_log_dir()
    service = getattr(args, "service", None)
    num_lines = getattr(args, "lines", 50)

    if service:
        services = [service]
    else:
        services = ["monerod", "p2pool", "xmrig", "c2"]

    for svc in services:
        log_path = log_dir / f"{svc}.log"
        if not log_path.exists():
            print(f"--- {svc}: no log file ---")
            continue

        print(f"--- {svc} (last {num_lines} lines) ---")
        lines = _tail(log_path, num_lines)
        for line in lines:
            print(line, end="")
        print()


def _tail(path, num_lines):
    """Return the last *num_lines* lines of a file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        return all_lines[-num_lines:]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def get_process(name):
    """Return the Popen object for *name* if it is still alive, else None."""
    with _procs_lock:
        proc = _procs.get(name)
    if proc is None:
        return None
    if proc.poll() is not None:
        # Process has exited; clean up the registry entry.
        with _procs_lock:
            _procs.pop(name, None)
        return None
    return proc


def _print_service_status(services):
    """Print a status table for the given service names."""
    print()
    print("  Service      Status    PID")
    print("  ----------   -------   -----")
    for name in services:
        pid = _read_pid(name)
        running = is_running(name)
        status = "running" if running else "stopped"
        pid_str = str(pid) if pid and running else "-"
        print(f"  {name:<12} {status:<9} {pid_str}")
    print()
