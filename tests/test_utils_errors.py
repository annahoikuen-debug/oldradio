"""`retro_radio.utils.errors` の検証。

Streamlit 廃止に合わせ、UI 出力（st.warning / st.error）への依存は撤去されている。
handle_error はログのみを出力する。
"""

import logging

import pytest
from unittest.mock import Mock, patch

from retro_radio.utils.errors import (
    AppError,
    ScriptGenerationError,
    MusicSearchError,
    TTSError,
    ValidationError,
    ConfigurationError,
    handle_error,
    with_error_handling,
    with_retry_async,
)


def test_app_error_instantiation():
    """AppError の Motley 化テスト"""
    exc = AppError(
        user_message="原稿を生成できませんでした",
        technical_message="技術的な詳細情報",
    )
    assert exc.user_message == "原稿を生成できませんでした"
    assert exc.technical_message == "技術的な詳細情報"
    assert str(exc) == "技術的な詳細情報"


def test_app_error_keeps_original_exception():
    """original を保持し、user_message に例外文字列を混ぜない"""
    original = ValueError("boom")
    exc = AppError("安全なメッセージ", original=original)
    assert exc.original is original
    assert exc.technical_message == "boom"
    assert exc.user_message == "安全なメッセージ"


def test_app_error_subclasses():
    """AppError のサブクラス定義テスト"""
    for cls in (
        ScriptGenerationError,
        MusicSearchError,
        TTSError,
        ValidationError,
        ConfigurationError,
    ):
        assert issubclass(cls, AppError)


def test_handle_error_logs_user_and_technical_messages():
    """AppError は user_message と technical_message の両方をログに出す"""
    logger = Mock()
    with patch("retro_radio.utils.errors.logger", logger):
        handle_error(AppError("user msg", "tech msg"), "test_context")

    logger.warning.assert_called_once_with("[test_context] user msg (tech msg)")


def test_handle_error_logs_unexpected_exception():
    """AppError 以外は logger.exception で記録する"""
    logger = Mock()
    with patch("retro_radio.utils.errors.logger", logger):
        handle_error(ValueError("unexpected"), "test_context")

    logger.exception.assert_called_once()
    assert "unexpected" in logger.exception.call_args[0][0]


def test_handle_error_without_context_has_no_prefix():
    """context 空ならプレフィックスを付けない"""
    logger = Mock()
    with patch("retro_radio.utils.errors.logger", logger):
        handle_error(ValueError("unexpected"))

    assert not logger.exception.call_args[0][0].startswith("[")


def test_handle_error_does_not_raise():
    """handle_error は例外を投げない（UI 出力は廃止済み）"""
    handle_error(RuntimeError("boom"))


def test_with_error_handling_decorator_returns_fallback():
    """with_error_handling は例外を握りつぶして fallback を返す"""
    logger = Mock()
    with patch("retro_radio.utils.errors.logger", logger):

        @with_error_handling(context="test", fallback_return="fallback")
        def function_that_raises():
            raise ValueError("test error")

        assert function_that_raises() == "fallback"
        logger.exception.assert_called_once()


def test_with_error_handling_reraises_apperror():
    """AppError は握り潰さず呼び出し元へ再送出する"""
    logger = Mock()
    with patch("retro_radio.utils.errors.logger", logger):

        @with_error_handling(context="test", fallback_return="fallback")
        def function_that_raises():
            raise ScriptGenerationError("原稿生成に失敗しました")

        with pytest.raises(ScriptGenerationError):
            function_that_raises()

    logger.exception.assert_not_called()


def test_with_error_handling_passes_through_success():
    """正常系はそのまま戻り値を返す"""

    @with_error_handling(context="test")
    def function_that_returns():
        return "success"

    assert function_that_returns() == "success"


def test_with_retry_async_retries_transient_errors():
    """with_retry_async は ConnectionError 系のみリトライする"""
    retry_decorator = with_retry_async(max_attempts=3, min_wait=0.001, max_wait=0.002)
    assert callable(retry_decorator)

    call_count = 0

    @retry_decorator
    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert flaky() == "ok"
    assert call_count == 3


def test_with_retry_async_reraises_after_max_attempts():
    """最大試行回数を使い切ったら最後の例外を再送出する"""
    retry_decorator = with_retry_async(max_attempts=3, min_wait=0.001, max_wait=0.002)
    call_count = 0

    @retry_decorator
    def always_fails():
        nonlocal call_count
        call_count += 1
        raise ConnectionError("always fails")

    with pytest.raises(ConnectionError):
        always_fails()
    assert call_count == 3


def test_with_retry_async_does_not_retry_unrelated_errors():
    """リトライ対象外の例外は1回で素通りする"""
    retry_decorator = with_retry_async(max_attempts=3, min_wait=0.001, max_wait=0.002)
    call_count = 0

    @retry_decorator
    def raises_value_error():
        nonlocal call_count
        call_count += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        raises_value_error()
    assert call_count == 1


def test_handle_error_writes_to_real_logger():
    """ロガーが例外っても呼び出し側が落ちることはない（安全-net 検証）"""
    logger = logging.getLogger("retro_radio.tests.errors")
    assert logger is not None
    handle_error(AppError("user msg"))


# ---------------------------------------------------------------------------
# `ConfigurationError` の 1 クラス化
# ---------------------------------------------------------------------------
#
# 以前は `retro_radio.utils.errors.ConfigurationError`（`AppError` の子）と
# `retro_radio.config.ConfigurationError`（`RuntimeError` の子）が
# **別々に定義**されていた。
# `server.app_error_handler` は `utils.errors` 側を import して
# `isinstance(exc, ConfigurationError)` で 500 に振り分けるため、
# 実際の認証設定エラー（`config.require_secret_key()` が投げる `config` 側）が
# **この判定を素通り**し、設定不備（本来 500）がクライアントに 400 として見えていた。


def test_there_is_exactly_one_configuration_error_class():
    """`ConfigurationError` が二重定義されていないこと。"""
    from retro_radio import config, utils

    assert utils.ConfigurationError is config.ConfigurationError, (
        "ConfigurationError が二重定義されています。"
        "server.app_error_handler の isinstance 判定を素通りするため、"
        "どちらかに統一してください。"
    )


def test_configuration_error_is_recognised_by_the_error_handler():
    """`config.ConfigurationError` が `AppError` の判定に含まれること。"""
    from retro_radio.config import ConfigurationError as ConfigConfigurationError

    assert issubclass(ConfigConfigurationError, AppError), (
        "config.ConfigurationError が AppError を継承していないため、"
        "app_error_handler が 500 に振り分けられません"
    )


def test_configuration_error_carries_a_user_message():
    """AppError 化したので、利用者向けメッセージを持てる。"""
    from retro_radio.config import ConfigurationError as ConfigConfigurationError

    exc = ConfigConfigurationError("RETRO_RADIO_SECRET_KEY が未設定です")
    assert isinstance(exc, AppError)
    assert "RETRO_RADIO_SECRET_KEY" in exc.user_message
