"""セキュリティ回帰テスト（Wave 1 の修正を固定する）。

優先度:
  1. Path Traversal 防止（`/static/../.env` 等がファイルを読めないこと）
  2. 実日付バリデーション
  3. エラー応答の機密漏洩防止
  4. CORS がワイルドカードでないこと
"""

import os
from pathlib import Path

import pytest

from retro_radio.config import Settings, get_settings


SECRET_MARKERS = (
    "RETRO_RADIO_GEMINI_API_KEY",
    "RETRO_RADIO_SECRET_KEY",
    "stripe_secret",
    "AIza",
    "sk_live",
    "Traceback (most recent call last)",
)


# --- 1. Path Traversal ----------------------------------------------------------
TRAVERSAL_TARGETS = [
    "/static/../.env",
    "/static/../../.env",
    "/static/..%2F..%2F.env",
    "/static/%2e%2e/%2e%2e/.env",
    "/static/....//....//.env",
    "/static/../.git/config",
    "/static/../requirements.txt",
    "/static/../pytest.ini",
    "/static/../.github/workflows/ci.yml",
    "/static/..%2F..%2Fetc%2Fpasswd",
    "/static/..\\..\\.env",
    "/static/%2e%2e%2f%2e%2e%2f.env",
    "/static/../server.py",
    "/static/../config.py",
]


@pytest.mark.parametrize("path", TRAVERSAL_TARGETS)
def test_static_path_traversal_is_rejected(client, path):
    """`static/` 配下の外へ抜け出られない（404/403/400 のいずれか）"""
    res = client.get(path)
    assert res.status_code in (400, 403, 404), f"{path} -> {res.status_code}"
    for marker in SECRET_MARKERS:
        assert marker not in res.text


def test_static_traversal_does_not_leak_env_file(client):
    """実在するリポジトリの .env.example  EVEN traversal を含めても読めない"""
    project_root = Path(__file__).resolve().parent.parent
    target = project_root / ".env.example"
    if not target.exists():
        pytest.skip(".env.example が存在しない環境")

    res = client.get("/static/../.env.example")
    assert res.status_code in (400, 403, 404)
    assert "RETRO_RADIO_" not in res.text


def test_static_serves_files_inside_static_dir(client):
    """静的ディレクトリ内のファイルは配信できる（Traversal 対策で潰していないことの対比）"""
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if not (static_dir / "index.html").exists():
        pytest.skip("static/index.html が存在しない環境")

    res = client.get("/static/index.html")
    assert res.status_code == 200


def test_app_js_is_exposed(client):
    """フロントバンドル app.js が /static 配下で配信される"""
    res = client.get("/static/app.js")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith(("text/javascript", "application/javascript"))


def test_removed_unsafe_static_route_is_gone():
    """サニタイズなし的自作 `/static/{path}` ルートが復活していないこと"""
    from retro_radio.server import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/static/{path}" not in paths
    mounts = [getattr(route, "path", None) for route in app.routes if hasattr(route, "app")]
    assert "/static" in mounts


# --- 2. 実日付バリデーション ----------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"year": 2020, "month": 2, "day": 30},
        {"year": 2019, "month": 2, "day": 29},
        {"year": 1900, "month": 2, "day": 29},   # 平年の100年うるう年ではない
        {"year": 2020, "month": 4, "day": 31},
        {"year": 2020, "month": 6, "day": 31},
        {"year": 2020, "month": 9, "day": 31},
        {"year": 2020, "month": 11, "day": 31},
        {"year": 2020, "month": 13, "day": 1},
        {"year": 2020, "month": 0, "day": 1},
        {"year": 2020, "month": 1, "day": 0},
        {"year": 2020, "month": 1, "day": 32},
    ],
)
def test_generate_rejects_non_existent_dates(client, payload):
    """実在しない日付は 422（曜日計算での 500 を防止）"""
    res = client.post("/api/generate", json=payload)
    assert res.status_code == 422, res.text


@pytest.mark.parametrize("mode", ["bogus", "NORMAL", "", "care", "anniv"])
def test_generate_rejects_unknown_mode(client, mode):
    """mode は Literal で制限される"""
    res = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": mode}
    )
    assert res.status_code == 422, res.text


def test_generate_rejects_year_out_of_range(client):
    settings = get_settings()
    for year in (settings.min_year - 1, settings.max_year + 1, 0, -1):
        res = client.post("/api/generate", json={"year": year, "month": 1, "day": 1})
        assert res.status_code == 422


def test_generate_accepts_leap_day(client):
    """閏年の 2/29 は通る（ valentine's な偶然を弾かない）"""
    res = client.post("/api/generate", json={"year": 2020, "month": 2, "day": 29})
    assert res.status_code == 200, res.text


