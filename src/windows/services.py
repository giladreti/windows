"""Windows Services management subsystem."""

import json

from pydantic import BaseModel

from windows.executor import CommandController


class ServiceInfo(BaseModel):
    """Information about a Windows service."""

    name: str
    display_name: str
    status: str
    start_type: str = ""


class ServiceController:
    """Controller for inspecting and managing Windows services inside the guest VM."""

    def __init__(self, command_controller: CommandController):
        self.cmd = command_controller

    def list(self) -> list[ServiceInfo]:
        """List all installed Windows services and their current statuses."""
        ps_cmd = (
            "$services = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, State, StartMode; "
            "if ($services) { $services | ConvertTo-Json -Compress } else { '[]' }"
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

        services = []
        for item in data:
            try:
                status_val = str(item.get("State") or item.get("Status") or "")
                start_mode_val = str(item.get("StartMode") or item.get("StartType") or "")
                services.append(
                    ServiceInfo(
                        name=str(item.get("Name", "")),
                        display_name=str(item.get("DisplayName", "")),
                        status=status_val,
                        start_type=start_mode_val,
                    )
                )
            except Exception:
                continue
        return services

    def get(self, service_name: str) -> ServiceInfo | None:
        """Get details for a specific Windows service by name directly."""
        ps_cmd = (
            f"$svc = Get-CimInstance Win32_Service -Filter \"Name='{service_name}'\" -ErrorAction SilentlyContinue | "
            "Select-Object -First 1 Name, DisplayName, State, StartMode; "
            "if ($svc) { $svc | ConvertTo-Json -Compress } else { 'null' }"
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
            status_val = str(item.get("State") or item.get("Status") or "")
            start_mode_val = str(item.get("StartMode") or item.get("StartType") or "")
            return ServiceInfo(
                name=str(item.get("Name", "")),
                display_name=str(item.get("DisplayName", "")),
                status=status_val,
                start_type=start_mode_val,
            )
        except Exception:
            return None

    def start(self, service_name: str) -> bool:
        """Start a Windows service."""
        cmd = f"Start-Service -Name '{service_name}' -ErrorAction SilentlyContinue"
        res = self.cmd.run(cmd, auto_retry=False)
        return res.returncode == 0

    def stop(self, service_name: str, force: bool = True) -> bool:
        """Stop a Windows service."""
        force_flag = "-Force" if force else ""
        cmd = f"Stop-Service -Name '{service_name}' {force_flag} -ErrorAction SilentlyContinue"
        res = self.cmd.run(cmd, auto_retry=False)
        return res.returncode == 0

    def restart(self, service_name: str, force: bool = True) -> bool:
        """Restart a Windows service."""
        force_flag = "-Force" if force else ""
        cmd = f"Restart-Service -Name '{service_name}' {force_flag} -ErrorAction SilentlyContinue"
        res = self.cmd.run(cmd, auto_retry=False)
        return res.returncode == 0

    def set_startup_type(self, service_name: str, startup_type: str = "Automatic") -> bool:
        """Configure service startup type ('Automatic', 'Manual', 'Disabled')."""
        cmd = f"Set-Service -Name '{service_name}' -StartupType '{startup_type}' -ErrorAction SilentlyContinue"
        res = self.cmd.run(cmd, auto_retry=False)
        return res.returncode == 0
