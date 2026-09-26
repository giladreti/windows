"""Unit tests for user execution contexts (as_user, default_user, schtasks runner)."""

import json
from unittest.mock import MagicMock

import pytest

from windows.executor import CommandController
from windows.machine import Machine, MachineUserContext


def test_command_controller_default_system():
    mock_qga = MagicMock()
    mock_qga.exec.return_value = ("nt authority\\system\n", "", 0)

    cmd = CommandController(qga_socket_path="/tmp/test.sock")
    cmd.qga = mock_qga

    res = cmd.run("whoami")
    assert res.stdout.strip() == "nt authority\\system"
    assert res.returncode == 0

    # Ensure command was sent directly without schtasks wrapper
    call_arg = mock_qga.exec.call_args[1]["command"]
    assert call_arg == "whoami"


def test_command_controller_user_requires_password():
    cmd = CommandController(qga_socket_path="/tmp/test.sock")

    # Specifying user without password must raise ValueError
    with pytest.raises(ValueError, match="Password must be provided when executing commands as user 'Alice'"):
        cmd.run("whoami", user="Alice")

    # as_user without password must raise ValueError
    with pytest.raises(ValueError, match="Password must be provided when executing commands as user 'Alice'"):
        cmd.as_user("Alice")


def test_command_controller_explicit_user_execution():
    mock_qga = MagicMock()
    fake_json = json.dumps({"stdout": "win-vm\\alice\n", "stderr": "", "returncode": 0})
    mock_qga.exec.return_value = (fake_json, "", 0)

    cmd = CommandController(qga_socket_path="/tmp/test.sock")
    cmd.qga = mock_qga

    res = cmd.run("whoami", user="Alice", password="SecretPassword123!")
    assert res.stdout.strip() == "win-vm\\alice"
    assert res.returncode == 0

    # Verify task runner script was generated and sent
    call_arg = mock_qga.exec.call_args[1]["command"]
    assert "Register-ScheduledTask" in call_arg
    assert "Start-ScheduledTask" in call_arg
    assert "Unregister-ScheduledTask" in call_arg


def test_command_controller_as_user_factory():
    mock_qga = MagicMock()
    fake_json = json.dumps({"stdout": "win-vm\\bob\n", "stderr": "", "returncode": 0})
    mock_qga.exec.return_value = (fake_json, "", 0)

    cmd = CommandController(
        qga_socket_path="/tmp/test.sock",
        default_user="Bob",
        default_password="BobPassword123!",
    )
    cmd.qga = mock_qga

    bob_view = cmd.as_user()
    assert bob_view.user == "Bob"
    assert bob_view.password == "BobPassword123!"

    res = bob_view.run("whoami")
    assert res.stdout.strip() == "win-vm\\bob"


def test_command_controller_as_user_context_manager():
    mock_qga = MagicMock()

    def fake_exec(command, powershell=True, timeout=60):
        if "Register-ScheduledTask" in command:
            return (json.dumps({"stdout": "win-vm\\charlie\n", "stderr": "", "returncode": 0}), "", 0)
        return ("nt authority\\system\n", "", 0)

    mock_qga.exec.side_effect = fake_exec

    cmd = CommandController(qga_socket_path="/tmp/test.sock")
    cmd.qga = mock_qga

    # Outside: SYSTEM
    assert cmd.run("whoami").stdout.strip() == "nt authority\\system"

    # Inside context manager: Charlie
    with cmd.as_user("Charlie", password="CharliePassword123!"):
        assert cmd.run("whoami").stdout.strip() == "win-vm\\charlie"

        # Explicit as_system inside user block overrides user scope
        assert cmd.run("whoami", as_system=True).stdout.strip() == "nt authority\\system"
        assert cmd.as_system.run("whoami").stdout.strip() == "nt authority\\system"

    # Reverted back to SYSTEM outside context
    assert cmd.run("whoami").stdout.strip() == "nt authority\\system"


def test_machine_as_user_context_and_factory(tmp_path):
    mock_qga = MagicMock()

    def fake_exec(command, powershell=True, timeout=60):
        if "Register-ScheduledTask" in command:
            return (json.dumps({"stdout": "win-vm\\administrator\n", "stderr": "", "returncode": 0}), "", 0)
        return ("nt authority\\system\n", "", 0)

    mock_qga.exec.side_effect = fake_exec

    fake_disk = tmp_path / "test.qcow2"
    fake_disk.write_bytes(b"dummy")

    machine = Machine(
        image=fake_disk,
        default_user="Administrator",
        default_password="AdminPassword123!",
    )
    machine.command.qga = mock_qga

    assert machine.default_user == "Administrator"
    assert machine.default_password == "AdminPassword123!"

    # 1. As factory:
    admin_ctx = machine.as_user()
    assert isinstance(admin_ctx, MachineUserContext)
    assert admin_ctx.user == "Administrator"
    assert admin_ctx.run("whoami").stdout.strip() == "win-vm\\administrator"

    # 2. As context manager:
    assert machine.command.run("whoami").stdout.strip() == "nt authority\\system"
    with machine.as_user():
        assert machine.command.run("whoami").stdout.strip() == "win-vm\\administrator"
    assert machine.command.run("whoami").stdout.strip() == "nt authority\\system"
