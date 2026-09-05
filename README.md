# windows 🚀

Automated, hands-free Windows QEMU virtual machine provisioning, instant base image caching with thin disk overlays, out-of-band QEMU Guest Agent (QGA) execution, high-level Windows abstractions (Processes, Registry, Services), `pathlib.Path`-style remote file/directory transfers, and QEMU/`ntoseye` WinDbg kernel debugging.

---

## 🌟 Key Features

- ⚡ **Instant QEMU Disk Overlays**: Creates thin Copy-on-Write `.qcow2` overlay disks (~200 KB) backing cached base images in milliseconds. Auto-generates unique randomized filenames if omitted.
- 🔌 **Pure Out-of-Band QEMU Guest Agent (QGA)**: 100% networkless, zero TCP ports, zero firewall dependencies. Communicates directly over the hypervisor VirtIO serial channel (`\\.\Global\org.qemu.guest_agent.0` <-> Host UNIX domain socket).
- 🧩 **High-Level Typed Windows Subsystems**:
  - **`machine.processes`**: List, inspect, and kill running Windows processes (`PID`, `Name`, `CPU`, `WorkingSet MB`).
  - **`machine.registry`**: Query, set, and delete keys and values across `HKLM`, `HKCU`, etc.
  - **`machine.services`**: Inspect, start, stop, restart, and configure startup types for Windows services.
- 📸 **Live & Offline Snapshots (`machine.snapshot`)**:
  - Take live snapshots (`savevm`) saving RAM + disk state, or offline disk snapshots.
  - Instant rollback to any snapshot checkpoint (`machine.snapshot.revert()`).
  - **Fork Machine from Snapshot**: Extract any snapshot into an independent standalone VM (`machine.snapshot.fork()`).
- 📂 **Pythonic `pathlib.Path`-Style File Transfer (`RemotePath`)**:
  - Manipulate remote guest paths with familiar syntax (`machine.file.path(...)` or `machine.file / "C:\\path"`).
  - Direct text/byte reading and writing (`read_text()`, `write_text()`, `read_bytes()`, `write_bytes()`) via QGA file streaming.
  - Full recursive directory uploads and downloads with configurable exist policies (`overwrite`, `merge`, `abort`).
