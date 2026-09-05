import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from windows.image import Image
from windows.iso import ISO, WindowsVersion, create_dummy_iso
from windows.machine import Machine


def test_iso_init_and_properties(tmp_path):
    iso_file = tmp_path / "test_windows.iso"
    create_dummy_iso(iso_file)

    iso = ISO(iso_file)
    assert iso.path == iso_file.resolve()
    assert iso.name == "test_windows.iso"
    assert iso.stem == "test_windows"
    assert iso.exists() is True
    assert iso.stat().st_size > 0
    assert os.fspath(iso) == str(iso_file.resolve())
    assert str(iso) == str(iso_file.resolve())
    assert repr(iso) == f"<ISO path={str(iso_file.resolve())!r}>"
    assert iso == ISO(iso_file)
    assert iso == iso_file
    assert iso == str(iso_file)
    assert hash(iso) == hash(iso_file.resolve())

    # Wrap existing ISO
    iso_wrapped = ISO(iso)
    assert iso_wrapped.path == iso.path

    # Nonexistent file raises FileNotFoundError
    with pytest.raises(FileNotFoundError, match="does not exist"):
        ISO(tmp_path / "nonexistent.iso")


def test_iso_from_version(tmp_path):
    with patch("windows.iso.download_file") as mock_dl:

        def fake_download(url, dest, show_progress=True):
            create_dummy_iso(dest)

        mock_dl.side_effect = fake_download

        iso = ISO.from_version(WindowsVersion.WIN10_22H2, cache_dir=tmp_path, show_progress=False)
        assert isinstance(iso, ISO)
        assert iso.exists()
        assert "win10_22h2" in iso.name

        # Also via alias string
        iso2 = ISO.from_version("23h2", cache_dir=tmp_path, show_progress=False)
        assert isinstance(iso2, ISO)
        assert iso2.exists()


def test_iso_from_url(tmp_path):
    with patch("windows.iso.download_file") as mock_dl:

        def fake_download(url, dest, show_progress=True):
            create_dummy_iso(dest)

        mock_dl.side_effect = fake_download

        iso_url = ISO.from_url("https://example.com/custom_windows.iso", cache_dir=tmp_path, show_progress=False)
        assert isinstance(iso_url, ISO)
        assert iso_url.exists()


def test_iso_resolve(tmp_path):
    iso_file = tmp_path / "existing.iso"
    create_dummy_iso(iso_file)

    iso_obj = ISO(iso_file)
    assert ISO.resolve(iso_obj) is iso_obj

    res = ISO.resolve(str(iso_file))
    assert isinstance(res, ISO)
    assert res == iso_obj


def test_image_init_and_properties(tmp_path):
    disk_file = tmp_path / "installed.qcow2"
    disk_file.write_bytes(b"QCOW2_DATA")

    img = Image(disk_file)
    assert img.disk_path == disk_file.resolve()
    assert img.path == disk_file.resolve()
    assert img.name == "installed.qcow2"
    assert img.stem == "installed"
    assert img.exists() is True
    assert img.stat().st_size == 10
    assert os.fspath(img) == str(disk_file.resolve())
    assert str(img) == str(disk_file.resolve())
    assert repr(img) == f"<Image disk_path={str(disk_file.resolve())!r}>"
    assert img == Image(disk_file)
    assert img == disk_file
    assert img == str(disk_file)
    assert hash(img) == hash(disk_file.resolve())

    # Wrap existing Image
    img_wrapped = Image(img)
    assert img_wrapped.disk_path == img.disk_path

    with pytest.raises(FileNotFoundError, match="does not exist"):
        Image(tmp_path / "nonexistent.qcow2")


def test_image_from_iso_with_iso_object(tmp_path):
    iso_file = tmp_path / "win.iso"
    create_dummy_iso(iso_file)
    iso = ISO(iso_file)

    output_disk = tmp_path / "out.qcow2"
    img_cache_dir = tmp_path / "img_cache"

    with (
        patch("windows.image.get_image_cache_dir", return_value=img_cache_dir),
        patch("windows.image.create_qcow2_disk") as mock_create_disk,
        patch("windows.image.QEMUProcessManager") as mock_pm,
    ):

        def fake_create_disk(p, size):
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MOCK_DISK")
            return p

        mock_create_disk.side_effect = fake_create_disk
        mock_pm_inst = MagicMock()
        mock_pm_inst.is_running.return_value = False
        mock_pm.return_value = mock_pm_inst

        # Image.from_iso with ISO instance
        image = Image.from_iso(iso, output_disk=output_disk, use_cache=False)
        assert isinstance(image, Image)
        assert image.disk_path == output_disk.resolve()
        assert image.exists()


def test_image_from_iso_with_version_enum_and_string(tmp_path):
    img_cache_dir = tmp_path / "img_cache"
    output_disk = tmp_path / "out_ver.qcow2"

    with (
        patch("windows.image.get_image_cache_dir", return_value=img_cache_dir),
        patch("windows.iso.download_file") as mock_dl,
        patch("windows.image.create_qcow2_disk") as mock_create_disk,
        patch("windows.image.QEMUProcessManager") as mock_pm,
    ):

        def fake_download(url, dest, show_progress=True):
            create_dummy_iso(dest)

        def fake_create_disk(p, size):
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MOCK_DISK")
            return p

        mock_dl.side_effect = fake_download
        mock_create_disk.side_effect = fake_create_disk
        mock_pm_inst = MagicMock()
        mock_pm_inst.is_running.side_effect = [False, False]
        mock_pm.return_value = mock_pm_inst

        # Image.from_iso directly with WindowsVersion enum
        image = Image.from_iso(WindowsVersion.WIN10_22H2, output_disk=output_disk, use_cache=False)
        assert isinstance(image, Image)
        assert image.disk_path == output_disk.resolve()


def test_machine_init(tmp_path):
    disk_file = tmp_path / "win_vm.qcow2"
    disk_file.write_bytes(b"QCOW2_DATA")
    image = Image(disk_file)

    # Machine(image)
    m0 = Machine(image, ram_mb=4096, cpus=4)
    assert isinstance(m0, Machine)
    assert m0.image == image
    assert m0.ram_mb == 4096
    assert m0.cpus == 4

    # Machine(path)
    m_path = Machine(disk_file, ram_mb=2048, cpus=2)
    assert isinstance(m_path, Machine)
    assert m_path.image == image
    assert m_path.ram_mb == 2048
    assert m_path.cpus == 2

    # Machine(str)
    m_str = Machine(str(disk_file))
    assert isinstance(m_str, Machine)
    assert m_str.image == image
