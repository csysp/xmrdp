"""Unit tests for XMRDP core logic.

Tests only pure functions — no network calls, no filesystem side effects.
Uses only ``unittest`` from the standard library.
"""

import unittest


class TestWalletValidation(unittest.TestCase):
    """Tests for xmrdp.config.validate_wallet."""

    def setUp(self):
        from xmrdp.config import validate_wallet
        self.validate = validate_wallet

    def test_valid_standard_address(self):
        """Standard address: starts with 4, 95 base58 characters."""
        # 4 + 94 valid base58 chars = 95 total
        addr = "4" + "1" * 94
        ok, msg = self.validate(addr)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid")

    def test_valid_subaddress(self):
        """Subaddress: starts with 8, 95 base58 characters."""
        addr = "8" + "A" * 94
        ok, msg = self.validate(addr)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid")

    def test_too_short(self):
        """Address shorter than 95 chars should fail."""
        addr = "4" + "1" * 50
        ok, msg = self.validate(addr)
        self.assertFalse(ok)
        self.assertIn("Invalid", msg)

    def test_too_long(self):
        """Address longer than 106 chars should fail."""
        addr = "4" + "1" * 110
        ok, msg = self.validate(addr)
        self.assertFalse(ok)
        self.assertIn("Invalid", msg)

    def test_wrong_prefix(self):
        """Address not starting with 4 or 8 should fail."""
        addr = "5" + "1" * 94
        ok, msg = self.validate(addr)
        self.assertFalse(ok)
        self.assertIn("Invalid", msg)

    def test_empty_string(self):
        """Empty string should fail with specific message."""
        ok, msg = self.validate("")
        self.assertFalse(ok)
        self.assertIn("empty", msg.lower())

    def test_integrated_address(self):
        """Integrated address: starts with 4, 106 base58 characters."""
        addr = "4" + "B" * 105
        ok, msg = self.validate(addr)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid")


class TestConfigGeneration(unittest.TestCase):
    """Tests for xmrdp.config.generate_default_config."""

    def setUp(self):
        from xmrdp.config import generate_default_config
        self.generate = generate_default_config

    def test_contains_required_sections(self):
        """Generated config must contain all required TOML sections."""
        output = self.generate(wallet="4" + "1" * 94)
        for section in ("[cluster]", "[master]", "[binaries]", "[security]"):
            self.assertIn(section, output, f"Missing section: {section}")

    def test_wallet_included(self):
        """Wallet address must appear in the generated config."""
        wallet = "4" + "A" * 94
        output = self.generate(wallet=wallet)
        self.assertIn(wallet, output)

    def test_zero_workers(self):
        """Config with no workers should not contain [[workers]]."""
        output = self.generate(wallet="4" + "1" * 94, workers=[])
        self.assertNotIn("[[workers]]", output)

    def test_one_worker(self):
        """Config with one worker should have exactly one [[workers]] block."""
        output = self.generate(
            wallet="4" + "1" * 94,
            workers=["192.168.1.101"],
        )
        self.assertEqual(output.count("[[workers]]"), 1)
        self.assertIn("192.168.1.101", output)

    def test_five_workers(self):
        """Config with five workers should have five [[workers]] blocks."""
        hosts = [f"192.168.1.{100 + i}" for i in range(1, 6)]
        output = self.generate(wallet="4" + "1" * 94, workers=hosts)
        self.assertEqual(output.count("[[workers]]"), 5)
        for host in hosts:
            self.assertIn(host, output)

    def test_worker_dict_format(self):
        """Workers can be passed as dicts with name and host."""
        output = self.generate(
            wallet="4" + "1" * 94,
            workers=[{"name": "rig-01", "host": "10.0.0.5"}],
        )
        self.assertIn("rig-01", output)
        self.assertIn("10.0.0.5", output)


