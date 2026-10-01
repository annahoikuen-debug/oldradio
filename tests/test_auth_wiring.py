
"""認証の配線とテナント分離の不変条件（提案⑧・S4）。

## このファイルが固定すること

1. **既定が安全側**: `RETRO_RADIO_REQUIRE_AUTH` の既定は `True`。
   `.env.example` にも記載があり、設定が個人利用のものだけで
   **なることを確認する**（`tests/test_env_templates.py` と同型の固定）。
2. **fail-closed**: 認証を有効にしたまま資格情報が無い状態は
   「認証が無い」と同じ危険なので、保護対象は 503 で拒否される。
3. **テナント間のキャッシュ交差 = 0**（提案⑧ の効果指標）。
4. **トークンの改ざんが検出される**。
5. **`MusicProfileRepository` の 2 つの契約**（S3 の Protocol 契約）。

## 依存しないテスト方針

`tests/conftest.py` の `_isolate_tts_cache` は `retro_radio.core` を import するため、
`core/` に問題があると**このファイルごと collection できない**。
そこで `conftest` の autouse fixture に依存せず、
このファイル内で必要なモジュールだけを自前で import する。

`RETRO_RADIO_REQUIRE_AUTH` を import 時に書き換えることは**しない**。
このファイルのテストは全て `Settings(...)` を明示的に組み立てるため
プロセスの環境変数に依存しないうえ、`server._auth_enforced()` は環境変数を
優先して読むため、import 時の書き込みは他モジュールとの実行順序依存になる
（`tests/test_hygiene_regression.py::TestNoImportTimeEnvMutation` が機械的に検査）。
"""

from __future__ import annotations

#: `tokens.MIN_SECRET_LENGTH`（32）を満たすテスト用の署名鍵。
#: 短い鍵はオフライン総当たりで Cookie を偽造できるため、テストでも正規の長さを使う。
_SECRET = "shared-test-secret-key-at-least-32-chars"
_SECRET_A = "issuer-test-secret-key-at-least-32-chars-long"
_SECRET_B = "verifier-test-secret-key-at-least-32-chars-lon"


from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from retro_radio.auth import tokens  # noqa: E402
from retro_radio.config import Settings, get_settings  # noqa: E402
from retro_radio.db.privacy_models import DEFAULT_TENANT_ID  # noqa: E402
from retro_radio.services.tenant_cache import (  # noqa: E402
    AUDIO_FILENAME_PATTERN,
    InvalidTenantIdError,
    TenantTtsCache,
    normalize_tenant_id,
)


# ---------------------------------------------------------------------------
# 1. 既定が安全側であること
# ---------------------------------------------------------------------------
def test_require_auth_default_is_safe():
    """**既定は 1（認証必須）**。個人利用で 0 にするのは意図的な選択。

    既定が 0 だと「施設にデプロイしたのに認証が無い」状態が
    何も言わずに成立してしまうため、安全側に倒す。
    """
    assert Settings.model_fields["require_auth"].default is True


def test_env_example_documents_require_auth_and_single_user_key():
    """.env.example に新規設定が載っていること（前回の失敗原因の再発防止）。"""
    env = (Path(__file__).resolve().parent.parent / ".env.example").read_text(
        encoding="utf-8"
    )
    for key in (
        "RETRO_RADIO_REQUIRE_AUTH",
        "RETRO_RADIO_SINGLE_USER_KEY",
        "RETRO_RADIO_ADMIN_EMAILS",
        "RETRO_RADIO_TERMS_VERSION",
        "RETRO_RADIO_REQUIRE_CONSENT",
        "RETRO_RADIO_DELETION_SLA_HOURS",
    ):
        assert f"{key}=" in env, f".env.example に {key} が無い"


def _uncommented_env_values(text: str) -> dict:
    """`.env.example` の**コメントでない** `KEY=VALUE` だけを dict にする。

    なぜこれが要るか: 以前は
    ``"RETRO_RADIO_REQUIRE_AUTH=1" in env or "...=true" in env``
    という**部分一致**で検査していたため、
    **コメントの中に 1 文字でもあれば通っていた**。
    実際の代入行が ``=0`` でも、
    上の説明コメントに ``RETRO_RADIO_REQUIRE_AUTH=1`` と書いてあれば緑になる。
    つまり**このテストは「出荷テンプレートが認証を無効にしている」ことを
    一度も検出できなかった**。
    """
    values: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("RETRO_RADIO_"):
            values[key] = value.strip()
    return values


