"""Unit tests verifying Windows host compatibility, TCP socket endpoints, and WHPX/TCG execution."""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from windows.network import VirtualSwitch, is_root
from windows.qemu import (
    QEMUProcessManager,
    build_install_qemu_cmd,
    build_run_qemu_cmd,
    find_qemu_binary,
    find_qemu_img_binary,
    find_qemu_keymap,
    format_chardev_args,
    format_monitor_args,
    format_qmp_args,
    get_host_cpu_vendor,
    is_tcp_endpoint,
    parse_tcp_endpoint,
)


def test_tcp_endpoint_helpers():
    assert is_tcp_endpoint(("127.0.0.1", 5985)) is True
    assert is_tcp_endpoint(5985) is True
    assert is_tcp_endpoint("tcp:127.0.0.1:5985") is True
    assert is_tcp_endpoint("127.0.0.1:5985") is True
    assert is_tcp_endpoint(Path("/tmp/test.sock")) is False
    assert is_tcp_endpoint(r"C:\test\sock.sock") is False

    assert parse_tcp_endpoint(("127.0.0.1", 5985)) == ("127.0.0.1", 5985)
    assert parse_tcp_endpoint(5985) == ("127.0.0.1", 5985)
    assert parse_tcp_endpoint("tcp:127.0.0.1:5985") == ("127.0.0.1", 5985)


def test_format_chardev_and_socket_args():
    # TCP endpoint formatting
    args = format_chardev_args("qga0", ("127.0.0.1", 5950))
    assert args == ["-chardev", "socket,id=qga0,host=127.0.0.1,port=5950,server=on,wait=off"]

    args = format_monitor_args(("127.0.0.1", 5960))
    assert args == ["-monitor", "tcp:127.0.0.1:5960,server,nowait"]

    args = format_qmp_args(("127.0.0.1", 5970))
    assert args == ["-qmp", "tcp:127.0.0.1:5970,server,nowait"]

    # Unix domain socket formatting
    sock_path = Path("/tmp/qga.sock")
    args = format_chardev_args("qga0", sock_path)
    assert args == ["-chardev", f"socket,id=qga0,path={sock_path},server=on,wait=off"]

    args = format_monitor_args(sock_path)
    assert args == ["-monitor", f"unix:{sock_path},server,nowait"]


def test_find_qemu_binaries_windows_candidates(tmp_path):
    fake_qemu = tmp_path / "qemu-system-x86_64.exe"
    fake_qemu.touch()
    fake_img = tmp_path / "qemu-img.exe"
    fake_img.touch()

    with patch("sys.platform", "win32"):
        with patch("shutil.which", return_value=None):
            with patch.dict(os.environ, {"ProgramFiles": str(tmp_path)}):
                # When located in ProgramFiles/qemu/...
                qemu_dir = tmp_path / "qemu"
                qemu_dir.mkdir()
                real_fake_qemu = qemu_dir / "qemu-system-x86_64.exe"
                real_fake_qemu.touch()
                real_fake_img = qemu_dir / "qemu-img.exe"
                real_fake_img.touch()

                assert find_qemu_binary() == str(real_fake_qemu)
                assert find_qemu_img_binary() == str(real_fake_img)


def test_build_run_qemu_cmd_windows_whpx():
    with (
        patch("sys.platform", "win32"),
        patch("windows.qemu.find_qemu_binary", return_value=r"C:\Program Files\qemu\qemu-system-x86_64.exe"),
        patch("windows.qemu.is_whpx_available", return_value=True),
    ):
        cmd = build_run_qemu_cmd(
            disk_path=Path("C:/disk.qcow2"),
            qga_endpoint=("127.0.0.1", 5950),
            monitor_endpoint=("127.0.0.1", 5960),
            qmp_endpoint=("127.0.0.1", 5970),
            enable_kvm=True,
            smb_guestfwd_port=4445,
        )

        assert "-accel" in cmd
        accel_idx = cmd.index("-accel")
        assert cmd[accel_idx + 1] == "whpx"
        assert "-cpu" in cmd
        cpu_idx = cmd.index("-cpu")
        assert cmd[cpu_idx + 1] == "max"

        # Check TCP endpoints
        cmd_str = " ".join(cmd)
        assert "host=127.0.0.1,port=5950" in cmd_str
        assert "tcp:127.0.0.1:5960" in cmd_str
        assert "tcp:127.0.0.1:5970" in cmd_str

        # Check SMB guestfwd
        assert "guestfwd=tcp:10.0.2.4:445-tcp:127.0.0.1:4445" in cmd_str


