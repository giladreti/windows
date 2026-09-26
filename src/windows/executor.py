"""Command execution controller using QEMU Guest Agent (QGA) for Windows guest VMs."""

import base64
import json
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from windows.qemu import is_tcp_endpoint
from windows.qga import QGAClient, QGAError

# Thread/async-safe scoped user execution context (username, password)
_current_user: ContextVar[tuple[str, str] | None] = ContextVar("current_user", default=None)

USER_EXEC_RUNNER_TEMPLATE = """
$enc = [System.Text.Encoding]::Unicode
$targetUser = $enc.GetString([System.Convert]::FromBase64String('__USER_B64__'))
$targetPass = $enc.GetString([System.Convert]::FromBase64String('__PASS_B64__'))
$cmdB64 = '__CMD_B64__'
$timeoutSec = __TIMEOUT__

$uid = [System.Guid]::NewGuid().ToString("N").Substring(0, 8)
$tmp = if ($env:PUBLIC) { $env:PUBLIC } else { "C:\\Users\\Public" }
$stdoutFile = Join-Path $tmp "qga_${uid}.out"
$stderrFile = Join-Path $tmp "qga_${uid}.err"
$exitFile = Join-Path $tmp "qga_${uid}.exit"
$scriptFile = Join-Path $tmp "qga_${uid}.ps1"
$taskName = "QGA_Task_$uid"

$payload = @"
`$ErrorActionPreference = 'Continue'
`$enc = [System.Text.Encoding]::Unicode
`$rawCmd = `$enc.GetString([System.Convert]::FromBase64String('$cmdB64'))
`$ec = 0
try {
__EXEC_BODY__
} catch {
    `$_ | Out-File -FilePath "$stderrFile" -Append -Encoding utf8
    `$ec = 1
}
`$ec | Out-File -FilePath "$exitFile" -NoNewline -Encoding utf8
"@

[System.IO.File]::WriteAllText($scriptFile, $payload, [System.Text.Encoding]::UTF8)

try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptFile`""
    Register-ScheduledTask -TaskName "$taskName" -Action $action -User "$targetUser" -Password "$targetPass" -RunLevel Highest -Force | Out-Null
} catch {
    Remove-Item $scriptFile -Force -ErrorAction SilentlyContinue
    throw "Failed to create scheduled task for user '$targetUser': " + $_.Exception.Message
}

try {
    Start-ScheduledTask -TaskName "$taskName"

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        Start-Sleep -Milliseconds 150
        if (Test-Path $exitFile) {
            Start-Sleep -Milliseconds 50
            break
        }
        $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($t -and $t.State -ne 'Running' -and (Test-Path $exitFile)) {
            break
        }
    }
} finally {
    Unregister-ScheduledTask -TaskName "$taskName" -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
}

$outText = if (Test-Path $stdoutFile) { [string](Get-Content -Path $stdoutFile -Raw -ErrorAction SilentlyContinue) } else { "" }
if ($outText -eq $null) { $outText = "" }
$errText = if (Test-Path $stderrFile) { [string](Get-Content -Path $stderrFile -Raw -ErrorAction SilentlyContinue) } else { "" }
if ($errText -eq $null) { $errText = "" }
$exitVal = if (Test-Path $exitFile) { [int](([string](Get-Content -Path $exitFile -Raw -ErrorAction SilentlyContinue)).Trim()) } else { -1 }

Remove-Item $stdoutFile, $stderrFile, $exitFile, $scriptFile -Force -ErrorAction SilentlyContinue

[PSCustomObject]@{
    stdout = $outText
    stderr = $errText
    returncode = $exitVal
} | ConvertTo-Json -Compress
"""


def _build_user_exec_script(
    command: str,
    user: str,
    password: str,
    powershell: bool = True,
    timeout: int = 60,
) -> str:
    """Build a PowerShell runner that invokes a command as a target user via Task Scheduler."""
    user_b64 = base64.b64encode(user.encode("utf-16le")).decode("ascii")
    pass_b64 = base64.b64encode(password.encode("utf-16le")).decode("ascii")
    cmd_b64 = base64.b64encode(command.encode("utf-16le")).decode("ascii")

    if powershell:
        exec_body = (
            '    & ([ScriptBlock]::Create(`$rawCmd)) 1> "$stdoutFile" 2> "$stderrFile"\n'
            "    `$ec = if (`$LASTEXITCODE -ne `$null) { `$LASTEXITCODE } else { 0 }"
        )
    else:
        exec_body = (
            '    cmd.exe /c `$rawCmd 1> "$stdoutFile" 2> "$stderrFile"\n'
            "    `$ec = if (`$LASTEXITCODE -ne `$null) { `$LASTEXITCODE } else { 0 }"
        )

    return (
        USER_EXEC_RUNNER_TEMPLATE.replace("__USER_B64__", user_b64)
        .replace("__PASS_B64__", pass_b64)
        .replace("__CMD_B64__", cmd_b64)
        .replace("__EXEC_BODY__", exec_body)
        .replace("__TIMEOUT__", str(timeout))
    )


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


class UserCommandContext:
    """Scoped execution context and controller view for running commands as a specific user."""

    def __init__(
        self,
        controller: "CommandController",
        user: str,
        password: str,
    ):
        self.controller = controller
        self.user = user
        self.password = password
        self._token: Any = None

    def run(
        self,
        command: str,
        powershell: bool = True,
        timeout: int = 60,
        auto_retry: bool = True,
    ) -> CommandResult:
        """Run command as this user."""
        return self.controller.run(
            command=command,
            user=self.user,
            password=self.password,
            powershell=powershell,
            timeout=timeout,
            auto_retry=auto_retry,
        )

    def as_user(self, user: str | None = None, password: str | None = None) -> "UserCommandContext":
        """Create a sub-context with different user credentials."""
        return self.controller.as_user(user=user, password=password)

    @property
    def as_system(self) -> "SystemCommandContext":
        """Switch to system execution context."""
        return SystemCommandContext(self.controller)

    def __enter__(self) -> "UserCommandContext":
        self._token = _current_user.set((self.user, self.password))
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._token is not None:
            _current_user.reset(self._token)
            self._token = None

    def __repr__(self) -> str:
        return f"<UserCommandContext user={self.user!r}>"