def test_env_example_default_value_matches_config():
    """`.env.example` の**実際の代入行**が config の既定と一致すること。

    部分一致ではなく、コメントを除いた**代入行**を読む。
    """
    env = (Path(__file__).resolve().parent.parent / ".env.example").read_text(
        encoding="utf-8"
    )
    values = _uncommented_env_values(env)
    raw = values.get("RETRO_RADIO_REQUIRE_AUTH")
    assert raw is not None, "RETRO_RADIO_REQUIRE_AUTH の代入行が無い"

    normalized = raw.strip().lower()
    is_true = normalized in ("1", "true", "yes", "on")
    is_false = normalized in ("0", "false", "no", "off")
    assert is_true or is_false, (
        f"RETRO_RADIO_REQUIRE_AUTH の値が 1/0 でも true/false でもない: {raw!r}"
    )

    config_default = bool(Settings.model_fields["require_auth"].default)
    assert is_true == config_default, (
        f".env.example は {raw!r} だが、config の既定は {config_default}。"
        " 指示された最初の手順でセットアップしただけの構成が"
        "コード既定と食い違う状態になる"
    )


def test_env_example_does_not_disable_auth():
    """出荷テンプレートが**認証を無効にした状態で配布されない**こと

    コード既定は `require_auth=True`（安全側）で、`docs/privacy_and_tenancy.md`
    も `README.md` も 1 を指示している。テンプレートだけ 0 だと、
    **指示どおりにセットアップした運用が認証の無いサービスになる**。
    """
    env = (Path(__file__).resolve().parent.parent / ".env.example").read_text(
        encoding="utf-8"
    )
    values = _uncommented_env_values(env)
    raw = (values.get("RETRO_RADIO_REQUIRE_AUTH") or "").strip().lower()
    assert raw not in ("0", "false", "no", "off"), (
        f"出荷テンプレートが認証を無効にしている: RETRO_RADIO_REQUIRE_AUTH={raw!r}。"
        " 個人モードはコメントアウトされた例として示すこと"
    )


# ---------------------------------------------------------------------------
# 2. 認証モードの決定と fail-closed
# ---------------------------------------------------------------------------
def test_auth_mode_session_when_secret_key_present():
    assert Settings(require_auth=True, secret_key=_SECRET).require_auth_config() == "session"


def test_auth_mode_bearer_when_only_single_user_key_present():
    settings = Settings(
        require_auth=True, secret_key="", single_user_key=_SECRET
    )
    assert settings.require_auth_config() == "bearer"


def test_auth_mode_disabled_only_when_require_auth_is_off():
    """`require_auth=0` は「意図的に保護を切った」状態として明示的に存在させる。"""
    assert Settings(require_auth=False, secret_key="").require_auth_config() == "disabled"


def test_auth_mode_is_unavailable_when_require_auth_on_without_credentials():
    """認証を有効にしたまま資格情報が無い = fail-closed。"""
    settings = Settings(require_auth=True, secret_key="", single_user_key="")
    assert settings.require_auth_config() == "unavailable"
    assert settings.auth_ready is False


def test_auth_ready_is_true_with_either_credential():
    assert Settings(require_auth=True, secret_key=_SECRET).auth_ready is True
    assert (
        Settings(require_auth=True, secret_key="", single_user_key=_SECRET).auth_ready is True
    )


def test_auth_disabled_reflects_require_auth_flag():
    assert Settings(require_auth=False).auth_disabled is True
    assert Settings(require_auth=True, secret_key=_SECRET).auth_disabled is False


def test_resolve_mode_matches_config():
    assert tokens.resolve_mode(Settings(require_auth=False)) == "disabled"
    assert (
        tokens.resolve_mode(Settings(require_auth=True, secret_key=_SECRET)) == "session"
    )


# ---------------------------------------------------------------------------
# 3. トークンの署名と検証
# ---------------------------------------------------------------------------
def test_session_token_round_trip():
    secret = _SECRET
    token = tokens.issue_session_token(
        user_id="user-1", secret=secret, tenant_id="facility-a", role="admin"
    )
    payload = tokens.read_session_token(token, secret=secret)
    assert payload["uid"] == "user-1"
    assert payload["tid"] == "facility-a"
    assert payload["role"] == "admin"


