"""Interactive setup wizard for XMRDP cluster configuration."""

import hashlib
import os
import secrets
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

from xmrdp.config import generate_default_config, validate_wallet, _toml_str
from xmrdp.constants import CONFIG_FILENAME, C2_TLS_CERT_FILE, C2_TLS_KEY_FILE
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
        api_token_id = secrets.token_hex(8)
        xmrig_http_token = secrets.token_hex(16)
        xmrig_http_token_id = secrets.token_hex(8)

        # --- TLS certificate (master only) ---
        config_dir = get_config_dir()
        tls_enabled = False
        tls_fingerprint = ""
        tls_cert_path = config_dir / C2_TLS_CERT_FILE
        tls_key_path = config_dir / C2_TLS_KEY_FILE
        token_store_path = config_dir / "tokens.json"

        if role == "master":
            print()
            print("Generating TLS certificate for C2 server ...")
            if _generate_tls_cert(tls_cert_path, tls_key_path):
                tls_fingerprint = _get_cert_fingerprint(tls_cert_path)
                tls_enabled = True
                print(f"  Certificate ready.  Fingerprint: {tls_fingerprint[:16]}...")
            else:
                print("  TLS skipped — C2 will use plain HTTP.")
                print("  Install openssl and re-run 'xmrdp setup' to enable TLS.")

        # --- Generate and write config ---
        config_content = generate_default_config(
            wallet=wallet,
            master_host=master_host,
            workers=workers,
        )

        # Inject generated token IDs into config text (secrets stored separately).
        config_content = config_content.replace(
            'api_token = ""  # Auto-generated on first setup',
            f'api_token_id = "{api_token_id}"',
        )
        config_content = config_content.replace(
            'http_token = ""  # Auto-generated on first setup',
            f'http_token_id = "{xmrig_http_token_id}"',
        )
        if tls_enabled:
            config_content = config_content.replace(
                'tls_enabled = false', 'tls_enabled = true'
            )
            config_content = config_content.replace(
                'c2_tls_cert = ""', f'c2_tls_cert = "{_toml_str(str(tls_cert_path))}"'
            )
            config_content = config_content.replace(
                'c2_tls_key = ""', f'c2_tls_key = "{_toml_str(str(tls_key_path))}"'
            )
            config_content = config_content.replace(
                'c2_tls_fingerprint = ""',
                f'c2_tls_fingerprint = "{tls_fingerprint}"',
            )

        # --- Persist tokens in a separate, restricted token store ---
        token_store = {
            "api_tokens": {api_token_id: api_token},
            "xmrig_http_tokens": {xmrig_http_token_id: xmrig_http_token},
        }
        if token_store_path.exists():
            try:
                existing = token_store_path.read_text(encoding="utf-8")
                import json as _json  # local import to avoid top-level clashes if any
                data = _json.loads(existing) if existing.strip() else {}
            except Exception:
                data = {}
            api_tokens = data.get("api_tokens", {})
            api_tokens.update(token_store["api_tokens"])
            xmrig_http_tokens = data.get("xmrig_http_tokens", {})
            xmrig_http_tokens.update(token_store["xmrig_http_tokens"])
            data["api_tokens"] = api_tokens
            data["xmrig_http_tokens"] = xmrig_http_tokens
        else:
            data = token_store

        if sys.platform != "win32":
            # Create token store with mode 0o600 atomically.
            ts_fd = os.open(
                token_store_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                with os.fdopen(ts_fd, "w", encoding="utf-8") as ts_fh:
                    import json as _json
                    _json.dump(data, ts_fh)
            except BaseException:
                try:
                    os.close(ts_fd)
                except OSError:
                    pass
                raise
        else:
            import json as _json
            token_store_path.write_text(_json.dumps(data), encoding="utf-8")

        config_path = config_dir / CONFIG_FILENAME
        if sys.platform != "win32":
            # Create with mode 0o600 atomically — no world-readable race window (F-13).
            fd = os.open(
                config_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(config_content)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        else:
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
            tls_enabled=tls_enabled,
            tls_fingerprint=tls_fingerprint,
            xmrig_http_token=xmrig_http_token,
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
    if sys.platform != "win32":
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
        dest.write_text(content, encoding="utf-8")
    print(f"Default config written to: {dest}")
    print("Edit the file and run 'xmrdp setup' to continue.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_openssl() -> str:
    """Return path to the openssl binary, or empty string if not found."""
    # Prefer well-known absolute paths to avoid PATH hijack (Fix 7).
    if sys.platform != "win32":
        for trusted in ("/usr/bin/openssl", "/usr/local/bin/openssl", "/opt/homebrew/bin/openssl"):
            if Path(trusted).is_file():
                return trusted
    # Fall back to PATH search (acceptable if none of the above exist).
    found = shutil.which("openssl")
    if found:
        return found
    # Windows: check common install locations.
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
            r"C:\Program Files (x86)\OpenSSL-Win32\bin\openssl.exe",
            r"C:\OpenSSL-Win64\bin\openssl.exe",
        ]
        for p in candidates:
            if Path(p).is_file():
                return p
    return ""


def _generate_tls_cert(cert_path: Path, key_path: Path) -> bool:
    """Generate a self-signed TLS cert using openssl.  Returns True on success."""
    openssl = _find_openssl()
    if not openssl:
        return False
    try:
        result = subprocess.run(
            [
                openssl, "req", "-x509",
                "-newkey", "rsa:2048",
                "-keyout", str(key_path),
                "-out", str(cert_path),
                "-days", "3650",
                "-nodes",
                "-subj", "/CN=xmrdp-c2",
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False
        if sys.platform != "win32":
            os.chmod(key_path, 0o600)
            os.chmod(cert_path, 0o600)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _get_cert_fingerprint(cert_path: Path) -> str:
    """Return the SHA-256 fingerprint (hex) of a PEM certificate file."""
    cert_pem = cert_path.read_text(encoding="ascii")
    cert_der = ssl.PEM_cert_to_DER_cert(cert_pem)
    return hashlib.sha256(cert_der).hexdigest()


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


def _print_summary(wallet, role, master_host, workers, config_path, api_token,
                   tls_enabled=False, tls_fingerprint="", xmrig_http_token=""):
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
    print("  API token:     [generated and stored in config]")
    print(f"  xmrig HTTP token: {xmrig_http_token[:8]}..." if xmrig_http_token else "  xmrig HTTP token: not set")
    if tls_enabled:
        print(f"  C2 TLS:        enabled")
        print(f"  TLS fingerprint: {tls_fingerprint[:16]}...{tls_fingerprint[-8:]}")
    else:
        print(f"  C2 TLS:        disabled (install openssl and re-run setup to enable)")
    print()
    print("Next steps:")
    if role == "master":
        print("  1. Start the master:   xmrdp start master")
        print("  2. On each worker:     xmrdp setup  (then xmrdp start worker)")
    else:
        print("  1. Start the worker:   xmrdp start worker")
    print()
