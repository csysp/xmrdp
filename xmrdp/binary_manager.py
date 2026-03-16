"""Binary manager — download, verify, and cache monerod / p2pool / xmrig.

Handles GitHub release lookups, platform-specific asset matching, SHA-256
verification, archive extraction, and local version tracking.  Uses only
Python stdlib (no external dependencies).
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from xmrdp.constants import (
    ASSET_PATTERNS,
    BINARY_NAMES,
    CHECKSUM_PATTERNS,
    GITHUB_API,
    GITHUB_REPOS,
)
from xmrdp.platforms import detect_platform, get_binary_dir, make_executable

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_USER_AGENT = "xmrdp-binary-manager/1.0"
_CHUNK_SIZE = 8192  # 8 KB read chunks
_VERSIONS_FILE = ".versions.json"
_MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB — larger than any legitimate release


def _github_headers() -> dict:
    """Return headers for GitHub API requests, including auth if GITHUB_TOKEN is set."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(url, accept="application/json"):
    """Build and execute an HTTP GET, returning the response object.

    Raises ``RuntimeError`` on rate-limiting (HTTP 403 with rate-limit
    headers) so the caller can surface a clear message.
    """
    parsed_host = url.split("/")[2] if "/" in url else ""
    is_github_api = parsed_host == "api.github.com"

    if is_github_api:
        headers = _github_headers()
        headers["User-Agent"] = _USER_AGENT
        headers["Accept"] = accept
    else:
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": accept,
        }
    req = Request(url, headers=headers)
    try:
        return urlopen(req, timeout=30)  # nosec B310 — URLs are GitHub HTTPS only; scheme validated by caller
    except HTTPError as exc:
        if exc.code == 403:
            remaining = exc.headers.get("X-RateLimit-Remaining", "")
            if remaining == "0":
                reset = exc.headers.get("X-RateLimit-Reset", "unknown")
                raise RuntimeError(
                    f"GitHub API rate limit exceeded. Resets at epoch {reset}. "
                    "Set a GITHUB_TOKEN environment variable to raise the limit."
                ) from exc
        raise


def _read_versions():
    """Load the versions.json cache from the binary directory."""
    path = get_binary_dir() / _VERSIONS_FILE
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_versions(data):
    """Persist the versions.json cache atomically."""
    dest = get_binary_dir() / _VERSIONS_FILE
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(get_binary_dir()), suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        # On Windows, replace will fail if dest exists — remove first.
        if sys.platform == "win32" and dest.exists():
            dest.unlink()
        Path(tmp_path).replace(dest)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_latest_release(repo, tag=None):
    """Fetch release metadata from GitHub.

    Parameters
    ----------
    repo : str
        Owner/repo, e.g. ``"monero-project/monero"``.
    tag : str or None
        If provided, fetch that specific tag instead of ``latest``.

    Returns
    -------
    dict
        Has at least ``tag_name`` (str) and ``assets`` (list of dicts with
        ``name``, ``browser_download_url``, ``size``).
    """
    if tag and tag != "latest":
        url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    else:
        url = f"{GITHUB_API}/repos/{repo}/releases/latest"

    resp = _request(url)
    data = json.loads(resp.read().decode("utf-8"))
    return {
        "tag_name": data["tag_name"],
        "assets": [
            {
                "name": a["name"],
                "browser_download_url": a["browser_download_url"],
                "size": a.get("size", 0),
            }
            for a in data.get("assets", [])
        ],
    }


def match_asset(assets, software, system, machine):
    """Select the correct release asset for this platform.

    Parameters
    ----------
    assets : list[dict]
        Asset dicts as returned by :func:`get_latest_release`.
    software : str
        One of ``"monero"``, ``"p2pool"``, ``"xmrig"``.
    system : str
        Normalized OS (``"linux"``, ``"windows"``, ``"darwin"``).
    machine : str
        Normalized arch (``"x86_64"``, ``"aarch64"``).

    Returns
    -------
    dict or None
        The matching asset dict, or ``None`` if no match is found.
    """
    patterns = ASSET_PATTERNS.get(software, {})
    pattern = patterns.get((system, machine))
    if pattern is None:
        return None

    regex = re.compile(pattern)
    for asset in assets:
        if regex.search(asset["name"]):
            return asset
    return None


