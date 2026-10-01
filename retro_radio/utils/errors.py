import logging
from functools import wraps
from typing import TYPE_CHECKING, Callable, Any, TypeVar

from .app_errors import (
    AppError,
    ScriptGenerationError,
    MusicSearchError,
    TTSError,
    ValidationError,
)

if TYPE_CHECKING:  # pragma: no cover - 静的解析向けの宣言のみ
    # 実行時にはこの import は走らない（循環回避のため `__getattr__` で解決する）。
    # flake8 は `__all__` の名前を実行時定義として検査するため、TYPE_CHECKING 里有し込む。
    from ..config import ConfigurationError

logger = logging.getLogger(__name__)
F = TypeVar('F', bound=Callable[..., Any])

# `AppError` とそのサブクラスは `utils.app_errors`（循環しない葉モジュール）に置き、
# `ConfigurationError` の正本は `retro_radio.config` にある。
# 以前は両方が `utils.errors` に定義されていたため、
# `config.require_secret_key()` が投げる設定エラーが
# `server.app_error_handler` の `isinstance(exc, ConfigurationError)` を素通りし、
# 設定不備（本来 500）がクライアントに 400 として見えていた。
#
# `ConfigurationError` は**遅延解決**する（PEP 562 のモジュール `__getattr__`）。
# `config` は `AppError` をこの階層から取りたいので `utils.errors` を通るため、
# モジュール読み込み時に `from ..config import ConfigurationError` すると
# `config` → `utils.errors` → `config` の循環になる（実測: ImportError）。
def __getattr__(name: str):
    if name == "ConfigurationError":
        from ..config import ConfigurationError

        return ConfigurationError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AppError",
    "ScriptGenerationError",
    "MusicSearchError",
    "TTSError",
    "ValidationError",
    "ConfigurationError",
    "handle_error",
    "with_error_handling",
    "with_retry_async",
]

def handle_error(error: Exception, context: str = "") -> None:
    """統一エラー処理・ログ記録（Streamlit廃止のためUI出力は行わない）"""
    prefix = f"[{context}] " if context else ""
    if isinstance(error, AppError):
        logger.warning(f"{prefix}{error.user_message} ({error.technical_message})")
    else:
        logger.exception(f"{prefix}予期しないエラーが発生しました。時間をおいて再試行してください。: {error}")

def with_error_handling(context: str, fallback_return=None):
    """関数をラップしてエラー統一処理"""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except AppError:
                raise  # 再スロー（上位で処理）
            except Exception as e:
                handle_error(e, context)
                return fallback_return
        return wrapper
    return decorator

def with_retry_async(max_attempts: int = 3, min_wait: float = 2.0, max_wait: float = 10.0):
    """非同期関数用リトライデコレータ（Step 8で使用）"""
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, IOError)),
        reraise=True
    )