def test_session_token_rejects_tampering():
    secret = _SECRET
    token = tokens.issue_session_token(user_id="user-1", secret=secret)
    body, _, signature = token.rpartition(".")
    tampered = f"{body}x.{signature}"
    with pytest.raises(tokens.TokenError):
        tokens.read_session_token(tampered, secret=secret)


def test_session_token_rejects_other_secret():
    token = tokens.issue_session_token(user_id="user-1", secret=_SECRET_A)
    with pytest.raises(tokens.TokenError):
        tokens.read_session_token(token, secret=_SECRET_B)


def test_session_token_expires():
    """TTL を超えると検証で弾かれる（1 シフトで失効する運用を前提）。"""
    secret = _SECRET
    now = int(datetime.now(timezone.utc).timestamp())
    token = tokens.issue_session_token(user_id="u", secret=secret, ttl_seconds=10)

    # 期限内なら通る
    assert tokens.read_session_token(token, secret=secret, now=now + 5)["uid"] == "u"
    # 期限切れなら弾かれる
    with pytest.raises(tokens.TokenError):
        tokens.read_session_token(token, secret=secret, now=now + 11)


def test_session_token_is_not_accepted_as_bearer_token():
    """**鍵分離**: セッションのトークンをベearer検証器に投げても通らない。

    同じ鍵だと「ベアラートークンを Cookie として注入する」攻撃が通ってしまう。
    """
    secret = _SECRET
    session = tokens.issue_session_token(user_id="u", secret=secret)
    with pytest.raises(tokens.TokenError):
        tokens.read_bearer_token(session, key=secret)


def test_bearer_token_round_trip():
    token = tokens.issue_bearer_token(key=_SECRET)
    assert tokens.read_bearer_token(token, key=_SECRET)["single_user"] is True


def test_bearer_token_without_key_raises_configuration_error():
    from retro_radio.config import ConfigurationError

    with pytest.raises(ConfigurationError):
        tokens.issue_bearer_token(key="")


# --- Authorization ヘッダーの解析 ----------------------------------------------
@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),          # RFC 7235: スキームは大文字小文字を区別しない
        ("BEARER abc", "abc"),
        ("Bearer   spaced  ", "spaced"),
        ("Bearer", None),                # トークンなし
        ("Bearer   ", None),
        ("", None),
        (None, None),
        ("Basic dXNlcjpwYXNz", None),     # 別スキーム
        ("Token abc", None),
    ],
)
def test_extract_bearer(header, expected):
    assert tokens.extract_bearer(header) == expected


# ---------------------------------------------------------------------------
# 4. authenticate_request の 4 モード
# ---------------------------------------------------------------------------
def test_authenticate_request_disabled_mode_returns_anonymous_default_tenant():
    principal, reason = tokens.authenticate_request(
        settings=Settings(require_auth=False, secret_key="", single_user_key="")
    )
    assert reason is None
    assert principal is not None
    assert principal.auth_mode == "disabled"
    assert principal.tenant_id == DEFAULT_TENANT_ID


def test_authenticate_request_fails_closed_without_credentials():
    principal, reason = tokens.authenticate_request(
        settings=Settings(require_auth=True, secret_key="", single_user_key="")
    )
    assert principal is None
    assert reason == "auth_not_configured"


def test_authenticate_request_missing_credentials_is_401_reason():
    principal, reason = tokens.authenticate_request(
        settings=Settings(require_auth=True, secret_key=_SECRET, single_user_key="")
    )
    assert principal is None
    assert reason == "missing_credentials"


def test_authenticate_request_accepts_valid_bearer():
    key = _SECRET
    token = tokens.issue_bearer_token(key=key)
    principal, reason = tokens.authenticate_request(
        authorization=f"Bearer {token}",
        settings=Settings(require_auth=True, secret_key="", single_user_key=key),
    )
    assert reason is None
    assert principal.auth_mode == "bearer"
    assert principal.authenticated is True


def test_authenticate_request_rejects_invalid_bearer():
    principal, reason = tokens.authenticate_request(
        authorization="Bearer wrong",
        settings=Settings(require_auth=True, secret_key="", single_user_key=_SECRET_B),
    )
    assert principal is None
    assert reason == "invalid_bearer"


