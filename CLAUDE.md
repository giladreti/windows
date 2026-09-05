# CLAUDE.md - Developer Guide & Project Architecture

## Project Overview

`windows` is a modern Python library for fully automated, hands-free Windows virtual machine provisioning, lifecycle management, out-of-band QEMU Guest Agent (QGA) execution, high-level Windows abstractions, and QEMU/`ntoseye` kernel debugging.

### Core Goals & Architecture Principles
- **Hands-Free Automation**: Zero-interaction Windows installation via dynamically constructed unattended media (`autounattend.xml`, `setup.cmd`, drivers/tools ISO).
- **Fast Ephemeral VMs**: Instant creation of thin QEMU Copy-on-Write overlay disks (`.qcow2`) backing pre-installed cached base images.
- **Pure QEMU Guest Agent (QGA) Architecture**: Zero TCP network dependencies, zero open ports, zero SSH setup, zero firewall rules. Operates 100% out-of-band via VirtIO Serial device (`\\.\Global\org.qemu.guest_agent.0` <-> Host UNIX domain socket).
- **High-Level Typed Abstractions**: First-class abstractions for Processes (`machine.processes`), Windows Registry (`machine.registry`), and Services (`machine.services`).
- **Pythonic `pathlib.Path`-Style File Transfer**: Remote paths represented via `RemotePath` supporting upload/download of files and directories, recursive tree replication, exist policies (`overwrite`, `merge`, `abort`), and direct text/bytes reading and writing over QGA file streaming APIs.
- **QEMU Monitor & `ntoseye` WinDbg Debugger**: Interactive debugger support (`machine.debug()`) with boot pausing (`-S`), HMP monitor scripting, QEMU GDB stub (`-gdb tcp::1234`), and native `ntoseye` WinDbg-compatible kernel debugging on Linux hosts.
- **Absolute Coordinate USB Tablet**: Configured with `-device qemu-xhci -device usb-tablet` for 1:1 pixel-perfect cursor tracking in VNC viewers like Remmina and TigerVNC.

---

## Directory Structure

```
windows/
├── src/
│   └── windows/
│       ├── __init__.py      # Public exports and top-level entrypoints
│       ├── console.py       # VNC console launcher, screendump, & QEMU HMP monitor client
│       ├── executor.py      # CommandController for guest command execution via QGA
│       ├── file.py          # FileController & RemotePath (pathlib-style remote file & directory ops)
│       ├── image.py         # Image representation and Image.from_iso provisioning logic
│       ├── iso.py           # Windows ISO downloading, resolving, hashing, and caching
│       ├── machine.py       # Machine instance, PowerController, machine.run(), machine.debug()
│       ├── processes.py     # ProcessController & ProcessInfo (listing, getting, and killing processes)
│       ├── qemu.py          # QEMU command builders, process manager, and qemu-img disk utilities
│       ├── qga.py           # Out-of-band QEMU Guest Agent JSON-RPC over UNIX domain socket
│       ├── registry.py      # RegistryController (get, set, delete registry keys and values)
│       ├── services.py      # ServiceController & ServiceInfo (list, start, stop, restart services)
│       ├── snapshot.py      # SnapshotController & SnapshotInfo (create, revert, list, and fork snapshots)
│       └── unattend.py      # Autounattend.xml generation, setup.cmd, and secondary ISO builder
├── tests/                   # Mocked, ultra-fast unit test suite (60+ tests)
│   ├── test_abstractions.py # Process, Registry, Service, and random output_disk tests
│   ├── test_advanced.py     # Cache, ISO resolution, and edge case tests
│   ├── test_cache.py        # ISO and image overlay caching verification
│   ├── test_console.py      # VNC launcher, screenshot, and monitor tests
│   ├── test_file_ops.py     # RemotePath directory upload, download, and exist_policy tests
│   ├── test_machine_debug.py # machine.debug() and machine.run() tests
│   ├── test_oop_interfaces.py # ISO, Image, Machine canonical interface tests
│   ├── test_qga.py          # QGA JSON-RPC protocol and file streaming tests
│   ├── test_snapshots.py    # SnapshotController lifecycle and machine forking tests
│   ├── test_windows.py      # Core workflow unit tests
│   └── test_working_machine.py # Machine instance creation tests
├── tests_e2e/               # Optional live end-to-end integration tests
├── examples/
│   ├── demo_workflow.py     # Comprehensive runnable workflow example
│   ├── ntoseye_debugging.py # ntoseye WinDbg-compatible kernel debugging example
│   ├── remote_file_ops.py   # Path-style file & directory transfers example
│   ├── monitor_debugging.py # QEMU monitor debugger & HMP scripting example
│   └── snapshot_management.py # Snapshot management and machine forking example
├── pyproject.toml           # Project metadata, dependencies, ruff & ty configuration
├── README.md                # User-facing guide and quickstart
└── CLAUDE.md                # Complete developer and architecture guide
```

