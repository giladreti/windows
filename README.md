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

# 7. Hotplug Shared Folders directly as a guest drive letter (e.g. Z:)
with machine.file.share(from="./my_project", to="Z:") as share:
    res = machine.command.run("dir Z:\\")
    print(res.stdout)

# 8. Clean shutdown
machine.power.off()
```

---

## 👤 User Execution Contexts (`as_user`)

By default, commands run via QGA execute with `NT AUTHORITY\SYSTEM` privileges. You can run commands as specific users (e.g., `Administrator`) with password authentication using scoped context managers, controller views, or explicit arguments:

```python
# 1. Scoped execution block: all commands inside automatically run as Administrator
with machine.as_user():
    res = machine.command.run("whoami")
    print(res.stdout)  # -> desktop-xxxx\administrator

    # Internal subsystems or commands can bypass user scoping when needed:
    sys_res = machine.command.run("whoami", as_system=True)  # -> nt authority\system

# Outside context manager, execution automatically returns to SYSTEM:
print(machine.command.run("whoami").stdout)  # -> nt authority\system

# 2. Factory / View execution
admin = machine.as_user()
admin.run("whoami")  # -> desktop-xxxx\administrator

# 3. Explicit single command execution with credentials
machine.command.run("whoami", user="Administrator", password="Password123!")
```

See [`examples/user_execution.py`](examples/user_execution.py) for a complete example.

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

## ⏪ Time Travel Debugging (TTD) & Deterministic Replay

Record deterministic instruction-level execution sessions and replay them with forward and reverse stepping, bookmarks, exact seeking, and GDB/`ntoseye` inspection:

```python
from windows import ISO, Image, Machine, WindowsVersion

iso = ISO.from_version(WindowsVersion.WIN10_22H2)
image = Image.from_iso(iso, output_disk="ttd_vm.qcow2", use_cache=True)

with Machine(image, ttd=True) as machine:
    machine.power.on()
    machine.wait_for_boot()

    # 1. Record deterministic execution block
    with machine.record_session("exploit_trace") as recording:
        print(f"Tracing to: {recording.trace_path}")
        res = machine.command.run("whoami", powershell=False)
        print("Guest output:", res.stdout.strip())

    # 2. Replay with reverse stepping and bookmarks
    with machine.ttd.replay("exploit_trace") as session:
        # Step forward 5 instructions
        session.step(count=5)
        bm = session.add_bookmark("step5")

        # Step backward in time (reverse execution)
        session.reverse_step(count=3)

        # Deterministic seek to an exact instruction count
        session.seek(icount=100)
        session.goto_bookmark("step5")

# Attach GDB for reverse debugging:
# --> gdb -ex 'target remote localhost:<port>' -> reverse-stepi, reverse-continue
# Attach ntoseye for kernel object inspection:
# --> ntoseye -b gdb --connect localhost:<port> -> !process 0 0, k, dt nt!_IRP
```
See [`docs/time_travel_debugging.md`](docs/time_travel_debugging.md) for architecture, troubleshooting, and constraints.

---

## 🪟 Windows Host Compatibility

`windows` runs seamlessly on **Windows host machines** (as well as Linux and macOS):

- **Automatic `qemu.exe` Discovery**: Scans `PATH`, `C:\Program Files\qemu`, `%LOCALAPPDATA%\Programs\qemu`, Chocolatey, and Scoop.
- **Hardware Acceleration**: Automatically selects Windows Hypervisor Platform (`-accel whpx`) when available, falling back to multi-threaded TCG (`-accel tcg`).
- **Loopback TCP Sockets**: Because Windows lacks Unix domain socket parity in QEMU, IPC endpoints (QGA, QEMU Monitor, QMP) use automatically allocated loopback TCP ports (`127.0.0.1:<port>`).
- **Terminal Integration**: `machine.debug()` automatically opens Windows Terminal (`wt.exe`), PowerShell, or `cmd.exe`.
- **Shared Folders**: Mount host folders inside the guest as drive letters using `machine.file.share(from=host_path, to="Z:")`.

---

## 🔌 USB Flash Drive Emulation (`machine.usb`)

Create virtual disk images on the host and hotplug them as USB flash storage into a running Windows VM:

```python
from windows import Machine, create_usb_disk

# 1. Create formatted FAT32 USB disk image on host with pre-populated files
usb_img = create_usb_disk(
    path="my_flash_drive.img",
    size="64M",
    filesystem="fat32",
    label="MYUSB",
    files={"payload.txt": b"Hello from Host via USB!\n"},
)

# 2. Hotplug into running Windows VM
with machine.usb.mount(usb_img, to="E:") as usb_dev:
    print(f"Mounted USB device on Windows drive {usb_dev.drive_letter}")
    output = machine.command.run(f"Get-Content {usb_dev.drive_letter}\\payload.txt")
    print(output.stdout)

# Cleanly unmounted and removed upon exiting context
```

See [`examples/usb_storage.py`](examples/usb_storage.py) for details.

---

## 💿 CD-ROM Drive & ISO Media (`machine.cd`)

Create ISO 9660 / Joliet filesystem images on the host and insert them into the VM's CD-ROM drive:

```python
from windows import Machine, create_cdrom_iso

# 1. Create ISO image on host
cd_iso = create_cdrom_iso(
    path="install.iso",
    label="SETUP_DISC",
    files={"setup.exe": b"\x90\x90...", "readme.txt": "Installer notes\n"},
)

# 2. Insert disc into CD-ROM drive
with machine.cd.insert(cd_iso, to="D:") as cd_dev:
    print(f"Inserted CD-ROM on drive {cd_dev.drive_letter}")
    res = machine.command.run(f"dir {cd_dev.drive_letter}\\")
    print(res.stdout)

# Disc automatically ejected upon exiting context
```

See [`examples/cdrom_media.py`](examples/cdrom_media.py) for details.

---

## 🎙️ Virtual Microphone Audio Emulation (`machine.microphone`)

Feed audio from any host WAV or MP3 file into the guest VM's virtual microphone input, and record/verify the captured audio from inside Windows:

```python
from windows import Machine, analyze_wav_data, create_sine_wav

# 1. Prepare audio tone or provide an MP3/WAV file
tone = create_sine_wav("tone.wav", duration_sec=5.0, frequency=440.0)

# 2. Attach virtual microphone and stream audio into guest
with machine.microphone.emulate(tone, loop=True) as mic:
    # Record live audio from inside the Windows guest
    recorded_wav = machine.microphone.record_guest(duration_sec=3.0)

    # Inspect signal to verify audio capture
    stats = analyze_wav_data(recorded_wav)
    print(f"Recorded {stats['duration_sec']:.2f}s, max amplitude: {stats['max_amplitude']}")
    assert not stats["is_silent"], "Captured non-silent tone!"
```

See [`examples/microphone_audio.py`](examples/microphone_audio.py) for details.

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
