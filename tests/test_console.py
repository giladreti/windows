"""Unit tests for ConsoleController (machine.console.open)."""

from unittest.mock import MagicMock, patch

from windows.console import ConsoleController, ConsoleInfo
from windows.image import Image
from windows.machine import Machine


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

    machine = Machine(img, vnc_display=1)

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


def test_console_record_context_manager(tmp_path):
    monitor_sock = tmp_path / "monitor.sock"
    monitor_sock.write_bytes(b"")

    console = ConsoleController(monitor_socket_path=monitor_sock)
    output_video = tmp_path / "session.mp4"

    def fake_dump(ppm_path):
        # Create a small valid PPM P6 image
        ppm_path.write_bytes(b"P6\n2 2\n255\n" + b"\xff\x00\x00" * 4)

    with (
        patch.object(console, "_dump_screen_ppm", side_effect=fake_dump),
        patch("subprocess.run") as mock_run,
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
    ):
        mock_run.return_value = MagicMock(returncode=0)

        with console.record(output_video, fps=5.0) as rec:
            assert rec.is_recording
            assert rec.output_path == output_video.resolve()
            assert rec.fps == 5.0
            import time

            time.sleep(0.15)
            assert rec.frame_count >= 1
            assert rec.duration > 0.0

        assert not rec.is_recording
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/ffmpeg"
        assert "-framerate" in cmd
        assert str(output_video) in cmd


def test_console_record_standalone_and_machine_record(tmp_path):
    disk = tmp_path / "vm.qcow2"
    disk.write_bytes(b"QCOW2")
    img = Image(disk_path=disk)
    machine = Machine(img)

    monitor_sock = machine.monitor_socket_path
    monitor_sock.parent.mkdir(parents=True, exist_ok=True)
    monitor_sock.write_bytes(b"")

    output_gif = tmp_path / "anim.gif"

    def fake_dump(ppm_path):
        ppm_path.write_bytes(b"P6\n2 2\n255\n" + b"\x00\xff\x00" * 4)

    with (
        patch.object(machine.console, "_dump_screen_ppm", side_effect=fake_dump),
        patch("shutil.which", return_value=None),  # Test PIL fallback for GIF
    ):
        rec = machine.record(output_gif, fps=10.0)
        rec.start()
        import time

        time.sleep(0.1)
        res_path = rec.stop()

        assert res_path == output_gif.resolve()
        assert output_gif.exists()
        assert output_gif.stat().st_size > 0
