"""Windows Defender Firewall management subsystem for rules, profiles, and network categories."""

import json
from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr

if TYPE_CHECKING:
    from windows.machine import Machine
    from windows.network import NetworkInterface


class FirewallError(RuntimeError):
    """Exception raised for Windows Firewall operations."""


class FirewallAction(StrEnum):
    """Firewall rule action."""

    ALLOW = "Allow"
    BLOCK = "Block"


class FirewallDirection(StrEnum):
    """Firewall traffic direction."""

    INBOUND = "Inbound"
    OUTBOUND = "Outbound"
    IN = "Inbound"
    OUT = "Outbound"


class FirewallProfile(StrEnum):
    """Windows Firewall profiles."""

    DOMAIN = "Domain"
    PRIVATE = "Private"
    PUBLIC = "Public"
    ANY = "Any"
    ALL = "All"


class NetworkCategory(StrEnum):
    """Network connection profile categories for network interfaces."""

    PUBLIC = "Public"
    PRIVATE = "Private"
    DOMAIN = "DomainAuthenticated"
    DOMAIN_AUTHENTICATED = "DomainAuthenticated"


def _normalize_direction(direction: str | FirewallDirection) -> str:
    d = str(direction).strip().lower()
    if d in ("in", "inbound"):
        return "Inbound"
    if d in ("out", "outbound"):
        return "Outbound"
    return "Inbound"


def _normalize_action(action: str | FirewallAction) -> str:
    a = str(action).strip().lower()
    if a == "block":
        return "Block"
    return "Allow"


def _normalize_profile(profile: str | Sequence[str] | FirewallProfile) -> str:
    if isinstance(profile, (list, tuple, set)):
        return ",".join(str(p).strip().capitalize() for p in profile)
    p = str(profile).strip().lower()
    if p in ("all", "any"):
        return "Any"
    if p == "domain":
        return "Domain"
    if p == "private":
        return "Private"
    if p == "public":
        return "Public"
    return str(profile).strip()


def _normalize_ports(port: int | str | Sequence[int | str] | None) -> str | None:
    if port is None:
        return None
    if isinstance(port, (list, tuple, set)):
        return ",".join(str(p).strip() for p in port)
    return str(port).strip()


