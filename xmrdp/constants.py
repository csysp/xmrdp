"""Constants for XMRDP — GitHub repos, ports, platform maps, default paths."""

# GitHub repositories for binary downloads
GITHUB_REPOS = {
    "monero": "monero-project/monero",
    "p2pool": "SChernykh/p2pool",
    "xmrig": "xmrig/xmrig",
}

GITHUB_API = "https://api.github.com"

# Default network ports
PORTS = {
    "monerod_rpc": 18081,
    "monerod_p2p": 18080,
    "monerod_zmq": 18083,
    "p2pool_stratum": 3333,
    "p2pool_p2p": 37888,
    "c2_api": 7099,
}

# Regex patterns to match the correct release asset per platform/arch.
# Keys: (system, machine) where system = platform.system().lower()
# and machine = platform.machine().lower() normalized.
ASSET_PATTERNS = {
    "monero": {
        ("linux", "x86_64"):  r"monero-linux-x64-v[\d.]+\.tar\.bz2$",
        ("linux", "aarch64"): r"monero-linux-armv8-v[\d.]+\.tar\.bz2$",
        ("windows", "x86_64"): r"monero-win-x64-v[\d.]+\.zip$",
        ("darwin", "x86_64"): r"monero-mac-x64-v[\d.]+\.tar\.bz2$",
        ("darwin", "aarch64"): r"monero-mac-armv8-v[\d.]+\.tar\.bz2$",
    },
    "p2pool": {
        ("linux", "x86_64"):  r"p2pool-v[\d.]+-linux-x64\.tar\.gz$",
        ("linux", "aarch64"): r"p2pool-v[\d.]+-linux-aarch64\.tar\.gz$",
        ("windows", "x86_64"): r"p2pool-v[\d.]+-windows-x64\.zip$",
        ("darwin", "x86_64"): r"p2pool-v[\d.]+-macos-x64\.tar\.gz$",
        ("darwin", "aarch64"): r"p2pool-v[\d.]+-macos-aarch64\.tar\.gz$",
    },
    "xmrig": {
        ("linux", "x86_64"):  r"xmrig-[\d.]+-linux-x64\.tar\.gz$",
        ("linux", "aarch64"): r"xmrig-[\d.]+-linux-static-arm.*\.tar\.gz$",
        ("windows", "x86_64"): r"xmrig-[\d.]+-msvc-win64\.zip$",
        ("darwin", "x86_64"): r"xmrig-[\d.]+-macos-x64\.tar\.gz$",
        ("darwin", "aarch64"): r"xmrig-[\d.]+-macos-arm64\.tar\.gz$",
    },
}

# Binary executable names per platform
BINARY_NAMES = {
    "monero": {
        "windows": "monerod.exe",
        "linux": "monerod",
        "darwin": "monerod",
    },
    "p2pool": {
        "windows": "p2pool.exe",
        "linux": "p2pool",
        "darwin": "p2pool",
    },
    "xmrig": {
        "windows": "xmrig.exe",
        "linux": "xmrig",
        "darwin": "xmrig",
    },
}

# SHA256 checksum file patterns in releases
CHECKSUM_PATTERNS = {
    "monero": r"hashes\.txt$",
    "p2pool": r"sha256sums\.txt$",
    "xmrig": r"SHA256SUMS$",
}

# Default config file name
CONFIG_FILENAME = "cluster.toml"

# Heartbeat interval for workers (seconds)
HEARTBEAT_INTERVAL = 60

# Health check timeouts (seconds)
HEALTH_CHECK_TIMEOUT = 5
HEALTH_CHECK_RETRIES = 30
HEALTH_CHECK_INTERVAL = 10
