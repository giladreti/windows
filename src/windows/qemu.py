"""QEMU process runner, port selection, and process management."""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


class QEMUError(RuntimeError):
    """Exception raised for QEMU execution failures."""


def find_qemu_binary() -> str:
    """Find available qemu-system-x86_64 binary."""
    try:
        binary = shutil.which("qemu-system-x86_64")
        if binary:
            return binary
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            binary = shutil.which("qemu-system-x86_64.exe")
            if binary:
                return binary
        except Exception:
            pass
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu" / "qemu-system-x86_64.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "qemu" / "qemu-system-x86_64.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "qemu" / "qemu-system-x86_64.exe",
            Path(r"C:\ProgramData\chocolatey\bin\qemu-system-x86_64.exe"),
            Path.home() / "scoop" / "shims" / "qemu-system-x86_64.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    raise QEMUError("qemu-system-x86_64 is not installed or not found in PATH.")


def find_qemu_img_binary() -> str:
    """Find available qemu-img binary."""
    try:
        binary = shutil.which("qemu-img")
        if binary:
            return binary
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            binary = shutil.which("qemu-img.exe")
            if binary:
                return binary
        except Exception:
            pass
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu" / "qemu-img.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "qemu" / "qemu-img.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "qemu" / "qemu-img.exe",
            Path(r"C:\ProgramData\chocolatey\bin\qemu-img.exe"),
            Path.home() / "scoop" / "shims" / "qemu-img.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
    raise QEMUError("qemu-img is not installed or not found in PATH.")


def find_qemu_keymap(layout: str = "en-us") -> str | None:
    """Find the path to a QEMU keymap file.

    On Windows, QEMU's internal keymap loader frequently fails to resolve default
    keymaps (e.g. 'en-us') relative to its working directory or installation prefix,
    causing '-vnc ...: Could not open 'en-us': Permission denied'. Supplying the
    absolute path via '-k <path>' resolves this issue.
    """
    try:
        qemu_bin = Path(find_qemu_binary()).resolve()
        bin_dir = qemu_bin.parent
        candidates = [
            bin_dir / "share" / "keymaps" / layout,
            bin_dir / "keymaps" / layout,
            bin_dir / "share" / "qemu" / "keymaps" / layout,
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu" / "share" / "keymaps" / layout,
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
            / "qemu"
            / "share"
            / "keymaps"
            / layout,
            Path(r"C:\ProgramData\chocolatey\lib\qemu\tools\share\keymaps") / layout,
        ]
        if sys.platform != "win32":
            candidates.extend(
                [
                    Path("/usr/share/qemu/keymaps") / layout,
                    Path("/usr/share/qemu-system-x86/keymaps") / layout,
                    Path("/usr/local/share/qemu/keymaps") / layout,
                ]
            )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    except Exception:
        pass
    return None


def is_tcp_endpoint(endpoint: Any) -> bool:
    """Return True if the endpoint represents a TCP socket (tuple, int port, or tcp string)."""
    if isinstance(endpoint, tuple) and len(endpoint) == 2:
        return True
    if isinstance(endpoint, int):
        return True
    if isinstance(endpoint, str):
        if endpoint.startswith("tcp:"):
            return True
        if ":" in endpoint and not (len(endpoint) >= 2 and endpoint[1] == ":"):
            return True
    return False


def parse_tcp_endpoint(endpoint: Any) -> tuple[str, int]:
    """Extract (host, port) from a TCP endpoint representation."""
    if isinstance(endpoint, tuple):
        return str(endpoint[0]), int(endpoint[1])
    if isinstance(endpoint, int):
        return "127.0.0.1", endpoint
    if isinstance(endpoint, str):
        clean = endpoint.removeprefix("tcp:")
        parts = clean.split(":")
        return parts[0], int(parts[1])
    return "127.0.0.1", int(endpoint)


