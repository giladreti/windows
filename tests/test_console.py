"""Unit tests for ConsoleController (machine.console.open)."""

from unittest.mock import MagicMock, patch

from windows.console import ConsoleController, ConsoleInfo
from windows.image import Image
from windows.machine import create_machine_from_image


def test_console_info():
    console = ConsoleController(host="127.0.0.1", port=5900, display_index=0)
    info = console.info

    assert isinstance(info, ConsoleInfo)
    assert info.host == "127.0.0.1"
    assert info.port == 5900
    assert info.vnc_url == "vnc://127.0.0.1:5900"
    assert info.to_dict()["display"] == ":0"


def test_console_open_launch_process():
    console = ConsoleController(host="127.0.0.1", port=5900)

    with patch("shutil.which") as mock_which, patch("subprocess.Popen") as mock_popen:
        mock_which.return_value = "/usr/bin/vncviewer"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        info = console.open()

        assert info.vnc_url == "vnc://127.0.0.1:5900"
        assert console.is_open
        mock_popen.assert_called_once()


def test_console_close_process():
    console = ConsoleController(host="127.0.0.1", port=5900)

    with patch("shutil.which") as mock_which, patch("subprocess.Popen") as mock_popen:
        mock_which.return_value = "/usr/bin/vncviewer"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        console.open()
        assert console.is_open

        console.close()
        mock_proc.terminate.assert_called_once()
        assert not console.is_open


def test_machine_console_integration(tmp_path):
    disk = tmp_path / "test.qcow2"
    disk.write_bytes(b"dummy qcow2")
    img = Image(disk_path=disk)

    machine = create_machine_from_image(img, vnc_display=1)

    assert machine.console.port == 5901
    assert machine.console.info.vnc_url == "vnc://127.0.0.1:5901"

    with patch("shutil.which") as mock_which, patch("subprocess.Popen") as mock_popen:
        mock_which.return_value = "/usr/bin/vncviewer"
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        info = machine.console.open()
        assert info.port == 5901
        assert machine.console.is_open

        machine.console.close()
        assert not machine.console.is_open
