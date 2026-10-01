"""`/health` エンドポイント検証。

旧 `retro_radio.utils.health.health_check()` は Streamlit 依存モジュールとともに削除された。
リソース枯渇の観測点である `GET /health` を検証する形に置き換えた。

**情報開示の方針**: `auth_mode` / `auth_ready` / `secret_key_configured` は
**認証済みの呼び出しにだけ**返す（`retro_radio/server.py` の `health` の docstring）。
匿名のプローバ／攻撃者に「どの認証経路を選べばよいか」の手がかりを渡さないため。
したがってこれらのフィールドを検証するテストは**認証済み**の client を使う。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

#: 監視に必要な最小集合。認証状態に関わらず常に返る。
REQUIRED_KEYS = {
    "status",
    "service",
    "version",
    "api_key_configured",
}

#: **認証済み**の呼び出しにだけ返るフィールド。
AUTHENTICATED_ONLY_KEYS = {
    "auth_mode",
    "auth_ready",
    "secret_key_configured",
}

#: 32 文字以上のテスト用署名鍵（`tokens.MIN_SECRET_LENGTH` を満たす必要あり）。
TEST_SECRET = "health-endpoint-test-secret-key-0123456789"


@pytest.fixture
def auth_client(monkeypatch):
    """認証済みで `/health` を叩ける TestClient（session モード）。"""
    from retro_radio import server as server_module
    from retro_radio.api.deps import settings_dependency
    from retro_radio.config import Settings
    from retro_radio.auth.tokens import SESSION_COOKIE_NAME, issue_session_token

    monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "1")
    settings = Settings(require_auth=True, secret_key=TEST_SECRET)
    server_module.app.dependency_overrides[settings_dependency] = lambda: settings
    token = issue_session_token(
        user_id="health-probe", secret=TEST_SECRET, tenant_id="default", role="member"
    )
    server_module.app.dependency_overrides[settings_dependency] = lambda: settings
    try:
        with TestClient(server_module.app) as c:
            c.cookies.set(SESSION_COOKIE_NAME, token)
            yield c
    finally:
        server_module.app.dependency_overrides.pop(settings_dependency, None)


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


def test_health_hides_auth_fields_from_anonymous_callers(client, monkeypatch):
    """匿名の呼び出しには認証モードの情報が**出ない**こと。

    `auth_mode`（session / bearer / disabled）と `secret_key_configured` は
    「どの認証経路を選べばよいか」を攻撃者に与える。
    `server.health` の docstring が宣言している契約（認証済みだけ）を固定する。

    以前は分岐が無く**無条件**に返していた（実測）。
    """
    import retro_radio.server as server_module

    # 認証情報は設定済みだが、呼び出しは匿名のままにする。
    monkeypatch.setattr(server_module.settings, "secret_key", TEST_SECRET, raising=False)

    data = client.get("/health").json()

    for field in AUTHENTICATED_ONLY_KEYS:
        assert field not in data, (
            f"{field} が匿名の呼び出しに出ています（情報開示）。payload={data}"
        )
    # 監視に必要な最小集合は出ていること（過剰に隠さない）。
    assert REQUIRED_KEYS.issubset(set(data))


def test_health_returns_auth_fields_to_authenticated_callers(auth_client):
    """認証済みの呼び出しには認証モードの情報が**出る**こと（監視用途）。"""
    data = auth_client.get("/health").json()

    for field in AUTHENTICATED_ONLY_KEYS:
        assert field in data, f"{field} が認証済みの呼び出しで欠けています: {data}"

    assert data["auth_mode"] == "session"
    assert data["auth_ready"] is True
    assert data["secret_key_configured"] is True


def test_health_secret_key_flag_tracks_config(auth_client, monkeypatch):
    """secret_key_configured は設定と一致する（認証機能が使えるかの観測点）"""
    import retro_radio.server as server_module

    monkeypatch.setattr(server_module.settings, "secret_key", TEST_SECRET, raising=False)
    assert auth_client.get("/health").json()["secret_key_configured"] is True

    monkeypatch.setattr(server_module.settings, "secret_key", "", raising=False)
    assert auth_client.get("/health").json()["secret_key_configured"] is False


def test_health_does_not_require_api_key(client, monkeypatch):
    """API キー未設定でも 200（ヘルスチェックが 503 にならない）"""
    import retro_radio.server as server_module

    monkeypatch.setattr(server_module.settings, "gemini_api_key", "", raising=False)
    assert client.get("/health").status_code == 200
