"""ラジオ番組の構成（オープニング曲 → 原稿 → 曲 → 原稿 → … → エンディング曲）を
守らせる回帰テスト。

実測していた問題:
  - バックエンドはトークから始めていた（オープニング曲が無い）
  - フロントは `preview_url` の無い曲を「無言で落として」いた
    → エンディング曲が構造的に鳴り得なかった
  - iTunes が全滅すると原稿 5 本が連続再生され、番組構造が消えた
  - 「2〜3 周ループ」の概念がどこにも無かった
  - `program_guide`（番組表）が生成されているのに UI に出なかった
  - ON AIR バッジが静的な文字列で、状態を表さなかった
"""

import re
from pathlib import Path

import pytest

from retro_radio import server as server_module
from retro_radio.config import get_settings
from retro_radio.core.fallback import (
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
)
from retro_radio.core.script_generator import parse_script_segments
from retro_radio.models.radio import ScriptSegment

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"
INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"
APP_CSS = Path(__file__).resolve().parents[1] / "static" / "app.css"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ==============================================================================
# 1. バックエンド: 番組は曲で始まり曲で終わる
# ==============================================================================
def _segment(index: int, title: str) -> ScriptSegment:
    return ScriptSegment(
        id=f"seg{index}",
        title=title,
        content="テスト原稿です。",
        estimated_duration=10.0,
        order=index,
    )


def _titles(count: int) -> list:
    return ["オープニング", *[f"トーク{i}" for i in range(1, count - 1)], "エンディング"]


def _songs(count: int, playable: bool = True) -> list:
    return [
        {
            "title": f"曲{i}",
            "artist": f"アーティスト{i}",
            "preview_url": f"https://example.test/{i}.m4a" if playable else None,
            "artwork_url": None,
            "is_fallback": not playable,
        }
        for i in range(count)
    ]


def test_playlist_opens_with_the_opening_theme():
    """番組の最初の一音がテーマ曲（司会の声ではない）"""
    segments = [_segment(i, t) for i, t in enumerate(_titles(5))]
    playlist = server_module.build_playlist(segments, _songs(6), year=1975)

    assert playlist[0]["type"] == "song"
    assert playlist[1]["type"] == "talk"
    assert playlist[1]["title"] == "オープニング"


def test_playlist_closes_with_the_ending_theme():
    """番組の最後の一音もテーマ曲（挨拶で無音に終わらない）"""
    segments = [_segment(i, t) for i, t in enumerate(_titles(5))]
    playlist = server_module.build_playlist(segments, _songs(6), year=1975)

    assert playlist[-1]["type"] == "song"
    assert playlist[-2]["type"] == "talk"
    assert playlist[-2]["title"] == "エンディング"


@pytest.mark.parametrize("song_count", [0, 1, 2, 3, 6, 10])
def test_playlist_never_ends_with_two_talks(song_count):
    """曲が何本でもトーク同士が連続しない（曲不足分は FALLBACK で埋める）"""
    segments = [_segment(i, t) for i, t in enumerate(_titles(5))]
    playlist = server_module.build_playlist(segments, _songs(song_count), year=1975)
    types = [item["type"] for item in playlist]

    assert types[0] == "song", types
    assert not any(types[i] == types[i + 1] == "talk" for i in range(len(types) - 1)), types
    assert types.count("talk") == 5, types


def test_talk_items_carry_segment_index():
    """トーク要素が原稿セグメント番号を持つ（原稿のハイライト用）"""
    segments = [_segment(i, t) for i, t in enumerate(_titles(5))]
    for i, seg in enumerate(segments):
        seg.metadata = {"audio_url": f"/api/audio/t{i}.mp3"}
    playlist = server_module.build_playlist(segments, _songs(6), year=1975)

    indexes = [i["metadata"]["segment_index"] for i in playlist if i["type"] == "talk"]
    assert indexes == [0, 1, 2, 3, 4], indexes


def test_talk_item_does_not_mutate_caller_segment_metadata():
    """`_talk_item` が segment.metadata を直接汚さない"""
    segment = _segment(0, "オープニング")
    segment.metadata = {"audio_url": "/api/audio/t0.mp3"}
    server_module._talk_item(segment)

    assert "segment_index" not in segment.metadata


# ==============================================================================
# 2. 曲数: プレビュー音源をトーク数ぶん確保する
# ==============================================================================
def test_playlist_gets_more_candidates_than_medley_count():
    """1 パスの番組在实际に流す曲の下限

    トーク 5 に対して曲スロットが 6 必要なので、`medley_song_count`(3) だけだと
    先頭と末尾のテーマ曲が無音になる。
    """
    settings = get_settings()
    assert settings.program_min_song_count > 1
    assert settings.program_min_song_count >= settings.medley_song_count


