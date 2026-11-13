"""後方互換性の回帰テスト。

Wave 1 で外部 API（`select_song` の戻り値）が変わった箇所を、
現在の仕様を正として固定する。
"""

import pytest

from retro_radio.core.fallback import (
    FALLBACK_SONGS,
    get_fallback_song,
    get_fallback_songs,
    get_reminiscence_quiz,
)
from retro_radio.core.music_search import search_itunes_songs, select_song, select_songs
from retro_radio.core.songs import load_songs


SONG = {"trackName": "テスト", "artistName": "歌手", "previewUrl": "http://x.mp3"}


def test_legacy_select_song_returns_five_tuple():
    """`select_song` は (title, artist, url, fallback, artwork) の5要素を返す"""
    title, artist, url, fallback, artwork = select_song(1980, [SONG])
    assert title == "テスト"
    assert artist == "歌手"
    assert url == "http://x.mp3"
    assert fallback is False
    assert artwork is None


def test_legacy_select_song_marks_missing_preview_as_fallback():
    result = select_song(1980, [{"trackName": "曲", "artistName": "歌手", "previewUrl": None}])
    assert result[3] is True
    assert result[2] is None


def test_legacy_select_song_with_empty_list_uses_fallback():
    title, artist, url, fallback, artwork = select_song(1980, [])
    assert fallback is True
    assert url is None
    assert title and artist


def test_fallback_song_comes_from_master_data():
    """フォールバック曲は必ず静的マスター（FALLBACK_SONGS）から選ばれる"""
    for _ in range(20):
        title, artist = get_fallback_song(1960)
        assert ("上を向いて歩こう", "坂本九") == (title, artist) or (
            title, artist
        ) in FALLBACK_SONGS[1960]


def test_get_fallback_songs_returns_requested_count():
    for year in (1950, 1965, 1980, 1995, 2010):
        songs = get_fallback_songs(year, count=3)
        assert len(songs) == 3
        assert all(isinstance(s, tuple) and len(s) == 2 for s in songs)


def test_get_reminiscence_quiz_shape():
    quiz = get_reminiscence_quiz(1965)
    assert isinstance(quiz, list) and quiz
    for item in quiz:
        assert {"question", "answer", "hint"} <= set(item)


@pytest.mark.network
def test_search_itunes_songs_returns_list(mock_itunes):
    """実ネットワークを使うテストは `network` マーカー付き（既定では skip される）"""
    mock_itunes.hit(3)
    songs = search_itunes_songs(1980)
    assert isinstance(songs, list)


def test_search_itunes_songs_keeps_catalog_songs_when_no_hit(mock_itunes):
    """ヒット0件のときは**空リストを返さず**、正本カタログの曲をそのまま返す

    旧契約は「空リストを返し、補完は `select_songs` が担う」だったが、
    音源が無い曲を脱落させると 1 パスの曲スロット（トーク数 + 1）が埋まらず、
    トークが連続して**番組の骨組みが崩れる**。そのため契約を変更し、
    `previewUrl: None` のまま返す（フロントが間奏として扱う）。

    ただし「捏造」は禁止。返すレコードはすべて正本カタログに実在する。
    """
    from retro_radio.core.songs import load_songs

    mock_itunes.empty()
    result = search_itunes_songs(1980, count=3)

    assert isinstance(result, list)
    assert result, "音源が無いだけで曲リストを空にしてはいけない"
    # 音源を捏造しない
    assert all(song.get("previewUrl") is None for song in result), result
    # かつ、すべて正本カタログに実在する曲である
    known = {str(item.get("title")) for item in (load_songs() or [])}
    for song in result:
        assert str(song.get("trackName")) in known, song


def test_select_songs_does_not_fabricate_itunes_records():
    """select_songs は iTunes のレスポンスを捏造せず、正本の曲を使う

    補充に使う曲ソースは「静的マスター → 正本カタログ」の順に広がる。
    （静的マスター 1 バケット 4 曲では対象年の曲になりきれず、
    後年の曲を名前で呼ぶ=:doc:`facts_registry` の誤認になるため、
    ``fallback.select_program_songs`` が先に正本カタログを見る。）
    したがって「捏造していない」の基準は**正本カタログ**であり、
    静的マスター由来の曲も正本に含まれる。
    """
    result = select_songs(1980, [], count=3)
    assert len(result) == 3
    assert all(song["previewUrl"] is None for song in result)
    known = {
        (str(item.get("title")), str(item.get("artist")))
        for item in (load_songs() or [])
    }
    known |= {(t, a) for songs in FALLBACK_SONGS.values() for t, a in songs}
    for song in result:
        assert (song["trackName"], song["artistName"]) in known, song


def test_select_songs_prefers_records_with_preview():
    songs = [dict(SONG, trackName=f"曲{i}", previewUrl=f"http://x/{i}.mp3") for i in range(5)]
    result = select_songs(1980, songs, 3)
    assert len(result) == 3
    assert all(song["previewUrl"] for song in result)
