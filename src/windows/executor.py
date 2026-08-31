"""Command execution controller using QEMU Guest Agent (QGA) for Windows guest VMs."""

import time
from pathlib import Path

from windows.qga import QGAClient, QGAError


class CommandResult:
    """Wrapper around remote command execution output."""

    def __init__(self, stdout: str, stderr: str, returncode: int):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.status_code = returncode

    def __contains__(self, item: str) -> bool:
        return item in self.stdout or item in self.stderr

    def __str__(self) -> str:
        return self.stdout

    def __repr__(self) -> str:
        return f"<CommandResult returncode={self.returncode} stdout={self.stdout!r} stderr={self.stderr!r}>"


class CommandController:
    """Controller for running commands on the guest Windows machine via QEMU Guest Agent (QGA)."""

    qga: QGAClient

    def __init__(
        self,
        qga_socket_path: str | Path,
    ):
        self.qga_socket_path = Path(qga_socket_path).resolve()
        self.qga = QGAClient(self.qga_socket_path)

    def wait_until_ready(self, timeout: int = 120, interval: float = 2.0) -> bool:
        """Poll QEMU Guest Agent until it is responsive and accepting commands."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.qga_socket_path.exists():
                try:
                    if self.qga.ping(timeout=2.0):
                        return True
                except Exception:
                    pass
            time.sleep(interval)
        return False

    def run(
        self,
        command: str,
        powershell: bool = True,
        timeout: int = 60,
        auto_retry: bool = True,
    ) -> CommandResult:
        """Run a command on the remote Windows machine via QGA with automatic connection retry."""
        start_time = time.time()
        last_exception = None

        while True:
            try:
                stdout, stderr, returncode = self.qga.exec(
                    command=command,
                    powershell=powershell,
                    timeout=timeout,
                )
                return CommandResult(stdout=stdout, stderr=stderr, returncode=returncode)
            except Exception as exc:
                last_exception = exc

            if not auto_retry or (time.time() - start_time) >= timeout:
                if last_exception:
                    raise last_exception
                raise QGAError(f"Command execution failed on {self.qga_socket_path}")
            time.sleep(1.0)
