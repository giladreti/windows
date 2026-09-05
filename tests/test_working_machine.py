"""Test to verify machine.power.on() creates an active, working QEMU process without defunct state."""

import time

from windows.image import Image
from windows.machine import Machine
from windows.qemu import create_qcow2_disk


def test_machine_power_on_creates_working_process(tmp_path):
    """Test that machine.power.on() starts a healthy, active QEMU process that is NOT defunct."""
    disk_path = tmp_path / "working_machine_test.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    img = Image(disk_path=disk_path)
    machine = Machine(img, ram_mb=512, cpus=1, headless=True)

    # 1. Power ON
    machine.power.on()

    # 2. Verify VM is actively running (NOT defunct/dead)
    assert machine.power.status == "running"
    assert machine._process_manager.is_running() is True
    assert machine._process_manager.process is not None
    assert machine._process_manager.process.poll() is None  # Active process in OS process table

    # 3. Allow it to run for a short duration
    time.sleep(1)
    assert machine._process_manager.is_running() is True
    assert machine._process_manager.process.poll() is None

    # 4. Power OFF cleanly
    machine.power.off()

    # 5. Verify process stopped and reaped
    assert machine.power.status == "stopped"
    assert machine._process_manager.is_running() is False
