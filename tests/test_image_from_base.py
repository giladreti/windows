"""Unit tests for Image.from_base, create_overlay, and backing file inspection."""

import pytest

from windows.image import Image
from windows.qemu import create_qcow2_disk


def test_image_from_base_with_image_instance(tmp_path):
    base_file = tmp_path / "base.qcow2"
    create_qcow2_disk(base_file, size="10M")
    base_image = Image(base_file)

    output_overlay = tmp_path / "custom_overlay.qcow2"
    overlay_img = Image.from_base(base_image, output_disk=output_overlay)

    assert isinstance(overlay_img, Image)
    assert overlay_img.disk_path == output_overlay.resolve()
    assert overlay_img.exists()
    assert overlay_img.is_overlay is True
    assert overlay_img.backing_file == base_file.resolve()
    assert overlay_img.base == base_image

    # Clean up
    output_overlay.unlink()
    base_file.unlink()


def test_image_from_base_with_path_and_str(tmp_path):
    base_file = tmp_path / "base.qcow2"
    create_qcow2_disk(base_file, size="10M")

    # Pass Path
    overlay1 = Image.from_base(base_file, output_disk=tmp_path / "ov1.qcow2")
    assert overlay1.exists()
    assert overlay1.backing_file == base_file.resolve()

    # Pass str
    overlay2 = Image.from_base(str(base_file), output_disk=tmp_path / "ov2.qcow2")
    assert overlay2.exists()
    assert overlay2.backing_file == base_file.resolve()


def test_image_from_base_random_output_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base_file = tmp_path / "win10.qcow2"
    create_qcow2_disk(base_file, size="10M")

    overlay = Image.from_base(base_file)
    assert overlay.exists()
    assert "win10_overlay_" in overlay.disk_path.name
    assert overlay.backing_file == base_file.resolve()
    overlay.disk_path.unlink()


def test_image_from_base_destination_directory(tmp_path):
    base_file = tmp_path / "base_win.qcow2"
    create_qcow2_disk(base_file, size="10M")
    out_dir = tmp_path / "sub_overlays"
    out_dir.mkdir()

    overlay = Image.from_base(base_file, output_disk=out_dir)
    assert overlay.exists()
    assert overlay.disk_path.parent == out_dir.resolve()
    assert "base_win_overlay_" in overlay.disk_path.name
    assert overlay.backing_file == base_file.resolve()


def test_image_from_base_keyword_aliases(tmp_path):
    base_file = tmp_path / "base.qcow2"
    create_qcow2_disk(base_file, size="10M")

    # base_image kwarg and overlay_path kwarg
    ov_path = tmp_path / "ov_kwarg.qcow2"
    overlay = Image.from_base(base_image=base_file, overlay_path=ov_path)
    assert overlay.exists()
    assert overlay.disk_path == ov_path.resolve()

    # output_disk as an Image instance
    ov_placeholder = tmp_path / "ov_placeholder.qcow2"
    create_qcow2_disk(ov_placeholder, size="10M")
    placeholder_img = Image(ov_placeholder)
    recreated = Image.from_base(base=base_file, output_disk=placeholder_img)
    assert recreated.disk_path == ov_placeholder.resolve()
    assert recreated.backing_file == base_file.resolve()


def test_image_create_overlay_and_overlay_methods(tmp_path):
    base_file = tmp_path / "base.qcow2"
    create_qcow2_disk(base_file, size="10M")
    base_image = Image(base_file)

    # create_overlay()
    ov1 = base_image.create_overlay(tmp_path / "method_ov1.qcow2")
    assert ov1.exists()
    assert ov1.backing_file == base_file.resolve()

    # overlay() alias
    ov2 = base_image.overlay(tmp_path / "method_ov2.qcow2")
    assert ov2.exists()
    assert ov2.backing_file == base_file.resolve()


def test_image_properties_non_overlay(tmp_path):
    base_file = tmp_path / "standalone.qcow2"
    create_qcow2_disk(base_file, size="10M")
    img = Image(base_file)

    assert img.backing_file is None
    assert img.is_overlay is False
    assert img.base is None


def test_image_from_base_errors(tmp_path):
    base_file = tmp_path / "base.qcow2"
    create_qcow2_disk(base_file, size="10M")

    # Missing base
    with pytest.raises(ValueError, match="A base image must be provided"):
        Image.from_base()

    # Non-existent base file
    with pytest.raises(FileNotFoundError, match="Base image file does not exist"):
        Image.from_base(tmp_path / "does_not_exist.qcow2")

    # Unsupported type
    with pytest.raises(TypeError, match="Unsupported base image type"):
        Image.from_base(12345)  # type: ignore

    # Output disk same as base path
    with pytest.raises(ValueError, match="Overlay path cannot be the same as the base image path"):
        Image.from_base(base_file, output_disk=base_file)