def download_binary(url, dest_path, expected_size=None):
    """Download a file with a progress indicator.

    Parameters
    ----------
    url : str
        Direct download URL.
    dest_path : str or Path
        Local destination path.
    expected_size : int or None
        Expected file size in bytes (used for progress display).
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    resp = _request(url, accept="application/octet-stream")

    # Check Content-Length upfront if present to catch oversized responses early.
    try:
        content_length = int(resp.headers.get("Content-Length", 0) or 0)
    except (ValueError, TypeError):
        content_length = 0
    if content_length > _MAX_DOWNLOAD_SIZE:
        raise RuntimeError(
            f"Response Content-Length {content_length} exceeds maximum "
            f"allowed download size {_MAX_DOWNLOAD_SIZE}"
        )

    total = expected_size or content_length
    total_mb = total / (1024 * 1024) if total else 0.0

    downloaded = 0
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(dest_path.parent), suffix=".dl")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > _MAX_DOWNLOAD_SIZE:
                    Path(tmp_path).unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Download of {url!r} exceeded {_MAX_DOWNLOAD_SIZE} bytes — "
                        "aborting to prevent disk exhaustion."
                    )
                fh.write(chunk)
                done_mb = downloaded / (1024 * 1024)
                if total:
                    sys.stdout.write(
                        f"\r  Downloading: {done_mb:.1f} / {total_mb:.1f} MB"
                    )
                else:
                    sys.stdout.write(f"\r  Downloading: {done_mb:.1f} MB")
                sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()

        # Move completed download into place.
        if sys.platform == "win32" and dest_path.exists():
            dest_path.unlink()
        Path(tmp_path).replace(dest_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def verify_checksum(file_path, expected_hash):
    """Verify a file's SHA-256 hash.

    Returns ``True`` if the computed hash matches *expected_hash*
    (case-insensitive comparison).
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest().lower() == expected_hash.strip().lower()


def get_release_checksums(assets, software):
    """Download and parse the checksum file from a release.

    Parameters
    ----------
    assets : list[dict]
        Release assets.
    software : str
        ``"monero"``, ``"p2pool"``, or ``"xmrig"``.

    Returns
    -------
    dict
        Mapping of ``{filename: sha256_hex}`` (lowercased hashes).
        Empty dict if no checksum file is found.
    """
    pattern = CHECKSUM_PATTERNS.get(software)
    if pattern is None:
        return {}

    regex = re.compile(pattern)
    checksum_asset = None
    for asset in assets:
        if regex.search(asset["name"]):
            checksum_asset = asset
            break
    if checksum_asset is None:
        return {}

    resp = _request(
        checksum_asset["browser_download_url"],
        accept="application/octet-stream",
    )
    text = resp.read().decode("utf-8", errors="replace")

    checksums = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Common formats:
        #   hash  filename          (GNU coreutils / monero / p2pool)
        #   hash *filename          (binary-mode indicator)
        parts = re.split(r"[\s*]+", line, maxsplit=1)
        if len(parts) == 2:
            maybe_hash, maybe_name = parts
            # Determine which part is the hash (always 64 hex chars for SHA-256)
            if re.fullmatch(r"[0-9a-fA-F]{64}", maybe_hash):
                checksums[maybe_name] = maybe_hash.lower()
            elif re.fullmatch(r"[0-9a-fA-F]{64}", maybe_name):
                checksums[maybe_hash] = maybe_name.lower()
    return checksums


def extract_binary(archive_path, software, dest_dir):
    """Extract the target binary from an archive into *dest_dir*.

    Handles ``.tar.gz``, ``.tar.bz2``, and ``.zip`` archives.  Walks the
    archive contents to locate the binary by name (from
    :data:`BINARY_NAMES`) regardless of directory nesting.

    Returns
    -------
    Path
        Absolute path to the extracted binary.

    Raises
    ------
    FileNotFoundError
        If the expected binary is not found inside the archive.
    """
    archive_path = Path(archive_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    system, _ = detect_platform()
    binary_name = BINARY_NAMES[software][system]

    with tempfile.TemporaryDirectory(prefix="xmrdp_extract_") as tmp:
        tmp_dir = Path(tmp)

        # --- Extract the archive ---
        name_lower = archive_path.name.lower()
        tmp_dir_resolved = tmp_dir.resolve()

        def _safe_tar_extract(tf):
            """Extract a tarfile with Zip Slip protection on all Python versions."""
            if sys.version_info >= (3, 12):
                tf.extractall(tmp_dir, filter="data")
            else:
                # Manual member-path validation for Python < 3.12 (F-06).
                for member in tf.getmembers():
                    member_path = (tmp_dir / member.name).resolve()
                    try:
                        member_path.relative_to(tmp_dir_resolved)
                    except ValueError:
                        raise RuntimeError(
                            f"Zip Slip detected in tar: {member.name!r} would "
                            "escape extraction directory"
                        )
                tf.extractall(tmp_dir)  # nosec B202 — all members validated by relative_to() above

        if name_lower.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                for member in zf.namelist():
                    member_path = (tmp_dir / member).resolve()
                    try:
                        member_path.relative_to(tmp_dir_resolved)
                    except ValueError:
                        raise RuntimeError(
                            f"Zip Slip detected: {member!r} would escape extraction directory"
                        )
                zf.extractall(tmp_dir)  # nosec B202 — all members validated by relative_to() above
        elif name_lower.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive_path, "r:gz") as tf:
                _safe_tar_extract(tf)
        elif name_lower.endswith((".tar.bz2", ".tbz2")):
            with tarfile.open(archive_path, "r:bz2") as tf:
                _safe_tar_extract(tf)
        elif name_lower.endswith(".tar"):
            with tarfile.open(archive_path, "r:") as tf:
                _safe_tar_extract(tf)
        else:
            raise ValueError(f"Unsupported archive format: {archive_path.name}")

        # --- Locate the binary inside the extracted tree ---
        found = None
        for root, _dirs, files in os.walk(tmp_dir):
            if binary_name in files:
                found = Path(root) / binary_name
                break

        if found is None:
            raise FileNotFoundError(
                f"Binary '{binary_name}' not found in archive {archive_path.name}"
            )

        dest_path = dest_dir / binary_name
        shutil.copy2(found, dest_path)
        make_executable(dest_path)

    return dest_path


