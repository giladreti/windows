"""Offline disk inspection, partition analysis, and NTFS file read/write controller for Windows disk images."""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from windows.file import ExistPolicy, RemoteFile

if TYPE_CHECKING:
    from windows.image import Image


def _find_binary(name: str) -> str | None:
    """Find binary executable in PATH or standard system directories."""
    path = shutil.which(name)
    if path:
        return path
    if sys.platform == "win32":
        path = shutil.which(name + ".exe")
        if path:
            return path
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / f"{name}.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "7-Zip" / f"{name}.exe",
            Path(r"C:\ProgramData\chocolatey\bin") / f"{name}.exe",
            Path.home() / "scoop" / "shims" / f"{name}.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None
    for d in ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin", "/usr/local/sbin"):
        candidate = Path(d) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def normalize_ntfs_path(path: str | Path | PureWindowsPath) -> str:
    """Normalize Windows file path (e.g. 'C:\\Windows\\System32' -> 'Windows/System32')."""
    p = str(path).strip().replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = p[2:]
    return p.lstrip("/\\")


@dataclass
class PartitionInfo:
    """Metadata describing a disk partition inside an image."""

    index: int
    offset: int
    size: int
    type_name: str = "NTFS"
    is_bootable: bool = False
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset": self.offset,
            "size": self.size,
            "type": self.type_name,
            "bootable": self.is_bootable,
            "label": self.label,
        }

    def __repr__(self) -> str:
        size_gb = self.size / (1024**3)
        return (
            f"<Partition #{self.index} type={self.type_name!r} offset={self.offset} "
            f"size={size_gb:.2f}GB bootable={self.is_bootable}>"
        )


