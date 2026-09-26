"""Unit tests for machine.file.share() and SharedFolder lifecycle."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from windows.executor import CommandController, CommandResult
from windows.file import FileController, SharedFolder


def test_shared_folder_lifecycle(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    fc = FileController(mock_cmd)

    unmount_called = False

    def on_unmount():
        nonlocal unmount_called
        unmount_called = True

    share = SharedFolder(
        controller=fc,
        host_path=tmp_path,
        guest_drive="Z:",
        device_id="usb_123",
        drive_id="drv_123",
        backend="usb",
        unmount_cb=on_unmount,
    )

    assert share.is_active is True
    assert share.guest_drive == "Z:"
    assert share.host_path == tmp_path.resolve()
    assert share.backend == "usb"
    assert "Z:" in repr(share)
    assert "active" in repr(share)

    # Test context manager
    with share as s:
        assert s.is_active is True

    assert share.is_active is False
    assert unmount_called is True
    assert "unmounted" in repr(share)


def test_file_controller_share_validation(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    fc = FileController(mock_cmd)

    # Missing from_path
    with pytest.raises(ValueError, match="source directory path"):
        fc.share()

    # Non-existent directory
    with pytest.raises(FileNotFoundError):
        fc.share(from_path=tmp_path / "nonexistent_dir")

    # Invalid backend
    with pytest.raises(ValueError, match="Invalid backend"):
        fc.share(from_path=tmp_path, backend="invalid_backend")  # type: ignore


def test_file_controller_share_usb_success(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="True\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_console = MagicMock()
    mock_console.send_monitor_command.return_value = ""
    mock_machine.console = mock_console

    fc = FileController(mock_cmd, machine=mock_machine)

    share_dir = tmp_path / "shared_content"
    share_dir.mkdir()

    # Test syntax: machine.file.share(from=host_path, to="Z:")
    kwargs: dict[str, Any] = {"from": share_dir, "to": "Z:", "backend": "usb"}
    share = fc.share(**kwargs)

    assert isinstance(share, SharedFolder)
    assert share.guest_drive == "Z:"
    assert share.host_path == share_dir.resolve()
    assert share.is_active is True
    assert share.device_id is not None
    assert share.backend == "usb"

    # Verify QEMU monitor commands were executed
    assert mock_console.send_monitor_command.call_count >= 2
    calls = [c[0][0] for c in mock_console.send_monitor_command.call_args_list]
    assert any("drive_add" in call and f"drv_{share.device_id[4:]}" in call for call in calls)
    assert any("device_add" in call and share.device_id in call for call in calls)

    # Verify guest PowerShell volume assignment was executed
    cmd_calls = [c[0][0] for c in mock_cmd.run.call_args_list]
    assert any("$targetLetter = 'Z:'" in call for call in cmd_calls)

    # Unshare and verify device_del
    share.unshare()
    assert share.is_active is False
    del_calls = [c[0][0] for c in mock_console.send_monitor_command.call_args_list]
    assert any(f"device_del {share.device_id}" in call for call in del_calls)


def test_file_controller_share_usb_monitor_error(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_machine = MagicMock()
    mock_console = MagicMock()
    mock_console.send_monitor_command.return_value = "Could not read directory: Permission denied"
    mock_machine.console = mock_console

    fc = FileController(mock_cmd, machine=mock_machine)

    share_dir = tmp_path / "shared_content"
    share_dir.mkdir()

    with pytest.raises(RuntimeError, match="QEMU failed to add USB drive"):
        fc.share(from_path=share_dir, to="Z:", backend="usb")


def test_file_controller_share_usb_guest_mount_error(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    # Guest script returns False (drive not found / failed to assign)
    mock_cmd.run.return_value = CommandResult(stdout="False\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_console = MagicMock()
    mock_console.send_monitor_command.return_value = ""
    mock_machine.console = mock_console

    fc = FileController(mock_cmd, machine=mock_machine)

    share_dir = tmp_path / "shared_content"
    share_dir.mkdir()

    with pytest.raises(RuntimeError, match="Guest failed to detect and assign USB volume"):
        fc.share(from_path=share_dir, to="Z:", backend="usb")

    # Verify device_del was attempted during rollback
    calls = [c[0][0] for c in mock_console.send_monitor_command.call_args_list]
    assert any("device_del" in call for call in calls)


def test_file_controller_share_usb_limit_exceeded(tmp_path, monkeypatch):
    mock_cmd = MagicMock(spec=CommandController)
    fc = FileController(mock_cmd)

    share_dir = tmp_path / "large_dir"
    share_dir.mkdir()

    # Simulate directory containing a file >= 2GB
    monkeypatch.setattr("windows.file._inspect_dir_limits", lambda path, **kwargs: (3 * 1024**3, 2500 * 1024**2))

    with pytest.raises(ValueError, match=r"cannot be shared via USB vvfat.*backend='smb'"):
        fc.share(from_path=share_dir, to="Z:", backend="usb")


def test_file_controller_share_auto_selects_smb_for_large_dir(tmp_path, monkeypatch):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="SUCCESS\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_smb = MagicMock()
    mock_smb.is_running = True
    mock_machine.smb = mock_smb

    fc = FileController(mock_cmd, machine=mock_machine)

    share_dir = tmp_path / "large_dir"
    share_dir.mkdir()

    # Simulate >500MB directory
    monkeypatch.setattr("windows.file._inspect_dir_limits", lambda path, **kwargs: (600 * 1024**2, 100 * 1024**2))

    share = fc.share(from_path=share_dir, to="Z:", backend="auto")
    assert share.backend == "smb"
    assert mock_smb.add_share.called

    # Unshare should remove share from smb manager
    share.unshare()
    assert mock_smb.remove_share.called


def test_file_controller_share_smb_success(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="SUCCESS\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_smb = MagicMock()
    mock_smb.is_running = True
    mock_smb.username = "smbuser"
    mock_smb.password = "Password123!"
    mock_machine.smb = mock_smb

    fc = FileController(mock_cmd, machine=mock_machine)

    share_dir = tmp_path / "smb_dir"
    share_dir.mkdir()

    share = fc.share(from_path=share_dir, to="Y:", backend="smb")
    assert share.backend == "smb"
    assert share.guest_drive == "Y:"
    assert mock_smb.add_share.called
    assert share.is_active is True

    # Check PowerShell commands executed: NO AllowInsecureGuestAuth, YES credentials
    cmd_calls = [c[0][0] for c in mock_cmd.run.call_args_list]
    assert not any("AllowInsecureGuestAuth" in call for call in cmd_calls)
    assert not any("EnableLinkedConnections" in call for call in cmd_calls)
    assert any("New-SmbGlobalMapping" in call for call in cmd_calls)
    assert any("-Credential $cred" in call for call in cmd_calls)
    assert any("net use $drive $remote $pass /user:$user" in call for call in cmd_calls)

    share.unshare()
    assert share.is_active is False
    assert mock_smb.remove_share.called


def test_smb_server_manager_credentials():
    from windows.smb import SMBServerManager, UniversalCredentials

    # Default credentials
    mgr = SMBServerManager(port=5999)
    assert mgr.username == "Administrator"
    assert mgr.password == "Password123!"

    # Custom credentials
    mgr2 = SMBServerManager(port=5998, username="customuser", password="SecretPassword99!")
    assert mgr2.username == "customuser"
    assert mgr2.password == "SecretPassword99!"

    # UniversalCredentials tests
    u = UniversalCredentials(0, b"lm", b"nt")
    assert "administrator" in u
    assert "randomuser" in u
    assert u["administrator"] == (0, b"lm", b"nt")
    assert u["randomuser"] == (0, b"lm", b"nt")
    assert len(u) >= 1


def test_file_controller_share_smb_inherits_user_context(tmp_path):
    from windows.executor import _current_user

    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="SUCCESS\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_smb = MagicMock()
    mock_smb.is_running = True
    mock_machine.smb = mock_smb

    fc = FileController(mock_cmd, machine=mock_machine)
    share_dir = tmp_path / "user_smb_dir"
    share_dir.mkdir()

    token = _current_user.set(("SpecialUser", "SpecialPassword123!"))
    try:
        share = fc.share(from_path=share_dir, to="W:", backend="smb")
        assert share.backend == "smb"
        mock_smb.add_credential.assert_called_with("SpecialUser", "SpecialPassword123!")

        cmd_calls = [c[0][0] for c in mock_cmd.run.call_args_list]
        assert any("$user = 'SpecialUser'" in call for call in cmd_calls)
        assert any("$pass = 'SpecialPassword123!'" in call for call in cmd_calls)
    finally:
        _current_user.reset(token)


def test_file_controller_share_smb_uses_default_credentials(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="SUCCESS\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_machine.default_user = "DefaultAdmin"
    mock_machine.default_password = "DefaultPassword456!"
    mock_smb = MagicMock()
    mock_smb.is_running = True
    mock_machine.smb = mock_smb

    fc = FileController(mock_cmd, machine=mock_machine)
    share_dir = tmp_path / "default_smb_dir"
    share_dir.mkdir()

    share = fc.share(from_path=share_dir, to="V:", backend="smb")
    assert share.backend == "smb"
    mock_smb.add_credential.assert_called_with("DefaultAdmin", "DefaultPassword456!")

    cmd_calls = [c[0][0] for c in mock_cmd.run.call_args_list]
    assert any("$user = 'DefaultAdmin'" in call for call in cmd_calls)
    assert any("$pass = 'DefaultPassword456!'" in call for call in cmd_calls)


def test_file_controller_share_smb_explicit_user_inherits_default_password(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="SUCCESS\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_machine.default_user = "DefaultAdmin"
    mock_machine.default_password = "DefaultPassword456!"
    mock_smb = MagicMock()
    mock_smb.is_running = True
    mock_machine.smb = mock_smb

    fc = FileController(mock_cmd, machine=mock_machine)
    share_dir = tmp_path / "explicit_smb_dir"
    share_dir.mkdir()

    # Specifying default user without password inherits default password
    share = fc.share(from_path=share_dir, to="U:", backend="smb", user="DefaultAdmin")
    assert share.backend == "smb"
    mock_smb.add_credential.assert_called_with("DefaultAdmin", "DefaultPassword456!")

    # Specifying non-default user without password raises ValueError
    with pytest.raises(ValueError, match="Password must be provided"):
        fc.share(from_path=share_dir, to="T:", backend="smb", user="UnknownUser")


def test_file_controller_share_smb_as_system(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="SUCCESS\n", stderr="", returncode=0)

    mock_machine = MagicMock()
    mock_machine.default_user = "DefaultAdmin"
    mock_machine.default_password = "DefaultPassword456!"
    mock_smb = MagicMock()
    mock_smb.is_running = True
    mock_smb.username = "smb_sys"
    mock_smb.password = "smb_sys_pass"
    mock_machine.smb = mock_smb

    fc = FileController(mock_cmd, machine=mock_machine)
    share_dir = tmp_path / "system_smb_dir"
    share_dir.mkdir()

    share = fc.share(from_path=share_dir, to="S:", backend="smb", as_system=True)
    assert share.backend == "smb"
    mock_smb.add_credential.assert_called_with("smb_sys", "smb_sys_pass")


def test_smb_server_manager_lifecycle_and_clean_exit(tmp_path):
    """Verify that SMBServerManager starts, tracks clients, stops cleanly without hanging, and reaps threads."""
    import socket
    import threading
    import time

    from windows.smb import SMBServerManager

    mgr = SMBServerManager(port=15499)
    mgr.start()
    assert mgr.is_running is True

    # Connect client socket (simulating QEMU guestfwd / Windows SMB client)
    client_sock = socket.create_connection(("127.0.0.1", 15499))
    time.sleep(0.2)

    # Verify client socket was registered for tracking
    assert len(mgr._client_sockets) >= 1

    # Verify stop terminates server, closes sockets, and reaps threads quickly (<1.5s)
    t0 = time.time()
    mgr.stop()
    elapsed = time.time() - t0
    assert elapsed < 1.5
    assert mgr.is_running is False
    assert len(mgr._client_sockets) == 0

    # Ensure all SMB worker and pipe server threads are terminated
    active_thread_names = [t.name for t in threading.enumerate()]
    assert "SMBServerWorker" not in active_thread_names

    client_sock.close()
