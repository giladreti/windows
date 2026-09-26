"""Example demonstrating CD-ROM drive emulation and ISO media management in windows.

This example:
1. Generates an ISO 9660 / Joliet filesystem image on the host with files and directories.
2. Inserts / mounts the ISO image into the CD-ROM drive of a running Windows VM.
3. Accesses and reads files from the CD-ROM drive from inside Windows.
4. Safely ejects the CD-ROM media from the drive.
"""

from pathlib import Path

from windows import (
    ISO,
    Image,
    Machine,
    WindowsVersion,
    create_cdrom_iso,
)


def main():
    print("=== Windows CD-ROM Drive & ISO Media Demo ===")

    # 1. Prepare base VM image
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay.qcow2", use_cache=True)

    # 2. Create formatted ISO image on host with pre-populated files
    cd_iso_path = Path("sample_cdrom.iso").resolve()
    create_cdrom_iso(
        path=cd_iso_path,
        label="SOFTWARE_CD",
        files={
            "readme.txt": b"Welcome to the installer CD-ROM!\n",
            "setup/payload.bin": b"\x7fELF_OR_PE_BINARY_DATA",
            "setup/version.info": "Build 10.0.19045\n",
        },
    )
    print(f"Created ISO image on host: {cd_iso_path}")

    # 3. Boot Windows VM
    machine = Machine(image, ram_mb=4096, cpus=4)
    machine.power.on()
    print("Waiting for guest QGA interface...")
    machine.command.wait_until_ready(timeout=180)

    try:
        # 4. Insert ISO media into CD-ROM drive using context manager
        print("\n[1] Inserting CD-ROM disc into Windows VM...")
        with machine.cd.insert(cd_iso_path, to="D:") as cd_dev:
            dl = cd_dev.drive_letter
            print(f"    CD-ROM disc inserted! ID: {cd_dev.device_id}, Windows Drive: {dl}")

            # Verify files from guest
            res = machine.command.run(f"Get-ChildItem {dl}\\ | Select-Object Name, Length", powershell=True)
            print(f"    Directory contents on {dl}:\n{res.stdout.strip()}")

            # Read content from guest
            content = machine.command.run(f"Get-Content {dl}\\readme.txt", powershell=True)
            print(f"    Read from {dl}\\readme.txt:\n      {content.stdout.strip()}")

            # Verify nested file
            sub_content = machine.command.run(f"Get-Content {dl}\\setup\\version.info", powershell=True)
            print(f"    Read from {dl}\\setup\\version.info:\n      {sub_content.stdout.strip()}")

        # 5. Verify disc ejection
        print("\n[2] CD-ROM disc ejected.")
        dl_char = dl.rstrip(":") if dl else "D"
        check = machine.command.run(
            f"(Get-Volume | Where-Object {{ $_.DriveLetter -eq '{dl_char}' }}).FileSystemLabel",
            powershell=True,
        )
        print(f"    Drive {dl} label after eject: '{check.stdout.strip()}'")

    finally:
        machine.power.off()
        machine.close()
        cd_iso_path.unlink(missing_ok=True)
        print("\nExample finished successfully!")


if __name__ == "__main__":
    main()
