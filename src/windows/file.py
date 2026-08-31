"""File transfer controller for remote Windows QEMU VMs providing pathlib.Path-style RemotePath interface over QGA."""

import json
import shutil
from pathlib import Path
from typing import Literal

from tqdm import tqdm

from windows.executor import CommandController

ExistPolicy = Literal["overwrite", "merge", "abort"]


class RemoteFile:
    """Represents a file downloaded from the remote guest VM."""

    def __init__(self, remote_path: str, content: bytes):
        self.remote_path = remote_path
        self._content = content

    def read_bytes(self) -> bytes:
        """Return the raw byte contents of the file."""
        return self._content

    def read_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        """Return text contents of the file decoded with specified encoding."""
        return self._content.decode(encoding, errors=errors)

    def save(self, local_path: str | Path) -> Path:
        """Save the downloaded file contents to a local path."""
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self._content)
        return target


class RemotePath:
    """Path-like interface for guest VM files and directories using QEMU Guest Agent."""

    def __init__(self, controller: "FileController", remote_path: str):
        self.controller = controller
        self.remote_path = str(remote_path)
        self.clean_path = self.remote_path.replace("/", "\\")

    def __truediv__(self, child: str) -> "RemotePath":
        joined = f"{self.remote_path}\\{child}" if "\\" in self.remote_path else f"{self.remote_path}/{child}"
        return RemotePath(self.controller, joined)

    def exists(self) -> bool:
        """Check if remote path exists on guest VM."""
        try:
            res = self.controller.cmd.run(f"Test-Path -LiteralPath '{self.remote_path}'", auto_retry=False)
            return res.stdout.strip().lower() == "true"
        except Exception:
            return False

    def is_dir(self) -> bool:
        """Check if remote path is a directory."""
        try:
            res = self.controller.cmd.run(
                f"if (Test-Path -LiteralPath '{self.remote_path}') {{ (Get-Item -LiteralPath '{self.remote_path}').PSIsContainer }} else {{ 'False' }}",
                auto_retry=False,
            )
            return res.stdout.strip().lower() == "true"
        except Exception:
            return False

    def is_file(self) -> bool:
        """Check if remote path is a regular file."""
        try:
            res = self.controller.cmd.run(
                f"if (Test-Path -LiteralPath '{self.remote_path}') {{ -not (Get-Item -LiteralPath '{self.remote_path}').PSIsContainer }} else {{ 'False' }}",
                auto_retry=False,
            )
            return res.stdout.strip().lower() == "true"
        except Exception:
            return False

    def read_bytes(self) -> bytes:
        """Read bytes directly from remote file via QGA."""
        try:
            return self.controller.cmd.qga.read_file(self.remote_path)
        except Exception as exc:
            raise FileNotFoundError(f"Remote file not found or inaccessible: {self.remote_path}") from exc

    def read_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        """Read text directly from remote file."""
        return self.read_bytes().decode(encoding, errors=errors)

    def write_bytes(self, data: bytes, exist_policy: ExistPolicy = "overwrite") -> None:
        """Write raw bytes directly to remote file via QGA."""
        if self.exists() and exist_policy == "abort":
            raise FileExistsError(f"Target remote path already exists: {self.remote_path}")

        parent = str(Path(self.clean_path).parent)
        if parent and parent not in (".", "\\"):
            self.controller.cmd.run(f"New-Item -ItemType Directory -Force -Path '{parent}'", auto_retry=False)

        self.controller.cmd.qga.write_file(self.remote_path, data)

    def write_text(self, text: str, encoding: str = "utf-8", exist_policy: ExistPolicy = "overwrite") -> None:
        """Write text directly to remote file."""
        self.write_bytes(text.encode(encoding), exist_policy=exist_policy)

    def upload(
        self,
        local_path: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        chunk_size: int = 250000,
        show_progress: bool = True,
    ) -> None:
        """Upload local file or directory to this remote path."""
        source = Path(local_path).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Local path does not exist: {local_path}")

        if source.is_file():
            if self.exists():
                if exist_policy == "abort":
                    raise FileExistsError(f"Target remote path already exists: {self.remote_path}")
                elif exist_policy == "overwrite" and self.is_dir():
                    self.controller.cmd.run(
                        f"Remove-Item -LiteralPath '{self.remote_path}' -Recurse -Force", auto_retry=False
                    )

            if show_progress:
                with tqdm(
                    total=source.stat().st_size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=f"Uploading {source.name}",
                    leave=True,
                ) as pbar:
                    self.write_bytes(source.read_bytes(), exist_policy=exist_policy)
                    pbar.update(source.stat().st_size)
            else:
                self.write_bytes(source.read_bytes(), exist_policy=exist_policy)

        elif source.is_dir():
            if self.exists():
                if exist_policy == "abort":
                    raise FileExistsError(f"Target remote path already exists: {self.remote_path}")
                elif exist_policy == "overwrite":
                    if self.is_dir():
                        self.controller.cmd.run(
                            f"Remove-Item -LiteralPath '{self.remote_path}' -Recurse -Force", auto_retry=False
                        )
                    self.controller.cmd.run(
                        f"New-Item -ItemType Directory -Force -Path '{self.remote_path}'", auto_retry=False
                    )
                elif exist_policy == "merge" and not self.is_dir():
                    raise ValueError(f"Cannot merge directory into remote file: {self.remote_path}")
            else:
                self.controller.cmd.run(
                    f"New-Item -ItemType Directory -Force -Path '{self.remote_path}'", auto_retry=False
                )

            all_items = list(source.rglob("*"))
            if show_progress:
                items_iter = tqdm(all_items, desc=f"Uploading {source.name}", leave=False)
            else:
                items_iter = all_items

            for item in items_iter:
                rel = item.relative_to(source)
                r_item_target = f"{self.remote_path}\\{rel}".replace("/", "\\")
                if item.is_dir():
                    self.controller.cmd.run(
                        f"New-Item -ItemType Directory -Force -Path '{r_item_target}'", auto_retry=False
                    )
                else:
                    if exist_policy == "abort":
                        check = self.controller.cmd.run(f"Test-Path -LiteralPath '{r_item_target}'", auto_retry=False)
                        if check.stdout.strip().lower() == "true":
                            raise FileExistsError(f"Target remote file already exists: {r_item_target}")

                    r_parent = str(Path(r_item_target).parent)
                    self.controller.cmd.run(f"New-Item -ItemType Directory -Force -Path '{r_parent}'", auto_retry=False)
                    self.controller.cmd.qga.write_file(r_item_target, item.read_bytes())

    def upload_dir(
        self,
        local_dir: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = True,
    ) -> None:
        """Upload local directory to this remote path."""
        source = Path(local_dir)
        if not source.is_dir():
            raise ValueError(f"local_dir must be a directory: {local_dir}")
        self.upload(source, exist_policy=exist_policy, show_progress=show_progress)

    def download(
        self,
        local_path: str | Path | None = None,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = True,
    ) -> RemoteFile | Path:
        """Download file or directory from this remote path."""
        if not self.exists():
            raise FileNotFoundError(f"Remote path not found: {self.remote_path}")

        if self.is_dir():
            if local_path is None:
                local_target = Path.cwd() / Path(self.clean_path).name
            else:
                local_target = Path(local_path).resolve()

            if local_target.exists():
                if exist_policy == "abort":
                    raise FileExistsError(f"Target local path already exists: {local_target}")
                elif exist_policy == "overwrite":
                    if local_target.is_dir():
                        shutil.rmtree(local_target)
                    else:
                        local_target.unlink()
                    local_target.mkdir(parents=True, exist_ok=True)
                elif exist_policy == "merge" and not local_target.is_dir():
                    raise ValueError(f"Cannot merge directory into local file: {local_target}")
            else:
                local_target.mkdir(parents=True, exist_ok=True)

            # Query remote items inside directory
            cmd = f"$items = Get-ChildItem -LiteralPath '{self.remote_path}' -Recurse | Select-Object FullName, PSIsContainer; if ($items) {{ $items | ConvertTo-Json -Compress }} else {{ '[]' }}"
            res = self.controller.cmd.run(cmd, auto_retry=False)
            out = res.stdout.strip()

            raw_items = []
            if out and out != "[]":
                try:
                    parsed = json.loads(out)
                    raw_items = parsed if isinstance(parsed, list) else [parsed]
                except Exception:
                    raw_items = []

            for entry in raw_items:
                full_r_path = entry.get("FullName", "")
                is_c = entry.get("PSIsContainer", False)
                if not full_r_path:
                    continue

                rel_part = full_r_path[len(self.remote_path) :].lstrip("\\").lstrip("/")
                local_item = local_target / rel_part

                if is_c:
                    local_item.mkdir(parents=True, exist_ok=True)
                else:
                    local_item.parent.mkdir(parents=True, exist_ok=True)
                    if local_item.exists():
                        if exist_policy == "abort":
                            raise FileExistsError(f"Target local file already exists: {local_item}")
                        elif exist_policy == "overwrite":
                            local_item.unlink(missing_ok=True)
                    data = self.controller.cmd.qga.read_file(full_r_path)
                    local_item.write_bytes(data)

            return local_target

        else:
            # Single file download
            data = self.read_bytes()
            if local_path is not None:
                local_target = Path(local_path).resolve()
                if local_target.exists():
                    if exist_policy == "abort":
                        raise FileExistsError(f"Target local file already exists: {local_target}")
                    elif exist_policy == "overwrite":
                        if local_target.is_dir():
                            shutil.rmtree(local_target)
                        else:
                            local_target.unlink()

                local_target.parent.mkdir(parents=True, exist_ok=True)
                local_target.write_bytes(data)
                return local_target

            return RemoteFile(self.remote_path, data)

    def download_dir(
        self,
        local_dir: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = True,
    ) -> Path:
        """Download remote directory to local directory."""
        target = Path(local_dir)
        res = self.download(local_path=target, exist_policy=exist_policy, show_progress=show_progress)
        if isinstance(res, RemoteFile):
            res.save(target)
            return target
        return res

    def __repr__(self) -> str:
        return f"<RemotePath '{self.remote_path}'>"


