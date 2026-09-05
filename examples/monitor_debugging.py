"""Example demonstrating QEMU Monitor debugger (machine.debug) and HMP scripting."""

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== QEMU Monitor Debugger Example ===")

    # 1. Prepare VM image
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay2.qcow2", use_cache=True)
    machine = Machine(image)

    # 2. Start VM stopped at boot (-S) with custom QEMU HMP monitor script
    init_debug_script = [
        "info status",
        "info cpus",
        "info block",
        "info network",
    ]

    print("\nStarting machine in debug mode (stopped at boot)...")
    machine.debug(
        init_script=init_debug_script,
        pause_at_boot=True,
        auto_continue=True,
        open_console=True,
    )
    print(f"VM Power status: {machine.power.status}")

    # 3. Interactive HMP Monitor commands while VM is running
    print("\nSending live HMP monitor command...")
    status_output = machine.console.send_monitor_command("info status")
    print(f"Monitor Status Output:\n{status_output.strip()}")

    # 4. Capture screenshot via monitor screendump
    screenshot_file = machine.console.screenshot("debug_screen.png")
    print(f"Saved screenshot: {screenshot_file}")

    # 5. Clean shutdown
    machine.power.off()
    print("\nMonitor debugging example completed successfully!")


if __name__ == "__main__":
    main()