def test_build_run_qemu_cmd_windows_tcg_fallback():
    with (
        patch("sys.platform", "win32"),
        patch("windows.qemu.find_qemu_binary", return_value=r"C:\Program Files\qemu\qemu-system-x86_64.exe"),
        patch("windows.qemu.is_whpx_available", return_value=False),
        patch.dict(os.environ, {"PROCESSOR_IDENTIFIER": "GenuineIntel"}),
    ):
        cmd = build_run_qemu_cmd(
            disk_path=Path("C:/disk.qcow2"),
            qga_endpoint=("127.0.0.1", 5950),
            monitor_endpoint=("127.0.0.1", 5960),
            qmp_endpoint=("127.0.0.1", 5970),
            enable_kvm=True,
            smb_guestfwd_port=4445,
        )

        assert "-accel" in cmd
        accel_idx = cmd.index("-accel")
        assert cmd[accel_idx + 1] == "tcg,tb-size=1024"
        assert "-cpu" in cmd
        cpu_idx = cmd.index("-cpu")
        assert cmd[cpu_idx + 1] == "max,vendor=GenuineIntel"
        assert "-cpu host" not in " ".join(cmd)


def test_build_install_qemu_cmd_windows():
    with (
        patch("sys.platform", "win32"),
        patch("windows.qemu.find_qemu_binary", return_value=r"C:\Program Files\qemu\qemu-system-x86_64.exe"),
        patch("windows.qemu.is_whpx_available", return_value=True),
    ):
        cmd = build_install_qemu_cmd(
            disk_path=Path("C:/disk.qcow2"),
            iso_path=Path("C:/win.iso"),
            unattend_dir=Path("C:/unattend"),
            qga_endpoint=("127.0.0.1", 5951),
            monitor_endpoint=("127.0.0.1", 5961),
            enable_kvm=True,
        )
        assert "-accel" in cmd
        assert cmd[cmd.index("-accel") + 1] == "whpx"
        assert "-cpu" in cmd
        assert cmd[cmd.index("-cpu") + 1] == "max"
        cmd_str = " ".join(cmd)
        assert "host=127.0.0.1,port=5951" in cmd_str
        assert "tcp:127.0.0.1:5961" in cmd_str


def test_get_host_cpu_vendor_windows():
    with patch("sys.platform", "win32"):
        with patch.dict(os.environ, {"PROCESSOR_IDENTIFIER": "AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD"}):
            assert get_host_cpu_vendor() == "AuthenticAMD"

        with patch.dict(os.environ, {"PROCESSOR_IDENTIFIER": "Intel64 Family 6 Model 158 Stepping 10, GenuineIntel"}):
            assert get_host_cpu_vendor() == "GenuineIntel"


def test_process_manager_no_pass_fds_on_windows():
    with patch("sys.platform", "win32"):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            pm = QEMUProcessManager(["dummy_cmd"])
            pm.start(startup_check_delay=0.01)

            assert mock_popen.called
            call_kwargs = mock_popen.call_args[1]
            # pass_fds MUST NOT be passed on Windows platform
            assert "pass_fds" not in call_kwargs
            # creationflags MUST include CREATE_NEW_PROCESS_GROUP to protect from Ctrl+C on Windows
            assert call_kwargs.get("creationflags") == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            assert "start_new_session" not in call_kwargs


def test_is_root_and_virtual_switch_on_windows():
    with patch("sys.platform", "win32"):
        assert is_root() is False

        # VirtualSwitch in auto mode on Windows defaults to socket mode
        sw = VirtualSwitch("win_switch", mode="auto")
        assert sw.mode == "socket"
        sw.destroy()


def test_find_qemu_keymap_windows(tmp_path):
    fake_bin_dir = tmp_path / "qemu"
    fake_bin = fake_bin_dir / "qemu-system-x86_64.exe"
    fake_keymap = fake_bin_dir / "share" / "keymaps" / "en-us"
    fake_keymap.parent.mkdir(parents=True)
    fake_keymap.touch()

    with (
        patch("sys.platform", "win32"),
        patch("windows.qemu.find_qemu_binary", return_value=str(fake_bin)),
    ):
        found = find_qemu_keymap("en-us")
        assert found == str(fake_keymap)


def test_build_run_qemu_cmd_windows_appends_keymap():
    with (
        patch("sys.platform", "win32"),
        patch("windows.qemu.find_qemu_binary", return_value=r"C:\Program Files\qemu\qemu-system-x86_64.exe"),
        patch("windows.qemu.find_qemu_keymap", return_value=r"C:\Program Files\qemu\share\keymaps\en-us"),
    ):
        cmd = build_run_qemu_cmd(
            disk_path=Path("C:/disk.qcow2"),
            vnc_display=0,
        )
        assert "-vnc" in cmd
        assert cmd[cmd.index("-vnc") + 1] == "127.0.0.1:0"
        assert "-k" in cmd
        assert cmd[cmd.index("-k") + 1] == r"C:\Program Files\qemu\share\keymaps\en-us"