class FileController:
    """Controller for uploading and downloading files and directories to/from the guest VM via QGA."""

    def __init__(self, command_controller: CommandController):
        self.cmd = command_controller

    def path(self, remote_path: str) -> RemotePath:
        """Create a RemotePath object for a remote guest VM file or directory."""
        return RemotePath(self, remote_path)

    def __truediv__(self, remote_path: str) -> RemotePath:
        """Support machine.file / 'C:\\path' syntax."""
        return self.path(remote_path)

    def download(
        self,
        remote_path: str,
        local_path: str | Path | None = None,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = True,
    ) -> RemoteFile | Path:
        """Download a file or directory from the guest VM via QGA."""
        return self.path(remote_path).download(
            local_path=local_path, exist_policy=exist_policy, show_progress=show_progress
        )

    def download_dir(
        self,
        remote_dir: str,
        local_dir: str | Path,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = True,
    ) -> Path:
        """Download a directory from the guest VM via QGA."""
        return self.path(remote_dir).download_dir(
            local_dir=local_dir, exist_policy=exist_policy, show_progress=show_progress
        )

    def upload(
        self,
        local_path: str | Path,
        remote_path: str,
        exist_policy: ExistPolicy = "overwrite",
        chunk_size: int = 250000,
        show_progress: bool = True,
    ) -> None:
        """Upload a local file or directory to a remote path on the guest VM via QGA."""
        self.path(remote_path).upload(local_path, exist_policy=exist_policy, show_progress=show_progress)

    def upload_dir(
        self,
        local_dir: str | Path,
        remote_dir: str,
        exist_policy: ExistPolicy = "overwrite",
        show_progress: bool = True,
    ) -> None:
        """Upload a local directory to a remote directory path on the guest VM via QGA."""
        self.path(remote_dir).upload_dir(local_dir, exist_policy=exist_policy, show_progress=show_progress)
