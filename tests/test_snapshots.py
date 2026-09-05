"""Unit tests for SnapshotController (machine.snapshot)."""

from unittest.mock import patch

import pytest

from windows.image import Image
from windows.machine import Machine
from windows.qemu import create_qcow2_disk
from windows.snapshot import SnapshotController, SnapshotError, SnapshotInfo


def test_snapshot_info_dataclass():
    snap_live = SnapshotInfo(id="1", name="clean_install", date_sec=1788614139, vm_clock_sec=120, vm_state_size=524288)
    assert snap_live.id == "1"
    assert snap_live.name == "clean_install"
    assert snap_live.date_sec == 1788614139
    assert snap_live.vm_clock_sec == 120
    assert snap_live.vm_state_size == 524288
    assert snap_live.is_live is True
    assert snap_live.type == "live"

    snap_dict = snap_live.to_dict()
    assert snap_dict["id"] == "1"
    assert snap_dict["name"] == "clean_install"
    assert snap_dict["is_live"] is True
    assert snap_dict["type"] == "live"
    assert snap_dict["vm_state_size"] == 524288
    assert "clean_install" in repr(snap_live)
    assert "type='live'" in repr(snap_live)

    snap_disk = SnapshotInfo(id="2", name="disk_only_snap", vm_state_size=0)
    assert snap_disk.is_live is False
    assert snap_disk.type == "disk-only"
    assert "type='disk-only'" in repr(snap_disk)


def test_snapshot_offline_lifecycle_and_listing(tmp_path):
    disk_path = tmp_path / "vm_disk.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    img = Image(disk_path)
    mach = Machine(img)

    assert isinstance(mach.snapshot, SnapshotController)
    assert len(mach.snapshot.list()) == 0
    assert not mach.snapshot.exists("initial_state")

    # 1. Create snapshot 1
    snap1 = mach.snapshot.create("initial_state")
    assert isinstance(snap1, SnapshotInfo)
    assert snap1.name == "initial_state"
    assert mach.snapshot.exists("initial_state")

    # 2. Create snapshot 2
    snap2 = mach.snapshot.create("after_update")
    assert isinstance(snap2, SnapshotInfo)
    assert snap2.name == "after_update"

    # 3. List snapshots
    snapshots = mach.snapshot.list()
    assert len(snapshots) == 2
    names = [s.name for s in snapshots]
    assert "initial_state" in names
    assert "after_update" in names

    # 4. Get specific snapshot
    retrieved = mach.snapshot.get("initial_state")
    assert retrieved is not None
    assert retrieved.name == "initial_state"

    # 5. Revert snapshot
    mach.snapshot.revert("initial_state")

    # 6. Delete snapshot
    mach.snapshot.delete("after_update")
    assert not mach.snapshot.exists("after_update")
    assert len(mach.snapshot.list()) == 1


def test_snapshot_live_running_vm(tmp_path):
    disk_path = tmp_path / "vm_live.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    img = Image(disk_path)
    mach = Machine(img)

    with (
        patch.object(mach._process_manager, "is_running", return_value=True),
        patch.object(mach.console, "send_monitor_command") as mock_send,
    ):
        mock_send.return_value = ""

        # Live snapshot creation
        mach.snapshot.create("live_checkpoint")
        mock_send.assert_called_with("savevm live_checkpoint", timeout=60.0)

        # Mock exists returning True
        with patch.object(mach.snapshot, "exists", return_value=True):
            # Live snapshot revert
            mach.snapshot.revert("live_checkpoint")
            mock_send.assert_called_with("loadvm live_checkpoint", timeout=60.0)

            # Live snapshot delete
            mach.snapshot.delete("live_checkpoint")
            mock_send.assert_called_with("delvm live_checkpoint", timeout=30.0)


def test_snapshot_fork_new_machine(tmp_path):
    source_disk = tmp_path / "base_machine.qcow2"
    create_qcow2_disk(source_disk, size="10M")

    base_machine = Machine(source_disk, ram_mb=2048, cpus=2, headless=True)

    # Create a snapshot to fork from
    base_machine.snapshot.create("checkpoint_v1")
    assert base_machine.snapshot.exists("checkpoint_v1")

    # 1. Fork with explicit output_disk
    forked_disk = tmp_path / "forked_machine.qcow2"
    forked_machine = base_machine.snapshot.fork(
        "checkpoint_v1",
        output_disk=forked_disk,
        ram_mb=4096,
        cpus=4,
    )

    assert isinstance(forked_machine, Machine)
    assert forked_machine.image.disk_path == forked_disk.resolve()
    assert forked_machine.image.exists()
    assert forked_machine.ram_mb == 4096
    assert forked_machine.cpus == 4
    assert forked_machine.headless is True

    # 2. Fork with auto-generated output_disk and inherited settings
    auto_forked = base_machine.snapshot.fork("checkpoint_v1")
    assert isinstance(auto_forked, Machine)
    assert auto_forked.image.exists()
    assert auto_forked.ram_mb == 2048
    assert auto_forked.cpus == 2
    assert "checkpoint_v1" in auto_forked.image.name


def test_snapshot_error_cases(tmp_path):
    disk_path = tmp_path / "vm_errors.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    mach = Machine(disk_path)
    mach.snapshot.create("snap1")

    # Duplicate creation error
    with pytest.raises(SnapshotError, match="already exists"):
        mach.snapshot.create("snap1")

    # Revert non-existent error
    with pytest.raises(SnapshotError, match="does not exist"):
        mach.snapshot.revert("nonexistent")

    # Delete non-existent error
    with pytest.raises(SnapshotError, match="does not exist"):
        mach.snapshot.delete("nonexistent")

    # Fork non-existent error
    with pytest.raises(SnapshotError, match="does not exist"):
        mach.snapshot.fork("nonexistent")

    # Monitor error in live mode
    with (
        patch.object(mach._process_manager, "is_running", return_value=True),
        patch.object(mach.console, "send_monitor_command", return_value="Error: device does not support snapshot"),
    ):
        with pytest.raises(SnapshotError, match="Failed to create live VM snapshot"):
            mach.snapshot.create("failing_live_snap")


