import time
import pytest
from retro_radio.core.pipeline import generate_all_async

@pytest.mark.asyncio
async def test_generation_latency_within_limit():
    """メドレー込みでも生成時間が許容内か"""
    start = time.perf_counter()
    result = await generate_all_async(1980, 5, 15)
    elapsed = time.perf_counter() - start
    
    # ネットワーク含め30秒以内（従来25秒→マージン込み）
    assert elapsed < 30.0, f"生成時間超過: {elapsed:.1f}秒"
    assert len(result.all_songs) == 3
