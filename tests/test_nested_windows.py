"""End-to-end integration test verifying nested Windows VM execution with host .cache sharing.

This test:
1. Boots an outer Windows VM with hardware virtualization passthrough (enable_kvm=True).
2. Mounts the host repository as guest drive Z: and the host .cache directory as guest drive Y:.
3. Inside the Windows guest, discovers the host's cached Windows base image on Y:.
4. Creates a thin qcow2 overlay using Image.from_base() on the guest's local C: drive.
5. Boots an inner (nested) Windows VM inside the Windows guest from the overlay.
6. Verifies that the nested QEMU VM runs actively without errors, then tears down cleanly.
"""

from pathlib import Path

import pytest

from windows import Machine
from windows.iso import get_image_cache_dir
from windows.qemu import create_qcow2_overlay, is_kvm_available

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
PREPARED_IMAGE = PROJECT_ROOT / "scratch" / "nested_prepared.qcow2"
HOST_CACHE_DIR = Path.home() / ".cache"
CACHED_IMAGES_DIR = Path(get_image_cache_dir())


def _has_cached_base_image() -> bool:
    if not CACHED_IMAGES_DIR.exists():
        return False
    return any(p.name.endswith(".qcow2") and not p.name.endswith("test.img") for p in CACHED_IMAGES_DIR.glob("*.qcow2"))


@pytest.mark.slow
@pytest.mark.nested
def test_nested_windows_vm_from_cached_base(tmp_path):
    """Test running a nested Windows VM inside an outer Windows VM from the host's cached base image."""
    if not is_kvm_available():
        pytest.skip("KVM hardware acceleration is required on host for nested virtualization test.")
    if not PREPARED_IMAGE.exists():
        pytest.skip(f"Pre-configured outer Windows image not found at {PREPARED_IMAGE}.")
    if not _has_cached_base_image():
        pytest.skip(f"No cached Windows base images found in {CACHED_IMAGES_DIR}.")

    # 1. Create outer VM overlay in tmp_path
    outer_overlay = tmp_path / "outer_test_overlay.qcow2"
    create_qcow2_overlay(outer_overlay, PREPARED_IMAGE)

    # 2. Configure outer VM with KVM nested virtualization passthrough
    outer_vm = Machine(
        image=outer_overlay,
        ram_mb=6144,
        cpus=4,
        enable_kvm=True,
        headless=True,
    )

    try:
        outer_vm.power.on()
        ready = outer_vm.command.wait_until_ready(timeout=180)
        assert ready is True, "Outer Windows VM QGA failed to become ready."

        # 3. Mount repository as Z: and host .cache as Y:
        with outer_vm.file.share(from_=PROJECT_ROOT, to="Z:", backend="smb") as repo_share:
            assert repo_share.is_active is True

            with outer_vm.file.share(from_=HOST_CACHE_DIR, to="Y:", backend="smb") as cache_share:
                assert cache_share.is_active is True

                # 4. Guest script that uses Image.from_base to spawn nested VM
                guest_nested_script = r"""
import sys, time
from pathlib import Path

# Add Z:\src to sys.path so guest runs repo's windows library
sys.path.insert(0, r"Z:\src")

from windows import Image, Machine

# Locate cached Windows base image on mounted Y: drive
cache_img_dir = Path(r"Y:\windows\images")
assert cache_img_dir.exists(), f"Mounted cache directory not found: {cache_img_dir}"

cached_images = [p for p in cache_img_dir.glob("*.qcow2") if not p.name.endswith("test.img")]
assert len(cached_images) > 0, "No cached Windows base images (.qcow2) found on Y:\\windows\\images"

base_img_path = cached_images[0]
print(f"Found cached base image: {base_img_path}")

# Create overlay on local C: drive backed by base image on Y:
overlay_path = Path(r"C:\nested_win_overlay.qcow2")
if overlay_path.exists():
    overlay_path.unlink()

overlay_img = Image.from_base(base_img_path, output_disk=overlay_path)
assert overlay_img.exists(), "Nested overlay image was not created!"
assert overlay_img.is_overlay is True, "Overlay was not recognized as an overlay image!"
print(f"Created overlay at {overlay_img.disk_path} backing {overlay_img.backing_file}")

# Spawn the nested VM inside the guest
nested_vm = Machine(
    image=overlay_img,
    ram_mb=3072,
    cpus=1,
    enable_kvm=True,
    headless=True,
    enable_audio=False,
    enable_network=False,
    enable_usb=False,
    cache="unsafe",
)
nested_vm.power.on()
time.sleep(3.0)

# Verify nested VM process is actively running
status = nested_vm.power.status
poll = nested_vm._process_manager.process.poll() if nested_vm._process_manager and nested_vm._process_manager.process else -999
print(f"Nested VM power status: {status}, process poll: {poll}")

assert status == "running", f"Expected nested VM status 'running', got '{status}'"
assert poll is None, f"Expected nested QEMU process to be alive, but exited with code {poll}"

# Cleanly stop and close the nested VM
nested_vm.power.off()
nested_vm.close()
assert nested_vm.power.status == "stopped", f"Expected status 'stopped', got '{nested_vm.power.status}'"

# Clean up overlay
if overlay_path.exists():
    overlay_path.unlink()

print("NESTED_VM_TEST_SUCCESS")
"""
                outer_vm.file.path(r"C:\test_nested_run.py").write_text(guest_nested_script)

                res = outer_vm.command.run(
                    r"& 'C:\Program Files\Python313\python.exe' 'C:\test_nested_run.py'",
                    powershell=True,
                    timeout=180,
                )
                print("Guest Output:\n", res.stdout)
                if res.stderr:
                    print("Guest Error:\n", res.stderr)

                assert res.returncode == 0, f"Guest execution failed with code {res.returncode}"
                assert "NESTED_VM_TEST_SUCCESS" in res.stdout

    finally:
        outer_vm.power.off()
        outer_vm.close()
        if outer_overlay.exists():
            outer_overlay.unlink(missing_ok=True)