class TestPlatformDetection(unittest.TestCase):
    """Tests for xmrdp.platforms.detect_platform."""

    def setUp(self):
        from xmrdp.platforms import detect_platform
        self.detect = detect_platform

    def test_returns_tuple(self):
        """detect_platform should return a 2-tuple."""
        result = self.detect()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_valid_system(self):
        """System should be one of the supported values."""
        system, _ = self.detect()
        self.assertIn(system, ("linux", "windows", "darwin"))

    def test_valid_machine(self):
        """Machine architecture should be normalized."""
        _, machine = self.detect()
        self.assertIn(machine, ("x86_64", "aarch64"))


class TestConfigGenerator(unittest.TestCase):
    """Tests for xmrdp.config_generator argument/config builders."""

    def setUp(self):
        from xmrdp.config_generator import (
            generate_monerod_args,
            generate_p2pool_args,
            generate_xmrig_config,
        )
        self.gen_monerod = generate_monerod_args
        self.gen_p2pool = generate_p2pool_args
        self.gen_xmrig = generate_xmrig_config

        # Minimal valid config that satisfies all generators.
        self.base_config = {
            "cluster": {"wallet": "4" + "1" * 94},
            "master": {
                "host": "192.168.1.10",
                "monerod": {"prune": True, "extra_args": []},
                "p2pool": {"mini": True, "extra_args": []},
                "xmrig": {"threads": 0},
            },
        }

    def test_monerod_prune_enabled(self):
        """monerod args should include --prune-blockchain when prune=True."""
        args = self.gen_monerod(self.base_config)
        self.assertIn("--prune-blockchain", args)

    def test_monerod_prune_disabled(self):
        """monerod args should omit --prune-blockchain when prune=False."""
        cfg = {
            "cluster": self.base_config["cluster"],
            "master": {
                **self.base_config["master"],
                "monerod": {"prune": False, "extra_args": []},
            },
        }
        args = self.gen_monerod(cfg)
        self.assertNotIn("--prune-blockchain", args)

    def test_p2pool_mini_enabled(self):
        """p2pool args should include --mini when mini=True."""
        args = self.gen_p2pool(self.base_config)
        self.assertIn("--mini", args)

    def test_p2pool_mini_disabled(self):
        """p2pool args should omit --mini when mini=False."""
        cfg = {
            "cluster": self.base_config["cluster"],
            "master": {
                **self.base_config["master"],
                "p2pool": {"mini": False, "extra_args": []},
            },
        }
        args = self.gen_p2pool(cfg)
        self.assertNotIn("--mini", args)

    def test_p2pool_includes_wallet(self):
        """p2pool args should contain the wallet address."""
        args = self.gen_p2pool(self.base_config)
        wallet = self.base_config["cluster"]["wallet"]
        self.assertIn(wallet, args)

    def test_xmrig_master_pool(self):
        """xmrig config for master should use 127.0.0.1:3333."""
        cfg = self.gen_xmrig(self.base_config, role="master")
        pool_url = cfg["pools"][0]["url"]
        self.assertEqual(pool_url, "127.0.0.1:3333")

    def test_xmrig_worker_pool(self):
        """xmrig config for worker should use master_host:3333."""
        cfg = self.gen_xmrig(self.base_config, role="worker")
        pool_url = cfg["pools"][0]["url"]
        self.assertEqual(pool_url, "192.168.1.10:3333")

    def test_xmrig_has_cpu_section(self):
        """xmrig config should have a cpu configuration section."""
        cfg = self.gen_xmrig(self.base_config, role="master")
        self.assertIn("cpu", cfg)
        self.assertTrue(cfg["cpu"]["enabled"])

    def test_monerod_returns_list(self):
        """monerod args should be a list of strings."""
        args = self.gen_monerod(self.base_config)
        self.assertIsInstance(args, list)
        for arg in args:
            self.assertIsInstance(arg, str)


