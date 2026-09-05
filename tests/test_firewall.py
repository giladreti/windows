"""Unit tests for Windows Defender Firewall abstraction (rules, profiles, and per-NIC categories)."""

import json
from unittest.mock import MagicMock

import pytest

from windows.executor import CommandController, CommandResult
from windows.firewall import (
    FirewallAction,
    FirewallController,
    FirewallDirection,
    FirewallError,
    FirewallProfile,
    FirewallRule,
    NetworkCategory,
)
from windows.image import Image
from windows.machine import Machine


@pytest.fixture
def mock_machine(tmp_path):
    disk = tmp_path / "win.qcow2"
    disk.write_bytes(b"DATA")
    img = Image(disk)
    m = Machine(img)
    m.command = MagicMock(spec=CommandController)
    m.power._manager.is_running = MagicMock(return_value=True)
    return m


def test_firewall_rule_model_and_enums():
    rule = FirewallRule(
        name="AllowHTTP",
        display_name="Allow HTTP Inbound",
        description="Web traffic",
        direction=FirewallDirection.INBOUND,
        action=FirewallAction.ALLOW,
        enabled=True,
        profile=FirewallProfile.ANY,
        protocol="TCP",
        local_port="80",
    )
    assert rule.name == "AllowHTTP"
    assert rule.direction == "Inbound"
    assert rule.action == "Allow"
    assert rule.enabled is True
    assert rule.protocol == "TCP"
    assert rule.local_port == "80"
    assert "AllowHTTP" in repr(rule)


def test_firewall_status_and_is_enabled(mock_machine):
    fw = mock_machine.firewall
    assert isinstance(fw, FirewallController)

    mock_profile_json = json.dumps(
        [
            {"Name": "Domain", "Enabled": 1},
            {"Name": "Private", "Enabled": 1},
            {"Name": "Public", "Enabled": 0},
        ]
    )
    mock_machine.command.run.return_value = CommandResult(
        returncode=0, stdout=mock_profile_json, stderr=""
    )

    status = fw.status()
    assert status == {"domain": True, "private": True, "public": False}
    assert fw.is_enabled("domain") is True
    assert fw.is_enabled("public") is False
    assert fw.is_enabled("all") is False
    assert fw.is_enabled("any") is True


def test_firewall_on_and_off(mock_machine):
    fw = mock_machine.firewall
    mock_machine.command.run.return_value = CommandResult(returncode=0, stdout="", stderr="")

    fw.on("all")
    mock_machine.command.run.assert_called_with(
        "Set-NetFirewallProfile -Profile 'Domain,Private,Public' -Enabled True",
        auto_retry=False,
    )

    fw.off("public")
    mock_machine.command.run.assert_called_with(
        "Set-NetFirewallProfile -Profile 'Public' -Enabled False",
        auto_retry=False,
    )

    fw.enable("private")
    mock_machine.command.run.assert_called_with(
        "Set-NetFirewallProfile -Profile 'Private' -Enabled True",
        auto_retry=False,
    )

    fw.disable()
    mock_machine.command.run.assert_called_with(
        "Set-NetFirewallProfile -Profile 'Domain,Private,Public' -Enabled False",
        auto_retry=False,
    )


def test_firewall_set_and_get_profile_for_nic(mock_machine):
    fw = mock_machine.firewall
    nic = mock_machine.network.default

    # 1. Set profile via firewall controller
    mock_machine.command.run.return_value = CommandResult(returncode=0, stdout="", stderr="")
    fw.set_profile(nic, "Private")
    call_script = mock_machine.command.run.call_args[0][0]
    assert "Set-NetConnectionProfile" in call_script
    assert "-NetworkCategory 'Private'" in call_script

    # 2. Set profile directly via NIC method
    nic.set_profile(NetworkCategory.PUBLIC)
    call_script2 = mock_machine.command.run.call_args[0][0]
    assert "-NetworkCategory 'Public'" in call_script2

    # 3. Get profile via NIC method
    mock_machine.command.run.return_value = CommandResult(
        returncode=0, stdout="Private\n", stderr=""
    )
    prof = nic.get_profile()
    assert prof == "Private"


def test_firewall_list_connection_profiles(mock_machine):
    fw = mock_machine.firewall
    mock_json = json.dumps(
        [
            {
                "Name": "Network",
                "InterfaceAlias": "Ethernet",
                "InterfaceIndex": 12,
                "NetworkCategory": "Private",
                "IPv4Connectivity": "Internet",
            }
        ]
    )
    mock_machine.command.run.return_value = CommandResult(returncode=0, stdout=mock_json, stderr="")

    profiles = fw.list_connection_profiles()
    assert len(profiles) == 1
    assert profiles[0]["InterfaceAlias"] == "Ethernet"
    assert profiles[0]["NetworkCategory"] == "Private"


