"""Generate runtime configurations for monerod, p2pool, and xmrig."""

import json
from pathlib import Path

from xmrdp.platforms import get_data_dir, get_log_dir


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
        "--zmq-pub", "tcp://0.0.0.0:18083",
        "--rpc-bind-ip", "0.0.0.0",
        "--rpc-bind-port", "18081",
        "--confirm-external-bind",
        "--restricted-rpc",
    ]

    if monerod_cfg.get("prune", True):
        args.append("--prune-blockchain")

    for extra in monerod_cfg.get("extra_args", []):
        args.append(str(extra))

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
        "--rpc-port", "18081",
        "--zmq-port", "18083",
        "--wallet", wallet,
        "--stratum", "0.0.0.0:3333",
        "--p2p", "0.0.0.0:37888",
        "--data-api", str(data_dir / "p2pool"),
    ]

    if p2pool_cfg.get("mini", True):
        args.append("--mini")

    for extra in p2pool_cfg.get("extra_args", []):
        args.append(str(extra))

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

    xmrig_json = {
        "autosave": False,
        "background": False,
        "colors": True,
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
    }

    return xmrig_json


def write_xmrig_config(config, role="master"):
    """Generate xmrig config and write it to the data directory.

    Returns the path to the written JSON config file.
    """
    data_dir = get_data_dir()
    xmrig_json = generate_xmrig_config(config, role=role)
    config_path = data_dir / "xmrig_config.json"
    config_path.write_text(json.dumps(xmrig_json, indent=2), encoding="utf-8")
    return config_path
