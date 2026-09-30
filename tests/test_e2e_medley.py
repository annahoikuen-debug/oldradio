"""メドレー生成のエンドツーエンド検証（外部依存は conftest でモック済み）。

旧テストは `generate_all_async` を無防備に呼び、iTunes と gTTS に
実ネットワークへアクセスさせていたため CI で不安定になっていた。
"""

from unittest.mock import patch

import pytest

from retro_radio.core.pipeline import generate_all_async, generate_all_parallel


SCRIPT = "### オープニング\n" + ("ヒット曲を3曲お届けします。" * 80) + "\n### エンディング\nおわり。"
SONGS = [
    {"trackName": f"曲{i}", "artistName": f"歌手{i}", "previewUrl": f"http://x/{i}.mp3"}
    for i in range(3)
]


async def test_full_medley_generation():
    """年指定→原稿・3曲選択・音声生成の全フロー"""
    with patch("retro_radio.core.pipeline.generate_radio_script", return_value=SCRIPT), patch(
        "retro_radio.core.pipeline.search_itunes_songs", return_value=SONGS
    ), patch("retro_radio.core.pipeline.text_to_speech", return_value="/tmp/audio.mp3"):
        result = await generate_all_async(1980, 5, 15)

    assert len(result.script) >= 800
    assert "ヒット曲を3曲" in result.script or "ヒット曲を 3 曲" in result.script
    assert len(result.all_songs) == 3
    for song in result.all_songs:
        assert "trackName" in song
        assert "artistName" in song
    assert result.audio_path is None or isinstance(result.audio_path, str)


async def test_medley_without_itunes_hits_uses_fallback_songs():
    """iTunes ヒット0件でも 3 曲に満たす（捏造レコードではなく静的マスター）"""
    with patch("retro_radio.core.pipeline.generate_radio_script", return_value=SCRIPT), patch(
        "retro_radio.core.pipeline.search_itunes_songs", return_value=[]
    ), patch("retro_radio.core.pipeline.text_to_speech", return_value=None):
        result = await generate_all_async(1980, 5, 15)

    assert len(result.all_songs) == 3
    assert all(song["previewUrl"] is None for song in result.all_songs)
    assert result.use_fallback_song is True


async def test_parallel_variant_matches_async_contract():
    """並列版も同じ GenerationResult 契約を返す"""
    with patch("retro_radio.core.pipeline.generate_radio_script", return_value=SCRIPT), patch(
        "retro_radio.core.pipeline.search_itunes_songs", return_value=SONGS
    ), patch("retro_radio.core.pipeline.text_to_speech", return_value="/tmp/audio.mp3"):
        result = await generate_all_parallel(1980, 5, 15)

    assert result.script == SCRIPT
    assert result.audio_path == "/tmp/audio.mp3"
    assert len(result.all_songs) == 3
    assert result.errors == []


@pytest.mark.network
async def test_real_pipeline_is_opt_in():
    """実ネットワーク版は `network` マーカー付き（既定では skip される）"""
    result = await generate_all_async(1980, 5, 15)
    assert result.all_songs