def test_authenticate_request_rejects_invalid_session_cookie():
    principal, reason = tokens.authenticate_request(
        session_cookie="garbage",
        settings=Settings(require_auth=True, secret_key=_SECRET, single_user_key=""),
    )
    assert principal is None
    assert reason == "invalid_session"


def test_authenticate_request_accepts_valid_session_cookie():
    secret = _SECRET
    cookie = tokens.issue_session_token(
        user_id="u-9", secret=secret, tenant_id="facility-b", role="admin"
    )
    principal, reason = tokens.authenticate_request(
        session_cookie=cookie,
        settings=Settings(require_auth=True, secret_key=secret, single_user_key=""),
    )
    assert reason is None
    assert principal.user_id == "u-9"
    assert principal.tenant_id == "facility-b"
    assert principal.is_admin is True


# ---------------------------------------------------------------------------
# 5. テナント ID の安全性とキャッシュ交差 = 0
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["..", "../etc", "a/b", "a\\b", "a..b", "x" * 65, ".", "-"])
def test_normalize_tenant_id_rejects_unsafe(bad):
    with pytest.raises(InvalidTenantIdError):
        normalize_tenant_id(bad)


def test_normalize_tenant_id_empty_falls_back_to_default():
    """空は「テナント未指定」ではなく **個人モードの default** として扱う。"""
    assert normalize_tenant_id(None) == DEFAULT_TENANT_ID
    assert normalize_tenant_id("") == DEFAULT_TENANT_ID
    assert normalize_tenant_id("   ") == DEFAULT_TENANT_ID


def test_tenant_dirs_are_never_identical(tmp_path):
    """**テナント交差 = 0 の第 1 段**: ディレクトリが物理的に別であること。"""
    cache = TenantTtsCache(tmp_path)
    assert cache.tenant_dir("facility-a") != cache.tenant_dir("facility-b")
    assert cache.tenant_dir("facility-a") != cache.tenant_dir(DEFAULT_TENANT_ID)


def test_same_text_yields_separate_files_per_tenant(tmp_path):
    """**第 2 段**: 同じテキストでも別テナントは別ファイルになる。"""
    cache = TenantTtsCache(tmp_path)
    name = cache.filename_for("同じ原稿", "ja", "co.jp", False)

    a = cache.ensure_tenant_dir("facility-a") / name
    b = cache.ensure_tenant_dir("facility-b") / name
    a.write_bytes(b"ID3-A")
    b.write_bytes(b"ID3-B")

    assert a.read_bytes() != b.read_bytes()
    assert cache.files_of("facility-a") == {name}
    assert cache.files_of("facility-b") == {name}


def test_resolve_does_not_cross_tenants(tmp_path):
    """**第 3 段**: 他テナントのファイルを解決できないこと。"""
    cache = TenantTtsCache(tmp_path)
    cache.ensure_tenant_dir("facility-a")
    name = cache.filename_for("原稿", "ja", "co.jp", False)
    (cache.tenant_dir("facility-a") / name).write_bytes(b"ID3-AAAAAAAA")

    assert cache.resolve("facility-a", name) is not None
    assert cache.resolve("facility-b", name) is None


def test_resolve_rejects_path_traversal_and_bad_names(tmp_path):
    cache = TenantTtsCache(tmp_path)
    cache.ensure_tenant_dir("facility-a")
    for bad in ("../../secret.mp3", "not_mp3.txt", "a" * 200 + ".mp3"):
        assert cache.resolve("facility-a", bad) is None
    # テナント ID 側viii directories脱出不許可
    assert cache.resolve("../facility-a", "tts_x.mp3") is None


def test_sweep_of_one_tenant_leaves_other_tenant_untouched(tmp_path):
    """**第 4 段**: テナント単位の sweeper が他テナントを消さないこと。"""
    cache = TenantTtsCache(tmp_path, ttl_days=1)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    name = cache.filename_for("原稿", "ja", "co.jp", False)

    for tenant in ("facility-a", "facility-b"):
        directory = cache.ensure_tenant_dir(tenant)
        target = directory / name
        target.write_bytes(b"ID3-old")
        import os as _os

        _os.utime(target, (old, old))

    removed = cache.sweep(tenant_id="facility-a", force=True)
    assert removed == 1
    assert cache.files_of("facility-a") == set()
    assert cache.files_of("facility-b") == {name}


