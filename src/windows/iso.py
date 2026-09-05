import hashlib
import os
from enum import StrEnum
from pathlib import Path

import requests
from tqdm import tqdm


class WindowsVersion(StrEnum):
    """Supported Windows version enum for automated ISO download and provisioning."""

    WIN10_22H2 = "win10_22h2"
    WIN11_22H2 = "win11_22h2"
    WIN11_23H2 = "win11_23h2"
    WIN11_24H2 = "win11_24h2"
    WIN11_25H2 = "win11_25h2"
    SERVER_2022 = "server2022"
    SERVER_2025 = "server2025"


# Alias mapping from convenient user strings to canonical WindowsVersion enum members
VERSION_ALIAS_MAP: dict[str, WindowsVersion] = {
    "win10_22h2": WindowsVersion.WIN10_22H2,
    "10_22h2": WindowsVersion.WIN10_22H2,
    "win11_22h2": WindowsVersion.WIN11_22H2,
    "11_22h2": WindowsVersion.WIN11_22H2,
    "win11_23h2": WindowsVersion.WIN11_23H2,
    "23h2": WindowsVersion.WIN11_23H2,
    "win11_24h2": WindowsVersion.WIN11_24H2,
    "24h2": WindowsVersion.WIN11_24H2,
    "win11_25h2": WindowsVersion.WIN11_25H2,
    "25h2": WindowsVersion.WIN11_25H2,
    "2022": WindowsVersion.SERVER_2022,
    "server2022": WindowsVersion.SERVER_2022,
    "2025": WindowsVersion.SERVER_2025,
    "server2025": WindowsVersion.SERVER_2025,
}


# Version-specific Windows Evaluation and Mirror download URLs mapped by WindowsVersion Enum
WINDOWS_VERSION_URLS: dict[WindowsVersion, list[str]] = {
    WindowsVersion.WIN10_22H2: [
        "https://archive.org/download/Win10_22H2_English_x64/Win10_22H2_English_x64.iso",
        "https://software-static.download.prss.microsoft.com/sg/19045.2006.220908-0225.22h2_release_svc_refresh_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
    ],
    WindowsVersion.WIN11_22H2: [
        "https://archive.org/download/Win11_22H2_English_x64/Win11_22H2_English_x64.iso",
    ],
    WindowsVersion.WIN11_23H2: [
        "https://archive.org/download/Win11_23H2_English_x64/Win11_23H2_English_x64.iso",
        "https://software-static.download.prss.microsoft.com/db/22631.2428.231001-0608.23H2_NI_RELEASE_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
    ],
    WindowsVersion.WIN11_24H2: [
        "https://archive.org/download/Win11_24H2_English_x64/Win11_24H2_English_x64.iso",
        "https://software-static.download.prss.microsoft.com/db/26100.1.240331-1435.ge_release_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
    ],
    WindowsVersion.WIN11_25H2: [
        "https://archive.org/download/Win11_24H2_English_x64/Win11_24H2_English_x64.iso",
        "https://software-static.download.prss.microsoft.com/db/26200.1.240331-1435.ge_release_CLIENTENTERPRISEEVAL_OEMRET_x64FRE_en-us.iso",
    ],
    WindowsVersion.SERVER_2022: [
        "https://software-static.download.prss.microsoft.com/sg/20348.169.210806-2348.fe_release_svc_refresh_SERVER_EVAL_x64FRE_en-us.iso",
    ],
    WindowsVersion.SERVER_2025: [
        "https://software-static.download.prss.microsoft.com/db/26100.2.240331-1435.ge_release_SERVER_EVAL_x64FRE_en-us.iso",
    ],
}


def get_default_cache_dir() -> Path:
    """Return the default local ISO cache directory path."""
    env_cache = os.environ.get("WINDOWS_CACHE")
    if env_cache:
        cache_dir = Path(env_cache) / "isos"
    else:
        cache_dir = Path.home() / ".cache" / "windows" / "isos"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_image_cache_dir() -> Path:
    """Return the default local installed disk image cache directory path."""
    env_cache = os.environ.get("WINDOWS_CACHE")
    if env_cache:
        cache_dir = Path(env_cache) / "images"
    else:
        cache_dir = Path.home() / ".cache" / "windows" / "images"

    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def list_cached_isos(cache_dir: str | Path | None = None) -> list[Path]:
    """List all cached ISO files."""
    c_dir = Path(cache_dir) if cache_dir else get_default_cache_dir()
    if not c_dir.exists():
        return []
    return list(c_dir.glob("*.iso"))


def clear_iso_cache(cache_dir: str | Path | None = None) -> int:
    """Delete all cached ISO files. Return number of files deleted."""
    c_dir = Path(cache_dir) if cache_dir else get_default_cache_dir()
    if not c_dir.exists():
        return 0
    count = 0
    for iso_file in c_dir.glob("*.iso"):
        try:
            iso_file.unlink()
            count += 1
        except OSError:
            pass
    return count


