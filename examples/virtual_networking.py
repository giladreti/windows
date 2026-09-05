"""Example demonstrating Virtual Networking, Inter-VM Switching, and Packet Capture:
- VirtualSwitch: Interconnect multiple Windows VMs across an isolated virtual switch.
  (Uses userspace multicast UDP switching without root, or Linux bridge when root).
- NIC Hotplugging: Dynamically attach network adapters with custom hardware models (e1000e, virtio-net-pci, rtl8139).
- Guest IP Configuration: Configure static IP addresses via QGA.
- Inter-VM Ping: Verify Layer 2 and Layer 3 communication between two live Windows VMs.
- Packet Capture: Record traffic to standard .pcap files (via QEMU filter-dump).
- Live Wireshark: Inspect network traffic in real time.

Note on Host Gateway (.1):
If running with root privileges on a Linux kernel bridge (`VirtualSwitch(mode="bridge")`),
the host itself can act as the .1 entity by assigning an IP to the bridge:
`sudo ip addr add 192.168.100.1/24 dev <bridge_name>`.
In userspace mode, connecting two VMs together (as shown below) verifies the entire L2/L3 path.
"""

import time

from windows import ISO, Image, Machine, NICModel, VirtualSwitch, WindowsVersion


def main():
    print("=== Virtual Networking & Inter-VM Switch Demo ===")

    # 1. Create an isolated virtual switch for inter-VM communication
    # VirtualSwitch automatically uses userspace multicast socket switching (no root required)
    # or Linux kernel bridges if running as root.
    print("\n1. Creating VirtualSwitch 'lab_net'...")
    switch = VirtualSwitch("lab_net")
    print(f"   ✓ Virtual switch initialized: {switch.name} (mode={switch.mode})")

    # 2. Prepare two thin VM overlay disks backing the same cached Windows base image
    print("\n2. Preparing Windows VM overlay disks (VM1 and VM2)...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image1 = Image.from_iso(iso, output_disk="net_demo_vm1.qcow2", use_cache=True)
    image2 = Image.from_iso(iso, output_disk="net_demo_vm2.qcow2", use_cache=True)

    vm1 = Machine(image1, ram_mb=2048, cpus=2)
    vm2 = Machine(image2, ram_mb=2048, cpus=2)

    try:
        # 3. Power on VM1 and VM2
        print("\n3. Powering on VM1...")
        vm1.run(timeout=180)
        print("   ✓ VM1 is online and responding.")

        print("\n4. Powering on VM2...")
        vm2.run(timeout=180)
        print("   ✓ VM2 is online and responding.")

        # 5. Hotplug secondary NICs on both VMs connected to the same virtual switch
        # Using Intel e1000e (82574L) which has native signed drivers in all Windows 10/11 versions
        print("\n5. Hotplugging NICs connected to 'lab_net' virtual switch...")
        nic1 = vm1.network.add(
            model=NICModel.E1000E,
            mac="52:54:00:12:34:10",
            switch=switch,
        )
        nic2 = vm2.network.add(
            model=NICModel.E1000E,
            mac="52:54:00:12:34:20",
            switch=switch,
        )
        print(f"   ✓ VM1 NIC: {nic1.id} (MAC: {nic1.mac}, Model: {nic1.model})")
        print(f"   ✓ VM2 NIC: {nic2.id} (MAC: {nic2.mac}, Model: {nic2.model})")

        # 6. Configure static IP addresses inside each Windows guest
        print("\n6. Configuring guest static IP addresses via QGA...")
        nic1.configure(ip="192.168.100.10/24")
        nic2.configure(ip="192.168.100.20/24")
        print("   ✓ VM1 configured with 192.168.100.10/24")
        print("   ✓ VM2 configured with 192.168.100.20/24")

        # 7. Enable ICMP Echo in Windows Firewall on VM2 so it replies to ping
        print("\n7. Enabling ICMP Echo in Windows Firewall on VM2...")
        vm2.firewall.add_rule(
            name="Allow-Ping",
            display_name="Allow Ping ICMPv4",
            protocol="ICMPv4",
            icmp_type=8,
            direction="in",
            action="allow",
        )
        print("   ✓ Firewall rule added via vm.firewall.")

        # 8. Start packet capture on VM1's interface
        pcap_file = "inter_vm_traffic.pcap"
        print(f"\n8. Starting packet capture on VM1 ({nic1.id}) -> {pcap_file}...")
        with nic1.capture(pcap_file) as cap:
            print(f"   ✓ Packet capture active (pcap: {cap.pcap_path})")

            # VM1 pings VM2 across the virtual switch
            print("   Pinging VM2 (192.168.100.20) from VM1 across the virtual switch...")
            ping_res = vm1.command.run("ping 192.168.100.20 -n 4", timeout=20)
            print(f"   Ping Output:\n{ping_res.stdout.strip()}")
            time.sleep(1)

        print(f"   ✓ Capture stopped. Saved to {pcap_file}")

        # 9. (Optional) Launch live Wireshark view
        # nic1.wireshark()

        # 10. Hot-unplug NICs
        print("\n9. Hot-unplugging NICs...")
        nic1.remove()
        nic2.remove()
        print("   ✓ NICs removed cleanly from running VMs.")

    finally:
        # 11. Clean up VMs and virtual switch
        print("\n10. Shutting down VMs and destroying virtual switch...")
        vm1.power.off()
        vm2.power.off()
        switch.destroy()
        print("   ✓ Done.")


if __name__ == "__main__":
    main()
