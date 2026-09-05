"""Unit tests for offline disk inspection and NTFS file read/write (Image.disk)."""

import struct
from unittest.mock import MagicMock, patch

import pytest

from windows.disk import (
    DiskController,
    ImageFileController,
    ImagePath,
    MountedPartition,
    PartitionInfo,
    normalize_ntfs_path,
)
from windows.file import RemoteFile
from windows.image import Image


def test_normalize_ntfs_path():
    assert normalize_ntfs_path(r"C:\Windows\System32") == "Windows/System32"
    assert normalize_ntfs_path("C:/Windows/System32/drivers/etc/hosts") == "Windows/System32/drivers/etc/hosts"
    assert normalize_ntfs_path(r"\Users\Public\file.txt") == "Users/Public/file.txt"
    assert normalize_ntfs_path("file.txt") == "file.txt"
    assert normalize_ntfs_path("C:") == ""
    assert normalize_ntfs_path("C:\\") == ""


def test_partition_info_dataclass():
    p = PartitionInfo(index=1, offset=1048576, size=10485760, type_name="NTFS", is_bootable=True, label="OS")
    assert p.index == 1
    assert p.offset == 1048576
    assert p.size == 10485760
    assert p.type_name == "NTFS"
    assert p.is_bootable is True
    assert p.label == "OS"

    d = p.to_dict()
    assert d["index"] == 1
    assert d["offset"] == 1048576
    assert "<Partition #1" in repr(p)


def test_mbr_partition_parsing(tmp_path):
    disk_path = tmp_path / "test_mbr.qcow2"
    disk_path.write_bytes(b"QCOW2")

    # Create dummy MBR with 2 partitions
    mbr = bytearray(512)
    mbr[510:512] = b"\x55\xaa"  # Boot signature

    # Entry 1: LBA start = 2048, sectors = 20480
    mbr[446 : 446 + 16] = struct.pack("<B3sB3sII", 0x80, b"\x00\x00\x00", 0x07, b"\x00\x00\x00", 2048, 20480)
    # Entry 2: LBA start = 22528, sectors = 40960
    mbr[462 : 462 + 16] = struct.pack("<B3sB3sII", 0x00, b"\x00\x00\x00", 0x07, b"\x00\x00\x00", 22528, 40960)

    image = Image(disk_path)
    ctrl = DiskController(image)

    with patch("subprocess.run") as mock_run:
        # Mock qemu-img dd writing mbr to temporary file
        def fake_dd(cmd, **kwargs):
            # Extract output filename: of=...
            for arg in cmd:
                if arg.startswith("of="):
                    out_f = arg.split("of=")[1]
                    with open(out_f, "wb") as f:
                        f.write(mbr)
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_dd

        parts = ctrl.partitions()
        assert len(parts) == 2
        assert parts[0].index == 1
        assert parts[0].offset == 2048 * 512
        assert parts[0].size == 20480 * 512
        assert parts[0].is_bootable is True

        assert parts[1].index == 2
        assert parts[1].offset == 22528 * 512
        assert parts[1].size == 40960 * 512
        assert parts[1].is_bootable is False

        # Default partition is largest (partition 2)
        default_p = ctrl.get_default_partition()
        assert default_p.index == 2


def test_mounted_partition_read_write_list(tmp_path):
    mount_file = tmp_path / "mount.raw"
    mount_file.write_bytes(b"DATA")

    part_info = PartitionInfo(index=1, offset=0, size=1048576, type_name="NTFS")
    proc = MagicMock()
    proc.poll.return_value = None

    mounted = MountedPartition(mount_path=mount_file, partition_info=part_info, process=proc, writable=True)

    # 1. Test read_text / read_bytes using ntfscat mock
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"Hello from NTFS\r\n")
        text = mounted.read_text("test.txt")
        assert text == "Hello from NTFS\r\n"

        data = mounted.read_bytes("test.bin")
        assert data == b"Hello from NTFS\r\n"

    # 2. Test write_text / write_bytes using ntfscp mock
    with patch("subprocess.run") as mock_run, patch("windows.disk._find_binary", return_value="/usr/sbin/ntfscp"):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        mounted.write_text("output.txt", "New content")
        assert mock_run.called

    # 3. Test read-only write rejection
    read_only_mounted = MountedPartition(
        mount_path=mount_file,
        partition_info=part_info,
        process=proc,
        writable=False,
    )
    with pytest.raises(PermissionError, match="mounted read-only"):
        read_only_mounted.write_text("forbidden.txt", "fail")

    # 4. Test list_files using ntfsls mock
    with patch("subprocess.run") as mock_run, patch("windows.disk._find_binary", return_value="/usr/bin/ntfsls"):
        mock_run.return_value = MagicMock(returncode=0, stdout=".\n..\n$MFT\nWindows\nUsers\ntest.txt\n")
        files = mounted.list_files()
        assert "Windows" in files
        assert "Users" in files
        assert "test.txt" in files
        assert "." not in files
        assert "$MFT" not in files

    # 5. Test cleanup / close
    mounted.close()
    proc.terminate.assert_called_once()


