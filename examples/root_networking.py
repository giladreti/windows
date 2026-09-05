"""Example demonstrating Root-Level Host-to-VM Networking via Linux Kernel Bridge:
- Linux Bridge (`VirtualSwitch(mode="bridge")`): Creates a kernel bridge (e.g. `winbr0`).
- Host IP Assignment: Assigns `192.168.100.1/24` directly to the host bridge interface.
- TAP Hotplugging: Attaches the Windows VM via a dedicated kernel TAP device.
- Guest Static IP: Assigns `192.168.100.10/24` with default gateway `192.168.100.1` inside Windows.
- Bidirectional Ping:
  1. Host pings VM (`ping -c 4 192.168.100.10`).
  2. VM pings Host (`ping 192.168.100.1 -n 4`).
- Bridge Packet Capture: Captures all host-to-VM traffic to `host_vm_traffic.pcap`.

Usage:
    sudo uv run python3 examples/root_networking.py
    # or
    sudo python3 examples/root_networking.py
"""

import os
import subprocess
import sys
import time

from windows import ISO, Image, Machine, NICModel, VirtualSwitch, WindowsVersion


def main() -> None:
    print("=== Host-to-VM Root Networking Demo (Linux Bridge & TAP) ===")

    # 0. Check root privileges
    if os.geteuid() != 0:
        print("\n[!] This example requires root privileges to manage Linux kernel bridges and TAP devices.")
        print("    Please run with sudo:")
        print("    sudo uv run python3 examples/root_networking.py")
        print("    (or: sudo python3 examples/root_networking.py)")
        sys.exit(1)

    bridge_name = "winbr0"
    host_ip = "192.168.100.1/24"
    vm_ip = "192.168.100.10/24"
    gateway_ip = "192.168.100.1"
    pcap_file = "host_vm_traffic.pcap"

    # 1. Initialize Linux bridge switch
    print(f"\n1. Creating Linux kernel bridge '{bridge_name}'...")
    switch = VirtualSwitch(bridge_name, mode="bridge")
    print(f"   ✓ Bridge interface '{switch.bridge_name}' created and set UP.")

    # 2. Assign host IP address to bridge interface
    print(f"\n2. Assigning host IP {host_ip} to bridge '{switch.bridge_name}'...")
    switch.assign_host_ip(host_ip)
    print(f"   ✓ Host is now reachable on {gateway_ip} across the bridge.")

    # 3. Prepare thin VM overlay disk backing cached Windows base image
    print("\n3. Preparing Windows VM overlay disk...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="root_demo_vm.qcow2", use_cache=True)
    vm = Machine(image, ram_mb=2048, cpus=2)

    try:
        # 4. Power on the VM
        print("\n4. Powering on Windows VM...")
        vm.run(timeout=180)
        print("   ✓ Windows VM is online and responsive.")

        # 5. Hotplug secondary NIC connected to the Linux bridge
        print(f"\n5. Hotplugging NIC connected to Linux bridge '{bridge_name}'...")
        nic = vm.network.add(
            model=NICModel.E1000E,
            mac="52:54:00:12:34:56",
            switch=switch,
        )
        print(f"   ✓ NIC hotplugged: {nic.id} (MAC: {nic.mac}, TAP: {nic._tap_name})")

        # 6. Configure in-guest static IP and default gateway
        print(f"\n6. Configuring guest static IP ({vm_ip}, GW: {gateway_ip}) via QGA...")
        nic.configure(ip=vm_ip, gateway=gateway_ip)
        print(f"   ✓ In-guest interface configured with {vm_ip}.")

        # 7. Enable ICMP Echo in Windows Firewall
        print("\n7. Enabling ICMP Echo in Windows Firewall...")
        vm.command.run(
            "netsh advfirewall firewall add rule name='Allow Ping' protocol=icmpv4:8,any dir=in action=allow",
            timeout=15,
        )
        print("   ✓ Firewall rule added for ICMPv4 echo.")

        # 8. Start packet capture on the bridge interface
        print(f"\n8. Starting packet capture on bridge '{bridge_name}' -> {pcap_file}...")
        with switch.capture(pcap_file) as cap:
            print(f"   ✓ Packet capture active (file: {cap.pcap_path})")

            # 9. Host pings VM
            print(f"\n9. Pinging Windows VM ({vm_ip.split('/')[0]}) from Host...")
            host_ping = subprocess.run(
                ["ping", "-c", "4", vm_ip.split("/")[0]],
                capture_output=True,
                text=True,
            )
            print(f"   Host Ping Output:\n{host_ping.stdout.strip()}")

            # 10. VM pings Host
            print(f"\n10. Pinging Host ({gateway_ip}) from Windows VM...")
            vm_ping = vm.command.run(f"ping {gateway_ip} -n 4", timeout=20)
            print(f"   VM Ping Output:\n{vm_ping.stdout.strip()}")
            time.sleep(1)

        print(f"   ✓ Capture stopped. Saved to {pcap_file}")

        # 11. Hot-unplug NIC
        print("\n11. Hot-unplugging NIC from VM...")
        nic.remove()
        print("   ✓ NIC removed cleanly.")

    finally:
        # 12. Clean up VM and bridge
        print("\n12. Shutting down VM and tearing down Linux bridge...")
        vm.power.off()
        switch.destroy()
        print("   ✓ Bridge and TAP interfaces destroyed cleanly.")
        print("\n=== Demo Complete ===")


if __name__ == "__main__":
    main()
