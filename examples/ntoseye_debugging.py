"""Example demonstrating Windows kernel debugging with ntoseye (WinDbg-compatible debugger on Linux).

ntoseye project: https://github.com/dmaivel/ntoseye
Install ntoseye:
  - CLI:    curl --proto '=https' --tlsv1.2 -LsSf https://github.com/dmaivel/ntoseye/releases/latest/download/ntoseye-installer.sh | sh
  - Cargo:  cargo install ntoseye
  - Python: pip install ntoseye
"""

import importlib

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== Windows Kernel Debugging with ntoseye ===")

    # 1. Resolve ISO and prepare VM overlay disk
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay.qcow2", use_cache=True)
    machine = Machine(image)

    # 2. Start VM with QEMU GDB stub active on port 1234
    # Note: We let Windows boot (pause_at_boot=False) so ntoskrnl.exe is loaded into kernel memory.
    # If the VM is paused at the BIOS bootloader, ntoskrnl.exe is not in memory yet!
    gdb_port = 1234
    print(f"\n1. Starting VM with GDB Stub on localhost:{gdb_port}...")
    machine.debug(
        backend="ntoseye",
        gdb_port=gdb_port,
        pause_at_boot=False,  # Boot into Windows so ntoskrnl.exe is mapped in memory
        open_console=True,  # Opens live VNC display with 1:1 USB tablet cursor tracking
    )

    # 3. Wait for Windows guest kernel and QGA to be ready
    print("2. Waiting for Windows kernel and QGA to initialize...")
    ready = machine.command.wait_until_ready(timeout=180)
    print(f"   Guest ready: {ready}")
    if not ready:
        raise RuntimeError("Guest failed to boot within timeout")

    print("\n=========================================================================")
    print("🎯 WINDOWS KERNEL (ntoskrnl.exe) IS LOADED & READY FOR DEBUGGING!")
    print("=========================================================================")
    print("To attach ntoseye from your host terminal, run:\n")
    print(f"   ntoseye -b gdb --connect localhost:{gdb_port}\n")
    print("Supported WinDbg commands in ntoseye:")
    print("   r              - Display CPU registers (RAX, RBX, RCX, RDX, RIP, RSP)")
    print("   k              - Display kernel call stack trace")
    print("   u rip          - Unassemble instructions at current instruction pointer")
    print("   !process 0 0   - List all active Windows processes and EPROCESS addresses")
    print("   !thread        - Display current ETHREAD state")
    print("   bp nt!KeBugCheckEx - Set kernel breakpoint")
    print("   g              - Continue execution")
    print("=========================================================================\n")

    # 4. If Python SDK is installed (pip install ntoseye), demonstrate programmatic attachment:
    try:
        ntoseye_mod = importlib.import_module("ntoseye")
        print("3. Found ntoseye Python SDK! Attaching programmatically...")
        dbg = ntoseye_mod.attach("gdb", connect=f"localhost:{gdb_port}")
        print(f"   Connected to debugger: {dbg}")
    except (ImportError, Exception) as exc:
        print(f"3. Programmatic attach note: {exc}")

    # 5. Execute guest command via QGA while debugger is active
    res = machine.command.run("Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version")
    print(f"\n4. Guest OS Info via QGA:\n{res.stdout.strip()}")

    # 6. Clean shutdown
    print("\n5. Powering off VM...")
    machine.power.off()
    print("ntoseye demo completed successfully!")


if __name__ == "__main__":
    main()
