"""Unit tests for QEMU Guest Agent (QGA) client protocol and file streaming."""

import base64
import json
import socket
import threading
from pathlib import Path

import pytest

from windows.qga import QGAClient, QGAError


class MockQGAServer:
    """Mock QGA Unix domain socket server simulating QGA JSON-RPC responses."""

    def __init__(self, socket_path: Path):
        self.socket_path = socket_path
        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(socket_path))
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
                    data = conn.recv(4096).decode("utf-8")
                    if not data:
                        continue
                    for line in data.splitlines():
                        if not line.strip():
                            continue
                        req = json.loads(line)
                        cmd = req.get("execute")
                        args = req.get("arguments", {})

                        if cmd == "guest-ping":
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
    yield sock_path
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
