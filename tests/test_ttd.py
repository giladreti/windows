"""Tests for Time Travel Debugging (TTD) subsystem."""

from unittest.mock import MagicMock, patch

import pytest

from windows import (
    Bookmark,
    GDBRemoteClient,
    Image,
    Machine,
    TTDController,
    TTDRecording,
    TTDReplaySession,
)
from windows.qemu import QEMUError, build_run_qemu_cmd


def test_build_run_qemu_cmd_ttd_record(tmp_path):
    disk = tmp_path / "disk.qcow2"
    trace = tmp_path / "trace.bin"
    cmd = build_run_qemu_cmd(
        disk_path=disk,
        cpus=1,
        enable_kvm=False,
        rr_mode="record",
        rr_file=trace,
        rr_snapshot="snap0",
        blkreplay=True,
        filter_replay=True,
    )
    cmd_str = " ".join(cmd)
    assert f"-icount shift=auto,rr=record,rrfile={trace},rrsnapshot=snap0" in cmd_str
    assert "driver=blkreplay" in cmd_str
    assert "filter-replay" in cmd_str
    assert "-smp 1" in cmd_str
    assert "-enable-kvm" not in cmd_str


def test_build_run_qemu_cmd_ttd_replay(tmp_path):
    disk = tmp_path / "disk.qcow2"
    trace = tmp_path / "trace.bin"
    cmd = build_run_qemu_cmd(
        disk_path=disk,
        cpus=1,
        enable_kvm=False,
        rr_mode="replay",
        rr_file=trace,
        rr_snapshot="snap0",
        blkreplay=True,
        filter_replay=True,
    )
    cmd_str = " ".join(cmd)
    assert f"-icount shift=auto,rr=replay,rrfile={trace},rrsnapshot=snap0" in cmd_str
    assert "driver=blkreplay" in cmd_str
    assert "filter-replay" in cmd_str


def test_build_run_qemu_cmd_ttd_restrictions(tmp_path):
    disk = tmp_path / "disk.qcow2"
    trace = tmp_path / "trace.bin"

    # Restriction 1: KVM must be disabled
    with pytest.raises(QEMUError, match="requires TCG emulation; KVM must be disabled"):
        build_run_qemu_cmd(
            disk_path=disk,
            cpus=1,
            enable_kvm=True,
            rr_mode="record",
            rr_file=trace,
        )

    # Restriction 2: cpus must be 1 (SMP is not supported)
    with pytest.raises(QEMUError, match="only supports a single vCPU"):
        build_run_qemu_cmd(
            disk_path=disk,
            cpus=4,
            enable_kvm=False,
            rr_mode="record",
            rr_file=trace,
        )

    # Restriction 3: rr_file required
    with pytest.raises(QEMUError, match="rr_file is required"):
        build_run_qemu_cmd(
            disk_path=disk,
            cpus=1,
            enable_kvm=False,
            rr_mode="record",
            rr_file=None,
        )

    # Restriction 4: invalid rr_mode
    with pytest.raises(QEMUError, match="Invalid rr_mode"):
        build_run_qemu_cmd(
            disk_path=disk,
            cpus=1,
            enable_kvm=False,
            rr_mode="invalid_mode",
            rr_file=trace,
        )


def test_bookmark_serialization():
    bm = Bookmark(icount=123456, name="crash_point", description="Null pointer dereference", rip=0x140001000)
    data = bm.to_dict()
    assert data["icount"] == 123456
    assert data["name"] == "crash_point"
    assert data["rip"] == 0x140001000

    loaded = Bookmark.from_dict(data)
    assert loaded.icount == bm.icount
    assert loaded.name == bm.name
    assert loaded.description == bm.description
    assert loaded.rip == bm.rip


def test_ttd_recording_metadata(tmp_path):
    trace_path = tmp_path / "rec.bin"
    trace_path.write_bytes(b"TRACE_DATA_1234")
    overlay_path = tmp_path / "overlay.qcow2"
    base_path = tmp_path / "base.qcow2"
    meta_path = tmp_path / "rec.json"

    rec = TTDRecording(
        name="test_rec",
        trace_path=trace_path,
        snapshot_name="snap0",
        overlay_disk_path=overlay_path,
        base_disk_path=base_path,
        metadata_path=meta_path,
    )
    assert rec.size_bytes == 15

    bm = rec.add_bookmark(name="bp1", icount=5000, description="Before trigger", rip=0x7FF000)
    assert len(rec.bookmarks) == 1
    assert rec.get_bookmark("bp1") == bm
    assert meta_path.exists()

    # Re-add with same name updates it
    rec.add_bookmark(name="bp1", icount=5500, description="Updated trigger", rip=0x7FF004)
    bm = rec.get_bookmark("bp1")
    assert bm is not None
    assert bm.icount == 5500


