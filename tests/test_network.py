"""Unit tests for virtual networking, NIC hotplugging, IP config, VirtualSwitch, and packet capture."""

from unittest.mock import MagicMock, patch

import pytest

from windows.executor import CommandController, CommandResult
from windows.image import Image
from windows.machine import Machine
from windows.network import (
    NetworkController,
    NetworkError,
    NetworkInterface,
    NICModel,
    PacketCapture,
    VirtualSwitch,
    generate_mac,
    normalize_mac,
)


@pytest.fixture
def mock_machine(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"DATA")
    img = Image(disk)
    m = Machine(img)
    m.command = MagicMock(spec=CommandController)
    m.power._manager.is_running = MagicMock(return_value=True)
    return m


def test_nic_models_and_mac_generator():
    assert NICModel.E1000E.value == "e1000e"
    assert NICModel.VIRTIO.value == "virtio-net-pci"
    assert NICModel.RTL8139.value == "rtl8139"
    assert NICModel.E1000.value == "e1000"
    assert NICModel.VMXNET3.value == "vmxnet3"

    mac = generate_mac()
    assert mac.startswith("52:54:00:")
    assert len(mac.split(":")) == 6

    # Normalization
    assert normalize_mac("52-54-00-12-34-56") == "52:54:00:12:34:56"
    assert normalize_mac("52:54:00:12:34:56") == "52:54:00:12:34:56"
    assert normalize_mac("525400123456") == "52:54:00:12:34:56"
    with pytest.raises(ValueError, match="Invalid MAC address"):
        normalize_mac("invalid")


def test_network_controller_initial_state(mock_machine):
    net = mock_machine.network
    assert isinstance(net, NetworkController)
    assert len(net.list()) == 1

    default_nic = net.default
    assert default_nic.id == "nic0"
    assert default_nic.netdev_id == "net0"
    assert default_nic.model == NICModel.E1000
    assert default_nic.mac == "52:54:00:12:34:50"

    assert net["nic0"] is default_nic
    assert net[0] is default_nic
    assert net["52:54:00:12:34:50"] is default_nic


def test_nic_hotplug_add_and_remove(mock_machine):
    net = mock_machine.network
    with patch.object(net.qmp, "execute") as mock_qmp:
        # Hotplug new e1000e NIC
        nic = net.add(
            model=NICModel.E1000E,
            mac="52:54:00:aa:bb:cc",
            id="test_nic",
        )

        assert isinstance(nic, NetworkInterface)
        assert nic.id == "test_nic"
        assert nic.model == NICModel.E1000E
        assert nic.mac == "52:54:00:AA:BB:CC"
        assert nic.bus == "rp1"
        assert len(net.list()) == 2

        # Verify QMP calls
        mock_qmp.assert_any_call("netdev_add", {"id": "net1", "type": "user"})
        mock_qmp.assert_any_call(
            "device_add",
            {
                "driver": "e1000e",
                "netdev": "net1",
                "id": "test_nic",
                "mac": "52:54:00:AA:BB:CC",
                "bus": "rp1",
            },
        )

        # Unplug NIC
        nic.remove()
        assert len(net.list()) == 1
        mock_qmp.assert_any_call("device_del", {"id": "test_nic"})
        mock_qmp.assert_any_call("netdev_del", {"id": "net1"})


