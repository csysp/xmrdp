"""Binary update management for XMRDP.

Checks GitHub releases for newer versions of monerod, p2pool, and xmrig,
and performs rolling updates with a safe restart strategy.
"""

import copy
import json
import sys

from xmrdp.binary_manager import (
    _read_versions,
    ensure_binaries,
    get_latest_release,
)
from xmrdp.config import load_config
from xmrdp.constants import GITHUB_REPOS
from xmrdp.node_manager import is_running, start_service, stop_service


# Update order: workers first (xmrig), then infrastructure (p2pool, monerod).
# This minimises downtime — miners reconnect automatically once p2pool is back.
_UPDATE_ORDER = ("xmrig", "p2pool", "monero")


def check_updates(config):
    """Compare installed versions against latest GitHub releases.

    Parameters
    ----------
    config : dict
        Loaded cluster configuration.

    Returns
    -------
    dict
        ``{software: {"current": tag, "latest": tag, "update_available": bool}}``
        for each of monero, p2pool, and xmrig.
    """
    versions = _read_versions()
    results = {}

    for software, repo in GITHUB_REPOS.items():
        current_tag = versions.get(software, {}).get("version")

        # Respect pinned versions in config — only check "latest" otherwise.
        configured = config.get("binaries", {}).get(
            f"{software}_version", "latest"
        )
        tag_to_check = None if configured == "latest" else configured

        try:
            release = get_latest_release(repo, tag=tag_to_check)
            latest_tag = release["tag_name"]
        except Exception as exc:
            # Network errors should not crash the whole check; report unknown.
            print(
                f"  Warning: could not fetch release info for {software}: {exc}",
                file=sys.stderr,
            )
            results[software] = {
                "current": current_tag or "not installed",
                "latest": "unknown",
                "update_available": False,
            }
            continue

        update_available = (
            current_tag is not None
            and latest_tag != current_tag
        )
        # If nothing is installed yet, it is not an "update" — it is a fresh
        # install, which ``xmrdp setup`` handles.
        results[software] = {
            "current": current_tag or "not installed",
            "latest": latest_tag,
            "update_available": update_available,
        }

    return results


def apply_updates(config, updates):
    """Download and install binaries that have pending updates.

    Uses a rolling-restart strategy in safe order:
    xmrig (miners) -> p2pool (stratum) -> monerod (node).

    Each service is stopped, the binary replaced via ``ensure_binaries``
    with ``force=True``, and then restarted.

    Parameters
    ----------
    config : dict
        Loaded cluster configuration.
    updates : dict
        Output of :func:`check_updates`.

    Returns
    -------
    list[str]
        Names of software that were successfully updated.
    """
    updated = []

    for software in _UPDATE_ORDER:
        info = updates.get(software, {})
        if not info.get("update_available"):
            continue

        current = info["current"]
        latest = info["latest"]
        print(f"\n--- Updating {software}: {current} -> {latest} ---")

        # 1. Stop the running service (if it is running).
        service_name = _service_name(software)
        was_running = is_running(service_name)
        if was_running:
            print(f"  Stopping {service_name} ...")
            stop_service(service_name)

        # 2. Re-download the binary.  We build a minimal config overlay so
        #    ensure_binaries only processes this single software entry.
        single_config = _single_software_config(config, software, latest)
        try:
            ensure_binaries(single_config, force=True)
        except Exception as exc:
            print(
                f"  ERROR: failed to download {software} {latest}: {exc}",
                file=sys.stderr,
            )
            # Attempt to restart the old binary so the service isn't left down.
            if was_running:
                print(f"  Restarting {service_name} with previous binary ...")
                _restart_service(service_name, config)
            continue

        # 3. Restart the service with the new binary.
        if was_running:
            print(f"  Starting {service_name} ...")
            _restart_service(service_name, config)

        print(f"  {software} updated to {latest}")
        updated.append(software)

    return updated


