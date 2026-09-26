"""USB drive emulation and storage device controller for Windows QEMU VMs."""

import os
import re
import shutil
import struct
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from windows.machine import Machine


def parse_size_to_bytes(size: str | int) -> int:
    """Parse size string like '512M', '1G', '2048K', or integer bytes into integer bytes."""
    if isinstance(size, int):
        return size
    s = str(size).strip().upper()
    units = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024 * 1024,
        "MB": 1024 * 1024,
        "G": 1024 * 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
        "T": 1024 * 1024 * 1024 * 1024,
        "TB": 1024 * 1024 * 1024 * 1024,
    }
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Z]*)$", s)
    if not m:
        raise ValueError(f"Invalid size specification: '{size}'")
    val = float(m.group(1))
    unit = m.group(2) or "M"
    mult = units.get(unit)
    if mult is None:
        raise ValueError(f"Unknown size unit: '{unit}' in '{size}'")
    return int(val * mult)


def create_usb_disk(
    path: str | Path | None = None,
    size: str | int = "512M",
    filesystem: str = "fat32",
    label: str = "USB",
    files: Mapping[str, bytes | str | Path] | None = None,
    source_dir: str | Path | None = None,
) -> Path:
    """Create a formatted virtual disk image on the host suitable for mounting as a USB drive.

    Args:
        path: Destination image path. If None, a temporary .img file is created in system temp.
        size: Size of the disk image (e.g. '256M', '1G', 536870912).
        filesystem: Filesystem type: 'fat32', 'fat', 'ntfs', 'raw', or 'qcow2'.
        label: Volume label (up to 11 alphanumeric characters for FAT).
        files: Optional dictionary mapping relative destination filenames to bytes, strings, or host file Paths.
        source_dir: Optional host directory whose contents will be recursively copied into the image.

    Returns:
        Path to the created disk image.
    """
    size_bytes = parse_size_to_bytes(size)
    fs_lower = filesystem.lower()
    clean_label = re.sub(r"[^A-Za-z0-9_-]", "", label)[:11] or "USB"

    if path is None:
        suffix = ".qcow2" if fs_lower == "qcow2" else ".img"
        fd, tmp_file = tempfile.mkstemp(prefix="usb_drive_", suffix=suffix)
        os.close(fd)
        target_path = Path(tmp_file).resolve()
    else:
        target_path = Path(path).resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)

    if fs_lower == "qcow2":
        qemu_img = shutil.which("qemu-img") or "qemu-img"
        cmd = [qemu_img, "create", "-f", "qcow2", str(target_path), f"{size_bytes}"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"qemu-img failed to create qcow2 disk: {res.stderr}")
        return target_path

    # Allocate raw file
    with open(target_path, "wb") as f:
        f.truncate(size_bytes)

    if fs_lower in ("fat32", "fat", "vfat"):
        # Write standard MBR partition table with 1 primary FAT32 partition starting at 1MiB (sector 2048)
        start_sector = 2048
        total_sectors = size_bytes // 512
        use_partition = total_sectors > start_sector + 1024

        if use_partition:
            mbr = bytearray(512)
            part_type = 0x0B if size_bytes < 512 * 1024 * 1024 else 0x0C
            mbr[446] = 0x00
            mbr[446 + 4] = part_type
            struct.pack_into("<I", mbr, 446 + 8, start_sector)
            struct.pack_into("<I", mbr, 446 + 12, total_sectors - start_sector)
            mbr[510:512] = b"\x55\xaa"
            with open(target_path, "r+b") as f:
                f.seek(0)
                f.write(mbr)

        offset_arg = ["--offset=2048"] if use_partition else []
        mcopy_target = f"{target_path}@@1048576" if use_partition else str(target_path)

        # Format as FAT32 using mkfs.vfat or mformat if available
        mkfs_vfat = shutil.which("mkfs.vfat") or shutil.which("mkfs.fat")
        if mkfs_vfat:
            cmd = [mkfs_vfat, "-F", "32", *offset_arg, "-n", clean_label, str(target_path)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                # If disk is smaller than FAT32 minimum (~33MB), try FAT16
                if size_bytes < 33 * 1024 * 1024:
                    subprocess.run(
                        [mkfs_vfat, "-F", "16", *offset_arg, "-n", clean_label, str(target_path)],
                        check=True,
                    )
                else:
                    raise RuntimeError(f"mkfs.vfat failed: {res.stderr}")
        elif shutil.which("mformat"):
            subprocess.run(["mformat", "-i", mcopy_target, "-F", "-v", clean_label, "::"], check=True)

        # Copy files into FAT image if requested
        mcopy = shutil.which("mcopy")
        if mcopy:
            if files:
                for rel_name, content in files.items():
                    rel_clean = str(rel_name).replace("\\", "/").lstrip("/")
                    # Create temporary file to copy via mcopy
                    with tempfile.NamedTemporaryFile(delete=False) as tf:
                        temp_in = Path(tf.name)
                        if isinstance(content, bytes):
                            temp_in.write_bytes(content)
                        elif isinstance(content, str):
                            temp_in.write_text(content, encoding="utf-8")
                        elif isinstance(content, Path) or hasattr(content, "read_bytes"):
                            temp_in.write_bytes(Path(content).read_bytes())
                    try:
                        # Make parent directory in FAT if needed
                        parts = rel_clean.split("/")
                        if len(parts) > 1:
                            dir_path = ""
                            for p in parts[:-1]:
                                dir_path = f"{dir_path}/{p}" if dir_path else p
                                subprocess.run(
                                    ["mmd", "-i", mcopy_target, f"::{dir_path}"],
                                    capture_output=True,
                                )
                        subprocess.run(
                            [mcopy, "-i", mcopy_target, str(temp_in), f"::{rel_clean}"],
                            capture_output=True,
                        )
                    finally:
                        if temp_in.exists():
                            temp_in.unlink()

            if source_dir:
                src_p = Path(source_dir).resolve()
                if src_p.is_dir():
                    for root, _, filenames in os.walk(src_p):
                        for fname in filenames:
                            f_path = Path(root) / fname
                            rel = f_path.relative_to(src_p)
                            rel_str = str(rel).replace("\\", "/")
                            parts = rel_str.split("/")
                            if len(parts) > 1:
                                dir_path = ""
                                for p in parts[:-1]:
                                    dir_path = f"{dir_path}/{p}" if dir_path else p
                                    subprocess.run(
                                        ["mmd", "-i", mcopy_target, f"::{dir_path}"],
                                        capture_output=True,
                                    )
                            subprocess.run(
                                [mcopy, "-i", mcopy_target, str(f_path), f"::{rel_str}"],
                                capture_output=True,
                            )

    elif fs_lower == "ntfs":
        mkfs_ntfs = shutil.which("mkfs.ntfs")
        if mkfs_ntfs:
            cmd = [mkfs_ntfs, "-F", "-L", clean_label, str(target_path)]
            subprocess.run(cmd, capture_output=True)

    return target_path


@dataclass
class USBDevice:
    """Represents an active emulated USB storage device attached to the virtual machine."""

    controller: "USBController"
    device_id: str
    drive_id: str
    disk_path: Path
    drive_letter: str | None = None
    read_only: bool = False
    is_active: bool = True
    is_temporary: bool = False

    def unmount(self) -> None:
        """Unmount and hot-unplug this USB device from the virtual machine."""
        if not self.is_active:
            return
        self.controller.unmount(self)

    def detach(self) -> None:
        """Alias for unmount()."""
        self.unmount()

    def __enter__(self) -> "USBDevice":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.unmount()

    def __repr__(self) -> str:
        letter_str = f" drive={self.drive_letter}" if self.drive_letter else ""
        ro_str = " (ro)" if self.read_only else ""
        status = "active" if self.is_active else "unmounted"
        return f"<USBDevice id={self.device_id!r}{letter_str}{ro_str} path={str(self.disk_path)!r} status={status}>"


class USBController:
    """Controller for creating, hotplugging, and managing emulated USB drives on a Windows Machine."""

    def __init__(self, machine: "Machine"):
        self.machine = machine
        self._devices: dict[str, USBDevice] = {}

    @property
    def devices(self) -> list[USBDevice]:
        """Return list of currently active emulated USB devices."""
        return [dev for dev in self._devices.values() if dev.is_active]

    def create_disk(
        self,
        path: str | Path | None = None,
        size: str | int = "512M",
        filesystem: str = "fat32",
        label: str = "USB",
        files: Mapping[str, bytes | str | Path] | None = None,
        source_dir: str | Path | None = None,
    ) -> Path:
        """Create a formatted virtual disk image on the host for use as a USB drive."""
        return create_usb_disk(
            path=path,
            size=size,
            filesystem=filesystem,
            label=label,
            files=files,
            source_dir=source_dir,
        )

    def mount(
        self,
        disk: str | Path | None = None,
        to: str | None = None,
        read_only: bool = False,
        size: str | int = "512M",
        filesystem: str = "fat32",
        label: str = "USB",
        files: Mapping[str, bytes | str | Path] | None = None,
        source_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> USBDevice:
        """Mount a disk image as an emulated USB flash drive into the running virtual machine.

        If `disk` is None or does not exist, a disk image is automatically created on the host
        using `size`, `filesystem`, `label`, `files`, and `source_dir`.

        Args:
            disk: Path to an existing .img, .raw, or .qcow2 disk image. If None, one is created.
            to: Desired guest drive letter (e.g. 'E:' or 'E'). If None, automatically detected.
            read_only: If True, disk is attached in read-only mode.
            size: Size of disk image if creating a new one.
            filesystem: Filesystem format if creating a new disk ('fat32', 'ntfs', 'raw', 'qcow2').
            label: Volume label for new disk image.
            files: Dict of filename -> content/Path to populate into the newly created disk image.
            source_dir: Directory to copy into the newly created disk image.

        Returns:
            USBDevice instance representing the mounted USB device.
        """
        if not self.machine.is_running:
            raise RuntimeError("Machine must be running to hotplug USB drives. Power on the machine first.")

        if not hasattr(self.machine, "console") or not self.machine.console:
            raise RuntimeError("Machine console is required to hotplug USB drives.")

        is_temporary = False
        if disk is None:
            disk_path = self.create_disk(
                path=None,
                size=size,
                filesystem=filesystem,
                label=label,
                files=files,
                source_dir=source_dir,
            )
            is_temporary = True
        else:
            disk_path = Path(disk).resolve()
            if not disk_path.exists():
                disk_path = self.create_disk(
                    path=disk_path,
                    size=size,
                    filesystem=filesystem,
                    label=label,
                    files=files,
                    source_dir=source_dir,
                )

        disk_fmt = "qcow2" if disk_path.suffix.lower() == ".qcow2" else "raw"
        share_uid = uuid.uuid4().hex[:8]
        drive_id = f"usb_drv_{share_uid}"
        dev_id = f"usb_dev_{share_uid}"

        clean_label = re.sub(r"[^A-Za-z0-9_-]", "", label)[:11] or "USB"
        qemu_path = str(disk_path).replace("\\", "/")
        ro_opt = ",readonly=on" if read_only else ""

        # 1. Add drive via QEMU monitor
        add_drive_cmd = f"drive_add 0 file={qemu_path},format={disk_fmt},if=none,id={drive_id}{ro_opt}"
        out = self.machine.console.send_monitor_command(add_drive_cmd)
        out_lower = out.lower()
        if "could not" in out_lower or "error" in out_lower or "failed" in out_lower or "can't" in out_lower:
            raise RuntimeError(f"QEMU failed to add drive for USB storage '{disk_path}': {out.strip()}")

        # 2. Attach USB storage device via QEMU monitor
        add_dev_cmd = f"device_add usb-storage,drive={drive_id},id={dev_id}"
        out = self.machine.console.send_monitor_command(add_dev_cmd)
        out_lower = out.lower()
        if "error" in out_lower or "failed" in out_lower or "can't" in out_lower:
            try:
                self.machine.console.send_monitor_command(f"drive_del {drive_id}")
            except Exception:
                pass
            raise RuntimeError(f"QEMU failed to attach USB storage device '{dev_id}': {out.strip()}")

        # 3. Detect and optionally assign drive letter inside Windows guest
        assigned_letter: str | None = None
        target_letter = str(to).strip().rstrip(":\\/").upper() if to else None

        if hasattr(self.machine, "command") and self.machine.command:
            # Poll guest OS for the USB volume and assign target letter if requested
            detect_script = f"""
$target = '{target_letter or ""}'
$label = '{clean_label}'
$assigned = ''

for ($i = 0; $i -lt 30; $i++) {{
    $usbDisks = Get-Disk | Where-Object {{ $_.BusType -eq 'USB' }}
    foreach ($d in $usbDisks) {{
        if ($d.OperationalStatus -eq 'Offline') {{
            Set-Disk -Number $d.Number -IsOffline $false -ErrorAction SilentlyContinue
        }}
        $parts = Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue | Where-Object {{ $_.Type -ne 'Reserved' -and $_.Size -gt 0 }}
        foreach ($p in $parts) {{
            $cur = $p.DriveLetter
            if ($cur) {{
                $curChar = "$cur".TrimEnd(':').ToUpper()
                if ($target -and $curChar -ne $target) {{
                    try {{
                        Set-Partition -DiskNumber $d.Number -PartitionNumber $p.PartitionNumber -NewDriveLetter $target -ErrorAction Stop
                        $assigned = "$($target):"
                    }} catch {{
                        $assigned = "$($curChar):"
                    }}
                }} else {{
                    $assigned = "$($curChar):"
                }}
                break
            }} else {{
                if ($target) {{
                    try {{
                        Set-Partition -DiskNumber $d.Number -PartitionNumber $p.PartitionNumber -NewDriveLetter $target -ErrorAction Stop
                        $assigned = "$($target):"
                    }} catch {{
                        try {{
                            $p | Add-PartitionAccessPath -AssignDriveLetter -ErrorAction SilentlyContinue
                            $refreshed = Get-Partition -DiskNumber $d.Number -PartitionNumber $p.PartitionNumber
                            if ($refreshed.DriveLetter) {{
                                $assigned = "$($refreshed.DriveLetter):"
                            }}
                        }} catch {{}}
                    }}
                }} else {{
                    try {{
                        $p | Add-PartitionAccessPath -AssignDriveLetter -ErrorAction SilentlyContinue
                        $refreshed = Get-Partition -DiskNumber $d.Number -PartitionNumber $p.PartitionNumber
                        if ($refreshed.DriveLetter) {{
                            $assigned = "$($refreshed.DriveLetter):"
                        }}
                    }} catch {{}}
                }}
                if ($assigned) {{ break }}
            }}
        }}
        if ($assigned) {{ break }}
    }}
    if ($assigned) {{ break }}

    if ($label) {{
        $vol = Get-Volume -FileSystemLabel $label -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($vol -and $vol.DriveLetter) {{
            $curChar = "$($vol.DriveLetter)".TrimEnd(':').ToUpper()
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

        usb_device = USBDevice(
            controller=self,
            device_id=dev_id,
            drive_id=drive_id,
            disk_path=disk_path,
            drive_letter=assigned_letter,
            read_only=read_only,
            is_active=True,
            is_temporary=is_temporary,
        )
        self._devices[dev_id] = usb_device
        return usb_device

    def create_and_mount(
        self,
        path: str | Path | None = None,
        to: str | None = None,
        size: str | int = "512M",
        filesystem: str = "fat32",
        label: str = "USB",
        files: Mapping[str, bytes | str | Path] | None = None,
        source_dir: str | Path | None = None,
        read_only: bool = False,
    ) -> USBDevice:
        """Create a disk image on the host and mount it as a USB drive."""
        return self.mount(
            disk=path,
            to=to,
            read_only=read_only,
            size=size,
            filesystem=filesystem,
            label=label,
            files=files,
            source_dir=source_dir,
        )

    def unmount(self, usb: USBDevice | str) -> None:
        """Dismount and hot-unplug an emulated USB device."""
        dev_id = usb.device_id if isinstance(usb, USBDevice) else str(usb)
        device = self._devices.get(dev_id)
        drive_id = device.drive_id if device else None
        letter = device.drive_letter if device else None

        if letter and hasattr(self.machine, "command") and self.machine.command and self.machine.is_running:
            clean_l = letter.rstrip(":")
            dismount_ps = f"""
try {{
    Get-Volume -DriveLetter '{clean_l}' -ErrorAction SilentlyContinue | Get-Partition | Remove-PartitionAccessPath -Accesspath '{letter}\\' -ErrorAction SilentlyContinue | Out-Null
}} catch {{}}
"""
            try:
                self.machine.command.run(dismount_ps.strip(), powershell=True, timeout=10)
            except Exception:
                pass

        if hasattr(self.machine, "console") and self.machine.console:
            try:
                self.machine.console.send_monitor_command(f"device_del {dev_id}")
            except Exception:
                pass
            if drive_id:
                try:
                    self.machine.console.send_monitor_command(f"drive_del {drive_id}")
                except Exception:
                    pass

        if device:
            device.is_active = False
            if device.is_temporary and device.disk_path.exists():
                try:
                    device.disk_path.unlink()
                except OSError:
                    pass
            self._devices.pop(dev_id, None)

    def unmount_all(self) -> None:
        """Unmount all currently active USB devices."""
        for dev in list(self.devices):
            self.unmount(dev)
