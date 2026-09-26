"""File transfer controller for remote Windows QEMU VMs providing pathlib.Path-style RemotePath interface over QGA."""

import json
import os
import shutil
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from tqdm import tqdm

from windows.executor import CommandController

ExistPolicy = Literal["overwrite", "merge", "abort"]


def _inspect_dir_limits(path: Path, max_entries: int = 1000) -> tuple[int, int]:
    """Inspect directory to return (total_size, max_file_size).

    Stops early if max_file_size >= 2GB or total_size > 500MB (exceeds QEMU USB vvfat limit).
    """
    total_size = 0
    max_file_size = 0
    count = 0
    for root, _, files in os.walk(path):
        for f in files:
            count += 1
            if count > max_entries:
                total_size += 500 * 1024 * 1024
                return total_size, max_file_size
            try:
                sz = (Path(root) / f).stat().st_size
                total_size += sz
                if sz > max_file_size:
                    max_file_size = sz
                if max_file_size >= 2 * 1024 * 1024 * 1024 or total_size > 500 * 1024 * 1024:
                    return total_size, max_file_size
            except OSError:
                pass
    return total_size, max_file_size


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
    def parent(self) -> "RemotePath":
        """The logical parent of the path."""
        clean = self.clean_path.rstrip("\\/")
        p = str(PureWindowsPath(clean).parent)
        if clean.startswith("C:") and not p.startswith("C:"):
            p = f"C:\\{p}".replace("C:\\.", "C:\\")
        return RemotePath(self.controller, p)

    def iterdir(self) -> list["RemotePath"]:
        """Iterate over the files and directories in this remote guest directory."""
        cmd = (
            f"$items = Get-ChildItem -LiteralPath '{self.remote_path}'; "
            f"if ($items) {{ ($items | ForEach-Object {{ $_.FullName }}) -join [Environment]::NewLine }} else {{ '' }}"
        )
        res = self.controller.cmd.run(cmd, auto_retry=False)
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        return [RemotePath(self.controller, line) for line in lines]

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


class SharedFolder:
    """Represents an active host-to-guest shared folder mounted inside the Windows VM."""

    def __init__(
        self,
        controller: "FileController",
        host_path: Path,
        guest_drive: str = "Z:",
        device_id: str | None = None,
        drive_id: str | None = None,
        share_name: str | None = None,
        backend: str = "usb",
        unmount_cb: Any | None = None,
    ):
        self.controller = controller
        self.host_path = Path(host_path).resolve()
        clean_drive = guest_drive.strip().rstrip(":\\/").upper()
        self.guest_drive = f"{clean_drive}:"
        self.device_id = device_id
        self.drive_id = drive_id
        self.share_name = share_name
        self.backend = backend
        self._unmount_cb = unmount_cb
        self.is_active = True

    def unshare(self) -> None:
        """Unmount the drive in guest and detach the virtual storage device / stop share."""
        if not self.is_active:
            return
        self.is_active = False
        if self._unmount_cb:
            try:
                self._unmount_cb()
            except Exception:
                pass

    def __enter__(self) -> "SharedFolder":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.unshare()

    def __repr__(self) -> str:
        status = "active" if self.is_active else "unmounted"
        return f"<SharedFolder '{self.host_path}' -> {self.guest_drive} ({status}, backend={self.backend})>"


