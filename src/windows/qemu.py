"""QEMU process runner, port selection, and process management."""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


class QEMUError(RuntimeError):
    """Exception raised for QEMU execution failures."""


def find_qemu_binary() -> str:
    """Find available qemu-system-x86_64 binary."""
    binary = shutil.which("qemu-system-x86_64")
    if not binary:
        raise QEMUError("qemu-system-x86_64 is not installed or not found in PATH.")
    return binary


def find_qemu_img_binary() -> str:
    """Find available qemu-img binary."""
    binary = shutil.which("qemu-img")
    if not binary:
        raise QEMUError("qemu-img is not installed or not found in PATH.")
    return binary


def find_free_port(start_port: int = 5985, max_attempts: int = 100) -> int:
    """Find the next available TCP port on localhost starting from start_port."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
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
    monitor_socket_path: Path | None = None,
    qga_socket_path: Path | None = None,
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

    if qga_socket_path:
        cmd.extend(
            [
                "-chardev",
                f"socket,id=qga0,path={qga_socket_path},server=on,wait=off",
                "-device",
                "virtio-serial-pci",
                "-device",
                "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            ]
        )

    if vnc_display is not None:
        cmd.extend(["-vnc", f"127.0.0.1:{vnc_display}"])

    if serial_log_path:
        cmd.extend(["-serial", f"file:{serial_log_path}"])

    if monitor_socket_path:
        cmd.extend(["-monitor", f"unix:{monitor_socket_path},server,nowait"])

    if enable_kvm and os.path.exists("/dev/kvm"):
        cmd.extend(["-enable-kvm", "-cpu", "host"])
    else:
        cmd.extend(["-cpu", "qemu64"])

    if headless and vnc_display is None:
        cmd.extend(["-display", "none"])

    return cmd


def build_run_qemu_cmd(
    disk_path: Path,
    ram_mb: int = 4096,
    cpus: int = 4,
    enable_kvm: bool = True,
    headless: bool = True,
    vnc_display: int | None = 0,
    monitor_socket_path: Path | None = None,
    qga_socket_path: Path | None = None,
    gdb_port: int | None = None,
    stop_at_boot: bool = False,
    **kwargs,
) -> list[str]:
    """Build QEMU command line arguments for running an installed Windows VM."""
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
        "-boot",
        "order=c",
        "-netdev",
        "user,id=net0",
        "-device",
        "e1000,netdev=net0",
        "-device",
        "qemu-xhci",
        "-device",
        "usb-tablet",
        "-rtc",
        "base=localtime",
    ]

    if stop_at_boot:
        cmd.append("-S")

    if gdb_port is not None:
        cmd.extend(["-gdb", f"tcp::{gdb_port}"])

    if qga_socket_path:
        cmd.extend(
            [
                "-chardev",
                f"socket,id=qga0,path={qga_socket_path},server=on,wait=off",
                "-device",
                "virtio-serial-pci",
                "-device",
                "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            ]
        )

    if vnc_display is not None:
        cmd.extend(["-vnc", f"127.0.0.1:{vnc_display}"])

    if monitor_socket_path:
        cmd.extend(["-monitor", f"unix:{monitor_socket_path},server,nowait"])

    if enable_kvm and os.path.exists("/dev/kvm"):
        cmd.extend(["-enable-kvm", "-cpu", "host"])
    else:
        cmd.extend(["-cpu", "qemu64"])

    if headless and vnc_display is None:
        cmd.extend(["-display", "none"])

    return cmd


class QEMUProcessManager:
    """Manages background QEMU process lifecycles with health checks."""

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self.process: subprocess.Popen | None = None

    def start(self, startup_check_delay: float = 0.5) -> None:
        """Start QEMU process in background and verify it remains alive."""
        if self.is_running():
            return

        self.process = subprocess.Popen(
            self.cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Brief pause to verify QEMU did not crash or exit immediately
        time.sleep(startup_check_delay)
        retcode = self.process.poll()
        if retcode is not None:
            stderr_out = ""
            if self.process.stderr:
                stderr_out = self.process.stderr.read()
            self.process.wait()  # Reap process to prevent defunct zombie state
            self.process = None
            raise QEMUError(f"QEMU process failed to start (exit code {retcode}):\n{stderr_out.strip()}")

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the QEMU process cleanly and reap zombie state."""
        if not self.process:
            return

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

        # Final poll/wait to ensure process is reaped
        if self.process.poll() is not None:
            self.process.wait()
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

    def send_boot_keypress(self, socket_path: Path, count: int = 5, delay: float = 0.5) -> None:
        """Send Enter keypresses to QEMU monitor socket to bypass 'Press any key to boot from CD' prompts."""
        socket_path = Path(socket_path)
        for _ in range(count):
            if not self.is_running():
                break
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(str(socket_path))
                    s.recv(1024)
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
