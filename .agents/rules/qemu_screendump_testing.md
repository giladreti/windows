# QEMU Screendump & Headless VNC Verification Workflow

When verifying QEMU VM startup, display rendering, and Windows ISO unattended setup progress:

## 1. QEMU Display & Monitor Configuration
- Use `-vnc 127.0.0.1:<vnc_display>` to render framebuffers over VNC without spawning host desktop GUI windows.
- **Do NOT use `-display none`** when `-vnc` is enabled, as `-display none` completely disables video hardware framebuffer generation in QEMU, causing VNC clients (like Remmina) to display an empty/black prompt.
- Use `-machine q35` for modern Windows 10 and 11 ISO compatibility (avoids legacy 440FX ACPI/HAL freeze and avoids OVMF read-only pflash hangs).
- Pass `-monitor unix:<socket_path>,server,nowait` to allow programmatic control and screen inspection.

## 2. Automated ISO Boot Keypress Bypassing
- Windows ISOs display `"Press any key to boot from CD or DVD..."` and wait 5 seconds.
- Connect to the monitor UNIX domain socket after VM startup and send keypresses:
  ```python
  with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
      s.connect(str(monitor_socket_path))
      s.recv(1024)
      s.sendall(b"sendkey ret\n")
      time.sleep(0.1)
      s.sendall(b"sendkey spc\n")
  ```

## 3. Headless Screen Verification via Screendump
- Issue the QEMU monitor command `screendump <path.ppm>` over the monitor UNIX socket:
  ```python
  s.sendall(f"screendump {dump_path}\n".encode())
  ```
- Inspect the PPM header resolution and pixel values to verify graphical output without requiring visual user intervention:
  - **720x400 with ~54 non-black pixels**: Stuck at VGA text mode boot prompt / blinking cursor.
  - **1024x768 / 1280x800 with > 10,000 non-black pixels**: Windows PE graphical installer GUI successfully loaded.