def test_nic_configure_guest_ip_and_dns(mock_machine):
    net = mock_machine.network
    nic = net.default

    mock_machine.command.run.return_value = CommandResult(stdout="OK", stderr="", returncode=0)

    # 1. Configure static IP and Gateway
    nic.configure(ip="192.168.100.15/24", gateway="192.168.100.1", dns=["8.8.8.8", "1.1.1.1"])

    assert mock_machine.command.run.called
    ps_cmd = mock_machine.command.run.call_args[0][0]
    assert "192.168.100.15" in ps_cmd
    assert "-PrefixLength 24" in ps_cmd
    assert "-DefaultGateway '192.168.100.1'" in ps_cmd
    assert "'8.8.8.8', '1.1.1.1'" in ps_cmd
    assert "52:54:00:12:34:50" in ps_cmd

    # 2. Reset to DHCP
    nic.set_dhcp()
    ps_dhcp = mock_machine.command.run.call_args[0][0]
    assert "-Dhcp Enabled" in ps_dhcp
    assert "-ResetServerAddresses" in ps_dhcp

    # 3. Query guest config
    mock_machine.command.run.return_value = CommandResult(
        stdout='{"Name":"Ethernet","IPAddresses":["192.168.100.15"],"Gateway":"192.168.100.1","DNSServers":["8.8.8.8"],"Status":"Up"}',
        stderr="",
        returncode=0,
    )
    conf = nic.get_guest_config()
    assert conf["Name"] == "Ethernet"
    assert conf["IPAddresses"] == ["192.168.100.15"]
    assert conf["Gateway"] == "192.168.100.1"


def test_virtual_switch_socket_mode(mock_machine):
    switch = VirtualSwitch("lan0", mode="socket")
    assert switch.name == "lan0"
    assert switch.mode == "socket"
    assert hasattr(switch, "switch_port")

    net = mock_machine.network
    with patch.object(net.qmp, "execute") as mock_qmp:
        nic = net.add(model=NICModel.E1000E, switch=switch)
        assert nic.switch is switch
        assert nic in switch.attached_nics
        assert nic._client_port is not None

        mock_qmp.assert_any_call(
            "netdev_add",
            {
                "id": "net1",
                "type": "socket",
                "udp": f"127.0.0.1:{switch.switch_port}",
                "localaddr": f"127.0.0.1:{nic._client_port}",
            },
        )

    switch.destroy()


def test_virtual_switch_mcast_mode(mock_machine):
    switch = VirtualSwitch("lan0", mode="mcast", mcast_port=43210)
    assert switch.name == "lan0"
    assert switch.mode == "mcast"
    assert switch.mcast_port == 43210

    net = mock_machine.network
    with patch.object(net.qmp, "execute") as mock_qmp:
        nic = net.add(model=NICModel.E1000E, switch=switch)
        assert nic.switch is switch
        assert nic in switch.attached_nics

        mock_qmp.assert_any_call("netdev_add", {"id": "net1", "type": "socket", "mcast": "230.0.0.1:43210"})

    switch.destroy()


def test_virtual_switch_bridge_mode(mock_machine):
    with patch("os.geteuid", return_value=0), patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        switch = VirtualSwitch("br0", mode="bridge")
        assert switch.mode == "bridge"
        net = mock_machine.network
        with patch.object(net.qmp, "execute") as mock_qmp:
            nic = net.add(model=NICModel.E1000E, switch=switch)
            assert nic.switch is switch
            assert nic._tap_name is not None
            mock_qmp.assert_any_call(
                "netdev_add",
                {
                    "id": "net1",
                    "type": "tap",
                    "ifname": nic._tap_name,
                    "script": "no",
                    "downscript": "no",
                },
            )
        switch.destroy()


def test_packet_capture_qmp_filter_dump(mock_machine, tmp_path):
    net = mock_machine.network
    nic = net.default

    pcap_out = tmp_path / "traffic.pcap"

    with patch.object(net.qmp, "execute") as mock_qmp:
        with nic.capture(pcap_out) as cap:
            assert isinstance(cap, PacketCapture)
            assert cap.is_active is True
            assert cap.output_path == pcap_out

            # Verify object-add filter-dump called
            mock_qmp.assert_any_call(
                "object-add",
                {
                    "qom-type": "filter-dump",
                    "id": cap._filter_id,
                    "netdev": "net0",
                    "file": str(pcap_out),
                },
            )

        # Verify object-del called on exit
        assert cap.is_active is False
        mock_qmp.assert_any_call("object-del", {"id": cap._filter_id})


