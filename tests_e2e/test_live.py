"""Live internet connectivity test for default ISO URLs."""

import requests

from windows.iso import WINDOWS_VERSION_URLS, WindowsVersion


def test_live_primary_default_url_is_working():
    """Verify that the default ISO URL configured for 25H2 responds with HTTP 200."""
    primary_url = WINDOWS_VERSION_URLS[WindowsVersion.WIN11_25H2][0]

    # Perform a streaming GET request with stream=True so we only fetch headers and first chunk
    response = requests.get(primary_url, stream=True, timeout=10, headers={"User-Agent": "Mozilla/5.0"})

    assert response.status_code == 200, f"Primary default ISO URL {primary_url} returned status {response.status_code}"

    # Read the first 1KB to verify data transmission over real network stream
    first_chunk = next(response.iter_content(chunk_size=1024))
    assert len(first_chunk) > 0, "Failed to read byte stream from primary default ISO URL"
