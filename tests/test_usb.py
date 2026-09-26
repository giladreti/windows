"""Unit tests for USB drive emulation, disk creation, and USBController."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from windows.usb import (
    USBController,
    USBDevice,
    create_usb_disk,
    parse_size_to_bytes,
)


def test_parse_size_to_bytes():
    assert parse_size_to_bytes(1024) == 1024
    assert parse_size_to_bytes("512") == 512 * 1024 * 1024  # default M
    assert parse_size_to_bytes("1024B") == 1024
    assert parse_size_to_bytes("64K") == 64 * 1024
    assert parse_size_to_bytes("64KB") == 64 * 1024
    assert parse_size_to_bytes("512M") == 512 * 1024 * 1024
    assert parse_size_to_bytes("512MB") == 512 * 1024 * 1024
    assert parse_size_to_bytes("1G") == 1024 * 1024 * 1024
    assert parse_size_to_bytes("2GB") == 2 * 1024 * 1024 * 1024

    with pytest.raises(ValueError, match="Invalid size specification"):
        parse_size_to_bytes("invalid_size")

    with pytest.raises(ValueError, match="Unknown size unit"):
        parse_size_to_bytes("100XYZ")


def test_create_usb_disk_raw(tmp_path):
    target = tmp_path / "test_raw.img"
    res = create_usb_disk(path=target, size="16M", filesystem="raw")
    assert res == target
    assert target.exists()
    assert target.stat().st_size == 16 * 1024 * 1024


def test_create_usb_disk_qcow2(tmp_path):
    target = tmp_path / "test_qcow.qcow2"
    res = create_usb_disk(path=target, size="32M", filesystem="qcow2")
    assert res == target
    assert target.exists()
    assert target.stat().st_size > 0


def test_create_usb_disk_fat32_with_files(tmp_path):
    target = tmp_path / "test_fat32.img"
    files = {
        "hello.txt": b"Hello World!",
        "config.ini": "[Main]\nkey=val",
    }
    src_dir = tmp_path / "src_files"
    src_dir.mkdir()
    (src_dir / "sample.log").write_text("log content")

    res = create_usb_disk(
        path=target,
        size="64M",
        filesystem="fat32",
        label="MYTEST",
        files=files,
        source_dir=src_dir,
    )
    assert res == target
    assert target.exists()
    assert target.stat().st_size == 64 * 1024 * 1024


def test_create_usb_disk_temp_path():
    res = create_usb_disk(path=None, size="16M", filesystem="raw")
    try:
        assert res.exists()
        assert res.stat().st_size == 16 * 1024 * 1024
    finally:
        if res.exists():
            res.unlink()


def test_usb_device_lifecycle():
    controller = MagicMock()
    dev = USBDevice(
        controller=controller,
        device_id="usb_dev_1234",
        drive_id="usb_drv_1234",
        disk_path=Path("/tmp/test.img"),
        drive_letter="E:",
        read_only=False,
        is_active=True,
    )

    assert "usb_dev_1234" in repr(dev)
    assert "E:" in repr(dev)

    # Test context manager
    with dev as d:
        assert d == dev
    controller.unmount.assert_called_with(dev)

    dev.is_active = True
    dev.detach()
    controller.unmount.assert_called_with(dev)


def test_usb_controller_mount_and_unmount(tmp_path):
    disk_img = tmp_path / "usb_mount.img"
    create_usb_disk(path=disk_img, size="16M", filesystem="raw")

    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.console.send_monitor_command.return_value = "OK"
    mock_cmd_result = MagicMock()
    mock_cmd_result.stdout = "E:\n"
    mock_machine.command.run.return_value = mock_cmd_result

    ctrl = USBController(mock_machine)
    usb_dev = ctrl.mount(disk=disk_img, to="E:")

    assert usb_dev.disk_path == disk_img
    assert usb_dev.drive_letter == "E:"
    assert usb_dev.is_active is True
    assert usb_dev in ctrl.devices

    # Verify monitor commands called
    calls = [c[0][0] for c in mock_machine.console.send_monitor_command.call_args_list]
    assert any("drive_add" in c for c in calls)
    assert any("device_add usb-storage" in c for c in calls)

    # Test unmount
    ctrl.unmount(usb_dev)
    assert usb_dev.is_active is False
    assert usb_dev not in ctrl.devices

    unmount_calls = [c[0][0] for c in mock_machine.console.send_monitor_command.call_args_list]
    assert any("device_del" in c for c in unmount_calls)
    assert any("drive_del" in c for c in unmount_calls)


def test_usb_controller_create_and_mount(tmp_path):
    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.console.send_monitor_command.return_value = "OK"
    mock_cmd_result = MagicMock()
    mock_cmd_result.stdout = "F:\n"
    mock_machine.command.run.return_value = mock_cmd_result

    ctrl = USBController(mock_machine)
    usb_dev = ctrl.create_and_mount(size="16M", filesystem="raw", to="F:")

    assert usb_dev.is_active is True
    assert usb_dev.drive_letter == "F:"
    assert usb_dev.disk_path.exists()

    # Unmount cleans up temporary disk
    tmp_disk = usb_dev.disk_path
    ctrl.unmount(usb_dev)
    assert not tmp_disk.exists()


def test_usb_controller_unmount_all():
    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.console.send_monitor_command.return_value = "OK"
    mock_machine.command.run.return_value.stdout = "G:\n"

    ctrl = USBController(mock_machine)
    _d1 = ctrl.mount(size="8M", filesystem="raw")
    _d2 = ctrl.mount(size="8M", filesystem="raw")

    assert len(ctrl.devices) == 2
    ctrl.unmount_all()
    assert len(ctrl.devices) == 0


def test_usb_controller_not_running_error():
    mock_machine = MagicMock()
    mock_machine.is_running = False

    ctrl = USBController(mock_machine)
    with pytest.raises(RuntimeError, match="Machine must be running"):
        ctrl.mount()