class MountedPartition:
    """Active FUSE-mounted NTFS partition view providing file listing, reading, and writing."""

    def __init__(
        self,
        mount_path: Path,
        partition_info: PartitionInfo,
        process: subprocess.Popen | None,
        writable: bool = False,
    ):
        self.mount_path = mount_path
        self.partition_info = partition_info
        self._process = process
        self.writable = writable

    def list_entries(self, path: str = "") -> dict[str, bool]:
        """Return a mapping of {entry_name: is_directory} for direct children in partition folder."""
        norm_path = normalize_ntfs_path(path)
        entries: dict[str, bool] = {}

        ntfsls_bin = _find_binary("ntfsls")
        if ntfsls_bin:
            cmd = [ntfsls_bin, "-F"]
            if norm_path:
                cmd.extend(["-p", norm_path])
            cmd.append(str(self.mount_path))
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if not line or line in ("./", "../", ".", ".."):
                        continue
                    if line.startswith("$"):
                        continue
                    if line.endswith("/"):
                        entries[line[:-1]] = True
                    else:
                        entries[line] = False
                return entries

        seven_z = _find_binary("7z")
        if seven_z:
            target_pattern = f"{norm_path}/*" if norm_path else "*"
            cmd = [seven_z, "l", "-slt", str(self.mount_path), target_pattern]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                prefix = f"{norm_path}/" if norm_path else ""
                current_path = ""
                is_folder = False
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("Path = "):
                        current_path = line[7:].strip()
                        is_folder = False
                    elif line.startswith("Folder = "):
                        is_folder = line[9:].strip() == "+"
                    elif not line and current_path:
                        if not current_path.startswith("[SYSTEM]"):
                            if prefix and current_path.startswith(prefix):
                                rel = current_path[len(prefix) :]
                            else:
                                rel = current_path
                            if "/" in rel:
                                child = rel.split("/")[0]
                                entries[child] = True
                            elif rel:
                                entries[rel] = is_folder
                        current_path = ""
                if current_path and not current_path.startswith("[SYSTEM]"):
                    if prefix and current_path.startswith(prefix):
                        rel = current_path[len(prefix) :]
                    else:
                        rel = current_path
                    if "/" in rel:
                        child = rel.split("/")[0]
                        entries[child] = True
                    elif rel:
                        entries[rel] = is_folder
                return entries

        return entries

    def list_files(self, path: str = "", recursive: bool = False) -> list[str]:
        """List files and directories in the specified partition folder."""
        norm_path = normalize_ntfs_path(path)

        if not recursive:
            entries = self.list_entries(norm_path)
            if entries:
                return list(entries.keys())

        ntfsls_bin = _find_binary("ntfsls")
        if ntfsls_bin:
            cmd = [ntfsls_bin]
            if recursive:
                cmd.append("-R")
            if norm_path:
                cmd.extend(["-p", norm_path])
            cmd.append(str(self.mount_path))
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                return [line for line in lines if line not in (".", "..") and not line.startswith("$")]

        seven_z = _find_binary("7z")
        if seven_z:
            target_pattern = f"{norm_path}/*" if norm_path else "*"
            cmd = [seven_z, "l", "-ba", str(self.mount_path)]
            if norm_path:
                cmd.append(target_pattern)
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0:
                results = []
                for line in res.stdout.splitlines():
                    parts = line.split(maxsplit=5)
                    if len(parts) >= 6:
                        name = parts[5].strip()
                        if not name.startswith("[SYSTEM]"):
                            results.append(name)
                return results

        return []

    def read_bytes(self, path: str) -> bytes:
        """Read raw bytes of a file inside the partition."""
        norm_path = normalize_ntfs_path(path)

        ntfscat_bin = _find_binary("ntfscat")
        if ntfscat_bin:
            cmd = [ntfscat_bin, str(self.mount_path), norm_path]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0:
                return res.stdout

        seven_z = _find_binary("7z")
        if seven_z:
            cmd = [seven_z, "e", "-so", str(self.mount_path), norm_path]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode == 0 and len(res.stdout) > 0:
                return res.stdout

        raise FileNotFoundError(f"File '{path}' was not found in partition #{self.partition_info.index}.")

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        """Read text content of a file inside the partition."""
        return self.read_bytes(path).decode(encoding, errors="replace")

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write raw bytes to a file inside the partition."""
        if not self.writable:
            raise PermissionError("Cannot write to image partition: mounted read-only. Open with writable=True.")

        norm_path = normalize_ntfs_path(path)
        ntfscp_bin = _find_binary("ntfscp")
        if not ntfscp_bin:
            raise RuntimeError("ntfscp binary not found. Writing to offline NTFS images requires ntfsprogs / ntfs-3g.")

        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            tf_path = tf.name

        try:
            cmd = [ntfscp_bin, str(self.mount_path), tf_path, norm_path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to write '{path}' to image partition: {res.stderr.strip()}")
        finally:
            if os.path.exists(tf_path):
                os.unlink(tf_path)

    def write_text(self, path: str, text: str, encoding: str = "utf-8") -> None:
        """Write string text to a file inside the partition."""
        self.write_bytes(path, text.encode(encoding))

    def is_dir(self, path: str) -> bool:
        """Return True if path is a directory inside the partition."""
        norm = normalize_ntfs_path(path)
        if not norm:
            return True

        parent = str(Path(norm).parent)
        if parent == ".":
            parent = ""
        base = Path(norm).name

        entries = self.list_entries(parent)
        for name, is_directory in entries.items():
            if name.lower() == base.lower():
                return is_directory

        ntfsls_bin = _find_binary("ntfsls")
        if ntfsls_bin:
            res = subprocess.run([ntfsls_bin, "-F", "-p", norm, str(self.mount_path)], capture_output=True, text=True)
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
                return any(line in ("./", "../") or line.endswith("/") for line in lines)

        return False

    def is_file(self, path: str) -> bool:
        """Return True if path is a regular file inside the partition."""
        norm = normalize_ntfs_path(path)
        if not norm:
            return False

        parent = str(Path(norm).parent)
        if parent == ".":
            parent = ""
        base = Path(norm).name

        entries = self.list_entries(parent)
        for name, is_directory in entries.items():
            if name.lower() == base.lower():
                return not is_directory

        try:
            self.read_bytes(path)
            return True
        except Exception:
            return False

    def exists(self, path: str) -> bool:
        """Return True if the specified file or directory exists in the partition."""
        norm = normalize_ntfs_path(path)
        if not norm:
            return True

        parent = str(Path(norm).parent)
        if parent == ".":
            parent = ""
        base = Path(norm).name

        entries = self.list_entries(parent)
        for name in entries:
            if name.lower() == base.lower():
                return True

        return self.is_dir(path) or self.is_file(path)

    def close(self) -> None:
        """Unmount FUSE partition and terminate storage daemon."""
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2.0)
            except Exception:
                self._process.kill()
            self._process = None

        if self.mount_path.exists():
            for unmount_tool in ("fusermount3", "fusermount"):
                bin_path = _find_binary(unmount_tool)
                if bin_path:
                    subprocess.run([bin_path, "-u", str(self.mount_path)], capture_output=True)
            if self.mount_path.exists():
                try:
                    self.mount_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def __enter__(self) -> "MountedPartition":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


class ImagePath:
    """Path-like interface for offline Windows disk image files and directories matching RemotePath."""

    def __init__(self, controller: "ImageFileController", remote_path: str | Path | PureWindowsPath):
        self.controller = controller
        self._path = PureWindowsPath(remote_path)
        self.remote_path = str(self._path)
        self.clean_path = str(self._path)

    def __truediv__(self, child: str | Path | PureWindowsPath) -> "ImagePath":
        return ImagePath(self.controller, self._path / child)

    @property
    def name(self) -> str:
        """The final path component."""
        clean = self.clean_path.rstrip("\\/")
        if not clean or clean.endswith(":"):
            return ""
        return PureWindowsPath(clean).name

    @property
    def stem(self) -> str:
        """The final path component without suffix."""
        return PureWindowsPath(self.name).stem

    @property
    def suffix(self) -> str:
        """The path extension."""
        return PureWindowsPath(self.name).suffix

    @property
    def parent(self) -> "ImagePath":
        """The logical parent of the path."""
        clean = self.clean_path.rstrip("\\/")
        p = str(PureWindowsPath(clean).parent)
        if clean.startswith("C:") and not p.startswith("C:"):
            p = f"C:\\{p}".replace("C:\\.", "C:\\")
        return ImagePath(self.controller, p)

    @property
    def parts(self) -> tuple[str, ...]:
        """Sequence of path components."""
        return self._path.parts

    def as_posix(self) -> str:
        """Return path with forward slashes."""
        return self._path.as_posix()

    def exists(self) -> bool:
        """Check if path exists in the offline disk image partition."""
        with self.controller._open_mount(writable=False) as mnt:
            return mnt.exists(self.remote_path)

    def is_dir(self) -> bool:
        """Check if path is a directory."""
        with self.controller._open_mount(writable=False) as mnt:
            return mnt.is_dir(self.remote_path)

    def is_file(self) -> bool:
        """Check if path is a regular file."""
        with self.controller._open_mount(writable=False) as mnt:
            return mnt.is_file(self.remote_path)

    def iterdir(self) -> list["ImagePath"]:
        """Iterate over the files and subdirectories in this offline directory."""
        with self.controller._open_mount(writable=False) as mnt:
            if not mnt.exists(self.remote_path):
                raise FileNotFoundError(f"Offline path does not exist: {self.remote_path}")
            if not mnt.is_dir(self.remote_path):
                raise NotADirectoryError(f"Offline path is not a directory: {self.remote_path}")

            entries = mnt.list_entries(self.remote_path)
            return [self / child_name for child_name in entries]

    def read_bytes(self) -> bytes:
        """Read bytes directly from offline file."""
        with self.controller._open_mount(writable=False) as mnt:
            return mnt.read_bytes(self.remote_path)

    def read_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        """Read text directly from offline file."""
        return self.read_bytes().decode(encoding, errors=errors)

    def write_bytes(self, data: bytes, exist_policy: ExistPolicy = "overwrite") -> None:
        """Write raw bytes directly to offline file."""
        if exist_policy == "abort" and self.exists():
            raise FileExistsError(f"Target offline path already exists: {self.remote_path}")
        with self.controller._open_mount(writable=True) as mnt:
            mnt.write_bytes(self.remote_path, data)

    def write_text(self, text: str, encoding: str = "utf-8", exist_policy: ExistPolicy = "overwrite") -> None:
        """Write text directly to offline file."""
        self.write_bytes(text.encode(encoding), exist_policy=exist_policy)

    def download(
        self,
        local_path: str | Path | None = None,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> RemoteFile | Path:
        """Download file or directory from offline image to host."""
        if not self.exists():
            raise FileNotFoundError(f"Offline path not found: {self.remote_path}")

        if self.is_dir():
            if local_path is None:
                target_dir = Path.cwd() / (self.name or "downloaded_dir")
            else:
                target_dir = Path(local_path).resolve()

            if target_dir.exists():
                if exist_policy == "abort":
                    raise FileExistsError(f"Target local path already exists: {target_dir}")
                elif exist_policy == "overwrite":
                    if target_dir.is_dir():
                        shutil.rmtree(target_dir)
                    else:
                        target_dir.unlink()
                    target_dir.mkdir(parents=True, exist_ok=True)
            else:
                target_dir.mkdir(parents=True, exist_ok=True)

            for child in self.iterdir():
                child.download(target_dir / child.name, exist_policy=exist_policy, show_progress=show_progress)
            return target_dir
        else:
            data = self.read_bytes()
            if local_path is not None:
                target_file = Path(local_path).resolve()
                if target_file.exists() and exist_policy == "abort":
                    raise FileExistsError(f"Target local file already exists: {target_file}")
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_bytes(data)
                return target_file
            return RemoteFile(self.remote_path, data)

    def download_dir(
        self,
        local_dir: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> Path:
        """Download offline directory to local directory."""
        target = Path(local_dir)
        res = self.download(local_path=target, exist_policy=exist_policy, show_progress=show_progress)
        if isinstance(res, RemoteFile):
            res.save(target)
            return target
        return res

    def upload(
        self,
        local_path: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> None:
        """Upload local file or directory into this offline image path."""
        source = Path(local_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Local path does not exist: {local_path}")

        if source.is_file():
            self.write_bytes(source.read_bytes(), exist_policy=exist_policy)
        elif source.is_dir():
            for item in source.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(source)
                    target_path = self / str(rel)
                    target_path.write_bytes(item.read_bytes(), exist_policy=exist_policy)

    def upload_dir(
        self,
        local_dir: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> None:
        """Upload local directory into this offline image path."""
        source = Path(local_dir)
        if not source.is_dir():
            raise ValueError(f"local_dir must be a directory: {local_dir}")
        self.upload(source, exist_policy=exist_policy, show_progress=show_progress)

    def __repr__(self) -> str:
        return f"<ImagePath '{self.clean_path}'>"

    def __str__(self) -> str:
        return self.clean_path


class ImageFileController:
    """Controller for offline file operations on Windows disk images providing pathlib.Path-style ImagePath interface."""

    def __init__(self, image: "Image", partition: int | None = None):
        self._image = image
        self._partition = partition
        self._active_mount: MountedPartition | None = None
        self._active_writable: bool = False
        self._mount_count: int = 0

    @property
    def image(self) -> "Image":
        return self._image

    @property
    def partition_index(self) -> int | None:
        return self._partition

    def partition(self, index: int) -> "ImageFileController":
        """Return an ImageFileController scoped to a specific partition index."""
        return ImageFileController(self._image, partition=index)

    def path(self, remote_path: str | Path | PureWindowsPath) -> ImagePath:
        """Create an ImagePath object for an offline Windows image path."""
        return ImagePath(self, remote_path)

    def __truediv__(self, remote_path: str | Path | PureWindowsPath) -> ImagePath:
        """Support image.file / 'C:\\path' syntax."""
        return self.path(remote_path)

    def __call__(
        self,
        target: int | str | Path | PureWindowsPath | None = None,
        partition: int | None = None,
    ) -> Any:
        """Support image.file('C:\\path'), image.file(2), or image.file(partition=2)."""
        if partition is not None:
            return self.partition(partition)
        if isinstance(target, int):
            return self.partition(target)
        if isinstance(target, (str, Path, PureWindowsPath)):
            return self.path(target)
        return self

    def __enter__(self) -> "ImageFileController":
        """Open persistent FUSE mount for batch operations."""
        if self._active_mount is None:
            try:
                self._active_mount = self._image.mount(partition=self._partition, writable=True)
                self._active_writable = True
            except Exception:
                self._active_mount = self._image.mount(partition=self._partition, writable=False)
                self._active_writable = False
        self._mount_count += 1
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Close persistent FUSE mount."""
        self._mount_count -= 1
        if self._mount_count <= 0 and self._active_mount is not None:
            try:
                self._active_mount.close()
            finally:
                self._active_mount = None
                self._active_writable = False
                self._mount_count = 0

    @contextmanager
    def _open_mount(self, writable: bool = False):
        """Context manager providing an active mount (reusing active or opening transient)."""
        if self._active_mount is not None:
            if not writable or self._active_writable:
                yield self._active_mount
                return
        with self._image.mount(partition=self._partition, writable=writable) as mnt:
            yield mnt

    def download(
        self,
        remote_path: str | Path | PureWindowsPath,
        local_path: str | Path | None = None,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> RemoteFile | Path:
        """Download a file or directory from the offline image."""
        return self.path(remote_path).download(
            local_path=local_path, exist_policy=exist_policy, show_progress=show_progress
        )

    def download_dir(
        self,
        remote_dir: str | Path | PureWindowsPath,
        local_dir: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> Path:
        """Download a directory from the offline image."""
        return self.path(remote_dir).download_dir(
            local_dir=local_dir, exist_policy=exist_policy, show_progress=show_progress
        )

    def upload(
        self,
        local_path: str | Path,
        remote_path: str | Path | PureWindowsPath,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> None:
        """Upload a local file or directory into the offline image."""
        self.path(remote_path).upload(local_path, exist_policy=exist_policy, show_progress=show_progress)

    def upload_dir(
        self,
        local_dir: str | Path,
        remote_dir: str | Path | PureWindowsPath,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = False,
    ) -> None:
        """Upload a local directory into the offline image."""
        self.path(remote_dir).upload_dir(local_dir, exist_policy=exist_policy, show_progress=show_progress)

    def __repr__(self) -> str:
        part_str = f" partition={self._partition}" if self._partition is not None else ""
        return f"<ImageFileController image={self._image.name!r}{part_str}>"


class DiskController:
    """Controller for inspecting partitions, listing files, and reading/writing files from offline disk images."""

    def __init__(self, image: "Image"):
        self._image = image
        self._file: ImageFileController | None = None

    @property
    def disk_path(self) -> Path:
        return self._image.disk_path

    @property
    def file(self) -> ImageFileController:
        """Path-like file controller for offline disk operations."""
        if self._file is None:
            self._file = ImageFileController(self._image)
        return self._file

    def partitions(self) -> list[PartitionInfo]:
        """Scan and return all MBR or GPT partitions present in the disk image."""
        qemu_img = _find_binary("qemu-img") or "qemu-img"
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
            mbr_tmp = tf.name

        try:
            cmd = [qemu_img, "dd", f"if={self.disk_path}", f"of={mbr_tmp}", "bs=512", "count=34"]
            res = subprocess.run(cmd, capture_output=True)
            if res.returncode != 0 or not os.path.exists(mbr_tmp):
                return []
            first_sectors = Path(mbr_tmp).read_bytes()
        finally:
            if os.path.exists(mbr_tmp):
                os.unlink(mbr_tmp)

        if len(first_sectors) < 512:
            return []

        boot_sig = struct.unpack("<H", first_sectors[510:512])[0]
        if boot_sig != 0xAA55:
            size = self.disk_path.stat().st_size
            return [PartitionInfo(index=1, offset=0, size=size, type_name="RAW")]

        part0_type = first_sectors[446 + 4]
        if part0_type == 0xEE and len(first_sectors) >= 34 * 512:
            gpt_hdr = first_sectors[512:1024]
            if gpt_hdr[:8] == b"EFI PART":
                _, _, _, _, _, _, _, _part_entry_lba, num_entries, entry_size, _ = struct.unpack(
                    "<8sIIIIQQQQII", gpt_hdr[0:56]
                )
                entries_offset = 2 * 512
                gpt_parts = []
                for i in range(min(num_entries, 128)):
                    e = first_sectors[entries_offset + i * entry_size : entries_offset + (i + 1) * entry_size]
                    if len(e) < entry_size or e[:16] == b"\x00" * 16:
                        continue
                    first_lba, last_lba = struct.unpack("<QQ", e[32:48])
                    offset = first_lba * 512
                    size = (last_lba - first_lba + 1) * 512
                    name = e[56:128].decode("utf-16le", errors="ignore").rstrip("\x00")
                    gpt_parts.append(
                        PartitionInfo(
                            index=i + 1,
                            offset=offset,
                            size=size,
                            type_name="NTFS",
                            label=name,
                        )
                    )
                if gpt_parts:
                    return gpt_parts

        mbr_parts = []
        for i in range(4):
            entry = first_sectors[446 + i * 16 : 446 + (i + 1) * 16]
            boot, _, p_type, _, lba_start, num_sectors = struct.unpack("<B3sB3sII", entry)
            if p_type != 0 and num_sectors > 0:
                type_name = "NTFS" if p_type in (0x07, 0x17) else f"0x{p_type:02x}"
                mbr_parts.append(
                    PartitionInfo(
                        index=i + 1,
                        offset=lba_start * 512,
                        size=num_sectors * 512,
                        type_name=type_name,
                        is_bootable=bool(boot & 0x80),
                    )
                )

        if mbr_parts:
            return mbr_parts

        size = self.disk_path.stat().st_size
        return [PartitionInfo(index=1, offset=0, size=size, type_name="RAW")]

    def get_default_partition(self, partition: int | None = None) -> PartitionInfo:
        """Return the target partition (defaults to the largest NTFS/Windows partition)."""
        parts = self.partitions()
        if not parts:
            return PartitionInfo(index=1, offset=0, size=self.disk_path.stat().st_size)

        if partition is not None:
            for p in parts:
                if p.index == partition:
                    return p
            raise ValueError(f"Partition #{partition} not found. Available partitions: {[p.index for p in parts]}")

        return max(parts, key=lambda p: p.size)

    def mount(self, partition: int | None = None, writable: bool = False) -> MountedPartition:
        """Mount the specified partition as a virtual block device via FUSE.

        Args:
            partition: Partition number (1-indexed). Defaults to largest Windows partition.
            writable: Whether to allow write modifications into the disk image.

        Returns:
            A `MountedPartition` instance supporting file listing, reading, and writing.
        """
        part_info = self.get_default_partition(partition)
        qsd_bin = _find_binary("qemu-storage-daemon")
        if not qsd_bin:
            raise RuntimeError(
                "qemu-storage-daemon binary is required for mounting offline disk partitions. "
                "Please install qemu-utils / qemu-system."
            )

        uid = uuid.uuid4().hex[:8]
        mount_file = Path(tempfile.gettempdir()) / f"win_part_{uid}.raw"
        mount_file.touch(mode=0o600, exist_ok=True)

        is_qcow2 = self.disk_path.suffix.lower() in (".qcow2", ".qcow") or self._detect_format() == "qcow2"
        driver = "qcow2" if is_qcow2 else "raw"

        node_file = f"f_{uid}"
        node_fmt = f"d_{uid}"
        node_part = f"p_{uid}"
        exp_id = f"exp_{uid}"
        ro_str = "off" if writable else "on"
        w_str = "on" if writable else "off"

        cmd = [
            qsd_bin,
            "--blockdev",
            f"driver=file,node-name={node_file},filename={self.disk_path},read-only={ro_str}",
            "--blockdev",
            f"driver={driver},node-name={node_fmt},file={node_file},read-only={ro_str}",
        ]

        if part_info.offset > 0 or part_info.size > 0:
            cmd.extend(
                [
                    "--blockdev",
                    f"driver=raw,node-name={node_part},offset={part_info.offset},size={part_info.size},file={node_fmt},read-only={ro_str}",
                    "--export",
                    f"type=fuse,id={exp_id},node-name={node_part},mountpoint={mount_file},writable={w_str}",
                ]
            )
        else:
            cmd.extend(
                [
                    "--export",
                    f"type=fuse,id={exp_id},node-name={node_fmt},mountpoint={mount_file},writable={w_str}",
                ]
            )

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        start = time.time()
        mounted = False
        while time.time() - start < 5.0:
            if proc.poll() is not None:
                _, stderr = proc.communicate()
                mount_file.unlink(missing_ok=True)
                raise RuntimeError(f"qemu-storage-daemon failed to start: {stderr.decode(errors='replace')}")
            try:
                if mount_file.exists() and os.path.getsize(mount_file) > 0:
                    mounted = True
                    break
            except OSError:
                pass
            time.sleep(0.1)

        if not mounted:
            proc.terminate()
            mount_file.unlink(missing_ok=True)
            raise RuntimeError("Timed out waiting for FUSE partition export to activate.")

        return MountedPartition(
            mount_path=mount_file,
            partition_info=part_info,
            process=proc,
            writable=writable,
        )

    def _detect_format(self) -> str:
        try:
            with open(self.disk_path, "rb") as f:
                header = f.read(4)
                if header == b"QFI\xfb":
                    return "qcow2"
        except Exception:
            pass
        return "raw"

    def list_files(self, path: str = "", partition: int | None = None, recursive: bool = False) -> list[str]:
        """List files inside a folder in the offline image partition."""
        with self.mount(partition=partition, writable=False) as mnt:
            return mnt.list_files(path=path, recursive=recursive)

    def read_bytes(self, path: str, partition: int | None = None) -> bytes:
        """Read binary contents of a file inside the offline image partition."""
        with self.mount(partition=partition, writable=False) as mnt:
            return mnt.read_bytes(path)

    def read_text(self, path: str, encoding: str = "utf-8", partition: int | None = None) -> str:
        """Read text contents of a file inside the offline image partition."""
        with self.mount(partition=partition, writable=False) as mnt:
            return mnt.read_text(path, encoding=encoding)

    def write_bytes(self, path: str, data: bytes, partition: int | None = None) -> None:
        """Write binary data to a file inside the offline image partition."""
        with self.mount(partition=partition, writable=True) as mnt:
            mnt.write_bytes(path, data)

    def write_text(self, path: str, text: str, encoding: str = "utf-8", partition: int | None = None) -> None:
        """Write text to a file inside the offline image partition."""
        with self.mount(partition=partition, writable=True) as mnt:
            mnt.write_text(path, text, encoding=encoding)

    def exists(self, path: str, partition: int | None = None) -> bool:
        """Return True if the specified file or directory exists in the offline image partition."""
        with self.mount(partition=partition, writable=False) as mnt:
            return mnt.exists(path)
