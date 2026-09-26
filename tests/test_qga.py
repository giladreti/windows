"""Unit tests for QEMU Guest Agent (QGA) client protocol and file streaming."""

import base64
import json
import socket
import threading
from typing import Any

import pytest

from windows.qga import QGAClient, QGAError


class MockQGAServer:
    """Mock QGA socket server simulating QGA JSON-RPC responses over UNIX domain socket or TCP."""

    def __init__(self, endpoint: Any):
        self.endpoint = endpoint
        if isinstance(endpoint, tuple) or isinstance(endpoint, int):
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port = endpoint[1] if isinstance(endpoint, tuple) else endpoint
            self.server_socket.bind(("127.0.0.1", port))
        else:
            try:
                self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.server_socket.bind(str(endpoint))
            except (AttributeError, OSError):
                self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server_socket.bind(("127.0.0.1", 0))
                self.endpoint = ("127.0.0.1", self.server_socket.getsockname()[1])
        self.server_socket.listen(5)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.active = True
        self.files: dict[int, bytearray] = {}
        self.next_handle = 100

    def start(self):
        self.thread.start()

    def stop(self):
        self.active = False
        try:
            self.server_socket.close()
        except Exception:
            pass

    def _run(self):
        while self.active:
            try:
                conn, _ = self.server_socket.accept()
                with conn:
                    while self.active:
                        data = conn.recv(4096).decode("utf-8", errors="replace")
                        if not data:
                            break
                        for line in data.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("\xff"):
                                line = line[1:].strip()
                            req = json.loads(line)
                            cmd = req.get("execute")
                            args = req.get("arguments", {})

                            if cmd in ("guest-sync-delimited", "guest-sync"):
                                sid = args.get("id", 0)
                                if cmd == "guest-sync-delimited":
                                    conn.sendall(b"\xff" + json.dumps({"return": sid}).encode("utf-8") + b"\n")
                                else:
                                    conn.sendall(json.dumps({"return": sid}).encode("utf-8") + b"\n")
                            elif cmd == "guest-ping":
                                conn.sendall(json.dumps({"return": {}}).encode("utf-8") + b"\n")
                            elif cmd == "guest-exec":
                                conn.sendall(json.dumps({"return": {"pid": 42}}).encode("utf-8") + b"\n")
                            elif cmd == "guest-exec-status":
                                resp = {
                                    "return": {
                                        "exited": True,
                                        "exitcode": 0,
                                        "out-data": base64.b64encode(b"MOCK_QGA_STDOUT").decode("utf-8"),
                                        "err-data": "",
                                    }
                                }
                                conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                            elif cmd == "guest-file-open":
                                h = self.next_handle
                                self.next_handle += 1
                                self.files[h] = bytearray(b"MOCK_FILE_CONTENT")
                                conn.sendall(json.dumps({"return": h}).encode("utf-8") + b"\n")
                            elif cmd == "guest-file-read":
                                h = args.get("handle")
                                buf = self.files.get(h, bytearray())
                                conn.sendall(
                                    json.dumps(
                                        {
                                            "return": {
                                                "count": len(buf),
                                                "buf-b64": base64.b64encode(buf).decode("utf-8"),
                                                "eof": True,
                                            }
                                        }
                                    ).encode("utf-8")
                                    + b"\n"
                                )
                            elif cmd == "guest-file-write":
                                b64 = args.get("buf-b64", "")
                                raw = base64.b64decode(b64)
                                conn.sendall(
                                    json.dumps({"return": {"count": len(raw), "eof": False}}).encode("utf-8") + b"\n"
                                )
                            elif cmd == "guest-file-close":
                                conn.sendall(json.dumps({"return": {}}).encode("utf-8") + b"\n")
                            else:
                                conn.sendall(
                                    json.dumps({"error": {"class": "GenericError", "desc": "unknown"}}).encode("utf-8")
                                    + b"\n"
                                )
            except Exception:
                break


@pytest.fixture
def qga_server(tmp_path):
    sock_path = tmp_path / "test_qga.sock"
    server = MockQGAServer(sock_path)
    server.start()
    yield server.endpoint
    server.stop()


def test_qga_client_ping(qga_server):
    client = QGAClient(qga_server)
    assert client.ping() is True


def test_qga_client_exec(qga_server):
    client = QGAClient(qga_server)
    stdout, stderr, returncode = client.exec("dir C:\\")
    assert stdout == "MOCK_QGA_STDOUT"
    assert stderr == ""
    assert returncode == 0


def test_qga_client_file_read_and_write(qga_server):
    client = QGAClient(qga_server)
    data = client.read_file(r"C:\test.txt")
    assert data == b"MOCK_FILE_CONTENT"

    client.write_file(r"C:\test_out.txt", b"NEW_DATA")


def test_qga_client_connection_error(tmp_path):
    client = QGAClient(tmp_path / "nonexistent.sock")
    assert client.ping() is False
    with pytest.raises(QGAError):
        client.exec("hostname")


def test_qga_client_tcp():
    """Test QGAClient over TCP loopback endpoint."""
    server = MockQGAServer(("127.0.0.1", 0))
    # get actual bound port
    port = server.server_socket.getsockname()[1]
    server.endpoint = ("127.0.0.1", port)
    server.start()
    try:
        client = QGAClient(server.endpoint)
        assert client.ping() is True
        stdout, _stderr, code = client.exec("whoami")
        assert stdout == "MOCK_QGA_STDOUT"
        assert code == 0
        data = client.read_file(r"C:\tcp_test.txt")
        assert data == b"MOCK_FILE_CONTENT"
    finally:
        server.stop()
