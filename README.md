# XMRDP — Monero Mining Cluster Rapid Deployment Tool

Deploy a Monero mining cluster on P2Pool mini in minutes. One master node runs
monerod + p2pool + xmrig and acts as C2 for any number of worker nodes running
xmrig only. OS-agnostic (Windows/Linux/macOS), secure (no private keys on any
node), and self-updating.

## Quick Start

```bash
pip install .
xmrdp setup          # Download binaries + interactive config wizard
xmrdp start master   # Start master node services
xmrdp start worker   # Start worker node
xmrdp status         # Cluster overview
```

## Architecture

```
Master (monerod:18081 + p2pool:3333 + xmrig + C2:7099)
  ├── Worker 1 (xmrig -> master:3333)
  ├── Worker 2 (xmrig -> master:3333)
  └── ...N workers
```

All payouts go directly to your wallet via P2Pool coinbase transactions.
No private keys are stored on any node.

## Configuration

Copy `configs/cluster.example.toml` and edit:

```bash
xmrdp config --generate    # Generate default config
xmrdp config --validate    # Validate current config
xmrdp config --show        # Print current config
```

## Commands

| Command | Description |
|---------|-------------|
| `xmrdp setup` | Download binaries and run config wizard |
| `xmrdp start [master\|worker]` | Start services for the given role |
| `xmrdp stop [master\|worker]` | Stop services |
| `xmrdp status` | Show cluster status |
| `xmrdp update` | Check for and apply binary updates |
| `xmrdp logs [service]` | Tail service logs |
| `xmrdp firewall` | Generate firewall rules for current role |
| `xmrdp config` | Manage cluster configuration |

## Security Model

- No private keys anywhere — only public wallet address
- SHA256 binary verification from GitHub releases
- API auth via pre-shared Bearer token (64-char hex)
- C2 is data-only (configs + status), cannot execute commands
- Firewall rule generation per role
- No telemetry — only GitHub API + Monero/p2pool p2p networks

## Requirements

- Python 3.8+
- No external dependencies (stdlib only, `tomli` fallback for Python < 3.11)

## License

MIT
