"""Integration tests for the C2 server and client (telemetry bus).

Starts a real HTTPServer on a free localhost port for each test module
and exercises every endpoint via http.client.

Coverage:
  - Authentication (missing, bad token, rate limiting, clear on success)
  - POST /api/register
  - POST /api/status  (heartbeat + IP binding enforcement)
  - GET  /api/cluster/status
  - Body size cap + negative Content-Length guard
  - C2 client helper functions (register, report_status, get_cluster_status)
"""

import http.client
import json
import socket
import sys
import time
import unittest

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

TOKEN = "test-token-deadbeef1234"

_BASE_CONFIG = {
    "cluster": {"name": "test-cluster", "wallet": "4" + "1" * 94},
    "master": {
        "host": "127.0.0.1",
        "api_port": 0,          # overwritten in setUpModule
        "api_token": TOKEN,
        "monerod": {"prune": True, "extra_args": []},
        "p2pool": {"mini": True, "extra_args": []},
        "xmrig": {"threads": 0, "http_token": ""},
    },
    "workers": [],
    "binaries": {
        "monero_version": "latest",
        "p2pool_version": "latest",
        "xmrig_version": "latest",
    },
    "security": {
        "verify_checksums": True,
        "tls_enabled": False,
        "c2_tls_cert": "",
        "c2_tls_key": "",
        "c2_tls_fingerprint": "",
    },
}

_server = None
_port: int = 0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def setUpModule():
    """Start one C2 server for all tests in this module."""
    global _server, _port
    from xmrdp.c2_server import start_c2_server
    _port = _free_port()
    cfg = dict(_BASE_CONFIG)
    cfg["master"] = dict(_BASE_CONFIG["master"])
    cfg["master"]["api_port"] = _port
    _server = start_c2_server(cfg)
    time.sleep(0.05)   # give the server thread time to bind


def tearDownModule():
    from xmrdp.c2_server import stop_c2_server
    if _server:
        stop_c2_server(_server)


# ---------------------------------------------------------------------------
# Base class — clears shared mutable state between every test
# ---------------------------------------------------------------------------

