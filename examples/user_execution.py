"""Example demonstrating user execution contexts (as_user) and credentials in windows.

By default, commands run via QEMU Guest Agent (QGA) execute with full
`NT AUTHORITY\\SYSTEM` privileges. The `as_user` APIs allow running commands
under a specific user context (such as `Administrator`) with password-based
authentication.
"""

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== Windows User Execution Contexts (as_user) Example ===")

    # 1. Prepare/reuse VM image
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay.qcow2", use_cache=True)

    # Initialize Machine with default user credentials (defaults to Administrator / Password123!)
    machine = Machine(
        image,
        default_user="Administrator",
        default_password="Password123!",
    )

    # 2. Start VM and wait for QGA readiness
    machine.power.on()
    print("Waiting for guest QGA interface...")
    machine.command.wait_until_ready(timeout=180)

    # 3. Default execution: NT AUTHORITY\SYSTEM
    # Commands run directly via QGA execute as SYSTEM by default.
    res_sys = machine.command.run("whoami")
    print(f"\n[Default SYSTEM Execution]\n  whoami: {res_sys.stdout.strip()}")

    # 4. Scoped execution via Context Manager: with machine.as_user(): ...
    # All commands inside this block automatically run as Administrator.
    print("\n[Scoped Execution with machine.as_user() Context Manager]")
    with machine.as_user():
        res_admin = machine.command.run("whoami")
        print(f"  whoami inside context: {res_admin.stdout.strip()}")

        res_profile = machine.command.run("echo $env:USERPROFILE", powershell=True)
        print(f"  UserProfile: {res_profile.stdout.strip()}")

        # Subsystems or internal commands can still execute as SYSTEM when needed:
        res_sys_override = machine.command.run("whoami", as_system=True)
        print(f"  Overridden with as_system=True: {res_sys_override.stdout.strip()}")

    # Once outside the context manager, execution automatically returns to SYSTEM:
    res_reverted = machine.command.run("whoami")
    print(f"  Outside context (reverted): {res_reverted.stdout.strip()}")

    # 5. Factory / View execution: admin = machine.as_user()
    # Create a reusable command controller view bound to a specific user.
    print("\n[Factory / View Execution with admin = machine.as_user()]")
    admin = machine.as_user()
    res_view = admin.run("whoami")
    print(f"  admin.run('whoami'): {res_view.stdout.strip()}")

    # 6. Explicit single-command execution with credentials
    # Credentials can also be specified per-command on machine.command.run().
    print("\n[Explicit Single-Command Execution]")
    res_explicit = machine.command.run(
        "whoami",
        user="Administrator",
        password="Password123!",
    )
    print(f"  Explicit credentials whoami: {res_explicit.stdout.strip()}")

    # 7. Cleanup
    machine.power.off()
    print("\nVM powered off. Example completed successfully!")


if __name__ == "__main__":
    main()
