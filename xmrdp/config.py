"""Configuration loading, validation, and generation."""

import re
import sys
from pathlib import Path

from xmrdp.constants import CONFIG_FILENAME, PORTS
from xmrdp.platforms import get_config_dir

# Allowlist for host values: IPv4, bracketed IPv6, or a valid hostname (NF-03)
_HOST_RE = re.compile(
    r'^('
    r'(?:\d{1,3}\.){3}\d{1,3}'   # IPv4
    r'|\[[\da-fA-F:]+\]'          # bracketed IPv6
    r'|[a-zA-Z0-9][a-zA-Z0-9._-]*'  # hostname
    r')$'
)

_TOML_SENSITIVE_CHARS = str.maketrans({"\\": "\\\\", '"': '\\"'})

# Allowlist for extra_args validated at config load time (mirrors config_generator._SAFE_ARG_RE)
_SAFE_ARG_RE = re.compile(r'^--[a-zA-Z0-9][a-zA-Z0-9\-_.:/=,]*$')


def _validate_extra_args(args, context):
    """Raise ValueError if any extra_arg does not match the safe allowlist."""
    for arg in args:
        s = str(arg)
        if not _SAFE_ARG_RE.match(s):
            raise ValueError(
                f"Unsafe extra_arg in [{context}] rejected: {s!r}. "
                "Only --flag or --flag=value style arguments are allowed."
            )


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

    # Validate master.host to prevent SSRF via crafted config values (NF-03).
    host = config["master"]["host"]
    if not _HOST_RE.match(str(host)):
        raise ValueError(
            f"Invalid master.host value: {host!r}. "
            "Use an IPv4 address, bracketed IPv6, or a valid hostname."
        )

    # bind_host controls what address the C2 server listens on.
    # Defaults to master.host so single-interface setups need no extra config.
    # Set to 0.0.0.0 to listen on all interfaces (e.g. multi-homed master).
    config["master"].setdefault("bind_host", config["master"]["host"])
    bind_host = config["master"]["bind_host"]
    if not _HOST_RE.match(str(bind_host)):
        raise ValueError(
            f"Invalid master.bind_host value: {bind_host!r}. "
            "Use an IPv4 address, bracketed IPv6, a valid hostname, or 0.0.0.0."
        )

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
    config["master"]["xmrig"].setdefault("http_token", "")

    # Validate extra_args at config load time so bad values are caught early (F-05).
    _validate_extra_args(
        config["master"]["monerod"].get("extra_args", []),
        "master.monerod",
    )
    _validate_extra_args(
        config["master"]["p2pool"].get("extra_args", []),
        "master.p2pool",
    )

    config.setdefault("workers", [])

    # Validate worker host values to prevent SSRF via crafted config (NF-03).
    for i, worker in enumerate(config["workers"]):
        if not isinstance(worker, dict):
            continue
        w_host = worker.get("host", "")
        if w_host and not _HOST_RE.match(str(w_host)):
            raise ValueError(
                f"Invalid host value in workers[{i}]: {w_host!r}. "
                "Use an IPv4 address, bracketed IPv6, or a valid hostname."
            )

    config.setdefault("binaries", {})
    config["binaries"].setdefault("monero_version", "latest")
    config["binaries"].setdefault("p2pool_version", "latest")
    config["binaries"].setdefault("xmrig_version", "latest")

    config.setdefault("security", {})
    config["security"].setdefault("verify_checksums", True)
    config["security"].setdefault("tls_enabled", False)
    config["security"].setdefault("c2_tls_cert", "")
    config["security"].setdefault("c2_tls_key", "")
    config["security"].setdefault("c2_tls_fingerprint", "")


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


def _toml_str(s: str) -> str:
    """Escape a string for safe embedding in a TOML double-quoted value (NF-01)."""
    return s.translate(_TOML_SENSITIVE_CHARS)


def generate_default_config(wallet="", master_host="127.0.0.1", workers=None):
    """Generate a default cluster.toml content string."""
    workers = workers or []

    lines = [
        '[cluster]',
        'name = "xmrdp-cluster"',
        f'wallet = "{_toml_str(wallet)}"',
        '',
        '[master]',
        f'host = "{_toml_str(master_host)}"',
        '# bind_host = "0.0.0.0"  # Optional: override C2 listen address (default = host)',
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
        'http_token = ""  # Auto-generated on first setup',
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
        lines.append(f'name = "{_toml_str(name)}"')
        lines.append(f'host = "{_toml_str(host)}"')
        lines.append("")

    lines.extend([
        '[binaries]',
        'monero_version = "latest"',
        'p2pool_version = "latest"',
        'xmrig_version = "latest"',
        '',
        '[security]',
        'verify_checksums = true',
        'tls_enabled = false',
        'c2_tls_cert = ""',
        'c2_tls_key = ""',
        'c2_tls_fingerprint = ""',
        '',
    ])

    return "\n".join(lines)
