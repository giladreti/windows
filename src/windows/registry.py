"""Windows Registry management subsystem using native Windows registry utilities."""

import re
from typing import Any

from windows.executor import CommandController


class RegistryController:
    """Controller for inspecting, reading, and modifying the Windows Registry."""

    def __init__(self, command_controller: CommandController):
        self.cmd = command_controller

    def _normalize_key_path(self, key_path: str) -> str:
        """Normalize PowerShell style 'HKLM:\\...' or 'HKCU:\\...' to standard 'HKLM\\...'."""
        clean = key_path.replace(":\\", "\\").replace(":/", "\\").replace("/", "\\")
        return clean.rstrip("\\")

    def key_exists(self, key_path: str) -> bool:
        """Check if a registry key path exists (e.g. 'HKLM\\Software\\MyApp')."""
        norm_path = self._normalize_key_path(key_path)
        cmd = f'reg query "{norm_path}"'
        res = self.cmd.run(cmd, powershell=False, auto_retry=False)
        return res.returncode == 0

    def get_value(self, key_path: str, value_name: str = "", default: Any = None) -> Any:
        """Get a value from the registry.

        Args:
            key_path: Registry path (e.g. 'HKLM\\Software\\MyApp' or 'HKCU\\...').
            value_name: Name of the property (empty for default value).
            default: Value returned if the key or property does not exist.
        """
        norm_path = self._normalize_key_path(key_path)
        if value_name:
            cmd = f'reg query "{norm_path}" /v "{value_name}"'
        else:
            cmd = f'reg query "{norm_path}" /ve'

        res = self.cmd.run(cmd, powershell=False, auto_retry=False)
        if res.returncode != 0 or not res.stdout.strip():
            return default

        # Output format:
        # HKEY_LOCAL_MACHINE\Software\...
        #     ValueName    REG_SZ    ValueData
        lines = res.stdout.strip().splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith("HKEY_"):
                continue
            parts = re.split(r"\s{4,}|\t+", line)
            if len(parts) >= 3:
                val_type = parts[1].strip()
                val_data = parts[2].strip()
                if "DWORD" in val_type or "QWORD" in val_type:
                    try:
                        return int(val_data, 16) if val_data.startswith("0x") else int(val_data)
                    except ValueError:
                        return val_data
                return val_data
            elif len(parts) == 2 and not value_name:
                return parts[1].strip()

        return default

    def set_value(
        self,
        key_path: str,
        value_name: str,
        value: Any,
        value_type: str = "String",
    ) -> None:
        """Create or update a registry property and its parent key if needed.

        Args:
            key_path: Registry path (e.g. 'HKLM\\Software\\MyApp').
            value_name: Property name.
            value: Property value.
            value_type: Windows registry type ('String', 'DWord', 'QWord', 'Binary', 'MultiString', 'ExpandString').
        """
        norm_path = self._normalize_key_path(key_path)
        type_map = {
            "string": "REG_SZ",
            "dword": "REG_DWORD",
            "qword": "REG_QWORD",
            "binary": "REG_BINARY",
            "multistring": "REG_MULTI_SZ",
            "expandstring": "REG_EXPAND_SZ",
        }
        reg_type = type_map.get(value_type.lower(), "REG_SZ")

        if value_name:
            cmd = f'reg add "{norm_path}" /v "{value_name}" /t {reg_type} /d "{value}" /f'
        else:
            cmd = f'reg add "{norm_path}" /ve /t {reg_type} /d "{value}" /f'

        res = self.cmd.run(cmd, powershell=False, auto_retry=False)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to set registry value at {key_path}\\{value_name}: {res.stderr}")

    def delete_value(self, key_path: str, value_name: str) -> bool:
        """Remove a specific property from a registry key."""
        norm_path = self._normalize_key_path(key_path)
        cmd = f'reg delete "{norm_path}" /v "{value_name}" /f'
        res = self.cmd.run(cmd, powershell=False, auto_retry=False)
        return res.returncode == 0

    def delete_key(self, key_path: str, recurse: bool = True) -> bool:
        """Remove a registry key and all subkeys."""
        norm_path = self._normalize_key_path(key_path)
        cmd = f'reg delete "{norm_path}" /f'
        res = self.cmd.run(cmd, powershell=False, auto_retry=False)
        return res.returncode == 0