---

## Development Tooling & Commands

The project uses `uv` for package management, virtual environments, linting, type-checking, and testing.

### 1. Run Unit Tests
```bash
uv run python -m pytest
```

### 2. Run Type Checker (`ty`)
```bash
uv run ty check
```

### 3. Run Linter & Formatter (`ruff`)
```bash
uv run ruff check .
uv run ruff format .
```

### 4. Run Example Workflow
```bash
uv run python3 examples/demo_workflow.py
```

---

## Key Modules & Component Reference

### 1. `windows.iso` & `ISO`
- **`ISO`**: Object-oriented Windows ISO representation.
  - **`ISO(path)`**: Instantiates an ISO object from a local file path or existing ISO instance.
  - **`ISO.from_version(version, cache_dir=None)`**: Resolves official/direct download links, verifies existing files, and caches ISOs in `~/.cache/windows/isos/`.
  - **`ISO.from_url(url, cache_dir=None)`**: Downloads and caches ISO from direct URL.
  - **`ISO.resolve(iso_or_version)`**: Resolves any ISO identifier (ISO instance, version alias, URL, or Path) to an `ISO` object.
- **`WindowsVersion`**: Enum for supported Windows builds (`WIN10_22H2`, `WIN11_25H2`, etc.).
- **`create_dummy_iso(path)`**: Generates lightweight ISO files for fast testing.

### 2. `windows.unattend`
- **`generate_unattend_xml()`**: Generates unattended Windows installation XML bypassing OOBE, privacy questions, TPM/SecureBoot checks, and creating the default `Admin` administrator account.
- **`SETUP_GUEST_CMD` / `setup.cmd`**: Batch script executed during FirstLogon. Configures:
  - VirtIO Serial driver installation via `virtio-win-guest-tools.exe /passive`
  - QEMU Guest Agent installation via `msiexec.exe /i qemu-ga-x86_64.msi /qn`
  - Automatic `QEMU-GA` service startup
  - `LocalAccountTokenFilterPolicy = 1` registry key
  - Windows kernel debugging and testsigning enabled (`bcdedit /debug on`, `bcdedit /set testsigning on`)
- **`create_unattend_iso(target_iso_path, ...)`**: Packages `autounattend.xml`, `setup.cmd`, `qemu-ga-x86_64.msi`, and VirtIO tools into a secondary CD-ROM ISO.

### 3. `windows.image` & `Image`
- **`Image`**: Represents an installed Windows disk image (`.qcow2`).
  - **`Image(disk_path)`**: Instantiates an `Image` from a local disk path or existing Image instance.
  - **`Image.from_iso(iso, output_disk=None, use_cache=True, ...)`**:
    - Automatically provisions a clean Windows installation in a QEMU VM.
    - Caches the installed disk in `~/.cache/windows/images/`.
    - Creates a lightweight QEMU overlay disk (`.qcow2`) pointing to the base image. If `output_disk` is omitted, automatically generates a unique randomized filename (`windows_overlay_<uuid>.qcow2`).
  - **Offline Disk Analysis & File Operations (`image.file` / `ImagePath`)**:
    - **`image.file` / `ImagePath`**: Path-like offline filesystem interface mirroring `machine.file`:
      ```python
      hosts = image.file / r"C:\Windows\System32\drivers\etc\hosts"
      print(hosts.exists(), hosts.is_file(), hosts.read_text())

      note = image.file / r"C:\Users\Public\offline_script.bat"
      note.write_text("@echo off\r\necho Injected offline")

      for child in (image.file / r"C:\Windows").iterdir():
          print(child.name, child.is_dir())

      with image.file as fs:
          (fs / r"C:\batch_file.txt").write_text("Fast batch writes")
      ```
    - **`image.partitions()`**: Inspects MBR/GPT partition tables (offset, size, type, bootable status).
    - **`image.file.partition(n)`**: Scopes offline file operations to a specific partition number.
    - **`with image.mount(writable=True) as disk:`**: Low-level FUSE mount context manager for partition access.

