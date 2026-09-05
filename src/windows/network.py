"""Virtual networking, NIC hotplugging, in-guest IP configuration, virtual switching, and packet capture."""

import ipaddress
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from windows.qemu import find_free_port

if TYPE_CHECKING:
    from windows.machine import Machine


class NetworkError(RuntimeError):
    """Exception raised for network controller or configuration errors."""


class NICModel(StrEnum):
    """Supported virtual network interface card models in QEMU."""

    E1000E = "e1000e"  # Intel 82574L Gigabit - native in-box Windows 10/11 drivers (Default)
    E1000 = "e1000"  # Intel 82540EM Gigabit
    RTL8139 = "rtl8139"  # Realtek 8139 10/100 Mbps
    VIRTIO = "virtio-net-pci"  # High-performance VirtIO (requires VirtIO guest driver)
    VIRTIO_NET_PCI = "virtio-net-pci"
    VMXNET3 = "vmxnet3"  # VMware VMXNET3 virtual NIC


def generate_mac(prefix: str = "52:54:00") -> str:
    """Generate a random valid unicast MAC address with standard QEMU OUI prefix."""
    octets = [f"{random.randint(0x00, 0xFF):02x}" for _ in range(3)]
    return f"{prefix}:{':'.join(octets)}"


def normalize_mac(mac: str) -> str:
    """Normalize MAC address string to standard uppercase colon-separated format."""
    cleaned = mac.replace("-", "").replace(":", "").replace(".", "").upper()
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac}")
    return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))