def test_machine_ttd_controller_integration(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"qcow2_header")
    img = Image(disk_path=disk)
    machine = Machine(img)

    assert hasattr(machine, "ttd")
    assert isinstance(machine.ttd, TTDController)
    assert machine.ttd.recordings_dir.exists()
    assert machine.record_execution is not None
    assert machine.replay_execution is not None


def test_ttd_controller_record_and_list(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"qcow2_header")
    img = Image(disk_path=disk)
    machine = Machine(img)

    with (
        patch("windows.ttd.create_qcow2_overlay") as mock_overlay,
        patch("windows.ttd.QEMUProcessManager") as mock_pm_cls,
    ):
        mock_pm = MagicMock()
        mock_pm.is_running.return_value = True
        mock_pm_cls.return_value = mock_pm

        # Start recording
        rec = machine.ttd.record("session1", duration=None)
        assert rec.name == "session1"
        assert rec.snapshot_name == "snap0"
        mock_pm.start.assert_called_once()
        mock_overlay.assert_called_once()

        # List recordings
        recordings = machine.ttd.list_recordings()
        assert len(recordings) == 1
        assert recordings[0].name == "session1"

        fetched = machine.ttd.get_recording("session1")
        assert fetched is not None
        assert fetched.name == "session1"

        # Delete recording
        machine.ttd.delete_recording("session1")
        assert machine.ttd.get_recording("session1") is None


def test_ttd_replay_session_methods(tmp_path):
    trace_path = tmp_path / "session.bin"
    trace_path.write_bytes(b"replay_bytes")
    meta_path = tmp_path / "session.json"
    overlay_path = tmp_path / "overlay.qcow2"
    base_path = tmp_path / "base.qcow2"
    qmp_sock = tmp_path / "qmp.sock"

    rec = TTDRecording(
        name="session",
        trace_path=trace_path,
        snapshot_name="snap0",
        overlay_disk_path=overlay_path,
        base_disk_path=base_path,
        metadata_path=meta_path,
    )

    mock_pm = MagicMock()
    mock_pm.is_running.return_value = True

    session = TTDReplaySession(
        recording=rec,
        process_manager=mock_pm,
        qmp_socket_path=qmp_sock,
        gdb_port=1234,
        vnc_display=0,
        vnc_port=5900,
    )

    assert session.is_active is True
    assert repr(session) == "<TTDReplaySession recording='session' gdb_port=1234 active=True>"

    # Mock QMP execution
    session._qmp.execute = MagicMock(return_value={"icount": 42000})
    assert session.current_icount == 42000
    session._qmp.execute.assert_called_with("query-replay")

    # Mock GDB execution
    session._gdb.run_gdb_commands = MagicMock(return_value="rip            0x140002000         0x140002000\n")
    assert session.rip == 0x140002000

    # Step backward
    session.reverse_step(count=3)
    session._qmp.execute.assert_any_call("replay-seek", {"icount": 41997}, timeout=60.0)

    # Step forward
    session.step(count=2)
    session._qmp.execute.assert_any_call("replay-seek", {"icount": 42002}, timeout=60.0)

    # Reverse continue
    session._gdb.reverse_continue = MagicMock()
    session.reverse_continue()
    session._gdb.reverse_continue.assert_called_once()

    # Seek
    session.seek(50000)
    session._qmp.execute.assert_called_with("replay-seek", {"icount": 50000}, timeout=60.0)

    # Bookmarks
    bm = session.add_bookmark("before_crash", "Null deref check")
    assert bm.name == "before_crash"
    assert bm.icount == 42000

    session.goto_bookmark("before_crash")
    session._qmp.execute.assert_called_with("replay-seek", {"icount": 42000}, timeout=60.0)

    # Set breakpoint and watchpoint
    session._gdb.set_breakpoint = MagicMock()
    session.set_breakpoint(0x140003000)
    session._gdb.set_breakpoint.assert_called_with(0x140003000)

    session._gdb.set_watchpoint = MagicMock()
    session.set_watchpoint(0x7FFF0000)
    session._gdb.set_watchpoint.assert_called_with(0x7FFF0000)

    # Stop session
    session.stop()
    mock_pm.stop.assert_called_once()