### 4. `windows.machine` & `Machine`
- **`Machine(image, ram_mb=4096, cpus=4, headless=True, ...)`**: Instantiates a runnable VM controller from an `Image` or disk path.
- **`machine.power.on()` / `.off()` / `.restart()` / `.pause()` / `.resume()` / `.kill()`**: Manages QEMU process and execution state.
- **`machine.pause()` / `machine.resume()` / `machine.kill()`**: Top-level convenience methods for execution pause/unpause and forceful termination.
- **`machine.fork(snapshot_name=None, output_disk=None, run=True, ...)`**: Creates a snapshot of current state and runs a new concurrent machine from it.
- **`machine.run(timeout=180)`**: Powers on VM, waits for QGA readiness, and runs initial sanity check.
- **`machine.debug(backend="ntoseye" | "qemu_monitor", gdb_port=1234, ...)`**:
  - Supports QEMU monitor scripting and `ntoseye` WinDbg-compatible kernel debugging over QEMU GDB stub.
- **`with machine.record("session.mp4", fps=10):`**: Convenience alias for `machine.console.record()`.

### 5. `windows.console` (`machine.console`)
`machine.console` manages VNC display viewer processes, screenshot capture, and video recording:
- **`with machine.console.record(output_path="demo.mp4", fps=10) as rec:`**: Context manager for recording video of the VM console (supports `.mp4`, `.webm`, `.gif`, `.mkv`, `.avi`).
- **`machine.console.screenshot(output_path="screen.png")`**: Captures a live screen snapshot.
- **`machine.console.open()` / `machine.console.close()`**: Launches or closes native VNC viewer client (e.g. `vncviewer`, `tigervnc`, `remmina`).

### 6. `windows.processes` (`machine.processes`)
- `machine.processes.list()`: Returns `list[ProcessInfo]` (PID, ProcessName, CPU, WorkingSet MB, Path).
- `machine.processes.get(name_or_pid)`: Retrieve specific process details.
- `machine.processes.kill(name_or_pid, force=True)`: Terminate process by PID or name.

### 6. `windows.registry` (`machine.registry`)
- `machine.registry.get_value(key_path, value_name)`: Read registry value (`HKLM`, `HKCU`, `HKCR`, `HKU`).
- `machine.registry.set_value(key_path, value_name, value, value_type="String")`: Create/update registry keys and values.
- `machine.registry.delete_value(key_path, value_name)` / `delete_key(key_path)`: Remove keys or properties.

### 7. `windows.services` (`machine.services`)
- `machine.services.list()`: Returns `list[ServiceInfo]` (Name, DisplayName, Status, StartType).
- `machine.services.get(name)`: Get service status.
- `machine.services.start(name)` / `stop(name)` / `restart(name)` / `set_startup_type(name, "Automatic")`.

### 8. `windows.file` & `RemotePath` (`pathlib.Path`-Style Interface)
`machine.file` provides a `pathlib.Path`-like interface for manipulating remote guest filesystem resources over QGA file streaming:
```python
remote_dir = machine.file.path(r"C:\Users\Public\my_folder")
remote_file = machine.file / r"C:\Users\Public\config.json"

remote_file.write_text('{"active": true}')
content = remote_file.read_text()

remote_dir.upload(local_dir_path, exist_policy="overwrite")
remote_dir.download_dir(local_target_dir, exist_policy="overwrite")
```

### 9. `windows.snapshot` (`machine.snapshot`)
`machine.snapshot` provides comprehensive virtual machine / disk snapshot management, graph hierarchy traversal, and VM forking:
- `machine.snapshot.create(name)`: Takes a snapshot (live with RAM + CPU state via QEMU monitor if running, or internal disk state if stopped). Returns a `Snapshot` node instance.
- `machine.snapshot.list()`: Returns a `SnapshotList` object containing `Snapshot` nodes.
  - `repr(machine.snapshot.list())` / `str(...)`: Automatically renders a formatted ASCII tree drawing.
  - Dict & int indexing: `snapshots["snap_name"]`, `snapshots[0]`.
  - Hierarchy traversal & chaining: `machine.snapshot.list().parent.parent.revert()`.
- `Snapshot` node:
  - `snap.parent`: Returns parent `Snapshot` node in the tree hierarchy (or `None`).
  - `snap.children`: Returns list of child `Snapshot` nodes.
  - `snap.revert()`: Restores VM memory and disk state directly to this snapshot.
  - `snap.delete()`: Deletes this snapshot.
  - `snap.fork()`: Forks a new independent `Machine` from this snapshot.
- `machine.snapshot.revert(name_or_id)`: Rollback VM to a previously saved snapshot.
- `machine.snapshot.tree()`: Returns an ASCII tree view representation of all snapshots.
- `machine.snapshot.get(name)` / `machine.snapshot.exists(name)`: Query snapshot metadata.
- `machine.snapshot.delete(name)`: Deletes a snapshot.
- `machine.snapshot.fork(name=None, output_disk=None, run=False, ...)`: Extracts snapshot state to a new standalone disk and returns a new runnable `Machine` instance.
