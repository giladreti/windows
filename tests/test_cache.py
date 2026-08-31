"""Unit tests for ISO caching, installed disk image caching, and qcow2 disk isolation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from windows.image import create_image_from_iso
from windows.iso import (
    clear_image_cache,
    clear_iso_cache,
    create_dummy_iso,
    list_cached_images,
    list_cached_isos,
    resolve_iso,
)


def test_iso_caching_prevents_redownload(tmp_path):
    """Test that resolving an ISO caches it locally and avoids redownloading on subsequent calls."""
    cache_dir = tmp_path / "iso_cache"

    with patch("windows.iso.download_file") as mock_download:

        def fake_download(url, dest_path, show_progress=True):
            create_dummy_iso(dest_path)

        mock_download.side_effect = fake_download

        # First call: triggers download and caches file
        iso1 = resolve_iso("23H2", cache_dir=cache_dir)
        assert mock_download.call_count == 1
        assert iso1.exists()

        # Second call: reuses existing cached file without calling download_file
        iso2 = resolve_iso("23H2", cache_dir=cache_dir)
        assert mock_download.call_count == 1  # Still 1, download skipped!
        assert iso1 == iso2


def test_installed_image_caching_and_reuse(tmp_path):
    """Test that installed disk images are cached in image cache and reused instantly."""
    iso_cache_dir = tmp_path / "iso_cache"
    img_cache_dir = tmp_path / "img_cache"
    cached_iso = create_dummy_iso(iso_cache_dir / "win_25h2.iso")

    output_disk1 = tmp_path / "vm1.qcow2"
    output_disk2 = tmp_path / "vm2.qcow2"

    with (
        patch("windows.image.get_image_cache_dir", return_value=img_cache_dir),
        patch("windows.image.create_qcow2_disk") as mock_create_disk,
        patch("windows.image.create_qcow2_overlay") as mock_create_overlay,
        patch("windows.image.QEMUProcessManager") as mock_pm,
    ):

        def fake_create_disk(p, size):
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MOCK_QCOW2_HEADER_INSTALLED_IMAGE_DATA")
            return p

        def fake_create_overlay(overlay_p, backing_p):
            overlay_p = Path(overlay_p)
            overlay_p.parent.mkdir(parents=True, exist_ok=True)
            overlay_p.write_bytes(b"MOCK_QCOW2_HEADER_INSTALLED_IMAGE_DATA")
            return overlay_p

        mock_create_disk.side_effect = fake_create_disk
        mock_create_overlay.side_effect = fake_create_overlay
        mock_pm_instance = MagicMock()
        mock_pm_instance.is_running.side_effect = [False]
        mock_pm.return_value = mock_pm_instance

        # First call: performs setup and caches installed disk image
        create_image_from_iso(cached_iso, output_disk=output_disk1)
        assert mock_pm.call_count == 1
        assert output_disk1.exists()

        # Check installed image cache directory contains the cached image
        cached_images = list_cached_images(cache_dir=img_cache_dir)
        assert len(cached_images) == 1

        # Second call: reuses cached installed image instantly via overlay, skipping QEMU setup!
        create_image_from_iso(cached_iso, output_disk=output_disk2)
        assert mock_pm.call_count == 1  # QEMU setup skipped!
        assert output_disk2.exists()
        assert output_disk2.read_bytes() == b"MOCK_QCOW2_HEADER_INSTALLED_IMAGE_DATA"


def test_multiple_separate_qcow_disk_images(tmp_path):
    """Test that creating multiple disk images generates separate qcow2 files."""
    cache_dir = tmp_path / "iso_cache"
    img_cache = tmp_path / "img_cache"
    cached_iso = create_dummy_iso(cache_dir / "win_25h2.iso")

    disk1_path = tmp_path / "vm1_disk.qcow2"
    disk2_path = tmp_path / "vm2_disk.qcow2"

    with (
        patch("windows.image.get_image_cache_dir", return_value=img_cache),
        patch("windows.image.create_qcow2_disk") as mock_create_disk,
        patch("windows.image.QEMUProcessManager") as mock_pm,
    ):

        def fake_create_disk(p, size):
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"MOCK_DISK_DATA")
            return p

        mock_create_disk.side_effect = fake_create_disk
        mock_pm_instance = MagicMock()
        mock_pm_instance.is_running.side_effect = [False, False]
        mock_pm.return_value = mock_pm_instance

        img1 = create_image_from_iso(str(cached_iso), output_disk=disk1_path, use_cache=False)
        img2 = create_image_from_iso(str(cached_iso), output_disk=disk2_path, use_cache=False)

        assert img1.disk_path != img2.disk_path
        assert img1.disk_path == disk1_path.resolve()
        assert img2.disk_path == disk2_path.resolve()
        assert cached_iso.exists()


def test_cache_management_helpers(tmp_path):
    """Test ISO and installed image cache management helper functions."""
    iso_cache = tmp_path / "iso_cache"
    img_cache = tmp_path / "img_cache"

    assert len(list_cached_isos(iso_cache)) == 0
    assert len(list_cached_images(img_cache)) == 0

    # Populate ISO and Image caches
    create_dummy_iso(iso_cache / "iso1.iso")
    create_dummy_iso(iso_cache / "iso2.iso")

    img_cache.mkdir(parents=True, exist_ok=True)
    (img_cache / "win_11_installed.qcow2").write_bytes(b"QCOW2_DATA")
    (img_cache / "win_25h2_installed.qcow2").write_bytes(b"QCOW2_DATA")

    assert len(list_cached_isos(iso_cache)) == 2
    assert len(list_cached_images(img_cache)) == 2

    # Clear ISO cache
    deleted_isos = clear_iso_cache(iso_cache)
    assert deleted_isos == 2
    assert len(list_cached_isos(iso_cache)) == 0

    # Clear Image cache
    deleted_imgs = clear_image_cache(img_cache)
    assert deleted_imgs == 2
    assert len(list_cached_images(img_cache)) == 0
