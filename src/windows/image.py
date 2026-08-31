"""Image creation and Windows installation pipeline with image caching and live guest setup log streaming."""

import hashlib
import tempfile
import time
import uuid
from pathlib import Path

from tqdm import tqdm

from windows.console import ConsoleController
from windows.iso import (
    get_image_cache_dir,
)
from windows.qemu import (
    QEMUProcessManager,
    build_install_qemu_cmd,
    create_qcow2_disk,
    create_qcow2_overlay,
    find_free_port,
)
from windows.unattend import (
    create_unattend_iso,
    save_unattend_xml,
)


class Image:
    """Represents an installed Windows disk image (.qcow2)."""

    def __init__(self, disk_path: str | Path):
        self.disk_path = Path(disk_path).resolve()
        if not self.disk_path.exists():
            raise FileNotFoundError(f"Disk image file does not exist: {self.disk_path}")

    def __repr__(self) -> str:
        return f"<Image disk_path={str(self.disk_path)!r}>"


def _is_valid_installed_image(path: Path) -> bool:
    """Return True if path exists and contains a valid non-empty disk image."""
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _get_installed_image_cache_path(iso_path: str | Path) -> Path:
    """Generate a canonical cache filename for an installed disk image based on ISO path."""
    cache_dir = get_image_cache_dir()
    iso_str = str(iso_path).strip().lower()

    file_hash = hashlib.md5(str(Path(iso_str).resolve()).encode("utf-8")).hexdigest()[:8]
    stem = Path(iso_str).stem.lower()
    return cache_dir / f"win_{stem}_{file_hash}_installed.qcow2"


def _infer_image_name(iso_path: Path) -> str:
    """Infer the Windows edition name from the ISO filename for unattended install."""
    name = iso_path.stem.lower()
    if "win11" in name or "11" in name:
        return "Windows 11 Pro"
    return "Windows 10 Pro"


