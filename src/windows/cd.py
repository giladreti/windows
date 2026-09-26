"""CD-ROM drive emulation and ISO media controller for Windows QEMU VMs."""

import io
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from windows.machine import Machine


def create_cdrom_iso(
    path: str | Path | None = None,
    label: str = "CDROM",
    files: Mapping[str, bytes | str | Path] | None = None,
    source_dir: str | Path | None = None,
) -> Path:
    """Create a standard ISO 9660 / Joliet filesystem image on the host suitable for mounting as a CD-ROM.

    Uses pycdlib for cross-platform, dependency-free generation of ISO images with long filename support.

    Args:
        path: Destination ISO path. If None, a temporary .iso file is created in system temp.
        label: Volume label (up to 32 characters for ISO9660/Joliet).
        files: Optional mapping of relative paths to bytes, strings, or host file Paths.
        source_dir: Optional host directory whose contents will be recursively copied into the ISO.

    Returns:
        Path to the created ISO image.
    """
    clean_label = re.sub(r"[^A-Za-z0-9_-]", "_", label)[:32] or "CDROM"

    if path is None:
        fd, tmp_file = tempfile.mkstemp(prefix="cdrom_", suffix=".iso")
        os.close(fd)
        target_path = Path(tmp_file).resolve()
    else:
        target_path = Path(path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident=clean_label)

    # Track created directories in ISO (both ISO9660 uppercase path and Joliet path)
    created_dirs: set[str] = set()

    def _ensure_iso_dir(rel_dir: str) -> tuple[str, str]:
        """Ensure all intermediate directories exist in the ISO."""
        parts = [p for p in rel_dir.replace("\\", "/").split("/") if p]
        iso_curr = ""
        joliet_curr = ""
        for p in parts:
            iso_curr = f"{iso_curr}/{p.upper()}"
            joliet_curr = f"{joliet_curr}/{p}"
            if joliet_curr not in created_dirs:
                try:
                    iso.add_directory(iso_curr, joliet_path=joliet_curr)
                except Exception:
                    pass
                created_dirs.add(joliet_curr)
        return iso_curr, joliet_curr

    if files:
        for rel_name, content in files.items():
            rel_clean = str(rel_name).replace("\\", "/").lstrip("/")
            parts = rel_clean.split("/")
            filename = parts[-1]
            parent_dir = "/".join(parts[:-1]) if len(parts) > 1 else ""

            iso_parent, joliet_parent = _ensure_iso_dir(parent_dir)

            iso_file = f"{iso_parent}/{filename.upper()};1" if iso_parent else f"/{filename.upper()};1"
            joliet_file = f"{joliet_parent}/{filename}" if joliet_parent else f"/{filename}"

            if isinstance(content, bytes):
                data = content
            elif isinstance(content, str):
                data = content.encode("utf-8")
            elif isinstance(content, (Path, os.PathLike)) or hasattr(content, "read_bytes"):
                data = Path(content).read_bytes()
            else:
                data = bytes(content)

            iso.add_fp(io.BytesIO(data), len(data), iso_file, joliet_path=joliet_file)

    if source_dir:
        src_p = Path(source_dir).resolve()
        if src_p.is_dir():
            for root, _, filenames in os.walk(src_p):
                for fname in filenames:
                    f_path = Path(root) / fname
                    rel = f_path.relative_to(src_p)
                    rel_clean = str(rel).replace("\\", "/").lstrip("/")
                    parts = rel_clean.split("/")
                    filename = parts[-1]
                    parent_dir = "/".join(parts[:-1]) if len(parts) > 1 else ""

                    iso_parent, joliet_parent = _ensure_iso_dir(parent_dir)
                    iso_file = f"{iso_parent}/{filename.upper()};1" if iso_parent else f"/{filename.upper()};1"
                    joliet_file = f"{joliet_parent}/{filename}" if joliet_parent else f"/{filename}"

                    data = f_path.read_bytes()
                    iso.add_fp(io.BytesIO(data), len(data), iso_file, joliet_path=joliet_file)

    iso.write(str(target_path))
    iso.close()
    return target_path


@dataclass
class CDDevice:
    """Represents an active CD-ROM drive / mounted ISO media in the virtual machine."""

    controller: "CDController"
    device_id: str
    drive_id: str
    iso_path: Path
    drive_letter: str | None = None
    is_active: bool = True
    is_temporary: bool = False
    backend: str = "ide"  # "ide" (via change/eject) or "usb" (via hotplug usb-storage)

    def eject(self) -> None:
        """Eject this CD-ROM disc / unmount from the virtual machine."""
        if not self.is_active:
            return
        self.controller.eject(self)

    def unmount(self) -> None:
        """Alias for eject()."""
        self.eject()

    def __enter__(self) -> "CDDevice":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.eject()

    def __repr__(self) -> str:
        letter_str = f" drive={self.drive_letter}" if self.drive_letter else ""
        status = "active" if self.is_active else "ejected"
        return f"<CDDevice id={self.device_id!r}{letter_str} path={str(self.iso_path)!r} status={status}>"


