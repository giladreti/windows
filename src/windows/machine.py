import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from windows.console import ConsoleController, ScreenRecorder
from windows.executor import CommandController, CommandResult
from windows.file import FileController
from windows.image import Image
from windows.processes import ProcessController
from windows.qemu import QEMUProcessManager, build_run_qemu_cmd, find_free_port
from windows.registry import RegistryController
from windows.services import ServiceController
from windows.snapshot import SnapshotController
from windows.unattend import DEFAULT_PASSWORD, DEFAULT_USERNAME


class PowerController:
    """Power management controller for QEMU VM."""

    def __init__(self, process_manager: QEMUProcessManager, console: ConsoleController | None = None):
        self._manager = process_manager
        self._console = console
        self._is_paused = False

    def on(self) -> None:
        """Power on the virtual machine."""
        self.start()

    def start(self) -> None:
        """Start the virtual machine process."""
        self._manager.start()
        self._is_paused = False

    def off(self) -> None:
        """Power off the virtual machine."""
        self.stop()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the virtual machine process."""
        self._manager.stop(timeout=timeout)
        self._is_paused = False

    def kill(self) -> None:
        """Immediately and forcefully kill the virtual machine process (SIGKILL)."""
        self._manager.kill()
        self._is_paused = False

    def pause(self) -> None:
        """Pause VM CPU execution via QEMU monitor."""
        if self._console and self.status == "running":
            self._console.monitor_stop()
        self._is_paused = True

    def resume(self) -> None:
        """Resume VM CPU execution via QEMU monitor."""
        if self._console and self._manager.is_running():
            self._console.monitor_continue()
        self._is_paused = False

    def unpause(self) -> None:
        """Alias for resume()."""
        self.resume()

    def restart(self) -> None:
        """Restart the virtual machine."""
        self.off()
        time.sleep(2)
        self.on()

    @property
    def status(self) -> str:
        """Return VM power status ('running', 'paused', or 'stopped')."""
        if not self._manager.is_running():
            self._is_paused = False
            return "stopped"
        if self._is_paused:
            return "paused"
        return "running"


class Machine:
    """Represents a runnable Windows virtual machine instance."""

    def __init__(
        self,
        image: "Image | str | Path | os.PathLike",
        ram_mb: int = 4096,
        cpus: int = 4,
        headless: bool = True,
        vnc_display: int | None = None,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
    ):
        if not isinstance(image, Image):
            image = Image(image)
        self.image = image
        self.ram_mb = ram_mb
        self.cpus = cpus
        self.headless = headless
        self.username = username
        self.password = password

        if vnc_display is not None:
            actual_vnc_display = vnc_display
            actual_vnc_port = 5900 + vnc_display
        else:
            actual_vnc_port = find_free_port(5900)
            actual_vnc_display = actual_vnc_port - 5900

        self.vnc_display = actual_vnc_display
        self.qga_socket_path = image.disk_path.parent / f"{image.disk_path.stem}_qga.sock"
        self.monitor_socket_path = image.disk_path.parent / f"{image.disk_path.stem}_monitor.sock"

        cmd = build_run_qemu_cmd(
            disk_path=image.disk_path,
            ram_mb=ram_mb,
            cpus=cpus,
            headless=headless,
            vnc_display=actual_vnc_display,
            monitor_socket_path=self.monitor_socket_path,
            qga_socket_path=self.qga_socket_path,
        )
        self._process_manager = QEMUProcessManager(cmd)

        self.console = ConsoleController(
            host="127.0.0.1",
            port=actual_vnc_port,
            display_index=actual_vnc_display,
            monitor_socket_path=self.monitor_socket_path,
        )
        self.power = PowerController(self._process_manager, console=self.console)
        self.command = CommandController(
            qga_socket_path=self.qga_socket_path,
        )
        self.file = FileController(self.command)
        self.processes = ProcessController(self.command)
        self.registry = RegistryController(self.command)
        self.services = ServiceController(self.command)
        self.snapshot = SnapshotController(self)

    def pause(self) -> None:
        """Pause VM CPU execution."""
        self.power.pause()

    def resume(self) -> None:
        """Resume VM CPU execution."""
        self.power.resume()

    def unpause(self) -> None:
        """Alias for resume()."""
        self.power.resume()

    def kill(self) -> None:
        """Immediately and forcefully kill the QEMU process (SIGKILL)."""
        self.power.kill()

    def record(
        self,
        output_path: "str | Path" = "recording.mp4",
        fps: float = 10.0,
    ) -> ScreenRecorder:
        """Record a video of the machine screen console (convenience alias for machine.console.record).

        Usage:
            with machine.record("session.mp4", fps=10):
                # Run guest operations...
        """
        return self.console.record(output_path=output_path, fps=fps)

    def fork(
        self,
        snapshot_name: str | None = None,
        output_disk: "str | Path | None" = None,
        run: bool = True,
        **machine_kwargs,
    ) -> "Machine":
        """Fork this virtual machine from a snapshot, creating and running a new machine.

        Creates a snapshot of the current VM state (if not already existing) and instantiates
        a new independent Machine from it, allowing multiple machines to run concurrently from the same base state.

        Args:
            snapshot_name: Optional name for the base snapshot. If None, auto-generates a name.
            output_disk: Optional disk path for the new forked machine.
            run: If True, powers on the newly forked machine immediately.
            **machine_kwargs: Overrides for the new machine (e.g. ram_mb, cpus, headless, etc.).

        Returns:
            A new `Machine` instance running from the snapshot.
        """
        import uuid

        if snapshot_name is None:
            snapshot_name = f"fork_snap_{uuid.uuid4().hex[:8]}"

        if not self.snapshot.exists(snapshot_name):
            self.snapshot.create(snapshot_name)

        return self.snapshot.fork(
            name_or_id=snapshot_name,
            output_disk=output_disk,
            run=run,
            **machine_kwargs,
        )

    def run(self, timeout: int = 180) -> CommandResult:
        """Power on the machine and wait until the guest QGA interface is ready."""
        if self.power.status != "running":
            self.power.on()
        ready = self.command.wait_until_ready(timeout=timeout)
        if not ready:
            raise RuntimeError("Guest OS failed to respond within timeout")
        return self.command.run("hostname")

    def debug(
        self,
        backend: str = "qemu_monitor",
        gdb_port: int | None = None,
        init_script: str | list[str] | Callable | None = None,
        pause_at_boot: bool = True,
        auto_continue: bool = True,
        open_console: bool = True,
        launch_ntoseye: bool = False,
    ) -> "Machine":
        """Start the VM in debug mode using QEMU monitor or ntoseye (WinDbg-like kernel debugger).

        Args:
            backend: Debugger backend ('qemu_monitor' or 'ntoseye').
            gdb_port: TCP port for QEMU GDB stub (defaults to 1234 when using ntoseye).
            init_script: Startup HMP monitor commands or script callable.
            pause_at_boot: If True, pauses VM CPU execution at boot (-S flag).
            auto_continue: If True, resumes execution after running init_script.
            open_console: If True, opens native VNC viewer console window.
            launch_ntoseye: If True, launches interactive ntoseye terminal session.
        """
        if self.power.status == "running":
            self.power.off()

        actual_gdb_port = gdb_port
        if backend == "ntoseye" and actual_gdb_port is None:
            actual_gdb_port = find_free_port(1234)

        cmd = build_run_qemu_cmd(
            disk_path=self.image.disk_path,
            ram_mb=self.ram_mb,
            cpus=self.cpus,
            headless=self.headless,
            vnc_display=self.vnc_display,
            monitor_socket_path=self.monitor_socket_path,
            qga_socket_path=self.qga_socket_path,
            gdb_port=actual_gdb_port,
            stop_at_boot=pause_at_boot,
        )
        self._process_manager = QEMUProcessManager(cmd)
        self.power._manager = self._process_manager
        self.power.on()

        if open_console:
            self.console.open()

        if init_script:
            self.console.execute_monitor_script(init_script)

        if backend == "ntoseye" and launch_ntoseye and actual_gdb_port:
            import subprocess

            terminal_cmds = ["x-terminal-emulator", "gnome-terminal", "kitty", "alacritty", "xterm"]
            for term in terminal_cmds:
                if shutil.which(term):
                    try:
                        subprocess.Popen([term, "-e", f"ntoseye -b gdb --connect localhost:{actual_gdb_port}"])
                        break
                    except Exception:
                        pass

        if pause_at_boot and auto_continue and backend == "qemu_monitor":
            self.console.monitor_continue()

        return self

    def __repr__(self) -> str:
        return f"<Machine image={self.image!r} status={self.power.status!r}>"