def test_validation_error_does_not_echo_secrets(client):
    """422 のレスポンスに API キー等が含まれない"""
    res = client.post("/api/generate", json={"year": 2020, "month": 2, "day": 30})
    assert res.status_code == 422
    for marker in SECRET_MARKERS:
        assert marker not in res.text


# --- 3. エラー応答の機密漏洩防止 -------------------------------------------------
def test_script_generation_failure_does_not_leak_internals(client, monkeypatch):
    """原稿生成が例外を投げても、例外文字列・スタック・パスが応答に出ない"""
    import retro_radio.server as server_module

    secret = "AIzaSySUPERSECRETKEY0000000000000000"
    monkeypatch.setattr(server_module.settings, "gemini_api_key", secret, raising=False)

    def boom(*args, **kwargs):
        raise RuntimeError(f"connection to /home/deploy/.env failed, key={secret}")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)

    res = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
    assert res.status_code >= 400

    body = res.text
    assert secret not in body
    assert "connection to" not in body
    assert "/home/deploy" not in body
    assert "Traceback" not in body
    assert "RuntimeError" not in body
    assert ".env" not in body
    # ユーザー向けの定型メッセージだけが返る
    assert "時間をおいて" in res.json()["detail"]


def test_script_generation_error_does_not_leak_internals(client, monkeypatch):
    """AppError  subtypes も同じく内部情報を漏らさない"""
    import retro_radio.server as server_module
    from retro_radio.utils.errors import ScriptGenerationError

    def boom(*args, **kwargs):
        raise ScriptGenerationError("安全メッセージ", "key=AIzaSySECRET /home/deploy/x.py")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)

    res = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
    assert res.status_code >= 400
    assert "AIzaSySECRET" not in res.text
    assert "key=" not in res.text
    assert "/home/deploy" not in res.text


def test_script_generation_api_key_error_maps_to_503(client, monkeypatch):
    """API キー起因の原稿生成エラーは 503 になるべき"""
    import retro_radio.server as server_module
    from retro_radio.utils.errors import ScriptGenerationError

    def boom(*args, **kwargs):
        raise ScriptGenerationError("Gemini APIキーが設定されていません", "key=AIzaSySECRET")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)

    res = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
    assert res.status_code == 503


def test_health_reports_degraded_without_api_key_but_generate_returns_503(client, monkeypatch):
    """/health が degraded を返すなら生成APIも 503 を返すべき（502/400との不整合防止）"""
    import retro_radio.server as server_module
    from retro_radio.utils.errors import ScriptGenerationError

    monkeypatch.setattr(server_module.settings, "gemini_api_key", "", raising=False)
    assert client.get("/health").json()["status"] == "degraded"

    def boom(*args, **kwargs):
        raise ScriptGenerationError("Gemini APIキーが設定されていません", "GEMINI_API_KEY not configured")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)
    res = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
    assert res.status_code == 503


def test_non_api_key_script_error_stays_400(client, monkeypatch):
    """APIキー起因でない ScriptGenerationError は 400 のまま"""
    import retro_radio.server as server_module
    from retro_radio.utils.errors import ScriptGenerationError

    def boom(*args, **kwargs):
        raise ScriptGenerationError("原稿の生成に失敗しました。", "boom")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)
    res = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
    assert res.status_code == 400


def test_health_endpoint_does_not_leak_settings(client):
    """/health は内部パスやキー値を返さない"""
    body = client.get("/health").text
    for marker in SECRET_MARKERS + ("E:\\", "/home/", "sqlite:///"):
        assert marker not in body


def test_openapi_schema_does_not_leak_secrets(client):
    """OpenAPI スキーマに既定値が漏れない"""
    body = client.get("/openapi.json").text
    for marker in ("AIza", "sk_live", "whsec_"):
        assert marker not in body


# --- 4. CORS --------------------------------------------------------------------
def test_cors_origins_are_not_wildcard():
    """CORS 許可オリジンが `*` ではない"""
    settings = Settings()
    assert settings.cors_origins != ["*"]
    assert "*" not in settings.cors_origins
    assert all(origin.startswith("http") for origin in settings.cors_origins)


def test_default_cors_origins_are_localhost():
    assert Settings().cors_origins == [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ]


def test_credentials_disabled_by_default():
    """ワイルドカードと組ませる credentials は既定で無効"""
    assert Settings().cors_allow_credentials is False


def test_unlisted_origin_gets_no_cors_header(client):
    """許可リストに無いオリジンには Access-Control-Allow-Origin を返さない"""
    res = client.options(
        "/api/generate",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers} or (
        res.headers.get("access-control-allow-origin") != "https://evil.example.com"
    )


def test_unlisted_origin_response_has_no_allow_credentials(client):
    """未許可オリジンに対してAccess-Control-Allow-Credentials を返さない"""
    res = client.get("/health", headers={"Origin": "https://evil.example.com"})
    headers = {k.lower() for k in res.headers}
    assert "access-control-allow-credentials" not in headers or (
        res.headers.get("access-control-allow-origin") != "https://evil.example.com"
    )


