import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Callable, TypeVar, Any

logger = logging.getLogger(__name__)

F = TypeVar('F', bound=Callable[..., Any])
_executor: ThreadPoolExecutor | None = None

# 設定クラスに項目が無いため定数で運用する（config.py 側で設定化できる）
EXECUTOR_MAX_WORKERS = 8

def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=EXECUTOR_MAX_WORKERS)
    return _executor

def shutdown_executor() -> None:
    global _executor
    if _executor:
        _executor.shutdown(wait=True)
        _executor = None

def run_in_executor(func: F, *args, **kwargs) -> asyncio.Future:
    """同期関数をスレッドプールで非同期実行"""
    loop = asyncio.get_running_loop()
    executor = get_executor()
    return loop.run_in_executor(executor, lambda: func(*args, **kwargs))

class AsyncProgress:
    """進捗通知のno-op実装（旧Streamlitの進捗バー描画のみを担当していた）"""
    def __init__(self):
        self._current = 0
        self._message = ""
    
    def update(self, percent: int, message: str = "") -> None:
        self._current = percent
        if message:
            self._message = message
        logger.debug(f"進捗更新: {percent}% {self._message}")
    
    def complete(self, message: str = "完了") -> None:
        self.update(100, f"✅ {message}")

def async_step(progress: AsyncProgress, percent: int, message: str):
    """デコレータ: 非同期ステップ実行・進捗更新"""
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            progress.update(percent, message)
            result = await run_in_executor(func, *args, **kwargs)
            return result
        return wrapper
    return decorator