"""`retro_radio/server.py` の監査ログとセッション発行の回帰テスト。

このファイルが固定すること:

1. **同期 `/api/generate` の監査ログが結果を隠さない**こと
   （`finally` で常に `completed` / `success` を書いていた実装は、
   失敗した生成を成功として記録していた）。
2. **ブラウザがセッション Cookie を入手できること**
   （`auth/tokens.issue_session_token` には呼び出し元が無く、
   Cookie 認証でしか保護できない `/api/audio/{tenant}/{file}` に
   ブラウザが到達できなかった）。
3. ログアウトで Cookie が消え、保護が復活すること。

依存しないテスト方針:
`Settings` は `get_settings()` の lru_cache を経由するため、
`app.dependency_overrides` で**このテスト専用の設定**を注入する
（他のテストモジュールが `RETRO_RADIO_REQUIRE_AUTH` を 0 に差し替えると、
プロセス全体で影響を受けるため、環境変数に依存しない）。
"""

from __future__ import annotations

import os

import pytest

# `server._auth_enforced()` は環境変数を優先して読むため、
# このモジュールのテストを「認証必須」の状態で動かす。
os.environ.setdefault("RETRO_RADIO_REQUIRE_AUTH", "1")

from fastapi.testclient import TestClient  # noqa: E402

from retro_radio import server as server_module  # noqa: E402
from retro_radio.api.deps import settings_dependency  # noqa: E402
from retro_radio.auth.tokens import SESSION_COOKIE_NAME  # noqa: E402
from retro_radio.config import Settings  # noqa: E402

TEST_SECRET = "server-api-auth-test-secret-key"
TEST_SINGLE_USER_KEY = "server-api-auth-test-single-user-key"

AUTH_SETTINGS = Settings(
    require_auth=True,
    secret_key=TEST_SECRET,
    single_user_key=TEST_SINGLE_USER_KEY,
)

PAYLOAD = {"year": 1975, "month": 9, "day": 24, "mode": "normal"}


@pytest.fixture(autouse=True)
def privacy_tables():
    """S4 の `audit_logs` / `user_security` テーブルを 1 度だけ作る。

    `privacy_models` は `db/models.Base` と**別の MetaData** を持つため、
    conftest の `db_session` だけでは作られない。作らないと
    `_resolve_tenant`（Cookie 認証時のテナント照合）が
    `no such table` で落ちる。
    """
    from retro_radio.db.models import Base
    from retro_radio.db.privacy_models import create_privacy_tables
    from retro_radio.db.session import get_engine

    engine = get_engine()
    Base.metadata.create_all(bind=engine)  # checkfirst=True -> 2 回目以降は no-op
    create_privacy_tables(bind=engine)
    yield


@pytest.fixture
def auth_env(monkeypatch):
    """`/api/generate` が「認証必須」で動いている状態を作る。

    - `RETRO_RADIO_REQUIRE_AUTH=1`（`server._auth_enforced()` は環境変数を優先）
    - `app.dependency_overrides` で認証系依存に専用 Settings を注入
    """
    monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "1")
    server_module.app.dependency_overrides[settings_dependency] = lambda: AUTH_SETTINGS
    try:
        yield AUTH_SETTINGS
    finally:
        server_module.app.dependency_overrides.pop(settings_dependency, None)


@pytest.fixture
def client(mock_gtts, mock_itunes, auth_env):
    """認証必須かつ外部依存を遮断した TestClient（http）。"""
    with TestClient(server_module.app) as test_client:
        yield test_client