def list_cached_images(cache_dir: str | Path | None = None) -> list[Path]:
    """List all cached installed qcow2 disk images."""
    c_dir = Path(cache_dir) if cache_dir else get_image_cache_dir()
    if not c_dir.exists():
        return []
    return list(c_dir.glob("*.qcow2"))


def clear_image_cache(cache_dir: str | Path | None = None) -> int:
    """Delete all cached installed qcow2 disk images. Return number of files deleted."""
    c_dir = Path(cache_dir) if cache_dir else get_image_cache_dir()
    if not c_dir.exists():
        return 0
    count = 0
    for img_file in c_dir.glob("*.qcow2"):
        try:
            img_file.unlink()
            count += 1
        except OSError:
            pass
    return count


def _download_file_with_progress(url: str, target_path: Path, show_progress: bool = True) -> None:
    """Download a remote file with optional tqdm progress bar and atomic temp file rename."""
    temp_path = target_path.with_suffix(".tmp")
    response = requests.get(url, stream=True, timeout=30)
    response.raise_for_status()

    total_size = int(response.headers.get("content-length", 0))
    block_size = 1024 * 1024  # 1MB chunk size

    if show_progress:
        with (
            open(temp_path, "wb") as f,
            tqdm(
                total=total_size,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"Downloading {target_path.name}",
            ) as progress_bar,
        ):
            for data in response.iter_content(block_size):
                size = f.write(data)
                progress_bar.update(size)
    else:
        with open(temp_path, "wb") as f:
            for data in response.iter_content(block_size):
                f.write(data)

    temp_path.rename(target_path)


def download_file(url: str, target_path: Path, show_progress: bool = True) -> Path:
    """Download a file from a URL to target_path with a tqdm progress bar."""
    _download_file_with_progress(url, target_path, show_progress=show_progress)
    return target_path


def _download_from_url(url: str, target_path: Path) -> Path:
    """Internal alias for download_file."""
    return download_file(url, target_path)


def create_dummy_iso(target_path: Path) -> Path:
    """Create a dummy ISO file for testing purposes."""
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"DUMMY_ISO_HEADER_DATA_" * 2000)
    return target_path


class ISO:
    """Represents a Windows installation ISO image."""

    def __init__(self, path: "ISO | str | Path | os.PathLike"):
        if isinstance(path, ISO):
            self.path = path.path
        else:
            self.path = Path(path).resolve()
        if not self.path.exists():
            raise FileNotFoundError(f"Specified local ISO file does not exist: {self.path}")

    @property
    def name(self) -> str:
        """Return the filename of the ISO file."""
        return self.path.name

    @property
    def stem(self) -> str:
        """Return the stem (filename without extension) of the ISO file."""
        return self.path.stem

    def exists(self) -> bool:
        """Return True if the ISO file exists."""
        return self.path.exists()

    def stat(self) -> os.stat_result:
        """Return the stat result for the ISO file."""
        return self.path.stat()

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"<ISO path={str(self.path)!r}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ISO):
            return self.path == other.path
        if isinstance(other, (str, Path, os.PathLike)):
            return self.path == Path(other).resolve()
        return False

    def __hash__(self) -> int:
        return hash(self.path)

    @classmethod
    def from_version(
        cls,
        version: WindowsVersion | str,
        cache_dir: str | Path | None = None,
        show_progress: bool = True,
    ) -> "ISO":
        """Fetch/download and return an ISO instance for the specified Windows version.

        Args:
            version: WindowsVersion enum member or version string alias (e.g. WindowsVersion.WIN10_22H2, "win11_24h2", "23h2").
            cache_dir: Optional custom ISO cache directory path.
            show_progress: Whether to display download progress bar.

        Returns:
            An ISO instance pointing to the cached/downloaded ISO file.
        """
        resolved_path = resolve_iso(version, cache_dir=cache_dir, show_progress=show_progress)
        return cls(resolved_path)

    @classmethod
    def from_url(
        cls,
        url: str,
        cache_dir: str | Path | None = None,
        show_progress: bool = True,
    ) -> "ISO":
        """Download and cache an ISO from a direct HTTP/HTTPS URL and return an ISO instance."""
        resolved_path = resolve_iso(url, cache_dir=cache_dir, show_progress=show_progress)
        return cls(resolved_path)

    @classmethod
    def resolve(
        cls,
        iso_path_or_version: "ISO | str | Path | WindowsVersion",
        cache_dir: str | Path | None = None,
        show_progress: bool = True,
    ) -> "ISO":
        """Resolve any ISO identifier (ISO instance, version, URL, or local path) to an ISO object."""
        if isinstance(iso_path_or_version, ISO):
            if not iso_path_or_version.exists():
                raise FileNotFoundError(f"Specified local ISO file does not exist: {iso_path_or_version.path}")
            return iso_path_or_version
        resolved_path = resolve_iso(iso_path_or_version, cache_dir=cache_dir, show_progress=show_progress)
        return cls(resolved_path)