class _C2Base(unittest.TestCase):
    """Reset _workers and _auth_failures before each test."""

    def setUp(self):
        import xmrdp.c2_server as srv
        with srv._workers_lock:
            srv._workers.clear()
        with srv._auth_failures_lock:
            srv._auth_failures.clear()

    # ------------------------------------------------------------------
    # Request helper
    # ------------------------------------------------------------------

    def _req(self, method, path, token=TOKEN, body=None, raw_body=None,
             extra_headers=None):
        """Send a request to the test server; return (status, data, resp).

        ``data`` is the parsed JSON dict when the response body is valid
        JSON, otherwise the raw bytes.
        """
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        if body is not None:
            b = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(b))
        elif raw_body is not None:
            b = raw_body
            headers["Content-Length"] = str(len(b))
        else:
            b = None

        if extra_headers:
            headers.update(extra_headers)

        conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=5)
        try:
            conn.request(method, path, body=b, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                return resp.status, json.loads(raw), resp
            except Exception:
                return resp.status, raw, resp
        finally:
            conn.close()

    def _register(self, name, **kwargs):
        """Register a worker and assert success."""
        payload = {"name": name, "platform": "linux-x86_64", "cpus": 4, "ram": 8192}
        payload.update(kwargs)
        status, data, _ = self._req("POST", "/api/register", body=payload)
        self.assertEqual(status, 200)
        return data


# ===========================================================================
# Authentication
# ===========================================================================

class TestC2Auth(_C2Base):
    """Auth: missing/bad token, rate limiting, and success clearing."""

    def test_missing_auth_header_returns_401(self):
        status, data, _ = self._req("GET", "/api/cluster/status", token=None)
        self.assertEqual(status, 401)

    def test_bearer_prefix_required(self):
        """Sending token without 'Bearer ' prefix must return 401."""
        status, data, _ = self._req(
            "GET", "/api/cluster/status",
            token=None,
            extra_headers={"Authorization": TOKEN},  # no "Bearer " prefix
        )
        self.assertEqual(status, 401)

    def test_wrong_token_returns_401(self):
        status, data, _ = self._req("GET", "/api/cluster/status", token="wrongtoken")
        self.assertEqual(status, 401)

    def test_correct_token_returns_200(self):
        status, _, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(status, 200)

    def test_rate_limit_triggers_at_ten_failures(self):
        """After 10 failures within the window the next request must return 429."""
        import xmrdp.c2_server as srv
        now = time.time()
        # Pre-seed 10 failures — the rate limit check fires when len >= 10,
        # so any subsequent request must return 429.
        with srv._auth_failures_lock:
            srv._auth_failures["127.0.0.1"] = [now - 1] * 10

        status1, _, _ = self._req("GET", "/api/cluster/status", token="bad")
        self.assertEqual(status1, 429)

        # Follow-up must also be rate-limited
        status2, _, _ = self._req("GET", "/api/cluster/status", token="bad")
        self.assertEqual(status2, 429)

    def test_successful_auth_clears_failure_record(self):
        """A successful auth must remove the failure record for that IP."""
        import xmrdp.c2_server as srv
        now = time.time()
        with srv._auth_failures_lock:
            srv._auth_failures["127.0.0.1"] = [now - 1] * 5

        self._req("GET", "/api/cluster/status")  # valid

        with srv._auth_failures_lock:
            remaining = srv._auth_failures.get("127.0.0.1", [])
        self.assertEqual(remaining, [], "Failure record must be cleared after successful auth")

    def test_unknown_route_returns_404(self):
        status, _, _ = self._req("GET", "/api/not-a-real-endpoint")
        self.assertEqual(status, 404)

    def test_unknown_post_route_returns_404(self):
        status, _, _ = self._req("POST", "/api/not-a-real-endpoint", body={})
        self.assertEqual(status, 404)


# ===========================================================================
# Worker registration
# ===========================================================================

class TestC2Register(_C2Base):
    """POST /api/register"""

    def test_valid_registration_returns_registered(self):
        status, data, _ = self._req(
            "POST", "/api/register",
            body={"name": "worker-1", "platform": "linux-x86_64", "cpus": 4, "ram": 8192},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "registered")

    def test_registration_stores_worker_in_state(self):
        import xmrdp.c2_server as srv
        self._req("POST", "/api/register",
                  body={"name": "stored-worker", "cpus": 2, "ram": 4096})
        with srv._workers_lock:
            self.assertIn("stored-worker", srv._workers)
            self.assertEqual(srv._workers["stored-worker"]["cpus"], 2)

    def test_registration_records_source_ip(self):
        import xmrdp.c2_server as srv
        self._req("POST", "/api/register", body={"name": "ip-worker"})
        with srv._workers_lock:
            self.assertEqual(srv._workers["ip-worker"]["registered_ip"], "127.0.0.1")

    def test_missing_name_field_returns_400(self):
        status, data, _ = self._req("POST", "/api/register",
                                    body={"platform": "linux-x86_64"})
        self.assertEqual(status, 400)

    def test_empty_body_returns_400(self):
        status, _, _ = self._req("POST", "/api/register", body={})
        self.assertEqual(status, 400)

    def test_oversized_body_returns_400(self):
        """Body larger than 64 KB must be rejected."""
        big = b'{"name":"x","pad":"' + b"A" * 70_000 + b'"}'
        try:
            status, _, _ = self._req("POST", "/api/register", raw_body=big)
            self.assertEqual(status, 400)
        except (ConnectionResetError, ConnectionAbortedError):
            # Windows: server closes the connection while the client is still
            # sending the oversized body, raising WinError 10053/10054.
            pass

    def test_reregistration_updates_existing_worker(self):
        """Registering the same name twice should update fields."""
        import xmrdp.c2_server as srv
        self._req("POST", "/api/register", body={"name": "re-reg", "cpus": 2})
        self._req("POST", "/api/register", body={"name": "re-reg", "cpus": 16})
        with srv._workers_lock:
            self.assertEqual(srv._workers["re-reg"]["cpus"], 16)


# ===========================================================================
# Heartbeat / status
# ===========================================================================

class TestC2Heartbeat(_C2Base):
    """POST /api/status"""

    def test_valid_heartbeat_returns_ok(self):
        status, data, _ = self._req(
            "POST", "/api/status",
            body={"name": "beat-worker", "hashrate": 1234.5, "uptime": 3600, "cpu_usage": 0.5},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "ok")

    def test_missing_name_returns_400(self):
        status, _, _ = self._req("POST", "/api/status", body={"hashrate": 100.0})
        self.assertEqual(status, 400)

    def test_heartbeat_updates_stats_in_state(self):
        import xmrdp.c2_server as srv
        self._register("stats-worker")
        self._req("POST", "/api/status",
                  body={"name": "stats-worker", "hashrate": 9999.0, "uptime": 100})
        with srv._workers_lock:
            self.assertAlmostEqual(srv._workers["stats-worker"]["hashrate"], 9999.0,
                                   places=1)

    def test_heartbeat_auto_registers_unknown_worker(self):
        """Heartbeat from an unregistered worker should auto-register it."""
        import xmrdp.c2_server as srv
        status, data, _ = self._req(
            "POST", "/api/status",
            body={"name": "unknown-worker", "hashrate": 100.0},
        )
        self.assertEqual(status, 200)
        with srv._workers_lock:
            self.assertIn("unknown-worker", srv._workers)

    def test_ip_mismatch_returns_403(self):
        """Heartbeat from a different IP than registered must be rejected with 403."""
        import xmrdp.c2_server as srv
        self._register("ip-check-worker")

        # Simulate the worker having been registered from a different IP
        with srv._workers_lock:
            srv._workers["ip-check-worker"]["registered_ip"] = "10.0.0.99"

        # Our heartbeat comes from 127.0.0.1, which != 10.0.0.99
        status, data, _ = self._req(
            "POST", "/api/status",
            body={"name": "ip-check-worker", "hashrate": 500.0},
        )
        self.assertEqual(status, 403, "Heartbeat from wrong IP must return 403")

    def test_ip_match_after_registration_succeeds(self):
        """Heartbeat from the registered IP (127.0.0.1) must succeed."""
        self._register("same-ip-worker")
        status, data, _ = self._req(
            "POST", "/api/status",
            body={"name": "same-ip-worker", "hashrate": 200.0},
        )
        self.assertEqual(status, 200)

    def test_oversized_heartbeat_returns_400(self):
        big = b'{"name":"x","pad":"' + b"A" * 70_000 + b'"}'
        try:
            status, _, _ = self._req("POST", "/api/status", raw_body=big)
            self.assertEqual(status, 400)
        except (ConnectionResetError, ConnectionAbortedError):
            # Windows: server closes the connection instead of sending HTTP 400
            pass


# ===========================================================================
# Cluster status
# ===========================================================================

class TestC2ClusterStatus(_C2Base):
    """GET /api/cluster/status"""

    def test_response_shape(self):
        status, data, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(status, 200)
        for key in ("cluster", "workers", "aggregate_hashrate",
                    "total_workers", "online_workers", "master"):
            self.assertIn(key, data, f"Missing key in cluster status: {key!r}")

    def test_cluster_name_matches_config(self):
        status, data, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(data["cluster"], "test-cluster")

    def test_registered_worker_appears_in_status(self):
        self._register("visible-worker")
        self._req("POST", "/api/status",
                  body={"name": "visible-worker", "hashrate": 500.0})
        status, data, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(status, 200)
        names = [w["name"] for w in data["workers"]]
        self.assertIn("visible-worker", names)

    def test_aggregate_hashrate_sums_workers(self):
        self._register("hr-worker-a")
        self._register("hr-worker-b")
        self._req("POST", "/api/status",
                  body={"name": "hr-worker-a", "hashrate": 1000.0})
        self._req("POST", "/api/status",
                  body={"name": "hr-worker-b", "hashrate": 2000.0})
        status, data, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(data["aggregate_hashrate"], 3000.0, places=0)

    def test_total_and_online_worker_counts(self):
        self._register("count-a")
        self._register("count-b")
        self._req("POST", "/api/status", body={"name": "count-a", "hashrate": 0})
        self._req("POST", "/api/status", body={"name": "count-b", "hashrate": 0})
        status, data, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(data["total_workers"], 2)
        self.assertEqual(data["online_workers"], 2)

    def test_no_workers_returns_empty_list(self):
        status, data, _ = self._req("GET", "/api/cluster/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["workers"], [])
        self.assertEqual(data["total_workers"], 0)


# ===========================================================================
# Body size and malformed headers
# ===========================================================================

class TestC2BodyGuards(_C2Base):
    """Body size cap and malformed Content-Length handling."""

    def test_negative_content_length_rejected(self):
        """Negative Content-Length must not crash the server."""
        conn = http.client.HTTPConnection("127.0.0.1", _port, timeout=5)
        conn.request(
            "POST", "/api/register",
            body=b"{}",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Content-Length": "-1",
            },
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        # Server must not crash; any 4xx is acceptable
        self.assertGreaterEqual(resp.status, 400)
        self.assertLess(resp.status, 500)

    def test_body_exactly_at_limit_accepted(self):
        """A body right at the 64 KB limit must be processed, not rejected on size."""
        # Build a valid JSON body that is close to but under the limit.
        padding = "x" * (65_000 - 30)
        body_bytes = json.dumps({"name": "edge", "extra": padding}).encode()
        self.assertLessEqual(len(body_bytes), 65536)
        status, _, _ = self._req("POST", "/api/register", raw_body=body_bytes)
        # Status may be 200 (registered) or 400 (name validation) but not 413/500
        self.assertIn(status, (200, 400))

    def test_body_over_limit_rejected(self):
        """A body over 64 KB must return 400."""
        oversized = json.dumps({"name": "big", "pad": "A" * 70_000}).encode()
        self.assertGreater(len(oversized), 65536)
        try:
            status, _, _ = self._req("POST", "/api/register", raw_body=oversized)
            self.assertEqual(status, 400)
        except (ConnectionResetError, ConnectionAbortedError):
            # Windows: server closes the connection while the client is still
            # sending the oversized body, raising WinError 10053/10054.
            pass


# ===========================================================================
# C2 client helper functions
# ===========================================================================

class TestC2Client(_C2Base):
    """c2_client public functions exercised against the live test server."""

    def setUp(self):
        super().setUp()
        from xmrdp.c2_client import configure_tls
        configure_tls(enabled=False, fingerprint="")

    def _base(self):
        return "127.0.0.1", _port, TOKEN

    def test_register_succeeds(self):
        from xmrdp.c2_client import register
        host, port, tok = self._base()
        result = register(host, port, tok, "client-test-worker")
        self.assertEqual(result.get("status"), "registered")

    def test_report_status_returns_ok(self):
        from xmrdp.c2_client import register, report_status
        host, port, tok = self._base()
        register(host, port, tok, "status-reporter")
        result = report_status(host, port, tok, "status-reporter",
                               {"hashrate": 500.0, "uptime": 60, "cpu_usage": 0.4})
        self.assertEqual(result.get("status"), "ok")

    def test_get_cluster_status_returns_dict(self):
        from xmrdp.c2_client import get_cluster_status
        host, port, tok = self._base()
        status = get_cluster_status(host, port, tok)
        self.assertIn("workers", status)
        self.assertIn("aggregate_hashrate", status)

    def test_wrong_token_raises(self):
        from xmrdp.c2_client import get_cluster_status
        host, port, _ = self._base()
        with self.assertRaises((RuntimeError, ConnectionError)):
            get_cluster_status(host, port, "wrong-token")

    def test_worker_appears_in_status_after_register(self):
        from xmrdp.c2_client import register, report_status, get_cluster_status
        host, port, tok = self._base()
        register(host, port, tok, "roundtrip-worker")
        report_status(host, port, tok, "roundtrip-worker",
                      {"hashrate": 777.0, "uptime": 10, "cpu_usage": 0.2})
        cluster = get_cluster_status(host, port, tok)
        names = [w["name"] for w in cluster["workers"]]
        self.assertIn("roundtrip-worker", names)


if __name__ == "__main__":
    unittest.main()
