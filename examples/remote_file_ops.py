"""Example demonstrating pathlib.Path-style RemotePath file and directory operations over QGA."""

import shutil
from pathlib import Path

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== RemotePath File & Directory Operations Example over QGA ===")

    # 1. Prepare/reuse VM image
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="my_vm_overlay.qcow2", use_cache=True)
    machine = Machine(image)

    # 2. Start VM and wait for QGA
    machine.power.on()
    print("Waiting for guest QGA interface...")
    machine.command.wait_until_ready(timeout=180)

    # 3. Direct file text & byte operations
    remote_note = machine.file / r"C:\Users\Public\welcome.txt"
    remote_note.write_text("Hello from RemotePath write_text over QGA!")
    print(f"Read text: {remote_note.read_text().strip()}")
    print(f"File exists: {remote_note.exists()}, is_file: {remote_note.is_file()}")

    # 4. Upload directory with exist_policy ('overwrite', 'merge', 'abort')
    local_dir = Path("/tmp/sample_project")
    if local_dir.exists():
        shutil.rmtree(local_dir)
    local_dir.mkdir(parents=True)
    (local_dir / "app.py").write_text("print('Running inside VM')")
    sub = local_dir / "data"
    sub.mkdir()
    (sub / "records.csv").write_text("id,val\n1,100\n2,200\n")

    remote_dir = machine.file.path(r"C:\Users\Public\sample_project")
    print("\nUploading directory to guest (exist_policy='overwrite')...")
    remote_dir.upload(local_dir, exist_policy="overwrite")

    # 5. Download directory back to host
    local_dest = Path("/tmp/downloaded_project")
    if local_dest.exists():
        shutil.rmtree(local_dest)
    print("Downloading directory from guest...")
    remote_dir.download_dir(local_dest, exist_policy="overwrite")
    print(f"Downloaded files: {[p.name for p in local_dest.rglob('*')]}")

    # 6. Cleanup
    shutil.rmtree(local_dir, ignore_errors=True)
    shutil.rmtree(local_dest, ignore_errors=True)
    machine.power.off()
    print("\nRemote file operations completed successfully!")


if __name__ == "__main__":
    main()