- 🪟 **Kernel Debugging with `ntoseye` (WinDbg for Linux)**:
  - Integrated with [`dmaivel/ntoseye`](https://github.com/dmaivel/ntoseye).
  - Start VMs with QEMU GDB stub active (`machine.debug(backend="ntoseye", gdb_port=1234)`).
  - Execute WinDbg commands (`r`, `k`, `u`, `!process 0 0`, `bp`) from your Linux terminal or Python SDK.
- 🛠️ **QEMU Monitor & VNC Display**:
  - Start VMs stopped at boot (`-S`) with automated startup monitor scripts.
  - Native VNC console with **1:1 pixel-perfect cursor tracking** via USB tablet device (`-device qemu-xhci -device usb-tablet`).
- 📦 **Automated ISO Resolution & Unattended Setup**:
  - Download official Windows ISOs using standard enums (`WindowsVersion.WIN10_22H2`, `WindowsVersion.WIN11_25H2`).
  - Fully automated installation via generated `autounattend.xml` and driver injection bypassing OOBE and hardware checks.
- 🌐 **Virtual Networking, Hotplugging & Switches (`machine.network`, `VirtualSwitch`)**:
  - Hotplug and remove NICs on running VMs with custom hardware models (`NICModel.E1000E`, `NICModel.VIRTIO_NET_PCI`, `NICModel.RTL8139`, etc.).
  - Configure static IP, subnet masks, default gateway, and DNS servers inside Windows over QGA.
  - Multi-VM virtual switching (`VirtualSwitch`) using isolated userspace multicast sockets (no root required) or Linux kernel bridges.
- 🦈 **Packet Capture & Live Wireshark (`nic.capture()`, `switch.capture()`)**:
  - Direct Layer 2/3 packet capture to `.pcap` files using QEMU's `filter-dump` subsystem without host root permissions.
  - Live traffic inspection via Wireshark (`nic.wireshark()`).
- 📸 **Live Screen Capture**: Capture live high-resolution PNG screenshots of the VM display directly over the QEMU monitor socket.

---

## 📦 Installation

```bash
# Clone the repository
git clone https://github.com/giladreti/windows.git
cd windows

# Install dependencies using uv
uv sync
```

---

## 🚀 Quickstart Example

```python
import windows
from pathlib import Path
from windows import ISO, Image, Machine, WindowsVersion

# 1. Fetch / resolve Windows ISO
iso = ISO.from_version(WindowsVersion.WIN10_22H2)

# 2. Provision VM image (uses cached base image + creates instant overlay disk)
image = Image.from_iso(
    iso=iso,
    output_disk=None,  # Automatically generates unique random filename
    use_cache=True,
)

# 3. Create Machine instance
machine = Machine(image)

# 4. Start VM and wait for QGA interface
machine.run(timeout=180)

# 5. High-Level Subsystems: Processes, Registry, Services
# Processes:
for proc in machine.processes.list():
    if "explorer" in proc.name.lower():
        print(f"Explorer PID: {proc.pid}, Memory: {proc.working_set_mb} MB")

# Registry:
machine.registry.set_value(r"HKCU:\Software\MyApp", "Enabled", 1, value_type="DWord")
val = machine.registry.get_value(r"HKCU:\Software\MyApp", "Enabled")
print("Registry value:", val)

# Services:
svc = machine.services.get("wuauserv")
print(f"Windows Update Service status: {svc.status if svc else 'Unknown'}")

# 6. Path-style File & Directory Transfers via QGA
config_file = machine.file / r"C:\Users\Public\config.json"
config_file.write_text('{"status": "active", "environment": "test"}')
print("Config content:", config_file.read_text())

# Upload local directory to guest
local_dir = Path("./local_assets")
remote_dir = machine.file.path(r"C:\Users\Public\assets")
remote_dir.upload(local_dir, exist_policy="overwrite")

# 7. Clean shutdown
machine.power.off()
```

---

## 💾 Offline Disk & Filesystem Analysis (`image.file`)

Inspect partition tables and read or write files directly in offline Windows images using the same path-like interface as `machine.file`:

```python
from windows import Image

image = Image("windows.qcow2")

# 1. Inspect partitions
for part in image.partitions():
    print(f"Partition #{part.index}: {part.type_name}, {part.size / (1024**3):.1f} GB")

# 2. Path-like file reading & writing without booting the VM
hosts = image.file / r"C:\Windows\System32\drivers\etc\hosts"
if hosts.exists():
    print("Hosts file content:\n", hosts.read_text())

# 3. Inject scripts or configs directly into offline disk
script = image.file / r"C:\Users\Public\setup.bat"
script.write_text("@echo off\r\necho Offline Provisioned")

# 4. Directory traversal
for child in (image.file / r"C:\Windows").iterdir():
    print(child.name, "[DIR]" if child.is_dir() else "[FILE]")

# 5. High-performance batch mount context
with image.file as fs:
    (fs / r"C:\Users\Public\note1.txt").write_text("Batch note 1")
    (fs / r"C:\Users\Public\note2.txt").write_text("Batch note 2")
```

---

## 🌐 Virtual Networking, NIC Hotplug & Packet Capture

Hotplug network interfaces, configure guest IPs/DNS, interconnect VMs across virtual switches, and capture traffic to `.pcap`:

```python
from windows import Machine, VirtualSwitch, NICModel

# 1. Create an isolated virtual switch (zero root privileges needed!)
switch = VirtualSwitch("lab_switch")

# 2. Hotplug a secondary NIC connected to the switch
nic = machine.network.add(
    model=NICModel.E1000E,
    mac="52:54:00:12:34:56",
    switch=switch,
)

# 3. Configure guest static IP and DNS inside Windows
nic.configure(
    ip="192.168.100.10/24",
    gateway="192.168.100.1",
    dns=["8.8.8.8", "1.1.1.1"],
)

# 4. Capture traffic to PCAP or launch live Wireshark
with nic.capture("traffic.pcap"):
    machine.command.run("ping 192.168.100.1 -n 4")

# Or open live Wireshark:
# nic.wireshark()

# 5. Hot-unplug NIC
nic.remove()
```

---

## 🪟 Kernel Debugging Example (`ntoseye`)

```python
# Start machine in ntoseye debug mode (QEMU GDB stub active on port 1234)
machine.debug(
    backend="ntoseye",
    gdb_port=1234,
    pause_at_boot=False,
    open_console=True,
)

# Attach WinDbg-compatible REPL from your host terminal:
# --> ntoseye -b gdb --connect localhost:1234
```

---

## 🧪 Testing & Verification

```bash
# Run unit test suite (55+ tests, fully mocked)
uv run python -m pytest

# Run type checker
uv run ty check

# Run linter and formatter
uv run ruff check .
uv run ruff format .

# Run end-to-end workflow demo
uv run python3 examples/demo_workflow.py
```

---

## 📄 License

MIT License. Copyright (c) Gilad Reti.