@pytest.fixture
def https_client(auth_env):
    """HTTPS で喋る TestClient（Cookie の `Secure` 判定を検証するため）。"""
    with TestClient(server_module.app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def logged_in_client(client):
    """単一ベアラー資格でセッションを発行済みのクライアント。

    監査ログのテストは「認証を通ったあと」の生成だけを観測したいので、
    先に `POST /api/auth/session` で Cookie を持たせる。
    """
    login = client.post(
        "/api/auth/session",
        headers={"Authorization": f"Bearer {TEST_SINGLE_USER_KEY}"},
    )
    assert login.status_code == 200, login.text
    return client


@pytest.fixture
def clean_throttle():
    """ログインの連続失敗記録を前後ともに取り除く（テスト間で混ざらないように）。"""
    from retro_radio.auth.authenticator import reset_login_throttle

    reset_login_throttle()
    yield
    reset_login_throttle()


@pytest.fixture
def audit_spy(monkeypatch):
    """`_record_generation_audit` の呼び出しを記録する。

    **元の実装へ委譲する**（潰さない）ため、
    「実際に監査ログへ 1 行書ける」ことも同時に検証される。
    監査が例外を投げた場合も、原本の fail-closed に任せる。
    """
    calls = []
    original = server_module._record_generation_audit

    def spy(tenant_id, user_id, phase, outcome, meta=None):
        calls.append({"phase": phase, "outcome": outcome, "meta": dict(meta or {})})
        return original(tenant_id, user_id, phase, outcome, meta)

    monkeypatch.setattr(server_module, "_record_generation_audit", spy)
    return calls


# ==============================================================================
# 1. 同期 /api/generate の監査ログが「実際に起きたこと」を記録する
# ==============================================================================
def test_failed_generation_records_failure_not_success(
    logged_in_client, monkeypatch, audit_spy
):
    """**失敗した生成を `completed` / `success` として記録しない**。

    修正前: 監査の書き込みが `finally` にあり、常に
    `phase="completed", outcome="success"` を書いていた。
    事故調査で「成功した」と嘘になるため、終端行は結果に従う。
    """
    from retro_radio.utils.errors import ScriptGenerationError

    def boom(*args, **kwargs):
        raise ScriptGenerationError("原稿の生成に失敗しました。")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)

    response = logged_in_client.post("/api/generate", json=PAYLOAD)
    assert response.status_code == 400, response.text

    phases = [call["phase"] for call in audit_spy]
    outcomes = [call["outcome"] for call in audit_spy]
    assert phases[0] == "started", phases
    assert phases[-1] == "failed", phases
    assert "completed" not in phases, phases
    assert outcomes[-1] == "failure", outcomes
    assert "success" not in outcomes[-1:], outcomes


def test_successful_generation_records_started_then_completed(
    logged_in_client, audit_spy
):
    """成功時は「開始」と「終端」で **2 行**、順序は `started` -> `completed`。

    `_record_generation_audit` の docstring が守る不変条件
    （1 回の生成 = 2 行 = 完全率 200%）も、ここで実際に成立する。
    """
    response = logged_in_client.post("/api/generate", json=PAYLOAD)
    assert response.status_code == 200, response.text

    assert [call["phase"] for call in audit_spy] == ["started", "completed"]
    assert [call["outcome"] for call in audit_spy] == ["success", "success"]


def test_audit_meta_carries_no_personal_text(logged_in_client, audit_spy):
    """監査 `meta` に原稿・氏名・対象年（生年相当）を入れない。"""
    response = logged_in_client.post(
        "/api/generate",
        json={**PAYLOAD, "mode": "anniversary", "target_name": "山田太郎"},
    )
    assert response.status_code == 200, response.text

    assert audit_spy, "監査ログが 1 行も書かれていない"
    for call in audit_spy:
        assert "script" not in call["meta"]
        assert "target_name" not in call["meta"]
        assert "year" not in call["meta"]


def test_busy_generation_records_failure(logged_in_client, monkeypatch, audit_spy):
    """同時実行上限で 503 になった場合も `failed` / `failure` で残る。"""

    class _BusySlots:
        def acquire(self, timeout=None):
            return False

        def release(self):  # pragma: no cover - 到達しない
            raise AssertionError("release() must not run when acquire failed")

    monkeypatch.setattr(server_module, "_generation_slots", _BusySlots())

    response = logged_in_client.post("/api/generate", json=PAYLOAD)
    assert response.status_code == 503, response.text

    assert [call["phase"] for call in audit_spy] == ["started", "failed"]
    assert audit_spy[-1]["outcome"] == "failure"


# ==============================================================================
# 2. セッション Cookie の発行（ブラウザが認証するための唯一の経路）
# ==============================================================================
def test_generate_requires_credentials_before_login(client):
    """Cookie が無い状態では保護が生きている（= ログインが意味を持つ）。"""
    response = client.post("/api/generate", json=PAYLOAD)
    assert response.status_code == 401, response.text


