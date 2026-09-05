"""Example workflow demonstrating windows usage with QEMU Guest Agent (QGA), Path-style file/dir operations, and QEMU Disk Overlays."""

import shutil
from pathlib import Path

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== windows Workflow Example ===")

    # 1. Fetch/download Windows ISO
    print("\n1. Resolving/fetching Windows ISO...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    print(f"   ISO ready: {iso}")

    # 2. Provision Windows QEMU VM image
    print("\n2. Creating VM image from ISO...")
    image = Image.from_iso(
        iso=iso,
        output_disk="my_vm_overlay.qcow2",
        use_cache=True,
        interactive=True,
    )
    print(f"   VM Image ready: {image}")

    # 3. Instantiate Machine from Image
    print("\n3. Creating Machine instance...")
    machine = Machine(image)

    # 4. Power management - Start VM in debug mode
    print("\n4. Starting VM in debug mode using machine.debug() (QEMU monitor active)...")
    init_debug_script = [
        "info status",
        "info cpus",
        "info network",
    ]
    machine.debug(init_script=init_debug_script, pause_at_boot=True, auto_continue=True, open_console=True)
    print(f"   VM Power status: {machine.power.status}")

    # 5. Wait for guest OS & QGA interface to become responsive
    print("\n5. Waiting for guest execution interface (QGA) to be ready...")
    ready = machine.command.wait_until_ready(timeout=180)
    print(f"   Guest ready: {ready}")
    if not ready:
        raise RuntimeError("Guest OS failed to respond within timeout")

    # 6. Execute remote commands via QGA
    print("\n6. Executing remote commands inside guest via QGA...")
    result = machine.command.run("Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version")
    print(f"   Output:\n{result.stdout}")

    # Capture live VM screen as PNG
    screenshot_path = machine.console.screenshot("vm_screenshot.png")
    print(f"   Saved VM screen capture to: {screenshot_path}")

    # 7. Path-style File read & write via QGA
    print("\n7. Using RemotePath interface for text read/write via QGA...")
    hello_path = machine.file.path(r"C:\Users\Public\windows_path_demo.txt")
    hello_path.write_text("Hello from windows pathlib.Path style interface over QGA!")
    print(f"   Verified remote text content: {hello_path.read_text().strip()}")

    # 8. Path-style Directory Upload & Download with exist_policy
    print("\n8. Directory Upload & Download using RemotePath (exist_policy='overwrite')...")
    local_demo_dir = Path("/tmp/windows_demo_folder")
    if local_demo_dir.exists():
        shutil.rmtree(local_demo_dir)
    local_demo_dir.mkdir(parents=True, exist_ok=True)
    (local_demo_dir / "config.json").write_text('{"name": "windows", "status": "active"}')
    sub_dir = local_demo_dir / "scripts"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / "setup.ps1").write_text('Write-Host "Running guest script"')

    remote_demo_dir = machine.file.path(r"C:\Users\Public\windows_demo_folder")
    remote_demo_dir.upload(local_demo_dir, exist_policy="overwrite")
    print(f"   Uploaded local directory to guest path: {remote_demo_dir}")

    download_local_target = Path("/tmp/windows_downloaded_folder")
    if download_local_target.exists():
        shutil.rmtree(download_local_target)

    remote_demo_dir.download_dir(download_local_target, exist_policy="overwrite")
    print(f"   Downloaded remote directory to local path ({len(list(download_local_target.rglob('*')))} files/dirs)")

    # Clean up local temporary demo folders
    shutil.rmtree(local_demo_dir, ignore_errors=True)
    shutil.rmtree(download_local_target, ignore_errors=True)

    # 9. Power off VM cleanly
    print("\n9. Powering off VM...")
    machine.power.off()
    print(f"   VM Power status: {machine.power.status}")
    print("\nWorkflow completed successfully!")


if __name__ == "__main__":
    main()
