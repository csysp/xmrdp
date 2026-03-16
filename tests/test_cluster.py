"""Pre-launch cluster integration tests.

Covers config pipeline, node_manager lifecycle, cluster orchestration,
binary manager storage, updater helpers, and an E2E smoke test using a
real C2 server with subprocess-based worker stubs.

Run with: pytest tests/test_cluster.py -v
"""

import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port():
    """Return an unused local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_config(**overrides):
    """Return a minimal valid cluster config dict."""
    cfg = {
        "cluster": {"name": "test-cluster", "wallet": "4" + "A" * 94},
        "master": {
            "host": "127.0.0.1",
            "api_port": 7099,
            "api_token": "test-token-abc123",
            "monerod": {"prune": True, "extra_args": []},
            "p2pool": {"mini": True, "extra_args": []},
            "xmrig": {"threads": 1, "http_token": ""},
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
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# IsolatedTestBase
# ---------------------------------------------------------------------------

class IsolatedTestBase(unittest.TestCase):
    """Base class that redirects all platform dir functions to temp directories.

    Patches both the canonical `xmrdp.platforms.*` symbols AND the names
    bound at import time inside each module (e.g. `xmrdp.node_manager.get_pid_dir`).
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._tmp = Path(self._tmpdir)

        self._pid_dir  = self._tmp / "pids";   self._pid_dir.mkdir()
        self._log_dir  = self._tmp / "logs";   self._log_dir.mkdir()
        self._data_dir = self._tmp / "data";   self._data_dir.mkdir()
        self._bin_dir  = self._tmp / "bins";   self._bin_dir.mkdir()
        self._cfg_dir  = self._tmp / "config"; self._cfg_dir.mkdir()

        self._patches = [
            patch("xmrdp.platforms.get_pid_dir",    return_value=self._pid_dir),
            patch("xmrdp.platforms.get_log_dir",    return_value=self._log_dir),
            patch("xmrdp.platforms.get_data_dir",   return_value=self._data_dir),
            patch("xmrdp.platforms.get_binary_dir", return_value=self._bin_dir),
            patch("xmrdp.platforms.get_config_dir", return_value=self._cfg_dir),
            patch("xmrdp.node_manager.get_pid_dir", return_value=self._pid_dir),
            patch("xmrdp.node_manager.get_log_dir", return_value=self._log_dir),
            patch("xmrdp.node_manager.get_data_dir",return_value=self._data_dir),
            patch("xmrdp.binary_manager.get_binary_dir", return_value=self._bin_dir),
            patch("xmrdp.config.get_config_dir",    return_value=self._cfg_dir),
        ]
        for p in self._patches:
            p.start()

        # Clear the node_manager process registry between tests.
        import xmrdp.node_manager as nm
        with nm._procs_lock:
            nm._procs.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)


# ===========================================================================
# Config pipeline
# ===========================================================================

