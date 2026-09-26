import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from windows.qemu import connect_socket, is_tcp_endpoint


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


class ScreenRecorder:
    """Context manager and controller for recording screen videos of the QEMU VM console."""

    def __init__(
        self,
        console: "ConsoleController",
        output_path: str | Path = "recording.mp4",
        fps: float = 10.0,
    ):
        self.console = console
        self.output_path = Path(output_path).resolve()
        if not self.output_path.suffix:
            self.output_path = self.output_path.with_suffix(".mp4")
        self.fps = max(1.0, float(fps))
        self.frame_count: int = 0
        self._temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
        self._temp_dir: Path | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._start_time: float | None = None
        self._end_time: float | None = None

    @property
    def is_recording(self) -> bool:
        """Return True if background video capture is currently active."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def duration(self) -> float:
        """Return total elapsed recording duration in seconds."""
        if self._start_time is None:
            return 0.0
        end = self._end_time if self._end_time is not None else time.time()
        return max(0.0, end - self._start_time)

    def start(self) -> "ScreenRecorder":
        """Start capturing screen frames in the background."""
        if self.is_recording:
            return self

        has_mon = self.console.monitor_socket_path is not None and (
            is_tcp_endpoint(self.console.monitor_socket_path)
            or (isinstance(self.console.monitor_socket_path, Path) and self.console.monitor_socket_path.exists())
        )
        if not has_mon:
            raise ConsoleError(
                f"Cannot record video: QEMU monitor socket is not connected or active: {self.console.monitor_socket_path}"
            )

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self._temp_dir_obj = tempfile.TemporaryDirectory(prefix="qemu_rec_")
        self._temp_dir = Path(self._temp_dir_obj.name)
        self.frame_count = 0
        self._stop_event.clear()
        self._start_time = time.time()
        self._end_time = None

        # Capture initial frame immediately
        self._capture_single_frame()

        self._thread = threading.Thread(target=self._capture_worker, daemon=True, name="ScreenRecorderWorker")
        self._thread.start()
        return self

    def _capture_single_frame(self) -> None:
        if not self._temp_dir:
            return
        frame_path = self._temp_dir / f"frame_{self.frame_count:06d}.ppm"
        try:
            self.console._dump_screen_ppm(frame_path)
            if frame_path.exists() and frame_path.stat().st_size > 0:
                self.frame_count += 1
        except Exception:
            pass

    def _capture_worker(self) -> None:
        interval = 1.0 / self.fps
        next_capture_time = time.time() + interval
        while not self._stop_event.is_set():
            now = time.time()
            sleep_duration = next_capture_time - now
            if sleep_duration > 0:
                if self._stop_event.wait(sleep_duration):
                    break
            self._capture_single_frame()
            next_capture_time += interval
            if next_capture_time < time.time():
                next_capture_time = time.time() + interval

    def stop(self) -> Path:
        """Stop screen recording, compile the captured frames into a video, and return the video path."""
        if not self.is_recording and self._temp_dir is None:
            return self.output_path

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        self._end_time = time.time()

        try:
            self._encode_video()
        finally:
            if self._temp_dir_obj is not None:
                self._temp_dir_obj.cleanup()
                self._temp_dir_obj = None
                self._temp_dir = None

        return self.output_path

    def _encode_video(self) -> None:
        if not self._temp_dir or self.frame_count == 0:
            raise ConsoleError("No video frames captured during recording.")

        ffmpeg_binary = shutil.which("ffmpeg")
        suffix = self.output_path.suffix.lower()

        if not ffmpeg_binary:
            if suffix == ".gif":
                self._encode_gif_pil()
                return
            raise ConsoleError(
                f"ffmpeg binary was not found on PATH. ffmpeg is required to encode '{suffix}' video recordings."
            )

        if suffix == ".mp4":
            cmd = [
                ffmpeg_binary,
                "-y",
                "-framerate",
                str(self.fps),
                "-i",
                str(self._temp_dir / "frame_%06d.ppm"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-movflags",
                "+faststart",
                str(self.output_path),
            ]
        elif suffix == ".webm":
            cmd = [
                ffmpeg_binary,
                "-y",
                "-framerate",
                str(self.fps),
                "-i",
                str(self._temp_dir / "frame_%06d.ppm"),
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuv420p",
                str(self.output_path),
            ]
        elif suffix == ".gif":
            cmd = [
                ffmpeg_binary,
                "-y",
                "-framerate",
                str(self.fps),
                "-i",
                str(self._temp_dir / "frame_%06d.ppm"),
                "-vf",
                "split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                str(self.output_path),
            ]
        else:
            cmd = [
                ffmpeg_binary,
                "-y",
                "-framerate",
                str(self.fps),
                "-i",
                str(self._temp_dir / "frame_%06d.ppm"),
                str(self.output_path),
            ]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise ConsoleError(f"Failed to encode video recording with ffmpeg: {res.stderr.strip()}")

    def _encode_gif_pil(self) -> None:
        from PIL import Image as PILImage

        if not self._temp_dir:
            return
        frame_files = sorted(self._temp_dir.glob("frame_*.ppm"))
        if not frame_files:
            raise ConsoleError("No frames available for GIF encoding.")

        images = [PILImage.open(f) for f in frame_files]
        images[0].save(
            self.output_path,
            save_all=True,
            append_images=images[1:],
            duration=int(1000 / self.fps),
            loop=0,
        )
        for img in images:
            img.close()

    def __enter__(self) -> "ScreenRecorder":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def __repr__(self) -> str:
        status = "recording" if self.is_recording else "stopped"
        return f"<ScreenRecorder output={str(self.output_path)!r} fps={self.fps} status={status!r} frames={self.frame_count}>"


class ConsoleController:
    """Controller for opening, managing, screenshotting, and recording the QEMU VM display console."""

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
        "vncviewer.exe",
        "tigervnc.exe",
        "tvnviewer.exe",
        "UltraVNC.exe",
    ]

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5900,
        display_index: int = 0,
        monitor_socket_path: Any | None = None,
    ):
        self.host = host
        self.port = port
        self.display_index = display_index
        if monitor_socket_path is not None and is_tcp_endpoint(monitor_socket_path):
            self.monitor_socket_path = monitor_socket_path
        elif monitor_socket_path:
            self.monitor_socket_path = Path(monitor_socket_path).resolve()
        else:
            self.monitor_socket_path = None
        self._viewer_process: subprocess.Popen | None = None
        self._monitor_lock = threading.Lock()

    @property
    def info(self) -> ConsoleInfo:
        """Return VNC connection details."""
        return ConsoleInfo(host=self.host, port=self.port, display_index=self.display_index)

    def _has_monitor(self) -> bool:
        """Return True if monitor endpoint is configured and active."""
        if self.monitor_socket_path is None:
            return False
        if is_tcp_endpoint(self.monitor_socket_path):
            return True
        return isinstance(self.monitor_socket_path, Path) and self.monitor_socket_path.exists()

    def _dump_screen_ppm(self, ppm_path: Path) -> None:
        """Send screendump command to QEMU monitor socket."""
        ppm_path = Path(ppm_path).resolve()
        with self._monitor_lock:
            with connect_socket(self.monitor_socket_path, timeout=3.0) as s:
                try:
                    s.recv(1024)
                except Exception:
                    pass
                s.sendall(f"screendump {ppm_path}\n".encode())
                time.sleep(0.05)

    def screenshot(self, output_path: str | Path = "screenshot.png") -> Path:
        """Capture a live screenshot of the VM's active display screen and save as PNG."""
        from PIL import Image as PILImage

        output_path = Path(output_path).resolve()
        if not output_path.suffix:
            output_path = output_path.with_suffix(".png")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if not self._has_monitor():
            raise ConsoleError(f"QEMU monitor socket is not connected or active: {self.monitor_socket_path}")

        ppm_path = output_path.with_suffix(".tmp.ppm")
        self._dump_screen_ppm(ppm_path)

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

    def record(
        self,
        output_path: str | Path = "recording.mp4",
        fps: float = 10.0,
    ) -> ScreenRecorder:
        """Create a video recording context manager / recorder for the VM screen.

        Usage:
            with machine.console.record("demo.mp4", fps=10) as rec:
                # Perform guest actions...
                machine.command.run(...)

        Args:
            output_path: Target video file path (supports .mp4, .webm, .gif, .mkv, .avi).
            fps: Frame rate for recording in frames per second (default: 10.0).

        Returns:
            A `ScreenRecorder` instance usable as a context manager or standalone recorder.
        """
        return ScreenRecorder(console=self, output_path=output_path, fps=fps)

    def send_monitor_command(self, command: str, timeout: float = 5.0) -> str:
        """Send a raw HMP command string to the QEMU monitor socket and return output."""
        if not self._has_monitor():
            raise ConsoleError(f"QEMU monitor socket is not connected or active: {self.monitor_socket_path}")

        output = ""
        with self._monitor_lock:
            with connect_socket(self.monitor_socket_path, timeout=timeout) as s:
                # Drain initial banner or prompt
                s.settimeout(0.15)
                try:
                    while True:
                        if not s.recv(4096):
                            break
                except Exception:
                    pass

                cmd = command.strip() + "\n"
                s.sendall(cmd.encode("utf-8"))

                chunks: list[bytes] = []
                start_time = time.time()
                s.settimeout(0.2)
                while time.time() - start_time < timeout:
                    try:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        decoded = b"".join(chunks).decode("utf-8", errors="replace")
                        if "\n(qemu)" in decoded or "\r\n(qemu)" in decoded or decoded.rstrip().endswith("(qemu)"):
                            break
                    except TimeoutError:
                        if chunks:
                            break
                    except Exception:
                        break

                raw = b"".join(chunks).decode("utf-8", errors="replace")
                lines = raw.replace("\r\n", "\n").split("\n")
                if lines and lines[-1].strip() == "(qemu)":
                    lines.pop()
                elif lines and lines[-1].endswith("(qemu)"):
                    lines[-1] = lines[-1][:-6]
                if lines:
                    lines.pop(0)
                output = "\n".join(lines).strip()

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
        if sys.platform == "win32":
            candidates = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "TigerVNC" / "vncviewer.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "TightVNC" / "tvnviewer.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "uvnc bvba" / "UltraVNC" / "vncviewer.exe",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "TigerVNC" / "vncviewer.exe",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "TightVNC" / "tvnviewer.exe",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)
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