def resolve_iso(
    iso_path_or_version: "ISO | str | Path | WindowsVersion",
    cache_dir: str | Path | None = None,
    show_progress: bool = True,
) -> Path:
    """Resolve an ISO parameter to a valid local Path.

    Accepts:
      - ISO instance
      - Path object to a local ISO file
      - String path to a local ISO file
      - WindowsVersion Enum (e.g. WindowsVersion.WIN10_22H2, WindowsVersion.WIN11_24H2)
      - HTTP/HTTPS direct ISO download URL
      - Full Windows version alias ('win10_22h2', 'win11_22h2', '23h2', '24h2', '25h2', '2022', '2025')

    If given a version alias or URL, checks local cache before downloading.
    """
    if isinstance(iso_path_or_version, ISO):
        iso_path_or_version = iso_path_or_version.path

    if isinstance(iso_path_or_version, Path):
        path = iso_path_or_version.resolve()
        if not path.exists():
            raise FileNotFoundError(f"Specified local ISO file does not exist: {path}")
        return path

    if isinstance(iso_path_or_version, WindowsVersion):
        version_enum = iso_path_or_version
        iso_str = version_enum.value
    else:
        iso_str = str(iso_path_or_version).strip()
        version_enum = None

    c_dir = Path(cache_dir) if cache_dir else get_default_cache_dir()
    c_dir.mkdir(parents=True, exist_ok=True)

    # Case A: String path to existing local file
    local_path = Path(iso_str)
    if local_path.exists() and local_path.is_file():
        return local_path.resolve()

    # Case B: HTTP/HTTPS Direct URL
    if iso_str.startswith("http://") or iso_str.startswith("https://"):
        filename = iso_str.split("/")[-1].split("?")[0]
        if not filename.endswith(".iso"):
            url_hash = hashlib.md5(iso_str.encode("utf-8")).hexdigest()[:8]
            filename = f"win_custom_{url_hash}.iso"

        cached_file = c_dir / filename
        if cached_file.exists() and cached_file.stat().st_size > 0:
            print(f"[windows] Using cached ISO file: {cached_file}")
            return cached_file

        # Check environment override
        env_url = os.environ.get("WINDOWS_25H2_ISO_URL") if "25" in iso_str else None
        target_url = env_url if env_url else iso_str

        print(f"[windows] Downloading ISO from {target_url}...")
        try:
            download_file(target_url, cached_file, show_progress=show_progress)
            return cached_file
        except Exception as exc:
            if cached_file.exists():
                cached_file.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download ISO from URL {target_url}: {exc}") from exc

    # Case C: Version key mapping via Enum or Alias
    clean_key = iso_str.lower()
    if not version_enum:
        version_enum = VERSION_ALIAS_MAP.get(clean_key)

    if not version_enum or version_enum not in WINDOWS_VERSION_URLS:
        available_versions = ", ".join(v.value for v in WINDOWS_VERSION_URLS)
        raise ValueError(
            f"Unknown Windows version alias '{iso_str}' is not a recognized Windows version. Supported full versions: {available_versions}"
        )

    urls = WINDOWS_VERSION_URLS[version_enum]
    filename = f"win_{version_enum.value}.iso"
    cached_file = c_dir / filename

    if cached_file.exists() and cached_file.stat().st_size > 0:
        print(f"[windows] Using cached ISO file for Windows '{version_enum.value}': {cached_file}")
        return cached_file

    # Environment variable override check (e.g. WINDOWS_WIN11_25H2_ISO_URL or WINDOWS_25H2_ISO_URL)
    env_var_names = [
        f"WINDOWS_{version_enum.name}_ISO_URL",
        f"WINDOWS_{version_enum.value.upper()}_ISO_URL",
        f"WINDOWS_{clean_key.upper()}_ISO_URL",
    ]
    for env_name in env_var_names:
        env_override_url = os.environ.get(env_name)
        if env_override_url:
            urls = [env_override_url, *urls]
            break

    print(f"[windows] Resolving ISO for Windows version '{version_enum.value}'...")
    errors: list[str] = []

    for url in urls:
        try:
            print(f"[windows] Downloading from primary source: {url}")
            download_file(url, cached_file, show_progress=show_progress)
            return cached_file
        except Exception as exc:
            errors.append(f"Source {url} failed: {exc}")
            if cached_file.exists():
                cached_file.unlink(missing_ok=True)
            print(f"[windows] Source failed ({exc}). Trying next fallback URL if available...")

    error_summary = "\n".join(errors)
    raise ValueError(
        f"Failed to download Windows ISO for version '{version_enum.value}' from all available sources:\n{error_summary}"
    )
