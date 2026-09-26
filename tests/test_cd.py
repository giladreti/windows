"""Unit tests for CD-ROM drive emulation, ISO creation, and CDController."""

import io
from pathlib import Path
from unittest.mock import MagicMock

import pycdlib
import pytest

from windows.cd import (
    CDController,
    CDDevice,
    create_cdrom_iso,
)


def test_create_cdrom_iso_basic(tmp_path):
    iso_file = tmp_path / "test.iso"
    files = {
        "readme.txt": b"Hello CD world!",
        "docs/info.txt": "Documentation file text",
    }
    result = create_cdrom_iso(path=iso_file, label="TEST_DISC", files=files)
    assert result == iso_file
    assert iso_file.exists()
    assert iso_file.stat().st_size > 0

    # Read back ISO with pycdlib and verify files
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_file))
    try:
        # Check root file
        out_fp = io.BytesIO()
        iso.get_file_from_iso_fp(out_fp, joliet_path="/readme.txt")
        assert out_fp.getvalue() == b"Hello CD world!"

        # Check nested file
        out_fp_sub = io.BytesIO()
        iso.get_file_from_iso_fp(out_fp_sub, joliet_path="/docs/info.txt")
        assert out_fp_sub.getvalue() == b"Documentation file text"
    finally:
        iso.close()


def test_create_cdrom_iso_from_source_dir(tmp_path):
    src_dir = tmp_path / "iso_source"
    src_dir.mkdir()
    (src_dir / "file1.bin").write_bytes(b"DATA1")
    sub = src_dir / "sub"
    sub.mkdir()
    (sub / "file2.txt").write_text("DATA2")

    iso_file = tmp_path / "dir_test.iso"
    create_cdrom_iso(path=iso_file, label="FROM_DIR", source_dir=src_dir)

    assert iso_file.exists()
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_file))
    try:
        fp1 = io.BytesIO()
        iso.get_file_from_iso_fp(fp1, joliet_path="/file1.bin")
        assert fp1.getvalue() == b"DATA1"

        fp2 = io.BytesIO()
        iso.get_file_from_iso_fp(fp2, joliet_path="/sub/file2.txt")
        assert fp2.getvalue() == b"DATA2"
    finally:
        iso.close()


def test_cd_device_lifecycle():
    mock_ctrl = MagicMock()
    dev = CDDevice(
        controller=mock_ctrl,
        device_id="onboard_cd0",
        drive_id="cd0",
        iso_path=Path("/tmp/fake.iso"),
        drive_letter="D:",
        backend="ide",
    )
    assert dev.is_active is True
    assert dev.drive_letter == "D:"
    assert "onboard_cd0" in repr(dev)

    # Context manager unmount/eject
    with dev as d:
        assert d == dev
    mock_ctrl.eject.assert_called_with(dev)


def test_cd_controller_insert_onboard_ide(tmp_path):
    mock_machine = MagicMock()
    mock_machine.is_running = True
    # Simulate block info showing onboard cd0
    mock_machine.console.send_monitor_command.side_effect = lambda cmd: (
        "cd0: [not inserted]\n" if "info block" in cmd else ""
    )
    mock_machine.command.run.return_value.stdout = "D:\n"

    ctrl = CDController(mock_machine)
    iso_path = tmp_path / "sample.iso"
    create_cdrom_iso(iso_path, label="SAMPLE", files={"test.txt": b"123"})

    with ctrl.insert(iso_path, to="D:") as cd_dev:
        assert cd_dev.drive_letter == "D:"
        assert cd_dev.backend == "ide"
        assert cd_dev.is_active is True
        assert len(ctrl.devices) == 1

        # Check monitor change command was sent
        calls = [str(c[0][0]) for c in mock_machine.console.send_monitor_command.call_args_list]
        assert any("change cd0" in c for c in calls)

    # After context exit, eject -f cd0 should have been called
    calls = [str(c[0][0]) for c in mock_machine.console.send_monitor_command.call_args_list]
    assert any("eject -f cd0" in c for c in calls)
    assert len(ctrl.devices) == 0


def test_cd_controller_insert_hotplug_usb(tmp_path):
    mock_machine = MagicMock()
    mock_machine.is_running = True
    # Simulate no onboard cd0 in block info
    mock_machine.console.send_monitor_command.side_effect = lambda cmd: (
        "virtio0: /disk.qcow2\n" if "info block" in cmd else ""
    )
    mock_machine.command.run.return_value.stdout = "E:\n"

    ctrl = CDController(mock_machine)
    iso_path = tmp_path / "hotplug.iso"
    create_cdrom_iso(iso_path, label="HOTCD", files={"payload.bin": b"abc"})

    cd_dev = ctrl.insert(iso_path, to="E:")
    assert cd_dev.drive_letter == "E:"
    assert cd_dev.backend == "usb"
    assert cd_dev.is_active is True

    # Check drive_add and device_add usb-storage were called
    calls = [str(c[0][0]) for c in mock_machine.console.send_monitor_command.call_args_list]
    assert any("drive_add" in c and "media=cdrom" in c for c in calls)
    assert any("device_add usb-storage" in c and "removable=true" in c for c in calls)

    ctrl.eject(cd_dev)
    assert cd_dev.is_active is False
    assert len(ctrl.devices) == 0


def test_cd_controller_not_running_error():
    mock_machine = MagicMock()
    mock_machine.is_running = False

    ctrl = CDController(mock_machine)
    with pytest.raises(RuntimeError, match="Machine must be running"):
        ctrl.insert()


def test_cd_controller_eject_all(tmp_path):
    mock_machine = MagicMock()
    mock_machine.is_running = True
    mock_machine.console.send_monitor_command.return_value = ""
    mock_machine.command.run.return_value.stdout = "D:\n"

    ctrl = CDController(mock_machine)
    _d1 = ctrl.insert(prefer_hotplug=True)
    _d2 = ctrl.insert(prefer_hotplug=True)
    assert len(ctrl.devices) == 2

    ctrl.eject_all()
    assert len(ctrl.devices) == 0