class TestFirewallRules(unittest.TestCase):
    """Tests for xmrdp.firewall rule generation and formatting."""

    def setUp(self):
        from xmrdp.firewall import (
            format_iptables,
            format_netsh,
            format_pf,
            format_ufw,
            generate_rules,
        )
        self.generate_rules = generate_rules
        self.format_ufw = format_ufw
        self.format_iptables = format_iptables
        self.format_netsh = format_netsh
        self.format_pf = format_pf

        self.config = {
            "master": {"host": "192.168.1.10"},
            "workers": [
                {"name": "w1", "host": "192.168.1.20"},
                {"name": "w2", "host": "192.168.1.21"},
            ],
        }

    def test_master_has_more_rules_than_worker(self):
        """Master should have significantly more rules than a worker."""
        master_rules = self.generate_rules("master", self.config)
        worker_rules = self.generate_rules("worker", self.config)
        self.assertGreater(len(master_rules), len(worker_rules))

    def test_rules_have_required_keys(self):
        """Every rule dict must have direction, port, proto, description."""
        required = {"direction", "port", "proto", "description"}
        for role in ("master", "worker"):
            rules = self.generate_rules(role, self.config)
            for rule in rules:
                for key in required:
                    self.assertIn(
                        key, rule,
                        f"Missing key '{key}' in {role} rule: {rule}",
                    )

    def test_master_rules_include_key_ports(self):
        """Master rules should cover monerod P2P, stratum, and C2 ports."""
        rules = self.generate_rules("master", self.config)
        ports = {r["port"] for r in rules}
        self.assertIn(18080, ports)  # monerod P2P
        self.assertIn(3333, ports)   # p2pool stratum
        self.assertIn(7099, ports)   # C2 API

    def test_worker_rules_are_outbound(self):
        """All worker rules should be outbound."""
        rules = self.generate_rules("worker", self.config)
        for rule in rules:
            self.assertEqual(rule["direction"], "out")

    def test_format_ufw_nonempty(self):
        """format_ufw should produce non-empty output."""
        rules = self.generate_rules("master", self.config)
        output = self.format_ufw(rules)
        self.assertTrue(len(output) > 0)
        self.assertIn("ufw", output)

    def test_format_iptables_nonempty(self):
        """format_iptables should produce non-empty output."""
        rules = self.generate_rules("master", self.config)
        output = self.format_iptables(rules)
        self.assertTrue(len(output) > 0)
        self.assertIn("iptables", output)

    def test_format_netsh_nonempty(self):
        """format_netsh should produce non-empty output."""
        rules = self.generate_rules("master", self.config)
        output = self.format_netsh(rules)
        self.assertTrue(len(output) > 0)
        self.assertIn("netsh", output)

    def test_format_pf_nonempty(self):
        """format_pf should produce non-empty output."""
        rules = self.generate_rules("master", self.config)
        output = self.format_pf(rules)
        self.assertTrue(len(output) > 0)
        self.assertIn("pass", output)

    def test_c2_restricted_to_workers(self):
        """C2 API rules should be restricted to worker IPs when workers exist."""
        rules = self.generate_rules("master", self.config)
        c2_rules = [r for r in rules if r["port"] == 7099]
        # Should have one rule per worker, not a blanket LAN rule.
        self.assertEqual(len(c2_rules), 2)
        sources = {r.get("source") for r in c2_rules}
        self.assertIn("192.168.1.20", sources)
        self.assertIn("192.168.1.21", sources)


