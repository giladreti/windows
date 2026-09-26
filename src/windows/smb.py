import atexit
import logging
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from impacket.ntlm import compute_lmhash, compute_nthash
    from impacket.smbserver import SimpleSMBServer

    HAS_IMPACKET = True
except Exception:
    HAS_IMPACKET = False
    compute_lmhash: Any = None
    compute_nthash: Any = None
    SimpleSMBServer: Any = None

logger = logging.getLogger(__name__)


class SMBError(RuntimeError):
    """Exception raised for SMB server or sharing errors."""


class UniversalCredentials(dict):
    """Credentials dictionary that resolves any username to default password hashes,
    while also storing explicitly added user credentials.
    """

    def __init__(self, uid: int, lmhash: bytes | str, nthash: bytes | str):
        super().__init__()
        self.default = (uid, lmhash, nthash)

    def __contains__(self, key: Any) -> bool:
        return True

    def __getitem__(self, key: Any) -> tuple[int, Any, Any]:
        return super().get(str(key).lower(), self.default)

    def __len__(self) -> int:
        return max(1, super().__len__())


class SMBServerManager:
    """Manages an embedded user-space SMB server backed by Impacket with NTLM authentication."""

    DEFAULT_USERNAME = "Administrator"
    DEFAULT_PASSWORD = "Password123!"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5445,
        username: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username or self.DEFAULT_USERNAME
        self.password = password or self.DEFAULT_PASSWORD
        self.server: Any = None
        self._thread: threading.Thread | None = None
        self._shares: dict[str, tuple[Path, bool]] = {}
        self._credentials: dict[str, tuple[bytes, bytes]] = {}
        self._client_sockets: set[socket.socket] = set()
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Return True if the SMB server thread is active."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the SMB server in a background daemon thread."""
        with self._lock:
            if self.is_running:
                return

            if not HAS_IMPACKET:
                raise SMBError("SMB sharing requires the 'impacket' package to be installed.")

            try:
                server = SimpleSMBServer(listenAddress=self.host, listenPort=self.port)
                server.setSMB2Support(True)
                inner_server: Any = getattr(server, "getServer", lambda: None)()
                if inner_server is None:
                    inner_server = getattr(server, "_SimpleSMBServer__server", None)
                if inner_server is not None:
                    inner_server.daemon_threads = True

                    # Track active client sockets to forcefully terminate on shutdown
                    orig_get_request = inner_server.get_request

                    def hooked_get_request(s_self=inner_server, orig=orig_get_request):
                        sock, addr = orig()
                        self._client_sockets.add(sock)
                        return sock, addr

                    inner_server.get_request = hooked_get_request

                # Wrap pipe servers to silently catch OSError when unblocked on shutdown
                for attr in ("_SimpleSMBServer__srvsServer", "_SimpleSMBServer__wkstServer"):
                    pipe_server = getattr(server, attr, None)
                    if pipe_server is not None:
                        orig_pipe_run = pipe_server.run

                        def safe_pipe_run(r=orig_pipe_run):
                            try:
                                r()
                            except OSError:
                                pass

                        pipe_server.run = safe_pipe_run

                # Configure initial and registered credentials for authenticated SMB2 access
                lmhash = compute_lmhash(self.password)
                nthash = compute_nthash(self.password)
                self._credentials[self.username.lower()] = (lmhash, nthash)

                for user, (lm, nt) in self._credentials.items():
                    server.addCredential(user, 0, lm, nt)

                # Re-add existing registered shares if restarting
                for name, (path, ro) in self._shares.items():
                    ro_flag = "yes" if ro else "no"
                    server.addShare(name, str(path), readOnly=ro_flag)

                self.server = server
                self._thread = threading.Thread(target=self._run_server, daemon=True, name="SMBServerWorker")
                self._thread.start()

                # Register atexit handler so Python exit always stops the server cleanly
                atexit.register(self.stop)

                # Wait for port to open and accept connections
                start_t = time.time()
                while time.time() - start_t < 5.0:
                    try:
                        with socket.create_connection((self.host, self.port), timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise SMBError(f"SMB server failed to start listening on {self.host}:{self.port}")
            except Exception as exc:
                self.server = None
                self._thread = None
                raise SMBError(f"Failed to start SMB server: {exc}") from exc

    def _run_server(self) -> None:
        try:
            if self.server:
                self.server.start()
        except Exception as exc:
            logger.debug(f"SMB server loop terminated: {exc}")

    def stop(self) -> None:
        """Stop the SMB server and clean up threads and sockets.

        We guarantee immediate and clean termination by:
        1. Setting the shutdown request flag on the inner TCPServer.
        2. Forcefully closing all connected client sockets (unblocking recv in handler threads).
        3. Forcefully closing the listening socket (unblocking accept).
        4. Closing DCERPC pipe-server sockets (SRVSServer/WKSTServer) and waking them with dummy connections.
        5. Joining server threads with short timeouts so no deadlock can occur.
        """
        atexit.unregister(self.stop)
        with self._lock:
            if not self.server:
                return
            try:
                inner_server: Any = getattr(self.server, "getServer", lambda: None)()
                if inner_server is None:
                    inner_server = getattr(self.server, "_SimpleSMBServer__server", None)

                # 1. Signal serve_forever to stop and unblock client connections
                if inner_server is not None:
                    try:
                        inner_server._BaseServer__shutdown_request = True
                    except Exception:
                        pass

                    # 2. Forcefully close all accepted client sockets to unblock handler recv()
                    for client_sock in list(self._client_sockets):
                        try:
                            client_sock.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                        try:
                            client_sock.close()
                        except Exception:
                            pass
                    self._client_sockets.clear()

                    # 3. Close the main listening socket
                    try:
                        inner_server.socket.close()
                    except Exception:
                        pass

                # 4. Close DCERPC pipe-server sockets and wake them up from accept()
                for attr in ("_SimpleSMBServer__srvsServer", "_SimpleSMBServer__wkstServer"):
                    pipe_server = getattr(self.server, attr, None)
                    if pipe_server is not None:
                        sock = getattr(pipe_server, "_sock", None)
                        if sock is not None:
                            try:
                                port = sock.getsockname()[1]
                            except Exception:
                                port = None
                            try:
                                sock.close()
                            except Exception:
                                pass
                            if port is not None:
                                try:
                                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                        pass
                                except Exception:
                                    pass
                        try:
                            pipe_server.join(timeout=0.5)
                        except Exception:
                            pass

                # 5. Shutdown inner server with timeout protection
                if inner_server is not None:
                    try:
                        shutdown_thread = threading.Thread(target=inner_server.shutdown, daemon=True)
                        shutdown_thread.start()
                        shutdown_thread.join(timeout=0.5)
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug(f"Error shutting down SMB server: {exc}")
            finally:
                if self._thread:
                    self._thread.join(timeout=0.5)
                self.server = None
                self._thread = None

    def add_share(self, name: str, path: str | Path, read_only: bool = False) -> str:
        """Add a directory share to the SMB server."""
        norm_path = Path(path).resolve()
        if not norm_path.exists() or not norm_path.is_dir():
            raise FileNotFoundError(f"Share path does not exist or is not a directory: {path}")

        norm_name = name.strip().upper()
        with self._lock:
            self._shares[norm_name] = (norm_path, read_only)
            if not self.is_running:
                self.start()
            elif self.server:
                ro_flag = "yes" if read_only else "no"
                self.server.addShare(norm_name, str(norm_path), readOnly=ro_flag)
        return norm_name

    def add_credential(self, username: str, password: str) -> None:
        """Add an additional authenticated user credential to the SMB server."""
        lmhash = compute_lmhash(password)
        nthash = compute_nthash(password)
        with self._lock:
            self._credentials[username.lower()] = (lmhash, nthash)
            if self.server:
                self.server.addCredential(username, 0, lmhash, nthash)

    def remove_share(self, name: str) -> None:
        """Remove a directory share from the SMB server."""
        norm_name = name.strip().upper()
        with self._lock:
            self._shares.pop(norm_name, None)
            if self.server and self.is_running:
                try:
                    self.server.removeShare(norm_name)
                except Exception:
                    pass

    def has_share(self, name: str) -> bool:
        """Check if a share is currently registered."""
        norm_name = name.strip().upper()
        return norm_name in self._shares


def pipe(host: str, port: int) -> None:
    """Stream bidirectional TCP data between stdin/stdout and (host, port).

    Used as an inetd-like forwarder for QEMU guestfwd if netcat (nc) is not installed.
    """
    s = socket.create_connection((host, port))

    def to_sock() -> None:
        try:
            while True:
                chunk = sys.stdin.buffer.read(65536)
                if not chunk:
                    break
                s.sendall(chunk)
        except Exception:
            pass
        finally:
            try:
                s.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    t = threading.Thread(target=to_sock, daemon=True)
    t.start()

    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except Exception:
        pass
    finally:
        s.close()


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "pipe":
        pipe(sys.argv[2], int(sys.argv[3]))
