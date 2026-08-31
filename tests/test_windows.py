"""Core unit tests for windows functionality."""

from unittest.mock import MagicMock

import windows
from windows.executor import CommandController, CommandResult
from windows.file import FileController, RemoteFile
from windows.image import Image
from windows.iso import WINDOWS_VERSION_URLS, resolve_iso
from windows.machine import Machine, create_machine_from_image
from windows.qemu import build_install_qemu_cmd
from windows.unattend import generate_unattend_xml, save_unattend_xml


def test_iso_version_mapping():
    assert windows.WindowsVersion.WIN11_23H2 in WINDOWS_VERSION_URLS
    assert windows.WindowsVersion.WIN11_25H2 in WINDOWS_VERSION_URLS
    assert windows.WindowsVersion.WIN10_22H2 in WINDOWS_VERSION_URLS


def test_iso_enum_resolution():
    assert windows.WindowsVersion.WIN10_22H2.value == "win10_22h2"
    assert windows.WindowsVersion.WIN11_22H2.value == "win11_22h2"
    assert windows.WindowsVersion.WIN11_23H2.value == "win11_23h2"
    assert windows.WindowsVersion.WIN11_24H2.value == "win11_24h2"
    assert windows.WindowsVersion.WIN11_25H2.value == "win11_25h2"
    assert windows.WindowsVersion.SERVER_2022.value == "server2022"
    assert windows.WindowsVersion.SERVER_2025.value == "server2025"


def test_iso_local_file_resolution(tmp_path):
    fake_iso = tmp_path / "custom.iso"
    fake_iso.write_bytes(b"dummy iso header")

    resolved = resolve_iso(str(fake_iso))
    assert resolved == fake_iso.resolve()


def test_unattend_xml_generation():
    xml = generate_unattend_xml(language="en-US")
    assert "<unattend" in xml.lower()
    assert "<uilanguage>en-us</uilanguage>" in xml.lower()


def test_unattend_xml_saving(tmp_path):
    save_unattend_xml(tmp_path, language="en-US")
    target_file = tmp_path / "autounattend.xml"
    assert target_file.exists()
    content = target_file.read_text(encoding="utf-8")
    assert "<unattend" in content.lower()


def test_qemu_cmd_building(tmp_path):
    disk = tmp_path / "test.qcow2"
    iso = tmp_path / "win.iso"
    unattend = tmp_path / "unattend"

    cmd = build_install_qemu_cmd(disk, iso, unattend)
    assert "qemu-system-x86_64" in cmd[0]
    assert str(disk) in " ".join(cmd)
    assert str(iso) in " ".join(cmd)
    assert "-enable-kvm" in cmd or "-cpu" in cmd


def test_command_result_behavior():
    res = CommandResult(stdout="hello\n", stderr="", returncode=0)
    assert res.stdout == "hello\n"
    assert res.returncode == 0
    assert bool(res) is True

    res_fail = CommandResult(stdout="", stderr="error\n", returncode=1)
    assert res_fail.returncode == 1


def test_remote_file_save(tmp_path):
    rf = RemoteFile(remote_path="C:\\test.txt", content=b"hello remote")
    local_target = tmp_path / "downloaded.txt"
    rf.save(local_target)
    assert local_target.exists()
    assert local_target.read_bytes() == b"hello remote"


def test_file_controller_download():
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()

    def mock_run_cmd(cmd, auto_retry=True):
        if "PSIsContainer" in cmd:
            return CommandResult(stdout="False\n", stderr="", returncode=0)
        return CommandResult(stdout="True\n", stderr="", returncode=0)

    mock_cmd.run.side_effect = mock_run_cmd
    mock_cmd.qga.read_file.return_value = b"hello world"

    fc = FileController(mock_cmd)
    rf = fc.download(r"C:\remote.txt")

    assert isinstance(rf, RemoteFile)
    assert rf.remote_path == r"C:\remote.txt"
    assert rf.read_bytes() == b"hello world"
    mock_cmd.qga.read_file.assert_called_once_with(r"C:\remote.txt")


def test_file_controller_upload(tmp_path):
    local_file = tmp_path / "payload.txt"
    local_file.write_bytes(b"upload payload content")

    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()
    mock_cmd.run.return_value = CommandResult(stdout="False\n", stderr="", returncode=0)

    fc = FileController(mock_cmd)
    fc.upload(local_file, r"C:\destination.txt")

    mock_cmd.qga.write_file.assert_called_once_with(r"C:\destination.txt", b"upload payload content")


def test_machine_creation(tmp_path):
    fake_disk = tmp_path / "installed_windows.qcow2"
    fake_disk.write_bytes(b"dummy qcow2 content")

    img = Image(fake_disk)
    mach = create_machine_from_image(img)
    assert isinstance(mach, Machine)
    assert mach.image.disk_path == fake_disk.resolve()
