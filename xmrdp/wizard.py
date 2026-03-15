"""Interactive setup wizard for XMRDP cluster configuration."""

import secrets
import sys
from pathlib import Path

from xmrdp.config import generate_default_config, validate_wallet
from xmrdp.constants import CONFIG_FILENAME
from xmrdp.platforms import get_config_dir


def run_setup(args):
    """Run the interactive setup wizard.

    Walks the user through wallet, role, networking, and worker
    configuration, then writes a cluster.toml and optionally
    downloads binaries.
    """
    try:
        _print_banner()

        # --- Wallet ---
        wallet = _ask_wallet()

        # --- Role ---
        role = _ask_choice(
            "What role will THIS machine play?",
            ["master", "worker"],
            default="master",
        )

        # --- Master host ---
        if role == "master":
            master_host = _ask(
                "Master IP / hostname (reachable by workers)",
                default="127.0.0.1",
            )
        else:
            master_host = _ask(
                "Master node IP / hostname to connect to",
                default="192.168.1.100",
            )

        # --- Workers ---
        workers = []
        if role == "master":
            print()
            print("Enter worker nodes (one per line, format: name host)")
            print("Example: worker-1 192.168.1.101")
            print("Press Enter on an empty line when done.")
            print()
            idx = 1
            while True:
                line = input(f"  Worker {idx}: ").strip()
                if not line:
                    break
                parts = line.split(None, 1)
                if len(parts) == 2:
                    workers.append({"name": parts[0], "host": parts[1]})
                elif len(parts) == 1:
                    # Treat as host only; auto-name.
                    workers.append({"name": f"worker-{idx}", "host": parts[0]})
                idx += 1

        # --- API token ---
        api_token = secrets.token_hex(32)

        # --- Generate and write config ---
        config_content = generate_default_config(
            wallet=wallet,
            master_host=master_host,
            workers=workers,
        )

        # Inject the generated api_token into the config text.
        config_content = config_content.replace(
            'api_token = ""  # Auto-generated on first setup',
            f'api_token = "{api_token}"',
        )

        config_dir = get_config_dir()
        config_path = config_dir / CONFIG_FILENAME
        config_path.write_text(config_content, encoding="utf-8")

        # --- Binaries ---
        skip_binaries = getattr(args, "skip_binaries", False)
        if not skip_binaries:
            print()
            print("Downloading binaries ...")
            try:
                from xmrdp.binary_manager import ensure_binaries
                from xmrdp.config import load_config
                config = load_config(str(config_path))
                ensure_binaries(config)
                print("Binaries downloaded and verified.")
            except Exception as exc:
                print(f"WARNING: Binary download failed: {exc}")
                print("You can retry later with: xmrdp setup")
        else:
            print()
            print("Skipping binary download (--skip-binaries).")

        # --- Summary ---
        _print_summary(
            wallet=wallet,
            role=role,
            master_host=master_host,
            workers=workers,
            config_path=config_path,
            api_token=api_token,
        )

    except KeyboardInterrupt:
        print()
        print("Setup cancelled.")
        sys.exit(130)


def generate_config_cmd(args):
    """Generate a default cluster.toml config file.

    If --config is set on *args*, write there; otherwise write to
    the platform config directory.
    """
    config_path_arg = getattr(args, "config", None)

    if config_path_arg:
        dest = Path(config_path_arg)
    else:
        dest = get_config_dir() / CONFIG_FILENAME

    if dest.exists():
        print(f"Config already exists: {dest}")
        overwrite = input("Overwrite? [y/N] ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("Aborted.")
            return

    content = generate_default_config()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    print(f"Default config written to: {dest}")
    print("Edit the file and run 'xmrdp setup' to continue.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_banner():
    """Print the welcome banner."""
    print()
    print("=" * 56)
    print("  XMRDP -- Monero Mining Cluster Rapid Deployment Tool")
    print("=" * 56)
    print()
    print("This wizard will walk you through initial configuration.")
    print()


def _ask(prompt, default=None):
    """Prompt the user for input with an optional default."""
    if default:
        display = f"{prompt} [{default}]: "
    else:
        display = f"{prompt}: "

    value = input(display).strip()
    if not value and default is not None:
        return default
    return value


def _ask_choice(prompt, choices, default=None):
    """Prompt the user to pick from a list of choices."""
    options = "/".join(choices)
    if default:
        display = f"{prompt} ({options}) [{default}]: "
    else:
        display = f"{prompt} ({options}): "

    while True:
        value = input(display).strip().lower()
        if not value and default:
            return default
        if value in choices:
            return value
        print(f"  Please choose one of: {options}")


def _ask_wallet():
    """Prompt for and validate a Monero wallet address."""
    while True:
        wallet = _ask("Monero wallet address (public)")
        if not wallet:
            print("  Wallet address is required.")
            continue
        ok, msg = validate_wallet(wallet)
        if ok:
            return wallet
        print(f"  {msg}")
        print("  Please try again.")


def _print_summary(wallet, role, master_host, workers, config_path, api_token):
    """Print a summary of the generated configuration."""
    print()
    print("-" * 56)
    print("  Setup Complete")
    print("-" * 56)
    print()
    print(f"  Role:          {role}")
    print(f"  Wallet:        {wallet[:8]}...{wallet[-8:]}" if len(wallet) > 20
          else f"  Wallet:        {wallet}")
    print(f"  Master host:   {master_host}")
    print(f"  Workers:       {len(workers)}")
    for w in workers:
        print(f"                   {w['name']} @ {w['host']}")
    print(f"  Config file:   {config_path}")
    print(f"  API token:     {api_token[:8]}...{api_token[-8:]}")
    print()
    print("Next steps:")
    if role == "master":
        print("  1. Start the master:   xmrdp start master")
        print("  2. On each worker:     xmrdp setup  (then xmrdp start worker)")
    else:
        print("  1. Start the worker:   xmrdp start worker")
    print()