def test_sweep_all_covers_every_tenant(tmp_path):
    cache = TenantTtsCache(tmp_path / "root", ttl_days=1)
    name = cache.filename_for("原稿", "ja", "co.jp", False)
    for tenant in ("a", "b", "c"):
        (cache.ensure_tenant_dir(tenant) / name).write_bytes(b"ID3")

    assert cache.sweep_all(force=True) == 0  # TTL 内なので消さない
    assert cache.list_tenants() == ["a", "b", "c"]


def test_list_tenants_ignores_directories_without_cache_files(tmp_path):
    """キャッシュファイルが無いディレクトリは「テナント」として列挙しない。

    ルート直下に別の用途（`audio_cache` など）のディレクトリがあっても
    テナントとして数えない。保証する。
    """
    root = tmp_path / "root"
    cache = TenantTtsCache(root)
    (root / "audio_cache").mkdir(parents=True)
    (root / "audio_cache" / "notes.txt").write_text("x", encoding="utf-8")
    # 拡張子は .mp3 でも名前が tts_ で始まらなければキャッシュではない
    (root / "audio_cache" / "decoy.mp3.txt").write_text("x", encoding="utf-8")
    name = cache.filename_for("原稿", "ja", "co.jp", False)
    (cache.ensure_tenant_dir("real") / name).write_bytes(b"ID3")

    assert cache.list_tenants() == ["real"]


def test_sweep_interval_throttles(tmp_path):
    cache = TenantTtsCache(tmp_path, ttl_days=1, sweep_interval=3)
    name = cache.filename_for("原稿", "ja", "co.jp", False)
    target = cache.ensure_tenant_dir("a") / name
    target.write_bytes(b"ID3")
    import os as _os

    old = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
    _os.utime(target, (old, old))

    assert cache.sweep(tenant_id="a") == 0  # 1 回目: 間隔カウンタ
    assert cache.sweep(tenant_id="a") == 0  # 2 回目: まだ
    assert cache.sweep(tenant_id="a") == 1  # 3 回目: 実行


def test_purge_tenant_only_touches_that_tenant(tmp_path):
    cache = TenantTtsCache(tmp_path)
    name = cache.filename_for("原稿", "ja", "co.jp", False)
    (cache.ensure_tenant_dir("a") / name).write_bytes(b"ID3")
    (cache.ensure_tenant_dir("b") / name).write_bytes(b"ID3")

    assert cache.purge_tenant("a") == 1
    assert cache.files_of("a") == set()
    assert cache.files_of("b") == {name}


def test_relative_url_carries_tenant_id(tmp_path):
    """配信 URL にテナント ID が入る = セッション照合の材料になる。"""
    cache = TenantTtsCache(tmp_path)
    name = cache.filename_for("原稿", "ja", "co.jp", False)
    url = cache.relative_url_for("facility-a", name)
    assert url.startswith("/api/audio/facility-a/")
    assert url.endswith(name)


def test_audio_filename_pattern_matches_server_rule():
    """`server.py` の `AUDIO_FILENAME_PATTERN` と同じ制約であること。"""
    import re

    from retro_radio import server

    assert AUDIO_FILENAME_PATTERN.pattern == server.AUDIO_FILENAME_PATTERN.pattern
    assert re.match(AUDIO_FILENAME_PATTERN, "tts_abc.mp3")
    assert not re.match(AUDIO_FILENAME_PATTERN, "../x.mp3")


# ---------------------------------------------------------------------------
# 6. config の検証（TDD ではなく retroactive な固定）
# ---------------------------------------------------------------------------
def test_retry_wait_min_greater_than_max_is_rejected():
    """`min > max` は tenacity が `max` を黙って無視するため**起動時に落とす**。"""
    with pytest.raises(Exception):
        Settings(retry_wait_min=30, retry_wait_max=5)


def test_retry_wait_min_equal_to_max_is_accepted():
    settings = Settings(retry_wait_min=5, retry_wait_max=5)
    assert settings.retry_wait_min == settings.retry_wait_max


def test_admin_emails_accepts_csv_and_json():
    assert Settings(admin_emails="a@example.com,b@example.com").admin_emails == [
        "a@example.com",
        "b@example.com",
    ]
    assert Settings(admin_emails='["A@Example.com"]').admin_emails == ["a@example.com"]
    assert Settings(admin_emails="").admin_emails == []


def test_get_settings_cache_can_be_cleared():
    """テストが設定を差し替えられること（他テストへの汚染防止）。"""
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
