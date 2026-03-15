"""Firewall rule generation for XMRDP.

Generates firewall rules appropriate to the node role (master or worker)
and formats them for ufw, iptables, Windows netsh, and macOS pf.

IMPORTANT: This module NEVER applies rules automatically.  It only prints
them for the operator to review and apply manually.
"""

import sys

from xmrdp.config import load_config
from xmrdp.constants import PORTS
from xmrdp.platforms import detect_platform


def generate_rules(role, config):
    """Build a list of firewall rule dicts for the given role.

    Parameters
    ----------
    role : str
        ``"master"`` or ``"worker"``.
    config : dict
        Loaded cluster configuration.

    Returns
    -------
    list[dict]
        Each dict has keys: ``direction``, ``port``, ``proto``,
        ``description``, and optionally ``source`` (CIDR or keyword).
    """
    if role == "master":
        return _master_rules(config)
    return _worker_rules(config)


def _master_rules(config):
    """Generate inbound rules for a master node."""
    master_host = config.get("master", {}).get("host", "127.0.0.1")

    # Collect worker IPs for restricting C2 API access.
    worker_ips = []
    for w in config.get("workers", []):
        host = w.get("host")
        if host:
            worker_ips.append(host)

    rules = [
        {
            "direction": "in",
            "port": PORTS["monerod_p2p"],
            "proto": "tcp",
            "description": "monerod P2P (blockchain sync)",
        },
        {
            "direction": "in",
            "port": PORTS["monerod_rpc"],
            "proto": "tcp",
            "source": "192.168.0.0/16",
            "description": "monerod RPC (restricted to LAN)",
        },
        {
            "direction": "in",
            "port": PORTS["monerod_zmq"],
            "proto": "tcp",
            "source": "127.0.0.1",
            "description": "monerod ZMQ (localhost only)",
        },
        {
            "direction": "in",
            "port": PORTS["p2pool_stratum"],
            "proto": "tcp",
            "description": "p2pool stratum (miners connect here)",
        },
        {
            "direction": "in",
            "port": PORTS["p2pool_p2p"],
            "proto": "tcp",
            "description": "p2pool P2P (sidechain sync)",
        },
    ]

    # C2 API — restrict to worker IPs when known, otherwise LAN.
    if worker_ips:
        for ip in worker_ips:
            rules.append({
                "direction": "in",
                "port": PORTS["c2_api"],
                "proto": "tcp",
                "source": ip,
                "description": f"C2 API (worker {ip})",
            })
    else:
        rules.append({
            "direction": "in",
            "port": PORTS["c2_api"],
            "proto": "tcp",
            "source": "192.168.0.0/16",
            "description": "C2 API (LAN only)",
        })

    return rules


def _worker_rules(config):
    """Generate outbound rules for a worker node."""
    master_host = config.get("master", {}).get("host", "127.0.0.1")

    return [
        {
            "direction": "out",
            "port": PORTS["p2pool_stratum"],
            "proto": "tcp",
            "source": master_host,
            "description": f"Stratum to master ({master_host}:3333)",
        },
        {
            "direction": "out",
            "port": PORTS["c2_api"],
            "proto": "tcp",
            "source": master_host,
            "description": f"C2 API to master ({master_host}:7099)",
        },
    ]


# ---------------------------------------------------------------------------
# Formatters — each returns a multi-line string of commands/config.
# ---------------------------------------------------------------------------


def format_ufw(rules):
    """Format rules as ufw commands.

    Returns
    -------
    str
        Newline-separated ufw allow/deny commands.
    """
    lines = []
    for r in rules:
        direction = r["direction"]
        port = r["port"]
        proto = r["proto"]
        source = r.get("source")
        desc = r.get("description", "")

        comment = f"  # {desc}" if desc else ""

        if direction == "in":
            if source:
                lines.append(
                    f"sudo ufw allow from {source} to any port {port} proto {proto}{comment}"
                )
            else:
                lines.append(
                    f"sudo ufw allow {port}/{proto}{comment}"
                )
        else:
            # Outbound — ufw handles outbound via 'allow out'
            if source:
                lines.append(
                    f"sudo ufw allow out to {source} port {port} proto {proto}{comment}"
                )
            else:
                lines.append(
                    f"sudo ufw allow out {port}/{proto}{comment}"
                )

    return "\n".join(lines)


