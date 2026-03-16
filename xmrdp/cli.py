"""CLI entry point for XMRDP."""

import argparse
import logging
import sys

from xmrdp import __version__


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="xmrdp",
        description="XMRDP — Monero Mining Cluster Rapid Deployment Tool",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"xmrdp {__version__}",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable verbose logging output (DEBUG level).",
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to cluster.toml config file",
        default=None,
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # setup
    sp_setup = sub.add_parser("setup", help="Download binaries and configure cluster")
    sp_setup.add_argument(
        "--skip-binaries", action="store_true",
        help="Skip binary download (config only)",
    )

    # start
    sp_start = sub.add_parser("start", help="Start services")
    sp_start.add_argument(
        "role", choices=["master", "worker"],
        help="Node role to start",
    )

    # stop
    sp_stop = sub.add_parser("stop", help="Stop services")
    sp_stop.add_argument(
        "role", choices=["master", "worker"],
        help="Node role to stop",
    )

    # status
    sub.add_parser("status", help="Show cluster status")

    # update
    sp_update = sub.add_parser("update", help="Check for and apply binary updates")
    sp_update.add_argument(
        "--check-only", action="store_true",
        help="Only check for updates, don't apply",
    )

    # logs
    sp_logs = sub.add_parser("logs", help="Tail service logs")
    sp_logs.add_argument(
        "service", nargs="?", default=None,
        choices=["monerod", "p2pool", "xmrig", "c2"],
        help="Service to show logs for (default: all)",
    )
    sp_logs.add_argument(
        "-n", "--lines", type=int, default=50,
        help="Number of lines to show (default: 50)",
    )

    # firewall
    sp_fw = sub.add_parser("firewall", help="Generate firewall rules")
    sp_fw.add_argument(
        "role", choices=["master", "worker"],
        help="Role to generate rules for",
    )

    # sync
    sp_sync = sub.add_parser(
        "sync",
        help="Push cluster.toml to worker nodes via SSH/SCP",
    )
    sp_sync.add_argument(
        "--worker",
        metavar="NAME",
        nargs="+",
        help="Sync only the named worker(s) (default: all workers in config)",
    )
    sp_sync.add_argument(
        "--ssh-user",
        metavar="USER",
        default="",
        help="SSH username (default: current user)",
    )
    sp_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without making any changes",
    )
    sp_sync.add_argument(
        "--restart",
        action="store_true",
        help="Restart xmrig on each worker after syncing config",
    )

    # config
    sp_cfg = sub.add_parser("config", help="Manage cluster configuration")
    sp_cfg.add_argument(
        "--generate", action="store_true",
        help="Generate default config file",
    )
    sp_cfg.add_argument(
        "--validate", action="store_true",
        help="Validate current config",
    )
    sp_cfg.add_argument(
        "--show", action="store_true",
        help="Print current config",
    )

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if args.command == "setup":
        from xmrdp.wizard import run_setup
        run_setup(args)
    elif args.command == "start":
        from xmrdp.cluster import cmd_start
        cmd_start(args)
    elif args.command == "stop":
        from xmrdp.cluster import cmd_stop
        cmd_stop(args)
    elif args.command == "status":
        from xmrdp.cluster import cmd_status
        cmd_status(args)
    elif args.command == "update":
        from xmrdp.updater import cmd_update
        cmd_update(args)
    elif args.command == "logs":
        from xmrdp.node_manager import cmd_logs
        cmd_logs(args)
    elif args.command == "sync":
        from xmrdp.sync import cmd_sync
        cmd_sync(args)
    elif args.command == "firewall":
        from xmrdp.firewall import cmd_firewall
        cmd_firewall(args)
    elif args.command == "config":
        _handle_config(args)


def _handle_config(args):
    if args.generate:
        from xmrdp.wizard import generate_config_cmd
        generate_config_cmd(args)
    elif args.validate:
        from xmrdp.config import load_config
        try:
            config = load_config(args.config)
            from xmrdp.config import validate_wallet
            wallet = config.get("cluster", {}).get("wallet", "")
            ok, msg = validate_wallet(wallet)
            if ok:
                print("Config is valid.")
            else:
                print(f"Config warning: {msg}")
        except Exception as e:
            print(f"Config error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.show:
        from xmrdp.config import load_config
        import copy
        import json
        _SENSITIVE_KEYS = frozenset({"api_token", "token", "secret", "password", "key"})

        def _redact(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.lower() in _SENSITIVE_KEYS:
                        obj[k] = "[REDACTED]"
                    else:
                        _redact(v)
            elif isinstance(obj, list):
                for item in obj:
                    _redact(item)

        try:
            config = load_config(args.config)
            safe = copy.deepcopy(config)
            _redact(safe)
            print(json.dumps(safe, indent=2))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Use --generate, --validate, or --show. See: xmrdp config -h")
