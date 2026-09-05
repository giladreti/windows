"""Unit tests for machine.debug(), machine.run(), and QEMU monitor debug scripts."""

from unittest.mock import patch

from windows.image import Image
from windows.machine import Machine


@patch("windows.machine.QEMUProcessManager")
@patch("windows.machine.build_run_qemu_cmd")
def test_machine_debug_stop_at_boot(mock_build_cmd, mock_pm_cls, tmp_path):
    fake_disk = tmp_path / "win.qcow2"
    fake_disk.write_bytes(b"dummy")
    img = Image(fake_disk)
    mach = Machine(img)

    init_script = ["info status", "info cpus"]

    with (
        patch.object(mach.console, "open") as mock_open,
        patch.object(mach.console, "execute_monitor_script") as mock_exec,
        patch.object(mach.console, "monitor_continue") as mock_cont,
    ):
        mach.debug(init_script=init_script, pause_at_boot=True, auto_continue=True, open_console=True)

        mock_open.assert_called_once()
        mock_exec.assert_called_once_with(init_script)
        mock_cont.assert_called_once()

    # Verify stop_at_boot=True was passed to build_run_qemu_cmd
    _, kwargs = mock_build_cmd.call_args
    assert kwargs.get("stop_at_boot") is True


@patch("windows.machine.QEMUProcessManager")
def test_machine_run_success(mock_pm_cls, tmp_path):
    fake_disk = tmp_path / "win.qcow2"
    fake_disk.write_bytes(b"dummy")
    img = Image(fake_disk)
    mach = Machine(img)

    with (
        patch.object(mach.command, "wait_until_ready", return_value=True),
        patch.object(mach.command, "run") as mock_run,
    ):
        mach.run()
        mock_run.assert_called_once_with("hostname")
