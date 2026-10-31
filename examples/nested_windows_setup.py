"""Example demonstrating nested Windows setup, shared folders, and running the windows library inside the guest.

This example illustrates:
1. Booting a Windows VM with hardware virtualization passthrough (nested virtualization).
2. Hotplugging a host directory into the guest as a drive letter using `machine.file.share(from=..., to="Z:")`.
3. Verifying CPU virtualization features inside the guest.
4. Installing/discovering QEMU and Python inside the Windows guest.
5. Executing the `windows` library directly inside the Windows guest VM to verify Windows host functionality.
6. Cleanly unmounting and hot-unplugging the shared folder upon exit.
"""

import time
from pathlib import Path

from windows import ISO, Image, Machine, WindowsVersion
from windows.qemu import create_qcow2_overlay


def main():
    print("=== Nested Windows & Shared Folder Example ===")

    project_root = Path(__file__).parent.parent.resolve()

    # 1. Resolve or provision base Windows image
    print("\n1. Resolving Windows base disk image...")
    prepared_image = project_root / "scratch" / "nested_prepared.qcow2"
    overlay_path = project_root / "scratch" / "nested_demo_overlay.qcow2"
    if overlay_path.exists():
        overlay_path.unlink()

    if prepared_image.exists():
        create_qcow2_overlay(overlay_path, prepared_image, size="80G")
        image = Image(disk_path=overlay_path)
    else:
        iso = ISO.from_version(WindowsVersion.WIN10_22H2)
        image = Image.from_iso(
            iso=iso,
            output_disk=overlay_path,
            disk_size="80G",
            use_cache=True,
        )
    print(f"   Image ready: {image.disk_path}")

    # 2. Configure Machine with nested virtualization enabled
    # enable_kvm=True passes -cpu host with VMX/SVM flags to allow running qemu.exe inside the guest
    print("\n2. Initializing Machine with nested virtualization (enable_kvm=True, cpus=4, ram_mb=8192)...")
    machine = Machine(
        image=image,
        ram_mb=8192,
        cpus=4,
        enable_kvm=True,
        headless=True,
    )

    try:
        # 3. Start VM and wait for QEMU Guest Agent (QGA)
        print("\n3. Powering on VM and waiting for QGA readiness...")
        machine.power.on()
        t0 = time.time()
        ready = machine.command.wait_until_ready(timeout=360)
        if not ready:
            raise RuntimeError("Guest failed to respond within timeout.")
        print(f"   VM ready in {time.time() - t0:.1f}s (status: {machine.power.status})")

        # Expand C: to fill the 80GB virtual disk if not already expanded
        print("\n   Checking and expanding C: partition if needed...")
        machine.command.run(
            r"Update-Disk 0; Resize-Partition -DriveLetter C -Size (Get-PartitionSupportedSize -DriveLetter C).SizeMax",
            powershell=True,
            timeout=180,
        )
        free_gb = machine.command.run("(Get-PSDrive C).Free / 1GB", powershell=True)
        try:
            print(f"   Free space on C: {float(free_gb.stdout.strip()):.1f} GB")
        except ValueError:
            print(f"   Free space on C: {free_gb.stdout.strip()}")

        # Disable Windows Update services and scheduled reboot tasks to prevent unexpected restarts
        print("\n   Disabling Windows Update auto-reboot and servicing tasks...")
        machine.command.run(
            r"Stop-Service -Name wuauserv, bits, WaaSMedicSvc -Force -ErrorAction SilentlyContinue; "
            r"Set-Service -Name wuauserv -StartupType Disabled -ErrorAction SilentlyContinue; "
            r"Set-Service -Name bits -StartupType Disabled -ErrorAction SilentlyContinue; "
            r"Set-Service -Name WaaSMedicSvc -StartupType Disabled -ErrorAction SilentlyContinue; "
            r"Disable-ScheduledTask -TaskPath '\Microsoft\Windows\UpdateOrchestrator\' -TaskName 'Reboot' -ErrorAction SilentlyContinue; "
            r"Disable-ScheduledTask -TaskPath '\Microsoft\Windows\UpdateOrchestrator\' -TaskName 'Reboot_AC' -ErrorAction SilentlyContinue; "
            r"Disable-ScheduledTask -TaskPath '\Microsoft\Windows\WindowsUpdate\' -TaskName 'Scheduled Start' -ErrorAction SilentlyContinue; "
            r'reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f; '
            r'reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v AUOptions /t REG_DWORD /d 2 /f',
            powershell=True,
            timeout=120,
        )

        # 4. Query processor and nested virtualization support inside the guest
        print("\n4. Checking guest CPU and virtualization capabilities...")
        cpu_info = machine.command.run(
            "(Get-WmiObject Win32_Processor) | Select-Object Name, NumberOfCores",
            powershell=True,
        )
        print(f"   Guest CPU: {cpu_info.stdout.strip()}")

        # 5. Share repository as drive Z: and host .cache as drive Y:
        print(f"\n5. Hotplugging repository '{project_root}' as Z: and host .cache as Y:...")
        host_cache_dir = Path.home() / ".cache"
        with machine.file.share(from_=project_root, to="Z:") as repo_share:
            print(f"   Repository mounted: {repo_share.guest_drive} (active: {repo_share.is_active})")

            with machine.file.share(from_=host_cache_dir, to="Y:") as cache_share:
                print(f"   Host .cache mounted: {cache_share.guest_drive} (active: {cache_share.is_active})")

                # 6. Verify contents of drive Z: and Y: inside Windows guest
                print("\n6. Listing contents of shared drives inside Windows guest...")
                res_z = machine.command.run("Get-ChildItem Z:\\ | Select-Object -First 5 Name", powershell=True)
                print(f"   Guest files on Z:\n{res_z.stdout.strip()}")
                res_y = machine.command.run("Get-ChildItem Y:\\windows\\images | Select-Object Name", powershell=True)
                print(f"   Guest files on Y:\\windows\\images:\n{res_y.stdout.strip()}")

                # Ensure cached base image is on local C:\base.qcow2 for fast NVMe/SSD access
                print("\n   Ensuring cached base image is on local C:\\base.qcow2 for fast NVMe/SSD I/O...")
                t_cp = time.time()
                res_cp = machine.command.run(
                    r"if (-not (Test-Path 'C:\base.qcow2') -or (Get-Item 'C:\base.qcow2').Length -lt 10000000000) { "
                    r"Copy-Item 'Y:\windows\images\win_win_win10_22h2_9b12f659_installed.qcow2' 'C:\base.qcow2' }",
                    powershell=True,
                    timeout=900,
                )
                print(f"   Base image ready in {time.time() - t_cp:.1f}s (returncode: {res_cp.returncode})")

                # 7. Execute nested VM creation using Image.from_base inside the guest
                print("\n7. Creating nested Windows VM from local base image via Image.from_base()...")
                log_file = project_root / "scratch" / "nested_demo.log"
                if log_file.exists():
                    log_file.unlink()

                guest_nested_script = r"""
import sys, time, shutil
from pathlib import Path
import threading

local_log = Path(r"C:\nested_demo.log")
shared_log = Path(r"Z:\scratch\nested_demo.log")

def log(msg):
    try:
        with open(local_log, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%X')}] {msg}\n")
    except Exception:
        pass
    print(msg, flush=True)

# Background mirror thread to keep host updated via Z: without ever blocking execution
stop_monitoring = threading.Event()
def mirror_loop():
    while not stop_monitoring.is_set():
        time.sleep(10.0)
        try:
            if local_log.exists():
                shutil.copyfile(local_log, shared_log)
        except Exception:
            pass

mirror_th = threading.Thread(target=mirror_loop, daemon=True)
mirror_th.start()

log("Starting nested demo script...")

# Add Z:\src to sys.path so guest runs repo's windows library
sys.path.insert(0, r"Z:\src")

from windows import Image, Machine

# Locate base image (prefer local C:\base.qcow2, fallback to Y:\windows\images)
local_base = Path(r"C:\base.qcow2")
if local_base.exists() and local_base.stat().st_size > 10_000_000_000:
    base_img_path = local_base
    log(f"Using local base image on C: {base_img_path} ({base_img_path.stat().st_size} bytes)")
else:
    cache_img_dir = Path(r"Y:\windows\images")
    cached_images = [p for p in cache_img_dir.glob("*.qcow2") if not p.name.endswith("test.img")]
    assert len(cached_images) > 0, "No cached Windows base images found on Y:\\windows\\images"
    base_img_path = cached_images[0]
    log(f"Found cached base image on Y: {base_img_path}")

# Create overlay on local C: drive backed by local base image
overlay_path = Path(r"C:\nested_win_demo_overlay.qcow2")
if overlay_path.exists():
    overlay_path.unlink()

overlay_img = Image.from_base(base_img_path, output_disk=overlay_path)
log(f"Created overlay at {overlay_img.disk_path} backing {overlay_img.backing_file}")
assert overlay_img.is_overlay is True

# Spawn the nested VM inside the guest with vnc_display=None for pure headless speed
nested_vm = Machine(
    image=overlay_img,
    ram_mb=3072,
    cpus=1,
    enable_kvm=True,
    headless=True,
    vnc_display=None,
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
log(f"Nested VM power status: {status}, process poll: {poll}")

assert status == "running", f"Expected nested VM status 'running', got '{status}'"
assert poll is None, f"Expected nested QEMU process to be alive, but exited with code {poll}"

# Background heartbeat thread to log progress
def monitor_loop():
    start = time.monotonic()
    while not stop_monitoring.is_set():
        time.sleep(30.0)
        elapsed = time.monotonic() - start
        proc = nested_vm._process_manager.process if nested_vm._process_manager else None
        p = proc.poll() if proc else None
        if p is not None:
            log(f"[{elapsed:.0f}s] QEMU PROCESS EXITED! exit_code={p}")
            break
        log(f"[{elapsed:.0f}s] Nested VM actively running under WHPX...")

mon_th = threading.Thread(target=monitor_loop, daemon=True)
mon_th.start()

try:
    # Run a command inside the nested VM via QGA
    log("Waiting for nested VM QGA readiness (timeout=1800s)...")
    t_ready_start = time.monotonic()
    ready = nested_vm.command.wait_until_ready(timeout=1800, interval=5.0)
    ready_elapsed = time.monotonic() - t_ready_start
    stop_monitoring.set()
    log(f"nested_vm.command.wait_until_ready returned: {ready} in {ready_elapsed:.1f}s")

    if ready:
        time.sleep(5.0)
        log("Nested VM QGA is ready! Running whoami...")
        cmd_res = nested_vm.command.run("whoami", powershell=False, timeout=120)
        log(f">>> Nested VM whoami output:\n{cmd_res.stdout.strip()}")
        assert cmd_res.returncode == 0
        assert "system" in cmd_res.stdout.lower() or "administrator" in cmd_res.stdout.lower()
        log("NESTED_DEMO_SUCCESS")
    else:
        err = nested_vm._process_manager.stderr_text if nested_vm._process_manager else ""
        if err:
            log(f"Nested QEMU stderr: {err}")
        raise RuntimeError("Nested VM failed to reach QGA readiness within timeout.")
finally:
    stop_monitoring.set()
    try:
        nested_vm.power.off()
    except Exception:
        pass
    nested_vm.close()
    log("Nested VM successfully powered off.")
    try:
        if local_log.exists():
            shutil.copyfile(local_log, shared_log)
    except Exception:
        pass

    if overlay_path.exists():
        try:
            overlay_path.unlink()
        except Exception:
            pass
"""
                machine.file.path(r"C:\nested_demo.py").write_text(guest_nested_script)
                res = machine.command.run(
                    r"& 'C:\Program Files\Python313\python.exe' 'C:\nested_demo.py'",
                    powershell=True,
                    timeout=2400,
                )
                print(f"   Guest execution stdout:\n{res.stdout.strip()}")
                if res.stderr.strip():
                    print(f"   Guest execution stderr:\n{res.stderr.strip()}")

                if res.returncode != 0:
                    raise RuntimeError(f"Nested VM execution failed in guest with code {res.returncode}")

            # 8. Shared folders are unmounted
            print(f"\n8. Cache share unmounted. Active status: {cache_share.is_active}")
        print(f"   Repo share unmounted. Active status: {repo_share.is_active}")

        print("\n=======================================================")
        print("  [SUCCESS] Nested Windows setup and execution passed! ")
        print("=======================================================")

    except Exception as e:
        print("[EXCEPTION in main]:", e)
        try:
            guest_log = machine.file.path(r"C:\nested_demo.log").read_text()
            print(f"=== GUEST LOG (C:\\nested_demo.log) ===\n{guest_log}")
        except Exception as log_err:
            print("Could not read C:\\nested_demo.log:", log_err)
        if machine._process_manager and machine._process_manager.process:
            print("QEMU poll:", machine._process_manager.process.poll())
            print("QEMU stderr:", machine._process_manager.stderr_text)
        raise

    finally:
        # 11. Clean shutdown
        print("\n11. Powering off Machine...")
        machine.power.off()
        machine.close()
        if overlay_path.exists():
            overlay_path.unlink(missing_ok=True)
        print("    Shutdown complete.")


if __name__ == "__main__":
    main()
