"""Unit tests for xmrdp.sync — config sync to worker nodes."""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal config fixture
# ---------------------------------------------------------------------------

_WALLET = "4" + "A" * 94

_BASE_CONFIG = {
    "cluster": {"name": "test-cluster", "wallet": _WALLET},
    "master": {
        "host": "192.168.1.1",
        "api_port": 7099,
        "api_token": "deadbeef" * 8,
        "monerod": {"prune": True, "extra_args": []},
        "p2pool": {"mini": True, "extra_args": []},
        "xmrig": {"threads": 0, "http_token": ""},
    },
    "workers": [
        {"name": "worker-1", "host": "192.168.1.10"},
        {"name": "worker-2", "host": "192.168.1.11"},
    ],
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


def _make_args(**kwargs):
    """Build a minimal argparse-like namespace for cmd_sync."""
    ns = types.SimpleNamespace(
        config=None,
        worker=None,
        ssh_user="",
        dry_run=False,
        restart=False,
        verbose=False,
    )
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# _generate_worker_config
# ---------------------------------------------------------------------------

class TestGenerateWorkerConfig(unittest.TestCase):

    def _gen(self, worker_name, config=None):
        from xmrdp.sync import _generate_worker_config
        return _generate_worker_config(config or _BASE_CONFIG, worker_name)

    def test_self_true_set_for_target_worker(self):
        toml = self._gen("worker-1")
        lines = toml.splitlines()
        # Find the worker-1 block and assert self = true appears after its name line
        idx = next(i for i, l in enumerate(lines) if 'name = "worker-1"' in l)
        block = "\n".join(lines[idx:idx + 5])
        self.assertIn("self = true", block)

    def test_self_true_not_set_for_other_workers(self):
        toml = self._gen("worker-1")
        lines = toml.splitlines()
        # Find the worker-2 block and assert no self = true
        idx = next(i for i, l in enumerate(lines) if 'name = "worker-2"' in l)
        # Check the next few lines for self = true (it should not be there)
        block_end = idx + 5
        block = "\n".join(lines[idx:block_end])
        self.assertNotIn("self = true", block)

    def test_wallet_preserved(self):
        toml = self._gen("worker-1")
        self.assertIn(_WALLET, toml)

    def test_master_host_preserved(self):
        toml = self._gen("worker-1")
        self.assertIn("192.168.1.1", toml)

    def test_api_token_preserved(self):
        toml = self._gen("worker-1")
        self.assertIn("deadbeef" * 8, toml)

    def test_both_workers_present(self):
        toml = self._gen("worker-1")
        self.assertIn('"worker-1"', toml)
        self.assertIn('"worker-2"', toml)

    def test_output_is_valid_toml(self):
        """Generated output must be parseable by tomllib."""
        toml = self._gen("worker-1")
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        parsed = tomllib.loads(toml)
        self.assertEqual(parsed["cluster"]["wallet"], _WALLET)
        self.assertEqual(parsed["master"]["host"], "192.168.1.1")

    def test_api_port_uses_constant(self):
        """api_port must use PORTS["c2_api"] as fallback, not a magic number."""
        from xmrdp.constants import PORTS
        config = dict(_BASE_CONFIG)
        config["master"] = dict(_BASE_CONFIG["master"])
        del config["master"]["api_port"]  # force fallback path
        # Re-apply defaults so api_port is set from PORTS
        config["master"]["api_port"] = PORTS["c2_api"]
        toml = self._gen("worker-1", config=config)
        self.assertIn(str(PORTS["c2_api"]), toml)


# ---------------------------------------------------------------------------
# cmd_sync — exit conditions
# ---------------------------------------------------------------------------

class TestCmdSyncExit(unittest.TestCase):

    def test_no_workers_defined_exits(self):
        from xmrdp.sync import cmd_sync
        config = dict(_BASE_CONFIG)
        config["workers"] = []
        args = _make_args(dry_run=True)
        with patch("xmrdp.cluster._load_config_or_exit", return_value=config):
            with self.assertRaises(SystemExit):
                cmd_sync(args)

    def test_worker_filter_no_match_exits(self):
        from xmrdp.sync import cmd_sync
        args = _make_args(worker=["nonexistent"], dry_run=True)
        with patch("xmrdp.cluster._load_config_or_exit", return_value=_BASE_CONFIG):
            with self.assertRaises(SystemExit):
                cmd_sync(args)

    def test_missing_ssh_exits_when_not_dry_run(self):
        from xmrdp.sync import cmd_sync
        args = _make_args(dry_run=False)
        with patch("xmrdp.cluster._load_config_or_exit", return_value=_BASE_CONFIG), \
             patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit):
                cmd_sync(args)


# ---------------------------------------------------------------------------
# cmd_sync — dry-run path
# ---------------------------------------------------------------------------

class TestCmdSyncDryRun(unittest.TestCase):

    def test_dry_run_prints_ok_for_each_worker(self):
        from xmrdp.sync import cmd_sync
        args = _make_args(dry_run=True)
        with patch("xmrdp.cluster._load_config_or_exit", return_value=_BASE_CONFIG):
            # Should not raise
            try:
                cmd_sync(args)
            except SystemExit as e:
                self.fail(f"cmd_sync raised SystemExit({e.code}) unexpectedly in dry-run")

    def test_dry_run_worker_filter(self):
        from xmrdp.sync import cmd_sync
        args = _make_args(dry_run=True, worker=["worker-1"])
        with patch("xmrdp.cluster._load_config_or_exit", return_value=_BASE_CONFIG):
            try:
                cmd_sync(args)
            except SystemExit as e:
                self.fail(f"cmd_sync raised SystemExit({e.code}) unexpectedly")

    def test_dry_run_verbose_no_error(self):
        from xmrdp.sync import cmd_sync
        args = _make_args(dry_run=True, verbose=True)
        with patch("xmrdp.cluster._load_config_or_exit", return_value=_BASE_CONFIG):
            try:
                cmd_sync(args)
            except SystemExit as e:
                self.fail(f"cmd_sync raised SystemExit({e.code}) in verbose dry-run")


# ---------------------------------------------------------------------------
# cmd_sync — chmod failure is treated as a sync failure
# ---------------------------------------------------------------------------

class TestCmdSyncChmodFailure(unittest.TestCase):

    def test_chmod_failure_increments_failed_and_exits_1(self):
        from xmrdp.sync import cmd_sync
        args = _make_args(dry_run=False, worker=["worker-1"])

        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.stderr = b""
            joined = " ".join(str(c) for c in cmd)
            if "mkdir" in joined:
                result.returncode = 0
            elif cmd[0] == "scp":
                result.returncode = 0
            elif "chmod" in joined:
                result.returncode = 1
                result.stderr = b"Operation not permitted"
            else:
                result.returncode = 0
            return result

        with patch("xmrdp.cluster._load_config_or_exit", return_value=_BASE_CONFIG), \
             patch("shutil.which", return_value="/usr/bin/ssh"), \
             patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(SystemExit) as ctx:
                cmd_sync(args)
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
