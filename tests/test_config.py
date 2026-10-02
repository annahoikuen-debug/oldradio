"""`retro_radio.config.Settings` の検証。

旧 `retro_radio/tests/test_config.py`（既定値と環境変数上書きの3本）を統合し、
Wave 1 の変更（gemini_model 既定変更、retry_multiplier、extra="ignore"、
secret_key 警告、新規4フィールド）を含めて検証する。
"""

import pytest

from retro_radio.config import (
    MIN_SECRET_KEY_LENGTH,
    MIN_SINGLE_USER_KEY_LENGTH,
    ConfigurationError,
    Settings,
    get_settings,
)


def test_default_cache_and_year_range():
    s = Settings()
    assert s.cache_ttl == 3600
    assert s.min_year == 1950
    assert s.max_year == 2025


def test_default_year_is_consistent():
    settings = Settings()
    assert settings.min_year <= settings.default_year <= settings.max_year


def test_env_override(monkeypatch):
    monkeypatch.setenv("RETRO_RADIO_CACHE_TTL", "7200")
    assert Settings().cache_ttl == 7200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cache_ttl": 30},          # ge=60 違反
        {"default_year": 1900},     # range 違反
        {"medley_song_count": 99},  # le=10 違反
        {"retry_multiplier": 9},    # le=5 違反
        {"max_concurrent_generations": 0},   # ge=1 違反
        {"generation_wait_timeout": 9999},   # le=300 違反
        {"tts_cache_ttl_days": 0},  # ge=1 違反
    ],
)
def test_validation_errors(kwargs):
    with pytest.raises(Exception):
        Settings(**kwargs)


def test_year_range_consistency_is_enforced():
    with pytest.raises(Exception):
        Settings(min_year=2000, max_year=1990)


def test_default_year_must_be_inside_year_range():
    """default_year が範囲外でも起動しない"""
    with pytest.raises(Exception):
        Settings(min_year=1960, max_year=1970, default_year=1980)


def test_year_range_accepts_valid_bounds():
    settings = Settings(min_year=1955, max_year=1995, default_year=1975)
    assert (settings.min_year, settings.max_year) == (1955, 1995)


def test_year_range_out_of_absolute_bounds_is_rejected():
    """1950〜2025 の外側は範囲制約で弾かれる"""
    for kwargs in ({"min_year": 1949}, {"max_year": 2026}):
        with pytest.raises(Exception):
            Settings(**kwargs)


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_gemini_model_default_is_current():
    """既定モデルが実在する contemporary なモデルであること"""
    model = Settings().gemini_model
    assert model
    assert not model.startswith("gemini-1.0")
    assert "gemini-1.5-flash" not in model, "退役済みモデルが既定になっている"


def test_retry_multiplier_default():
    """指数バックオフが意味のある速さで効くよう 2 以上"""
    assert Settings().retry_multiplier >= 2


def test_extra_env_is_ignored(monkeypatch):
    """未知の RETRO_RADIO_* は無視される（extra='forbid' を廃止）"""
    monkeypatch.setenv("RETRO_RADIO_SOME_UNKNOWN_FIELD", "whatever")
    assert Settings() is not None


def test_secret_key_warns_when_missing():
    with pytest.warns(RuntimeWarning, match="RETRO_RADIO_SECRET_KEY"):
        Settings(secret_key="")


def test_secret_key_does_not_warn_when_set():
    """**十分な長さの** `secret_key` では資格情報の警告が出ない。"""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        long_key = "k" * MIN_SECRET_KEY_LENGTH
        assert Settings(secret_key=long_key).secret_key == long_key


def test_short_secret_key_warns():
    """3 文字の `secret_key` は HMAC 鍵として総当たり可能なため警告する。

    以前は `secret_key` が「設定されているか」しか見ていなかったため、
    `SECRET_KEY=set` で警告 0 のまま起動していた。
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        Settings(secret_key="set")
    assert any("SECRET_KEY" in str(item.message) for item in caught)


def test_short_single_user_key_warns():
    """`single_user_key` にも同じ床が要る（P0-8）。

    個人モードの資格情報は 1 つしかなく、当たれば 8 時間有効な
    署名済みセッション Cookie に化ける。`secret_key` だけ見ていた時期には
    `SINGLE_USER_KEY=x` で警告が 0 だった。
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        Settings(single_user_key="x")
    assert any("SINGLE_USER_KEY" in str(item.message) for item in caught)


def test_short_single_user_key_is_not_a_usable_credential():
    """短い `single_user_key` は `bearer` モードを**成立させない**（fail-closed）。"""
    s = Settings(require_auth=True, secret_key="", single_user_key="x")
    assert s.require_auth_config() == "unavailable"
    assert s.auth_ready is False
    with pytest.raises(ConfigurationError):
        s.require_single_user_key()


def test_long_single_user_key_is_usable():
    long_key = "u" * MIN_SINGLE_USER_KEY_LENGTH
    s = Settings(require_auth=True, secret_key="", single_user_key=long_key)
    assert s.require_auth_config() == "bearer"
    assert s.auth_ready is True
    assert s.require_single_user_key() == long_key


def test_new_resource_protection_fields_exist():
    """Wave 1 で追加された4フィールドの既定値"""
    settings = Settings()
    assert settings.tts_cache_ttl_days == 7
    assert settings.tts_cache_sweep_interval == 50
    assert settings.max_concurrent_generations == 2
    assert settings.generation_wait_timeout == 30


def test_cors_defaults():
    settings = Settings()
    assert settings.cors_origins == [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ]
    assert settings.cors_allow_credentials is False


def test_tts_defaults():
    settings = Settings()
    assert settings.tts_language == "ja"
    assert settings.tts_tld == "co.jp"
    assert settings.tts_slow is False


def test_medley_defaults():
    settings = Settings()
    assert settings.medley_song_count == 3
    assert settings.target_script_chars == 1000
    assert settings.script_char_tolerance == 200


def test_itunes_timeouts_are_positive():
    settings = Settings()
    assert settings.itunes_timeout_connect >= 1
    assert settings.itunes_timeout_read >= 1
    assert 1 <= settings.itunes_limit <= 200


def test_retro_radio_prefix_is_required(monkeypatch):
    """接頭辞なしの環境変数は無視される（DATABASE_URL だけでは効かない）"""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///should-be-ignored.db")
    assert Settings().database_url != "sqlite:///should-be-ignored.db"
