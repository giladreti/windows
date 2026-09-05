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
