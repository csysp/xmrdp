"""High-level cluster orchestration commands.

Provides the CLI-facing functions for starting, stopping, and querying
the status of master and worker nodes.  Delegates to node_manager for
process control and c2_server/c2_client for coordination.
"""

import logging
import sys
import time

from xmrdp.config import load_config
from xmrdp.constants import PORTS

log = logging.getLogger("xmrdp.cluster")


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_start(args) -> None:
    """Handle ``xmrdp start master|worker``.

    Loads the cluster config and delegates to the appropriate deployment
    function based on the selected role.
    """
    config = _load_config_or_exit(args)

    if args.role == "master":
        deploy_master(config)
    elif args.role == "worker":
        deploy_worker(config)


def cmd_stop(args) -> None:
    """Handle ``xmrdp stop master|worker``.

    Stops services in the correct order to ensure clean shutdown.
    """
    from xmrdp.node_manager import stop_service

    if args.role == "master":
        # Reverse startup order: xmrig -> p2pool -> monerod, plus C2.
        log.info("Stopping master services ...")

        for service in ("xmrig", "p2pool", "monerod"):
            try:
                stop_service(service)
                log.info("Stopped %s", service)
            except Exception as exc:
                log.warning("Failed to stop %s: %s", service, exc)

        # Stop the C2 server if a reference is stored.
        try:
            from xmrdp.c2_server import stop_c2_server
            stop_c2_server(None)  # Module-level server; handler is stateless.
        except Exception as exc:
            log.debug("C2 server stop note: %s", exc)

        print("Master services stopped.")

    elif args.role == "worker":
        try:
            stop_service("xmrig")
            log.info("Stopped xmrig")
            print("Worker stopped.")
        except Exception as exc:
            log.error("Failed to stop worker: %s", exc)
            print(f"Error stopping worker: {exc}", file=sys.stderr)
            sys.exit(1)


def cmd_status(args) -> None:
    """Handle ``xmrdp status``.

    Attempts to contact the C2 server for a cluster-wide view.  Falls
    back to local process checks when the master is not reachable.
    """
    config = _load_config_or_exit(args)
    status = cluster_status(config)

    _print_status_table(status)


# ---------------------------------------------------------------------------
# Core orchestration functions
# ---------------------------------------------------------------------------

def cluster_status(config: dict) -> dict:
    """Build a cluster status dict, querying C2 if possible.

    Returns
    -------
    dict
        Status dict with keys ``master``, ``workers``, ``aggregate_hashrate``.
        Suitable for JSON serialization or table printing.
    """
    from xmrdp.node_manager import is_running

    master_cfg = config.get("master", {})
    token = master_cfg.get("api_token", "")
    host = master_cfg.get("host", "127.0.0.1")
    port = master_cfg.get("api_port", PORTS["c2_api"])

    # Try remote cluster status from C2 first.
    if token:
        try:
            from xmrdp.c2_client import get_cluster_status, configure_tls
            tls_cfg = config.get("security", {})
            configure_tls(
                enabled=tls_cfg.get("tls_enabled", False),
                fingerprint=tls_cfg.get("c2_tls_fingerprint", ""),
            )
            return get_cluster_status(host, port, token)
        except Exception as exc:
            log.debug("C2 unreachable, falling back to local checks: %s", exc)

    # Fallback: local service status.
    services = ["monerod", "p2pool", "xmrig"]
    local_status = {
        "cluster": config.get("cluster", {}).get("name", "xmrdp-cluster"),
        "master": {
            "status": "unknown",
            "services": {},
        },
        "workers": [],
        "total_workers": 0,
        "online_workers": 0,
        "aggregate_hashrate": 0.0,
    }

    any_running = False
    for svc in services:
        running = False
        try:
            running = is_running(svc)
        except Exception:
            pass
        local_status["master"]["services"][svc] = "running" if running else "stopped"
        if running:
            any_running = True

    local_status["master"]["status"] = "running" if any_running else "stopped"

    return local_status


def deploy_master(config: dict) -> None:
    """Start the master node with all required services.

    Pre-checks are performed before launching each service.

    Startup order: monerod -> p2pool -> xmrig -> C2 server.
    """
    from xmrdp.node_manager import start_master, is_running
    from xmrdp.c2_server import start_c2_server

    # Validate wallet is configured.
    wallet = config.get("cluster", {}).get("wallet", "")
    if not wallet:
        print("Error: No wallet address configured. Run 'xmrdp setup' first.",
              file=sys.stderr)
        sys.exit(1)

    # Validate API token is set.
    token = config.get("master", {}).get("api_token", "")
    if not token:
        print("Warning: No API token configured. Workers cannot connect.",
              file=sys.stderr)

    log.info("Deploying master node ...")

    # Start core mining stack via node_manager.
    try:
        start_master(config)
    except Exception as exc:
        print(f"Error starting master services: {exc}", file=sys.stderr)
        sys.exit(1)

    # Start C2 server for worker coordination.
    try:
        server = start_c2_server(config)
        port = config.get("master", {}).get("api_port", PORTS["c2_api"])
        print(f"Master node started. C2 API on port {port}.")
    except Exception as exc:
        log.error("Failed to start C2 server: %s", exc)
        print(f"Warning: C2 server failed to start: {exc}", file=sys.stderr)
        print("Master mining services are running, but workers cannot connect.")