def create_image_from_iso(
    iso_path: str | Path,
    output_disk: str | Path | None = None,
    disk_size: str = "50G",
    ram_mb: int = 4096,
    cpus: int = 4,
    enable_kvm: bool = True,
    headless: bool = True,
    winrm_port: int | None = None,
    vnc_display: int | None = None,
    language: str = "en-US",
    input_locale: str = "en-US",
    time_zone: str = "UTC",
    image_name: str | None = None,
    timeout_minutes: int = 45,
    interactive: bool = False,
    use_cache: bool = True,
) -> Image:
    """Provision a fully installed unattended Windows QEMU VM disk image from an ISO.

    Args:
        iso_path: Path to a local Windows ISO file.
                  Use windows.get_iso() to fetch/download an ISO first.
        output_disk: Path for the output qcow2 disk image. If None, generates a unique random name.
        image_name: Windows edition name for unattend (e.g. "Windows 10 Pro").
                    Auto-detected from ISO filename if not specified.

    Example:
        iso = windows.get_iso(WindowsVersion.WIN10_22H2)
        image = windows.create_image_from_iso(iso)
    """
    iso_path = Path(iso_path).resolve()
    if not iso_path.exists():
        raise FileNotFoundError(f"ISO file does not exist: {iso_path}")

    if output_disk is None:
        random_suffix = uuid.uuid4().hex[:8]
        output_disk = Path(f"windows_overlay_{random_suffix}.qcow2")
    else:
        output_disk = Path(output_disk)
    output_disk = output_disk.resolve()
    cached_image_path = _get_installed_image_cache_path(iso_path)

    # 1. Check if a pre-installed image is already cached
    if use_cache and _is_valid_installed_image(cached_image_path):
        print(f"[windows] Reusing cached installed Windows base image: {cached_image_path}")
        output_disk.parent.mkdir(parents=True, exist_ok=True)
        if output_disk != cached_image_path:
            create_qcow2_overlay(output_disk, cached_image_path)
            print(f"[windows] Created QEMU overlay disk {output_disk} backing {cached_image_path}")
        return Image(output_disk)

    # 2. Infer image name if not explicitly provided
    if image_name is None:
        image_name = _infer_image_name(iso_path)

    # Install directly to cached_image_path if caching is enabled, else to output_disk
    install_target = cached_image_path if use_cache else output_disk
    create_qcow2_disk(install_target, size=disk_size)

    # Auto-allocate free ports if occupied
    actual_winrm_port = find_free_port(winrm_port if winrm_port is not None else 5985)

    if vnc_display is not None:
        actual_vnc_port = find_free_port(5900 + vnc_display)
        actual_vnc_display = actual_vnc_port - 5900
    else:
        actual_vnc_port = find_free_port(5900)
        actual_vnc_display = actual_vnc_port - 5900

    console_ctrl = ConsoleController(
        host="127.0.0.1",
        port=actual_vnc_port,
        display_index=actual_vnc_display,
    )

    with tempfile.TemporaryDirectory(prefix="windows_unattend_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        save_unattend_xml(
            tmp_path,
            language=language,
            input_locale=input_locale,
            time_zone=time_zone,
            image_name=image_name,
        )

        unattend_iso_path = create_unattend_iso(
            tmp_path / "unattend.iso",
            language=language,
            input_locale=input_locale,
            time_zone=time_zone,
            image_name=image_name,
        )

        serial_log_path = tmp_path / "guest_setup_serial.log"
        serial_log_path.touch(exist_ok=True)
        monitor_socket_path = tmp_path / "qemu_monitor.sock"
        qga_socket_path = tmp_path / "qga.sock"

        actual_ssh_port = find_free_port(2222)

        cmd = build_install_qemu_cmd(
            disk_path=install_target,
            iso_path=iso_path,
            unattend_dir=tmp_path,
            unattend_iso_path=unattend_iso_path,
            ram_mb=ram_mb,
            cpus=cpus,
            enable_kvm=enable_kvm,
            headless=headless,
            winrm_port=actual_winrm_port,
            ssh_port=actual_ssh_port,
            vnc_display=actual_vnc_display,
            serial_log_path=serial_log_path,
            monitor_socket_path=monitor_socket_path,
            qga_socket_path=qga_socket_path,
        )

        manager = QEMUProcessManager(cmd)
        manager.start()
        manager.send_boot_keypress(monitor_socket_path)

        if interactive:
            print(
                f"[windows] Opening interactive VNC screen viewer for installation debugging ({console_ctrl.info.vnc_url})..."
            )
            console_ctrl.open()

        total_seconds = timeout_minutes * 60
        install_phases = [
            "Booting installer",
            "Copying files",
            "Getting files ready",
            "Installing features",
            "Installing updates",
            "Configuring settings",
            "OOBE / First login",
            "Shutting down",
        ]
        try:
            with tqdm(
                total=len(install_phases),
                desc=f"Installing Windows to {output_disk.name}",
                unit="phase",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} phases [{elapsed}<{remaining}, {postfix}]",
            ) as pbar:
                current_phase = 0
                pbar.set_postfix_str(install_phases[0])
                elapsed = 0
                step_interval = 5

                while elapsed < total_seconds:
                    if not manager.is_running():
                        # QEMU exited — installation completed and VM shut down
                        pbar.n = len(install_phases)
                        pbar.set_postfix_str("Complete ✓")
                        pbar.refresh()
                        break

                    time.sleep(step_interval)
                    elapsed += step_interval

                    # Estimate phase from elapsed time (cap at OOBE; final phases only on QEMU exit)
                    progress_ratio = min(elapsed / total_seconds, 1.0)
                    max_time_phase = len(install_phases) - 2  # Don't reach "Shutting down" by time alone
                    new_phase = min(int(progress_ratio * max_time_phase), max_time_phase)

                    if new_phase > current_phase:
                        pbar.update(new_phase - current_phase)
                        current_phase = new_phase
                        if current_phase < len(install_phases):
                            pbar.set_postfix_str(install_phases[current_phase])
                else:
                    # Timeout reached without QEMU exiting
                    pbar.set_postfix_str("Timeout ✗")
                    pbar.refresh()
        finally:
            if interactive:
                console_ctrl.close()
            manager.stop()

    # 3. Create thin QEMU overlay disk backing the cached base image if applicable
    if use_cache and _is_valid_installed_image(cached_image_path):
        if output_disk != cached_image_path:
            create_qcow2_overlay(output_disk, cached_image_path)
            print(f"[windows] Created QEMU overlay disk {output_disk} backing {cached_image_path}")

    return Image(output_disk)
