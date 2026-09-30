from retro_radio.core.music_search import select_songs

def test_select_songs_with_preview():
    """プレビューあり曲から3曲選択"""
    songs = [
        {"trackName": "曲1", "artistName": "歌手1", "previewUrl": "http://a.mp3"},
        {"trackName": "曲2", "artistName": "歌手2", "previewUrl": "http://b.mp3"},
        {"trackName": "曲3", "artistName": "歌手3", "previewUrl": "http://c.mp3"},
        {"trackName": "曲4", "artistName": "歌手4", "previewUrl": "http://d.mp3"},
    ]
    result = select_songs(1980, songs, 3)
    assert len(result) == 3
    assert all(s.get("previewUrl") for s in result)

def test_select_songs_fallback_supplement():
    """プレビュー不足時にフォールバック補完"""
    songs = [{"trackName": "曲1", "artistName": "歌手1", "previewUrl": "http://a.mp3"}]
    result = select_songs(1980, songs, 3)
    assert len(result) == 3
    assert result[0]["previewUrl"] is not None
    # 2,3曲目はフォールバック（previewUrl=None）

def test_select_songs_empty():
    """検索結果ゼロでもフォールバックで3曲"""
    result = select_songs(1980, [], 3)
    assert len(result) == 3
    assert all(s.get("previewUrl") is None for s in result)
