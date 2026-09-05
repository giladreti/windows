"""Example demonstrating Windows Defender Firewall Management abstraction:
- Firewall Status & On/Off: Query profile status, turn firewall ON/OFF for Domain/Private/Public.
- Per-NIC Profile Selection: Query and set network connection category (Public / Private) per NIC.
- Rule Management:
  - Add rules (protocol, port, direction, action, program, ICMP).
  - List and filter rules.
  - Modify existing rules (ports, actions, names).
  - Enable / Disable rules.
  - Remove / Delete rules.
"""

from windows import (
    ISO,
    FirewallAction,
    FirewallDirection,
    FirewallProfile,
    Image,
    Machine,
    NetworkCategory,
    WindowsVersion,
)


def main() -> None:
    print("=== Windows Defender Firewall Abstraction Demo ===")

    # 1. Prepare and boot Windows VM
    print("\n1. Booting Windows VM from cached base image...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="fw_demo_vm.qcow2", use_cache=True)
    vm = Machine(image, ram_mb=2048, cpus=2)

    try:
        vm.run(timeout=180)
        print("   ✓ VM is online.")

        # 2. Inspect Firewall Profile Status
        print("\n2. Checking current firewall profile status...")
        status = vm.firewall.status()
        print(f"   Domain profile:  {'ON' if status['domain'] else 'OFF'}")
        print(f"   Private profile: {'ON' if status['private'] else 'OFF'}")
        print(f"   Public profile:  {'ON' if status['public'] else 'OFF'}")

        # 3. Turn Firewall OFF and back ON
        print("\n3. Testing Firewall ON / OFF control...")
        print("   Turning firewall OFF for Public profile...")
        vm.firewall.off(profile=FirewallProfile.PUBLIC)
        print(f"   Public profile status: {vm.firewall.is_enabled('public')}")

        print("   Turning firewall ON for all profiles...")
        vm.firewall.on(profile="all")
        print(f"   All profiles enabled: {vm.firewall.is_enabled('all')}")

        # 4. Per-NIC Profile Selection
        print("\n4. Managing Per-NIC Network Categories / Profiles...")
        default_nic = vm.network.default
        current_cat = default_nic.get_profile()
        print(f"   Current NIC profile: {current_cat}")

        print("   Switching NIC network category to Private...")
        default_nic.set_profile(NetworkCategory.PRIVATE)
        print(f"   Updated NIC profile: {default_nic.get_profile()}")

        # 5. Add Firewall Rules
        print("\n5. Adding custom firewall rules...")
        # Rule A: Allow inbound TCP port 8080 (Web App)
        web_rule = vm.firewall.add_rule(
            name="App-Web-8080",
            display_name="Demo Web Application",
            direction=FirewallDirection.INBOUND,
            action=FirewallAction.ALLOW,
            protocol="TCP",
            local_port=8080,
            profile=FirewallProfile.PRIVATE,
            description="Allows inbound traffic to demo web service",
        )
        print(f"   ✓ Added rule: {web_rule}")

        # Rule B: Allow inbound ICMP echo (Ping)
        ping_rule = vm.firewall.add_rule(
            name="Allow-ICMP-Ping",
            display_name="ICMPv4 Echo Request",
            protocol="ICMPv4",
            icmp_type=8,
            direction="in",
            action="allow",
        )
        print(f"   ✓ Added rule: {ping_rule}")

        # 6. List and Filter Rules
        print("\n6. Querying and filtering firewall rules...")
        custom_rules = vm.firewall.list_rules(name="App-*")
        print(f"   Found {len(custom_rules)} rule(s) matching 'App-*':")
        for r in custom_rules:
            print(f"     - {r.name}: {r.protocol} port {r.local_port} ({r.action})")

        # 7. Modify Rule
        print("\n7. Modifying firewall rule...")
        print(f"   Original rule ports: {web_rule.local_port}, action: {web_rule.action}")
        web_rule.modify(local_port="8080,8443", action=FirewallAction.BLOCK)
        print(f"   ✓ Modified rule ports: {web_rule.local_port}, action: {web_rule.action}")

        # 8. Disable and Re-enable Rule
        print("\n8. Disabling and re-enabling rule...")
        web_rule.disable()
        print(f"   Rule enabled: {web_rule.enabled}")
        web_rule.enable()
        print(f"   Rule enabled: {web_rule.enabled}")

        # 9. Clean up / Remove Rules
        print("\n9. Removing firewall rules...")
        web_rule.remove()
        ping_rule.remove()
        print("   ✓ Rules removed cleanly.")

    finally:
        print("\n10. Shutting down VM...")
        vm.power.off()
        print("   ✓ Done.")


if __name__ == "__main__":
    main()
