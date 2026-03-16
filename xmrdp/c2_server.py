"""HTTP-based telemetry bus for master-worker coordination.

Runs on the master node.  Provides three endpoints: worker registration
(POST /api/register), worker heartbeat (POST /api/status), and aggregate
cluster status (GET /api/cluster/status).  Configuration is pushed to
workers via SSH/SCP (xmrdp sync) -- not served here.  Uses only stdlib
http.server -- no external dependencies.
"""

import collections
import hmac
import json
import logging
import os
import re
import ssl
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

from xmrdp.constants import PORTS, HEARTBEAT_INTERVAL
from xmrdp.platforms import get_data_dir

log = logging.getLogger("xmrdp.c2")
_audit_log = logging.getLogger("xmrdp.audit")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_workers: dict = {}
_workers_lock = threading.Lock()
_config: dict = {}
_server_start_time: float = 0.0
_running_server = None

# Rate limiting for auth failures (Fix 1)
_auth_failures: dict = {}          # ip -> [timestamp, ...]
_auth_failures_lock = threading.Lock()
_RATE_LIMIT_WINDOW = 60            # seconds
_RATE_LIMIT_MAX_FAILURES = 10      # failures allowed per window

# Worker eviction age (Fix 5)
_WORKER_EVICTION_AGE = 24 * 3600  # 24 hours

# Worker name validation (NF-NEW-02): alphanumeric + hyphen/underscore, 1-64 chars
_WORKER_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')

# Rate limiting dict max size (NF-NEW-04): bound memory against IP spoofing
_RATE_LIMIT_MAX_IPS = 10_000


# ---------------------------------------------------------------------------
# Worker registry persistence
# ---------------------------------------------------------------------------

def _workers_file() -> Path:
    return get_data_dir() / "workers.json"


def _load_workers() -> dict:
    """Load persisted worker registry from disk. Returns {} on any error."""
    try:
        path = _workers_file()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.warning("Could not load persisted workers: %s", exc)
    return {}


