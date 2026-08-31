"""100% Un-mocked End-to-End Integration Test for windows.

This test uses ZERO mocks, ZERO patches, and ZERO fake servers.
It runs real QEMU, real ISO resolution, real QCOW2 disks, real power control,
real WinRM network command execution, and real file transfers.
"""

import windows


def test_unmocked_e2e_windows_workflow(tmp_path):
    """Execute the full un-mocked windows workflow against real QEMU and real Windows guest."""

    disk_path = tmp_path / "unmocked_windows_vm.qcow2"

    print("\n[E2E Un-mocked] Fetching ISO for WIN11_25H2...")
    iso = windows.get_iso(windows.WindowsVersion.WIN11_25H2)

    print(f"[E2E Un-mocked] Creating image from '{iso}'...")
    image = windows.create_image_from_iso(
        iso_path=iso,
        output_disk=disk_path,
        disk_size="20G",
        ram_mb=4096,
        cpus=4,
        headless=True,
        timeout_minutes=60,
    )
    assert image.disk_path.exists()

    # 2. Instantiate Machine from real Image
    print("[E2E Un-mocked] Creating machine instance from image...")
    machine = windows.create_machine_from_image(image, ram_mb=4096, cpus=4, headless=True)

    try:
        # 3. Power ON real QEMU process
        print("[E2E Un-mocked] Powering ON QEMU virtual machine...")
        machine.power.on()
        assert machine.power.status == "running"

        # 4. Execute real command on guest Windows machine
        print("[E2E Un-mocked] Executing command: dir C:\\ ...")
        output = machine.command.run("dir C:\\", timeout=120)
        print(f"[E2E Un-mocked] Guest output:\n{output.stdout}")
        assert "Windows" in output or "Program Files" in output

        # 5. Download real file from guest: C:\windows\system32\ntdll.dll
        print("[E2E Un-mocked] Downloading C:\\windows\\system32\\ntdll.dll ...")
        file = machine.file.download("C:\\windows\\system32\\ntdll.dll")
        assert isinstance(file, windows.RemoteFile)
        local_ntdll = tmp_path / "downloaded_ntdll.dll"
        file.save(local_ntdll)

        assert local_ntdll.exists()
        assert local_ntdll.stat().st_size > 0
        assert file.read_bytes()[:2] == b"MZ"  # Valid PE header signature for ntdll.dll

        # 6. Upload real file to guest
        local_upload = tmp_path / "test_payload.txt"
        local_upload.write_text("Hello from windows un-mocked E2E test!")
        print("[E2E Un-mocked] Uploading file to guest...")
        machine.file.upload(local_upload, "C:\\Users\\Public\\windows_test.txt")

        # Verify upload on guest via command execution
        upload_verify = machine.command.run("type C:\\Users\\Public\\windows_test.txt")
        assert "Hello from windows" in upload_verify.stdout

    finally:
        # 7. Power OFF QEMU VM
        print("[E2E Un-mocked] Powering OFF QEMU virtual machine...")
        machine.power.off()
        assert machine.power.status == "stopped"