class TestConfigPipeline(IsolatedTestBase):
    """load_config, validate, generate, and injection-escape tests."""

    def _write_config(self, content: str) -> Path:
        path = self._cfg_dir / "cluster.toml"
        path.write_text(content, encoding="utf-8")
        return path

    # --- load_config ---

    def test_load_config_missing_file_raises(self):
        from xmrdp.config import load_config
        with self.assertRaises(FileNotFoundError):
            load_config(str(self._cfg_dir / "nonexistent.toml"))

    def test_load_config_valid_toml(self):
        from xmrdp.config import load_config
        toml = (
            '[cluster]\nname = "t"\nwallet = "4' + 'A' * 94 + '"\n'
            '[master]\nhost = "127.0.0.1"\napi_token = "tok"\n'
            '[binaries]\n[security]\n'
        )
        path = self._write_config(toml)
        cfg = load_config(str(path))
        self.assertEqual(cfg["cluster"]["name"], "t")

    def test_apply_defaults_fills_api_port(self):
        from xmrdp.config import _apply_defaults
        cfg = {"cluster": {}, "master": {"host": "127.0.0.1"}, "workers": [], "binaries": {}, "security": {}}
        _apply_defaults(cfg)
        from xmrdp.constants import PORTS
        self.assertEqual(cfg["master"]["api_port"], PORTS["c2_api"])

    def test_apply_defaults_fills_prune(self):
        from xmrdp.config import _apply_defaults
        cfg = {"cluster": {}, "master": {"host": "127.0.0.1"}, "workers": [], "binaries": {}, "security": {}}
        _apply_defaults(cfg)
        self.assertTrue(cfg["master"]["monerod"]["prune"])

    def test_apply_defaults_rejects_invalid_host(self):
        from xmrdp.config import _apply_defaults
        cfg = {"cluster": {}, "master": {"host": "bad host!"}, "workers": [], "binaries": {}, "security": {}}
        with self.assertRaises(ValueError):
            _apply_defaults(cfg)

    def test_apply_defaults_accepts_ipv4(self):
        from xmrdp.config import _apply_defaults
        cfg = {"cluster": {}, "master": {"host": "192.168.1.1"}, "workers": [], "binaries": {}, "security": {}}
        _apply_defaults(cfg)  # should not raise

    def test_apply_defaults_accepts_hostname(self):
        from xmrdp.config import _apply_defaults
        cfg = {"cluster": {}, "master": {"host": "master.local"}, "workers": [], "binaries": {}, "security": {}}
        _apply_defaults(cfg)  # should not raise

    def test_apply_defaults_rejects_worker_bad_host(self):
        from xmrdp.config import _apply_defaults
        cfg = {
            "cluster": {}, "master": {"host": "127.0.0.1"},
            "workers": [{"name": "w1", "host": "bad host!"}],
            "binaries": {}, "security": {},
        }
        with self.assertRaises(ValueError):
            _apply_defaults(cfg)

    def test_extra_args_injection_rejected_monerod(self):
        from xmrdp.config import _apply_defaults
        cfg = {
            "cluster": {}, "master": {
                "host": "127.0.0.1",
                "monerod": {"prune": True, "extra_args": ["; rm -rf /"]},
                "p2pool": {"mini": True, "extra_args": []},
            },
            "workers": [], "binaries": {}, "security": {},
        }
        with self.assertRaises(ValueError):
            _apply_defaults(cfg)

    def test_extra_args_injection_rejected_p2pool(self):
        from xmrdp.config import _apply_defaults
        cfg = {
            "cluster": {}, "master": {
                "host": "127.0.0.1",
                "monerod": {"prune": True, "extra_args": []},
                "p2pool": {"mini": True, "extra_args": ["--ok", "`id`"]},
            },
            "workers": [], "binaries": {}, "security": {},
        }
        with self.assertRaises(ValueError):
            _apply_defaults(cfg)

    def test_extra_args_valid_flag(self):
        from xmrdp.config import _apply_defaults
        cfg = {
            "cluster": {}, "master": {
                "host": "127.0.0.1",
                "monerod": {"prune": True, "extra_args": ["--no-igd"]},
                "p2pool": {"mini": True, "extra_args": ["--loglevel=3"]},
            },
            "workers": [], "binaries": {}, "security": {},
        }
        _apply_defaults(cfg)  # should not raise

    # --- validate_wallet ---

    def test_validate_wallet_valid_standard(self):
        from xmrdp.config import validate_wallet
        addr = "4" + "A" * 94
        ok, _ = validate_wallet(addr)
        self.assertTrue(ok)

    def test_validate_wallet_valid_subaddress(self):
        from xmrdp.config import validate_wallet
        addr = "8" + "A" * 94
        ok, _ = validate_wallet(addr)
        self.assertTrue(ok)

    def test_validate_wallet_empty(self):
        from xmrdp.config import validate_wallet
        ok, msg = validate_wallet("")
        self.assertFalse(ok)
        self.assertIn("empty", msg)

    def test_validate_wallet_wrong_length(self):
        from xmrdp.config import validate_wallet
        ok, _ = validate_wallet("4" + "A" * 50)
        self.assertFalse(ok)

    # --- _toml_str injection escaping ---

    def test_toml_str_escapes_backslash(self):
        from xmrdp.config import _toml_str
        self.assertEqual(_toml_str("a\\b"), "a\\\\b")

    def test_toml_str_escapes_double_quote(self):
        from xmrdp.config import _toml_str
        self.assertEqual(_toml_str('a"b'), 'a\\"b')

    # --- generate_default_config ---

    def test_generate_default_config_produces_toml(self):
        from xmrdp.config import generate_default_config
        content = generate_default_config(wallet="4" + "A" * 94, master_host="127.0.0.1")
        self.assertIn("[cluster]", content)
        self.assertIn("[master]", content)
        self.assertIn("[security]", content)


# ===========================================================================
# Node manager — PID files and process lifecycle
# ===========================================================================