class TestBinaryManager(unittest.TestCase):
    """Tests for xmrdp.binary_manager.match_asset (pure logic, no network)."""

    def setUp(self):
        from xmrdp.binary_manager import match_asset
        self.match_asset = match_asset

        # Fabricated asset lists that mirror real GitHub release structures.
        self.monero_assets = [
            {"name": "monero-linux-x64-v0.18.3.4.tar.bz2",
             "browser_download_url": "https://example.com/monero-linux.tar.bz2",
             "size": 100_000_000},
            {"name": "monero-linux-armv8-v0.18.3.4.tar.bz2",
             "browser_download_url": "https://example.com/monero-arm.tar.bz2",
             "size": 95_000_000},
            {"name": "monero-win-x64-v0.18.3.4.zip",
             "browser_download_url": "https://example.com/monero-win.zip",
             "size": 110_000_000},
            {"name": "monero-mac-x64-v0.18.3.4.tar.bz2",
             "browser_download_url": "https://example.com/monero-mac.tar.bz2",
             "size": 105_000_000},
            {"name": "hashes.txt",
             "browser_download_url": "https://example.com/hashes.txt",
             "size": 1024},
        ]

        self.p2pool_assets = [
            {"name": "p2pool-v4.1-linux-x64.tar.gz",
             "browser_download_url": "https://example.com/p2pool-linux.tar.gz",
             "size": 10_000_000},
            {"name": "p2pool-v4.1-windows-x64.zip",
             "browser_download_url": "https://example.com/p2pool-win.zip",
             "size": 12_000_000},
            {"name": "p2pool-v4.1-linux-aarch64.tar.gz",
             "browser_download_url": "https://example.com/p2pool-arm.tar.gz",
             "size": 9_500_000},
            {"name": "sha256sums.txt",
             "browser_download_url": "https://example.com/sha256sums.txt",
             "size": 512},
        ]

        self.xmrig_assets = [
            {"name": "xmrig-6.21.0-linux-x64.tar.gz",
             "browser_download_url": "https://example.com/xmrig-linux.tar.gz",
             "size": 5_000_000},
            {"name": "xmrig-6.21.0-msvc-win64.zip",
             "browser_download_url": "https://example.com/xmrig-win.zip",
             "size": 6_000_000},
            {"name": "xmrig-6.21.0-macos-arm64.tar.gz",
             "browser_download_url": "https://example.com/xmrig-mac-arm.tar.gz",
             "size": 4_500_000},
            {"name": "SHA256SUMS",
             "browser_download_url": "https://example.com/SHA256SUMS",
             "size": 256},
        ]

    def test_monero_linux_x86_64(self):
        """Should match the linux-x64 monero archive."""
        result = self.match_asset(self.monero_assets, "monero", "linux", "x86_64")
        self.assertIsNotNone(result)
        self.assertIn("linux-x64", result["name"])

    def test_monero_linux_aarch64(self):
        """Should match the linux-armv8 monero archive."""
        result = self.match_asset(self.monero_assets, "monero", "linux", "aarch64")
        self.assertIsNotNone(result)
        self.assertIn("armv8", result["name"])

    def test_monero_windows_x86_64(self):
        """Should match the Windows monero archive."""
        result = self.match_asset(self.monero_assets, "monero", "windows", "x86_64")
        self.assertIsNotNone(result)
        self.assertIn("win-x64", result["name"])

    def test_p2pool_linux_x86_64(self):
        """Should match the linux-x64 p2pool archive."""
        result = self.match_asset(self.p2pool_assets, "p2pool", "linux", "x86_64")
        self.assertIsNotNone(result)
        self.assertIn("linux-x64", result["name"])

    def test_xmrig_windows_x86_64(self):
        """Should match the msvc-win64 xmrig archive."""
        result = self.match_asset(self.xmrig_assets, "xmrig", "windows", "x86_64")
        self.assertIsNotNone(result)
        self.assertIn("msvc-win64", result["name"])

    def test_xmrig_macos_aarch64(self):
        """Should match the macos-arm64 xmrig archive."""
        result = self.match_asset(self.xmrig_assets, "xmrig", "darwin", "aarch64")
        self.assertIsNotNone(result)
        self.assertIn("macos-arm64", result["name"])

    def test_unsupported_platform_returns_none(self):
        """Should return None for an unsupported platform combination."""
        result = self.match_asset(self.monero_assets, "monero", "freebsd", "x86_64")
        self.assertIsNone(result)

    def test_unsupported_software_returns_none(self):
        """Should return None for unknown software name."""
        result = self.match_asset(self.monero_assets, "unknown_miner", "linux", "x86_64")
        self.assertIsNone(result)

    def test_empty_asset_list(self):
        """Should return None when asset list is empty."""
        result = self.match_asset([], "monero", "linux", "x86_64")
        self.assertIsNone(result)

    def test_matched_asset_has_download_url(self):
        """Matched asset should contain a browser_download_url."""
        result = self.match_asset(self.p2pool_assets, "p2pool", "linux", "aarch64")
        self.assertIsNotNone(result)
        self.assertIn("browser_download_url", result)
        self.assertTrue(result["browser_download_url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