def test_listed_origin_gets_cors_header(client):
    """許可リストのオリジンには CORS ヘッダーが付く"""
    res = client.options(
        "/api/generate",
        headers={
            "Origin": "http://localhost:8501",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert res.headers.get("access-control-allow-origin") == "http://localhost:8501"


def test_cors_origins_env_override(monkeypatch):
    """CORS 許可オリジンは環境変数で上書きできる（カンマ区切り）"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "https://a.example.com, https://b.example.com")
    settings = Settings()
    assert settings.cors_origins == ["https://a.example.com", "https://b.example.com"]


def test_cors_origins_env_override_json(monkeypatch):
    """JSON 配列形式の環境変数なら上書きできる"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", '["https://a.example.com"]')
    assert Settings().cors_origins == ["https://a.example.com"]


def test_cors_origins_env_override_wildcard(monkeypatch):
    """ワイルドカード `*` でも起動できる（SettingsError にしない）"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "*")
    assert Settings().cors_origins == ["*"]


def test_cors_origins_env_override_single_origin(monkeypatch):
    """単独の origin でも List になる"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "http://localhost:8501")
    assert Settings().cors_origins == ["http://localhost:8501"]


def test_cors_origins_env_override_comma_without_space(monkeypatch):
    """スペース無しのカンマ区切りも要素を trim して返す"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "http://a,http://b")
    assert Settings().cors_origins == ["http://a", "http://b"]


def test_cors_origins_env_override_empty_is_empty_list(monkeypatch):
    """空文字は起動を壊さず空リストになる"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "")
    assert Settings().cors_origins == []


def test_cors_origins_accepts_list_directly():
    """コンストラクタへ list を渡した場合もそのまま通る"""
    assert Settings(cors_origins=["http://a"]).cors_origins == ["http://a"]


def test_broken_cors_origins_json_does_not_crash_startup(monkeypatch):
    """`[` で始まる壊れた JSON でもカンマ区切りとして解釈して起動を保つ"""
    monkeypatch.setenv("RETRO_RADIO_CORS_ORIGINS", "[http://a")
    assert Settings().cors_origins == ["[http://a"]


# --- 5. その他の設定面の硬化 ----------------------------------------------------
def test_secret_key_warning_is_emitted_when_missing():
    """secret_key 未設定は起動を妨げないが RuntimeWarning で明示する"""
    with pytest.warns(RuntimeWarning, match="RETRO_RADIO_SECRET_KEY"):
        Settings(secret_key="")


def test_settings_ignore_unknown_env(monkeypatch):
    """未知の RETRO_RADIO_* は無視される（extra='forbid' を廃止）"""
    monkeypatch.setenv("RETRO_RADIO_TOTALLY_UNKNOWN_FIELD", "1")
    assert Settings() is not None


def test_concurrency_limit_defaults():
    """同時実行数の上限と待ち時間タイムアウトが設定されている"""
    settings = Settings()
    assert settings.max_concurrent_generations >= 1
    assert settings.generation_wait_timeout >= 1


def test_server_enforces_concurrency_limit(client, monkeypatch):
    """同時実行数を超えたら 503（保護がenda されていることの確認）"""
    import threading

    import retro_radio.server as server_module

    started = threading.Event()
    release = threading.Event()

    def slow_script(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        return "### オープニング\n遅い原稿"

    monkeypatch.setattr(server_module, "generate_radio_script", slow_script)
    monkeypatch.setattr(server_module, "_generation_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(
        server_module.settings, "generation_wait_timeout", 1, raising=False
    )

    results = []

    def call():
        results.append(
            client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
        )

    first = threading.Thread(target=call)
    first.start()
    try:
        assert started.wait(timeout=5)
        second = client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})
        assert second.status_code == 503
        assert "混雑" in second.json()["detail"]
    finally:
        release.set()
        first.join(timeout=10)

    assert len(results) <= 1


def test_semaphore_is_released_after_failure(client, monkeypatch):
    """生成が失敗してもスロットは必ず解放される（デッドロック防止）"""
    import retro_radio.server as server_module

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server_module, "generate_radio_script", boom)
    for _ in range(3):
        client.post("/api/generate", json={"year": 1975, "month": 9, "day": 24})

    # 3回失敗した後もスロットが枯渇していない
    assert server_module._generation_slots._value == server_module.settings.max_concurrent_generations


def test_static_dir_is_outside_package():
    """静的ファイルは package 内ではなくプロジェクト直下の static/ を配信する"""
    from retro_radio.server import STATIC_DIR

    assert STATIC_DIR.name == "static"
    assert STATIC_DIR.exists()
    assert os.path.isdir(STATIC_DIR)