class FileController:
    """Controller for uploading, downloading, and sharing files and directories with the guest VM."""

    def __init__(self, command_controller: CommandController, machine: Any | None = None):
        self.cmd = command_controller
        self.machine = machine

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

    def share(
        self,
        from_path: str | Path | None = None,
        to: str = "Z:",
        *,
        from_: str | Path | None = None,
        backend: Literal["auto", "usb", "smb"] = "auto",
        read_only: bool = False,
        **kwargs: Any,
    ) -> SharedFolder:
        """Share a host directory with the Windows guest and mount it as a drive letter.

        Supports:
            machine.file.share(from=host_path, to=guest_drive)
            machine.file.share(from_path, to="Z:")
            with machine.file.share(from_path, to="Z:", backend="auto") as share: ...

        Backends:
            - "auto": Automatically selects "smb" if folder exceeds 500MB or contains files >= 2GB;
                      otherwise selects "usb" if machine console is available.
            - "usb": Uses QEMU virtual FAT USB storage hotplug. Fails if files >= 2GB or folder > 500MB.
            - "smb": Uses user-space SMB server over QEMU networking. Supports arbitrarily large folders.
        """
        raw_from = kwargs.get("from") or from_ or from_path
        if raw_from is None:
            raise ValueError("A source directory path ('from' or 'from_path') must be provided.")

        host_dir = Path(raw_from).resolve()
        if not host_dir.exists() or not host_dir.is_dir():
            raise FileNotFoundError(f"Host directory does not exist or is not a directory: {host_dir}")

        guest_drive = kwargs.get("to") or kwargs.get("guest_drive") or to
        clean_drive_letter = str(guest_drive).strip().rstrip(":\\/").upper()
        target_drive = f"{clean_drive_letter}:"

        if backend not in ("auto", "usb", "smb"):
            raise ValueError(f"Invalid backend '{backend}'. Must be 'auto', 'usb', or 'smb'.")

        total_size, max_file = _inspect_dir_limits(host_dir)
        usb_limit_exceeded = max_file >= 2 * 1024 * 1024 * 1024 or total_size > 500 * 1024 * 1024

        if backend == "auto":
            if usb_limit_exceeded:
                chosen_backend = "smb"
            else:
                chosen_backend = "usb" if (self.machine and hasattr(self.machine, "console")) else "smb"
        elif backend == "usb":
            if usb_limit_exceeded:
                reason = (
                    "contains file(s) >= 2GB"
                    if max_file >= 2 * 1024 * 1024 * 1024
                    else "total size exceeds 500MB limit"
                )
                raise ValueError(
                    f"Directory '{host_dir}' cannot be shared via USB vvfat ({reason}). Use backend='smb' instead."
                )
            chosen_backend = "usb"
        else:
            chosen_backend = "smb"

        share_uid = uuid.uuid4().hex[:8]

        if chosen_backend == "usb":
            drive_id = f"drv_{share_uid}"
            dev_id = f"usb_{share_uid}"
            rw_flag = "ro" if read_only else "rw"
            qemu_path = str(host_dir).replace("\\", "/")

            if not (self.machine and hasattr(self.machine, "console")):
                raise RuntimeError("USB sharing requires an active machine console.")

            # 1. Add virtual FAT drive via QEMU monitor
            out = self.machine.console.send_monitor_command(
                f"drive_add 0 file=fat:{rw_flag}:{qemu_path},format=raw,if=none,id={drive_id}"
            )
            out_lower = out.lower()
            if "could not" in out_lower or "larger than" in out_lower or "can't" in out_lower or "error" in out_lower:
                raise RuntimeError(f"QEMU failed to add USB drive for '{host_dir}': {out.strip()}")

            # 2. Attach USB storage device
            out = self.machine.console.send_monitor_command(f"device_add usb-storage,drive={drive_id},id={dev_id}")
            out_lower = out.lower()
            if "error" in out_lower or "failed" in out_lower or "can't" in out_lower:
                try:
                    self.machine.console.send_monitor_command(f"drive_del {drive_id}")
                except Exception:
                    pass
                raise RuntimeError(f"QEMU failed to attach USB storage device '{dev_id}': {out.strip()}")

            # 3. Inside Windows guest, detect the newly attached USB disk and assign target drive letter
            ps_assign_script = f"""
$targetLetter = '{clean_drive_letter}:'
$assigned = $false
for ($i = 0; $i -lt 20; $i++) {{
    $v = Get-WmiObject -Class Win32_Volume | Where-Object {{ $_.Label -eq 'QEMU VVFAT' -or $_.FileSystem -like '*FAT*' }} | Select-Object -Last 1
    if ($v) {{
        if ($v.DriveLetter -eq $targetLetter) {{
            $assigned = $true
            break
        }}
        try {{
            $v.DriveLetter = $targetLetter
            $v.Put() | Out-Null
            $assigned = $true
            break
        }} catch {{}}
    }}
    Start-Sleep -Milliseconds 500
}}
$assigned
"""
            res = self.cmd.run(
                ps_assign_script.strip(),
                as_system=True,
                powershell=True,
                auto_retry=True,
                timeout=25,
            )
            if res.stdout.strip().lower() != "true":
                try:
                    self.machine.console.send_monitor_command(f"device_del {dev_id}")
                except Exception:
                    pass
                raise RuntimeError(
                    f"Guest failed to detect and assign USB volume as drive '{target_drive}'. "
                    f"Output: {res.stdout.strip() or res.stderr.strip()}"
                )

            # Verify drive accessibility
            check_res = self.cmd.run(
                f"Test-Path '{clean_drive_letter}:\\'",
                as_system=True,
                powershell=True,
                auto_retry=False,
            )
            if check_res.stdout.strip().lower() != "true":
                try:
                    self.machine.console.send_monitor_command(f"device_del {dev_id}")
                except Exception:
                    pass
                raise RuntimeError(f"USB drive '{target_drive}' was assigned but is not accessible in guest.")

            def _cleanup_usb():
                unmount_script = f"""
try {{
    $v = Get-WmiObject -Class Win32_Volume | Where-Object {{ $_.DriveLetter -eq '{clean_drive_letter}:' }} | Select-Object -Last 1
    if ($v) {{
        $v.DriveLetter = $null
        $v.Put() | Out-Null
    }}
}} catch {{}}
"""
                try:
                    self.cmd.run(unmount_script.strip(), as_system=True, powershell=True, auto_retry=False)
                except Exception:
                    pass

                if self.machine and hasattr(self.machine, "console"):
                    try:
                        self.machine.console.send_monitor_command(f"device_del {dev_id}")
                    except Exception:
                        pass

            return SharedFolder(
                controller=self,
                host_path=host_dir,
                guest_drive=target_drive,
                device_id=dev_id,
                drive_id=drive_id,
                backend="usb",
                unmount_cb=_cleanup_usb,
            )

        # chosen_backend == "smb"
        from windows.smb import SMBServerManager

        if self.machine and hasattr(self.machine, "smb") and self.machine.smb:
            smb_mgr = self.machine.smb
        else:
            smb_mgr = SMBServerManager()
            if self.machine:
                self.machine.smb = smb_mgr

        if not smb_mgr.is_running:
            smb_mgr.start()

        share_name = f"SHARE_{share_uid}"
        smb_mgr.add_share(share_name, host_dir, read_only=read_only)

        # Determine credentials: use explicit user/password kwargs,
        # active as_user scope, or default machine/controller credentials (matching command.run)
        from windows.executor import _current_user

        as_sys = kwargs.get("as_system", False)
        active_scope = _current_user.get()
        user_arg = kwargs.get("user")
        pass_arg = kwargs.get("password")

        default_user = getattr(self.machine, "default_user", None) or getattr(self.cmd, "default_user", None)
        default_password = getattr(self.machine, "default_password", None) or getattr(
            self.cmd, "default_password", None
        )

        if as_sys:
            cred_user = smb_mgr.username
            cred_pass = smb_mgr.password
        elif user_arg is not None:
            cred_user = str(user_arg)
            if pass_arg is not None:
                cred_pass = str(pass_arg)
            elif default_user and str(cred_user).lower() == str(default_user).lower() and default_password:
                cred_pass = str(default_password)
            else:
                raise ValueError(f"Password must be provided when sharing files as user '{cred_user}'.")
        elif active_scope:
            cred_user, cred_pass = active_scope
        elif default_user:
            cred_user = str(default_user)
            cred_pass = str(default_password or "Password123!")
        else:
            cred_user = smb_mgr.username
            cred_pass = smb_mgr.password

        # Dynamically register the user credentials on the SMB server
        smb_mgr.add_credential(str(cred_user), str(cred_pass))

        smb_user = cred_user.replace("'", "''")
        smb_pass = cred_pass.replace("'", "''")

        smb_host = "10.0.2.4"
        mount_script = f"""
$drive = '{clean_drive_letter}:'
$remote = '\\\\{smb_host}\\{share_name}'
$user = '{smb_user}'
$pass = '{smb_pass}'

Remove-SmbGlobalMapping -LocalPath $drive -Force -ErrorAction SilentlyContinue | Out-Null
net use $drive /delete /y 2>$null | Out-Null
cmdkey /add:$smb_host /user:$user /pass:$pass 2>&1 | Out-Null

$mounted = $false
try {{
    $secPass = ConvertTo-SecureString $pass -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential($user, $secPass)
    New-SmbGlobalMapping -LocalPath $drive -RemotePath $remote -Credential $cred -FullAccess -ErrorAction Stop | Out-Null
    if (Test-Path "$drive\\") {{ $mounted = $true }}
}} catch {{}}

if (-not $mounted) {{
    $out = net use $drive $remote $pass /user:$user /persistent:yes 2>&1
    if (Test-Path "$drive\\") {{ $mounted = $true }}
}}

if ($mounted) {{
    "SUCCESS"
}} else {{
    "FAIL: " + ($out -join "`n")
}}
"""
        mount_res = self.cmd.run(mount_script.strip(), as_system=True, powershell=True, auto_retry=True, timeout=25)
        if "SUCCESS" not in mount_res.stdout:
            # Check if 10.0.2.4 was unreachable because VM was started without guestfwd, and try hotplugging SMB netdev
            hotplugged = False
            if self.machine and hasattr(self.machine, "console"):
                try:
                    self.machine.console.send_monitor_command(
                        f"netdev_add user,id=smbnet,net=10.0.3.0/24,guestfwd=tcp:10.0.3.4:445-cmd:nc\\ 127.0.0.1\\ {smb_mgr.port}"
                    )
                    self.machine.console.send_monitor_command("device_add e1000e,netdev=smbnet,id=smbnic,bus=rp1")
                    time.sleep(3.0)
                    smb_host = "10.0.3.4"
                    mount_script_retry = f"""
$drive = '{clean_drive_letter}:'
$remote = '\\\\{smb_host}\\{share_name}'
$user = '{smb_user}'
$pass = '{smb_pass}'

Remove-SmbGlobalMapping -LocalPath $drive -Force -ErrorAction SilentlyContinue | Out-Null
net use $drive /delete /y 2>$null | Out-Null
cmdkey /add:$smb_host /user:$user /pass:$pass 2>&1 | Out-Null

$mounted = $false
try {{
    $secPass = ConvertTo-SecureString $pass -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential($user, $secPass)
    New-SmbGlobalMapping -LocalPath $drive -RemotePath $remote -Credential $cred -FullAccess -ErrorAction Stop | Out-Null
    if (Test-Path "$drive\\") {{ $mounted = $true }}
}} catch {{}}

if (-not $mounted) {{
    $out = net use $drive $remote $pass /user:$user /persistent:yes 2>&1
    if (Test-Path "$drive\\") {{ $mounted = $true }}
}}

if ($mounted) {{ "SUCCESS" }} else {{ "FAIL: " + ($out -join "`n") }}
"""
                    retry_res = self.cmd.run(
                        mount_script_retry.strip(),
                        as_system=True,
                        powershell=True,
                        auto_retry=True,
                        timeout=25,
                    )
                    if "SUCCESS" in retry_res.stdout:
                        hotplugged = True
                except Exception:
                    pass

            if not hotplugged and "SUCCESS" not in mount_res.stdout:
                smb_mgr.remove_share(share_name)
                raise RuntimeError(
                    f"Failed to mount SMB share '{share_name}' on {smb_host} as drive '{target_drive}': "
                    f"{mount_res.stdout.strip() or mount_res.stderr.strip()}"
                )

        def _cleanup_smb():
            cleanup_script = f"""
Remove-SmbGlobalMapping -LocalPath '{clean_drive_letter}:' -Force -ErrorAction SilentlyContinue | Out-Null
net use {clean_drive_letter}: /delete /y 2>$null | Out-Null
cmdkey /delete:{smb_host} 2>$null | Out-Null
cmdkey /delete:10.0.2.4 2>$null | Out-Null
cmdkey /delete:10.0.3.4 2>$null | Out-Null
"""
            try:
                self.cmd.run(cleanup_script.strip(), as_system=True, powershell=True, auto_retry=False)
            except Exception:
                pass
            try:
                smb_mgr.remove_share(share_name)
            except Exception:
                pass

        return SharedFolder(
            controller=self,
            host_path=host_dir,
            guest_drive=target_drive,
            share_name=share_name,
            backend="smb",
            unmount_cb=_cleanup_smb,
        )
