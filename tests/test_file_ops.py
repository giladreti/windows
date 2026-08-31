"""Unit tests for FileController file and directory upload/download operations with exist policies."""

from unittest.mock import MagicMock

import pytest

from windows.executor import CommandController, CommandResult
from windows.file import FileController


def test_file_controller_download_file_abort_exists(tmp_path):
    local_file = tmp_path / "existing.txt"
    local_file.write_text("existing content")

    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()

    def mock_run_cmd(cmd, auto_retry=True):
        if "PSIsContainer" in cmd:
            return CommandResult(stdout="False\n", stderr="", returncode=0)
        return CommandResult(stdout="True\n", stderr="", returncode=0)

    mock_cmd.run.side_effect = mock_run_cmd
    mock_cmd.qga.read_file.return_value = b"remote payload"

    fc = FileController(mock_cmd)
    with pytest.raises(FileExistsError, match="Target local file already exists"):
        fc.download(r"C:\remote.txt", local_path=local_file, exist_policy="abort")


def test_file_controller_upload_file_abort_exists(tmp_path):
    local_file = tmp_path / "payload.txt"
    local_file.write_text("payload content")

    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()
    mock_cmd.run.return_value = CommandResult(stdout="True\n", stderr="", returncode=0)

    fc = FileController(mock_cmd)
    with pytest.raises(FileExistsError, match="Target remote path already exists"):
        fc.upload(local_file, r"C:\destination.txt", exist_policy="abort")


def test_file_controller_upload_dir_merge(tmp_path):
    local_dir = tmp_path / "my_folder"
    local_dir.mkdir()
    (local_dir / "f1.txt").write_text("file 1 content")

    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()
    # Test-Path returns True, is_dir returns True
    mock_cmd.run.return_value = CommandResult(stdout="True\n", stderr="", returncode=0)

    fc = FileController(mock_cmd)
    fc.upload(local_dir, r"C:\remote_dir", exist_policy="merge", show_progress=False)

    mock_cmd.qga.write_file.assert_called_once()
    call_args = mock_cmd.qga.write_file.call_args[0]
    assert r"C:\remote_dir\f1.txt" in call_args[0]
    assert call_args[1] == b"file 1 content"


def test_remote_path_write_read_text():
    mock_cmd = MagicMock(spec=CommandController)
    mock_cmd.qga = MagicMock()
    mock_cmd.run.return_value = CommandResult(stdout="False\n", stderr="", returncode=0)
    mock_cmd.qga.read_file.return_value = b"hello from qga"

    fc = FileController(mock_cmd)
    p = fc.path(r"C:\test.txt")
    p.write_text("hello from qga")
    mock_cmd.qga.write_file.assert_called_once_with(r"C:\test.txt", b"hello from qga")

    assert p.read_text() == "hello from qga"