class SystemCommandContext:
    """Scoped execution context for running commands as NT AUTHORITY\\SYSTEM."""

    def __init__(self, controller: "CommandController"):
        self.controller = controller
        self._token: Any = None

    def run(
        self,
        command: str,
        powershell: bool = True,
        timeout: int = 60,
        auto_retry: bool = True,
    ) -> CommandResult:
        """Run command as SYSTEM."""
        return self.controller.run(
            command=command,
            as_system=True,
            powershell=powershell,
            timeout=timeout,
            auto_retry=auto_retry,
        )

    def __enter__(self) -> "SystemCommandContext":
        self._token = _current_user.set(None)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._token is not None:
            _current_user.reset(self._token)
            self._token = None

    def __repr__(self) -> str:
        return "<SystemCommandContext user='NT AUTHORITY\\SYSTEM'>"


class CommandController:
    """Controller for running commands on the guest Windows machine via QEMU Guest Agent (QGA)."""

    qga: QGAClient

    def __init__(
        self,
        qga_socket_path: Any,
        default_user: str | None = None,
        default_password: str | None = None,
    ):
        self.qga_socket_path = qga_socket_path if is_tcp_endpoint(qga_socket_path) else Path(qga_socket_path).resolve()
        self.qga = QGAClient(self.qga_socket_path)
        self.default_user = default_user
        self.default_password = default_password

    def wait_until_ready(self, timeout: int = 120, interval: float = 2.0) -> bool:
        """Poll QEMU Guest Agent until it is responsive and accepting commands."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            should_ping = True
            if isinstance(self.qga_socket_path, Path) and not self.qga_socket_path.exists():
                should_ping = False
            if should_ping:
                try:
                    if self.qga.ping(timeout=2.0):
                        return True
                except Exception:
                    pass
            time.sleep(interval)
        return False

    def as_user(self, user: str | None = None, password: str | None = None) -> UserCommandContext:
        """Return a user execution context bound to (user, password), usable as a view or context manager."""
        target_user = user or self.default_user
        target_password = password or (self.default_password if target_user == self.default_user else None)

        if not target_user:
            raise ValueError("A user must be specified (or default_user must be configured on controller/machine).")
        if not target_password:
            raise ValueError(f"Password must be provided when executing commands as user '{target_user}'.")

        return UserCommandContext(controller=self, user=target_user, password=target_password)

    @property
    def as_system(self) -> SystemCommandContext:
        """Return a system execution context bound to NT AUTHORITY\\SYSTEM."""
        return SystemCommandContext(self)

    def run(
        self,
        command: str,
        user: str | None = None,
        password: str | None = None,
        as_system: bool = False,
        powershell: bool = True,
        timeout: int = 60,
        auto_retry: bool = True,
    ) -> CommandResult:
        """Run a command on the remote Windows machine via QGA with automatic connection retry.

        Args:
            command: The command string to execute.
            user: Optional username to execute the command as. Requires a password.
            password: Password for the user (required if user is specified and not configured as default).
            as_system: If True, forces execution as NT AUTHORITY\\SYSTEM, bypassing any user scope.
            powershell: If True, executes command in PowerShell; if False, executes in cmd.exe.
            timeout: Command execution timeout in seconds.
            auto_retry: If True, retries connection on transient socket errors.
        """
        effective_user: str | None = None
        effective_password: str | None = None

        if not as_system:
            if user is not None:
                effective_user = user
                effective_password = password or (self.default_password if user == self.default_user else None)
                if not effective_password:
                    raise ValueError(f"Password must be provided when executing commands as user '{user}'.")
            else:
                scoped = _current_user.get()
                if scoped is not None:
                    effective_user, effective_password = scoped
                elif self.default_user is not None:
                    effective_user = self.default_user
                    effective_password = self.default_password
                    if not effective_password:
                        raise ValueError(
                            f"Password must be provided when executing commands as user '{effective_user}'."
                        )

        start_time = time.time()
        last_exception = None

        while True:
            try:
                if effective_user is not None and effective_password is not None:
                    runner_cmd = _build_user_exec_script(
                        command=command,
                        user=effective_user,
                        password=effective_password,
                        powershell=powershell,
                        timeout=timeout,
                    )
                    stdout, stderr, returncode = self.qga.exec(
                        command=runner_cmd,
                        powershell=True,
                        timeout=timeout + 30,
                    )
                    try:
                        data = json.loads(stdout.strip())
                        return CommandResult(
                            stdout=data.get("stdout", ""),
                            stderr=data.get("stderr", "") or stderr,
                            returncode=int(data.get("returncode", returncode)),
                        )
                    except (json.JSONDecodeError, KeyError, ValueError):
                        return CommandResult(stdout=stdout, stderr=stderr, returncode=returncode)
                else:
                    stdout, stderr, returncode = self.qga.exec(
                        command=command,
                        powershell=powershell,
                        timeout=timeout,
                    )
                    return CommandResult(stdout=stdout, stderr=stderr, returncode=returncode)
            except Exception as exc:
                last_exception = exc
                self.qga.close()

            if not auto_retry or (time.time() - start_time) >= timeout:
                if last_exception:
                    raise last_exception
                raise QGAError(f"Command execution failed on {self.qga_socket_path}")
            time.sleep(1.0)