class FirewallRule(BaseModel):
    """Represents a Windows Defender Firewall rule."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    display_name: str = ""
    description: str = ""
    direction: str = "Inbound"
    action: str = "Allow"
    enabled: bool = True
    profile: str = "Any"
    protocol: str = "Any"
    local_port: str = "Any"
    remote_port: str = "Any"
    local_address: str = "Any"
    remote_address: str = "Any"
    program: str = "Any"
    service: str = "Any"

    _controller: Any = PrivateAttr(default=None)

    def enable(self) -> "FirewallRule":
        """Enable this firewall rule."""
        if self._controller:
            self._controller.enable_rule(self.name)
            self.enabled = True
        return self

    def disable(self) -> "FirewallRule":
        """Disable this firewall rule."""
        if self._controller:
            self._controller.disable_rule(self.name)
            self.enabled = False
        return self

    def modify(self, **kwargs) -> "FirewallRule":
        """Modify attributes of this firewall rule in the Windows guest."""
        if self._controller:
            updated = self._controller.modify_rule(self.name, **kwargs)
            for k, v in updated.model_dump().items():
                if hasattr(self, k):
                    setattr(self, k, v)
        return self

    def remove(self) -> None:
        """Delete this firewall rule from the Windows guest."""
        if self._controller:
            self._controller.remove_rule(self.name)

    def delete(self) -> None:
        """Alias for remove()."""
        self.remove()

    def __repr__(self) -> str:
        status = "enabled" if self.enabled else "disabled"
        port_info = f" port={self.local_port}" if self.local_port != "Any" else ""
        proto_info = f" proto={self.protocol}" if self.protocol != "Any" else ""
        return (
            f"<FirewallRule name={self.name!r} dir={self.direction} "
            f"action={self.action}{proto_info}{port_info} status={status}>"
        )


class FirewallController:
    """Controller for inspecting and managing Windows Defender Firewall rules and profiles."""

    def __init__(self, machine: "Machine"):
        self.machine = machine

    # --- Profile State Control (ON / OFF) ---

    def status(self) -> dict[str, bool]:
        """Query the on/off status of Domain, Private, and Public firewall profiles.

        Returns:
            Dictionary mapping profile names to boolean enabled state,
            e.g. {'domain': True, 'private': True, 'public': True}.
        """
        ps_cmd = (
            "$profiles = Get-NetFirewallProfile | Select-Object Name, Enabled; "
            "if ($profiles) { $profiles | ConvertTo-Json -Compress } else { '[]' }"
        )
        res = self.machine.command.run(ps_cmd, auto_retry=False)
        out = res.stdout.strip()
        result: dict[str, bool] = {"domain": False, "private": False, "public": False}
        if not out or out == "[]":
            return result

        try:
            parsed = json.loads(out)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                p_name = str(item.get("Name", "")).lower()
                val = item.get("Enabled")
                is_on = val in (1, True, "1", "True", "true")
                if p_name in result:
                    result[p_name] = is_on
        except Exception:
            pass

        return result

    def is_enabled(self, profile: Literal["all", "any", "domain", "private", "public"] | str = "all") -> bool:
        """Check if firewall is enabled for the specified profile(s)."""
        current = self.status()
        p = profile.lower()
        if p == "all":
            return all(current.values())
        if p == "any":
            return any(current.values())
        return current.get(p, False)

    def on(self, profile: Literal["all", "domain", "private", "public"] | str = "all") -> None:
        """Turn ON the Windows Firewall for the specified profile(s) ('all', 'domain', 'private', 'public')."""
        target = "Domain,Private,Public" if profile.lower() == "all" else _normalize_profile(profile)
        ps_cmd = f"Set-NetFirewallProfile -Profile '{target}' -Enabled True"
        res = self.machine.command.run(ps_cmd, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to enable firewall for profile(s) '{target}': {res.stderr.strip()}")

    def off(self, profile: Literal["all", "domain", "private", "public"] | str = "all") -> None:
        """Turn OFF the Windows Firewall for the specified profile(s) ('all', 'domain', 'private', 'public')."""
        target = "Domain,Private,Public" if profile.lower() == "all" else _normalize_profile(profile)
        ps_cmd = f"Set-NetFirewallProfile -Profile '{target}' -Enabled False"
        res = self.machine.command.run(ps_cmd, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to disable firewall for profile(s) '{target}': {res.stderr.strip()}")

    def enable(self, profile: Literal["all", "domain", "private", "public"] | str = "all") -> None:
        """Alias for on(). Turn on Windows Firewall."""
        self.on(profile=profile)

    def disable(self, profile: Literal["all", "domain", "private", "public"] | str = "all") -> None:
        """Alias for off(). Turn off Windows Firewall."""
        self.off(profile=profile)

    # --- Network Connection Profiles (Per-NIC Profile Selection) ---

    def _resolve_interface_index(self, interface: "str | int | NetworkInterface") -> str:
        """Resolve interface identifier, alias, or NetworkInterface instance to PowerShell target."""
        from windows.network import NetworkInterface

        if isinstance(interface, NetworkInterface):
            target_mac = interface.mac.replace(":", "").replace("-", "").upper()
            return f"(Get-NetAdapter | Where-Object {{ ($_.MacAddress -replace '[:-]', '').ToUpper() -eq '{target_mac}' }} | Select-Object -First 1 -ExpandProperty ifIndex)"
        if isinstance(interface, int):
            return str(interface)
        # If string looks like a MAC address
        cleaned = str(interface).replace(":", "").replace("-", "").upper()
        if len(cleaned) == 12:
            return f"(Get-NetAdapter | Where-Object {{ ($_.MacAddress -replace '[:-]', '').ToUpper() -eq '{cleaned}' }} | Select-Object -First 1 -ExpandProperty ifIndex)"
        # String interface alias
        return f"(Get-NetAdapter -Name '{interface}' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty ifIndex)"

    def set_profile(
        self,
        interface: "str | int | NetworkInterface",
        profile: Literal["Public", "Private", "Domain", "DomainAuthenticated"] | str | NetworkCategory,
    ) -> None:
        """Set the network category / firewall profile for a specific network interface.

        Args:
            interface: Interface index, alias name, MAC address, or `NetworkInterface` object.
            profile: Target category ('Public', 'Private', or 'DomainAuthenticated').
        """
        cat_str = str(profile).strip().lower()
        if cat_str in ("private", "priv"):
            category = "Private"
        elif cat_str in ("domain", "domainauthenticated"):
            category = "DomainAuthenticated"
        else:
            category = "Public"

        idx_expr = self._resolve_interface_index(interface)
        ps_script = f"""
