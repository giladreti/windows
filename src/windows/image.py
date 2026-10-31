import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from tqdm import tqdm

from windows.console import ConsoleController
from windows.disk import (
    DiskController,
    ImageFileController,
    ImagePath,
    MountedPartition,
    PartitionInfo,
)
from windows.iso import (
    get_image_cache_dir,
    list_cached_images,
)
from windows.qemu import (
    QEMUProcessManager,
    build_install_qemu_cmd,
    build_xp_install_qemu_cmd,
    create_qcow2_disk,
    create_qcow2_overlay,
    find_free_port,
)
from windows.unattend import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    DEFAULT_XP_PRODUCT_KEY,
    XP_DEFAULT_COMPUTER_NAME,
    create_unattend_iso,
    create_xp_unattend_floppy,
    save_unattend_xml,
)

if TYPE_CHECKING:
    from windows.iso import ISO, WindowsVersion


class Image:
    """Represents an installed Windows disk image (.qcow2)."""

    def __init__(self, disk_path: "Image | str | Path | os.PathLike"):
        if isinstance(disk_path, Image):
            self.disk_path = disk_path.disk_path
        else:
            self.disk_path = Path(disk_path).resolve()
        if not self.disk_path.exists():
            raise FileNotFoundError(f"Disk image file does not exist: {self.disk_path}")

    @property
    def path(self) -> Path:
        """Alias for disk_path."""
        return self.disk_path

    @property
    def name(self) -> str:
        """Return filename of the disk image."""
        return self.disk_path.name

    @property
    def stem(self) -> str:
        """Return stem (filename without extension) of the disk image."""
        return self.disk_path.stem

    def exists(self) -> bool:
        """Return True if the disk image exists."""
        return self.disk_path.exists()

    def stat(self) -> os.stat_result:
        """Return stat result of the disk image."""
        return self.disk_path.stat()

    def __fspath__(self) -> str:
        return str(self.disk_path)

    def __str__(self) -> str:
        return str(self.disk_path)

    def __repr__(self) -> str:
        return f"<Image disk_path={str(self.disk_path)!r}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Image):
            return self.disk_path == other.disk_path
        if isinstance(other, (str, Path, os.PathLike)):
            return self.disk_path == Path(other).resolve()
        return False

    def __hash__(self) -> int:
        return hash(self.disk_path)

    @property
    def backing_file(self) -> Path | None:
        """Return the path to the backing disk image if this image is an overlay, else None."""
        try:
            from windows.qemu import find_qemu_img_binary

            qemu_img = find_qemu_img_binary()
            res = subprocess.run(
                [qemu_img, "info", "--output=json", str(self.disk_path)],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                backing = data.get("full-backing-filename") or data.get("backing-filename")
                if backing:
                    return Path(backing)
        except Exception:
            pass
        return None

    @property
    def is_overlay(self) -> bool:
        """Return True if this image is an overlay backing another disk image."""
        return self.backing_file is not None

    @property
    def base(self) -> "Image | None":
        """Return an Image instance of the backing disk image if this image is an overlay, else None."""
        bf = self.backing_file
        if bf and bf.exists():
            return Image(bf)
        return None

    @property
    def file(self) -> ImageFileController:
        """Path-like offline file controller for this disk image (e.g. `image.file / 'C:\\Windows'`)."""
        if not hasattr(self, "_file_controller") or self._file_controller is None:
            self._file_controller = ImageFileController(self)
        return self._file_controller

    def __truediv__(self, remote_path: str | Path) -> ImagePath:
        """Support image / 'C:\\path' syntax as an alias for image.file / 'C:\\path'."""
        return self.file / remote_path

    @property
    def disk(self) -> DiskController:
        """Offline disk controller for analyzing partitions, listing files, and reading/writing files."""
        if not hasattr(self, "_disk_controller") or self._disk_controller is None:
            self._disk_controller = DiskController(self)
        return self._disk_controller

    def partitions(self) -> list[PartitionInfo]:
        """Return all disk partitions found in this image."""
        return self.disk.partitions()

    def mount(self, partition: int | None = None, writable: bool = False) -> MountedPartition:
        """Mount an NTFS partition from this offline disk image via FUSE.

        Usage:
            with image.mount(writable=True) as disk:
                print(disk.list_files("Windows"))
                disk.write_text("test.txt", "hello")
        """
        return self.disk.mount(partition=partition, writable=writable)

    def list_files(self, path: str = "", partition: int | None = None, recursive: bool = False) -> list[str]:
        """List files in an offline directory inside the image's Windows partition."""
        return self.disk.list_files(path=path, partition=partition, recursive=recursive)

    def read_text(self, path: str, encoding: str = "utf-8", partition: int | None = None) -> str:
        """Read text from a file inside the offline image's Windows partition."""
        return self.disk.read_text(path=path, encoding=encoding, partition=partition)

    def read_bytes(self, path: str, partition: int | None = None) -> bytes:
        """Read binary bytes from a file inside the offline image's Windows partition."""
        return self.disk.read_bytes(path=path, partition=partition)

    def write_text(self, path: str, text: str, encoding: str = "utf-8", partition: int | None = None) -> None:
        """Write text into a file inside the offline image's Windows partition."""
        self.disk.write_text(path=path, text=text, encoding=encoding, partition=partition)

    def write_bytes(self, path: str, data: bytes, partition: int | None = None) -> None:
        """Write binary data into a file inside the offline image's Windows partition."""
        self.disk.write_bytes(path=path, data=data, partition=partition)

    def file_exists(self, path: str, partition: int | None = None) -> bool:
        """Return True if a file exists inside the offline image's Windows partition."""
        return self.disk.exists(path=path, partition=partition)

    @classmethod
    def from_base(
        cls,
        base: "Image | str | Path | os.PathLike | None" = None,
        output_disk: "Image | str | Path | os.PathLike | None" = None,
        *,
        base_image: "Image | str | Path | os.PathLike | None" = None,
        overlay_path: "Image | str | Path | os.PathLike | None" = None,
        size: str | None = None,
    ) -> "Image":
        """Create a thin qcow2 overlay backed by a base Windows disk image.

        The overlay only stores writes/diffs on top of the base image (~200KB initial size),
        enabling instant reuse and branching of base images without copying.
        Optionally, a custom virtual size (e.g. '60G') can be specified to expand disk capacity.

        Args:
            base: An existing Image instance, str, Path, or os.PathLike pointing to a base qcow2 image.
            output_disk: Path for the new overlay disk image. If None, generates a unique random name.
            base_image: Alias for base.
            overlay_path: Alias for output_disk.
            size: Optional expanded virtual size for the overlay (e.g. '60G').

        Returns:
            An Image instance referencing the newly created overlay disk.

        Raises:
            ValueError: If neither base nor base_image is provided, or if output_disk equals base.
            FileNotFoundError: If the base image file does not exist.
        """
        source = base if base is not None else base_image
        if source is None:
            raise ValueError("A base image must be provided.")

        if isinstance(source, Image):
            base_path = source.disk_path
        elif isinstance(source, (str, Path, os.PathLike)):
            base_path = Path(source).resolve()
        else:
            raise TypeError(f"Unsupported base image type: {type(source).__name__}")

        if not base_path.exists():
            raise FileNotFoundError(f"Base image file does not exist: {base_path}")

        target = overlay_path if overlay_path is not None else output_disk
        if target is None:
            random_suffix = uuid.uuid4().hex[:8]
            target = Path(f"{base_path.stem}_overlay_{random_suffix}.qcow2").resolve()
        else:
            if isinstance(target, Image):
                target = target.disk_path
            else:
                target = Path(target)
            target = target.resolve()
            if target.is_dir():
                random_suffix = uuid.uuid4().hex[:8]
                target = target / f"{base_path.stem}_overlay_{random_suffix}.qcow2"

        if target == base_path:
            raise ValueError("Overlay path cannot be the same as the base image path.")

        create_qcow2_overlay(target, base_path, size=size)
        return cls(target)

    def create_overlay(
        self,
        output_disk: "Image | str | Path | os.PathLike | None" = None,
        *,
        overlay_path: "Image | str | Path | os.PathLike | None" = None,
        size: str | None = None,
    ) -> "Image":
        """Create a thin qcow2 overlay backed by this base image."""
        return self.from_base(self, output_disk=output_disk, overlay_path=overlay_path, size=size)

    def overlay(
        self,
        output_disk: "Image | str | Path | os.PathLike | None" = None,
        *,
        overlay_path: "Image | str | Path | os.PathLike | None" = None,
        size: str | None = None,
    ) -> "Image":
        """Alias for create_overlay()."""
        return self.create_overlay(output_disk=output_disk, overlay_path=overlay_path, size=size)

    @classmethod
    def from_iso(
        cls,
        iso: "ISO | str | Path | WindowsVersion",
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
    ) -> "Image":
        """Provision a fully installed unattended Windows QEMU VM disk image from an ISO.

        Args:
            iso: An ISO instance, WindowsVersion enum, version alias string, or path to a local ISO file.
            output_disk: Path for the output qcow2 disk image. If None, generates a unique random name.
            disk_size: Target disk size (e.g. "50G").
            ram_mb: VM RAM in megabytes.
            cpus: Number of vCPUs.
            enable_kvm: Enable KVM acceleration if supported.
            headless: Run headless without graphical window.
            winrm_port: Custom port for WinRM forwarding.
            vnc_display: Custom VNC display index (e.g. 0 -> port 5900).
            language: Windows display language (e.g. "en-US").
            input_locale: Input locale / keyboard layout.
            time_zone: Windows time zone identifier.
            image_name: Windows edition name for unattend (e.g. "Windows 10 Pro"). Auto-detected if None.
            timeout_minutes: Installation timeout in minutes.
            interactive: If True, opens a VNC viewer console window during installation.
            use_cache: If True, uses cached pre-installed base image and creates a thin overlay.

        Example:
            iso = ISO.from_version(WindowsVersion.WIN10_22H2)
            image = Image.from_iso(iso)
            # or directly from version:
            image = Image.from_iso(WindowsVersion.WIN10_22H2)
        """
        from windows.iso import ISO, resolve_iso

        if isinstance(iso, ISO):
            iso_path = iso.path
        elif isinstance(iso, Path):
            iso_path = iso.resolve()
            if not iso_path.exists():
                raise FileNotFoundError(f"ISO file does not exist: {iso_path}")
        elif isinstance(iso, str) and Path(iso).exists() and Path(iso).is_file():
            iso_path = Path(iso).resolve()
        else:
            iso_path = resolve_iso(iso)

        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file does not exist: {iso_path}")

        # Route legacy Windows XP media (NT 5.1) to the XP provisioning flow,
        # which uses i440fx/IDE + winnt.sif instead of q35 + autounattend.xml.
        from windows.iso import WindowsVersion

        if WindowsVersion.is_xp(iso) or _looks_like_xp_iso(Path(iso_path)):
            return cls.from_xp_iso(
                Path(iso_path),
                output_disk=output_disk,
                disk_size=disk_size,
                ram_mb=ram_mb,
                cpus=cpus,
                enable_kvm=enable_kvm,
                headless=headless,
                vnc_display=vnc_display,
                timeout_minutes=timeout_minutes,
                interactive=interactive,
                use_cache=use_cache,
            )

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
                if disk_size and disk_size != "50G":
                    create_qcow2_overlay(output_disk, cached_image_path, size=disk_size)
                else:
                    create_qcow2_overlay(output_disk, cached_image_path)
                print(f"[windows] Created QEMU overlay disk {output_disk} backing {cached_image_path}")
            return cls(output_disk)

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
                if disk_size and disk_size != "50G":
                    create_qcow2_overlay(output_disk, cached_image_path, size=disk_size)
                else:
                    create_qcow2_overlay(output_disk, cached_image_path)
                print(f"[windows] Created QEMU overlay disk {output_disk} backing {cached_image_path}")

        return cls(output_disk)

    @classmethod
    def from_xp_iso(
        cls,
        iso: "ISO | str | Path | WindowsVersion",
        output_disk: str | Path | None = None,
        disk_size: str = "20G",
        ram_mb: int = 1024,
        cpus: int = 2,
        enable_kvm: bool = True,
        headless: bool = True,
        vnc_display: int | None = None,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        product_key: str = DEFAULT_XP_PRODUCT_KEY,
        computer_name: str = XP_DEFAULT_COMPUTER_NAME,
        full_name: str | None = None,
        time_zone: str = "UTC",
        workgroup: str = "WORKGROUP",
        timeout_minutes: int = 90,
        interactive: bool = False,
        use_cache: bool = True,
    ) -> "Image":
        """Provision a fully unattended Windows XP (NT 5.1) VM disk image from a stock XP ISO.

        The untouched (plain) XP install CD is used as-is: setup is fed via a
        legacy FAT12 floppy image containing `WINNT.SIF`
        (windows.unattend.create_xp_unattend_floppy). XP's cdboot polls
        ``A:\\WINNT.SIF`` on start, so both the text-mode and GUI setup phases run
        without any interaction.

        Because XP has no inbox AHCI driver, the media runs on the legacy i440fx
        (``pc``) machine type with PIIX3 IDE storage instead of the q35 profile.
        The multi-reboot installer runs as two QEMU phases (CD-boot text-mode ->
        disk-boot GUI setup), each with ``-no-reboot`` so QEMU exits exactly when
        the guest reboots between phases.

        Args:
            iso: An ISO instance, WindowsVersion.XP_* enum, XP alias string
                ("xp", "xp_sp3", "winxp", ...), or path to a local XP ISO file.
            output_disk: Path for the output qcow2 disk image. If None, generates a unique random name.
            disk_size: Target disk size (e.g. "20G").
            ram_mb: VM RAM in megabytes (XP sweet spot: 512-2048).
            cpus: Number of vCPUs.
            enable_kvm: Enable KVM acceleration if supported.
            headless: Run headless without graphical window (VNC always on).
            vnc_display: Custom VNC display index (e.g. 0 -> port 5900).
            username: Legacy account name for FullName/answer file.
            password: Administrator account password (AdminPassword).
            product_key: 25-char XP product key (dashes optional). Defaults to the
                retail EN key published for the XP SP1 MSDN media.
            computer_name: XP computer name (max 15 chars).
            full_name: Setup "Full Name" (defaults to username).
            time_zone: XP time zone index will be inferred (e.g. "UTC", "Europe/Jerusalem", "America/New_York").
            workgroup: Workgroup name to join.
            timeout_minutes: Installation timeout in minutes (total for both phases).
            interactive: If True, opens a VNC viewer console window per phase.
            use_cache: If True, uses cached pre-installed XP images and creates a thin overlay.

        Example:
            image = Image.from_xp_iso(WindowsVersion.XP_SP1)
        """
        from windows.iso import ISO, resolve_iso

        if isinstance(iso, ISO):
            iso_path = iso.path
        elif isinstance(iso, Path):
            iso_path = iso.resolve()
            if not iso_path.exists():
                raise FileNotFoundError(f"ISO file does not exist: {iso_path}")
        elif isinstance(iso, str) and Path(iso).exists() and Path(iso).is_file():
            iso_path = Path(iso).resolve()
        else:
            iso_path = resolve_iso(iso)

        if not iso_path.exists():
            raise FileNotFoundError(f"ISO file does not exist: {iso_path}")

        if output_disk is None:
            random_suffix = uuid.uuid4().hex[:8]
            output_disk = Path(f"windows_overlay_{random_suffix}.qcow2")
        else:
            output_disk = Path(output_disk)
        output_disk = output_disk.resolve()
        cached_image_path = _get_installed_image_cache_path(iso_path)

        # 1. Reuse a cached XP base image when available
        if use_cache and _is_valid_installed_image(cached_image_path):
            print(f"[windows] Reusing cached installed Windows XP base image: {cached_image_path}")
            output_disk.parent.mkdir(parents=True, exist_ok=True)
            if output_disk != cached_image_path:
                create_qcow2_overlay(output_disk, cached_image_path)
                print(f"[windows] Created QEMU overlay disk {output_disk} backing {cached_image_path}")
            return cls(output_disk)

        install_target = cached_image_path if use_cache else output_disk
        create_qcow2_disk(install_target, size=disk_size)

        with tempfile.TemporaryDirectory(prefix="windows_xp_unattend_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            floppy_path = create_xp_unattend_floppy(
                tmp_path / "unattend_floppy.img",
                product_key=product_key,
                username=username,
                password=password,
                computer_name=computer_name,
                full_name=full_name,
                time_zone=time_zone,
                workgroup=workgroup,
            )
            serial_log_path = tmp_path / "xp_install_serial.log"
            monitor_socket_path = tmp_path / "xp_monitor.sock"

            phases = [
                ("d", "Text-mode setup (partition + file copy)"),
                ("c", "GUI setup (automated first boot)"),
            ]
            total_seconds = timeout_minutes * 60

            with tqdm(
                total=len(phases),
                desc=f"Installing Windows XP from {Path(iso_path).name}",
                unit="phase",
            ) as pbar:
                for boot_letter, phase_label in phases:
                    pbar.set_postfix_str(phase_label)

                    port = vnc_display if vnc_display is not None else find_free_port(5900)
                    actual_vnc_display = port - 5900

                    cmd = build_xp_install_qemu_cmd(
                        disk_path=install_target,
                        iso_path=iso_path,
                        unattend_floppy_path=floppy_path,
                        ram_mb=ram_mb,
                        cpus=cpus,
                        enable_kvm=enable_kvm,
                        headless=headless,
                        vnc_display=actual_vnc_display,
                        serial_log_path=serial_log_path,
                        monitor_socket_path=monitor_socket_path,
                        boot_from=boot_letter,
                        no_reboot=True,
                    )
                    console_ctrl = ConsoleController(host="127.0.0.1", port=port, display_index=actual_vnc_display)
                    manager = QEMUProcessManager(cmd)
                    manager.start()

                    if interactive:
                        print(f"[windows] Opening VNC viewer ({console_ctrl.info.vnc_url}) -> {phase_label}...")
                        console_ctrl.open()

                    try:
                        if boot_letter == "d":
                            # Consume the 'Press any key to boot from CD' prompt
                            manager.send_boot_keypress(monitor_socket_path)

                        deadline = time.time() + total_seconds / len(phases)
                        while manager.is_running():
                            if time.time() > deadline:
                                raise RuntimeError(
                                    f"Windows XP setup phase '{phase_label}' timed out after "
                                    f"{total_seconds // len(phases)}s. Re-run with interactive=True "
                                    "and inspect the VNC screen."
                                )
                            time.sleep(5)
                    finally:
                        if interactive:
                            console_ctrl.close()
                        manager.stop()
                    pbar.update(1)

        if use_cache and _is_valid_installed_image(cached_image_path):
            if output_disk != cached_image_path:
                create_qcow2_overlay(output_disk, cached_image_path)
                print(f"[windows] Created QEMU overlay disk {output_disk} backing {cached_image_path}")

        return cls(output_disk)

    @classmethod
    def list(cls, cache_dir: str | Path | None = None) -> list["Image"]:
        """List all cached installed Windows disk images as Image instances."""
        return [cls(p) for p in list_cached_images(cache_dir=cache_dir)]


def _is_valid_installed_image(path: Path) -> bool:
    """Return True if path exists and contains a valid non-empty disk image."""
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _looks_like_xp_iso(iso_path: Path) -> bool:
    """Heuristically detect Windows XP install media from a local ISO filename.

    Matches tokens like 'win_xp_sp1' / 'wxpvol_en' / 'WindowsXP...' in the ISO stem.
    """
    name = iso_path.name.replace("_", " ").replace("-", " ").lower()
    tokens = name.split()
    return "xp" in tokens or any(
        token.startswith("wxp") or token.startswith("winxp") or token == "windowsxp" for token in tokens
    )


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