def cmd_update(args):
    """CLI handler for ``xmrdp update [--check-only]``."""
    config_path = getattr(args, "config", None)
    check_only = getattr(args, "check_only", False)

    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Checking for updates ...\n")
    updates = check_updates(config)

    # --- Print comparison table ---
    header = f"{'Software':<10} {'Current':<20} {'Latest':<20} {'Status'}"
    print(header)
    print("-" * len(header))

    any_available = False
    for software in ("monero", "p2pool", "xmrig"):
        info = updates.get(software, {})
        current = info.get("current", "?")
        latest = info.get("latest", "?")
        if info.get("update_available"):
            status = "UPDATE AVAILABLE"
            any_available = True
        elif current == "not installed":
            status = "not installed"
        elif latest == "unknown":
            status = "check failed"
        else:
            status = "up to date"
        print(f"{software:<10} {current:<20} {latest:<20} {status}")

    print()

    if check_only:
        sys.exit(0 if not any_available else 2)

    if not any_available:
        print("All binaries are up to date.")
        return

    # --- Confirmation prompt ---
    try:
        answer = input("Apply updates? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    if answer not in ("y", "yes"):
        print("Update cancelled.")
        return

    updated = apply_updates(config, updates)

    # --- Summary ---
    print("\n=== Update Summary ===")
    if updated:
        for sw in updated:
            info = updates[sw]
            print(f"  {sw}: {info['current']} -> {info['latest']}")
        print(f"\n{len(updated)} component(s) updated successfully.")
    else:
        print("  No updates were applied.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _service_name(software):
    """Map software key to the service name used by node_manager."""
    return {
        "monero": "monerod",
        "p2pool": "p2pool",
        "xmrig": "xmrig",
    }[software]


def _restart_service(service_name, config):
    """Restart a single service using the correct binary and arguments."""
    from xmrdp.binary_manager import get_binary_path
    from xmrdp.config_generator import (
        generate_monerod_args,
        generate_p2pool_args,
        write_xmrig_config,
    )

    if service_name == "monerod":
        binary = get_binary_path("monero")
        if binary is None:
            raise RuntimeError(
                "monerod binary not found. Run 'xmrdp setup' to download binaries."
            )
        args = generate_monerod_args(config)
    elif service_name == "p2pool":
        binary = get_binary_path("p2pool")
        if binary is None:
            raise RuntimeError(
                "p2pool binary not found. Run 'xmrdp setup' to download binaries."
            )
        args = generate_p2pool_args(config)
    elif service_name == "xmrig":
        binary = get_binary_path("xmrig")
        if binary is None:
            raise RuntimeError(
                "xmrig binary not found. Run 'xmrdp setup' to download binaries."
            )
        config_path = write_xmrig_config(config, role="master")
        args = ["--config", str(config_path)]
    else:
        raise ValueError(f"Unknown service: {service_name}")

    start_service(service_name, binary, args)


def _single_software_config(config, software, tag):
    """Build a config dict that causes ensure_binaries to process only one
    software entry, pinned to *tag*.

    ensure_binaries reads version pins from config["binaries"] with keys
    like "monero_version", "p2pool_version", "xmrig_version".  We build a
    patched binaries section that pins the target software to *tag* and
    all others to their cached versions so they are skipped (cache hit).

    Returns a deep copy so that nested dicts (master, workers, security, …)
    are independent of the caller's config and cannot be mutated downstream.
    """
    from xmrdp.binary_manager import _read_versions

    key_map = {
        "monero": "monero_version",
        "p2pool": "p2pool_version",
        "xmrig": "xmrig_version",
    }
    cached = _read_versions()

    # Deep copy so nested dicts are fully independent of the caller's config.
    patched = copy.deepcopy(config)
    binaries_override = dict(patched.get("binaries", {}))

    for sw in GITHUB_REPOS:
        key = key_map.get(sw, f"{sw}_version")
        if sw == software:
            binaries_override[key] = tag
        else:
            # Pin to the cached version so ensure_binaries treats it as a
            # cache hit and does not re-download.
            cached_ver = cached.get(sw, {}).get("version", "latest")
            binaries_override[key] = cached_ver

    patched["binaries"] = binaries_override
    return patched