def test_program_loop_count_is_within_range():
    """既定の周回数はラジオ番組として妥当な範囲（1〜5）"""
    settings = get_settings()
    assert 1 <= settings.program_loop_count <= 5


def test_api_returns_loop_count(client):
    """応答が推奨周回数を持つ"""
    data = client.post(
        "/api/generate", json={"year": 1975, "month": 9, "day": 24, "mode": "normal"}
    ).json()
    assert data["loop_count"] == get_settings().program_loop_count


# ==============================================================================
# 3. 原稿: オープニング / エンディングに「曲|delivers」を置かない
# ==============================================================================
@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(generate_fallback_script, id="normal"),
        pytest.param(generate_care_script, id="care_recreation"),
        pytest.param(
            lambda y, m, d: generate_anniversary_script(y, m, d, "花子"), id="anniversary"
        ),
    ],
)
@pytest.mark.parametrize("year", [1955, 1975, 1995, 2025])
def test_opening_and_ending_have_no_song_cue(builder, year):
    """テーマ曲が既に鳴っているセグメントで「曲をお届けします」と言わない

    旧実装はオープニングの最後に「それでは、オープニングの曲をお届けします」と
    言っていたが、build_playlist は 1 曲目を不移にテーマ曲として使うため、
    司会が既に鳴った曲をこれから鳴らすと誤解する矛盾が生じていた。
    """
    segments = parse_script_segments(builder(year, 5, 15))
    assert segments, "原稿がパースできない"

    opening = next(s for s in segments if "オープニング" in s.title)
    ending = next(s for s in segments if "エンディング" in s.title)
    for segment in (opening, ending):
        assert "オープニングの曲" not in segment.content, segment.title
        assert "エンディングの曲でお別れ" not in segment.content, segment.title


@pytest.mark.parametrize("year", [1955, 1975, 1995, 2025])
def test_middle_talks_still_announce_songs(year):
    """中間のトークは曲の存在を告げる（曲への手がかりが消えていない）"""
    script = generate_fallback_script(year, 5, 15)
    assert "この年のヒット曲をお届けします" in script
    assert "懐かしい一曲をお届けします" in script


# ==============================================================================
# 4. フロント: 音源が無い曲を「落とさない」
# ==============================================================================
def test_frontend_no_longer_drops_songs_without_preview(app_js):
    """`if (!song.preview_url) { return; }` という無言ドロップが無い"""
    assert "!song.preview_url) { return; }" not in app_js
    assert "var INTERMISSION = 'INTERMISSION';" in app_js


def test_frontend_turns_missing_song_into_intermission(app_js):
    """音源が無い曲スロットは間奏（無音トラック）になる"""
    assert "kind: INTERMISSION" in app_js
    assert "getSilenceUrl(SILENCE_SLOT_SECONDS)" in app_js


def test_frontend_keeps_playlist_order(app_js):
    """`buildPass` が playlist を 1 パスとして順にたどる（順序を入れ替えない）

    曲スロットを別配列に集めるのは「実際に鳴らせる曲の一覧」を作るためだけにし、
    並べるのは playlist をそのまま walk した 2 巡目。
    """
    build_pass = app_js[app_js.index("function buildPass"):app_js.index("function buildQueue(data)")]
    # 曲スロットを別に集めるだけで、並べるのは playlist をそのまま walk した 2 巡目
    assert "playable = [];" in build_pass
    assert build_pass.index("--- 2 巡目") > build_pass.index("--- 1 巡目")


def test_frontend_never_produces_two_talks_in_a_row(app_js):
    """音源が全滅してもトークが連続しない（間奏で必ず挟む）"""
    build_pass = app_js[app_js.index("function buildPass"):app_js.index("function buildQueue(data)")]
    # SONG / INTERMISSION を必ず pass へ push している
    assert "pass.push({ kind: SONG" in build_pass
    assert "kind: INTERMISSION," in build_pass


# ==============================================================================
# 5. フロント: 2〜3 周ループ
# ==============================================================================
def test_frontend_expands_queue_by_repeat_count(app_js):
    """キューは周回数ぶん繰り返される"""
    assert "function effectiveRepeatCount" in app_js
    assert "var repeats = effectiveRepeatCount();" in app_js
    assert "track.passTotal = repeats;" in app_js


