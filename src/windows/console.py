"""Console and screen viewer controller for QEMU VMs."""

import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class ConsoleError(RuntimeError):
    """Exception raised for console viewer failures."""


class ConsoleInfo:
    """Represents VNC connection information for a VM console."""

    def __init__(self, host: str, port: int, display_index: int):
        self.host = host
        self.port = port
        self.display_index = display_index
        self.vnc_url = f"vnc://{host}:{port}"

    def to_dict(self) -> dict[str, str]:
        return {
            "host": self.host,
            "port": str(self.port),
            "display": f":{self.display_index}",
            "vnc_url": self.vnc_url,
        }

    def __repr__(self) -> str:
        return f"<ConsoleInfo host={self.host!r} port={self.port} vnc_url={self.vnc_url!r}>"


class ConsoleController:
    """Controller for opening, managing, and taking screenshots of the QEMU VM display console."""

    # Dedicated native VNC viewer binaries (excluding browser handlers like xdg-open)
    KNOWN_VIEWERS: list[str] = [
        "vncviewer",
        "tigervnc",
        "tightvncviewer",
        "vinagre",
        "remmina",
        "virt-viewer",
        "spicy",
        "bvnc",
    ]

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5900,
        display_index: int = 0,
        monitor_socket_path: str | Path | None = None,
    ):
        self.host = host
        self.port = port
        self.display_index = display_index
        self.monitor_socket_path = Path(monitor_socket_path).resolve() if monitor_socket_path else None
        self._viewer_process: subprocess.Popen | None = None

    @property
    def info(self) -> ConsoleInfo:
        """Return VNC connection details."""
        return ConsoleInfo(host=self.host, port=self.port, display_index=self.display_index)

    def screenshot(self, output_path: str | Path = "screenshot.png") -> Path:
        """Capture a live screenshot of the VM's active display screen and save as PNG."""
        from PIL import Image as PILImage

        output_path = Path(output_path).resolve()
        if not output_path.suffix:
            output_path = output_path.with_suffix(".png")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.monitor_socket_path or not self.monitor_socket_path.exists():
            raise ConsoleError(f"QEMU monitor socket is not connected or active: {self.monitor_socket_path}")

        ppm_path = output_path.with_suffix(".tmp.ppm")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3.0)
            s.connect(str(self.monitor_socket_path))
            s.recv(1024)
            s.sendall(f"screendump {ppm_path}\n".encode())
            time.sleep(0.5)

        if output_path.suffix.lower() == ".ppm":
            if ppm_path != output_path and ppm_path.exists():
                ppm_path.rename(output_path)
            return output_path

        # Convert PPM screendump to PNG
        with PILImage.open(ppm_path) as img:
            img.save(output_path, format="PNG")

        if ppm_path.exists():
            ppm_path.unlink(missing_ok=True)

        return output_path

    def send_monitor_command(self, command: str, timeout: float = 3.0) -> str:
        """Send a raw HMP command string to the QEMU monitor socket and return output."""
        if not self.monitor_socket_path or not self.monitor_socket_path.exists():
            raise ConsoleError(f"QEMU monitor socket is not connected or active: {self.monitor_socket_path}")

        output = ""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(self.monitor_socket_path))
            try:
                s.recv(1024)
            except Exception:
                pass

            cmd = command.strip() + "\n"
            s.sendall(cmd.encode("utf-8"))
            time.sleep(0.2)
            try:
                data = s.recv(4096)
                output = data.decode("utf-8", errors="replace")
            except Exception:
                output = ""

        return output

    def monitor_continue(self) -> str:
        """Unpause CPU execution via QEMU monitor ('cont')."""
        return self.send_monitor_command("cont")

    def monitor_stop(self) -> str:
        """Pause CPU execution via QEMU monitor ('stop')."""
        return self.send_monitor_command("stop")

    def execute_monitor_script(self, init_script: str | list[str] | Callable[["ConsoleController"], Any]) -> None:
        """Execute a QEMU monitor debug script (string of commands, list of commands, or callable)."""
        if isinstance(init_script, str):
            for line in init_script.strip().splitlines():
                line_str = line.strip()
                if line_str and not line_str.startswith("#"):
                    self.send_monitor_command(line_str)
        elif isinstance(init_script, list):
            for cmd in init_script:
                self.send_monitor_command(str(cmd))
        elif callable(init_script):
            init_script(self)

    def find_available_viewer(self) -> str | None:
        """Find the first available VNC viewer binary on the system PATH."""
        for viewer in self.KNOWN_VIEWERS:
            path = shutil.which(viewer)
            if path:
                return path
        return None

    def _wait_for_vnc_port(self, timeout: float = 5.0) -> bool:
        """Wait until the VNC port is open and accepting TCP connections."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    if s.connect_ex((self.host, self.port)) == 0:
                        return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def open(self, viewer: str | None = None) -> ConsoleInfo:
        """Open the QEMU screen console using a native VNC viewer application."""
        if self.is_open:
            return self.info

        viewer_binary = viewer
        if not viewer_binary:
            viewer_binary = self.find_available_viewer()

        if not viewer_binary:
            print(
                f"[windows] No native VNC viewer binary found on PATH (e.g. vncviewer, remmina, tigervnc).\n"
                f"[windows] Connect manually to VNC screen at: {self.info.vnc_url} (Host: {self.host}, Port: {self.port})\n"
                f"[windows] To install a viewer on Linux, run: sudo apt install tigervnc-viewer (or remmina)"
            )
            return self.info

        binary_path = shutil.which(viewer_binary) or viewer_binary

        # Ensure QEMU VNC server port is active before launching viewer
        self._wait_for_vnc_port(timeout=5.0)

        if "remmina" in str(binary_path):
            target_cmd = [str(binary_path), "-c", self.info.vnc_url]
        elif "virt-viewer" in str(binary_path):
            target_cmd = [str(binary_path), f"vnc://{self.host}:{self.port}"]
        else:
            # Standard vncviewer syntax: vncviewer host:port
            target_cmd = [str(binary_path), f"{self.host}:{self.port}"]

        try:
            self._viewer_process = subprocess.Popen(
                target_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            raise ConsoleError(f"Failed to launch VNC viewer '{viewer_binary}': {exc}") from exc

        return self.info

    def close(self) -> None:
        """Close the active VNC viewer application process."""
        if self._viewer_process and self._viewer_process.poll() is None:
            self._viewer_process.terminate()
            try:
                self._viewer_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._viewer_process.kill()
        self._viewer_process = None

    @property
    def is_open(self) -> bool:
        """Return True if the console viewer process is running."""
        return self._viewer_process is not None and self._viewer_process.poll() is None
