"""End-to-end (E2E) integration tests for windows workflow using QGA."""

import os
from unittest.mock import MagicMock, patch

import pytest

import windows
from windows.image import Image, create_image_from_iso
from windows.machine import create_machine_from_image
from windows.qemu import create_qcow2_disk


def test_e2e_full_workflow_mocked(tmp_path):
    """End-to-End integration test validating full machine workflow with QGA layer."""
    disk_path = tmp_path / "e2e_installed.qcow2"
    create_qcow2_disk(disk_path, size="10M")
    image = Image(disk_path)

    # 1. Instantiate VM machine
    machine = create_machine_from_image(image, ram_mb=512, cpus=1)

    mock_qga = MagicMock()
    mock_qga.exec.return_value = ("Microsoft Windows [Version 10.0.19045]\n", "", 0)
    mock_qga.read_file.return_value = b"MZ\x90\x00\x03\x00\x00\x00NTDLL_MOCK_DATA_STREAM"

    with (
        patch.object(machine._process_manager, "start") as mock_start,
        patch.object(machine._process_manager, "stop") as mock_stop,
        patch.object(machine.command, "qga", mock_qga),
    ):
        # 2. Power ON VM
        machine.power.on()
        mock_start.assert_called_once()

        # 3. Execute guest command
        res = machine.command.run("ver")
        assert "Windows" in res.stdout

        # 4. Download file from guest: C:\windows\system32\ntdll.dll
        file = machine.file.download(r"C:\windows\system32\ntdll.dll")
        assert isinstance(file, windows.RemoteFile)
        save_dest = tmp_path / "ntdll.dll"
        file.save(save_dest)

        assert save_dest.exists()
        assert b"NTDLL_MOCK_DATA_STREAM" in save_dest.read_bytes()

        # 5. Upload file to guest
        upload_src = tmp_path / "local_test.txt"
        upload_src.write_text("Local text content")
        machine.file.upload(upload_src, r"C:\Users\Public\uploaded.txt")
        mock_qga.write_file.assert_called_once_with(r"C:\Users\Public\uploaded.txt", b"Local text content")

        # 6. Power OFF VM
        machine.power.off()
        mock_stop.assert_called_once()


@pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="Full live ISO QEMU install test requires RUN_E2E=1")
def test_e2e_live_iso_windows_install_and_command():
    """Full live QEMU installation and guest execution E2E test."""
    iso = windows.get_iso(windows.WindowsVersion.WIN10_22H2)
    image = create_image_from_iso(iso, output_disk="live_e2e.qcow2", timeout_minutes=30)
    machine = create_machine_from_image(image)

    try:
        machine.power.on()
        machine.command.wait_until_ready(timeout=180)
        res = machine.command.run("Get-CimInstance Win32_OperatingSystem | Select-Object Caption")
        assert "Windows" in res.stdout
    finally:
        machine.power.off()