class CDController:
    """Controller for creating ISO images, inserting discs, and managing CD-ROM drives on a Windows Machine."""

    def __init__(self, machine: "Machine"):
        self.machine = machine
        self._devices: dict[str, CDDevice] = {}

    @property
    def devices(self) -> list[CDDevice]:
        """Return list of currently active CD devices."""
        return [dev for dev in self._devices.values() if dev.is_active]

    @property
    def is_inserted(self) -> bool:
        """Return True if at least one CD disc is currently mounted/inserted."""
        return any(dev.is_active for dev in self._devices.values())

    def create_iso(
        self,
        path: str | Path | None = None,
        label: str = "CDROM",
        files: Mapping[str, bytes | str | Path] | None = None,
        source_dir: str | Path | None = None,
    ) -> Path:
        """Create a formatted ISO 9660 / Joliet filesystem image on the host for use as a CD-ROM."""
        return create_cdrom_iso(
            path=path,
            label=label,
            files=files,
            source_dir=source_dir,
        )

    def insert(
        self,
        iso: str | Path | None = None,
        to: str | None = None,
        label: str = "CDROM",
        files: Mapping[str, bytes | str | Path] | None = None,
        source_dir: str | Path | None = None,
        prefer_hotplug: bool = False,
    ) -> CDDevice:
        """Insert or mount an ISO image into a CD-ROM drive in the virtual machine.

        If `iso` is None or does not exist, an ISO image is automatically generated on the host
        using `label`, `files`, and `source_dir`.

        Args:
            iso: Path to an existing .iso file on the host. If None, one is created.
            to: Desired Windows drive letter (e.g. 'D:' or 'D').
            label: Volume label if creating a new ISO image.
            files: Dict of relative paths to contents to put inside the ISO.
            source_dir: Host directory whose contents will be recursively copied into the ISO.
            prefer_hotplug: If True, hotplugs a new USB CD-ROM drive instead of using the primary SATA/IDE CD-ROM.

        Returns:
            CDDevice instance representing the mounted CD-ROM drive.
        """
        if not self.machine.is_running:
            raise RuntimeError("Machine must be running to insert/mount CD-ROM media. Power on the machine first.")

        if not hasattr(self.machine, "console") or not self.machine.console:
            raise RuntimeError("Machine console is required to manage CD-ROM media.")

        is_temporary = False
        if iso is None:
            iso_path = self.create_iso(
                path=None,
                label=label,
                files=files,
                source_dir=source_dir,
            )
            is_temporary = True
        else:
            iso_path = Path(iso).resolve()
            if not iso_path.exists():
                iso_path = self.create_iso(
                    path=iso_path,
                    label=label,
                    files=files,
                    source_dir=source_dir,
                )

        clean_label = re.sub(r"[^A-Za-z0-9_-]", "", label)[:16] or "CDROM"
        qemu_path = str(iso_path).replace("\\", "/")
        target_letter = str(to).strip().rstrip(":\\/").upper() if to else None

        # Check if onboard SATA/IDE CD-ROM drive exists and is available
        ide_available = False
        if not prefer_hotplug:
            try:
                block_info = self.machine.console.send_monitor_command("info block")
                if "cd0" in block_info or "cdrom0" in block_info:
                    ide_available = True
            except Exception:
                pass

        if ide_available and not any(dev.backend == "ide" and dev.is_active for dev in self._devices.values()):
            # Use onboard CD-ROM drive via `change`
            cd_name = "cd0" if "cd0" in block_info else "cdrom0"
            out = self.machine.console.send_monitor_command(f"change {cd_name} {qemu_path}")
            out_lower = out.lower()
            if "error" in out_lower or "could not" in out_lower or "failed" in out_lower:
                raise RuntimeError(f"QEMU failed to insert CD media into {cd_name}: {out.strip()}")

            dev_id = f"onboard_{cd_name}"
            drive_id = cd_name
            backend = "ide"
        else:
            # Hotplug via USB CD-ROM (usb-storage with removable=true)
            share_uid = uuid.uuid4().hex[:8]
            drive_id = f"cd_drv_{share_uid}"
            dev_id = f"cd_dev_{share_uid}"
            backend = "usb"

            add_drive_cmd = f"drive_add 0 file={qemu_path},format=raw,if=none,id={drive_id},media=cdrom,readonly=on"
            out = self.machine.console.send_monitor_command(add_drive_cmd)
            out_lower = out.lower()
            if "could not" in out_lower or "error" in out_lower or "failed" in out_lower:
                raise RuntimeError(f"QEMU failed to add CD drive: {out.strip()}")

            add_dev_cmd = f"device_add usb-storage,drive={drive_id},id={dev_id},removable=true"
            out = self.machine.console.send_monitor_command(add_dev_cmd)
            out_lower = out.lower()
            if "error" in out_lower or "failed" in out_lower:
                try:
                    self.machine.console.send_monitor_command(f"drive_del {drive_id}")
                except Exception:
                    pass
                raise RuntimeError(f"QEMU failed to attach USB CD device: {out.strip()}")

        # Detect CD-ROM drive letter inside Windows guest
        assigned_letter: str | None = None
        if hasattr(self.machine, "command") and self.machine.command:
            detect_script = f"""
$target = '{target_letter or ""}'
$label = '{clean_label}'
$assigned = ''

for ($i = 0; $i -lt 30; $i++) {{
    # 1. Query CD-ROM volumes
    $vols = Get-Volume | Where-Object {{ $_.DriveType -eq 'CD-ROM' -or $_.FileSystemType -in @('CDFS', 'UDF') }}
    foreach ($v in $vols) {{
        if ($v.DriveLetter) {{
            $curChar = "$($v.DriveLetter)".TrimEnd(':').ToUpper()
            if ($target -and $curChar -ne $target) {{
                try {{
                    Get-Partition -DriveLetter $curChar | Set-Partition -NewDriveLetter $target -ErrorAction Stop
                    $assigned = "$($target):"
                }} catch {{
                    $assigned = "$($curChar):"
                }}
            }} else {{
                $assigned = "$($curChar):"
            }}
            break
        }}
    }}
    if ($assigned) {{ break }}

    # 2. Query by label
    if ($label) {{
        $lblVol = Get-Volume -FileSystemLabel $label -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($lblVol -and $lblVol.DriveLetter) {{
            $assigned = "$($lblVol.DriveLetter):"
            break
        }}
    }}

    Start-Sleep -Milliseconds 500
}}
Write-Output $assigned
"""
            try:
                res = self.machine.command.run(detect_script.strip(), powershell=True, timeout=25, auto_retry=True)
                val = res.stdout.strip()
                match = re.search(r"([A-Za-z]:)", val)
                if match:
                    assigned_letter = match.group(1).upper()
            except Exception:
                pass

        if not assigned_letter and target_letter:
            assigned_letter = f"{target_letter}:"

        cd_device = CDDevice(
            controller=self,
            device_id=dev_id,
            drive_id=drive_id,
            iso_path=iso_path,
            drive_letter=assigned_letter,
            is_active=True,
            is_temporary=is_temporary,
            backend=backend,
        )
        self._devices[dev_id] = cd_device
        return cd_device

    def mount(
        self,
        iso: str | Path | None = None,
        to: str | None = None,
        label: str = "CDROM",
        files: Mapping[str, bytes | str | Path] | None = None,
        source_dir: str | Path | None = None,
        prefer_hotplug: bool = False,
    ) -> CDDevice:
        """Alias for insert()."""
        return self.insert(
            iso=iso,
            to=to,
            label=label,
            files=files,
            source_dir=source_dir,
            prefer_hotplug=prefer_hotplug,
        )

    def eject(self, cd: CDDevice | str | None = None) -> None:
        """Eject CD-ROM media from the drive."""
        if cd is None:
            # Eject primary active disc
            active = self.devices
            if not active:
                return
            target_device = active[0]
        elif isinstance(cd, CDDevice):
            target_device = cd
        else:
            target_device = self._devices.get(str(cd))

        if not target_device:
            return

        if target_device.backend == "ide":
            if hasattr(self.machine, "console") and self.machine.console:
                try:
                    self.machine.console.send_monitor_command(f"eject -f {target_device.drive_id}")
                except Exception:
                    pass
        else:
            if hasattr(self.machine, "console") and self.machine.console:
                try:
                    self.machine.console.send_monitor_command(f"device_del {target_device.device_id}")
                except Exception:
                    pass
                try:
                    self.machine.console.send_monitor_command(f"drive_del {target_device.drive_id}")
                except Exception:
                    pass

        target_device.is_active = False
        if target_device.is_temporary and target_device.iso_path.exists():
            try:
                target_device.iso_path.unlink()
            except OSError:
                pass
        self._devices.pop(target_device.device_id, None)

    def unmount(self, cd: CDDevice | str | None = None) -> None:
        """Alias for eject()."""
        self.eject(cd)

    def eject_all(self) -> None:
        """Eject all currently mounted CD discs."""
        for dev in list(self.devices):
            self.eject(dev)