def test_image_path_syntax_and_properties(tmp_path):
    disk_path = tmp_path / "vm.qcow2"
    disk_path.write_bytes(b"DATA")
    img = Image(disk_path)

    assert isinstance(img.file, ImageFileController)
    assert repr(img.file) == "<ImageFileController image='vm.qcow2'>"

    # Path building via / operator
    p = img.file / r"C:\Windows\System32\drivers\etc\hosts"
    assert isinstance(p, ImagePath)
    assert p.name == "hosts"
    assert p.stem == "hosts"
    assert p.suffix == ""
    assert p.parts == ("C:\\", "Windows", "System32", "drivers", "etc", "hosts")
    assert p.as_posix() == "C:/Windows/System32/drivers/etc/hosts"
    assert p.parent.clean_path == r"C:\Windows\System32\drivers\etc"
    assert repr(p) == f"<ImagePath '{r'C:\Windows\System32\drivers\etc\hosts'}'>"

    # Path chaining
    child = img.file / "Windows" / "System32" / "cmd.exe"
    assert child.name == "cmd.exe"
    assert child.stem == "cmd"
    assert child.suffix == ".exe"

    # image / path syntax alias
    direct = img / r"C:\Users\Public\script.bat"
    assert isinstance(direct, ImagePath)
    assert direct.name == "script.bat"


def test_image_file_controller_partition_and_context(tmp_path):
    disk_path = tmp_path / "vm.qcow2"
    disk_path.write_bytes(b"DATA")
    img = Image(disk_path)

    # Scoped partition
    p2_ctrl = img.file.partition(2)
    assert p2_ctrl.partition_index == 2
    assert repr(p2_ctrl) == "<ImageFileController image='vm.qcow2' partition=2>"

    call_ctrl = img.file(partition=2)
    assert call_ctrl.partition_index == 2

    # Calling with path
    called_path = img.file(r"C:\test.txt")
    assert isinstance(called_path, ImagePath)
    assert called_path.name == "test.txt"

    # Context manager lifecycle
    fake_mnt = MagicMock()
    with patch.object(img, "mount", return_value=fake_mnt) as mock_mount:
        with img.file as fs:
            assert fs._active_mount is fake_mnt
            (fs / r"C:\test.txt").read_text()
            mock_mount.assert_called_once_with(partition=None, writable=True)
            fake_mnt.read_bytes.assert_called_once()
        fake_mnt.close.assert_called_once()
        assert img.file._active_mount is None


def test_image_path_mocked_operations(tmp_path):
    disk_path = tmp_path / "vm.qcow2"
    disk_path.write_bytes(b"DATA")
    img = Image(disk_path)

    fake_mnt = MagicMock()
    fake_mnt.__enter__.return_value = fake_mnt
    fake_mnt.read_bytes.return_value = b"sample text"
    fake_mnt.list_entries.return_value = {"hosts": False, "etc": True}
    fake_mnt.exists.return_value = True
    fake_mnt.is_dir.return_value = False
    fake_mnt.is_file.return_value = True

    with patch.object(img, "mount", return_value=fake_mnt):
        p = img.file / r"C:\Windows\System32\drivers\etc\hosts"

        assert p.exists() is True
        assert p.is_file() is True
        assert p.is_dir() is False
        assert p.read_text() == "sample text"
        assert p.read_bytes() == b"sample text"

        # Write text and bytes
        p.write_text("updated text")
        fake_mnt.write_bytes.assert_called_with(r"C:\Windows\System32\drivers\etc\hosts", b"updated text")

        p.write_bytes(b"binary data")
        fake_mnt.write_bytes.assert_called_with(r"C:\Windows\System32\drivers\etc\hosts", b"binary data")

        # Download to RemoteFile
        rf = p.download()
        assert isinstance(rf, RemoteFile)
        assert rf.read_text() == "sample text"

        # Download to local file
        local_target = tmp_path / "downloaded_hosts.txt"
        saved = p.download(local_target)
        assert saved == local_target
        assert local_target.read_text() == "sample text"

        # Upload local file
        local_src = tmp_path / "upload_src.txt"
        local_src.write_text("uploaded content")
        p.upload(local_src)
        fake_mnt.write_bytes.assert_called_with(r"C:\Windows\System32\drivers\etc\hosts", b"uploaded content")


