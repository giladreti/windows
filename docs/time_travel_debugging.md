# Time Travel Debugging (TTD) in `windows`

`windows` includes a deterministic Time Travel Debugging (TTD) subsystem built on QEMU's record/replay engine (`-icount rr=record` and `-icount rr=replay`), GDB remote serial protocol (RSP), and `ntoseye` WinDbg-compatible kernel debugging.

---

## 1. Overview & Tiers

The library provides two tiers of timeline navigation:

### Tier 1: Macro-TTD (High-Speed Timeline Checkpoints)
* **Execution**: Native hardware virtualization (KVM) with multi-core SMP.
* **Mechanism**: Internal and external live RAM+disk snapshots (`machine.snapshot.create()`, `machine.snapshot.revert()`).
* **Speed**: Checkpoints and rollbacks in 1–2 seconds.
* **Use Case**: Major execution milestones (e.g. before launching a test or suspicious binary).

### Tier 2: Micro-TTD (Instruction-Level Deterministic Replay)
* **Execution**: Deterministic single-vCPU software emulation (TCG, `cpus=1`).
* **Mechanism**: QEMU instruction counter (`-icount`) with recorded block I/O (`blkreplay`) and network replay (`filter-replay`).
* **Capabilities**: Instruction-by-instruction forward and backward stepping (`step`, `reverse_step`), deterministic seeking (`seek`), execution bookmarks, GDB reverse debugging, and `ntoseye` kernel object inspection.

---

## 2. API Usage

### Recording a Session
Use the context manager `with machine.record_session(name):` on a running VM. Standard `machine.command.run(...)` executes commands directly within the guest:

```python
from windows import ISO, Image, Machine, WindowsVersion

iso = ISO.from_version(WindowsVersion.WIN10_22H2)
image = Image.from_iso(iso, output_disk="ttd_demo_vm.qcow2", use_cache=True)

with Machine(image, ram_mb=4096, ttd=True) as machine:
    machine.power.on()
    machine.wait_for_boot(timeout=900)

    # Deterministic recording block
    with machine.record_session("calc_test") as recording:
        print(f"Tracing execution into: {recording.trace_path}")
        res = machine.command.run("whoami", powershell=False)
        print("Guest response:", res.stdout.strip())

    # Recording finalized automatically on exit (via SIGINT)
```

### Deterministic Replay & Timeline Navigation
Replay the recorded session deterministically:

```python
with machine.ttd.replay("calc_test") as session:
    print(f"Replay GDB port: {session.gdb_port}")
    print(f"Initial icount: {session.current_icount}, RIP: 0x{session.rip:x}")

    # Step forward 5 instructions
    new_rip = session.step(count=5)

    # Bookmark a point in time
    bm = session.add_bookmark("after_setup", description="Point after initialization")

    # Step backward in time (reverse execution)
    back_rip = session.reverse_step(count=3)

    # Seek deterministically to an exact instruction count
    session.seek(icount=100)

    # Jump directly back to a bookmark
    session.goto_bookmark("after_setup")
```

### Attaching External Debuggers

#### GDB
While `machine.ttd.replay(...)` is active:
```bash
gdb -ex 'target remote localhost:<session.gdb_port>'
(gdb) reverse-stepi     # Step backward 1 instruction
(gdb) reverse-continue  # Run backward to breakpoint
(gdb) watch *0xaddr     # Break on memory modification
```

#### ntoseye (WinDbg for Linux)
```bash
ntoseye -b gdb --connect localhost:<session.gdb_port>
ntoseye> !process 0 0   # Enumerate EPROCESS structures
ntoseye> !thread        # Inspect active thread
ntoseye> k              # Stack trace with PDB symbols
ntoseye> dt nt!_IRP     # Inspect kernel structures
```

---

## 3. Critical Technical Constraints & Troubleshooting

### 1. The USB 0x9F BSOD Fix
* **BugCheck**: `DRIVER_POWER_STATE_FAILURE (0x9F)`.
* **Root Cause**: Virtual USB 3.0 XHCI controller and USB tablet (`-device qemu-xhci -device usb-tablet`) hang during selective suspend power transitions under single-core TCG emulation. After 240 seconds, the kernel watchdog (`PopIrpWatchdog`) triggers BugCheck `0x9F`.
* **Resolution**: In `src/windows/qemu.py`, `qemu-xhci` and `usb-tablet` are omitted when `ttd=True` or `rr_mode` is set. Standard PS/2 devices (`i8042`) are used instead.

### 2. RTC UTC Synchronization
* QEMU's record/replay engine strictly forbids `-rtc base=localtime`. It requires `-rtc base=utc`.
* The Windows registry has `RealTimeIsUniversal = 1` set in `HKLM\SYSTEM\CurrentControlSet\Control\TimeZoneInformation` so Windows natively aligns with the hardware UTC clock without clock skew or boot stalls.

### 3. Base Image Optimizations
* `SysMain` (SuperFetch) and `WSearch` (Windows Search indexer) are disabled in the base image to eliminate background disk thrashing during emulation.
* Windows Update remains active (`Manual` start type).

### 4. Boot Performance Characteristics
* **KVM**: Boots to desktop in 8–15 seconds.
* **TCG Software Emulation**: Single-vCPU software translation of tens of billions of instructions takes ~12–15 minutes on cold boot. Once booted, saving an internal snapshot (`savevm`) or restoring via `-loadvm` takes 1–2 seconds.
