"""HTTP-based C2 server for master-worker coordination.

Runs on the master node, providing endpoints for worker registration,
configuration distribution, heartbeat collection, and cluster status.
Uses only stdlib http.server -- no external dependencies.
"""

import json
import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from xmrdp.config_generator import generate_xmrig_config
from xmrdp.platforms import get_binary_dir

log = logging.getLogger("xmrdp.c2")

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_workers: dict = {}
_workers_lock = threading.Lock()
_config: dict = {}
_server_start_time: float = 0.0


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class C2Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the XMRDP C2 API."""

    # Suppress default stderr request logging.
    def log_message(self, format, *args):  # noqa: A002
        log.debug("C2 %s %s", self.client_address[0], format % args)

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self) -> bool:
        """Validate Bearer token. Sends 401 and returns False on failure."""
        expected = _config.get("master", {}).get("api_token", "")
        if not expected:
            # No token configured -- reject everything (misconfiguration).
            self._send_json({"error": "No API token configured on master"}, 500)
            return False

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_json({"error": "Missing or malformed Authorization header"}, 401)
            return False

        token = auth_header[len("Bearer "):]
        if token != expected:
            self._send_json({"error": "Invalid token"}, 401)
            return False

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

    def _send_binary(self, data: bytes, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        """Read and parse the JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
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

        if path == "/api/config/worker":
            self._handle_get_worker_config(parsed)
        elif path == "/api/cluster/status":
            self._handle_get_cluster_status()
        elif path.startswith("/api/binaries/"):
            name = path[len("/api/binaries/"):]
            self._handle_get_binary(name)
        else:
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
        elif path == "/api/update/check":
            self._handle_update_check()
        else:
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

        with _workers_lock:
            _workers[name] = {
                "name": name,
                "platform": body.get("platform", "unknown"),
                "cpus": body.get("cpus", 0),
                "ram": body.get("ram", 0),
                "registered_at": time.time(),
                "last_seen": time.time(),
                "hashrate": 0.0,
                "uptime": 0,
                "cpu_usage": 0.0,
            }

        log.info("Worker registered: %s (cpus=%s, ram=%s MB)",
                 name, body.get("cpus", "?"), body.get("ram", "?"))
        self._send_json({"status": "registered"})

    def _handle_get_worker_config(self, parsed) -> None:
        """GET /api/config/worker -- Return xmrig config for a worker."""
        params = parse_qs(parsed.query)
        worker_name = params.get("name", ["worker"])[0]

        xmrig_cfg = generate_xmrig_config(_config, role="worker")

        # Personalize the pass field with the worker name.
        if xmrig_cfg.get("pools"):
            xmrig_cfg["pools"][0]["pass"] = worker_name

        self._send_json(xmrig_cfg)

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

        with _workers_lock:
            if name not in _workers:
                # Auto-register on heartbeat if not yet known.
                _workers[name] = {
                    "name": name,
                    "platform": "unknown",
                    "cpus": 0,
                    "ram": 0,
                    "registered_at": time.time(),
                }

            _workers[name]["last_seen"] = time.time()
            _workers[name]["hashrate"] = body.get("hashrate", 0.0)
            _workers[name]["uptime"] = body.get("uptime", 0)
            _workers[name]["cpu_usage"] = body.get("cpu_usage", 0.0)

        self._send_json({"status": "ok"})

    def _handle_get_cluster_status(self) -> None:
        """GET /api/cluster/status -- Full cluster view."""
        now = time.time()
        stale_threshold = 120  # seconds

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

        cluster_name = _config.get("cluster", {}).get("name", "xmrdp-cluster")

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
        })

    def _handle_update_check(self) -> None:
        """POST /api/update/check -- Report current and available versions."""
        binaries_cfg = _config.get("binaries", {})
        self._send_json({
            "current_versions": {
                "monero": binaries_cfg.get("monero_version", "latest"),
                "p2pool": binaries_cfg.get("p2pool_version", "latest"),
                "xmrig": binaries_cfg.get("xmrig_version", "latest"),
            },
            "updates_available": False,
            "message": "Version check completed. Use 'xmrdp update' for full check.",
        })

    def _handle_get_binary(self, name: str) -> None:
        """GET /api/binaries/<name> -- Serve a cached binary file."""
        if not name or "/" in name or "\\" in name or ".." in name:
            self._send_json({"error": "Invalid binary name"}, 400)
            return

        bin_dir = get_binary_dir()

        # Search for the binary -- try exact name first, then common extensions.
        candidates = [
            bin_dir / name,
            bin_dir / f"{name}.exe",
        ]

        target = None
        for candidate in candidates:
            if candidate.is_file():
                target = candidate
                break

        if target is None:
            self._send_json({"error": f"Binary not found: {name}"}, 404)
            return

        try:
            data = target.read_bytes()
            self._send_binary(data, target.name)
        except OSError as exc:
            log.error("Failed to read binary %s: %s", target, exc)
            self._send_json({"error": "Failed to read binary file"}, 500)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

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
    global _config, _workers, _server_start_time

    _config = config
    _workers = {}
    _server_start_time = time.time()

    host = config.get("master", {}).get("host", "0.0.0.0")
    port = config.get("master", {}).get("api_port", 7099)

    server = HTTPServer((host, port), C2Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    log.info("C2 server started on %s:%d", host, port)
    return server


def stop_c2_server(server: HTTPServer) -> None:
    """Shut down the C2 server gracefully."""
    if server is not None:
        server.shutdown()
        log.info("C2 server stopped")
