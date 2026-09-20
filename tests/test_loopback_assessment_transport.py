from __future__ import annotations

import ssl

import pytest

from veyro.veyro.jev import JevVeyroModel


@pytest.mark.parametrize(
    "base_url,proxy_expected",
    [
        ("http://127.0.0.1:8080", False),
        ("http://localhost:8080", False),
        ("http://[::1]:8080", False),
        ("https://example.invalid", True),
    ],
)
async def test_local_assessment_stays_local_without_weakening_external_transport(
    monkeypatch,
    base_url,
    proxy_expected,
):
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:8080")
    client = JevVeyroModel(base_url=base_url, api_key="test-key")._make_client()
    try:
        assert bool(client._http_client._mounts) is proxy_expected
        assert client._http_client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    finally:
        await client.aclose()
