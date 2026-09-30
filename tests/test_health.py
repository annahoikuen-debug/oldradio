"""`/health` エンドポイント検証。

旧 `retro_radio.utils.health.health_check()` は Streamlit 依存モジュールとともに削除された。
リソース枯渇の観測点である `GET /health` を検証する形に置き換えた。
"""

REQUIRED_KEYS = {
    "status",
    "service",
    "version",
    "api_key_configured",
    "secret_key_configured",
}


def test_health_check_shape(client):
    """必須キーがすべて含まれ、200 を返す"""
    res = client.get("/health")
    assert res.status_code == 200
    assert REQUIRED_KEYS.issubset(set(res.json()))


def test_health_degraded_without_api_key(client, monkeypatch):
    """Gemini API キー未設定なら status=degraded / api_key_configured=False"""
    import retro_radio.server as server_module

    settings = server_module.settings
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)

    data = client.get("/health").json()
    assert data["status"] == "degraded"
    assert data["api_key_configured"] is False


def test_health_healthy_with_api_key(client, monkeypatch):
    """API キー設定済みなら status=healthy / api_key_configured=True"""
    import retro_radio.server as server_module

    settings = server_module.settings
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-test-key", raising=False)

    data = client.get("/health").json()
    assert data["status"] == "healthy"
    assert data["api_key_configured"] is True


def test_health_reports_service_metadata(client):
    """service / version は設定値と一致する"""
    from retro_radio.config import get_settings

    settings = get_settings()
    data = client.get("/health").json()
    assert "Retro Radio" in data["service"]
    assert data["version"] == settings.app_version


def test_health_secret_key_flag_tracks_config(client, monkeypatch):
    """secret_key_configured は設定と一致する（認証機能が使えるかの観測点）"""
    import retro_radio.server as server_module

    settings = server_module.settings
    monkeypatch.setattr(settings, "secret_key", "configured", raising=False)
    assert client.get("/health").json()["secret_key_configured"] is True

    monkeypatch.setattr(settings, "secret_key", "", raising=False)
    assert client.get("/health").json()["secret_key_configured"] is False


def test_health_does_not_require_api_key(client, monkeypatch):
    """API キー未設定でも 200（ヘルスチェックが 503 にならない）"""
    import retro_radio.server as server_module

    monkeypatch.setattr(server_module.settings, "gemini_api_key", "", raising=False)
    assert client.get("/health").status_code == 200