def test_packet_capture_process_permission_error(tmp_path):
    pcap_out = tmp_path / "traffic.pcap"
    mock_proc = MagicMock()
    mock_proc.terminate.side_effect = PermissionError("Permission denied")
    mock_proc.kill.side_effect = PermissionError("Permission denied")
    mock_proc.pid = 99999

    cap = PacketCapture(output_path=pcap_out, process=mock_proc)
    cap.is_active = True

    with patch("subprocess.run") as mock_run:
        result_path = cap.stop()
        assert result_path == pcap_out
        assert cap.is_active is False
        assert mock_proc.terminate.called
        assert mock_proc.kill.called
        assert mock_run.called


def test_wireshark_launch(mock_machine, tmp_path):
    nic = mock_machine.network.default
    pcap_out = tmp_path / "sample.pcap"
    pcap_out.write_bytes(b"PCAP_DATA")

    with (
        patch("shutil.which", return_value="/usr/bin/wireshark"),
        patch("subprocess.Popen") as mock_popen,
        patch.object(mock_machine.network.qmp, "execute"),
    ):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        cap = nic.capture(pcap_out, live=True)
        cap.start()

        assert mock_popen.called
        call_args = mock_popen.call_args[0][0]
        assert "/usr/bin/wireshark" in call_args[0]
        assert str(pcap_out) in call_args
        cap.stop()


def test_nic_hotplug_custom_bus(mock_machine):
    net = mock_machine.network
    with patch.object(net.qmp, "execute") as mock_qmp:
        nic = net.add(model=NICModel.E1000, bus="pci.1")
        assert nic.bus == "pci.1"
        mock_qmp.assert_any_call(
            "device_add",
            {
                "driver": "e1000",
                "netdev": "net1",
                "id": "nic1",
                "mac": nic.mac,
                "bus": "pci.1",
            },
        )


def test_nic_hotplug_query_pci_bus_selection(mock_machine):
    net = mock_machine.network
    fake_pci_data = [
        {
            "bus": 0,
            "devices": [
                {
                    "qdev_id": "rp1",
                    "class_info": {"desc": "PCI bridge"},
                    "pci_bridge": {"devices": [{"qdev_id": "existing_dev"}]},  # busy
                },
                {
                    "qdev_id": "rp2",
                    "class_info": {"desc": "PCI bridge"},
                    "pci_bridge": {"devices": []},  # empty
                },
                {
                    "qdev_id": "pci.1",
                    "class_info": {"desc": "PCI bridge"},
                    "pci_bridge": {"devices": []},  # bridge
                },
            ],
        }
    ]

    with patch.object(net.qmp, "execute", return_value=fake_pci_data):
        # PCIe device should select empty root port rp2
        nic1 = net.add(model=NICModel.E1000E)
        assert nic1.bus == "rp2"

        # Legacy PCI device should select pci.1 bridge
        nic2 = net.add(model=NICModel.RTL8139)
        assert nic2.bus == "pci.1"


def test_nic_hotplug_pcie0_unsupported_error(mock_machine):
    net = mock_machine.network

    def qmp_handler(cmd, args=None):
        if cmd == "device_add":
            raise Exception("Bus 'pcie.0' does not support hotplugging")
        return {}

    with patch.object(net.qmp, "execute", side_effect=qmp_handler):
        with pytest.raises(NetworkError, match=r"Bus 'pcie\.0' does not support hotplugging"):
            net.add(model=NICModel.E1000E)


def test_multiple_machines_distinct_vnc_ports(tmp_path):
    disk1 = tmp_path / "win1.qcow2"
    disk2 = tmp_path / "win2.qcow2"
    disk1.write_bytes(b"DATA1")
    disk2.write_bytes(b"DATA2")
    m1 = Machine(disk1)
    m2 = Machine(disk2)
    assert m1.vnc_port != m2.vnc_port
    assert m1.vnc_display != m2.vnc_display
    m1.close()
    m2.close()
