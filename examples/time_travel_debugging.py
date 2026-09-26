"""Example demonstrating Time Travel Debugging (TTD) with QEMU Record/Replay, GDB reverse execution, and ntoseye.

Features demonstrated:
1. Macro-TTD: Fast state timeline checkpointing with KVM + SMP via machine.snapshot
2. Micro-TTD: Deterministic instruction-level recording via machine.ttd.record()
3. Timeline Navigation:
   - reverse_step(): Step backward in CPU instructions (reverse-stepi)
   - step(): Step forward in CPU instructions
   - seek(icount): Seek execution deterministically to an exact instruction count
   - add_bookmark() / goto_bookmark(): Bookmark and jump between key moments
4. Debugger Attachment:
   - GDB: Reverse stepping and reverse watchpoints
   - ntoseye: Windows kernel introspection (PDB symbols, EPROCESS, ETHREAD, stack traces)
"""

import sys

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== Windows Time Travel Debugging (TTD) Demo ===")

    # 1. Prepare base VM image using cached Windows base image
    print("\n1. Preparing Windows VM overlay disk...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="ttd_demo_vm.qcow2", use_cache=True)
    machine = Machine(image, ram_mb=4096, ttd=True)

    # -------------------------------------------------------------------------
    # Tier 1: Macro-TTD (Fast State Timeline Checkpointing with KVM + Multi-core)
    # -------------------------------------------------------------------------
    print("\n--- Tier 1: Macro-TTD (High-Speed Timeline Checkpoints) ---")
    print("Macro-TTD runs at native speed with KVM and multi-core SMP enabled.")
    print("Use machine.snapshot.create() to bookmark major milestones in seconds:")
    print("   machine.power.on()")
    print("   machine.snapshot.create('before_malware_exec')")
    print("   # ... run suspicious payload or test ...")
    print("   machine.snapshot.revert('before_malware_exec')  # Instant rollback!")

    # -------------------------------------------------------------------------
    # Tier 2: Micro-TTD (Deterministic Instruction-Level Record & Replay)
    # -------------------------------------------------------------------------
    print("\n--- Tier 2: Micro-TTD (Instruction-Level Deterministic Replay) ---")
    print("Micro-TTD uses QEMU's deterministic -icount engine (TCG emulation, 1 vCPU).")
    print("All disk I/O (blkreplay) and network packets (filter-replay) are recorded.")
    print("You can boot the machine normally and record only a specific execution block!")

    print("\nBooting Windows VM in TTD-compatible mode (TCG, 1 vCPU)...")
    print("   machine = Machine(image, ttd=True, ram_mb=4096)")
    print("   machine.power.on()")
    print("   machine.wait_for_boot()")

    # Record deterministic execution session using the record_session context manager
    recording_name = "reproduce_issue"
    print(f"\n2. Recording deterministic execution session '{recording_name}'...")
    print("   Using context manager: with machine.record_session(...)")
    print("   Inside the block, use machine.command.run directly to execute guest commands:")
    print(f"   with machine.record_session('{recording_name}') as rec:")
    print("       res = machine.command.run('whoami')")
    print("       print(res.stdout)")
    print("   ✓ Recording is automatically finalized on context exit!")

    machine.power.on()
    print("   Waiting for Windows guest to complete booting...")
    machine.wait_for_boot(timeout=900)

    # In this script, we demonstrate recording a session directly:
    with machine.record_session(recording_name) as recording:
        print(f"   Recording active! Tracing execution into: {recording.trace_path}")
        print("   Running guest operations via machine.command.run...")
        res = machine.command.run("whoami", powershell=False)
        print(f"   Guest response: {res.stdout.strip()}")

    print(f"   ✓ Recording complete! Trace saved to: {recording.trace_path}")
    print(f"   ✓ Trace file size: {recording.size_bytes} bytes")

    # B. Start deterministic replay session
    print(f"\n3. Starting deterministic replay session for '{recording_name}'...")
    with machine.ttd.replay(name=recording_name) as session:
        print(f"   ✓ Replay session active! GDB stub listening on localhost:{session.gdb_port}")
        print(f"   Initial instruction count: icount = {session.current_icount}")
        print(f"   Initial instruction pointer: RIP = 0x{session.rip:x}")

        # C. Step forward
        print("\n4. Stepping forward 5 instructions...")
        new_rip = session.step(count=5)
        print(f"   After step(5): icount = {session.current_icount}, RIP = 0x{new_rip:x}")

        # Bookmark this milestone
        bm = session.add_bookmark("step_forward_5", description="Execution milestone after 5 instructions")
        print(f"   ✓ Saved bookmark '{bm.name}' at icount {bm.icount}")

        # D. Time Travel: Step backward in time!
        print("\n5. Stepping BACKWARD in time (reverse-stepi)...")
        back_rip = session.reverse_step(count=3)
        print(f"   After reverse_step(3): RIP = 0x{back_rip:x}")

        # E. Seek to an exact instruction count
        target_icount = 100
        print(f"\n6. Seeking deterministically to icount = {target_icount}...")
        session.seek(target_icount)
        print(f"   Current position: icount = {session.current_icount}, RIP = 0x{session.rip:x}")

        # F. Return to bookmark
        print(f"\n7. Returning to saved bookmark '{bm.name}'...")
        session.goto_bookmark(bm.name)
        print(f"   Current position: icount = {session.current_icount}, RIP = 0x{session.rip:x}")

        # G. Attaching Tools (GDB and ntoseye)
        print("\n=========================================================================")
        print("🎯 TIME TRAVEL DEBUGGING ENVIRONMENT READY")
        print("=========================================================================")
        print("Attach GDB for reverse instruction execution & watchpoints:")
        print(f"   gdb -ex 'target remote localhost:{session.gdb_port}'")
        print("   Commands: reverse-stepi (rsi), reverse-continue (rc), watch *0xaddr")
        print("\nAttach ntoseye for Windows kernel object & symbol introspection:")
        print(f"   ntoseye -b gdb --connect localhost:{session.gdb_port}")
        print("   Commands: !process 0 0, !thread, k, r, dt nt!_IRP")
        print("=========================================================================\n")

        if sys.stdin.isatty():
            print("Press Enter to stop replay session...")
            try:
                input()
            except EOFError:
                pass
        else:
            print("Testing GDB remote instruction query...")
            out = session._gdb.run_gdb_commands(["info registers rip"])
            print("GDB verification output:", out.strip())

        print("Stopping replay session...")

    print("TTD Demo completed successfully!")


if __name__ == "__main__":
    main()