def get_binary_path(software):
    """Return the cached binary path for *software*, or ``None``.

    Reads from the ``.versions.json`` cache and verifies the file exists
    on disk before returning.
    """
    versions = _read_versions()
    entry = versions.get(software)
    if entry is None:
        return None
    path = Path(entry.get("path", ""))
    if path.exists():
        return path
    return None


def ensure_binaries(config, force=False):
    """Download, verify, and cache all required binaries.

    Parameters
    ----------
    config : dict
        Deployment configuration.  May contain a ``versions`` key mapping
        software names to release tags (e.g. ``{"monero": "v0.18.3.4"}``).
        A value of ``"latest"`` (or absence) means fetch the latest release.
    force : bool
        If ``True``, re-download even when a cached version exists.

    Returns
    -------
    dict
        Mapping of ``{software: Path}`` to the binary on disk.
    """
    system, machine = detect_platform()
    bin_dir = get_binary_dir()
    versions = _read_versions()

    results = {}

    for software, repo in GITHUB_REPOS.items():
        binaries_cfg = config.get("binaries", {})
        key_map = {"monero": "monero_version", "p2pool": "p2pool_version", "xmrig": "xmrig_version"}
        desired_tag = binaries_cfg.get(key_map.get(software, f"{software}_version"))

        # Check cache
        cached = versions.get(software, {})
        cached_path = Path(cached.get("path", "")) if cached.get("path") else None
        if (
            not force
            and cached_path is not None
            and cached_path.exists()
        ):
            # If a specific version is requested, only accept a cache hit
            # when the cached version matches.
            if desired_tag and desired_tag != "latest":
                if cached.get("version") == desired_tag:
                    print(f"  {software}: {desired_tag} (cached)")
                    results[software] = cached_path
                    continue
            else:
                print(f"  {software}: {cached.get('version', '?')} (cached)")
                results[software] = cached_path
                continue

        # Fetch release metadata
        print(f"  {software}: fetching release info ...")
        release = get_latest_release(repo, tag=desired_tag)
        tag = release["tag_name"]
        assets = release["assets"]

        # Match the platform asset
        asset = match_asset(assets, software, system, machine)
        if asset is None:
            raise RuntimeError(
                f"No compatible {software} release asset found for "
                f"{system}/{machine} in {tag}"
            )

        # Prepare download directory
        sw_dir = bin_dir / software
        sw_dir.mkdir(parents=True, exist_ok=True)
        archive_dest = sw_dir / asset["name"]

        # Download
        print(f"  {software}: downloading {asset['name']} ...")
        download_binary(
            asset["browser_download_url"],
            archive_dest,
            expected_size=asset.get("size"),
        )

        # Checksum verification — wired to security.verify_checksums (F-08)
        verify = config.get("security", {}).get("verify_checksums", True)
        checksums = get_release_checksums(assets, software)
        if checksums:
            expected = checksums.get(asset["name"])
            if expected:
                print(f"  {software}: verifying SHA-256 ...")
                if not verify_checksum(archive_dest, expected):
                    archive_dest.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"Checksum verification failed for {asset['name']}"
                    )
                print(f"  {software}: checksum OK")
            elif verify:
                archive_dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"No checksum entry found for {asset['name']} in release checksums. "
                    "Set security.verify_checksums = false to skip (not recommended)."
                )
            else:
                print(f"  {software}: WARNING: no matching checksum entry, skipping verify")
        elif verify:
            archive_dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"No checksum file found in {software} release. "
                "Set security.verify_checksums = false to skip (not recommended)."
            )
        else:
            print(f"  {software}: WARNING: no checksum file in release, skipping verify")

        # Extract
        print(f"  {software}: extracting binary ...")
        binary_path = extract_binary(archive_dest, software, sw_dir)

        # Clean up the archive to save disk space
        archive_dest.unlink(missing_ok=True)

        # Update version cache
        versions[software] = {
            "version": tag,
            "path": str(binary_path),
        }
        _write_versions(versions)

        print(f"  {software}: {tag} ready")
        results[software] = binary_path

    return results