def format_chardev_args(chardev_id: str, endpoint: Any, server: bool = True, wait: bool = False) -> list[str]:
    """Build QEMU -chardev argument list for TCP or Unix socket."""
    srv_str = "server=on" if server else "server=off"
    wait_str = "wait=on" if wait else "wait=off"
    if is_tcp_endpoint(endpoint):
        host, port = parse_tcp_endpoint(endpoint)
        return ["-chardev", f"socket,id={chardev_id},host={host},port={port},{srv_str},{wait_str}"]
    return ["-chardev", f"socket,id={chardev_id},path={endpoint},{srv_str},{wait_str}"]


def format_monitor_args(endpoint: Any) -> list[str]:
    """Build QEMU -monitor argument list for TCP or Unix socket."""
    if is_tcp_endpoint(endpoint):
        host, port = parse_tcp_endpoint(endpoint)
        return ["-monitor", f"tcp:{host}:{port},server,nowait"]
    return ["-monitor", f"unix:{endpoint},server,nowait"]


def format_qmp_args(endpoint: Any) -> list[str]:
    """Build QEMU -qmp argument list for TCP or Unix socket."""
    if is_tcp_endpoint(endpoint):
        host, port = parse_tcp_endpoint(endpoint)
        return ["-qmp", f"tcp:{host}:{port},server,nowait"]
    return ["-qmp", f"unix:{endpoint},server,nowait"]


def connect_socket(endpoint: Any, timeout: float = 3.0) -> socket.socket:
    """Connect to a TCP or Unix domain socket based on endpoint type."""
    if is_tcp_endpoint(endpoint):
        host, port = parse_tcp_endpoint(endpoint)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        return s
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(endpoint))
    return s


def find_free_port(
    start_port: int = 5985,
    max_attempts: int = 100,
    exclude: set[int] | None = None,
) -> int:
    """Find the next available TCP port on localhost starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        if exclude and port in exclude:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if sys.platform == "win32":
                so_exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", -5)
                try:
                    s.setsockopt(socket.SOL_SOCKET, so_exclusive, 1)
                except OSError:
                    pass
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


def create_qcow2_disk(path: Path, size: str = "50G") -> Path:
    """Create a new qcow2 virtual disk using qemu-img."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    qemu_img = find_qemu_img_binary()

    cmd = [qemu_img, "create", "-f", "qcow2", str(path), size]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise QEMUError(f"Failed to create qcow2 disk image: {res.stderr}")
    return path