def test_image_path_iterdir_mocked(tmp_path):
    disk_path = tmp_path / "vm.qcow2"
    disk_path.write_bytes(b"DATA")
    img = Image(disk_path)

    fake_mnt = MagicMock()
    fake_mnt.__enter__.return_value = fake_mnt
    fake_mnt.exists.return_value = True
    fake_mnt.is_dir.return_value = True
    fake_mnt.list_entries.return_value = {
        "hosts": False,
        "lmhosts.sam": False,
        "networks": False,
    }

    with patch.object(img, "mount", return_value=fake_mnt):
        folder = img.file / r"C:\Windows\System32\drivers\etc"
        items = folder.iterdir()
        assert len(items) == 3
        names = [item.name for item in items]
        assert "hosts" in names
        assert "lmhosts.sam" in names
        assert "networks" in names
        assert all(isinstance(item, ImagePath) for item in items)


def test_image_offline_convenience_methods(tmp_path):
    disk_path = tmp_path / "vm_offline.qcow2"
    disk_path.write_bytes(b"QCOW2_DATA")

    img = Image(disk_path)
    assert isinstance(img.disk, DiskController)
    assert isinstance(img.file, ImageFileController)

    # Mock mount context manager
    fake_mounted = MagicMock()
    fake_mounted.list_files.return_value = ["Windows", "Program Files", "test.txt"]
    fake_mounted.read_text.return_value = "System hosts file"
    fake_mounted.read_bytes.return_value = b"binary_data"
    fake_mounted.exists.return_value = True

    with patch.object(img.disk, "mount") as mock_mount:
        mock_mount.return_value.__enter__.return_value = fake_mounted

        # image.list_files
        assert img.list_files("Windows") == ["Windows", "Program Files", "test.txt"]

        # image.read_text
        assert img.read_text("Windows/System32/drivers/etc/hosts") == "System hosts file"

        # image.read_bytes
        assert img.read_bytes("test.bin") == b"binary_data"

        # image.write_text
        img.write_text("test.txt", "hello")
        fake_mounted.write_text.assert_called_with("test.txt", "hello", encoding="utf-8")

        # image.write_bytes
        img.write_bytes("data.bin", b"xyz")
        fake_mounted.write_bytes.assert_called_with("data.bin", b"xyz")

        # image.file_exists
        assert img.file_exists("test.txt") is True


def test_offline_disk_real_ntfs_end_to_end(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("mkfs.ntfs") or not shutil.which("qemu-storage-daemon"):
        pytest.skip("mkfs.ntfs or qemu-storage-daemon not available")

    # Create a small raw disk (20MB) and format with NTFS
    raw_disk = tmp_path / "ntfs_test.raw"
    with open(raw_disk, "wb") as f:
        f.truncate(20 * 1024 * 1024)

    subprocess.run(["mkfs.ntfs", "-F", "-q", str(raw_disk)], check=True)

    img = Image(raw_disk)
    assert len(img.partitions()) >= 1

    # 1. Path-like offline write via img.file / "path"
    note_path = img.file / r"C:\offline_hello.txt"
    note_path.write_text("Hello Offline Windows!")

    # 2. Verify existence and properties
    assert note_path.exists() is True
    assert note_path.is_file() is True
    assert note_path.is_dir() is False
    assert note_path.name == "offline_hello.txt"
    assert note_path.stem == "offline_hello"
    assert note_path.suffix == ".txt"

    # 3. Read back text via path
    assert note_path.read_text() == "Hello Offline Windows!"

    # 4. Download file to local Path
    local_download = tmp_path / "downloaded_note.txt"
    res = note_path.download(local_download)
    assert res == local_download
    assert local_download.read_text() == "Hello Offline Windows!"

    # 5. Download to RemoteFile
    rf = note_path.download()
    assert isinstance(rf, RemoteFile)
    assert rf.read_text() == "Hello Offline Windows!"

    # 6. Iterate directory children using iterdir()
    root_items = (img.file / "C:\\").iterdir()
    assert any(item.name == "offline_hello.txt" for item in root_items)

    # 7. Batch operations using context manager with img.file as fs
    with img.file as fs:
        batch_p = fs / r"C:\batch_test.txt"
        batch_p.write_text("Batch write content")
        assert batch_p.exists() is True
        assert batch_p.read_text() == "Batch write content"
