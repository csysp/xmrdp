"""Config sync — push cluster.toml to worker nodes via SSH/SCP.

Generates a per-worker config (identical to master config but with
``self = true`` set for the target worker) and copies it to each worker
machine using scp.  Workers use ``self = true`` to determine their own
identity when ``xmrdp start worker`` runs.

Requires ``ssh`` and ``scp`` on PATH.  Authentication uses the system
SSH agent or default key files (~/.ssh/id_*) — no credentials are
accepted or stored by xmrdp itself.
"""

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

_SSH_USER_RE = re.compile(r'^[a-zA-Z0-9._-]+$')

from xmrdp.constants import PORTS

log = logging.getLogger("xmrdp.sync")

_REMOTE_CONFIG_PATH = "~/.xmrdp/config/cluster.toml"
_REMOTE_CONFIG_DIR = "~/.xmrdp/config"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cmd_sync(args) -> None:
    """Handle ``xmrdp sync``."""
    from xmrdp.cluster import _load_config_or_exit

    config = _load_config_or_exit(args)
    workers = config.get("workers", [])

    if not workers:
        print("No workers defined in cluster.toml. Add [[workers]] entries first.")
        sys.exit(1)

    # Filter to named workers when --worker is given.
    target_names = set(args.worker) if args.worker else None
    targets = []
    for w in workers:
        if not isinstance(w, dict):
            continue
        name = w.get("name", "").strip()
        host = w.get("host", "").strip()
        if not name or not host:
            log.warning("Skipping worker entry missing name or host: %s", w)
            continue
        if target_names and name not in target_names:
            continue
        targets.append({"name": name, "host": host})

    if not targets:
        names = ", ".join(sorted(target_names)) if target_names else "(none defined)"
        print(f"No matching workers found. Requested: {names}")
        sys.exit(1)

    if not args.dry_run:
        for tool in ("ssh", "scp"):
            if not shutil.which(tool):
                print(
                    f"Error: '{tool}' not found on PATH. "
                    "Install OpenSSH or ensure it is in your PATH.",
                    file=sys.stderr,
                )
                sys.exit(1)

    ssh_user = (args.ssh_user or "").strip()
    if ssh_user and not _SSH_USER_RE.match(ssh_user):
        print(
            f"Error: invalid --ssh-user value {ssh_user!r}. "
            "Only alphanumeric characters, dots, hyphens, and underscores are allowed.",
            file=sys.stderr,
        )
        sys.exit(1)
    dry_run = args.dry_run
    restart = args.restart

    success = 0
    failed = 0

    for w in targets:
        name = w["name"]
        host = w["host"]
        remote = f"{ssh_user + '@' if ssh_user else ''}{host}"

        print(f"  [{name}] {remote}", end=" ... ", flush=True)

        if dry_run:
            config_content = _generate_worker_config(config, name)
            print("(dry-run)")
            if getattr(args, "verbose", False):
                print()
                for line in config_content.splitlines():
                    print(f"    {line}")
                print()
            success += 1
            continue

        config_content = _generate_worker_config(config, name)

        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".toml")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(config_content)

            # Ensure remote config directory exists.
            result = subprocess.run(
                ["ssh", remote, f"mkdir -p {shlex.quote(_REMOTE_CONFIG_DIR)}"],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"FAILED (mkdir: {err})")
                failed += 1
                continue

            # Copy config to worker.
            result = subprocess.run(
                ["scp", "-q", tmp_path, f"{remote}:{_REMOTE_CONFIG_PATH}"],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"FAILED (scp: {err})")
                failed += 1
                continue

            # Restrict permissions on the remote config file.
            result = subprocess.run(
                ["ssh", remote, f"chmod 600 {shlex.quote(_REMOTE_CONFIG_PATH)}"],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", errors="replace").strip()
                print(f"FAILED (chmod: {err})")
                failed += 1
                continue

            print("OK")

            if restart:
                print(f"  [{name}] Restarting xmrig", end=" ... ", flush=True)
                result = subprocess.run(
                    ["ssh", remote, "xmrdp stop worker && xmrdp start worker"],
                    capture_output=True, timeout=60,
                )
                if result.returncode == 0:
                    print("OK")
                else:
                    err = result.stderr.decode("utf-8", errors="replace").strip()
                    print(f"WARN ({err})")

            success += 1

        except subprocess.TimeoutExpired:
            print("FAILED (timeout)")
            failed += 1
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    print()
    print(f"  Sync complete: {success} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def _generate_worker_config(config: dict, worker_name: str) -> str:
    """Return a cluster.toml string for *worker_name*.

    Identical to the master config but marks the matching worker entry
    with ``self = true`` so the worker knows its own identity.
    """
    from xmrdp.config import _toml_str

    cluster = config.get("cluster", {})
    master = config.get("master", {})
    security = config.get("security", {})
    binaries = config.get("binaries", {})
    workers = config.get("workers", [])
    monerod = master.get("monerod", {})
    p2pool = master.get("p2pool", {})
    xmrig_cfg = master.get("xmrig", {})

    lines = [
        "[cluster]",
        f'name = "{_toml_str(cluster.get("name", "xmrdp-cluster"))}"',
        f'wallet = "{_toml_str(cluster.get("wallet", ""))}"',
        "",
        "[master]",
        f'host = "{_toml_str(master.get("host", "127.0.0.1"))}"',
        f'api_port = {master.get("api_port", PORTS["c2_api"])}',
        f'api_token = "{_toml_str(master.get("api_token", ""))}"',
        "",
        "[master.monerod]",
        f'prune = {"true" if monerod.get("prune", True) else "false"}',
        f"extra_args = {_toml_list(monerod.get('extra_args', []))}",
        "",
        "[master.p2pool]",
        f'mini = {"true" if p2pool.get("mini", True) else "false"}',
        f"extra_args = {_toml_list(p2pool.get('extra_args', []))}",
        "",
        "[master.xmrig]",
        f'threads = {xmrig_cfg.get("threads", 0)}',
        f'http_token = "{_toml_str(xmrig_cfg.get("http_token", ""))}"',
        "",
    ]

    for w in workers:
        if not isinstance(w, dict):
            continue
        name = w.get("name", "")
        host = w.get("host", "")
        lines.append("[[workers]]")
        lines.append(f'name = "{_toml_str(name)}"')
        lines.append(f'host = "{_toml_str(host)}"')
        if name == worker_name:
            lines.append("self = true")
        lines.append("")

    lines.extend([
        "[binaries]",
        f'monero_version = "{_toml_str(binaries.get("monero_version", "latest"))}"',
        f'p2pool_version = "{_toml_str(binaries.get("p2pool_version", "latest"))}"',
        f'xmrig_version = "{_toml_str(binaries.get("xmrig_version", "latest"))}"',
        "",
        "[security]",
        f'verify_checksums = {"true" if security.get("verify_checksums", True) else "false"}',
        f'tls_enabled = {"true" if security.get("tls_enabled", False) else "false"}',
        f'c2_tls_cert = "{_toml_str(security.get("c2_tls_cert", ""))}"',
        f'c2_tls_key = "{_toml_str(security.get("c2_tls_key", ""))}"',
        f'c2_tls_fingerprint = "{_toml_str(security.get("c2_tls_fingerprint", ""))}"',
        "",
    ])

    return "\n".join(lines)


def _toml_list(items) -> str:
    """Render a Python list as a TOML inline array of strings."""
    from xmrdp.config import _toml_str
    inner = ", ".join(f'"{_toml_str(str(i))}"' for i in items)
    return f"[{inner}]"
