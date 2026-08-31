"""Unit tests for high-level Windows abstractions (processes, registry, services) and random image naming."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from windows.executor import CommandController, CommandResult
from windows.image import Image, create_image_from_iso
from windows.machine import Machine
from windows.processes import ProcessController, ProcessInfo
from windows.registry import RegistryController
from windows.services import ServiceController, ServiceInfo


def test_process_controller_list_and_get():
    mock_cmd = MagicMock(spec=CommandController)

    def mock_run(cmd, auto_retry=False):
        if "missing" in cmd:
            return CommandResult(stdout="null", stderr="", returncode=0)
        return CommandResult(
            stdout='[{"Id": 1234, "ProcessName": "notepad", "CPU": 0.5, "WS_MB": 12.4, "Path": "C:\\\\Windows\\\\notepad.exe"}]',
            stderr="",
            returncode=0,
        )

    mock_cmd.run.side_effect = mock_run

    pc = ProcessController(mock_cmd)
    procs = pc.list()

    assert len(procs) == 1
    assert isinstance(procs[0], ProcessInfo)
    assert procs[0].pid == 1234
    assert procs[0].name == "notepad"
    assert procs[0].working_set_mb == 12.4

    proc = pc.get(1234)
    assert proc is not None
    assert proc.name == "notepad"

    proc_name = pc.get("notepad")
    assert proc_name is not None
    assert proc_name.pid == 1234

    proc_missing = pc.get("missing.exe")
    assert proc_missing is None


def test_process_controller_spawn():
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="5678\n", stderr="", returncode=0)

    pc = ProcessController(mock_cmd)
    pid = pc.spawn("calc.exe")
    assert pid == 5678
    mock_cmd.run.assert_called_once()


def test_process_controller_kill():
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(stdout="", stderr="", returncode=0)

    pc = ProcessController(mock_cmd)
    assert pc.kill(1234) is True
    mock_cmd.run.assert_called_with("Stop-Process -Id 1234 -Force -ErrorAction Stop", auto_retry=False)

    assert pc.kill("notepad", force=False) is True
    mock_cmd.run.assert_called_with("Stop-Process -Name 'notepad'  -ErrorAction Stop", auto_retry=False)


def test_registry_controller_get_and_set():
    mock_cmd = MagicMock(spec=CommandController)

    def mock_run(cmd, powershell=True, auto_retry=False):
        if "reg query" in cmd and "/v" in cmd:
            output = "HKEY_LOCAL_MACHINE\\Software\\Test\n    TestProp    REG_SZ    MyValueData\n"
            return CommandResult(stdout=output, stderr="", returncode=0)
        elif "reg query" in cmd:
            return CommandResult(stdout="HKEY_LOCAL_MACHINE\\Software\\Test\n", stderr="", returncode=0)
        return CommandResult(stdout="", stderr="", returncode=0)

    mock_cmd.run.side_effect = mock_run
    rc = RegistryController(mock_cmd)

    assert rc.key_exists(r"HKLM:\Software\Test") is True
    val = rc.get_value(r"HKLM:\Software\Test", "TestProp")
    assert val == "MyValueData"

    rc.set_value(r"HKLM:\Software\Test", "NewProp", "NewVal")
    assert rc.delete_value(r"HKLM:\Software\Test", "OldProp") is True
    assert rc.delete_key(r"HKLM:\Software\Test") is True


def test_service_controller_list_and_manage():
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.run.return_value = CommandResult(
        stdout='[{"Name": "wuauserv", "DisplayName": "Windows Update", "Status": "Running", "StartType": "Automatic"}]',
        stderr="",
        returncode=0,
    )

    sc = ServiceController(mock_cmd)
    services = sc.list()

    assert len(services) == 1
    assert isinstance(services[0], ServiceInfo)
    assert services[0].name == "wuauserv"
    assert services[0].status == "Running"

    svc = sc.get("wuauserv")
    assert svc is not None
    assert svc.display_name == "Windows Update"

    assert sc.start("wuauserv") is True
    assert sc.stop("wuauserv") is True
    assert sc.restart("wuauserv") is True
    assert sc.set_startup_type("wuauserv", "Disabled") is True


def test_machine_has_abstractions():
    mock_image = MagicMock(spec=Image)
    mock_image.disk_path = MagicMock()
    mock_image.disk_path.stem = "test_disk"
    mock_image.disk_path.parent = MagicMock()

    m = Machine(mock_image)
    assert hasattr(m, "processes")
    assert hasattr(m, "registry")
    assert hasattr(m, "services")
    assert isinstance(m.processes, ProcessController)
    assert isinstance(m.registry, RegistryController)
    assert isinstance(m.services, ServiceController)


def test_create_image_random_output_disk(tmp_path):
    iso_file = tmp_path / "test.iso"
    iso_file.write_bytes(b"MOCK_ISO")

    def fake_overlay(target, backing):
        Path(target).write_bytes(b"MOCK_OVERLAY")

    with (
        patch("windows.image._is_valid_installed_image", return_value=True),
        patch("windows.image.create_qcow2_overlay", side_effect=fake_overlay) as mock_overlay,
    ):
        img = create_image_from_iso(iso_path=iso_file, output_disk=None, use_cache=True)
        assert isinstance(img, Image)
        assert "windows_overlay_" in img.disk_path.name
        mock_overlay.assert_called_once()
        if img.disk_path.exists():
            img.disk_path.unlink()