def _save_workers() -> None:
    """Write current _workers dict to disk. Must be called under _workers_lock."""
    try:
        path = _workers_file()
        content = json.dumps(_workers, indent=2)
        if sys.platform != "win32":
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        else:
            path.write_text(content, encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to persist workers: %s", exc)


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class C2Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the XMRDP C2 API."""

    # Suppress default stderr request logging.
    def log_message(self, format, *args):  # noqa: A002
        log.debug("C2 %s %s", self.client_address[0], format % args)

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    def _audit(self, event: str, **kwargs) -> None:
        """Emit a structured audit log entry."""
        parts = [f"event={event}", f"ip={self.client_address[0]}"]
        parts.extend(f"{k}={v!r}" for k, v in kwargs.items())
        _audit_log.warning("AUDIT %s", " ".join(parts))

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self) -> bool:
        """Validate Bearer token. Sends 401/429 and returns False on failure."""
        ip = self.client_address[0]
        now = time.time()

        # Per-IP rate limiting — check before reading any token (Fix 1)
        with _auth_failures_lock:
            # Evict silent IPs when dict would grow too large (NF-NEW-04)
            if ip not in _auth_failures and len(_auth_failures) >= _RATE_LIMIT_MAX_IPS:
                stale = [k for k, ts in _auth_failures.items()
                         if all(now - t >= _RATE_LIMIT_WINDOW for t in ts)]
                for k in stale:
                    del _auth_failures[k]

            timestamps = _auth_failures.get(ip, [])
            timestamps = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
            _auth_failures[ip] = timestamps
            if len(timestamps) >= _RATE_LIMIT_MAX_FAILURES:
                self._audit("rate_limit", failures=len(timestamps))
                self._send_json({"error": "Too many requests"}, 429)
                return False

        expected = str(_config.get("master", {}).get("api_token", ""))
        if not expected:
            # No token configured -- reject everything (misconfiguration).
            self._send_json({"error": "No API token configured on master"}, 500)
            return False

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            with _auth_failures_lock:
                _auth_failures.setdefault(ip, []).append(now)
            self._audit("auth_failure", reason="missing_header")
            self._send_json({"error": "Missing or malformed Authorization header"}, 401)
            return False

        token = auth_header[len("Bearer "):]
        if not hmac.compare_digest(token, expected):
            with _auth_failures_lock:
                _auth_failures.setdefault(ip, []).append(now)
            self._audit("auth_failure", reason="bad_token")
            self._send_json({"error": "Invalid token"}, 401)
            return False

        # Successful auth — clear failure record for this IP
        with _auth_failures_lock:
            _auth_failures.pop(ip, None)

        self._audit("auth_success")
        return True

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    _MAX_BODY = 65536  # 64 KB — reject oversized payloads

    def _read_body(self) -> dict:
        """Read and parse the JSON request body."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            raise ValueError("Invalid Content-Length header")
        if length < 0:
            raise ValueError(f"Invalid Content-Length: {length}")
        if length == 0:
            return {}
        if length > self._MAX_BODY:
            raise ValueError(
                f"Request body too large: {length} bytes (max {self._MAX_BODY})"
            )
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/cluster/status":
            self._handle_get_cluster_status()
        else:
            self._audit("unknown_route", path=self.path)
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):  # noqa: N802
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/register":
            self._handle_register()
        elif path == "/api/status":
            self._handle_status()
        else:
            self._audit("unknown_route", path=self.path)
            self._send_json({"error": "Not found"}, 404)

    # ------------------------------------------------------------------
    # Endpoint implementations
    # ------------------------------------------------------------------

    def _handle_register(self) -> None:
        """POST /api/register -- Worker registration."""
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON body"}, 400)
            return

        name = body.get("name")
        if not name:
            self._send_json({"error": "Missing required field: name"}, 400)
            return
        if not _WORKER_NAME_RE.match(str(name)):
            self._send_json({"error": "Invalid worker name"}, 400)
            return

        platform = body.get("platform", "unknown")
        cpus = body.get("cpus", 0)
        ram = body.get("ram", 0)
        registered_ip = self.client_address[0]

        with _workers_lock:
            _workers[name] = {
                "name": name,
                "platform": platform,
                "cpus": cpus,
                "ram": ram,
                "registered_at": time.time(),
                "last_seen": time.time(),
                "hashrate": 0.0,
                "uptime": 0,
                "cpu_usage": 0.0,
                "registered_ip": registered_ip,
            }
            _save_workers()

        log.info("Worker registered: %s (cpus=%s, ram=%s MB)", name, cpus, ram)
        self._audit("worker_registered", name=name, platform=platform, cpus=cpus)
        self._send_json({"status": "registered"})

    def _handle_status(self) -> None:
        """POST /api/status -- Worker heartbeat with stats."""
        try:
            body = self._read_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON body"}, 400)
            return

        name = body.get("name")
        if not name:
            self._send_json({"error": "Missing required field: name"}, 400)
            return
        if not _WORKER_NAME_RE.match(str(name)):
            self._send_json({"error": "Invalid worker name"}, 400)
            return

        request_ip = self.client_address[0]

        with _workers_lock:
            if name not in _workers:
                # Auto-register on heartbeat if not yet known.
                _workers[name] = {
                    "name": name,
                    "platform": "unknown",
                    "cpus": 0,
                    "ram": 0,
                    "registered_at": time.time(),
                    "registered_ip": request_ip,
                }
            else:
                registered_ip = _workers[name].get("registered_ip", "")
                if registered_ip and request_ip != registered_ip:
                    self._audit("worker_ip_mismatch",
                                name=name, expected=registered_ip, actual=request_ip)
                    self._send_json(
                        {"error": "IP mismatch — re-register to update binding"}, 403
                    )
                    return

            _workers[name]["last_seen"] = time.time()
            _workers[name]["hashrate"] = body.get("hashrate", 0.0)
            _workers[name]["uptime"] = body.get("uptime", 0)
            _workers[name]["cpu_usage"] = body.get("cpu_usage", 0.0)
            _save_workers()

        self._audit("heartbeat", name=name)
        self._send_json({"status": "ok"})

    def _handle_get_cluster_status(self) -> None:
        """GET /api/cluster/status -- Full cluster view."""
        now = time.time()
        stale_threshold = HEARTBEAT_INTERVAL * 3  # mark stale after 3 missed heartbeats

        worker_list = []
        total_hashrate = 0.0

        with _workers_lock:
            workers_snapshot = list(_workers.values())

        for w in workers_snapshot:
            last_seen = w.get("last_seen", 0)
            is_online = (now - last_seen) < stale_threshold
            hashrate = w.get("hashrate", 0.0)
            total_hashrate += hashrate

            worker_list.append({
                "name": w["name"],
                "platform": w.get("platform", "unknown"),
                "cpus": w.get("cpus", 0),
                "ram": w.get("ram", 0),
                "hashrate": hashrate,
                "uptime": w.get("uptime", 0),
                "cpu_usage": w.get("cpu_usage", 0.0),
                "status": "online" if is_online else "stale",
                "last_seen": last_seen,
            })

        # Lazy eviction of workers unseen for more than 24 hours (Fix 5)
        with _workers_lock:
            evicted = [w["name"] for w in workers_snapshot
                       if (now - w.get("last_seen", 0)) > _WORKER_EVICTION_AGE]
            for w_name in evicted:
                _workers.pop(w_name, None)
                log.info("Evicted stale worker: %s", w_name)
            if evicted:
                _save_workers()

        cluster_name = _config.get("cluster", {}).get("name", "xmrdp-cluster")

        pool_stats = _read_p2pool_stats()

        self._audit("cluster_status_poll")
        self._send_json({
            "cluster": cluster_name,
            "master": {
                "status": "running",
                "uptime": now - _server_start_time,
            },
            "workers": worker_list,
            "total_workers": len(worker_list),
            "online_workers": sum(1 for w in worker_list if w["status"] == "online"),
            "aggregate_hashrate": total_hashrate,
            "pool": pool_stats,
        })


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def _read_p2pool_stats() -> dict:
    """Read pool statistics from the P2Pool data-api directory.

    P2Pool writes JSON files to ``--data-api <dir>`` when it is running.
    We read ``stats`` (main chain) or ``stats_mod`` (mini chain), whichever
    is present.  Returns an empty dict on any error so the cluster status
    endpoint degrades gracefully when p2pool is not running.
    """
    try:
        data_dir = get_data_dir() / "p2pool"
        # p2pool mini writes stats_mod; main chain writes stats.
        for filename in ("stats_mod", "stats"):
            stats_file = data_dir / filename
            if stats_file.is_file():
                raw = stats_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                # Normalise the two common p2pool JSON layouts.
                pool_stats = (
                    data.get("pool_statistics")
                    or data.get("pool", {}).get("stats")
                    or {}
                )
                return {
                    "pool_hashrate": pool_stats.get("hashRate", 0),
                    "pool_miners": pool_stats.get("miners", 0),
                    "pool_blocks_found": pool_stats.get("totalBlocksFound", 0),
                    "pool_last_block_time": pool_stats.get("lastBlockFoundTime", 0),
                }
    except Exception:
        pass
    return {}


