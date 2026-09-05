"""Example demonstrating VM Snapshot Management and Machine Forking (machine.snapshot):
- machine.snapshot.create(): Create VM/disk snapshots (live with RAM state or offline disk state)
- machine.snapshot.list(): List all available snapshots with IDs and metadata
- machine.snapshot.revert(): Instant rollback to a previous snapshot
- machine.snapshot.fork(): Create a new independent VM instance from an existing snapshot
- machine.snapshot.delete(): Remove snapshot from disk
"""

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== VM Snapshot Management & Forking Live Demo ===")

    # 1. Prepare base VM image using cached base image
    print("\n1. Preparing Windows VM overlay disk...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="base_vm_snapshot_demo.qcow2", use_cache=True)
    machine = Machine(image)

    try:
        # 2. Start VM and wait for QGA interface
        print("\n2. Starting base VM and waiting for QGA guest agent...")
        machine.run(timeout=180)
        print("   Guest OS is ready and responding to commands.")

        # Create initial state file before taking snapshot
        initial_file = machine.file / r"C:\Users\Public\state.txt"
        initial_file.write_text("STATE_CLEAN_INSTALL")
        print(f"   [Initial State] state.txt = '{initial_file.read_text().strip()}'")

        # 3. Create a snapshot (live with memory & disk state)
        print("\n3. Creating live VM snapshot 'checkpoint_clean'...")
        snap = machine.snapshot.create("checkpoint_clean")
        print(f"   ✓ Snapshot created successfully: {snap}")

        # 4. Modify VM state: create temporary artifacts after snapshot
        print("\n4. Creating temporary artifacts in VM after snapshot...")
        # (a) Modify existing file
        initial_file.write_text("STATE_DIRTY_MODIFIED")
        print(f"   [Modified State] state.txt = '{initial_file.read_text().strip()}'")

        # (b) Create a new temporary artifact file
        artifact_file = machine.file / r"C:\Users\Public\temp_artifact.txt"
        artifact_file.write_text("THIS_FILE_SHOULD_DISAPPEAR_ON_REVERT")
        print(f"   [Created Artifact] temp_artifact.txt exists: {artifact_file.exists()}")

        # (c) Create a new registry key & value
        machine.registry.set_value(r"HKCU:\Software\SnapshotDemo", "TestArtifact", 999, value_type="DWord")
        reg_val = machine.registry.get_value(r"HKCU:\Software\SnapshotDemo", "TestArtifact")
        print(f"   [Created Registry] HKCU:\\Software\\SnapshotDemo\\TestArtifact = {reg_val}")

        # 5. List available snapshots (prints ASCII tree automatically!)
        print("\n5. Listing snapshots on disk (ASCII tree view):")
        print(machine.snapshot.list())

        # 6. Fork a new machine from the 'checkpoint_clean' snapshot
        print("\n6. Forking a new Machine instance from 'checkpoint_clean' snapshot...")
        forked_machine = machine.snapshot.fork(
            "checkpoint_clean",
            output_disk="forked_clean_vm.qcow2",
            ram_mb=4096,
            cpus=4,
        )
        print(
            f"   ✓ Forked machine created with disk: {forked_machine.image.disk_path} (size: {forked_machine.image.stat().st_size} bytes)"
        )

        # 7. Revert original VM back to 'checkpoint_clean' using snapshot object hierarchy
        print("\n7. Reverting base VM back to snapshot 'checkpoint_clean' via snapshot object...")
        machine.snapshot.list()["checkpoint_clean"].revert()
        print("   ✓ Revert command executed.")

        # Brief pause to ensure QGA handles post-revert resume
        machine.command.wait_until_ready(timeout=30)

        # 8. Verify artifacts were removed / state restored
        print("\n8. Verifying artifacts and state restoration:")
        restored_state = initial_file.read_text().strip()
        print(f"   - state.txt content: '{restored_state}' (Expected: 'STATE_CLEAN_INSTALL')")
        assert restored_state == "STATE_CLEAN_INSTALL", f"Expected STATE_CLEAN_INSTALL, got {restored_state}"
        print("     ✓ State file restored correctly!")

        artifact_still_exists = artifact_file.exists()
        print(f"   - temp_artifact.txt exists: {artifact_still_exists} (Expected: False)")
        assert not artifact_still_exists, "temp_artifact.txt should have been removed on revert!"
        print("     ✓ Temporary artifact file removed correctly!")

        reg_after_revert = machine.registry.get_value(r"HKCU:\Software\SnapshotDemo", "TestArtifact")
        print(f"   - Registry TestArtifact value: {reg_after_revert} (Expected: None)")
        assert reg_after_revert is None, "Registry key should have been removed on revert!"
        print("     ✓ Temporary registry value removed correctly!")

        print("\n🎉 ALL VERIFICATION CHECKS PASSED!")

    finally:
        # 9. Clean shutdown
        print("\n9. Shutting down VM...")
        machine.power.off()
        print("VM powered off.")


if __name__ == "__main__":
    main()
