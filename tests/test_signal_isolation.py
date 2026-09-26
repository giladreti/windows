"""Unit tests verifying that QEMU processes are isolated from KeyboardInterrupt (SIGINT)."""

import os
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from windows.qemu import QEMUProcessManager


def test_qemu_process_manager_starts_new_session_posix():
    """Verify that on non-Windows platforms, start_new_session=True is passed to Popen."""
    with patch("sys.platform", "linux"):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            pm = QEMUProcessManager(["qemu-system-x86_64", "--version"])
            pm.start(startup_check_delay=0.01)

            assert mock_popen.called
            kwargs = mock_popen.call_args[1]
            assert kwargs.get("start_new_session") is True
            assert "creationflags" not in kwargs


def test_qemu_process_manager_starts_new_process_group_windows():
    """Verify that on Windows, CREATE_NEW_PROCESS_GROUP is passed in creationflags."""
    with patch("sys.platform", "win32"):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_popen.return_value = mock_proc

            pm = QEMUProcessManager(["qemu-system-x86_64.exe", "--version"])
            pm.start(startup_check_delay=0.01)

            assert mock_popen.called
            kwargs = mock_popen.call_args[1]
            assert kwargs.get("creationflags") == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            assert "start_new_session" not in kwargs


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group tests only apply on Linux/macOS")
def test_live_qemu_process_manager_survives_process_group_sigint():
    """Verify that a real spawned QEMUProcessManager process is NOT killed when SIGINT is broadcast to parent process group."""
    # Spawn a sleeping python process using QEMUProcessManager
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    pm = QEMUProcessManager(cmd)
    pm.start(startup_check_delay=0.05)

    try:
        assert pm.is_running() is True
        assert pm.process is not None
        child_pid = pm.process.pid
        child_pgid = os.getpgid(child_pid)
        parent_pgid = os.getpgrp()

        # Child must be in a different process group than parent
        assert child_pgid != parent_pgid
        assert child_pgid == child_pid

        # Trap SIGINT in parent so test runner does not die
        received_sigint = False

        def sigint_handler(sig, frame):
            nonlocal received_sigint
            received_sigint = True

        old_handler = signal.signal(signal.SIGINT, sigint_handler)
        try:
            # Send SIGINT to only this process (not the whole process group,
            # which would disrupt the pytest runner).  The child is in a
            # separate process group and should not receive it.
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.1)

            assert received_sigint is True, "Parent should have received SIGINT"

            # Child process must STILL be running and alive (NOT terminated by SIGINT)
            assert pm.is_running() is True
            assert pm.process.poll() is None
        finally:
            signal.signal(signal.SIGINT, old_handler)

    finally:
        pm.stop(timeout=2.0)
        assert pm.is_running() is False
