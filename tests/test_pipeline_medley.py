import pytest
from retro_radio.core.pipeline import generate_all_async, GenerationResult

@pytest.mark.asyncio
async def test_generation_result_has_all_songs(mock_gtts, mock_itunes):
    """GenerationResultに全曲情報が含まれるか"""
    result = await generate_all_async(1980, 5, 15)
    assert isinstance(result, GenerationResult)
    assert hasattr(result, 'all_songs')
    assert isinstance(result.all_songs, list)
    assert len(result.all_songs) == 3
    assert all('trackName' in s for s in result.all_songs)
    assert all('artistName' in s for s in result.all_songs)

@pytest.mark.asyncio
async def test_first_song_matches_legacy_fields(mock_gtts, mock_itunes):
    """第1曲目がレガシーフィールドと一致"""
    result = await generate_all_async(1980, 5, 15)
    assert result.song_title == result.all_songs[0].get("trackName")
    assert result.artist_name == result.all_songs[0].get("artistName")
    assert result.preview_url == result.all_songs[0].get("previewUrl")