def test_firewall_add_rule(mock_machine):
    fw = mock_machine.firewall

    # Mock response for get_rule after creation
    rule_data = {
        "Name": "AllowHTTPS",
        "DisplayName": "Allow Web HTTPS",
        "Description": "Secure web traffic",
        "Direction": "Inbound",
        "Action": "Allow",
        "Enabled": True,
        "Profile": "Any",
        "Protocol": "TCP",
        "LocalPort": "443",
        "RemotePort": "Any",
        "LocalAddress": "Any",
        "RemoteAddress": "Any",
        "Program": "Any",
        "Service": "Any",
    }
    mock_machine.command.run.side_effect = [
        CommandResult(returncode=0, stdout="", stderr=""),  # New-NetFirewallRule
        CommandResult(returncode=0, stdout=json.dumps(rule_data), stderr=""),  # Get-NetFirewallRule
    ]

    rule = fw.add_rule(
        name="AllowHTTPS",
        display_name="Allow Web HTTPS",
        direction="in",
        action="allow",
        protocol="TCP",
        local_port=443,
        description="Secure web traffic",
    )

    assert isinstance(rule, FirewallRule)
    assert rule.name == "AllowHTTPS"
    assert rule.local_port == "443"
    assert rule.action == "Allow"

    create_call = mock_machine.command.run.call_args_list[0][0][0]
    assert "New-NetFirewallRule" in create_call
    assert "-Name 'AllowHTTPS'" in create_call
    assert "-Direction Inbound" in create_call
    assert "-Action Allow" in create_call
    assert "-Protocol 'TCP'" in create_call


def test_firewall_list_rules_and_filtering(mock_machine):
    fw = mock_machine.firewall

    rules_json = json.dumps(
        [
            {
                "Name": "Rule1",
                "DisplayName": "Rule 1",
                "Description": "",
                "Direction": "Inbound",
                "Action": "Allow",
                "Enabled": True,
                "Profile": "Private",
                "Protocol": "TCP",
                "LocalPort": "80",
                "RemotePort": "Any",
                "LocalAddress": "Any",
                "RemoteAddress": "Any",
                "Program": "Any",
                "Service": "Any",
            },
            {
                "Name": "Rule2",
                "DisplayName": "Rule 2",
                "Description": "",
                "Direction": "Outbound",
                "Action": "Block",
                "Enabled": False,
                "Profile": "Public",
                "Protocol": "UDP",
                "LocalPort": "53",
                "RemotePort": "Any",
                "LocalAddress": "Any",
                "RemoteAddress": "Any",
                "Program": "Any",
                "Service": "Any",
            },
        ]
    )

    mock_machine.command.run.return_value = CommandResult(
        returncode=0, stdout=rules_json, stderr=""
    )

    # All rules
    all_rules = fw.list_rules()
    assert len(all_rules) == 2
    assert all_rules[0].name == "Rule1"
    assert all_rules[1].name == "Rule2"

    # Filtered by protocol
    tcp_rules = fw.list_rules(protocol="TCP")
    assert len(tcp_rules) == 1
    assert tcp_rules[0].name == "Rule1"

    # Filtered by port
    dns_rules = fw.list_rules(local_port=53)
    assert len(dns_rules) == 1
    assert dns_rules[0].name == "Rule2"


def test_firewall_get_rule_and_modify(mock_machine):
    fw = mock_machine.firewall

    initial_rule = {
        "Name": "CustomApp",
        "DisplayName": "Custom Application",
        "Description": "Original",
        "Direction": "Inbound",
        "Action": "Allow",
        "Enabled": True,
        "Profile": "Any",
        "Protocol": "TCP",
        "LocalPort": "9000",
        "RemotePort": "Any",
        "LocalAddress": "Any",
        "RemoteAddress": "Any",
        "Program": "Any",
        "Service": "Any",
    }
    modified_rule = dict(initial_rule, Action="Block", LocalPort="9001")

    mock_machine.command.run.side_effect = [
        CommandResult(returncode=0, stdout=json.dumps(initial_rule), stderr=""),  # get_rule
        CommandResult(returncode=0, stdout="", stderr=""),  # modify Set-NetFirewallRule
        CommandResult(returncode=0, stdout=json.dumps(modified_rule), stderr=""),  # get updated
    ]

    rule = fw.get_rule("CustomApp")
    assert rule is not None
    assert rule.name == "CustomApp"
    assert rule.action == "Allow"

    # Modify through rule object
    rule.modify(action="block", local_port=9001)
    assert rule.action == "Block"
    assert rule.local_port == "9001"


def test_firewall_remove_and_enable_disable_rule(mock_machine):
    fw = mock_machine.firewall
    mock_machine.command.run.return_value = CommandResult(returncode=0, stdout="", stderr="")

    # Remove rule
    res = fw.remove_rule("TestRule")
    assert res is True
    assert "Remove-NetFirewallRule -Name 'TestRule'" in mock_machine.command.run.call_args[0][0]

    # Enable rule
    res_en = fw.enable_rule("TestRule")
    assert res_en is True
    assert "Enable-NetFirewallRule -Name 'TestRule'" in mock_machine.command.run.call_args[0][0]

    # Disable rule
    res_dis = fw.disable_rule("TestRule")
    assert res_dis is True
    assert "Disable-NetFirewallRule -Name 'TestRule'" in mock_machine.command.run.call_args[0][0]


def test_firewall_error_handling(mock_machine):
    fw = mock_machine.firewall
    mock_machine.command.run.return_value = CommandResult(
        returncode=1, stdout="", stderr="Cannot find rule"
    )

    with pytest.raises(FirewallError, match="Cannot find rule"):
        fw.remove_rule("NonExistent")

    with pytest.raises(FirewallError):
        fw.on("invalid")
