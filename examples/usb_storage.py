"""Example demonstrating USB flash drive emulation and hotplugging in windows.

This example:
1. Formats a virtual disk image on the host with MBR partition table and FAT32 filesystem.
2. Pre-populates files and directories into the disk image.
3. Hotplugs the USB drive into a running Windows VM via QEMU monitor.
4. Accesses, reads, and writes files on the USB drive from inside Windows.
5. Safely unmounts and hot-unplugs the USB drive from the VM.
"""

from pathlib import Path

from windows import (
    ISO,
    Image,
    Machine,
    WindowsVersion,
    create_usb_disk,
)


def main():
    print("=== Windows USB Flash Drive Emulation Demo ===")

    # 1. Prepare base VM image
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay.qcow2", use_cache=True)

    # 2. Create formatted USB disk image on host with pre-populated files
    usb_img_path = Path("sample_flash_drive.img").resolve()
    create_usb_disk(
        path=usb_img_path,
        size="64M",
        filesystem="fat32",
        label="USBTEST",
        files={
            "readme.txt": b"Hello from Host via emulated USB drive!\n",
            "configs/app_config.json": b'{"version": "1.0", "author": "Host"}\n',
        },
    )
    print(f"Created host USB disk image: {usb_img_path}")

    # 3. Boot Windows VM
    machine = Machine(image, ram_mb=4096, cpus=4)
    machine.power.on()
    print("Waiting for guest QGA interface...")
    machine.command.wait_until_ready(timeout=180)

    try:
        # 4. Hotplug USB drive into running VM using context manager
        print("\n[1] Hotplugging USB drive into Windows VM...")
        with machine.usb.mount(usb_img_path, to="E:") as usb_dev:
            dl = usb_dev.drive_letter
            print(f"    USB drive mounted! ID: {usb_dev.device_id}, Windows Drive: {dl}")

            # Verify files from guest
            res = machine.command.run(f"Get-ChildItem {dl}\\ | Select-Object Name, Length", powershell=True)
            print(f"    Directory contents on {dl}:\n{res.stdout.strip()}")

            # Read content from guest
            content = machine.command.run(f"Get-Content {dl}\\readme.txt", powershell=True)
            print(f"    Read from {dl}\\readme.txt:\n      {content.stdout.strip()}")

            # Write a response file from the guest onto the USB drive
            machine.command.run(
                f"'Response from guest' | Out-File -FilePath {dl}\\guest_report.txt -Encoding ascii",
                powershell=True,
            )
            print(f"    Successfully wrote {dl}\\guest_report.txt from guest!")

        # 5. Verify unmount
        print("\n[2] USB drive unmounted.")
        check = machine.command.run(f"Test-Path {dl}\\", powershell=True)
        print(f"    Drive {dl} exists after unmount: {check.stdout.strip()}")
        assert check.stdout.strip().lower() == "false", f"Drive {dl} should not exist after unmount!"

    finally:
        machine.power.off()
        machine.close()
        usb_img_path.unlink(missing_ok=True)
        print("\nExample finished successfully!")


if __name__ == "__main__":
    main()
