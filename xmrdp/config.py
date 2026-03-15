"""Configuration loading, validation, and generation."""

import re
import sys
from pathlib import Path

from xmrdp.constants import CONFIG_FILENAME, PORTS
from xmrdp.platforms import get_config_dir


def _load_toml(path):
    """Load a TOML file, using tomllib (3.11+) or tomli fallback."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib
        except ImportError:
            raise RuntimeError(
                "Python < 3.11 requires the 'tomli' package. "
                "Install it with: pip install tomli"
            )
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(path=None):
    """Load and return the cluster config dict.

    Search order when path is None:
      1. ./cluster.toml
      2. <config_dir>/cluster.toml
    """
    if path:
        p = Path(path)
    else:
        local = Path.cwd() / CONFIG_FILENAME
        if local.exists():
            p = local
        else:
            p = get_config_dir() / CONFIG_FILENAME

    if not p.exists():
        raise FileNotFoundError(
            f"Config not found: {p}\n"
            f"Run 'xmrdp setup' to generate one or copy configs/cluster.example.toml"
        )

    config = _load_toml(p)
    _apply_defaults(config)
    return config


def _apply_defaults(config):
    """Fill in missing optional fields with defaults."""
    config.setdefault("cluster", {})
    config["cluster"].setdefault("name", "xmrdp-cluster")

    config.setdefault("master", {})
    config["master"].setdefault("host", "127.0.0.1")
    config["master"].setdefault("api_port", PORTS["c2_api"])
    config["master"].setdefault("api_token", "")

    config["master"].setdefault("monerod", {})
    config["master"]["monerod"].setdefault("prune", True)
    config["master"]["monerod"].setdefault("extra_args", [])

    config["master"].setdefault("p2pool", {})
    config["master"]["p2pool"].setdefault("mini", True)
    config["master"]["p2pool"].setdefault("extra_args", [])

    config["master"].setdefault("xmrig", {})
    config["master"]["xmrig"].setdefault("threads", 0)

    config.setdefault("workers", [])

    config.setdefault("binaries", {})
    config["binaries"].setdefault("monero_version", "latest")
    config["binaries"].setdefault("p2pool_version", "latest")
    config["binaries"].setdefault("xmrig_version", "latest")

    config.setdefault("security", {})
    config["security"].setdefault("verify_checksums", True)


def validate_wallet(address):
    """Validate a Monero wallet address (basic format check).

    Standard addresses start with 4 and are 95 chars.
    Subaddresses start with 8 and are 95 chars.
    Integrated addresses start with 4 and are 106 chars.
    """
    if not address:
        return False, "Wallet address is empty"

    if not re.match(r'^[48][1-9A-HJ-NP-Za-km-z]{94}$', address):
        if not re.match(r'^4[1-9A-HJ-NP-Za-km-z]{105}$', address):
            return False, (
                "Invalid Monero address format. "
                "Must be a 95-char standard/subaddress (starts with 4 or 8) "
                "or 106-char integrated address (starts with 4)."
            )

    return True, "Valid"


def generate_default_config(wallet="", master_host="127.0.0.1", workers=None):
    """Generate a default cluster.toml content string."""
    workers = workers or []

    lines = [
        '[cluster]',
        'name = "xmrdp-cluster"',
        f'wallet = "{wallet}"',
        '',
        '[master]',
        f'host = "{master_host}"',
        f'api_port = {PORTS["c2_api"]}',
        'api_token = ""  # Auto-generated on first setup',
        '',
        '[master.monerod]',
        'prune = true',
        'extra_args = []',
        '',
        '[master.p2pool]',
        'mini = true',
        'extra_args = []',
        '',
        '[master.xmrig]',
        'threads = 0  # 0 = auto-detect',
        '',
    ]

    for i, w in enumerate(workers, 1):
        if isinstance(w, dict):
            name = w.get("name", f"worker-{i}")
            host = w.get("host", f"192.168.1.{100 + i}")
        else:
            name = f"worker-{i}"
            host = w
        lines.append("[[workers]]")
        lines.append(f'name = "{name}"')
        lines.append(f'host = "{host}"')
        lines.append("")

    lines.extend([
        '[binaries]',
        'monero_version = "latest"',
        'p2pool_version = "latest"',
        'xmrig_version = "latest"',
        '',
        '[security]',
        'verify_checksums = true',
        '',
    ])

    return "\n".join(lines)
