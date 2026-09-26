# Agent Guidelines for `windows` Codebase

This document defines core rules, architectural conventions, and validation policies for AI agents working on this project.

---

## 1. Zero-Mock Live Verification Policy
- While unit tests in `tests/` use `unittest.mock` for fast CI regression testing, **any new features or bug fixes must be verified on a real Windows VM without mocks**.
- Use thin `.qcow2` overlays on cached base images (`Image.from_base()` or `create_qcow2_overlay()`) to keep test iterations fast (~15-20 seconds to boot) and avoid modifying base images.
- Always clean up overlays and temporary images in `finally:` blocks.

---

## 2. Command Execution & Guest Agent (QGA)
- **Out-of-Band Channel**: All guest operations run over QEMU Guest Agent (`org.qemu.guest_agent.0`), not WinRM or SSH.
- **Privilege Separation**: By default, QGA executes as `NT AUTHORITY\SYSTEM`. Use `machine.as_user()` or `command.run(user=..., password=...)` when user context (e.g. `Administrator`, mapped drives, UserProfile) is required.
- **PowerShell Execution**: Set `powershell=True` when executing PowerShell commands; otherwise commands run in `cmd.exe`.
- **Readiness Check**: Always invoke `machine.command.wait_until_ready(timeout=180)` before running commands on a newly started VM.

---

## 3. Hardware & Storage Emulation
- **USB Storage (`machine.usb`)**: MBR-partitioned FAT32 raw disk images created on host and attached via `usb-storage`.
- **CD-ROM Media (`machine.cd`)**: Joliet Level 3 ISO images created via `pycdlib`, inserted via monitor `change cd0 <path>` or hotplugged USB CD-ROM, and ejected via `eject -f cd0`.
- **Virtual Microphone (`machine.microphone`)**: Isolated virtual audio null sinks on the host, bound to QEMU's audio backend and in-guest recording via WinMM MCI.
- **Shared Folders (`machine.file.share`)**: Host folders shared over local SMB (`10.0.2.4`) and mapped to Windows guest drive letters (e.g., `Z:`).

---

## 4. Code Quality & Standards
- Python `>= 3.13` with full type annotations.
- Code style enforced via `ruff check` and `ruff format`.
- Static typing verified via `ty check`.
- All tests must pass: `uv run python -m pytest tests/ -m "not slow"`.