class QMPClient:
    """Client for executing structured JSON commands over QEMU Machine Protocol (QMP) UNIX socket."""

    def __init__(self, socket_path: Path | str):
        self.socket_path = Path(socket_path)

    def execute(self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        """Send a QMP command and return the parsed JSON return object."""
        if not self.socket_path.exists():
            raise NetworkError(f"QMP socket does not exist or VM is not running: {self.socket_path}")

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            try:
                s.connect(str(self.socket_path))
            except OSError as exc:
                raise NetworkError(f"Failed to connect to QMP socket at {self.socket_path}: {exc}") from exc

            f = s.makefile("r", encoding="utf-8")
            # 1. Read QMP greeting banner: {"QMP": ...}
            f.readline()

            # 2. Negotiate capabilities
            s.sendall(json.dumps({"execute": "qmp_capabilities"}).encode("utf-8") + b"\n")
            f.readline()

            # 3. Execute requested command
            payload: dict[str, Any] = {"execute": command}
            if arguments:
                payload["arguments"] = arguments
            s.sendall(json.dumps(payload).encode("utf-8") + b"\n")

            # 4. Read response
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    data = json.loads(line)
                    if "return" in data:
                        return data["return"]
                    if "error" in data:
                        err_desc = data["error"].get("desc", str(data["error"]))
                        raise NetworkError(f"QMP command '{command}' failed: {err_desc}")
                except json.JSONDecodeError:
                    continue

        return {}


class PacketCapture:
    """Context manager controlling active packet capture session dumping to a .pcap file."""

    def __init__(
        self,
        output_path: str | Path = "capture.pcap",
        qmp_client: QMPClient | None = None,
        netdev_id: str | None = None,
        process: subprocess.Popen | None = None,
        live: bool = False,
    ):
        self.output_path = Path(output_path).resolve()
        if not self.output_path.suffix:
            self.output_path = self.output_path.with_suffix(".pcap")
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        self._qmp = qmp_client
        self._netdev_id = netdev_id
        self._filter_id = f"dump_{uuid.uuid4().hex[:8]}" if netdev_id else None
        self._process = process
        self._live = live
        self._wireshark_proc: subprocess.Popen | None = None
        self.is_active: bool = False

    @property
    def pcap_path(self) -> Path:
        """Convenience alias for output_path."""
        return self.output_path

    def start(self) -> "PacketCapture":
        """Start packet capture."""
        if self.is_active:
            return self

        if self._qmp and self._netdev_id:
            try:
                self._qmp.execute(
                    "object-add",
                    {
                        "qom-type": "filter-dump",
                        "id": self._filter_id,
                        "netdev": self._netdev_id,
                        "file": str(self.output_path),
                    },
                )
            except Exception as exc:
                raise NetworkError(f"Failed to start QEMU filter-dump on netdev '{self._netdev_id}': {exc}") from exc

        self.is_active = True

        if self._live:
            self.wireshark()

        return self

    def stop(self) -> Path:
        """Stop packet capture and return the output .pcap path."""
        if not self.is_active:
            return self.output_path

        self.is_active = False

        if self._qmp and self._filter_id:
            try:
                self._qmp.execute("object-del", {"id": self._filter_id})
            except Exception:
                pass

        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2.0)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=2.0)
                except Exception:
                    try:
                        subprocess.run(["kill", "-9", str(self._process.pid)], capture_output=True)
                        self._process.wait(timeout=1.0)
                    except Exception:
                        pass
            self._process = None

        if self._wireshark_proc is not None:
            try:
                self._wireshark_proc.poll()
            except Exception:
                pass

        return self.output_path

    def wireshark(self) -> subprocess.Popen | None:
        """Launch Wireshark to inspect the captured packets."""
        ws_bin = shutil.which("wireshark")
        if not ws_bin:
            return None

        # If capture file exists, open it directly in Wireshark
        try:
            self._wireshark_proc = subprocess.Popen(
                [ws_bin, "-k", "-i", str(self.output_path)]
                if os.path.exists(self.output_path)
                else [ws_bin, str(self.output_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return self._wireshark_proc
        except Exception:
            return None

    def __enter__(self) -> "PacketCapture":
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()

    def __repr__(self) -> str:
        status = "active" if self.is_active else "stopped"
        return f"<PacketCapture path={str(self.output_path)!r} status={status!r}>"


class PCAPDumper:
    """Writes standard libpcap format files (.pcap)."""

    def __init__(self, file_obj: Any):
        self.file = file_obj
        # PCAP Global Header: magic 0xa1b2c3d4, v2.4, thiszone=0, sigfigs=0, snaplen=65535, network=1 (Ethernet)
        self.file.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        self.file.flush()

    def write_packet(self, data: bytes) -> None:
        ts = time.time()
        sec = int(ts)
        usec = int((ts - sec) * 1_000_000)
        length = len(data)
        self.file.write(struct.pack("<IIII", sec, usec, length, length))
        self.file.write(data)
        self.file.flush()


class SwitchPacketCapture(PacketCapture):
    """Context manager for switch packet capture session in userspace socket mode."""

    def __init__(self, switch: "VirtualSwitch", output_path: str | Path = "switch.pcap", live: bool = False):
        super().__init__(output_path=output_path, live=live)
        self.switch = switch
        self._file: Any = None

    def start(self) -> "SwitchPacketCapture":
        if self.is_active:
            return self
        self._file = open(self.output_path, "wb")
        self.switch.start_pcap(self._file)
        self.is_active = True
        if self._live:
            self.wireshark()
        return self

    def stop(self) -> Path:
        if not self.is_active:
            return self.output_path
        self.is_active = False
        self.switch.stop_pcap()
        if self._file:
            self._file.close()
            self._file = None
        if self._wireshark_proc is not None:
            try:
                self._wireshark_proc.poll()
            except Exception:
                pass
        return self.output_path


class NetworkInterface:
    """Represents a virtual network interface card (NIC) attached to a Windows VM."""

    def __init__(
        self,
        controller: "NetworkController",
        id: str,
        netdev_id: str,
        model: NICModel | str = NICModel.E1000E,
        mac: str | None = None,
        switch: "VirtualSwitch | None" = None,
        bus: str | None = None,
        client_port: int | None = None,
        tap_name: str | None = None,
    ):
        self.controller = controller
        self.id = id
        self.netdev_id = netdev_id
        self.model = NICModel(model) if isinstance(model, str) else model
        self.mac = normalize_mac(mac if mac else generate_mac())
        self.switch = switch
        self.bus = bus
        self._client_port = client_port
        self._tap_name = tap_name

    @property
    def machine(self) -> "Machine":
        return self.controller.machine

    def configure(
        self,
        ip: str,
        gateway: str | None = None,
        dns: str | Sequence[str] | None = None,
    ) -> None:
        """Configure static IP address, subnet mask/prefix, default gateway, and DNS inside the guest VM.

        Args:
            ip: IPv4 address, optionally with CIDR prefix (e.g. '192.168.100.10/24' or '192.168.100.10').
            gateway: Optional default gateway IPv4 address (e.g. '192.168.100.1').
            dns: Optional primary DNS or sequence of DNS servers (e.g. ['8.8.8.8', '1.1.1.1']).
        """
        if "/" in ip:
            interface = ipaddress.ip_interface(ip)
            ip_addr = str(interface.ip)
            prefix_len = interface.network.prefixlen
        else:
            ip_addr = str(ipaddress.ip_address(ip))
            prefix_len = 24

        target_mac = self.mac
        gw_arg = f"-DefaultGateway '{gateway}'" if gateway else ""

        ps_script = f"""
$targetMac = '{target_mac}'
$adapter = $null
for ($i = 0; $i -lt 15; $i++) {{
    $adapter = Get-NetAdapter | Where-Object {{ ($_.MacAddress -replace '[:-]', '').ToUpper() -eq ($targetMac -replace '[:-]', '').ToUpper() }}
    if ($adapter) {{ break }}
    Start-Sleep -Seconds 1
}}
if (-not $adapter) {{
    throw "Network adapter with MAC $targetMac not found inside Windows guest."
}}

# Disable DHCP
Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -Dhcp Disabled -Confirm:$false

# Clear existing non-link-local IPv4 addresses
Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {{ $_.IPAddress -notlike '169.254.*' }} |
    Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue

# Configure new static IP
New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress '{ip_addr}' -PrefixLength {prefix_len} {gw_arg} -Confirm:$false
"""
        if dns:
            if isinstance(dns, str):
                dns_list = [dns]
            else:
                dns_list = list(dns)
            dns_formatted = ", ".join(f"'{d}'" for d in dns_list)
            ps_script += f"""
Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ServerAddresses @({dns_formatted}) -Confirm:$false
"""

        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise NetworkError(f"Failed to configure guest IP on NIC {self.id} ({self.mac}): {res.stderr.strip()}")

    def set_dhcp(self) -> None:
        """Re-enable DHCP and dynamic DNS resolution on this network adapter inside the Windows guest."""
        ps_script = f"""
$targetMac = '{self.mac}'
$adapter = Get-NetAdapter | Where-Object {{ ($_.MacAddress -replace '[:-]', '').ToUpper() -eq ($targetMac -replace '[:-]', '').ToUpper() }}
if ($adapter) {{
    Set-NetIPInterface -InterfaceIndex $adapter.ifIndex -Dhcp Enabled -Confirm:$false
    Set-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -ResetServerAddresses -Confirm:$false
}}
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        if res.returncode != 0:
            raise NetworkError(f"Failed to set DHCP on NIC {self.id}: {res.stderr.strip()}")

    def get_guest_config(self) -> dict[str, Any]:
        """Query current in-guest network configuration (IP addresses, gateway, DNS, status) via QGA."""
        ps_script = f"""
$targetMac = '{self.mac}'
$adapter = Get-NetAdapter | Where-Object {{ ($_.MacAddress -replace '[:-]', '').ToUpper() -eq ($targetMac -replace '[:-]', '').ToUpper() }}
if ($adapter) {{
    $ips = (Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue | Where-Object {{ $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike 'fe80*' }} | Select-Object -ExpandProperty IPAddress)
    $dns = (Get-DnsClientServerAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ServerAddresses)
    $routes = (Get-NetRoute -InterfaceIndex $adapter.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty NextHop)
    [PSCustomObject]@{{
        Name = $adapter.Name
        InterfaceIndex = $adapter.ifIndex
        MacAddress = $adapter.MacAddress
        Status = $adapter.Status
        IPAddresses = @($ips)
        Gateway = if ($routes) {{ $routes[0] }} else {{ $null }}
        DNSServers = @($dns)
    }} | ConvertTo-Json -Compress
}} else {{
    '{{}}'
}}
"""
        res = self.machine.command.run(ps_script.strip(), powershell=True, auto_retry=False)
        out = res.stdout.strip()
        if out and out != "{}":
            try:
                return json.loads(out)
            except Exception:
                pass
        return {}

    def capture(self, output_path: str | Path = "capture.pcap", live: bool = False) -> PacketCapture:
        """Start capturing raw Layer-2 Ethernet packets on this NIC into a .pcap file.

        Usage:
            with nic.capture("traffic.pcap"):
                machine.command.run("ping 10.0.0.2")
        """
        cap = PacketCapture(
            output_path=output_path,
            qmp_client=self.controller.qmp,
            netdev_id=self.netdev_id,
            live=live,
        )
        return cap

    def wireshark(self) -> subprocess.Popen | None:
        """Launch Wireshark live viewer for this network interface."""
        pcap_file = self.machine.image.disk_path.parent / f"{self.id}_capture.pcap"
        cap = self.capture(output_path=pcap_file, live=True)
        cap.start()
        return cap._wireshark_proc

    def set_profile(self, profile: "str | Any") -> None:
        """Set the network firewall category/profile for this network interface (e.g. 'Private', 'Public')."""
        self.machine.firewall.set_profile(self, profile)

    def get_profile(self) -> str:
        """Get the current network firewall category/profile for this network interface."""
        return self.machine.firewall.get_profile(self)

    def remove(self) -> None:
        """Hot-unplug this network interface from the virtual machine."""
        self.controller.remove(self)

    def __repr__(self) -> str:
        sw_str = f" switch={self.switch.name!r}" if self.switch else ""
        bus_str = f" bus={self.bus!r}" if self.bus else ""
        return f"<NetworkInterface id={self.id!r} model={self.model.value!r} mac={self.mac!r}{bus_str}{sw_str}>"


class VirtualSwitch:
    """Virtual network switch connecting multiple Windows VMs together over an isolated Layer-2 network."""

    _allocated_ports: set[int] = set()

    def __init__(
        self,
        name: str = "vswitch0",
        mode: Literal["auto", "socket", "bridge", "mcast"] = "auto",
        mcast_addr: str = "230.0.0.1",
        mcast_port: int | None = None,
    ):
        self.name = name
        self.mcast_addr = mcast_addr
        self.mcast_port = mcast_port if mcast_port is not None else (30000 + (hash(name) % 25000))

        # Determine mode
        if mode == "auto":
            if self._can_create_linux_bridge():
                self.mode = "bridge"
            else:
                self.mode = "socket"
        else:
            self.mode = mode

        self.bridge_name = self.name
        self.attached_nics: list[NetworkInterface] = []
        self._created_bridge = False
        self._created_taps: list[str] = []
        self._pcap_dumper: PCAPDumper | None = None

        if self.mode == "bridge":
            self._setup_linux_bridge()
        elif self.mode == "socket":
            self.switch_port = find_free_port(40000, exclude=VirtualSwitch._allocated_ports)
            VirtualSwitch._allocated_ports.add(self.switch_port)
            self._client_ports: set[int] = set()
            self._running = True
            self._lock = threading.Lock()
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", self.switch_port))
            self._sock.setblocking(False)
            self._thread = threading.Thread(target=self._hub_loop, daemon=True)
            self._thread.start()
        elif self.mode == "mcast":
            if mcast_port is not None:
                self.mcast_port = mcast_port
            else:
                port = 30000 + (hash(name) % 25000)
                while port in VirtualSwitch._allocated_ports:
                    port += 1
                VirtualSwitch._allocated_ports.add(port)
                self.mcast_port = port

    def _can_create_linux_bridge(self) -> bool:
        """Return True if user has permissions to create a Linux bridge."""
        if os.geteuid() == 0:
            return True
        return False

    def _setup_linux_bridge(self) -> None:
        """Create Linux kernel bridge using ip link."""
        res = subprocess.run(["ip", "link", "show", self.bridge_name], capture_output=True)
        if res.returncode != 0:
            res_add = subprocess.run(["ip", "link", "add", self.bridge_name, "type", "bridge"], capture_output=True)
            if res_add.returncode == 0:
                subprocess.run(["ip", "link", "set", self.bridge_name, "up"], capture_output=True)
                self._created_bridge = True
            else:
                # Fallback to userspace socket mode if bridge creation failed
                self.mode = "socket"

    def _hub_loop(self) -> None:
        """Forward Ethernet packets between all client ports in userspace socket mode."""
        import select

        while getattr(self, "_running", False):
            try:
                r, _, _ = select.select([self._sock], [], [], 0.2)
                if not r or not getattr(self, "_running", False):
                    continue
                data, addr = self._sock.recvfrom(65535)
                src_port = addr[1]
                with self._lock:
                    if self._pcap_dumper is not None:
                        try:
                            self._pcap_dumper.write_packet(data)
                        except Exception:
                            pass
                    if src_port not in self._client_ports:
                        self._client_ports.add(src_port)
                    for p in list(self._client_ports):
                        if p != src_port:
                            try:
                                self._sock.sendto(data, ("127.0.0.1", p))
                            except OSError:
                                pass
            except Exception:
                if not getattr(self, "_running", False):
                    break

    def register_client_port(self) -> int:
        """Allocate and register a unique client UDP port for a VM NIC connecting to this switch."""
        port = find_free_port(40000, exclude=VirtualSwitch._allocated_ports)
        VirtualSwitch._allocated_ports.add(port)
        if hasattr(self, "_lock"):
            with self._lock:
                self._client_ports.add(port)
        return port

    def unregister_client_port(self, port: int) -> None:
        """Unregister and release a client UDP port."""
        if hasattr(self, "_lock"):
            with self._lock:
                self._client_ports.discard(port)
        VirtualSwitch._allocated_ports.discard(port)

    def start_pcap(self, file_obj: Any) -> None:
        """Start streaming packets received by this switch to a PCAPDumper."""
        if hasattr(self, "_lock"):
            with self._lock:
                self._pcap_dumper = PCAPDumper(file_obj)

    def stop_pcap(self) -> None:
        """Stop streaming packets to the PCAPDumper."""
        if hasattr(self, "_lock"):
            with self._lock:
                self._pcap_dumper = None

    def assign_host_ip(self, ip: str) -> None:
        """Assign an IP address to the host Linux bridge interface (e.g. '192.168.100.1/24')."""
        if self.mode != "bridge":
            raise NetworkError(f"assign_host_ip is only supported in 'bridge' mode, current mode is '{self.mode}'")
        res = subprocess.run(["ip", "addr", "add", ip, "dev", self.bridge_name], capture_output=True, text=True)
        if res.returncode != 0 and "File exists" not in res.stderr:
            raise NetworkError(f"Failed to assign IP {ip} to bridge {self.bridge_name}: {res.stderr.strip()}")
        subprocess.run(["ip", "link", "set", self.bridge_name, "up"], capture_output=True)

    def create_tap_device(self, prefix: str = "tap") -> str:
        """Create a Linux TAP device attached to this bridge for a VM interface."""
        if self.mode != "bridge":
            raise NetworkError(f"create_tap_device is only supported in 'bridge' mode, current mode is '{self.mode}'")
        idx = len(self._created_taps)
        safe_name = self.name.replace("-", "")[:4]
        safe_prefix = prefix.replace("-", "")[:4]
        tap_name = f"t_{safe_name}_{safe_prefix}{idx}"[:15]
        res = subprocess.run(["ip", "tuntap", "add", "dev", tap_name, "mode", "tap"], capture_output=True, text=True)
        if res.returncode != 0:
            raise NetworkError(f"Failed to create TAP device {tap_name}: {res.stderr.strip()}")
        subprocess.run(["ip", "link", "set", tap_name, "master", self.bridge_name], capture_output=True)
        subprocess.run(["ip", "link", "set", tap_name, "up"], capture_output=True)
        self._created_taps.append(tap_name)
        return tap_name

    def remove_tap_device(self, tap_name: str) -> None:
        """Delete a Linux TAP device."""
        subprocess.run(["ip", "link", "set", tap_name, "down"], capture_output=True)
        subprocess.run(["ip", "tuntap", "del", "dev", tap_name, "mode", "tap"], capture_output=True)
        if tap_name in self._created_taps:
            self._created_taps.remove(tap_name)

    def connect(self, nic: NetworkInterface) -> None:
        """Register a NIC as connected to this switch."""
        if nic not in self.attached_nics:
            self.attached_nics.append(nic)
            nic.switch = self

    def disconnect(self, nic: NetworkInterface) -> None:
        """Unregister a NIC from this switch."""
        if nic in self.attached_nics:
            self.attached_nics.remove(nic)
            nic.switch = None

    def capture(self, output_path: str | Path = "switch.pcap", live: bool = False) -> PacketCapture:
        """Capture all traffic passing through this virtual switch into a .pcap file."""
        output_path = Path(output_path).resolve()
        if self.mode == "bridge":
            # Capture directly on bridge interface via tcpdump or dumpcap
            tool = shutil.which("tcpdump") or shutil.which("dumpcap")
            if tool:
                cmd = [tool, "-i", self.bridge_name, "-w", str(output_path), "-U"]
                if "tcpdump" in tool and os.geteuid() == 0:
                    cmd.extend(["-Z", "root"])
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return PacketCapture(output_path=output_path, process=proc, live=live)

        if self.mode == "socket":
            return SwitchPacketCapture(self, output_path=output_path, live=live)

        # In mcast mode: capture on primary attached NIC or first available NIC
        if self.attached_nics:
            return self.attached_nics[0].capture(output_path=output_path, live=live)

        # Fallback empty capture
        return PacketCapture(output_path=output_path, live=live)

    def wireshark(self) -> subprocess.Popen | None:
        """Open Wireshark inspecting this virtual switch."""
        pcap_file = Path(f"{self.name}_traffic.pcap")
        cap = self.capture(pcap_file, live=True)
        cap.start()
        return cap._wireshark_proc

    def destroy(self) -> None:
        """Tear down switch resources."""
        if hasattr(self, "_running") and self._running:
            self._running = False
            if hasattr(self, "_sock"):
                try:
                    self._sock.close()
                except OSError:
                    pass
            if hasattr(self, "_thread") and self._thread.is_alive():
                self._thread.join(timeout=1.0)
            if hasattr(self, "switch_port"):
                VirtualSwitch._allocated_ports.discard(self.switch_port)
            if hasattr(self, "_client_ports"):
                for p in list(self._client_ports):
                    VirtualSwitch._allocated_ports.discard(p)

        if hasattr(self, "_created_taps"):
            for tap in list(self._created_taps):
                self.remove_tap_device(tap)

        if self._created_bridge:
            subprocess.run(["ip", "link", "set", self.bridge_name, "down"], capture_output=True)
            subprocess.run(["ip", "link", "del", self.bridge_name], capture_output=True)
            self._created_bridge = False

        if hasattr(self, "mcast_port") and self.mcast_port in VirtualSwitch._allocated_ports:
            VirtualSwitch._allocated_ports.discard(self.mcast_port)

    def __del__(self) -> None:
        self.destroy()

    def __repr__(self) -> str:
        return f"<VirtualSwitch name={self.name!r} mode={self.mode!r} nics={len(self.attached_nics)}>"


class NetworkController:
    """Controller for managing virtual network interfaces, hotplugging, and packet capture on a Windows VM."""

    def __init__(self, machine: "Machine"):
        self.machine = machine
        self._qmp = QMPClient(machine.qmp_socket_path)
        self._nics: dict[str, NetworkInterface] = {}
        self._counter: int = 0

        # Register initial default NIC (nic0 / net0) defined at startup
        default_nic = NetworkInterface(
            controller=self,
            id="nic0",
            netdev_id="net0",
            model=NICModel.E1000,
            mac="52:54:00:12:34:50",
            bus="pcie.0",
        )
        self._nics["nic0"] = default_nic

    @property
    def qmp(self) -> QMPClient:
        return self._qmp

    def list(self) -> list[NetworkInterface]:
        """List all network interfaces configured on this VM."""
        return list(self._nics.values())

    @property
    def default(self) -> NetworkInterface:
        """Return the default primary network interface (nic0)."""
        return self._nics["nic0"]

    def __getitem__(self, key: str | int) -> NetworkInterface:
        if isinstance(key, int):
            return self.list()[key]
        if key in self._nics:
            return self._nics[key]
        for nic in self._nics.values():
            if nic.mac.lower() == str(key).lower():
                return nic
        raise KeyError(f"Network interface '{key}' not found. Available: {list(self._nics.keys())}")

    def find_available_pci_bus(self, is_pcie: bool = True) -> str | None:
        """Find an available PCIe root port or PCI bridge bus for hotplugging."""
        busy_buses = {nic.bus for nic in self._nics.values() if nic.bus}
        pci_buses = []
        try:
            res = self.qmp.execute("query-pci")
            if isinstance(res, list):
                pci_buses = res
        except Exception:
            pci_buses = []

        candidate_root_port: str | None = None
        candidate_pci_bridge: str | None = None

        for bus in pci_buses:
            if not isinstance(bus, dict):
                continue
            for dev in bus.get("devices", []):
                if not isinstance(dev, dict):
                    continue
                qdev_id = dev.get("qdev_id", "")
                if not qdev_id:
                    continue
                bridge = dev.get("pci_bridge")
                if bridge is not None and isinstance(bridge, dict):
                    subdevices = bridge.get("devices", [])
                    desc = dev.get("class_info", {}).get("desc", "").lower()
                    is_rp = "rp" in qdev_id.lower() or "root" in desc
                    if is_rp:
                        if len(subdevices) == 0 and qdev_id not in busy_buses:
                            if candidate_root_port is None:
                                candidate_root_port = qdev_id
                    else:
                        if len(subdevices) < 30:
                            candidate_pci_bridge = qdev_id

        if is_pcie and candidate_root_port:
            return candidate_root_port
        if candidate_pci_bridge:
            return candidate_pci_bridge
        if candidate_root_port:
            return candidate_root_port

        # If query-pci returned no results (e.g. mock QMP environment), fallback to rpX or pci.1
        if not pci_buses:
            for i in range(1, 9):
                rp_name = f"rp{i}"
                if rp_name not in busy_buses:
                    return rp_name
            return "pci.1"

        return None

    def add(
        self,
        model: NICModel | str = NICModel.E1000E,
        mac: str | None = None,
        switch: VirtualSwitch | None = None,
        isolated: bool = False,
        id: str | None = None,
        bus: str | None = None,
    ) -> NetworkInterface:
        """Hotplug a new network interface card into the running or stopped virtual machine.

        Args:
            model: NIC device model (e.g. NICModel.E1000E, NICModel.VIRTIO, NICModel.RTL8139).
            mac: Optional MAC address. Automatically generated if omitted.
            switch: Optional VirtualSwitch to connect this NIC to.
            isolated: If True and switch is None, creates an isolated network backend without internet/DHCP.
            id: Optional device identifier (auto-generated e.g. 'nic1' if omitted).
            bus: Optional target PCI bus (e.g. 'rp1', 'pci.1'). Automatically discovered if omitted.

        Returns:
            The newly created and hotplugged `NetworkInterface` instance.
        """
        self._counter += 1
        nic_id = id if id else f"nic{self._counter}"
        netdev_id = f"net{self._counter}"
        actual_model = NICModel(model) if isinstance(model, str) else model
        actual_mac = normalize_mac(mac if mac else generate_mac())

        # Configure backend netdev
        netdev_args: dict[str, Any] = {"id": netdev_id}
        client_port: int | None = None
        tap_name: str | None = None

        if switch is not None:
            if switch.mode == "socket":
                client_port = switch.register_client_port()
                netdev_args["type"] = "socket"
                netdev_args["udp"] = f"127.0.0.1:{switch.switch_port}"
                netdev_args["localaddr"] = f"127.0.0.1:{client_port}"
            elif switch.mode == "bridge":
                if os.geteuid() == 0:
                    tap_name = switch.create_tap_device(prefix=nic_id)
                    netdev_args["type"] = "tap"
                    netdev_args["ifname"] = tap_name
                    netdev_args["script"] = "no"
                    netdev_args["downscript"] = "no"
                else:
                    netdev_args["type"] = "bridge"
                    netdev_args["br"] = switch.bridge_name
            elif switch.mode == "mcast":
                netdev_args["type"] = "socket"
                netdev_args["mcast"] = f"{switch.mcast_addr}:{switch.mcast_port}"
        elif isolated:
            # Hub / socket without peers creates an isolated interface
            netdev_args["type"] = "hubport"
            netdev_args["hubid"] = 100 + self._counter
        else:
            netdev_args["type"] = "user"

        # Determine target PCI bus for device_add
        target_bus = bus
        if target_bus is None and self.machine.power.status == "running":
            is_pcie_model = actual_model in (NICModel.E1000E, NICModel.VIRTIO, NICModel.VIRTIO_NET_PCI)
            target_bus = self.find_available_pci_bus(is_pcie=is_pcie_model)

        # If VM is currently running, perform QMP hotplug
        if self.machine.power.status == "running":
            self.qmp.execute("netdev_add", netdev_args)
            dev_args: dict[str, Any] = {
                "driver": actual_model.value,
                "netdev": netdev_id,
                "id": nic_id,
                "mac": actual_mac,
            }
            if target_bus:
                dev_args["bus"] = target_bus

            try:
                self.qmp.execute("device_add", dev_args)
            except Exception as exc:
                try:
                    self.qmp.execute("netdev_del", {"id": netdev_id})
                except Exception:
                    pass
                if client_port and switch:
                    switch.unregister_client_port(client_port)
                if tap_name and switch:
                    switch.remove_tap_device(tap_name)
                if "does not support hotplugging" in str(exc) or not target_bus:
                    raise NetworkError(
                        f"Failed to hotplug NIC device '{nic_id}': {exc}. "
                        "The machine must have PCIe root ports (e.g. 'pcie-root-port') or a PCI bridge ('pcie-pci-bridge') configured, "
                        "as QEMU q35's root bus 'pcie.0' does not support hotplugging."
                    ) from exc
                raise NetworkError(f"Failed to hotplug NIC device '{nic_id}': {exc}") from exc

        nic = NetworkInterface(
            controller=self,
            id=nic_id,
            netdev_id=netdev_id,
            model=actual_model,
            mac=actual_mac,
            switch=switch,
            bus=target_bus,
            client_port=client_port,
            tap_name=tap_name,
        )
        self._nics[nic_id] = nic

        if switch:
            switch.connect(nic)

        return nic

    def hotplug(
        self,
        model: NICModel | str = NICModel.E1000E,
        mac: str | None = None,
        switch: VirtualSwitch | None = None,
        isolated: bool = False,
        id: str | None = None,
        bus: str | None = None,
    ) -> NetworkInterface:
        """Alias for add(). Hotplug a new network interface into the VM."""
        return self.add(model=model, mac=mac, switch=switch, isolated=isolated, id=id, bus=bus)

    def remove(self, nic_or_id: NetworkInterface | str) -> None:
        """Hot-unplug a network interface from the virtual machine."""
        nic_id = nic_or_id.id if isinstance(nic_or_id, NetworkInterface) else str(nic_or_id)
        if nic_id not in self._nics:
            return

        nic = self._nics[nic_id]

        if self.machine.power.status == "running":
            try:
                self.qmp.execute("device_del", {"id": nic.id})
            except Exception:
                pass
            try:
                self.qmp.execute("netdev_del", {"id": nic.netdev_id})
            except Exception:
                pass

        if hasattr(nic, "_client_port") and nic._client_port and nic.switch:
            nic.switch.unregister_client_port(nic._client_port)
        if hasattr(nic, "_tap_name") and nic._tap_name and nic.switch:
            nic.switch.remove_tap_device(nic._tap_name)

        if nic.switch:
            nic.switch.disconnect(nic)

        del self._nics[nic_id]

    def capture_host(
        self,
        interface: str = "any",
        output_path: str | Path = "host.pcap",
        live: bool = False,
    ) -> PacketCapture:
        """Capture host network traffic using tcpdump or dumpcap into a .pcap file."""
        tool = shutil.which("tcpdump") or shutil.which("dumpcap")
        if not tool:
            raise NetworkError("Neither tcpdump nor dumpcap was found on PATH for host packet capture.")

        output_path = Path(output_path).resolve()
        cmd = [tool, "-i", interface, "-w", str(output_path), "-U"]
        if "tcpdump" in tool and os.geteuid() == 0:
            cmd.extend(["-Z", "root"])
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return PacketCapture(output_path=output_path, process=proc, live=live)

    def __repr__(self) -> str:
        return f"<NetworkController machine={self.machine.image.name!r} nics={len(self._nics)}>"