def test_snapshot_tree_rendering(tmp_path):
    disk_path = tmp_path / "vm_tree.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    mach = Machine(disk_path)
    # Empty tree
    assert "no snapshots" in mach.snapshot.tree()

    mach.snapshot.create("base")
    mach.snapshot.create("updated")

    tree_str = mach.snapshot.tree()
    assert "vm_tree.qcow2" in tree_str
    assert "base" in tree_str
    assert "updated" in tree_str
    assert "disk-only" in tree_str

    # list(tree=True)
    tree_via_list = mach.snapshot.list(tree=True)
    assert tree_via_list == tree_str


def test_machine_fork_method(tmp_path):
    disk_path = tmp_path / "vm_fork_test.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    mach = Machine(disk_path, ram_mb=1024, cpus=1)

    # Fork with auto-created snapshot and run=False
    forked = mach.fork(output_disk=tmp_path / "forked_out.qcow2", run=False)
    assert isinstance(forked, Machine)
    assert forked.image.exists()
    assert forked.ram_mb == 1024
    assert forked.cpus == 1
    assert forked.power.status == "stopped"


def test_machine_pause_resume_kill(tmp_path):
    disk_path = tmp_path / "vm_power_test.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    mach = Machine(disk_path)

    with (
        patch.object(mach._process_manager, "start") as mock_start,
        patch.object(mach._process_manager, "is_running", return_value=True),
        patch.object(mach.console, "monitor_stop") as mock_m_stop,
        patch.object(mach.console, "monitor_continue") as mock_m_cont,
        patch.object(mach._process_manager, "kill") as mock_kill,
    ):
        mach.power.start()
        mock_start.assert_called_once()
        assert mach.power.status == "running"

        # Pause
        mach.pause()
        mock_m_stop.assert_called_once()
        assert mach.power.status == "paused"

        # Resume / Unpause
        mach.unpause()
        mock_m_cont.assert_called_once()
        assert mach.power.status == "running"

        # Kill
        mach.kill()
        mock_kill.assert_called_once()


def test_snapshot_parent_chain_and_revert(tmp_path):
    disk_path = tmp_path / "vm_chain.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    mach = Machine(disk_path)

    # 1. Create a chain of 3 snapshots: s1 -> s2 -> s3
    s1 = mach.snapshot.create("s1")
    s2 = mach.snapshot.create("s2")
    s3 = mach.snapshot.create("s3")

    assert s1.name == "s1"
    assert s2.name == "s2"
    assert s3.name == "s3"

    # 2. Test list object and hierarchy traversal
    snap_list = mach.snapshot.list()
    assert isinstance(snap_list, list)
    assert len(snap_list) == 3

    # __str__ and __repr__ return the ASCII tree
    assert "Disk: vm_chain.qcow2" in str(snap_list)
    assert "s1" in repr(snap_list)
    assert "s2" in repr(snap_list)
    assert "s3" in repr(snap_list)

    # Dict-like indexing and int indexing
    assert snap_list[0].name == "s1"
    assert snap_list["s1"].name == "s1"
    assert snap_list["s2"].name == "s2"
    assert "s2" in snap_list

    # Traversal from list object
    assert snap_list.current is not None
    assert snap_list.current.name == "s3"
    assert snap_list.parent is not None
    assert snap_list.parent.name == "s2"
    assert snap_list.parent.parent is not None
    assert snap_list.parent.parent.name == "s1"
    assert snap_list.parent.parent.parent is None

    # Children traversal
    assert [c.name for c in snap_list["s1"].children] == ["s2"]
    assert [c.name for c in snap_list["s2"].children] == ["s3"]
    assert snap_list["s3"].children == []

    # 3. Test m.snapshot.list().parent.parent.revert()
    p = mach.snapshot.list().parent
    assert p is not None and p.parent is not None
    p.parent.revert()

    # After reverting to s1, current is now s1
    assert mach.snapshot.current is not None
    assert mach.snapshot.current.name == "s1"
    assert mach.snapshot.list().parent is None

    # 4. Create snapshot s4 on top of s1 (branching)
    mach.snapshot.create("s4")

    # Now s1 has two children: s2 and s4
    reloaded_list = mach.snapshot.list()
    assert reloaded_list.current is not None
    assert reloaded_list.current.name == "s4"
    assert reloaded_list.parent is not None
    assert reloaded_list.parent.name == "s1"

    s1_node = reloaded_list["s1"]
    child_names = [c.name for c in s1_node.children]
    assert "s2" in child_names
    assert "s4" in child_names

    # Direct snapshot method calls
    reloaded_list["s2"].revert()
    assert mach.snapshot.current is not None
    assert mach.snapshot.current.name == "s2"


def test_snapshot_item_methods(tmp_path):
    disk_path = tmp_path / "vm_item.qcow2"
    create_qcow2_disk(disk_path, size="10M")

    mach = Machine(disk_path)
    s1 = mach.snapshot.create("snap1")

    # Item revert
    s1.revert()

    # Item fork
    forked = s1.fork(output_disk=tmp_path / "forked_snap1.qcow2")
    assert isinstance(forked, Machine)
    assert forked.image.exists()

    # Item delete
    s1.delete()
    assert len(mach.snapshot.list()) == 0
