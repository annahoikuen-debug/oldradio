"""`retro_radio.utils.async_runner` の検証。

旧 `retro_radio/tests/test_async_runner.py` から統合。
Wave 1 で `get_event_loop` → `get_running_loop`、`AsyncProgress` の no-op 化、
`max_workers` 3→8 beekman変更。旧 `get_event_loop` 経路の復活を防止する。
"""

import asyncio
import threading

import pytest

from retro_radio.utils.async_runner import (
    EXECUTOR_MAX_WORKERS,
    AsyncProgress,
    async_step,
    get_executor,
    run_in_executor,
    shutdown_executor,
)


async def test_run_in_executor_returns_result():
    def sync_add(a, b):
        return a + b

    assert await run_in_executor(sync_add, 2, 3) == 5


async def test_run_in_executor_propagates_exception():
    def sync_raise():
        raise ValueError("test")

    with pytest.raises(ValueError, match="test"):
        await run_in_executor(sync_raise)


async def test_run_in_executor_uses_running_loop():
    """`asyncio.get_event_loop` ではなく `get_running_loop` を使う（3.10+ で deprecated）"""
    import inspect

    source = inspect.getsource(run_in_executor)
    assert "get_running_loop" in source
    assert "get_event_loop" not in source


async def test_shutdown_executor_is_idempotent():
    """2回呼んでも例外にならない（lifespan の finally とテストの teardown で二重に呼ばれうる）"""
    shutdown_executor()
    shutdown_executor()


def test_executor_worker_count():
    assert EXECUTOR_MAX_WORKERS == 8


def test_get_executor_is_singleton():
    assert get_executor() is get_executor()


async def test_run_in_executor_uses_threadpool():
    main_thread = threading.get_ident()
    worker_thread = await run_in_executor(threading.get_ident)
    assert worker_thread != main_thread


def test_async_progress_is_noop():
    """進捗通知は no-op（Streamlit の進捗バー描画は廃止済み）"""
    progress = AsyncProgress()
    progress.update(50, " halfway")
    progress.complete("done")
    assert progress._current == 100
    assert "done" in progress._message


def test_async_progress_update_keeps_last_message():
    progress = AsyncProgress()
    progress.update(10, "first")
    progress.update(20)
    assert progress._current == 20
    assert progress._message == "first"


async def test_async_step_updates_progress_and_runs():
    progress = AsyncProgress()
    seen = []

    @async_step(progress, 42, "processing")
    def work(a, b):
        seen.append((a, b))
        return a + b

    assert await work(1, 2) == 3
    assert seen == [(1, 2)]
    assert progress._current == 42
    assert progress._message == "processing"


async def test_concurrent_executor_calls():
    results = await asyncio.gather(*[run_in_executor(lambda i=i: i * 2) for i in range(10)])
    assert results == [i * 2 for i in range(10)]
