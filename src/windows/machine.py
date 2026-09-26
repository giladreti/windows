import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from windows.console import ConsoleController, ScreenRecorder
from windows.executor import CommandController, CommandResult
from windows.file import FileController
from windows.firewall import FirewallController
from windows.image import Image
from windows.network import NetworkController
from windows.processes import ProcessController
from windows.qemu import QEMUProcessManager, build_run_qemu_cmd, find_free_port
from windows.registry import RegistryController
from windows.services import ServiceController
from windows.snapshot import SnapshotController
from windows.ttd import TTDController
from windows.unattend import DEFAULT_PASSWORD, DEFAULT_USERNAME


class PowerController:
    """Power management controller for QEMU VM."""

    def __init__(
        self,
        process_manager: QEMUProcessManager,
        console: ConsoleController | None = None,
        machine: Any | None = None,
    ):
        self._manager = process_manager
        self._console = console
        self._machine = machine
        self._is_paused = False

    def on(self) -> None:
        """Power on the virtual machine."""
        self.start()

    def start(self) -> None:
        """Start the virtual machine process."""
        if sys.platform == "win32" and self._machine and hasattr(self._machine, "smb") and self._machine.smb:
            if not self._machine.smb.is_running:
                try:
                    self._machine.smb.start()
                except Exception:
                    pass
        self._manager.start()
        self._is_paused = False
        if self._machine and hasattr(self._machine, "_initial_microphone") and self._machine._initial_microphone:
            try:
                self._machine.microphone.play(self._machine._initial_microphone)
            except Exception:
                pass

    def off(self) -> None:
        """Power off the virtual machine."""
        self.stop()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the virtual machine process."""
        if self._machine and hasattr(self._machine, "microphone") and self._machine.microphone:
            try:
                self._machine.microphone.stop()
            except Exception:
                pass
        self._manager.stop(timeout=timeout)
        self._is_paused = False
        if self._machine and hasattr(self._machine, "smb") and self._machine.smb:
            try:
                self._machine.smb.stop()
            except Exception:
                pass

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


class MachineUserContext:
    """Scoped execution context on Machine that switches default execution to a target user."""

    def __init__(self, machine: "Machine", user: str, password: str):
        self.machine = machine
        self.user = user
        self.password = password
        self.command = self.machine.command.as_user(user=user, password=password)
        self._token: Any = None

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self.command.run(*args, **kwargs)

    def __enter__(self) -> "MachineUserContext":
        from windows.executor import _current_user

        self._token = _current_user.set((self.user, self.password))
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._token is not None:
            from windows.executor import _current_user

            _current_user.reset(self._token)
            self._token = None

    def __repr__(self) -> str:
        return f"<MachineUserContext user={self.user!r}>"


class Machine:
    """Represents a runnable Windows virtual machine instance."""

    _allocated_vnc_ports: set[int] = set()

    def __init__(
        self,
        image: "Image | str | Path | os.PathLike",
        ram_mb: int = 4096,
        cpus: int = 4,
        headless: bool = True,
        vnc_display: int | None = None,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        enable_kvm: bool = True,
        ttd: bool = False,
        default_user: str | None = None,
        default_password: str | None = None,
        usb_drives: list[str | Path] | None = None,
        microphone: str | Path | None = None,
        cd: str | Path | None = None,
        cdrom: str | Path | None = None,
        enable_audio: bool = True,
        sound_device: str | None = "intel-hda",
    ):
        if not isinstance(image, Image):
            image = Image(image)
        self.image = image
        self.ram_mb = ram_mb
        if ttd:
            self.cpus = 1
            self.enable_kvm = False
            self.ram_mb = max(ram_mb, 2048)
        else:
            self.cpus = cpus
            self.enable_kvm = enable_kvm
            self.ram_mb = ram_mb
        self.ttd_mode = ttd
        self.headless = headless
        self.default_user = default_user or username or DEFAULT_USERNAME
        self.default_password = default_password or password or DEFAULT_PASSWORD
        self.username = self.default_user
        self.password = self.default_password

        if vnc_display is not None:
            actual_vnc_display = vnc_display
            actual_vnc_port = 5900 + vnc_display
            Machine._allocated_vnc_ports.add(actual_vnc_port)
        else:
            actual_vnc_port = find_free_port(5900, exclude=Machine._allocated_vnc_ports)
            Machine._allocated_vnc_ports.add(actual_vnc_port)
            actual_vnc_display = actual_vnc_port - 5900

        self.vnc_port = actual_vnc_port
        self.vnc_display = actual_vnc_display

        if sys.platform == "win32":
            self.qga_port = find_free_port(5950, exclude=Machine._allocated_vnc_ports)
            Machine._allocated_vnc_ports.add(self.qga_port)
            self.monitor_port = find_free_port(5960, exclude=Machine._allocated_vnc_ports)
            Machine._allocated_vnc_ports.add(self.monitor_port)
            self.qmp_port = find_free_port(5970, exclude=Machine._allocated_vnc_ports)
            Machine._allocated_vnc_ports.add(self.qmp_port)

            self.qga_endpoint: Any = ("127.0.0.1", self.qga_port)
            self.monitor_endpoint: Any = ("127.0.0.1", self.monitor_port)
            self.qmp_endpoint: Any = ("127.0.0.1", self.qmp_port)

            self.qga_socket_path = self.qga_endpoint
            self.monitor_socket_path = self.monitor_endpoint
            self.qmp_socket_path = self.qmp_endpoint
        else:
            self.qga_socket_path = image.disk_path.parent / f"{image.disk_path.stem}_qga.sock"
            self.monitor_socket_path = image.disk_path.parent / f"{image.disk_path.stem}_monitor.sock"
            self.qmp_socket_path = image.disk_path.parent / f"{image.disk_path.stem}_qmp.sock"

            # Remove stale unix socket files if left behind by crashed/killed processes
            for sock_p in (self.qga_socket_path, self.monitor_socket_path, self.qmp_socket_path):
                if isinstance(sock_p, Path) and sock_p.exists():
                    try:
                        sock_p.unlink()
                    except OSError:
                        pass

            self.qga_endpoint = self.qga_socket_path
            self.monitor_endpoint = self.monitor_socket_path
            self.qmp_endpoint = self.qmp_socket_path

        from windows.smb import HAS_IMPACKET, SMBServerManager

        if HAS_IMPACKET:
            self.smb_port: int | None = find_free_port(5445, exclude=Machine._allocated_vnc_ports)
            Machine._allocated_vnc_ports.add(self.smb_port)
            self.smb: SMBServerManager | None = SMBServerManager(
                port=self.smb_port,
                username=self.default_user,
                password=self.default_password,
            )
        else:
            self.smb_port = None
            self.smb = None

        self.usb_drives = [Path(u) for u in usb_drives] if usb_drives else None
        self._initial_microphone = Path(microphone) if microphone else None
        raw_cd = cd if cd is not None else cdrom
        self._initial_cd = Path(raw_cd) if raw_cd is not None else None
        self.enable_audio = enable_audio
        self.sound_device = sound_device

        cmd = build_run_qemu_cmd(
            disk_path=image.disk_path,
            ram_mb=self.ram_mb,
            cpus=self.cpus,
            enable_kvm=self.enable_kvm,
            headless=headless,
            vnc_display=actual_vnc_display,
            monitor_socket_path=self.monitor_endpoint,
            qga_socket_path=self.qga_endpoint,
            qmp_socket_path=self.qmp_endpoint,
            ttd=self.ttd_mode,
            smb_guestfwd_port=self.smb_port,
            usb_drives=self.usb_drives,
            cdrom_iso=self._initial_cd,
            enable_audio=self.enable_audio,
            sound_device=self.sound_device,
        )
        self._process_manager = QEMUProcessManager(cmd)

        self.console = ConsoleController(
            host="127.0.0.1",
            port=actual_vnc_port,
            display_index=actual_vnc_display,
            monitor_socket_path=self.monitor_endpoint,
        )
        self.power = PowerController(self._process_manager, console=self.console, machine=self)
        self.command = CommandController(
            qga_socket_path=self.qga_endpoint,
        )
        self.file = FileController(self.command, machine=self)
        self.processes = ProcessController(self.command)
        self.registry = RegistryController(self.command)
        self.services = ServiceController(self.command)
        self.snapshot = SnapshotController(self)
        self.network = NetworkController(self)
        self.firewall = FirewallController(self)
        self.ttd = TTDController(self)

        from windows.audio import MicrophoneController
        from windows.cd import CDController
        from windows.usb import USBController

        self.usb = USBController(self)
        self.cd = CDController(self)
        self.cdrom = self.cd
        self.microphone = MicrophoneController(self)
        self.mic = self.microphone

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

    def record_execution(self, name: str, **kwargs: Any) -> Any:
        """Record deterministic execution trace for Time Travel Debugging (alias for machine.ttd.record)."""
        return self.ttd.record(name=name, **kwargs)

    def replay_execution(self, name: str, **kwargs: Any) -> Any:
        """Replay deterministic execution trace for Time Travel Debugging (alias for machine.ttd.replay)."""
        return self.ttd.replay(name=name, **kwargs)

    def record_session(
        self,
        name: str,
        start_snapshot: str | None = None,
        open_console: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Record deterministic execution session for Time Travel Debugging (alias for machine.ttd.record_session).

        Usage:
            machine.wait_for_boot()
            with machine.record_session("process_trace") as rec:
                machine.command.run("powershell Get-Process")
        """
        return self.ttd.record_session(
            name=name,
            start_snapshot=start_snapshot,
            open_console=open_console,
            **kwargs,
        )

    def wait_for_boot(
        self,
        timeout: int | None = None,
        interval: float = 2.0,
        raise_on_timeout: bool = True,
    ) -> bool:
        """Wait until the Windows guest OS has booted and QGA is responsive.

        Args:
            timeout: Maximum time in seconds to wait for boot (default 300s, or 600s for ttd mode).
            interval: Polling interval in seconds.
            raise_on_timeout: If True, raises TimeoutError if VM does not finish booting within timeout.
        """
        actual_timeout = timeout if timeout is not None else (900 if self.ttd_mode else 300)
        if self.power.status != "running":
            self.power.on()
        ready = self.command.wait_until_ready(timeout=actual_timeout, interval=interval)
        if not ready and raise_on_timeout:
            raise TimeoutError(
                f"Windows guest did not finish booting within {actual_timeout} seconds. "
                "QEMU Guest Agent is not yet responsive. "
                "If running under TCG emulation (ttd=True), Windows boot can take several minutes; "
                "try increasing the timeout (e.g. wait_for_boot(timeout=900))."
            )
        return ready

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
            monitor_socket_path=self.monitor_endpoint,
            qga_socket_path=self.qga_endpoint,
            gdb_port=actual_gdb_port,
            stop_at_boot=pause_at_boot,
            smb_guestfwd_port=getattr(self, "smb_port", None),
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

            if sys.platform == "win32":
                terminal_cmds = ["wt.exe", "cmd.exe", "powershell.exe"]
                for term in terminal_cmds:
                    if shutil.which(term):
                        try:
                            if term == "wt.exe":
                                subprocess.Popen(
                                    ["wt.exe", "ntoseye", "-b", "gdb", "--connect", f"localhost:{actual_gdb_port}"]
                                )
                            elif term == "powershell.exe":
                                subprocess.Popen(
                                    [
                                        "powershell.exe",
                                        "-NoExit",
                                        "-Command",
                                        f"ntoseye -b gdb --connect localhost:{actual_gdb_port}",
                                    ]
                                )
                            else:
                                subprocess.Popen(
                                    ["cmd.exe", "/k", f"ntoseye -b gdb --connect localhost:{actual_gdb_port}"]
                                )
                            break
                        except Exception:
                            pass
            else:
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

    def as_user(self, user: str | None = None, password: str | None = None) -> MachineUserContext:
        """Create a user execution context on this Machine for running commands as that user.

        Usage:
            # As a context manager (using default credentials):
            with machine.as_user():
                machine.command.run("whoami")

            # As a context manager with specific credentials:
            with machine.as_user("Bob", password="SecretPassword123!"):
                machine.command.run("whoami")

            # As a controller view:
            admin = machine.as_user()
            admin.command.run("whoami")
        """
        target_user = user or self.default_user
        target_password = password or (self.default_password if target_user == self.default_user else None)

        if not target_user:
            raise ValueError("A user must be specified (or default_user must be configured on Machine).")
        if not target_password:
            raise ValueError(f"Password must be provided when executing commands as user '{target_user}'.")

        return MachineUserContext(machine=self, user=target_user, password=target_password)

    @property
    def is_running(self) -> bool:
        """Return True if the machine power status is 'running'."""
        return self.power.status == "running"

    def close(self) -> None:
        """Clean up machine resources and release reserved ports."""
        if hasattr(self, "microphone") and self.microphone is not None:
            try:
                self.microphone.close()
            except Exception:
                pass
        if hasattr(self, "usb") and self.usb is not None:
            try:
                self.usb.unmount_all()
            except Exception:
                pass
        if hasattr(self, "cd") and self.cd is not None:
            try:
                self.cd.eject_all()
            except Exception:
                pass
        if hasattr(self, "vnc_port"):
            Machine._allocated_vnc_ports.discard(self.vnc_port)
        if hasattr(self, "qga_port"):
            Machine._allocated_vnc_ports.discard(self.qga_port)
        if hasattr(self, "monitor_port"):
            Machine._allocated_vnc_ports.discard(self.monitor_port)
        if hasattr(self, "qmp_port"):
            Machine._allocated_vnc_ports.discard(self.qmp_port)
        if getattr(self, "smb", None) is not None and self.smb is not None:
            try:
                self.smb.stop()
            except Exception:
                pass
        if getattr(self, "smb_port", None) is not None:
            Machine._allocated_vnc_ports.discard(self.smb_port)

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> "Machine":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.power.status == "running":
            self.power.stop()
        self.close()

    def __repr__(self) -> str:
        return f"<Machine image={self.image!r} status={self.power.status!r}>"
