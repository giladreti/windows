import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from windows.console import ConsoleController
from windows.executor import CommandController, CommandResult
from windows.file import FileController
from windows.image import Image
from windows.processes import ProcessController
from windows.qemu import QEMUProcessManager, build_run_qemu_cmd, find_free_port
from windows.registry import RegistryController
from windows.services import ServiceController
from windows.unattend import DEFAULT_PASSWORD, DEFAULT_USERNAME


class PowerController:
    """Power management controller for QEMU VM."""

    def __init__(self, process_manager: QEMUProcessManager):
        self._manager = process_manager

    def on(self) -> None:
        """Power on the virtual machine."""
        self.start()

    def start(self) -> None:
        """Start the virtual machine process."""
        self._manager.start()

    def off(self) -> None:
        """Power off the virtual machine."""
        self.stop()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the virtual machine process."""
        self._manager.stop(timeout=timeout)

    def restart(self) -> None:
        """Restart the virtual machine."""
        self.off()
        time.sleep(2)
        self.on()

    @property
    def status(self) -> str:
        """Return VM power status ('running' or 'stopped')."""
        return "running" if self._manager.is_running() else "stopped"


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

        self.power = PowerController(self._process_manager)
        self.console = ConsoleController(
            host="127.0.0.1",
            port=actual_vnc_port,
            display_index=actual_vnc_display,
            monitor_socket_path=self.monitor_socket_path,
        )
        self.command = CommandController(
            qga_socket_path=self.qga_socket_path,
        )
        self.file = FileController(self.command)
        self.processes = ProcessController(self.command)
        self.registry = RegistryController(self.command)
        self.services = ServiceController(self.command)

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
