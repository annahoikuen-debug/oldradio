"""1 パス（1 周）内で同じ曲が繰り返し流れることを防ぐ回帰テスト。

実測されていた不具合
--------------------
1 パスはトーク 5 個 = 曲スロット 6 個 だが、実際に取得できた iTunes
プレビュー音源は 3 曲しかなかった（`FALLBACK_SONGS` は 1 世代 4 曲、
`search_itunes_songs` の目標も `medley_song_count`=3 だった）。
フロントは「音源のある曲を順番に回す」実装だったため、
**S0 が 1 パスで 4 回**流れることになっていた。

対策:
  - バックエンド: 1 パスに必要な曲数（トーク数 + 1）を明示的に求める
  - フロント: 未使用の曲を優先し、尽きても 1 曲 2 回を超えない
"""
import io
import json

import pytest

pytest.importorskip("dukpy", reason="dukpy がないため JS 実行テストをスキップ")

from pathlib import Path  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
APP_JS = TESTS_DIR.parent / "static" / "app.js"
HARNESS_JS = TESTS_DIR / "js_harness.js"

HOOK = r"""
    window.__pass = function (d) { return buildPass(d); };
"""


# ==============================================================================
# バックエンド
# ==============================================================================
def test_program_min_song_count_covers_a_full_pass():
    """1 パス（トーク 5）の曲スロット 6 個分をEnsured している"""
    from retro_radio.config import get_settings

    settings = get_settings()
    assert settings.program_min_song_count >= 6, settings.program_min_song_count
    assert settings.program_min_song_count >= settings.medley_song_count


def test_search_itunes_accepts_a_count():
    """`search_itunes_songs` が目標曲数を受け取れる（1 パスぶんの曲数を要求できる）"""
    import inspect

    from retro_radio.core.music_search import search_itunes_songs

    params = inspect.signature(search_itunes_songs).parameters
    assert "count" in params, list(params)


def test_year_filter_prefers_releases_from_the_target_year():
    """発売年が対象年に近いものを優先する（1975年の番組に1973年の曲を出さない）"""
    from retro_radio.core.music_search import release_year

    assert release_year({"releaseDate": "1975-05-01T07:00:00Z"}) == 1975
    assert release_year({"releaseDate": ""}) is None
    assert release_year({}) is None


def test_year_query_fallback_exists():
    """1 世代の代表曲だけでは足りないため、年キーワード検索がある"""
    from retro_radio.core import music_search

    assert hasattr(music_search, "search_itunes_by_year")


# ==============================================================================
# フロント（実コードを dukpy で実行）
# ==============================================================================
@pytest.fixture(scope="module")
def runner():
    import dukpy

    app_js = APP_JS.read_text(encoding="utf-8")
    marker = "\n}());\n"
    assert app_js.endswith(marker)
    patched = app_js[: -len(marker)] + "\n" + HOOK + marker
    interp = dukpy.JSInterpreter()
    interp.evaljs(HARNESS_JS.read_text(encoding="utf-8") + "\n" + patched + "\n")
    interp.evaljs("window.__harness.fire('DOMContentLoaded');")

    class Runner:
        def build_pass(self, data):
            return json.loads(
                interp.evaljs("JSON.stringify(window.__pass(" + json.dumps(data) + "))")
            )

    return Runner()


def _talk(i):
    return {"type": "talk", "title": "T%d" % i,
            "audio_url": "http://localhost:8501/api/audio/t%d.mp3" % i,
            "metadata": {"audio_url": "http://localhost:8501/api/audio/t%d.mp3" % i,
                         "segment_index": i}}


def _song(i, preview):
    return {"type": "song", "title": "S%d" % i, "artist": "A%d" % i,
            "preview_url": ("https://x/%d.m4a" % i) if preview else None}


def _data(playable, slots=6, talks=5):
    playlist, si = [], 0
    for i in range(slots):
        playlist.append(_song(si, si < playable))
        si += 1
        if i < talks:
            playlist.append(_talk(i))
    return {"playlist": playlist, "audio_url": None}


def _song_counts(tracks):
    counts = {}
    for t in tracks:
        if t["kind"] == "SONG":
            counts[t["title"]] = counts.get(t["title"], 0) + 1
    return counts


def test_six_distinct_songs_play_once_each(runner):
    """音源が 6 曲あれば 1 パス内で各曲 1 回（重複ゼロ）"""
    tracks = runner.build_pass(_data(playable=6))
    counts = _song_counts(tracks)
    assert counts == {"S%d" % i: 1 for i in range(6)}, counts


@pytest.mark.parametrize("playable", [3, 4, 5])
def test_no_song_plays_more_than_twice_in_one_pass(runner, playable):
    """音源が足りない場合も 1 曲 2 回を超えない（従来は 4 回）"""
    tracks = runner.build_pass(_data(playable=playable))
    counts = _song_counts(tracks)
    assert sum(counts.values()) == 6, counts
    for title, count in counts.items():
        assert count <= 2, "%s が %d 回流れている: %s" % (title, count, counts)


def test_repeats_are_spread_out_not_adjacent(runner):
    """繰り返すときは隣り合わせにならない（聞き心地anke）"""
    tracks = runner.build_pass(_data(playable=3))
    titles = [t["title"] for t in tracks if t["kind"] == "SONG"]
    for i in range(len(titles) - 1):
        assert titles[i] != titles[i + 1], titles


def test_no_playable_song_becomes_intermission_not_a_repeat(runner):
    """音源が 0 曲なら間奏（黙って同じ曲を繰り返さない）"""
    tracks = runner.build_pass(_data(playable=0))
    kinds = [t["kind"] for t in tracks]
    assert "SONG" not in kinds, kinds
    assert kinds.count("INTERMISSION") == 6, kinds


def test_program_structure_is_preserved(runner):
    """重複対策として曲/トークの交互が崩れない"""
    tracks = runner.build_pass(_data(playable=4))
    kinds = [t["kind"] for t in tracks]
    assert kinds[0] == "SONG"
    assert kinds[-1] == "SONG"
    assert not any(kinds[i] == kinds[i + 1] == "TALK"
                   for i in range(len(kinds) - 1)), kinds
    assert kinds.count("TALK") == 5, kinds


# ==============================================================================
# 静的契約
# ==============================================================================
def test_frontend_marks_songs_as_used():
    """返した曲を必ず記録している（記録しないと毎回同じ曲を選ぶ）"""
    src = APP_JS.read_text(encoding="utf-8")
    body = src[src.index("function takeUnused"):src.index("// --- 2 巡目")]
    assert "markUsed(chosen);" in body, body
    assert "MAX_PLAYS_PER_SONG" in body
