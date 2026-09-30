import logging
from functools import wraps
from typing import Callable, Any, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar('F', bound=Callable[..., Any])

class AppError(Exception):
    """ユーザー向けメッセージを持つ共通エラー"""
    def __init__(self, user_message: str, technical_message: str = "", original: Exception | None = None):
        self.user_message = user_message
        self.technical_message = technical_message or str(original or "")
        self.original = original
        super().__init__(self.technical_message)

class ScriptGenerationError(AppError): pass
class MusicSearchError(AppError): pass
class TTSError(AppError): pass
class ValidationError(AppError): pass
class ConfigurationError(AppError): pass

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


