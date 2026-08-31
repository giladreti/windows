"""Example demonstrating high-level typed Windows abstractions in wawalab/windows:
- machine.processes: Process inspection, memory metrics, and process termination
- machine.registry: Querying, creating, updating, and deleting registry keys and values
- machine.services: Listing services, checking status, starting/stopping, and setting startup modes
"""

import windows
from windows.iso import WindowsVersion


def main():
    print("=== Windows High-Level Subsystems Demo ===")

    # 1. Resolve ISO & Image
    print("\n1. Resolving Windows ISO & creating thin overlay disk...")
    iso = windows.get_iso(WindowsVersion.WIN10_22H2)
    # Note: output_disk=None auto-generates a unique random overlay disk name
    image = windows.create_image_from_iso(iso, output_disk=None, use_cache=True)
    machine = windows.create_machine_from_image(image)

    # 2. Start VM and wait for QGA interface
    print("2. Starting VM and waiting for QGA guest interface...")
    machine.run(timeout=180)

    # =========================================================================
    # 3. Process Management (machine.processes)
    # =========================================================================
    print("\n=========================================================================")
    print("3. Process Subsystem (machine.processes)")
    print("=========================================================================")

    # List all processes
    all_procs = machine.processes.list()
    print(f"Total running processes in guest: {len(all_procs)}")

    # Display top 5 processes by memory usage
    top_memory = sorted(all_procs, key=lambda p: p.working_set_mb, reverse=True)[:5]
    print("\nTop 5 processes by Working Set Memory:")
    for p in top_memory:
        print(f"  - [{p.pid:>5}] {p.name:<25} Memory: {p.working_set_mb:>7.2f} MB | CPU: {p.cpu:>5.1f}s")

    # Inspect a specific process by name
    explorer = machine.processes.get("explorer")
    if explorer:
        print(f"\nFound Explorer process: PID={explorer.pid}, Path={explorer.path}")

    # Launch a test process (notepad.exe) cleanly using machine.processes.spawn() and kill it
    print("\nSpawning notepad.exe in guest via machine.processes.spawn()...")
    notepad_pid = machine.processes.spawn("notepad.exe")
    print(f"Notepad spawned with PID: {notepad_pid}")

    if notepad_pid > 0:
        killed = machine.processes.kill(notepad_pid, force=True)
        print(f"Killed Notepad by PID: {killed}")

    # =========================================================================
    # 4. Registry Subsystem (machine.registry)
    # =========================================================================
    print("\n=========================================================================")
    print("4. Registry Subsystem (machine.registry)")
    print("=========================================================================")

    # Read existing Windows information from Registry
    current_ver_key = r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    product_name = machine.registry.get_value(current_ver_key, "ProductName")
    build_num = machine.registry.get_value(current_ver_key, "CurrentBuildNumber")
    print(f"Windows Product Name: {product_name} (Build: {build_num})")

    # Create a custom application registry key and write values
    app_key = r"HKCU:\Software\MyCustomApp"
    print(f"\nWriting custom registry settings to {app_key}...")
    machine.registry.set_value(app_key, "AppTitle", "My Automated Tool", value_type="String")
    machine.registry.set_value(app_key, "LogLevel", 3, value_type="DWord")
    machine.registry.set_value(app_key, "IsConfigured", 1, value_type="DWord")

    # Read back and verify
    app_title = machine.registry.get_value(app_key, "AppTitle")
    log_level = machine.registry.get_value(app_key, "LogLevel")
    print(f"Verified Registry Values -> AppTitle: '{app_title}', LogLevel: {log_level}")

    # Delete specific property and key
    machine.registry.delete_value(app_key, "LogLevel")
    machine.registry.delete_key(app_key)
    print(f"Cleaned up registry key {app_key} (Key exists: {machine.registry.key_exists(app_key)})")

    # =========================================================================
    # 5. Services Subsystem (machine.services)
    # =========================================================================
    print("\n=========================================================================")
    print("5. Services Subsystem (machine.services)")
    print("=========================================================================")

    # List all services
    services = machine.services.list()
    running_services = [s for s in services if s.status.lower() == "running"]
    print(f"Total installed services: {len(services)} (Running: {len(running_services)})")

    # Inspect a specific service
    spooler = machine.services.get("Spooler")  # Print Spooler
    if spooler:
        print(
            f"\nService '{spooler.name}' ({spooler.display_name}): Status={spooler.status}, StartType={spooler.start_type}"
        )

        # Stop and restart service
        print(f"Stopping service '{spooler.name}'...")
        machine.services.stop("Spooler")
        svc_stopped = machine.services.get("Spooler")
        print(f"Status after stop: {svc_stopped.status if svc_stopped else 'Unknown'}")

        print(f"Starting service '{spooler.name}'...")
        machine.services.start("Spooler")
        svc_started = machine.services.get("Spooler")
        print(f"Status after start: {svc_started.status if svc_started else 'Unknown'}")

    # =========================================================================
    # 6. Shutdown
    # =========================================================================
    print("\n6. Powering off VM...")
    machine.power.off()
    print("Abstractions demo completed successfully!")


if __name__ == "__main__":
    main()
