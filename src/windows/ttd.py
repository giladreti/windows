"""Time Travel Debugging (TTD) subsystem for deterministic recording, reverse execution, and kernel introspection."""

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from windows.network import QMPClient
from windows.qemu import (
    QEMUProcessManager,
    build_run_qemu_cmd,
    create_qcow2_overlay,
    find_free_port,
)

if TYPE_CHECKING:
    from windows.machine import Machine


class TTDError(RuntimeError):
    """Exception raised for Time Travel Debugging operations."""


class TTDRestrictionError(TTDError):
    """Exception raised when TTD operations violate QEMU deterministic replay requirements."""


@dataclass
class Bookmark:
    """Represents a saved point in execution time (instruction counter & instruction pointer)."""

    icount: int
    name: str
    description: str = ""
    timestamp: float = field(default_factory=time.time)
    rip: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Bookmark":
        return cls(
            icount=data["icount"],
            name=data["name"],
            description=data.get("description", ""),
            timestamp=data.get("timestamp", time.time()),
            rip=data.get("rip"),
        )


@dataclass
class TTDRecording:
    """Represents a recorded deterministic execution trace."""

    name: str
    trace_path: Path
    snapshot_name: str
    overlay_disk_path: Path
    base_disk_path: Path
    metadata_path: Path
    created_at: float = field(default_factory=time.time)
    bookmarks: list[Bookmark] = field(default_factory=list)
    start_icount: int = 0
    final_icount: int | None = None
    gdb_port: int = 1234
    controller: "TTDController | None" = field(default=None, repr=False)
    _pm: Any = field(default=None, repr=False)
    _qmp: Any = field(default=None, repr=False)

    def __enter__(self) -> "TTDRecording":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._pm is not None and self._pm.is_running():
            if self._qmp is not None:
                try:
                    rep = self._qmp.execute("query-replay")
                    self.final_icount = int(rep.get("icount", 0))
                except Exception:
                    pass
            self._pm.stop(timeout=10.0, sig=signal.SIGINT)
            self.save_metadata()

    @property
    def size_bytes(self) -> int:
        """Return the size of the recorded trace file in bytes."""
        if self.trace_path.exists():
            return self.trace_path.stat().st_size
        return 0

    def add_bookmark(
        self,
        name: str,
        icount: int,
        description: str = "",
        rip: int | None = None,
    ) -> Bookmark:
        """Add and persist a bookmark to this recording."""
        bm = Bookmark(icount=icount, name=name, description=description, rip=rip)
        # Replace existing if name exists
        self.bookmarks = [b for b in self.bookmarks if b.name != name]
        self.bookmarks.append(bm)
        self.save_metadata()
        return bm

    def get_bookmark(self, name: str) -> Bookmark | None:
        """Find bookmark by name."""
        for bm in self.bookmarks:
            if bm.name == name:
                return bm
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_path": str(self.trace_path),
            "snapshot_name": self.snapshot_name,
            "overlay_disk_path": str(self.overlay_disk_path),
            "base_disk_path": str(self.base_disk_path),
            "created_at": self.created_at,
            "bookmarks": [b.to_dict() for b in self.bookmarks],
            "start_icount": self.start_icount,
            "final_icount": self.final_icount,
            "gdb_port": self.gdb_port,
        }

    def save_metadata(self) -> None:
        """Save recording metadata to disk."""
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def replay(
        self,
        gdb_port: int | None = None,
        vnc_display: int | None = None,
        open_console: bool = False,
        **kwargs: Any,
    ) -> "TTDReplaySession":
        """Replay this recorded session."""
        if self.controller is None:
            raise TTDError(f"Recording '{self.name}' is not attached to a TTDController.")
        return self.controller.replay(
            name=self.name,
            gdb_port=gdb_port,
            vnc_display=vnc_display,
            open_console=open_console,
            **kwargs,
        )


