"""Advanced unit tests for windows error handling, mock QEMU execution, and pipeline edge cases."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from windows.executor import CommandController, CommandResult
from windows.file import FileController
from windows.image import Image, create_image_from_iso
from windows.iso import create_dummy_iso, resolve_iso
from windows.machine import create_machine_from_image
from windows.qemu import QEMUProcessManager


def test_invalid_iso_version_raises_error():
    with pytest.raises(ValueError, match="is not a recognized Windows version"):
        resolve_iso("invalid_win_99")


def test_iso_503_error_fallback_handling(tmp_path):
    with patch("requests.get") as mock_get:
        mock_resp_503 = MagicMock()
        mock_resp_503.raise_for_status.side_effect = requests.exceptions.HTTPError(
            "503 Server Error: Service Unavailable"
        )

        mock_resp_200 = MagicMock()
        mock_resp_200.raise_for_status.return_value = None
        mock_resp_200.headers = {"content-length": "100"}
        mock_resp_200.iter_content.return_value = [b"ISO_DATA_CHUNK"]

        mock_get.side_effect = [mock_resp_503, mock_resp_200]

        target_file = tmp_path / "win_win11_25h2.iso"
        resolve_iso("25H2", cache_dir=tmp_path, show_progress=False)
        assert target_file.exists()
        assert target_file.read_bytes() == b"ISO_DATA_CHUNK"


def test_iso_all_urls_failing_includes_summary(tmp_path):
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
        mock_get.return_value = mock_resp

        with pytest.raises(ValueError, match="Failed to download Windows ISO"):
            resolve_iso("25H2", cache_dir=tmp_path, show_progress=False)


def test_create_dummy_iso_helper(tmp_path):
    dest = tmp_path / "synthetic.iso"
    result = create_dummy_iso(dest)
    assert result.exists()
    assert len(result.read_bytes()) > 30000


def test_iso_direct_url_download(tmp_path):
    url = "https://example.com/test_windows.iso"
    with patch("windows.iso.download_file") as mock_dl:

        def fake_dl(url, dest, show_progress=True):
            create_dummy_iso(dest)

        mock_dl.side_effect = fake_dl

        res = resolve_iso(url, cache_dir=tmp_path)
        assert res.exists()
        assert "test_windows" in res.name


def test_iso_fallback_on_404(tmp_path):
    with patch("requests.get") as mock_get:
        resp_404 = MagicMock()
        resp_404.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Client Error")

        resp_200 = MagicMock()
        resp_200.raise_for_status.return_value = None
        resp_200.headers = {"content-length": "50"}
        resp_200.iter_content.return_value = [b"VALID_BYTES"]

        mock_get.side_effect = [resp_404, resp_200]
        res = resolve_iso("23H2", cache_dir=tmp_path, show_progress=False)
        assert res.exists()
        assert res.read_bytes() == b"VALID_BYTES"


def test_iso_env_var_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDOWS_25H2_ISO_URL", "https://custom-mirror.org/win25h2.iso")
    with patch("windows.iso.download_file") as mock_dl:

        def fake_dl(url, dest, show_progress=True):
            create_dummy_iso(dest)

        mock_dl.side_effect = fake_dl

        resolve_iso("25H2", cache_dir=tmp_path)
        assert mock_dl.call_args[0][0] == "https://custom-mirror.org/win25h2.iso"


def test_download_remote_file_error_handling():
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.ssh = MagicMock()
    mock_sftp = MagicMock()
    mock_sftp.stat.side_effect = OSError("Remote path not found")
    mock_cmd.ssh.open_sftp.return_value = (MagicMock(), mock_sftp)

    file_ctrl = FileController(mock_cmd)
    with pytest.raises(FileNotFoundError, match="Remote path not found"):
        file_ctrl.download("C:\\nonexistent.txt")


def test_upload_nonexistent_local_file_raises_error(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    file_ctrl = FileController(mock_cmd)
    non_existent = tmp_path / "does_not_exist.txt"
    with pytest.raises(FileNotFoundError, match="Local path does not exist"):
        file_ctrl.upload(non_existent, "C:\\remote.txt")


def test_upload_multi_chunk_handling(tmp_path):
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()
    mock_cmd.run.return_value = CommandResult(stdout="False\n", stderr="", returncode=0)

    file_ctrl = FileController(mock_cmd)
    local_file = tmp_path / "large_file.txt"
    content = "A" * 100
    local_file.write_text(content)

    file_ctrl.upload(local_file, r"C:\remote_large.txt", chunk_size=10)
    mock_cmd.qga.write_file.assert_called_once_with(r"C:\remote_large.txt", b"A" * 100)


def test_qemu_process_manager_mock():
    mock_cmd = ["echo", "test"]
    manager = QEMUProcessManager(mock_cmd)
    assert not manager.is_running()

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        manager.start()
        assert manager.is_running()

        mock_proc.poll.return_value = None
        manager.stop()
        mock_proc.terminate.assert_called_once()


def test_power_controller_actions(tmp_path):
    disk = tmp_path / "test.qcow2"
    disk.write_bytes(b"qcow2")
    img = Image(disk_path=disk)
    machine = create_machine_from_image(img)

    with (
        patch.object(machine._process_manager, "start") as mock_start,
        patch.object(machine._process_manager, "stop") as mock_stop,
    ):
        machine.power.on()
        mock_start.assert_called_once()

        machine.power.off()
        mock_stop.assert_called_once()

        machine.power.restart()
        assert mock_start.call_count == 2
        assert mock_stop.call_count == 2


def test_create_image_from_iso_pipeline_mock(tmp_path):
    iso_file = tmp_path / "test.iso"
    iso_file.write_bytes(b"ISO content")
    disk_output = tmp_path / "output.qcow2"

    img_cache_dir = tmp_path / "img_cache"

    with (
        patch("windows.image.get_image_cache_dir", return_value=img_cache_dir),
        patch("windows.image.create_qcow2_disk") as mock_create_disk,
        patch("windows.image.QEMUProcessManager") as mock_qemu_pm,
        patch("windows.image.ConsoleController") as mock_console_cls,
    ):

        def fake_create_disk(p, size):
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MOCK_QCOW2_DATA")
            return p

        mock_create_disk.side_effect = fake_create_disk
        mock_pm_instance = MagicMock()
        mock_pm_instance.is_running.side_effect = [False]
        mock_qemu_pm.return_value = mock_pm_instance

        mock_console_inst = MagicMock()
        mock_console_cls.return_value = mock_console_inst

        img = create_image_from_iso(
            iso_path=str(iso_file),
            output_disk=disk_output,
            timeout_minutes=1,
            use_cache=False,
            interactive=True,
        )

        assert isinstance(img, Image)
        assert img.disk_path == disk_output.resolve()
        mock_console_inst.open.assert_called_once()
        mock_console_inst.close.assert_called_once()
