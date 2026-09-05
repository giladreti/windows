"""Example demonstrating VM Screen Video Recording (machine.console.record):
- with machine.console.record("session.mp4", fps=10): Record live video of guest execution
- with machine.console.record("action.gif", fps=5): Record animated GIF
- Access frame count, duration, and output path from recorder object
"""

import time
from pathlib import Path

from windows import ISO, Image, Machine, WindowsVersion


def main():
    print("=== VM Screen Video Recording Live Demo ===")

    # 1. Prepare base VM image using cached base image
    print("\n1. Preparing Windows VM overlay disk...")
    iso = ISO.from_version(WindowsVersion.WIN10_22H2)
    image = Image.from_iso(iso, output_disk="video_rec_demo.qcow2", use_cache=True)
    machine = Machine(image)

    try:
        # 2. Start VM and wait for QGA interface
        print("\n2. Starting Windows VM and waiting for QGA guest agent...")
        machine.run(timeout=180)
        print("   Guest OS is ready and responding.")

        # 3. Record an MP4 video of guest operations
        mp4_output = Path("guest_session.mp4")
        print(f"\n3. Recording live MP4 video to '{mp4_output}' at 10 FPS...")

        with machine.console.record(mp4_output, fps=10.0) as rec:
            print("   [Recording Started] Executing guest actions...")

            # (a) Create a text file in guest
            test_file = machine.file / r"C:\Users\Public\recording_demo.txt"
            test_file.write_text("Hello from Windows VM screen recording session!")
            print(f"   - Wrote text file: {test_file.read_text().strip()}")

            # (b) Run PowerShell commands
            print("   - Running PowerShell process listing command...")
            machine.command.run("Get-Process | Select-Object -First 5 ProcessName, Id | Format-Table")

            # (c) Registry modification
            print("   - Setting demo registry keys...")
            machine.registry.set_value(r"HKCU:\Software\VideoDemo", "Status", "Recorded", value_type="String")

            # Give a brief pause so visual changes are captured in video
            time.sleep(1.5)

            print(f"   [In-Progress Stats] Captured {rec.frame_count} frames ({rec.duration:.1f}s elapsed)")

        print(f"   ✓ Video recording saved: {mp4_output.resolve()} (size: {mp4_output.stat().st_size} bytes)")

        # 4. Record an animated GIF snippet
        gif_output = Path("guest_quick_action.gif")
        print(f"\n4. Recording animated GIF to '{gif_output}' at 5 FPS...")

        with machine.console.record(gif_output, fps=5.0) as gif_rec:
            print("   [GIF Recording Started] Writing temporary status updates...")
            for i in range(3):
                test_file.write_text(f"Step {i + 1}/3 completed at {time.time()}")
                time.sleep(0.5)

            print(f"   [GIF Stats] Captured {gif_rec.frame_count} frames ({gif_rec.duration:.1f}s)")

        print(f"   ✓ GIF recording saved: {gif_output.resolve()} (size: {gif_output.stat().st_size} bytes)")

        print("\n🎉 ALL VIDEO RECORDINGS COMPLETED SUCCESSFULLY!")

    finally:
        # 5. Clean shutdown
        print("\n5. Shutting down VM...")
        machine.power.off()
        print("VM powered off.")


if __name__ == "__main__":
    main()