def test_login_issues_cookie_and_cookie_authenticates_generate(client):
    """`POST /api/auth/session` の Cookie で `/api/generate` に入れる。"""
    response = client.post(
        "/api/auth/session",
        headers={"Authorization": f"Bearer {TEST_SINGLE_USER_KEY}"},
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["authenticated"] is True
    assert body["auth_mode"] == "session"
    assert body["tenant_id"] == "default"
    # トークンそのものは応答に出さない（漏れる経路を作らない）
    assert SESSION_COOKIE_NAME not in response.text
    assert "token" not in body

    assert response.cookies.get(SESSION_COOKIE_NAME), "セッション Cookie が無い"

    # HttpOnly / SameSite=Lax が付くこと
    raw_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in raw_cookie
    assert "samesite=lax" in raw_cookie
    # 平文 HTTP（TestClient）では Secure を付けない（開発環境を壊さない）
    assert "secure" not in raw_cookie

    # 同じクライアントが Cookie を自動送信して認証される
    generated = client.post("/api/generate", json=PAYLOAD)
    assert generated.status_code == 200, generated.text


def test_session_cookie_is_secure_over_https(https_client):
    """HTTPS なら `Secure` も付く（平文のときだけ外す）。"""
    response = https_client.post(
        "/api/auth/session",
        headers={"Authorization": f"Bearer {TEST_SINGLE_USER_KEY}"},
    )
    assert response.status_code == 200, response.text

    raw_cookie = response.headers["set-cookie"].lower()
    assert "httponly" in raw_cookie
    assert "secure" in raw_cookie


def test_login_rejects_wrong_bearer_secret(client):
    """一致しない資格情報は 401 で、Cookie も発行されない。"""
    response = client.post(
        "/api/auth/session", headers={"Authorization": "Bearer not-the-key"}
    )
    assert response.status_code == 401, response.text
    assert not response.cookies.get(SESSION_COOKIE_NAME)


def test_login_error_does_not_reveal_whether_email_exists(
    client, db_session, make_user, clean_throttle
):
    """メールアドレスが無い場合と誤った場合も**同じ 401・同じ文言**。"""
    from retro_radio.auth.authenticator import hash_password, reset_login_throttle

    make_user(
        db_session,
        email="known@example.com",
        hashed_password=hash_password("correct-password"),
    )
    db_session.commit()

    unknown = client.post(
        "/api/auth/session",
        json={"email": "nobody@example.com", "password": "correct-password"},
    )
    # 2 回目の試行の前に連続失敗記録を消す（429 ではなく 401 を比較したい）
    reset_login_throttle()
    wrong = client.post(
        "/api/auth/session",
        json={"email": "known@example.com", "password": "wrong-password"},
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_login_with_email_and_password_sets_cookie(
    client, db_session, make_user, clean_throttle
):
    """email + password（`Authenticator`）でも同じ Cookie が発行される。"""
    from retro_radio.auth.authenticator import hash_password

    user = make_user(
        db_session,
        email="operator@example.com",
        hashed_password=hash_password("correct-password"),
    )
    db_session.commit()

    response = client.post(
        "/api/auth/session",
        json={"email": "operator@example.com", "password": "correct-password"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == user.id
    assert response.cookies.get(SESSION_COOKIE_NAME)

    assert client.post("/api/generate", json=PAYLOAD).status_code == 200


def test_logout_clears_cookie_and_restores_protection(client):
    """ログアウトで Cookie が消え、未認証アクセスが 401 へ戻る。"""
    login = client.post(
        "/api/auth/session",
        headers={"Authorization": f"Bearer {TEST_SINGLE_USER_KEY}"},
    )
    assert login.status_code == 200, login.text
    assert client.post("/api/generate", json=PAYLOAD).status_code == 200

    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200, logout.text
    # クライアントの Cookie 入れ物から消え、削除ヘッダも出る
    assert not logout.cookies.get(SESSION_COOKIE_NAME)
    assert SESSION_COOKIE_NAME in logout.headers["set-cookie"].lower()

    assert client.post("/api/generate", json=PAYLOAD).status_code == 401