def start_c2_server(config: dict) -> HTTPServer:
    """Start the C2 HTTP server in a daemon thread.

    Parameters
    ----------
    config : dict
        The loaded cluster configuration.

    Returns
    -------
    HTTPServer
        The running server instance (call stop_c2_server to shut down).
    """
    global _config, _workers, _server_start_time, _running_server, _auth_failures

    _config = config
    _workers = _load_workers()
    _auth_failures = {}
    _server_start_time = time.time()

    master_cfg = config.get("master", {})
    # bind_host controls what interface the C2 server listens on.
    # Falls back to master.host so existing configs continue to work.
    host = master_cfg.get("bind_host") or master_cfg.get("host", "127.0.0.1")
    port = master_cfg.get("api_port", 7099)

    server = HTTPServer((host, port), C2Handler)

    # Wrap with TLS if cert/key are configured (Option C — stdlib ssl).
    tls_cfg = config.get("security", {})
    if tls_cfg.get("tls_enabled", False):
        cert = tls_cfg.get("c2_tls_cert", "")
        key = tls_cfg.get("c2_tls_key", "")
        if cert and key and Path(cert).is_file() and Path(key).is_file():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cert, key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            log.info("C2 server TLS enabled (cert: %s)", cert)
        else:
            log.warning(
                "tls_enabled=true but cert/key files not found — starting without TLS"
            )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    _running_server = server
    log.info("C2 server started on %s:%d", host, port)
    return server


def stop_c2_server(server=None) -> None:
    """Shut down the C2 server gracefully."""
    global _running_server
    target = server or _running_server
    if target is not None:
        target.shutdown()
        _running_server = None
        log.info("C2 server stopped")
