from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from handler.providers.sources import download_url


def test_vikingfile_r2_download_host_is_narrowly_allowed():
    download_url(
        "https://vikingfile.04b3d96d52475741e6b10f97f0a84a16.r2.cloudflarestorage.com/file"
    )

    for url in (
        "https://other.04b3d96d52475741e6b10f97f0a84a16.r2.cloudflarestorage.com/file",
        "https://vikingfile.invalid.r2.cloudflarestorage.com/file",
        "https://vikingfile.04b3d96d52475741e6b10f97f0a84a16.r2.cloudflarestorage.com.evil.test/file",
    ):
        with pytest.raises(ValueError):
            download_url(url)


def test_providers_require_authentication(client: TestClient):
    for path in ("/api/providers/status", "/api/providers/search"):
        assert client.get(path).status_code in (401, 403)
    assert client.post("/api/providers/index").status_code in (401, 403)
    assert client.post(
        "/api/providers/files", json={"result_id": "startgame:expired"}
    ).status_code in (401, 403)


def test_providers_require_administrator(client: TestClient, viewer_access_token: str):
    assert (
        client.get(
            "/api/providers/status",
            headers={"Authorization": f"Bearer {viewer_access_token}"},
        ).status_code
        == 403
    )


def test_disabled_provider_cannot_queue(client: TestClient, access_token: str):
    with (
        patch("endpoints.providers.ROM_PROVIDERS_ENABLED", False),
        patch("endpoints.providers.jobs.enqueue") as enqueue,
    ):
        response = client.post(
            "/api/providers/index", headers={"Authorization": f"Bearer {access_token}"}
        )
        assert response.status_code == 503
        enqueue.assert_not_called()


def test_provider_search_validates_query(client: TestClient, access_token: str):
    headers = {"Authorization": f"Bearer {access_token}"}
    with patch("endpoints.providers.ROM_PROVIDERS_ENABLED", True):
        assert (
            client.get(
                "/api/providers/search?provider=unknown", headers=headers
            ).status_code
            == 422
        )
        assert (
            client.get("/api/providers/search?page=0", headers=headers).status_code
            == 422
        )


def test_expired_result_cannot_supply_download_url(
    client: TestClient, access_token: str
):
    with (
        patch("endpoints.providers.ROM_PROVIDERS_ENABLED", True),
        patch("endpoints.providers.jobs.enqueue") as enqueue,
    ):
        response = client.post(
            "/api/providers/downloads",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "result_id": "edgeemu:expired",
                "platform_id": 1,
                "verified_url": "https://127.0.0.1/secret",
            },
        )
        assert response.status_code == 404
        enqueue.assert_not_called()
        response = client.post(
            "/api/providers/files",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "result_id": "startgame:expired",
                "verified_url": "https://127.0.0.1/secret",
            },
        )
        assert response.status_code == 404
