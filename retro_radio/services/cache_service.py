import functools
import threading
import time
from typing import Any, Callable, TypeVar
from ..config import get_settings

F = TypeVar('F', bound=Callable[..., Any])
settings = get_settings()

MAX_CACHE_ENTRIES = 256

_cached_functions = []
_cache_lock = threading.Lock()

def cached(
    ttl: int | None = None,
    key_prefix: str = "",
    include_tenant: bool = False,
):
    """キャッシュデコレータ（設定駆動・プロセス内TTLキャッシュ）。

    `include_tenant=True` の場合、関数の `tenant_id` 引数をキャッシュキーに
    含め、テナント間でキャッシュが漏洩するのを防ぐ（P2-13）。
    """
    cache_ttl = ttl if ttl is not None else settings.cache_ttl

    def decorator(func: F) -> F:
        memo = {}
        memo_lock = threading.Lock()

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                key_parts = [key_prefix]
                if include_tenant:
                    tenant_id = kwargs.get("tenant_id")
                    if tenant_id is None:
                        # テナントIDが見つからない場合は位置引数から探す
                        import inspect

                        sig = inspect.signature(func)
                        bound = sig.bind_partial(*args, **kwargs)
                        tenant_id = bound.arguments.get("tenant_id")
                    key_parts.append(f"tenant:{tenant_id}")
                key_parts.extend([args, tuple(sorted(kwargs.items()))])
                key = tuple(key_parts)
                hash(key)
            except TypeError:
                return func(*args, **kwargs)
            now = time.monotonic()
            with memo_lock:
                hit = memo.get(key)
                if hit is not None and now - hit[0] < cache_ttl:
                    return hit[1]
            value = func(*args, **kwargs)
            with memo_lock:
                if key not in memo and len(memo) >= MAX_CACHE_ENTRIES:
                    memo.pop(next(iter(memo)), None)
                memo[key] = (now, value)
            return value

        def cache_clear() -> None:
            with memo_lock:
                memo.clear()

        wrapper.cache_clear = cache_clear
        with _cache_lock:
            _cached_functions.append(wrapper)
        return wrapper

    return decorator

def clear_all_cache() -> None:
    """プロセス内 TTL キャッシュとセッション内オーディオキャッシュを消す。

    旧実装は `get_audio_cache().clear()` しか呼んでいなかったが、
    `get_audio_cache()` は**コピー**を返すため実体は空にならない（clear が no-op）。
    実体を消す `clear_audio_cache()` を通して消す。
    旧来の呼び出し契約（`get_audio_cache` を差し替えるテスト）も残すため、
    `get_audio_cache().clear()` も続けて呼ぶ。
    """
    for wrapper in list(_cached_functions):
        wrapper.cache_clear()
    from ..utils.session import get_audio_cache, clear_audio_cache
    get_audio_cache().clear()
    clear_audio_cache()