def format_iptables(rules):
    """Format rules as iptables commands.

    Returns
    -------
    str
        Newline-separated iptables commands.
    """
    lines = []
    for r in rules:
        direction = r["direction"]
        port = r["port"]
        proto = r["proto"]
        source = r.get("source")
        desc = r.get("description", "")

        comment = f"  # {desc}" if desc else ""
        chain = "INPUT" if direction == "in" else "OUTPUT"
        port_flag = "--dport" if direction == "in" else "--dport"

        cmd = f"sudo iptables -A {chain} -p {proto} {port_flag} {port}"
        if source and direction == "in":
            cmd += f" -s {source}"
        elif source and direction == "out":
            cmd += f" -d {source}"
        cmd += f" -j ACCEPT{comment}"
        lines.append(cmd)

    return "\n".join(lines)


def format_netsh(rules):
    """Format rules as Windows netsh advfirewall commands.

    Returns
    -------
    str
        Newline-separated netsh commands.
    """
    lines = []
    for r in rules:
        direction = r["direction"]
        port = r["port"]
        proto = r["proto"]
        source = r.get("source")
        desc = r.get("description", "")

        direction_kw = "dir=in" if direction == "in" else "dir=out"
        rule_name = f"XMRDP - {desc}" if desc else f"XMRDP - {proto}/{port}"

        cmd = (
            f'netsh advfirewall firewall add rule '
            f'name="{rule_name}" '
            f'{direction_kw} action=allow '
            f'protocol={proto} localport={port}'
        )
        if source:
            if direction == "in":
                cmd += f" remoteip={source}"
            else:
                cmd += f" remoteip={source}"

        lines.append(cmd)

    return "\n".join(lines)


def format_pf(rules):
    """Format rules as macOS/BSD pf.conf rules.

    Returns
    -------
    str
        Newline-separated pf rules suitable for inclusion in ``pf.conf``.
    """
    lines = []
    for r in rules:
        direction = r["direction"]
        port = r["port"]
        proto = r["proto"]
        source = r.get("source")
        desc = r.get("description", "")

        comment = f"  # {desc}" if desc else ""

        if direction == "in":
            if source:
                lines.append(
                    f"pass in on egress proto {proto} from {source} to any port {port}{comment}"
                )
            else:
                lines.append(
                    f"pass in on egress proto {proto} from any to any port {port}{comment}"
                )
        else:
            if source:
                lines.append(
                    f"pass out on egress proto {proto} from any to {source} port {port}{comment}"
                )
            else:
                lines.append(
                    f"pass out on egress proto {proto} from any to any port {port}{comment}"
                )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI handler
# ---------------------------------------------------------------------------

_FORMAT_MAP = {
    "linux": ("ufw / iptables", format_ufw, format_iptables),
    "windows": ("netsh advfirewall", format_netsh, None),
    "darwin": ("pf.conf", format_pf, None),
}


def cmd_firewall(args):
    """CLI handler for ``xmrdp firewall <role>``."""
    config_path = getattr(args, "config", None)
    role = args.role

    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    system, _ = detect_platform()
    rules = generate_rules(role, config)

    print(f"Firewall rules for role: {role}")
    print(f"Platform detected: {system}")
    print(f"Rules generated: {len(rules)}")
    print()

    # --- Primary format for the detected OS ---
    if system == "linux":
        print("=== UFW Commands ===")
        print(format_ufw(rules))
        print()
        print("=== iptables Commands ===")
        print(format_iptables(rules))
    elif system == "windows":
        print("=== netsh advfirewall Commands ===")
        print(format_netsh(rules))
    elif system == "darwin":
        print("=== pf.conf Rules ===")
        print(format_pf(rules))
    else:
        print("=== UFW Commands ===")
        print(format_ufw(rules))

    # --- All other formats (commented) ---
    print()
    print("# --- Other platforms (for reference) ---")

    all_formatters = [
        ("UFW", format_ufw),
        ("iptables", format_iptables),
        ("netsh advfirewall", format_netsh),
        ("pf.conf", format_pf),
    ]

    # Skip formatters already printed above.
    already_shown = set()
    if system == "linux":
        already_shown = {"UFW", "iptables"}
    elif system == "windows":
        already_shown = {"netsh advfirewall"}
    elif system == "darwin":
        already_shown = {"pf.conf"}

    for name, formatter in all_formatters:
        if name in already_shown:
            continue
        print(f"\n# === {name} ===")
        for line in formatter(rules).splitlines():
            print(f"# {line}")

    print()
    print(
        "WARNING: Review these rules before applying. "
        "XMRDP does not apply them automatically."
    )