def test_gdb_remote_client_command_generation():
    client = GDBRemoteClient(port=1234, host="127.0.0.1")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="rip            0x140002000         0x140002000\nrax            0x0                 0\n",
            stderr="",
        )

        rip = client.get_rip()
        assert rip == 0x140002000

        regs = client.get_registers()
        assert regs["rip"] == 0x140002000
        assert regs["rax"] == 0

        client.step(count=2)
        mock_run.assert_called_with(
            [
                "gdb",
                "-nx",
                "-batch",
                "-ex",
                "target remote 127.0.0.1:1234",
                "-ex",
                "stepi",
                "-ex",
                "stepi",
                "-ex",
                "disconnect",
                "-ex",
                "quit",
            ],
            capture_output=True,
            text=True,
            timeout=60.0,
        )

        client.reverse_step(count=1)
        mock_run.assert_called_with(
            [
                "gdb",
                "-nx",
                "-batch",
                "-ex",
                "target remote 127.0.0.1:1234",
                "-ex",
                "reverse-stepi",
                "-ex",
                "disconnect",
                "-ex",
                "quit",
            ],
            capture_output=True,
            text=True,
            timeout=60.0,
        )


def test_machine_wait_for_boot(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"qcow2_header")
    machine = Machine(Image(disk), ttd=True)

    with (
        patch.object(machine.power, "on") as mock_power_on,
        patch.object(machine.command, "wait_until_ready", return_value=True) as mock_wait,
    ):
        ready = machine.wait_for_boot(timeout=60)
        assert ready is True
        mock_power_on.assert_called_once()
        mock_wait.assert_called_once_with(timeout=60, interval=2.0)


def test_record_command_from_running_machine(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"qcow2_header")
    machine = Machine(Image(disk), ttd=True)

    with (
        patch.object(machine.power._manager, "is_running", return_value=True),
        patch.object(machine.command, "wait_until_ready", return_value=True),
        patch.object(machine.command, "run") as mock_cmd_run,
        patch.object(machine.snapshot, "create") as mock_snap_create,
        patch.object(machine.power, "stop") as mock_power_stop,
        patch("windows.ttd.create_qcow2_overlay"),
        patch("windows.ttd.QEMUProcessManager") as mock_pm_cls,
        patch("windows.ttd.QMPClient") as mock_qmp_cls,
    ):
        mock_pm = MagicMock()
        mock_pm.is_running.return_value = True
        mock_pm_cls.return_value = mock_pm

        mock_qmp = MagicMock()
        mock_qmp.execute.return_value = {"icount": 12345}
        mock_qmp_cls.return_value = mock_qmp

        mock_cmd_run.return_value = MagicMock(stdout="desktop-user\n", stderr="", returncode=0)

        # Use machine.record_session as a context manager and call machine.command.run directly
        with machine.record_session("test_whoami") as rec:
            res = machine.command.run("whoami")
            assert res.stdout == "desktop-user\n"

        assert rec.name == "test_whoami"
        assert rec.start_icount == 12345

        # Snapshot of running state was taken before recording
        mock_snap_create.assert_called_once_with("_ttd_booted_test_whoami")
        mock_power_stop.assert_called_once()

        # Recording QEMU was started and stopped
        mock_pm.start.assert_called_once()
        mock_pm.stop.assert_called_once()

        # Metadata was saved
        saved_rec = machine.ttd.get_recording("test_whoami")
        assert saved_rec is not None
        assert saved_rec.name == "test_whoami"


def test_record_session_restriction_error(tmp_path):
    from windows.ttd import TTDRestrictionError

    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"qcow2_header")
    # Default machine has KVM enabled and 4 CPUs
    machine = Machine(Image(disk), enable_kvm=True, cpus=4)

    with (
        patch.object(machine.power._manager, "is_running", return_value=True),
        patch.object(machine.command, "wait_until_ready", return_value=True),
    ):
        with pytest.raises(TTDRestrictionError, match="Machine is running with KVM or multi-core SMP"):
            with machine.record_session("my_session"):
                pass


def test_record_session_without_as(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"qcow2_header")
    machine = Machine(Image(disk), ttd=True)

    with (
        patch.object(machine.power._manager, "is_running", return_value=True),
        patch.object(machine.command, "wait_until_ready", return_value=True),
        patch.object(machine.command, "run") as mock_cmd_run,
        patch.object(machine.snapshot, "create"),
        patch.object(machine.power, "stop"),
        patch("windows.ttd.create_qcow2_overlay"),
        patch("windows.ttd.QEMUProcessManager") as mock_pm_cls,
        patch("windows.ttd.QMPClient"),
    ):
        mock_pm = MagicMock()
        mock_pm.is_running.return_value = True
        mock_pm_cls.return_value = mock_pm

        mock_cmd_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)

        with machine.ttd.record_session("my_session"):
            res = machine.command.run("calc.exe")
            assert res.stdout == "ok"

        mock_pm.start.assert_called_once()
        mock_pm.stop.assert_called_once()
        rec = machine.ttd.get_recording("my_session")
        assert rec is not None
        assert rec.name == "my_session"