def deploy_worker(config: dict) -> None:
    """Start a worker node, registering with the master.

    Startup order: register -> start xmrig -> heartbeat loop.
    """
    import threading
    from xmrdp.node_manager import start_worker
    from xmrdp.c2_client import register, run_heartbeat_loop, configure_tls, configure_xmrig_token

    # Configure TLS before any C2 calls.
    tls_cfg = config.get("security", {})
    tls_enabled = tls_cfg.get("tls_enabled", False)
    if not tls_enabled:
        print(
            "WARNING: TLS is disabled. All C2 traffic (including the API token) "
            "will be sent in plaintext.\n"
            "         Re-run 'xmrdp setup' on the master to generate a certificate, "
            "then copy c2_tls_fingerprint into this worker's cluster.toml and set "
            "tls_enabled = true.",
            file=sys.stderr,
        )
    configure_tls(
        enabled=tls_enabled,
        fingerprint=tls_cfg.get("c2_tls_fingerprint", ""),
    )
    xmrig_http_token = config.get("master", {}).get("xmrig", {}).get("http_token", "")
    configure_xmrig_token(xmrig_http_token)
    from xmrdp.constants import HEARTBEAT_INTERVAL

    master_cfg = config.get("master", {})
    host = master_cfg.get("host", "127.0.0.1")
    port = master_cfg.get("api_port", PORTS["c2_api"])
    token = master_cfg.get("api_token", "")

    # Determine worker name from config or hostname.
    workers_list = config.get("workers", [])
    worker_name = None
    for w in workers_list:
        if isinstance(w, dict) and w.get("self"):
            worker_name = w.get("name")
            break

    if not worker_name:
        import socket
        worker_name = socket.gethostname()

    if not token:
        print("Error: No API token configured. Cannot contact master.",
              file=sys.stderr)
        sys.exit(1)

    # Register with master.
    try:
        register(host, port, token, worker_name)
        log.info("Registered with master as '%s'", worker_name)
    except Exception as exc:
        print(f"Warning: Could not register with master: {exc}",
              file=sys.stderr)
        print("Starting xmrig with local config only.")

    # Start xmrig via node_manager using local config.
    try:
        start_worker(config)
        print(f"Worker '{worker_name}' started.")
    except Exception as exc:
        print(f"Error starting worker: {exc}", file=sys.stderr)
        sys.exit(1)

    # Give xmrig a moment to start and check it didn't immediately crash.
    time.sleep(1.5)
    from xmrdp.node_manager import get_process
    _startup_proc = get_process("xmrig")
    if _startup_proc is None:
        print(
            f"Error: xmrig exited immediately after launch. "
            f"Check logs for details. "
            f"(Run with -v for verbose output.)",
            file=sys.stderr,
        )
        sys.exit(1)

    # Start heartbeat loop in a daemon thread.
    heartbeat_interval = HEARTBEAT_INTERVAL
    hb_thread = threading.Thread(
        target=run_heartbeat_loop,
        args=(host, port, token, worker_name, heartbeat_interval),
        daemon=True,
    )
    hb_thread.start()
    log.info("Heartbeat loop started (interval=%ds)", heartbeat_interval)

    # Keep the process alive while xmrig is running so the daemon heartbeat
    # thread is not killed when the main thread returns.
    from xmrdp.node_manager import get_process
    xmrig_proc = get_process("xmrig")
    if xmrig_proc is not None:
        try:
            xmrig_proc.wait()
        except KeyboardInterrupt:
            from xmrdp.node_manager import stop_service
            stop_service("xmrig")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_config_or_exit(args) -> dict:
    """Load config from args.config path, exiting on failure."""
    config_path = getattr(args, "config", None)
    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error loading config: {exc}", file=sys.stderr)
        sys.exit(1)


def _print_status_table(status: dict) -> None:
    """Pretty-print cluster status as a formatted table."""
    cluster_name = status.get("cluster", "xmrdp-cluster")
    print(f"\n  Cluster: {cluster_name}")
    print(f"  Aggregate hashrate: {status.get('aggregate_hashrate', 0):.1f} H/s")
    print()

    # Header
    header = f"  {'Node':<20} {'Role':<10} {'Status':<10} {'Hashrate':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    # Master row
    master = status.get("master", {})
    master_status = master.get("status", "unknown")
    print(f"  {'master':<20} {'master':<10} {master_status:<10} {'--':>12}")

    # Master services detail
    services = master.get("services", {})
    for svc_name, svc_status in services.items():
        indicator = "+" if svc_status == "running" else "-"
        print(f"    {indicator} {svc_name}: {svc_status}")

    # Worker rows
    workers = status.get("workers", [])
    if workers:
        for w in workers:
            name = w.get("name", "unknown")
            w_status = w.get("status", "unknown")
            hashrate = w.get("hashrate", 0.0)
            hr_str = f"{hashrate:.1f} H/s" if hashrate else "--"
            print(f"  {name:<20} {'worker':<10} {w_status:<10} {hr_str:>12}")
    elif not services:
        print("  (no workers connected)")

    total = status.get("total_workers", 0)
    online = status.get("online_workers", 0)
    print(f"\n  Workers: {online}/{total} online")

    pool = status.get("pool", {})
    if pool:
        print()
        print(f"  P2Pool hashrate: {pool.get('pool_hashrate', 0):.0f} H/s")
        print(f"  P2Pool miners:   {pool.get('pool_miners', 0)}")
        print(f"  Blocks found:    {pool.get('pool_blocks_found', 0)}")
    print()
