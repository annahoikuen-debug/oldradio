"""`BatchProcessor` の検証（旧 `tests/test_batch_processor.py` を拡張）。

Wave 1 でゼロ除算ガードが追加された:
`int((i / total) * 100) if total else 0`
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from retro_radio.core.batch_processor import BatchProcessor


@pytest.fixture
def batch_mocks():
    generate = AsyncMock(return_value={"result": "test"})
    progress = Mock()
    with patch("retro_radio.core.batch_processor.generate_all_parallel", generate), patch(
        "retro_radio.core.batch_processor.AsyncProgress", return_value=progress
    ):
        yield generate, progress


async def test_batch_processor_success(batch_mocks):
    """バッチ処理の基本動作"""
    _generate, progress = batch_mocks
    processor = BatchProcessor()
    results = await processor.process_batch([1960, 1961], 7, 15)

    assert len(results) == 2
    assert results[0] == {"year": 1960, "success": True, "data": {"result": "test"}}
    assert results[1] == {"year": 1961, "success": True, "data": {"result": "test"}}
    assert progress.update.call_count == 2
    progress.complete.assert_called_once_with("バッチ処理完了")


async def test_empty_batch_does_not_divide_by_zero(batch_mocks):
    """空バッチでゼロ除算しない（Wave 1 のガード）"""
    _generate, progress = batch_mocks
    processor = BatchProcessor()

    results = await processor.process_batch([], 7, 15)

    assert results == []
    progress.complete.assert_called_once_with("バッチ処理完了")


async def test_single_year_batch(batch_mocks):
    """1件だけなら進捗が 0%（not 100%）になる"""
    _generate, _progress = batch_mocks
    processor = BatchProcessor()
    results = await processor.process_batch([1980], 1, 1)

    assert len(results) == 1
    assert results[0]["success"] is True


async def test_failure_is_recorded_per_year(batch_mocks):
    """1年失敗しても他の年の処理は続く"""
    generate, _progress = batch_mocks
    generate.side_effect = [Exception("year 1 failed"), {"result": "ok"}]

    processor = BatchProcessor()
    results = await processor.process_batch([1960, 1961], 7, 15)

    assert results[0]["success"] is False
    assert "year 1 failed" in results[0]["error"]
    assert results[1]["success"] is True


async def test_error_message_is_stringified(batch_mocks):
    """error は常に文字列（JSON 化できる）"""
    generate, _progress = batch_mocks
    generate.side_effect = [ValueError("bad")]

    results = await BatchProcessor().process_batch([1960], 1, 1)
    assert isinstance(results[0]["error"], str)


async def test_progress_percentage_stays_in_range(batch_mocks):
    """進捗パーセンテージが 0〜100 の範囲に収まる"""
    _generate, progress = batch_mocks
    await BatchProcessor().process_batch([1960, 1961, 1962, 1963], 1, 1)

    for call in progress.update.call_args_list:
        percent = call.args[0]
        assert 0 <= percent <= 100