def test_frontend_repeat_is_on_by_default(app_js):
    """既定で「2〜3 周」成立（連続 OFF が既定では無音 1 周にならない）"""
    assert "var DEFAULT_REPEAT = 3;" in app_js
    assert "readStore(LOOP_STORAGE_KEY, '1') === '1'" in app_js


def test_repeat_control_exists_in_markup():
    """周回数を操作できるボタンが UI にある"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="btnRepeat"' in html
    assert 'id="btnLoop"' in html


def test_playlist_marks_pass_boundaries(app_js):
    """プレイリストに周目の区切りが出る"""
    assert "function buildPassDivider" in app_js
    assert "track.isFirstInPass" in app_js
    assert "playlist-pass-divider" in app_js


# ==============================================================================
# 6. フロント: 継ぎ目（クロスフェード）
# ==============================================================================
def test_frontend_has_two_audio_elements(app_js):
    """2 本の <audio> を使う（1 本の src 差し替えでは無音が挟まる）"""
    assert "slots: { a: null, b: null }," in app_js
    assert "function createAudioPair" in app_js


def test_frontend_crossfades_at_track_boundary(app_js):
    """残り時間が切れたら次のトラックを音量 0 で鳴らし始める"""
    assert "var XFADE_MS = 420;" in app_js
    assert "var XFADE_PREROLL_SECONDS = 0.7;" in app_js
    assert "function maybeStartXfade" in app_js
    assert "function startXfade" in app_js
    # timeupdate を経由してクロスフェードが発火する
    assert "maybeStartXfade();" in app_js


def test_frontend_trims_talk_leading_silence(app_js):
    """読み上げ音声の冒頭の無音を削る"""
    assert "var TALK_LEAD_TRIM_SECONDS = 0.18;" in app_js
    assert "function onAudioLoadedMetadata" in app_js


# ==============================================================================
# 7. フロント: 読み上げ中の原稿ハイライト
# ==============================================================================
def test_frontend_renders_manuscript_as_segment_blocks(app_js):
    """原稿は 1 セグメント = 1 <section> で描画される"""
    assert "function renderManuscriptHtml" in app_js
    assert 'class="script-block" data-segment-index="' in app_js


def test_frontend_highlights_current_segment(app_js):
    """再生中の原稿セグメントを起こす"""
    assert "function highlightManuscript" in app_js
    assert "function clearManuscriptHighlight" in app_js
    assert "classList.add('is-now-reading')" in app_js
    # トーク再生時に必ず呼ばれる
    assert "highlightManuscript(track);" in app_js


def test_manuscript_highlight_has_styles():
    """ハイライトの見た目が定義されている"""
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".script-block.is-now-reading" in css


# ==============================================================================
# 8. フロント: 番組表（program_guide）を出す
# ==============================================================================
def test_frontend_renders_program_guide(app_js):
    """バックエンドが返す program_guide を UI に出す"""
    assert "function renderProgramGuide" in app_js
    assert "renderProgramGuide(data.program_guide);" in app_js


def test_program_guide_container_exists():
    """番組表の表示先がある"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="programGuide"' in html


# ==============================================================================
# 9. フロント: ON AIR バッジが動的
# ==============================================================================
def test_on_air_badge_is_not_hardcoded(app_js):
    """ON AIR が静的な文字列ではない（状態で切り替わる）"""
    assert "function setOnAir" in app_js
    for label in ("ON_AIR_LIVE", "ON_AIR_READY", "ON_AIR_ENDED"):
        assert "var %s =" % label in app_js


@pytest.mark.parametrize(
    "caller",
    [
        "setOnAir(ON_AIR_LIVE);",
        "setOnAir(ON_AIR_ENDED);",
        "setOnAir(ON_AIR_READY);",
    ],
)
def test_on_air_badge_reflects_playback_state(app_js, caller):
    """待機中 / 放送中 / 終了の各状態でバッジが更新される"""
    assert caller in app_js


def test_on_air_badge_markup_is_not_live_by_default():
    """HTML 上は「待機中」から始める（放送していないのに ON AIR と出さない）"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="onAirBadge"' in html
    badge = re.search(r'id="onAirBadge"[^>]*>([^<]*)<', html)
    assert badge is not None
    assert "ON AIR" not in badge.group(1)


# ==============================================================================
# 健全性: JS / CSS に残骸が無い
# ==============================================================================
def test_no_stale_helper_references(app_js):
    """削除した関数の参照が残っていない"""
    assert "replaceAudioElement(" not in app_js
    assert "renderScriptHtml(" not in app_js