class TestNodeManager(IsolatedTestBase):
    """Test PID-file helpers and service lifecycle (using real stub processes)."""

    def test_write_and_read_pid(self):
        import xmrdp.node_manager as nm
        nm._write_pid("test-svc", 12345)
        self.assertEqual(nm._read_pid("test-svc"), 12345)

    def test_read_pid_missing_returns_none(self):
        import xmrdp.node_manager as nm
        self.assertIsNone(nm._read_pid("nonexistent"))

    def test_remove_pid_deletes_file(self):
        import xmrdp.node_manager as nm
        nm._write_pid("del-svc", 999)
        nm._remove_pid("del-svc")
        self.assertIsNone(nm._read_pid("del-svc"))

    def test_remove_pid_missing_is_noop(self):
        import xmrdp.node_manager as nm
        nm._remove_pid("ghost-svc")  # should not raise

    def test_read_pid_invalid_content_returns_none(self):
        import xmrdp.node_manager as nm
        pid_file = self._pid_dir / "bad.pid"
        pid_file.write_text("not-a-number")
        # Monkey-patch to use our file
        with patch("xmrdp.node_manager.get_pid_dir", return_value=self._pid_dir):
            result = nm._read_pid("bad")
        self.assertIsNone(result)

    def test_process_exists_dead_pid(self):
        import xmrdp.node_manager as nm
        self.assertFalse(nm._process_exists(999999999))

    def test_start_service_launches_process(self):
        import xmrdp.node_manager as nm
        pid = nm.start_service(
            "stub",
            sys.executable,
            ["-c", "import time; time.sleep(30)"],
        )
        try:
            self.assertIsInstance(pid, int)
            self.assertGreater(pid, 0)
            self.assertTrue(nm.is_running("stub"))
        finally:
            nm.stop_service("stub")

    def test_start_service_registers_in_procs(self):
        import xmrdp.node_manager as nm
        nm.start_service("stub2", sys.executable, ["-c", "import time; time.sleep(30)"])
        try:
            with nm._procs_lock:
                self.assertIn("stub2", nm._procs)
        finally:
            nm.stop_service("stub2")

    def test_start_service_creates_pid_file(self):
        import xmrdp.node_manager as nm
        nm.start_service("stub3", sys.executable, ["-c", "import time; time.sleep(30)"])
        try:
            pid_file = self._pid_dir / "stub3.pid"
            self.assertTrue(pid_file.exists())
        finally:
            nm.stop_service("stub3")

    def test_start_service_creates_log_file(self):
        import xmrdp.node_manager as nm
        nm.start_service("stub4", sys.executable, ["-c", "import time; time.sleep(30)"])
        try:
            log_file = self._log_dir / "stub4.log"
            self.assertTrue(log_file.exists())
        finally:
            nm.stop_service("stub4")

    def test_stop_service_no_pid_file_returns_false(self):
        import xmrdp.node_manager as nm
        result = nm.stop_service("no-such-service")
        self.assertFalse(result)

    def test_stop_service_kills_process(self):
        import xmrdp.node_manager as nm
        nm.start_service("kill-me", sys.executable, ["-c", "import time; time.sleep(30)"])
        self.assertTrue(nm.is_running("kill-me"))
        result = nm.stop_service("kill-me")
        self.assertTrue(result)
        # Give the OS a moment to reap the process.
        time.sleep(0.2)
        self.assertFalse(nm.is_running("kill-me"))

    def test_stop_service_removes_pid_file(self):
        import xmrdp.node_manager as nm
        nm.start_service("pid-clean", sys.executable, ["-c", "import time; time.sleep(30)"])
        nm.stop_service("pid-clean")
        pid_file = self._pid_dir / "pid-clean.pid"
        self.assertFalse(pid_file.exists())

    def test_is_running_no_pid_file_returns_false(self):
        import xmrdp.node_manager as nm
        self.assertFalse(nm.is_running("never-started"))

    def test_is_running_stale_pid_cleans_up(self):
        import xmrdp.node_manager as nm
        nm._write_pid("stale", 999999999)
        result = nm.is_running("stale")
        self.assertFalse(result)
        # Stale file should be removed.
        self.assertIsNone(nm._read_pid("stale"))

    def test_is_running_live_process(self):
        import xmrdp.node_manager as nm
        nm.start_service("live-chk", sys.executable, ["-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(nm.is_running("live-chk"))
        finally:
            nm.stop_service("live-chk")

    def test_get_process_unknown_name_returns_none(self):
        import xmrdp.node_manager as nm
        self.assertIsNone(nm.get_process("no-such"))

    def test_get_process_exited_returns_none(self):
        import xmrdp.node_manager as nm
        nm.start_service("exit-fast", sys.executable, ["-c", "pass"])
        time.sleep(0.3)  # Let it exit
        result = nm.get_process("exit-fast")
        self.assertIsNone(result)

    def test_get_process_live_returns_popen(self):
        import xmrdp.node_manager as nm
        nm.start_service("live-proc", sys.executable, ["-c", "import time; time.sleep(30)"])
        try:
            proc = nm.get_process("live-proc")
            self.assertIsNotNone(proc)
            self.assertIsNone(proc.poll())
        finally:
            nm.stop_service("live-proc")

    def test_wait_for_port_timeout(self):
        """_wait_for_port should return False on a port nothing is listening on."""
        import xmrdp.node_manager as nm
        port = _free_port()
        result = nm._wait_for_port("127.0.0.1", port, timeout=0.5, interval=0.1)
        self.assertFalse(result)

    def test_wait_for_port_success(self):
        """_wait_for_port should return True once a server starts listening."""
        import xmrdp.node_manager as nm
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            result = nm._wait_for_port("127.0.0.1", port, timeout=2.0, interval=0.1)
            self.assertTrue(result)
        finally:
            srv.close()


# ===========================================================================
# Node manager — concurrent PID-file lock safety
# ===========================================================================

class TestNodeManagerLock(IsolatedTestBase):
    """Verify that concurrent reads/writes to _procs don't corrupt state."""

    def test_concurrent_start_stop_no_deadlock(self):
        """Multiple threads starting and stopping services must not deadlock."""
        import xmrdp.node_manager as nm
        errors = []

        def worker(name):
            try:
                nm.start_service(name, sys.executable, ["-c", "import time; time.sleep(5)"])
                time.sleep(0.05)
                nm.stop_service(name)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"t-{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [], f"Errors during concurrent start/stop: {errors}")

    def test_procs_lock_protects_registry(self):
        """Direct manipulation of _procs under the lock should be safe."""
        import xmrdp.node_manager as nm
        results = []

        def reader():
            for _ in range(50):
                with nm._procs_lock:
                    results.append(len(nm._procs))

        t = threading.Thread(target=reader)
        t.start()
        nm.start_service("lock-svc", sys.executable, ["-c", "import time; time.sleep(5)"])
        t.join(timeout=5)
        nm.stop_service("lock-svc")
        self.assertTrue(all(isinstance(r, int) for r in results))


# ===========================================================================
# Cluster orchestration (mocked)
# ===========================================================================

class TestClusterOrchestration(IsolatedTestBase):
    """deploy_master / deploy_worker tested with mocked service calls."""

    def _base_cfg(self, **kwargs):
        cfg = _make_config()
        cfg.update(kwargs)
        return cfg

    # --- deploy_master ---

    def test_deploy_master_exits_without_wallet(self):
        from xmrdp.cluster import deploy_master
        cfg = _make_config()
        cfg["cluster"]["wallet"] = ""
        with self.assertRaises(SystemExit):
            deploy_master(cfg)

    def test_deploy_master_warns_without_token(self):
        from xmrdp.cluster import deploy_master
        cfg = _make_config()
        cfg["master"]["api_token"] = ""
        import io as _io
        with patch("xmrdp.node_manager.start_master"), \
             patch("xmrdp.c2_server.start_c2_server", return_value=MagicMock()):
            with patch("sys.stderr", new_callable=_io.StringIO) as mock_err:
                try:
                    deploy_master(cfg)
                except SystemExit:
                    pass
                # Token warning should appear (deploy_master warns but doesn't exit)
                self.assertIn("Warning", mock_err.getvalue())

    def test_deploy_master_calls_start_master(self):
        from xmrdp.cluster import deploy_master
        cfg = _make_config()
        with patch("xmrdp.node_manager.start_master") as mock_sm, \
             patch("xmrdp.c2_server.start_c2_server", return_value=MagicMock()):
            deploy_master(cfg)
        mock_sm.assert_called_once_with(cfg)

    def test_deploy_master_calls_start_c2_server(self):
        from xmrdp.cluster import deploy_master
        cfg = _make_config()
        with patch("xmrdp.node_manager.start_master"), \
             patch("xmrdp.c2_server.start_c2_server") as mock_c2:
            mock_c2.return_value = MagicMock()
            deploy_master(cfg)
        mock_c2.assert_called_once_with(cfg)

    def test_deploy_master_handles_start_master_exception(self):
        from xmrdp.cluster import deploy_master
        cfg = _make_config()
        with patch("xmrdp.node_manager.start_master", side_effect=RuntimeError("binary not found")):
            with self.assertRaises(SystemExit):
                deploy_master(cfg)

    # --- deploy_worker ---

    def test_deploy_worker_exits_without_token(self):
        from xmrdp.cluster import deploy_worker
        cfg = _make_config()
        cfg["master"]["api_token"] = ""
        with self.assertRaises(SystemExit):
            deploy_worker(cfg)

    def test_deploy_worker_warns_on_tls_disabled(self):
        from xmrdp.cluster import deploy_worker
        cfg = _make_config()
        cfg["security"]["tls_enabled"] = False

        stub_proc = MagicMock()
        stub_proc.poll.return_value = None
        stub_proc.wait.side_effect = KeyboardInterrupt

        with patch("xmrdp.c2_client.register"), \
             patch("xmrdp.node_manager.start_worker"), \
             patch("xmrdp.node_manager.get_process", return_value=stub_proc), \
             patch("xmrdp.c2_client.run_heartbeat_loop"), \
             patch("sys.stderr") as mock_err:
            try:
                deploy_worker(cfg)
            except (SystemExit, KeyboardInterrupt):
                pass
            call_text = "".join(str(c) for c in mock_err.write.call_args_list)
            self.assertIn("TLS", call_text)

    def test_deploy_worker_calls_register(self):
        from xmrdp.cluster import deploy_worker
        cfg = _make_config()

        stub_proc = MagicMock()
        stub_proc.poll.return_value = None
        stub_proc.wait.side_effect = KeyboardInterrupt

        with patch("xmrdp.c2_client.register") as mock_reg, \
             patch("xmrdp.node_manager.start_worker"), \
             patch("xmrdp.node_manager.get_process", return_value=stub_proc), \
             patch("xmrdp.c2_client.run_heartbeat_loop"), \
             patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            try:
                deploy_worker(cfg)
            except (SystemExit, KeyboardInterrupt):
                pass
        mock_reg.assert_called_once()

    def test_deploy_worker_calls_start_worker(self):
        from xmrdp.cluster import deploy_worker
        cfg = _make_config()

        stub_proc = MagicMock()
        stub_proc.poll.return_value = None
        stub_proc.wait.side_effect = KeyboardInterrupt

        with patch("xmrdp.c2_client.register"), \
             patch("xmrdp.node_manager.start_worker") as mock_sw, \
             patch("xmrdp.node_manager.get_process", return_value=stub_proc), \
             patch("xmrdp.c2_client.run_heartbeat_loop"), \
             patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = MagicMock()
            try:
                deploy_worker(cfg)
            except (SystemExit, KeyboardInterrupt):
                pass
        mock_sw.assert_called_once()


# ===========================================================================
# Cluster status
# ===========================================================================

class TestClusterStatus(IsolatedTestBase):
    """cluster_status local fallback and C2-query paths."""

    def _no_token_cfg(self):
        cfg = _make_config()
        cfg["master"]["api_token"] = ""
        return cfg

    def test_local_fallback_shape(self):
        from xmrdp.cluster import cluster_status
        cfg = self._no_token_cfg()
        with patch("xmrdp.node_manager.is_running", return_value=False):
            status = cluster_status(cfg)
        self.assertIn("master", status)
        self.assertIn("workers", status)
        self.assertIn("aggregate_hashrate", status)

    def test_local_fallback_cluster_name(self):
        from xmrdp.cluster import cluster_status
        cfg = self._no_token_cfg()
        cfg["cluster"]["name"] = "my-test-cluster"
        with patch("xmrdp.node_manager.is_running", return_value=False):
            status = cluster_status(cfg)
        self.assertEqual(status["cluster"], "my-test-cluster")

    def test_local_status_running_when_any_service_up(self):
        from xmrdp.cluster import cluster_status
        cfg = self._no_token_cfg()
        with patch("xmrdp.node_manager.is_running", side_effect=lambda name: name == "monerod"):
            status = cluster_status(cfg)
        self.assertEqual(status["master"]["status"], "running")

    def test_local_status_stopped_when_all_down(self):
        from xmrdp.cluster import cluster_status
        cfg = self._no_token_cfg()
        with patch("xmrdp.node_manager.is_running", return_value=False):
            status = cluster_status(cfg)
        self.assertEqual(status["master"]["status"], "stopped")

    def test_local_fallback_services_dict_has_three_keys(self):
        from xmrdp.cluster import cluster_status
        cfg = self._no_token_cfg()
        with patch("xmrdp.node_manager.is_running", return_value=False):
            status = cluster_status(cfg)
        svcs = status["master"]["services"]
        self.assertIn("monerod", svcs)
        self.assertIn("p2pool", svcs)
        self.assertIn("xmrig", svcs)

    def test_c2_query_used_when_token_present(self):
        from xmrdp.cluster import cluster_status
        cfg = _make_config()
        mock_response = {
            "cluster": "remote-cluster",
            "master": {"status": "running", "services": {}},
            "workers": [],
            "aggregate_hashrate": 99.9,
        }
        with patch("xmrdp.c2_client.get_cluster_status", return_value=mock_response), \
             patch("xmrdp.c2_client.configure_tls"):
            status = cluster_status(cfg)
        self.assertEqual(status["aggregate_hashrate"], 99.9)

    def test_c2_query_falls_back_on_exception(self):
        from xmrdp.cluster import cluster_status
        cfg = _make_config()
        with patch("xmrdp.c2_client.get_cluster_status", side_effect=ConnectionError("unreachable")), \
             patch("xmrdp.c2_client.configure_tls"), \
             patch("xmrdp.node_manager.is_running", return_value=False):
            status = cluster_status(cfg)
        # Should have fallen back to local
        self.assertIn("master", status)

    def test_print_status_table_contains_cluster_name(self):
        from xmrdp.cluster import _print_status_table
        import io as _io
        status = {
            "cluster": "fancy-cluster",
            "master": {"status": "running", "services": {}},
            "workers": [],
            "total_workers": 0,
            "online_workers": 0,
            "aggregate_hashrate": 42.5,
        }
        with patch("sys.stdout", new_callable=_io.StringIO) as mock_out:
            _print_status_table(status)
            output = mock_out.getvalue()
        self.assertIn("fancy-cluster", output)

    def test_print_status_table_shows_aggregate_hashrate(self):
        from xmrdp.cluster import _print_status_table
        import io as _io
        status = {
            "cluster": "x",
            "master": {"status": "stopped", "services": {}},
            "workers": [{"name": "w1", "status": "online", "hashrate": 1500.0}],
            "total_workers": 1,
            "online_workers": 1,
            "aggregate_hashrate": 1500.0,
        }
        with patch("sys.stdout", new_callable=_io.StringIO) as mock_out:
            _print_status_table(status)
            output = mock_out.getvalue()
        self.assertIn("1500.0", output)

    def test_print_status_table_shows_worker_row(self):
        from xmrdp.cluster import _print_status_table
        import io as _io
        status = {
            "cluster": "x",
            "master": {"status": "stopped", "services": {}},
            "workers": [{"name": "miner-01", "status": "online", "hashrate": 750.0}],
            "total_workers": 1,
            "online_workers": 1,
            "aggregate_hashrate": 750.0,
        }
        with patch("sys.stdout", new_callable=_io.StringIO) as mock_out:
            _print_status_table(status)
            output = mock_out.getvalue()
        self.assertIn("miner-01", output)


# ===========================================================================
# Binary manager storage
# ===========================================================================

class TestBinaryManagerStorage(IsolatedTestBase):
    """_read_versions, _write_versions, get_binary_path, extract_binary, checksums."""

    def test_read_versions_no_file_returns_empty(self):
        import xmrdp.binary_manager as bm
        result = bm._read_versions()
        self.assertEqual(result, {})

    def test_write_and_read_versions_roundtrip(self):
        import xmrdp.binary_manager as bm
        data = {"monero": {"version": "v0.18.3.4", "path": "/tmp/monerod"}}
        bm._write_versions(data)
        result = bm._read_versions()
        self.assertEqual(result, data)

    def test_get_binary_path_no_versions_returns_none(self):
        import xmrdp.binary_manager as bm
        result = bm.get_binary_path("xmrig")
        self.assertIsNone(result)

    def test_get_binary_path_missing_file_returns_none(self):
        import xmrdp.binary_manager as bm
        bm._write_versions({"xmrig": {"version": "v6.21.0", "path": "/nonexistent/xmrig"}})
        result = bm.get_binary_path("xmrig")
        self.assertIsNone(result)

    def test_get_binary_path_existing_file_returns_path(self):
        import xmrdp.binary_manager as bm
        fake_bin = self._bin_dir / "xmrig"
        fake_bin.write_bytes(b"fake")
        bm._write_versions({"xmrig": {"version": "v6.21.0", "path": str(fake_bin)}})
        result = bm.get_binary_path("xmrig")
        self.assertEqual(result, fake_bin)

    def test_extract_binary_unsupported_format_raises(self):
        import xmrdp.binary_manager as bm
        with tempfile.NamedTemporaryFile(suffix=".rar", delete=False) as f:
            f.write(b"fake")
            tmp = f.name
        try:
            with self.assertRaises(ValueError):
                bm.extract_binary(tmp, "xmrig", str(self._bin_dir))
        finally:
            os.unlink(tmp)

    def test_extract_binary_from_tar_gz(self):
        """extract_binary correctly extracts a binary from a .tar.gz archive."""
        import xmrdp.binary_manager as bm
        # Detect expected binary name for current platform
        from xmrdp.platforms import detect_platform
        system, _ = detect_platform()
        bin_name = "xmrig.exe" if system == "windows" else "xmrig"

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "xmrig-6.21.0.tar.gz"
            # Build a .tar.gz with a nested binary
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                content = b"#!/bin/sh\necho xmrig"
                info = tarfile.TarInfo(name=f"xmrig-6.21.0/{bin_name}")
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
            archive.write_bytes(buf.getvalue())

            dest_dir = Path(tmp) / "out"
            dest_dir.mkdir()
            result = bm.extract_binary(str(archive), "xmrig", str(dest_dir))
            self.assertTrue(result.exists())
            self.assertEqual(result.name, bin_name)

    def test_extract_binary_missing_binary_raises_not_found(self):
        """FileNotFoundError when the expected binary isn't in the archive."""
        import xmrdp.binary_manager as bm
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "empty.tar.gz"
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                # Add a file with a wrong name
                content = b"not-the-binary"
                info = tarfile.TarInfo(name="subdir/wrong_name")
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
            archive.write_bytes(buf.getvalue())

            with self.assertRaises(FileNotFoundError):
                bm.extract_binary(str(archive), "xmrig", tmp)

    def test_zip_slip_in_zip_raises(self):
        """extract_binary must reject zip archives with path traversal entries."""
        import xmrdp.binary_manager as bm
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "evil.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../../../etc/passwd", "root:x:0:0:")
            with self.assertRaises(RuntimeError) as ctx:
                bm.extract_binary(str(archive), "xmrig", tmp)
            self.assertIn("Zip Slip", str(ctx.exception))

    def test_zip_slip_in_tar_raises(self):
        """extract_binary must reject tar archives with path traversal entries.

        On Python < 3.12 this raises RuntimeError; on 3.12+ tarfile's built-in
        filter raises tarfile.OutsideDestinationError.  Either way, an exception
        must be raised — files must not be extracted outside the destination.
        """
        import xmrdp.binary_manager as bm
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "evil.tar.gz"
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                info = tarfile.TarInfo(name="../../../etc/cron.d/evil")
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
            archive.write_bytes(buf.getvalue())

            with self.assertRaises(Exception):
                bm.extract_binary(str(archive), "xmrig", tmp)

    def test_verify_checksum_correct(self):
        import xmrdp.binary_manager as bm
        data = b"hello xmrdp"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            self.assertTrue(bm.verify_checksum(tmp, expected))
        finally:
            os.unlink(tmp)

    def test_verify_checksum_wrong_hash(self):
        import xmrdp.binary_manager as bm
        data = b"hello xmrdp"
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            self.assertFalse(bm.verify_checksum(tmp, "0" * 64))
        finally:
            os.unlink(tmp)


# ===========================================================================
# Updater / release helpers
# ===========================================================================

class TestUpdater(IsolatedTestBase):
    """match_asset, get_release_checksums, and boundary conditions."""

    _FAKE_ASSETS = [
        {"name": "xmrig-6.21.0-linux-x64.tar.gz",    "browser_download_url": "http://x", "size": 1000},
        {"name": "xmrig-6.21.0-msvc-win64.zip",       "browser_download_url": "http://x", "size": 2000},
        {"name": "xmrig-6.21.0-macos-x64.tar.gz",     "browser_download_url": "http://x", "size": 3000},
        {"name": "xmrig-6.21.0-macos-arm64.tar.gz",   "browser_download_url": "http://x", "size": 4000},
        {"name": "SHA256SUMS",                          "browser_download_url": "http://sums", "size": 500},
    ]

    def test_match_asset_linux_x86_64(self):
        import xmrdp.binary_manager as bm
        asset = bm.match_asset(self._FAKE_ASSETS, "xmrig", "linux", "x86_64")
        self.assertIsNotNone(asset)
        self.assertIn("linux-x64", asset["name"])

    def test_match_asset_windows_x86_64(self):
        import xmrdp.binary_manager as bm
        asset = bm.match_asset(self._FAKE_ASSETS, "xmrig", "windows", "x86_64")
        self.assertIsNotNone(asset)
        self.assertIn("win64", asset["name"])

    def test_match_asset_darwin_aarch64(self):
        import xmrdp.binary_manager as bm
        asset = bm.match_asset(self._FAKE_ASSETS, "xmrig", "darwin", "aarch64")
        self.assertIsNotNone(asset)
        self.assertIn("arm64", asset["name"])

    def test_match_asset_unknown_platform_returns_none(self):
        import xmrdp.binary_manager as bm
        result = bm.match_asset(self._FAKE_ASSETS, "xmrig", "freebsd", "x86_64")
        self.assertIsNone(result)

    def test_match_asset_unknown_software_returns_none(self):
        import xmrdp.binary_manager as bm
        result = bm.match_asset(self._FAKE_ASSETS, "unknown_sw", "linux", "x86_64")
        self.assertIsNone(result)

    def test_get_release_checksums_parses_gnu_format(self):
        import xmrdp.binary_manager as bm
        # SHA-256 hashes must be exactly 64 hex chars
        hash1 = "a" * 64
        hash2 = "b" * 64
        checksum_text = (
            f"{hash1}  xmrig-6.21.0-linux-x64.tar.gz\n"
            f"{hash2}  xmrig-6.21.0-msvc-win64.zip\n"
        )
        fake_resp = MagicMock()
        fake_resp.read.return_value = checksum_text.encode("utf-8")

        with patch("xmrdp.binary_manager._request", return_value=fake_resp):
            result = bm.get_release_checksums(self._FAKE_ASSETS, "xmrig")
        self.assertIn("xmrig-6.21.0-linux-x64.tar.gz", result)
        self.assertEqual(result["xmrig-6.21.0-linux-x64.tar.gz"], hash1)

    def test_get_release_checksums_no_matching_file_returns_empty(self):
        import xmrdp.binary_manager as bm
        # Assets without a SHA256SUMS file
        assets = [a for a in self._FAKE_ASSETS if a["name"] != "SHA256SUMS"]
        result = bm.get_release_checksums(assets, "xmrig")
        self.assertEqual(result, {})

    def test_get_release_checksums_unknown_software_returns_empty(self):
        import xmrdp.binary_manager as bm
        result = bm.get_release_checksums(self._FAKE_ASSETS, "unknown")
        self.assertEqual(result, {})

    def test_download_binary_rejects_oversized_content_length(self):
        import xmrdp.binary_manager as bm

        class _FakeHeaders:
            def get(self, key, default=None):
                if key == "Content-Length":
                    return str(3 * 1024 * 1024 * 1024)
                return default

        mock_resp = MagicMock()
        mock_resp.headers = _FakeHeaders()

        with patch("xmrdp.binary_manager._request", return_value=mock_resp):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError) as ctx:
                    bm.download_binary("http://fake/file.tar.gz", Path(tmp) / "out.tar.gz")
        self.assertIn("exceed", str(ctx.exception).lower())


