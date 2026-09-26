# Live VM Testing & Verification Guidelines

When developing, debugging, or validating features in this codebase:

## 1. Zero-Mock Live Verification Requirement
- Unit tests (`tests/test_*.py`) with `unittest.mock` are necessary for regression CI, but **live integration verification without mocks is strictly required** for all new features before marking them complete.
- Verify features against an actual running Windows guest whenever possible.

## 2. Fast & Safe Live Testing Pattern
- Never mutate base images directly. Always create a thin copy-on-write overlay:
  ```python
  from windows import Image
  from windows.qemu import create_qcow2_overlay

  # Create an ephemeral overlay
  overlay_path = tmp_path / "test_overlay.qcow2"
  create_qcow2_overlay(overlay_path, base_image_path)
  ```
- Always boot with `headless=True` and appropriate memory/CPU allocation (`ram_mb=4096, cpus=4`).
- Always wait for QEMU Guest Agent (QGA) readiness before issuing commands:
  ```python
  vm.power.on()
  assert vm.command.wait_until_ready(timeout=180), "VM failed to boot"
  ```
- Always ensure resources are torn down cleanly in `finally` blocks:
  ```python
  try:
      # test operations...
  finally:
      vm.power.off()
      vm.close()
      overlay_path.unlink(missing_ok=True)
  ```

## 3. Signal Isolation & Background Process Management
- Subprocesses spawned by `QEMUProcessManager` must be isolated from parent process group signals (`start_new_session=True` on POSIX, `CREATE_NEW_PROCESS_GROUP` on Windows) so that `KeyboardInterrupt` / `SIGINT` on test runners does not leave orphan QEMU processes or kill them prematurely.
- Always close sockets and file descriptors when terminating tests.