class GDBRemoteClient:
    """Lightweight client communicating with GDB Remote Serial Protocol (RSP) or local gdb binary."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.port = port
        self.host = host

    def run_gdb_commands(self, commands: list[str], timeout: float = 60.0) -> str:
        """Execute a batch of GDB commands via 'gdb -nx -batch' attached to the QEMU GDB stub."""
        cmd = ["gdb", "-nx", "-batch", "-ex", f"target remote {self.host}:{self.port}"]
        for c in commands:
            cmd.extend(["-ex", c])
        cmd.extend(["-ex", "disconnect", "-ex", "quit"])

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 and "No executable has been specified" not in res.stderr:
            raise TTDError(f"GDB command failed:\n{res.stderr.strip()}")
        return res.stdout

    def step(self, count: int = 1) -> None:
        """Step forward one or more instructions."""
        cmds = ["stepi"] * count
        self.run_gdb_commands(cmds)

    def reverse_step(self, count: int = 1) -> None:
        """Step backward one or more instructions using reverse-stepi."""
        cmds = ["reverse-stepi"] * count
        self.run_gdb_commands(cmds)

    def reverse_continue(self) -> None:
        """Continue backward until the previous breakpoint or start of replay."""
        self.run_gdb_commands(["reverse-continue"])

    def continue_forward(self) -> None:
        """Continue forward until the next breakpoint or end of replay."""
        self.run_gdb_commands(["continue"])

    def get_rip(self) -> int:
        """Read current instruction pointer (RIP)."""
        output = self.run_gdb_commands(["info registers rip"])
        for line in output.splitlines():
            if "rip" in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1], 16)
                    except ValueError:
                        pass
        return 0

    def get_registers(self) -> dict[str, int]:
        """Read general CPU registers."""
        output = self.run_gdb_commands(["info registers"])
        regs: dict[str, int] = {}
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0]
                try:
                    regs[name] = int(parts[1], 16)
                except ValueError:
                    pass
        return regs

    def set_breakpoint(self, location: str | int) -> None:
        """Set a breakpoint at a memory address or symbol."""
        loc = f"*0x{location:x}" if isinstance(location, int) else str(location)
        self.run_gdb_commands([f"break {loc}"])

    def set_watchpoint(self, location: str | int) -> None:
        """Set a write watchpoint at a memory address or symbol."""
        loc = f"*(unsigned long long *)0x{location:x}" if isinstance(location, int) else str(location)
        self.run_gdb_commands([f"watch {loc}"])


class TTDReplaySession:
    """Controls an active QEMU replay session with GDB and QMP navigation."""

    def __init__(
        self,
        recording: TTDRecording,
        process_manager: QEMUProcessManager,
        qmp_socket_path: Path,
        gdb_port: int,
        vnc_display: int,
        vnc_port: int,
    ):
        self.recording = recording
        self._pm = process_manager
        self.qmp_socket_path = Path(qmp_socket_path)
        self.gdb_port = gdb_port
        self.vnc_display = vnc_display
        self.vnc_port = vnc_port
        self._qmp = QMPClient(self.qmp_socket_path)
        self._gdb = GDBRemoteClient(port=self.gdb_port)

    @property
    def is_active(self) -> bool:
        """Return True if the replay session process is running."""
        return self._pm.is_running()

    @property
    def current_icount(self) -> int:
        """Query current deterministic instruction count from QEMU QMP."""
        try:
            res = self._qmp.execute("query-replay")
            return int(res.get("icount", 0))
        except Exception:
            return 0

    @property
    def rip(self) -> int:
        """Query current RIP/EIP instruction pointer."""
        regs = self.get_registers()
        return regs.get("rip", regs.get("eip", 0))

    def reverse_step(self, count: int = 1) -> int:
        """Step backward in the instruction stream by count instructions.

        Returns the new RIP instruction pointer.
        """
        target = max(self.recording.start_icount, self.current_icount - count)
        self.seek(target)
        return self.rip

    def step(self, count: int = 1) -> int:
        """Step forward in the instruction stream by count instructions.

        Returns the new RIP instruction pointer.
        """
        self.seek(self.current_icount + count)
        return self.rip

    def reverse_continue(self) -> None:
        """Continue backward execution until hitting previous breakpoint or start."""
        self._gdb.reverse_continue()

    def continue_forward(self) -> None:
        """Continue forward execution until hitting next breakpoint or end."""
        self._gdb.continue_forward()

    def seek(self, icount: int) -> None:
        """Seek execution deterministically to an exact instruction count via QMP."""
        try:
            self._qmp.execute(
                "human-monitor-command",
                {"command-line": f"loadvm {self.recording.snapshot_name}"},
                timeout=60.0,
            )
        except Exception:
            pass
        if icount > self.recording.start_icount:
            self._qmp.execute("replay-seek", {"icount": icount}, timeout=60.0)

    def add_bookmark(self, name: str, description: str = "") -> Bookmark:
        """Save a bookmark at the current execution moment."""
        icount = self.current_icount
        rip = self.rip
        return self.recording.add_bookmark(name=name, icount=icount, description=description, rip=rip)

    def goto_bookmark(self, name: str) -> None:
        """Seek execution directly to a named bookmark."""
        bm = self.recording.get_bookmark(name)
        if bm is None:
            raise TTDError(f"Bookmark '{name}' not found in recording '{self.recording.name}'.")
        self.seek(bm.icount)

    def get_registers(self) -> dict[str, int]:
        """Read all general CPU registers at current moment."""
        try:
            raw_out = self._qmp.execute("human-monitor-command", {"command-line": "info registers"})
            out_str = (
                raw_out
                if isinstance(raw_out, str)
                else str(raw_out.get("return", ""))
                if isinstance(raw_out, dict)
                else ""
            )
            regs: dict[str, int] = {}
            for line in out_str.splitlines():
                for token in line.split():
                    if "=" in token:
                        k, v = token.split("=", 1)
                        try:
                            regs[k.lower()] = int(v, 16)
                        except ValueError:
                            pass
            if regs:
                return regs
        except Exception:
            pass
        return self._gdb.get_registers()

    def set_breakpoint(self, location: str | int) -> None:
        """Set a breakpoint at an instruction address or symbol."""
        self._gdb.set_breakpoint(location)

    def set_watchpoint(self, location: str | int) -> None:
        """Set a write watchpoint at memory address or expression."""
        self._gdb.set_watchpoint(location)

    def launch_ntoseye(
        self,
        terminal: bool = True,
        wait_for_exit: bool = False,
    ) -> subprocess.Popen:
        """Launch an ntoseye kernel debugger session connected to this replay.

        Args:
            terminal: If True, spawns ntoseye in a visible terminal emulator window.
            wait_for_exit: If True, blocks until the ntoseye process exits.
        """
        ntoseye_bin = shutil.which("ntoseye") or "/home/gilad/.local/bin/ntoseye"
        if not os.path.exists(ntoseye_bin):
            raise TTDError(f"ntoseye binary not found at '{ntoseye_bin}'.")

        dbg_cmd = f"{ntoseye_bin} -b gdb --connect localhost:{self.gdb_port}"

        if terminal:
            terminal_cmds = ["x-terminal-emulator", "gnome-terminal", "kitty", "alacritty", "xterm"]
            term_bin = None
            for t in terminal_cmds:
                if shutil.which(t):
                    term_bin = t
                    break
            if not term_bin:
                proc = subprocess.Popen(dbg_cmd.split())
            else:
                proc = subprocess.Popen([term_bin, "-e", dbg_cmd])
        else:
            proc = subprocess.Popen(
                dbg_cmd.split(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        if wait_for_exit:
            proc.wait()
        return proc

    def launch_gdb(
        self,
        terminal: bool = True,
        wait_for_exit: bool = False,
    ) -> subprocess.Popen:
        """Launch an interactive GDB session configured for reverse debugging."""
        gdb_bin = shutil.which("gdb") or "gdb"
        gdb_cmd = f"{gdb_bin} -ex 'target remote localhost:{self.gdb_port}'"

        if terminal:
            terminal_cmds = ["x-terminal-emulator", "gnome-terminal", "kitty", "alacritty", "xterm"]
            term_bin = None
            for t in terminal_cmds:
                if shutil.which(t):
                    term_bin = t
                    break
            if not term_bin:
                proc = subprocess.Popen(gdb_cmd.split())
            else:
                proc = subprocess.Popen([term_bin, "-e", gdb_cmd])
        else:
            proc = subprocess.Popen(
                gdb_cmd.split(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        if wait_for_exit:
            proc.wait()
        return proc

    def stop(self) -> None:
        """Stop the replay session and clean up resources."""
        if self._pm.is_running():
            self._pm.stop(timeout=5.0, sig=signal.SIGINT)

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> "TTDReplaySession":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<TTDReplaySession recording={self.recording.name!r} gdb_port={self.gdb_port} active={self.is_active}>"


class TTDController:
    """Controller for recording and replaying deterministic execution traces."""

    def __init__(self, machine: "Machine"):
        self._machine = machine
        self.recordings_dir = self._machine.image.disk_path.parent / ".ttd_recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def _get_recording_paths(self, name: str) -> tuple[Path, Path, Path, Path, Path]:
        trace_path = self.recordings_dir / f"{name}.bin"
        meta_path = self.recordings_dir / f"{name}.json"
        overlay_path = self.recordings_dir / f"{name}_disk.qcow2"
        qmp_sock = self.recordings_dir / f"{name}_qmp.sock"
        mon_sock = self.recordings_dir / f"{name}_mon.sock"
        return trace_path, meta_path, overlay_path, qmp_sock, mon_sock

    def record(
        self,
        name: str,
        duration: float | None = None,
        start_snapshot: str | None = None,
        output_disk: Path | str | None = None,
        gdb_port: int | None = None,
        icount: str = "shift=auto",
        open_console: bool = False,
        **kwargs: Any,
    ) -> TTDRecording:
        """Record deterministic execution of the virtual machine.

        To guarantee determinism and image safety:
        1. Disables KVM (`enable_kvm=False`) and forces single vCPU (`cpus=1`).
        2. Wraps the disk in an overlay with QEMU's `blkreplay` driver.
        3. Wraps the network in QEMU's `filter-replay`.
        4. Saves an execution trace to `<recordings_dir>/<name>.bin`.

        Args:
            name: Unique name for this recording session.
            duration: Optional duration in seconds to record before stopping.
            start_snapshot: Optional snapshot tag to record from (defaults to 'snap0').
            output_disk: Optional path for recording disk overlay.
            gdb_port: Optional GDB port to expose during recording.
            icount: Instruction counter config (default 'shift=auto').
            open_console: If True, opens VNC display.
        """
        if self._machine.power.status == "running":
            self._machine.power.off()

        trace_path, meta_path, default_overlay_path, qmp_sock, mon_sock = self._get_recording_paths(name)
        overlay_path = Path(output_disk) if output_disk else default_overlay_path
        snapshot_name = f"{name}_snap" if start_snapshot else "snap0"
        loadvm_snap = start_snapshot
        actual_gdb_port = gdb_port or find_free_port(1234)

        for s in (qmp_sock, mon_sock):
            if s.exists():
                try:
                    s.unlink()
                except OSError:
                    pass

        # Create isolated fresh disk overlay for recording so base disk is pristine
        if overlay_path.exists():
            try:
                overlay_path.unlink()
            except OSError:
                pass
        create_qcow2_overlay(overlay_path, self._machine.image.disk_path)

        # Build QEMU record command
        vnc_disp = self._machine.vnc_display
        cmd = build_run_qemu_cmd(
            disk_path=overlay_path,
            ram_mb=self._machine.ram_mb,
            cpus=1,  # Strictly 1 CPU for TTD
            enable_kvm=False,  # Strictly TCG for TTD
            headless=self._machine.headless,
            vnc_display=vnc_disp,
            monitor_socket_path=mon_sock,
            qmp_socket_path=qmp_sock,
            qga_socket_path=self._machine.qga_socket_path,
            gdb_port=actual_gdb_port,
            icount=icount,
            rr_mode="record",
            rr_file=trace_path,
            rr_snapshot=snapshot_name,
            loadvm=loadvm_snap,
            blkreplay=True,
            filter_replay=True,
            ttd=True,
            **kwargs,
        )

        pm = QEMUProcessManager(cmd)
        pm.start(startup_check_delay=1.0)

        qmp = QMPClient(qmp_sock)
        start_icount = 0
        try:
            rep = qmp.execute("query-replay")
            start_icount = int(rep.get("icount", 0))
        except Exception:
            pass

        recording = TTDRecording(
            name=name,
            trace_path=trace_path,
            snapshot_name=snapshot_name,
            overlay_disk_path=overlay_path,
            base_disk_path=self._machine.image.disk_path,
            metadata_path=meta_path,
            start_icount=start_icount,
            gdb_port=actual_gdb_port,
            controller=self,
            _pm=pm,
            _qmp=qmp,
        )

        self._machine._process_manager = pm
        self._machine.power._manager = pm

        if open_console:
            self._machine.console.open()

        if duration is not None:
            time.sleep(duration)
            try:
                rep = qmp.execute("query-replay")
                recording.final_icount = int(rep.get("icount", 0))
            except Exception:
                pass
            pm.stop(timeout=10.0, sig=signal.SIGINT)
            recording.save_metadata()

        recording.save_metadata()
        return recording

    @contextmanager
    def record_session(
        self,
        name: str,
        start_snapshot: str | None = None,
        open_console: bool = False,
        icount: str = "shift=auto",
        **kwargs: Any,
    ) -> Generator[TTDRecording]:
        """Context manager to record a section of execution for Time Travel Debugging.

        Supports recording on a booted/running machine or from a stopped state.
        When recording on a running machine:
        1. Verifies TTD requirements (cpus=1, enable_kvm=False).
        2. Checkpoints the running booted VM state via internal snapshot.
        3. Stops the unrecorded instance.
        4. Starts QEMU in deterministic record mode (-loadvm <booted_snapshot>, rr=record).
        5. Inside the with-block, execute commands directly via machine.command.run(...).
        6. On exit, stops QEMU cleanly with SIGINT and saves the deterministic trace.

        Usage:
            machine.wait_for_boot()
            with machine.record_session("calc_execution") as rec:
                machine.command.run("powershell.exe -Command ...")
        """
        rec_name = name
        effective_start_snap = start_snapshot

        if self._machine.power.status == "running":
            if self._machine.enable_kvm or self._machine.cpus > 1:
                raise TTDRestrictionError(
                    "Machine is running with KVM or multi-core SMP. "
                    "Deterministic TTD recording requires single vCPU and TCG emulation. "
                    "Start Machine with enable_kvm=False and cpus=1 (or Machine(image, ttd=True))."
                )
            if not self._machine.command.wait_until_ready(timeout=30):
                raise TTDError(
                    "Cannot start record_session: QEMU Guest Agent is not responsive on the running VM. "
                    "Make sure the machine has completed booting (e.g. machine.wait_for_boot()) before recording."
                )
            snap_tag = f"_ttd_booted_{rec_name}"
            self._machine.snapshot.create(snap_tag)
            self._machine.power.stop()
            self._machine.command.qga.close()
            effective_start_snap = snap_tag

        trace_path, meta_path, default_overlay_path, _, _ = self._get_recording_paths(rec_name)
        rec_snap = f"{rec_name}_snap"
        actual_gdb_port = find_free_port(1234)

        if effective_start_snap is not None:
            target_disk = self._machine.image.disk_path
        else:
            target_disk = default_overlay_path
            if target_disk.exists():
                try:
                    target_disk.unlink()
                except OSError:
                    pass
            create_qcow2_overlay(target_disk, self._machine.image.disk_path)

        for s in (self._machine.qga_socket_path, self._machine.monitor_socket_path, self._machine.qmp_socket_path):
            if s.exists():
                try:
                    s.unlink()
                except OSError:
                    pass

        rec_snapshot_name = effective_start_snap if effective_start_snap else rec_snap

        cmd = build_run_qemu_cmd(
            disk_path=target_disk,
            ram_mb=self._machine.ram_mb,
            cpus=1,
            enable_kvm=False,
            headless=self._machine.headless,
            vnc_display=self._machine.vnc_display,
            monitor_socket_path=self._machine.monitor_socket_path,
            qmp_socket_path=self._machine.qmp_socket_path,
            qga_socket_path=self._machine.qga_socket_path,
            gdb_port=actual_gdb_port,
            icount=icount,
            rr_mode="record",
            rr_file=trace_path,
            rr_snapshot=None if effective_start_snap else rec_snap,
            loadvm=effective_start_snap,
            blkreplay=True,
            filter_replay=True,
            ttd=True,
            **kwargs,
        )

        pm = QEMUProcessManager(cmd)
        orig_pm = self._machine._process_manager
        self._machine._process_manager = pm
        self._machine.power._manager = pm
        pm.start(startup_check_delay=1.5)

        if open_console:
            self._machine.console.open()

        qmp = QMPClient(self._machine.qmp_socket_path)
        start_icount = 0
        try:
            rep = qmp.execute("query-replay")
            start_icount = int(rep.get("icount", 0))
        except Exception:
            pass

        recording = TTDRecording(
            name=rec_name,
            trace_path=trace_path,
            snapshot_name=rec_snapshot_name,
            overlay_disk_path=target_disk,
            base_disk_path=self._machine.image.disk_path,
            metadata_path=meta_path,
            start_icount=start_icount,
            gdb_port=actual_gdb_port,
            controller=self,
            _pm=pm,
            _qmp=qmp,
        )

        try:
            if effective_start_snap is not None:
                if not self._machine.command.wait_until_ready(timeout=120):
                    raise TTDError(
                        f"QEMU Guest Agent failed to become responsive after loading snapshot '{effective_start_snap}'. "
                        "The guest OS may need more time to resume under TCG emulation."
                    )
            yield recording
        finally:
            self._machine.command.qga.close()
            final_icount = None
            try:
                rep = qmp.execute("query-replay")
                final_icount = int(rep.get("icount", 0))
            except Exception:
                pass
            pm.stop(timeout=10.0, sig=signal.SIGINT)
            self._machine._process_manager = orig_pm
            self._machine.power._manager = orig_pm
            recording.final_icount = final_icount
            recording.save_metadata()

    def replay(
        self,
        name: str,
        gdb_port: int | None = None,
        vnc_display: int | None = None,
        open_console: bool = False,
        icount: str = "shift=auto",
        **kwargs: Any,
    ) -> TTDReplaySession:
        """Start a deterministic replay session from a recorded execution trace.

        Args:
            name: Name of the recorded session.
            gdb_port: GDB stub port (defaults to recorded port or free port).
            vnc_display: Optional VNC display index.
            open_console: If True, opens VNC display.
            icount: Instruction counter config (default 'shift=auto').

        Returns:
            TTDReplaySession for reverse-stepping, seeking, and attaching debuggers.
        """
        if self._machine.power.status == "running":
            self._machine.power.off()

        recording = self.get_recording(name)
        if recording is None:
            raise TTDError(f"Recording '{name}' does not exist.")

        if not recording.trace_path.exists():
            raise TTDError(f"Replay trace file '{recording.trace_path}' not found.")

        actual_gdb_port = gdb_port or recording.gdb_port or find_free_port(1234)
        actual_vnc_disp = vnc_display if vnc_display is not None else self._machine.vnc_display
        actual_vnc_port = 5900 + actual_vnc_disp

        _, _, _, qmp_sock, mon_sock = self._get_recording_paths(name)
        for s in (qmp_sock, mon_sock):
            if s.exists():
                try:
                    s.unlink()
                except OSError:
                    pass

        cmd = build_run_qemu_cmd(
            disk_path=recording.overlay_disk_path,
            ram_mb=self._machine.ram_mb,
            cpus=1,  # Strictly 1 CPU
            enable_kvm=False,  # Strictly TCG
            headless=self._machine.headless,
            vnc_display=actual_vnc_disp,
            monitor_socket_path=mon_sock,
            qmp_socket_path=qmp_sock,
            gdb_port=actual_gdb_port,
            stop_at_boot=True,  # Pauses at boot (-S)
            icount=icount,
            rr_mode="replay",
            rr_file=recording.trace_path,
            loadvm=recording.snapshot_name,
            blkreplay=True,
            filter_replay=True,
            ttd=True,
            **kwargs,
        )

        pm = QEMUProcessManager(cmd)
        pm.start(startup_check_delay=1.0)

        session = TTDReplaySession(
            recording=recording,
            process_manager=pm,
            qmp_socket_path=qmp_sock,
            gdb_port=actual_gdb_port,
            vnc_display=actual_vnc_disp,
            vnc_port=actual_vnc_port,
        )

        if open_console:
            self._machine.console.open()

        return session

    def list_recordings(self) -> list[TTDRecording]:
        """List all recorded deterministic traces in this machine's repository."""
        recordings: list[TTDRecording] = []
        if not self.recordings_dir.exists():
            return recordings

        for meta_file in sorted(self.recordings_dir.glob("*.json")):
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                name = data["name"]
                rec = TTDRecording(
                    name=name,
                    trace_path=Path(data["trace_path"]),
                    snapshot_name=data.get("snapshot_name", "snap0"),
                    overlay_disk_path=Path(data["overlay_disk_path"]),
                    base_disk_path=Path(data.get("base_disk_path", self._machine.image.disk_path)),
                    metadata_path=meta_file,
                    created_at=data.get("created_at", meta_file.stat().st_mtime),
                    bookmarks=[Bookmark.from_dict(b) for b in data.get("bookmarks", [])],
                    start_icount=data.get("start_icount", 0),
                    final_icount=data.get("final_icount"),
                    gdb_port=data.get("gdb_port", 1234),
                    controller=self,
                )
                recordings.append(rec)
            except Exception:
                continue
        return recordings

    def get_recording(self, name: str) -> TTDRecording | None:
        """Find a recording by name."""
        for rec in self.list_recordings():
            if rec.name == name:
                return rec
        # Check if json file exists directly
        _, meta_path, _, _, _ = self._get_recording_paths(name)
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                return TTDRecording(
                    name=name,
                    trace_path=Path(data["trace_path"]),
                    snapshot_name=data.get("snapshot_name", "snap0"),
                    overlay_disk_path=Path(data["overlay_disk_path"]),
                    base_disk_path=Path(data.get("base_disk_path", self._machine.image.disk_path)),
                    metadata_path=meta_path,
                    created_at=data.get("created_at", meta_path.stat().st_mtime),
                    bookmarks=[Bookmark.from_dict(b) for b in data.get("bookmarks", [])],
                    start_icount=data.get("start_icount", 0),
                    final_icount=data.get("final_icount"),
                    gdb_port=data.get("gdb_port", 1234),
                    controller=self,
                )
            except Exception:
                pass
        return None

    def delete_recording(self, name: str) -> None:
        """Delete a recording and its trace, overlay disk, and metadata files."""
        for p in self._get_recording_paths(name):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
