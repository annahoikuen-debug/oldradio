"""`retro_radio.core.pipeline` の検証。

旧 `retro_radio/tests/test_pipeline.py` を統合。
同ファイルは `retro_radio.core.script_generator.generate_radio_script` を patch して
いたが、pipeline は import 時に `from .script_generator import generate_radio_script`
と束縛しているためパッチが効かず、実ネットワークへアクセスしていた。
ここでは `retro_radio.core.pipeline.<name>` を patch する。
"""

from unittest.mock import patch

import pytest

from retro_radio.core.pipeline import GenerationResult, generate_all_async, generate_all_parallel


SCRIPT = "### オープニング\n原稿"
SONGS = [{"trackName": "テスト曲", "artistName": "テストアーティスト", "previewUrl": "http://example.com/t.mp3"}]


def _patched(script=None, songs=None, tts=None, script_error=None, songs_error=None, tts_error=None):
    stack = []
    script_mock = patch("retro_radio.core.pipeline.generate_radio_script")
    songs_mock = patch("retro_radio.core.pipeline.search_itunes_songs")
    tts_mock = patch("retro_radio.core.pipeline.text_to_speech")

    s = script_mock.start()
    stack.append(script_mock)
    m = songs_mock.start()
    stack.append(songs_mock)
    t = tts_mock.start()
    stack.append(tts_mock)

    if script_error is not None:
        s.side_effect = script_error
    else:
        s.return_value = script if script is not None else SCRIPT
    if songs_error is not None:
        m.side_effect = songs_error
    else:
        m.return_value = songs if songs is not None else list(SONGS)
    if tts_error is not None:
        t.side_effect = tts_error
    else:
        t.return_value = tts if tts is not None else "/tmp/test.mp3"

    return stack


@pytest.fixture
def pipeline_mocks():
    started = []

    def _apply(**kwargs):
        started.extend(_patched(**kwargs))
        return started

    yield _apply
    for mock in started:
        mock.stop()


# --- 正常系 --------------------------------------------------------------------
async def test_generate_all_async_success(pipeline_mocks):
    pipeline_mocks()
    result = await generate_all_async(2020, 5, 15)

    assert isinstance(result, GenerationResult)
    assert result.script == SCRIPT
    assert result.song_title == "テスト曲"
    assert result.artist_name == "テストアーティスト"
    assert result.preview_url == "http://example.com/t.mp3"
    assert result.audio_path == "/tmp/test.mp3"
    assert result.use_fallback_song is False
    assert result.errors == []


async def test_generate_all_parallel_success(pipeline_mocks):
    pipeline_mocks()
    result = await generate_all_parallel(2020, 5, 15)

    assert isinstance(result, GenerationResult)
    assert result.script == SCRIPT
    assert result.audio_path == "/tmp/test.mp3"
    assert result.errors == []


# --- フォールバック ------------------------------------------------------------
async def test_script_failure_falls_back_to_template(pipeline_mocks):
    pipeline_mocks(script_error=Exception("API Error"))
    result = await generate_all_async(2020, 5, 15)

    from retro_radio.core.fallback import generate_fallback_script

    assert result.script == generate_fallback_script(2020, 5, 15)
    assert "script_fallback" in result.errors
    assert result.song_title == "テスト曲"
    assert result.use_fallback_song is False


async def test_music_failure_falls_back_to_songs(pipeline_mocks):
    pipeline_mocks(songs_error=Exception("API Error"))
    result = await generate_all_async(2020, 5, 15)

    assert result.preview_url is None
    assert result.use_fallback_song is True
    assert "music_fallback_all" in result.errors
    assert result.audio_path == "/tmp/test.mp3"
    assert result.script == SCRIPT


async def test_tts_failure_is_recorded(pipeline_mocks):
    pipeline_mocks(tts_error=Exception("TTS Error"))
    result = await generate_all_async(2020, 5, 15)

    assert result.audio_path is None
    assert "tts_failed" in result.errors
    assert result.script == SCRIPT


async def test_all_three_fail_still_returns_result(pipeline_mocks):
    """全部失敗しても例外ではなく errors を詰めた GenerationResult を返す"""
    pipeline_mocks(
        script_error=Exception("api"), songs_error=Exception("api"), tts_error=Exception("tts")
    )
    result = await generate_all_async(2020, 5, 15)

    assert isinstance(result, GenerationResult)
    assert set(result.errors) == {"script_fallback", "music_fallback_all", "tts_failed"}
    assert result.audio_path is None


async def test_parallel_variant_handles_exceptions(pipeline_mocks):
    """並列版も return_exceptions=True で同じフォールバックを行う"""
    pipeline_mocks(
        script_error=Exception("api"), songs_error=Exception("api"), tts_error=Exception("tts")
    )
    result = await generate_all_parallel(2020, 5, 15)

    assert set(result.errors) == {"script_fallback", "tts_failed"}
    assert result.audio_path is None


# --- 進行状況 ------------------------------------------------------------------
async def test_progress_is_reported(pipeline_mocks):
    from retro_radio.utils.async_runner import AsyncProgress

    pipeline_mocks()
    progress = AsyncProgress()
    await generate_all_async(2020, 5, 15, progress)
    assert progress._current == 100


# --- メタデータ ----------------------------------------------------------------
def test_generation_result_defaults_all_songs():
    result = GenerationResult(
        script="s", song_title="曲", artist_name="歌手", preview_url=None,
        audio_path=None, use_fallback_song=True, errors=[],
    )
    assert result.all_songs == [
        {"trackName": "曲", "artistName": "歌手", "previewUrl": None}
    ]


def test_generation_result_keeps_explicit_all_songs():
    songs = [{"trackName": "A", "artistName": "X", "previewUrl": None}]
    result = GenerationResult(
        script="s", song_title="A", artist_name="X", preview_url=None,
        audio_path=None, use_fallback_song=True, errors=[], all_songs=songs,
    )
    assert result.all_songs is songs
