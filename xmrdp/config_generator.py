"""Generate runtime configurations for monerod, p2pool, and xmrig."""

import json
import os
import re
import sys
from pathlib import Path

from xmrdp.constants import PORTS
from xmrdp.platforms import get_data_dir, get_log_dir

# Allowlist for extra_args: only --flag or --flag=value style (F-05)
_SAFE_ARG_RE = re.compile(r'^--[a-zA-Z0-9][a-zA-Z0-9\-_.:/=,]*$')


def _validate_extra_args(args, context):
    """Raise ValueError if any extra_arg does not match the safe allowlist."""
    validated = []
    for arg in args:
        s = str(arg)
        if not _SAFE_ARG_RE.match(s):
            raise ValueError(
                f"Unsafe extra_arg in [{context}] rejected: {s!r}. "
                "Only --flag or --flag=value style arguments are allowed."
            )
        validated.append(s)
    return validated


def generate_monerod_args(config):
    """Build CLI argument list for monerod from the cluster config.

    Returns a list of strings ready to pass to subprocess.
    """
    data_dir = get_data_dir()
    log_dir = get_log_dir()
    monerod_cfg = config.get("master", {}).get("monerod", {})

    args = [
        "--data-dir", str(data_dir / "monerod"),
        "--log-file", str(log_dir / "monerod.log"),
        "--zmq-pub", f"tcp://127.0.0.1:{PORTS['monerod_zmq']}",
        "--rpc-bind-ip", "0.0.0.0",
        "--rpc-bind-port", str(PORTS["monerod_rpc"]),
        "--confirm-external-bind",
        "--restricted-rpc",
    ]

    if monerod_cfg.get("prune", True):
        args.append("--prune-blockchain")

    for extra in _validate_extra_args(monerod_cfg.get("extra_args", []), "master.monerod"):
        args.append(extra)

    args.append("--non-interactive")

    return args


def generate_p2pool_args(config):
    """Build CLI argument list for p2pool from the cluster config.

    Returns a list of strings ready to pass to subprocess.
    """
    data_dir = get_data_dir()
    wallet = config.get("cluster", {}).get("wallet", "")
    p2pool_cfg = config.get("master", {}).get("p2pool", {})

    args = [
        "--host", "127.0.0.1",
        "--rpc-port", str(PORTS["monerod_rpc"]),
        "--zmq-port", str(PORTS["monerod_zmq"]),
        "--wallet", wallet,
        "--stratum", f"0.0.0.0:{PORTS['p2pool_stratum']}",
        "--p2p", f"0.0.0.0:{PORTS['p2pool_p2p']}",
        "--data-api", str(data_dir / "p2pool"),
    ]

    if p2pool_cfg.get("mini", True):
        args.append("--mini")

    for extra in _validate_extra_args(p2pool_cfg.get("extra_args", []), "master.p2pool"):
        args.append(extra)

    return args


def generate_xmrig_config(config, role="master"):
    """Build an xmrig JSON config dict.

    Parameters
    ----------
    config : dict
        The loaded cluster config.
    role : str
        Either "master" or "worker". Determines pool URL and password.

    Returns
    -------
    dict
        A configuration dict suitable for writing as xmrig's JSON config.
    """
    log_dir = get_log_dir()
    wallet = config.get("cluster", {}).get("wallet", "")
    xmrig_cfg = config.get("master", {}).get("xmrig", {})

    if role == "master":
        pool_url = "127.0.0.1:3333"
        pool_pass = "master"
    else:
        master_host = config.get("master", {}).get("host", "127.0.0.1")
        pool_url = f"{master_host}:3333"
        # Use worker name from config if available, fall back to "worker"
        pool_pass = "worker"

    threads_hint = xmrig_cfg.get("threads", 0)
    if threads_hint == 0:
        threads_hint = 100

    http_token = xmrig_cfg.get("http_token", "") or None

    xmrig_json = {
        "autosave": False,
        "background": False,
        "colors": True,
        "donate-level": 0,
        "log-file": str(log_dir / "xmrig.log"),
        "pools": [
            {
                "url": pool_url,
                "user": wallet,
                "pass": pool_pass,
                "keepalive": True,
                "tls": False,
            }
        ],
        "cpu": {
            "enabled": True,
            "max-threads-hint": threads_hint,
        },
        "http": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "access-token": http_token,
            "restricted": True,
        },
    }

    return xmrig_json


def write_xmrig_config(config, role="master"):
    """Generate xmrig config and write it to the data directory.

    Returns the path to the written JSON config file.
    """
    data_dir = get_data_dir()
    xmrig_json = generate_xmrig_config(config, role=role)
    config_path = data_dir / "xmrig_config.json"
    content = json.dumps(xmrig_json, indent=2)
    if sys.platform != "win32":
        # Create with mode 0o600 atomically — no world-readable race window (F-13).
        fd = os.open(
            config_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
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
        config_path.write_text(content, encoding="utf-8")
    return config_path
