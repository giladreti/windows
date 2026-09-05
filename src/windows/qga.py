"""QEMU Guest Agent (QGA) JSON-RPC client over Unix domain socket."""

import base64
import json
import socket
import time
from pathlib import Path
from typing import Any


class QGAError(RuntimeError):
    """Exception raised for QEMU Guest Agent errors."""


class QGAClient:
    """Client for communicating with QEMU Guest Agent over a Unix domain socket using JSON-RPC."""

    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path).resolve()

    def _send_command(self, cmd: str, args: dict[str, Any] | None = None, timeout: float = 30.0) -> dict[str, Any]:
        """Send a JSON-RPC request to QGA and return the decoded response dict."""
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args

        req_bytes = json.dumps(payload).encode("utf-8") + b"\n"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            try:
                s.connect(str(self.socket_path))
            except OSError as exc:
                raise QGAError(f"Failed to connect to QGA socket at {self.socket_path}: {exc}") from exc

            # Drain any stale buffered bytes from previous operations
            s.setblocking(False)
            try:
                while True:
                    discarded = s.recv(4096)
                    if not discarded:
                        break
            except (BlockingIOError, OSError):
                pass
            finally:
                s.setblocking(True)
                s.settimeout(timeout)

            s.sendall(req_bytes)

            # Read response lines until valid JSON object is received
            buffer = ""
            while True:
                try:
                    chunk = s.recv(4096).decode("utf-8", errors="replace")
                except TimeoutError as exc:
                    raise QGAError(f"Timed out waiting for response from QGA socket for command {cmd}") from exc
                if not chunk:
                    break
                buffer += chunk
                if "\n" in buffer:
                    for line in buffer.splitlines():
                        line = line.strip()
                        if line:
                            try:
                                resp = json.loads(line)
                                if "error" in resp:
                                    raise QGAError(f"QGA error response for {cmd}: {resp['error']}")
                                if "return" in resp:
                                    return resp["return"]
                            except json.JSONDecodeError:
                                continue

        raise QGAError(f"No valid JSON response received from QGA socket for command {cmd}")

    def ping(self, timeout: float = 5.0) -> bool:
        """Ping the QEMU Guest Agent. Return True if responsive."""
        try:
            self._send_command("guest-ping", timeout=timeout)
            return True
        except Exception:
            return False

    def exec(
        self,
        command: str,
        args: list[str] | None = None,
        powershell: bool = True,
        timeout: int = 60,
    ) -> tuple[str, str, int]:
        """Execute a command inside the guest via QGA and return (stdout, stderr, returncode)."""
        if powershell:
            exec_path = "powershell.exe"
            exec_args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
        else:
            exec_path = "cmd.exe"
            exec_args = ["/c", command]

        if args:
            exec_args.extend(args)

        # 1. Spawn process inside guest
        spawn_resp = self._send_command(
            "guest-exec",
            args={
                "path": exec_path,
                "arg": exec_args,
                "capture-output": True,
            },
        )
        pid = spawn_resp.get("pid")
        if pid is None:
            raise QGAError("QGA guest-exec failed to return a PID")

        # 2. Poll guest-exec-status until process exits
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                status_resp = self._send_command(
                    "guest-exec-status",
                    args={"pid": pid},
                    timeout=15.0,
                )
            except Exception:
                time.sleep(0.5)
                continue

            if status_resp.get("exited", False):
                exitcode = status_resp.get("exitcode", 0)
                out_b64 = status_resp.get("out-data", "")
                err_b64 = status_resp.get("err-data", "")

                stdout = base64.b64decode(out_b64).decode("utf-8", errors="replace") if out_b64 else ""
                stderr = base64.b64decode(err_b64).decode("utf-8", errors="replace") if err_b64 else ""

                return stdout, stderr, exitcode

            time.sleep(0.5)

        raise QGAError(f"Command execution timed out after {timeout} seconds (PID {pid})")

    def file_open(self, path: str, mode: str = "r") -> int:
        """Open a file on the guest and return an integer file handle."""
        resp = self._send_command("guest-file-open", args={"path": path, "mode": mode})
        handle = resp.get("return") if isinstance(resp, dict) and "return" in resp else resp
        if isinstance(handle, int):
            return handle
        raise QGAError(f"QGA guest-file-open failed to return handle for {path}: {resp}")

    def file_close(self, handle: int) -> None:
        """Close an open file handle on the guest."""
        try:
            self._send_command("guest-file-close", args={"handle": handle})
        except Exception:
            pass

    def file_read(self, handle: int, count: int = 524288) -> tuple[bytes, bool]:
        """Read up to count bytes from an open file handle. Returns (data_bytes, eof_bool)."""
        resp = self._send_command("guest-file-read", args={"handle": handle, "count": count})
        buf_b64 = resp.get("buf-b64", "")
        eof = resp.get("eof", False)
        data = base64.b64decode(buf_b64) if buf_b64 else b""
        return data, eof

    def file_write(self, handle: int, data: bytes) -> int:
        """Write bytes to an open file handle. Returns count of bytes written."""
        buf_b64 = base64.b64encode(data).decode("ascii")
        resp = self._send_command("guest-file-write", args={"handle": handle, "buf-b64": buf_b64})
        return resp.get("count", len(data))

    def read_file(self, path: str) -> bytes:
        """Read entire file from guest via QGA file APIs."""
        handle = self.file_open(path, mode="rb")
        chunks = []
        try:
            while True:
                data, eof = self.file_read(handle, count=524288)
                if data:
                    chunks.append(data)
                if eof or not data:
                    break
            return b"".join(chunks)
        finally:
            self.file_close(handle)

    def write_file(self, path: str, data: bytes, chunk_size: int = 262144) -> None:
        """Write entire byte payload to file on guest via QGA file APIs."""
        handle = self.file_open(path, mode="wb")
        try:
            offset = 0
            total = len(data)
            while offset < total:
                chunk = data[offset : offset + chunk_size]
                self.file_write(handle, chunk)
                offset += len(chunk)
        finally:
            self.file_close(handle)