def create_qcow2_overlay(overlay_path: Path, backing_path: Path) -> Path:
    """Create a thin qcow2 overlay that uses backing_path as a read-only base.

    The overlay file is tiny (~200KB) and only stores writes/diffs on top of the
    backing image. This enables instant reuse of cached base images without copying.
    """
    overlay_path = Path(overlay_path)
    backing_path = Path(backing_path).resolve()
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    qemu_img = find_qemu_img_binary()

    cmd = [
        qemu_img,
        "create",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        str(backing_path),
        str(overlay_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise QEMUError(f"Failed to create qcow2 overlay: {res.stderr}")
    return overlay_path


def build_install_qemu_cmd(
    disk_path: Path,
    iso_path: Path,
    unattend_dir: Path,
    unattend_iso_path: Path | None = None,
    ram_mb: int = 4096,
    cpus: int = 4,
    enable_kvm: bool = True,
    headless: bool = True,
    winrm_port: int = 5985,
    ssh_port: int = 2222,
    vnc_display: int | None = 0,
    serial_log_path: Path | None = None,
    monitor_socket_path: Any | None = None,
    qga_socket_path: Any | None = None,
    qga_endpoint: Any | None = None,
    monitor_endpoint: Any | None = None,
) -> list[str]:
    """Build QEMU command line arguments for automated Windows ISO installation."""
    cmd = [
        find_qemu_binary(),
        "-machine",
        "q35",
        "-m",
        str(ram_mb),
        "-smp",
        str(cpus),
        "-drive",
        f"file={disk_path},format=qcow2,index=0,media=disk",
        "-cdrom",
        str(iso_path),
    ]

    if unattend_iso_path:
        cmd.extend(["-drive", f"file={unattend_iso_path},media=cdrom,index=3"])

    cmd.extend(
        [
            "-usb",
            "-drive",
            f"id=unattend_usb,file=fat:rw:label=OEMDRIVERS:{unattend_dir},format=raw,if=none",
            "-device",
            "usb-storage,drive=unattend_usb",
            "-boot",
            "order=d",
            "-netdev",
            f"user,id=net0,hostfwd=tcp::{winrm_port}-:5985,hostfwd=tcp::{ssh_port}-:22",
            "-device",
            "e1000,netdev=net0",
            "-rtc",
            "base=localtime",
        ]
    )

    qga_ep = qga_endpoint if qga_endpoint is not None else qga_socket_path
    if qga_ep is not None:
        cmd.extend(format_chardev_args("qga0", qga_ep, server=True, wait=False))
        cmd.extend(
            [
                "-device",
                "virtio-serial-pci",
                "-device",
                "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            ]
        )

    if vnc_display is not None:
        cmd.extend(["-vnc", f"127.0.0.1:{vnc_display}"])
        if sys.platform == "win32":
            keymap = find_qemu_keymap()
            if keymap:
                cmd.extend(["-k", keymap])

    if serial_log_path:
        cmd.extend(["-serial", f"file:{serial_log_path}"])

    mon_ep = monitor_endpoint if monitor_endpoint is not None else monitor_socket_path
    if mon_ep is not None:
        cmd.extend(format_monitor_args(mon_ep))

    if sys.platform == "win32":
        if enable_kvm and is_whpx_available():
            cmd.extend(["-accel", "whpx", "-cpu", "max"])
        else:
            cmd.extend(["-cpu", "qemu64"])
    elif enable_kvm and os.path.exists("/dev/kvm"):
        cmd.extend(["-enable-kvm", "-cpu", "host"])
    else:
        cmd.extend(["-cpu", "qemu64"])

    if headless and vnc_display is None:
        cmd.extend(["-display", "none"])

    return cmd


def get_host_cpu_vendor() -> str:
    """Detect host CPU vendor from /proc/cpuinfo or environment, defaulting to GenuineIntel."""
    if sys.platform == "win32":
        ident = os.environ.get("PROCESSOR_IDENTIFIER", "")
        if "AMD" in ident or "AuthenticAMD" in ident:
            return "AuthenticAMD"
        return "GenuineIntel"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("vendor_id"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "GenuineIntel"


def is_whpx_available() -> bool:
    """Check if Windows Hypervisor Platform (WHPX) is available and functional on Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        winhv = ctypes.WinDLL("WinHvPlatform.dll")
        # WHvCapabilityCodeHypervisorPresent = 0
        present = wintypes.BOOL()
        written = wintypes.UINT()
        hr = winhv.WHvGetCapability(
            0,
            ctypes.byref(present),
            ctypes.sizeof(present),
            ctypes.byref(written),
        )
        return hr == 0 and bool(present.value)
    except Exception:
        return False


def is_kvm_available() -> bool:
    """Check if hardware virtualization acceleration is available on the current host."""
    if sys.platform == "win32":
        return is_whpx_available()
    return os.path.exists("/dev/kvm")


def build_run_qemu_cmd(
    disk_path: Path,
    ram_mb: int = 4096,
    cpus: int = 4,
    enable_kvm: bool = True,
    headless: bool = True,
    vnc_display: int | None = 0,
    monitor_socket_path: Any | None = None,
    qga_socket_path: Any | None = None,
    gdb_port: int | None = None,
    stop_at_boot: bool = False,
    qmp_socket_path: Any | None = None,
    icount: str | None = None,
    rr_mode: str | None = None,
    rr_file: Path | str | None = None,
    rr_snapshot: str | None = None,
    blkreplay: bool = False,
    filter_replay: bool = False,
    loadvm: str | None = None,
    ttd: bool = False,
    qga_endpoint: Any | None = None,
    monitor_endpoint: Any | None = None,
    qmp_endpoint: Any | None = None,
    smb_guestfwd_port: int | None = None,
    usb_drives: list[Any] | None = None,
    cdrom_iso: str | Path | None = None,
    enable_audio: bool = True,
    audio_backend: str | None = None,
    audio_in_name: str | None = None,
    sound_device: str | None = "intel-hda",
    **kwargs,
) -> list[str]:
    """Build QEMU command line arguments for running an installed Windows VM."""
    if rr_mode:
        if rr_mode not in ("record", "replay"):
            raise QEMUError(f"Invalid rr_mode '{rr_mode}'. Must be 'record' or 'replay'.")
        if enable_kvm:
            raise QEMUError(
                "Deterministic record/replay (TTD) requires TCG emulation; KVM must be disabled (enable_kvm=False)."
            )
        if cpus > 1:
            raise QEMUError(
                f"Deterministic record/replay (TTD) only supports a single vCPU (cpus=1, got cpus={cpus}); "
                "SMP is not supported by QEMU rr engine."
            )
        if not rr_file:
            raise QEMUError("rr_file is required when rr_mode is set.")

    cmd = [
        find_qemu_binary(),
        "-machine",
        "q35",
        "-global",
        "ICH9-LPC.disable_s3=1",
        "-global",
        "ICH9-LPC.disable_s4=1",
        "-m",
        str(ram_mb),
        "-smp",
        str(cpus),
    ]

    if rr_mode:
        icount_val = icount or "shift=auto"
        rr_opt = f"{icount_val},rr={rr_mode},rrfile={rr_file}"
        if rr_snapshot:
            rr_opt += f",rrsnapshot={rr_snapshot}"
        cmd.extend(["-icount", rr_opt])
    elif icount:
        cmd.extend(["-icount", icount])

    cache_opt = ",cache=unsafe" if (ttd or rr_mode) else ""
    if blkreplay or rr_mode:
        cmd.extend(
            [
                "-drive",
                f"file={disk_path},format=qcow2{cache_opt},if=none,id=disk0",
                "-drive",
                "driver=blkreplay,if=none,image=disk0,id=disk0-rr",
                "-device",
                "ide-hd,bus=ide.0,drive=disk0-rr",
            ]
        )
    else:
        cmd.extend(
            [
                "-drive",
                f"file={disk_path},format=qcow2{cache_opt},if=none,id=disk0",
                "-device",
                "ide-hd,bus=ide.0,drive=disk0",
            ]
        )

    if cdrom_iso:
        p_cd = Path(cdrom_iso).resolve()
        cmd.extend(
            [
                "-drive",
                f"file={p_cd},format=raw,if=none,id=cd0,media=cdrom,readonly=on",
                "-device",
                "ide-cd,bus=ide.1,drive=cd0,id=cdrom_dev",
            ]
        )
    elif not (ttd or rr_mode):
        cmd.extend(
            [
                "-drive",
                "if=none,id=cd0,media=cdrom,readonly=on",
                "-device",
                "ide-cd,bus=ide.1,drive=cd0,id=cdrom_dev",
            ]
        )

    netdev_user = "user,id=net0"
    if smb_guestfwd_port is not None:
        if sys.platform == "win32":
            netdev_user += f",guestfwd=tcp:10.0.2.4:445-tcp:127.0.0.1:{smb_guestfwd_port}"
        elif shutil.which("nc"):
            netdev_user += f",guestfwd=tcp:10.0.2.4:445-cmd:nc 127.0.0.1 {smb_guestfwd_port}"
        else:
            netdev_user += (
                f",guestfwd=tcp:10.0.2.4:445-cmd:{sys.executable} -m windows.smb pipe 127.0.0.1 {smb_guestfwd_port}"
            )

    cmd.extend(
        [
            "-boot",
            "order=c",
            "-netdev",
            netdev_user,
            "-device",
            "e1000,netdev=net0,id=nic0,mac=52:54:00:12:34:50",
        ]
    )

    if filter_replay or rr_mode:
        cmd.extend(["-object", "filter-replay,id=flt0,netdev=net0"])

    if not (ttd or rr_mode):
        cmd.extend(
            [
                "-device",
                "qemu-xhci",
                "-device",
                "usb-tablet",
            ]
        )
        if usb_drives:
            for idx, usb_disk in enumerate(usb_drives):
                p = Path(usb_disk).resolve()
                fmt = "qcow2" if p.suffix.lower() == ".qcow2" else "raw"
                cmd.extend(
                    [
                        "-drive",
                        f"file={p},format={fmt},if=none,id=boot_usb_drv_{idx}",
                        "-device",
                        f"usb-storage,drive=boot_usb_drv_{idx},id=boot_usb_dev_{idx}",
                    ]
                )

    if not (ttd or rr_mode):
        cmd.extend(
            [
                # PCIe root ports for hotplugging PCIe devices (e1000e, virtio-net-pci, etc.)
                "-device",
                "pcie-root-port,id=rp1,slot=1,chassis=1",
                "-device",
                "pcie-root-port,id=rp2,slot=2,chassis=2",
                "-device",
                "pcie-root-port,id=rp3,slot=3,chassis=3",
                "-device",
                "pcie-root-port,id=rp4,slot=4,chassis=4",
                "-device",
                "pcie-root-port,id=rp5,slot=5,chassis=5",
                "-device",
                "pcie-root-port,id=rp6,slot=6,chassis=6",
                "-device",
                "pcie-root-port,id=rp7,slot=7,chassis=7",
                "-device",
                "pcie-root-port,id=rp8,slot=8,chassis=8",
                # PCIe-to-PCI bridge for legacy PCI device hotplugging (e1000, rtl8139, etc.)
                "-device",
                "pcie-pci-bridge,id=pci.1,bus=pcie.0",
            ]
        )

    if ttd or rr_mode:
        cmd.extend(
            [
                "-rtc",
                "base=utc",
            ]
        )
    else:
        cmd.extend(
            [
                "-rtc",
                "base=localtime",
            ]
        )

    if stop_at_boot:
        cmd.append("-S")

    if gdb_port is not None:
        cmd.extend(["-gdb", f"tcp::{gdb_port}"])

    qga_ep = qga_endpoint if qga_endpoint is not None else qga_socket_path
    if qga_ep is not None:
        cmd.extend(format_chardev_args("qga0", qga_ep, server=True, wait=False))
        cmd.extend(
            [
                "-device",
                "virtio-serial-pci",
                "-device",
                "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            ]
        )

    if vnc_display is not None:
        cmd.extend(["-vnc", f"127.0.0.1:{vnc_display}"])
        if sys.platform == "win32":
            keymap = find_qemu_keymap()
            if keymap:
                cmd.extend(["-k", keymap])

    mon_ep = monitor_endpoint if monitor_endpoint is not None else monitor_socket_path
    if mon_ep is not None:
        cmd.extend(format_monitor_args(mon_ep))

    qmp_ep = qmp_endpoint if qmp_endpoint is not None else qmp_socket_path
    if qmp_ep is not None:
        cmd.extend(format_qmp_args(qmp_ep))

    if enable_audio and not (ttd or rr_mode):
        if audio_backend:
            adev = audio_backend
        elif sys.platform == "win32":
            adev = "dsound,id=snd0"
        else:
            is_pulse = False
            try:
                uid = os.getuid()
                if os.path.exists(f"/run/user/{uid}/pulse/native"):
                    is_pulse = True
            except (AttributeError, OSError):
                pass
            if not is_pulse and shutil.which("pactl"):
                is_pulse = True

            if is_pulse:
                in_opt = f",in.name={audio_in_name}" if audio_in_name else ""
                adev = f"pa,id=snd0{in_opt}"
            elif shutil.which("pipewire"):
                adev = "pipewire,id=snd0"
            else:
                adev = "none,id=snd0"

        cmd.extend(["-audiodev", adev])
        if sound_device == "intel-hda":
            cmd.extend(["-device", "intel-hda", "-device", "hda-duplex,audiodev=snd0"])
        elif sound_device == "usb-audio":
            cmd.extend(["-device", "usb-audio,audiodev=snd0,id=usb_mic0"])

    if sys.platform == "win32":
        if enable_kvm and is_whpx_available():
            cmd.extend(["-accel", "whpx", "-cpu", "max"])
        else:
            cmd.extend(["-accel", "tcg,tb-size=1024"])
            vendor = get_host_cpu_vendor()
            cmd.extend(["-cpu", f"max,vendor={vendor}"])
    elif enable_kvm and os.path.exists("/dev/kvm"):
        cmd.extend(["-enable-kvm", "-cpu", "host"])
    else:
        cmd.extend(["-accel", "tcg,tb-size=1024"])
        vendor = get_host_cpu_vendor()
        cmd.extend(["-cpu", f"max,vendor={vendor}"])

    if headless and vnc_display is None:
        cmd.extend(["-display", "none"])

    if loadvm:
        cmd.extend(["-loadvm", loadvm])

    return cmd


class QEMUProcessManager:
    """Manages background QEMU process lifecycles with health checks."""

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self.process: subprocess.Popen | None = None
        self._kvm_fd: int | None = None

    def start(self, startup_check_delay: float = 0.5) -> None:
        """Start QEMU process in background and verify it remains alive."""
        if self.is_running():
            return

        pass_fds: list[int] = []
        if sys.platform != "win32" and os.path.exists("/dev/kvm"):
            try:
                self._kvm_fd = os.open("/dev/kvm", os.O_RDWR)
                pass_fds.append(self._kvm_fd)
            except OSError:
                self._kvm_fd = None

        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if pass_fds and sys.platform != "win32":
            popen_kwargs["pass_fds"] = pass_fds

        if sys.platform == "win32":
            create_new_pg = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | create_new_pg
        else:
            popen_kwargs["start_new_session"] = True

        self.process = subprocess.Popen(self.cmd, **popen_kwargs)

        # Brief pause to verify QEMU did not crash or exit immediately
        time.sleep(startup_check_delay)
        retcode = self.process.poll()
        if retcode is not None:
            stderr_out = ""
            if self.process.stderr:
                stderr_out = self.process.stderr.read()
            self.process.wait()  # Reap process to prevent defunct zombie state
            self.process = None
            if self._kvm_fd is not None:
                try:
                    os.close(self._kvm_fd)
                except OSError:
                    pass
                self._kvm_fd = None
            raise QEMUError(f"QEMU process failed to start (exit code {retcode}):\n{stderr_out.strip()}")

    def stop(self, timeout: float = 10.0, sig: int | None = None) -> None:
        """Stop the QEMU process cleanly and reap zombie state."""
        if self._kvm_fd is not None:
            try:
                os.close(self._kvm_fd)
            except OSError:
                pass
            self._kvm_fd = None

        if not self.process:
            return

        if self.process.poll() is None:
            try:
                if sig is None:
                    self.process.terminate()
                else:
                    self.process.send_signal(sig)
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            except Exception:
                pass

        # Final poll/wait to ensure process is reaped
        if self.process and self.process.poll() is not None:
            self.process = None

    def kill(self) -> None:
        """Immediately and unconditionally kill the QEMU process with SIGKILL."""
        if not self.process:
            return

        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=3.0)
            except Exception:
                pass

        if self.process.poll() is not None:
            self.process.wait()
            self.process = None

    def send_boot_keypress(self, socket_path: Any, count: int = 5, delay: float = 0.5) -> None:
        """Send Enter keypresses to QEMU monitor socket to bypass 'Press any key to boot from CD' prompts."""
        for _ in range(count):
            if not self.is_running():
                break
            try:
                with connect_socket(socket_path, timeout=1.0) as s:
                    try:
                        s.recv(1024)
                    except Exception:
                        pass
                    s.sendall(b"sendkey ret\n")
                    time.sleep(0.1)
                    s.sendall(b"sendkey spc\n")
            except Exception:
                pass
            time.sleep(delay)

    def is_running(self) -> bool:
        """Check if process is currently running and active."""
        if self.process is None:
            return False
        return self.process.poll() is None
