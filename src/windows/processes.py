"""Process management subsystem for Windows guest VMs."""

import json

from pydantic import BaseModel

from windows.executor import CommandController


class ProcessInfo(BaseModel):
    """Information about a running Windows process."""

    pid: int
    name: str
    cpu: float = 0.0
    working_set_mb: float = 0.0
    path: str = ""


class ProcessController:
    """Controller for inspecting and managing processes inside the Windows guest VM."""

    def __init__(self, command_controller: CommandController):
        self.cmd = command_controller

    def list(self) -> list[ProcessInfo]:
        """List all active processes in the Windows guest."""
        ps_cmd = (
            "$procs = Get-Process | Select-Object Id, ProcessName, CPU, "
            "@{Name='WS_MB';Expression={[math]::Round($_.WorkingSet64 / 1MB, 2)}}, "
            "@{Name='Path';Expression={$_.Path}}; "
            "if ($procs) { $procs | ConvertTo-Json -Compress } else { '[]' }"
        )
        res = self.cmd.run(ps_cmd, auto_retry=False)
        out = res.stdout.strip()
        if not out or out == "[]":
            return []

        try:
            parsed = json.loads(out)
            data = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []

        procs = []
        for item in data:
            try:
                procs.append(
                    ProcessInfo(
                        pid=int(item.get("Id", 0)),
                        name=str(item.get("ProcessName", "")),
                        cpu=float(item.get("CPU") or 0.0),
                        working_set_mb=float(item.get("WS_MB") or 0.0),
                        path=str(item.get("Path") or ""),
                    )
                )
            except Exception:
                continue
        return procs

    def get(self, name_or_pid: str | int) -> ProcessInfo | None:
        """Get details for a specific process by name or PID."""
        if isinstance(name_or_pid, int):
            selector = f"-Id {name_or_pid}"
        else:
            clean_name = name_or_pid[:-4] if name_or_pid.lower().endswith(".exe") else name_or_pid
            selector = f"-Name '{clean_name}'"

        ps_cmd = (
            f"$p = Get-Process {selector} -ErrorAction SilentlyContinue | Select-Object -First 1 Id, ProcessName, CPU, "
            "@{Name='WS_MB';Expression={[math]::Round($_.WorkingSet64 / 1MB, 2)}}, "
            "@{Name='Path';Expression={$_.Path}}; "
            "if ($p) { $p | ConvertTo-Json -Compress } else { 'null' }"
        )
        res = self.cmd.run(ps_cmd, auto_retry=False)
        out = res.stdout.strip()
        if not out or out == "null":
            return None

        try:
            raw = json.loads(out)
            item = raw[0] if isinstance(raw, list) and raw else raw
            if not isinstance(item, dict):
                return None
            return ProcessInfo(
                pid=int(item.get("Id", 0)),
                name=str(item.get("ProcessName", "")),
                cpu=float(item.get("CPU") or 0.0),
                working_set_mb=float(item.get("WS_MB") or 0.0),
                path=str(item.get("Path") or ""),
            )
        except Exception:
            return None

    def spawn(self, command_line: str) -> int:
        """Spawn a detached background process inside the guest and return its PID immediately.

        Uses WMI (Win32_Process.Create) to cleanly detach from QGA execution handles.
        """
        ps_cmd = (
            f"$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{command_line}'}}; "
            "if ($res.ReturnValue -eq 0) { $res.ProcessId } else { -1 }"
        )
        res = self.cmd.run(ps_cmd, auto_retry=False)
        try:
            return int(res.stdout.strip())
        except Exception:
            return -1

    def kill(self, name_or_pid: str | int, force: bool = True) -> bool:
        """Terminate a process by name or PID."""
        force_flag = "-Force" if force else ""
        if isinstance(name_or_pid, int):
            cmd = f"Stop-Process -Id {name_or_pid} {force_flag} -ErrorAction Stop"
        else:
            cmd = f"Stop-Process -Name '{name_or_pid}' {force_flag} -ErrorAction Stop"

        try:
            res = self.cmd.run(cmd, auto_retry=False)
            return res.returncode == 0
        except Exception:
            return False
