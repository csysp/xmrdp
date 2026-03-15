"""Platform detection and OS-specific utilities."""

import os
import platform
import stat
import sys
from pathlib import Path


def detect_platform():
    """Return normalized (system, machine) tuple.

    system: 'linux', 'windows', or 'darwin'
    machine: 'x86_64' or 'aarch64'
    """
    system = platform.system().lower()
    machine = platform.machine().lower()

    # Normalize architecture names
    if machine in ("x86_64", "amd64", "x64"):
        machine = "x86_64"
    elif machine in ("aarch64", "arm64", "armv8l"):
        machine = "aarch64"

    return system, machine


def get_data_dir():
    """Return the XMRDP data directory, creating it if needed.

    - Windows: %LOCALAPPDATA%/xmrdp  (or %USERPROFILE%/.xmrdp)
    - macOS:   ~/Library/Application Support/xmrdp
    - Linux:   ~/.xmrdp  (or $XDG_DATA_HOME/xmrdp)
    """
    system = platform.system().lower()

    if system == "windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            data_dir = Path(base) / "xmrdp"
        else:
            data_dir = Path.home() / ".xmrdp"
    elif system == "darwin":
        data_dir = Path.home() / "Library" / "Application Support" / "xmrdp"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            data_dir = Path(xdg) / "xmrdp"
        else:
            data_dir = Path.home() / ".xmrdp"

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_config_dir():
    """Return the XMRDP config directory, creating it if needed."""
    system = platform.system().lower()

    if system == "windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            config_dir = Path(base) / "xmrdp" / "config"
        else:
            config_dir = Path.home() / ".xmrdp" / "config"
    elif system == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "xmrdp" / "config"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            config_dir = Path(xdg) / "xmrdp"
        else:
            config_dir = Path.home() / ".xmrdp" / "config"

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def make_executable(path):
    """Make a file executable on Unix systems. No-op on Windows."""
    if platform.system().lower() == "windows":
        return
    p = Path(path)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def get_pid_dir():
    """Return the directory for PID files."""
    pid_dir = get_data_dir() / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir


def get_log_dir():
    """Return the directory for log files."""
    log_dir = get_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_binary_dir():
    """Return the directory for cached binaries."""
    bin_dir = get_data_dir() / "binaries"
    bin_dir.mkdir(parents=True, exist_ok=True)
    return bin_dir
