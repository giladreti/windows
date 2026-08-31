"""Integration tests for real HTTP ISO downloading."""

import http.server
import socketserver
import threading

import pytest

from windows.iso import download_file, resolve_iso


class MockISOHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    """Simple HTTP handler serving a dummy ISO byte stream."""

    def do_GET(self):
        body = b"\x00" * 32768 + b"\x01CD001\x01\x00REAL_DOWNLOADED_ISO_DATA" + b"\x00" * 2048
        self.send_response(200)
        self.send_header("Content-Type", "application/x-iso9660-image")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress server stdout logs in test output


@pytest.fixture
def http_iso_server():
    """Fixture providing a running local HTTP server serving an ISO file."""
    server = socketserver.TCPServer(("127.0.0.1", 0), MockISOHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/windows_11_test.iso"
    server.shutdown()
    server.server_close()


def test_real_http_iso_download_end_to_end(http_iso_server, tmp_path):
    """Test that downloading an ISO via real HTTP socket request works end-to-end."""
    url = http_iso_server
    cache_dir = tmp_path / "iso_cache"

    # Call resolve_iso with a real HTTP URL
    downloaded_path = resolve_iso(url, cache_dir=str(cache_dir))

    # Verify file was downloaded to disk
    assert downloaded_path.exists()
    assert downloaded_path.is_file()
    assert downloaded_path.stat().st_size > 0

    # Verify actual contents received over HTTP
    content = downloaded_path.read_bytes()
    assert b"REAL_DOWNLOADED_ISO_DATA" in content


def test_download_file_direct(http_iso_server, tmp_path):
    """Test direct download_file method over HTTP stream."""
    url = http_iso_server
    dest_file = tmp_path / "direct_download.iso"

    download_file(url, dest_file)

    assert dest_file.exists()
    assert b"REAL_DOWNLOADED_ISO_DATA" in dest_file.read_bytes()
