from app.adapters.plane_client import PlaneClient
from app.config import Settings


def test_plane_client_uses_x_api_key_header():
    settings = Settings(
        PLANE_BASE_URL="http://localhost:8080",
        PLANE_WORKSPACE_SLUG="test_workspace",
        PLANE_API_TOKEN="plane_api_test",
    )
    client = PlaneClient(settings)

    headers = client._headers()

    assert headers["X-API-Key"] == "plane_api_test"
    assert "Authorization" not in headers


def test_plane_client_enabled_requires_base_url_and_token():
    ok_settings = Settings(
        PLANE_BASE_URL="http://localhost:8080",
        PLANE_WORKSPACE_SLUG="test_workspace",
        PLANE_API_TOKEN="plane_api_test",
    )
    missing_token = Settings(
        PLANE_BASE_URL="http://localhost:8080",
        PLANE_WORKSPACE_SLUG="test_workspace",
        PLANE_API_TOKEN="",
    )

    assert PlaneClient(ok_settings)._enabled() is True
    assert PlaneClient(missing_token)._enabled() is False


def test_plane_client_has_description_update_api():
    settings = Settings(
        PLANE_BASE_URL="http://localhost:8080",
        PLANE_WORKSPACE_SLUG="test_workspace",
        PLANE_API_TOKEN="plane_api_test",
    )
    client = PlaneClient(settings)
    assert callable(getattr(client, "update_work_item_description", None))