$idx = {idx_expr}
if (-not $idx) {{
    throw "Network interface '{interface}' not found inside Windows guest."
}}
Set-NetConnectionProfile -InterfaceIndex $idx -NetworkCategory '{category}' -Confirm:$false
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to set network category on interface '{interface}': {res.stderr.strip()}")

    def get_profile(self, interface: "str | int | NetworkInterface") -> str:
        """Get the current network category / firewall profile for a specific network interface."""
        idx_expr = self._resolve_interface_index(interface)
        ps_script = f"""
$idx = {idx_expr}
if (-not $idx) {{
    throw "Network interface '{interface}' not found inside Windows guest."
}}
$prof = Get-NetConnectionProfile -InterfaceIndex $idx -ErrorAction SilentlyContinue
if ($prof) {{
    [string]$prof.NetworkCategory
}} else {{
    'Public'
}}
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        out = res.stdout.strip()
        return out if out else "Public"

    def list_connection_profiles(self) -> list[dict[str, Any]]:
        """List all active network connection profiles across network interfaces."""
        ps_script = """
$profs = Get-NetConnectionProfile -ErrorAction SilentlyContinue | ForEach-Object {
    [PSCustomObject]@{
        Name = [string]$_.Name
        InterfaceAlias = [string]$_.InterfaceAlias
        InterfaceIndex = [int]$_.InterfaceIndex
        NetworkCategory = [string]$_.NetworkCategory
        IPv4Connectivity = [string]$_.IPv4Connectivity
    }
}
if ($profs) { $profs | ConvertTo-Json -Compress } else { '[]' }
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        out = res.stdout.strip()
        if not out or out == "[]":
            return []
        try:
            parsed = json.loads(out)
            return parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            return []

    # --- Rule Management (Add, List, Modify, Remove) ---

    def _convert_ps_rule_dict(self, item: dict[str, Any]) -> FirewallRule:
        """Convert PowerShell JSON rule dictionary to a FirewallRule instance."""
        rule = FirewallRule(
            name=str(item.get("Name", "")),
            display_name=str(item.get("DisplayName", "")),
            description=str(item.get("Description", "")),
            direction=str(item.get("Direction", "Inbound")),
            action=str(item.get("Action", "Allow")),
            enabled=bool(item.get("Enabled", True)),
            profile=str(item.get("Profile", "Any")),
            protocol=str(item.get("Protocol", "Any")),
            local_port=str(item.get("LocalPort", "Any")),
            remote_port=str(item.get("RemotePort", "Any")),
            local_address=str(item.get("LocalAddress", "Any")),
            remote_address=str(item.get("RemoteAddress", "Any")),
            program=str(item.get("Program", "Any")),
            service=str(item.get("Service", "Any")),
        )
        rule._controller = self
        return rule

    def add_rule(
        self,
        name: str,
        display_name: str | None = None,
        direction: Literal["in", "out", "inbound", "outbound"] | str | FirewallDirection = "in",
        action: Literal["allow", "block"] | str | FirewallAction = "allow",
        enabled: bool = True,
        profile: Literal["any", "domain", "private", "public"] | str | Sequence[str] | FirewallProfile = "any",
        protocol: str | None = None,
        local_port: int | str | Sequence[int | str] | None = None,
        remote_port: int | str | Sequence[int | str] | None = None,
        local_address: str | Sequence[str] | None = None,
        remote_address: str | Sequence[str] | None = None,
        program: str | None = None,
        service: str | None = None,
        icmp_type: int | str | None = None,
        interface: "str | int | NetworkInterface | None" = None,
        description: str | None = None,
    ) -> FirewallRule:
        """Create and add a new firewall rule inside the Windows guest.

        Args:
            name: Unique internal rule name identifier.
            display_name: Optional human-readable display name.
            direction: 'Inbound' or 'Outbound' (defaults to 'Inbound').
            action: 'Allow' or 'Block' (defaults to 'Allow').
            enabled: Whether the rule is active (defaults to True).
            profile: Firewall profiles ('Domain', 'Private', 'Public', 'Any').
            protocol: Protocol ('TCP', 'UDP', 'ICMPv4', 'ICMPv6', 'Any').
            local_port: Local port, range ('8000-8080'), or list ([80, 443]).
            remote_port: Remote port, range, or list.
            local_address: Local IP address or subnet.
            remote_address: Remote IP address or subnet.
            program: Absolute path to application executable.
            service: Short name of Windows service.
            icmp_type: ICMP type (e.g. 8 for Echo Request / ping).
            interface: Specific network interface name, alias, or instance.
            description: Optional textual description.

        Returns:
            The created `FirewallRule` instance.
        """
        dir_val = _normalize_direction(direction)
        act_val = _normalize_action(action)
        prof_val = _normalize_profile(profile)
        disp_val = display_name if display_name is not None else name
        desc_val = description or ""

        # Auto-detect protocol if ports are specified without an explicit protocol
        actual_proto = protocol
        if (local_port or remote_port) and not actual_proto:
            actual_proto = "TCP"

        local_port_str = _normalize_ports(local_port)
        remote_port_str = _normalize_ports(remote_port)

        args = [
            f"-Name '{name}'",
            f"-DisplayName '{disp_val}'",
            f"-Direction {dir_val}",
            f"-Action {act_val}",
            f"-Enabled {'$True' if enabled else '$False'}",
        ]
        if prof_val:
            args.append(f"-Profile '{prof_val}'")
        if desc_val:
            args.append(f"-Description '{desc_val}'")
        if actual_proto:
            args.append(f"-Protocol '{actual_proto}'")
        if local_port_str:
            args.append(f"-LocalPort @({','.join(repr(p.strip()) for p in local_port_str.split(','))})")
        if remote_port_str:
            args.append(f"-RemotePort @({','.join(repr(p.strip()) for p in remote_port_str.split(','))})")
        if local_address:
            local_addr_str = ",".join(f"'{a.strip()}'" for a in (local_address if isinstance(local_address, (list, tuple)) else [local_address]))
            args.append(f"-LocalAddress @({local_addr_str})")
        if remote_address:
            remote_addr_str = ",".join(f"'{a.strip()}'" for a in (remote_address if isinstance(remote_address, (list, tuple)) else [remote_address]))
            args.append(f"-RemoteAddress @({remote_addr_str})")
        if program:
            args.append(f"-Program '{program}'")
        if service:
            args.append(f"-Service '{service}'")
        if icmp_type is not None:
            args.append(f"-IcmpType '{icmp_type}'")
        if interface is not None:
            from windows.network import NetworkInterface
            if isinstance(interface, NetworkInterface):
                target_mac = interface.mac.replace(":", "").replace("-", "").upper()
                args.append(f"-InterfaceAlias (Get-NetAdapter | Where-Object {{ ($_.MacAddress -replace '[:-]', '').ToUpper() -eq '{target_mac}' }} | Select-Object -First 1 -ExpandProperty Name)")
            else:
                args.append(f"-InterfaceAlias '{interface}'")

        ps_script = f"New-NetFirewallRule {' '.join(args)} -ErrorAction Stop"
        res = self.machine.command.run(ps_script, powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to add firewall rule '{name}': {res.stderr.strip()}")

        return self.get_rule(name) or FirewallRule(
            name=name,
            display_name=disp_val,
            description=desc_val,
            direction=dir_val,
            action=act_val,
            enabled=enabled,
            profile=prof_val,
            protocol=actual_proto or "Any",
            local_port=local_port_str or "Any",
            remote_port=remote_port_str or "Any",
        )

    def list_rules(
        self,
        name: str | None = None,
        direction: Literal["in", "out", "inbound", "outbound"] | str | None = None,
        action: Literal["allow", "block"] | str | None = None,
        enabled: bool | None = None,
        profile: str | None = None,
        protocol: str | None = None,
        local_port: int | str | None = None,
    ) -> list[FirewallRule]:
        """List firewall rules matching optional criteria.

        Args:
            name: Optional rule name or wildcard pattern (e.g. 'Allow*').
            direction: Optional filter by direction ('Inbound' or 'Outbound').
            action: Optional filter by action ('Allow' or 'Block').
            enabled: Optional filter by enabled state (True/False).
            profile: Optional filter by profile ('Domain', 'Private', 'Public').
            protocol: Optional filter by protocol ('TCP', 'UDP').
            local_port: Optional filter by local port.

        Returns:
            List of matching `FirewallRule` objects.
        """
        filters = []
        if name:
            filters.append(f"-Name '{name}'")
        if direction:
            filters.append(f"-Direction {_normalize_direction(direction)}")
        if action:
            filters.append(f"-Action {_normalize_action(action)}")
        if enabled is not None:
            filters.append(f"-Enabled {'$True' if enabled else '$False'}")

        filter_args = " ".join(filters)
        ps_script = f"""
$rules = Get-NetFirewallRule {filter_args} -ErrorAction SilentlyContinue | ForEach-Object {{
    $r = $_
    $port = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
    $addr = $r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
    $app = $r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
    [PSCustomObject]@{{
        Name = [string]$r.Name
        DisplayName = [string]$r.DisplayName
        Description = [string]$r.Description
        Direction = [string]$r.Direction
        Action = [string]$r.Action
        Enabled = [bool]($r.Enabled -eq 1 -or $r.Enabled -eq 'True' -or $r.Enabled -eq $true)
        Profile = [string]$r.Profile
        Protocol = if ($port -and $port.Protocol) {{ [string]$port.Protocol }} else {{ 'Any' }}
        LocalPort = if ($port -and $port.LocalPort) {{ [string]$port.LocalPort }} else {{ 'Any' }}
        RemotePort = if ($port -and $port.RemotePort) {{ [string]$port.RemotePort }} else {{ 'Any' }}
        LocalAddress = if ($addr -and $addr.LocalAddress) {{ [string]$addr.LocalAddress }} else {{ 'Any' }}
        RemoteAddress = if ($addr -and $addr.RemoteAddress) {{ [string]$addr.RemoteAddress }} else {{ 'Any' }}
        Program = if ($app -and $app.Program) {{ [string]$app.Program }} else {{ 'Any' }}
        Service = if ($app -and $app.Service) {{ [string]$app.Service }} else {{ 'Any' }}
    }}
}}
if ($rules) {{ $rules | ConvertTo-Json -Compress }} else {{ '[]' }}
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        out = res.stdout.strip()
        if not out or out == "[]":
            return []

        rules = []
        try:
            parsed = json.loads(out)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                rule = self._convert_ps_rule_dict(item)
                # Additional client-side filtering if needed
                if profile and profile.lower() != "any":
                    if profile.lower() not in rule.profile.lower():
                        continue
                if protocol and protocol.lower() != "any":
                    if protocol.lower() != rule.protocol.lower():
                        continue
                if local_port is not None and str(local_port) not in rule.local_port:
                    continue
                rules.append(rule)
        except Exception:
            pass

        return rules

    def get_rule(self, name: str) -> FirewallRule | None:
        """Get a specific firewall rule by its unique name or display name."""
        ps_script = f"""
$r = Get-NetFirewallRule -Name '{name}' -ErrorAction SilentlyContinue
if (-not $r) {{
    $r = Get-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue | Select-Object -First 1
}}
if ($r) {{
    $port = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
    $addr = $r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
    $app = $r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
    [PSCustomObject]@{{
        Name = [string]$r.Name
        DisplayName = [string]$r.DisplayName
        Description = [string]$r.Description
        Direction = [string]$r.Direction
        Action = [string]$r.Action
        Enabled = [bool]($r.Enabled -eq 1 -or $r.Enabled -eq 'True' -or $r.Enabled -eq $true)
        Profile = [string]$r.Profile
        Protocol = if ($port -and $port.Protocol) {{ [string]$port.Protocol }} else {{ 'Any' }}
        LocalPort = if ($port -and $port.LocalPort) {{ [string]$port.LocalPort }} else {{ 'Any' }}
        RemotePort = if ($port -and $port.RemotePort) {{ [string]$port.RemotePort }} else {{ 'Any' }}
        LocalAddress = if ($addr -and $addr.LocalAddress) {{ [string]$addr.LocalAddress }} else {{ 'Any' }}
        RemoteAddress = if ($addr -and $addr.RemoteAddress) {{ [string]$addr.RemoteAddress }} else {{ 'Any' }}
        Program = if ($app -and $app.Program) {{ [string]$app.Program }} else {{ 'Any' }}
        Service = if ($app -and $app.Service) {{ [string]$app.Service }} else {{ 'Any' }}
    }} | ConvertTo-Json -Compress
}} else {{
    'null'
}}
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        out = res.stdout.strip()
        if not out or out == "null":
            return None
        try:
            item = json.loads(out)
            return self._convert_ps_rule_dict(item)
        except Exception:
            return None

    def rule_exists(self, name: str) -> bool:
        """Check if a firewall rule exists."""
        return self.get_rule(name) is not None

    def modify_rule(
        self,
        name: str | FirewallRule,
        new_name: str | None = None,
        display_name: str | None = None,
        direction: Literal["in", "out", "inbound", "outbound"] | str | None = None,
        action: Literal["allow", "block"] | str | None = None,
        enabled: bool | None = None,
        profile: Literal["any", "domain", "private", "public"] | str | None = None,
        protocol: str | None = None,
        local_port: int | str | Sequence[int | str] | None = None,
        remote_port: int | str | Sequence[int | str] | None = None,
        local_address: str | Sequence[str] | None = None,
        remote_address: str | Sequence[str] | None = None,
        program: str | None = None,
        service: str | None = None,
        description: str | None = None,
    ) -> FirewallRule:
        """Modify an existing firewall rule inside the Windows guest.

        Returns:
            The updated `FirewallRule` instance.
        """
        rule_name = name.name if isinstance(name, FirewallRule) else str(name)
        updates = []
        if new_name is not None:
            updates.append(f"-NewName '{new_name}'")
        if display_name is not None:
            updates.append(f"-NewDisplayName '{display_name}'")
        if direction is not None:
            updates.append(f"-Direction {_normalize_direction(direction)}")
        if action is not None:
            updates.append(f"-Action {_normalize_action(action)}")
        if enabled is not None:
            updates.append(f"-Enabled {'$True' if enabled else '$False'}")
        if profile is not None:
            updates.append(f"-Profile '{_normalize_profile(profile)}'")
        if description is not None:
            updates.append(f"-Description '{description}'")

        ps_parts = []
        if updates:
            ps_parts.append(f"Set-NetFirewallRule -Name '{rule_name}' {' '.join(updates)}")

        # Modify port filters if port/protocol specified
        port_updates = []
        if protocol is not None:
            port_updates.append(f"-Protocol '{protocol}'")
        if local_port is not None:
            p_str = _normalize_ports(local_port)
            port_updates.append(f"-LocalPort @({','.join(repr(p.strip()) for p in p_str.split(','))})")
        if remote_port is not None:
            p_str = _normalize_ports(remote_port)
            port_updates.append(f"-RemotePort @({','.join(repr(p.strip()) for p in p_str.split(','))})")
        if port_updates:
            ps_parts.append(f"Get-NetFirewallRule -Name '{rule_name}' | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter {' '.join(port_updates)}")

        # Modify address filters if addresses specified
        addr_updates = []
        if local_address is not None:
            local_addr_str = ",".join(f"'{a.strip()}'" for a in (local_address if isinstance(local_address, (list, tuple)) else [local_address]))
            addr_updates.append(f"-LocalAddress @({local_addr_str})")
        if remote_address is not None:
            remote_addr_str = ",".join(f"'{a.strip()}'" for a in (remote_address if isinstance(remote_address, (list, tuple)) else [remote_address]))
            addr_updates.append(f"-RemoteAddress @({remote_addr_str})")
        if addr_updates:
            ps_parts.append(f"Get-NetFirewallRule -Name '{rule_name}' | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter {' '.join(addr_updates)}")

        # Modify application filters if program/service specified
        app_updates = []
        if program is not None:
            app_updates.append(f"-Program '{program}'")
        if service is not None:
            app_updates.append(f"-Service '{service}'")
        if app_updates:
            ps_parts.append(f"Get-NetFirewallRule -Name '{rule_name}' | Get-NetFirewallApplicationFilter | Set-NetFirewallApplicationFilter {' '.join(app_updates)}")

        if ps_parts:
            script = "; ".join(ps_parts)
            res = self.machine.command.run(script, powershell=True, auto_retry=False)
            if res.returncode != 0:
                raise FirewallError(f"Failed to modify firewall rule '{rule_name}': {res.stderr.strip()}")

        target_lookup = new_name if new_name else rule_name
        updated = self.get_rule(target_lookup)
        if not updated:
            raise FirewallError(f"Firewall rule '{target_lookup}' not found after modification.")
        return updated

    def remove_rule(self, name_or_rule: str | FirewallRule) -> bool:
        """Remove/delete a firewall rule by name or FirewallRule object."""
        rule_name = name_or_rule.name if isinstance(name_or_rule, FirewallRule) else str(name_or_rule)
        ps_script = f"Remove-NetFirewallRule -Name '{rule_name}' -Confirm:$false -ErrorAction Stop"
        res = self.machine.command.run(ps_script, powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to remove firewall rule '{rule_name}': {res.stderr.strip()}")
        return True

    def delete_rule(self, name_or_rule: str | FirewallRule) -> bool:
        """Alias for remove_rule()."""
        return self.remove_rule(name_or_rule)

    def enable_rule(self, name_or_rule: str | FirewallRule) -> bool:
        """Enable a firewall rule."""
        rule_name = name_or_rule.name if isinstance(name_or_rule, FirewallRule) else str(name_or_rule)
        ps_script = f"Enable-NetFirewallRule -Name '{rule_name}' -ErrorAction Stop"
        res = self.machine.command.run(ps_script, powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to enable firewall rule '{rule_name}': {res.stderr.strip()}")
        return True

    def disable_rule(self, name_or_rule: str | FirewallRule) -> bool:
        """Disable a firewall rule."""
        rule_name = name_or_rule.name if isinstance(name_or_rule, FirewallRule) else str(name_or_rule)
        ps_script = f"Disable-NetFirewallRule -Name '{rule_name}' -ErrorAction Stop"
        res = self.machine.command.run(ps_script, powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise FirewallError(f"Failed to disable firewall rule '{rule_name}': {res.stderr.strip()}")
        return True

    def __repr__(self) -> str:
        return f"<FirewallController status={self.status()!r}>"