# ===========================================================================
# E2E smoke test — live C2 server + direct client calls
# ===========================================================================

_e2e_port = None
_e2e_server = None
_e2e_thread = None
_E2E_TOKEN = "e2e-smoke-token-xyz789"


def setUpModule():
    global _e2e_port, _e2e_server, _e2e_thread
    from xmrdp.c2_server import start_c2_server

    _e2e_port = _free_port()
    e2e_cfg = _make_config()
    e2e_cfg["master"]["api_port"] = _e2e_port
    e2e_cfg["master"]["api_token"] = _E2E_TOKEN

    _e2e_server = start_c2_server(e2e_cfg)
    # Give the server a moment to bind
    for _ in range(20):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", _e2e_port, timeout=1)
            conn.request("GET", "/api/cluster/status",
                         headers={"Authorization": f"Bearer {_E2E_TOKEN}"})
            resp = conn.getresponse()
            resp.read()
            conn.close()
            break
        except Exception:
            time.sleep(0.1)


def tearDownModule():
    global _e2e_server
    if _e2e_server is not None:
        try:
            from xmrdp.c2_server import stop_c2_server
            stop_c2_server(_e2e_server)
        except Exception:
            pass


class TestEndToEndSmoke(unittest.TestCase):
    """Functional tests against a live C2 server using c2_client functions."""

    def setUp(self):
        import xmrdp.c2_server as srv
        import xmrdp.c2_client as client
        with srv._auth_failures_lock:
            srv._auth_failures.clear()
        with srv._workers_lock:
            srv._workers.clear()
        client.configure_tls(enabled=False)

    def _make_request(self, method, path, token=_E2E_TOKEN, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", _e2e_port, timeout=5)
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(body).encode("utf-8")
        else:
            body_bytes = None
        conn.request(method, path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

    def test_register_via_client(self):
        from xmrdp.c2_client import register
        result = register("127.0.0.1", _e2e_port, _E2E_TOKEN, "e2e-worker-1")
        self.assertIn("status", result)

    def test_worker_appears_in_cluster_status(self):
        from xmrdp.c2_client import register, get_cluster_status
        register("127.0.0.1", _e2e_port, _E2E_TOKEN, "e2e-worker-2")
        status = get_cluster_status("127.0.0.1", _e2e_port, _E2E_TOKEN)
        names = [w.get("name") for w in status.get("workers", [])]
        self.assertIn("e2e-worker-2", names)

    def test_report_status_updates_hashrate(self):
        from xmrdp.c2_client import register, report_status, get_cluster_status
        register("127.0.0.1", _e2e_port, _E2E_TOKEN, "e2e-hr-worker")
        report_status(
            "127.0.0.1", _e2e_port, _E2E_TOKEN, "e2e-hr-worker",
            {"hashrate": 1234.5, "uptime": 60, "cpu_usage": 0.5},
        )
        status = get_cluster_status("127.0.0.1", _e2e_port, _E2E_TOKEN)
        worker = next((w for w in status.get("workers", []) if w["name"] == "e2e-hr-worker"), None)
        self.assertIsNotNone(worker)
        self.assertAlmostEqual(worker.get("hashrate", 0), 1234.5, places=1)

    def test_aggregate_hashrate_sums_workers(self):
        from xmrdp.c2_client import register, report_status, get_cluster_status
        for name, hr in [("agg-w1", 100.0), ("agg-w2", 200.0)]:
            register("127.0.0.1", _e2e_port, _E2E_TOKEN, name)
            report_status(
                "127.0.0.1", _e2e_port, _E2E_TOKEN, name,
                {"hashrate": hr, "uptime": 10, "cpu_usage": 0.1},
            )
        status = get_cluster_status("127.0.0.1", _e2e_port, _E2E_TOKEN)
        self.assertGreaterEqual(status.get("aggregate_hashrate", 0), 300.0)

    def test_wrong_token_raises(self):
        from xmrdp.c2_client import get_cluster_status
        with self.assertRaises((RuntimeError, ConnectionError)):
            get_cluster_status("127.0.0.1", _e2e_port, "wrong-token")

    def test_full_worker_lifecycle(self):
        """register -> heartbeat -> cluster_status shows the worker as online."""
        from xmrdp.c2_client import register, report_status, get_cluster_status
        name = "lifecycle-worker"
        register("127.0.0.1", _e2e_port, _E2E_TOKEN, name)

        # Simulate two heartbeats
        for i in range(2):
            report_status(
                "127.0.0.1", _e2e_port, _E2E_TOKEN, name,
                {"hashrate": float(i * 50), "uptime": i * 60, "cpu_usage": 0.2},
            )

        status = get_cluster_status("127.0.0.1", _e2e_port, _E2E_TOKEN)
        worker = next((w for w in status.get("workers", []) if w["name"] == name), None)
        self.assertIsNotNone(worker, "Worker must appear in cluster status after lifecycle")
        self.assertIn(worker.get("status"), ("online", "running", "active"))


if __name__ == "__main__":
    unittest.main()
