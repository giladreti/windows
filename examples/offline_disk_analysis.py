r"""Example demonstrating Offline Windows Disk Analysis and File Read/Write (image.file):
- image.partitions(): Inspect MBR/GPT partition tables without booting a VM
- image.file / r"C:\Windows": Path-like interface identical to machine.file
- path.iterdir(): List files and folders inside offline Windows NTFS partitions
- path.read_text() / path.read_bytes(): Extract and inspect file contents offline
- path.write_text() / path.write_bytes(): Inject or patch files directly into offline disks
- path.download() / path.upload(): Transfer files between host and offline guest disk
- with image.file as fs: Batch operations maintaining an active partition mount
"""

from windows import ISO, Image, WindowsVersion


def main():
    print("=== Offline Windows Disk Analysis & File Read/Write Demo ===")

    # 1. Provision or resolve cached Windows base image and thin overlay
    print("\n1. Preparing Windows overlay disk for offline inspection...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="offline_analysis_demo.qcow2", use_cache=True)
    print(f"   Disk Image: {image.disk_path} (exists: {image.exists()})")

    # 2. Inspect MBR/GPT partitions inside the offline image
    print("\n2. Inspecting offline disk partitions...")
    partitions = image.partitions()
    for p in partitions:
        print(
            f"   - Partition #{p.index}: {p.type_name}, Offset: {p.offset} bytes, "
            f"Size: {p.size / (1024**3):.2f} GB (bootable: {p.is_bootable})"
        )

    print("\n3. Inspecting files in partition root using (image.file / 'C:\\').iterdir()...")
    root_folder = image.file / "C:\\"
    root_items = root_folder.iterdir()
    for item in root_items[:10]:
        kind = "[DIR]" if item.is_dir() else "[FILE]"
        print(f"   {kind} {item.clean_path} (name={item.name})")
    if len(root_items) > 10:
        print(f"   ... and {len(root_items) - 10} more items.")

    # 4. Read an existing Windows system file offline using path-like syntax
    hosts = image.file / r"C:\Windows\System32\drivers\etc\hosts"
    print(f"\n4. Reading Windows hosts file offline ({hosts})...")
    print(f"   Exists: {hosts.exists()} | Is file: {hosts.is_file()} | Name: {hosts.name}")
    hosts_content = hosts.read_text()
    print("   --- File Preview ---")
    for line in hosts_content.splitlines()[:8]:
        print(f"   {line}")
    print("   --------------------")

    # 5. Inject a new file into the offline image using path-like syntax
    note = image.file / r"C:\Users\Public\offline_injected_note.txt"
    print(f"\n5. Injecting file offline into '{note}'...")
    note.write_text("This file was injected offline into the Windows disk image using image.file!")
    print("   ✓ File written to offline disk.")

    # 6. Verify file existence and properties
    print("\n6. Verifying injected file offline...")
    assert note.exists(), f"Expected '{note}' to exist in offline image!"
    assert note.is_file(), f"Expected '{note}' to be recognized as a file!"
    print(f"   ✓ Verified content: '{note.read_text()}'")

    # 7. Batch operations using context manager with image.file
    print("\n7. Performing batch offline operations via with image.file as fs...")
    with image.file as fs:
        etc = fs / r"C:\Windows\System32\drivers\etc"
        etc_files = [child.name for child in etc.iterdir()]
        print(f"   Files in etc folder: {etc_files}")

        batch_note = fs / r"C:\Users\Public\batch_note.txt"
        batch_note.write_text("Created during batch image.file session.")
        assert batch_note.exists()
        print("   ✓ Batch note written and verified.")

    print("\n🎉 ALL OFFLINE DISK OPERATIONS COMPLETED SUCCESSFULLY!")


if __name__ == "__main__":
    main()
